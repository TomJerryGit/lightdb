"""流程控制函数"""
import json
from typing import Any


def if_(cond, a, b):
    """IF(cond, a, b)"""
    if cond is None:
        return b
    return a if cond else b


def ifnull_(a, b):
    """IFNULL"""
    return a if a is not None else b


def nullif_(a, b):
    """NULLIF"""
    return None if a == b else a


def coalesce_(*args):
    """COALESCE"""
    for a in args:
        if a is not None:
            return a
    return None


def case_expr(val, whens, else_val=None):
    """CASE 表达式（内部使用）"""
    for when_val, then_val in whens:
        if val == when_val:
            return then_val
    return else_val


def is_null_(v): return 1 if v is None else 0
def is_not_null_(v): return 0 if v is None else 1


def cast_(v, target):
    """CAST(v AS type)"""
    if v is None:
        return None
    t = str(target).upper()
    try:
        if t in ('INT', 'SIGNED', 'UNSIGNED'):
            return int(float(v)) if isinstance(v, str) else int(v)
        if t in ('CHAR', 'VARCHAR', 'TEXT', 'NCHAR'):
            return str(v)
        if t in ('DECIMAL', 'NUMERIC', 'DOUBLE', 'FLOAT'):
            return float(v)
        if t == 'DATE':
            return str(v)[:10]
        if t == 'DATETIME':
            return str(v)[:19]
        if t == 'TIME':
            return str(v)[11:19] if len(str(v)) > 19 else str(v)
        if t == 'BINARY':
            return str(v).encode('utf-8')
        if t == 'JSON':
            return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    except (ValueError, TypeError):
        return None
    return v


def convert_(v, target, charset=None):
    """CONVERT(v, type)"""
    return cast_(v, target)


CONTROL_FUNCTIONS = {
    'IF': if_,
    'IFNULL': ifnull_,
    'NULLIF': nullif_,
    'COALESCE': coalesce_,
    'ISNULL': is_null_,
    'CAST': cast_,
    'CONVERT': convert_,
}