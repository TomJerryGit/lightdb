"""
数据虚拟化
- 虚拟表：把 SQL 结果作为逻辑表
- 逻辑模型：跨库统一视图
- 可像真实表一样被查询
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional


VIRTUAL_FILE = Path.home() / '.lightdb_studio' / 'virtual_tables.json'


@dataclass
class VirtualColumn:
    name: str
    type: str = "VARCHAR"
    source_expr: str = ""     # 映射到源 SQL 的表达式
    comment: str = ""


@dataclass
class VirtualTable:
    """虚拟表"""
    name: str = ""
    description: str = ""
    # 来源
    sources: List[Dict] = field(default_factory=list)
    # 例: [{'connection': 'prod', 'sql': 'SELECT ...', 'alias': 't1'}]

    # 定义（一条 SQL，可以用 {source_alias} 引用来源）
    definition: str = ""       # 空则单源直接返回

    # 列定义
    columns: List[VirtualColumn] = field(default_factory=list)

    # 缓存
    cache_enabled: bool = False
    cache_ttl: int = 300       # 秒
    _cache_data: List = field(default_factory=list)
    _cache_columns: List[str] = field(default_factory=list)
    _cache_time: float = 0

    # 使用统计
    query_count: int = 0
    last_queried: float = 0

    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def is_cache_valid(self) -> bool:
        if not self.cache_enabled:
            return False
        if not self._cache_time:
            return False
        return (time.time() - self._cache_time) < self.cache_ttl

    def to_dict(self):
        d = asdict(self)
        # 不持久化缓存数据
        d.pop('_cache_data', None)
        d.pop('_cache_columns', None)
        d.pop('_cache_time', None)
        return d


class VirtualizationEngine:
    """数据虚拟化引擎"""

    def __init__(self, conn_provider: Callable = None):
        """
        conn_provider(name) -> DBConnection
        """
        self.file = VIRTUAL_FILE
        self.conn_provider = conn_provider
        self.tables: Dict[str, VirtualTable] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def set_conn_provider(self, provider):
        self.conn_provider = provider

    # ==================== 持久化 ====================

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for name, td in data.get('tables', {}).items():
                cols = [VirtualColumn(**c)
                        for c in td.pop('columns', [])]
                t = VirtualTable(**td)
                t.columns = cols
                t._cache_data = []
                t._cache_columns = []
                t._cache_time = 0
                self.tables[name] = t
        except Exception:
            pass

    def save(self):
        data = {
            'tables': {n: t.to_dict()
                        for n, t in self.tables.items()}
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== CRUD ====================

    def create_table(self, name: str, sources: List[Dict],
                     definition: str = "",
                     description: str = "",
                     cache_enabled: bool = False,
                     cache_ttl: int = 300) -> VirtualTable:
        t = VirtualTable(
            name=name,
            description=description,
            sources=sources,
            definition=definition,
            cache_enabled=cache_enabled,
            cache_ttl=cache_ttl,
        )
        self.tables[name] = t
        self.save()
        return t

    def update_table(self, name: str, **kwargs):
        if name not in self.tables:
            return False
        t = self.tables[name]
        for k, v in kwargs.items():
            if hasattr(t, k):
                setattr(t, k, v)
        t.updated_at = time.time()
        t._cache_time = 0   # 清缓存
        self.save()
        return True

    def remove_table(self, name: str):
        if name in self.tables:
            del self.tables[name]
            self.save()
            return True
        return False

    def get_table(self, name: str) -> Optional[VirtualTable]:
        return self.tables.get(name)

    def list_tables(self) -> List[VirtualTable]:
        return list(self.tables.values())

    # ==================== 查询 ====================

    def execute(self, name: str, where: str = "",
                limit: int = 0, use_cache: bool = True) -> tuple:
        """
        执行虚拟表查询
        返回 (columns, rows)
        """
        t = self.tables.get(name)
        if not t:
            raise ValueError(f"虚拟表 {name} 不存在")

        # 检查缓存
        if use_cache and t.is_cache_valid():
            t.query_count += 1
            t.last_queried = time.time()
            cols = list(t._cache_columns)
            rows = list(t._cache_data)
            if where:
                rows = self._filter(rows, cols, where)
            if limit:
                rows = rows[:limit]
            return cols, rows

        # 执行
        if len(t.sources) == 1 and not t.definition:
            # 单源直接返回
            src = t.sources[0]
            conn = self._get_conn(src['connection'])
            sql = src['sql']
            if where:
                sql = f"SELECT * FROM ({sql}) AS t WHERE {where}"
            if limit:
                sql = f"{sql} LIMIT {limit}"
            cols, rows = conn.query(sql)

        else:
            # 多源：先把每个源的数据取出来
            source_data = {}
            for src in t.sources:
                conn = self._get_conn(src['connection'])
                cols, rows = conn.query(src['sql'])
                alias = src.get('alias', src['connection'])
                source_data[alias] = {'columns': cols, 'rows': rows}

            # 简化定义：从第一个源做投影，其他源做 JOIN
            # 完整实现需要用 SQL 引擎执行虚拟查询
            if t.definition:
                cols, rows = self._execute_definition(
                    t.definition, source_data)
            else:
                # 直接用第一个源
                first_key = next(iter(source_data))
                cols = source_data[first_key]['columns']
                rows = source_data[first_key]['rows']

            if where:
                rows = self._filter(rows, cols, where)
            if limit:
                rows = rows[:limit]

        # 更新列定义
        if not t.columns:
            t.columns = [VirtualColumn(name=c)
                         for c in cols]

        # 更新缓存
        if t.cache_enabled:
            t._cache_columns = list(cols)
            t._cache_data = list(rows)
            t._cache_time = time.time()

        t.query_count += 1
        t.last_queried = time.time()
        self.save()

        return cols, rows

    def _get_conn(self, name: str):
        if not self.conn_provider:
            raise RuntimeError("未配置连接提供者")
        conn = self.conn_provider(name)
        if not conn:
            raise ValueError(f"连接 {name} 不存在")
        return conn

    def _execute_definition(self, definition: str,
                            source_data: Dict) -> tuple:
        """
        执行虚拟表定义（简化）
        definition 支持: SELECT cols FROM {alias}
        """
        cols, rows = [], []
        # 解析 SELECT 部分
        m = re.match(
            r'\s*SELECT\s+(.+?)\s+FROM\s+\{(\w+)\}(?:\s+WHERE\s+(.+?))?(?:\s+ORDER\s+BY\s+(.+?))?$',
            definition, re.IGNORECASE | re.DOTALL)

        if not m:
            # 降级：第一个源
            first_key = next(iter(source_data))
            return source_data[first_key]['columns'], \
                   source_data[first_key]['rows']

        select_cols = m.group(1)
        source_alias = m.group(2)
        where_clause = m.group(3)

        if source_alias not in source_data:
            first_key = next(iter(source_data))
            return source_data[first_key]['columns'], \
                   source_data[first_key]['rows']

        src = source_data[source_alias]
        src_cols = src['columns']
        src_rows = src['rows']

        # 投影
        if select_cols.strip() == '*':
            target_cols = list(src_cols)
            target_idx = list(range(len(src_cols)))
        else:
            target_cols = []
            target_idx = []
            for c in select_cols.split(','):
                c = c.strip().strip('`"')
                if c in src_cols:
                    target_cols.append(c)
                    target_idx.append(src_cols.index(c))

        rows = [[r[i] for i in target_idx] for r in src_rows]

        # WHERE
        if where_clause:
            rows = self._filter(rows, target_cols, where_clause)

        return target_cols, rows

    def _filter(self, rows, cols, where: str):
        """简单 WHERE 过滤"""
        # 支持: col op value AND col2 op value2
        conditions = re.split(r'\s+AND\s+', where, flags=re.IGNORECASE)

        result = list(rows)
        for cond in conditions:
            cond = cond.strip()
            m = re.match(
                r'[`"]?(\w+)[`"]?\s*(=|!=|<>|>=|<=|>|<|LIKE)\s*(.+?)$',
                cond, re.IGNORECASE)
            if not m:
                continue
            col, op, val = m.group(1), m.group(2).upper(), m.group(3).strip()
            if col not in cols:
                continue
            ci = cols.index(col)

            val_str = val.strip("'\"")
            try:
                val_num = float(val_str)
            except ValueError:
                val_num = None

            new_rows = []
            for r in result:
                v = r[ci]
                try:
                    if val_num is not None and \
                            isinstance(v, (int, float)):
                        cmp_v = val_num
                    else:
                        cmp_v = val_str
                    if op == '=': ok = v == cmp_v
                    elif op in ('!=', '<>'): ok = v != cmp_v
                    elif op == '>': ok = v > cmp_v
                    elif op == '<': ok = v < cmp_v
                    elif op == '>=': ok = v >= cmp_v
                    elif op == '<=': ok = v <= cmp_v
                    elif op == 'LIKE':
                        import re as _re
                        pat = val_str.replace('%', '.*').replace('_', '.')
                        ok = bool(_re.match('^' + pat + '$', str(v)))
                    else:
                        ok = False
                except (TypeError, ValueError):
                    ok = False
                if ok:
                    new_rows.append(r)
            result = new_rows
        return result

    # ==================== 依赖 ====================

    def dependencies_of(self, name: str) -> List[Dict]:
        """虚拟表依赖的连接"""
        t = self.tables.get(name)
        if not t:
            return []
        return [
            {'connection': s.get('connection'),
             'sql': s.get('sql', '')[:200]}
            for s in t.sources
        ]

    def stats(self) -> dict:
        return {
            'total_tables': len(self.tables),
            'cached': sum(1 for t in self.tables.values()
                          if t.cache_enabled),
            'total_queries': sum(t.query_count
                                  for t in self.tables.values()),
        }