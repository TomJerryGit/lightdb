"""
资源组
- 隔离不同类型查询
- 限制资源使用（线程数、内存）
- 支持优先级
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ResourceLimits:
    """资源限制"""
    max_workers: int = 4              # 最大并行度
    max_memory_mb: int = 1024         # 最大内存
    max_concurrent_queries: int = 10  # 最大并发查询
    priority: int = 5                 # 1-10（越小越优先）
    timeout_s: float = 300            # 单查询超时


class ResourceGroup:
    """
    资源组
    """

    def __init__(self, name: str, limits: ResourceLimits):
        self.name = name
        self.limits = limits

        # 使用统计
        self.active_queries = 0
        self.active_workers = 0
        self.memory_used_mb = 0
        self.lock = threading.RLock()

        # 等待队列（按优先级）
        self.waiting_queue = deque()

        # 统计
        self.stats = {
            'total_queries': 0,
            'rejected': 0,
            'timed_out': 0,
            'total_elapsed_ms': 0,
        }

    def can_accept(self) -> bool:
        """是否可接受新查询"""
        with self.lock:
            return self.active_queries < self.limits.max_concurrent_queries

    def acquire(self) -> bool:
        """获取资源"""
        with self.lock:
            if not self.can_accept():
                return False
            self.active_queries += 1
            self.stats['total_queries'] += 1
            return True

    def release(self, elapsed_ms: float = 0):
        """释放资源"""
        with self.lock:
            self.active_queries = max(0, self.active_queries - 1)
            self.stats['total_elapsed_ms'] += elapsed_ms

    def stats_report(self) -> dict:
        with self.lock:
            return {
                'name': self.name,
                'active_queries': self.active_queries,
                'max_concurrent': self.limits.max_concurrent_queries,
                'priority': self.limits.priority,
                'max_workers': self.limits.max_workers,
                **self.stats,
            }


class ResourceGroupManager:
    """资源组管理器"""

    # 预定义组
    DEFAULT_GROUPS = {
        'system': ResourceLimits(
            max_workers=16, max_memory_mb=2048,
            max_concurrent_queries=20, priority=1),
        'oltp': ResourceLimits(
            max_workers=2, max_memory_mb=512,
            max_concurrent_queries=100, priority=3),
        'olap': ResourceLimits(
            max_workers=16, max_memory_mb=4096,
            max_concurrent_queries=5, priority=5),
        'etl': ResourceLimits(
            max_workers=8, max_memory_mb=2048,
            max_concurrent_queries=3, priority=7),
        'default': ResourceLimits(
            max_workers=4, max_memory_mb=1024,
            max_concurrent_queries=10, priority=5),
    }

    def __init__(self):
        self.groups: Dict[str, ResourceGroup] = {}
        for name, limits in self.DEFAULT_GROUPS.items():
            self.groups[name] = ResourceGroup(name, limits)

        # 用户 -> 组映射
        self.user_groups: Dict[str, str] = {}
        self.lock = threading.RLock()

    # ==================== 组管理 ====================

    def create_group(self, name: str, limits: ResourceLimits):
        with self.lock:
            self.groups[name] = ResourceGroup(name, limits)

    def get(self, name: str) -> Optional[ResourceGroup]:
        return self.groups.get(name)

    def assign_user(self, user: str, group_name: str):
        with self.lock:
            self.user_groups[user] = group_name

    def group_for_user(self, user: str) -> ResourceGroup:
        with self.lock:
            name = self.user_groups.get(user, 'default')
            return self.groups.get(name, self.groups['default'])

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        with self.lock:
            return {
                name: g.stats_report()
                for name, g in self.groups.items()
            }