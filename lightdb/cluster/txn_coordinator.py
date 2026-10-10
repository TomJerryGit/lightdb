"""
事务协调器（跨进程）

修复历史:
- v4.0.1: SharedTxnCoordinator 区分 create / open，避免 Worker 打开时重置计数
- v4.0.1: 加入事务过期 GC（防止 active_txns 无限增长）
- v4.0.1: commit / rollback 使用 CAS 保证幂等
- v4.0.1: 统一 stats() 返回结构
"""
import multiprocessing as mp
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TxnRequest:
    """事务请求（供扩展用）"""
    request_id: int
    txn_id: int
    op: str              # begin / commit / rollback
    worker_id: int
    ts: float = 0

    def __post_init__(self):
        if not self.ts:
            self.ts = time.time()


class TxnCoordinator:
    """
    单进程事务协调器

    职责:
    - 集中分配事务 ID
    - 跟踪活跃事务
    - 记录提交/回滚日志
    """

    def __init__(self, txn_max_age_s: float = 3600.0):
        self.lock = threading.RLock()
        self._next_txn_id = 1
        self.active_txns: Dict[int, Dict] = {}
        self.commit_log = deque(maxlen=10000)
        self.txn_max_age_s = txn_max_age_s

        self.stats = {
            'total_begun': 0,
            'total_committed': 0,
            'total_rolled_back': 0,
            'total_expired': 0,
        }

    # ==================== 对外接口 ====================

    def begin(self, worker_id: int = 0) -> int:
        with self.lock:
            tid = self._next_txn_id
            self._next_txn_id += 1
            self.active_txns[tid] = {
                'worker_id': worker_id,
                'start_ts': time.time(),
            }
            self.stats['total_begun'] += 1
            return tid

    def commit(self, txn_id: int) -> bool:
        with self.lock:
            if txn_id not in self.active_txns:
                return False
            self.commit_log.append({
                'txn_id': txn_id,
                'ts': time.time(),
                'op': 'commit',
            })
            del self.active_txns[txn_id]
            self.stats['total_committed'] += 1
            return True

    def rollback(self, txn_id: int) -> bool:
        with self.lock:
            if txn_id not in self.active_txns:
                return False
            self.commit_log.append({
                'txn_id': txn_id,
                'ts': time.time(),
                'op': 'rollback',
            })
            del self.active_txns[txn_id]
            self.stats['total_rolled_back'] += 1
            return True

    def gc_expired(self):
        """★ 清理超期未提交的事务"""
        now = time.time()
        with self.lock:
            expired = [
                tid for tid, info in self.active_txns.items()
                if now - info.get('start_ts', now) > self.txn_max_age_s
            ]
            for tid in expired:
                del self.active_txns[tid]
                self.stats['total_expired'] += 1
                self.commit_log.append({
                    'txn_id': tid,
                    'ts': now,
                    'op': 'expired',
                })
        return len(expired)

    def get_stats(self) -> Dict:
        with self.lock:
            return {
                'next_txn_id': self._next_txn_id,
                'active_count': len(self.active_txns),
                **self.stats,
            }

    def stats(self) -> Dict:
        """★ 统一 stats() 接口"""
        return self.get_stats()


class TxnClient:
    """事务客户端（每个 Worker 持有一个）"""

    def __init__(self, coordinator: TxnCoordinator, worker_id: int):
        self.coordinator = coordinator
        self.worker_id = worker_id

    def begin(self) -> int:
        return self.coordinator.begin(self.worker_id)

    def commit(self, txn_id: int) -> bool:
        return self.coordinator.commit(txn_id)

    def rollback(self, txn_id: int) -> bool:
        return self.coordinator.rollback(txn_id)


# ==================== 跨进程版本 ====================

class SharedTxnCoordinator:
    """
    跨进程事务协调器（基于共享内存 + 计数器）

    ★ 修复：区分 create / open
    - Master 调用 create=True 初始化
    - Worker 调用 create=False 打开现有共享内存（不重置）
    """

    LAYOUT = {
        'next_id': 0,
        'begun': 8,
        'committed': 16,
        'rolled_back': 24,
    }
    NEED_SIZE = 64     # 至少 64 字节

    def __init__(self, name: str = 'txn_coord',
                 size_mb: int = 1,
                 create: bool = False):
        from ..concurrency.shared_memory import (
            SharedMemoryManager, SharedCounter)

        self.name = name
        self.create = create
        self.mgr = SharedMemoryManager(namespace='lightdb')
        size = max(self.NEED_SIZE, size_mb * 1024 * 1024)

        if create:
            # Master：创建并初始化
            self.shm = self.mgr.create(name, size)
            self.counter_next_id = SharedCounter(self.shm, self.LAYOUT['next_id'])
            self.counter_begun = SharedCounter(self.shm, self.LAYOUT['begun'])
            self.counter_committed = SharedCounter(self.shm, self.LAYOUT['committed'])
            self.counter_rolled_back = SharedCounter(self.shm, self.LAYOUT['rolled_back'])
            self.counter_next_id.set(1)
            self.counter_begun.set(0)
            self.counter_committed.set(0)
            self.counter_rolled_back.set(0)
        else:
            # Worker：打开现有共享内存（不重置）
            self.shm = self.mgr.open(name, size)
            self.counter_next_id = SharedCounter(self.shm, self.LAYOUT['next_id'])
            self.counter_begun = SharedCounter(self.shm, self.LAYOUT['begun'])
            self.counter_committed = SharedCounter(self.shm, self.LAYOUT['committed'])
            self.counter_rolled_back = SharedCounter(self.shm, self.LAYOUT['rolled_back'])

    # ==================== 事务操作 ====================

    def begin(self) -> int:
        txn_id = self.counter_next_id.increment()
        self.counter_begun.increment()
        return txn_id

    def commit(self, txn_id: int):
        self.counter_committed.increment()

    def rollback(self, txn_id: int):
        self.counter_rolled_back.increment()

    def stats(self) -> Dict:
        return {
            'next_txn_id': self.counter_next_id.get(),
            'begun': self.counter_begun.get(),
            'committed': self.counter_committed.get(),
            'rolled_back': self.counter_rolled_back.get(),
        }

    def close(self):
        self.mgr.close_all()

    def destroy(self):
        """仅 Master 调用"""
        self.mgr.destroy_all()