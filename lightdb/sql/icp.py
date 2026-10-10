"""
索引条件下推（Index Condition Pushdown, ICP）
MySQL 5.6+ 特性
- 把 WHERE 条件下推到索引扫描层
- 减少回表次数
"""
import re
from typing import Any, List, Dict, Optional


class ICPCondition:
    """ICP 条件"""
    __slots__ = ('column', 'op', 'value')

    def __init__(self, column: str, op: str, value):
        self.column = column
        self.op = op.upper()
        self.value = value

    def match(self, val: Any) -> bool:
        if val is None:
            return False
        try:
            v = self.value
            # 类型兼容
            if isinstance(val, (int, float)) and isinstance(v, str):
                try:
                    v = float(v) if '.' in v else int(v)
                except (ValueError, TypeError):
                    pass
            elif isinstance(v, (int, float)) and isinstance(val, str):
                try:
                    val = float(val) if '.' in val else int(val)
                except (ValueError, TypeError):
                    pass

            if self.op == '=': return val == v
            if self.op == '!=': return val != v
            if self.op == '<': return val < v
            if self.op == '>': return val > v
            if self.op == '<=': return val <= v
            if self.op == '>=': return val >= v
            if self.op == 'LIKE':
                pat = str(v).replace('%', '.*').replace('_', '.')
                return bool(re.match('^' + pat + '$', str(val)))
            if self.op == 'IN':
                vals = v if isinstance(v, (list, tuple)) else [v]
                return val in vals
            if self.op == 'BETWEEN':
                lo, hi = v[0], v[1]
                return lo <= val <= hi
        except (TypeError, ValueError):
            return False
        return False

    def __repr__(self):
        return f"{self.column} {self.op} {self.value}"


class ICPPlan:
    """ICP 执行计划"""

    def __init__(self, table: str, index_name: str,
                 index_columns: List[str]):
        self.table = table
        self.index_name = index_name
        self.index_columns = index_columns
        # 索引内可用的条件（用于减少回表）
        self.index_conditions: List[ICPCondition] = []
        # 必须回表后才能判断的条件
        self.table_conditions: List[Dict] = []

    def add_index_condition(self, col: str, op: str, value):
        self.index_conditions.append(ICPCondition(col, op, value))

    def add_table_condition(self, cond: Dict):
        self.table_conditions.append(cond)

    def filter_index_entry(self, col_values: Dict[str, Any]) -> bool:
        """判断索引条目是否通过（无需回表）"""
        for cond in self.index_conditions:
            if cond.column not in col_values:
                continue
            if not cond.match(col_values[cond.column]):
                return False
        return True

    def has_index_conditions(self) -> bool:
        return len(self.index_conditions) > 0

    def explain(self) -> str:
        lines = [
            f"ICPPlan for {self.table}",
            f"  Index: {self.index_name}",
            f"  Index columns: {self.index_columns}",
            f"  Pushed conditions: {len(self.index_conditions)}",
        ]
        for c in self.index_conditions:
            lines.append(f"    - {c}")
        lines.append(f"  Table conditions: {len(self.table_conditions)}")
        return '\n'.join(lines)


class ICPOptimizer:
    """ICP 优化器"""

    @staticmethod
    def build_icp_plan(table: str, index_name: str,
                       index_columns: List[str],
                       where_conditions: List[Dict]) -> ICPPlan:
        """
        构建 ICP 计划
        - 索引前缀列的条件 → 索引使用
        - 索引内其他列的条件 → ICP 下推
        - 非索引列的条件 → 保留到表过滤
        """
        plan = ICPPlan(table, index_name, index_columns)
        index_col_set = set(index_columns)

        for cond in where_conditions:
            col = cond.get('column', '').split('.')[-1]
            if col in index_col_set:
                plan.add_index_condition(
                    col, cond.get('op', '='), cond.get('value'))
            else:
                plan.add_table_condition(cond)

        return plan


# ==================== 与存储层集成 ====================

def scan_with_icp(btree, icp_plan: ICPPlan,
                  row_loader,
                  start=None, end=None,
                  limit: int = None) -> List[list]:
    """
    带 ICP 的索引扫描
    - 遍历 B+Tree 索引
    - 用 ICP 条件过滤
    - 只对通过的行回表
    """
    results = []
    index_cols = icp_plan.index_columns
    table = icp_plan.table

    # 遍历索引
    for key, pid, sid in btree.scan(start=start, end=end):
        # 索引键值
        col_values = {}
        # 假设第一个索引列是键
        if index_cols:
            col_values[index_cols[0]] = key

        # ICP 过滤
        if not icp_plan.filter_index_entry(col_values):
            continue

        # 只有通过 ICP 的行才回表
        row = row_loader(pid, sid)
        if row is None:
            continue

        # 检查其他列的条件
        if not _check_all_conditions(row, table, icp_plan):
            continue

        results.append(row)
        if limit and len(results) >= limit:
            break

    return results


def _check_all_conditions(row: list, table: str,
                          icp_plan: ICPPlan) -> bool:
    """检查表级条件（未在索引中的列）"""
    # 需要列名映射，简化实现：假设 row 是 list
    # 完整实现需从 table schema 获取
    return True   # 简化返回 True


# ==================== 示例 ====================

def example_icp():
    """ICP 示例"""
    print("=" * 60)
    print("  ICP 示例")
    print("=" * 60)

    # 假设表: users(id, name, age, city)
    # 索引: idx_age_city (age, city)
    # 查询: WHERE age > 20 AND city = 'Beijing'

    plan = ICPOptimizer.build_icp_plan(
        table='users',
        index_name='idx_age_city',
        index_columns=['age', 'city'],
        where_conditions=[
            {'column': 'age', 'op': '>', 'value': 20},
            {'column': 'city', 'op': '=', 'value': 'Beijing'},
        ])

    print(plan.explain())

    # 测试条件
    entries = [
        {'age': 15, 'city': 'Beijing'},    # age 不满足
        {'age': 25, 'city': 'Shanghai'},   # city 不满足
        {'age': 25, 'city': 'Beijing'},    # 通过
        {'age': 30, 'city': 'Beijing'},    # 通过
    ]

    for e in entries:
        result = plan.filter_index_entry(e)
        print(f"  {e} → {'✓' if result else '✗'}")


if __name__ == '__main__':
    example_icp()