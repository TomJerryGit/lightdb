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

        # 编码（必须把声明类型传给编码器：编/解码按同一份类型规则，
        # 否则「FLOAT 列里出现整数」「INT 列里出现 1.0」会静默错值）
        self.encoded_data = encoder.encode(self.values, self.col_type)
        self.encoding = encoder.encoding_id
        self.compressed_size = len(self.encoded_data)

        # 组装：头部 + 数据
        header = self._build_header()
        return header + self.encoded_data

    def decode(self, raw: bytes) -> List:
        """解码

        ★ 正确性修复：除 values 外，**统计信息也随块头一起还原**
        （min/max/null_count/distinct_count/sum/total）。旧实现把块头的
        stats JSON 解析出来后直接丢弃，于是 `ColumnStore.flush()` 再
        `__init__()` 重载之后 `get_column_stats()` 的 min/max 变成 None、
        null_count 变成 0、sum 变成 0 —— 与行存/内存态语义不一致，且
        优化器的 `can_skip_scan` 会因此失效。
        """
        header, data = self._parse_header(raw)
        self.encoding = header['encoding']
        self.compressed_size = len(raw)
        stats = header.get('stats') or {}
        self.stats.null_count = int(stats.get('null', header['null_count']) or 0)
        self.stats.total_count = int(
            stats.get('count', header['count']) or header['count'])
        self.stats.min_value = stats.get('min')
        self.stats.max_value = stats.get('max')
        self.stats.distinct_count = int(stats.get('distinct', 0) or 0)
        self.stats.sum_value = stats.get('sum', 0) or 0
        self.stats.is_sorted = bool(stats.get('sorted', False))

        encoder = EncoderRegistry.get_by_id(self.encoding)
        self.values = encoder.decode(data, self.col_type)
        if len(self.values) != header['count']:
            raise ValueError(
                f"列块 {self.column_name} 解码长度不符: "
                f"头部 {header['count']} 行，实际 {len(self.values)} 行")
        return self.values

    def _build_header(self) -> bytes:
        """块头部

        布局: [4B magic][4B count][1B encoding][4B data_len]
              [2B name_len][name][4B stats_json_len][stats_json]
        """
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
        """解析头部（显式校验 magic/长度，损坏数据抛 ValueError 而不是 assert）"""
        import json
        if len(raw) < 4 or raw[:4] != b'CDCH':
            raise ValueError(
                f"列块头 magic 非法: {raw[:4]!r}（表 {self.column_name}）")
        offset = 4
        try:
            count = struct.unpack_from('<I', raw, offset)[0]
            offset += 4
            encoding = raw[offset]
            offset += 1
            data_len = struct.unpack_from('<I', raw, offset)[0]
            offset += 4
            name_len = struct.unpack_from('<H', raw, offset)[0]
            offset += 2
            if offset + name_len > len(raw):
                raise ValueError("列块头 name 越界")
            name = raw[offset:offset+name_len].decode('utf-8')
            offset += name_len
            stats_len = struct.unpack_from('<I', raw, offset)[0]
            offset += 4
            if offset + stats_len > len(raw):
                raise ValueError("列块头 stats 越界")
            stats_json = raw[offset:offset+stats_len].decode('utf-8')
            offset += stats_len
        except struct.error as e:
            raise ValueError(f"列块头损坏: {e}") from e
        try:
            stats = json.loads(stats_json) if stats_json else {}
        except ValueError:
            stats = {}
        data = raw[offset:offset + data_len]
        if len(data) != data_len:
            raise ValueError(
                f"列块 {name} 数据长度不符: 头声明 {data_len}，"
                f"实际 {len(data)}")
        return ({
            'count': count,
            'encoding': encoding,
            'data_len': data_len,
            'name': name,
            'null_count': int(stats.get('null', 0) or 0),
            'stats': stats,
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