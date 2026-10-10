"""
数据血缘追踪
- 从 SQL 解析表依赖
- 追踪表之间的引用关系
- 支持字段级血缘（简化：SELECT col FROM table）
"""
import re
import json
import time
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, asdict, field   # ★ 修复：补 dataclass
from typing import List, Dict, Set, Optional

LINEAGE_FILE = Path.home() / '.lightdb_studio' / 'lineage.json'


class LineageAnalyzer:
    """SQL 血缘分析器"""

    @staticmethod
    def extract_tables(sql: str) -> List[Dict]:
        results = []
        sql = sql.strip()
        up = sql.upper()

        if up.startswith('SELECT') or up.startswith('WITH'):
            for m in re.finditer(
                r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?(?:\s+(?:AS\s+)?(\w+))?',
                sql, re.IGNORECASE):
                tbl = m.group(1)
                alias = m.group(2) or tbl
                if tbl.upper() in ('SELECT', 'WHERE', 'GROUP', 'ORDER',
                                   'HAVING', 'LIMIT', 'UNION'):
                    continue
                if alias and alias.upper() in (
                        'ON', 'WHERE', 'GROUP', 'ORDER', 'JOIN',
                        'INNER', 'LEFT', 'RIGHT', 'OUTER', 'UNION',
                        'HAVING', 'LIMIT', 'AS'):
                    alias = tbl
                results.append({
                    'table': tbl,
                    'alias': alias,
                    'role': 'source',
                })

        m = re.search(r'\bINSERT\s+(?:INTO\s+)?[`"]?(\w+)[`"]?',
                      sql, re.IGNORECASE)
        if m:
            results.append({
                'table': m.group(1),
                'alias': '',
                'role': 'target',
            })

        m = re.search(r'\bUPDATE\s+[`"]?(\w+)[`"]?', sql, re.IGNORECASE)
        if m:
            results.append({
                'table': m.group(1),
                'alias': '',
                'role': 'target',
            })

        m = re.search(r'\bDELETE\s+FROM\s+[`"]?(\w+)[`"]?',
                      sql, re.IGNORECASE)
        if m:
            results.append({
                'table': m.group(1),
                'alias': '',
                'role': 'target',
            })

        m = re.search(
            r'\bCREATE\s+TABLE\s+[`"]?(\w+)[`"]?\s+AS\s+SELECT',
            sql, re.IGNORECASE)
        if m:
            results.append({
                'table': m.group(1),
                'alias': '',
                'role': 'target',
            })

        seen = set()
        unique = []
        for r in results:
            key = (r['table'].lower(), r['role'])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    @staticmethod
    def extract_columns(sql: str) -> Dict[str, List[str]]:
        result = defaultdict(list)
        m = re.search(r'SELECT\s+(.+?)\s+FROM', sql,
                      re.IGNORECASE | re.DOTALL)
        if not m:
            return dict(result)
        cols_str = m.group(1)
        for c in re.split(r',', cols_str):
            c = c.strip()
            if not c or c == '*':
                continue
            m2 = re.match(r'[`"]?(\w+)[`"]?\.[`"]?(\w+)[`"]?', c)
            if m2:
                result[m2.group(1)].append(m2.group(2))
        return dict(result)


@dataclass
class LineageEdge:
    from_table: str
    to_table: str
    role: str                # 'source' 或 'target'
    sql_preview: str = ""
    connection: str = ""
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class LineageStore:
    """血缘存储"""

    def __init__(self):
        self.file = LINEAGE_FILE
        self.edges: List[LineageEdge] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for e in data.get('edges', []):
                self.edges.append(LineageEdge(**e))
        except Exception:
            pass

    def save(self):
        data = {'edges': [asdict(e) for e in self.edges[-5000:]]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def record_sql(self, sql: str, connection: str = ''):
        tables = LineageAnalyzer.extract_tables(sql)
        target = next((t for t in tables if t['role'] == 'target'), None)
        sources = [t for t in tables if t['role'] == 'source']

        if not sources:
            return 0

        n = 0
        preview = sql[:200].replace('\n', ' ')

        if target:
            for s in sources:
                self.edges.append(LineageEdge(
                    from_table=s['table'],
                    to_table=target['table'],
                    role='write',
                    sql_preview=preview,
                    connection=connection,
                ))
                n += 1
        else:
            for s in sources:
                self.edges.append(LineageEdge(
                    from_table=s['table'],
                    to_table='__query__',
                    role='read',
                    sql_preview=preview,
                    connection=connection,
                ))
                n += 1

        if n:
            self.save()
        return n

    # ==================== 查询血缘 ====================

    def upstream(self, table: str, depth: int = 3) -> List[Dict]:
        result = []
        visited = set()

        def walk(t, d):
            if d <= 0 or t in visited:
                return
            visited.add(t)
            for e in self.edges:
                if e.to_table == t and e.role == 'write':
                    result.append({
                        'table': e.from_table,
                        'via': e.to_table,
                        'depth': 3 - d + 1,
                        'sql': e.sql_preview,
                    })
                    walk(e.from_table, d - 1)

        walk(table, depth)
        seen = set()
        unique = []
        for r in result:
            key = (r['table'], r['depth'])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    def downstream(self, table: str, depth: int = 3) -> List[Dict]:
        result = []
        visited = set()

        def walk(t, d):
            if d <= 0 or t in visited:
                return
            visited.add(t)
            for e in self.edges:
                if e.from_table == t and e.role == 'write':
                    result.append({
                        'table': e.to_table,
                        'via': e.from_table,
                        'depth': 3 - d + 1,
                        'sql': e.sql_preview,
                    })
                    walk(e.to_table, d - 1)

        walk(table, depth)
        seen = set()
        unique = []
        for r in result:
            key = (r['table'], r['depth'])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    def related(self, table: str) -> List[Dict]:
        up = self.upstream(table, 5)
        down = self.downstream(table, 5)
        return [
            {'table': t, 'direction': 'up'} for t in
            {u['table'] for u in up}
        ] + [
            {'table': t, 'direction': 'down'} for t in
            {d['table'] for d in down}
        ]

    def stats(self) -> Dict:
        tables = set()
        for e in self.edges:
            if e.from_table != '__query__':
                tables.add(e.from_table)
            if e.to_table != '__query__':
                tables.add(e.to_table)
        return {
            'total_edges': len(self.edges),
            'total_tables': len(tables),
            'tables': sorted(tables),
        }

    def clear(self):
        self.edges.clear()
        self.save()