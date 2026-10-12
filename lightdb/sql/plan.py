"""
逻辑计划 + 物理计划节点
从 AST → 计划树 → 优化 → 执行
"""


class PlanNode:
    def __init__(self, name):
        self.name = name
        self.children = []
        self.rows = 0          # 估算行数（CBO用）
        self.cost = 0.0        # 估算代价
        self.detail = {}

    def add_child(self, child):
        self.children.append(child)
        return child

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> {self.name}"

    def __repr__(self):
        return f"{self.name}(rows={self.rows})"


# ==================== 扫描 ====================
class SeqScan(PlanNode):
    def __init__(self, table, filter_=None, alias=None):
        super().__init__('SeqScan')
        self.table = table
        # 表别名：列名前缀用别名（`u.id` 而不是 `users.id`），
        # 否则 JOIN 里两表同名列会被 _resolve_col 判成歧义
        self.alias = alias
        self.filter = filter_

    def explain_row(self, indent=0):
        f = self._fmt_filter(self.filter)
        label = f"{self.table} {self.alias}" if self.alias else self.table
        return f"{'  ' * indent}-> SeqScan on {label}" + (f" (filter: {f})" if f else "")

    @staticmethod
    def _fmt_filter(f):
        if not f:
            return ""
        if 'and' in f:
            return " AND ".join(SeqScan._fmt_filter(c) for c in f['and'])
        if 'or' in f:
            return " OR ".join(f"({SeqScan._fmt_filter(c)})" for c in f['or'])
        return f"{f['column']} {f['op']} {f['value']}"


class IndexScan(PlanNode):
    def __init__(self, table, index_col, filter_=None):
        super().__init__('IndexScan')
        self.table = table
        self.index_col = index_col
        self.filter = filter_

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> IndexScan using {self.index_col} on {self.table}"


# ==================== 其他算子 ====================
class Filter(PlanNode):
    def __init__(self, child, condition):
        super().__init__('Filter')
        self.condition = condition
        self.add_child(child)

    def explain_row(self, indent=0):
        f = SeqScan._fmt_filter(self.condition)
        return f"{'  ' * indent}-> Filter: {f}"


class Project(PlanNode):
    def __init__(self, child, columns):
        super().__init__('Project')
        self.columns = columns
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> Project: {', '.join(self.columns)}"


class Sort(PlanNode):
    def __init__(self, child, column, desc=False):
        super().__init__('Sort')
        self.column = column
        self.desc = desc
        self.add_child(child)

    def explain_row(self, indent=0):
        d = "DESC" if self.desc else "ASC"
        return f"{'  ' * indent}-> Sort: {self.column} {d}"


class Limit(PlanNode):
    def __init__(self, child, n, offset=0):
        super().__init__('Limit')
        self.n = n                  # None 表示只做 OFFSET、不限制行数
        self.offset = int(offset or 0)
        self.add_child(child)

    def explain_row(self, indent=0):
        off = f" OFFSET {self.offset}" if self.offset else ""
        n = self.n if self.n is not None else 'ALL'
        return f"{'  ' * indent}-> Limit: {n}{off}"


class Aggregate(PlanNode):
    def __init__(self, child, columns):
        super().__init__('Aggregate')
        self.columns = columns
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> Aggregate: {', '.join(self.columns)}"


class Explain(PlanNode):
    def __init__(self, child):
        super().__init__('Explain')
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> Explain"


# ==================== 打印 ====================
def print_plan(node, indent=0, lines=None):
    """递归打印计划树"""
    if lines is None:
        lines = []
    lines.append(node.explain_row(indent))
    for c in node.children:
        print_plan(c, indent + 1, lines)
    return lines
    # ==================== JOIN 节点 ====================

class Join(PlanNode):
    def __init__(self, left, right, join_type='INNER'):
        super().__init__('Join')
        self.left = left
        self.right = right
        self.join_type = join_type
        self.add_child(left)
        self.add_child(right)


class HashJoin(Join):
    def __init__(self, left, right, left_key, right_key, join_type='INNER'):
        super().__init__(left, right, join_type)
        self.name = 'HashJoin'
        self.left_key = left_key
        self.right_key = right_key

    def explain_row(self, indent=0):
        return (f"{'  ' * indent}-> HashJoin ({self.join_type}) "
                f"on {self.left_key} = {self.right_key}")


class NestedLoopJoin(Join):
    def __init__(self, left, right, left_key=None, right_key=None,
                 join_type='INNER'):
        super().__init__(left, right, join_type)
        self.name = 'NestedLoopJoin'
        self.left_key = left_key
        self.right_key = right_key

    def explain_row(self, indent=0):
        c = ""
        if self.left_key and self.right_key:
            c = f" on {self.left_key} = {self.right_key}"
        return f"{'  ' * indent}-> NestedLoopJoin ({self.join_type}){c}"


class CrossJoin(Join):
    def __init__(self, left, right):
        super().__init__(left, right, 'CROSS')
        self.name = 'CrossJoin'

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> CrossJoin"
        # ==================== Batch 7 新增 ====================

class Distinct(PlanNode):
    def __init__(self, child):
        super().__init__('Distinct')
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> Distinct"


class GroupBy(PlanNode):
    """分组聚合"""
    def __init__(self, child, group_cols, agg_cols, having=None):
        super().__init__('GroupBy')
        self.group_cols = group_cols  # 分组列名列表
        self.agg_cols = agg_cols      # 聚合表达式列表
        self.having = having          # HAVING 条件
        self.add_child(child)

    def explain_row(self, indent=0):
        g = ", ".join(self.group_cols) if self.group_cols else "(全表)"
        h = f" HAVING {self.having}" if self.having else ""
        return f"{'  ' * indent}-> GroupBy: {g}{h}"


class Union(PlanNode):
    def __init__(self, left, right, all_=False):
        super().__init__('UnionAll' if all_ else 'Union')
        self.all = all_
        self.add_child(left)
        self.add_child(right)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> {'UNION ALL' if self.all else 'UNION'}"


class SubqueryScan(PlanNode):
    """FROM (SELECT ...) AS alias"""
    def __init__(self, child, alias):
        super().__init__('SubqueryScan')
        self.alias = alias
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> SubqueryScan: {self.alias}"


class ValueList(PlanNode):
    """IN 子查询的结果值列表"""
    def __init__(self, values):
        super().__init__('ValueList')
        self.values = values
        self.rows = len(values)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> ValueList ({len(self.values)} values)"
        # ==================== Batch 11 新增 ====================

class Window(PlanNode):
    """窗口函数节点"""
    def __init__(self, child, window_calls):
        super().__init__('Window')
        self.window_calls = window_calls
        self.add_child(child)

    def explain_row(self, indent=0):
        fns = ', '.join(w.func_name for w in self.window_calls)
        return f"{'  ' * indent}-> Window: {fns}"


class CTEScan(PlanNode):
    """CTE 扫描（从 CTE 结果集读）"""
    def __init__(self, name):
        super().__init__('CTEScan')
        self.cte_name = name

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> CTEScan: {self.cte_name}"


class CTEDefine(PlanNode):
    """CTE 定义（把子查询结果物化到 CTE）"""
    def __init__(self, name, child, columns=None):
        super().__init__('CTEDefine')
        self.cte_name = name
        self.columns = columns
        self.add_child(child)

    def explain_row(self, indent=0):
        return f"{'  ' * indent}-> CTEDefine: {self.cte_name}"


class RecursiveCTE(PlanNode):
    """递归 CTE 节点"""
    def __init__(self, name, base_plan, recursive_plan, union_all=True):
        super().__init__('RecursiveCTE')
        self.cte_name = name
        self.base_plan = base_plan
        self.recursive_plan = recursive_plan
        self.union_all = union_all
        self.add_child(base_plan)
        self.add_child(recursive_plan)

    def explain_row(self, indent=0):
        u = 'UNION ALL' if self.union_all else 'UNION'
        return f"{'  ' * indent}-> RecursiveCTE: {self.cte_name} ({u})"