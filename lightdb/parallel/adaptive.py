"""
自适应并行度
- 根据数据量、CPU 数、当前负载动态决定
- 小数据不并行（避免开销）
- 大数据按 CPU 核数并行
"""
import os
import time
from typing import Optional


# 并行度阈值
MIN_PARALLEL_ROWS = 10000         # 少于该行数不并行
ROWS_PER_WORKER = 50000           # 每 Worker 处理行数
MAX_WORKERS = 16                  # 最大并行度


class AdaptiveParallelism:
    """自适应并行度决策"""

    def __init__(self, max_workers: int = None):
        self.cpu_count = os.cpu_count() or 4
        self.max_workers = max_workers or min(
            self.cpu_count * 2, MAX_WORKERS)

        # 负载跟踪
        self.recent_loads = []   # 最近 N 次负载 (0-1)
        self.window_size = 10

        # 统计
        self.stats = {
            'decisions': 0,
            'parallel': 0,
            'serial': 0,
        }

    # ==================== 决策 ====================

    def decide(self, total_rows: int,
               is_expensive: bool = False,
               has_lock: bool = False) -> int:
        """
        决策并行度
        total_rows: 预估总行数
        is_expensive: 是否 CPU 密集（如聚合、排序）
        has_lock: 是否持有锁（并行会争锁）
        返回: 并行度（1 = 串行）
        """
        self.stats['decisions'] += 1

        # 1. 数据太小 → 串行
        if total_rows < MIN_PARALLEL_ROWS:
            self.stats['serial'] += 1
            return 1

        # 2. 有锁 → 减少并行
        if has_lock:
            self.stats['serial'] += 1
            return min(2, self.max_workers)

        # 3. 计算基础并行度
        workers = min(
            self.max_workers,
            max(1, total_rows // ROWS_PER_WORKER)
        )

        # 4. CPU 密集 → 提升
        if is_expensive:
            workers = min(self.max_workers, workers * 2)

        # 5. 考虑当前系统负载
        load = self._current_load()
        if load > 0.8:
            # 系统已经很忙，减少
            workers = max(1, workers // 2)

        # 6. 至少为 1
        workers = max(1, workers)

        if workers > 1:
            self.stats['parallel'] += 1
        else:
            self.stats['serial'] += 1

        return workers

    def _current_load(self) -> float:
        """估算当前 CPU 负载"""
        try:
            import psutil
            return psutil.cpu_percent(interval=0.01) / 100.0
        except ImportError:
            pass

        # 降级：用最近样本
        if self.recent_loads:
            return sum(self.recent_loads) / len(self.recent_loads)
        return 0.3

    def record_load(self, load: float):
        """记录负载样本"""
        self.recent_loads.append(load)
        if len(self.recent_loads) > self.window_size:
            self.recent_loads = self.recent_loads[-self.window_size:]

    # ==================== 分片建议 ====================

    def suggest_split(self, total_rows: int,
                       workers: int) -> tuple:
        """
        建议分片方案
        返回 (split_type, chunk_size)
        """
        if workers <= 1:
            return ('none', total_rows)

        # 每片大小
        chunk = (total_rows + workers - 1) // workers

        # 太小就减少并行
        if chunk < MIN_PARALLEL_ROWS:
            new_workers = max(1, total_rows // MIN_PARALLEL_ROWS)
            chunk = (total_rows + new_workers - 1) // new_workers
            workers = new_workers

        return ('chunk', chunk)

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        return {
            **self.stats,
            'cpu_count': self.cpu_count,
            'max_workers': self.max_workers,
            'recent_load': (
                sum(self.recent_loads) / len(self.recent_loads)
                if self.recent_loads else 0),
            'parallel_rate': (
                self.stats['parallel'] / max(1, self.stats['decisions'])),
        }