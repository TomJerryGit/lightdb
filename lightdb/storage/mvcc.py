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
- 事务原子性批次:
  * 新增 **物理 undo 镜像** (UndoAction + push_undo/pop_undo/purge_undo)。
    旧实现只有 undo_log 版本链，abort 时仅丢弃链节点，**页上已被原地
    覆写/物理删除的数据不会回来**，ROLLBACK 是假的。
    现在每条 UPDATE/DELETE 前把"旧行完整字节 + 原页/槽位"压入事务 undo 栈，
    INSERT 记录主键，abort 时由 Table 倒序回放，恢复到事务前状态。
  * `find_visible_version` 语义不变（供读视图回查历史版本）。
  * 物理 undo 只在内存中（按事务号索引）；事务结束后由 purge_undo 丢弃。
    崩溃恢复阶段的撤销来自 WAL 里的旧行镜像，不依赖这里。

已知限制:
- undo_log 版本链不落盘：重启后历史版本只能靠页上 xmax 标记判断，
  重启前的老快照无法再查到被覆盖的旧版本内容。
- 页上被 UPDATE 淘汰的旧版本不会回收（没有 vacuum），只做空间泄漏。
"""
import threading

# 合成事务号下界：Table 给 autocommit 语句分配的高位事务号从
# 2**32-1 往下取（行头 xmin/xmax 是 '<I'，uint32，不能为负）。
# 这些号没有 COMMIT 记录、也不在 active/aborted 集合里，但数值上
# 远大于 max_id；若按普通规则会被判成"未来事务"而不可见，因此
# 显式规定：>= 该阈值的事务号一律视为已提交。
SYNTHETIC_TXN_FLOOR = 0xF0000000


class UndoRecord:
    __slots__ = ('table', 'pk', 'xmin', 'xmax', 'data', 'next_undo')

    def __init__(self, table, pk, xmin, xmax, data, next_undo=None):
        self.table = table
        self.pk = pk
        self.xmin = xmin
        self.xmax = xmax
        self.data = data
        self.next_undo = next_undo


class UndoAction:
    """
    物理回滚镜像（事务原子性用）。

    op:
      'INSERT' - 本事务插入的行，回滚要删掉；rb 为 None
      'UPDATE' - 本事务覆盖的行，回滚要恢复 rb 到 (page_id, slot_id)；
                 新版本所在位置记在 new_page_id/new_slot_id，回滚时
                 一并清掉，避免留下一个不可见的死版本。
      'DELETE' - 本事务删除的行，回滚要恢复 rb 到 (page_id, slot_id)
    rb: 事务之前的**完整行字节**（含原 xmin/xmax 行头）
    txn_id: 产生该修改的事务号（恢复阶段判断"盘上这份改动是不是我的"）
    """
    __slots__ = ('op', 'table', 'pk', 'txn_id', 'rb',
                 'page_id', 'slot_id', 'new_page_id', 'new_slot_id')

    def __init__(self, op, table, pk, txn_id=0, rb=None,
                 page_id=None, slot_id=None,
                 new_page_id=None, new_slot_id=None):
        self.op = op
        self.table = table
        self.pk = pk
        self.txn_id = txn_id
        self.rb = rb
        self.page_id = page_id
        self.slot_id = slot_id
        self.new_page_id = new_page_id
        self.new_slot_id = new_slot_id


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
        """
        可见性判断。

        ★ 合成事务号（>= SYNTHETIC_TXN_FLOOR）一律视为"已提交"：
          Table 给 autocommit 语句分配的高位事务号（0xFFFFFFFF 往下）
          没有 COMMIT 记录、也不在 active/aborted 集合里，但它们
          在数值上远大于 max_id；若按普通规则会被当成"未来事务"而
          不可见，于是 autocommit 写入对自己的读视图就消失了。
        """
        # ---------- 创建者 ----------
        if xmin == self.txn_id:
            pass
        elif xmin >= SYNTHETIC_TXN_FLOOR:
            pass                      # 合成号 = 已提交
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
        if xmax >= SYNTHETIC_TXN_FLOOR:
            return False              # 合成号删除 = 已提交删除
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

        # 物理 undo 栈: {txn_id -> [UndoAction, ...]}（按发生顺序）
        self.physical_undo = {}

    # ==================== 事务 ====================

    def next_txn_id(self) -> int:
        with self.lock:
            tid = self._next_txn_id
            self._next_txn_id += 1
            return tid

    def advance_next_txn_id(self, tid: int) -> int:
        """
        把事务号计数器**单调**推进到不小于 tid（只前进，不后退）。

        为什么需要: 事务号只在内存里递增，重启后又从 1 开始，会和页上
        持久化的行头 xmin/xmax 撞号。读视图的 "xmin >= max_id → 不可见"
        规则会把"重启前最后写入的那些行"当成未来事务写的，于是显式事务
        的 SELECT/UPDATE/DELETE 都看不到它们（实测: 重启后
        `UPDATE t SET v=.. WHERE id=2` 返回 affected=0，行还在，只是
        对该事务不可见；重放该 UPDATE 的 WAL 也因此无效）。
        Database 启动时扫描页上行头 + WAL 得到最大值后调用本方法。
        """
        with self.lock:
            if tid > self._next_txn_id:
                self._next_txn_id = tid
            return self._next_txn_id

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
                # 提交后物理镜像不再需要（防止调用方忘记 purge 造成泄漏）
                for key in list(self.physical_undo.keys()):
                    if key[0] == txn_id:
                        self.physical_undo.pop(key, None)
            else:
                self.aborted_txns.add(txn_id)

    def mark_aborted(self, txn_id: int):
        """
        把某个事务号登记为"已回滚"。

        崩溃恢复用: WAL 分析出的 loser 事务（页上可能留下了它们写的
        新版本，xmin = loser 号）重启后在内存里没有回滚记录，如果不
        登记，读视图会把 xmin < max_id 的它们判成"已提交可见"。
        登记后 ReadView.is_visible 的 xmin in aborted → False，
        老快照/当前读都会跳过这些版本，回滚语义跨重启成立。
        """
        with self.lock:
            self.aborted_txns.add(txn_id)
            self.active_txns.pop(txn_id, None)
            self.committed_txns.discard(txn_id)

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

    # ==================== 物理 undo（真回滚）====================

    def push_undo(self, txn_id: int, action: UndoAction):
        """
        记录一条物理回滚镜像（UPDATE/DELETE 存旧行字节，INSERT 只存主键）。

        返回该 action，调用方可以在后续补记位置信息
        （例如 UPDATE 之后回调填充 new_page_id/new_slot_id）。
        """
        with self.lock:
            self.physical_undo.setdefault(
                (txn_id, action.table), []).append(action)
            return action

    def pop_undo(self, txn_id: int, table: str = None):
        """
        取出并清空事务在指定表上的物理 undo 栈，**倒序**返回（LIFO）。
        倒序是必须的: 同一行被改多次时，只有逆序回放才能回到事务前状态。
        table=None 表示该事务在所有表上的操作（按 (txn, table) 分组拼接）。
        """
        with self.lock:
            out = []
            for key in list(self.physical_undo.keys()):
                k_txn, k_table = key
                if k_txn != txn_id:
                    continue
                if table is not None and k_table != table:
                    continue
                out.extend(self.physical_undo.pop(key))
            return list(reversed(out))

    def purge_undo(self, txn_id: int, table: str = None):
        """丢弃事务的物理 undo（提交 / 回滚完成后不再需要）"""
        with self.lock:
            for key in list(self.physical_undo.keys()):
                k_txn, k_table = key
                if k_txn != txn_id:
                    continue
                if table is not None and k_table != table:
                    continue
                self.physical_undo.pop(key, None)

    def undo_depth(self, txn_id: int, table: str = None) -> int:
        with self.lock:
            n = 0
            for (k_txn, k_table), v in self.physical_undo.items():
                if k_txn != txn_id:
                    continue
                if table is not None and k_table != table:
                    continue
                n += len(v)
            return n

    # ---------- 语句级保存点（显式事务里的"该语句无效"）----------
    #
    # 一条多值 INSERT 在**用户显式事务**里失败时，不能提交/回滚用户事务
    # （那会破坏 BEGIN...COMMIT 语义），但这条语句本身必须无效。做法就是
    # 语句级保存点：记下进入语句时各表物理 undo 栈的深度，失败后只把
    # 这一句追加的那一段倒序回放。

    def undo_marks(self, txn_id: int) -> dict:
        """记录某个事务当前的物理 undo 栈深度（语句级保存点）"""
        with self.lock:
            return {k: len(v) for k, v in self.physical_undo.items()
                    if k[0] == txn_id}

    def peek_undo_after(self, txn_id: int, marks: dict) -> list:
        """**查看**保存点之后追加的 undo 镜像（不取出），按发生顺序"""
        with self.lock:
            out = []
            for key in list(self.physical_undo.keys()):
                if key[0] != txn_id:
                    continue
                depth = (marks or {}).get(key, 0)
                out.extend(self.physical_undo[key][depth:])
            return out

    def pop_undo_after(self, txn_id: int, marks: dict) -> list:
        """取出并清空保存点之后追加的 undo 镜像，**倒序**返回

        倒序是必须的（与 `pop_undo` 同理）：同一行被改多次时只有逆序
        回放才能回到语句前的状态。跨表时按 (事务,表) 分组拼接后再整体
        倒序 —— 单表语句（多值 INSERT 的常规形态）因此是严格 LIFO。
        """
        with self.lock:
            out = []
            for key in list(self.physical_undo.keys()):
                if key[0] != txn_id:
                    continue
                depth = (marks or {}).get(key, 0)
                stack = self.physical_undo[key]
                if len(stack) <= depth:
                    continue
                out.extend(stack[depth:])
                del stack[depth:]
            return list(reversed(out))

    def oldest_active_txn(self) -> int:
        """最小活跃事务号（没有活跃事务时返回下一个待分配号），供 GC 使用"""
        with self.lock:
            return (min(self.active_txns) if self.active_txns
                    else self._next_txn_id)

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
                'physical_undo_txns': len(
                    {k[0] for k in self.physical_undo}),
            }