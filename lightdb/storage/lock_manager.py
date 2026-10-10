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

        # key -> 正在 cond.wait() 上等待的线程数。
        # ★ 性能关键 1：release() 旧实现遍历**所有见过的 key**（含从未
        #   有人等待的）逐个 notify_all()，autocommit 场景下每个主键
        #   都会留下一个 key_conds 表项，于是每次放锁都变成
        #   O(历史主键数)。
        self._waiters = defaultdict(int)

        # 表名 -> {正在等待该表下某个行 key 的 key}
        # ★ 性能关键 2：表级意向锁释放时，旧实现（以及按 key_conds
        #   过滤的中间版本）都要扫"该表下出现过的全部行 key"，同样是
        #   O(历史行数)。这里只登记**真的有人在等**的 key，没有竞争时
        #   集合为空，放锁就是 O(1)。
        self._waiting_by_table = defaultdict(set)

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
                    # 成功拿到锁 → 不再等待：只清自己的出边。
                    # 绝不能顺手把"别人等我"的边也删掉（那会破坏死锁检测）。
                    self._clear_wait_graph(txn_id)
                    return True

                # 清理自己的旧等待关系，重新记录当前 holder
                self._clear_wait_graph(txn_id)

                holders = {e.txn_id for e in self.locks.get(key, [])
                           if e.txn_id != txn_id}
                for h in holders:
                    self.wait_graph[txn_id].add(h)

                # 死锁检测
                if holders and self._detect_cycle(txn_id):
                    self._drop_waiter(txn_id)
                    raise DeadlockError(
                        f"死锁检测: 事务 {txn_id} 被选为受害者")

                remaining = deadline - time.time()
                if remaining <= 0:
                    # 放弃等待 → 双向清理，避免留下陈旧边造成误判
                    self._drop_waiter(txn_id)
                    return False

                self._waiters[key] += 1
                if key[0] != 'TABLE':
                    self._waiting_by_table[key[0]].add(key)
                try:
                    cond.wait(timeout=min(remaining, 0.5))
                finally:
                    self._waiters[key] -= 1
                    if not self._waiters[key]:
                        del self._waiters[key]
                        if key[0] != 'TABLE':
                            tbl_set = self._waiting_by_table.get(key[0])
                            if tbl_set is not None:
                                tbl_set.discard(key)
                                if not tbl_set:
                                    self._waiting_by_table.pop(
                                        key[0], None)

    def release(self, txn_id: int):
        """
        释放事务持有的所有锁，并只通知等待相关 key 的事务。

        ★ 性能：旧实现无条件遍历 key_conds 全表逐个 notify_all()，
        而 key_conds 会为每个出现过的行 key 留一个表项、永不回收，
        于是每次放锁都是 O(历史行数)。autocommit 场景下每个 INSERT
        都放一次锁，插入 3000 行时 notify_all 被调用百万次。
        现在只遍历**本事务真正持有的 key**，并且只有该 key 上确实
        有线程在 wait（_waiters > 0）才通知。
        """
        with self.lock:
            held = list(self.txn_locks.get(txn_id, ()))
            if not held:
                self.txn_locks.pop(txn_id, None)
                self._drop_waiter(txn_id)
                return

            released_keys = []
            touched_tables = []
            for key in held:
                entries = self.locks.get(key)
                if entries:
                    kept = [e for e in entries if e.txn_id != txn_id]
                    if kept:
                        self.locks[key] = kept
                    else:
                        del self.locks[key]
                released_keys.append(key)
                if key[0] == 'TABLE':
                    touched_tables.append(key[1])

            self.txn_locks.pop(txn_id, None)
            self._drop_waiter(txn_id)

            # 按 key 通知：只通知**真的有人在等**的 key
            notified = set()
            for key in released_keys:
                if self._waiters.get(key) and key not in notified:
                    cond = self.key_conds.get(key)
                    if cond is not None:
                        cond.notify_all()
                        notified.add(key)

            # 表级意向锁释放 → 通知等待该表下任何行的事务。
            # _waiting_by_table 只包含"当前真的有人在等"的 key，
            # 没有竞争时是空集合。
            for table in touched_tables:
                for k in list(self._waiting_by_table.get(table, ())):
                    if k in notified:
                        continue
                    cond = self.key_conds.get(k)
                    if cond is not None:
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
        """
        只清除 txn_id 的**出边**（它不再等待别人）。
        修复: 旧实现同时把 txn_id 从所有其他事务的等待集合里删掉，
        等于单方面抹掉"别人正在等我"这条边，双向等待因此常常检测不到环，
        两个事务最后一起超时（死锁只能靠 5 秒超时兜底）。
        """
        self.wait_graph.pop(txn_id, None)

    def _drop_waiter(self, txn_id):
        """
        事务彻底退出等待（拿到锁/超时/被选为死锁受害者/事务结束）时，
        双向清理它的等待关系，避免陈旧边导致误报。
        """
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