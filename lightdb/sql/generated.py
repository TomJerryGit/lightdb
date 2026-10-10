"""生成列 + CHECK 约束"""
import re


class GeneratedColumn:
    def __init__(self, name, expr, stored=False):
        self.name = name
        self.expr = expr
        self.stored = stored

    def eval(self, row, col_names):
        return eval_expr(self.expr, row, col_names)


class CheckConstraint:
    def __init__(self, name, expr):
        self.name = name
        self.expr = expr

    def check(self, row, col_names):
        try:
            return bool(_eval_check(self.expr, row, col_names))
        except (ValueError, TypeError, IndexError):
            return True


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


# ==================== CHECK ====================

def _eval_check(expr, row, col_names):
    # AND
    parts = _split_logical(expr, 'AND')
    if len(parts) > 1:
        return all(_eval_check(p, row, col_names) for p in parts)

    parts = _split_logical(expr, 'OR')
    if len(parts) > 1:
        return any(_eval_check(p, row, col_names) for p in parts)

    up = expr.strip().upper()
    if up.startswith('NOT '):
        return not _eval_check(expr.strip()[4:], row, col_names)

    # 比较
    for op in ['<=', '>=', '!=', '<>', '=', '<', '>']:
        idx = _find_op(expr, op)
        if idx >= 0:
            left = expr[:idx].strip()
            right = expr[idx + len(op):].strip()
            a = eval_expr(left, row, col_names)
            b = eval_expr(right, row, col_names)

            # 类型兼容
            try:
                if isinstance(a, (int, float)) and isinstance(b, str):
                    b = type(a)(b)
                elif isinstance(b, (int, float)) and isinstance(a, str):
                    a = type(b)(a)
            except (ValueError, TypeError):
                pass

            if a is None or b is None:
                return False

            if op == '<=': return a <= b
            if op == '>=': return a >= b
            if op in ('!=', '<>'): return a != b
            if op == '=': return a == b
            if op == '<': return a < b
            if op == '>': return a > b
    return True


def _split_logical(expr, kw):
    parts, cur, depth, in_str = [], '', 0, None
    i, n = 0, len(expr)
    up = expr.upper()
    klen = len(kw)
    while i < n:
        ch = expr[i]
        if in_str:
            cur += ch
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            cur += ch
            i += 1
        elif ch == '(':
            depth += 1
            cur += ch
            i += 1
        elif ch == ')':
            depth -= 1
            cur += ch
            i += 1
        elif depth == 0 and up[i:i + klen] == kw:
            left_ok = (i == 0 or not up[i - 1].isalnum())
            right_ok = (i + klen >= n or not up[i + klen].isalnum())
            if left_ok and right_ok:
                parts.append(cur.strip())
                cur = ''
                i += klen
            else:
                cur += ch
                i += 1
        else:
            cur += ch
            i += 1
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _find_op(expr, op):
    depth = 0
    in_str = None
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_str:
            if ch == in_str:
                in_str = None
            i += 1
        elif ch in ("'", '"'):
            in_str = ch
            i += 1
        elif ch == '(':
            depth += 1
            i += 1
        elif ch == ')':
            depth -= 1
            i += 1
        elif depth == 0 and expr[i:i + len(op)] == op:
            # 避免匹配 `<=` 内的 `<`
            if op == '<' and i + 1 < len(expr) and expr[i + 1] in '=>':
                i += 1
                continue
            if op == '>' and i + 1 < len(expr) and expr[i + 1] == '=':
                i += 1
                continue
            return i
        else:
            i += 1
    return -1