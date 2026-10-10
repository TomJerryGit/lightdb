"""
Worker 进程

接线后的职责：
- 从 Master 的共享内存里打开统计中心（只读统计用，不持有存储）
- 通过「执行请求队列 + 本 Worker 专属的响应队列」调用 Master 执行 SQL
- 无 SO_REUSEPORT 的平台（Windows）：从自己的连接队列取出 Master 交接过来的
  连接（句柄用 socket.share / fromshare 跨进程传递），每个连接一个线程处理
- 有 SO_REUSEPORT 的平台：Worker 自行 bind 同一端口并 accept

重要：Worker **不直接打开数据库文件**。多进程各自打开同一份文件型存储会各自
持有 BufferPool 页缓存与 BTree root，交叉写回必然损坏数据。
"""
import os
import signal
import socket
import sys
import threading
import time
from queue import Empty as QueueEmpty
from typing import Optional

from .listener import create_listener, HAS_REUSEPORT
from ..logger import logger

IS_WINDOWS = sys.platform == 'win32'


# ==================== RPC 客户端 ====================

class RPCError(RuntimeError):
    pass


class _RPCClient:
    """Worker 侧的请求/响应客户端。

    请求发到共享的 exec_queue，响应从 **本 Worker 专属** 的 resp_queue 回来，
    因此多线程并发调用不会串包；响应线程按 req_id 唤醒等待者。
    """

    def __init__(self, exec_queue, resp_queue, worker_id: int):
        self.exec_queue = exec_queue
        self.resp_queue = resp_queue
        self.worker_id = worker_id
        self._lock = threading.Lock()
        self._seq = 0
        self._pending = {}
        self._alive = True
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f'worker-{worker_id}-rpc')
        self._reader.start()

    def call(self, kind: str, payload=None, timeout: float = 60.0):
        with self._lock:
            self._seq += 1
            req_id = self._seq
            slot = {'event': threading.Event(), 'value': None}
            self._pending[req_id] = slot

        try:
            self.exec_queue.put(
                (req_id, self.worker_id, kind, payload))
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RPCError(f'发送请求失败: {e}')

        if not slot['event'].wait(timeout):
            self._pending.pop(req_id, None)
            raise RPCError(
                f'等待 Master 响应超时({timeout:.0f}s): {kind}')

        self._pending.pop(req_id, None)
        return slot['value']

    def _read_loop(self):
        while self._alive:
            try:
                item = self.resp_queue.get(timeout=0.5)
            except QueueEmpty:
                continue
            except (EOFError, OSError, ValueError):
                # Master 退出、响应队列关闭
                self._alive = False
                break
            except Exception:
                continue
            try:
                req_id, ok, value = item
            except Exception:
                continue
            slot = self._pending.get(req_id)
            if slot is None:
                # 已超时放弃的请求，忽略
                continue
            slot['value'] = (ok, value)
            slot['event'].set()

    def close(self):
        self._alive = False


# ==================== 代理对象 ====================

class RemoteExecutor:
    """把 execute() 转发给 Master 的 Executor"""

    def __init__(self, rpc: _RPCClient):
        self.rpc = rpc
        self.stats = {}

    def execute(self, parsed, session):
        try:
            ok, result = self.rpc.call(
                'execute', (parsed, session), timeout=60.0)
        except RPCError as e:
            return {'type': 'error', 'code': 1105,
                    'message': str(e)[:200]}
        if not ok:
            if isinstance(result, dict):
                return result
            return {'type': 'error', 'code': 1105,
                    'message': str(result)[:200]}
        return result


class RemoteTable:
    """表结构的最小投影（供 handler 读取 .cols）"""

    def __init__(self, name: str, cols: list):
        self.name = name
        self.cols = cols or []


class RemoteDB:
    """把只读元数据（用户表 / 表结构）从 Master 同步过来的代理

    handler 会用到的接口：users / tables / autocommit
    """

    def __init__(self, rpc: _RPCClient, data_dir: str = ''):
        self.rpc = rpc
        self.data_dir = data_dir
        self.users = {}
        self.tables = {}
        self.autocommit = True

    def refresh(self, timeout: float = 15.0) -> bool:
        ok, snap = self.rpc.call('snapshot', None, timeout=timeout)
        if not ok or not isinstance(snap, dict):
            return False
        self.users = snap.get('users') or {}
        self.tables = {
            name: RemoteTable(name, meta.get('cols', []))
            for name, meta in (snap.get('tables') or {}).items()
        }
        self.autocommit = bool(snap.get('autocommit', True))
        return True


# ==================== Worker 入口 ====================

def worker_main(worker_id: int, config: dict,
                shared_state: dict = None):
    """Worker 进程入口"""
    logger.info(
        f"[Worker-{worker_id}] 启动 PID={os.getpid()}")

    if shared_state is None:
        shared_state = {}

    host = config.get('host', '0.0.0.0')
    port = int(config.get('port', 8888))

    # 忽略 SIGINT（由 Master 统一管理）
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass

    listener = None
    sock = None
    rpc = None
    stats_hub = None

    try:
        # ---------- 与 Master 建立 RPC ----------
        exec_queue = shared_state.get('exec_queue')
        resp_queues = shared_state.get('resp_queues') or []
        if exec_queue is None or worker_id >= len(resp_queues):
            logger.error(
                f"[Worker-{worker_id}] 缺少 RPC 队列，退出")
            return
        rpc = _RPCClient(exec_queue, resp_queues[worker_id], worker_id)

        # 等 Master 就绪（执行服务线程先于 Worker 启动，通常一次即通）
        for attempt in range(30):
            try:
                ok, _ = rpc.call('ping', None, timeout=5.0)
                if ok:
                    break
            except RPCError:
                pass
            time.sleep(0.5)
        else:
            logger.error(
                f"[Worker-{worker_id}] 无法连接 Master 执行服务")
            return

        # ---------- 元数据 / 统计 ----------
        data_dir = config.get('data_dir', 'data')
        remote_db = RemoteDB(rpc, data_dir)
        for attempt in range(3):
            try:
                if remote_db.refresh():
                    break
            except RPCError as e:
                logger.warn(
                    f"[Worker-{worker_id}] 元数据快照失败"
                    f"（第 {attempt + 1} 次）: {e}")
            time.sleep(0.5)
        else:
            logger.warn(
                f"[Worker-{worker_id}] 元数据快照获取失败，"
                f"登录校验可能失败")

        # 元数据快照只在启动时拉一次的话，别的 Worker 上执行的 DDL
        # 在本进程的 SHOW TABLES / SHOW COLUMNS 里看不到，因此定期刷新
        _start_snapshot_refresher(worker_id, remote_db)

        stats_hub = _open_stats_hub(shared_state)

        ctx = {
            'config': config,
            'db': remote_db,
            'executor': RemoteExecutor(rpc),
            'stats_hub': stats_hub,
            'rpc': rpc,
            'data_dir': data_dir,
        }

        # 上报就绪（Master 的 WorkerPool 依赖这个信号）
        ready_queue = shared_state.get('ready_queue')
        if ready_queue is not None:
            try:
                ready_queue.put(worker_id)
            except Exception:
                pass

        # ---------- 主循环 ----------
        if HAS_REUSEPORT:
            listener = create_listener(host, port, mode='reuseport')
            sock = listener.bind()
            _accept_loop(worker_id, sock, ctx, shared_state)
        else:
            _queue_loop(worker_id, ctx, shared_state)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"[Worker-{worker_id}] 异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if rpc is not None:
            rpc.close()
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        if stats_hub is not None:
            try:
                stats_hub.close()
            except Exception:
                pass
        logger.info(f"[Worker-{worker_id}] 退出")


def _start_snapshot_refresher(worker_id: int, remote_db: 'RemoteDB',
                              interval: float = 5.0):
    """后台定期刷新元数据快照（用户表 / 表结构）

    只捕获异常、不写日志：避免守护线程在解释器关闭阶段去写 stdout。
    """
    def loop():
        while True:
            time.sleep(interval)
            try:
                remote_db.refresh(timeout=10)
            except Exception:
                pass

    threading.Thread(
        target=loop, daemon=True,
        name=f'worker-{worker_id}-meta').start()


def _open_stats_hub(shared_state: dict):
    """打开 Master 创建的统计中心（只读打开，不重置计数）"""
    name = shared_state.get('stats_name')
    if not name:
        return None
    try:
        from ..cluster.stats_hub import StatsHub
        return StatsHub(
            name=name,
            worker_count=shared_state.get('worker_count', 1),
            create=False,
            shm_dir=shared_state.get('shm_dir'))
    except Exception as e:
        logger.warn(f"打开统计中心失败: {e}")
        return None


# ==================== 接受循环 ====================

def _accept_loop(worker_id: int, sock, ctx: dict, shared_state: dict):
    """SO_REUSEPORT 模式：Worker 自行 accept"""
    sock.settimeout(1.0)
    stats_hub = ctx.get('stats_hub')

    last_check = time.time()
    while True:
        if time.time() - last_check > 3.0:
            last_check = time.time()
            if _master_gone(shared_state):
                logger.warn(
                    f"[Worker-{worker_id}] Master 已退出，Worker 随之退出")
                break
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        except Exception as e:
            logger.warn(f"[Worker-{worker_id}] accept 异常: {e}")
            time.sleep(0.1)
            continue

        _count_connection(stats_hub, worker_id)
        threading.Thread(
            target=_handle_connection,
            args=(worker_id, conn, addr, ctx),
            daemon=True).start()


def _queue_loop(worker_id: int, ctx: dict, shared_state: dict):
    """Master 交接模式：从自己的连接队列取连接"""
    conn_queues = shared_state.get('conn_queues') or []
    if worker_id >= len(conn_queues):
        logger.warn(
            f"[Worker-{worker_id}] 无 conn_queue，退出")
        return
    conn_queue = conn_queues[worker_id]
    stats_hub = ctx.get('stats_hub')

    logger.info(f"[Worker-{worker_id}] 等待 Master 分发连接")

    last_check = time.time()
    while True:
        # 定期确认 Master 还在（避免父进程被强杀后变成孤儿进程）
        if time.time() - last_check > 3.0:
            last_check = time.time()
            if _master_gone(shared_state):
                logger.warn(
                    f"[Worker-{worker_id}] Master 已退出，Worker 随之退出")
                break

        try:
            item = conn_queue.get(timeout=1.0)
        except QueueEmpty:
            continue
        except (EOFError, OSError, ValueError) as e:
            # Master 进程已退出，队列被关闭：直接退出，避免空转成孤儿进程
            logger.warn(
                f"[Worker-{worker_id}] 连接队列已关闭 ({e})，退出")
            break
        except Exception:
            continue

        if item is None:
            break

        try:
            payload, addr = item
        except Exception:
            continue

        try:
            conn = _materialize(payload)
        except Exception as e:
            logger.warn(f"[Worker-{worker_id}] 连接重建失败: {e}")
            continue

        _count_connection(stats_hub, worker_id)
        threading.Thread(
            target=_handle_connection,
            args=(worker_id, conn, addr, ctx),
            daemon=True).start()


def _materialize(payload):
    """把队列里的连接句柄还原成 socket

    Windows 下传递的是 socket.share() 的字节，POSIX 下直接是 socket 对象。
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return socket.fromshare(bytes(payload))
    return payload


def _process_alive(pid) -> bool:
    """Master 是否还活着

    子进程自己也持有队列的写端，所以父进程被强杀时管道不会 EOF，
    必须主动检查父进程存活，否则 Worker 会变成孤儿进程空转。
    无法判断时返回 True（宁可多跑也不误杀）。
    """
    if not pid:
        return True
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return True


def _master_gone(shared_state: dict) -> bool:
    return not _process_alive(shared_state.get('master_pid'))


def _count_connection(stats_hub, worker_id: int):
    if stats_hub is None:
        return
    try:
        stats_hub.report(worker_id, 'connections')
    except Exception:
        pass


# ==================== 连接处理 ====================

def _handle_connection(worker_id: int, conn, addr, ctx: dict):
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

        def __getattr__(self, name):
            return getattr(self._sock, name)

    class _FakeServer:
        """模拟 socketserver 的 server 接口"""
        def __init__(self, c):
            self.db = c.get('db')
            self.executor = c.get('executor')
            self.config = _ConfigAdapter(c.get('config') or {})
            self.start_time = time.time()
            # MySQLHandler 可能引用的可选字段
            self.query_cache = None
            self.pool = None
            self.semi_sync = None
            self.ssl_mgr = None
            self.ssl_enabled = False

    class _ConfigAdapter:
        """把 dict 适配为带属性的 Config 对象"""
        def __init__(self, cfg: dict):
            self._cfg = cfg
            self.host = cfg.get('host', '0.0.0.0')
            self.port = int(cfg.get('port', 8888))

        def __getattr__(self, name):
            return self._cfg.get(name)

    try:
        handler = MySQLHandler.__new__(MySQLHandler)
        handler.request = _FakeRequest(conn)
        handler.client_address = addr
        handler.server = _FakeServer(ctx)
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
            f"[Worker-{worker_id}] 连接处理失败 ({addr}): {e}")
        stats_hub = ctx.get('stats_hub')
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
