"""
行格式 v4（使用 Cython 加速）
自动回退到 Python 版本
"""
from typing import List, Any, Optional

from ..cython import row_codec as _cy
from .row_format import BinaryRowFormat as _PyRowFormat


class BinaryRowFormatV4:
    """
    二进制行格式 v4
    优先使用 Cython 加速版本
    """

    def __init__(self, columns: List[dict]):
        self.columns = columns
        self.n_cols = len(columns)

        # 类型列表
        self.types = [(c.get('type') or 'VARCHAR').upper()
                       for c in columns]

        # 是否使用「真正编译过的」Cython 加速实现
        # 注意：_cy 现在恒不为 None（未编译时会拿到纯 Python 回退），
        # 所以不能再拿 `_cy is not None` 当加速判据，否则会谎报在用 Cython。
        try:
            from ..cython import status as _cstatus
            _accel = bool(_cstatus().get('any_accelerated'))
        except Exception:
            _accel = False
        if _cy is not None and _accel:
            self.use_cython = True
            self._cy_codec = _cy.RowCodec(self.types)
        else:
            self.use_cython = False
            self._py_codec = _PyRowFormat(columns)

    def encode(self, row: List[Any]) -> bytes:
        if self.use_cython:
            return self._cy_codec.encode(row)
        return self._py_codec.encode(row)

    def decode(self, data: bytes) -> List[Any]:
        if self.use_cython:
            return self._cy_codec.decode(data)
        return self._py_codec.decode(data)

    def encode_many(self, rows: List[List]) -> List[bytes]:
        if self.use_cython:
            return [self._cy_codec.encode(r) for r in rows]
        return [self._py_codec.encode(r) for r in rows]

    def decode_many(self, blobs: List[bytes]) -> List[List]:
        if self.use_cython:
            return [self._cy_codec.decode(b) for b in blobs]
        return [self._py_codec.decode(b) for b in blobs]

    def encode_batch(self, rows: List[List]) -> bytes:
        """批量编码为单块"""
        if self.use_cython:
            return self._cy_codec.encode_many(rows)
        # Python 回退：手动拼装
        import struct
        encoded = [self._py_codec.encode(r) for r in rows]
        parts = [struct.pack('<I', len(encoded))]
        for e in encoded:
            parts.append(struct.pack('<I', len(e)))
            parts.append(e)
        return b''.join(parts)

    def decode_batch(self, data: bytes) -> List[List]:
        """从单块解码批量"""
        if self.use_cython:
            return self._cy_codec.decode_many(data)
        import struct
        offset = 0
        count = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        result = []
        for _ in range(count):
            blen = struct.unpack_from('<I', data, offset)[0]
            offset += 4
            blob = data[offset:offset + blen]
            offset += blen
            result.append(self._py_codec.decode(blob))
        return result


# 自动选择
def get_row_format(columns):
    """工厂函数：返回最佳行格式"""
    return BinaryRowFormatV4(columns)