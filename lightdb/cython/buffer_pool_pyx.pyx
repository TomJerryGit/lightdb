# cython: language_level=3, boundscheck=False, wraparound=False
"""
缓冲池操作（Cython）
"""
from libc.stdint cimport uint32_t, uint64_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset


cdef int PAGE_SIZE = 4096


cdef class FastLRU:
    """
    高速 LRU 缓存（基于数组）
    """
    cdef int* keys
    cdef int* values
    cdef uint64_t* access_time
    cdef int capacity
    cdef int size
    cdef uint64_t clock

    def __cinit__(self, int capacity):
        self.capacity = capacity
        self.size = 0
        self.clock = 0
        self.keys = <int*>malloc(capacity * sizeof(int))
        self.values = <int*>malloc(capacity * sizeof(int))
        self.access_time = <uint64_t*>malloc(capacity * sizeof(uint64_t))
        if not self.keys or not self.values or not self.access_time:
            raise MemoryError()
        cdef int i
        for i in range(capacity):
            self.keys[i] = -1

    def __dealloc__(self):
        if self.keys: free(self.keys)
        if self.values: free(self.values)
        if self.access_time: free(self.access_time)

    cpdef int get(self, int key):
        """读取（返回 -1 未命中）"""
        cdef int i
        self.clock += 1
        for i in range(self.size):
            if self.keys[i] == key:
                self.access_time[i] = self.clock
                return self.values[i]
        return -1

    cpdef void put(self, int key, int value):
        """写入"""
        cdef int i
        self.clock += 1

        # 已有 → 更新
        for i in range(self.size):
            if self.keys[i] == key:
                self.values[i] = value
                self.access_time[i] = self.clock
                return

        # 未满
        if self.size < self.capacity:
            self.keys[self.size] = key
            self.values[self.size] = value
            self.access_time[self.size] = self.clock
            self.size += 1
            return

        # 淘汰最久未使用
        cdef int oldest = 0
        cdef uint64_t min_t = self.access_time[0]
        for i in range(1, self.size):
            if self.access_time[i] < min_t:
                min_t = self.access_time[i]
                oldest = i

        self.keys[oldest] = key
        self.values[oldest] = value
        self.access_time[oldest] = self.clock

    def __len__(self):
        return self.size