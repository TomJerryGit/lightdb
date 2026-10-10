"""
B+Tree 优化版
- 批量插入
- 缓存友好
"""
from .btree import BTree


class OptimizedBTree(BTree):
    """优化版 B+Tree"""

    def bulk_insert(self, items: list):
        """
        批量插入（已排序）
        items: [(key, page_id, slot_id), ...]
        """
        for key, pid, sid in items:
            self.insert(key, pid, sid)

    def search_range(self, start, end):
        """范围查询（生成器）"""
        return self.scan(start, end)

    def bulk_delete(self, keys: list) -> int:
        n = 0
        for k in keys:
            if self.delete(k):
                n += 1
        return n


class CachedBTree:
    """
    带 LRU 缓存的 B+Tree 包装
    """

    def __init__(self, btree: BTree, cache_size: int = 1024):
        self.btree = btree
        self.cache_size = cache_size
        from collections import OrderedDict
        self.cache = OrderedDict()
        self.hits = 0
        self.misses = 0

    def search(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        result = self.btree.search(key)
        if result:
            self.cache[key] = result
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        return result

    def insert(self, key, pid, sid):
        self.btree.insert(key, pid, sid)
        self.cache.pop(key, None)

    def delete(self, key):
        self.cache.pop(key, None)
        return self.btree.delete(key)

    def scan(self, start=None, end=None):
        return self.btree.scan(start, end)

    def stats(self):
        total = self.hits + self.misses
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': self.hits / total if total else 0,
            'cache_size': len(self.cache),
        }