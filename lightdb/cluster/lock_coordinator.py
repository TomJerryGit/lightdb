"""
锁协调器（跨进程）

修复历史:
- v4.0.1: 引入真正的条件等待（每 key 一个 Condition），移除 sleep 轮询
- v4.0.1: 修正 S -> X 升级时的兼容性判断
- v4.0.1: 加入死锁检测（三色 DFS）
- v4.0.1: release 只通知等待相关 key 的事务，减少惊群
- v4.0.1: stats 统计修正

说明:
本模块目前是 "进程内 + 可被多线程共享" 的锁协调器。
若需要真正跨进程锁，需要基于 cluster/shared_buffer 的 CAS；
本类提供统一的 API，由上层决定单进程 / 多进程后端。
"""
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Optional, Set


class DeadlockError(Exception):
    """死锁异常"""
    pass


class LockCoordinator:
    """
    集中式锁协调器

    支持:
    - Record Lock: S / X
    - 意向锁: IS / IX / SIX
    - 同一事务重入 & S -> X 升级
    - 死锁检测
    """

    S = 'S'
    X = 'X'
    IS = 'IS'
    IX = 'IX'
    SIX = 'SIX'

    # 锁兼容矩阵
    COMPAT = {
        (S,  S):   True,
        (S,  X):   False,
        (S,  IS):  True,
        (S,  IX):  False,
        (S,  SIX): False,
        (X,  S):   False,
        (X,  X):   False,
        (X,  IS):  False,
        (X,  IX):  False,
        (X,  SIX): False,
        (IS, S):   True,
        (IS, X):   False,
        (IS, IS):  True,
        (IS, IX):  True,
        (IS, SIX): False,
        (IX, S):   False,
        (IX, X):   False,
        (IX, IS):  True,
        (IX, IX):  True,
        (IX, SIX): False,
        (SIX, S):  False,
        (SIX, X):  False,
        (SIX, IS): False,
        (SIX, IX): False,
        (SIX, SIX): False,
    }

    def __init__(self, timeout: float = 5.0):
        # 全局互斥锁，保护所有内部结构
        self.lock = threading.RLock()

        # 每 key 一个 Condition（减少惊群）
        self.key_conds: Dict = {}

        # key -> {txn_id: lock_type}
        self.holders: Dict = defaultdict(dict)
        # txn_id -> set(keys)
        self.txn_locks: Dict[int, Set] = defaultdict(set)
        # 等待图: waiter_txn -> set(blocker_txn)
        self.wait_graph: Dict[int, Set[int]] = defaultdict(set)

        self.timeout = timeout

        self.stats = {
            'acquired': 0,
            'waited': 0,
            'timeouts': 0,
            'deadlocks': 0,
            'upgraded': 0,
        }

    # ==================== 条件变量管理 ====================

    def _get_key_cond(self, key) -> threading.Condition:
        cond = self.key_conds.get(key)
        if cond is None:
            cond = threading.Condition(self.lock)
            self.key_conds[key] = cond
        return cond

    # ==================== 核心 API ====================

    def acquire(self, txn_id: int, key, lock_type: str = 'X') -> bool:
        """
        获取锁

        key: (table, pk) 或 ('TABLE', table) 或任意 hashable
        lock_type: 'S' / 'X' / 'IS' / 'IX' / 'SIX'
        """
        deadline = time.time() + self.timeout
        cond = self._get_key_cond(key)

        with cond:
            while True:
                if self._can_acquire(key, lock_type, txn_id):
                    upgraded = self._grant(key, lock_type, txn_id)
                    self._clear_wait_graph(txn_id)
                    self.stats['acquired'] += 1
                    if upgraded:
                        self.stats['upgraded'] += 1
                    return True

                # 记录等待关系
                self._clear_wait_graph(txn_id)
                holders = [
                    t for t in self.holders.get(key, {})
                    if t != txn_id
                ]
                for h in holders:
                    self.wait_graph[txn_id].add(h)

                # 死锁检测
                if holders and self._detect_cycle(txn_id):
                    self.stats['deadlocks'] += 1
                    self._clear_wait_graph(txn_id)
                    raise DeadlockError(
                        f"死锁: 事务 {txn_id} 被选为受害者")

                remaining = deadline - time.time()
                if remaining <= 0:
                    self.stats['timeouts'] += 1
                    self._clear_wait_graph(txn_id)
                    return False

                self.stats['waited'] += 1
                cond.wait(timeout=min(remaining, 0.5))

    def release_all(self, txn_id: int):
        """释放事务所有锁"""
        with self.lock:
            released_keys = set()
            for key in list(self.txn_locks.get(txn_id, ())):
                if key in self.holders:
                    self.holders[key].pop(txn_id, None)
                    if not self.holders[key]:
                        del self.holders[key]
                released_keys.add(key)

            self.txn_locks.pop(txn_id, None)
            self._clear_wait_graph(txn_id)

            # 只通知等待相关 key 的事务
            notified = set()
            for key in released_keys:
                cond = self.key_conds.get(key)
                if cond is not None:
                    cond.notify_all()
                    notified.add(key)

            # 表级锁释放 -> 通知该表下所有 key
            for key in released_keys:
                if isinstance(key, tuple) and key and key[0] == 'TABLE':
                    table = key[1]
                    for k, cond in list(self.key_conds.items()):
                        if isinstance(k, tuple) and len(k) >= 2 \
                                and k[0] == table and k not in notified:
                            cond.notify_all()
                            notified.add(k)

    def release_one(self, txn_id: int, key):
        with self.lock:
            if key in self.holders:
                self.holders[key].pop(txn_id, None)
                if not self.holders[key]:
                    del self.holders[key]
            self.txn_locks[txn_id].discard(key)
            cond = self.key_conds.get(key)
            if cond is not None:
                cond.notify_all()

    # ==================== 兼容性检查 ====================

    def _can_acquire(self, key, lock_type: str, txn_id: int) -> bool:
        holders = self.holders.get(key, {})
        if not holders:
            return True

        my_type = holders.get(txn_id)
        if my_type is not None:
            # 同一事务已持有
            if my_type == lock_type:
                return True
            # 升级：S -> X
            if my_type == self.S and lock_type == self.X:
                # 只有自己是唯一持有者才能升级
                others = [t for t in holders if t != txn_id]
                return not others
            # 其他升级（IS -> IX 等）：允许
            others = [t for t in holders if t != txn_id]
            for other_type in (holders[t] for t in others):
                if not self.COMPAT.get((lock_type, other_type), False):
                    return False
            return True

        # 自己不是持有者：检查与所有 holder 的兼容性
        for other_type in holders.values():
            if not self.COMPAT.get((lock_type, other_type), False):
                return False
        return True

    def _grant(self, key, lock_type: str, txn_id: int) -> bool:
        """授予锁；返回是否发生了升级"""
        holders = self.holders[key]
        old_type = holders.get(txn_id)
        upgraded = False

        if old_type is not None and old_type != lock_type:
            upgraded = True
        holders[txn_id] = lock_type
        self.txn_locks[txn_id].add(key)

        # 自动加表级意向锁
        if isinstance(key, tuple) and len(key) >= 2 and key[0] != 'TABLE':
            table_key = ('TABLE', key[0])
            if lock_type in (self.S, self.IS):
                intent = self.IS
            else:
                intent = self.IX

            table_holders = self.holders[table_key]
            cur = table_holders.get(txn_id)
            if cur is None:
                table_holders[txn_id] = intent
                self.txn_locks[txn_id].add(table_key)
            elif cur == self.IS and intent == self.IX:
                # IS -> IX 升级
                table_holders[txn_id] = self.IX

        return upgraded

    # ==================== 死锁检测 ====================

    def _detect_cycle(self, start: int) -> bool:
        """三色 DFS 检测有向图中的环"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {}

        def dfs(node):
            color[node] = GRAY
            for nxt in self.wait_graph.get(node, ()):
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE and dfs(nxt):
                    return True
            color[node] = BLACK
            return False

        return dfs(start)

    def _clear_wait_graph(self, txn_id: int):
        self.wait_graph.pop(txn_id, None)
        for s in self.wait_graph.values():
            s.discard(txn_id)

    # ==================== 统计 ====================

    def stats_report(self) -> Dict:
        with self.lock:
            active_locks = sum(
                len(h) for h in self.holders.values())
            return {
                **self.stats,
                'active_locks': active_locks,
                'active_txns': len(self.txn_locks),
                'waiting_txns': len(self.wait_graph),
                'key_conds': len(self.key_conds),
            }

    # ==================== 上下文管理器 ====================

    @contextmanager
    def hold(self, txn_id: int, key, lock_type: str = 'X'):
        self.acquire(txn_id, key, lock_type)
        try:
            yield
        finally:
            self.release_one(txn_id, key)