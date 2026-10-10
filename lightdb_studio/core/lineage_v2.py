"""
SQL 血缘增强
- CTE 血缘
- 视图血缘
- 存储过程血缘
- 深度递归追踪
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set
from collections import defaultdict


LINEAGE_V2_FILE = Path.home() / '.lightdb_studio' / 'lineage_deep.json'


@dataclass
class ObjectNode:
    """数据库对象（表/视图/过程/函数）"""
    name: str
    obj_type: str = "table"      # table / view / procedure / function
    database: str = ""
    definition: str = ""          # 视图定义或过程体
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class CTEInfo:
    """CTE 信息"""
    name: str
    query: str
    source_tables: List[str] = field(default_factory=list)
    ref_count: int = 0


class LineageAnalyzerV2:
    """血缘分析器 2.0"""

    @staticmethod
    def extract_ctes(sql: str) -> List[CTEInfo]:
        """提取 CTE"""
        result = []
        up = sql.strip().upper()
        if not up.startswith('WITH'):
            return result

        # WITH [RECURSIVE] name AS (query), name2 AS (query)
        m = re.match(r'\s*WITH\s+(RECURSIVE\s+)?(.+)',
                     sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return result

        rest = m.group(2).strip()

        # 逐个提取 name AS (...)
        pos = 0
        while pos < len(rest):
            cm = re.match(
                r'\s*(\w+)\s*(?:\([^)]+\))?\s+AS\s*\(',
                rest[pos:], re.IGNORECASE)
            if not cm:
                break

            name = cm.group(1)
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
                break

            query = rest[start:i - 1].strip()
            cte = CTEInfo(name=name, query=query)

            # 提取源表（排除 CTE 名称）
            for tm in re.finditer(
                    r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?',
                    query, re.IGNORECASE):
                t = tm.group(1)
                if t not in (x.name for x in result) and t.lower() != name.lower():
                    cte.source_tables.append(t)

            result.append(cte)
            pos = i
            # 跳过逗号
            while pos < len(rest) and rest[pos] in ' \t\n\r,':
                pos += 1
            if pos < len(rest) and not re.match(
                    r'\w+\s*(?:\([^)]+\))?\s+AS\s*\(',
                    rest[pos:], re.IGNORECASE):
                break

        return result

    @staticmethod
    def extract_view_deps(view_def: str) -> List[str]:
        """从视图定义提取依赖表"""
        tables = []
        # CREATE VIEW name AS SELECT ... FROM t1 JOIN t2
        # 去掉 CREATE VIEW 部分
        m = re.search(r'\bAS\s+(.+)', view_def,
                      re.IGNORECASE | re.DOTALL)
        if not m:
            return tables

        body = m.group(1)
        for tm in re.finditer(
                r'\b(?:FROM|JOIN)\s+[`"]?(\w+)[`"]?',
                body, re.IGNORECASE):
            t = tm.group(1)
            if t.upper() not in ('SELECT', 'WHERE', 'GROUP'):
                if t not in tables:
                    tables.append(t)

        return tables

    @staticmethod
    def extract_procedure_deps(proc_body: str) -> Dict:
        """从存储过程提取依赖"""
        result = {
            'reads': [],
            'writes': [],
        }

        # SELECT ... FROM
        for m in re.finditer(
                r'\bSELECT\s+.+?\bFROM\s+[`"]?(\w+)[`"]?',
                proc_body, re.IGNORECASE | re.DOTALL):
            t = m.group(1)
            if t not in result['reads']:
                result['reads'].append(t)

        # INSERT INTO
        for m in re.finditer(
                r'\bINSERT\s+(?:INTO\s+)?[`"]?(\w+)[`"]?',
                proc_body, re.IGNORECASE):
            t = m.group(1)
            if t not in result['writes']:
                result['writes'].append(t)

        # UPDATE
        for m in re.finditer(
                r'\bUPDATE\s+[`"]?(\w+)[`"]?',
                proc_body, re.IGNORECASE):
            t = m.group(1)
            if t not in result['writes']:
                result['writes'].append(t)

        # DELETE FROM
        for m in re.finditer(
                r'\bDELETE\s+FROM\s+[`"]?(\w+)[`"]?',
                proc_body, re.IGNORECASE):
            t = m.group(1)
            if t not in result['writes']:
                result['writes'].append(t)

        return result


class DeepLineageStore:
    """深度血缘存储"""

    def __init__(self):
        self.file = LINEAGE_V2_FILE
        self.objects: Dict[str, ObjectNode] = {}
        self.ctes: List[Dict] = []
        self.edges: List[Dict] = []    # {from, to, type, detail}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, o in data.get('objects', {}).items():
                self.objects[k] = ObjectNode(**o)
            self.ctes = data.get('ctes', [])
            self.edges = data.get('edges', [])
        except Exception:
            pass

    def save(self):
        data = {
            'objects': {k: asdict(o)
                         for k, o in list(self.objects.items())[-1000:]},
            'ctes': self.ctes[-2000:],
            'edges': self.edges[-10000:],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 注册对象 ====================

    def register_table(self, name: str, db: str = '', definition: str = ''):
        key = f"{db}.{name}" if db else name
        if key not in self.objects:
            self.objects[key] = ObjectNode(
                name=name, obj_type='table',
                database=db, definition=definition)
        self.save()

    def register_view(self, name: str, definition: str, db: str = ''):
        key = f"{db}.{name}" if db else name
        self.objects[key] = ObjectNode(
            name=name, obj_type='view',
            database=db, definition=definition)

        # 提取依赖
        deps = LineageAnalyzerV2.extract_view_deps(definition)
        for d in deps:
            self.edges.append({
                'from': d,
                'to': key,
                'type': 'view_dep',
                'detail': '',
            })
        self.save()

    def register_procedure(self, name: str, body: str, db: str = ''):
        key = f"{db}.{name}" if db else name
        self.objects[key] = ObjectNode(
            name=name, obj_type='procedure',
            database=db, definition=body)

        deps = LineageAnalyzerV2.extract_procedure_deps(body)
        for r in deps['reads']:
            self.edges.append({
                'from': r, 'to': key, 'type': 'proc_read',
                'detail': '',
            })
        for w in deps['writes']:
            self.edges.append({
                'from': key, 'to': w, 'type': 'proc_write',
                'detail': '',
            })
        self.save()

    def register_ctes_from_sql(self, sql: str, target: str = ''):
        """从 SQL 提取 CTE"""
        ctes = LineageAnalyzerV2.extract_ctes(sql)
        for cte in ctes:
            self.ctes.append({
                'name': cte.name,
                'query': cte.query[:500],
                'sources': cte.source_tables,
                'target': target,
                'ts': time.time(),
            })
            for src in cte.source_tables:
                self.edges.append({
                    'from': src,
                    'to': f"cte:{cte.name}",
                    'type': 'cte',
                    'detail': cte.query[:100],
                })
            if target:
                self.edges.append({
                    'from': f"cte:{cte.name}",
                    'to': target,
                    'type': 'cte_use',
                    'detail': '',
                })
        self.save()

    # ==================== 分析 ====================

    def dependencies_of(self, obj_name: str,
                        depth: int = 5) -> List[Dict]:
        """查询对象的依赖（它依赖谁）"""
        result = []
        visited = set()

        def walk(name, level):
            if level > depth:
                return
            if name in visited:
                return
            visited.add(name)

            for e in self.edges:
                if e['to'] == name or e['to'].endswith(f'.{name}'):
                    result.append({
                        'from': e['from'],
                        'to': e['to'],
                        'type': e['type'],
                        'level': level,
                    })
                    walk(e['from'], level + 1)

        walk(obj_name, 1)
        return result

    def dependents_of(self, obj_name: str,
                      depth: int = 5) -> List[Dict]:
        """查询对象的反向依赖（谁依赖它）"""
        result = []
        visited = set()

        def walk(name, level):
            if level > depth:
                return
            if name in visited:
                return
            visited.add(name)

            for e in self.edges:
                if e['from'] == name or e['from'].endswith(f'.{name}'):
                    result.append({
                        'from': e['from'],
                        'to': e['to'],
                        'type': e['type'],
                        'level': level,
                    })
                    walk(e['to'], level + 1)

        walk(obj_name, 1)
        return result

    def get_object(self, name: str) -> ObjectNode:
        for key, obj in self.objects.items():
            if obj.name == name or key == name:
                return obj
        return None

    def list_views(self) -> List[ObjectNode]:
        return [o for o in self.objects.values()
                if o.obj_type == 'view']

    def list_procedures(self) -> List[ObjectNode]:
        return [o for o in self.objects.values()
                if o.obj_type == 'procedure']

    def list_ctes(self) -> List[Dict]:
        return list(reversed(self.ctes))[:200]

    def stats(self) -> dict:
        by_type = defaultdict(int)
        for o in self.objects.values():
            by_type[o.obj_type] += 1
        return {
            'total_objects': len(self.objects),
            'by_type': dict(by_type),
            'total_ctes': len(self.ctes),
            'total_edges': len(self.edges),
        }

    def clear(self):
        self.objects.clear()
        self.ctes.clear()
        self.edges.clear()
        self.save()