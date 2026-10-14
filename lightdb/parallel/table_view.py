"""
存储表 → 列式视图适配器

`ParallelExecutor` 的并行聚合/扫描按「列式存储」分片（需要
`row_count` + `scan_range(columns, start, end)`），而 `lightdb/db/table.py`
的 `Table` 是行式（按主键 BTree 定位、逐行 JSON 解码）。

这个适配器把行式表**只读地**暴露成列式视图：
- `row_count` 用 BTree 条目数（与 `Table.row_count` 同源）；
- `scan_range` 只把请求范围内的行解码成 `{列名: [值...]}`；
- `columns=['*']` / 不存在的列 → 返回 `[]`，不读任何数据。

**已知限制**：每次聚合要按分片把行值解码成 list，属于额外开销
（`Table` 的行载荷是 JSON，没有列存那种零拷贝读列）。因此它只用于
「够大且能安全按行分片」的聚合（目前是单表裸 COUNT，见
`Executor._is_plain_count`），小表一律走原有串行路径。
"""
from typing import Dict, List


class TableColumnView:
    """把一个 `Table` 当成只读列式存储用（供 ParallelExecutor 分片）"""

    def __init__(self, table):
        self._tbl = table
        self.name = table.name
        self.cols = list(table.cols)
        self.col_names = [c['name'] for c in table.cols]

    @property
    def row_count(self) -> int:
        return self._tbl.bt.count()

    def scan(self, columns: List[str] = None) -> Dict[str, List]:
        return self.scan_range(columns or self.col_names, 0, self.row_count)

    def scan_range(self, columns: List[str],
                   start: int, end: int) -> Dict[str, List]:
        target = [c for c in (columns or self.col_names)
                  if c in self.col_names]
        out: Dict[str, List] = {c: [] for c in target}
        # `COUNT(*)`（`column == '*'`）没有任何具体列可读，但"数行"依然需要
        # 知道区间里有多少行。列式存储里 `scan_range(['*'])` 返回空列表，
        # 会让 `_agg_fragment` 得到「本分片 0 行」，于是并行 COUNT(*) 恒为 0。
        # 这里给 `'*'` 填**非 NULL 占位值**（`_agg_fragment` 会先滤掉 None），
        # 语义就是"这一区间的行"，与具体列值无关。
        want_star = '*' in (columns or [])
        if want_star:
            out['*'] = []
        if not target and not want_star:
            return out
        if start >= end:
            return out

        # 只解码 [start, end) 区间：BTree 扫描是主键序，行号即"第几个条目"。
        # 表在扫描期间被并发修改（行数变化）时结果可能偏移——这是本适配器
        # 与所有"逻辑一致性读"共享的限制（见 logical_dump 的格式说明第 4 条）。
        idx = -1
        tbl = self._tbl
        for _pk, pid, sid in tbl.bt.scan():
            idx += 1
            if idx < start:
                continue
            if idx >= end:
                break
            page = tbl.bp.get_page(pid)
            rb = page.get_row(sid) if page is not None else None
            row = tbl._decode_row(rb)
            if row is None:
                continue
            for c in target:
                try:
                    i = self.col_names.index(c)
                except ValueError:            # pragma: no cover
                    continue
                out[c].append(row[i] if i < len(row) else None)
            if want_star:
                out['*'].append(1)             # 行占位（非 NULL，用于计数）
        return out
