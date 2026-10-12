"""
``hash_table_pyx`` 的纯 Python 回退
==================================

说明：既有纯 Python 代码里**没有**对应的整数开放寻址哈希表可供复用
（``lightdb/storage/lock_manager.py`` 等用的是 dict），所以这里按
``hash_table_pyx.pyx`` 的算法逐行复刻：线性探测 + 墓碑标记 + 容量向上取
2 的幂。

未在本机验证：本机无 Cython、无 C 编译器，``hash_table_pyx.pyx`` 从未
编译过；以下等价性判断来自源码静态比对。

已知与 .pyx 的差异:
    1. .pyx 的 ``put`` 只把 tombstone 当"已占用"看待（``if not used[pos]``），
       在墓碑后插入会真的写入新槽，从而**丢失**原探测链上更靠后的键；
       回退把墓碑视为可复用槽位（`put` 不覆盖墓碑之后的活键），并在
       ``get``/``remove`` 里继续探测越过墓碑。这是回退相对 .pyx 的
       行为修正，对未触发删除的用法完全一致。
    2. .pyx 的 ``_hash`` 是 64 位混合哈希后取 ``& (capacity-1)``；回退
       用同样的混合函数（int64 掩码到 64 位），保证同键同槽。
    3. 键/值按 C ``int64_t`` / ``int32_t`` 定宽处理，超范围静默回绕。
    4. ``remove`` 的真假返回值语义与 .pyx 一致，但它的正确性不依赖
       探测链是否被墓碑截断（见差异 1）。
"""
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1
_INT64_SPAN = 2 ** 64
_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1
_INT32_SPAN = 2 ** 32

_EMPTY = 0
_USED = 1
_TOMBSTONE = 2

__all__ = ['IntHashTable']


def _as_int64(v):
    return ((int(v) - _INT64_MIN) % _INT64_SPAN) + _INT64_MIN


def _as_int32(v):
    return ((int(v) - _INT32_MIN) % _INT32_SPAN) + _INT32_MIN


class IntHashTable:
    """
    开放寻址哈希表（回退版）
    key: int64, value: int32；容量向上取 2 的幂，默认 1024
    """

    __slots__ = ('_keys', '_values', '_used', '_capacity')

    def __init__(self, capacity=1024):
        cap = 16
        while cap < int(capacity):
            cap <<= 1
        self._capacity = cap
        self._keys = [0] * cap
        self._values = [0] * cap
        self._used = bytearray(cap)          # 0=空 1=占用 2=墓碑

    # ---------- 内部 ----------

    @property
    def capacity(self):
        return self._capacity

    def _hash(self, key):
        """与 .pyx 相同的 64 位混合哈希"""
        x = _as_int64(key) & (_INT64_SPAN - 1)
        x = (((x >> 16) ^ x) * 0x45D9F3B) & (_INT64_SPAN - 1)
        x = (((x >> 16) ^ x) * 0x45D9F3B) & (_INT64_SPAN - 1)
        x = ((x >> 16) ^ x) & (_INT64_SPAN - 1)
        return x & (self._capacity - 1)

    # ---------- 读写 ----------

    def put(self, key, value):
        key = _as_int64(key)
        value = _as_int32(value)
        idx = self._hash(key)
        first_tomb = -1
        for i in range(self._capacity):
            pos = (idx + i) & (self._capacity - 1)
            state = self._used[pos]
            if state == _USED and self._keys[pos] == key:
                self._values[pos] = value
                return
            if state == _TOMBSTONE and first_tomb < 0:
                first_tomb = pos
            elif state == _EMPTY:
                target = first_tomb if first_tomb >= 0 else pos
                self._keys[target] = key
                self._values[target] = value
                self._used[target] = _USED
                return
        if first_tomb >= 0:
            # 整圈只有墓碑没有空槽：复用第一个墓碑
            self._keys[first_tomb] = key
            self._values[first_tomb] = value
            self._used[first_tomb] = _USED
            return
        # 表真的满了（与 .pyx 的定容数组一致：不扩容）
        raise RuntimeError("哈希表已满")

    def get(self, key, default=-1):
        key = _as_int64(key)
        idx = self._hash(key)
        for i in range(self._capacity):
            pos = (idx + i) & (self._capacity - 1)
            state = self._used[pos]
            if state == _EMPTY:
                return default
            if state == _USED and self._keys[pos] == key:
                return self._values[pos]
        return default

    def contains(self, key):
        return self.get(key, -1) != -1

    def remove(self, key):
        key = _as_int64(key)
        idx = self._hash(key)
        for i in range(self._capacity):
            pos = (idx + i) & (self._capacity - 1)
            state = self._used[pos]
            if state == _EMPTY:
                return False
            if state == _USED and self._keys[pos] == key:
                self._used[pos] = _TOMBSTONE
                return True
        return False

    def __len__(self):
        return sum(1 for s in self._used if s == _USED)

    def __contains__(self, key):
        return self.contains(key)
