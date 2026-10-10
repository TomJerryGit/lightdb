"""
向量化扫描器
"""
from typing import Dict, List, Optional

from .vectorized_engine import VectorizedScan, VectorBatch, VECTOR_SIZE


class ColumnScanner:
    """列扫描器（带谓词下推）"""

    def __init__(self, column_store, projection: List[str] = None,
                 batch_size: int = VECTOR_SIZE):
        self.store = column_store
        self.projection = projection or column_store.col_names
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
    """谓词下推扫描器"""

    def __init__(self, scanner: ColumnScanner,
                 predicates: List[Dict] = None):
        self.scanner = scanner
        self.predicates = predicates or []

    def _match(self, row: List, cols: List[str]) -> bool:
        for p in self.predicates:
            col = p.get('column')
            op = p.get('op', '=')
            val = p.get('value')
            if col not in cols:
                continue
            ci = cols.index(col)
            v = row[ci]
            if not self._compare(v, op, val):
                return False
        return True

    @staticmethod
    def _compare(v, op, val) -> bool:
        if v is None:
            return op in ('IS NULL',)
        try:
            if isinstance(v, (int, float)):
                try:
                    val = type(v)(val)
                except (ValueError, TypeError):
                    pass
            if op == '=': return v == val
            if op == '!=': return v != val
            if op == '>': return v > val
            if op == '<': return v < val
            if op == '>=': return v >= val
            if op == '<=': return v <= val
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