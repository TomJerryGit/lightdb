"""
增强血缘 - 列级追踪 + 影响分析
- 记录列到列的映射
- 影响分析（改一个列影响什么）
- 时间旅行（追溯历史）
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Optional
from collections import defaultdict


ENHANCED_LINEAGE_FILE = Path.home() / '.lightdb_studio' / 'lineage_v2.json'


@dataclass
class ColumnEdge:
    """列级血缘边"""
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    transform: str = ""       # 转换类型: direct / sum / count / concat / ...
    sql_preview: str = ""
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class ColumnNode:
    """列节点"""
    table: str
    column: str
    first_seen: float = 0
    last_used: float = 0
    read_count: int = 0
    write_count: int = 0

    def __post_init__(self):
        if not self.first_seen:
            self.first_seen = time.time()


class EnhancedLineageStore:
    """列级血缘存储"""

    def __init__(self):
        self.file = ENHANCED_LINEAGE_FILE
        self.edges: List[ColumnEdge] = []
        self.nodes: Dict[str, ColumnNode] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for e in data.get('edges', []):
                self.edges.append(ColumnEdge(**e))
            for k, n in data.get('nodes', {}).items():
                self.nodes[k] = ColumnNode(**n)
        except Exception:
            pass

    def save(self):
        # 只保留最近 10000 条边
        edges = self.edges[-10000:]
        data = {
            'edges': [asdict(e) for e in edges],
            'nodes': {k: asdict(n) for k, n in self.nodes.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 记录 ====================

    def record_sql(self, sql: str, connection: str = ''):
        """从 SQL 提取列级血缘"""
        up = sql.strip().upper()

        # INSERT INTO t1 (cols) SELECT cols FROM t2
        if up.startswith('INSERT'):
            self._record_insert_select(sql)
        # UPDATE t1 SET col = expr
        elif up.startswith('UPDATE'):
            self._record_update(sql)
        # CREATE TABLE AS SELECT
        elif 'CREATE TABLE' in up and 'AS SELECT' in up:
            self._record_ctas(sql)
        # SELECT（只记录读取）
        elif up.startswith('SELECT') or up.startswith('WITH'):
            self._record_select_read(sql)

        self.save()

    def _record_insert_select(self, sql: str):
        """INSERT INTO ... SELECT ..."""
        # 提取目标
        m = re.search(
            r'INSERT\s+(?:INTO\s+)?[`"]?(\w+)[`"]?\s*\(([^)]+)\)',
            sql, re.IGNORECASE)
        if not m:
            return

        to_table = m.group(1)
        to_cols = [c.strip().strip('`"')
                   for c in m.group(2).split(',')]

        # 提取 SELECT 部分
        sm = re.search(
            r'\bSELECT\s+(.+?)\s+FROM\s+(.+)',
            sql, re.IGNORECASE | re.DOTALL)
        if not sm:
            return

        select_exprs = self._split_top(sm.group(1))
        from_clause = sm.group(2)

        # 提取源表
        source_tables = []
        for fm in re.finditer(
                r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?(?:\s+(?:AS\s+)?(\w+))?',
                from_clause, re.IGNORECASE):
            source_tables.append({
                'table': fm.group(1),
                'alias': fm.group(2) or fm.group(1),
            })

        # 逐列匹配
        for i, expr in enumerate(select_exprs):
            if i >= len(to_cols):
                break
            to_col = to_cols[i]
            from_table, from_col, transform = self._parse_expr(
                expr, source_tables)

            if from_table and from_col:
                self._add_edge(
                    from_table, from_col, to_table, to_col,
                    transform, sql)

    def _record_update(self, sql: str):
        """UPDATE t SET col = expr"""
        m = re.search(r'UPDATE\s+[`"]?(\w+)[`"]?', sql, re.IGNORECASE)
        if not m:
            return
        to_table = m.group(1)

        sm = re.search(r'\bSET\s+(.+?)(?=\s+WHERE\s|$)',
                       sql, re.IGNORECASE | re.DOTALL)
        if not sm:
            return

        for pair in self._split_top(sm.group(1)):
            if '=' not in pair:
                continue
            col, expr = pair.split('=', 1)
            to_col = col.strip().strip('`"')
            expr = expr.strip()

            # 提取源列（可能是常量）
            cm = re.search(r'[`"]?(\w+)[`"]?\.?[`"]?(\w+)[`"]?', expr)
            if cm:
                from_table = cm.group(1) if cm.group(2) else to_table
                from_col = cm.group(2) or cm.group(1)
                self._add_edge(from_table, from_col,
                               to_table, to_col, 'direct', sql)

    def _record_ctas(self, sql: str):
        """CREATE TABLE t AS SELECT ..."""
        m = re.search(
            r'CREATE\s+TABLE\s+[`"]?(\w+)[`"]?\s+AS\s+SELECT\s+(.+?)\s+FROM\s+(.+)',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return
        to_table = m.group(1)
        select_exprs = self._split_top(m.group(2))
        from_clause = m.group(3)

        source_tables = []
        for fm in re.finditer(
                r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?',
                from_clause, re.IGNORECASE):
            source_tables.append({
                'table': fm.group(1),
                'alias': fm.group(1),
            })

        for expr in select_exprs:
            from_table, from_col, transform = self._parse_expr(
                expr, source_tables)
            if from_table and from_col:
                # 目标列名 = 表达式别名或表达式本身
                alias_m = re.search(r'\s+AS\s+(\w+)\s*$', expr, re.IGNORECASE)
                to_col = alias_m.group(1) if alias_m else from_col
                self._add_edge(from_table, from_col,
                               to_table, to_col, transform, sql)

    def _record_select_read(self, sql: str):
        """SELECT（记录读取）"""
        sm = re.search(r'\bSELECT\s+(.+?)\s+FROM\s+(.+)',
                       sql, re.IGNORECASE | re.DOTALL)
        if not sm:
            return

        select_exprs = self._split_top(sm.group(1))
        from_clause = sm.group(2)

        source_tables = []
        for fm in re.finditer(
                r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?(?:\s+(?:AS\s+)?(\w+))?',
                from_clause, re.IGNORECASE):
            alias = fm.group(2) or fm.group(1)
            if alias.upper() in ('ON', 'WHERE', 'GROUP', 'ORDER', 'JOIN'):
                alias = fm.group(1)
            source_tables.append({
                'table': fm.group(1),
                'alias': alias,
            })

        for expr in select_exprs:
            if expr.strip() == '*':
                for st in source_tables:
                    self._touch_node(st['table'], '*', read=True)
                continue
            from_table, from_col, _ = self._parse_expr(expr, source_tables)
            if from_table and from_col:
                self._touch_node(from_table, from_col, read=True)

    def _parse_expr(self, expr: str, source_tables: List[Dict]) -> tuple:
        """
        解析表达式，返回 (table, column, transform)
        """
        expr = expr.strip()

        # 别名
        m = re.match(r'(.+?)\s+AS\s+\w+\s*$', expr, re.IGNORECASE)
        if m:
            expr = m.group(1).strip()
        else:
            m = re.match(r'(.+?)\s+\w+\s*$', expr)
            if m and not any(op in expr for op in '()+-*/'):
                # 简单表达式 + 别名
                pass

        # 聚合函数
        for fn, transform in [
                ('COUNT', 'count'), ('SUM', 'sum'),
                ('AVG', 'avg'), ('MIN', 'min'), ('MAX', 'max')]:
            fm = re.match(rf'{fn}\s*\(\s*(.+?)\s*\)', expr, re.IGNORECASE)
            if fm:
                inner = fm.group(1)
                if inner.strip() == '*':
                    if source_tables:
                        return (source_tables[0]['table'], '*', transform)
                t, c, _ = self._parse_expr(inner, source_tables)
                return (t, c, transform)

        # CONCAT
        if re.match(r'CONCAT\s*\(', expr, re.IGNORECASE):
            return (None, None, 'concat')

        # table.column
        m = re.match(r'[`"]?(\w+)[`"]?\.[`"]?(\w+)[`"]?', expr)
        if m:
            return (m.group(1), m.group(2), 'direct')

        # 单列名
        m = re.match(r'^[`"]?(\w+)[`"]?$', expr)
        if m:
            col = m.group(1)
            # 找出哪个源表有此列（简化：取第一个）
            if source_tables:
                return (source_tables[0]['table'], col, 'direct')

        # 数字/字符串常量
        if re.match(r'^\d+$', expr) or expr.startswith("'"):
            return (None, None, 'const')

        return (None, None, '')

    def _split_top(self, s: str) -> List[str]:
        """顶层逗号分割"""
        parts, buf, depth = [], '', 0
        in_str = None
        for ch in s:
            if in_str:
                buf += ch
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
                buf += ch
            elif ch == '(':
                depth += 1
                buf += ch
            elif ch == ')':
                depth -= 1
                buf += ch
            elif ch == ',' and depth == 0:
                parts.append(buf.strip())
                buf = ''
            else:
                buf += ch
        if buf.strip():
            parts.append(buf.strip())
        return parts

    def _add_edge(self, from_table, from_col, to_table, to_col,
                  transform, sql):
        edge = ColumnEdge(
            from_table=from_table,
            from_column=from_col,
            to_table=to_table,
            to_column=to_col,
            transform=transform,
            sql_preview=sql[:200].replace('\n', ' '),
        )
        # 去重
        for e in self.edges[-500:]:
            if (e.from_table == from_table and
                    e.from_column == from_col and
                    e.to_table == to_table and
                    e.to_column == to_col):
                return
        self.edges.append(edge)
        self._touch_node(from_table, from_col, read=True)
        self._touch_node(to_table, to_col, write=True)

    def _touch_node(self, table, column, read=False, write=False):
        key = f"{table}.{column}"
        node = self.nodes.get(key)
        if not node:
            node = ColumnNode(table=table, column=column)
            self.nodes[key] = node
        node.last_used = time.time()
        if read:
            node.read_count += 1
        if write:
            node.write_count += 1

    # ==================== 影响分析 ====================

    def impact_analysis(self, table: str, column: str = None,
                        depth: int = 5) -> Dict:
        """
        影响分析：修改这个列会影响哪些表/列
        """
        result = {
            'source': {'table': table, 'column': column},
            'direct': [],
            'indirect': [],
            'paths': [],
        }

        # 收集直接下游
        direct = set()
        for e in self.edges:
            if e.from_table == table:
                if column is None or e.from_column == column:
                    key = f"{e.to_table}.{e.to_column}"
                    direct.add(key)
                    result['direct'].append({
                        'table': e.to_table,
                        'column': e.to_column,
                        'transform': e.transform,
                    })

        # BFS 收集间接
        visited = set()
        queue = [(d, 1) for d in direct]
        while queue:
            node_key, level = queue.pop(0)
            if level > depth or node_key in visited:
                continue
            visited.add(node_key)

            t, c = node_key.split('.', 1)
            for e in self.edges:
                if e.from_table == t and e.from_column == c:
                    next_key = f"{e.to_table}.{e.to_column}"
                    if next_key not in visited and next_key not in direct:
                        result['indirect'].append({
                            'table': e.to_table,
                            'column': e.to_column,
                            'level': level + 1,
                            'transform': e.transform,
                        })
                        queue.append((next_key, level + 1))

        return result

    def upstream_of_column(self, table: str, column: str,
                           depth: int = 5) -> List[Dict]:
        """追踪某个列的数据来源"""
        result = []
        visited = set()

        def walk(t, c, level):
            if level > depth:
                return
            key = f"{t}.{c}"
            if key in visited:
                return
            visited.add(key)

            for e in self.edges:
                if e.to_table == t and e.to_column == c:
                    result.append({
                        'table': e.from_table,
                        'column': e.from_column,
                        'transform': e.transform,
                        'level': level,
                        'sql': e.sql_preview,
                    })
                    walk(e.from_table, e.from_column, level + 1)

        walk(table, column, 1)
        return result

    def stats(self) -> dict:
        tables = set()
        for e in self.edges:
            tables.add(e.from_table)
            tables.add(e.to_table)
        return {
            'total_edges': len(self.edges),
            'total_nodes': len(self.nodes),
            'total_tables': len(tables),
        }