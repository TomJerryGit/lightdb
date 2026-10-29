"""
``btree_pyx`` 的纯 Python 回退
=============================

复用：页布局常量与 ``lightdb/storage/btree.py`` **v3** 完全一致
（LEAF_HEADER=6, INTERNAL_HEADER=6, LEAF_ENTRY_SIZE=24,
INTERNAL_ENTRY_SIZE=20, PAGE_HEADER_SIZE=24；键为 16 字节大端 128 位，
页头 reserved[21] 格式标记=3），因此回退读同一份页字节就能得到与 .pyx
相同的结果。
``RawBTreeIndex`` 用 ``bisect`` 维护有序键数组，替代 .pyx 的 C 数组游标插入。

=========================================================================
重要：本模块**未经编译验证**。本机无 Cython、无 C 编译器，以下函数是
按 ``btree_pyx.pyx`` 逐行静态比对写出的等价实现，**没有**性能加速，
也**没有**与编译产物对照过。
=========================================================================

已知与 .pyx 的行为差异:
    1. ``RawBTreeIndex`` 在 .pyx 里是"定容有序数组"（``__cinit__`` 分配
       ``capacity`` 个槽位，满了抛 ``RuntimeError``）。回退同样在超过
       ``capacity`` 时抛 ``RuntimeError("索引已满")``，语义一致；但回退
       底层是 Python list，容量只是显式校验。
    2. .pyx 存的是 ``int32``/``uint128`` 定宽整数，超范围会静默回绕；
       回退把 page_id / slot_id 折叠到 int32（与 C 强转一致），key 保持
       Python 大整数（不静默回绕；v3 键是 128 位内大端编码）。
    3. ``compare_keys_bulk`` 在 .pyx 里是 ``cpdef`` 且带
       ``const unsigned char*`` 参数 —— Python 层无法转换指针，本身就是
       编译错误（本轮已改为 ``const unsigned char[:]`` memoryview 签名）。
       回退接受 bytes / bytearray / memoryview / 整数序列。
"""
import bisect

from lightdb.storage.btree import (  # noqa: F401  (re-export 供一致性核对)
    INTERNAL_ENTRY_SIZE,
    INTERNAL_HEADER,
    LEAF_ENTRY_SIZE,
    LEAF_HEADER,
)
from lightdb.storage.page import PAGE_HEADER_SIZE  # noqa: F401

__all__ = [
    'BTreeLeafView',
    'INTERNAL_ENTRY_SIZE',
    'INTERNAL_HEADER',
    'LEAF_ENTRY_SIZE',
    'LEAF_HEADER',
    'PAGE_HEADER_SIZE',
    'RawBTreeIndex',
    'compare_keys_bulk',
    'internal_find_child',
    'leaf_lower_bound',
    'leaf_range_scan',
    'leaf_search',
]

_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1
_INT32_SPAN = 2 ** 32


# ==================== 定宽整数读写（与 memcpy 小端一致）====================

def _int32_at(buf, offset):
    v = buf[offset] | (buf[offset + 1] << 8) | (buf[offset + 2] << 16) \
        | (buf[offset + 3] << 24)
    return v - _INT32_SPAN if v > _INT32_MAX else v


def _uint16_at(buf, offset):
    return buf[offset] | (buf[offset + 1] << 8)


def _uint128_at(buf, offset):
    """v3 主键键：16 字节大端 128 位（与 storage/btree.py 页格式一致）"""
    v = 0
    for i in range(16):
        v = (v << 8) | buf[offset + i]
    return v


def _as_int32(v):
    """模拟 C 的 (int32_t) 强转：超范围静默回绕"""
    return ((int(v) - _INT32_MIN) % _INT32_SPAN) + _INT32_MIN


def _to_bytes_view(a):
    """接受 bytes/bytearray/memoryview/整数序列，返回可按下标取值且元素为 int 的对象"""
    if isinstance(a, (bytes, bytearray)):
        return a
    if isinstance(a, memoryview):
        return a.cast('B') if a.format != 'B' else a
    return bytes(bytearray(int(x) & 0xFF for x in a))


# ==================== 叶子节点 ====================

def _leaf_n_keys(data):
    return _uint16_at(data, PAGE_HEADER_SIZE)


def _leaf_key_at(data, i):
    return _uint128_at(data,
                       PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE)


def _leaf_pid_at(data, i):
    return _int32_at(data,
                     PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 16)


def _leaf_sid_at(data, i):
    return _int32_at(data,
                     PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 20)


def leaf_search(data, key):
    """叶子节点二分查找 → (found, page_id, slot_id)"""
    data = _to_bytes_view(data)
    lo = 0
    hi = _leaf_n_keys(data) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        k = _leaf_key_at(data, mid)
        if k == key:
            return (True, _leaf_pid_at(data, mid), _leaf_sid_at(data, mid))
        if k < key:
            lo = mid + 1
        else:
            hi = mid - 1
    return (False, 0, 0)


def leaf_lower_bound(data, key):
    """第一个 >= key 的位置"""
    data = _to_bytes_view(data)
    lo = 0
    hi = _leaf_n_keys(data)
    while lo < hi:
        mid = (lo + hi) >> 1
        if _leaf_key_at(data, mid) < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def leaf_range_scan(data, start_key, end_key):
    """叶子节点范围扫描 → [(key, pid, sid), ...]（闭区间）"""
    data = _to_bytes_view(data)
    result = []
    for i in range(_leaf_n_keys(data)):
        k = _leaf_key_at(data, i)
        if k < start_key:
            continue
        if k > end_key:
            break
        result.append((k, _leaf_pid_at(data, i), _leaf_sid_at(data, i)))
    return result


# ==================== 内部节点 ====================

def _internal_n_keys(data):
    return _uint16_at(data, PAGE_HEADER_SIZE)


def _internal_key_at(data, i):
    return _uint128_at(
        data, PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE)


def _internal_child_at(data, i):
    return _int32_at(
        data,
        PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE + 16)


def _internal_first_child(data):
    return _int32_at(data, PAGE_HEADER_SIZE + 2)


def internal_find_child(data, key):
    """找内部节点应下推的子节点 page_id"""
    data = _to_bytes_view(data)
    n = _internal_n_keys(data)
    child = _internal_first_child(data)
    for i in range(n):
        if key < _internal_key_at(data, i):
            return child
        child = _internal_child_at(data, i)
    return child


# ==================== 批量键比较 ====================

def compare_keys_bulk(a, b, n):
    """批量比较两个 **128 位**键数组（v3：每个键 16 字节大端），返回 -1/0/1"""
    a = _to_bytes_view(a)
    b = _to_bytes_view(b)
    for i in range(n):
        ka = _uint128_at(a, i * 16)
        kb = _uint128_at(b, i * 16)
        if ka < kb:
            return -1
        if ka > kb:
            return 1
    return 0


# ==================== 零拷贝叶子视图 ====================

class BTreeLeafView:
    """叶子节点视图（回退版：持有一份 bytes，接口与 .pyx 一致）"""

    __slots__ = ('_data', '_n')

    def __init__(self, page_data):
        self._data = _to_bytes_view(bytes(page_data))
        self._n = _leaf_n_keys(self._data)

    @property
    def n_keys(self):
        return self._n

    def search(self, key):
        return leaf_search(self._data, key)

    def scan(self, start, end):
        return leaf_range_scan(self._data, start, end)


# ==================== 内存索引 ====================

class RawBTreeIndex:
    """
    纯 Python 的内存有序索引（对应 .pyx 的 C 数组版本）

    键按升序存放，重复键（insert 时）按"后插入者覆盖"处理 —— 与 .pyx
    的游标插入一致。
    """

    __slots__ = ('_keys', '_pids', '_sids', '_capacity')

    def __init__(self, capacity=1024):
        self._capacity = int(capacity)
        self._keys = []
        self._pids = []
        self._sids = []

    def __len__(self):
        return len(self._keys)

    @property
    def capacity(self):
        return self._capacity

    def insert(self, key, pid, sid):
        key = int(key)
        pid = _as_int32(pid)
        sid = _as_int32(sid)

        i = bisect.bisect_left(self._keys, key)
        if i < len(self._keys) and self._keys[i] == key:
            self._pids[i] = pid
            self._sids[i] = sid
            return
        if len(self._keys) >= self._capacity:
            raise RuntimeError("索引已满")
        self._keys.insert(i, key)
        self._pids.insert(i, pid)
        self._sids.insert(i, sid)

    def search(self, key):
        i = bisect.bisect_left(self._keys, int(key))
        if i < len(self._keys) and self._keys[i] == key:
            return (True, self._pids[i], self._sids[i])
        return (False, 0, 0)

    def find_position(self, key):
        return bisect.bisect_left(self._keys, int(key))

    def delete(self, key):
        i = bisect.bisect_left(self._keys, int(key))
        if i >= len(self._keys) or self._keys[i] != key:
            return False
        del self._keys[i]
        del self._pids[i]
        del self._sids[i]
        return True

    def scan(self, start, end):
        """闭区间范围扫描 → [(key, pid, sid), ...]"""
        i = bisect.bisect_left(self._keys, start)
        result = []
        n = len(self._keys)
        while i < n and self._keys[i] <= end:
            result.append((self._keys[i], self._pids[i], self._sids[i]))
            i += 1
        return result

    # ---------- 便于与 storage.btree 的 BTree 做交叉核对 ----------

    def keys(self):
        return list(self._keys)
