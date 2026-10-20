"""
OLAP 优化器（正确性止血版，2026-10）

- 聚合下推
- 列裁剪
- 谓词下推

本批次修复：
1. **谓词下推丢条件**：`_push_predicate` 只认顶层 `{'and': [...]}` 或单
   条件；顶层 `{'or': ...}` 被整体忽略 —— 过滤条件静默消失、返回未过滤
   结果。现在 OR 显式 NotImplementedError（不静默、不假装支持）。
2. **优化器产出的计划不可执行**：`order_by` 之前用
   `VectorizedSort(VectorizedGroupBy(...))` 拼接，而 GroupBy 是物化节点
   （只有 `execute()`，没有 `next_batch()`），一执行就 AttributeError。
   现在统一返回 `OLAPPlan`：流式计划（扫描/过滤/投影）与行式计划
   （分组/聚合/排序）都提供 `execute()`；流式计划另外提供 `next_batch()`。
3. `can_skip_scan` 对字符串列/全 NULL 列做 float() 比较时异常被吞成
   "不跳过"（可接受），但 `'='` 分支在 max 为 None 时可能 TypeError；
   现在显式类型检查。

执行计划结构（自上而下）::

    VectorizedScan → VectorizedFilter* → VectorizedGroupBy → (row sort)
                                     ↘ VectorizedProject (只投影时)
"""
from typing import Any, Dict, List, Optional

from .vectorized_engine import (
    VectorizedScan, VectorizedFilter, VectorizedAggregate,
    VectorizedGroupBy, VectorizedProject, VectorizedSort, VectorBatch,
    HAS_NUMPY)


class OLAPPlan:
    """可执行计划（统一 execute()；流式节点另有 next_batch()）"""

    def __init__(self, node, columns: List[str], kind: str = 'stream',
                 rows: List[List] = None, row_columns: List[str] = None):
        self.node = node
        self.columns = list(columns or [])
        self.kind = kind                 # 'stream' | 'rows'
        self._rows = rows                # kind == 'rows' 时的物化结果
        self._row_columns = list(row_columns or [])
        self._pos = 0

    # ---------------- 流式 ----------------

    def next_batch(self):
        if self.kind == 'rows':
            if self._pos >= len(self._rows):
                return None
            rows = self._rows[self._pos:]
            batch = VectorBatch(columns=list(self._row_columns))
            for ci, name in enumerate(self._row_columns):
                batch.data[name] = [r[ci] for r in rows]
            batch.size = len(rows)
            self._pos = len(self._rows)
            return batch
        return self.node.next_batch()

    # ---------------- 物化 ----------------

    def execute(self) -> List[List]:
        if self.kind == 'rows':
            return [list(r) for r in self._rows]

        out: List[List] = []
        while True:
            batch = self.node.next_batch()
            if batch is None:
                break
            names = batch.column_names()
            cols = self.columns or names
            for name in cols:
                if name not in batch.data:
                    raise KeyError(
                        f"OLAPPlan: 结果批次不存在列 {name!r}，可用列 {names}")
            for i in range(batch.size):
                out.append([batch.data[c][i] for c in cols])
        return out

    def __iter__(self):
        return iter(self.execute())


def _flatten_and(where: Optional[Dict]) -> List[Dict]:
    """把 AND 条件树拍平成条件列表；遇到 OR 显式报错"""
    if not where:
        return []
    if 'or' in where:
        raise NotImplementedError(
            "OLAPOptimizer: 暂不支持 OR 谓词下推（为避免静默丢条件，"
            "这里显式报错；请拆成多次调用或先在 Python 侧过滤）")
    if 'and' in where:
        conds = []
        for c in (where.get('and') or []):
            conds.extend(_flatten_and(c))
        return conds
    if 'column' in where:
        return [where]
    raise NotImplementedError(
        f"OLAPOptimizer: 无法理解的谓词形态 {where!r}（不静默忽略）")


def _sort_rows(rows: List[List], ci: int, desc: bool) -> List[List]:
    """按第 ci 列排序（MySQL NULL 口径：ASC 在前，DESC 在后）"""
    non_null = [r for r in rows if r[ci] is not None]
    null_rows = [r for r in rows if r[ci] is None]
    try:
        non_null.sort(key=lambda r: r[ci], reverse=desc)
    except TypeError as e:
        raise ValueError(f"OLAPOptimizer: 排序列存在无法比较的值: {e}") from e
    return (null_rows + non_null) if not desc else (non_null + null_rows)


class OLAPOptimizer:
    """分析型查询优化器"""

    def __init__(self, column_store):
        self.store = column_store

    def optimize(self, query: Dict) -> OLAPPlan:
        """
        生成向量化执行计划并返回可执行对象。

        query: {
            'columns': ['*'],       # 投影（默认全部）
            'where': {...},         # 谓词（支持 AND 组合；OR 显式报错）
            'group_by': [...],      # 分组
            'aggregations': [...],  # 聚合
            'order_by': (col, desc)
        }
        """
        query = dict(query or {})
        columns = query.get('columns', ['*'])
        where = query.get('where')
        group_by = list(query.get('group_by') or [])
        aggregations = list(query.get('aggregations') or [])
        order_by = query.get('order_by')

        if columns == ['*'] or columns is None:
            out_columns = list(self.store.col_names)
        else:
            out_columns = [str(c).split('.')[-1] for c in columns]

        # 1. 列裁剪：只扫描需要的列
        needed: List[str] = []
        for c in out_columns:
            if c not in needed:
                needed.append(c)
        for cond in _flatten_and(where):
            col = str(cond.get('column', '')).split('.')[-1]
            if col and col not in needed:
                needed.append(col)
        for g in group_by:
            g = str(g).split('.')[-1]
            if g not in needed:
                needed.append(g)
        for agg in aggregations:
            col = agg.get('column')
            if col and col != '*':
                col = str(col).split('.')[-1]
                if col not in needed:
                    needed.append(col)
        if order_by and len(order_by) >= 1:
            ob = str(order_by[0]).split('.')[-1]
            if ob not in needed and ob in self.store.col_names:
                needed.append(ob)

        available = set(self.store.col_names)
        unknown = [c for c in needed if c not in available]
        if unknown:
            raise KeyError(
                f"OLAPOptimizer: 列不存在 {unknown!r}，"
                f"可用列 {self.store.col_names}")

        # 2. 扫描
        node = VectorizedScan(self.store, needed)

        # 3. 谓词下推（AND 链）
        for cond in _flatten_and(where):
            col = str(cond['column']).split('.')[-1]
            op = cond.get('op', '=')
            val = cond.get('value')
            node = VectorizedFilter(node, col, op, val)

        # 4. 分组聚合 或 纯投影
        if group_by or aggregations:
            group_by = [str(g).split('.')[-1] for g in group_by]
            gb = VectorizedGroupBy(node, group_by, aggregations)
            rows = gb.execute()
            row_columns = list(group_by) + [
                a.get('alias') or f"agg{i}" for i, a in enumerate(aggregations)]

            if order_by:
                ob_col = str(order_by[0]).split('.')[-1]
                if ob_col in row_columns:
                    ci = row_columns.index(ob_col)
                elif ob_col in self.store.col_names:
                    raise NotImplementedError(
                        f"OLAPOptimizer: 分组后按非分组列 {ob_col!r} 排序"
                        f"（SQL 语义不允许；显式报错）")
                else:
                    raise KeyError(
                        f"OLAPOptimizer: 排序列 {ob_col!r} 不存在，"
                        f"可用列 {row_columns}")
                rows = _sort_rows(rows, ci, bool(order_by[1]))
            return OLAPPlan(None, row_columns, kind='rows',
                            rows=rows, row_columns=row_columns)

        # 纯过滤/投影：流式
        if columns != ['*'] and columns is not None:
            result_columns = list(out_columns)
        else:
            result_columns = list(needed)

        if order_by:
            # 排序节点是物化节点（只有 execute()）；先排序再按投影列取行，
            # 避免「先投影把排序列裁掉」导致 KeyError。
            sort_col = str(order_by[0]).split('.')[-1]
            sorted_batch = VectorizedSort(
                node, sort_col, bool(order_by[1])).execute()
            rows = []
            for i in range(sorted_batch.size):
                rows.append([sorted_batch.data[c][i]
                             for c in result_columns])
            return OLAPPlan(None, list(result_columns), kind='rows',
                            rows=rows, row_columns=list(result_columns))

        if columns != ['*'] and columns is not None:
            node = VectorizedProject(node, result_columns)
        return OLAPPlan(node, list(result_columns), kind='stream')

    def _push_predicate(self, scan, where):
        """谓词下推（保留旧内部接口：仅支持可拍平的 AND 条件）"""
        for cond in _flatten_and(where):
            col = str(cond['column']).split('.')[-1]
            scan = VectorizedFilter(
                scan, col, cond.get('op', '='), cond.get('value'))
        return scan

    # ==================== 统计信息驱动的优化 ====================

    def can_skip_scan(self, where: Dict) -> bool:
        """
        基于列统计，判断是否可以跳过扫描
        例：WHERE age > 100 但 age 列 max = 50，则直接返回空
        """
        if not where or 'column' not in where:
            return False

        col = str(where['column']).split('.')[-1]
        stats = self.store.get_column_stats(col)
        if not stats:
            return False

        op = str(where.get('op', '=')).upper()
        val = where.get('value')
        min_v = stats.get('min')
        max_v = stats.get('max')
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return False
        if min_v is None or max_v is None:
            return False
        try:
            fval = float(val)
            fmin = float(min_v)
            fmax = float(max_v)
        except (TypeError, ValueError):
            return False

        if op == '>':
            return fval >= fmax
        if op == '>=':
            return fval > fmax
        if op == '<':
            return fval <= fmin
        if op == '<=':
            return fval < fmin
        if op == '=':
            return not (fmin <= fval <= fmax)
        return False
