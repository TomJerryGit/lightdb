# cython: language_level=3, boundscheck=False, wraparound=False
# cython: cdivision=True, initializedcheck=False, nonecheck=False
# =============================================================================
# ⚠️ 未在本机验证编译：本机未安装 Cython，也没有任何 C 编译器
#    （import Cython → ModuleNotFoundError；gcc / cl / clang 全部未找到），
#    因此本文件**从未被编译过**，以下修复只是按 Cython 语义做的静态更正。
#    验证条件：pip install cython 且具备 C 编译器（Windows 用 MSVC cl.exe，
#    Linux 用 gcc），然后运行 scripts/build_cython.sh。
#
# 修复 3：原先 leaf_search / leaf_lower_bound / internal_find_child /
#        compare_keys_bulk / leaf_range_scan 都是
#        ``cpdef ... (const unsigned char* data, ...)``。
#        cpdef 会同时生成 Python 可见的包装函数，而 Python 层无法把
#        bytes/bytearray 转成 ``const unsigned char*``，编译期就会报
#        "Cannot convert Python object to 'const unsigned char *'"。
#        现已改为 ``const unsigned char[:]`` 内存视图签名：既保留 cpdef 的
#        Python 可调用性，又能自动接受 bytes/bytearray/memoryview，
#        并且因为 boundscheck=False，索引仍是裸 C 指针访问（不牺牲性能）。
# =============================================================================
"""
B+Tree 核心（Cython 实现）
- 叶子节点查找
- 键值比较
- 快速二分
"""

from libc.stdint cimport int32_t, uint16_t, uint64_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset


# ==================== 常量 ====================
cdef int LEAF_HEADER = 6       # n_keys(2) + next_leaf(4)
cdef int INTERNAL_HEADER = 6
cdef int LEAF_ENTRY_SIZE = 24   # v3: key(16) + pid(4) + sid(4)
cdef int INTERNAL_ENTRY_SIZE = 20  # v3: key(16) + child(4)
cdef int PAGE_HEADER_SIZE = 24


# ==================== 快速读取（无边界检查）====================

cdef inline int32_t _read_int32(const unsigned char* buf, int offset) nogil:
    cdef int32_t v
    memcpy(&v, buf + offset, 4)
    return v

cdef inline uint64_t _read_int64(const unsigned char* buf, int offset) nogil:
    cdef uint64_t v
    memcpy(&v, buf + offset, 8)
    return v

cdef inline uint16_t _read_uint16(const unsigned char* buf, int offset) nogil:
    cdef uint16_t v
    memcpy(&v, buf + offset, 2)
    return v


cdef inline object _read_key(const unsigned char* buf, int offset):
    """v3 页键：16 字节大端 → Python int（128 位；未编译验证，回退为准）"""
    cdef int i
    cdef object v = 0
    for i in range(16):
        v = (v << 8) | buf[offset + i]
    return v

cdef inline void _write_int32(unsigned char* buf, int offset, int32_t v) nogil:
    memcpy(buf + offset, &v, 4)

cdef inline void _write_int64(unsigned char* buf, int offset, uint64_t v) nogil:
    memcpy(buf + offset, &v, 8)

cdef inline void _write_uint16(unsigned char* buf, int offset, uint16_t v) nogil:
    memcpy(buf + offset, &v, 2)


# ==================== 叶子节点操作 ====================

cdef inline int _leaf_n_keys(const unsigned char* data) nogil:
    return <int>_read_uint16(data, PAGE_HEADER_SIZE)

cdef inline object _leaf_key_at(const unsigned char* data, int i):
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE
    return _read_key(data, offset)


cdef inline int _leaf_pid_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 16
    return _read_int32(data, offset)


cdef inline int _leaf_sid_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE + 20
    return _read_int32(data, offset)


# ==================== 二分查找 ====================

cdef tuple _leaf_search_c(const unsigned char* data, object key):
    """
    叶子节点二分查找（C 指针内核）

    注意：这里**不能**加 nogil —— 返回 tuple 需要创建 Python 对象、
    持有 GIL，``cdef tuple`` 函数一旦标 nogil 就无法编译。
    """
    cdef int n = _leaf_n_keys(data)
    cdef int lo = 0
    cdef int hi = n - 1
    cdef int mid
    cdef object k

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


cpdef tuple leaf_search(const unsigned char[:] data, object key):
    """
    叶子节点二分查找
    返回 (found, page_id, slot_id)

    修复：原签名为 ``cpdef tuple leaf_search(const unsigned char* data, ...)``，
    cpdef 会生成 Python 包装层，而 Python 无法把 bytes 转成
    ``const unsigned char*``，编译期报错。现改用内存视图签名，
    Python 侧可直接传 bytes/bytearray/memoryview。
    """
    return _leaf_search_c(&data[0], key)


cdef int _leaf_lower_bound_c(const unsigned char* data, object key):
    """找第一个 >= key 的位置（C 指针内核）"""
    cdef int n = _leaf_n_keys(data)
    cdef int lo = 0
    cdef int hi = n
    cdef int mid

    while lo < hi:
        mid = (lo + hi) >> 1
        if _leaf_key_at(data, mid) < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


cpdef int leaf_lower_bound(const unsigned char[:] data, object key):
    """
    找第一个 >= key 的位置（修复 3：指针参数 → 内存视图签名）
    """
    return _leaf_lower_bound_c(&data[0], key)


# ==================== 内部节点 ====================

cdef inline int _internal_n_keys(const unsigned char* data) nogil:
    return <int>_read_uint16(data, PAGE_HEADER_SIZE)

cdef inline object _internal_key_at(const unsigned char* data, int i):
    cdef int offset = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE
    return _read_key(data, offset)

cdef inline int _internal_child_at(const unsigned char* data, int i) nogil:
    cdef int offset = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE + 16
    return _read_int32(data, offset)

cdef inline int _internal_first_child(const unsigned char* data) nogil:
    return _read_int32(data, PAGE_HEADER_SIZE + 2)


cdef int _internal_find_child_c(const unsigned char* data, object key):
    """找内部节点的子节点（C 指针内核）"""
    cdef int n = _internal_n_keys(data)
    cdef int child = _internal_first_child(data)
    cdef int i
    cdef object k

    for i in range(n):
        k = _internal_key_at(data, i)
        if key < k:
            return child
        child = _internal_child_at(data, i)
    return child


cpdef int internal_find_child(const unsigned char[:] data, object key):
    """
    找内部节点的子节点（修复 3：指针参数 → 内存视图签名）
    """
    return _internal_find_child_c(&data[0], key)


# ==================== 批量键比较 ====================

cdef int _compare_keys_bulk_c(const unsigned char* a,
                              const unsigned char* b, int n):
    """批量比较两个 128 位键数组（v3：每键 16 字节大端），返回 -1/0/1"""
    cdef int i
    cdef object ka, kb
    for i in range(n):
        ka = _read_key(a, i * 16)
        kb = _read_key(b, i * 16)
        if ka < kb:
            return -1
        elif ka > kb:
            return 1
    return 0


cpdef int compare_keys_bulk(
        const unsigned char[:] a, const unsigned char[:] b,
        int n):
    """
    批量比较两个键数组
    返回 -1 / 0 / 1

    修复 3：原签名两个参数都是 ``const unsigned char*``，cpdef 无法暴露给
    Python；现改为内存视图（调用方传 bytes 即可）。
    """
    return _compare_keys_bulk_c(&a[0], &b[0], n)


# ==================== 范围扫描 ====================

cdef list _leaf_range_scan_c(const unsigned char* data,
                             object start_key, object end_key):
    """叶子节点范围扫描（C 指针内核；返回 list，故不能标 nogil）"""
    cdef int n = _leaf_n_keys(data)
    cdef list result = []
    cdef int i
    cdef object k

    for i in range(n):
        k = _leaf_key_at(data, i)
        if k < start_key:
            continue
        if k > end_key:
            break
        result.append((k, _leaf_pid_at(data, i), _leaf_sid_at(data, i)))

    return result


cpdef list leaf_range_scan(
        const unsigned char[:] data,
        object start_key, object end_key):
    """
    叶子节点范围扫描
    返回 [(key, pid, sid), ...]

    修复 3：指针参数 → 内存视图签名。
    """
    return _leaf_range_scan_c(&data[0], start_key, end_key)


# ==================== C 结构版本（更快）====================

cdef class BTreeLeafView:
    """
    零拷贝叶子节点视图
    """
    cdef const unsigned char* data
    cdef int n

    def __cinit__(self, bytes page_data):
        # ⚠️ 与编译无关的隐患（本轮不改行为，仅标注）：Cython 允许把 bytes
        #    隐式转成 const unsigned char*，但 C 字段**不持有该 bytes 的引用**，
        #    page_data 一旦被回收，self.data 就是悬垂指针。若要真正安全，
        #    应额外加一个 ``cdef bytes _keepalive`` 字段持有 page_data。
        self.data = page_data
        self.n = _leaf_n_keys(self.data)

    @property
    def n_keys(self):
        return self.n

    cpdef tuple search(self, object key):
        cdef int lo = 0
        cdef int hi = self.n - 1
        cdef int mid
        cdef object k
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

    cpdef list scan(self, object start, object end):
        cdef list result = []
        cdef int i
        cdef object k
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
    cdef uint64_t* keys
    cdef int32_t* pids
    cdef int32_t* sids
    cdef int n
    cdef int capacity

    def __cinit__(self, int capacity=1024):
        self.capacity = capacity
        self.n = 0
        self.keys = <uint64_t*>malloc(capacity * sizeof(uint64_t))
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

    cpdef void insert(self, uint64_t key, int32_t pid, int32_t sid):
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

    cpdef tuple search(self, uint64_t key):
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

    cpdef int find_position(self, uint64_t key):
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

    cpdef bint delete(self, uint64_t key):
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

    cpdef list scan(self, uint64_t start, uint64_t end):
        """范围扫描"""
        cdef list result = []
        cdef int i = self.find_position(start)
        while i < self.n and self.keys[i] <= end:
            result.append((self.keys[i], self.pids[i], self.sids[i]))
            i += 1
        return result