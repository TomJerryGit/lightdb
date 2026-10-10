"""
CTE (Common Table Expression)
支持:
  - 普通 CTE: WITH name AS (SELECT ...) SELECT * FROM name
  - 多 CTE: WITH a AS (...), b AS (...) SELECT ...
  - 递归 CTE: WITH RECURSIVE name AS (base UNION ALL recursive)
"""
import re


class CTE:
    def __init__(self, name, query_sql, columns=None, recursive=False):
        self.name = name
        self.query_sql = query_sql
        self.columns = columns        # 显式列名（可选）
        self.recursive = recursive


def parse_with_clause(sql: str):
    """
    解析 WITH ... SELECT 结构
    返回 (ctes, main_sql) 或 None
    """
    s = sql.strip()
    m = re.match(r'\s*WITH\s+(RECURSIVE\s+)?(.+)', s, re.IGNORECASE | re.DOTALL)
    if not m:
        return None

    recursive = bool(m.group(1))
    rest = m.group(2).strip()

    # 解析每个 CTE: name [(cols)] AS (subquery)
    ctes = []
    pos = 0

    while True:
        # 匹配 name [cols] AS (
        cm = re.match(
            r'\s*(\w+)\s*(?:\(([^)]+)\))?\s+AS\s*\(',
            rest[pos:], re.IGNORECASE
        )
        if not cm:
            break

        name = cm.group(1)
        cols = None
        if cm.group(2):
            cols = [c.strip() for c in cm.group(2).split(',')]

        # 找匹配的右括号
        start = pos + cm.end()
        depth = 1
        i = start
        while i < len(rest) and depth > 0:
            if rest[i] == '(':
                depth += 1
            elif rest[i] == ')':
                depth -= 1
            i += 1

        if depth != 0:
            raise SyntaxError("CTE 括号不匹配")

        subquery = rest[start:i-1].strip()
        ctes.append(CTE(name, subquery, cols, recursive))

        pos = i
        # 跳过逗号
        while pos < len(rest) and rest[pos] in ' \t\n\r,':
            pos += 1
        if pos >= len(rest):
            break
        # 检查是否还有下一个 CTE
        if not re.match(r'\w+\s*(?:\([^)]+\))?\s+AS\s*\(', rest[pos:], re.IGNORECASE):
            break

    if not ctes:
        return None

    main_sql = rest[pos:].strip()
    return ctes, main_sql


def split_recursive(query_sql: str):
    """
    递归 CTE 拆分: base UNION ALL recursive
    返回 (base_sql, recursive_sql, union_all)
    """
    s = query_sql.strip()
    # 找顶层 UNION [ALL]
    depth = 0
    i = 0
    upper = s.upper()
    while i < len(s):
        if s[i] == '(':
            depth += 1
        elif s[i] == ')':
            depth -= 1
        elif depth == 0:
            if upper[i:i+9] == 'UNION ALL':
                return s[:i].strip(), s[i+9:].strip(), True
            if upper[i:i+5] == 'UNION':
                return s[:i].strip(), s[i+5:].strip(), False
        i += 1
    return s, None, False