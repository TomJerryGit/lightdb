# cython: language_level=3, boundscheck=False, wraparound=False
# =============================================================================
# ⚠️ 未在本机验证编译：本机未安装 Cython，也没有任何 C 编译器
#    （import Cython → ModuleNotFoundError；gcc / cl / clang 全部未找到），
#    因此本文件**从未被编译过**，以下修复只是按 Cython 语义做的静态更正。
#    验证条件：pip install cython 且具备 C 编译器（Windows 用 MSVC cl.exe，
#    Linux 用 gcc），然后运行 scripts/build_cython.sh。
# =============================================================================
"""
行编解码（Cython）
- 二进制行格式
- 快速编码/解码
"""

from libc.stdint cimport int32_t, int64_t, uint16_t, uint8_t
# 修复 2：encode() / encode_many() 里调用了 memset，但原先没有 cimport，
#        编译时报 "undeclared name not builtin: memset"。
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset, strlen
from cpython.bytes cimport PyBytes_FromStringAndSize, PyBytes_AsString


# 类型编码
cdef int T_INT = 1
cdef int T_BIGINT = 2
cdef int T_FLOAT = 3
cdef int T_DOUBLE = 4
cdef int T_BOOL = 5
cdef int T_STRING = 6
cdef int T_BYTES = 7


cdef class RowCodec:
    """
    二进制行编解码器
    """
    cdef int n_cols
    cdef int* types           # 每列类型
    cdef int null_bitmap_len
    cdef int capacity         # 预估行大小

    def __cinit__(self, list col_types):
        """
        col_types: ['INT', 'VARCHAR', 'DECIMAL', ...]
        """
        self.n_cols = len(col_types)
        self.types = <int*>malloc(self.n_cols * sizeof(int))
        if not self.types:
            raise MemoryError()

        cdef int i
        cdef str t
        for i in range(self.n_cols):
            t = col_types[i].upper()
            if 'BIGINT' in t:
                self.types[i] = T_BIGINT
            elif 'INT' in t:
                self.types[i] = T_INT
            elif 'BOOLEAN' in t or 'BOOL' in t:
                self.types[i] = T_BOOL
            elif 'DOUBLE' in t:
                self.types[i] = T_DOUBLE
            elif 'FLOAT' in t:
                self.types[i] = T_FLOAT
            else:
                self.types[i] = T_STRING

        self.null_bitmap_len = (self.n_cols + 7) // 8
        self.capacity = 1024

    def __dealloc__(self):
        if self.types:
            free(self.types)

    cpdef bytes encode(self, list values):
        """
        编码一行
        """
        cdef int n = self.n_cols
        cdef int null_len = self.null_bitmap_len

        # 分配缓冲（宽松）
        cdef int buf_cap = 65536
        cdef unsigned char* buf = <unsigned char*>malloc(buf_cap)
        if not buf:
            raise MemoryError()

        try:
            memset(buf, 0, buf_cap)
            cdef int offset = null_len
            cdef int i
            cdef object v
            cdef int64_t iv
            cdef double dv
            cdef bytes b
            cdef int slen
            cdef const char* sdata

            for i in range(n):
                v = values[i]

                if v is None:
                    buf[i // 8] |= (1 << (i % 8))
                    continue

                cdef int t = self.types[i]

                if t == T_INT:
                    iv = <int64_t>int(v)
                    memcpy(buf + offset, &iv, 4)  # 只存 4 字节
                    offset += 4
                elif t == T_BIGINT:
                    iv = <int64_t>int(v)
                    memcpy(buf + offset, &iv, 8)
                    offset += 8
                elif t == T_FLOAT:
                    dv = <double>float(v)
                    memcpy(buf + offset, &dv, 4)
                    offset += 4
                elif t == T_DOUBLE:
                    dv = <double>float(v)
                    memcpy(buf + offset, &dv, 8)
                    offset += 8
                elif t == T_BOOL:
                    buf[offset] = 1 if v else 0
                    offset += 1
                else:   # T_STRING
                    if isinstance(v, str):
                        b = v.encode('utf-8')
                    else:
                        b = bytes(v)
                    slen = len(b)
                    if slen < 65535:
                        memcpy(buf + offset, &slen, 2)
                        offset += 2
                        memcpy(buf + offset, <const char*>b, slen)
                        offset += slen
                    else:
                        slen = 65535
                        memcpy(buf + offset, &slen, 2)
                        offset += 2

            # 转 Python bytes
            result = PyBytes_FromStringAndSize(
                <const char*>buf, offset)
            return result
        finally:
            free(buf)

    cpdef list decode(self, bytes data):
        """
        解码一行
        """
        cdef const unsigned char* buf = data
        cdef int data_len = len(data)
        cdef int null_len = self.null_bitmap_len
        cdef int offset = null_len
        cdef list result = []

        cdef int i
        cdef int t
        cdef int32_t iv32
        cdef int64_t iv64
        cdef float fv
        cdef double dv
        cdef uint16_t slen
        cdef bytes b

        for i in range(self.n_cols):
            is_null = (buf[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
                continue

            t = self.types[i]

            if t == T_INT:
                memcpy(&iv32, buf + offset, 4)
                offset += 4
                result.append(int(iv32))
            elif t == T_BIGINT:
                memcpy(&iv64, buf + offset, 8)
                offset += 8
                result.append(int(iv64))
            elif t == T_FLOAT:
                memcpy(&fv, buf + offset, 4)
                offset += 4
                result.append(float(fv))
            elif t == T_DOUBLE:
                memcpy(&dv, buf + offset, 8)
                offset += 8
                result.append(float(dv))
            elif t == T_BOOL:
                result.append(bool(buf[offset]))
                offset += 1
            else:  # T_STRING
                memcpy(&slen, buf + offset, 2)
                offset += 2
                if slen == 65535:
                    # 大文本，跳过简化处理
                    result.append('')
                    break
                b = PyBytes_FromStringAndSize(
                    <const char*>(buf + offset), slen)
                offset += slen
                try:
                    result.append(b.decode('utf-8'))
                except UnicodeDecodeError:
                    result.append(b)

        return result

    cpdef bytes encode_many(self, list rows):
        """
        批量编码（每行长度前缀）
        格式: [count(4B)] [len1(4B) data1] [len2(4B) data2] ...
        """
        cdef list encoded = [self.encode(r) for r in rows]
        cdef int total = 4
        cdef bytes b
        for b in encoded:
            total += 4 + len(b)

        cdef unsigned char* buf = <unsigned char*>malloc(total)
        if not buf:
            raise MemoryError()

        try:
            cdef int count = len(encoded)
            memcpy(buf, &count, 4)
            cdef int offset = 4
            cdef int blen
            for b in encoded:
                blen = len(b)
                memcpy(buf + offset, &blen, 4)
                offset += 4
                memcpy(buf + offset, <const char*>b, blen)
                offset += blen

            return PyBytes_FromStringAndSize(
                <const char*>buf, offset)
        finally:
            free(buf)

    cpdef list decode_many(self, bytes data):
        """批量解码"""
        cdef const unsigned char* buf = data
        cdef int count
        memcpy(&count, buf, 4)

        cdef int offset = 4
        cdef int blen
        cdef list result = []
        cdef bytes b

        cdef int i
        for i in range(count):
            memcpy(&blen, buf + offset, 4)
            offset += 4
            b = PyBytes_FromStringAndSize(
                <const char*>(buf + offset), blen)
            offset += blen
            result.append(self.decode(b))

        return result


# ==================== 快速整数编码 ====================

cpdef bytes encode_int64_list(list values):
    """int64 列表 → 二进制"""
    cdef int n = len(values)
    cdef unsigned char* buf = <unsigned char*>malloc(n * 8)
    if not buf:
        raise MemoryError()
    try:
        cdef int64_t v
        cdef int i
        for i in range(n):
            v = <int64_t>values[i]
            memcpy(buf + i * 8, &v, 8)
        return PyBytes_FromStringAndSize(<const char*>buf, n * 8)
    finally:
        free(buf)


cpdef list decode_int64_list(bytes data):
    """二进制 → int64 列表"""
    cdef const unsigned char* buf = data
    cdef int n = len(data) // 8
    cdef list result = []
    cdef int64_t v
    cdef int i
    for i in range(n):
        memcpy(&v, buf + i * 8, 8)
        result.append(int(v))
    return result