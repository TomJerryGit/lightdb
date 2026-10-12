"""
``row_codec`` 的纯 Python 回退
=============================

复用：``lightdb/storage/row_format.py`` 的 ``BinaryRowFormat``（既有纯
Python 行编解码器，null bitmap + 定宽字段 + 2 字节长度前缀的 VARCHAR）。
本模块只做 **参数与返回值的薄适配**，不重写编解码逻辑：

    .pyx 的 ``RowCodec(list col_types)``    ← 形如 ['INT','VARCHAR']
    ``BinaryRowFormat(list of {'type':...})`` ← 形如 [{'name':'id','type':'INT'}]

未在本机验证：本机无 Cython、无 C 编译器，无法编译 ``row_codec.pyx``，
因此下面标注的"与 .pyx 等价/差异"全部来自源码静态比对，未做实测对照。

已知差异（都被 ``fallback/__init__.py`` 记录在案）:
    1. >65535 字节字符串：.pyx 写 2 字节 65535 标记后直接截断（后续列
       丢失、长度字段不自洽）；回退走 4 字节扩展长度，可正确往返。
    2. ``T_FLOAT``：.pyx 把值算成 C double 只 ``memcpy`` 4 字节；回退按
       单精度 IEEE-754 正常处理。
    3. ``T_INT``：两边都是 4 字节有符号整数（``<i``），可互通。
    4. ``RowCodec`` 回退不解析 64KB 以外的单列（同 1）。
"""
import struct

from lightdb.storage.row_format import (
    BinaryRowFormat as _PyRowFormat,
    STRUCT_BIGINT,
    STRUCT_INT,
    T_BIGINT,
    T_BOOL,
    T_DOUBLE,
    T_FLOAT,
    T_INT,
    T_STRING,
)

__all__ = [
    'RowCodec',
    'T_BIGINT',
    'T_BOOL',
    'T_DOUBLE',
    'T_FLOAT',
    'T_INT',
    'T_STRING',
    'decode_int64_list',
    'encode_int64_list',
]

_STRUCT_I64 = struct.Struct('<q')


class RowCodec:
    """二进制行编解码器（回退版，包装 ``BinaryRowFormat``）"""

    __slots__ = ('_fmt', 'col_types')

    def __init__(self, col_types):
        if isinstance(col_types, str):
            raise TypeError("col_types 需要是序列，如 ['INT', 'VARCHAR']")
        self.col_types = [str(t).upper() for t in col_types]
        # 关键适配点：.pyx 收的是类型名字符串，BinaryRowFormat 收的是 dict
        self._fmt = _PyRowFormat([{'name': f'c{i}', 'type': t}
                                  for i, t in enumerate(self.col_types)])

    @property
    def n_cols(self):
        return self._fmt.n_cols

    @property
    def null_bitmap_len(self):
        return self._fmt.null_bitmap_len

    # ---------- 单行 ----------

    def encode(self, values):
        return self._fmt.encode(list(values))

    def decode(self, data):
        return self._fmt.decode(bytes(data))

    # ---------- 批量（单块，格式与 .pyx 一致：count + [len + data] ...）----------

    def encode_many(self, rows):
        encoded = [self.encode(r) for r in rows]
        parts = [struct.pack('<I', len(encoded))]
        for e in encoded:
            parts.append(struct.pack('<I', len(e)))
            parts.append(e)
        return b''.join(parts)

    def decode_many(self, data):
        count = struct.unpack_from('<I', data, 0)[0]
        offset = 4
        result = []
        for _ in range(count):
            blen = struct.unpack_from('<I', data, offset)[0]
            offset += 4
            result.append(self.decode(data[offset:offset + blen]))
            offset += blen
        return result

    # ---------- 与既有 Python 行格式互通 ----------

    def to_python_format(self):
        """返回底层 ``BinaryRowFormat``（便于与 v4 路径交叉核对）"""
        return self._fmt


# ==================== int64 列表快速编解码 ====================

def encode_int64_list(values):
    """int64 列表 → 小端二进制（与 .pyx 的 memcpy 结果一致）"""
    return b''.join(_STRUCT_I64.pack(int(v)) for v in values)


def decode_int64_list(data):
    """小端二进制 → int64 列表（尾部不足 8 字节的部分忽略，同 .pyx）"""
    n = len(data) // 8
    return [STRUCT_BIGINT.unpack_from(data, i * 8)[0] for i in range(n)]


# 供一致性核对：STRUCT_INT 在 .pyx 的 T_INT 路径里是 4 字节
_INT_STRUCT = STRUCT_INT
