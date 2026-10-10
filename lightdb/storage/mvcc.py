"""
MVCC 版本控制（修复版）
- 行头: xmin (创建事务ID) + xmax (删除事务ID) + undo_ptr
- undo 区域: 存历史版本
- ReadView: 快照可见性（包含 aborted 集合）

修复历史:
- 第二批: is_visible 增加 aborted 检查
- 第二批: undo_log key 改为 (table, pk)
- 第二批: 新增 rollback_undo(txn_id)
- 第三批: gc() 重写，逻辑清晰
"""
import threading


class UndoRecord:
    __slots__ = ('table', 'pk', 'xmin', 'xmax', 'data', 'next_undo')

    def __init__(self, table, pk, xmin, xmax, data, next_undo=None):
        self.table = table
        self.pk = pk
        self.xmin = xmin
        self.xmax = xmax
        self.data = data
        self.next_undo = next_undo


class ReadView:
    """事务快照"""

    __slots__ = ('txn_id', 'active', 'min_id', 'max_id', 'aborted')

    def __init__(self, txn_id, active_ids, min_id, max_id,
                 aborted_ids=None):
        self.txn_id = txn_id
        self.active = set(active_ids)
        self.min_id = min_id
        self.max_id = max_id
        self.aborted = set(aborted_ids or ())

    def is_visible(self, xmin, xmax) -> bool:
        """可见性判断"""
        # ---------- 创建者 ----------
        if xmin == self.txn_id:
            pass
        elif xmin in self.aborted:
            return False
        elif xmin in self.active:
            return False
        elif xmin >= self.max_id:
            return False

        # ---------- 删除者 ----------
        if xmax == 0:
            return True
        if xmax == self.txn_id:
            return False
        if xmax in self.aborted:
            return True
        if xmax in self.active:
            return True
        if xmax >= self.max_id:
            return True
        return False


class MVCCManager:
    """MVCC 管理器"""

    def __init__(self):
        self.lock = threading.RLock()
        self._next_txn_id = 1
        self.active_txns = {}
        self.committed_txns = set()
        self.aborted_txns = set()

        # undo 链: {(table, pk) -> UndoRecord}
        self.undo_log = {}

    # ==================== 事务 ====================

    def next_txn_id(self) -> int:
        with self.lock:
            tid = self._next_txn_id
            self._next_txn_id += 1
            return tid

    def create_readview(self, txn_id: int) -> ReadView:
        with self.lock:
            active = set(self.active_txns.keys())
            min_id = min(active) if active else txn_id
            max_id = self._next_txn_id
            rv = ReadView(
                txn_id, active, min_id, max_id,
                aborted_ids=self.aborted_txns)
            self.active_txns[txn_id] = rv
            return rv

    def release(self, txn_id: int, committed: bool = True):
        with self.lock:
            self.active_txns.pop(txn_id, None)
            if committed:
                self.committed_txns.add(txn_id)
            else:
                self.aborted_txns.add(txn_id)

    # ==================== Undo ====================

    def add_undo(self, pk, xmin=None, xmax=None, old_data=None,
                 table=''):
        """
        兼容两种签名:
        - add_undo(pk, xmin, xmax, old_data)
        - add_undo(pk, xmin, xmax, old_data, table=表名)
        """
        if xmin is None or xmax is None:
            raise TypeError("add_undo 需要 xmin / xmax")
        key = (table, pk)
        with self.lock:
            old = self.undo_log.get(key)
            self.undo_log[key] = UndoRecord(
                table, pk, xmin, xmax, old_data, old)

    def find_visible_version(self, pk, rv: ReadView, table=''):
        """找对 rv 可见的最新版本"""
        key = (table, pk)
        with self.lock:
            node = self.undo_log.get(key)
            while node:
                if rv.is_visible(node.xmin, node.xmax):
                    return (node.data, node.xmin, node.xmax)
                node = node.next_undo
            return None

    def rollback_undo(self, txn_id: int):
        """
        事务回滚时撤销它产生的 undo（把 xmax == txn_id 的版本"复活"）
        """
        with self.lock:
            for key in list(self.undo_log.keys()):
                node = self.undo_log[key]
                chain = []
                while node:
                    if node.xmax == txn_id:
                        node = node.next_undo
                        continue
                    chain.append(node)
                    node = node.next_undo

                if not chain:
                    del self.undo_log[key]
                    continue

                for i in range(len(chain) - 1):
                    chain[i].next_undo = chain[i + 1]
                chain[-1].next_undo = None
                self.undo_log[key] = chain[0]

    # ==================== GC（重写）====================

    def gc(self, oldest_active_txn: int):
        """
        清理不再需要的 undo。
        条件: node.xmax != 0 且 node.xmax < oldest_active_txn
        """
        with self.lock:
            for key in list(self.undo_log.keys()):
                node = self.undo_log[key]
                chain = []
                while node:
                    if not (node.xmax != 0 and
                            node.xmax < oldest_active_txn):
                        chain.append(node)
                    node = node.next_undo

                if not chain:
                    del self.undo_log[key]
                    continue

                for i in range(len(chain) - 1):
                    chain[i].next_undo = chain[i + 1]
                chain[-1].next_undo = None
                self.undo_log[key] = chain[0]

    # ==================== 统计 ====================

    def stats(self) -> dict:
        with self.lock:
            return {
                'next_txn_id': self._next_txn_id,
                'active': len(self.active_txns),
                'committed': len(self.committed_txns),
                'aborted': len(self.aborted_txns),
                'undo_entries': len(self.undo_log),
            }