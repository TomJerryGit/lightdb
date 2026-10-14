"""
SQL 解析器 - 正则驱动（修复版）

修复:
- 移除重复的 Parser 类定义
- 移除重复的 _column_def（保留完整版）
- 移除重复的 _select（保留完整版）
"""
import os
import re
from typing import Optional, List, Dict, Any

# `FROM (子查询)` 之后的词若不是别名就一定是这些子句关键字，
# 用于避免把 `FROM (SELECT ...) WHERE ...` 里的 WHERE 误当别名
_FROM_SUBQUERY_KEYWORDS = {
    'WHERE', 'GROUP', 'ORDER', 'LIMIT', 'HAVING', 'JOIN', 'INNER',
    'LEFT', 'RIGHT', 'CROSS', 'OUTER', 'FULL', 'UNION', 'ON', 'AS',
}

# ==================== JSON 路径运算符（`col->'$.k'` / `col->>'$.k'`） ====================
#
# MySQL 的 `->` / `->>` 不是普通运算符，而是「按路径取 JSON 值」的语法糖:
#     col->'$.k'   ≡ JSON_EXTRACT(col, '$.k')     （结果仍是 JSON 值）
#     col->>'$.k'  ≡ JSON_UNQUOTE(JSON_EXTRACT(col, '$.k'))  （结果是 TEXT）
# 本引擎的标量表达式求值器（parse_scalar_expr / eval_expr_node）与
# `JSON_EXTRACT` / `JSON_UNQUOTE` 两个标量函数是已经成型的通用机制，所以这里
# 在**解析期**把 `->` 语法糖改写成等价函数调用：投影、WHERE/HAVING、JOIN ON、
# GROUP BY / ORDER BY 各处一律复用同一条求值路径，不需要在每个执行分支里
# 单独加注解。
#
# 只支持 `列 -> '字符串字面量路径'` 形态：左侧不是列引用（如 `f(x)->'$.a'`）
# 或右侧不是字符串字面量（如 `col->$.a`、`col->?`）都是**显式 SyntaxError**，
# 绝不静默当成别的含义。
_JSON_PATH_OP_RE = re.compile(
    r"""(?P<base>[`"]?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?[`"]?)"""
    r"""\s*(?P<op>->>|->)\s*'(?P<path>(?:[^']|'')*)'""")


def _json_op_repl(m: 're.Match') -> str:
    base = m.group('base')
    path = m.group('path').replace("''", "'")
    if not path.startswith('$'):
        raise SyntaxError(
            f"JSON 路径必须以 $ 开头（MySQL 语法）: "
            f"{m.group('base')}{m.group('op')}'{path}'")
    expr = f"JSON_EXTRACT({base}, '{path}')"
    if m.group('op') == '->>':
        return f"JSON_UNQUOTE({expr})"
    return expr


def rewrite_json_paths(text):
    """把 `col->'$.k'` / `col->>'$.k'` 改写成 JSON_EXTRACT / JSON_UNQUOTE

    幂等（改写结果里不再有 `->`），链式写法 `a->'$.x'->'$.y'` 逐轮收敛。
    残留的、不符合上述形态的 `->`（引号外的）→ 显式 SyntaxError。
    """
    s = str(text) if text is not None else text
    if not isinstance(s, str) or '->' not in s:
        return s
    for _ in range(8):
        new = _JSON_PATH_OP_RE.sub(_json_op_repl, s)
        if new == s:
            break
        s = new
    for i, _ch, quoted in Parser._scan_quotes(s):
        if quoted:
            continue
        if s.startswith('->', i):
            raise SyntaxError(
                f"不支持的 JSON 路径写法: {str(text).strip()} —— "
                f"本引擎只支持 `列 -> '$.路径'` / `列 ->> '$.路径'`"
                f"（左侧必须是列引用，右侧必须是字符串字面量路径）")
    return s


class Parser:
    """SQL 解析器"""

    def parse(self, sql: str) -> Optional[Dict]:
        """解析入口：在结果 AST 上附加原始语句文本（`source_sql`）

        `source_sql` 只用于审计 / 诊断（`SHOW AUDIT LOG` 需要真实语句文本，
        而 pymysql 入口 `net/server.py` 只把 AST 交给执行器，拿不到原文），
        不参与任何语义判断。
        """
        sql = sql.strip().rstrip(';').strip()
        if not sql:
            return None
        parsed = self._parse_inner(sql)
        if isinstance(parsed, dict) and 'source_sql' not in parsed:
            parsed = dict(parsed)
            parsed['source_sql'] = sql
        return parsed

    def _parse_inner(self, sql: str) -> Optional[Dict]:
        upper = sql.upper()

        # WITH [RECURSIVE] name AS (subquery), ... <主体语句>
        # 必须在 UNION 拆分之前处理：CTE 前缀属于整条语句（包括 UNION）
        if upper == 'WITH' or upper.startswith('WITH '):
            ctes, rest = self._extract_ctes(sql)
            if ctes:
                body = self.parse(rest)
                if body is None:
                    raise SyntaxError("WITH 之后缺少可解析的语句")
                body = dict(body)
                body['ctes'] = ctes
                return body

        # UNION
        union_parts = self._split_union(sql)
        if union_parts:
            return self._parse_union(union_parts)

        if upper.startswith('EXPLAIN'):
            rest = sql[len('EXPLAIN'):].strip()
            analyze = False
            if re.match(r'ANALYZE\b', rest, re.IGNORECASE):
                # MySQL 的 `EXPLAIN ANALYZE <stmt>`：先执行再给出真实耗时。
                # 原实现把 'ANALYZE SELECT ...' 交给 ANALYZE 分支，`(\w+)`
                # 吃掉了 'SELECT' 当表名 → 计划对象为 None（报
                # "'NoneType' object has no attribute 'explain_row'"）。
                analyze = True
                rest = rest[len('ANALYZE'):].strip()
            inner = self.parse(rest)
            if inner is None:
                raise SyntaxError("EXPLAIN 需要跟随一条 SQL")
            return {'command': 'EXPLAIN', 'inner': inner,
                    'analyze': analyze}

        if upper.startswith('ANALYZE'):
            m = re.match(
                r'ANALYZE\s+(?:TABLE\s+)?[`"]?(\w+)[`"]?',
                sql, re.IGNORECASE)
            if m:
                return {'command': 'ANALYZE', 'table': m.group(1)}

        if upper.startswith('CREATE TABLE'):
            return self._create_table(sql)
        if upper.startswith('DROP TABLE'):
            return self._drop_table(sql)
        if upper.startswith('INSERT'):
            return self._insert(sql)
        if upper.startswith('SELECT'):
            return self._select(sql)
        if upper.startswith('UPDATE'):
            return self._update(sql)
        if upper.startswith('DELETE'):
            return self._delete(sql)

        if upper.startswith('SHOW TABLES'):
            return {'command': 'SHOW TABLES'}
        if upper.startswith('SHOW DATABASES'):
            return {'command': 'SHOW DATABASES'}
        if upper == 'SHOW STATS':
            return {'command': 'SHOW STATS'}
        if (upper.startswith('SHOW COLUMNS') or
                upper.startswith('DESCRIBE ') or
                upper.startswith('DESC ')):
            return self._show_columns(sql)

        # ---------- MySQL 兼容的运维 / 复制 / 安全语句 ----------
        stmt = self._parse_ops_stmt(sql, upper)
        if stmt is not None:
            return stmt

        if upper in ('BEGIN', 'START TRANSACTION'):
            return {'command': 'BEGIN'}
        if upper == 'COMMIT':
            return {'command': 'COMMIT'}
        if upper == 'ROLLBACK':
            return {'command': 'ROLLBACK'}

        m = re.match(
            r'USE\s+[`"]?(\w+)[`"]?', sql, re.IGNORECASE)
        if m:
            return {'command': 'USE', 'database': m.group(1)}

        return None

    # ============ 运维 / 复制 / 安全语句（MySQL 兼容面） ============

    def _show_result(self, show_stmt: str, **extra) -> Dict:
        """构造「以结果集回传的 SHOW/CHECK 类语句」

        为什么 `command` 是 `SELECT`：`net/server.py::_send_execution_result`
        只在 `cmd == 'SELECT'`（或返回结果集的 `EXPLAIN`）时才把执行结果序列化
        成 MySQL 结果集，其它命令名一律回 OK 包 —— 那样客户端 `fetchall()`
        什么都拿不到。`net/**` 不在本次可改范围内，因此这里用
        `command='SELECT'` + `show_stmt` 标记，执行器在 `_cmd_select` 最前面
        按标记分流（见 `executor._exec_show_stmt`）。返回的 AST 补齐了普通
        SELECT 的全部键，避免下游按 SELECT 结构访问时缺键。
        """
        d = {
            'command': 'SELECT', 'table': None, 'table_alias': None,
            'subquery': None, 'no_from': True, 'joins': [],
            'columns': ['*'], 'windows': [], 'projection': [],
            'distinct': False, 'where': None, 'group_by': None,
            'having': None, 'order_by': None, 'limit': None,
            'offset': None, 'show_stmt': show_stmt,
        }
        d.update(extra)
        return d

    def _parse_ops_stmt(self, sql: str, upper: str):
        """解析运维/复制/安全语句；不认识则返回 None（交回兜底 1064）"""
        # ---- 复制状态 ----
        if upper in ('SHOW MASTER STATUS', 'SHOW BINARY LOG STATUS'):
            return self._show_result('MASTER_STATUS')
        if upper in ('SHOW SLAVE STATUS', 'SHOW REPLICA STATUS'):
            return self._show_result('SLAVE_STATUS')
        if upper.startswith('SHOW BINLOG EVENTS'):
            return self._show_result('BINLOG_EVENTS')

        # ---- 存储引擎状态 ----
        em = re.match(
            r'SHOW\s+ENGINE\s+([`"]?\w+[`"]?)'
            r'(?:\s+(STATUS|MUTEX))?\s*$', sql, re.IGNORECASE)
        if em:
            return self._show_result(
                'ENGINE',
                engine=em.group(1).strip('`"').upper(),
                engine_kind=(em.group(2) or 'STATUS').upper())

        # ---- 审计日志 ----
        am = re.match(r'SHOW\s+AUDIT\s+LOG(?:\s+LIMIT\s+(\d+))?\s*$',
                      sql, re.IGNORECASE)
        if am:
            return self._show_result(
                'AUDIT_LOG',
                limit=int(am.group(1)) if am.group(1) else None)

        # ---- 表检查 / 优化（可带多个表，MySQL 语法） ----
        for kw, tag in (('CHECK', 'CHECK_TABLE'),
                        ('OPTIMIZE', 'OPTIMIZE_TABLE')):
            tm = re.match(
                rf'{kw}\s+TABLE\s+(.+?)\s*$', sql, re.IGNORECASE | re.DOTALL)
            if tm:
                names = [
                    t.strip().strip('`"')
                    for t in self._split_top_level(tm.group(1))
                    if t.strip()
                ]
                # 去掉 FOR UPGRADE / QUICK / FAST / MEDIUM / EXTENDED 等选项
                names = [n for n in names
                         if n.upper() not in (
                             'FOR', 'UPGRADE', 'QUICK', 'FAST', 'MEDIUM',
                             'EXTENDED', 'CHANGED', 'LOCAL', 'NO_WRITE_TO_BINLOG')]
                if not names:
                    raise SyntaxError(f"{kw} TABLE 缺少表名: {sql}")
                return self._show_result(tag, tables=names)

        # ---- 分区 ----
        pm = re.match(r'SHOW\s+PARTITIONS\s+FROM\s+[`"]?(\w+)[`"]?\s*$',
                      sql, re.IGNORECASE)
        if pm:
            return self._show_result('PARTITIONS', table=pm.group(1))

        # ---- 安全面 ----
        if upper in ('SHOW IP WHITELIST', 'SHOW IP WHITE LIST'):
            return self._show_result('IP_WHITELIST')
        wm = re.match(r'(ADD|REMOVE|DELETE)\s+IP\s+WHITELIST\s+(\S+)\s*$',
                      sql, re.IGNORECASE)
        if wm:
            return {
                'command': f'{wm.group(1).upper()} IP WHITELIST',
                'entry': wm.group(2).strip().strip("'\""),
            }
        if upper in ('SHOW SECURITY', 'SHOW SECURITY STATUS'):
            return self._show_result('SECURITY')

        # ---- 用户 / 口令策略（DCL） ----
        if re.match(r'CREATE\s+USER\b', sql, re.IGNORECASE):
            return self._create_user(sql)
        dm = re.match(r'DROP\s+USER\s+(?:IF\s+EXISTS\s+)?(.+?)\s*$',
                      sql, re.IGNORECASE | re.DOTALL)
        if dm:
            return {
                'command': 'DROP USER',
                'users': [self._parse_account(a)
                          for a in self._split_top_level(dm.group(1))
                          if a.strip()],
            }
        sm = re.match(r'SET\s+PASSWORD\s+POLICY\s+(.+?)\s*$',
                      sql, re.IGNORECASE | re.DOTALL)
        if sm:
            opts = {}
            for item in self._split_top_level(sm.group(1)):
                item = item.strip()
                if not item:
                    continue
                kv = re.match(r'(\w+)\s*=\s*(.+?)\s*$', item)
                if not kv:
                    raise SyntaxError(f"SET PASSWORD POLICY 选项错误: {item}")
                opts[kv.group(1).lower()] = kv.group(2).strip().strip("'\"")
            return {'command': 'SET PASSWORD POLICY', 'options': opts}

        # ---- 存储过程 / 触发器 / 全文索引 ----
        if re.match(r'CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\b',
                    sql, re.IGNORECASE):
            return self._create_procedure(sql)
        cm = re.match(r'CALL\s+[`"]?(\w+)[`"]?\s*\((.*)\)\s*$',
                      sql, re.IGNORECASE | re.DOTALL)
        if cm:
            args = [a.strip() for a in self._split_top_level(cm.group(2))]
            return {'command': 'CALL', 'name': cm.group(1),
                    'args': [a for a in args if a]}
        if re.match(r'CREATE\s+TRIGGER\b', sql, re.IGNORECASE):
            return self._create_trigger(sql)
        if re.match(r'CREATE\s+FULLTEXT\s+INDEX\b', sql, re.IGNORECASE):
            return self._create_fulltext_index(sql)
        return None

    def _parse_account(self, text: str) -> Dict:
        """`'user'@'host'` → {'user','host'}（省略 host 按 '%'）"""
        t = text.strip()
        m = re.match(
            r"""^(?:'([^']*)'|"([^"]*)"|([A-Za-z_]\w*))"""
            r"""(?:\s*@\s*(?:'([^']*)'|"([^"]*)"|([A-Za-z_%.\w]*)))?""",
            t)
        if not m:
            raise SyntaxError(f"账号写法错误: {text}")
        user = m.group(1) if m.group(1) is not None else (
            m.group(2) if m.group(2) is not None else m.group(3))
        host = m.group(4) if m.group(4) is not None else (
            m.group(5) if m.group(5) is not None else m.group(6))
        return {'user': user, 'host': host or '%'}

    def _create_user(self, sql: str) -> Dict:
        m = re.match(r'CREATE\s+USER\s+(IF\s+NOT\s+EXISTS\s+)?(.+?)\s*$',
                     sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("CREATE USER 语法错误")
        if_not_exists = bool(m.group(1))
        accounts = []
        for part in self._split_top_level(m.group(2)):
            part = part.strip()
            if not part:
                continue
            # `IDENTIFIED [WITH plugin] BY 'pwd'`
            pm = re.search(
                r"""IDENTIFIED\s+(?:WITH\s+\w+\s+)?BY\s+"""
                r"""('(?:[^']|'')*'|NULL)""", part, re.IGNORECASE)
            password = None
            has_ident = bool(pm)
            if pm:
                raw = pm.group(1)
                password = (None if raw.upper() == 'NULL'
                            else raw[1:-1].replace("''", "'"))
                part = part[:pm.start()].strip()
            elif re.search(r'IDENTIFIED\s+WITH\b', part, re.IGNORECASE):
                raise SyntaxError(
                    f"CREATE USER 只支持 IDENTIFIED BY '密码' / "
                    f"IDENTIFIED WITH <plugin> BY '密码': {sql}")
            acct = self._parse_account(part)
            acct['password'] = password
            acct['has_ident'] = has_ident
            accounts.append(acct)
        if not accounts:
            raise SyntaxError(f"CREATE USER 缺少账号: {sql}")
        return {'command': 'CREATE USER', 'accounts': accounts,
                'if_not_exists': if_not_exists}

    def _create_procedure(self, sql: str) -> Dict:
        m = re.match(
            r'CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+'
            r'[`"]?(\w+)[`"]?\s*\(', sql, re.IGNORECASE)
        if not m:
            raise SyntaxError("CREATE PROCEDURE 语法错误")
        open_idx = sql.index('(', m.end() - 1)
        end = self._matching_paren(sql, open_idx)
        params = self._split_top_level(sql[open_idx + 1:end])
        body = sql[end + 1:].strip()
        body = re.sub(r'^(?:COMMENT\s+\'(?:[^\']|\'\')*\'\s*)+', '',
                      body, flags=re.IGNORECASE).strip()
        if not body:
            raise SyntaxError("CREATE PROCEDURE 缺少过程体")
        return {'command': 'CREATE PROCEDURE', 'name': m.group(1),
                'params': [p.strip() for p in params if p.strip()],
                'body': body}

    def _create_trigger(self, sql: str) -> Dict:
        m = re.match(
            r'CREATE\s+TRIGGER\s+[`"]?(\w+)[`"]?\s+'
            r'(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+'
            r'[`"]?(\w+)[`"]?\s+FOR\s+EACH\s+ROW\s+(.+)$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError(
                f"CREATE TRIGGER 语法错误（需要 "
                f"CREATE TRIGGER 名 BEFORE|AFTER INSERT|UPDATE|DELETE ON 表 "
                f"FOR EACH ROW <语句块>）: {sql[:120]}")
        return {'command': 'CREATE TRIGGER', 'name': m.group(1),
                'timing': m.group(2).upper(), 'event': m.group(3).upper(),
                'table': m.group(4), 'body': m.group(5).strip()}

    def _create_fulltext_index(self, sql: str) -> Dict:
        m = re.match(
            r'CREATE\s+FULLTEXT\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'[`"]?(\w+)[`"]?\s+ON\s+[`"]?(\w+)[`"]?\s*\((.*)\)\s*$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError(f"CREATE FULLTEXT INDEX 语法错误: {sql[:120]}")
        cols = [c.strip().strip('`"')
                for c in self._split_top_level(m.group(3))]
        cols = [c for c in cols if c]
        if not cols:
            raise SyntaxError("CREATE FULLTEXT INDEX 缺少列名")
        return {'command': 'CREATE FULLTEXT INDEX', 'name': m.group(1),
                'table': m.group(2), 'columns': cols,
                'if_not_exists': 'IF NOT EXISTS' in sql.upper()}

    # ==================== UNION ====================

    def _split_union(self, sql) -> Optional[list]:
        parts = []
        buf = ''
        depth = 0
        i = 0
        n = len(sql)
        upper = sql.upper()
        while i < n:
            if sql[i] == '(':
                depth += 1
                buf += sql[i]; i += 1
            elif sql[i] == ')':
                depth -= 1
                buf += sql[i]; i += 1
            elif depth == 0 and upper[i:i+9] == 'UNION ALL':
                parts.append((buf.strip(), True))
                buf = ''
                i += 9
            elif depth == 0 and upper[i:i+5] == 'UNION':
                parts.append((buf.strip(), False))
                buf = ''
                i += 5
            else:
                buf += sql[i]; i += 1
        if parts:
            parts.append((buf.strip(), False))
            return parts
        return None

    def _parse_union(self, parts) -> Dict:
        segments = []
        all_flags = []
        for sql, all_flag in parts:
            p = self.parse(sql)
            if p is None:
                raise SyntaxError(f"UNION 子查询解析失败: {sql[:40]}")
            segments.append(p)
            all_flags.append(bool(all_flag))
        # unions_all[i] 表示 segments[i] 与 segments[i+1] 之间是 UNION ALL
        # （原实现把 all_flag 丢掉了，导致 UNION ALL 被当成 UNION 去重）
        return {
            'command': 'UNION',
            'segments': segments,
            'unions_all': all_flags[:max(0, len(segments) - 1)],
        }

    # ==================== CTE ====================

    _CTE_HEAD = re.compile(
        r'[`"]?(\w+)[`"]?\s*(?:\([^)]*\)\s*)?AS\s*\(',
        re.IGNORECASE)

    def _extract_ctes(self, sql: str):
        """解析 `WITH [RECURSIVE] a AS (...), b AS (...) <rest>`

        返回 ([{'name','query'}...], 剩余语句)。递归 CTE 只做到「能解析」，
        执行期不保证正确（见 executor 的说明）。
        """
        m = re.match(r'WITH\s+(RECURSIVE\s+)?', sql, re.IGNORECASE)
        if not m:
            return [], sql
        recursive = bool(m.group(1))
        rest = sql[m.end():].lstrip()

        ctes = []
        while True:
            hm = self._CTE_HEAD.match(rest)
            if not hm:
                break
            name = hm.group(1)
            # 找到与该 '(' 配对的 ')'
            start = hm.end() - 1
            depth = 0
            end = -1
            for j in range(start, len(rest)):
                if rest[j] == '(':
                    depth += 1
                elif rest[j] == ')':
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end < 0:
                raise SyntaxError(f"CTE {name} 括号不匹配")
            inner = rest[start + 1:end].strip()
            rest = rest[end + 1:].lstrip()
            ctes.append({
                'name': name,
                'query': self.parse(inner),
                'recursive': recursive,
            })
            if rest.startswith(','):
                rest = rest[1:].lstrip()
                continue
            break
        return ctes, rest

    # ==================== 窗口函数 ====================

    _WINDOW_RE = re.compile(
        r'^(?P<func>[A-Za-z_]\w*)\s*\(\s*(?P<args>[^)]*)\)\s*'
        r'OVER\s*\(\s*(?P<spec>[^)]*)\)\s*'
        r'(?:AS\s+(?P<alias>[`"]?\w+[`"]?))?$',
        re.IGNORECASE | re.DOTALL)

    def _split_windows(self, raw_columns):
        """把 `FUNC(...) OVER (...)` 从列表达式里抽出来

        返回 (基本列, 窗口列表, 投影顺序)：
        - 基本列：交给现有查询引擎（table.select）处理的表达式
        - 窗口列表：每个元素含 func/args/partition_by/order_by/alias
        - 投影顺序：('col', 表达式) 或 ('win', 窗口字典)，用于拼最终行
        """
        if raw_columns == ['*']:
            return ['*'], [], [('col', '*')]

        base, windows, projection = [], [], []
        for raw in raw_columns:
            text = raw.strip()
            m = self._WINDOW_RE.match(text)
            if not m:
                base.append(raw)
                projection.append(('col', text))
                continue

            spec = (m.group('spec') or '').strip()
            # 帧子句（ROWS ...）必须先摘出来：不然 `ORDER BY x ROWS ...`
            # 会把帧文本当成排序键的一部分，而 `PARTITION BY d ROWS ...`
            # 会把 "d ROWS ..." 当成一个分区列名
            frame, spec = self._parse_window_frame(spec)
            partition, order = [], []
            pm = re.search(
                r'PARTITION\s+BY\s+(.+?)(?=\s+ORDER\s+BY\b|$)',
                spec, re.IGNORECASE | re.DOTALL)
            if pm:
                partition = [c.strip()
                             for c in self._split_top_level(pm.group(1))]
            om = re.search(r'ORDER\s+BY\s+(.+)$', spec,
                           re.IGNORECASE | re.DOTALL)
            if om:
                for item in self._split_top_level(om.group(1)):
                    im = re.match(r'[`"]?([\w.]+)[`"]?(?:\s+(ASC|DESC))?',
                                  item.strip(), re.IGNORECASE)
                    if im:
                        order.append((im.group(1),
                                      (im.group(2) or '').upper() == 'DESC'))

            func = m.group('func').upper()
            alias = (m.group('alias') or '').strip('`"')
            win = {
                'func': func,
                'args': [a.strip() for a in (m.group('args') or '').split(',')
                         if a.strip()],
                'partition_by': partition,
                'order_by': order,
                'alias': alias or f'{func}()',
                # 帧子句（None 表示没写，执行器按默认语义：有 ORDER BY
                # 累计、无 ORDER BY 整分区）
                'frame': frame,
            }
            windows.append(win)
            projection.append(('win', win))
        return base, windows, projection

    # ==================== 窗口帧（ROWS ...） ====================

    # 帧子句：ROWS BETWEEN <bound> AND <bound> 或简写 ROWS <bound>
    _FRAME_RE = re.compile(
        r'\b(?P<type>ROWS|RANGE|GROUPS)\s+'
        r'(?:BETWEEN\s+(?P<start>.+?)\s+AND\s+(?P<end>.+?)'
        r'|(?P<short>.+?))\s*$',
        re.IGNORECASE | re.DOTALL)

    def _parse_window_frame(self, spec: str):
        """从 `OVER (...)` 的 spec 里摘出帧子句

        返回 (frame 或 None, 去掉帧之后的 spec)。frame 形如::

            {'start': ('PRECEDING', 2), 'end': ('CURRENT ROW', None),
             'type': 'ROWS'}

        帧类型支持：
        * ROWS：按物理行取帧（任意边界组合）；
        * RANGE：只支持 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`
          （及简写 `RANGE UNBOUNDED PRECEDING`）——即 SQL 默认帧，按
          **对等行组（peer）** 取帧：CURRENT ROW 表示与当前行 ORDER BY
          值相同的最后一行。RANGE 的其它组合（数值偏移、CURRENT ROW 作
          起始边界等）与 GROUPS 一律明确报错，绝不静默按 ROWS 处理 ——
          引擎没有值域帧语义，静默降级会给出看似正常但错误的边界值。
        """
        if not spec:
            return None, spec
        m = self._FRAME_RE.search(spec)
        if not m:
            # 有帧关键字但不匹配已知语法 -> 明确报错，不要静默忽略
            if re.search(r'\b(BETWEEN|PRECEDING|FOLLOWING|CURRENT\s+ROW)\b',
                         spec, re.IGNORECASE):
                raise SyntaxError(f"窗口帧子句语法错误: {spec}")
            return None, spec

        ftype = m.group('type').upper()
        if ftype == 'GROUPS':
            raise SyntaxError("暂不支持 GROUPS 窗口帧，仅支持 ROWS / RANGE")

        if m.group('start'):
            start_txt, end_txt = m.group('start'), m.group('end')
        else:
            # 简写 `ROWS UNBOUNDED PRECEDING` = UNBOUNDED PRECEDING
            # → CURRENT ROW
            start_txt, end_txt = m.group('short'), 'CURRENT ROW'
        if not start_txt or not end_txt:
            raise SyntaxError(f"窗口帧子句语法错误: {spec}")

        start = self._parse_frame_bound(start_txt)
        end = self._parse_frame_bound(end_txt)
        if start[0] == 'UNBOUNDED FOLLOWING' or end[0] == 'UNBOUNDED PRECEDING':
            raise SyntaxError(f"窗口帧边界顺序不合法: {spec}")
        if self._bound_pos(start) > self._bound_pos(end):
            raise SyntaxError(f"窗口帧起始边界不能晚于结束边界: {spec}")

        if ftype == 'RANGE' and not (
                start[0] == 'UNBOUNDED PRECEDING'
                and end[0] == 'CURRENT ROW'):
            raise SyntaxError(
                f"暂不支持 RANGE 帧 {m.group(0).strip()}："
                f"只支持 RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
                f"（其它 RANGE 组合请显式写成 ROWS）")

        frame = {'start': start, 'end': end, 'type': ftype,
                 'raw': m.group(0).strip()}
        return frame, spec[:m.start()].strip()

    @staticmethod
    def _parse_frame_bound(text: str):
        """解析单个帧边界"""
        t = ' '.join(str(text).upper().split())
        if t in ('UNBOUNDED PRECEDING', 'UNBOUNDED FOLLOWING', 'CURRENT ROW'):
            return (t, None)
        m = re.match(r'^(\d+)\s+(PRECEDING|FOLLOWING)$', t)
        if m:
            return (m.group(2), int(m.group(1)))
        raise SyntaxError(f"窗口帧边界不合法: {text}")

    @staticmethod
    def _bound_pos(bound):
        """把边界换算成可比较的位置权重（用于校验 start <= end）"""
        kind, n = bound
        if kind == 'UNBOUNDED PRECEDING':
            return float('-inf')
        if kind == 'UNBOUNDED FOLLOWING':
            return float('inf')
        if kind == 'CURRENT ROW':
            return 0.0
        if kind == 'PRECEDING':
            return -float(n)
        return float(n)

    # ==================== 表别名 ====================

    # 这些关键字不能当成表别名（否则 `FROM users JOIN ...` 会把 JOIN 吃掉）
    _ALIAS_STOP = {
        'INNER', 'LEFT', 'RIGHT', 'CROSS', 'FULL', 'OUTER', 'JOIN',
        'STRAIGHT_JOIN', 'ON', 'USING', 'WHERE', 'GROUP', 'ORDER',
        'LIMIT', 'HAVING', 'UNION', 'SET', 'VALUES', 'AS',
    }

    def _take_alias(self, rest: str):
        """吃掉可选表别名（`u` 或 `AS u`），返回 (别名或None, 剩余文本)"""
        m = re.match(r'\s+AS\s+[`"]?(\w+)[`"]?', rest, re.IGNORECASE)
        if m:
            return m.group(1), rest[m.end():]
        m = re.match(r'\s+[`"]?(\w+)[`"]?', rest)
        if m and m.group(1).upper() not in self._ALIAS_STOP:
            return m.group(1), rest[m.end():]
        return None, rest

    # ==================== SELECT ====================

    def _matching_paren(self, s: str, open_idx: int) -> int:
        """返回 `s[open_idx] == '('` 对应右括号的下标（引号内括号不计）

        调用前必须已通过 `_check_paren_balance`，因此正常输入一定配对成功；
        仍然在找不到时抛 SyntaxError（防御性）。
        """
        depth = 0
        for i, ch, quoted in self._scan_quotes(s):
            if i < open_idx or quoted:
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
        raise SyntaxError(f"括号不匹配: {s}")

    def _split_select_from(self, sql: str):
        """把 `SELECT ... FROM ...` 从**顶层** FROM 处切开

        返回 (cols_str, from_rest)；没有 FROM 时返回 (cols_str, None)。

        原实现用 `re.match(r'SELECT\\s+(.+?)\\s+FROM\\s+(.+)$')`，非贪婪的
        `(.+?)` 会在**子查询里的第一个 FROM** 处断开：投影列表里带标量子
        查询时（`SELECT u.id, (SELECT COUNT(*) FROM orders o WHERE ...) AS
        cnt FROM users u`）列表达式被截成半截、括号不平衡，直接误报
        SyntaxError。这里改成逐字符找「圆括号深度 0」的 FROM。
        """
        m = re.match(r'SELECT\s+', sql, re.IGNORECASE)
        if not m:
            return None, None
        n = len(sql)
        depth = 0
        i = m.end()
        while i < n:
            ch = sql[i]
            if ch in ("'", '"', '`'):
                j = i + 1
                while j < n and sql[j] != ch:
                    j += 1
                i = j + 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0 and (ch.isalpha() or ch == '_'):
                if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == '_'):
                    i += 1
                    continue
                j = i
                while j < n and (sql[j].isalnum() or sql[j] == '_'):
                    j += 1
                if sql[i:j].upper() == 'FROM':
                    return sql[m.end():i].strip(), sql[j:].strip()
                i = j
                continue
            i += 1
        return sql[m.end():].strip(), None

    def _find_top_level_keyword(self, s: str, kws):
        """在圆括号深度 0 处寻找第一个关键字，返回 (起始下标, 关键字)

        用于切开 WHERE / GROUP BY / HAVING / ORDER BY / LIMIT 这些顶层子句：
        子查询内部的同名子句（`EXISTS (SELECT 1 FROM orders o WHERE ...
        GROUP BY o.user_id HAVING COUNT(*) >= 2)`）位于括号里，深度不为 0，
        这里一律跳过 —— 原先用 `re.search(r'\\bWHERE\\s+(.+?)(?=\\s+GROUP\\b|$)')`
        会在子查询的 GROUP BY 处提前截断，导致括号不平衡而误报 SyntaxError。
        不在字符串字面量里的关键字才算（`dept = 'GROUP BY'` 不算）。
        """
        want = tuple(k.upper() for k in kws)
        n = len(s)
        depth = 0
        i = 0
        while i < n:
            ch = s[i]
            if ch in ("'", '"', '`'):
                j = i + 1
                while j < n and s[j] != ch:
                    j += 1
                i = j + 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0 and (ch.isalpha() or ch == '_'):
                if i > 0 and (s[i - 1].isalnum() or s[i - 1] == '_'):
                    i += 1
                    continue
                j = i
                while j < n and (s[j].isalnum() or s[j] == '_'):
                    j += 1
                word = s[i:j].upper()
                if word in want:
                    if word in ('GROUP', 'ORDER'):
                        # 只认 `GROUP BY` / `ORDER BY` 两词形式
                        if re.match(r'\s+BY\b', s[j:], re.IGNORECASE):
                            return i, word
                    else:
                        return i, word
                i = j
                continue
            i += 1
        return None

    def _extract_clause(self, s: str, kw: str, kws):
        """取顶层子句 `<kw> ...` 的正文（到下一个顶层子句为止）

        返回 None 表示文本里没有这个顶层子句。括号深度感知，因此
        `WHERE ... (SELECT ... GROUP BY ...)` 不会被内层 GROUP BY 截断。
        """
        head = self._find_top_level_keyword(s, (kw,))
        if head is None:
            return None
        start = head[0] + len(kw)
        # `GROUP BY` / `ORDER BY` 要把 BY 一起吃掉
        if kw.upper() in ('GROUP', 'ORDER'):
            m = re.match(r'\s+BY\b', s[start:], re.IGNORECASE)
            if not m:
                return None
            start += m.end()
        nxt = self._find_top_level_keyword(s[start:], kws)
        if nxt is None:
            return s[start:].strip()
        return s[start:start + nxt[0]].strip()

    def _select(self, sql: str) -> Dict:
        no_from = False
        cols_str, from_rest = self._split_select_from(sql)
        if cols_str is None:
            raise SyntaxError("SELECT 语法错误")
        if from_rest is None:
            # FROM-less SELECT：递归 CTE 的 base 分支常见写法
            # （`WITH RECURSIVE n AS (SELECT 1 AS n UNION ALL ...)`），
            # 原先直接 SyntaxError，导致整个递归 CTE 用不了
            no_from = True

        # DISTINCT
        distinct = False
        if cols_str.upper().startswith('DISTINCT'):
            distinct = True
            cols_str = cols_str[len('DISTINCT'):].strip()

        # FROM：子查询 or 主表
        subquery = None
        main_table = None
        main_alias = None

        if no_from:
            rest = ''
        elif from_rest.startswith('('):
            depth = 0
            end = -1
            for i, ch in enumerate(from_rest):
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end < 0:
                raise SyntaxError("子查询括号不匹配")
            inner_sql = from_rest[1:end].strip()
            tail = from_rest[end+1:].strip()
            am = re.match(r'(?:AS\s+)?(\w+)', tail, re.IGNORECASE)
            alias = 'sub'
            # `FROM (SELECT ...) WHERE ...`（省略别名）时不能把 WHERE
            # 当成别名，故挡掉 SQL 关键字
            if am and am.group(1).upper() not in _FROM_SUBQUERY_KEYWORDS:
                alias = am.group(1)
                tail = tail[am.end():]
            subquery = {
                'sql': inner_sql, 'alias': alias, 'rest': tail,
            }
            # 关键：别名之后的 WHERE / GROUP BY / HAVING / ORDER BY /
            # LIMIT 属于**外层**查询，必须交给下面的通用解析流程
            # （原实现直接置 rest=''，外层子句被整段静默丢弃）
            rest = tail
        else:
            tm = re.match(r'[`"]?(\w+)[`"]?', from_rest)
            if not tm:
                raise SyntaxError("表名缺失")
            main_table = tm.group(1)
            rest = from_rest[tm.end():]
            # 表别名：`FROM users u JOIN ...` —— 不识别别名的话下面
            # 的 JOIN 正则会在 ' u JOIN' 处匹配失败，整个 JOIN 被静默丢弃
            main_alias, rest = self._take_alias(rest)

        # JOIN
        joins = []
        while True:
            jm = re.match(
                r'\s*(INNER|LEFT|RIGHT|CROSS)?\s*(?:OUTER\s+)?JOIN\s+[`"]?(\w+)[`"]?',
                rest, re.IGNORECASE)
            if not jm:
                break
            jtype = (jm.group(1) or 'INNER').upper()
            jtable = jm.group(2)
            rest = rest[jm.end():]
            jalias, rest = self._take_alias(rest)
            on_cond = None
            # ON 文本只切到下一个顶层子句为止。注意 `(.+?)` 是非贪婪的，
            # `AND` 不会被当成分隔符（JOIN 子句的 AND 属于 ON 条件），
            # 传给 `_join_condition` 后若形态不认识会显式报错，
            # 而不是静默丢掉 ON（丢掉就退化成交叉连接 → 静默错答）。
            om = re.match(
                r'\s+ON\s+(.+?)(?=\s+(?:INNER|LEFT|RIGHT|CROSS)?\s*'
                r'(?:OUTER\s+)?JOIN\b|\s+WHERE\b|\s+GROUP\b|\s+ORDER\b'
                r'|\s+HAVING\b|\s+LIMIT\b|$)',
                rest, re.IGNORECASE | re.DOTALL)
            if om:
                on_cond = self._join_condition(
                    om.group(1).strip())
                rest = rest[om.end():]
            joins.append({
                'type': jtype, 'table': jtable, 'on': on_cond,
                'alias': jalias,
            })

        # 列（窗口函数从列表达式里抽出来，交给执行器单独计算）
        # JSON 路径语法糖在投影里同样先改写（`SELECT data->'$.k' FROM docs`），
        # 改写后是普通标量函数投影，走既有投影求值路径。
        #
        # ★ 但 MySQL 的**输出列名**是用户写的原文（`data->'$.k'`），不是改写后
        # 的 `JSON_EXTRACT(data, '$.k')`。改写只在内部进行，因此这里额外记一份
        # **与投影列表逐项对齐**的原文表（`json_display`：无改写处为 None），
        # 供执行器取列名（executor `_projection_displays`）。
        # 用「逐项对齐」而不是「表达式 -> 原文」字典：`SELECT data->'$.k',
        # JSON_EXTRACT(data, '$.k')` 里第二项的改写结果与第一项的原文完全同形，
        # 字典会把两项都显示成 `data->'$.k'`（MySQL 只显示第一项为原文）。
        # 有 `AS 别名` 时列名就是别名（与 MySQL 一致），记 None。
        json_display = []
        if cols_str == '*':
            raw_columns = ['*']
            json_display.append(None)
        else:
            raw_columns = []
            for c in self._split_top_level(cols_str):
                rewritten = rewrite_json_paths(c)
                raw_columns.append(rewritten)
                _src_expr, src_alias = _split_select_alias(c)
                json_display.append(
                    None if (src_alias or rewritten == c) else _src_expr)
        for c in raw_columns:
            if re.search(r'\bAGAINST\s*\(', str(c), re.IGNORECASE):
                # MySQL 允许 `SELECT MATCH(c) AGAINST('x') ...`（返回相关度
                # 分数），本引擎没实现打分投影。原来会一路走到
                # 「列 'MATCH(c) AGAINST(...)' 不存在」这种误导性错误，
                # 这里直接说明原因（显式失败，不静默给 NULL）。
                raise SyntaxError(
                    "MATCH ... AGAINST 目前只支持出现在 WHERE 里："
                    "本引擎未实现相关度打分投影（MySQL 在这里返回分数）")
        columns, windows, projection = self._split_windows(raw_columns)

        # WHERE / GROUP / HAVING / ORDER / LIMIT
        # 全部用「括号深度感知」的扫描器切分（_extract_clause）：子查询
        # 内部的 GROUP BY / HAVING / LIMIT 位于括号里，不算本层的子句。
        # 原先用 `re.search(r'\bWHERE\s+(.+?)(?=\s+GROUP\b|$)')`，会在子查询
        # 的 GROUP BY 处提前截断 → WHERE 正文缺右括号 → 误报 SyntaxError。
        where = None
        order_by = None
        limit = None
        group_by = None
        having = None

        wtext = self._extract_clause(
            rest, 'WHERE', ('GROUP', 'ORDER', 'LIMIT', 'HAVING'))
        if wtext is not None:
            where = self._where(wtext)

        gtext = self._extract_clause(
            rest, 'GROUP', ('HAVING', 'ORDER', 'LIMIT'))
        if gtext is not None:
            group_by = [
                rewrite_json_paths(c.strip())
                for c in self._split_top_level(gtext)
            ]

        htext = self._extract_clause(rest, 'HAVING', ('ORDER', 'LIMIT'))
        if htext is not None:
            having = self._where(htext)

        # ORDER BY：取**顶层** ORDER BY 正文（到 LIMIT 为止），按顶层逗号
        # 拆成**任意个**排序键，每个键独立解析可选的 ASC / DESC。
        #
        # 数据结构：`order_by = [(键文本, desc), ...]`（列表，逐键独立方向）。
        # 键文本原样保留（普通列 / 限定列 `u.city` / 投影别名 / 标量表达式
        # `LENGTH(name)` / 聚合表达式 `SUM(salary)` / 序号 `2`），由执行器按
        # 「输出列 → 表达式」的顺序解析；**任一个键解析不到都在执行期显式
        # 报错**（报出第几个键与原因），绝不静默丢弃。
        #
        # 原实现是 `items[0]` 单键 + `([\w.]+)(?:\s+(ASC|DESC))?`，有两个
        # 静默错答：① `ORDER BY a, b` 的第二键之后被直接丢掉，只按 a 排；
        # ② `ORDER BY LENGTH(name) DESC` 只取到 `LENGTH`（函数名当列名），
        # `(name) DESC` 让 ASC/DESC 分组匹配不上 → DESC 也被丢掉。
        # 解析不出来的形态（`ORDER BY a NULLS LAST`）不在这里放过：键文本
        # 会带着 `NULLS LAST` 落到执行器，由统一排序路径显式报错。
        otext = self._extract_clause(rest, 'ORDER', ('LIMIT',))
        if otext is not None:
            keys = []
            for pos, raw in enumerate(self._split_top_level(otext)):
                item = rewrite_json_paths(raw.strip())
                if not item:
                    continue
                desc = False
                dm = re.match(r'^(.*?)\s+(ASC|DESC)\s*$', item,
                              re.IGNORECASE | re.DOTALL)
                if dm:
                    item = dm.group(1).strip()
                    desc = dm.group(2).upper() == 'DESC'
                if not item:
                    raise SyntaxError(
                        f"ORDER BY 第 {pos + 1} 个排序键为空"
                        f"（只写了 ASC / DESC）")
                keys.append((item, desc))
            if keys:
                order_by = keys

        # LIMIT：三种写法都要认
        #   LIMIT n                 -> limit=n
        #   LIMIT n OFFSET m        -> limit=n, offset=m
        #   LIMIT m, n              -> offset=m, limit=n（MySQL 风格）
        # 原实现只取 n，OFFSET / `LIMIT m, n` 的偏移被静默丢掉。
        offset = None
        lm = re.search(r'LIMIT\s+(\d+)\s+OFFSET\s+(\d+)',
                       rest, re.IGNORECASE)
        if lm:
            limit = int(lm.group(1))
            offset = int(lm.group(2))
        else:
            lm = re.search(r'LIMIT\s+(\d+)\s*,\s*(\d+)',
                           rest, re.IGNORECASE)
            if lm:
                offset = int(lm.group(1))
                limit = int(lm.group(2))
            else:
                lm = re.search(r'LIMIT\s+(\d+)', rest, re.IGNORECASE)
                if lm:
                    limit = int(lm.group(1))

        return {
            'command': 'SELECT',
            'table': main_table,
            'table_alias': main_alias,
            'subquery': subquery,
            'no_from': no_from,
            'joins': joins,
            'columns': columns,
            'windows': windows,
            'projection': projection,
            # `data->'$.k'` 等 JSON 路径投影的「输出列名用原文」表
            'json_display': json_display,
            'distinct': distinct,
            'where': where,
            'group_by': group_by,
            'having': having,
            'order_by': order_by,
            'limit': limit,
            'offset': offset,
        }

    # ==================== WHERE ====================

    def _where(self, s: str) -> Dict:
        """解析 WHERE / HAVING 条件，产出 and/or/比较 条件树

        实现方式（本次重写，原实现是裸 `re.split(r'\\s+AND\\s+')`）：

        1. 先做括号平衡校验，真语法错误（`(a = 1`、`a = 1)`）直接
           抛 SyntaxError，不再靠"后面碰巧匹配不上"来兜底；
        2. 剥掉「整段被一对平衡括号包住」的外层括号（`(A OR B)` ->
           `A OR B`），这样括号分组才能参与递归求值；
        3. 先按**顶层** OR 切（OR 优先级低于 AND），再按**顶层**
           AND 切；切分只认「圆括号深度 0 且不在字符串字面量里」的
           关键字，因此子查询 `EXISTS (SELECT ... WHERE a AND b)`
           里的 AND 不会被外层拆开。

        已知限制：AND/OR 两级 + NOT / IS [NOT] NULL / 标量子查询。
        子查询里的 OR / AND 由 _split_cond_top_level 的深度感知切分保护，
        不会被外层拆开，所以这里不需要再动切分逻辑。
        """
        s = s.strip()
        # JSON 路径语法糖（`data->'$.k'`）必须在括号/AND-OR 切分之前改写：
        # 否则 `->` 里的 `>` 会被 `_split_top_level_comparison` 当成比较运算符，
        # 整段条件解析失败（原实现直接报「WHERE 条件错误」）。
        s = rewrite_json_paths(s)
        self._check_paren_balance(s)

        # `NOT EXISTS (...)` / `NOT IN (...)` 是独立谓词形态（有自己的
        # {'op': 'NOT EXISTS'} / {'op': 'NOT IN'} 表示），不按整体取反，
        # 交给下面的 EXISTS / IN 分支处理。
        is_pred_not = bool(
            re.match(r'NOT\s+(EXISTS|IN)\b', s, re.IGNORECASE))

        # 整段 `NOT ( ... )`：在 OR/AND 切分**之前**处理，否则
        # `NOT (a = 1 OR b = 2)` 会被切成 ['NOT (a = 1', 'b = 2)']。
        # 必须要求配对括号正好在末尾：`NOT (A) AND B` 的右括号不在末尾，
        # 它等价于 `(NOT (A)) AND B`，交给下面按顶层 AND 切分（AND 比
        # NOT 结合得更紧），逐段递归后自然得到正确树形。
        if not is_pred_not:
            nm = re.match(r'NOT\s*\(', s, re.IGNORECASE)
            if nm:
                end = self._matching_paren(s, nm.end() - 1)
                if end == len(s) - 1:
                    inner = self._where(s[nm.end():end])
                    # 规范化：`NOT (c IN (...))` 直接产出 `c NOT IN (...)`
                    # （含子查询与字面量列表两种形态）。原因：
                    # 1) `IN (v1, v2)` 的解析结果本身要由外层补 column，
                    #    嵌在 NOT 里会变成没有 column 的 {'values': ...}，
                    #    执行器取 column 时报「表不存在: 'value'」；
                    # 2) 执行器对 NOT IN 的三值语义（列表/子查询含 NULL）
                    #    已有完整实现，直接复用比再包一层 NOT 更可靠。
                    if inner.get('op') == 'IN':
                        inner = dict(inner)
                        inner['op'] = 'NOT IN'
                        return inner
                    # 同理 `NOT (c IS NULL)` -> `c IS NOT NULL`
                    if 'column' in inner and inner.get('value') is None \
                            and inner.get('op') in ('=', '!='):
                        inner = dict(inner)
                        inner['op'] = '!=' if inner['op'] == '=' else '='
                        return inner
                    return {'not': inner}

        s = self._strip_outer_parens(s)

        or_parts = self._split_cond_top_level(s, 'OR')
        if len(or_parts) > 1:
            return {'or': [self._where(p) for p in or_parts]}

        and_parts = self._split_cond_top_level(s, 'AND')
        if len(and_parts) > 1:
            return {'and': [self._where(p) for p in and_parts]}

        # 走到这里说明整段没有顶层 OR/AND：`NOT (...)` 一定是整段形态，
        # 但前面的括号匹配失败（例如 `NOT (A) 尾随垃圾`），显式报错，
        # 避免把 `NOT dept = 'Eng'` 之类静默当成别的东西。
        if not is_pred_not and re.match(r'NOT\b', s, re.IGNORECASE):
            raise SyntaxError(f"NOT 后面必须是一段括号条件: {s}")

        # EXISTS
        em = re.match(
            r'EXISTS\s*\(\s*(.+)\s*\)\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if em:
            inner = self.parse(em.group(1).strip())
            return {'op': 'EXISTS', 'subquery': inner}

        nm = re.match(
            r'NOT\s+EXISTS\s*\(\s*(.+)\s*\)\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if nm:
            inner = self.parse(nm.group(1).strip())
            return {'op': 'NOT EXISTS', 'subquery': inner}

        # 标量（相关）子查询：`(SELECT COUNT(*) FROM o WHERE o.uid = u.id) > 1`
        # 产出 {'op': '>', 'value': '1', 'subquery': <内层 AST>}，
        # 与 IN (SELECT ...) 同构，执行器的逐行求值路径据此取标量值再比较。
        sm = re.match(r'^\s*\(\s*SELECT\b', s, re.IGNORECASE)
        if sm:
            end = self._matching_paren(s, s.index('('))
            tail = s[end + 1:].strip()
            tm = re.match(r'(=|!=|<>|<=|>=|<|>)\s*(.+?)\s*$',
                          tail, re.IGNORECASE | re.DOTALL)
            if tm and tail:
                inner = self.parse(s[1:end].strip())
                op = tm.group(1).upper()
                return {
                    'op': '!=' if op == '<>' else op,
                    'value': tm.group(2).strip().strip("'\""),
                    'subquery': inner,
                }

        # IS NULL / IS NOT NULL（普通列；聚合形态在下面的聚合谓词里处理）
        # 统一编码成 op='=' / '!=' + value=None —— 与存储层 _match、
        # plan_executor._eval_condition、统一路径 _eval_condition 的既有
        # NULL 语义完全一致，因此两条求值路径都不需要额外分支。
        zm = re.match(
            r'\s*[`"]?([\w.]+)[`"]?\s+IS\s+(NOT\s+)?NULL\s*$',
            s, re.IGNORECASE)
        if zm:
            return {
                'column': zm.group(1),
                'op': '!=' if zm.group(2) else '=',
                'value': None,
            }

        # IN (SELECT ...)
        im = re.match(
            r'[`"]?([\w.]+)[`"]?\s+(NOT\s+)?IN\s*\(\s*SELECT\s+(.+)\s*\)\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if im:
            inner = self.parse('SELECT ' + im.group(3))
            return {
                'column': im.group(1),
                'op': 'NOT IN' if im.group(2) else 'IN',
                'subquery': inner,
            }

        # MATCH(列[, 列...]) AGAINST('查询词' [IN BOOLEAN MODE])
        # 全文检索谓词（MySQL 语法）。解析期只产出标记，命中行集由执行器
        # 用表上的倒排索引真算（见 executor._apply_match），不做假数据。
        fm = re.match(
            r"""MATCH\s*\(\s*(?P<cols>[`"]?\w+[`"]?"""
            r"""(?:\s*,\s*[`"]?\w+[`"]?)*)\s*\)\s*"""
            r"""AGAINST\s*\(\s*'(?P<q>(?:[^']|'')*)'\s*"""
            r"""(?:IN\s+(?P<mode>BOOLEAN|NATURAL\s+LANGUAGE)\s+MODE\s*)?\)\s*$""",
            s, re.IGNORECASE | re.DOTALL)
        if fm:
            cols = [c.strip().strip('`"')
                    for c in fm.group('cols').split(',')]
            mode = (fm.group('mode') or 'NATURAL LANGUAGE').upper()
            return {
                'op': 'MATCH',
                'columns': [c for c in cols if c],
                'against': fm.group('q').replace("''", "'"),
                'mode': ' '.join(mode.split()),
            }

        # IN (v1, v2)
        im2 = re.match(
            r'[`"]?([\w.]+)[`"]?\s+(NOT\s+)?IN\s*\(\s*(.+?)\s*\)\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if im2:
            raw_items = self._split_top_level(im2.group(3))
            # 未加引号且是标识符形态的项 = **列引用**（`o.user_id IN (u.id)`）：
            # 记下标，供 `_rewrite_in_lists` / 求值器按列取值。
            # 引号包起来的（`IN ('id')`）返回 None → 保持字符串字面量语义。
            col_refs = {}
            for idx, raw in enumerate(raw_items):
                ref = _column_ref_text(raw)
                if ref is not None:
                    col_refs[idx] = ref
            node = {
                'column': im2.group(1),
                'op': 'NOT IN' if im2.group(2) else 'IN',
                'values': [
                    v.strip().strip("'\"") for v in raw_items
                ],
            }
            if col_refs:
                node['value_columns'] = col_refs
            return node

        # 聚合谓词（HAVING 的常见形式）：
        #   COUNT(*) > 1 / SUM(amount) >= 100 / COUNT(id) < 3
        # 左值是聚合函数而不是普通列，_where 原先直接抛 SyntaxError，
        # 导致 `... GROUP BY city HAVING COUNT(*) > 5` 连解析都过不去。
        # 产出 {'agg': 'COUNT(*)', 'op': '>', 'value': 1}，聚合值由
        # GroupBy / 统一分组路径用 _eval_agg_expr 求出后再比较。
        # 聚合值上的 IS [NOT] NULL 同样编码成 op='=' / '!=' + value=None。
        # 函数名必须是**聚合函数**才算聚合谓词。原来的正则对任意 `名字(...)`
        # 都成立，于是 `WHERE LOWER(name) = 'alice'` / `WHERE ABS(v-20) < 5`
        # 会被编成 `{'agg': 'LOWER(name)', ...}`（标量函数被当成聚合），
        # 分组路径按聚合去算 → 报出与真实原因无关的错误。这里放行给下面的
        # 表达式谓词分支（`_parse_expr_predicate`）处理。
        agm = re.match(
            r'\s*(\w+)\s*\(\s*(\*|[^()]*?)\s*\)\s+IS\s+(NOT\s+)?NULL\s*$',
            s, re.IGNORECASE)
        if agm and agm.group(1).upper() in _AGG_FUNC_NAMES:
            return {
                'agg': f'{agm.group(1).upper()}({agm.group(2).strip()})',
                'op': '!=' if agm.group(3) else '=',
                'value': None,
            }
        am = re.match(
            r'\s*(\w+)\s*\(\s*(\*|[^()]*?)\s*\)\s*'
            r'(=|!=|<>|<=|>=|<|>)\s*(.+?)\s*$',
            s, re.IGNORECASE)
        if am and am.group(1).upper() in _AGG_FUNC_NAMES:
            op = am.group(3).upper()
            if op == '<>':
                op = '!='
            return {
                'agg': f'{am.group(1).upper()}({am.group(2).strip()})',
                'op': op,
                'value': am.group(4).strip().strip("'\""),
            }

        # 表达式谓词（标量函数 / 算术，如 `LOWER(name) = 'alice'`、
        # `salary * 2 + 10 > 50`、`ABS(v - 20) < 5`、`SUM(v) * 2 > 100`）：
        # 左右两侧都由下面的标量表达式解析器解析，求值交给
        # `eval_expr_predicate`（见文件末尾「标量表达式」一节）。
        # **必须放在普通比较之前**：否则 `name = UPPER(x)` 会被普通比较
        # 当成「name 等于字面量 'UPPER(x)'」静默给出错误行集。
        expr_pred = self._parse_expr_predicate(s)
        if expr_pred is not None:
            return expr_pred

        # 普通比较
        m = re.match(
            r'\s*[`"]?([\w.]+)[`"]?\s*(=|!=|<>|<=|>=|<|>|LIKE)\s*(.+?)\s*$',
            s, re.IGNORECASE)
        if m:
            op = m.group(2).upper()
            if op == '<>':
                op = '!='
            raw_val = m.group(3).strip()
            # 右侧若还含**顶层**（引号/括号之外）的逗号或比较运算符，
            # 说明这段条件本身是坏的（典型：`WHERE a = b, c > 5` 用逗号
            # 当 AND 用）。原实现把 `b, c > 5` 整段当字面量存进 value，
            # 执行时比较永远不成立 → 静默返回空集/0 行。这里直接报语法错。
            if self._has_top_level_break(raw_val):
                raise SyntaxError(f"WHERE 条件错误（缺少 AND/OR？）: {s}")
            val = raw_val.strip("'\"")
            node = {
                'column': m.group(1), 'op': op, 'value': val,
            }
            # ★ 右侧是**列引用**（`WHERE o.user_id = u.id`）而不是字面量时
            # 打上 `value_column` 标记。
            #
            # 为什么必须区分：原来的编码把右侧文本一律当字面量
            # （`val = raw_val.strip("'\"")`），求值器于是执行
            # `row[o.user_id] == 'u.id'` —— 与字符串比较永远不成立，
            # **静默返回空集**。跨表 WHERE（`o.user_id = u.id`）、
            # JOIN ON 回退成连接后过滤条件、单表 `WHERE user_id = id`
            # 全部命中这条路（实测都返回 0 行且不报错，是静默错答）。
            #
            # 只认「无引号 / 反引号」的标识符形态：`= 'id'`（字符串字面量）
            # 不加标记，因此 `WHERE city = 'name'` 仍然是字面量比较。
            col_ref = _column_ref_text(raw_val)
            if col_ref is not None:
                node['value'] = col_ref
                node['value_column'] = True
            return node

        # 行值（tuple）比较：`(a, b) IN (SELECT x, y FROM u)` /
        # `(a, b) = (1, 2)`。
        #
        # 本引擎的存储层 `_match` 与各条求值路径都只支持**单列**比较，
        # 没有元组匹配能力。这种形态原先落到下面兜底的
        # 「WHERE 条件错误: ...」——虽然也是显式失败（不会静默错答），
        # 但看不出原因。这里显式说明「不支持行值比较」。
        #
        # **红线**：绝不静默降级成「行值只比第一列」——那会给出一份
        # 看起来正确、实际错误的行集（本会话消灭的正是这类静默错答）。
        #
        # 已知限制：如需支持行值 IN，需要 ① 解析层产出
        # `{'columns': ['a','b'], 'op': 'IN', 'subquery': ...}`；
        # ② 在四条求值路径上实现行值匹配——`_rewrite_subqueries`
        # （单表下推）、`_eval_plain_subquery_predicate`（逐行非相关）、
        # `_eval_correlated_predicate`（相关）、
        # `plan_executor._eval_condition`（JOIN 纯计划路径）；
        # ③ 行值比较的 NULL 语义：任一列含 NULL → 该行比较结果为
        # UNKNOWN（IN 不命中；NOT IN 遇到含 NULL 的候选行整体为 UNKNOWN）。
        # 这是一次跨解析/执行/计划三层的语义扩展，本次不做，故显式拒绝。
        tup = re.match(
            r'\s*\(\s*[\w`".]+\s*(?:,\s*[\w`".]+\s*)+\)\s*'
            r'(?:NOT\s+)?(?:IN\b|=|!=|<>|<=|>=|<|>)',
            s, re.IGNORECASE)
        if tup:
            raise SyntaxError(
                f"不支持行值（多列）比较 / 行值 IN: {s} —— "
                f"本引擎的 IN 与比较运算只支持单列左值，行值形态需要多个列"
                f"同时参与比较（任一列含 NULL 时整体为 UNKNOWN），"
                f"无法安全降级为「只比第一列」；"
                f"请改用单列条件组合，例如 "
                f"(a, b) IN (SELECT x, y FROM t) 可改写为 "
                f"EXISTS (SELECT 1 FROM t WHERE x = a AND y = b)")

        raise SyntaxError(f"WHERE 条件错误: {s}")

    # ============ 表达式谓词（标量函数 / 算术，任务 1、2） ============

    # 单个操作数：裸列名（可带表限定符 / 反引号 / 双引号）
    _PLAIN_OPERAND_RE = re.compile(r'^[`"]?[\w.]+[`"]?$')

    @classmethod
    def _split_top_level_comparison(cls, s: str):
        """在顶层（括号 / 字符串字面量之外）找**第一个**比较运算符

        返回 (左文本, op, 右文本)；找不到返回 None。op 归一到
        `= != < > <= >=`（`<>` 写成 `!=`）。两字符运算符优先匹配，
        因此 `a >= 1` 不会在 `>` 处断开。
        """
        ops = ('<=', '>=', '<>', '!=', '=', '<', '>')
        depth = 0
        for i, ch, quoted in cls._scan_quotes(s):
            if quoted:
                continue
            if ch == '(':
                depth += 1
                continue
            if ch == ')':
                depth -= 1
                continue
            if depth != 0:
                continue
            for op in ops:
                if s.startswith(op, i):
                    left, right = s[:i].strip(), s[i + len(op):].strip()
                    if not left or not right:
                        return None
                    return left, ('!=' if op == '<>' else op), right
        return None

    def _parse_expr_predicate(self, s: str):
        """把 `表达式 比较 表达式` 解析成 `{'expr_pred': {...}}`

        适用形态（任务 1 / 2）：
        * 左值是标量函数 / 算术：`WHERE LOWER(name) = 'alice'`、
          `WHERE LENGTH(name) > 3`、`WHERE salary * 2 + 10 > 50`；
        * 右值是函数 / 算术：`WHERE name = UPPER(dept)`；
        * 聚合参与运算：`HAVING SUM(v) * 2 > 100`（`sum` 由聚合求值器提供值）。

        返回 None = 「不归本分支管」，交回既有比较分支：

        * 两侧都是「普通列名 / 字面量」的比较（`id = 1`、`u.city = u2.city`）
          **保持原有编码**（`{'column','op','value'}`）—— 相关子查询的外层
          列折叠（`_fold_outer_literals` 处理 `value` 是列名的情况）依赖它；
        * 左值是**裸聚合函数**（`SUM(v) > 100`）：保持既有 `{'agg': ...}`；
        * 任一侧解析不出标量表达式（例如右侧是 `(SELECT ...)` 标量子查询）：
          交回既有分支（标量子查询比较有自己的一套行数语义）。
        """
        split = self._split_top_level_comparison(s)
        if split is None:
            return None
        left_text, op, right_text = split
        left_plain = bool(self._PLAIN_OPERAND_RE.match(left_text))
        right_plain = bool(self._PLAIN_OPERAND_RE.match(right_text))
        if left_plain and (right_plain or is_literal_text(right_text)):
            return None
        left = parse_scalar_expr(left_text)
        right = parse_scalar_expr(right_text)
        if left is None or right is None:
            return None
        if left.get('kind') == 'func' and left.get('name') in _AGG_FUNC_NAMES:
            return None
        return {'expr_pred': {'left': left, 'op': op, 'right': right}}

    # ==================== DDL ====================

    @staticmethod
    def _extract_paren(text: str, open_idx: int):
        """取 `text[open_idx] == '('` 的配对右括号内部文本

        返回 (内部文本, 右括号下标)。引号内的括号不计。
        必须用配对扫描而不是 `\\((.*)\\)`：后者是贪婪的，
        `CREATE TABLE t (...) PARTITION BY HASH(id)` 会把列定义切到
        `PARTITION BY HASH(id` 的右括号上（分区描述被整段吞掉 —— 这正是
        原来 `Table._partition` 为 None 的原因）；非贪婪版本又会把
        `GENERATED ALWAYS AS (UPPER(a))` 的表达式截成 `UPPER(a`。
        """
        depth = 0
        for i, ch, quoted in Parser._scan_quotes(text):
            if i < open_idx or quoted:
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return text[open_idx + 1:i], i
        raise SyntaxError(f"括号不匹配: {text[:120]}")

    def _create_table(self, sql: str) -> Dict:
        m = re.match(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?\s*\(',
            sql, re.IGNORECASE)
        if not m:
            raise SyntaxError("CREATE TABLE 语法错误")

        table = m.group(1)
        body, end = self._extract_paren(sql, m.end() - 1)
        tail = sql[end + 1:].strip()

        cols = []
        constraints = []
        for part in self._split_top_level(body):
            part_stripped = part.strip()
            up = part_stripped.upper()

            if up.startswith('CHECK'):
                ck = self._parse_check(part_stripped)
                if ck:
                    constraints.append(ck)
                continue

            col = self._column_def(part_stripped)
            if col:
                cols.append(col)

        if not cols:
            raise SyntaxError("表至少需要一列")

        if not any(c.get('primary') for c in cols):
            cols[0]['primary'] = True

        # 分区
        partition = None
        pm = re.search(
            r'PARTITION\s+BY\s+(HASH|RANGE|LIST)\s*\((\w+)\)(.*)$',
            tail, re.IGNORECASE | re.DOTALL)
        if pm:
            ptype = pm.group(1).upper()
            pcol = pm.group(2)
            prest = pm.group(3).strip()
            partition = self._parse_partition(
                ptype, pcol, prest)

        return {
            'command': 'CREATE TABLE',
            'table': table,
            'columns': cols,
            'checks': constraints,
            'partition': partition,
            # `CREATE TABLE IF NOT EXISTS` 原实现被正则吃掉后丢掉：执行器
            # 于是"表已存在就报错"，同一份 dump 无法重复恢复。此处如实
            # 回传，由执行器决定幂等语义。
            'if_not_exists': bool(re.match(
                r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b', sql, re.IGNORECASE)),
        }

    def _parse_check(self, s: str):
        m = re.match(
            r'CHECK\s*\(\s*(.+?)\s*\)(?:\s+NOT\s+ENFORCED)?\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        return {'expr': m.group(1).strip()}

    def _parse_partition(self, ptype, pcol, rest):
        if ptype == 'HASH':
            m = re.search(
                r'PARTITIONS?\s+(\d+)', rest, re.IGNORECASE)
            n = int(m.group(1)) if m else 4
            return {'type': 'HASH', 'col': pcol, 'num': n}
        if ptype == 'RANGE':
            parts = []
            for pm in re.finditer(
                r'PARTITION\s+(\w+)\s+VALUES\s+LESS\s+THAN\s*\(\s*([^)]+?)\s*\)',
                rest, re.IGNORECASE):
                name = pm.group(1)
                val = pm.group(2).strip()
                if val.upper() == 'MAXVALUE':
                    hi = None
                else:
                    try:
                        hi = int(val) if '.' not in val \
                            else float(val)
                    except ValueError:
                        hi = None
                parts.append({'name': name, 'hi': hi})
            ranges = []
            lo = None
            for p in parts:
                ranges.append({
                    'lo': lo, 'hi': p['hi'], 'name': p['name'],
                })
                lo = p['hi']
            return {
                'type': 'RANGE', 'col': pcol, 'ranges': ranges,
            }
        if ptype == 'LIST':
            parts = []
            for pm in re.finditer(
                r'PARTITION\s+(\w+)\s+VALUES\s+IN\s*\(\s*([^)]+?)\s*\)',
                rest, re.IGNORECASE):
                name = pm.group(1)
                vals = [
                    v.strip().strip("'\"")
                    for v in pm.group(2).split(',')
                ]
                coerced = []
                for v in vals:
                    try:
                        coerced.append(int(v))
                    except ValueError:
                        coerced.append(v)
                parts.append({'name': name, 'values': coerced})
            return {
                'type': 'LIST', 'col': pcol, 'lists': parts,
            }
        return None

    def _column_def(self, s: str) -> Optional[Dict]:
        """列定义（完整版）"""
        s = s.strip()
        if not s:
            return None
        upper = s.upper()

        # 跳过表级约束
        if upper.startswith((
                'PRIMARY KEY', 'KEY ', 'INDEX ',
                'UNIQUE KEY', 'CONSTRAINT ',
                'FOREIGN KEY', 'FULLTEXT', 'CHECK')):
            return None

        parts = s.split(None, 2)
        if len(parts) < 2:
            raise SyntaxError(f"列定义错误: {s}")

        name = parts[0].strip('`"')
        col_type = parts[1].upper()
        rest = (parts[2] if len(parts) > 2 else '')

        # 生成列（表达式用配对括号提取：`GENERATED ALWAYS AS (UPPER(a))`）
        generated = None
        gm = re.search(r'GENERATED\s+ALWAYS\s+AS\s*\(', rest, re.IGNORECASE)
        if gm:
            gexpr, gend = self._extract_paren(rest, gm.end() - 1)
            after = rest[gend + 1:]
            sm = re.match(r'\s*(STORED|VIRTUAL)', after, re.IGNORECASE)
            generated = {
                'expr': gexpr.strip(),
                'stored': bool(sm) and sm.group(1).upper() == 'STORED',
            }

        # CHECK
        check = None
        cm = re.search(r'CHECK\s*\(', rest, re.IGNORECASE)
        if cm:
            cexpr, _cend = self._extract_paren(rest, cm.end() - 1)
            check = {'expr': cexpr.strip()}

        return {
            'name': name,
            'type': self._normalize_type(col_type),
            'primary': 'PRIMARY KEY' in rest.upper(),
            'notnull': 'NOT NULL' in rest.upper(),
            'unique': 'UNIQUE' in rest.upper(),
            'generated': generated,
            'check': check,
        }

    def _normalize_type(self, t: str) -> str:
        t = re.sub(r'\(.*?\)', '', t).strip().upper()
        mapping = {
            'INTEGER': 'INT', 'SMALLINT': 'INT',
            'TINYINT': 'INT', 'MEDIUMINT': 'INT',
            'SERIAL': 'INT',
            'BIGINT': 'BIGINT',
            'NUMERIC': 'DECIMAL', 'REAL': 'DOUBLE',
            'CHAR': 'VARCHAR',
            'LONGTEXT': 'TEXT', 'MEDIUMTEXT': 'TEXT',
            'TINYTEXT': 'TEXT',
            'BOOL': 'BOOLEAN', 'TIMESTAMP': 'DATETIME',
        }
        return mapping.get(t, t)

    def _drop_table(self, sql: str) -> Dict:
        m = re.match(
            r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
            sql, re.IGNORECASE)
        if not m:
            raise SyntaxError("DROP TABLE 语法错误")
        return {'command': 'DROP TABLE', 'table': m.group(1),
                # 同上：`IF EXISTS` 的"表不存在也算成功"语义要一路传到执行器
                'if_exists': bool(re.match(
                    r'DROP\s+TABLE\s+IF\s+EXISTS\b', sql, re.IGNORECASE))}

    # ==================== DML ====================

    # `INSERT ... VALUES` 里 VALUES 关键字（后面必须跟一个 `(`，避免
    # 把 `INSERT INTO t SELECT ...` 或列名里的 "values" 误判）
    _INSERT_VALUES_RE = re.compile(r'\bVALUES\s*\(', re.IGNORECASE)
    _ON_DUP_RE = re.compile(
        r'\s*ON\s+DUPLICATE\s+KEY\s+UPDATE\b', re.IGNORECASE)

    @staticmethod
    def _read_paren_rows(sql: str, i: int):
        """从 `sql[i] == '('` 开始，读出若干顶层分组 `(a, b), (c, d)`

        返回 (行文本列表, 结束下标)。这是 `_insert` 的底层扫描器：
        - **引号内的 `)` 不算分组结束**（`'x)y'`）；
        - **引号可以跨行**（dump 里的 VARCHAR 真含换行）；
        - 引号规则同 MySQL：`''` 是转义的单引号、`""` 是转义的双引号，
          反引号整体包住标识符（`'it''s)'` 只结束一次）。
        原实现用 `re.finditer(r'\\(([^)]*)\\)', ...)`，任何一个 `)` 都会
        截断行、并且会跨分组匹配，于是 `'x)y'` 被存成 `'x'`——静默的数据
        损坏。恢复路径绝对不能容忍这种截断。
        """
        rows = []
        n = len(sql)
        while i < n and sql[i] == '(':
            start = i + 1
            depth = 1
            i += 1
            while i < n:
                ch = sql[i]
                if ch in ("'", '"', '`'):
                    q = ch
                    if q == '`':
                        i += 1
                        while i < n and sql[i] != '`':
                            i += 1
                        i += 1
                        continue
                    i += 1
                    while i < n:
                        if sql[i] == q:
                            if i + 1 < n and sql[i + 1] == q:
                                i += 2          # '' / "" 表示一个字面引号
                                continue
                            i += 1
                            break
                        i += 1
                    continue
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        rows.append(sql[start:i])
                        i += 1
                        break
                i += 1
            else:
                raise SyntaxError("INSERT 的括号不匹配")
            # 跳过 `, ` / 空白
            while i < n and sql[i] in ' \t\r\n':
                i += 1
            if i < n and sql[i] == ',':
                i += 1
                while i < n and sql[i] in ' \t\r\n':
                    i += 1
                if i >= n or sql[i] != '(':
                    raise SyntaxError("INSERT 的 VALUES 列表在逗号后缺少 '('")
                continue
            break
        if not rows:
            raise SyntaxError("INSERT 的 VALUES 列表为空")
        return rows, i

    def _insert(self, sql: str) -> Dict:
        head = re.match(
            r'INSERT\s+INTO\s+(?:[`"]?(\w+)[`"]?\.)?[`"]?(\w+)[`"]?',
            sql, re.IGNORECASE)
        if not head:
            raise SyntaxError("INSERT 语法错误")
        table = head.group(2) or head.group(1)

        cols = None
        pos = head.end()             # 列清单结束位置（绝对下标）
        m = re.match(r'\s*\(([^)]*)\)', sql[pos:])
        if m:                        # 显式列清单：`INSERT INTO t (a, b) VALUES ...`
            cols = [c.strip().strip('`"') for c in m.group(1).split(',')
                    if c.strip()]
            pos += m.end()

        vm = self._INSERT_VALUES_RE.search(sql, pos)
        if not vm:
            raise SyntaxError("INSERT 语法错误（只支持 VALUES 形式）")
        if sql[pos:vm.start()].strip():
            raise SyntaxError(
                f"INSERT 在表名（或列清单）与 VALUES 之间有多余内容: "
                f"{sql[pos:vm.start()].strip()[:40]!r}")

        # `vm.group(0)` 形如 `VALUES (`：定位那个真正的 '('
        open_idx = vm.end() - 1
        rows_text, end = self._read_paren_rows(sql, open_idx)

        # 尾部只允许 `ON DUPLICATE KEY UPDATE ...`（恢复路径不会生成，
        # 但客户端会；就地忽略，语义与本引擎"主键重复即报错"一致）
        tail = sql[end:].strip()
        if tail:
            od = self._ON_DUP_RE.match(tail)
            if not od:
                raise SyntaxError(
                    f"INSERT 尾部有多余内容: {tail[:40]!r}（只支持 "
                    f"ON DUPLICATE KEY UPDATE）")

        values = [self._value_row(t) for t in rows_text]
        return {
            'command': 'INSERT',
            'table': table,
            'columns': cols,
            'values': values,
        }

    @staticmethod
    def _unquote_literal(tok: str):
        """把 `_value_row` 产出的 token 还原成 Python 值

        - 带引号 → 字符串（去掉引号、`''` → `'`）
        - 不带引号 → 原样返回，交由列类型 `coerce` 决定（`42` / `3.5` /
          `NULL`）

        **反斜杠按字面保留**（SQLite / MySQL `NO_BACKSLASH_ESCAPES` 语义），
        唯一转义是成对引号。这样"含一个反斜杠的字符串"dump 出去、
        恢复回来仍是同一个值（否则备份/恢复会把 `C:\\tmp` 变成 `C:tmp`）。
        必须是引号敏感、而不是"含字母就当字符串"：dump 恢复依赖它把
        `'123'` 存成字符串而不是被 `int()` 掉。
        """
        if tok is None or not isinstance(tok, str):
            return tok
        if len(tok) >= 2 and tok[0] in ("'", '"') and tok[-1] == tok[0]:
            q = tok[0]
            body = tok[1:-1]
            if q == '"':
                return body.replace('""', '"')
            return body.replace("''", "'")
        return tok

    def _value_row(self, s: str) -> List:
        """把一个 VALUES 分组解析成值列表（引号内的逗号/空白不参与切分）

        原实现有两个正确性缺陷（都会静默改数据）：
        1. `''` 只被当成"空串"，`'it''s'` 被拆成 `it` 和 `s`；
        2. 引号前的空白靠 `strip()` 处理，`' a '` 这类**值本身带空格**的
           字符串会被削掉。
        现在的规则：引号内的字符逐字保留（只做 `''` 反转义，反斜杠按字面
        保留），引号外的 token 去首尾空白，`NULL` → None。类型转换交给
        `coerce`（按列类型）而不是在这里猜。
        """
        values = []
        cur = ''                 # 当前 token 的原文
        quoted = False           # 当前 token 是否以引号字面量开头
        i = 0
        n = len(s)

        def flush():
            nonlocal cur, quoted
            if quoted:
                values.append(cur)
            else:
                t = cur.strip()
                if t:
                    values.append(None if t.upper() == 'NULL' else t)
                elif cur:
                    # 纯空白 token：SQL 里 `(1, , 3)` 是错误，但要显式报错
                    # 而不是静默当成"少一列"
                    raise SyntaxError("VALUES 里有空的取值")
            cur = ''
            quoted = False

        while i < n:
            ch = s[i]
            if ch in ("'", '"') and not quoted:
                # 引号前的 SQL 空白不属于字面量：丢掉
                if not cur.strip():
                    cur = ''
                q = ch
                cur += ch
                quoted = True
                i += 1
                while i < n:
                    cur += s[i]
                    if s[i] == q:
                        if i + 1 < n and s[i + 1] == q:
                            cur += s[i + 1]      # '' / "" = 一个字面引号
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                continue
            if ch == ',':
                flush()
                i += 1
                continue
            cur += ch
            i += 1
        flush()

        out = []
        for v in values:
            if v is None:
                out.append(None)
            elif isinstance(v, str) and v[:1] in ("'", '"'):
                out.append(self._unquote_literal(v))
            else:
                out.append(v)
        return out

    def _update(self, sql: str) -> Dict:
        m = re.match(
            r'UPDATE\s+[`"]?(\w+)[`"]?\s+SET\s+(.+?)(?:\s+WHERE\s+(.+))?$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("UPDATE 语法错误")

        set_pairs = {}
        for pair in self._split_top_level(m.group(2)):
            if '=' not in pair:
                continue
            k, v = pair.split('=', 1)
            set_pairs[k.strip().strip('`"')] = \
                v.strip().strip("'\"")

        where = self._where(m.group(3)) if m.group(3) else None
        return {
            'command': 'UPDATE',
            'table': m.group(1),
            'set': set_pairs,
            'where': where,
        }

    def _delete(self, sql: str) -> Dict:
        m = re.match(
            r'DELETE\s+FROM\s+[`"]?(\w+)[`"]?(?:\s+WHERE\s+(.+))?$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("DELETE 语法错误")
        where = self._where(m.group(2)) if m.group(2) else None
        return {
            'command': 'DELETE', 'table': m.group(1),
            'where': where,
        }

    def _show_columns(self, sql: str) -> Dict:
        m = re.search(
            r'(?:FROM|DESCRIBE|DESC)\s+[`"]?(\w+)[`"]?',
            sql, re.IGNORECASE)
        if not m:
            raise SyntaxError("SHOW COLUMNS 语法错误")
        return {'command': 'SHOW COLUMNS', 'table': m.group(1)}

    def _join_condition(self, s: str) -> Optional[Dict]:
        """解析 JOIN ON 条件（标准语义：ON = 连接后的过滤条件）

        产出两个字段：

        * `left` / `right`：**仅当** ON 恰好是「两个列引用的单个等值比较」
          （`a.x = b.y`）时给出 —— 它是可用作等值连接键的形态，优化器据此
          构造 HashJoin（快路径，性能不变）；
        * `filter`：ON 的通用条件树（与 WHERE 同一套编码，右侧列引用带
          `value_column` 标记）。当等值键在连接结果里**解析不出来**时
          （自比较 `ON o.id = o.id`、只引用单侧、常量 `ON 1 = 1`、
          `ON o.uid = 1`、非等值 `ON o.amt > 0`），计划执行器按 SQL 标准
          把它当**连接后过滤条件**逐候选行求值（笛卡尔积 + ON 过滤）。

        **绝不返回 None**：返回 None 会让 JOIN 静默退化成无条件的交叉连接
        （`ON a.x=b.x AND b.y=1` 会丢掉后半段、结果行数翻倍），属于典型的
        静默错答。原来非等值形态直接抛 SyntaxError（显式失败、不静默），
        现在按标准语义算对；`_where` 认不出的形态仍抛 SyntaxError。
        """
        text = s.strip()
        out = {'filter': self._join_filter(text), 'text': text}
        m = re.match(
            r'[`"]?([\w.]+)[`"]?\s*=\s*[`"]?([\w.]+)[`"]?\s*$',
            text)
        if m:
            left, right = m.group(1), m.group(2)
            # 两侧都得是**列引用**：`ON 1 = 1`（常量）、`ON o.id = 3`
            # （右侧是数字）都不是等值连接键，不能拿去构造 HashJoin
            # （`_resolve_col` 解析不到时会静默退化成笛卡尔积）。
            if _column_ref_text(left) and _column_ref_text(right):
                out['left'] = left
                out['right'] = right
        return out

    def _join_filter(self, text: str) -> Dict:
        """ON 文本 -> 条件树

        常量比较（`1 = 1`、`'a' = 'a'`）必须走**表达式求值器**：普通比较
        分支会把 `1 = 1` 编成 `{'column': '1', ...}`（把数字当列名），
        求值时再报「列 '1' 不存在」—— 恒真的 ON 反而变成显式错误。
        """
        split = self._split_top_level_comparison(text)
        if split is not None:
            left, op, right = split
            if is_literal_text(left) and is_literal_text(right):
                ln = parse_scalar_expr(left)
                rn = parse_scalar_expr(right)
                if ln is not None and rn is not None:
                    return {'expr_pred': {'left': ln, 'op': op, 'right': rn}}
        return self._where(text)

    def _split_top_level(self, s: str) -> List[str]:
        """按顶层逗号分割"""
        parts, current, depth, in_str = [], '', 0, None
        for ch in s:
            if in_str:
                current += ch
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"', '`'):
                in_str = ch
                current += ch
            elif ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                parts.append(current.strip())
                current = ''
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        return parts

    # ============ 条件文本的「深度感知」切分（WHERE 用） ============

    @staticmethod
    def _scan_quotes(s: str):
        """逐字符扫描，产出 (字符下标, 该字符是否处于字符串字面量内)

        字符串字面量按 SQL 约定用 ' / " / ` 包起来；引号内的括号与
        关键字（`'a AND b'`）一律不算结构字符。转义用成对引号（`''`）
        表示，成对写法在状态机里自然闭合，不需要额外处理。
        """
        in_str = None
        for i, ch in enumerate(s):
            if in_str:
                yield i, ch, True
                if ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"', '`'):
                    in_str = ch
                    yield i, ch, True
                else:
                    yield i, ch, False

    @classmethod
    def _has_top_level_break(cls, s: str) -> bool:
        """文本里是否含顶层（不在引号/括号内）的逗号或比较运算符

        用于识别「比较右侧还塞了另一段条件」的坏形态，例如
        `WHERE o.user_id = users.id, o.amount > 5`（用逗号当 AND）。
        这种文本被当成字面量后比较永远不成立，会静默返回空集。
        """
        depth = 0
        for _, ch, quoted in cls._scan_quotes(s):
            if quoted:
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0 and (ch == ',' or ch in '=<>!'):
                return True
        return False

    def _check_paren_balance(self, s: str) -> None:
        """括号/引号平衡校验：不平衡即真语法错误 → SyntaxError

        原先 `WHERE (a = 1` 靠后续正则匹配不上才报错，错误信息含糊；
        显式校验后报错位置明确，且 `a = 1)` 这种多余右括号也会被拦下
        （而不是把 `1)` 当成字面量静默接受）。
        """
        depth = 0
        in_str = None
        for _, ch, quoted in self._scan_quotes(s):
            if quoted:
                if in_str is None:
                    in_str = ch          # 字符串开始
                elif ch == in_str:
                    in_str = None        # 字符串结束
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth < 0:
                    raise SyntaxError(f"WHERE 条件括号不匹配: {s}")
        if depth != 0:
            raise SyntaxError(f"WHERE 条件括号不匹配: {s}")
        # 未闭合的字符串字面量（`name = 'A`）同样是语法错误
        if in_str is not None:
            raise SyntaxError(f"WHERE 条件字符串未闭合: {s}")

    def _strip_outer_parens(self, s: str) -> str:
        """剥掉整段被一对平衡括号包住的外层括号

        只有「第一个 '(' 的配对 ')' 正好在末尾」时才剥，且可重复剥：
        `((a = 1 OR b = 2))` -> `a = 1 OR b = 2`。
        `(a = 1 OR b = 2) AND c = 3` 的配对 ')' 不在末尾，原样返回，
        括号的优先级覆盖交由 AND/OR 切分处理。
        """
        while True:
            t = s.strip()
            if not t.startswith('('):
                return t
            depth = 0
            end = -1
            for i, ch, quoted in self._scan_quotes(t):
                if quoted:
                    continue
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != len(t) - 1:
                return t
            s = t[1:end]

    def _split_cond_top_level(self, s: str, kw: str) -> List[str]:
        """按顶层的 AND / OR 切分条件文本（kw 传大写 'AND' / 'OR'）

        只切「圆括号深度 0 且不在字符串字面量里」的关键字，且关键字
        两侧必须是空白字符，避免切开 `band = 1` / `ORACLE` 这类标识符。
        未切到关键字时返回单元素列表（调用方据此判断是否有顶层运算符）。
        """
        kw = kw.upper()
        klen = len(kw)
        parts, start = [], 0
        depth = 0
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch in ("'", '"', '`'):
                # 跳过一个完整的字符串字面量
                j = i + 1
                while j < n and s[j] != ch:
                    j += 1
                i = j + 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif (depth == 0 and ch.isspace()
                    and s[i + 1:i + 1 + klen].upper() == kw
                    and i + 1 + klen < n
                    and s[i + 1 + klen].isspace()):
                parts.append(s[start:i].strip())
                start = i + 1 + klen
                i = start
                continue
            i += 1
        parts.append(s[start:].strip())
        return [p for p in parts if p]


# 全局解析器实例
parser = Parser()


# ==========================================================================
# GROUP BY 严格语义校验（等价 MySQL 的 ONLY_FULL_GROUP_BY）
#
# 背景（本次收紧的静默错答）：GROUP BY 之后引用**非分组键、且未被聚合**
# 的裸列时，两条执行路径原来都会悄悄取「组内第一行」的值当代表：
#   * 计划路径：`plan_executor._build_group_row` / `_eval_agg_expr`
#     （`row.append(grows[0][ci] if grows else None)`）；
#   * 统一路径：`executor._group_rows` 先把非分组键列从输出里丢掉，
#     随后由 `_project_rows` 报「列不存在」——报错原文完全看不出是
#     GROUP BY 的问题，用户改成 `MAX(name)` 之前根本猜不到原因。
# MySQL 非严格模式也是任取一行，同一份 SQL 换台服务器就可能换一组答案。
# 本项目「正确性优先」，因此改成与标准/MySQL 严格模式一致：**显式报错**。
#
# 实现方式（本函数是两条路径共用的**纯静态**校验，只吃解析后的 AST）：
#   粒度 = 三种位置 × 三种允许形态
#     位置：SELECT 投影列表 / HAVING / ORDER BY（外加 GROUP BY + 窗口函数）
#     允许：① 分组键本身（含 `u.city` 限定名与别名形态）；
#           ② 聚合函数作用于任意列；③ 与分组无关的常量/字面量。
#   * 认不出的形态（标量子查询投影、标量函数、窗口函数体）**不在静态层拦**：
#     引擎在运行时对它们本来就是显式报错（`_projection_item_plan` 的
#     「列不存在」），静态层再拦一次只会给出更容易误导的列名。
#   * 校验只在「聚合语境」下生效（有 GROUP BY / SELECT 里有聚合 / 有
#     HAVING）；没有聚合的普通查询、纯 DISTINCT 查询完全不经过这里。
#
# 逃生口（兼容旧行为，默认必须是严格）：
#   * 代码里：`Executor.strict_group_by = False`
#             （计划路径同名开关：`PlanExecutor.strict_group_by = False`）
#   * 环境变量：`LIGHTDB_STRICT_GROUP_BY=0`（false/off/no 同样关闭）
#   详见 `resolve_strict_group_by`。
#
# 已知限制（仍可能静默的形态，逐条在测试里标注）：
#   * `HAVING <聚合别名>`（`SELECT COUNT(*) AS c ... HAVING c > 1`）静态层
#     视为合法（MySQL 也允许），但存储层 `_col_index_of` 认不出聚合别名，
#     运行时会显式报「取不到」——不会静默，只是错误信息不如静态层精确。
#   * 标量函数/窗口函数体：静态层放行，运行时由 `_projection_item_plan` /
#     `_compute_window` 处理（前者显式报错；后者见 `validate_group_by`
#     对 `windows` 的校验——有 GROUP BY 时窗口键必须是分组键）。
# ==========================================================================

# 严格模式默认值（逃生口见 resolve_strict_group_by）
_STRICT_GROUP_BY_DEFAULT = True
_STRICT_GROUP_BY_ENV = 'LIGHTDB_STRICT_GROUP_BY'

# 聚合函数名（与 executor._AGG_FUNCS / optimizer._is_agg_expr 的超集保持一致）
_AGG_FUNC_NAMES = frozenset((
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'TOTAL', 'GROUP_CONCAT',
    'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
    'VARIANCE', 'VAR_POP', 'VAR_SAMP',
))

# `expr AS alias`（与 executor._split_alias 同一套写法）
_SELECT_ALIAS_RE = re.compile(r'^(.+?)\s+AS\s+[`"]?(\w+)[`"]?$', re.IGNORECASE)
# 整项就是一个常量
_LITERAL_RE = re.compile(
    r"^(?:-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|NULL|TRUE|FALSE)$", re.IGNORECASE)
# 函数调用头部（`COUNT(` / `LOWER(` / `COUNT (`）
_FUNC_HEAD_RE = re.compile(r'^\s*(\w+)\s*\(')
# 投影项以 `(SELECT` 开头 = 标量子查询
_SCALAR_SUB_RE = re.compile(r'^\(\s*SELECT\b', re.IGNORECASE)
# 表达式里的列标识符（可带表限定符）
_IDENT_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?')
# 比较右侧的「列引用」文本：裸标识符或反引号包裹的标识符（可带限定符）。
# 单/双引号包裹的一律视为**字符串字面量**（本引擎把双引号也当字符串），
# 数字 / NULL / TRUE / FALSE 同样排除。
_COL_REF_TEXT_RE = re.compile(
    r'^`?([A-Za-z_]\w*)`?(?:\.`?([A-Za-z_]\w*)`?)?$')


def _column_ref_text(raw_val):
    """比较右侧的原始文本是「列引用」时返回归一化列名，否则返回 None

    用于区分 `WHERE o.user_id = u.id`（右侧是列）与 `WHERE city = 'SH'`
    / `WHERE n = 5`（右侧是字面量）。后者不能加列解析标记，否则字符串
    常量会被拿去当列名解析。
    """
    t = str(raw_val or '').strip()
    if not t:
        return None
    up = t.upper()
    if up in ('NULL', 'TRUE', 'FALSE'):
        return None
    m = _COL_REF_TEXT_RE.match(t)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)


# 表达式里出现这些词不算列引用
_SQL_WORDS = frozenset((
    'SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'BY', 'HAVING', 'LIMIT',
    'AND', 'OR', 'NOT', 'NULL', 'TRUE', 'FALSE', 'AS', 'IN', 'IS', 'LIKE',
    'DISTINCT', 'BETWEEN', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'OVER',
    'PARTITION', 'ROWS', 'RANGE', 'UNION', 'ALL', 'JOIN', 'ON',
))


def resolve_strict_group_by(flag=None) -> bool:
    """解析「GROUP BY 严格模式」开关（逃生口）

    优先级（高 → 低）：
    1. 显式传入的 flag —— `Executor.strict_group_by` /
       `PlanExecutor.strict_group_by`（None 表示"没表态"，True/False 直接采用）；
    2. 环境变量 `LIGHTDB_STRICT_GROUP_BY`（0 / false / off / no / 空 关闭）；
    3. 默认**严格**（True）。
    """
    if flag is not None:
        return bool(flag)
    env = os.environ.get(_STRICT_GROUP_BY_ENV)
    if env is None:
        return _STRICT_GROUP_BY_DEFAULT
    return env.strip().lower() not in ('0', 'false', 'off', 'no', '')


def is_aggregate_expr(text) -> bool:
    """`SUM(x)` / `COUNT(*)` / `COUNT ( x )` -> True（别名不影响判定）"""
    expr, _ = _split_select_alias(text)
    m = _FUNC_HEAD_RE.match(expr)
    return bool(m) and m.group(1).upper() in _AGG_FUNC_NAMES


def _split_select_alias(text):
    """`SUM(x) AS t` -> ('SUM(x)', 't')；没有别名时返回 (原文, None)"""
    s = str(text).strip()
    m = _SELECT_ALIAS_RE.match(s)
    if m:
        return m.group(1).strip(), m.group(2)
    return s, None


def normalize_order_by(order_by) -> list:
    """把 `parsed['order_by']` 规范化成 `[(键文本, desc_bool), ...]`

    parser 现在的产出就是列表（`ORDER BY a DESC, b` → `[('a', True), ('b', False)]`），
    这里额外兼容历史上的**单键元组** `(键文本, desc)`：执行器 / 优化器 /
    计划执行器都通过本函数读取，因此单键与多键只有一套消费代码，
    不会出现「有的消费点还按元组解包」的半改状态。

    空 / None → `[]`（表示没有 ORDER BY）。
    """
    if not order_by:
        return []
    # 历史形态：单个 (键文本, desc) 元组
    if isinstance(order_by, (list, tuple)) and len(order_by) == 2 \
            and isinstance(order_by[0], str) \
            and isinstance(order_by[1], bool):
        return [(order_by[0], order_by[1])]
    out = []
    for item in order_by:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), bool(item[1])))
        else:
            out.append((str(item), False))
    return out


def _norm_ident(text) -> str:
    """列标识符归一化：去引号、去表限定符、小写（`u.City` -> `city`）"""
    return str(text).strip().strip('`"').rsplit('.', 1)[-1].strip().lower()


def _is_literal_expr(text) -> bool:
    return bool(_LITERAL_RE.match(_split_select_alias(text)[0]))


def _expr_idents(text) -> list:
    """取出表达式里真正被引用的列标识符（跳过函数名与 SQL 关键字）

    用于 `salary*2` 这类算术投影：只要它引用的列全是分组键就合法。
    """
    s = str(text)
    out = []
    for m in _IDENT_RE.finditer(s):
        if _FUNC_HEAD_RE.match(s[m.start():]):
            continue  # 函数名，不是列引用
        if m.group(0).upper() in _SQL_WORDS:
            continue
        out.append(m.group(0))
    return out


def group_by_violation_message(col, place='SELECT') -> str:
    """构造「非分组键裸列」的统一报错文案（列名 + 修复办法）

    两条执行路径（统一路径 / 计划路径）共用这一份文案，保证同一份 SQL
    在不同路径下报出的错误逐字一致。place 取值：
    'SELECT' / 'HAVING' / 'ORDER BY' / 'WINDOW' / 'AGG'。
    """
    if place == 'HAVING':
        # 沿用既有实现（executor._eval_condition / plan_executor._eval_condition）
        # 的「取不到」措辞，避免同一语义出现两套错误信息
        return (f"HAVING 里的列 '{col}' 取不到：它不是 GROUP BY 的分组键，"
                f"也没有被聚合函数包裹；GROUP BY 之后只保留分组键与聚合值。"
                f"请把 {col} 加入 GROUP BY，或用聚合函数包裹"
                f"（例如 MAX({col})）")
    if place == 'ORDER BY':
        return (f"ORDER BY 里的列 '{col}' 取不到：它不是 GROUP BY 的分组键，"
                f"也没有被聚合函数包裹，GROUP BY 之后每一组里它的取值不唯一。"
                f"请把 {col} 加入 GROUP BY，或用聚合函数包裹"
                f"（例如 MAX({col})），或改成按分组键/聚合值排序")
    if place == 'WINDOW':
        return (f"GROUP BY 与窗口函数无法协同：窗口函数引用的列 '{col}' 既不是"
                f"分组键也没有被聚合函数包裹，GROUP BY 之后取不到它的确定值。"
                f"请把 {col} 加入 GROUP BY，或用聚合函数包裹"
                f"（例如 MAX({col})）")
    if place == 'AGG':
        return (f"聚合查询里混用了非聚合列 '{col}'：没有 GROUP BY 时整张表聚成"
                f"一组，只能输出聚合值。请把 {col} 加入 GROUP BY，或用聚合"
                f"函数包裹（例如 MAX({col})）")
    return (f"GROUP BY 严格模式（ONLY_FULL_GROUP_BY）：SELECT 列表里的列 "
            f"'{col}' 取不到确定值 —— 它既不在 GROUP BY 里，也没有被聚合"
            f"函数包裹。请把 {col} 加入 GROUP BY，或用聚合函数包裹"
            f"（例如 MAX({col})）")


def _having_bare_columns(having) -> list:
    """收集 HAVING 条件树里直接引用的裸列（**不**钻进子查询）"""
    out = []
    if not isinstance(having, dict):
        return out
    col = having.get('column')
    if isinstance(col, str) and col.strip():
        out.append(col.strip())
    inner = having.get('not')
    if isinstance(inner, dict):
        out.extend(_having_bare_columns(inner))
    for key in ('and', 'or'):
        for c in (having.get(key) or []):
            out.extend(_having_bare_columns(c))
    return out


def validate_group_by(parsed, strict=True):
    """GROUP BY 静态语义校验（SELECT 投影 / HAVING / ORDER BY / 窗口）

    parsed: 解析器产出的 AST（也接受 plan_executor 用计划节点拼出的等价 dict）
    strict: False 时直接返回（逃生口，见 resolve_strict_group_by）

    不合法时抛 ValueError（执行器统一转成 SQL 错误返回），错误信息里带
    **列名**与**修复办法**（加入 GROUP BY 或用聚合函数包裹）。
    """
    if not strict or not isinstance(parsed, dict):
        return
    if parsed.get('command') not in (None, 'SELECT'):
        return

    group_raw = [str(g).strip() for g in (parsed.get('group_by') or [])]
    # 隐式分组：optimizer 对「无 GROUP BY 但有聚合」的查询会把裸列塞进
    # group_cols（`SELECT dept, SUM(s) FROM emp` -> GROUP BY dept）。这类
    # 分组键是执行器自己造的，不能算用户写的分组键，否则第 6 条验收
    # （无 GROUP BY 混用）会在计划路径上被静默放行。
    if parsed.get('implicit_group'):
        group_raw = []
    keys = {_norm_ident(g) for g in group_raw}

    columns = [str(c) for c in (parsed.get('columns') or [])]
    having = parsed.get('having')

    aggs, bare = [], []
    out_aliases = set()
    for raw in columns:
        expr, alias = _split_select_alias(raw)
        if alias:
            out_aliases.add(alias.strip().strip('`"').lower())
        if is_aggregate_expr(expr):
            aggs.append((expr, alias))
        elif expr == '*':
            bare.append((expr, alias))
        elif _is_literal_expr(expr):
            continue  # 常量投影与分组无关
        elif _SCALAR_SUB_RE.match(expr):
            continue  # 标量子查询投影：运行时单独校验（下同）
        elif _FUNC_HEAD_RE.match(expr):
            continue  # 标量/窗口函数体：运行时显式报错，静态层不猜
        else:
            bare.append((expr, alias))

    agg_context = bool(group_raw) or bool(aggs) or (having is not None)
    if not agg_context:
        return

    def _is_key(name) -> bool:
        return _norm_ident(name) in keys

    # ---------- ① SELECT 投影列表 ----------
    for expr, _alias in bare:
        if expr == '*':
            raise ValueError(
                "GROUP BY 严格模式（ONLY_FULL_GROUP_BY）：SELECT * 会展开出"
                "非分组键的列，GROUP BY 之后它们的取值不唯一。请显式只选"
                "分组键，或用聚合函数包裹其余列")
        idents = _expr_idents(expr)
        if idents:
            for ident in idents:
                if _is_key(ident):
                    continue
                # 无 GROUP BY（整表聚成一组）时任何裸列都不合法
                if not group_raw:
                    raise ValueError(
                        group_by_violation_message(ident, 'AGG'))
                raise ValueError(group_by_violation_message(ident))
        elif not group_raw:
            # 解析不出列引用的非字面量项：无 GROUP BY 时同样不合法
            raise ValueError(group_by_violation_message(expr, 'AGG'))

    # ---------- ② HAVING ----------
    for col in _having_bare_columns(having):
        n = _norm_ident(col)
        # 允许：分组键 / 聚合别名 / 任意输出别名（MySQL 同样允许 HAVING 引用别名）
        if _is_key(col) or n in out_aliases or n in keys:
            continue
        raise ValueError(group_by_violation_message(col, 'HAVING'))

    # ---------- ③ ORDER BY ----------
    # 排序键可以是分组键本身、输出别名、聚合表达式，或**只引用分组键**的
    # 表达式（`ORDER BY LENGTH(dept)`，dept 是分组键 → 组内取值确定）。
    # 引用非分组键裸列的表达式仍然显式报错（绝不取组内第一行当排序键）。
    # 多键 ORDER BY：**每个键**都要过同一份校验（原来只看 order[0]，
    # `ORDER BY dept, name` 里第二键的非法性会被静默放过）。
    order = parsed.get('order_by')
    for order_pos, (col, _desc) in enumerate(normalize_order_by(order)):
        if not (_is_key(col) or _norm_ident(col) in out_aliases
                or is_aggregate_expr(col)):
            node = parse_scalar_expr(_split_select_alias(col)[0])
            idents = expr_bare_columns(node) if node is not None else None
            if not (idents is not None
                    and all(_is_key(i) for i in idents)):
                # 报出「第几个排序键」，与执行器统一排序路径的文案风格一致
                raise ValueError(
                    f"ORDER BY 第 {order_pos + 1} 个排序键："
                    + group_by_violation_message(col, 'ORDER BY'))

    # ---------- ④ 有 GROUP BY 时的窗口函数 ----------
    for w in (parsed.get('windows') or []):
        if not isinstance(w, dict):
            continue
        order_cols = [c for c, _d in (w.get('order_by') or [])]
        arg_cols = [a for a in (w.get('args') or []) if str(a).strip() != '*']
        for c in (list(w.get('partition_by') or []) + order_cols + arg_cols):
            if _is_literal_expr(c) or is_aggregate_expr(c):
                continue
            if _is_key(c) or _norm_ident(c) in out_aliases:
                continue
            if not group_raw:
                raise ValueError(group_by_violation_message(c, 'AGG'))
            raise ValueError(group_by_violation_message(c, 'WINDOW'))


# ==========================================================================
# 标量表达式（单行函数 + 算术）—— 解析 / 渲染 / 求值
#
# 背景：`SELECT LOWER(name) FROM t` 原先整条投影被下推给存储层，存储层只认
# 列名 → 报「列 'LOWER(name)' 不存在」；`WHERE salary * 2 > 230` 连解析都
# 过不去（SyntaxError）。本次给引擎补上一层**单行标量函数 + 算术**能力，
# 覆盖 SELECT 投影 / WHERE / ORDER BY / GROUP BY / HAVING 五个位置。
#
# 实现方式（三层，本文件负责前两层）：
#   ① 解析：`parse_scalar_expr(text)` 用递归下降把文本编成节点 dict
#      （+ - * / 优先级、括号、一元负号、函数调用、字面量、列引用）；
#   ② 渲染：`render_scalar_expr(node)` 还原成规范 SQL 文本 —— 聚合表达式
#      （`SUM(v * 2)`）在 HAVING 里要按文本去 agg_values 里取值，必须有一份
#      「同一节点 ↔ 同一文本」的规范写法；
#   ③ 求值：`eval_expr_node` / `eval_expr_predicate` 是执行器（executor）与
#      计划执行器（plan_executor）**共用**的求值器，列解析由调用方以
#      `resolve(name)` 回调注入（单表 / JOIN 的列命名规则不同）。
#
# ★ 支持的标量函数清单（大小写不敏感，只此 7 个）：
#       LOWER(x)  UPPER(x)  LENGTH(x)  TRIM(x)  ABS(x)
#       ROUND(x[, n])      CONCAT(a, b, ...)   —— CONCAT 至少要 2 个参数
#   其它函数名（REVERSE / NOW / SUBSTRING / ...）**一律显式报错并回列本清单**
#   （见 `unsupported_function_message`），绝不静默返回 NULL，也绝不把函数名
#   当成列名去查询。
#
# ★ NULL 语义（SQL 标准，三值逻辑）：
#   * 任一参数为 NULL → 结果为 NULL（`LENGTH(NULL)`、`ABS(NULL)` 都是 NULL）；
#   * `CONCAT` 采用**标准语义**：任一参数为 NULL → 整体 NULL
#     （MySQL 的 CONCAT 也是这个语义；这里显式记录，避免与
#      `CONCAT_WS`（跳过 NULL）混淆 —— 本引擎未支持 CONCAT_WS）；
#   * 表达式谓词里任一侧是 NULL → 比较结果为 UNKNOWN（None），
#     行过滤时按「不通过」处理（与 SQL 的 WHERE 语义一致）。
#
# ★ 算术与除零：
#   * `+ - * /` 支持任意嵌套与括号；
#   * **除零返回 NULL**（与既有 `Executor._eval_arith` 对
#     `SELECT salary/0` 的行为一致，见 tests/test_v4_sql_features.py 的
#     「除零：显式返回 NULL 而不是崩溃」）——不抛异常、不崩溃；
#   * 类型不匹配（`'a' + 1`）**显式报错**，不静默给 NULL。
#
# ★ 已知限制：
#   * 只支持单行标量函数，没有 CASE / CAST / 字符串拼接 `||`；
#   * `COUNT(DISTINCT x)`、`SUM(x) FILTER (...)` 等形态解析不出表达式节点，
#     交回既有路径处理（那里会显式报错）；
#   * `LIKE` 不在表达式谓词里支持（`LOWER(name) LIKE 'a%'` 会显式
#     SyntaxError）。
# ==========================================================================

# 支持的单行标量函数名（**唯一清单**，报错文案与参数校验都取自这里）
SCALAR_FUNCTION_NAMES = ('LOWER', 'UPPER', 'LENGTH', 'TRIM', 'ABS',
                         'ROUND', 'CONCAT', 'JSON_EXTRACT', 'JSON_UNQUOTE')

# 每个标量函数的参数个数：(最少, 最多)，None 表示不限
SCALAR_FUNCTION_ARITY = {
    'LOWER': (1, 1),
    'UPPER': (1, 1),
    'LENGTH': (1, 1),
    'TRIM': (1, 1),
    'ABS': (1, 1),
    'ROUND': (1, 2),
    'CONCAT': (2, None),
    'JSON_EXTRACT': (2, 2),
    'JSON_UNQUOTE': (1, 1),
}

# 聚合函数名（与 executor._AGG_FUNCS / plan_executor._is_agg_expr 的超集一致）
AGGREGATE_FUNCTION_NAMES = _AGG_FUNC_NAMES

# 表达式词法：数字 / 字符串字面量 / 引号标识符 / 标识符 / 运算符
_EXPR_TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<num>\d+\.\d+|\d+)
    | (?P<str>'(?:[^']|'')*')
    | (?P<qid>`[^`]*`|"[^"]*")
    | (?P<ident>[A-Za-z_]\w*)
    | (?P<op><=|>=|<>|!=|\+|-|\*|/|\(|\)|,|\.)
""", re.VERBOSE)


def is_literal_text(text) -> bool:
    """文本是否是 SQL 字面量（数字 / 字符串 / NULL / TRUE / FALSE）"""
    return bool(_LITERAL_RE.match(str(text or '').strip()))


def _tokenize_expr(text):
    """把表达式文本切成 token 列表；出现无法识别的字符时返回 None"""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        m = _EXPR_TOKEN_RE.match(text, i)
        if m is None:
            return None
        i = m.end()
        if m.lastgroup == 'ws':
            continue
        tokens.append((m.lastgroup, m.group(0)))
    return tokens


class _ExprParser:
    """标量表达式递归下降解析器

    文法（只处理算术语义，比较运算由 `_split_top_level_comparison` 先切开）::

        expr    := term (('+' | '-') term)*
        term    := factor (('*' | '/') factor)*
        factor  := ('-' | '+') factor | primary
        primary := NUMBER | STRING | NULL | TRUE | FALSE
                 | FUNC '(' [args] ')' | COLUMN | '(' expr ')'
        args    := (expr | '*') (',' (expr | '*'))*
    """

    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.toks):
            return self.toks[self.pos]
        return (None, None)

    def accept_op(self, *ops):
        kind, val = self.peek()
        if kind == 'op' and val in ops:
            self.pos += 1
            return val
        return None

    def parse(self):
        node = self._expr()
        if node is None or self.pos != len(self.toks):
            return None
        return node

    def _expr(self):
        left = self._term()
        if left is None:
            return None
        while True:
            op = self.accept_op('+', '-')
            if op is None:
                return left
            right = self._term()
            if right is None:
                return None
            left = {'kind': 'arith', 'op': op,
                    'left': left, 'right': right}

    def _term(self):
        left = self._factor()
        if left is None:
            return None
        while True:
            op = self.accept_op('*', '/')
            if op is None:
                return left
            right = self._factor()
            if right is None:
                return None
            left = {'kind': 'arith', 'op': op,
                    'left': left, 'right': right}

    def _factor(self):
        sign = self.accept_op('-', '+')
        node = self._primary()
        if node is None:
            return None
        if sign == '-':
            if node.get('kind') == 'lit' \
                    and isinstance(node.get('value'), (int, float)) \
                    and not isinstance(node.get('value'), bool):
                return {'kind': 'lit', 'value': -node['value']}
            # 非字面量的负号：写成 `0 - x`，语义等价且求值器不必再处理
            return {'kind': 'arith', 'op': '-',
                    'left': {'kind': 'lit', 'value': 0}, 'right': node}
        return node

    def _primary(self):
        kind, val = self.peek()
        if kind == 'num':
            self.pos += 1
            return {'kind': 'lit',
                    'value': float(val) if '.' in val else int(val)}
        if kind == 'str':
            self.pos += 1
            return {'kind': 'lit', 'value': val[1:-1].replace("''", "'")}
        if kind == 'qid':
            self.pos += 1
            return self._qualified(val[1:-1])
        if kind == 'ident':
            self.pos += 1
            up = val.upper()
            if self.peek() == ('op', '('):
                self.pos += 1
                args = self._args()
                if args is None:
                    return None
                return {'kind': 'func', 'name': up, 'args': args}
            if up == 'NULL':
                return {'kind': 'lit', 'value': None}
            if up == 'TRUE':
                return {'kind': 'lit', 'value': True}
            if up == 'FALSE':
                return {'kind': 'lit', 'value': False}
            return self._qualified(val)
        if kind == 'op' and val == '(':
            self.pos += 1
            node = self._expr()
            if node is None or self.accept_op(')') is None:
                return None
            return node
        return None

    def _qualified(self, name):
        """`name` 或 `t.name`（点号 + 标识符）"""
        parts = [name]
        while self.peek() == ('op', '.'):
            if self.pos + 1 >= len(self.toks):
                return None
            nxt_kind, nxt_val = self.toks[self.pos + 1]
            if nxt_kind not in ('ident', 'qid'):
                return None
            parts.append(nxt_val.strip('`"'))
            self.pos += 2
        return {'kind': 'col', 'name': '.'.join(parts)}

    def _args(self):
        """函数参数列表（已吃掉左括号）；`*` 作为独立节点支持 `COUNT(*)`"""
        args = []
        if self.accept_op(')') is not None:
            return args
        while True:
            kind, val = self.peek()
            if kind == 'op' and val == '*':
                self.pos += 1
                args.append({'kind': 'star'})
            else:
                node = self._expr()
                if node is None:
                    return None
                args.append(node)
            if self.accept_op(',') is not None:
                continue
            if self.accept_op(')') is not None:
                return args
            return None


def parse_scalar_expr(text):
    """把文本解析成标量表达式节点；无法整体解析时返回 None

    返回的节点是纯 dict（可安全放进 AST / 缓存 / 比较）::

        {'kind': 'lit',   'value': 3}
        {'kind': 'col',   'name': 'salary'}          # 可带限定符 `e.salary`
        {'kind': 'star'}                              # 仅作为聚合参数 `*`
        {'kind': 'arith', 'op': '*', 'left': ..., 'right': ...}
        {'kind': 'func',  'name': 'LOWER', 'args': [...]}
    """
    t = str(text or '').strip()
    if not t:
        return None
    tokens = _tokenize_expr(t)
    if tokens is None:
        return None
    return _ExprParser(tokens).parse()


def render_scalar_expr(node) -> str:
    """把表达式节点还原成规范 SQL 文本（聚合取值键 / 报错文案用）

    规范化约定：二元运算两侧各一个空格、函数参数用 `, ` 分隔、字符串用单
    引号（内部单引号翻倍）。同一个节点渲染结果恒定，因此可以当字典键。
    """
    if not isinstance(node, dict):
        return str(node)
    kind = node.get('kind')
    if kind == 'lit':
        v = node.get('value')
        if v is None:
            return 'NULL'
        if v is True:
            return 'TRUE'
        if v is False:
            return 'FALSE'
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return repr(v)
    if kind == 'col':
        return str(node.get('name'))
    if kind == 'star':
        return '*'
    if kind == 'arith':
        return (f"{render_scalar_expr(node.get('left'))} "
                f"{node.get('op')} "
                f"{render_scalar_expr(node.get('right'))}")
    if kind == 'func':
        args = ', '.join(render_scalar_expr(a)
                         for a in (node.get('args') or []))
        return f"{node.get('name')}({args})"
    return str(node)


def expr_has_agg(node) -> bool:
    """表达式里是否出现聚合函数（`SUM(v * 2)` / `ROUND(AVG(v), 1)`）"""
    if not isinstance(node, dict):
        return False
    if node.get('kind') == 'func':
        if node.get('name') in AGGREGATE_FUNCTION_NAMES:
            return True
        return any(expr_has_agg(a) for a in (node.get('args') or []))
    if node.get('kind') == 'arith':
        return expr_has_agg(node.get('left')) or expr_has_agg(node.get('right'))
    return False


def expr_has_func(node) -> bool:
    """表达式里是否出现函数调用（标量或聚合）"""
    if not isinstance(node, dict):
        return False
    if node.get('kind') == 'func':
        return True
    if node.get('kind') == 'arith':
        return expr_has_func(node.get('left')) or expr_has_func(node.get('right'))
    return False


def expr_column_names(node, out=None) -> list:
    """表达式里引用的列名（保持出现顺序、可重复）"""
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    kind = node.get('kind')
    if kind == 'col':
        out.append(str(node.get('name')))
    elif kind == 'arith':
        expr_column_names(node.get('left'), out)
        expr_column_names(node.get('right'), out)
    elif kind == 'func':
        for a in (node.get('args') or []):
            expr_column_names(a, out)
    return out


def expr_columns_outside_agg(node, out=None) -> list:
    """表达式里**不在聚合函数内部**的列引用（相关性判定用）

    与 `expr_bare_columns` 的区别：标量函数的参数**算**引用 ——
    `LOWER(u.name)` 里的 `u.name` 是外层列引用，相关性判定必须看到它，
    否则相关子查询会被当成非相关一次性求值（静默错答）。
    聚合函数的参数**不算**：这与本引擎既有的 `{'agg': ...}` 编码一致
    （聚合表达式文本本来就不参与相关性判定），藏在聚合里的外层引用由
    `_leftover_outer_pattern` 的残留检查兜底并**显式报错**。
    """
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    kind = node.get('kind')
    if kind == 'col':
        out.append(str(node.get('name')))
    elif kind == 'arith':
        expr_columns_outside_agg(node.get('left'), out)
        expr_columns_outside_agg(node.get('right'), out)
    elif kind == 'func':
        if str(node.get('name') or '').upper() in AGGREGATE_FUNCTION_NAMES:
            return out
        for a in (node.get('args') or []):
            expr_columns_outside_agg(a, out)
    return out


def expr_bare_columns(node, out=None) -> list:
    """表达式里**不在聚合函数内部**的列引用（GROUP BY 校验用）

    `ROUND(AVG(v), 1)` → []（v 在聚合里，聚合值只有一个，与分组无关）；
    `LENGTH(dept)` → ['dept']（必须自己出现在 GROUP BY 里才合法）。
    """
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    kind = node.get('kind')
    if kind == 'col':
        out.append(str(node.get('name')))
    elif kind == 'arith':
        expr_bare_columns(node.get('left'), out)
        expr_bare_columns(node.get('right'), out)
    elif kind == 'func':
        if node.get('name') in AGGREGATE_FUNCTION_NAMES:
            return out
        for a in (node.get('args') or []):
            expr_bare_columns(a, out)
    return out


def unsupported_function_message(name) -> str:
    """不支持的函数名 → 显式报错文案（**必须回列支持清单**）"""
    return (f"不支持的函数 '{name}'：本引擎支持的**单行标量函数**是 "
            f"{' / '.join(SCALAR_FUNCTION_NAMES)}；聚合函数支持 "
            f"COUNT / SUM / AVG / MIN / MAX / TOTAL / GROUP_CONCAT / "
            f"STDDEV(_POP/_SAMP) / VARIANCE(VAR_POP/VAR_SAMP)。"
            f"其它函数名不会静默返回 NULL，也不会被当成列名")


def _norm_expr_key(text) -> str:
    """聚合取值键的宽松归一化（去空白 + 小写），用于容错匹配"""
    return re.sub(r'\s+', '', str(text or '')).lower()


def lookup_agg_value(agg_values, node):
    """按聚合成文本在 agg_values 里取值；取不到返回 _EXPR_MISSING"""
    if not isinstance(agg_values, dict):
        return _EXPR_MISSING
    text = render_scalar_expr(node)
    if text in agg_values:
        return agg_values[text]
    stripped = _strip_qualifiers_text(text)
    if stripped in agg_values:
        return agg_values[stripped]
    want = _norm_expr_key(text)
    for k, v in agg_values.items():
        if _norm_expr_key(k) == want:
            return v
    return _EXPR_MISSING


def _strip_qualifiers_text(text) -> str:
    """去掉列引用上的表限定符：`SUM(o.amount)` -> `SUM(amount)`"""
    return re.sub(r'\b\w+\.(\w+)', r'\1', str(text))


def apply_arith_values(left, op, right):
    """二元算术求值：NULL 传播 + 除零 NULL + 类型不匹配显式报错"""
    if left is None or right is None:
        return None
    try:
        if op == '+':
            return left + right
        if op == '-':
            return left - right
        if op == '*':
            return left * right
        if op == '/':
            if right == 0:
                # 除零 → NULL（与既有 `Executor._eval_arith` 的
                # `SELECT salary/0` 行为一致；不抛异常、不崩溃）
                return None
            return left / right
    except TypeError:
        raise ValueError(
            f"算术表达式类型不匹配: {left!r} {op} {right!r}")
    raise ValueError(f"不支持的算术运算符: {op}")


def apply_scalar_function(name, values):
    """执行单个标量函数（参数已求值）；NULL 传播与参数个数检查在这里"""
    name = str(name or '').upper()
    if name not in SCALAR_FUNCTION_ARITY:
        raise ValueError(unsupported_function_message(name))
    lo, hi = SCALAR_FUNCTION_ARITY[name]
    n = len(values)
    if n < lo or (hi is not None and n > hi):
        want = f"{lo}" if hi == lo else (
            f"{lo} 或 {hi}" if hi is not None else f"至少 {lo}")
        raise ValueError(
            f"函数 {name} 的参数个数不对：需要 {want} 个，实际 {n} 个")
    # ★ NULL 语义：任一参数为 NULL → 结果 NULL（CONCAT 也按标准返回 NULL）
    if any(v is None for v in values):
        return None
    try:
        if name == 'LOWER':
            return str(values[0]).lower()
        if name == 'UPPER':
            return str(values[0]).upper()
        if name == 'LENGTH':
            # 字符个数（与 SQLite / 标准 SQL 的 LENGTH 一致；MySQL 的
            # LENGTH 是字节数，中文场景会不同 —— 本引擎按字符数实现）
            return len(str(values[0]))
        if name == 'TRIM':
            return str(values[0]).strip()
        if name == 'ABS':
            return abs(values[0])
        if name == 'ROUND':
            digits = 0
            if len(values) == 2:
                digits = int(values[1])
            return _round_half_up(values[0], digits)
        if name == 'CONCAT':
            return ''.join(str(v) for v in values)
        if name == 'JSON_EXTRACT':
            # `col->'$.k'` 的改写目标。JSON 文本（或已解析的 dict/list）按
            # JSON 路径取值；**键/下标不存在 → NULL**（MySQL 标准语义，
            # 不是空串、不是报错）。json_func.json_extract 已实现该语义。
            from .json_func import json_extract
            return json_extract(values[0], str(values[1]))
        if name == 'JSON_UNQUOTE':
            # `col->>'$.k'` 的改写目标：把 JSON 值转成 TEXT。字符串原样
            # （JSON 的引号已由 json_extract 去掉），对象/数组转紧凑 JSON
            # 文本，布尔转 true/false，数字转十进制文本。
            import json as _json
            v = values[0]
            if isinstance(v, (dict, list)):
                return _json.dumps(v, ensure_ascii=False,
                                   separators=(',', ':'))
            if isinstance(v, bool):
                return 'true' if v else 'false'
            return str(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"函数 {name} 的参数不合法: {exc}")
    raise ValueError(unsupported_function_message(name))


def _round_half_up(x, digits):
    """SQL 语义的 ROUND：四舍五入（**half away from zero**）

    不能用 Python 内建 `round` —— 它是「银行家舍入」（round-half-even），
    `round(2.5) == 2`、`round(0.125, 2) == 0.12`，与 SQL 的
    `ROUND(2.5) == 3`、`ROUND(0.125, 2) == 0.13` 不一致，属于静默算错。
    这里用 Decimal + ROUND_HALF_UP 保证与 SQL 一致。
    """
    from decimal import Decimal, ROUND_HALF_UP
    d = Decimal(str(x))
    # digits >= 0 → 量化到 10^-digits；digits < 0 → 量化到 10^|digits|
    q = Decimal(1).scaleb(-int(digits))
    out = d.quantize(q, rounding=ROUND_HALF_UP)
    if digits <= 0:
        return int(out)
    return float(out)


def _coerce_compare_pair(left, right):
    """比较前的类型对齐：数字列 vs 字符串字面量（`LENGTH(name) > '3'`）"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left, right
    try:
        if isinstance(left, (int, float)) and isinstance(right, str):
            return left, float(right) if '.' in right else int(right)
        if isinstance(right, (int, float)) and isinstance(left, str):
            return (float(left) if '.' in left else int(left)), right
    except (TypeError, ValueError):
        pass
    return left, right


def compare_values(left, op, right):
    """比较运算求值；任一侧 NULL → None（UNKNOWN，三值逻辑）"""
    if left is None or right is None:
        return None
    left, right = _coerce_compare_pair(left, right)
    try:
        if op == '=':
            return left == right
        if op == '!=':
            return left != right
        if op == '<':
            return left < right
        if op == '>':
            return left > right
        if op == '<=':
            return left <= right
        if op == '>=':
            return left >= right
    except TypeError:
        raise ValueError(f"无法比较 {left!r} {op} {right!r}：类型不兼容")
    raise ValueError(f"不支持的比较运算符: {op}")


# 表达式求值里「聚合值取不到」的哨兵
_EXPR_MISSING = object()


def eval_expr_node(node, resolve, agg_values=None, position=''):
    """求值标量表达式节点

    resolve: 列解析回调 `resolve(name) -> value`，解析不到时**必须抛
             ValueError**（由调用方给出该路径下的准确措辞）。
    agg_values: HAVING/分组语境下预先算好的聚合值
             `{'SUM(v)': 320, ...}`；聚合节点按 `render_scalar_expr` 的
             规范文本去查（并做去限定符 / 去空白的容错匹配）。
    position: 报错前缀（'WHERE' / 'HAVING' / 'ORDER BY' / 'GROUP BY' /
              '投影'），只影响错误文案。
    """
    if not isinstance(node, dict):
        return node
    pos = f"{position} " if position else ""
    kind = node.get('kind')
    if kind == 'lit':
        return node.get('value')
    if kind == 'star':
        raise ValueError(
            f"{pos}里的 '*' 只能用作 COUNT(*) 的参数，不能单独求值")
    if kind == 'col':
        return resolve(str(node.get('name')))
    if kind == 'arith':
        left = eval_expr_node(node.get('left'), resolve, agg_values, position)
        right = eval_expr_node(node.get('right'), resolve, agg_values, position)
        return apply_arith_values(left, node.get('op'), right)
    if kind == 'func':
        name = str(node.get('name') or '').upper()
        if name in AGGREGATE_FUNCTION_NAMES:
            got = lookup_agg_value(agg_values, node)
            if got is _EXPR_MISSING:
                raise ValueError(
                    f"{pos}里的聚合函数 {render_scalar_expr(node)} 在这里"
                    f"取不到值：聚合值只在分组语境（GROUP BY / 整表聚合的"
                    f"SELECT 列表或 HAVING）里可用")
            return got
        values = [eval_expr_node(a, resolve, agg_values, position)
                  for a in (node.get('args') or [])]
        return apply_scalar_function(name, values)
    raise ValueError(f"{pos}无法求值的表达式节点: {render_scalar_expr(node)}")


def eval_expr_predicate(pred, resolve, agg_values=None, position=''):
    """求值 `表达式 比较 表达式` 谓词，返回 True / False / None(UNKNOWN)"""
    if not isinstance(pred, dict):
        raise ValueError(f"{position} 里的表达式谓词形态不合法: {pred!r}")
    left = eval_expr_node(pred.get('left'), resolve, agg_values, position)
    right = eval_expr_node(pred.get('right'), resolve, agg_values, position)
    return compare_values(left, pred.get('op'), right)


def aggregate_values(name, vals):
    """对**已求值并已剔除 NULL**的参数列表做聚合（两条执行路径共用）

    `vals` 为空表示「空集或全 NULL」：SUM/AVG/MIN/MAX/STDDEV/VARIANCE 系列
    都是 NULL（标准语义），`TOTAL` 按 SQLite 是 0.0。
    """
    name = str(name or '').upper()
    if not vals:
        return 0.0 if name == 'TOTAL' else None
    try:
        if name == 'SUM':
            return sum(vals)
        if name == 'TOTAL':
            return float(sum(vals))
        if name == 'AVG':
            return sum(vals) / len(vals)
        if name == 'MIN':
            return min(vals)
        if name == 'MAX':
            return max(vals)
        if name == 'GROUP_CONCAT':
            return ','.join(str(v) for v in vals)
        if name in ('STDDEV', 'STDDEV_POP', 'VARIANCE', 'VAR_POP'):
            import math
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            return var if name in ('VARIANCE', 'VAR_POP') else math.sqrt(var)
        if name in ('STDDEV_SAMP', 'VAR_SAMP'):
            import math
            if len(vals) < 2:
                return None
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
            return var if name == 'VAR_SAMP' else math.sqrt(var)
    except TypeError as exc:
        raise ValueError(f"聚合函数 {name} 的参数类型不匹配: {exc}")
    raise ValueError(unsupported_function_message(name))


def eval_agg_expr_over_rows(node, rows, resolve_row, position='', strict=True):
    """在**行集合**上求值「含聚合函数的表达式」（两条执行路径共用）

    * 聚合节点（`SUM(x)` / `COUNT(*)` / `GROUP_CONCAT(x)` / `ROUND(AVG(v),1)`
      里的 AVG）→ 对集合求值；聚合**参数**逐行求值（所以
      `SUM(salary * 2)`、`COUNT(ABS(v))` 都能算）；
    * 标量函数 / 算术节点 → 递归后套用共用求值器（NULL 传播、除零 NULL、
      不支持函数报错完全一致）；
    * 裸列节点：组内取值必须唯一（等价于分组键），否则 `strict=True` 时
      显式报错（`group_by_violation_message`），绝不取「组内第一行」。

    resolve_row(row, name) -> 值；解析不到时**必须抛 ValueError**。
    """
    if not isinstance(node, dict):
        return node
    kind = node.get('kind')
    if kind == 'lit':
        return node.get('value')
    if kind == 'star':
        raise ValueError(
            f"{position} 里的 '*' 只能用作 COUNT(*) 的参数")
    if kind == 'col':
        name = str(node.get('name'))
        vals = [resolve_row(r, name) for r in rows]
        first = vals[0] if vals else None
        if all(v == first for v in vals) or not strict:
            return first
        raise ValueError(group_by_violation_message(name))
    if kind == 'arith':
        left = eval_agg_expr_over_rows(node.get('left'), rows, resolve_row,
                                       position, strict)
        right = eval_agg_expr_over_rows(node.get('right'), rows, resolve_row,
                                        position, strict)
        return apply_arith_values(left, node.get('op'), right)
    if kind == 'func':
        name = str(node.get('name') or '').upper()
        args = node.get('args') or []
        if name in AGGREGATE_FUNCTION_NAMES:
            if not args:
                raise ValueError(
                    f"聚合函数 {name} 缺少参数（要写成 {name}(*) 或 "
                    f"{name}(列)）")
            star = args[0].get('kind') == 'star'

            def arg_of(row):
                return eval_expr_node(
                    args[0], lambda n, _r=row: resolve_row(_r, n),
                    None, position)

            if name == 'COUNT':
                return len(rows) if star \
                    else sum(1 for r in rows if arg_of(r) is not None)
            if star:
                raise ValueError(f"聚合函数 {name} 不接受 `*` 参数")
            return aggregate_values(
                name, [v for v in (arg_of(r) for r in rows) if v is not None])
        values = [eval_agg_expr_over_rows(a, rows, resolve_row, position,
                                          strict)
                  for a in args]
        return apply_scalar_function(name, values)
    raise ValueError(
        f"{position} 里无法求值的表达式节点: {render_scalar_expr(node)}")


def expr_agg_texts(node, out=None) -> list:
    """表达式里**最外层**聚合函数的规范文本（HAVING 取值键用）

    `SUM(v) * 2 > 100` → ['SUM(v)']；`ROUND(AVG(v), 1)` → ['AVG(v)']
    （外层 ROUND 不是聚合，继续往里找）。
    """
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    kind = node.get('kind')
    if kind == 'func':
        if str(node.get('name') or '').upper() in AGGREGATE_FUNCTION_NAMES:
            out.append(render_scalar_expr(node))
            return out
        for a in (node.get('args') or []):
            expr_agg_texts(a, out)
    elif kind == 'arith':
        expr_agg_texts(node.get('left'), out)
        expr_agg_texts(node.get('right'), out)
    return out


def expr_pred_agg_texts(pred) -> list:
    """表达式谓词里的聚合文本（`{'expr_pred': {...}}` → ['SUM(v)']）"""
    if not isinstance(pred, dict):
        return []
    out = []
    for node in (pred.get('left'), pred.get('right')):
        expr_agg_texts(node, out)
    return out


def row_agg_values_map(cols, row):
    """分组结果行上可用的聚合值映射 `{聚合成文本: 值}`

    分组之后列名列表里本来就摆着聚合表达式文本（`SUM(salary)`），
    这里把它们按 `render_scalar_expr` 的规范文本做成键，供
    `eval_expr_node` 处理 `ORDER BY ROUND(AVG(v), 1)` 这类表达式时取聚合值。
    """
    out = {}
    for i, c in enumerate(cols or []):
        text = str(c)
        if not re.match(r'^\w+\s*\(', text.strip()):
            continue
        node = parse_scalar_expr(_split_select_alias(text)[0])
        if node is None or not expr_has_agg(node):
            continue
        val = row[i] if i < len(row) else None
        out[render_scalar_expr(node)] = val
        out[text] = val
    return out