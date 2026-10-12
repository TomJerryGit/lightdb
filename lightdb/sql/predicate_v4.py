"""
谓词 v4（Cython 加速）
"""
from typing import List, Dict, Any

from ..cython import predicate_pyx as _cy


class CompiledPredicateSetV4:
    """
    编译后的谓词集合
    优先使用 Cython
    """

    def __init__(self, conditions: List[Dict],
                 col_map: Dict[str, int]):
        self.conditions = conditions
        self.col_map = col_map
        # _cy 恒不为 None（未编译时拿到的是纯 Python 回退），
        # 不能拿它当加速判据，否则会谎报在用 Cython。
        try:
            from ..cython import status as _cstatus
            self.use_cython = bool(_cstatus().get('any_accelerated'))
        except Exception:
            self.use_cython = False

        if self.use_cython:
            # 编译
            compiled = []
            for c in conditions:
                col = c.get('column', '').split('.')[-1]
                if col in col_map:
                    compiled.append(_cy.CompiledPredicate(
                        col_map[col],
                        c.get('op', '='),
                        c.get('value')))
            self._cy_set = _cy.PredicateSet(compiled)

    def match(self, row: List[Any]) -> bool:
        if self.use_cython:
            return self._cy_set.match(row)
        # Python fallback
        for c in self.conditions:
            col = c.get('column', '').split('.')[-1]
            if col not in self.col_map:
                continue
            v = row[self.col_map[col]]
            op = c.get('op', '=')
            val = c.get('value')
            if not self._eval(v, op, val):
                return False
        return True

    def filter(self, rows: List[List]) -> List[List]:
        if self.use_cython:
            return self._cy_set.filter(rows)
        return [r for r in rows if self.match(r)]

    def _eval(self, v, op, val) -> bool:
        if v is None:
            return op == 'IS NULL'
        try:
            if isinstance(v, (int, float)):
                try:
                    val = type(v)(val)
                except (ValueError, TypeError):
                    pass
            if op == '=': return v == val
            if op == '!=': return v != val
            if op == '<': return v < val
            if op == '>': return v > val
            if op == '<=': return v <= val
            if op == '>=': return v >= val
            if op == 'LIKE':
                import re
                pat = str(val).replace('%', '.*').replace('_', '.')
                return bool(re.match('^' + pat + '$', str(v)))
        except (TypeError, ValueError):
            return False
        return False


def compile_predicate(where: Dict,
                       col_names: List[str]) -> CompiledPredicateSetV4:
    """从 WHERE 编译谓词"""
    col_map = {c: i for i, c in enumerate(col_names)}

    conditions = []

    def walk(cond):
        if 'and' in cond:
            for c in cond['and']:
                walk(c)
        elif 'or' in cond:
            # OR 暂不支持 Cython 优化
            pass
        elif 'column' in cond:
            conditions.append(cond)

    walk(where)
    return CompiledPredicateSetV4(conditions, col_map)