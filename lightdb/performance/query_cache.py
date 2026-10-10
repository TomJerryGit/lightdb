"""
查询缓存 - 只读查询的 LRU 缓存
失效策略：任何写操作清空对应表
"""
import hashlib
import threading
import time
from collections import OrderedDict


class QueryCache:
    def __init__(self, max_size: int = 200, ttl: float = 60.0):
        self.max_size = max_size
        self.ttl = ttl

        self.lock = threading.RLock()
        self.cache = OrderedDict()   # key -> (value, ts, table)
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    def key(self, sql: str, database: str = 'default') -> str:
        h = hashlib.sha256(f"{database}:{sql}".encode('utf-8')).hexdigest()
        return h

    def get(self, sql: str, database: str = 'default'):
        with self.lock:
            k = self.key(sql, database)
            entry = self.cache.get(k)
            if entry is None:
                self.misses += 1
                return None
            value, ts, table = entry
            if time.time() - ts > self.ttl:
                del self.cache[k]
                self.misses += 1
                return None
            self.cache.move_to_end(k)
            self.hits += 1
            return value

    def put(self, sql: str, value, table: str = None, database: str = 'default'):
        with self.lock:
            k = self.key(sql, database)
            self.cache[k] = (value, time.time(), table)
            self.cache.move_to_end(k)
            while len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def invalidate_table(self, table: str):
        """表发生写操作 → 清空该表所有缓存"""
        with self.lock:
            to_del = [k for k, v in self.cache.items() if v[2] == table]
            for k in to_del:
                del self.cache[k]
                self.invalidations += 1

    def invalidate_all(self):
        with self.lock:
            self.invalidations += len(self.cache)
            self.cache.clear()

    def stats(self) -> dict:
        with self.lock:
            total = self.hits + self.misses
            return {
                'size': len(self.cache),
                'hits': self.hits,
                'misses': self.misses,
                'hit_rate': self.hits / total if total else 0.0,
                'invalidations': self.invalidations,
            }