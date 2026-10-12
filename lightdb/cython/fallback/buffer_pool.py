"""
``buffer_pool_pyx`` 的纯 Python 回退
===================================

复用手法：与 ``lightdb/storage/buffer_pool.py`` 的 ``BufferPool`` 相同 ——
用 ``collections.OrderedDict`` 维护访问顺序（该模块本身就用它做 LRU 帧表）。
回退把 .pyx 的"数组 + 逻辑时钟"替换为 ``OrderedDict``，语义严格等价：
最近使用移到队尾，淘汰时弹出队首，即标准 LRU。

未在本机验证：本机无 Cython、无 C 编译器，``buffer_pool_pyx.pyx`` 从未
编译过；以下等价性判断来自源码静态比对。

已知与 .pyx 的差异:
    1. .pyx 的 ``get`` 需要线性扫描内部数组，回退是哈希查找（更快，但
       **这不是本项目的"加速"**，只是数据结构差异）。
    2. .pyx 的 key/value 是 C ``int``（32 位），回退按 int32 折叠，超范围
       静默回绕；``get`` 未命中统一返回 -1（与 .pyx 相同，注意这意味着
       值 -1 与未命中不可区分 —— 保持 .pyx 原有语义）。
"""
from collections import OrderedDict

__all__ = ['FastLRU']

_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1
_INT32_SPAN = 2 ** 32
_MISS = -1


def _as_int32(v):
    return ((int(v) - _INT32_MIN) % _INT32_SPAN) + _INT32_MIN


class FastLRU:
    """高速 LRU 缓存（回退版，OrderedDict 实现，接口同 .pyx）"""

    __slots__ = ('_capacity', '_data', '_hits', '_misses', '_evictions')

    def __init__(self, capacity):
        self._capacity = int(capacity)
        if self._capacity <= 0:
            raise ValueError("capacity 必须为正整数")
        self._data = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def capacity(self):
        return self._capacity

    def get(self, key):
        """读取（返回 -1 未命中，同 .pyx）"""
        key = _as_int32(key)
        if key in self._data:
            self._data.move_to_end(key)   # 标记为最近使用
            self._hits += 1
            return self._data[key]
        self._misses += 1
        return _MISS

    def put(self, key, value):
        """写入，容量满时淘汰最久未使用项"""
        key = _as_int32(key)
        value = _as_int32(value)
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)
            return
        if len(self._data) >= self._capacity:
            self._data.popitem(last=False)   # 淘汰最久未使用
            self._evictions += 1
        self._data[key] = value

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return _as_int32(key) in self._data

    def keys(self):
        return list(self._data.keys())

    def stats(self):
        total = self._hits + self._misses
        return {
            'hits': self._hits,
            'misses': self._misses,
            'evictions': self._evictions,
            'hit_rate': self._hits / total if total else 0.0,
            'size': len(self._data),
            'capacity': self._capacity,
        }
