"""
表对象 - 集成 MVCC + 行级锁（修复版）

修复:
- MVCC add_undo / find_visible_version 传 table=self.name
- WAL 写入顺序
- notnull / primary 检查
- 主键重复检测

事务原子性 / 持久化 / 崩溃恢复批次（本文件）:
- **真 ROLLBACK**: 每条 UPDATE/DELETE 把自己的物理镜像
  (UndoAction) 压进 mvcc.physical_undo[txn_id]，abort 时由
  abort_txn() 倒序回放，把旧行**写回原页原槽位**；INSERT 的
  回滚是删除该行并摘掉索引项。旧实现只丢 MVCC 版本链节点，
  页上被覆写/物理删除的数据不会回来，ROLLBACK 是假的。
- **xmax 不再恒为 0**: DELETE / UPDATE 淘汰旧版本时把真实
  事务号写进旧行的 xmax，读路径按 read_view 过滤（见
  _is_visible()）。autocommit 没有真实事务号，用高位合成号
  （_next_autocommit_tid），mvcc 侧有 SYNTHETIC_TXN_FLOOR
  把它们当已提交。
- **锁不泄漏**: autocommit 语句不再用 txn_id=0 拿锁却永不释放
  （database.py 只 release 真实 tid）。_stmt_tid() 为每条
  autocommit 语句分配独立合成事务号，语句结束立刻
  lock_manager.release()。
- **崩溃恢复**: recover_from_wal() 用 WAL.analyze() 做
  redo(已提交) + undo(loser，倒序)，全部幂等；恢复成功后才
  truncate WAL。

已知限制:
- 合成事务号是进程内递减的高位 uint32（>= SYNTHETIC_TXN_FLOOR），
  重启后从 0xFFFFFFFF 重新开始，理论上会与上一进程写进页里的
  合成号重复；因为这类号一律按"已提交"处理，重复不会破坏可见性。
- 没有页 LSN / checkpoint 联动，恢复是"日志全量重放 + 幂等覆盖"，
  WAL 会随写入线性增长直到下次启动 truncate。
- 非整型主键不建索引（_rebuild_index 只收 isinstance(pk, int)），
  与本次修复无关，保持原状。
"""
import json
import re
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.page import PAGE_TYPE_HEAP
from ..storage.buffer_pool import BufferPool
from ..storage.btree import BTree
from ..storage.wal import WAL
from ..storage.lock_manager import (
    LockManager, LockType, DeadlockError)
from ..storage.mvcc import MVCCManager, UndoAction
from ..logger import logger


ROW_HEADER = 13  # xmin(4) + xmax(4) + flags(1) + len(4)

# autocommit 语句用的合成事务号。
#
# 行头里 xmin/xmax 是 struct '<I'（uint32），所以事务号必须落在
# [0, 2**32) 内，不能用负数。这里从 2**32-1 往下分配，形成一段
# 独立的高位区间，与 mvcc 从 1 开始递增的真实事务号永不冲突；
# 配套地在 MVCCManager 里加了一个"合成号恒视为已提交"的阈值判断。
SYNTHETIC_TXN_BASE = 0xFFFFFFFF
_synth_tid = SYNTHETIC_TXN_BASE
_synth_tid_lock = threading.Lock()


def _next_autocommit_tid() -> int:
    """分配一个 autocommit 语句专用的合成事务号（高位区间递减）"""
    global _synth_tid
    with _synth_tid_lock:
        _synth_tid -= 1
        if _synth_tid <= SYNTHETIC_TXN_BASE // 2:
            # 极端情况下回绕，正常 workload 到不了这里
            _synth_tid = SYNTHETIC_TXN_BASE
        return _synth_tid


def coerce(value, col_type: str):
    """类型转换"""
    if value is None:
        return None
    t = col_type.upper()
    try:
        if t in ('INT', 'BIGINT'):
            return int(value)
        if t in ('FLOAT', 'DOUBLE', 'DECIMAL'):
            return float(value)
        if t == 'BOOLEAN':
            if isinstance(value, str):
                return value.upper() in ('1', 'TRUE', 'YES', 'ON')
            return bool(value)
        return str(value)
    except (ValueError, TypeError):
        raise ValueError(f"无法将 {value!r} 转换为 {col_type}")


class Table:
    """表对象"""

    def __init__(self, name: str, cols: List[Dict], data_dir: str,
                 lock_manager: LockManager = None,
                 mvcc: MVCCManager = None):
        self.name = name
        self.cols = cols
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.lock_manager = lock_manager or LockManager()
        self.mvcc = mvcc or MVCCManager()

        self.pk_idx = next(
            (i for i, c in enumerate(cols) if c.get('primary')), 0)
        self.pk_col = cols[self.pk_idx]['name']
        self.pk_type = cols[self.pk_idx]['type']

        self.heap_path = str(self.data_dir / f"{name}.dat")
        self.btroot_path = self.data_dir / f"{name}.btroot"
        self.wal_path = str(self.data_dir / f"{name}.wal")

        self.bp = BufferPool(self.heap_path, capacity=500)
        self.wal = WAL(self.wal_path)

        root_id = self._load_btree_root()
        self.bt = BTree(self.bp, root_page_id=root_id)
        self._save_btree_root(self.bt.root_page_id)

        self._rebuild_index()
        self._lock = threading.RLock()
        self._next_pk = self._calc_next_pk()

        # 事务号 -> ReadView（供写路径的可见性判断 + 恢复用）
        self._read_views: Dict[int, Any] = {}

    # ==================== 事务辅助 ====================

    def _stmt_tid(self, txn_id: int) -> int:
        """
        归一化事务号。
        调用方传 0 表示 autocommit（executor 在没有显式事务时不带
        txn_id）。此时分配一个**独立合成事务号**，这样：
          - 行头的 xmin/xmax 拿到真实非零值，不再恒为 0；
          - 锁归属于这个合成号，语句结束就能精确 release，
            不会像 txn_id=0 那样永远泄漏。
        """
        return txn_id if txn_id else _next_autocommit_tid()

    def _cleanup_stmt(self, tid: int, txn_id: int,
                      pks=None, read_view=None):
        """
        autocommit 语句收尾。

        必须做两件事，缺一不可：
        1) 写 COMMIT 记录（fsync）。autocommit 的 WAL 记录如果只有
           INSERT/UPDATE/DELETE 而没有 COMMIT，崩溃恢复的
           WAL.analyze() 会把它当成 **loser 事务** 去 undo，于是
           "已提交但页还没刷" 的 autocommit 写入会被错误地撤销
           （实测：干净 shutdown 后重开只剩显式事务那一行）。
        2) 释放行锁。旧实现用 txn_id=0 拿锁、database.py 只 release
           真实 tid，锁永久泄漏。
        """
        if txn_id:
            return
        try:
            self.wal.commit_async(tid)
        except Exception as e:
            logger.warn(f"autocommit COMMIT 落盘失败: {e}")
        self.lock_manager.release(tid)
        self._read_views.pop(tid, None)

    def register_read_view(self, txn_id: int, read_view):
        if txn_id:
            self._read_views[txn_id] = read_view

    def _is_visible(self, xmin, xmax, read_view) -> bool:
        """
        读路径可见性。
        read_view 为 None（autocommit 读）时按"已提交即可见"处理：
        xmax == 0 可见；xmax 非 0 说明该行已被某事务删除/更新，
        而本读没有快照 → 不可见（删除已生效）。
        注意：负数 xmax 是 autocommit 删除的标记，同样不可见。
        """
        if read_view is None:
            return xmax == 0
        return read_view.is_visible(xmin, xmax)

    # ==================== 元数据 ====================

    def _load_btree_root(self):
        if self.btroot_path.exists():
            try:
                return int(self.btroot_path.read_text().strip())
            except Exception:
                return None
        return None

    def _save_btree_root(self, root_id: int):
        self.btroot_path.write_text(str(root_id))

    def _rebuild_index(self):
        """
        索引重建：只在 BTree 为空时扫描 heap 页重建。

        ★ 必须跳过"墓碑"（xmax != 0 的历史版本）：DELETE/UPDATE 会
        把旧行按 xmax=事务号 留在页上，它们不在索引里也不可读；
        如果重建时把它们也建进索引，已删除的行就会"复活"。
        启动时页上 xmax != 0 只可能是**已提交**的删除/更新
        （未提交的会被回滚改成 xmax=0 或物理移除），所以这里直接
        按 xmax == 0 过滤是安全的。
        """
        if self.bt.count() > 0:
            return
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page and page.page_type == PAGE_TYPE_HEAP:
                for sid, rb in page.iter_rows():
                    r = self._decode_row_full(rb)
                    if not r:
                        continue
                    row, _xmin, xmax = r
                    if xmax != 0:
                        continue        # 墓碑：不可见，不建索引
                    pk = row[self.pk_idx]
                    if isinstance(pk, int):
                        self.bt.insert(pk, pid, sid)

    def _calc_next_pk(self) -> int:
        max_pk = 0
        for k, _, _ in self.bt.scan():
            if isinstance(k, int) and k > max_pk:
                max_pk = k
        return max_pk + 1

    def _col_idx(self, name: str) -> int:
        for i, c in enumerate(self.cols):
            if c['name'] == name:
                return i
        raise KeyError(f"列 {name} 不存在")

    # ==================== 行编解码 ====================

    def _encode_row(self, values: List,
                    xmin: int = 0, xmax: int = 0) -> bytes:
        jd = json.dumps(
            values, ensure_ascii=False, default=str).encode('utf-8')
        return struct.pack(
            '<IIBI', xmin, xmax, 0, len(jd)) + jd

    def _decode_row_full(self, rb: bytes):
        if len(rb) < ROW_HEADER:
            return None
        try:
            xmin, xmax, flags, dlen = struct.unpack_from(
                '<IIBI', rb, 0)
            if len(rb) < ROW_HEADER + dlen:
                return None
            values = json.loads(
                rb[ROW_HEADER:ROW_HEADER + dlen].decode('utf-8'))
            return values, xmin, xmax
        except Exception:
            return None

    def _decode_row(self, rb: bytes):
        r = self._decode_row_full(rb)
        return r[0] if r else None

    # ==================== 写操作 ====================

    def insert(self, values: List, txn_id: int = 0,
               read_view=None) -> int:
        """插入一行

        txn_id=0 表示 autocommit：内部会分配合成事务号，
        WAL 仍按 0 号记录（WAL.find_committed_transactions 把 0 号
        当隐式提交），锁则挂在合成号上并在语句结束释放。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            owned = bool(txn_id)
            try:
                return self._insert_locked(
                    values, tid, read_view, owned)
            finally:
                self._cleanup_stmt(tid, txn_id)

    def _insert_locked(self, values: List, tid: int,
                       read_view, owned: bool) -> int:
        # 补齐列
        if len(values) < len(self.cols):
            values = list(values) + [None] * (
                len(self.cols) - len(values))

        # 类型转换 + 约束检查
        row = []
        for i, c in enumerate(self.cols):
            v = coerce(values[i], c['type'])
            if c.get('notnull') and v is None:
                raise ValueError(
                    f"列 {c['name']} 不允许为空")
            row.append(v)

        # 主键
        pk = row[self.pk_idx]
        if pk is None:
            pk = self._next_pk
            row[self.pk_idx] = pk

        # 加锁
        self._acquire_row_lock(tid, pk)

        # 检查主键重复
        loc = self.bt.search(pk)
        if loc is not None:
            page = self.bp.get_page(loc[0])
            rb = page.get_row(loc[1])
            r = self._decode_row_full(rb)
            if r:
                _, old_xmin, old_xmax = r
                # 只有"当前可见"的行才算重复；已被本事务或
                # 其他事务标记删除的历史版本可以复用主键。
                visible = self._is_visible(
                    old_xmin, old_xmax, read_view)
                if visible:
                    raise ValueError(f"主键重复: {pk}")

        # WAL（log-before-data）。flush 到 OS 必须发生在改页**之前**：
        # 否则脏页可能被驱逐落盘，而日志还留在 Python 缓冲里 ——
        # 崩溃后就出现"页改了、日志没了"，恢复无法撤销。
        self.wal.log(tid, 'INSERT', {
            'table': self.name, 'pk': pk, 'row': row,
        })
        self.wal.flush_to_os()

        # 写堆
        rb = self._encode_row(row, xmin=tid, xmax=0)
        page_id, slot_id = self._heap_insert(rb)
        if page_id is None:
            raise RuntimeError("存储空间不足")

        # 写索引
        self.bt.insert(pk, page_id, slot_id)
        self._next_pk = max(self._next_pk, pk + 1)

        # 物理 undo：本事务插入的行，回滚时删除
        if owned:
            self.mvcc.push_undo(tid, UndoAction(
                'INSERT', self.name, pk, txn_id=tid,
                page_id=page_id, slot_id=slot_id))
        return 1

    def _acquire_row_lock(self, tid, pk):
        """拿行级 X 锁，超时/死锁统一转 RuntimeError"""
        try:
            ok = self.lock_manager.acquire(
                tid, (self.name, pk), LockType.X, timeout=5.0)
            if not ok:
                raise RuntimeError("获取锁超时")
        except DeadlockError as e:
            raise RuntimeError(str(e))

    def _heap_insert(self, rb: bytes):
        last = None
        for pid in range(1, self.bp.total_pages()):
            p = self.bp.get_page(pid)
            if p and p.page_type == PAGE_TYPE_HEAP:
                last = p
        if last is None:
            last = self.bp.new_page(PAGE_TYPE_HEAP)
        sid = last.insert_row(rb)
        if sid is None:
            last = self.bp.new_page(PAGE_TYPE_HEAP)
            sid = last.insert_row(rb)
            if sid is None:
                return None, None
        return last.page_id, sid

    def update(self, set_pairs: Dict,
               where: Optional[Dict],
               txn_id: int = 0,
               read_view=None) -> int:
        """更新

        原地覆写时把旧行完整字节压入物理 undo；空间不足需要搬到
        新页时，额外记录"键->原页原槽位"，回滚时把索引项也搬回去。
        旧版本行头的 xmax 写真实事务号（autocommit 写负数合成号），
        供读视图过滤并发未提交的更新。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            owned = bool(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            try:
                return self._update_locked(
                    set_pairs, where, tid, read_view, owned)
            finally:
                self._cleanup_stmt(tid, txn_id)

    def _update_locked(self, set_pairs, where, tid,
                       read_view, owned) -> int:
        pks = self._find_pks(where, read_view)
        n = 0
        for pk in pks:
            self._acquire_row_lock(tid, pk)
            undo_act = None

            loc = self.bt.search(pk)
            if loc is None:
                continue

            page = self.bp.get_page(loc[0])
            if page is None:
                continue
            rb = page.get_row(loc[1])
            r = self._decode_row_full(rb)
            if r is None:
                continue

            old_row, old_xmin, old_xmax = r

            # ★ MVCC 加表名（版本链，供读视图回查）
            self.mvcc.add_undo(
                pk, old_xmin, tid, old_row,
                table=self.name)

            new = list(old_row)
            for col, val in set_pairs.items():
                ci = self._col_idx(col)
                if ci == self.pk_idx:
                    raise ValueError("不允许修改主键")
                new[ci] = coerce(val, self.cols[ci]['type'])

            # ★ xmax 写真实事务号：并发读者靠它把这条未提交的
            #   更新挡在外面（同时保留旧的 xmin 语义在 MVCC 链里）。
            old_ver = self._encode_row(
                old_row, xmin=old_xmin, xmax=tid)
            nrb = self._encode_row(new, xmin=tid, xmax=0)

            self.wal.log(tid, 'UPDATE', {
                'table': self.name, 'pk': pk,
                'old': old_row, 'new': new,
                'old_loc': [loc[0], loc[1]],
                'old_b': self._encode_row(
                    old_row, xmin=old_xmin, xmax=0).hex(),
                'tomb_b': old_ver.hex(),
            })
            self.wal.flush_to_os()      # 先落日志，再改页

            if owned:
                undo_act = self.mvcc.push_undo(tid, UndoAction(
                    'UPDATE', self.name, pk, txn_id=tid, rb=rb,
                    page_id=loc[0], slot_id=loc[1]))

            # ★ 先把旧版本按"已删除"标记写回（xmax = 本事务号），
            #   它就成了页上的历史版本；再把新版本放进去。
            #   旧实现的旧版本只存在于内存 undo 镜像里。
            if not page.update_row_inplace(loc[1], old_ver):
                page.restore_row(loc[1], old_ver)

            np, ns = self._heap_insert(nrb)
            if np is None:
                raise RuntimeError("存储空间不足")
            self.bt.delete(pk)
            self.bt.insert(pk, np, ns)

            # 回滚时还要把这个新版本清掉（否则页上留一个不可见死行）
            if owned and undo_act is not None:
                undo_act.new_page_id = np
                undo_act.new_slot_id = ns
            n += 1
        return n

    def delete(self, where: Optional[Dict],
               txn_id: int = 0, read_view=None) -> int:
        """删除

        物理摘除行 + 索引项，同时把旧行完整字节压入物理 undo，
        回滚时按原页原槽位复活；xmax 写真实事务号。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            owned = bool(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            try:
                return self._delete_locked(
                    where, tid, read_view, owned)
            finally:
                self._cleanup_stmt(tid, txn_id)

    def _delete_locked(self, where, tid, read_view, owned) -> int:
        pks = self._find_pks(where, read_view)
        n = 0
        for pk in pks:
            self._acquire_row_lock(tid, pk)

            loc = self.bt.search(pk)
            if loc is None:
                continue

            page = self.bp.get_page(loc[0])
            if page is None:
                continue
            rb = page.get_row(loc[1])
            r = self._decode_row_full(rb)
            if r is None:
                continue

            old_row, old_xmin, _ = r

            # ★ MVCC 加表名（版本链）
            self.mvcc.add_undo(
                pk, old_xmin, tid, old_row,
                table=self.name)

            # ★ 旧版本行头写入真实删除事务号（autocommit 为合成号），
            #   然后把它作为"墓碑"留在页上：不在索引里、对任何读视图
            #   都不可见，xmax 才是真实值而不是恒为 0。
            old_ver = self._encode_row(
                old_row, xmin=old_xmin, xmax=tid)

            self.wal.log(tid, 'DELETE', {
                'table': self.name, 'pk': pk,
                'old': old_row,
                # old_b = 事务前的**干净前像**（xmax=0），撤销时原样写回
                'old_b': self._encode_row(
                    old_row, xmin=old_xmin, xmax=0).hex(),
                'tomb_b': old_ver.hex(),
                'old_loc': [loc[0], loc[1]],
            })
            self.wal.flush_to_os()      # 先落日志，再改页

            if owned:
                self.mvcc.push_undo(tid, UndoAction(
                    'DELETE', self.name, pk, txn_id=tid, rb=rb,
                    page_id=loc[0], slot_id=loc[1]))

            if not page.update_row_inplace(loc[1], old_ver):
                page.restore_row(loc[1], old_ver)
            self.bt.delete(pk)
            n += 1
        return n

    # ==================== 读操作 ====================

    def _find_pks(self, where: Optional[Dict],
                  read_view=None) -> List:
        if where is None:
            result = []
            for k, pid, sid in self.bt.scan():
                page = self.bp.get_page(pid)
                if page is None:
                    continue
                rb = page.get_row(sid)
                r = self._decode_row_full(rb)
                if r is None:
                    continue
                _, xmin, xmax = r
                if self._is_visible(xmin, xmax, read_view):
                    result.append(k)
            return result

        # 主键等值
        if 'column' in where:
            col, op, val = (where['column'], where['op'],
                            where['value'])
            if col == self.pk_col and op == '=':
                try:
                    pk = coerce(val, self.pk_type)
                    loc = self.bt.search(pk)
                    if loc is None:
                        return []
                    page = self.bp.get_page(loc[0])
                    if page is None:
                        return []
                    rb = page.get_row(loc[1])
                    r = self._decode_row_full(rb)
                    if r is None:
                        return []
                    row, xmin, xmax = r
                    if not self._is_visible(xmin, xmax, read_view):
                        # 从 MVCC 历史
                        hist = self.mvcc.find_visible_version(
                            pk, read_view, table=self.name)
                        if hist:
                            row = hist[0]
                        else:
                            return []
                    if self._match(row, where):
                        return [pk]
                    return []
                except Exception:
                    return []

        # 全表
        result = []
        for k, pid, sid in self.bt.scan():
            page = self.bp.get_page(pid)
            if page is None:
                continue
            rb = page.get_row(sid)
            r = self._decode_row_full(rb)
            if r is None:
                continue
            row, xmin, xmax = r
            if not self._is_visible(xmin, xmax, read_view):
                hist = self.mvcc.find_visible_version(
                    k, read_view, table=self.name)
                if hist:
                    row = hist[0]
                else:
                    continue
            if self._match(row, where):
                result.append(k)
        return result

    def _match(self, row, where):
        if 'and' in where:
            return all(self._match(row, c)
                       for c in where['and'])
        if 'or' in where:
            return any(self._match(row, c)
                       for c in where['or'])

        ci = self._col_idx(where['column'])
        l = row[ci]
        op = where['op']
        try:
            r = coerce(where['value'], self.cols[ci]['type'])
        except Exception:
            r = where['value']

        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        if op == '=': return l == r
        if op == '!=': return l != r
        if op == '<': return l < r
        if op == '>': return l > r
        if op == '<=': return l <= r
        if op == '>=': return l >= r
        if op == 'LIKE':
            pat = str(r).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(l)))
        return False

    def select(self, columns, where=None, order_by=None,
               limit=None, read_view=None):
        with self._lock:
            pks = self._find_pks(where, read_view)
            rows = []
            for pk in pks:
                loc = self.bt.search(pk)
                if loc is not None:
                    page = self.bp.get_page(loc[0])
                    rb = page.get_row(loc[1]) if page else None
                    r = self._decode_row_full(rb)
                    if r:
                        row, xmin, xmax = r
                        if self._is_visible(xmin, xmax, read_view):
                            rows.append(row)
                            continue
                if read_view is not None:
                    hist = self.mvcc.find_visible_version(
                        pk, read_view, table=self.name)
                    if hist:
                        rows.append(hist[0])

        has_agg = any(
            re.match(r'\w+\s*\(', c.strip())
            for c in columns if c != '*')

        if columns == ['*']:
            out_cols = [c['name'] for c in self.cols]
            out_rows = rows
        elif has_agg:
            out_cols = [c.upper().strip() for c in columns]
            out_rows = [self._aggregate(rows, columns)]
        else:
            out_cols = [c.strip() for c in columns]
            out_rows = [
                [r[self._col_idx(c.strip())] for c in columns]
                for r in rows
            ]

        if order_by:
            col, desc = order_by
            try:
                i = out_cols.index(col)
                out_rows.sort(
                    key=lambda r: (r[i] is None, r[i]),
                    reverse=desc)
            except Exception:
                pass

        if limit:
            out_rows = out_rows[:limit]

        return out_cols, out_rows

    def _aggregate(self, rows, columns):
        result = []
        for col in columns:
            col = col.strip()
            m = re.match(
                r'(\w+)\s*\(\s*(\*|\w+)\s*\)',
                col, re.IGNORECASE)
            if not m:
                result.append(
                    rows[0][self._col_idx(col)] if rows else None)
                continue
            fn, arg = m.group(1).upper(), m.group(2)
            if fn == 'COUNT':
                if arg == '*':
                    result.append(len(rows))
                else:
                    ci = self._col_idx(arg)
                    result.append(sum(
                        1 for r in rows if r[ci] is not None))
            elif fn in ('SUM', 'AVG', 'MIN', 'MAX'):
                ci = self._col_idx(arg)
                vals = [r[ci] for r in rows if r[ci] is not None]
                if not vals:
                    result.append(None)
                elif fn == 'SUM': result.append(sum(vals))
                elif fn == 'AVG': result.append(
                    sum(vals) / len(vals))
                elif fn == 'MIN': result.append(min(vals))
                elif fn == 'MAX': result.append(max(vals))
            else:
                result.append(None)
        return result

    @property
    def row_count(self) -> int:
        return self.bt.count()

    # ==================== 事务回滚 / 恢复 ====================

    def abort_txn(self, txn_id: int) -> int:
        """
        真回滚：倒序回放本事务的物理 undo 镜像。

        - INSERT -> 删掉该行（堆 + 索引）
        - UPDATE -> 把事务前的旧行字节写回原页原槽位，并把索引项
          指回原位（若之前因空间不足搬到了新页）
        - DELETE -> 按原页原槽位复活旧行，重建索引项

        倒序（LIFO）是必须的：同一行被改多次时只有逆序才能回到
        事务前状态。
        """
        actions = self.mvcc.pop_undo(txn_id, table=self.name)
        restored = 0
        for act in actions:
            try:
                if self._undo_one(act):
                    restored += 1
            except Exception as e:
                logger.warn(f"回滚 {act.op} pk={act.pk} 失败: {e}")
        return restored

    def _undo_one(self, act) -> bool:
        if act.op == 'INSERT':
            # 事务插入的行 -> 删除
            loc = self.bt.search(act.pk)
            if loc is not None:
                self.bt.delete(act.pk)
            elif act.page_id is not None:
                page = self.bp.get_page(act.page_id)
                if page is not None:
                    page.delete_row(act.slot_id)
            self._next_pk = self._calc_next_pk()
            return True

        if act.rb is None:
            return False

        if act.op == 'UPDATE':
            # 先清掉本事务写入的新版本（页上不可见的死行）
            self._discard_dead_version(
                act.new_page_id, act.new_slot_id, act.page_id,
                act.slot_id)
            # 旧行写回原位置
            if not self._put_row_back(act.page_id, act.slot_id,
                                      act.rb):
                np, ns = self._heap_insert(act.rb)
                if np is None:
                    return False
                act.page_id, act.slot_id = np, ns
            # 索引指回原位置
            cur = self.bt.search(act.pk)
            if cur is None or cur != (act.page_id, act.slot_id):
                self.bt.delete(act.pk)
                self.bt.insert(act.pk, act.page_id, act.slot_id)
            return True

        if act.op == 'DELETE':
            # 复活被删的行
            if not self._put_row_back(act.page_id, act.slot_id,
                                      act.rb):
                np, ns = self._heap_insert(act.rb)
                if np is None:
                    return False
                act.page_id, act.slot_id = np, ns
            if self.bt.search(act.pk) is None:
                self.bt.insert(act.pk, act.page_id, act.slot_id)
            self._next_pk = max(self._next_pk, act.pk + 1)
            return True

        return False

    def _discard_dead_version(self, page_id, slot_id,
                              keep_page_id, keep_slot_id):
        """
        丢弃本事务 UPDATE 写出的新版本（回滚时用）。

        注意不要清到旧版本所在槽位：新版本可能和旧版本在同一个
        槽位（例如页满时 heap_insert 又选中同一页），此时跳过。
        """
        if page_id is None:
            return
        if (page_id, slot_id) == (keep_page_id, keep_slot_id):
            return
        page = self.bp.get_page(page_id)
        if page is not None:
            page.delete_row(slot_id)

    def _put_row_back(self, page_id, slot_id, rb: bytes) -> bool:
        """
        把整行字节写回指定的 (page_id, slot_id)。
        依赖 Page.restore_row：槽位空着（被 delete_row 清过）就重新挂上，
        已有数据且空间够就原地覆盖。返回 False 表示该页放不下。
        """
        if page_id is None:
            return False
        page = self.bp.get_page(page_id)
        if page is None:
            return False
        s = slot_id if slot_id is not None else 0
        return page.restore_row(s, rb)

    def recover_from_wal(self):
        """
        崩溃恢复：redo(已提交) + undo(loser)，成功落盘后才 truncate WAL。

        模拟场景是"WAL 已落盘、页未刷盘"：
        - loser（没写 COMMIT 就崩了）的页改动可能已经在盘上，
          必须用 WAL 里的旧行镜像撤销；
        - 已提交事务的页改动可能没落盘，必须按 WAL 重放。
        两个阶段的操作都幂等（覆盖式写 + 按主键定位），
        所以重复执行或页已落盘都不会出错。

        ★ truncate 是有条件的：任何一条 redo/undo 失败、或恢复结果刷盘
        失败，都保留 WAL 原样，让下次启动可以重试；否则一次恢复失败就会
        把已提交的数据永久丢掉（旧实现无条件 truncate）。
        """
        try:
            committed_ops, loser_ops = self.wal.analyze()
        except Exception as e:
            logger.warn(f"WAL 分析失败，跳过恢复 {self.name}: {e}")
            return 0

        replayed = 0
        failed = 0
        for lsn, tid, rtype, data in committed_ops:
            try:
                if self._redo_op(tid, rtype, data):
                    replayed += 1
            except Exception as e:
                failed += 1
                logger.warn(f"重放 lsn={lsn} {rtype} 失败: {e}")

        # loser 必须**倒序**撤销
        undone = 0
        for lsn, tid, rtype, data in reversed(loser_ops):
            try:
                if self._undo_wal_op(rtype, data):
                    undone += 1
            except Exception as e:
                failed += 1
                logger.warn(f"撤销 lsn={lsn} {rtype} 失败: {e}")

        if replayed or undone:
            logger.info(
                f"崩溃恢复 {self.name}: redo {replayed} 条, "
                f"undo {undone} 条")

        if failed:
            logger.warn(
                f"{self.name}: {failed} 条 WAL 操作恢复失败，"
                f"保留 WAL 不截断（下次启动重试）")
            try:
                self.flush()
            except Exception:
                pass
            return replayed

        # 恢复结果先落盘，落盘成功之后才允许截断/轮转 WAL
        try:
            self.flush()
        except Exception as e:
            logger.warn(
                f"{self.name}: 恢复结果刷盘失败，保留 WAL 不截断: {e}")
            return replayed
        try:
            self.wal.truncate()
        except Exception as e:
            logger.warn(f"truncate WAL 失败: {e}")
        return replayed

    def _redo_op(self, tid, rtype, data) -> bool:
        """重放一条已提交的操作（幂等）"""
        if rtype == 'INSERT':
            pk = data['pk']
            return self._redo_put(pk, data['row'], xmin=tid)
        if rtype == 'UPDATE':
            return self._redo_update(
                data['pk'], data['new'],
                data.get('old_b'), data.get('old_loc'),
                xmin=tid, tomb_b=data.get('tomb_b'))
        if rtype == 'DELETE':
            return self._redo_delete(
                data['pk'], data.get('old_b'),
                data.get('tomb_b'))
        return False

    def _redo_update(self, pk, values, old_b, old_loc,
                     xmin=0, tomb_b=None) -> bool:
        """
        重放已提交的 UPDATE（幂等）：
        - 若索引当前指向的已经是"新版本"或"墓碑"，说明这条日志
          已经重放过了，直接返回，别把新版本又覆盖成墓碑；
        - 否则把旧版本墓碑化（xmax = tid），再把新版本追加写入。
        """
        new_ver = self._encode_row(values, xmin=xmin, xmax=0)
        loc = self.bt.search(pk)
        if loc is not None and (old_b is not None or tomb_b is not None):
            page = self.bp.get_page(loc[0])
            cur = page.get_row(loc[1]) if page is not None else None
            tomb = (bytes.fromhex(tomb_b) if tomb_b
                    else self._with_xmax(bytes.fromhex(old_b), xmin))
            if cur != new_ver and cur != tomb:
                if not page.update_row_inplace(loc[1], tomb):
                    page.restore_row(loc[1], tomb)
        return self._redo_put(pk, values, xmin=xmin)

    def _redo_delete(self, pk, old_b, tomb_b=None) -> bool:
        loc = self.bt.search(pk)
        if loc is None:
            return False
        page = self.bp.get_page(loc[0])
        if page is None:
            return False
        if old_b is not None or tomb_b is not None:
            tomb = (bytes.fromhex(tomb_b) if tomb_b
                    else bytes.fromhex(old_b))
            cur = page.get_row(loc[1])
            if cur != tomb:
                # 墓碑化（幂等：第二次重放看到的就是墓碑）
                if not page.update_row_inplace(loc[1], tomb):
                    page.restore_row(loc[1], tomb)
        self.bt.delete(pk)
        return True

    @staticmethod
    def _with_xmax(rb: bytes, xmax: int) -> bytes:
        """把行头里的 xmax 换掉（用于旧格式 WAL 的兼容）"""
        b = bytearray(rb)
        struct.pack_into('<I', b, 4, xmax)
        return bytes(b)

    def _redo_put(self, pk, values, xmin=0) -> bool:
        """
        幂等写：主键存在就覆盖（行头 xmax 归零），不存在就插入。
        使用合成高位事务号的行在重启后依然可见。
        """
        rb = self._encode_row(values, xmin=xmin, xmax=0)
        loc = self.bt.search(pk)
        if loc is not None:
            page = self.bp.get_page(loc[0])
            if page is not None:
                if page.update_row_inplace(loc[1], rb):
                    return True
                page.delete_row(loc[1])
                self.bt.delete(pk)
        np, ns = self._heap_insert(rb)
        if np is None:
            return False
        self.bt.insert(pk, np, ns)
        self._next_pk = max(self._next_pk, int(pk) + 1)
        return True

    def _undo_wal_op(self, rtype, data) -> bool:
        """用 WAL 里的旧行镜像撤销 loser 事务的一条操作（幂等）"""
        pk = data.get('pk')
        old_b = data.get('old_b')
        old_loc = data.get('old_loc')

        if rtype == 'INSERT':
            loc = self.bt.search(pk)
            if loc is not None:
                page = self.bp.get_page(loc[0])
                if page is not None:
                    page.delete_row(loc[1])
                self.bt.delete(pk)
                return True
            return False

        if old_b is None:
            return False
        rb = bytes.fromhex(old_b)

        if rtype == 'UPDATE':
            page_id = old_loc[0] if old_loc else None
            slot_id = old_loc[1] if old_loc else None
            if page_id is None:
                return self._redo_put(pk, data['old'])
            acted = self._put_row_back(page_id, slot_id, rb)
            if not acted:
                np, ns = self._heap_insert(rb)
                if np is None:
                    return False
                page_id, slot_id = np, ns
            cur = self.bt.search(pk)
            if cur is None or cur != (page_id, slot_id):
                self.bt.delete(pk)
                self.bt.insert(pk, page_id, slot_id)
            return True

        if rtype == 'DELETE':
            page_id = old_loc[0] if old_loc else None
            slot_id = old_loc[1] if old_loc else None
            if page_id is None:
                return self._redo_put(
                    pk, data['old'], xmin=self._row_xmin(rb))
            if not self._put_row_back(page_id, slot_id, rb):
                np, ns = self._heap_insert(rb)
                if np is None:
                    return False
                page_id, slot_id = np, ns
            if self.bt.search(pk) is None:
                self.bt.insert(pk, page_id, slot_id)
            self._next_pk = max(self._next_pk, int(pk) + 1)
            return True

        return False

    @staticmethod
    def _row_xmin(rb: bytes) -> int:
        try:
            return struct.unpack_from('<I', rb, 0)[0]
        except Exception:
            return 0

    def flush(self):
        self.bp.flush()
        self.wal.flush()
        self._save_btree_root(self.bt.root_page_id)

    def close(self):
        """
        干净关闭：先把脏页 + BTree 根落盘（数据已持久），
        再 rotate/truncate WAL。

        为什么可以 truncate：close() 意味着本实例不再写，页已经
        完整落盘，WAL 里已提交的改动不再需要重放；没提交的残片
        本来就要丢弃。不 truncate 的话 WAL 会无限增长，而且下次
        启动要对整份历史做全量 redo。
        """
        self.flush()
        try:
            self.wal.truncate()
        except Exception as e:
            logger.warn(f"关闭时 truncate WAL 失败: {e}")
        self.bp.close()
        self.wal.close()

    def stats(self) -> dict:
        return {
            'table': self.name,
            'rows': self.row_count,
            'pages': self.bp.total_pages(),
            'btree_height': self.bt.height(),
            **self.bp.stats(),
        }