"""
数据目录
- 元数据全文搜索
- 业务标签 / 描述
- 数据分类（敏感度）
- 使用热度统计
- 数据地图（Domain 分组）
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field   # ★ 修复：补 dataclass
from typing import List, Dict, Optional, Set

CATALOG_FILE = Path.home() / '.lightdb_studio' / 'catalog.json'


# 敏感度等级
SENSITIVITY_LEVELS = {
    'public': {'label': '公开', 'color': '#89d185', 'level': 0},
    'internal': {'label': '内部', 'color': '#569cd6', 'level': 1},
    'confidential': {'label': '机密', 'color': '#dcdcaa', 'level': 2},
    'restricted': {'label': '受限', 'color': '#ce9178', 'level': 3},
    'pii': {'label': '个人隐私', 'color': '#f48771', 'level': 4},
}


# 敏感列名识别
SENSITIVE_COL_PATTERNS = {
    'pii': [
        'phone', 'mobile', 'email', 'name', 'address', 'id_card',
        'passport', 'ssn', 'birthday', 'gender', 'real_name',
    ],
    'restricted': [
        'password', 'passwd', 'pwd', 'token', 'secret', 'api_key',
        'private_key', 'credit_card', 'bank_card', 'salary',
    ],
    'confidential': [
        'cost', 'price', 'amount', 'revenue', 'profit', 'margin',
    ],
}


@dataclass
class ColumnMeta:
    name: str = ""
    type: str = ""
    comment: str = ""
    business_name: str = ""
    sensitivity: str = "internal"
    tags: List[str] = field(default_factory=list)
    is_primary: bool = False
    is_indexed: bool = False
    last_seen: float = 0


@dataclass
class TableMeta:
    name: str = ""
    database: str = ""
    connection: str = ""
    comment: str = ""
    business_name: str = ""
    owner: str = ""
    domain: str = "未分类"
    sensitivity: str = "internal"
    tags: List[str] = field(default_factory=list)
    columns: List[ColumnMeta] = field(default_factory=list)
    row_count: int = 0
    query_count: int = 0
    last_queried: float = 0
    top_queries: List[str] = field(default_factory=list)
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at


class CatalogEngine:
    """数据目录引擎"""

    def __init__(self):
        self.file = CATALOG_FILE
        self.tables: Dict[str, TableMeta] = {}
        self.domains: Dict[str, List[str]] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ==================== 持久化 ====================

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key, td in data.get('tables', {}).items():
                td = dict(td)
                cols = [ColumnMeta(**c) for c in td.pop('columns', [])]
                t = TableMeta(**td)
                t.columns = cols
                self.tables[key] = t
            self.domains = data.get('domains', {})
        except Exception:
            pass

    def save(self):
        data = {
            'tables': {
                k: {**asdict(t),
                    'columns': [asdict(c) for c in t.columns]}
                for k, t in self.tables.items()
            },
            'domains': self.domains,
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 索引 ====================

    def index_table(self, db: str, table: str, conn_name: str,
                    columns_meta: List[dict],
                    comment: str = '', row_count: int = 0):
        key = f"{db}.{table}"
        existing = self.tables.get(key)

        cols = []
        for c in columns_meta:
            cname = c['name']
            sensitivity = self._infer_sensitivity(cname)

            old_col = None
            if existing:
                for oc in existing.columns:
                    if oc.name == cname:
                        old_col = oc
                        break

            cm = ColumnMeta(
                name=cname,
                type=c.get('type', ''),
                comment=c.get('comment', '') or (old_col.comment if old_col else ''),
                business_name=(old_col.business_name if old_col else ''),
                sensitivity=sensitivity,
                tags=old_col.tags if old_col else [],
                is_primary=(c.get('key') == 'PRI'),
                is_indexed=(c.get('key') in ('PRI', 'UNI', 'MUL')),
            )
            cols.append(cm)

        table_sens = 'public'
        for c in cols:
            if SENSITIVITY_LEVELS[c.sensitivity]['level'] > \
               SENSITIVITY_LEVELS[table_sens]['level']:
                table_sens = c.sensitivity

        if existing:
            existing.columns = cols
            existing.row_count = row_count
            existing.connection = conn_name
            existing.database = db
            if comment:
                existing.comment = comment
            existing.sensitivity = table_sens
            existing.updated_at = time.time()
        else:
            t = TableMeta(
                name=table, database=db, connection=conn_name,
                comment=comment, row_count=row_count,
                sensitivity=table_sens, columns=cols,
            )
            self.tables[key] = t
            self.domains.setdefault('未分类', []).append(key)

        self.save()

    def _infer_sensitivity(self, col_name: str) -> str:
        cl = col_name.lower()
        for level, patterns in SENSITIVE_COL_PATTERNS.items():
            for p in patterns:
                if p in cl:
                    return level
        return 'internal'

    def update_table_meta(self, db: str, table: str, **kwargs):
        key = f"{db}.{table}"
        if key not in self.tables:
            return False
        t = self.tables[key]

        old_domain = t.domain
        for k, v in kwargs.items():
            if hasattr(t, k):
                setattr(t, k, v)
        t.updated_at = time.time()

        if 'domain' in kwargs and kwargs['domain'] != old_domain:
            if old_domain in self.domains and key in self.domains[old_domain]:
                self.domains[old_domain].remove(key)
            self.domains.setdefault(t.domain, []).append(key)

        self.save()
        return True

    def update_column_meta(self, db: str, table: str, column: str, **kwargs):
        key = f"{db}.{table}"
        if key not in self.tables:
            return False
        t = self.tables[key]
        for c in t.columns:
            if c.name == column:
                for k, v in kwargs.items():
                    if hasattr(c, k):
                        setattr(c, k, v)
                t.updated_at = time.time()
                self.save()
                return True
        return False

    # ==================== 搜索 ====================

    def search(self, query: str, filters: Dict = None,
               limit: int = 50) -> List[Dict]:
        query = (query or '').strip().lower()
        filters = filters or {}
        results = []

        for key, t in self.tables.items():
            if filters.get('sensitivity') and \
                    t.sensitivity != filters['sensitivity']:
                continue
            if filters.get('domain') and t.domain != filters['domain']:
                continue
            if filters.get('connection') and \
                    t.connection != filters['connection']:
                continue
            if filters.get('tag') and \
                    filters['tag'] not in t.tags:
                continue

            score = 0
            matches = []

            if query:
                if query in t.name.lower():
                    score += 100
                    matches.append(('table', t.name))
                if query in t.business_name.lower():
                    score += 80
                    matches.append(('business_name', t.business_name))
                if query in t.comment.lower():
                    score += 40
                    matches.append(('comment', t.comment))
                for tag in t.tags:
                    if query in tag.lower():
                        score += 30
                        matches.append(('tag', tag))
                for c in t.columns:
                    if query in c.name.lower():
                        score += 20
                        matches.append(('column', c.name))
                    elif query in c.business_name.lower():
                        score += 15
                        matches.append(('column_business', c.business_name))
                    elif query in c.comment.lower():
                        score += 10
                        matches.append(('column_comment', c.comment))

                if score == 0:
                    continue

            results.append({
                'key': key,
                'table': t,
                'score': score,
                'matches': matches[:5],
            })

        results.sort(key=lambda x: (
            -x['score'],
            -x['table'].query_count,
            -x['table'].last_queried,
        ))

        return results[:limit]

    # ==================== 使用统计 ====================

    def record_query(self, db: str, table: str, sql: str = ''):
        key = f"{db}.{table}"
        if key not in self.tables:
            return
        t = self.tables[key]
        t.query_count += 1
        t.last_queried = time.time()

        if sql:
            preview = sql[:200].replace('\n', ' ')
            if preview not in t.top_queries:
                t.top_queries.insert(0, preview)
                t.top_queries = t.top_queries[:10]

        self.save()

    def record_query_from_sql(self, sql: str, db: str = None):
        from .lineage import LineageAnalyzer
        tables = LineageAnalyzer.extract_tables(sql)
        for t in tables:
            if db:
                self.record_query(db, t['table'], sql)
            else:
                for key in self.tables:
                    if key.endswith(f".{t['table']}"):
                        self.record_query(
                            self.tables[key].database,
                            t['table'], sql)
                        break

    # ==================== 统计 ====================

    def stats(self) -> dict:
        sensitivity_count = {k: 0 for k in SENSITIVITY_LEVELS}
        domain_count = {d: len(ts) for d, ts in self.domains.items()}

        total_cols = 0
        for t in self.tables.values():
            sensitivity_count[t.sensitivity] = \
                sensitivity_count.get(t.sensitivity, 0) + 1
            total_cols += len(t.columns)

        hot_tables = sorted(
            self.tables.values(),
            key=lambda x: -x.query_count)[:10]

        return {
            'total_tables': len(self.tables),
            'total_columns': total_cols,
            'sensitivity_dist': sensitivity_count,
            'domain_dist': domain_count,
            'hot_tables': [
                {'key': f"{t.database}.{t.name}",
                 'count': t.query_count}
                for t in hot_tables
            ],
        }

    def list_domains(self) -> List[str]:
        return sorted(self.domains.keys())

    def list_tags(self) -> List[str]:
        tags = set()
        for t in self.tables.values():
            tags.update(t.tags)
        return sorted(tags)

    def get_table(self, db: str, table: str) -> Optional[TableMeta]:
        return self.tables.get(f"{db}.{table}")

    def clear(self):
        self.tables.clear()
        self.domains.clear()
        self.save()

    # ==================== 敏感数据扫描 ====================

    def scan_sensitive(self) -> List[Dict]:
        result = []
        for key, t in self.tables.items():
            for c in t.columns:
                level = SENSITIVITY_LEVELS.get(c.sensitivity, {})
                if level.get('level', 0) >= 2:
                    result.append({
                        'table': key,
                        'column': c.name,
                        'sensitivity': c.sensitivity,
                        'label': level.get('label', ''),
                        'color': level.get('color', ''),
                    })
        result.sort(key=lambda x: -SENSITIVITY_LEVELS[x['sensitivity']]['level'])
        return result