"""CHECK 约束：定义期校验 + 写入期求值（SQL 三值逻辑）

本模块是 `CREATE TABLE ... CHECK (...)` 的**唯一**实现，被两条写入路径共用：

* 存储层直写 —— `db/table.py::Table._insert_locked` / `_update_locked`
  在写完类型转换之后、落 WAL 之前调用 `Table._validate_checks`；
* SQL 路径 —— `INSERT` / `UPDATE` 最终也走上面两个方法，所以只有一份校验；
  违规时 `sql/executor.py::Executor.execute` 把 `CheckConstraintError`
  渲染成 `ERROR 3819 (HY000)` 风格的错误结果。

设计红线（与 docs/FAILURE_TRIAGE.md 的「要么生效，要么显式报错」一致）:

1. **UNKNOWN 放行**：表达式求值为 UNKNOWN（NULL 参与比较）时**不**算违规
   —— 与 MySQL 一致（`CHECK (price > 0)` 允许 `price IS NULL`）。
2. **绝不返回"看起来像通过"的替代结果**：解析不了 / 求值抛异常 /
   函数不认识，一律 `CheckConstraintError`。旧实现
   `except (ValueError, TypeError, IndexError): return True` 把
   "约束算不出来" 变成 "约束通过"，是标准静默放行，已删除。
3. **定义期就报错**：`validate_check_definitions` 在 `CREATE TABLE` 时
   解析表达式、校验列引用与函数名。这样不合法的约束根本进不了元数据，
   也不会在第一次 INSERT 时才炸。

支持的形态（其余一律显式报错，见 `_CheckParser._err`）::

    比较       =  !=  <>  <  <=  >  >=
    逻辑       AND / OR / NOT（三值逻辑）
    NULL 判定  IS NULL / IS NOT NULL
    集合       [NOT] IN (v1, v2, ...)
    区间       [NOT] BETWEEN a AND b
    模式       [NOT] LIKE 'a%'
    算术       + - * / %、括号、一元负号
    字面量     数字 / '字符串' / NULL / TRUE / FALSE
    函数       parser.SCALAR_FUNCTION_NAMES（LOWER/UPPER/LENGTH/TRIM/
               ABS/ROUND/CONCAT/JSON_EXTRACT/JSON_UNQUOTE）
"""
import re

# MySQL 错误码 / SQLSTATE：
#   3819 ER_CHECK_CONSTRAINT_VIOLATED                —— 约束被违反
#   3813 ER_CHECK_CONSTRAINT_REFERS_TO_UNKNOWN_COLUMN —— 定义期引用了不存在的列
CHECK_ERRNO = 3819
CHECK_DEF_ERRNO = 3813
CHECK_SQLSTATE = 'HY000'


class CheckConstraintError(ValueError):
    """CHECK 约束违规 / 定义错误

    继承 `ValueError`：`Table.insert` / `Table.update` 的既有契约就是
    ValueError（不允许为空、主键重复、类型转换失败都是它），调用方不必
    换异常类型就能捕获；`errno` / `sqlstate` 让 SQL 层能原样渲染成
    `ERROR 3819 (HY000): ...`。
    """

    def __init__(self, message, errno=CHECK_ERRNO, sqlstate=CHECK_SQLSTATE,
                 table=None, constraint=None, column=None):
        super().__init__(message)
        self.errno = errno
        self.sqlstate = sqlstate
        self.table = table
        self.constraint = constraint
        self.column = column


# ==================== 词法 ====================

# 与 `parser._EXPR_TOKEN_RE` 保持一致的列/字面量规则，额外补上比较运算符
# （标量表达式词法里没有 `=` `<` `>`，而谓词层必须切开它们）
_CHECK_TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<num>\d+\.\d+|\d+)
    | (?P<str>'(?:[^']|'')*')
    | (?P<qid>`[^`]*`|"[^"]*")
    | (?P<ident>[A-Za-z_]\w*)
    | (?P<op><=|>=|<>|!=|=|<|>|\+|-|\*|/|%|\(|\)|,)
""", re.VERBOSE)

_COMPARE_OPS = ('<=', '>=', '<>', '!=', '=', '<', '>')
_PRED_KEYWORDS = ('AND', 'OR', 'IS', 'NOT', 'IN', 'BETWEEN', 'LIKE')


def _tokenize(text):
    toks = []
    i, n = 0, len(text)
    while i < n:
        m = _CHECK_TOKEN_RE.match(text, i)
        if m is None:
            raise CheckConstraintError(
                f"CHECK 表达式里有无法识别的字符 {text[i]!r}: {text!r}"
                f" [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
                errno=CHECK_DEF_ERRNO)
        i = m.end()
        if m.lastgroup == 'ws':
            continue
        toks.append((m.lastgroup, m.group(0), m.start(), m.end()))
    return toks


# ==================== 语法 ====================

class _CheckParser:
    """CHECK 谓词递归下降解析器

    文法::

        pred      := or
        or        := and (OR and)*
        and       := not (AND not)*
        not       := NOT not | predicate
        predicate := operand ( cmp operand
                             | IS [NOT] NULL
                             | [NOT] IN '(' operand (',' operand)* ')'
                             | [NOT] BETWEEN operand AND operand
                             | [NOT] LIKE operand )?
        operand   := 标量表达式（交给 parser.parse_scalar_expr）
                   | '(' pred ')'（嵌套谓词）

    运算对象是**原文切片**，再交给 `parser` 既有的标量表达式解析/求值器，
    因此列、函数、算术、NULL 传播的语义与 SELECT 完全同一份实现。
    """

    def __init__(self, text, toks):
        self.text = text
        self.toks = toks
        self.pos = 0

    # ---------- 工具 ----------

    def _peek(self, off=0):
        i = self.pos + off
        return self.toks[i] if i < len(self.toks) else (None, None, 0, 0)

    def _at_kw(self, *kws, off=0):
        kind, val, _s, _e = self._peek(off)
        return kind == 'ident' and val.upper() in kws

    def _at_op(self, *ops, off=0):
        kind, val, _s, _e = self._peek(off)
        return kind == 'op' and val in ops

    def _eat_kw(self, *kws):
        if self._at_kw(*kws):
            self.pos += 1
            return True
        return False

    def _eat_op(self, *ops):
        if self._at_op(*ops):
            val = self._peek()[1]
            self.pos += 1
            return val
        return None

    def _err(self, msg):
        return CheckConstraintError(
            f"{msg}（CHECK 表达式: {self.text!r}）"
            f" [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
            errno=CHECK_DEF_ERRNO)

    # ---------- 文法 ----------

    def parse(self):
        node = self._or()
        if self.pos != len(self.toks):
            raise self._err(f"在 {self._peek()[1]!r} 处无法继续解析")
        return node

    def _or(self):
        left = self._and()
        while self._eat_kw('OR'):
            left = {'kind': 'or', 'left': left, 'right': self._and()}
        return left

    def _and(self):
        left = self._not()
        while self._eat_kw('AND'):
            left = {'kind': 'and', 'left': left, 'right': self._not()}
        return left

    def _not(self):
        if self._eat_kw('NOT'):
            return {'kind': 'not', 'operand': self._not()}
        return self._predicate()

    def _predicate(self):
        left = self._operand()

        if self._at_kw('IS'):
            self.pos += 1
            neg = self._eat_kw('NOT')
            if not self._eat_kw('NULL'):
                raise self._err(
                    "IS 后面只支持 NULL / NOT NULL"
                    "（IS TRUE / IS FALSE 等形态本引擎暂不支持）")
            return {'kind': 'isnull', 'neg': neg, 'operand': left}

        neg = False
        if self._at_kw('NOT') and self._at_kw('IN', 'BETWEEN', 'LIKE', off=1):
            self.pos += 1
            neg = True

        if self._eat_kw('IN'):
            if self._eat_op('(') is None:
                raise self._err("IN 后面必须是括号里的值清单")
            values = [self._operand()]
            while self._eat_op(','):
                values.append(self._operand())
            if self._eat_op(')') is None:
                raise self._err("IN 的值清单缺少右括号")
            return {'kind': 'in', 'neg': neg, 'operand': left,
                    'values': values}

        if self._eat_kw('BETWEEN'):
            low = self._operand()
            if not self._eat_kw('AND'):
                raise self._err("BETWEEN 缺少 AND")
            high = self._operand()
            return {'kind': 'between', 'neg': neg, 'operand': left,
                    'low': low, 'high': high}

        if self._eat_kw('LIKE'):
            pattern = self._operand()
            return {'kind': 'like', 'neg': neg, 'operand': left,
                    'pattern': pattern}

        if self._at_op(*_COMPARE_OPS):
            op = self._eat_op(*_COMPARE_OPS)
            right = self._operand()
            return {'kind': 'cmp', 'op': '!=' if op == '<>' else op,
                    'left': left, 'right': right}

        return {'kind': 'bool', 'operand': left}

    def _operand(self):
        """收集一个运算对象的**原文切片**（顶层遇谓词边界即停）"""
        start = self._peek()[2]
        depth = 0
        last_end = None
        while self.pos < len(self.toks):
            kind, val, s, e = self._peek()
            if kind == 'op':
                if val == '(':
                    depth += 1
                elif val == ')':
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and (val == ',' or val in _COMPARE_OPS):
                    break
            elif kind == 'ident' and depth == 0 \
                    and val.upper() in _PRED_KEYWORDS:
                break
            self.pos += 1
            last_end = e
        if last_end is None:
            raise self._err(
                f"缺少运算对象（在 {self._peek()[1]!r} 处）")
        return self._make_operand(self.text[start:last_end])

    def _make_operand(self, text):
        node = _scalar_expr(text)
        if node is not None and node.get('kind') != 'star':
            return {'ck': 'scalar', 'node': node, 'text': text}
        stripped = text.strip()
        if stripped.startswith('(') and stripped.endswith(')'):
            inner = stripped[1:-1].strip()
            sub = _scalar_expr(inner)
            if sub is not None and sub.get('kind') != 'star':
                return {'ck': 'scalar', 'node': sub, 'text': text}
            return {'ck': 'pred',
                    'node': parse_check_predicate(inner),
                    'text': text}
        raise self._err(f"无法解析运算对象 {text!r}")


def _scalar_expr(text):
    """惰性调用 parser 的标量表达式解析器（避免模块级循环导入）"""
    from .parser import parse_scalar_expr
    return parse_scalar_expr(text)


#: 解析结果缓存：键是表达式原文。CHECK 表达式在每行写入时都要求值，
#: 每次重新做词法/语法分析是纯浪费；表达式文本数量极少（每张表几条），
#: 因此用一个带上限的普通字典即可。
_NODE_CACHE = {}
_NODE_CACHE_LIMIT = 512


def parse_check_predicate(expr):
    """把 CHECK 表达式文本解析成谓词节点（可 JSON 序列化的纯 dict）

    解析不了 / 有本引擎不支持的形态时抛 `CheckConstraintError`
    （errno 3813），**绝不返回 None** 让调用方"当没看见"。
    """
    text = str(expr or '').strip()
    if not text:
        raise CheckConstraintError(
            f"CHECK 表达式为空 [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
            errno=CHECK_DEF_ERRNO)
    cached = _NODE_CACHE.get(text)
    if cached is not None:
        return cached
    node = _CheckParser(text, _tokenize(text)).parse()
    if len(_NODE_CACHE) >= _NODE_CACHE_LIMIT:
        _NODE_CACHE.clear()
    _NODE_CACHE[text] = node
    return node


# ==================== 求值（三值逻辑） ====================

def _and3(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _or3(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _not3(a):
    return None if a is None else (not a)


def _like_regex(pattern):
    parts = ['(?s)']
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == '\\' and i + 1 < n:
            parts.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == '%':
            parts.append('.*')
        elif ch == '_':
            parts.append('.')
        else:
            parts.append(re.escape(ch))
        i += 1
    return re.compile(''.join(parts) + r'\Z')


def _runtime_error(msg, context=''):
    where = f"（表 {context}）" if context else ""
    return CheckConstraintError(
        f"CHECK 表达式求值失败{where}: {msg}"
        f" [ERROR {CHECK_ERRNO} ({CHECK_SQLSTATE})]",
        errno=CHECK_ERRNO)


def _truth(value, context=''):
    """把标量值按 SQL 布尔语义收敛；NULL → None(UNKNOWN)，否则必须是布尔/数值"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    raise _runtime_error(
        f"表达式结果 {value!r} 不是布尔值，无法作为 CHECK 条件", context)


def _eval_operand(op, resolve, context):
    if op.get('ck') == 'pred':
        return evaluate_check_node(op['node'], resolve, context)
    from .parser import eval_expr_node
    return eval_expr_node(op['node'], resolve, position='CHECK')


def evaluate_check_node(node, resolve, context=''):
    """求值一个 CHECK 谓词节点

    返回 `True`（通过）/ `False`（违规）/ `None`（UNKNOWN —— NULL 参与的
    比较，按 SQL 三值逻辑**不算违规**，与 MySQL 一致）。

    resolve(name) -> 值；列解析不到时**必须抛异常**（由调用方给出措辞）。
    """
    try:
        return _eval_node(node, resolve, context)
    except CheckConstraintError:
        raise
    except Exception as exc:                     # noqa: BLE001 - 转成显式报错
        raise _runtime_error(f"{type(exc).__name__}: {exc}", context)


def _eval_node(node, resolve, context):
    kind = node.get('kind')

    if kind == 'and':
        return _and3(_eval_node(node['left'], resolve, context),
                     _eval_node(node['right'], resolve, context))
    if kind == 'or':
        return _or3(_eval_node(node['left'], resolve, context),
                    _eval_node(node['right'], resolve, context))
    if kind == 'not':
        return _not3(_eval_node(node['operand'], resolve, context))

    if kind == 'cmp':
        from .parser import compare_values
        a = _eval_operand(node['left'], resolve, context)
        b = _eval_operand(node['right'], resolve, context)
        return compare_values(a, node['op'], b)

    if kind == 'isnull':
        val = _eval_operand(node['operand'], resolve, context)
        return (val is not None) if node['neg'] else (val is None)

    if kind == 'in':
        from .parser import compare_values
        val = _eval_operand(node['operand'], resolve, context)
        if val is None:
            return None
        has_null = False
        matched = False
        for item in node['values']:
            cand = _eval_operand(item, resolve, context)
            if cand is None:
                has_null = True
                continue
            if compare_values(val, '=', cand):
                matched = True
                break
        if matched:
            verdict = True
        elif has_null:
            verdict = None          # IN 列表里有 NULL 且未命中 → UNKNOWN
        else:
            verdict = False
        return _not3(verdict) if node['neg'] else verdict

    if kind == 'between':
        from .parser import compare_values
        val = _eval_operand(node['operand'], resolve, context)
        low = _eval_operand(node['low'], resolve, context)
        high = _eval_operand(node['high'], resolve, context)
        if val is None:
            verdict = None
        else:
            # `x BETWEEN a AND b` ≡ `x >= a AND x <= b`（逐边三值逻辑）
            ge = None if low is None else compare_values(val, '>=', low)
            le = None if high is None else compare_values(val, '<=', high)
            verdict = _and3(ge, le)
        return _not3(verdict) if node['neg'] else verdict

    if kind == 'like':
        val = _eval_operand(node['operand'], resolve, context)
        pattern = _eval_operand(node['pattern'], resolve, context)
        if val is None or pattern is None:
            verdict = None
        else:
            if not isinstance(pattern, str):
                raise _runtime_error(
                    f"LIKE 的模式必须是字符串，实际是 {pattern!r}", context)
            verdict = bool(_like_regex(pattern).match(str(val)))
        return _not3(verdict) if node['neg'] else verdict

    if kind == 'bool':
        return _truth(_eval_operand(node['operand'], resolve, context),
                      context)

    raise _runtime_error(f"无法求值的谓词节点: {node!r}", context)


def evaluate_check(expr, resolve, context=''):
    """文本或节点 → True / False / None(UNKNOWN)；见 `evaluate_check_node`"""
    node = expr if isinstance(expr, dict) else parse_check_predicate(expr)
    return evaluate_check_node(node, resolve, context)


# ==================== 列引用收集 / 定义期校验 ====================

def _walk_columns(node, out):
    kind = node.get('kind')
    if kind in ('and', 'or'):
        _walk_columns(node['left'], out)
        _walk_columns(node['right'], out)
    elif kind == 'not':
        _walk_columns(node['operand'], out)
    elif kind == 'cmp':
        _operand_columns(node['left'], out)
        _operand_columns(node['right'], out)
    elif kind in ('isnull', 'bool'):
        _operand_columns(node['operand'], out)
    elif kind == 'in':
        _operand_columns(node['operand'], out)
        for item in node['values']:
            _operand_columns(item, out)
    elif kind == 'between':
        for key in ('operand', 'low', 'high'):
            _operand_columns(node[key], out)
    elif kind == 'like':
        _operand_columns(node['operand'], out)
        _operand_columns(node['pattern'], out)


def _operand_columns(op, out):
    if op.get('ck') == 'pred':
        _walk_columns(op['node'], out)
    else:
        from .parser import expr_columns_outside_agg
        expr_columns_outside_agg(op['node'], out)


def _walk_functions(node, out):
    kind = node.get('kind')
    if kind in ('and', 'or'):
        _walk_functions(node['left'], out)
        _walk_functions(node['right'], out)
    elif kind == 'not':
        _walk_functions(node['operand'], out)
    elif kind == 'cmp':
        _operand_functions(node['left'], out)
        _operand_functions(node['right'], out)
    elif kind in ('isnull', 'bool'):
        _operand_functions(node['operand'], out)
    elif kind == 'in':
        _operand_functions(node['operand'], out)
        for item in node['values']:
            _operand_functions(item, out)
    elif kind == 'between':
        for key in ('operand', 'low', 'high'):
            _operand_functions(node[key], out)
    elif kind == 'like':
        _operand_functions(node['operand'], out)
        _operand_functions(node['pattern'], out)


def _operand_functions(op, out):
    if op.get('ck') == 'pred':
        _walk_functions(op['node'], out)
        return
    node = op['node']
    kind = node.get('kind') if isinstance(node, dict) else None
    if kind == 'func':
        out.append(str(node.get('name') or '').upper())
        for arg in (node.get('args') or []):
            _operand_functions({'ck': 'scalar', 'node': arg}, out)
    elif kind == 'arith':
        _operand_functions({'ck': 'scalar', 'node': node.get('left')}, out)
        _operand_functions({'ck': 'scalar', 'node': node.get('right')}, out)


def check_columns(node):
    """CHECK 节点引用的列名（含限定名，按出现顺序）"""
    out = []
    _walk_columns(node, out)
    return out


def check_functions(node):
    """CHECK 节点调用的函数名（大写，按出现顺序）"""
    out = []
    _walk_functions(node, out)
    return out


def _column_index(cols):
    return {str(c.get('name')): c for c in cols}


def validate_check_definition(table, expr, cols, name=''):
    """定义期校验一条 CHECK 表达式；不合法 → 抛 CheckConstraintError(3813)

    校验三件事（都在建表时就做，避免"建表成功、第一次写入才炸"）：
    1. 表达式能否解析（含"本引擎不支持的形态"）；
    2. 引用的列是否存在；
    3. 调用的函数是否在 `parser.SCALAR_FUNCTION_NAMES` 里。
    """
    from .parser import AGGREGATE_FUNCTION_NAMES, SCALAR_FUNCTION_ARITY

    where = f"表 {table} 的 CHECK 约束 {name}" if name else f"表 {table} 的 CHECK 约束"
    node = parse_check_predicate(expr)
    known = set(_column_index(cols))

    # 聚合函数在 CHECK 里没有意义（每行求值没有分组上下文）。必须在
    # **定义期**拒绝：否则列引用收集会把聚合内的列跳过，约束一路建成功，
    # 直到第一次写入才报错。
    aggs = [f for f in check_functions(node)
            if f in AGGREGATE_FUNCTION_NAMES]
    if aggs:
        raise CheckConstraintError(
            f"{where} 不允许聚合函数: {', '.join(sorted(set(aggs)))}"
            f"（表达式 {expr!r}） [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
            errno=CHECK_DEF_ERRNO, table=table, constraint=name)

    unknown = []
    for raw in check_columns(node):
        col = str(raw)
        if '.' in col:
            qual, _, base = col.rpartition('.')
            if qual.strip('`"') == str(table):
                col = base
        if col not in known and col not in unknown:
            unknown.append(col)
    if unknown:
        raise CheckConstraintError(
            f"{where} 引用了不存在的列: {', '.join(unknown)}"
            f"（表达式 {expr!r}） [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
            errno=CHECK_DEF_ERRNO, table=table, constraint=name)

    bad_funcs = [f for f in check_functions(node)
                 if f not in SCALAR_FUNCTION_ARITY]
    if bad_funcs:
        raise CheckConstraintError(
            f"{where} 使用了不支持的函数: {', '.join(sorted(set(bad_funcs)))}"
            f"（表达式 {expr!r}） [ERROR {CHECK_DEF_ERRNO} ({CHECK_SQLSTATE})]",
            errno=CHECK_DEF_ERRNO, table=table, constraint=name)
    return node


def resolve_check_names(table, cols, checks):
    """按 MySQL 规则给未命名的 CHECK 补 `{table}_chk_{n}`，返回规范化的表级清单

    MySQL 的自动名是 `表名_chk_序号`（序号按约束在语句里的出现顺序）。
    列级 CHECK 的名字写回 `cols[i]['check']['name']`（列定义本来就是随
    元数据一起持久化的），表级 CHECK 的规范化结果由调用方存进表元数据。
    """
    n = 0
    for col in cols or []:
        ck = col.get('check')
        if not ck or not ck.get('expr'):
            continue
        n += 1
        if not ck.get('name'):
            ck['name'] = f"{table}_chk_{n}"
    out = []
    for ck in checks or []:
        if not ck or not ck.get('expr'):
            continue
        item = dict(ck)
        n += 1
        if not item.get('name'):
            item['name'] = f"{table}_chk_{n}"
        item['expr'] = str(item['expr']).strip()
        out.append(item)
    return out


def validate_check_definitions(table, cols, checks):
    """校验**全部** CHECK：列级（cols[i]['check']）+ 表级（checks）"""
    for col in cols or []:
        ck = col.get('check')
        if ck and ck.get('expr'):
            validate_check_definition(
                table, ck['expr'], cols, ck.get('name') or col.get('name'))
    for ck in checks or []:
        if ck and ck.get('expr'):
            validate_check_definition(
                table, ck['expr'], cols, ck.get('name') or '')


def format_violation(table, name, expr, column=None, value=None,
                     has_value=False):
    """违规文案（MySQL 风格：`ERROR 3819 (HY000): ...`）"""
    detail = ''
    if column:
        detail = f"，{column} = {value!r}" if has_value else f"，列 {column}"
    return (f"Check constraint '{name}' is violated."
            f" [ERROR {CHECK_ERRNO} ({CHECK_SQLSTATE})]"
            f" 表 {table} 的 CHECK 约束 ({expr}) 未通过{detail}")


class RowResolver:
    """把 CHECK 表达式里的列引用解析成行值（未知列 → 显式报错）"""

    __slots__ = ('_idx', '_row', '_table')

    def __init__(self, col_names, row, table=''):
        self._idx = {str(n): i for i, n in enumerate(col_names)}
        self._row = row
        self._table = table

    def __call__(self, name):
        key = str(name)
        if '.' in key:
            qual, _, base = key.rpartition('.')
            if not self._table or qual.strip('`"') == str(self._table):
                key = base
        i = self._idx.get(key)
        if i is None:
            raise ValueError(
                f"CHECK 表达式引用了不存在的列 {name}")
        return self._row[i]
