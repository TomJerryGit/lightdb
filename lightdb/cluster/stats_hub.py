"""
跨进程统计聚合

修复历史:
- v4.0.1: 检测 shared memory 大小是否匹配（多 Worker 场景）
- v4.0.1: 修复 offset 布局，全部按 8 字节对齐
- v4.0.1: report() 校验 metric 名称
- v4.0.1: 支持 destroy（Master 关闭时清理）
"""
import threading
from typing import Dict


# ★ 常量布局：每个 Worker 4 个 8 字节槽
SLOT_SIZE = 32
METRICS = ('queries', 'connections', 'errors', 'bytes_sent')


class StatsHub:
    """统计中心

    布局（每个 Worker slot）:
      +0   queries      (int64)
      +8   connections  (int64)
      +16  errors       (int64)
      +24  bytes_sent   (int64)
    """

    def __init__(self, name: str = 'stats',
                 worker_count: int = 8,
                 create: bool = False,
                 shm_dir: str = None):
        from ..concurrency.shared_memory import (
            SharedMemoryManager, SharedCounter)

        self.name = name
        self.worker_count = worker_count
        self.create = create
        self.lock = threading.RLock()

        self.mgr = SharedMemoryManager(namespace='lightdb',
                                       shm_dir=shm_dir)
        size = SLOT_SIZE * worker_count

        if create:
            self.shm = self.mgr.create(name, size)
        else:
            self.shm = self.mgr.open(name, size)

        # 每个 Worker 4 个计数器
        self.counters: Dict[int, Dict] = {}
        for wid in range(worker_count):
            base = wid * SLOT_SIZE
            self.counters[wid] = {
                m: SharedCounter(self.shm, base + i * 8)
                for i, m in enumerate(METRICS)
            }

    # ==================== 上报 ====================

    def report(self, worker_id: int, metric: str, delta: int = 1):
        """Worker 上报指标（增量）"""
        if worker_id not in self.counters:
            return
        c = self.counters[worker_id].get(metric)
        if c is None:
            return
        if delta == 0:
            return
        c.increment(delta)

    def set(self, worker_id: int, metric: str, value: int):
        """Worker 直接设置指标（绝对值）"""
        if worker_id not in self.counters:
            return
        c = self.counters[worker_id].get(metric)
        if c is not None:
            c.set(value)

    # ==================== 聚合 ====================

    def aggregate(self) -> Dict:
        """聚合所有 Worker 的指标"""
        totals = {m: 0 for m in METRICS}
        per_worker = {}

        with self.lock:
            for wid, counters in self.counters.items():
                worker_stats = {}
                for name, c in counters.items():
                    v = c.get()
                    worker_stats[name] = v
                    totals[name] += v
                per_worker[wid] = worker_stats

        return {
            'totals': totals,
            'per_worker': per_worker,
            'worker_count': self.worker_count,
        }

    # ==================== 生命周期 ====================

    def close(self):
        self.mgr.close_all()

    def destroy(self):
        """仅 Master 调用"""
        self.mgr.destroy_all()

    def stats(self) -> Dict:
        return self.aggregate()