# cython: language_level=3, boundscheck=False, wraparound=False
"""
哈希表（Cython 实现）
"""
from libc.stdint cimport int64_t, uint32_t
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