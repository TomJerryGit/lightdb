"""
并行统计聚合
- 汇总多个 Worker 的指标
"""
import threading
import time
from collections import defaultdict
from typing import Dict, List


class ParallelStatsHub:
    """并行统计中心"""

    def __init__(self):
        self.lock = threading.RLock()
        self.workers: Dict[int, Dict] = {}
        self.aggregated = defaultdict(int)
        self.start_time = time.time()

    def register_worker(self, worker_id: int):
        with self.lock:
            self.workers[worker_id] = {
                'queries': 0,
                'connections': 0,
                'errors': 0,
                'bytes_sent': 0,
                'bytes_recv': 0,
                'started_at': time.time(),
            }

    def report(self, worker_id: int, metric: str,
               delta: int = 1):
        with self.lock:
            if worker_id not in self.workers:
                self.register_worker(worker_id)
            w = self.workers[worker_id]
            if metric in w:
                w[metric] += delta
            self.aggregated[metric] += delta

    def aggregate(self) -> Dict:
        with self.lock:
            totals = {
                'queries': 0,
                'connections': 0,
                'errors': 0,
                'bytes_sent': 0,
                'bytes_recv': 0,
            }
            for w in self.workers.values():
                for k in totals:
                    totals[k] += w.get(k, 0)
            return {
                'totals': totals,
                'workers': len(self.workers),
                'uptime_s': time.time() - self.start_time,
                'per_worker': dict(self.workers),
            }

    def reset(self):
        with self.lock:
            for w in self.workers.values():
                for k in ('queries', 'connections', 'errors',
                           'bytes_sent', 'bytes_recv'):
                    w[k] = 0
            self.aggregated.clear()