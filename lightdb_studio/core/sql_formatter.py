"""
SQL 格式化 / 美化
- 关键字大写
- 缩进对齐
- 逗号前换行
- 逗号后加空格
"""
import re


# 主要关键字（大写）
KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT',
    'OFFSET', 'INSERT INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE FROM',
    'CREATE TABLE', 'DROP TABLE', 'ALTER TABLE', 'CREATE INDEX',
    'DROP INDEX', 'CREATE VIEW', 'DROP VIEW',
    'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'CROSS JOIN', 'LEFT OUTER JOIN',
    'RIGHT OUTER JOIN', 'FULL OUTER JOIN', 'ON', 'USING',
    'UNION ALL', 'UNION', 'DISTINCT', 'AS', 'AND', 'OR', 'NOT',
    'IN', 'EXISTS', 'BETWEEN', 'LIKE', 'IS NULL', 'IS NOT NULL',
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'WITH', 'RECURSIVE',
    'PRIMARY KEY', 'FOREIGN KEY', 'REFERENCES', 'UNIQUE', 'NOT NULL',
    'DEFAULT', 'AUTO_INCREMENT', 'CHECK', 'INDEX',
    'BEGIN', 'COMMIT', 'ROLLBACK', 'TRANSACTION',
]

# 函数（小写或保持原样）
FUNCTIONS = [
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
    'NOW', 'CURDATE', 'CURTIME', 'DATE_FORMAT',
    'CONCAT', 'SUBSTRING', 'UPPER', 'LOWER', 'LENGTH',
    'IFNULL', 'COALESCE', 'CAST', 'CONVERT',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LEAD', 'LAG',
    'JSON_EXTRACT', 'JSON_SET',
]


class SQLFormatter:
    """SQL 格式化器"""

    def __init__(self, uppercase_keywords=True, indent_size=2,
                 keyword_newline=True, comma_newline=True):
        self.uppercase = uppercase_keywords
        self.indent_size = indent_size
        self.keyword_newline = keyword_newline
        self.comma_newline = comma_newline

    def format(self, sql: str) -> str:
        if not sql.strip():
            return ''

        # 1. 保护字符串和注释
        sql, strings = self._protect_strings(sql)
        sql, comments = self._protect_comments(sql)

        # 2. 规范化空白
        sql = re.sub(r'\s+', ' ', sql)

        # 3. 逗号后加空格（临时）
        sql = re.sub(r',\s*', ', ', sql)

        # 4. 各种关键字前加换行
        if self.keyword_newline:
            # 主要子句
            major = [
                'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY',
                'HAVING', 'LIMIT', 'OFFSET',
                'INSERT INTO', 'VALUES', 'UPDATE', 'SET',
                'DELETE FROM',
                'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN',
                'CROSS JOIN', 'LEFT OUTER JOIN', 'RIGHT OUTER JOIN',
                'FULL OUTER JOIN', 'UNION ALL', 'UNION',
                'WITH', 'RETURNING',
            ]
            for kw in major:
                sql = re.sub(
                    r'\b' + kw.replace(' ', r'\s+') + r'\b',
                    '\n' + kw, sql, flags=re.IGNORECASE)
            # AND/OR 换行 + 缩进
            sql = re.sub(r'\bAND\b', '\n  AND', sql, flags=re.IGNORECASE)
            sql = re.sub(r'\bOR\b', '\n  OR', sql, flags=re.IGNORECASE)

        # 5. 括号内子句
        sql = re.sub(r'\(\s*', '(', sql)
        sql = re.sub(r'\s*\)', ')', sql)

        # 6. 恢复字符串和注释
        sql = self._restore_comments(sql, comments)
        sql = self._restore_strings(sql, strings)

        # 7. 关键字大写
        if self.uppercase:
            for kw in sorted(KEYWORDS, key=len, reverse=True):
                sql = re.sub(
                    r'\b' + kw.replace(' ', r'\s+') + r'\b',
                    kw, sql, flags=re.IGNORECASE)
            for fn in FUNCTIONS:
                sql = re.sub(
                    r'\b' + fn + r'\b(?=\s*\()', fn, sql,
                    flags=re.IGNORECASE)

        # 8. 清理多余空行
        sql = re.sub(r'\n{3,}', '\n\n', sql)
        sql = re.sub(r'^\s+', '', sql, flags=re.MULTILINE)

        return sql.strip()

    def minify(self, sql: str) -> str:
        """压缩 SQL 为单行"""
        sql, strings = self._protect_strings(sql)
        sql, comments = self._protect_comments(sql)
        sql = re.sub(r'\s+', ' ', sql)
        sql = self._restore_comments(sql, comments)
        sql = self._restore_strings(sql, strings)
        return sql.strip()

    def _protect_strings(self, sql):
        strings = []

        def repl(m):
            strings.append(m.group(0))
            return f'__STR_{len(strings)-1}__'

        sql = re.sub(r"'(?:[^'\\]|\\.|'')*'", repl, sql)
        sql = re.sub(r'"(?:[^"\\]|\\.)*"', repl, sql)
        return sql, strings

    def _restore_strings(self, sql, strings):
        for i, s in enumerate(strings):
            sql = sql.replace(f'__STR_{i}__', s)
        return sql

    def _protect_comments(self, sql):
        comments = []

        def repl(m):
            comments.append(m.group(0))
            return f'__CMT_{len(comments)-1}__'

        sql = re.sub(r'--[^\n]*', repl, sql)
        sql = re.sub(r'/\*.*?\*/', repl, sql, flags=re.DOTALL)
        return sql, comments

    def _restore_comments(self, sql, comments):
        for i, c in enumerate(comments):
            sql = sql.replace(f'__CMT_{i}__', c)
        return sql