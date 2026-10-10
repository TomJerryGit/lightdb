"""
列编码器（修复版）

修复:
- PlainEncoder.decode 完整实现（按 col_type 解码）
- 统一头部格式: [4B count][2B null_bitmap_len][null_bitmap][data]
- 所有编码器 decode 容错
- EncoderRegistry.select 稳定性
"""
import struct
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, List


# ==================== 基类 ====================

class BaseEncoder(ABC):
    encoding_id = 0

    @abstractmethod
    def encode(self, values: List) -> bytes:
        pass

    @abstractmethod
    def decode(self, data: bytes, col_type: str) -> List:
        pass

    @abstractmethod
    def estimate(self, values: List, col_type: str) -> int:
        pass


# ==================== 类型助手 ====================

def _coerce(v, col_type: str):
    """字符串转实际类型"""
    if v is None:
        return None
    t = (col_type or '').upper()
    try:
        if t in ('INT', 'BIGINT', 'SMALLINT', 'TINYINT',
                 'MEDIUMINT', 'INTEGER'):
            return int(v)
        if t in ('FLOAT', 'DOUBLE', 'DECIMAL', 'REAL', 'NUMERIC'):
            return float(v)
        if t in ('BOOLEAN', 'BOOL'):
            return v in ('1', 'true', 'True', True, 1)
    except (ValueError, TypeError):
        pass
    return v


def _pack_value(v):
    """值 → bytes"""
    if isinstance(v, bool):
        return struct.pack('<B', 1 if v else 0)
    if isinstance(v, int):
        return struct.pack('<q', v)
    if isinstance(v, float):
        return struct.pack('<d', v)
    s = str(v).encode('utf-8')
    return struct.pack('<H', len(s)) + s


def _unpack_value(data: bytes, offset: int, col_type: str):
    """bytes → 值，返回 (value, new_offset)"""
    t = (col_type or '').upper()
    if t in ('INT', 'BIGINT', 'SMALLINT', 'TINYINT',
             'MEDIUMINT', 'INTEGER'):
        v = struct.unpack_from('<q', data, offset)[0]
        return v, offset + 8
    if t in ('FLOAT', 'DOUBLE', 'DECIMAL', 'REAL', 'NUMERIC'):
        v = struct.unpack_from('<d', data, offset)[0]
        return v, offset + 8
    if t in ('BOOLEAN', 'BOOL'):
        v = struct.unpack_from('<B', data, offset)[0]
        return bool(v), offset + 1
    # 字符串
    ln = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    s = data[offset:offset + ln].decode('utf-8', errors='replace')
    return s, offset + ln


# ==================== Plain ====================

class PlainEncoder(BaseEncoder):
    """
    原样编码（无压缩）
    格式: [4B count][2B null_bitmap_len][null_bitmap][data...]
    """
    encoding_id = 0

    def encode(self, values):
        n = len(values)
        null_bitmap_len = (n + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)

        parts = []
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))
                continue
            parts.append(_pack_value(v))

        header = struct.pack('<IH', n, null_bitmap_len)
        return header + bytes(null_bitmap) + b''.join(parts)

    def decode(self, data, col_type):
        if len(data) < 6:
            return []
        try:
            n, null_bitmap_len = struct.unpack_from('<IH', data, 0)
        except struct.error:
            return []

        offset = 6
        if len(data) < offset + null_bitmap_len:
            return []
        null_bitmap = data[offset:offset + null_bitmap_len]
        offset += null_bitmap_len

        result = []
        for i in range(n):
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
                continue
            try:
                v, offset = _unpack_value(data, offset, col_type)
                result.append(v)
            except (struct.error, IndexError):
                result.append(None)
        return result

    def estimate(self, values, col_type):
        n = len(values)
        null_bytes = (n + 7) // 8
        data_bytes = 6  # header
        for v in values:
            if v is None:
                continue
            if isinstance(v, bool):
                data_bytes += 1
            elif isinstance(v, int):
                data_bytes += 8
            elif isinstance(v, float):
                data_bytes += 8
            else:
                data_bytes += 2 + len(str(v).encode('utf-8'))
        return null_bytes + data_bytes


# ==================== 字典编码 ====================

class DictEncoder(BaseEncoder):
    """字典编码：枚举值 → 索引"""
    encoding_id = 1

    def encode(self, values):
        unique = OrderedDict()
        for v in values:
            if v is not None and v not in unique:
                unique[v] = len(unique)

        dict_entries = []
        for v in unique.keys():
            s = str(v).encode('utf-8')
            dict_entries.append(struct.pack('<H', len(s)) + s)
        dict_data = b''.join(dict_entries)

        n = len(values)
        dict_size = len(unique)
        if dict_size <= 256:
            index_format, index_size = '<B', 1
        elif dict_size <= 65536:
            index_format, index_size = '<H', 2
        else:
            index_format, index_size = '<I', 4

        null_bitmap_len = (n + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)
        indices = []
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))
                indices.append(0)
            else:
                indices.append(unique[v])

        indices_data = b''.join(
            struct.pack(index_format, idx) for idx in indices)

        header = struct.pack(
            '<HHBI', dict_size, n, index_size, len(dict_data))
        return header + bytes(null_bitmap) + dict_data + indices_data

    def decode(self, data, col_type):
        if len(data) < 9:
            return []
        try:
            dict_size, n, index_size, dict_len = \
                struct.unpack_from('<HHBI', data, 0)
        except struct.error:
            return []
        offset = 9

        null_bitmap_len = (n + 7) // 8
        if len(data) < offset + null_bitmap_len:
            return []
        null_bitmap = data[offset:offset + null_bitmap_len]
        offset += null_bitmap_len

        # 字典
        dict_values = []
        dict_end = offset + dict_len
        while offset < dict_end and offset + 2 <= len(data):
            slen = struct.unpack_from('<H', data, offset)[0]
            offset += 2
            s = data[offset:offset + slen].decode(
                'utf-8', errors='replace')
            offset += slen
            dict_values.append(s)

        if index_size == 1:
            fmt = '<B'
        elif index_size == 2:
            fmt = '<H'
        else:
            fmt = '<I'

        result = []
        for i in range(n):
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
                continue
            try:
                idx = struct.unpack_from(fmt, data, offset)[0]
                offset += index_size
                if idx < len(dict_values):
                    result.append(_coerce(dict_values[idx], col_type))
                else:
                    result.append(None)
            except struct.error:
                result.append(None)
        return result

    def estimate(self, values, col_type):
        unique = len(set(str(v) for v in values if v is not None))
        n = len(values)
        dict_bytes = unique * 10
        index_size = 1 if unique <= 256 else (
            2 if unique <= 65536 else 4)
        return 9 + dict_bytes + (n + 7) // 8 + n * index_size


# ==================== RLE ====================

class RLEEncoder(BaseEncoder):
    """游程编码"""
    encoding_id = 2

    def encode(self, values):
        runs = []
        if values:
            current = values[0]
            count = 1
            for v in values[1:]:
                if v == current:
                    count += 1
                else:
                    runs.append((current, count))
                    current = v
                    count = 1
            runs.append((current, count))

        parts = []
        null_bitmap_len = (len(runs) + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)

        for i, (v, cnt) in enumerate(runs):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))
                parts.append(struct.pack('<I', cnt))
                continue
            parts.append(struct.pack('<I', cnt))
            parts.append(_pack_value(v))

        header = struct.pack('<II', len(runs), len(values))
        return header + bytes(null_bitmap) + b''.join(parts)

    def decode(self, data, col_type):
        if len(data) < 8:
            return []
        try:
            n_runs, n_total = struct.unpack_from('<II', data, 0)
        except struct.error:
            return []
        offset = 8

        null_bitmap_len = (n_runs + 7) // 8
        if len(data) < offset + null_bitmap_len:
            return []
        null_bitmap = data[offset:offset + null_bitmap_len]
        offset += null_bitmap_len

        result = []
        for i in range(n_runs):
            try:
                cnt = struct.unpack_from('<I', data, offset)[0]
                offset += 4
            except struct.error:
                break

            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.extend([None] * cnt)
                continue

            try:
                v, offset = _unpack_value(data, offset, col_type)
                result.extend([v] * cnt)
            except (struct.error, IndexError):
                result.extend([None] * cnt)

        return result

    def estimate(self, values, col_type):
        if not values:
            return 8
        runs = 1
        for i in range(1, len(values)):
            if values[i] != values[i - 1]:
                runs += 1
        return 8 + runs * 12


# ==================== Delta ====================

class DeltaEncoder(BaseEncoder):
    """差分编码：只对整数有效"""
    encoding_id = 3

    def encode(self, values):
        int_vals = []
        for v in values:
            if v is None:
                int_vals.append(None)
            elif isinstance(v, bool):
                int_vals.append(int(v))
            elif isinstance(v, int):
                int_vals.append(v)
            else:
                try:
                    int_vals.append(int(v))
                except (ValueError, TypeError):
                    int_vals.append(0)

        deltas = []
        prev = 0
        for v in int_vals:
            if v is None:
                deltas.append(0)
            else:
                deltas.append(v - prev)
                prev = v

        parts = [_varint_encode(d) for d in deltas]

        null_bitmap_len = (len(values) + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))

        header = struct.pack('<IQ', len(values), prev)
        return header + bytes(null_bitmap) + b''.join(parts)

    def decode(self, data, col_type):
        if len(data) < 12:
            return []
        try:
            n, _max_val = struct.unpack_from('<IQ', data, 0)
        except struct.error:
            return []
        offset = 12

        null_bitmap_len = (n + 7) // 8
        if len(data) < offset + null_bitmap_len:
            return []
        null_bitmap = data[offset:offset + null_bitmap_len]
        offset += null_bitmap_len

        result = []
        prev = 0
        for i in range(n):
            try:
                d, offset = _varint_decode(data, offset)
            except (IndexError, struct.error):
                result.append(None)
                continue
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
            else:
                prev += d
                result.append(prev)
        return result

    def estimate(self, values, col_type):
        return len(values) * 2 + 20


# ==================== BitPack ====================

class BitPackEncoder(BaseEncoder):
    """位压缩：小整数"""
    encoding_id = 4

    def encode(self, values):
        int_vals = []
        for v in values:
            if v is None:
                int_vals.append(0)
            elif isinstance(v, bool):
                int_vals.append(int(v))
            elif isinstance(v, int):
                int_vals.append(max(0, v))
            else:
                try:
                    int_vals.append(max(0, int(v)))
                except (ValueError, TypeError):
                    int_vals.append(0)

        if not int_vals:
            return struct.pack('<IBH', 0, 0, 0)

        max_val = max(int_vals)
        bits = max(1, min(64, max_val.bit_length()))

        packed = _bit_pack(int_vals, bits)

        null_bitmap_len = (len(values) + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))

        header = struct.pack(
            '<IBH', len(values), bits, null_bitmap_len)
        return header + bytes(null_bitmap) + packed

    def decode(self, data, col_type):
        if len(data) < 7:
            return []
        try:
            n, bits, null_len = struct.unpack_from(
                '<IBH', data, 0)
        except struct.error:
            return []
        offset = 7

        if len(data) < offset + null_len:
            return []
        null_bitmap = data[offset:offset + null_len]
        offset += null_len

        try:
            values = _bit_unpack(data[offset:], bits, n)
        except (IndexError, struct.error):
            return []

        result = []
        for i in range(n):
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
            else:
                v = values[i] if i < len(values) else 0
                result.append(_coerce(v, col_type))
        return result

    def estimate(self, values, col_type):
        max_val = 0
        for v in values:
            if v is None:
                continue
            try:
                max_val = max(max_val, int(v))
            except (ValueError, TypeError):
                pass
        bits = max(1, max_val.bit_length() if max_val else 1)
        bytes_size = (len(values) * bits + 7) // 8
        return bytes_size + (len(values) + 7) // 8 + 7


# ==================== 变长整数 ====================

def _varint_encode(n: int) -> bytes:
    if n < 0:
        n = (-n) * 2 - 1
    else:
        n = n * 2
    result = bytearray()
    while n >= 0x80:
        result.append((n & 0x7F) | 0x80)
        n >>= 7
    result.append(n)
    return bytes(result)


def _varint_decode(data: bytes, offset: int):
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise IndexError("varint 越界")
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")

    if result & 1:
        return -((result + 1) >> 1), offset
    return result >> 1, offset


# ==================== 位打包 ====================

def _bit_pack(values: List[int], bits: int) -> bytes:
    total_bits = len(values) * bits
    total_bytes = (total_bits + 7) // 8
    result = bytearray(total_bytes)

    bit_pos = 0
    for v in values:
        v = v & ((1 << bits) - 1)
        remaining = bits
        while remaining > 0:
            byte_pos = bit_pos // 8
            bit_offset = bit_pos % 8
            space = 8 - bit_offset
            take = min(space, remaining)
            mask = ((1 << take) - 1) << (remaining - take)
            piece = (v & mask) >> (remaining - take)
            result[byte_pos] |= piece << (space - take)
            bit_pos += take
            remaining -= take

    return bytes(result)


def _bit_unpack(data: bytes, bits: int, count: int) -> List[int]:
    values = []
    bit_pos = 0
    for _ in range(count):
        v = 0
        remaining = bits
        while remaining > 0:
            byte_pos = bit_pos // 8
            if byte_pos >= len(data):
                raise IndexError("bit_unpack 越界")
            bit_offset = bit_pos % 8
            space = 8 - bit_offset
            take = min(space, remaining)
            b = data[byte_pos]
            piece = (b >> (space - take)) & ((1 << take) - 1)
            v = (v << take) | piece
            bit_pos += take
            remaining -= take
        values.append(v)
    return values


# ==================== 注册表 ====================

ENCODING_PLAIN = 0
ENCODING_DICT = 1
ENCODING_RLE = 2
ENCODING_DELTA = 3
ENCODING_BITPACK = 4


class EncoderRegistry:
    _encoders = {
        ENCODING_PLAIN: PlainEncoder,
        ENCODING_DICT: DictEncoder,
        ENCODING_RLE: RLEEncoder,
        ENCODING_DELTA: DeltaEncoder,
        ENCODING_BITPACK: BitPackEncoder,
    }

    @classmethod
    def get_by_id(cls, encoding_id: int):
        klass = cls._encoders.get(encoding_id, PlainEncoder)
        return klass()

    @classmethod
    def select(cls, col_type: str, values: List) -> BaseEncoder:
        """根据数据特征自动选择编码"""
        if not values:
            return PlainEncoder()

        n = len(values)
        t = (col_type or '').upper()

        # 数值类型
        if t in ('INT', 'BIGINT', 'SMALLINT', 'TINYINT',
                 'MEDIUMINT', 'INTEGER'):
            if _is_monotonic(values) and _max_delta_small(values):
                return DeltaEncoder()
            max_v = max(
                (v for v in values if v is not None), default=0)
            if 0 < max_v <= 65535:
                bp_est = BitPackEncoder().estimate(values, t)
                plain_est = PlainEncoder().estimate(values, t)
                if bp_est < plain_est * 0.5:
                    return BitPackEncoder()

        # 字符串
        if t in ('VARCHAR', 'TEXT', 'CHAR'):
            distinct = len(
                set(str(v) for v in values if v is not None))
            if distinct < n * 0.3:
                return DictEncoder()

        # 重复
        if _has_many_repeats(values):
            return RLEEncoder()

        # 字典（其他类型低基数）
        distinct = len(
            set(str(v) for v in values if v is not None))
        if distinct < n * 0.3:
            return DictEncoder()

        return PlainEncoder()


# ==================== 判定助手 ====================

def _is_monotonic(values):
    prev = None
    for v in values:
        if v is None:
            continue
        try:
            if prev is not None and v < prev:
                return False
        except TypeError:
            return False
        prev = v
    return True


def _max_delta_small(values):
    prev = None
    for v in values:
        if v is None:
            continue
        try:
            if prev is not None and abs(v - prev) > 1000:
                return False
        except TypeError:
            return False
        prev = v
    return True


def _has_many_repeats(values):
    if len(values) < 4:
        return False
    consecutive = 0
    for i in range(1, len(values)):
        if values[i] == values[i - 1]:
            consecutive += 1
    return consecutive > len(values) * 0.5