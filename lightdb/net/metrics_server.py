"""内嵌 HTTP 指标端点：``GET /metrics``（Prometheus text）与 ``/health``

为什么内嵌在 ``lightdb/net`` 而不是只用 web 版：
  * 数据库进程独立运行时（不启动 Web 管理端）也要能被 Prometheus 抓取；
  * 端口/地址用环境变量配置，随 ``MySQLServer`` 生命周期启停；
  * 不新增 Config 字段（本任务范围不含 config.py）。

启动条件：``LIGHTDB_METRICS_PORT``
  * **未设置**（或负数）→ 不启动（生产默认关闭，显式开启）；
  * **0** → 由 OS 分配空闲端口（动态端口；实际端口见
    ``MetricsHTTPServer.server_address[1]`` / ``MySQLServer.metrics_url``）；
  * **>0** → 使用该固定端口。
监听地址：``LIGHTDB_METRICS_HOST``（默认 127.0.0.1）。
端点：
  ``GET /metrics``               Prometheus text（默认）
  ``GET /metrics?format=json``   结构化 JSON（排障/测试）
  ``GET /health``                ``ok``（存活探针）
  其它路径 → 404；非 GET → 405。

安全边界：默认只监听回环地址；端点**不含**任何 SQL 文本之外的凭据（慢查询
日志已对口令字面量脱敏，见 `observability.redact_sql`）。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..logger import logger
from .observability import (
    metrics_snapshot_json, render_prometheus, replication_status_payload)


class _MetricsHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LightDBMetrics/1.0'

    #: 由 `MetricsHTTPServer` 注入
    mysql_server = None

    def log_message(self, fmt, *args):           # noqa: D401 - 静音访问日志
        # 指标端点会被 Prometheus 高频抓取，默认访问日志会刷屏；只保留告警。
        return

    def _write(self, code: int, body: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def do_GET(self):                            # noqa: N802 - BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        path = parsed.path
        server = self.mysql_server
        if path == '/health':
            self._write(200, b'ok\n', 'text/plain; charset=utf-8')
            return
        if path in ('/metrics', '/metrics/'):
            if server is None:
                self._write(503, b'# metrics server not attached\n',
                            'text/plain; charset=utf-8')
                return
            query = parse_qs(parsed.query or '')
            fmt = (query.get('format') or [''])[0].lower()
            if fmt == 'json':
                body = json.dumps(metrics_snapshot_json(server),
                                  ensure_ascii=False, default=str).encode()
                self._write(200, body, 'application/json; charset=utf-8')
                return
            try:
                body = render_prometheus(server).encode('utf-8')
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"渲染 /metrics 失败: {e}")
                self._write(500, f'# metrics render error: {e}\n'.encode(),
                            'text/plain; charset=utf-8')
                return
            self._write(200, body,
                        'text/plain; version=0.0.4; charset=utf-8')
            return
        # ★ task-91：只读复制延迟面板数据（多副本位点/延迟/错误/采样历史）
        if path in ('/replication/status', '/replication/status/'):
            if server is None:
                self._write(503, b'{"error":"metrics server not attached"}',
                            'application/json; charset=utf-8')
                return
            query = parse_qs(parsed.query or '')
            try:
                limit = int((query.get('history_limit') or ['60'])[0])
            except (TypeError, ValueError):
                limit = 60
            try:
                payload = replication_status_payload(
                    server, history_limit=max(0, min(1000, limit)))
                body = json.dumps(payload, ensure_ascii=False,
                                  default=str).encode('utf-8')
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"生成 /replication/status 失败: {e}")
                body = json.dumps(
                    {'schema': 'lightdb-replication-status/1',
                     'empty': True, 'count': 0,
                     'error': f'状态生成失败: {e}'},
                    ensure_ascii=False).encode('utf-8')
            self._write(200, body, 'application/json; charset=utf-8')
            return
        self._write(404, b'# not found\n', 'text/plain; charset=utf-8')

    def do_HEAD(self):                           # noqa: N802
        self.do_GET()

    def _method_not_allowed(self):
        # Windows 上若未读取请求体就关闭连接，内核会回 RST，客户端看到的是
        # ConnectionAbortedError 而不是 405 响应（CI 高负载下必现）。先把
        # 请求体读掉（限长），再写 405。
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        try:
            if length > 0:
                self.rfile.read(min(length, 1024 * 1024))
            elif str(self.headers.get('Transfer-Encoding', '')).lower() == 'chunked':
                # 简单 drain：读到空行/终止块为止（限 1MB、最多 1024 块）
                total = 0
                while total < 1024 * 1024:
                    line = self.rfile.readline(8192)
                    if not line:
                        break
                    try:
                        size = int(line.strip().split(b';')[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        self.rfile.readline(8192)      # 末尾空行
                        break
                    self.rfile.read(size)
                    self.rfile.readline(8192)          # 块后 CRLF
                    total += size
        except Exception:                          # pragma: no cover - 防御
            pass
        self._write(405, b'# method not allowed\n',
                    'text/plain; charset=utf-8')

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


class MetricsHTTPServer:
    """内嵌指标 HTTP 服务（线程）；随 MySQLServer 启停

    ``port=0`` 表示由 OS 分配空闲端口；**实际监听端口**以
    ``server_address[1]``（等价于 ``self.port``）为准，测试/调用方据此读取，
    不能假设就是传入的 0。
    """

    def __init__(self, mysql_server, host: str = '127.0.0.1', port: int = 0):
        self.mysql_server = mysql_server
        handler = type('BoundMetricsHandler', (_MetricsHandler,),
                       {'mysql_server': mysql_server})
        self._httpd = ThreadingHTTPServer((host, int(port)), handler)
        self._httpd.daemon_threads = True
        self._thread = None
        self._closed = False
        self.host = host
        #: 真实监听端口（port=0 时由 OS 分配，不是 0）
        self.port = self._httpd.server_address[1]

    @property
    def server_address(self):
        """底层 socket 的真实 (host, port)（动态端口以这里为准）"""
        return tuple(self._httpd.server_address)

    @property
    def is_running(self) -> bool:
        return (not self._closed and self._thread is not None
                and self._thread.is_alive())

    @property
    def url(self) -> str:
        return f'http://{self.host}:{self.port}'

    def start(self):
        if self._closed:
            raise RuntimeError('MetricsHTTPServer 已关闭，不能复用')
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={'poll_interval': 0.1},
            name='lightdb-metrics-http', daemon=True)
        self._thread.start()
        logger.info(f"指标端点已启动: {self.url}/metrics "
                    f"(server_address={self.server_address})")
        return self

    def stop(self):
        """关闭监听 socket 并 join 线程（幂等；返回后端口已释放）"""
        if self._closed:
            return
        self._closed = True
        try:
            self._httpd.shutdown()
        except Exception:                         # pragma: no cover - 防御
            pass
        try:
            self._httpd.server_close()
        except Exception:                         # pragma: no cover - 防御
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def maybe_start_metrics(mysql_server) -> 'MetricsHTTPServer':
    """按环境变量启动指标端点；未配置/启动失败时返回 None（不阻断 DB 启动）

    * ``LIGHTDB_METRICS_PORT`` **未设置或为空** → 不启动（默认关闭）；
    * ``=0`` → OS 分配空闲端口（动态端口，实际端口见返回值的
      ``server_address[1]``）；
    * ``>0`` → 固定端口；``<0`` → 显式关闭；
    * 值非法 → 告警后按关闭处理（不阻断数据库启动）；
    * 启动失败（端口占用等）只告警，数据库继续服务 —— 指标不可用不能
      反过来让数据库起不来，但日志必须明确。
    """
    raw = os.environ.get('LIGHTDB_METRICS_PORT')
    if raw is None or not str(raw).strip():
        return None
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warn(
            f"LIGHTDB_METRICS_PORT={raw!r} 不是合法整数：指标端点不启动")
        return None
    if port < 0:
        return None
    host = os.environ.get('LIGHTDB_METRICS_HOST') or '127.0.0.1'
    try:
        srv = MetricsHTTPServer(mysql_server, host=host, port=port)
        return srv.start()
    except Exception as e:                        # pragma: no cover - 环境差异
        logger.warn(
            f"指标端点启动失败（{host}:{port}），数据库继续运行: {e}")
        return None
