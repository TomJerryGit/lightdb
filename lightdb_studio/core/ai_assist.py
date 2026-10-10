"""
AI 辅助
- 自然语言 → SQL（基于规则 + 模板匹配）
- SQL 解释（中文）
- SQL 优化建议
- 表关系推测
"""
import re
from typing import List, Dict, Optional


class SQLAssistant:
    """基于规则的 SQL 助手"""

    # 数字/中文数字
    NUM_MAP = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
               '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

    def __init__(self, schema_provider=None):
        """
        schema_provider: 返回 (tables, columns_by_table)
        """
        self.schema_provider = schema_provider

    def _get_schema(self):
        if self.schema_provider is None:
            return [], {}
        try:
            return self.schema_provider()
        except Exception:
            return [], {}

    # ==================== 自然语言 → SQL ====================

    def nl2sql(self, text: str) -> str:
        """自然语言转 SQL（基于规则）"""
        text = text.strip()
        if not text:
            return ""

        tables, columns = self._get_schema()

        # 找表名
        found_table = self._find_table(text, tables)

        # 分析意图
        intent = self._detect_intent(text)

        if intent == 'count':
            return self._build_count(text, found_table)
        if intent == 'insert':
            return self._build_insert_hint(text, found_table)
        if intent == 'update':
            return self._build_update_hint(text, found_table)
        if intent == 'delete':
            return self._build_delete_hint(text, found_table)
        if intent == 'list_tables':
            return "SHOW TABLES;"
        if intent == 'describe':
            if found_table:
                return f"SHOW COLUMNS FROM `{found_table}`;"
            return "-- 未识别到表名\nSHOW COLUMNS FROM `table_name`;"
        if intent == 'create':
            return self._build_create_hint(text, found_table)

        # 默认 SELECT
        return self._build_select(text, found_table, columns)

    def _find_table(self, text: str, tables: List[str]) -> Optional[str]:
        for t in tables:
            if t.lower() in text.lower():
                return t
        return None

    def _detect_intent(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in ['多少条', '多少行', '数量', '总数', '统计', 'count']):
            return 'count'
        if any(k in t for k in ['插入', '添加', '新增', 'insert']):
            return 'insert'
        if any(k in t for k in ['更新', '修改', 'update', '改为']):
            return 'update'
        if any(k in t for k in ['删除', '删掉', '移除', 'delete', 'remove']):
            return 'delete'
        if any(k in t for k in ['有哪些表', '所有表', '表列表', 'show tables', '列表']):
            return 'list_tables'
        if any(k in t for k in ['结构', '列信息', '字段', 'describe', 'desc']):
            return 'describe'
        if any(k in t for k in ['创建表', '新建表', '建表', 'create table']):
            return 'create'
        return 'select'

    def _parse_number(self, text: str) -> Optional[int]:
        # 阿拉伯数字
        m = re.search(r'(\d+)', text)
        if m:
            return int(m.group(1))
        # 中文数字
        for cn, n in self.NUM_MAP.items():
            if cn in text:
                return n
        return None

    def _extract_limit(self, text: str) -> Optional[int]:
        m = re.search(r'(?:前|头|first|top)\s*(\d+)', text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r'(\d+)\s*(?:条|行|个)', text)
        if m:
            return int(m.group(1))
        return None

    def _build_count(self, text: str, table: Optional[str]) -> str:
        if not table:
            return "-- 未识别到表名\nSELECT COUNT(*) FROM `table_name`;"
        cond = self._extract_condition(text, table)
        sql = f"SELECT COUNT(*) AS cnt FROM `{table}`"
        if cond:
            sql += f"\nWHERE {cond}"
        return sql + ";"

    def _build_select(self, text: str, table: Optional[str],
                      columns: Dict) -> str:
        if not table:
            return ("-- 未识别到表名\n"
                    "-- 可用表: (从左侧对象树查看)\n"
                    "SELECT * FROM `table_name` LIMIT 100;")

        # 提取排序列
        order_by = None
        m = re.search(r'(?:按|根据)\s*(\w+)\s*(?:降序|倒序|desc)', text, re.IGNORECASE)
        if m:
            order_by = (m.group(1), 'DESC')
        else:
            m = re.search(r'(?:按|根据)\s*(\w+)\s*(?:升序|asc)?', text, re.IGNORECASE)
            if m:
                order_by = (m.group(1), 'ASC')

        limit = self._extract_limit(text) or 100
        cond = self._extract_condition(text, table)

        sql = f"SELECT * FROM `{table}`"
        if cond:
            sql += f"\nWHERE {cond}"
        if order_by:
            sql += f"\nORDER BY `{order_by[0]}` {order_by[1]}"
        sql += f"\nLIMIT {limit};"
        return sql

    def _extract_condition(self, text: str, table: str) -> Optional[str]:
        """提取 WHERE 条件"""
        conditions = []

        # 数值条件：age > 20, age 大于 20
        m = re.search(
            r'(\w+)\s*(?:大于|>|超过)\s*(\d+(?:\.\d+)?)', text)
        if m:
            conditions.append(f"`{m.group(1)}` > {m.group(2)}")

        m = re.search(
            r'(\w+)\s*(?:小于|<|低于)\s*(\d+(?:\.\d+)?)', text)
        if m:
            conditions.append(f"`{m.group(1)}` < {m.group(2)}")

        m = re.search(
            r'(\w+)\s*(?:等于|是|=|==)\s*(\d+(?:\.\d+)?)', text)
        if m:
            conditions.append(f"`{m.group(1)}` = {m.group(2)}")

        # 字符串条件：name 是 'Alice', name = Alice
        m = re.search(
            r'(\w+)\s*(?:等于|是|=|==)\s*[\'"]?([\u4e00-\u9fff\w]+)[\'"]?',
            text)
        if m and not m.group(2).isdigit():
            conditions.append(f"`{m.group(1)}` = '{m.group(2)}'")

        # LIKE：name 包含 x
        m = re.search(r'(\w+)\s*(?:包含|含有|like)\s*[\'"]?([\w\u4e00-\u9fff]+)[\'"]?',
                      text, re.IGNORECASE)
        if m:
            conditions.append(f"`{m.group(1)}` LIKE '%{m.group(2)}%'")

        return ' AND '.join(conditions) if conditions else None

    def _build_insert_hint(self, text: str, table: Optional[str]) -> str:
        t = table or 'table_name'
        return (f"-- 插入语句（请填写具体值）\n"
                f"INSERT INTO `{t}` (`col1`, `col2`) VALUES ('v1', 'v2');")

    def _build_update_hint(self, text: str, table: Optional[str]) -> str:
        t = table or 'table_name'
        return (f"-- 更新语句（请确认 WHERE 条件）\n"
                f"UPDATE `{t}` SET `col` = 'new_value' WHERE `id` = 1;")

    def _build_delete_hint(self, text: str, table: Optional[str]) -> str:
        t = table or 'table_name'
        return (f"-- ⚠️ 删除语句（请谨慎使用！）\n"
                f"DELETE FROM `{t}` WHERE `id` = 1;")

    def _build_create_hint(self, text: str, table: Optional[str]) -> str:
        t = table or 'new_table'
        return (f"CREATE TABLE `{t}` (\n"
                f"  `id` INT PRIMARY KEY,\n"
                f"  `name` VARCHAR(100),\n"
                f"  `created_at` DATETIME\n"
                f");")

    # ==================== SQL 解释 ====================

    def explain_sql(self, sql: str) -> str:
        """解释 SQL 语句（中文）"""
        sql = sql.strip().rstrip(';')
        up = sql.upper()

        if up.startswith('SELECT'):
            return self._explain_select(sql)
        if up.startswith('INSERT'):
            return self._explain_insert(sql)
        if up.startswith('UPDATE'):
            return self._explain_update(sql)
        if up.startswith('DELETE'):
            return self._explain_delete(sql)
        if up.startswith('CREATE TABLE'):
            return self._explain_create(sql)
        if up.startswith('WITH'):
            return "**CTE 查询**：先定义临时结果集（WITH），再引用它查询。"
        if up.startswith('EXPLAIN'):
            return "**执行计划**：显示数据库如何执行该查询（不使用索引/走索引等）。"
        return "无法识别的 SQL 类型。"

    def _explain_select(self, sql: str) -> str:
        lines = ["**SELECT 查询**", ""]

        m = re.search(r'SELECT\s+(.+?)\s+FROM', sql, re.IGNORECASE | re.DOTALL)
        if m:
            cols = m.group(1).strip()
            if cols == '*':
                lines.append("• 查询**所有列**")
            else:
                lines.append(f"• 查询列: `{cols}`")

        m = re.search(r'FROM\s+(\w+)', sql, re.IGNORECASE)
        if m:
            lines.append(f"• 数据来自表 `{m.group(1)}`")

        if re.search(r'\bJOIN\b', sql, re.IGNORECASE):
            joins = re.findall(r'(\w+)\s+JOIN\s+(\w+)', sql, re.IGNORECASE)
            for jt, tbl in joins:
                lines.append(f"• 连接表 `{tbl}`（{jt} JOIN）")

        m = re.search(r'WHERE\s+(.+?)(?=\s+GROUP|\s+ORDER|\s+LIMIT|$)',
                      sql, re.IGNORECASE | re.DOTALL)
        if m:
            lines.append(f"• 过滤条件: `{m.group(1).strip()}`")

        if re.search(r'\bGROUP\s+BY\b', sql, re.IGNORECASE):
            lines.append("• 分组聚合")

        if re.search(r'\bORDER\s+BY\b', sql, re.IGNORECASE):
            m = re.search(r'ORDER\s+BY\s+(.+?)(?=\s+LIMIT|$)',
                          sql, re.IGNORECASE)
            if m:
                lines.append(f"• 排序: `{m.group(1).strip()}`")

        m = re.search(r'LIMIT\s+(\d+)', sql, re.IGNORECASE)
        if m:
            lines.append(f"• 最多返回 **{m.group(1)}** 行")

        return '\n'.join(lines)

    def _explain_insert(self, sql: str) -> str:
        m = re.search(r'INTO\s+(\w+)', sql, re.IGNORECASE)
        table = m.group(1) if m else '?'
        return (f"**INSERT 插入**\n\n"
                f"• 目标表: `{table}`\n"
                f"• 将插入一行或多行数据")

    def _explain_update(self, sql: str) -> str:
        m = re.search(r'UPDATE\s+(\w+)', sql, re.IGNORECASE)
        table = m.group(1) if m else '?'
        has_where = bool(re.search(r'\bWHERE\b', sql, re.IGNORECASE))
        warn = "\n\n⚠️ **没有 WHERE 条件，将更新全部行！**" if not has_where else ""
        return (f"**UPDATE 更新**\n\n"
                f"• 表: `{table}`\n"
                f"• 将修改符合条件的行{warn}")

    def _explain_delete(self, sql: str) -> str:
        m = re.search(r'FROM\s+(\w+)', sql, re.IGNORECASE)
        table = m.group(1) if m else '?'
        has_where = bool(re.search(r'\bWHERE\b', sql, re.IGNORECASE))
        warn = "\n\n⚠️ **没有 WHERE 条件，将删除全部行！**" if not has_where else ""
        return (f"**DELETE 删除**\n\n"
                f"• 表: `{table}`\n"
                f"• 将删除符合条件的行{warn}")

    def _explain_create(self, sql: str) -> str:
        m = re.search(r'TABLE\s+(\w+)', sql, re.IGNORECASE)
        table = m.group(1) if m else '?'
        cols = re.findall(r'`?(\w+)`?\s+(\w+(?:\([^)]+\))?)', sql)
        lines = [f"**CREATE TABLE 建表**", "", f"• 表名: `{table}`", "• 列定义:"]
        for cname, ctype in cols[:20]:
            if cname.upper() in ('TABLE', 'PRIMARY', 'KEY', 'INDEX',
                                 'UNIQUE', 'CONSTRAINT'):
                continue
            lines.append(f"    - `{cname}`: {ctype}")
        return '\n'.join(lines)

    # ==================== 优化建议 ====================

    def optimize_suggestions(self, sql: str, table: str = None) -> List[str]:
        """给出优化建议"""
        suggestions = []
        up = sql.upper()

        # SELECT *
        if re.search(r'SELECT\s+\*', up):
            suggestions.append(
                "⚠️ 使用 `SELECT *` 会返回所有列，建议明确列出需要的列以提高性能。")

        # 没有 WHERE 的 UPDATE / DELETE
        if up.startswith('UPDATE') and 'WHERE' not in up:
            suggestions.append(
                "🚨 **危险**：UPDATE 没有 WHERE 条件，会更新整表！")
        if up.startswith('DELETE') and 'WHERE' not in up:
            suggestions.append(
                "🚨 **危险**：DELETE 没有 WHERE 条件，会删除整表！")

        # LIKE 前置通配
        if re.search(r"LIKE\s+'%", up):
            suggestions.append(
                "⚠️ `LIKE '%xxx'` 前置通配符无法使用索引，建议改为后缀或全文索引。")

        # 函数包装列
        if re.search(r'WHERE\s+\w+\s*\(\s*\w+\s*\)\s*=', up):
            suggestions.append(
                "💡 WHERE 中对列使用函数会导致索引失效，建议改成对值使用函数。")

        # OR
        if re.search(r'\bOR\b', up) and 'UNION' not in up:
            suggestions.append(
                "💡 多个 OR 条件建议改写为 UNION ALL，或确认索引是否生效。")

        # 没有 LIMIT 的 SELECT
        if up.startswith('SELECT') and 'LIMIT' not in up and \
           'COUNT(' not in up and 'GROUP BY' not in up:
            suggestions.append(
                "💡 大表查询建议加 `LIMIT` 限制返回行数。")

        # 子查询嵌套
        if up.count('SELECT') > 2:
            suggestions.append(
                "💡 多层子查询考虑改写为 JOIN 或 CTE，可读性和性能更好。")

        # 隐式类型转换
        if re.search(r"=\s*'[0-9]+'", sql):
            suggestions.append(
                "💡 数字列不要用字符串比较（如 `id = '1'`），可能导致索引失效。")

        if not suggestions:
            suggestions.append("✅ 未发现明显问题。")
        return suggestions

    # ==================== 表关系推测 ====================

    def infer_relations(self, tables: List[str],
                        columns: Dict[str, List[str]]) -> List[Dict]:
        """推测表之间的关系"""
        relations = []
        table_set = {t.lower(): t for t in tables}

        for t, cols in columns.items():
            for c in cols:
                cl = c.lower()
                for other_lower, other in table_set.items():
                    if other_lower == t.lower():
                        continue
                    if cl in (f"{other_lower}_id", f"{other_lower}id"):
                        relations.append({
                            'from_table': t,
                            'from_col': c,
                            'to_table': other,
                            'to_col': 'id',
                            'confidence': 0.9,
                            'reason': '基于命名规则'
                        })
        return relations