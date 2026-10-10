"""
查询计划缓存
- 缓存 AST（解析结果）
- 缓存优化后计划
- LRU 淘汰
- 表变更时自动失效
"""
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CachedPlan:
    sql_hash: str
    sql: str
    parsed_ast: Any
    optimized_plan: Any = None
    referenced_tables: list = None
    hit_count: int = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if self.referenced_tables is None:
            self.referenced_tables = []


class PlanCache:
    """查询计划缓存"""

    def __init__(self, max_size: int = 500, ttl: float = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self.lock = threading.RLock()
        self.cache: OrderedDict = OrderedDict()

        # 表 -> [sql_hash] 反向索引，用于失效
        self.table_index: Dict[str, set] = {}

        # 统计
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    # ==================== Key ====================

    @staticmethod
    def hash_sql(sql: str) -> str:
        # 归一化：去多余空白
        normalized = ' '.join(sql.split())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    # ==================== Get/Put ====================

    def get_parsed(self, sql: str):
        """获取缓存的 AST"""
        h = self.hash_sql(sql)
        with self.lock:
            entry = self.cache.get(h)
            if entry is None:
                self.misses += 1
                return None
            if time.time() - entry.created_at > self.ttl:
                self._remove(h)
                self.misses += 1
                return None
            self.cache.move_to_end(h)
            entry.hit_count += 1
            self.hits += 1
            return entry.parsed_ast

    def put_parsed(self, sql: str, ast,
                   tables: list = None):
        h = self.hash_sql(sql)
        with self.lock:
            entry = CachedPlan(
                sql_hash=h,
                sql=sql,
                parsed_ast=ast,
                referenced_tables=tables or [],
            )
            self.cache[h] = entry
            self.cache.move_to_end(h)

            # 反向索引
            for t in (tables or []):
                self.table_index.setdefault(t, set()).add(h)

            # LRU 淘汰
            while len(self.cache) > self.max_size:
                k, v = self.cache.popitem(last=False)
                self._remove_from_index(k, v)

    def get_plan(self, sql: str):
        """获取优化后的计划"""
        h = self.hash_sql(sql)
        with self.lock:
            entry = self.cache.get(h)
            return entry.optimized_plan if entry else None

    def put_plan(self, sql: str, plan):
        h = self.hash_sql(sql)
        with self.lock:
            entry = self.cache.get(h)
            if entry:
                entry.optimized_plan = plan

    # ==================== 失效 ====================

    def invalidate_table(self, table: str):
        """表变更时失效相关缓存"""
        with self.lock:
            hashes = self.table_index.pop(table, set())
            for h in list(hashes):
                self._remove(h)
                self.invalidations += 1

    def invalidate_all(self):
        with self.lock:
            self.cache.clear()
            self.table_index.clear()
            self.invalidations += 1

    def _remove(self, h: str):
        entry = self.cache.pop(h, None)
        if entry:
            self._remove_from_index(h, entry)

    def _remove_from_index(self, h: str, entry: CachedPlan):
        for t in entry.referenced_tables:
            s = self.table_index.get(t)
            if s:
                s.discard(h)

    # ==================== 统计 ====================

    def stats(self) -> dict:
        with self.lock:
            total = self.hits + self.misses
            return {
                'size': len(self.cache),
                'max_size': self.max_size,
                'hits': self.hits,
                'misses': self.misses,
                'hit_rate': self.hits / total if total else 0,
                'invalidations': self.invalidations,
            }


# ==================== 集成到执行器 ====================

class CachedParser:
    """带缓存的解析器包装"""

    def __init__(self, parser, cache: PlanCache):
        self.parser = parser
        self.cache = cache

    def parse(self, sql: str):
        # 1. 尝试缓存
        ast = self.cache.get_parsed(sql)
        if ast is not None:
            return ast

        # 2. 解析
        ast = self.parser.parse(sql)

        # 3. 提取表名
        tables = self._extract_tables(ast)

        # 4. 缓存
        self.cache.put_parsed(sql, ast, tables)
        return ast

    def _extract_tables(self, ast) -> list:
        """从 AST 提取涉及的表"""
        if ast is None:
            return []
        tables = []
        if isinstance(ast, dict):
            t = ast.get('table')
            if t:
                tables.append(t)
            for j in ast.get('joins', []):
                if isinstance(j, dict) and j.get('table'):
                    tables.append(j['table'])
            for k in ('subquery', 'inner'):
                sub = ast.get(k)
                if sub:
                    tables.extend(self._extract_tables(sub))
        return tables