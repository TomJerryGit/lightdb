"""生成列；CHECK 约束的实现与求值在 `checks.py`"""
import re


class GeneratedColumn:
    def __init__(self, name, expr, stored=False):
        self.name = name
        self.expr = expr
        self.stored = stored

    def eval(self, row, col_names):
        return eval_expr(self.expr, row, col_names)


class CheckConstraint:
    """单条 CHECK 约束（名字 + 表达式原文）

    求值统一走 `sql/checks.py`（SQL 三值逻辑 + 显式报错）：UNKNOWN ——
    NULL 参与比较 —— 按 SQL / MySQL 语义**放行**；求值失败一律抛
    `CheckConstraintError`，绝不吞掉异常当成"通过"（旧实现
    `except: return True` 正是这样把约束变成摆设的）。
    """

    def __init__(self, name, expr):
        self.name = name
        self.expr = expr

    def check(self, row, col_names, table=""):
        """True = 通过（含 UNKNOWN）；False = 违规；求值失败 → 显式抛错"""
        from .checks import RowResolver, evaluate_check
        resolver = RowResolver(col_names, row, table)
        return evaluate_check(self.expr, resolver, context=table) is not False


def eval_expr(expr, row, col_names):
    s = expr.strip()

    # 函数形式
    m = re.match(r'CONCAT\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        parts = _split_top(m.group(1))
        return ''.join(str(_resolve(p.strip(), row, col_names) or '') for p in parts)

    m = re.match(r'UPPER\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        v = _resolve(m.group(1).strip(), row, col_names)
        return str(v).upper() if v is not None else None

    m = re.match(r'LOWER\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        v = _resolve(m.group(1).strip(), row, col_names)
        return str(v).lower() if v is not None else None

    m = re.match(r'LENGTH\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        v = _resolve(m.group(1).strip(), row, col_names)
        return len(str(v)) if v is not None else 0

    m = re.match(r'ABS\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        v = _resolve(m.group(1).strip(), row, col_names)
        return abs(v) if v is not None else None

    m = re.match(r'COALESCE\s*\((.+)\)\s*$', s, re.IGNORECASE)
    if m:
        for p in _split_top(m.group(1)):
            v = _resolve(p.strip(), row, col_names)
            if v is not None:
                return v
        return None

    # 二元运算（优先级：* / 高于 + -）
    for op in ['+', '-']:
        idx = _find_binop(s, op)
        if idx >= 0:
            a = eval_expr(s[:idx], row, col_names)
            b = eval_expr(s[idx + 1:], row, col_names)
            if a is None or b is None:
                return None
            return a + b if op == '+' else a - b

    for op in ['*', '/']:
        idx = _find_binop(s, op)
        if idx >= 0:
            a = eval_expr(s[:idx], row, col_names)
            b = eval_expr(s[idx + 1:], row, col_names)
            if a is None or b is None or (op == '/' and b == 0):
                return None
            return a * b if op == '*' else a / b

    # 括号
    if s.startswith('(') and s.endswith(')'):
        return eval_expr(s[1:-1], row, col_names)

    return _resolve(s, row, col_names)


def _resolve(token, row, col_names):
    token = token.strip()
    if not token:
        return None
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    if token.upper() == 'NULL':
        return None
    try:
        if '.' in token:
            return float(token)
        return int(token)
    except (ValueError, TypeError):
        pass
    if token in col_names:
        return row[col_names.index(token)]
    return None


def _split_top(s):
    parts, cur, depth, in_str = [], '', 0, None
    for ch in s:
        if in_str:
            cur += ch
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
            cur += ch
        elif ch == '(':
            depth += 1
            cur += ch
        elif ch == ')':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _find_binop(s, op):
    """从右向左找顶层二元操作符（避免匹配一元负号）"""
    depth = 0
    in_str = None
    result = -1
    for i, ch in enumerate(s):
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and ch == op:
            # 一元负号检查
            if op == '-':
                if i == 0:
                    continue
                prev = s[i - 1]
                if prev in '+-*/(' :
                    continue
            result = i
    return result
