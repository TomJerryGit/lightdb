"""
Worker 进程池
- 预启动 N 个 Worker
- 崩溃自动重启
- 优雅退出
"""
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..logger import logger


@dataclass
class WorkerInfo:
    """Worker 信息"""
    worker_id: int
    pid: int = 0
    process: Optional[mp.Process] = None
    started_at: float = 0
    restart_count: int = 0
    status: str = 'starting'      # starting / ready / busy / stopped
    # 统计
    connections: int = 0
    queries: int = 0

    def __post_init__(self):
        if not self.started_at:
            self.started_at = time.time()


class WorkerPool:
    """
    Worker 进程池
    - 使用 SO_REUSEPORT 时，每个 Worker 独立监听
    - 使用 Master 模式时，Pool 负责分发连接
    """

    def __init__(self, worker_func: Callable,
                 size: int = None,
                 config: Dict = None,
                 shared_state=None,
                 conn_queues: List = None,
                 ready_queue=None):
        """
        worker_func: Worker 进程入口函数（必须是模块级函数，才能被 spawn 序列化）
        size: Worker 数量，默认 CPU 核数
        config: 共享配置（dict）
        shared_state: 传给 Worker 的状态（只能放可 pickle 的基本量）
        conn_queues: 每个 Worker 一个连接队列（Master 交接连接用）
        ready_queue: Worker 就绪上报队列
        """
        self.worker_func = worker_func
        self.size = size or (os.cpu_count() or 4)
        self.config = config or {}
        self.shared_state = shared_state

        self.workers: Dict[int, WorkerInfo] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor_thread = None
        self._ready_thread = None

        # 连接队列（Master 模式用，由 Master 创建并传入）
        self.conn_queues = list(conn_queues or [])
        self.ready_queue = ready_queue
        # 轮询派发游标
        self._rr = 0
        self._dispatched = 0

        # 统计
        self.total_restarts = 0
        self.started_at = time.time()

    # ==================== 生命周期 ====================

    def start(self):
        """启动所有 Worker"""
        logger.info(f"启动 {self.size} 个 Worker 进程")
        for i in range(self.size):
            self._start_worker(i)

        # 就绪上报线程
        if self.ready_queue is not None:
            self._ready_thread = threading.Thread(
                target=self._ready_loop, daemon=True,
                name='WorkerPoolReady')
            self._ready_thread.start()

        # 启动监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name='WorkerPoolMonitor')
        self._monitor_thread.start()

        # 等待所有 Worker 就绪（没有就绪队列时无法观测，直接跳过）
        if self.ready_queue is not None:
            self._wait_ready(timeout=20)
        else:
            logger.debug("未提供 ready_queue，跳过就绪等待")

    def _ready_loop(self):
        """接收 Worker 的就绪上报"""
        while not self._stop.is_set():
            try:
                worker_id = self.ready_queue.get(timeout=0.5)
            except Exception:
                continue
            if worker_id is None:
                break
            with self.lock:
                info = self.workers.get(worker_id)
                if info is not None:
                    info.status = 'ready'

    def _start_worker(self, worker_id: int):
        """启动单个 Worker"""
        with self.lock:
            info = self.workers.get(worker_id)
            if info is None:
                info = WorkerInfo(worker_id=worker_id)
                self.workers[worker_id] = info
            else:
                info.restart_count += 1
                info.started_at = time.time()
                self.total_restarts += 1

            info.status = 'starting'
            info.pid = 0

            # 每个 Worker 独立进程
            # 注意：target 用模块级函数、参数走 args，这样 spawn 时只需序列化
            # 「函数引用 + 参数字典」，不会因为 pickle 整个 WorkerPool
            # （含 RLock / mp.Queue）而失败。
            p = mp.Process(
                target=self.worker_func,
                args=(worker_id, self.config, self.shared_state),
                name=f'lightdb-worker-{worker_id}',
                daemon=False,
            )
            p.start()
            info.process = p
            info.pid = p.pid

            logger.info(
                f"  Worker-{worker_id} 启动 (PID={p.pid})")

    def _monitor_loop(self):
        """监控 Worker 状态，崩溃自动重启"""
        while not self._stop.is_set():
            # 用 Event.wait 而不是 sleep：stop() 时能立刻醒来退出，
            # 避免守护线程活到解释器关闭阶段去写 stdout（会触发
            # Fatal Python error: _enter_buffered_busy）
            if self._stop.wait(2):
                break
            self._check_workers()

    def _check_workers(self):
        """检查所有 Worker 是否存活"""
        if self._stop.is_set():
            # 正在停机：不再重启
            return
        for worker_id, info in list(self.workers.items()):
            if info.process is None:
                continue
            if not info.process.is_alive():
                if info.status != 'stopped':
                    logger.warn(
                        f"Worker-{worker_id} (PID={info.pid}) "
                        f"已退出，重启中...")
                    self._start_worker(worker_id)

    def _wait_ready(self, timeout: float = 20):
        """等待所有 Worker 就绪"""
        deadline = time.time() + timeout
        ready = 0
        while time.time() < deadline:
            ready = sum(1 for w in self.workers.values()
                        if w.status == 'ready')
            if ready == self.size:
                logger.info(f"全部 {self.size} 个 Worker 就绪")
                return ready
            time.sleep(0.1)
        logger.warn(f"等待 Worker 就绪超时，{ready}/{self.size} 就绪")
        return ready

    def stop(self, timeout: float = 10):
        """优雅停止所有 Worker"""
        logger.info("停止 Worker 池...")
        self._stop.set()

        # 先标记为 stopped，避免监控线程把「已按哨兵正常退出」的 Worker
        # 当成崩溃而重启（原顺序会在停机过程中把 Worker 又拉起来）
        for info in self.workers.values():
            info.status = 'stopped'

        # 1. 发停机哨兵，让 Worker 从队列循环里正常退出
        for q in self.conn_queues:
            try:
                q.put(None)
            except Exception:
                pass
        if self.ready_queue is not None:
            try:
                self.ready_queue.put(None)
            except Exception:
                pass

        # 2. 给一点时间自行退出
        grace = min(2.0, max(0.5, timeout / 5))
        time.sleep(grace)

        # 3. 仍未退出的强制终止
        for info in self.workers.values():
            if info.process and info.process.is_alive():
                try:
                    info.process.terminate()
                except Exception:
                    pass

        # 等待退出
        deadline = time.time() + timeout
        for info in self.workers.values():
            if info.process:
                remaining = max(0, deadline - time.time())
                info.process.join(timeout=remaining)

                if info.process.is_alive():
                    logger.warn(
                        f"  Worker-{info.worker_id} 未退出，强杀")
                    try:
                        info.process.kill()
                    except Exception:
                        pass

        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
            if self._monitor_thread.is_alive():
                logger.warn("监控线程未能及时退出")
        if self._ready_thread:
            self._ready_thread.join(timeout=5)
        logger.info("Worker 池已停止")

    # ==================== 连接分发（Master 模式）====================

    def dispatch(self, conn_socket, addr=None) -> bool:
        """把 accept 到的连接交给某个 Worker

        - Windows：用 socket.share(pid) 生成句柄字节，经队列传给孩子进程，
          孩子用 socket.fromshare() 还原（原生支持的跨进程连接交接方式）
        - POSIX：直接放 socket 对象（fork 上下文可传递）

        按轮询顺序尝试，跳过已经退出的 Worker；全都不行才关闭连接。
        """
        if not self.conn_queues:
            try:
                conn_socket.close()
            except Exception:
                pass
            return False

        last_err = None
        for _ in range(self.size):
            with self.lock:
                worker_id = self._rr % self.size
                self._rr += 1
                info = self.workers.get(worker_id)
                q = (self.conn_queues[worker_id]
                     if worker_id < len(self.conn_queues) else None)

            alive = (info is not None and info.process is not None
                     and info.process.is_alive())
            pid = info.pid if info is not None else 0

            if q is None or not pid or not alive:
                last_err = f'Worker-{worker_id} 不可用'
                continue

            try:
                if sys.platform == 'win32':
                    payload = conn_socket.share(pid)
                else:
                    payload = conn_socket
                q.put((payload, addr), timeout=2)
            except Exception as e:
                last_err = e
                continue

            if sys.platform == 'win32':
                # share() 只是复制句柄，真正交接完成后本进程要关掉自己这份
                try:
                    conn_socket.close()
                except Exception:
                    pass
            with self.lock:
                self._dispatched += 1
            return True

        logger.warn(f"无可用 Worker，关闭连接 (addr={addr}): {last_err}")
        try:
            conn_socket.close()
        except Exception:
            pass
        return False

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        return {
            'size': self.size,
            'alive': sum(1 for w in self.workers.values()
                          if w.status != 'stopped'),
            'ready': sum(1 for w in self.workers.values()
                          if w.status == 'ready'),
            'total_restarts': self.total_restarts,
            'dispatched': self._dispatched,
            'uptime_s': time.time() - self.started_at,
            'workers': [
                {
                    'id': w.worker_id,
                    'pid': w.pid,
                    'status': w.status,
                    'restarts': w.restart_count,
                    'connections': w.connections,
                    'queries': w.queries,
                }
                for w in sorted(self.workers.values(),
                                 key=lambda x: x.worker_id)
            ],
        }


from .shared_memory import HAS_REUSEPORT