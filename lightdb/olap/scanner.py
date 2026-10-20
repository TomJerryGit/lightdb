"""
向量化扫描器（正确性止血版，2026-10）

- `ColumnScanner`：全量/范围扫描，直接代理 `ColumnStore`（未知列由
  ColumnStore 显式 KeyError）。
- `PredicateScanner`：谓词下推扫描（纯 Python 行过滤，供非向量化路径用）。

本批次修复：
- 谓词引用**不存在的列**以前被 `continue` 静默跳过 —— 等于过滤条件
  整条消失，返回全量行冒充结果。现在显式 KeyError。
- 不认识的比较符以前 `return False` 静默过滤掉所有行。现在显式
  ValueError。
- 补 `IS NULL` / `IS NOT NULL` / `IN` / `NOT IN` 的 SQL 语义；
  NULL 参与普通比较一律 UNKNOWN（不入选），`IS NOT NULL` 对非 NULL
  行必须为真（旧实现对非 NULL 行返回 False）。
"""
from typing import Dict, List, Optional

from .vectorized_engine import VectorizedScan, VectorBatch, VECTOR_SIZE


class ColumnScanner:
    """列扫描器（带谓词下推）"""

    def __init__(self, column_store, projection: List[str] = None,
                 batch_size: int = VECTOR_SIZE):
        self.store = column_store
        self.projection = (list(projection) if projection
                           else list(column_store.col_names))
        self.batch_size = batch_size

    def scan(self) -> Dict[str, List]:
        """全量扫描"""
        return self.store.scan(self.projection)

    def scan_range(self, start: int, end: int) -> Dict[str, List]:
        """范围扫描"""
        return self.store.scan_range(self.projection, start, end)

    def iter_batches(self):
        """迭代批次"""
        scan = VectorizedScan(self.store, self.projection, self.batch_size)
        while True:
            batch = scan.next_batch()
            if batch is None:
                break
            yield batch


class PredicateScanner:
    """谓词下推扫描器（逐行参考实现）

    predicates: [{'column': 'v', 'op': '>', 'value': 0}, ...]
                多个谓词之间是 AND；op 支持
                =, !=, <>, >, <, >=, <=, LIKE, IS NULL, IS NOT NULL,
                IN, NOT IN。
    """

    _KNOWN_OPS = ('=', '!=', '<>', '>', '<', '>=', '<=', 'LIKE',
                  'IS NULL', 'IS NOT NULL', 'IN', 'NOT IN')

    def __init__(self, scanner: ColumnScanner,
                 predicates: List[Dict] = None):
        self.scanner = scanner
        self.predicates = list(predicates or [])
        for p in self.predicates:
            op = str(p.get('op', '=')).upper()
            if op not in self._KNOWN_OPS:
                raise ValueError(
                    f"PredicateScanner: 不支持的比较符 {p.get('op')!r}，"
                    f"仅支持 {list(self._KNOWN_OPS)}")

    def _match(self, row: List, cols: List[str]) -> bool:
        for p in self.predicates:
            col = p.get('column')
            if col not in cols:
                # 列不存在 ⇒ 谓词无法求值 ⇒ 显式报错（绝不静默全通过）
                raise KeyError(
                    f"PredicateScanner: 谓词列 {col!r} 不存在，"
                    f"可用列 {cols}")
            ci = cols.index(col)
            v = row[ci]
            if not self._compare(v, p.get('op', '='), p.get('value')):
                return False
        return True

    @staticmethod
    def _compare(v, op, val) -> bool:
        op = str(op or '=').upper()
        if v is None:
            return op == 'IS NULL'
        if op == 'IS NULL':
            return False
        if op == 'IS NOT NULL':
            return True
        if op in ('IN', 'NOT IN'):
            if not isinstance(val, (list, tuple, set)):
                raise ValueError(
                    f"PredicateScanner: {op} 需要列表值，实际 {val!r}")
            has_null = any(x is None for x in val)
            hit = False
            for cand in val:
                if cand is None:
                    continue
                if _equal(v, cand):
                    hit = True
                    break
            if op == 'IN':
                return hit
            # NOT IN 语义：命中 → False；未命中且列表含 NULL → UNKNOWN → False
            return (not hit) and (not has_null)

        if val is None:
            return False          # NULL 参与普通比较恒 UNKNOWN
        try:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                try:
                    val = type(v)(val)
                except (ValueError, TypeError):
                    pass
            if op == '=':
                return _equal(v, val)
            if op in ('!=', '<>'):
                return not _equal(v, val)
            if op == '>':
                return v > val
            if op == '<':
                return v < val
            if op == '>=':
                return v >= val
            if op == '<=':
                return v <= val
            if op == 'LIKE':
                import re
                pat = str(val).replace('%', '.*').replace('_', '.')
                return bool(re.match('^' + pat + '$', str(v)))
        except (TypeError, ValueError):
            return False
        return False

    def scan(self) -> Dict[str, List]:
        data = self.scanner.scan()
        if not self.predicates:
            return data
        cols = list(data.keys())
        n = len(data[cols[0]]) if cols else 0
        keep = []
        for i in range(n):
            row = [data[c][i] for c in cols]
            if self._match(row, cols):
                keep.append(i)

        return {c: [data[c][i] for i in keep] for c in cols}


def _equal(a, b) -> bool:
    """SQL 等值（数值 1 与 1.0 相等；字符串不做隐式数字转换）"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b
