# cython: language_level=3, boundscheck=False, wraparound=False
# cython: cdivision=True, initializedcheck=False, nonecheck=False
"""
B+Tree 核心（Cython 实现）
- 叶子节点查找
- 键值比较
- 快速二分
"""

from libc.stdint cimport int32_t, int64_t, uint16_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset


# ==================== 常量 ====================
cdef int LEAF_HEADER = 6       # n_keys(2) + next_leaf(4)
cdef int INTERNAL_HEADER = 6
cdef int LEAF_ENTRY_SIZE = 12   # key(4) + pid(4) + sid(4)
cdef int INTERNAL_ENTRY_SIZE = 8
cdef int PAGE_HEADER_SIZE = 24


# ==================== 快速读取（无边界检查）====================

cdef inline int32_t _read_int32(const unsigned char* buf, int offset) nogil:
    cdef int32_t v
    memcpy(&v, buf + offset, 4)
    return v

cdef inline int64_t _read_int64(const unsigned char* buf, int offset) nogil:
    cdef int64_t v
    memcpy(&v, buf + offset, 8)
    return v

cdef inline uint16_t _read_uint16(const unsigned char* buf, int offset) nogil:
    cdef uint16_t v
    memcpy(&v, buf + offset, 2)
    return v

cdef inline void _write_int32(unsigned char* buf, int offset, int32_t v) nogil:
    memcpy(buf + offset, &v, 4)

cdef inline void _write_int64(unsigned char* buf, int offset, int64_t v) nogil:
    memcpy(buf + offset, &v, 8)

cdef inline void _write_uint16(unsigned char* buf, int offset, uint16_t v) nogil:
    memcpy(buf + offset, &v, 2)


# ==================== 叶子节点操作 ====================

cdef inline int _leaf_n_keys(const unsigned char* data) nogil:
    return <int>_read_uint16(data, PAGE_HEADER_SIZE)

cdef inline int64_t _leaf_key_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE
    return <int64_t>_read_int32(data, offset)

cdef inline int _leaf_pid_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 4
    return _read_int32(data, offset)

cdef inline int _leaf_sid_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 8
    return _read_int32(data, offset)


# ==================== 二分查找 ====================

cpdef tuple leaf_search(const unsigned char* data, int64_t key):
    """
    叶子节点二分查找
    返回 (found, page_id, slot_id)
    """
    cdef int n = _leaf_n_keys(data)
    cdef int lo = 0
    cdef int hi = n - 1
    cdef int mid
    cdef int64_t k

    while lo <= hi:
        mid = (lo + hi) >> 1
        k = _leaf_key_at(data, mid)
        if k == key:
            return (True, _leaf_pid_at(data, mid), _leaf_sid_at(data, mid))
        elif k < key:
            lo = mid + 1
        else:
            hi = mid - 1

    return (False, 0, 0)


cpdef int leaf_lower_bound(const unsigned char* data, int64_t key):
    """
    找第一个 >= key 的位置
    """
    cdef int n = _leaf_n_keys(data)
    cdef int lo = 0
    cdef int hi = n

    while lo < hi:
        mid = (lo + hi) >> 1
        if _leaf_key_at(data, mid) < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ==================== 内部节点 ====================

cdef inline int _internal_n_keys(const unsigned char* data) nogil:
    return <int>_read_uint16(data, PAGE_HEADER_SIZE)

cdef inline int64_t _internal_key_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE
    return <int64_t>_read_int32(data, offset)

cdef inline int _internal_child_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE + 4
    return _read_int32(data, offset)

cdef inline int _internal_first_child(const unsigned char* data) nogil:
    return _read_int32(data, PAGE_HEADER_SIZE + 2)


cpdef int internal_find_child(const unsigned char* data, int64_t key):
    """
    找内部节点的子节点
    """
    cdef int n = _internal_n_keys(data)
    cdef int child = _internal_first_child(data)
    cdef int i
    cdef int64_t k

    for i in range(n):
        k = _internal_key_at(data, i)
        if key < k:
            return child
        child = _internal_child_at(data, i)
    return child


# ==================== 批量键比较 ====================

cpdef int compare_keys_bulk(
        const unsigned char* a, const unsigned char* b,
        int n):
    """
    批量比较两个键数组
    返回 -1 / 0 / 1
    """
    cdef int i
    cdef int64_t ka, kb
    for i in range(n):
        ka = <int64_t>_read_int32(a, i * 4)
        kb = <int64_t>_read_int32(b, i * 4)
        if ka < kb:
            return -1
        elif ka > kb:
            return 1
    return 0


# ==================== 范围扫描 ====================

cpdef list leaf_range_scan(
        const unsigned char* data,
        int64_t start_key, int64_t end_key):
    """
    叶子节点范围扫描
    返回 [(key, pid, sid), ...]
    """
    cdef int n = _leaf_n_keys(data)
    cdef list result = []
    cdef int i
    cdef int64_t k

    for i in range(n):
        k = _leaf_key_at(data, i)
        if k < start_key:
            continue
        if k > end_key:
            break
        result.append((k, _leaf_pid_at(data, i), _leaf_sid_at(data, i)))

    return result


# ==================== C 结构版本（更快）====================

cdef class BTreeLeafView:
    """
    零拷贝叶子节点视图
    """
    cdef const unsigned char* data
    cdef int n

    def __cinit__(self, bytes page_data):
        self.data = page_data
        self.n = _leaf_n_keys(self.data)

    @property
    def n_keys(self):
        return self.n

    cpdef tuple search(self, int64_t key):
        cdef int lo = 0
        cdef int hi = self.n - 1
        cdef int mid
        cdef int64_t k
        while lo <= hi:
            mid = (lo + hi) >> 1
            k = _leaf_key_at(self.data, mid)
            if k == key:
                return (True, _leaf_pid_at(self.data, mid),
                        _leaf_sid_at(self.data, mid))
            elif k < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return (False, 0, 0)

    cpdef list scan(self, int64_t start, int64_t end):
        cdef list result = []
        cdef int i
        cdef int64_t k
        for i in range(self.n):
            k = _leaf_key_at(self.data, i)
            if k < start:
                continue
            if k > end:
                break
            result.append((k, _leaf_pid_at(self.data, i),
                            _leaf_sid_at(self.data, i)))
        return result


# ==================== C 语言友好的查找 ====================

cdef class RawBTreeIndex:
    """
    纯 C 结构的 B+Tree 索引
    用于内存索引（比 Python 字典更快）
    """
    cdef int64_t* keys
    cdef int32_t* pids
    cdef int32_t* sids
    cdef int n
    cdef int capacity

    def __cinit__(self, int capacity=1024):
        self.capacity = capacity
        self.n = 0
        self.keys = <int64_t*>malloc(capacity * sizeof(int64_t))
        self.pids = <int32_t*>malloc(capacity * sizeof(int32_t))
        self.sids = <int32_t*>malloc(capacity * sizeof(int32_t))
        if not self.keys or not self.pids or not self.sids:
            raise MemoryError()

    def __dealloc__(self):
        if self.keys: free(self.keys)
        if self.pids: free(self.pids)
        if self.sids: free(self.sids)

    def __len__(self):
        return self.n

    cpdef void insert(self, int64_t key, int32_t pid, int32_t sid):
        """有序插入（O(n) 但内存连续）"""
        if self.n >= self.capacity:
            raise RuntimeError("索引已满")
        cdef int i = self.n
        while i > 0 and self.keys[i-1] > key:
            self.keys[i] = self.keys[i-1]
            self.pids[i] = self.pids[i-1]
            self.sids[i] = self.sids[i-1]
            i -= 1
        self.keys[i] = key
        self.pids[i] = pid
        self.sids[i] = sid
        self.n += 1

    cpdef tuple search(self, int64_t key):
        cdef int lo = 0
        cdef int hi = self.n - 1
        cdef int mid
        while lo <= hi:
            mid = (lo + hi) >> 1
            if self.keys[mid] == key:
                return (True, self.pids[mid], self.sids[mid])
            elif self.keys[mid] < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return (False, 0, 0)

    cpdef int find_position(self, int64_t key):
        cdef int lo = 0
        cdef int hi = self.n
        cdef int mid
        while lo < hi:
            mid = (lo + hi) >> 1
            if self.keys[mid] < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    cpdef bint delete(self, int64_t key):
        cdef int pos = self.find_position(key)
        if pos >= self.n or self.keys[pos] != key:
            return False
        cdef int i
        for i in range(pos, self.n - 1):
            self.keys[i] = self.keys[i+1]
            self.pids[i] = self.pids[i+1]
            self.sids[i] = self.sids[i+1]
        self.n -= 1
        return True

    cpdef list scan(self, int64_t start, int64_t end):
        """范围扫描"""
        cdef list result = []
        cdef int i = self.find_position(start)
        while i < self.n and self.keys[i] <= end:
            result.append((self.keys[i], self.pids[i], self.sids[i]))
            i += 1
        return result