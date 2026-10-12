"""
SQL 解析器 - 正则驱动（修复版）

修复:
- 移除重复的 Parser 类定义
- 移除重复的 _column_def（保留完整版）
- 移除重复的 _select（保留完整版）
"""
import re
from typing import Optional, List, Dict, Any


class Parser:
    """SQL 解析器"""

    def parse(self, sql: str) -> Optional[Dict]:
        sql = sql.strip().rstrip(';').strip()
        if not sql:
            return None

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
            inner = self.parse(sql[len('EXPLAIN'):].strip())
            if inner is None:
                raise SyntaxError("EXPLAIN 需要跟随一条 SQL")
            return {'command': 'EXPLAIN', 'inner': inner}

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
            }
            windows.append(win)
            projection.append(('win', win))
        return base, windows, projection

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

    def _select(self, sql: str) -> Dict:
        m = re.match(
            r'SELECT\s+(.+?)\s+FROM\s+(.+)$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("SELECT 语法错误")

        cols_str = m.group(1).strip()
        from_rest = m.group(2).strip()

        # DISTINCT
        distinct = False
        if cols_str.upper().startswith('DISTINCT'):
            distinct = True
            cols_str = cols_str[len('DISTINCT'):].strip()

        # FROM：子查询 or 主表
        subquery = None
        main_table = None
        main_alias = None

        if from_rest.startswith('('):
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
            alias = am.group(1) if am else 'sub'
            tail = tail[am.end():] if am else tail
            subquery = {
                'sql': inner_sql, 'alias': alias, 'rest': tail,
            }
            rest = ''
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
            om = re.match(
                r'\s+ON\s+(.+?)(?=\s+(?:INNER|LEFT|RIGHT|CROSS)?\s*(?:OUTER\s+)?JOIN\b|\s+WHERE\b|\s+GROUP\b|\s+ORDER\b|\s+LIMIT\b|$)',
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
        raw_columns = (['*'] if cols_str == '*'
                       else self._split_top_level(cols_str))
        columns, windows, projection = self._split_windows(raw_columns)

        # WHERE / GROUP / HAVING / ORDER / LIMIT
        where = None
        order_by = None
        limit = None
        group_by = None
        having = None

        wm = re.search(
            r'\bWHERE\s+(.+?)(?=\s+(?:GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING)\b|$)',
            rest, re.IGNORECASE | re.DOTALL)
        if wm:
            where = self._where(wm.group(1))

        gm = re.search(
            r'\bGROUP\s+BY\s+(.+?)(?=\s+(?:HAVING|ORDER\s+BY|LIMIT)\b|$)',
            rest, re.IGNORECASE | re.DOTALL)
        if gm:
            group_by = [
                c.strip()
                for c in self._split_top_level(gm.group(1))
            ]

        hm = re.search(
            r'\bHAVING\s+(.+?)(?=\s+(?:ORDER\s+BY|LIMIT)\b|$)',
            rest, re.IGNORECASE | re.DOTALL)
        if hm:
            having = self._where(hm.group(1))

        om = re.search(
            r'ORDER\s+BY\s+[`"]?([\w.]+)[`"]?(?:\s+(ASC|DESC))?',
            rest, re.IGNORECASE)
        if om:
            order_by = (
                om.group(1),
                (om.group(2) or '').upper() == 'DESC')

        lm = re.search(
            r'LIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?',
            rest, re.IGNORECASE)
        if lm:
            limit = int(lm.group(1))

        return {
            'command': 'SELECT',
            'table': main_table,
            'table_alias': main_alias,
            'subquery': subquery,
            'joins': joins,
            'columns': columns,
            'windows': windows,
            'projection': projection,
            'distinct': distinct,
            'where': where,
            'group_by': group_by,
            'having': having,
            'order_by': order_by,
            'limit': limit,
        }

    # ==================== WHERE ====================

    def _where(self, s: str) -> Dict:
        s = s.strip()

        or_parts = re.split(r'\s+OR\s+', s, flags=re.IGNORECASE)
        if len(or_parts) > 1:
            return {'or': [self._where(p) for p in or_parts]}

        and_parts = re.split(r'\s+AND\s+', s,
                             flags=re.IGNORECASE)
        if len(and_parts) > 1:
            return {'and': [self._where(p) for p in and_parts]}

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

        # IN (v1, v2)
        im2 = re.match(
            r'[`"]?([\w.]+)[`"]?\s+(NOT\s+)?IN\s*\(\s*(.+?)\s*\)\s*$',
            s, re.IGNORECASE | re.DOTALL)
        if im2:
            values = self._split_top_level(im2.group(3))
            return {
                'column': im2.group(1),
                'op': 'NOT IN' if im2.group(2) else 'IN',
                'values': [
                    v.strip().strip("'\"") for v in values
                ],
            }

        # 聚合谓词（HAVING 的常见形式）：
        #   COUNT(*) > 1 / SUM(amount) >= 100 / COUNT(id) < 3
        # 左值是聚合函数而不是普通列，_where 原先直接抛 SyntaxError，
        # 导致 `... GROUP BY city HAVING COUNT(*) > 5` 连解析都过不去。
        # 产出 {'agg': 'COUNT(*)', 'op': '>', 'value': 1}，聚合值由
        # GroupBy / 统一分组路径用 _eval_agg_expr 求出后再比较。
        am = re.match(
            r'\s*(\w+)\s*\(\s*(\*|[^()]*?)\s*\)\s*'
            r'(=|!=|<>|<=|>=|<|>)\s*(.+?)\s*$',
            s, re.IGNORECASE)
        if am:
            op = am.group(3).upper()
            if op == '<>':
                op = '!='
            return {
                'agg': f'{am.group(1).upper()}({am.group(2).strip()})',
                'op': op,
                'value': am.group(4).strip().strip("'\""),
            }

        # 普通比较
        m = re.match(
            r'\s*[`"]?([\w.]+)[`"]?\s*(=|!=|<>|<=|>=|<|>|LIKE)\s*(.+?)\s*$',
            s, re.IGNORECASE)
        if m:
            op = m.group(2).upper()
            if op == '<>':
                op = '!='
            val = m.group(3).strip().strip("'\"")
            return {
                'column': m.group(1), 'op': op, 'value': val,
            }

        raise SyntaxError(f"WHERE 条件错误: {s}")

    # ==================== DDL ====================

    def _create_table(self, sql: str) -> Dict:
        m = re.match(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?\s*\((.*)\)\s*(.*)$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("CREATE TABLE 语法错误")

        table = m.group(1)
        body = m.group(2)
        tail = m.group(3).strip()

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

        # 生成列
        generated = None
        gm = re.search(
            r'GENERATED\s+ALWAYS\s+AS\s*\(\s*(.+?)\s*\)(?:\s+(STORED|VIRTUAL))?',
            rest, re.IGNORECASE | re.DOTALL)
        if gm:
            generated = {
                'expr': gm.group(1).strip(),
                'stored': (gm.group(2) or 'VIRTUAL').upper() == 'STORED',
            }

        # CHECK
        check = None
        cm = re.search(
            r'CHECK\s*\(\s*(.+?)\s*\)',
            rest, re.IGNORECASE | re.DOTALL)
        if cm:
            check = {'expr': cm.group(1).strip()}

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
        return {'command': 'DROP TABLE', 'table': m.group(1)}

    # ==================== DML ====================

    def _insert(self, sql: str) -> Dict:
        m = re.match(
            r'INSERT\s+INTO\s+[`"]?(\w+)[`"]?\s*(?:\(([^)]+)\))?\s+VALUES\s+(.+)',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            raise SyntaxError("INSERT 语法错误")

        table = m.group(1)
        cols = None
        if m.group(2):
            cols = [
                c.strip().strip('`"')
                for c in m.group(2).split(',')
            ]

        values = []
        for vm in re.finditer(r'\(([^)]*)\)', m.group(3)):
            values.append(self._value_row(vm.group(1)))

        return {
            'command': 'INSERT',
            'table': table,
            'columns': cols,
            'values': values,
        }

    def _value_row(self, s: str) -> List:
        values, current, in_str = [], '', None
        for ch in s:
            if in_str:
                if ch == in_str:
                    in_str = None
                    values.append(current)
                    current = ''
                else:
                    current += ch
            elif ch in ("'", '"'):
                in_str = ch
            elif ch == ',':
                if current.strip():
                    values.append(current.strip())
                current = ''
            else:
                current += ch
        if current.strip():
            values.append(current.strip())

        return [
            None if v.upper() == 'NULL' else v for v in values
        ]

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
        m = re.match(
            r'[`"]?([\w.]+)[`"]?\s*=\s*[`"]?([\w.]+)[`"]?\s*$',
            s.strip())
        if m:
            return {'left': m.group(1), 'right': m.group(2)}
        return None

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


# 全局解析器实例
parser = Parser()