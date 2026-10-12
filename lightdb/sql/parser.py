"""
SQL 解析器 - 正则驱动（修复版）

修复:
- 移除重复的 Parser 类定义
- 移除重复的 _column_def（保留完整版）
- 移除重复的 _select（保留完整版）
"""
import re
from typing import Optional, List, Dict, Any

# `FROM (子查询)` 之后的词若不是别名就一定是这些子句关键字，
# 用于避免把 `FROM (SELECT ...) WHERE ...` 里的 WHERE 误当别名
_FROM_SUBQUERY_KEYWORDS = {
    'WHERE', 'GROUP', 'ORDER', 'LIMIT', 'HAVING', 'JOIN', 'INNER',
    'LEFT', 'RIGHT', 'CROSS', 'OUTER', 'FULL', 'UNION', 'ON', 'AS',
}


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

    def _select(self, sql: str) -> Dict:
        no_from = False
        m = re.match(
            r'SELECT\s+(.+?)\s+FROM\s+(.+)$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            # FROM-less SELECT：递归 CTE 的 base 分支常见写法
            # （`WITH RECURSIVE n AS (SELECT 1 AS n UNION ALL ...)`），
            # 原先直接 SyntaxError，导致整个递归 CTE 用不了
            m2 = re.match(r'SELECT\s+(.+)$', sql,
                          re.IGNORECASE | re.DOTALL)
            if not m2:
                raise SyntaxError("SELECT 语法错误")
            no_from = True

        if no_from:
            cols_str = m2.group(1).strip()
            from_rest = None
        else:
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

        已知限制：只做 AND/OR 两级，不做 NOT / XOR / IS NULL 等谓词
        （原实现也没有，保持原状）。
        """
        s = s.strip()
        self._check_paren_balance(s)

        s = self._strip_outer_parens(s)

        or_parts = self._split_cond_top_level(s, 'OR')
        if len(or_parts) > 1:
            return {'or': [self._where(p) for p in or_parts]}

        and_parts = self._split_cond_top_level(s, 'AND')
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