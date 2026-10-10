"""
工作窃取调度器
- 每个 Worker 有自己的 deque
- 空闲 Worker 从其他 Worker 尾部窃取
- 提升负载均衡
"""
import threading
import time
from collections import deque
from typing import Any, Callable, List, Optional


class WorkQueue:
    """每个 Worker 的工作队列"""

    def __init__(self):
        self.queue = deque()
        self.lock = threading.Lock()

    def push_front(self, item):
        """Worker 自己从头部放（LIFO）"""
        with self.lock:
            self.queue.appendleft(item)

    def pop_front(self):
        """Worker 自己从头部取"""
        with self.lock:
            if not self.queue:
                return None
            return self.queue.popleft()

    def steal(self):
        """其他 Worker 从尾部窃取（FIFO）"""
        with self.lock:
            if not self.queue:
                return None
            return self.queue.pop()

    def __len__(self):
        with self.lock:
            return len(self.queue)

    def empty(self) -> bool:
        with self.lock:
            return not self.queue


class WorkStealingScheduler:
    """
    工作窃取调度器
    """

    def __init__(self, n_workers: int = 8):
        self.n_workers = n_workers
        self.queues = [WorkQueue() for _ in range(n_workers)]

        # 任务处理器
        self.handler: Optional[Callable] = None

        # 状态
        self.running = False
        self.threads: List[threading.Thread] = []
        self.active_tasks = 0
        self.finished_tasks = 0
        self.total_tasks = 0

        # 统计
        self.stats = {
            'local_pops': 0,
            'steals': 0,
            'steal_success': 0,
            'steal_attempts': 0,
        }

        self._counter_lock = threading.Lock()
        self._done_event = threading.Event()

    # ==================== 生命周期 ====================

    def start(self):
        """启动 Worker 线程"""
        if self.running:
            return
        self.running = True
        self.threads = [
            threading.Thread(
                target=self._worker_loop, args=(i,),
                daemon=True, name=f'ws-worker-{i}')
            for i in range(self.n_workers)
        ]
        for t in self.threads:
            t.start()

    def stop(self):
        self.running = False
        for t in self.threads:
            t.join(timeout=2)
        self.threads = []

    # ==================== 提交任务 ====================

    def submit(self, tasks: List[Any]):
        """
        提交一批任务
        均匀分配到各 Worker 的队列
        """
        if not tasks:
            return

        self.total_tasks = len(tasks)
        self.finished_tasks = 0
        self._done_event.clear()

        with self._counter_lock:
            self.active_tasks = len(tasks)

        # 轮询分配
        for i, task in enumerate(tasks):
            wid = i % self.n_workers
            self.queues[wid].push_front(task)

    def wait_all(self, timeout: float = None):
        """等待所有任务完成"""
        return self._done_event.wait(timeout)

    # ==================== Worker 循环 ====================

    def _worker_loop(self, worker_id: int):
        """Worker 主循环"""
        my_queue = self.queues[worker_id]

        while self.running:
            # 1. 尝试从自己队列拿
            task = my_queue.pop_front()
            if task is not None:
                with self._counter_lock:
                    self.stats['local_pops'] += 1
                self._execute(task)
                continue

            # 2. 尝试窃取
            task = self._try_steal(worker_id)
            if task is not None:
                self._execute(task)
                continue

            # 3. 空闲等待
            time.sleep(0.001)

    def _try_steal(self, worker_id: int):
        """从其他 Worker 窃取"""
        with self._counter_lock:
            self.stats['steal_attempts'] += 1

        # 顺序窃取（简化：轮询）
        for i in range(self.n_workers):
            if i == worker_id:
                continue
            task = self.queues[i].steal()
            if task is not None:
                with self._counter_lock:
                    self.stats['steals'] += 1
                    self.stats['steal_success'] += 1
                return task

        return None

    def _execute(self, task: Any):
        """执行任务"""
        try:
            if self.handler:
                self.handler(task)
        except Exception as e:
            import traceback
            traceback.print_exc()
        finally:
            with self._counter_lock:
                self.active_tasks -= 1
                self.finished_tasks += 1
                if self.active_tasks <= 0:
                    self._done_event.set()

    def set_handler(self, handler: Callable):
        """设置任务处理器"""
        self.handler = handler

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        with self._counter_lock:
            return {
                'total_tasks': self.total_tasks,
                'finished_tasks': self.finished_tasks,
                'active_tasks': self.active_tasks,
                **self.stats,
                'queue_sizes': [len(q) for q in self.queues],
            }