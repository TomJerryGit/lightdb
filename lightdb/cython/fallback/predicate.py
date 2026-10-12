"""
``predicate_pyx`` 的纯 Python 回退
=================================

复用：``lightdb/sql/predicate_v4.py`` 里既有的运算符求值逻辑 —— 本模块
直接调用该模块的 ``_eval`` / LIKE 正则构造，只补齐 .pyx 的类结构
（``CompiledPredicate`` / ``PredicateSet``）而不是另写一套比较语义。

未在本机验证：本机无 Cython、无 C 编译器，``predicate_pyx.pyx`` 从未编译
过，以下等价性判断来自源码静态比对。

为何 ``predicate_v4`` 是**延迟导入**
------------------------------------
``lightdb/sql/predicate_v4.py`` 顶层写着 ``from ..cython import predicate_pyx
as _cy``。本模块若在顶层导入它，就会在 ``lightdb.cython`` 尚未绑定
``predicate_pyx`` 时绕回半初始化的包，抛
``ImportError: cannot import name 'predicate_pyx' from partially initialized
module 'lightdb.cython' (most likely due to a circular import)``。
因此下面用 ``_pypred()`` 在**首次求值**时才导入 —— 那时 ``lightdb.cython``
早已初始化完毕。

已知与 .pyx 的差异:
    1. 字符串 ``<`` / ``>``：.pyx 用 C ``strcmp``（UTF-8 字节序），回退用
       Python 码点序。仅非 ASCII 字符串的排序比较会不同。
    2. 非空值遇到 ``IS NULL``：.pyx 走到末尾返回 False，回退显式保持
       ``False``（与既有的 ``_eval`` 一致）。
    3. 数值比较：.pyx 用 C 定宽整数/浮点；回退用 Python 数值比较（不会
       因 int64 溢出而回绕）。
"""
import importlib

__all__ = [
    'CompiledPredicate',
    'PredicateSet',
    'int_eq_filter',
    'int_range_filter',
]

_COMPARISONS = ('=', '!=', '<', '>', '<=', '>=', 'LIKE')

_PYPRED = None


def _pypred():
    """延迟导入既有纯 Python 谓词实现（避免 lightdb.cython 循环导入）"""
    global _PYPRED
    if _PYPRED is None:
        _PYPRED = importlib.import_module('lightdb.sql.predicate_v4')
    return _PYPRED


class CompiledPredicate:
    """编译后的单个谓词（回退版，包装 predicate_v4 的求值逻辑）"""

    __slots__ = ('col_idx', 'op', 'value')

    def __init__(self, col_idx, op, value):
        self.col_idx = int(col_idx)
        self.op = str(op).upper().strip()
        self.value = value

    # ---------- 求值（委托给既有纯 Python 实现）----------

    def match(self, row):
        if self.col_idx >= len(row):
            return False
        v = row[self.col_idx]
        if v is None:
            # .pyx: 空值只在 IS NULL 时为真
            return self.op == 'IS NULL'
        if self.op == 'IS NULL':
            # .pyx 对非空值会落到末尾返回 False
            return False
        return _eval_one(v, self.op, self.value)

    # 兼容既有调用的等价命名
    def eval(self, row):
        return self.match(row)

    def __repr__(self):
        return (f"CompiledPredicate(col_idx={self.col_idx}, op={self.op!r}, "
                f"value={self.value!r})")


class PredicateSet:
    """谓词集合（AND 语义）"""

    __slots__ = ('predicates',)

    def __init__(self, predicates):
        self.predicates = list(predicates)

    def match(self, row):
        for p in self.predicates:
            if not p.match(row):
                return False
        return True

    def filter(self, rows):
        return [r for r in rows if self.match(r)]

    def count_matches(self, rows):
        return sum(1 for r in rows if self.match(r))

    def __len__(self):
        return len(self.predicates)


# ==================== 运算符求值（复用 predicate_v4）====================

def _eval_one(v, op, val):
    """单值求值：直接走 predicate_v4._eval 的同一实现"""
    return _pypred().CompiledPredicateSetV4._eval(None, v, op, val)


# ==================== 批量过滤（与 .pyx 的 cpdef 对应）====================

def int_range_filter(rows, col_idx, lo, hi):
    """整数闭区间过滤（NULL 行被丢弃，同 .pyx）"""
    col_idx = int(col_idx)
    result = []
    for row in rows:
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v is None:
            continue
        if lo <= v <= hi:
            result.append(row)
    return result


def int_eq_filter(rows, col_idx, value):
    col_idx = int(col_idx)
    result = []
    for row in rows:
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v is None:
            continue
        if v == value:
            result.append(row)
    return result
