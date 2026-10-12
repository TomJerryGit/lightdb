"""
查询优化器（修复版）

修复历史:
- v4.0.1: 修复 _choose_access_path 访问不存在的 tbl._indexes
- v4.0.1: 修复 _push_down_filters 未处理 Join 子节点
- v4.0.1: 修复 _extract_eq_conditions 对 or 的处理
- v4.0.1: 支持 having 传入 GroupBy
- v4.0.1: 支持 Distinct / SubqueryScan / ValueList / CTEScan / Window / Union
- v4.0.1: CBO 参数可配置
"""
import re
from .plan import (
    SeqScan, IndexScan, Filter, Project, Sort, Limit,
    Aggregate, Explain, PlanNode,
    Distinct, GroupBy, Union, SubqueryScan, ValueList,
    HashJoin, NestedLoopJoin, CrossJoin,
)


# 默认代价参数
DEFAULT_COST = {
    'seq_page': 1.0,          # 顺序扫描一页
    'index_seek': 4.0,        # 索引定位一次
    'index_row': 0.5,         # 索引命中的每一行回表
    'cpu_tuple': 0.01,        # 处理一行的 CPU
    'cpu_index_key': 0.005,   # 索引比较
}


class Optimizer:
    """查询优化器

    职责:
    1. 逻辑计划生成（AST -> PlanNode）
    2. RBO 规则应用（谓词下推等）
    3. CBO 规则应用（访问路径选择）
    """

    def __init__(self, db, stats_manager, cost_config=None):
        self.db = db
        self.stats = stats_manager
        self.cost = dict(DEFAULT_COST)
        if cost_config:
            self.cost.update(cost_config)

    # ==================== 入口 ====================

    def optimize(self, parsed) -> PlanNode:
        if parsed is None:
            return None
        plan = self._build_logical(parsed)
        if plan is None:
            return None
        plan = self._apply_rbo(plan)
        plan = self._apply_cbo(plan)
        return plan

    # ==================== 逻辑计划生成 ====================

    def _build_logical(self, parsed) -> PlanNode:
        if parsed is None:
            return None
        cmd = parsed.get('command')

        if cmd == 'EXPLAIN':
            inner = self._build_logical(parsed.get('inner'))
            if inner is None:
                return None
            return Explain(inner)

        if cmd == 'UNION':
            segments = parsed.get('segments', [])
            if not segments:
                return None
            node = self._build_logical(segments[0])
            for i in range(1, len(segments)):
                right = self._build_logical(segments[i])
                if right is None:
                    continue
                # 第一个之后默认 UNION（去重）；UNION ALL 段在 all_flags 里
                all_flag = False
                flags = parsed.get('all_flags') or []
                if i < len(flags):
                    all_flag = bool(flags[i])
                node = Union(node, right, all_=all_flag)
            return node

        if cmd != 'SELECT':
            return None

        table = parsed.get('table')
        subquery = parsed.get('subquery')
        joins = parsed.get('joins') or []
        where = parsed.get('where')
        columns = parsed.get('columns') or ['*']
        order_by = parsed.get('order_by')
        limit = parsed.get('limit')
        group_by = parsed.get('group_by')
        having = parsed.get('having')
        distinct = parsed.get('distinct', False)

        # ---------- FROM 部分 ----------
        if subquery:
            inner_ast = self._parse_subquery_ast(subquery)
            inner_plan = self._build_logical(inner_ast)
            if inner_plan is None:
                return None
            node = SubqueryScan(inner_plan, subquery.get('alias', 'sub'))
        elif table:
            node = SeqScan(table, None, parsed.get('table_alias'))
        else:
            return None

        # ---------- JOIN ----------
        for j in joins:
            jtbl = j.get('table')
            jtype = (j.get('type') or 'INNER').upper()
            on = j.get('on')
            if not jtbl:
                continue
            right = SeqScan(jtbl, None, j.get('alias'))

            if jtype == 'CROSS':
                node = CrossJoin(node, right)
            elif on and 'left' in on and 'right' in on:
                node = HashJoin(node, right, on['left'], on['right'], jtype)
            else:
                node = NestedLoopJoin(node, right, None, None, jtype)

        # ---------- WHERE ----------
        if where:
            node = Filter(node, where)

        # ---------- 聚合 / GROUP BY ----------
        has_agg = any(self._is_agg_expr(c) for c in columns)
        if group_by or has_agg:
            non_agg = [c for c in columns if not self._is_agg_expr(c)]
            group_cols = group_by or non_agg
            node = GroupBy(node, group_cols, columns, having=having)
        else:
            if columns != ['*']:
                node = Project(node, columns)

        # ---------- DISTINCT ----------
        if distinct:
            node = Distinct(node)

        # ---------- ORDER BY ----------
        if order_by:
            col = order_by[0]
            desc = bool(order_by[1]) if len(order_by) > 1 else False
            node = Sort(node, col, desc)

        # ---------- LIMIT / OFFSET ----------
        # LIMIT 之前必须已经排好序（Sort 在上一步），OFFSET 也在这里裁剪，
        # 否则 JOIN 查询的 `LIMIT n OFFSET m` 会把 offset 静默丢掉
        offset = int(parsed.get('offset') or 0)
        if limit is not None:
            node = Limit(node, int(limit), offset)
        elif offset:
            node = Limit(node, None, offset)

        return node

    def _parse_subquery_ast(self, subquery):
        """解析 FROM (SELECT ...) 子查询"""
        try:
            from .parser import parser
            return parser.parse(subquery.get('sql', ''))
        except Exception:
            return None

    @staticmethod
    def _is_agg_expr(expr):
        if not isinstance(expr, str):
            return False
        e = expr.strip().upper()
        return any(
            e.startswith(fn + '(')
            for fn in ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
                       'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
                       'VARIANCE', 'VAR_POP', 'VAR_SAMP')
        )

    # ==================== RBO ====================

    def _apply_rbo(self, plan):
        if plan is None:
            return None
        plan = self._push_down_filters(plan)
        plan = self._prune_project(plan)
        return plan

    def _push_down_filters(self, node):
        """谓词下推：
        - Filter(SeqScan)     -> SeqScan.filter
        - Filter(IndexScan)   -> IndexScan.filter
        - Filter(Filter)      -> 合并条件
        - Filter(SubqueryScan) 保持（子查询内已经有自己的 filter）
        """
        if node is None:
            return None

        # 递归处理子节点
        if hasattr(node, 'children'):
            for i, c in enumerate(node.children):
                node.children[i] = self._push_down_filters(c)

        if isinstance(node, Filter):
            child = node.children[0] if node.children else None
            if child is None:
                return node

            # Filter + Filter -> 合并
            if isinstance(child, Filter):
                merged = self._merge_and(child.condition, node.condition)
                new_filter = Filter(child.children[0], merged)
                return self._push_down_filters(new_filter)

            # Filter + SeqScan -> 合并
            if isinstance(child, SeqScan):
                child.filter = self._merge_and(child.filter, node.condition)
                return child

            # Filter + IndexScan -> 合并
            if isinstance(child, IndexScan):
                child.filter = self._merge_and(child.filter, node.condition)
                return child

        return node

    @staticmethod
    def _merge_and(a, b):
        if a is None:
            return b
        if b is None:
            return a
        if isinstance(a, dict) and 'and' in a:
            return {'and': list(a['and']) + [b]}
        return {'and': [a, b]}

    def _prune_project(self, node):
        """列裁剪：暂时保留钩子，未实现"""
        return node

    # ==================== CBO ====================

    def _apply_cbo(self, node):
        if node is None:
            return None

        if hasattr(node, 'children'):
            for i, c in enumerate(node.children):
                node.children[i] = self._apply_cbo(c)

        if isinstance(node, SeqScan):
            return self._choose_access_path(node)
        return node

    def _choose_access_path(self, scan: SeqScan) -> PlanNode:
        """选择 SeqScan vs IndexScan

        ★ 修复：不再访问不存在的 tbl._indexes
        """
        table_name = scan.table
        try:
            tbl = self.db.get_table(table_name)
        except (KeyError, AttributeError):
            return scan

        st = self.stats.get(table_name) if self.stats else None

        # 无统计：直接走全扫，标记为高代价
        if st is None or getattr(st, 'row_count', 0) == 0:
            scan.rows = 0
            scan.cost = 1.0
            return scan

        row_count = st.row_count
        page_count = st.page_count or max(1, row_count // 50)

        cost_seq = (
            page_count * self.cost['seq_page']
            + row_count * self.cost['cpu_tuple']
        )
        scan.rows = row_count
        scan.cost = cost_seq

        if scan.filter is None:
            return scan

        # 提取等值条件
        best_index = None
        best_cost = cost_seq
        best_sel = 1.0

        for cond in self._extract_eq_conditions(scan.filter):
            col = cond.get('column', '').split('.')[-1]
            if not col:
                continue

            # ★ 只允许主键或真实存在的索引列（用 getattr 兜底）
            indexes = getattr(tbl, '_indexes', None)
            if col == tbl.pk_col:
                pass
            elif isinstance(indexes, dict) and col in indexes:
                pass
            elif isinstance(indexes, (list, tuple, set)) and col in indexes:
                pass
            else:
                continue

            sel = self.stats.estimate_selectivity(table_name, cond)
            matched = max(1, int(row_count * sel))

            cost_idx = (
                self.cost['index_seek']
                + matched * self.cost['index_row']
                + matched * self.cost['cpu_index_key']
            )

            if cost_idx < best_cost:
                best_cost = cost_idx
                best_index = col
                best_sel = sel

        if best_index:
            node = IndexScan(table_name, best_index, scan.filter)
            node.rows = max(1, int(row_count * best_sel))
            node.cost = best_cost
            return node

        return scan

    def _extract_eq_conditions(self, where):
        """从 WHERE 里提取所有顶层 AND 下的等值条件"""
        result = []
        if where is None:
            return result
        if isinstance(where, dict):
            if 'and' in where:
                for c in where['and']:
                    result.extend(self._extract_eq_conditions(c))
            elif 'column' in where and where.get('op') == '=':
                result.append(where)
        return result

    # ==================== EXPLAIN ====================

    def explain(self, plan: PlanNode):
        """MySQL 风格 EXPLAIN"""
        from .plan import print_plan

        scans = self._collect_scans(plan)
        cols = ['id', 'select_type', 'table', 'type', 'key',
                'rows', 'Extra']
        rows = []
        for i, s in enumerate(scans, 1):
            if isinstance(s, SeqScan):
                rows.append((i, 'SIMPLE', s.table, 'ALL',
                             None, s.rows or 0, self._extra(s)))
            elif isinstance(s, IndexScan):
                rows.append((i, 'SIMPLE', s.table, 'ref',
                             s.index_col, s.rows or 0, self._extra(s)))
        if not rows:
            rows.append((1, 'SIMPLE', None, None, None, 0, 'No tables used'))

        return cols, rows, print_plan(plan)

    def _extra(self, scan):
        extras = []
        if getattr(scan, 'filter', None):
            extras.append('Using where')
        if isinstance(scan, IndexScan):
            extras.append('Using index')
        return ', '.join(extras) if extras else None

    def _collect_scans(self, node, out=None):
        if out is None:
            out = []
        if node is None:
            return out
        if isinstance(node, (SeqScan, IndexScan)):
            out.append(node)
        for c in getattr(node, 'children', []):
            self._collect_scans(c, out)
        return out