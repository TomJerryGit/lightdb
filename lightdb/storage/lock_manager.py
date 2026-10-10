"""
行级锁管理器（修复版）
- Record Lock: 单行锁
- 意向锁（表级）: IS / IX
- 死锁检测：三色 DFS
- 支持同一事务重入、S → X 升级
- 按 key 通知：每个 key 一个 Condition，减少惊群

修复历史:
- 第一批: _detect_cycle 三色 DFS
- 第一批: _can_acquire 允许重入 / 升级
- 第二批: wait_graph 每轮等待循环前清理
- 第三批: release 只通知等待相关 key 的事务
"""
import threading
import time
from collections import defaultdict


# ==================== 异常 ====================

class DeadlockError(Exception):
    """死锁异常"""
    pass


# ==================== 类型 ====================

class LockType:
    S = 'S'
    X = 'X'
    IS = 'IS'
    IX = 'IX'


# 兼容矩阵
COMPAT = {
    (LockType.S, LockType.S): True,
    (LockType.S, LockType.X): False,
    (LockType.S, LockType.IS): True,
    (LockType.S, LockType.IX): False,
    (LockType.X, LockType.S): False,
    (LockType.X, LockType.X): False,
    (LockType.X, LockType.IS): False,
    (LockType.X, LockType.IX): False,
    (LockType.IS, LockType.S): True,
    (LockType.IS, LockType.X): False,
    (LockType.IS, LockType.IS): True,
    (LockType.IS, LockType.IX): True,
    (LockType.IX, LockType.S): False,
    (LockType.IX, LockType.X): False,
    (LockType.IX, LockType.IS): True,
    (LockType.IX, LockType.IX): True,
}


class LockEntry:
    __slots__ = ('txn_id', 'lock_type')

    def __init__(self, txn_id, lock_type):
        self.txn_id = txn_id
        self.lock_type = lock_type


# ==================== 锁管理器 ====================

class LockManager:
    """
    行级锁管理器
    粒度: (table_name, pk_value) -> List[LockEntry]
    """

    def __init__(self):
        # 全局锁 + 全局条件变量（用于死锁检测期间的一致性）
        self.lock = threading.RLock()

        # key -> Condition（按 key 通知）
        self.key_conds = {}

        # key -> list of LockEntry
        self.locks = defaultdict(list)
        # 事务持有的锁
        self.txn_locks = defaultdict(set)
        # 等待图: txn_id -> set(正在等待的 txn_id)
        self.wait_graph = defaultdict(set)

    # ==================== 内部: per-key Condition ====================

    def _get_key_cond(self, key):
        """获取或创建 key 的 Condition"""
        cond = self.key_conds.get(key)
        if cond is None:
            cond = threading.Condition(self.lock)
            self.key_conds[key] = cond
        return cond

    # ==================== 核心 API ====================

    def acquire(self, txn_id: int, key, lock_type: str,
                timeout: float = 5.0) -> bool:
        """
        获取锁。
        key = (table_name, pk_value) 或 ('TABLE', table_name)
        """
        deadline = time.time() + timeout
        cond = self._get_key_cond(key)

        with self.lock:
            while True:
                if self._can_acquire(key, lock_type, txn_id):
                    self._grant(key, lock_type, txn_id)
                    self._clear_wait_graph(txn_id)
                    return True

                # 清理旧等待关系，重新记录
                self._clear_wait_graph(txn_id)

                holders = {e.txn_id for e in self.locks.get(key, [])
                           if e.txn_id != txn_id}
                for h in holders:
                    self.wait_graph[txn_id].add(h)

                # 死锁检测
                if holders and self._detect_cycle(txn_id):
                    self._clear_wait_graph(txn_id)
                    raise DeadlockError(
                        f"死锁检测: 事务 {txn_id} 被选为受害者")

                remaining = deadline - time.time()
                if remaining <= 0:
                    self._clear_wait_graph(txn_id)
                    return False

                cond.wait(timeout=min(remaining, 0.5))

    def release(self, txn_id: int):
        """
        释放事务持有的所有锁，并只通知等待相关 key 的事务。
        """
        with self.lock:
            released_keys = set()
            for key in list(self.txn_locks.get(txn_id, [])):
                self.locks[key] = [
                    e for e in self.locks[key] if e.txn_id != txn_id]
                if not self.locks[key]:
                    del self.locks[key]
                released_keys.add(key)

            self.txn_locks.pop(txn_id, None)
            self._clear_wait_graph(txn_id)

            # 按 key 通知
            notified = set()
            for key in released_keys:
                cond = self.key_conds.get(key)
                if cond is not None:
                    cond.notify_all()
                    notified.add(key)

            # 表级锁释放 → 通知所有等待该表的 key
            for key in released_keys:
                if key[0] == 'TABLE':
                    table = key[1]
                    for k, cond in list(self.key_conds.items()):
                        if isinstance(k, tuple) and k[0] == table \
                                and k not in notified:
                            cond.notify_all()
                            notified.add(k)

    # ==================== 内部: 授予/检查 ====================

    def _can_acquire(self, key, lock_type, txn_id) -> bool:
        """
        允许:
        1. key 无锁
        2. 只有自己的锁
        3. 只有自己 + 兼容锁
        4. 同事务重入
        5. S → X 升级（仅当无其他 holder）
        """
        entries = self.locks.get(key, [])
        if not entries:
            return True

        my_entries = [e for e in entries if e.txn_id == txn_id]
        other_entries = [e for e in entries if e.txn_id != txn_id]

        if not my_entries:
            for e in other_entries:
                if not COMPAT.get((lock_type, e.lock_type), False):
                    return False
            return True

        # 已有锁
        if lock_type == LockType.X:
            if any(e.lock_type == LockType.S for e in my_entries):
                # S → X 升级
                return not other_entries
            return True

        # 请求 S / IS / IX
        for e in other_entries:
            if not COMPAT.get((lock_type, e.lock_type), False):
                return False
        return True

    def _grant(self, key, lock_type, txn_id):
        """授予锁；已持有则升级"""
        entries = self.locks[key]
        existing = None
        for e in entries:
            if e.txn_id == txn_id:
                existing = e
                break

        if existing is not None:
            existing.lock_type = lock_type
        else:
            entries.append(LockEntry(txn_id, lock_type))

        self.txn_locks[txn_id].add(key)

        # 表级意向锁
        if key[0] != 'TABLE':
            table_key = ('TABLE', key[0])
            intent = (LockType.IS if lock_type == LockType.S
                      else LockType.IX)
            if not self._has_lock(table_key, txn_id, intent):
                self.locks[table_key].append(
                    LockEntry(txn_id, intent))
                self.txn_locks[txn_id].add(table_key)

    def _has_lock(self, key, txn_id, lock_type) -> bool:
        for entry in self.locks.get(key, []):
            if entry.txn_id == txn_id and entry.lock_type == lock_type:
                return True
        return False

    # ==================== 死锁检测: 三色 DFS ====================

    def _detect_cycle(self, start) -> bool:
        """
        三色 DFS 检测有向图中是否存在环。
        - WHITE: 未访问
        - GRAY:  在当前递归栈上
        - BLACK: 已完成
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {}

        def dfs(node):
            color[node] = GRAY
            for nxt in self.wait_graph.get(node, set()):
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE:
                    if dfs(nxt):
                        return True
            color[node] = BLACK
            return False

        return dfs(start)

    def _clear_wait_graph(self, txn_id):
        self.wait_graph.pop(txn_id, None)
        for s in self.wait_graph.values():
            s.discard(txn_id)

    # ==================== 统计 ====================

    def stats(self) -> dict:
        with self.lock:
            return {
                'total_locks': sum(
                    len(v) for v in self.locks.values()),
                'waiting_txns': len(self.wait_graph),
                'key_conds': len(self.key_conds),
                'locks_detail': {
                    str(k): len(v)
                    for k, v in self.locks.items() if v
                },
            }