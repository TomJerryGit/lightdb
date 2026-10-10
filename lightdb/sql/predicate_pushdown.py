"""
谓词下推（Predicate Pushdown）
把 WHERE 条件下推到扫描节点，减少数据传输
"""
from dataclasses import dataclass
from typing import Any, List, Dict, Callable


@dataclass
class Predicate:
    column: str
    op: str
    value: Any

    def eval(self, row: list, col_idx: int) -> bool:
        v = row[col_idx]
        if v is None:
            return False
        try:
            if self.op == '=': return v == self.value
            if self.op == '!=': return v != self.value
            if self.op == '<': return v < self.value
            if self.op == '>': return v > self.value
            if self.op == '<=': return v <= self.value
            if self.op == '>=': return v >= self.value
        except TypeError:
            return False
        return False


class PredicateSet:
    """谓词集合"""

    def __init__(self, predicates: List[Predicate],
                 col_map: Dict[str, int]):
        self.predicates = predicates
        self.col_map = col_map
        # 预解析列索引
        self.compiled = []
        for p in predicates:
            if p.column in col_map:
                self.compiled.append((col_map[p.column], p))

    def match(self, row: list) -> bool:
        for idx, pred in self.compiled:
            v = row[idx]
            if v is None:
                return False
            try:
                if pred.op == '=':
                    if v != pred.value: return False
                elif pred.op == '!=':
                    if v == pred.value: return False
                elif pred.op == '<':
                    if v >= pred.value: return False
                elif pred.op == '>':
                    if v <= pred.value: return False
                elif pred.op == '<=':
                    if v > pred.value: return False
                elif pred.op == '>=':
                    if v < pred.value: return False
                elif pred.op == 'LIKE':
                    import re
                    pat = str(pred.value).replace('%', '.*').replace('_', '.')
                    if not re.match('^' + pat + '$', str(v)):
                        return False
            except TypeError:
                return False
        return True

    def selectivity(self) -> float:
        """估算选择率（用于 CBO）"""
        if not self.predicates:
            return 1.0
        s = 1.0
        for p in self.predicates:
            if p.op == '=':
                s *= 0.01
            elif p.op == 'LIKE':
                v = str(p.value)
                if v.startswith('%') and v.endswith('%'):
                    s *= 0.25
                else:
                    s *= 0.1
            else:
                s *= 0.33
        return max(0.0001, s)


class PushdownOptimizer:
    """谓词下推优化器"""

    @staticmethod
    def optimize(plan: dict) -> dict:
        """
        plan 结构:
        {
            'command': 'SELECT',
            'table': 'users',
            'where': {'and': [{'column': 'id', 'op': '>', 'value': 100}]},
            ...
        }
        """
        if plan is None:
            return None

        where = plan.get('where')
        if not where:
            return plan

        # 提取简单等值/范围条件（可下推）
        pushable, remaining = PushdownOptimizer._split(where)

        if pushable:
            plan = dict(plan)
            plan['pushdown'] = pushable
            plan['where'] = remaining
        return plan

    @staticmethod
    def _split(where: dict) -> tuple:
        """分离可下推与需保留的条件"""
        pushable = []
        remaining = []

        def walk(cond):
            if 'and' in cond:
                for c in cond['and']:
                    walk(c)
            elif 'or' in cond:
                # OR 不下推（除非两个子条件都针对同一列）
                remaining.append(cond)
            elif 'column' in cond:
                op = cond.get('op', '')
                if op in ('=', '!=', '<', '>', '<=', '>=', 'LIKE'):
                    pushable.append(cond)
                else:
                    remaining.append(cond)
            else:
                remaining.append(cond)

        walk(where)

        result_remaining = None
        if remaining:
            if len(remaining) == 1:
                result_remaining = remaining[0]
            else:
                result_remaining = {'and': remaining}

        return pushable, result_remaining


# ==================== 集成示例 ====================

def build_predicate_set(where: dict,
                        col_names: List[str]) -> PredicateSet:
    """从 WHERE 构建谓词集合"""
    col_map = {c: i for i, c in enumerate(col_names)}

    def walk(cond, out):
        if 'and' in cond:
            for c in cond['and']:
                walk(c, out)
        elif 'or' in cond:
            # 简化：OR 转成一个 Predicate 不做
            pass
        elif 'column' in cond:
            out.append(Predicate(
                column=cond['column'],
                op=cond['op'],
                value=cond['value']))

    preds = []
    walk(where, preds)
    return PredicateSet(preds, col_map)