"""
过程语言 (PL) 引擎
- 词法分析：tokenize
- 语法解析：PLParser → 语句列表
- 表达式：Pratt 解析器 + 求值
- 执行：PLInterpreter（薄壳，真正的语句执行器是
  `Executor._run_pl_statements` —— 过程体与函数体共用同一份实现）

支持的语句形态（过程体与函数体**完全一致**）：DECLARE（变量 / CURSOR）、
SET、IF、WHILE、LOOP、REPEAT、LEAVE / ITERATE（可带标签）、RETURN、
SELECT ... INTO、OPEN / FETCH / CLOSE、SIGNAL、普通 SQL。
"""
import os
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


# 注意：`REPEAT` 必须留在关键字表里 —— 否则 `REPEAT` 会被当成标识符，
# `LOOP ... END REPEAT` 的收尾判定与 `REPEAT` 语句本身都会静默走错。
KEYWORDS = {
    'DECLARE', 'SET', 'IF', 'THEN', 'ELSEIF', 'ELSE', 'END',
    'WHILE', 'DO', 'LOOP', 'REPEAT', 'UNTIL', 'LEAVE', 'ITERATE',
    'BEGIN', 'RETURN', 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
    'INTO', 'FROM', 'WHERE', 'CALL', 'AND', 'OR', 'NOT',
    'NULL', 'TRUE', 'FALSE', 'IS', 'IN', 'LIKE', 'AS',
    'DEFAULT', 'OUT', 'INOUT',
    # 游标语句的起始关键字：必须在关键字表里，否则 `OPEN` / `CURSOR`
    # 会被词法器当成标识符，`OPEN c;` 会退化成一条普通 SQL 语句
    # （静默走错路径）。
    'LOOP', 'OPEN', 'FETCH', 'CLOSE', 'CURSOR',
}

#: LEAVE / ITERATE 的健壮性上限（`Label: LOOP ... END LOOP`）。
#: 真正的"绝不挂死"保护在执行器（`PLContext.max_iterations`）；
#: 这里是记号上限，防止标签栈被异常路径无限增长。
_MAX_LOOP_LABEL_DEPTH = 64

#: `SIGNAL SQLSTATE 'xxxxx' [SET MESSAGE_TEXT = '...', ...]` 的解析
#: 不依赖 token 流（token 流里字符串字面量已经去了引号，且逗号/等号是
#: 独立的 PUNC/OP，用正则读原文更直白且能给出精确报错位置）。
_SIGNAL_RE = re.compile(
    r"^\s*SIGNAL\s+(?:SQLSTATE\s+(?:VALUE\s+)?(?P<state>'[^']*'|\d+))"
    r"(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)

#: `SET MESSAGE_TEXT = '...'` / `SET MYSQL_ERRNO = n` 等 item。
#: 两条边界（都是实测踩出来的）：
#: ① 字符串分支的收尾引号后面要求空白或分隔符（`(?=;|\s|,|$)`）；
#: ② **非字符串分支排除引号**（`[^,;']+`）—— 否则 `'boom';` 会被
#:    非字符串分支整个吃掉（消息文本里多出一个分号），因为正则回溯时
#:    第一分支的 lookahead 可以人为失败。
_SIGNAL_ITEM_RE = re.compile(
    r"([A-Za-z_]\w*)\s*=\s*"
    r"('(?:[^']|'')*'(?=;|\s|,|$)|[^,;']+)\s*(?:,|;)",
    re.DOTALL)


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
        elif ch in '()[],;.:':
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
        self.source = str(text or '')
        self.tokens = tokenize(self.source)
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
        """解析整个程序；**有剩余 token 就显式报语法错误**

        原实现只 `break`（`parse_statement` 返回 None 时），因此
        `END WHILE END WHILE` 这种尾部多余标记会被静默丢弃 —— 那是
        "看起来执行成功"的静默错解。这里改成显式报错。
        """
        stmts = []
        while self.pos < len(self.tokens):
            stmt = self.parse_statement()
            if stmt is None:
                break
            stmts.append(stmt)
        if self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            raise SyntaxError(
                f"PL 程序尾部有多余记号 {t.value!r}（位置 {t.pos}）："
                f"语句块没有正确闭合")
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

        # `label: LOOP|WHILE|REPEAT ...`（MySQL 13.6.5.5「Labels」）
        if t.type in ('IDENT',) and self.peek(1) is not None \
                and self.peek(1).type == 'PUNC' and self.peek(1).value == ':':
            label = t.value
            self.pos += 2
            stmt = self.parse_statement()
            if stmt is None or stmt[0] not in (
                    'WHILE', 'LOOP', 'REPEAT', 'BEGIN', 'IF', 'DECLARE',
                    'SET', 'RETURN', 'SELECT_INTO', 'SQL', 'OPEN', 'FETCH',
                    'CLOSE', 'SIGNAL', 'LEAVE', 'ITERATE'):
                raise SyntaxError(f"标签 {label}: 后面不是可加标签的语句: {stmt}")
            return (stmt[0], label) + tuple(stmt[1:])

        upper = t.value.upper() if t.type in ('KW', 'IDENT') else ''

        if upper == 'DECLARE':
            return self.parse_declare()
        if upper == 'SET':
            return self.parse_set()
        if upper == 'IF':
            return self.parse_if()
        if upper == 'WHILE':
            return self.parse_while()
        if upper == 'LOOP':
            return self.parse_loop()
        if upper == 'REPEAT':
            return self.parse_repeat()
        if upper == 'RETURN':
            return self.parse_return()
        if upper == 'LEAVE':
            return self.parse_leave()
        if upper == 'ITERATE':
            return self.parse_iterate()
        if upper == 'OPEN':
            return self.parse_open()
        if upper == 'FETCH':
            return self.parse_fetch()
        if upper == 'CLOSE':
            return self.parse_close()
        if upper == 'SIGNAL':
            return self.parse_signal()

        # 普通 SQL 语句
        return self.parse_sql_statement()

    # ==================== 变量 / 游标声明 ====================

    def parse_declare(self):
        """`DECLARE name TYPE [DEFAULT expr];` 或
        `DECLARE name CURSOR FOR <select>;`（也容忍显式参数表）

        游标声明必须在变量声明之后、HANDLER 与其它语句之前（MySQL
        13.6.6「Cursors」）。这里只做**结构**判断，顺序约束由执行器校验
        （每条语句都带源码位置，报错能指出来）。
        """
        start_tok = self.peek()
        self.expect_kw('DECLARE')
        name_t = self.next()
        if not name_t or name_t.type != 'IDENT':
            raise SyntaxError("DECLARE 变量名错误")
        name = name_t.value

        # `DECLARE CONTINUE|EXIT|UNDO HANDLER FOR ...`：MySQL 的条件处理器。
        # 本引擎**没有实现 HANDLER**，因此在解析期就显式报错 ——
        # 不能静默当成一个普通变量（那样 `DECLARE CONTINUE HANDLER FOR NOT
        # FOUND SET done = 1;` 会悄悄创建一个叫 CONTINUE 的变量，之后
        # `NOT FOUND` 永远不会生效，是典型的静默错解）。
        nxt = self.peek()
        if nxt is not None and nxt.type == 'IDENT' \
                and nxt.value.upper() in ('CONTINUE', 'EXIT', 'UNDO', 'HANDLER'):
            raise SyntaxError(
                "DECLARE ... HANDLER（条件处理器）暂不支持 —— 请改用"
                "隐式游标条件 `IF NOT FOUND THEN ... END IF;` / "
                "`IF FOUND = 0 THEN ... END IF;`（本引擎显式拒绝，"
                "不会把它静默当成一个普通变量声明）")

        if self.peek() and self.peek().type in ('KW', 'IDENT') \
                and self.peek().value.upper() == 'CURSOR':
            self.pos += 1
            if self.peek() and self.peek().type == 'PUNC' \
                    and self.peek().value == '(':
                # `DECLARE c CURSOR [(参数)] FOR ...` —— 参数列表暂不参与
                # 求值（MySQL 允许声明参数，本引擎不支持 → 下面显式报错）
                raise SyntaxError(
                    f"DECLARE {name} CURSOR: 游标参数列表暂不支持"
                    f"（请改用 `DECLARE {name} CURSOR FOR SELECT ...`）")
            if not (self.match_kw('FOR')):
                t = self.next()
                if not (t and t.type in ('KW', 'IDENT')
                        and t.value.upper() == 'FOR'):
                    raise SyntaxError(
                        f"DECLARE {name} CURSOR 缺少 FOR（MySQL 语法："
                        f"DECLARE name CURSOR FOR SELECT ...），"
                        f"实际读到 {getattr(t, 'value', None)!r}")
            sel_toks = self.consume_until_semicolon()
            if not sel_toks:
                raise SyntaxError(f"DECLARE {name} CURSOR FOR 后面缺 SELECT")
            return ('DECLARE_CURSOR', name, sel_toks,
                    getattr(start_tok, 'pos', 0))

        # 类型：MySQL 语法是 `DECLARE name type [DEFAULT expr]`，**只读
        # 一个类型标识符**（长度写在括号里，如 `VARCHAR(50)`）。
        # 原实现贪婪地吃掉了后面所有 IDENT，于是
        # `DECLARE acc INT DEFAULT 0;` 会把 `DEFAULT` 当成类型的一部分，
        # 位置停在 `0` 上 —— 一个数字 token 让后续解析整段错位
        # （表现为"函数体里有无法识别的语句 Token(IDENT, 'i')"）。
        type_parts = []
        t = self.peek()
        if t is not None and t.type == 'IDENT' \
                and t.value.upper() not in ('DEFAULT', 'CURSOR'):
            type_parts.append(t.value)
            self.pos += 1
            # 跳过括号中的长度（如 VARCHAR(50) / DECIMAL(10,2)）
            if self.peek() and self.peek().type == 'PUNC' \
                    and self.peek().value == '(':
                self.pos += 1
                while self.peek() and not (
                        self.peek().type == 'PUNC'
                        and self.peek().value == ')'):
                    self.pos += 1
                self.match_punc(')')

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
        if not cond_toks:
            raise SyntaxError("WHILE 缺少循环条件")
        self.expect_kw('DO')
        body = self.parse_block(('END',))
        self.expect_kw('END')
        got = self.match_kw('WHILE')
        if not got:
            raise SyntaxError(
                f"WHILE 循环没有用 `END WHILE` 收尾（读到的收尾记号是 "
                f"{getattr(self.peek(), 'value', None)!r}）")
        self.match_punc(';')
        return ('WHILE', cond_toks, body)

    def parse_loop(self):
        """LOOP ... END LOOP;（无条件的循环，靠 LEAVE 退出）"""
        self.expect_kw('LOOP')
        body = self.parse_block(('END',))
        self.expect_kw('END')
        got = self.match_kw('LOOP')
        if not got:
            raise SyntaxError(
                f"LOOP 没有用 `END LOOP` 收尾（读到的收尾记号是 "
                f"{getattr(self.peek(), 'value', None)!r}）")
        self.match_punc(';')
        return ('LOOP', body)

    def parse_repeat(self):
        """REPEAT ... UNTIL cond END REPEAT;（先执行体，再判条件）

        注意顺序：`UNTIL` 条件**先**扫描、再解析体 —— 反过来的话
        `parse_block(('UNTIL',))` 遇到 `UNTIL` 就返回，条件 token 一个都
        没被消费，调用方拿到空列表，随后这些 token 会被当成新语句
        （实测就是"'Token' object is not subscriptable"）。
        """
        self.expect_kw('REPEAT')
        # ① 先解析循环体：`parse_block` 遇到 `UNTIL` 停下来（游标不越界）
        body = self.parse_block(('UNTIL',))
        if not self.match_kw('UNTIL'):
            raise SyntaxError(
                f"REPEAT 循环缺少 UNTIL 条件（读到的记号是 "
                f"{getattr(self.peek(), 'value', None)!r}）")
        # ② 此时游标就在条件起点，从头读条件直到 `END REPEAT`
        cond_toks = self._consume_until_end_repeat()
        self.expect_kw('END')
        got = self.match_kw('REPEAT')
        if not got:
            raise SyntaxError(
                f"REPEAT 循环没有用 `END REPEAT` 收尾（读到的收尾记号是 "
                f"{getattr(self.peek(), 'value', None)!r}）")
        self.match_punc(';')
        return ('REPEAT', body, cond_toks)

    def _consume_until_end_repeat(self):
        """读 `UNTIL` 与 `END REPEAT` 之间的条件 token 并**推进游标**

        游标停在 `END` 上，交给调用方的 `END REPEAT` 收尾判定。
        **必须在 `match_kw('UNTIL')` 之后调用**：此时游标正好在条件起点。
        原实现返回 token 却不推进，`parse_block` 于是从 `UNTIL` 处继续，
        条件被当成新语句（实测报"REPEAT 循环缺少 UNTIL 条件"）。
        """
        start = self.pos
        while self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.type == 'KW' and t.value.upper() == 'END' \
                    and self.pos + 1 < len(self.tokens) \
                    and self.tokens[self.pos + 1].type == 'KW' \
                    and self.tokens[self.pos + 1].value.upper() == 'REPEAT':
                toks = self.tokens[start:self.pos]
                if not toks:
                    raise SyntaxError("REPEAT ... UNTIL 缺少条件表达式")
                return toks
            self.pos += 1
        raise SyntaxError("REPEAT 循环没有用 `END REPEAT` 收尾")

    def parse_open(self):
        self.expect_kw('OPEN')
        t = self.next()
        if not t or t.type != 'IDENT':
            raise SyntaxError("OPEN 缺少游标名")
        self.match_punc(';')
        return ('OPEN', t.value)

    def parse_fetch(self):
        self.expect_kw('FETCH')
        c = self.next()
        if not c or c.type != 'IDENT':
            raise SyntaxError("FETCH 缺少游标名")
        if not self.match_kw('INTO'):
            t = self.next()
            if not (t and t.type in ('KW', 'IDENT')
                    and t.value.upper() == 'INTO'):
                raise SyntaxError(
                    f"FETCH {c.value} 缺少 INTO（MySQL 语法："
                    f"FETCH cur INTO var1, var2）")
        var_toks = self.consume_until_semicolon()
        names = self._target_names(var_toks)
        if not names:
            raise SyntaxError(f"FETCH {c.value} INTO 后面缺少变量名")
        return ('FETCH', c.value, names)

    def parse_close(self):
        self.expect_kw('CLOSE')
        t = self.next()
        if not t or t.type != 'IDENT':
            raise SyntaxError("CLOSE 缺少游标名")
        self.match_punc(';')
        return ('CLOSE', t.value)

    def parse_signal(self):
        """`SIGNAL SQLSTATE 'xxxxx' [SET MESSAGE_TEXT = '...', ...]`

        读**源码原文**而不是 token 流：token 流里的字符串字面量已经去掉
        引号，且 `SQLSTATE` / `MESSAGE_TEXT` 会落到 IDENT，用原文正则
        解析更直白，报错也能给出原文片段。
        """
        start = self.peek()
        if start is None:
            raise SyntaxError("SIGNAL 语句意外结束")
        text = self.source[start.pos:]
        m = _SIGNAL_RE.match(text)
        if not m:
            raise SyntaxError(
                "SIGNAL 语法错误（MySQL 13.6.7.7：`SIGNAL SQLSTATE "
                "'xxxxx' [SET MESSAGE_TEXT = '...']`）")
        raw_state = m.group('state')
        state = raw_state.strip("'")
        if len(state) != 5:
            raise SyntaxError(
                f"SIGNAL SQLSTATE 必须是 5 个字符（MySQL 语义），"
                f"实际是 {state!r}（{len(state)} 字符）")
        msg = None
        items = {}
        rest = m.group('rest') or ''
        if rest.strip():
            rm = re.match(r'^\s*SET\s+(.*)$', rest, re.IGNORECASE | re.DOTALL)
            if not rm:
                raise SyntaxError(
                    f"SIGNAL SQLSTATE {raw_state} 后面只支持 "
                    f"`SET 条件信息项 = 值`：{rest.strip()[:60]!r}")
            for im in _SIGNAL_ITEM_RE.finditer(rm.group(1)):
                key = im.group(1).upper()
                val = im.group(2).strip()
                if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                    val = val[1:-1].replace("''", "'")
                items[key] = val
            if not items:
                raise SyntaxError(
                    f"SIGNAL 的 SET 子句无法解析: {rm.group(1).strip()[:60]!r}")
            msg = items.get('MESSAGE_TEXT')
        # 把 token 游标推到 SIGNAL 语句之后的下一个分号
        # （`consume_until_semicolon` 返回的是 SIGNAL 尾部到分号之间的
        # token —— 可能是空列表，也可能是 SET 子句的残余；这里只关心
        # 它把游标推进过了分号）。
        self.consume_until_semicolon()
        return ('SIGNAL', state, msg, items, getattr(start, 'pos', 0))

    @staticmethod
    def _target_names(toks):
        """从 `v1, v2` / `v1.v2` 的 token 序列里取出变量名"""
        names = []
        skip_next = False
        for t in toks:
            if skip_next:
                skip_next = False
                continue
            if t.type == 'IDENT':
                names.append(t.value)
            elif t.type == 'PUNC' and t.value == '.':
                skip_next = True
        return names

    def parse_return(self):
        """RETURN expr;"""
        self.expect_kw('RETURN')
        expr_toks = self.consume_until_semicolon()
        return ('RETURN', expr_toks)

    def parse_leave(self):
        """`LEAVE [label]` —— 标签可省略（省略时作用于最内层循环）"""
        self.expect_kw('LEAVE')
        t = self.peek()
        if t is None or (t.type == 'PUNC' and t.value == ';'):
            self.match_punc(';')
            return ('LEAVE', '')
        t = self.next()
        label = t.value if t else ''
        if label == ';':
            # `LEAVE ;`（省略标签）—— 分号已经被吃掉，直接返回
            return ('LEAVE', '')
        self.match_punc(';')
        return ('LEAVE', label)

    def parse_iterate(self):
        """`ITERATE [label]` —— 标签可省略（省略时作用于最内层循环）"""
        self.expect_kw('ITERATE')
        t = self.peek()
        if t is None or (t.type == 'PUNC' and t.value == ';'):
            self.match_punc(';')
            return ('ITERATE', '')
        t = self.next()
        label = t.value if t else ''
        if label == ';':
            return ('ITERATE', '')
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


def eval_expr(node, ctx, funcs=None):
    """求值表达式

    funcs：**存储函数解析回调**（可选，函数体模式用）。契约：
    `funcs.has_function(名字) -> bool` 判断是不是已注册的存储函数；
    `funcs(名字, [实参值]) -> 返回值` 真正调用，出错就**抛异常**
    （绝不返回"看起来像值"的替代对象 —— 那会静默算出错误结果）。
    名字不是存储函数时回落到 `call_builtin`（未知函数在那里显式报错）。
    """
    op = node[0]

    if op == 'lit':
        return node[1]

    if op == 'var':
        name = node[1]
        # 检查变量
        if name in ctx.variables:
            return ctx.variables[name]
        # 用户变量 `@name`：调用方（CALL 的 OUT/INOUT 回填）把值存在
        # 会话的 user_vars 里，过程体内读 `@name` 要能拿到。
        if name.startswith('@'):
            store = None
            sess = getattr(ctx, 'session', None)
            if isinstance(sess, dict):
                store = sess.get('user_vars')
            if isinstance(store, dict):
                key = name.lstrip('@')
                if key in store:
                    return store[key]
            try:
                from .variables import get_vm
                sid = (sess or {}).get('session_id') \
                    if isinstance(sess, dict) else None
                if sid is not None:
                    return get_vm().get_session(int(sid)).get_user(
                        name.lstrip('@'))
            except Exception:                      # pragma: no cover
                pass
            return None
        # MySQL 的隐式游标条件（13.6.6「Cursors」）：
        # `FOUND`（最近一次 FETCH 是否取到行）、`NOT_FOUND`、`SQLSTATE`、
        # `ROW_COUNT` 是只读的**条件伪变量**，不是普通变量。
        # 放在"变量查不到"之后判定，因此用户显式 DECLARE 同名变量时
        # 优先用用户的变量（与 MySQL 的命名解析一致）。
        _upper = name.upper()
        if _upper in ('FOUND', 'NOT_FOUND', 'SQLSTATE', 'ROW_COUNT'):
            nf = bool(getattr(ctx, 'not_found', False))
            if _upper == 'FOUND':
                return 0 if nf else 1
            if _upper == 'NOT_FOUND':
                return 1 if nf else 0
            if _upper == 'SQLSTATE':
                return '02000' if nf else '00000'
            return int(getattr(ctx, 'row_count', 0) or 0)
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
        a = eval_expr(node[2], ctx, funcs)
        b = eval_expr(node[3], ctx, funcs)
        o = node[1]
        if a is None or b is None:
            return None
        if o == '+': return a + b
        if o == '-': return a - b
        if o == '*': return a * b
        if o == '/': return a / b if b != 0 else None
        if o == '%': return a % b if b != 0 else None

    if op == 'neg':
        return -eval_expr(node[1], ctx, funcs)

    if op == 'and':
        return bool(eval_expr(node[1], ctx, funcs)) \
            and bool(eval_expr(node[2], ctx, funcs))
    if op == 'or':
        return bool(eval_expr(node[1], ctx, funcs)) \
            or bool(eval_expr(node[2], ctx, funcs))
    if op == 'not':
        return not bool(eval_expr(node[1], ctx, funcs))

    if op == 'cmp':
        a = eval_expr(node[2], ctx, funcs)
        b = eval_expr(node[3], ctx, funcs)
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
        return eval_expr(node[1], ctx, funcs) is None
    if op == 'notnull':
        return eval_expr(node[1], ctx, funcs) is not None

    if op == 'like':
        a = eval_expr(node[1], ctx, funcs)
        pat = eval_expr(node[2], ctx, funcs)
        if a is None or pat is None:
            return False
        pat = str(pat).replace('%', '.*').replace('_', '.')
        return bool(re.match('^' + pat + '$', str(a)))

    if op == 'in':
        a = eval_expr(node[1], ctx, funcs)
        return a in [eval_expr(n, ctx, funcs) for n in node[2]]

    if op == 'func':
        args = [eval_expr(a, ctx, funcs) for a in node[2]]
        if funcs is not None:
            has = getattr(funcs, 'has_function', None)
            if has is not None and has(node[1]):
                return funcs(node[1], args)
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

#: 迭代上限默认值（**绝不无限循环**）：每执行一条 PL 语句、或求值一次
#: 循环条件，都消耗 1 次配额；超过即显式报错并终止。100 万次是"正常
#: 存储程序很难碰到、而失控循环会立刻触发"的量级。
DEFAULT_MAX_ITERATIONS = 1000000


class PLCursor:
    """存储程序里的游标（`DECLARE c CURSOR FOR SELECT ...`）

    MySQL 的游标是**只读、不可滚动、敏感/不敏感由实现决定**的；本引擎
    按「OPEN 时一次性物化全部行、`FETCH` 顺序前进」实现 —— 语义上等价于
    MySQL 文档 13.6.6 里 asensitive 游标允许的实现自由，且能保证
    `FETCH` 的顺序性与"到底"（NOT FOUND）判定是确定的。
    """

    __slots__ = ('name', 'select_toks', 'rows', 'columns', 'pos', 'is_open',
                 'declared_at')

    def __init__(self, name, select_toks, declared_at=0):
        self.name = name
        self.select_toks = select_toks
        self.rows = []
        self.columns = []
        self.pos = -1
        self.is_open = False
        self.declared_at = declared_at


class PLContext:
    """PL 执行上下文（过程体与函数体**共用**同一个类）

    * `variables`：DECLARE 的局部变量 + 参数（含 OUT/INOUT）+ 用户变量；
    * `cursors`：本上下文声明的游标（每次调用一份，互不串扰）；
    * `loop_stack`：`[{'label':…, 'kind':…}]`，用于 LEAVE / ITERATE 的
      **动态作用域**查找（MySQL：`LEAVE label` 从内层往外找最近的同名标签）；
    * `max_iterations` / `iterations`：绝不能挂死的硬保护。
    """

    def __init__(self, executor, session, db):
        self.executor = executor
        self.session = session
        self.db = db
        self.variables = {}       # 变量名 -> 值
        self.new_row = None       # 触发器 NEW（dict）
        self.old_row = None       # 触发器 OLD（dict）
        self.return_value = None
        # 是否真的执行到过 RETURN（存储函数必须有返回值：执行器在
        # `_exec_function_body` 里据此显式报错，而不是静默返回 NULL）
        self.return_hit = False
        self.out_params = {}      # OUT 参数回填
        self.leave_flag = None    # LEAVE 标记（标签名；空串=最内层循环）
        self.iterate_flag = None  # ITERATE 标记
        self.iterations = 0
        self.loop_stack = []
        self.cursors = {}
        self.cursor_order = []
        self.declared_vars = set()
        # 最近一次 FETCH 是否"没有取到行"（NOT FOUND 条件，SQLSTATE 02000）
        self.not_found = False
        self.function_name = None
        # 迭代上限：默认 100 万。覆盖顺序（后者优先）：
        # ① 环境变量 `LIGHTDB_PL_MAX_ITERATIONS`；② 执行器属性
        # `Executor._pl_max_iterations`（测试/运维调小用，避免真的跑满
        # 100 万次）；③ 直接给 `ctx.max_iterations` 赋值。
        self.max_iterations = DEFAULT_MAX_ITERATIONS
        override = os.environ.get('LIGHTDB_PL_MAX_ITERATIONS')
        if not override and executor is not None:
            override = getattr(executor, '_pl_max_iterations', None)
        if override:
            try:
                self.max_iterations = max(1, int(override))
            except (TypeError, ValueError):
                self.max_iterations = DEFAULT_MAX_ITERATIONS


class PLInterpreter:
    """PL 语句序列的执行入口

    **真正的语句执行器只有一份**：`Executor._run_pl_statements`（过程体
    与函数体共用）。本类保留 `run(stmts)` / `_exec_stmt(stmt)` 这两个
    原入口（`stored.call_procedure` 等历史调用点在用），全部转发过去，
    避免"两个解释器语义不一致"这类静默分歧。
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def run(self, stmts):
        """执行语句列表"""
        ex = self.ctx.executor
        from .pl import ExprParser as _EP, eval_expr as _EV
        return ex._pl_run_block(stmts, self.ctx, _EP, _EV)

    def _exec_stmt(self, stmt):
        ex = self.ctx.executor
        from .pl import ExprParser as _EP, eval_expr as _EV
        return ex._pl_run_block([stmt], self.ctx, _EP, _EV)


# 兼容旧名字（有的调用点/文档引用 pl.PLInterpreter 的同义写法）
PLRunner = PLInterpreter