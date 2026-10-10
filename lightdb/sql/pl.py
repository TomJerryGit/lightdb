"""
过程语言 (PL) 引擎
- 词法分析：tokenize
- 语法解析：PLParser → 语句列表
- 表达式：Pratt 解析器 + 求值
- 执行：PLInterpreter
"""
import re


# ==================== 词法 ====================

class Token:
    __slots__ = ('type', 'value', 'pos')

    def __init__(self, t, v, p=0):
        self.type = t
        self.value = v
        self.pos = p

    def __repr__(self):
        return f"Token({self.type}, {self.value!r})"


KEYWORDS = {
    'DECLARE', 'SET', 'IF', 'THEN', 'ELSEIF', 'ELSE', 'END',
    'WHILE', 'DO', 'LOOP', 'LEAVE', 'ITERATE',
    'BEGIN', 'RETURN', 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
    'INTO', 'FROM', 'WHERE', 'CALL', 'AND', 'OR', 'NOT',
    'NULL', 'TRUE', 'FALSE', 'IS', 'IN', 'LIKE', 'AS',
    'DEFAULT', 'OUT', 'INOUT',
}


def tokenize(sql: str):
    tokens = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ' \t\r\n':
            i += 1
        elif ch == '-' and i + 1 < n and sql[i + 1] == '-':
            while i < n and sql[i] != '\n':
                i += 1
        elif ch == '/' and i + 1 < n and sql[i + 1] == '*':
            j = sql.find('*/', i + 2)
            i = j + 2 if j >= 0 else n
        elif ch == "'":
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == '\\' and j + 1 < n:
                    buf.append(sql[j + 1])
                    j += 2
                elif sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                    else:
                        break
                else:
                    buf.append(sql[j])
                    j += 1
            tokens.append(Token('STRING', ''.join(buf), i))
            i = j + 1
        elif ch == '`':
            j = sql.find('`', i + 1)
            if j < 0:
                j = n
            tokens.append(Token('IDENT', sql[i + 1:j], i))
            i = j + 1
        elif ch.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            tokens.append(Token('NUM', sql[i:j], i))
            i = j
        elif ch.isalpha() or ch == '_':
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            word = sql[i:j]
            if word.upper() in KEYWORDS:
                tokens.append(Token('KW', word, i))
            else:
                tokens.append(Token('IDENT', word, i))
            i = j
        elif ch in '()[],;.':
            tokens.append(Token('PUNC', ch, i))
            i += 1
        elif ch in '=<>!+*/%-':
            if i + 1 < n and sql[i:i + 2] in ('<=', '>=', '!=', '<>', '&&', '||'):
                tokens.append(Token('OP', sql[i:i + 2], i))
                i += 2
            else:
                tokens.append(Token('OP', ch, i))
                i += 1
        else:
            i += 1
    return tokens


# ==================== PL 解析器 ====================

class PLParser:
    def __init__(self, text):
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self, k=0):
        if self.pos + k < len(self.tokens):
            return self.tokens[self.pos + k]
        return None

    def next(self):
        t = self.peek()
        if t:
            self.pos += 1
        return t

    def match_kw(self, *kws):
        t = self.peek()
        if t and t.type == 'KW' and t.value.upper() in [k.upper() for k in kws]:
            self.pos += 1
            return t.value.upper()
        return None

    def match_punc(self, *ps):
        t = self.peek()
        if t and t.type == 'PUNC' and t.value in ps:
            self.pos += 1
            return t.value
        return None

    def expect_kw(self, kw):
        t = self.next()
        if not t or t.type != 'KW' or t.value.upper() != kw.upper():
            raise SyntaxError(f"期望 {kw}, 得到 {t}")

    def expect_punc(self, p):
        t = self.next()
        if not t or t.type != 'PUNC' or t.value != p:
            raise SyntaxError(f"期望 {p}, 得到 {t}")

    def consume_until_kw(self, kw):
        """消耗 token 直到遇到顶层关键字（不含该关键字）"""
        start = self.pos
        depth = 0
        while self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.type == 'PUNC' and t.value == '(':
                depth += 1
            elif t.type == 'PUNC' and t.value == ')':
                depth -= 1
            elif depth == 0 and t.type == 'KW' and t.value.upper() == kw.upper():
                return self.tokens[start:self.pos]
            self.pos += 1
        raise SyntaxError(f"未找到关键字 {kw}")

    def consume_until_semicolon(self):
        """消耗 token 直到顶层分号（不含分号）"""
        start = self.pos
        depth = 0
        while self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.type == 'PUNC':
                if t.value == '(':
                    depth += 1
                elif t.value == ')':
                    depth -= 1
                elif t.value == ';' and depth == 0:
                    result = self.tokens[start:self.pos]
                    self.pos += 1
                    return result
            self.pos += 1
        return self.tokens[start:self.pos]

    def tokens_to_sql(self, toks):
        """把 token 列表转回 SQL 字符串"""
        parts = []
        for t in toks:
            if t.type == 'STRING':
                parts.append("'" + t.value.replace("'", "''") + "'")
            else:
                parts.append(t.value)
        return ' '.join(parts)

    # ==================== 程序解析 ====================
    def parse_program(self):
        stmts = []
        while self.pos < len(self.tokens):
            stmt = self.parse_statement()
            if stmt is None:
                break
            stmts.append(stmt)
        return stmts

    def parse_block(self, end_kws):
        """解析语句块，遇到 end_kws 之一则返回"""
        stmts = []
        while self.pos < len(self.tokens):
            t = self.peek()
            if t is None:
                break
            if t.type == 'KW' and t.value.upper() in [k.upper() for k in end_kws]:
                return stmts
            stmt = self.parse_statement()
            if stmt is None:
                break
            stmts.append(stmt)
        return stmts

    def parse_statement(self):
        t = self.peek()
        if t is None:
            return None

        # 跳过单独的 ';'
        if t.type == 'PUNC' and t.value == ';':
            self.pos += 1
            return self.parse_statement()

        upper = t.value.upper() if t.type in ('KW', 'IDENT') else ''

        if upper == 'DECLARE':
            return self.parse_declare()
        if upper == 'SET':
            return self.parse_set()
        if upper == 'IF':
            return self.parse_if()
        if upper == 'WHILE':
            return self.parse_while()
        if upper == 'RETURN':
            return self.parse_return()
        if upper == 'LEAVE':
            return self.parse_leave()
        if upper == 'ITERATE':
            return self.parse_iterate()

        # 普通 SQL 语句
        return self.parse_sql_statement()

    def parse_declare(self):
        """DECLARE name TYPE [DEFAULT expr];"""
        self.expect_kw('DECLARE')
        name_t = self.next()
        if not name_t or name_t.type not in ('IDENT',):
            raise SyntaxError("DECLARE 变量名错误")
        name = name_t.value

        # 类型（一个或多个标识符）
        type_parts = []
        while True:
            t = self.peek()
            if t is None:
                break
            if t.type == 'IDENT' and t.value.upper() not in ('DEFAULT',):
                type_parts.append(t.value)
                self.pos += 1
                # 跳过括号中的长度（如 VARCHAR(50)）
                if self.peek() and self.peek().type == 'PUNC' and self.peek().value == '(':
                    self.pos += 1
                    while self.peek() and not (self.peek().type == 'PUNC' and self.peek().value == ')'):
                        self.pos += 1
                    self.match_punc(')')
            else:
                break

        default_expr = None
        if self.match_kw('DEFAULT'):
            expr_toks = self.consume_until_semicolon()
            default_expr = expr_toks

        self.match_punc(';')
        return ('DECLARE', name, type_parts, default_expr)

    def parse_set(self):
        """SET name = expr;"""
        self.expect_kw('SET')
        name_t = self.next()
        if not name_t or name_t.type not in ('IDENT',):
            raise SyntaxError("SET 变量名错误")
        name = name_t.value
        if not (self.peek() and self.peek().type == 'OP' and self.peek().value == '='):
            raise SyntaxError("SET 缺少 =")
        self.pos += 1
        expr_toks = self.consume_until_semicolon()
        return ('SET', name, expr_toks)

    def parse_if(self):
        """IF cond THEN ... [ELSEIF cond THEN ...] [ELSE ...] END IF;"""
        self.expect_kw('IF')
        cond_toks = self.consume_until_kw('THEN')
        self.expect_kw('THEN')
        then_body = self.parse_block(('ELSEIF', 'ELSE', 'END'))

        branches = [(cond_toks, then_body)]

        while True:
            if self.match_kw('ELSEIF'):
                cond = self.consume_until_kw('THEN')
                self.expect_kw('THEN')
                body = self.parse_block(('ELSEIF', 'ELSE', 'END'))
                branches.append((cond, body))
            else:
                break

        else_body = None
        if self.match_kw('ELSE'):
            else_body = self.parse_block(('END',))

        self.expect_kw('END')
        self.expect_kw('IF')
        self.match_punc(';')
        return ('IF', branches, else_body)

    def parse_while(self):
        """WHILE cond DO ... END WHILE;"""
        self.expect_kw('WHILE')
        cond_toks = self.consume_until_kw('DO')
        self.expect_kw('DO')
        body = self.parse_block(('END',))
        self.expect_kw('END')
        self.expect_kw('WHILE')
        self.match_punc(';')
        return ('WHILE', cond_toks, body)

    def parse_return(self):
        """RETURN expr;"""
        self.expect_kw('RETURN')
        expr_toks = self.consume_until_semicolon()
        return ('RETURN', expr_toks)

    def parse_leave(self):
        """LEAVE label;"""
        self.expect_kw('LEAVE')
        t = self.next()
        label = t.value if t else ''
        self.match_punc(';')
        return ('LEAVE', label)

    def parse_iterate(self):
        self.expect_kw('ITERATE')
        t = self.next()
        label = t.value if t else ''
        self.match_punc(';')
        return ('ITERATE', label)

    def parse_sql_statement(self):
        """普通SQL语句或 CALL"""
        toks = self.consume_until_semicolon()
        if not toks:
            return None

        # 检查是否是 SELECT ... INTO
        # 找顶层 INTO
        depth = 0
        into_pos = -1
        for i, t in enumerate(toks):
            if t.type == 'PUNC':
                if t.value == '(':
                    depth += 1
                elif t.value == ')':
                    depth -= 1
            elif depth == 0 and t.type == 'KW' and t.value.upper() == 'INTO':
                into_pos = i
                break

        if toks[0].type == 'KW' and toks[0].value.upper() == 'SELECT' and into_pos > 0:
            return ('SELECT_INTO', toks, into_pos)

        sql = self.tokens_to_sql(toks)
        return ('SQL', sql)


# ==================== 表达式解析 ====================

class ExprParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self, k=0):
        if self.pos + k < len(self.tokens):
            return self.tokens[self.pos + k]
        return None

    def next(self):
        t = self.peek()
        if t:
            self.pos += 1
        return t

    def match_kw(self, *kws):
        t = self.peek()
        if t and t.type == 'KW' and t.value.upper() in [k.upper() for k in kws]:
            self.pos += 1
            return t.value.upper()
        return None

    def match_op(self, *ops):
        t = self.peek()
        if t and t.type == 'OP' and t.value in ops:
            self.pos += 1
            return t.value
        return None

    def match_punc(self, *ps):
        t = self.peek()
        if t and t.type == 'PUNC' and t.value in ps:
            self.pos += 1
            return t.value
        return None

    def parse(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.match_kw('OR') or self.match_op('||'):
            right = self.parse_and()
            left = ('or', left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.match_kw('AND') or self.match_op('&&'):
            right = self.parse_not()
            left = ('and', left, right)
        return left

    def parse_not(self):
        if self.match_kw('NOT'):
            return ('not', self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_add()
        t = self.peek()
        if t and t.type == 'OP' and t.value in ('=', '!=', '<>', '<', '>', '<=', '>='):
            self.pos += 1
            right = self.parse_add()
            return ('cmp', t.value, left, right)
        if self.match_kw('IS'):
            if self.match_kw('NOT'):
                self.match_kw('NULL')
                return ('notnull', left)
            self.match_kw('NULL')
            return ('isnull', left)
        if self.match_kw('LIKE'):
            right = self.parse_add()
            return ('like', left, right)
        if self.match_kw('IN'):
            self.match_punc('(')
            items = []
            while True:
                items.append(self.parse_or())
                if not self.match_punc(','):
                    break
            self.match_punc(')')
            return ('in', left, items)
        return left

    def parse_add(self):
        left = self.parse_mul()
        while True:
            t = self.peek()
            if t and t.type == 'OP' and t.value in ('+', '-'):
                self.pos += 1
                right = self.parse_mul()
                left = ('binop', t.value, left, right)
            else:
                break
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t and t.type == 'OP' and t.value in ('*', '/', '%'):
                self.pos += 1
                right = self.parse_unary()
                left = ('binop', t.value, left, right)
            else:
                break
        return left

    def parse_unary(self):
        t = self.peek()
        if t and t.type == 'OP' and t.value == '-':
            self.pos += 1
            return ('neg', self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        t = self.next()
        if t is None:
            raise SyntaxError("表达式意外结束")

        if t.type == 'NUM':
            if '.' in t.value:
                return ('lit', float(t.value))
            return ('lit', int(t.value))
        if t.type == 'STRING':
            return ('lit', t.value)

        if t.type in ('KW', 'IDENT'):
            v = t.value.upper()
            if v == 'NULL':
                return ('lit', None)
            if v == 'TRUE':
                return ('lit', True)
            if v == 'FALSE':
                return ('lit', False)

            # 函数调用
            if self.peek() and self.peek().type == 'PUNC' and self.peek().value == '(':
                self.pos += 1
                args = []
                while True:
                    if self.peek() and self.peek().type == 'PUNC' and self.peek().value == ')':
                        self.pos += 1
                        break
                    args.append(self.parse_or())
                    if not self.match_punc(','):
                        self.match_punc(')')
                        break
                return ('func', t.value, args)

            # 变量或列
            name = t.value
            # 处理 a.b
            while self.peek() and self.peek().type == 'PUNC' and self.peek().value == '.':
                self.pos += 1
                nxt = self.next()
                if nxt:
                    name += '.' + nxt.value
            return ('var', name)

        if t.type == 'PUNC' and t.value == '(':
            expr = self.parse_or()
            self.match_punc(')')
            return expr

        raise SyntaxError(f"意外的 token: {t}")


def eval_expr(node, ctx):
    """求值表达式"""
    op = node[0]

    if op == 'lit':
        return node[1]

    if op == 'var':
        name = node[1]
        # 检查变量
        if name in ctx.variables:
            return ctx.variables[name]
        # 检查 NEW.x / OLD.x
        if '.' in name:
            prefix, col = name.split('.', 1)
            pu = prefix.upper()
            if pu == 'NEW' and ctx.new_row is not None:
                return ctx.new_row.get(col)
            if pu == 'OLD' and ctx.old_row is not None:
                return ctx.old_row.get(col)
            # 变量名带前缀（少见）？
        # 未定义
        raise NameError(f"变量 '{name}' 未定义")

    if op == 'binop':
        a = eval_expr(node[2], ctx)
        b = eval_expr(node[3], ctx)
        o = node[1]
        if a is None or b is None:
            return None
        if o == '+': return a + b
        if o == '-': return a - b
        if o == '*': return a * b
        if o == '/': return a / b if b != 0 else None
        if o == '%': return a % b if b != 0 else None

    if op == 'neg':
        return -eval_expr(node[1], ctx)

    if op == 'and':
        return bool(eval_expr(node[1], ctx)) and bool(eval_expr(node[2], ctx))
    if op == 'or':
        return bool(eval_expr(node[1], ctx)) or bool(eval_expr(node[2], ctx))
    if op == 'not':
        return not bool(eval_expr(node[1], ctx))

    if op == 'cmp':
        a = eval_expr(node[2], ctx)
        b = eval_expr(node[3], ctx)
        o = node[1]
        if a is None or b is None:
            return False
        try:
            if isinstance(a, (int, float)) and isinstance(b, str):
                b = type(a)(b)
            elif isinstance(b, (int, float)) and isinstance(a, str):
                a = type(b)(a)
        except (ValueError, TypeError):
            pass
        if o == '=': return a == b
        if o in ('!=', '<>'): return a != b
        if o == '<': return a < b
        if o == '>': return a > b
        if o == '<=': return a <= b
        if o == '>=': return a >= b

    if op == 'isnull':
        return eval_expr(node[1], ctx) is None
    if op == 'notnull':
        return eval_expr(node[1], ctx) is not None

    if op == 'like':
        a = eval_expr(node[1], ctx)
        pat = eval_expr(node[2], ctx)
        if a is None or pat is None:
            return False
        pat = str(pat).replace('%', '.*').replace('_', '.')
        return bool(re.match('^' + pat + '$', str(a)))

    if op == 'in':
        a = eval_expr(node[1], ctx)
        return a in [eval_expr(n, ctx) for n in node[2]]

    if op == 'func':
        args = [eval_expr(a, ctx) for a in node[2]]
        return call_builtin(node[1], args)

    raise NotImplementedError(f"表达式类型: {op}")


def call_builtin(name, args):
    n = name.upper()
    if n == 'ABS': return abs(args[0]) if args[0] is not None else None
    if n == 'CEIL' or n == 'CEILING': return int(-(-args[0] // 1)) if args[0] is not None else None
    if n == 'FLOOR': return int(args[0] // 1) if args[0] is not None else None
    if n == 'ROUND': return round(args[0], int(args[1]) if len(args) > 1 else 0)
    if n == 'LENGTH': return len(str(args[0])) if args[0] is not None else None
    if n == 'UPPER': return str(args[0]).upper() if args[0] is not None else None
    if n == 'LOWER': return str(args[0]).lower() if args[0] is not None else None
    if n == 'CONCAT': return ''.join(str(a) for a in args if a is not None)
    if n == 'COALESCE':
        for a in args:
            if a is not None:
                return a
        return None
    if n == 'IFNULL':
        return args[0] if args[0] is not None else args[1]
    if n == 'IF':
        return args[1] if args[0] else args[2]
    if n == 'NULLIF':
        return None if args[0] == args[1] else args[0]
    if n == 'NOW':
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if n == 'CURDATE':
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d')
    if n == 'CURTIME':
        from datetime import datetime
        return datetime.now().strftime('%H:%M:%S')
    raise ValueError(f"未知函数: {name}")


# ==================== 执行上下文 ====================

class PLContext:
    def __init__(self, executor, session, db):
        self.executor = executor
        self.session = session
        self.db = db
        self.variables = {}       # 变量名 -> 值
        self.new_row = None       # 触发器 NEW（dict）
        self.old_row = None       # 触发器 OLD（dict）
        self.return_value = None
        self.out_params = {}      # OUT 参数回填
        self.leave_flag = None    # LEAVE 标记
        self.iterate_flag = None  # ITERATE 标记
        self.max_iterations = 100000
        self.iterations = 0


class PLInterpreter:
    def __init__(self, ctx):
        self.ctx = ctx

    def run(self, stmts):
        """执行语句列表"""
        for stmt in stmts:
            self._exec_stmt(stmt)
            if self.ctx.return_value is not None:
                break
            if self.ctx.leave_flag is not None:
                break
        return self.ctx.return_value

    def _exec_stmt(self, stmt):
        self.ctx.iterations += 1
        if self.ctx.iterations > self.ctx.max_iterations:
            raise RuntimeError("PL 执行超过最大迭代数")

        op = stmt[0]

        if op == 'DECLARE':
            _, name, types, default_toks = stmt
            value = None
            if default_toks:
                ep = ExprParser(default_toks)
                value = eval_expr(ep.parse(), self.ctx)
            self.ctx.variables[name] = value
            return

        if op == 'SET':
            _, name, expr_toks = stmt
            ep = ExprParser(expr_toks)
            value = eval_expr(ep.parse(), self.ctx)
            self.ctx.variables[name] = value
            return

        if op == 'IF':
            _, branches, else_body = stmt
            for cond_toks, body in branches:
                ep = ExprParser(cond_toks)
                if eval_expr(ep.parse(), self.ctx):
                    for s in body:
                        self._exec_stmt(s)
                        if self.ctx.return_value is not None or self.ctx.leave_flag is not None:
                            return
                    return
            if else_body:
                for s in else_body:
                    self._exec_stmt(s)
                    if self.ctx.return_value is not None or self.ctx.leave_flag is not None:
                        return
            return

        if op == 'WHILE':
            _, cond_toks, body = stmt
            while True:
                ep = ExprParser(cond_toks)
                if not eval_expr(ep.parse(), self.ctx):
                    break
                for s in body:
                    self._exec_stmt(s)
                    if self.ctx.return_value is not None or self.ctx.leave_flag is not None:
                        return
                    if self.ctx.iterate_flag is not None:
                        self.ctx.iterate_flag = None
                        break
            return

        if op == 'RETURN':
            _, expr_toks = stmt
            if expr_toks:
                ep = ExprParser(expr_toks)
                self.ctx.return_value = eval_expr(ep.parse(), self.ctx)
            else:
                self.ctx.return_value = None
            return

        if op == 'LEAVE':
            self.ctx.leave_flag = stmt[1]
            return

        if op == 'ITERATE':
            self.ctx.iterate_flag = stmt[1]
            return

        if op == 'SELECT_INTO':
            _, toks, into_pos = stmt
            # 分割: [SELECT ... 列] INTO [var1, var2] [FROM ...]
            col_toks = toks[1:into_pos]
            rest = toks[into_pos + 1:]

            # 找 FROM
            from_pos = -1
            for i, t in enumerate(rest):
                if t.type == 'KW' and t.value.upper() == 'FROM':
                    from_pos = i
                    break

            if from_pos < 0:
                # SELECT ... INTO var  （无 FROM）
                var_toks = rest
                # 简单表达式赋给变量
                # 用表达式解析
                ep = ExprParser(col_toks)
                value = eval_expr(ep.parse(), self.ctx)
                if len(var_toks) == 1 and var_toks[0].type == 'IDENT':
                    self.ctx.variables[var_toks[0].value] = value
                return

            var_toks = rest[:from_pos]
            from_toks = rest[from_pos:]
            # 变量名列表
            var_names = []
            i = 0
            while i < len(var_toks):
                t = var_toks[i]
                if t.type == 'IDENT':
                    var_names.append(t.value)
                i += 1

            # 构造 SELECT SQL
            col_sql = PLParser('').tokens_to_sql(col_toks)
            from_sql = PLParser('').tokens_to_sql(from_toks)
            sql = f"SELECT {col_sql} {from_sql} LIMIT 1"

            # 执行
            from .parser import parser as sql_parser
            p = sql_parser.parse(sql)
            if p:
                result = self.ctx.executor.execute(p, self.ctx.session)
                if result.get('type') == 'resultset' and result['rows']:
                    row = result['rows'][0]
                    for i, v in enumerate(row):
                        if i < len(var_names):
                            self.ctx.variables[var_names[i]] = v
            return

        if op == 'SQL':
            sql = stmt[1]
            up = sql.strip().upper()

            # 处理 CALL
            if up.startswith('CALL '):
                from .stored import call_procedure
                call_procedure(sql, self.ctx)
                return

            # 替换变量（简单：${var} → 值）
            sql_replaced = self._substitute_vars(sql)
            from .parser import parser as sql_parser
            p = sql_parser.parse(sql_replaced)
            if p:
                self.ctx.executor.execute(p, self.ctx.session)
            return

        raise NotImplementedError(f"未知语句类型: {op}")

    def _substitute_vars(self, sql):
        """把变量注入SQL（简化：直接替换列名匹配的变量）"""
        # 不做复杂替换，保留原样
        # 变量在 SQL 里不可见，除非通过 NEW.x / OLD.x（这在触发器中已由 ctx 处理）
        return sql