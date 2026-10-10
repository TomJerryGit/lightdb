"""
查询调度器
- 按优先级调度
- 资源组隔离
- 公平分配
"""
import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .resource_group import ResourceGroupManager, ResourceGroup
from .adaptive import AdaptiveParallelism


@dataclass(order=True)
class ScheduledQuery:
    """调度中的查询"""
    priority: int
    submit_time: float = field(compare=False)
    query_id: str = field(compare=False)
    # 任务
    task: Callable = field(compare=False, default=None)
    # 上下文
    user: str = field(compare=False, default='')
    group: str = field(compare=False, default='default')
    # 状态
    status: str = field(compare=False, default='queued')
    result: Any = field(compare=False, default=None)
    error: str = field(compare=False, default='')
    # 同步
    event: threading.Event = field(compare=False, default=None)


class QueryScheduler:
    """
    查询调度器
    - 优先级队列
    - 资源组限制
    """

    def __init__(self, max_workers: int = 8):
        self.rgm = ResourceGroupManager()
        self.adaptive = AdaptiveParallelism(max_workers)
        self.max_workers = max_workers

        # 优先级队列（堆）
        self.queue = []
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)

        # 运行线程
        self.running = False
        self.dispatch_threads: List[threading.Thread] = []

        # 统计
        self.stats = {
            'submitted': 0,
            'completed': 0,
            'failed': 0,
            'rejected': 0,
        }

    # ==================== 生命周期 ====================

    def start(self, n_dispatchers: int = 4):
        if self.running:
            return
        self.running = True
        self.dispatch_threads = [
            threading.Thread(
                target=self._dispatch_loop,
                daemon=True, name=f'qs-dispatcher-{i}')
            for i in range(n_dispatchers)
        ]
        for t in self.dispatch_threads:
            t.start()

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()
        for t in self.dispatch_threads:
            t.join(timeout=2)

    # ==================== 提交 ====================

    def submit(self, task: Callable, user: str = 'root',
                group: str = None,
                priority: int = None,
                timeout: float = None) -> ScheduledQuery:
        """
        提交查询
        返回 ScheduledQuery（含 event，可 wait）
        """
        # 找到资源组
        rg = self.rgm.group_for_user(user) if not group \
              else self.rgm.get(group)
        if rg is None:
            rg = self.rgm.get('default')

        if not rg.can_accept():
            self.stats['rejected'] += 1
            q = ScheduledQuery(
                priority=99,
                submit_time=time.time(),
                query_id=str(uuid.uuid4())[:8],
                status='rejected',
                error='资源组满载',
                event=threading.Event(),
            )
            q.event.set()
            return q

        # 优先级：用户指定 > 组默认
        prio = priority if priority is not None \
               else rg.limits.priority

        q = ScheduledQuery(
            priority=prio,
            submit_time=time.time(),
            query_id=str(uuid.uuid4())[:8],
            task=task,
            user=user,
            group=rg.name,
            event=threading.Event(),
        )

        with self.cond:
            heapq.heappush(self.queue, q)
            self.cond.notify()
            self.stats['submitted'] += 1

        return q

    def wait(self, q: ScheduledQuery,
              timeout: float = None) -> bool:
        return q.event.wait(timeout)

    # ==================== 调度循环 ====================

    def _dispatch_loop(self):
        """调度线程"""
        while self.running:
            with self.cond:
                while not self.queue and self.running:
                    self.cond.wait(timeout=0.5)
                if not self.running:
                    break
                if not self.queue:
                    continue
                q = heapq.heappop(self.queue)

            # 尝试获取资源组
            rg = self.rgm.get(q.group)
            if rg is None or not rg.acquire():
                # 放回队列
                time.sleep(0.05)
                with self.cond:
                    heapq.heappush(self.queue, q)
                continue

            # 执行
            q.status = 'running'
            t0 = time.perf_counter()
            try:
                if q.task:
                    q.result = q.task()
                q.status = 'done'
                self.stats['completed'] += 1
            except Exception as e:
                q.status = 'failed'
                q.error = str(e)
                self.stats['failed'] += 1
            finally:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                rg.release(elapsed_ms)
                q.event.set()

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        with self.lock:
            return {
                **self.stats,
                'queue_size': len(self.queue),
                'groups': self.rgm.stats_report(),
            }