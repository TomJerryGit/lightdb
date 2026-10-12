# cython: language_level=3, boundscheck=False, wraparound=False
# =============================================================================
# ⚠️ 未在本机验证编译：本机未安装 Cython，也没有任何 C 编译器
#    （import Cython → ModuleNotFoundError；gcc / cl / clang 全部未找到），
#    因此本文件**从未被编译过**，以下修复只是按 Cython 语义做的静态更正。
#    验证条件：pip install cython 且具备 C 编译器（Windows 用 MSVC cl.exe，
#    Linux 用 gcc），然后运行 scripts/build_cython.sh。
# =============================================================================
"""
哈希表（Cython 实现）
"""
# 修复 1：_hash() 里用了 uint64_t，这里必须一并 cimport（原先只 cimport 了
#        int64_t / uint32_t，编译时会报 "'uint64_t' is not a type identifier"）。
from libc.stdint cimport int32_t, int64_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, free, calloc
from libc.string cimport memset


cdef class IntHashTable:
    """
    简单开放寻址哈希表
    key: int64_t, value: int32_t
    """
    cdef int64_t* keys
    cdef int32_t* values
    cdef uint32_t* used
    cdef uint32_t capacity
    cdef uint32_t size

    def __cinit__(self, int capacity=1024):
        # 向上取 2 的幂
        cdef uint32_t cap = 16
        while cap < capacity:
            cap <<= 1
        self.capacity = cap
        self.size = 0
        self.keys = <int64_t*>malloc(cap * sizeof(int64_t))
        self.values = <int32_t*>malloc(cap * sizeof(int32_t))
        self.used = <uint32_t*>calloc(cap, sizeof(uint32_t))
        if not self.keys or not self.values or not self.used:
            raise MemoryError()

    def __dealloc__(self):
        if self.keys: free(self.keys)
        if self.values: free(self.values)
        if self.used: free(self.used)

    cdef inline uint32_t _hash(self, int64_t key) nogil:
        cdef uint64_t x = <uint64_t>key
        x = ((x >> 16) ^ x) * 0x45d9f3b
        x = ((x >> 16) ^ x) * 0x45d9f3b
        x = (x >> 16) ^ x
        return <uint32_t>(x & (self.capacity - 1))

    cpdef void put(self, int64_t key, int32_t value):
        cdef uint32_t idx = self._hash(key)
        cdef uint32_t i
        for i in range(self.capacity):
            cdef uint32_t pos = (idx + i) & (self.capacity - 1)
            if not self.used[pos]:
                self.keys[pos] = key
                self.values[pos] = value
                self.used[pos] = 1
                self.size += 1
                return
            if self.keys[pos] == key:
                self.values[pos] = value
                return

    cpdef int32_t get(self, int64_t key, int32_t default=-1):
        cdef uint32_t idx = self._hash(key)
        cdef uint32_t i
        for i in range(self.capacity):
            cdef uint32_t pos = (idx + i) & (self.capacity - 1)
            if not self.used[pos]:
                return default
            if self.keys[pos] == key:
                return self.values[pos]
        return default

    cpdef bint contains(self, int64_t key):
        return self.get(key, -1) != -1

    cpdef bint remove(self, int64_t key):
        # 简化：标记删除
        cdef uint32_t idx = self._hash(key)
        cdef uint32_t i
        for i in range(self.capacity):
            cdef uint32_t pos = (idx + i) & (self.capacity - 1)
            if not self.used[pos]:
                return False
            if self.keys[pos] == key:
                self.used[pos] = 2  # tombstone
                self.size -= 1
                return True
        return False

    def __len__(self):
        return self.size