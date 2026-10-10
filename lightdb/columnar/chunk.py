"""
列数据块（Column Chunk）
- 一列的一段连续数据
- 存储编码类型、编码后数据、统计信息
"""
import struct
import time
from dataclasses import dataclass, asdict, field
from typing import Any, List, Optional

from .encoders import (
    BaseEncoder, PlainEncoder, DictEncoder, RLEEncoder,
    DeltaEncoder, BitPackEncoder, EncoderRegistry)


# 编码类型
ENCODING_PLAIN = 0
ENCODING_DICT = 1
ENCODING_RLE = 2
ENCODING_DELTA = 3
ENCODING_BITPACK = 4


@dataclass
class ColumnStats:
    """列统计信息"""
    min_value: Any = None
    max_value: Any = None
    null_count: int = 0
    distinct_count: int = 0
    sum_value: float = 0
    total_count: int = 0
    # 用于字典编码
    is_sorted: bool = False
    is_dense: bool = False    # 无 null 且值域紧凑


class ColumnChunk:
    """
    列数据块
    一段连续的同列数据
    """

    DEFAULT_SIZE = 4096   # 默认 4K 个值

    def __init__(self, column_name: str, col_type: str,
                 values: List = None):
        self.column_name = column_name
        self.col_type = col_type.upper()
        self.values = values or []
        self.encoding = ENCODING_PLAIN
        self.encoded_data: bytes = b''
        self.stats = ColumnStats()
        self.compressed_size: int = 0

    # ==================== 编码 ====================

    def encode(self) -> bytes:
        """编码数据块"""
        encoder = EncoderRegistry.select(self.col_type, self.values)

        # 更新统计
        self._compute_stats()

        # 编码
        self.encoded_data = encoder.encode(self.values)
        self.encoding = encoder.encoding_id
        self.compressed_size = len(self.encoded_data)

        # 组装：头部 + 数据
        header = self._build_header()
        return header + self.encoded_data

    def decode(self, raw: bytes) -> List:
        """解码"""
        header, data = self._parse_header(raw)
        self.encoding = header['encoding']
        self.stats.null_count = header['null_count']
        self.stats.total_count = header['count']

        encoder = EncoderRegistry.get_by_id(self.encoding)
        self.values = encoder.decode(data, self.col_type)
        return self.values

    def _build_header(self) -> bytes:
        """块头部"""
        # 布局:
        # [4B magic][4B count][1B encoding][4B data_len][8B stats_json_len]
        name_bytes = self.column_name.encode('utf-8')
        stats_json = _stats_to_json(self.stats).encode('utf-8')

        header = b'CDCH'                    # magic
        header += struct.pack('<I', len(self.values))
        header += struct.pack('<B', self.encoding)
        header += struct.pack('<I', self.compressed_size)
        header += struct.pack('<H', len(name_bytes))
        header += name_bytes
        header += struct.pack('<I', len(stats_json))
        header += stats_json
        return header

    def _parse_header(self, raw: bytes):
        """解析头部"""
        assert raw[:4] == b'CDCH'
        offset = 4
        count = struct.unpack_from('<I', raw, offset)[0]
        offset += 4
        encoding = raw[offset]
        offset += 1
        data_len = struct.unpack_from('<I', raw, offset)[0]
        offset += 4
        name_len = struct.unpack_from('<H', raw, offset)[0]
        offset += 2
        name = raw[offset:offset+name_len].decode('utf-8')
        offset += name_len
        stats_len = struct.unpack_from('<I', raw, offset)[0]
        offset += 4
        stats_json = raw[offset:offset+stats_len].decode('utf-8')
        offset += stats_len

        # 剩余为数据
        data = raw[offset:]
        return ({
            'count': count,
            'encoding': encoding,
            'data_len': data_len,
            'name': name,
            'null_count': 0,
        }, data)

    # ==================== 统计 ====================

    def _compute_stats(self):
        """计算统计信息"""
        self.stats.total_count = len(self.values)

        # 非空值
        non_null = [v for v in self.values if v is not None]
        self.stats.null_count = len(self.values) - len(non_null)

        if non_null:
            try:
                self.stats.min_value = min(non_null)
                self.stats.max_value = max(non_null)
            except TypeError:
                pass

            # 数值列统计
            if self.col_type in ('INT', 'BIGINT', 'FLOAT',
                                  'DOUBLE', 'DECIMAL'):
                try:
                    self.stats.sum_value = sum(
                        float(v) for v in non_null)
                except (TypeError, ValueError):
                    pass

        # 唯一值
        try:
            self.stats.distinct_count = len(set(
                str(v) for v in non_null))
        except Exception:
            self.stats.distinct_count = len(non_null)

        # 有序性
        if len(non_null) >= 2:
            try:
                self.stats.is_sorted = all(
                    non_null[i] <= non_null[i+1]
                    for i in range(len(non_null) - 1))
            except TypeError:
                self.stats.is_sorted = False

    # ==================== 序列化 ====================

    def to_dict(self) -> dict:
        return {
            'column_name': self.column_name,
            'col_type': self.col_type,
            'encoding': self.encoding,
            'stats': asdict(self.stats),
            'compressed_size': self.compressed_size,
            'count': len(self.values),
        }


def _stats_to_json(stats: ColumnStats) -> str:
    import json
    return json.dumps({
        'min': _safe_json(stats.min_value),
        'max': _safe_json(stats.max_value),
        'null': stats.null_count,
        'distinct': stats.distinct_count,
        'sum': stats.sum_value,
        'count': stats.total_count,
        'sorted': stats.is_sorted,
    })


def _safe_json(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


# ==================== 组合块 ====================

class ChunkGroup:
    """
    块组（同一批行所有列的块）
    """

    def __init__(self, columns: List[str], types: List[str],
                 row_count: int = 0):
        self.columns = columns
        self.types = types
        self.row_count = row_count
        self.chunks = {}   # column -> ColumnChunk

    def add_column_data(self, col_name: str, values: List):
        idx = self.columns.index(col_name)
        chunk = ColumnChunk(col_name, self.types[idx], values)
        chunk.encode()
        self.chunks[col_name] = chunk

    def get_chunk(self, col_name: str) -> Optional[ColumnChunk]:
        return self.chunks.get(col_name)

    def total_size(self) -> int:
        return sum(c.compressed_size for c in self.chunks.values())

    def to_dict(self) -> dict:
        return {
            'row_count': self.row_count,
            'columns': {n: c.to_dict() for n, c in self.chunks.items()},
        }