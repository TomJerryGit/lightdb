"""
表对象 - 集成 MVCC + 行级锁（修复版）

修复:
- MVCC add_undo / find_visible_version 传 table=self.name
- WAL 写入顺序
- notnull / primary 检查
- 主键重复检测
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
from ..storage.mvcc import MVCCManager
from ..logger import logger


ROW_HEADER = 13  # xmin(4) + xmax(4) + flags(1) + len(4)


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
        if self.bt.count() > 0:
            return
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page and page.page_type == PAGE_TYPE_HEAP:
                for sid, rb in page.iter_rows():
                    row = self._decode_row(rb)
                    if row:
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
        """插入一行"""
        with self._lock:
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
            try:
                ok = self.lock_manager.acquire(
                    txn_id, (self.name, pk),
                    LockType.X, timeout=5.0)
                if not ok:
                    raise RuntimeError("获取锁超时")
            except DeadlockError as e:
                raise RuntimeError(str(e))

            # 检查主键重复
            loc = self.bt.search(pk)
            if loc is not None:
                page = self.bp.get_page(loc[0])
                rb = page.get_row(loc[1])
                r = self._decode_row_full(rb)
                if r:
                    _, old_xmin, old_xmax = r
                    visible = (old_xmax == 0)
                    if not visible and read_view is not None:
                        visible = read_view.is_visible(
                            old_xmin, old_xmax)
                    if visible:
                        raise ValueError(f"主键重复: {pk}")

            # WAL
            self.wal.log(txn_id, 'INSERT', {
                'table': self.name, 'pk': pk, 'row': row,
            })

            # 写堆
            rb = self._encode_row(row, xmin=txn_id, xmax=0)
            page_id, slot_id = self._heap_insert(rb)
            if page_id is None:
                raise RuntimeError("存储空间不足")

            # 写索引
            self.bt.insert(pk, page_id, slot_id)
            self._next_pk = max(self._next_pk, pk + 1)
            return 1

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
        """更新"""
        with self._lock:
            pks = self._find_pks(where, read_view)
            n = 0
            for pk in pks:
                try:
                    ok = self.lock_manager.acquire(
                        txn_id, (self.name, pk),
                        LockType.X, timeout=5.0)
                    if not ok:
                        raise RuntimeError("获取锁超时")
                except DeadlockError as e:
                    raise RuntimeError(str(e))

                loc = self.bt.search(pk)
                if loc is None:
                    continue

                page = self.bp.get_page(loc[0])
                rb = page.get_row(loc[1])
                r = self._decode_row_full(rb)
                if r is None:
                    continue

                old_row, old_xmin, old_xmax = r

                # ★ MVCC 加表名
                self.mvcc.add_undo(
                    pk, old_xmin, txn_id, old_row,
                    table=self.name)

                new = list(old_row)
                for col, val in set_pairs.items():
                    ci = self._col_idx(col)
                    if ci == self.pk_idx:
                        raise ValueError("不允许修改主键")
                    new[ci] = coerce(val, self.cols[ci]['type'])

                self.wal.log(txn_id, 'UPDATE', {
                    'table': self.name, 'pk': pk,
                    'old': old_row, 'new': new,
                })

                nrb = self._encode_row(new, xmin=txn_id, xmax=0)
                if not page.update_row_inplace(loc[1], nrb):
                    page.delete_row(loc[1])
                    np, ns = self._heap_insert(nrb)
                    self.bt.delete(pk)
                    self.bt.insert(pk, np, ns)
                n += 1
            return n

    def delete(self, where: Optional[Dict],
               txn_id: int = 0, read_view=None) -> int:
        """删除"""
        with self._lock:
            pks = self._find_pks(where, read_view)
            n = 0
            for pk in pks:
                try:
                    ok = self.lock_manager.acquire(
                        txn_id, (self.name, pk),
                        LockType.X, timeout=5.0)
                    if not ok:
                        raise RuntimeError("获取锁超时")
                except DeadlockError as e:
                    raise RuntimeError(str(e))

                loc = self.bt.search(pk)
                if loc is None:
                    continue

                page = self.bp.get_page(loc[0])
                rb = page.get_row(loc[1])
                r = self._decode_row_full(rb)
                if r:
                    old_row, old_xmin, _ = r
                    # ★ MVCC 加表名
                    self.mvcc.add_undo(
                        pk, old_xmin, txn_id, old_row,
                        table=self.name)

                self.wal.log(txn_id, 'DELETE', {
                    'table': self.name, 'pk': pk,
                })
                page.delete_row(loc[1])
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
                rb = page.get_row(sid)
                r = self._decode_row_full(rb)
                if r is None:
                    continue
                _, xmin, xmax = r
                if read_view is None or \
                        read_view.is_visible(xmin, xmax):
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
                    rb = page.get_row(loc[1])
                    r = self._decode_row_full(rb)
                    if r is None:
                        return []
                    row, xmin, xmax = r
                    if read_view is not None and \
                            not read_view.is_visible(xmin, xmax):
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
            rb = page.get_row(sid)
            r = self._decode_row_full(rb)
            if r is None:
                continue
            row, xmin, xmax = r
            if read_view is not None and \
                    not read_view.is_visible(xmin, xmax):
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
                    rb = page.get_row(loc[1])
                    r = self._decode_row_full(rb)
                    if r:
                        row, xmin, xmax = r
                        if read_view is None or \
                                read_view.is_visible(xmin, xmax):
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

    # ==================== 恢复 + 生命周期 ====================

    def recover_from_wal(self):
        committed = self.wal.find_committed_transactions()
        if not committed:
            return 0
        replayed = 0
        for tid, ops in committed:
            for op in ops:
                try:
                    if op.type == 'INSERT':
                        pk = op.data['pk']
                        if self.bt.search(pk) is None:
                            row = op.data['row']
                            rb = self._encode_row(row, xmin=tid)
                            pid, sid = self._heap_insert(rb)
                            if pid is not None:
                                self.bt.insert(pk, pid, sid)
                                replayed += 1
                except Exception as e:
                    logger.warn(f"重放失败: {e}")
        if replayed:
            logger.info(
                f"从WAL恢复 {replayed} 条记录 ({self.name})")
        self.wal.truncate()
        return replayed

    def flush(self):
        self.bp.flush()
        self.wal.flush()
        self._save_btree_root(self.bt.root_page_id)

    def close(self):
        self.flush()
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