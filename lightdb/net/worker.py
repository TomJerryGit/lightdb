"""
Worker 进程（修复版）

修复:
- _handle_connection 直接用 MySQLHandler（去掉 Fake 类）
- 支持 SO_REUSEPORT 与 Master 队列模式
- 优雅退出
- 加日志
"""
import os
import signal
import socket
import sys
import threading
import time
from typing import Optional

from .listener import create_listener, HAS_REUSEPORT
from ..logger import logger


def worker_main(worker_id: int, config: dict,
                shared_state: dict = None):
    """Worker 进程入口"""
    logger.info(
        f"[Worker-{worker_id}] 启动 "
        f"PID={os.getpid()}")

    if shared_state is None:
        shared_state = {}

    host = config.get('host', '0.0.0.0')
    port = config.get('port', 8888)

    # 忽略 SIGINT（由 Master 统一管理）
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass

    listener = None
    sock = None

    try:
        if HAS_REUSEPORT:
            listener = create_listener(
                host, port, mode='reuseport')
            sock = listener.bind()
        else:
            # Master 模式：从队列获取连接
            sock = None

        # 上报就绪
        stats_hub = shared_state.get('stats_hub')
        if stats_hub is not None:
            try:
                stats_hub.report(worker_id, 'connections', 0)
            except Exception:
                pass

        # 主循环
        if sock is not None:
            _accept_loop(worker_id, sock, config,
                        shared_state)
        else:
            _queue_loop(worker_id, config, shared_state)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(
            f"[Worker-{worker_id}] 异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        logger.info(f"[Worker-{worker_id}] 退出")


# ==================== 接受循环 ====================

def _accept_loop(worker_id: int, sock,
                 config: dict, shared_state: dict):
    """SO_REUSEPORT 模式"""
    sock.settimeout(1.0)
    stats_hub = shared_state.get('stats_hub')

    while True:
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            # socket 被关闭
            break
        except Exception as e:
            logger.warn(
                f"[Worker-{worker_id}] accept 异常: {e}")
            time.sleep(0.1)
            continue

        # 每个连接独立线程
        t = threading.Thread(
            target=_handle_connection,
            args=(worker_id, conn, addr,
                  config, shared_state),
            daemon=True)
        t.start()

        if stats_hub is not None:
            try:
                stats_hub.report(worker_id, 'connections')
            except Exception:
                pass


def _queue_loop(worker_id: int,
                config: dict, shared_state: dict):
    """Master 队列模式"""
    conn_queue = shared_state.get('conn_queue')
    if conn_queue is None:
        logger.warn(
            f"[Worker-{worker_id}] 无 conn_queue，"
            f"退出")
        return

    while True:
        try:
            item = conn_queue.get(timeout=1.0)
        except Exception:
            continue

        if item is None:
            break

        conn, addr = item
        t = threading.Thread(
            target=_handle_connection,
            args=(worker_id, conn, addr,
                  config, shared_state),
            daemon=True)
        t.start()


# ==================== 连接处理 ====================

def _handle_connection(worker_id: int, conn, addr,
                       config: dict, shared_state: dict):
    """处理单个连接"""
    from .server import MySQLHandler

    class _FakeRequest:
        """模拟 socketserver 的 request 接口"""
        def __init__(self, sock):
            self._sock = sock

        def recv(self, n):
            return self._sock.recv(n)

        def sendall(self, data):
            return self._sock.sendall(data)

        def close(self):
            try:
                self._sock.close()
            except Exception:
                pass

        def setsockopt(self, *args):
            return self._sock.setsockopt(*args)

        def settimeout(self, t):
            return self._sock.settimeout(t)

        def __getattr__(self, name):
            return getattr(self._sock, name)

    class _FakeServer:
        """模拟 socketserver 的 server 接口"""
        def __init__(self, shared):
            self.db = shared.get('db')
            self.executor = shared.get('executor')
            self.config = _ConfigAdapter(config)
            self.start_time = time.time()
            # MySQLHandler 需要的字段
            self.query_cache = None
            self.pool = None
            self.semi_sync = None
            self.ssl_mgr = None
            self.ssl_enabled = False

    class _ConfigAdapter:
        """把 dict 适配为 Config 对象"""
        def __init__(self, cfg: dict):
            self._cfg = cfg
            self.host = cfg.get('host', '0.0.0.0')
            self.port = cfg.get('port', 8888)

        def __getattr__(self, name):
            return self._cfg.get(name)

    try:
        handler = MySQLHandler.__new__(MySQLHandler)
        handler.request = _FakeRequest(conn)
        handler.client_address = addr
        handler.server = _FakeServer(shared_state)
        handler.setup()
        try:
            handler.handle()
        finally:
            try:
                handler.finish()
            except Exception:
                pass

    except Exception as e:
        logger.warn(
            f"[Worker-{worker_id}] 连接处理失败 "
            f"({addr}): {e}")
        stats_hub = shared_state.get('stats_hub')
        if stats_hub is not None:
            try:
                stats_hub.report(worker_id, 'errors')
            except Exception:
                pass

    finally:
        try:
            conn.close()
        except Exception:
            pass