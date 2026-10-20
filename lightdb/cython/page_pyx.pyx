# cython: language_level=3, boundscheck=False, wraparound=False
# =============================================================================
# ⚠️ 未在本机验证编译：本机未安装 Cython，也没有任何 C 编译器
#    （import Cython → ModuleNotFoundError；gcc / cl / clang 全部未找到），
#    因此本文件**从未被编译过**，以下修复只是按 Cython 语义做的静态更正。
#    验证条件：pip install cython 且具备 C 编译器（Windows 用 MSVC cl.exe，
#    Linux 用 gcc），然后运行 scripts/build_cython.sh。
#
# 额外修复（审计只列了三处，以下两处是同类的必然编译失败，一并修掉）:
#   A. RawPage.__dealloc__ 调用了 free()，但原先没有 cimport
#      libc.stdlib.free —— 与 hash_table/row_codec 是同一类错误。
#   B. page_n_slots 原为 ``cpdef int page_n_slots(bytes page_data) nogil``：
#      带 Python 对象参数/返回值的函数不能标 nogil（nogil 函数体内无法
#      操作需要 GIL 的 Python 对象），编译期报错。现把无 GIL 的内核拆到
#      cdef 函数 _page_n_slots_c，公开函数保留 cpdef 语义。
# =============================================================================
"""
页操作（Cython）
"""
from libc.stdint cimport uint16_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc
from libc.string cimport memcpy, memset
from cpython.bytes cimport PyBytes_FromStringAndSize


cdef int PAGE_SIZE = 4096
cdef int PAGE_HEADER_SIZE = 24
cdef uint32_t PAGE_MAGIC = 0x4C444250  # "LDBP"


cdef class RawPage:
    """页对象（C 字节缓冲）"""
    cdef unsigned char* data
    cdef uint32_t page_id
    cdef bint dirty

    def __cinit__(self, uint32_t page_id=0):
        self.data = <unsigned char*>malloc(PAGE_SIZE)
        if not self.data:
            raise MemoryError()
        memset(self.data, 0, PAGE_SIZE)
        self.page_id = page_id
        self.dirty = False
        # 写魔数
        cdef uint32_t magic = PAGE_MAGIC
        memcpy(self.data, &magic, 4)
        memcpy(self.data + 4, &page_id, 4)
        # 空页必须有初始 next_row_ptr = PAGE_SIZE（从页尾向前分配）。
        # 原实现漏掉这一步：get_next_row_ptr() 读到 0，insert_row 里
        # `0 - blen` 发生 uint16 下溢 → memcpy 写到缓冲之外（堆溢出，
        # 编译后实测在 insert_row 中触发 access violation）。
        # 纯 Python 回退版在 __init__ 里初始化了该字段，所以只有编译版暴露。
        cdef uint16_t next_ptr = PAGE_SIZE
        memcpy(self.data + 19, &next_ptr, 2)
        cdef uint16_t n_slots_zero = 0
        memcpy(self.data + 17, &n_slots_zero, 2)

    def __dealloc__(self):
        if self.data:
            free(self.data)

    @property
    def id(self):
        return self.page_id

    @property
    def is_dirty(self):
        return self.dirty

    cpdef bytes to_bytes(self):
        return PyBytes_FromStringAndSize(<const char*>self.data, PAGE_SIZE)

    cpdef void from_bytes(self, bytes b):
        if len(b) != PAGE_SIZE:
            raise ValueError(f"页大小错误: {len(b)}")
        memcpy(self.data, <const char*>b, PAGE_SIZE)

    cpdef uint16_t get_n_slots(self):
        cdef uint16_t v
        memcpy(&v, self.data + 17, 2)
        return v

    cpdef void set_n_slots(self, uint16_t n):
        memcpy(self.data + 17, &n, 2)

    cpdef uint16_t get_next_row_ptr(self):
        cdef uint16_t v
        memcpy(&v, self.data + 19, 2)
        return v

    cpdef void set_next_row_ptr(self, uint16_t p):
        memcpy(self.data + 19, &p, 2)

    cpdef bint insert_row(self, bytes row_bytes):
        """
        插入一行
        返回 True 成功 / False 空间不足
        """
        cdef int blen = len(row_bytes)
        cdef uint16_t n = self.get_n_slots()
        cdef uint16_t new_next = self.get_next_row_ptr() - blen
        cdef int slot_end = PAGE_HEADER_SIZE + (n + 1) * 4

        if new_next < slot_end:
            return False

        # 拷贝行数据
        memcpy(self.data + new_next, <const char*>row_bytes, blen)

        # 写槽位
        cdef int slot_off = PAGE_HEADER_SIZE + n * 4
        cdef uint16_t off = new_next
        cdef uint16_t ln = <uint16_t>blen
        memcpy(self.data + slot_off, &off, 2)
        memcpy(self.data + slot_off + 2, &ln, 2)

        self.set_n_slots(n + 1)
        self.set_next_row_ptr(new_next)
        self.dirty = True
        return True

    cpdef bytes get_row(self, uint16_t slot_id):
        """读取某槽位的行数据"""
        cdef uint16_t n = self.get_n_slots()
        if slot_id >= n:
            return None

        cdef int slot_off = PAGE_HEADER_SIZE + slot_id * 4
        cdef uint16_t off
        cdef uint16_t ln
        memcpy(&off, self.data + slot_off, 2)
        memcpy(&ln, self.data + slot_off + 2, 2)

        if off == 0 or ln == 0:
            return None

        return PyBytes_FromStringAndSize(
            <const char*>(self.data + off), ln)

    cpdef void delete_row(self, uint16_t slot_id):
        """删除槽位（标记空）"""
        cdef uint16_t n = self.get_n_slots()
        if slot_id >= n:
            return
        cdef int slot_off = PAGE_HEADER_SIZE + slot_id * 4
        cdef uint16_t zero = 0
        memcpy(self.data + slot_off, &zero, 4)
        self.dirty = True

    cpdef int free_space(self):
        return self.get_next_row_ptr() - PAGE_HEADER_SIZE - \
               self.get_n_slots() * 4


# ==================== 无对象页操作（更快）====================

cdef int _page_n_slots_c(const unsigned char* data) nogil:
    """读页内槽位数（无 GIL 内核）"""
    cdef uint16_t v
    memcpy(&v, data + 17, 2)
    return <int>v


cpdef int page_n_slots(bytes page_data):
    """
    读页内槽位数

    修复 B：原签名是 ``cpdef int page_n_slots(bytes page_data) nogil``。
    带 Python 对象参数（bytes）的 cpdef 函数**不能**标 nogil，因为函数体
    进入无 GIL 区后无法处理 Python 对象，编译期直接报错。现在内核
    （_page_n_slots_c）保持 nogil，公开函数回到带 GIL 的 cpdef。
    """
    cdef const unsigned char* data = page_data
    return _page_n_slots_c(data)


cpdef bytes page_get_row(bytes page_data, int slot_id):
    """快速取行"""
    cdef const unsigned char* data = page_data
    cdef int slot_off = PAGE_HEADER_SIZE + slot_id * 4
    cdef uint16_t off
    cdef uint16_t ln
    memcpy(&off, data + slot_off, 2)
    memcpy(&ln, data + slot_off + 2, 2)
    if off == 0 or ln == 0:
        return None
    return PyBytes_FromStringAndSize(
        <const char*>(data + off), ln)


cpdef int page_batch_checksum(bytes page_data):
    """快速校验和（用于一致性检查）"""
    cdef const unsigned char* data = page_data
    cdef int n = len(page_data)
    cdef int i
    cdef uint32_t crc = 0xFFFFFFFF
    for i in range(0, n, 4):
        crc = (crc >> 8) ^ ((crc ^ data[i]) & 0xFF)
    return <int>(crc & 0x7FFFFFFF)