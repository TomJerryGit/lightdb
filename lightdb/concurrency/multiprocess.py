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
                 shared_state=None):
        """
        worker_func: Worker 进程入口函数
        size: Worker 数量，默认 CPU 核数
        config: 共享配置
        shared_state: 共享内存状态
        """
        self.worker_func = worker_func
        self.size = size or (os.cpu_count() or 4)
        self.config = config or {}
        self.shared_state = shared_state

        self.workers: Dict[int, WorkerInfo] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor_thread = None

        # 连接队列（Master 模式用）
        self._pending_conns = mp.Queue() if not HAS_REUSEPORT else None

        # 统计
        self.total_restarts = 0
        self.started_at = time.time()

    # ==================== 生命周期 ====================

    def start(self):
        """启动所有 Worker"""
        print(f"🚀 启动 {self.size} 个 Worker 进程")
        for i in range(self.size):
            self._start_worker(i)

        # 启动监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name='WorkerPoolMonitor')
        self._monitor_thread.start()

        # 等待所有 Worker 就绪
        self._wait_ready(timeout=10)

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
            p = mp.Process(
                target=self._worker_entry,
                args=(worker_id,),
                name=f'lightdb-worker-{worker_id}',
                daemon=False,
            )
            p.start()
            info.process = p
            info.pid = p.pid

            print(f"  ✅ Worker-{worker_id} 启动 (PID={p.pid})")

    def _worker_entry(self, worker_id: int):
        """Worker 进程入口（子进程内运行）"""
        # 设置进程标题（Linux）
        try:
            if hasattr(os, 'setpriority'):
                pass
        except Exception:
            pass

        # 忽略 SIGINT（由 Master 统一管理）
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass

        # 调用用户函数
        try:
            self.worker_func(worker_id, self.config, self.shared_state)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            import traceback
            print(f"[Worker-{worker_id}] 崩溃: {e}")
            traceback.print_exc()

    def _monitor_loop(self):
        """监控 Worker 状态，崩溃自动重启"""
        while not self._stop.is_set():
            time.sleep(2)
            self._check_workers()

    def _check_workers(self):
        """检查所有 Worker 是否存活"""
        for worker_id, info in list(self.workers.items()):
            if info.process is None:
                continue
            if not info.process.is_alive():
                if info.status != 'stopped':
                    print(f"⚠️ Worker-{worker_id} (PID={info.pid}) "
                          f"已退出，重启中...")
                    self._start_worker(worker_id)

    def _wait_ready(self, timeout: float = 10):
        """等待所有 Worker 就绪"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = sum(1 for w in self.workers.values()
                        if w.status == 'ready')
            if ready == self.size:
                print(f"✅ 全部 {self.size} 个 Worker 就绪")
                return
            time.sleep(0.1)
        print(f"⚠️ 等待超时，{ready}/{self.size} 就绪")

    def stop(self, timeout: float = 10):
        """优雅停止所有 Worker"""
        print("🛑 停止 Worker 池...")
        self._stop.set()

        # 发送 SIGTERM
        for info in self.workers.values():
            info.status = 'stopped'
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
                    print(f"  ⚠️ Worker-{info.worker_id} 未退出，强杀")
                    try:
                        info.process.kill()
                    except Exception:
                        pass

        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        print("✅ Worker 池已停止")

    # ==================== 连接分发（Master 模式）====================

    def dispatch(self, conn_socket):
        """把连接分发给 Worker（Master 模式）"""
        if self._pending_conns is None:
            # 无队列模式：直接给负载最低的
            return

        try:
            # 通过队列传递 socket
            self._pending_conns.put(conn_socket, timeout=1)
        except Exception:
            try:
                conn_socket.close()
            except Exception:
                pass

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        return {
            'size': self.size,
            'alive': sum(1 for w in self.workers.values()
                          if w.status != 'stopped'),
            'ready': sum(1 for w in self.workers.values()
                          if w.status == 'ready'),
            'total_restarts': self.total_restarts,
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