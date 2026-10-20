"""
列编码器（正确性止血版，2026-10）

修复历史:
- PlainEncoder.decode 完整实现（按 col_type 解码）
- 统一头部格式: [4B count][2B null_bitmap_len][null_bitmap][data]
- 所有编码器 decode 容错
- EncoderRegistry.select 稳定性

本批次（OLAP 接入第 1 步，正确性止血）修复的**正确性**问题:
1. **编码/解码类型不对称**：`_pack_value` 以前按 Python 运行时类型选字节
   布局（bool→1B / int→8B q / float→8B d / 其他→字符串），而
   `_unpack_value` 按**声明列类型**解释。于是「INT 列里出现 1.0」或
   「FLOAT 列里出现 1」这类真实数据（列存不做写入强转）会按 8 字节读成
   另一个数，往返静默错值。现在 `encode(values, col_type)` 接收声明类型，
   `_pack_value`/`_unpack_value` 严格按同一份类型规则编解码。
2. **负数结尾的单调整数序列让 Delta 编码崩**：头部用 `<Q`（无符号）存
   「最后一个值」，`[-20,-10,-3,-1]` 直接 struct.error。改为 `<q`（有符号）。
3. **BitPack 静默丢负数**：旧实现对负数 `max(0, v)` 截断成 0，往返得到 0。
   现在头部保存 `offset = min(0, min(values))`，所有值按 `v - offset`
   打包，decode 再加回 offset；值域超过 63 bit 时显式报错（不静默截断）。
4. **Dict 编码超过 65535 个唯一值 struct.error**：字典长度由 `<H` 改 `<I`，
   索引宽度 4B 分支现在真的可用。
5. **Dict 解码布尔值返回字符串**：`_coerce('False', 'BOOLEAN')` 以前
   返回 'False' 字符串（`v in ('1','true','True',True,1)` 漏了 False），
   现在 BOOLEAN 的 0/1、true/false、yes/no 都能还原成 bool。
6. **字符串超长静默截断风险**：长度前缀是 `<H`，超过 65535 字节现在
   显式 ValueError（以前 struct.error 或截断）。
7. `_unpack_value` 对越界读取显式 ValueError（以前切片静默拿到短串）。

不变式：`encode(values, col_type)` 之后 `decode(data, col_type)` 必须
返回与 `values` **逐项相等**（None 保持 None；数值 1 与 1.0 按 Python
`==` 相等）的列表；任何无法表示的输入显式报错，绝不静默改写。
"""
import struct
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, List

# 类型分组（与 db/table.py::coerce 的基类型口径一致）
_INT_TYPES = ('INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'MEDIUMINT', 'INTEGER')
_FLOAT_TYPES = ('FLOAT', 'DOUBLE', 'DECIMAL', 'REAL', 'NUMERIC')
_BOOL_TYPES = ('BOOLEAN', 'BOOL')
_STR_TYPES = ('VARCHAR', 'TEXT', 'CHAR', 'STRING', 'JSON')

_MAX_STR_LEN = 0xFFFF


# ==================== 基类 ====================

class BaseEncoder(ABC):
    encoding_id = 0

    @abstractmethod
    def encode(self, values: List, col_type: str = None) -> bytes:
        pass

    @abstractmethod
    def decode(self, data: bytes, col_type: str) -> List:
        pass

    @abstractmethod
    def estimate(self, values: List, col_type: str) -> int:
        pass


# ==================== 类型助手 ====================

def _base_type(col_type: str) -> str:
    t = (col_type or '').strip().upper()
    # 去掉参数与修饰：VARCHAR(20) → VARCHAR, INT(11) UNSIGNED → INT
    out = ''
    for ch in t:
        if ch.isalnum() or ch == '_':
            out += ch
        else:
            break
    return out


def _is_int_type(t: str) -> bool:
    return _base_type(t) in _INT_TYPES


def _is_float_type(t: str) -> bool:
    return _base_type(t) in _FLOAT_TYPES


def _is_bool_type(t: str) -> bool:
    return _base_type(t) in _BOOL_TYPES


def _is_str_type(t: str) -> bool:
    return _base_type(t) in _STR_TYPES or not _base_type(t)


def _coerce(v, col_type: str):
    """字符串 → 声明类型（Dict 编码的字典项是字符串，还原时用）"""
    if v is None:
        return None
    t = _base_type(col_type)
    try:
        if t in _INT_TYPES:
            if isinstance(v, str):
                return int(v.strip())
            return int(v)
        if t in _FLOAT_TYPES:
            if isinstance(v, str):
                return float(v.strip())
            return float(v)
        if t in _BOOL_TYPES:
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ('1', 'true', 'yes', 'on'):
                return True
            if s in ('0', 'false', 'no', 'off', ''):
                return False
            return v
    except (ValueError, TypeError):
        pass
    return v


def _pack_str(v) -> bytes:
    s = v if isinstance(v, str) else str(v)
    b = s.encode('utf-8')
    if len(b) > _MAX_STR_LEN:
        raise ValueError(
            f"字符串长度 {len(b)} 超过编码器上限 {_MAX_STR_LEN} 字节")
    return struct.pack('<H', len(b)) + b


def _pack_value(v, col_type: str = None):
    """值 → bytes，**按声明列类型**选择布局（与 _unpack_value 对称）"""
    if _is_int_type(col_type):
        try:
            return struct.pack('<q', int(v))
        except (TypeError, ValueError, struct.error):
            pass
    elif _is_float_type(col_type):
        try:
            return struct.pack('<d', float(v))
        except (TypeError, ValueError, struct.error):
            pass
    elif _is_bool_type(col_type):
        return struct.pack('<B', 1 if _coerce(v, 'BOOLEAN') else 0)

    # 未声明类型 / 声明类型与值不兼容：按 Python 运行时类型（旧行为），
    # 但 decode 侧仍会按 col_type 解释 —— 因此调用方应始终传 col_type；
    # ColumnChunk.encode 已保证这一点。
    if isinstance(v, bool):
        return struct.pack('<B', 1 if v else 0)
    if isinstance(v, int):
        return struct.pack('<q', v)
    if isinstance(v, float):
        return struct.pack('<d', v)
    return _pack_str(v)


def _unpack_value(data: bytes, offset: int, col_type: str):
    """bytes → 值，返回 (value, new_offset)；越界显式 ValueError"""
    t = _base_type(col_type)
    if t in _INT_TYPES:
        if offset + 8 > len(data):
            raise ValueError("整数解码越界")
        v = struct.unpack_from('<q', data, offset)[0]
        return int(v), offset + 8
    if t in _FLOAT_TYPES:
        if offset + 8 > len(data):
            raise ValueError("浮点解码越界")
        v = struct.unpack_from('<d', data, offset)[0]
        return float(v), offset + 8
    if t in _BOOL_TYPES:
        if offset + 1 > len(data):
            raise ValueError("布尔解码越界")
        v = struct.unpack_from('<B', data, offset)[0]
        return bool(v), offset + 1
    if offset + 2 > len(data):
        raise ValueError("字符串长度前缀越界")
    ln = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    if offset + ln > len(data):
        raise ValueError(f"字符串解码越界: 需要 {ln} 字节")
    s = data[offset:offset + ln].decode('utf-8', errors='replace')
    return s, offset + ln


# ==================== Plain ====================

class PlainEncoder(BaseEncoder):
    """
    原样编码（无压缩）
    格式: [4B count][2B null_bitmap_len][null_bitmap][data...]
    """
    encoding_id = 0

    def encode(self, values, col_type=None):
        n = len(values)
        null_bitmap_len = (n + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)

        parts = []
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))
                continue
            parts.append(_pack_value(v, col_type))

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
            except (struct.error, IndexError, ValueError):
                result.append(None)
        return result

    def estimate(self, values, col_type):
        n = len(values)
        null_bytes = (n + 7) // 8
        data_bytes = 6
        for v in values:
            if v is None:
                continue
            if _is_int_type(col_type) or _is_float_type(col_type):
                data_bytes += 8
            elif _is_bool_type(col_type):
                data_bytes += 1
            elif isinstance(v, bool):
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
    """字典编码：枚举值 → 索引（字典项一律以字符串保存，decode 按类型还原）"""
    encoding_id = 1

    def encode(self, values, col_type=None):
        unique = OrderedDict()
        for v in values:
            if v is not None and v not in unique:
                unique[v] = len(unique)

        dict_entries = []
        for v in unique.keys():
            s = str(v).encode('utf-8')
            if len(s) > _MAX_STR_LEN:
                raise ValueError(
                    f"字典项长度 {len(s)} 超过编码器上限 {_MAX_STR_LEN} 字节")
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
                continue          # NULL 不写索引（decode 侧同样不读）
            indices.append(unique[v])

        indices_data = b''.join(
            struct.pack(index_format, idx) for idx in indices)

        # [4B dict_size][4B n][1B index_size][4B dict_len]
        header = struct.pack(
            '<IIBI', dict_size, n, index_size, len(dict_data))
        return header + bytes(null_bitmap) + dict_data + indices_data

    def decode(self, data, col_type):
        if len(data) < 13:
            return []
        try:
            dict_size, n, index_size, dict_len = \
                struct.unpack_from('<IIBI', data, 0)
        except struct.error:
            return []
        offset = 13

        null_bitmap_len = (n + 7) // 8
        if len(data) < offset + null_bitmap_len:
            return []
        null_bitmap = data[offset:offset + null_bitmap_len]
        offset += null_bitmap_len

        dict_values = []
        dict_end = offset + dict_len
        while offset < dict_end and offset + 2 <= len(data):
            slen = struct.unpack_from('<H', data, offset)[0]
            offset += 2
            if offset + slen > len(data):
                break
            s = data[offset:offset + slen].decode(
                'utf-8', errors='replace')
            offset += slen
            dict_values.append(s)
        if len(dict_values) < dict_size:
            # 字典损坏：显式报错，不返回半套数据
            raise ValueError(
                f"字典编码损坏: 头部声明 {dict_size} 项，实际 {len(dict_values)} 项")
        offset = dict_end

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
        return 13 + dict_bytes + (n + 7) // 8 + n * index_size


# ==================== RLE ====================

class RLEEncoder(BaseEncoder):
    """游程编码"""
    encoding_id = 2

    def encode(self, values, col_type=None):
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
            parts.append(_pack_value(v, col_type))

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
            except (struct.error, IndexError, ValueError):
                result.extend([None] * cnt)

        if len(result) != n_total:
            raise ValueError(
                f"RLE 解码长度不符: 头部 {n_total}, 实际 {len(result)}")
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
    """差分编码：只对整数有效（负数走 zigzag varint；头部最后一个值用有符号 <q）"""
    encoding_id = 3

    def encode(self, values, col_type=None):
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
                    iv = int(v)
                except (ValueError, TypeError):
                    raise ValueError(
                        f"DeltaEncoder 只支持整数，遇到无法转换的值 {v!r}")
                if iv != v:              # 1.5 → 1 是静默丢精度，必须报错
                    raise ValueError(
                        f"DeltaEncoder 只支持整数，遇到非整数值 {v!r}")
                int_vals.append(iv)

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

        # 最后一个值用有符号 8B（旧实现用 <Q，负数直接 struct.error）
        last = prev
        if not (-2 ** 63 <= last <= 2 ** 63 - 1):
            raise ValueError(f"DeltaEncoder 的值超出 int64: {last}")
        header = struct.pack('<Iq', len(values), last)
        return header + bytes(null_bitmap) + b''.join(parts)

    def decode(self, data, col_type):
        if len(data) < 12:
            return []
        try:
            n, _last = struct.unpack_from('<Iq', data, 0)
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
                raise ValueError("Delta 编码数据越界")
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
            else:
                prev += d
                result.append(_coerce(prev, col_type))
        return result

    def estimate(self, values, col_type):
        return len(values) * 2 + 20


# ==================== BitPack ====================

class BitPackEncoder(BaseEncoder):
    """位压缩：小整数（支持负数/大整数：头部保存 offset，值按 v-offset 打包）"""
    encoding_id = 4

    def encode(self, values, col_type=None):
        int_vals = []
        for v in values:
            if v is None:
                int_vals.append(None)
                continue
            if isinstance(v, bool):
                int_vals.append(int(v))
                continue
            try:
                iv = int(v)
            except (ValueError, TypeError):
                raise ValueError(
                    f"BitPackEncoder 只支持整数，遇到无法转换的值 {v!r}")
            if iv != v:
                raise ValueError(
                    f"BitPackEncoder 只支持整数，遇到非整数值 {v!r}")
            int_vals.append(iv)

        non_null = [v for v in int_vals if v is not None]
        offset = min(0, min(non_null)) if non_null else 0
        shifted_max = max(v - offset for v in non_null) if non_null else 0
        bits = max(1, shifted_max.bit_length())
        if bits > 63:
            raise ValueError(
                f"BitPackEncoder 值域过宽（{bits} bit > 63），请使用 Plain/Delta")

        packed_vals = [0 if v is None else (v - offset) for v in int_vals]
        packed = _bit_pack(packed_vals, bits)

        null_bitmap_len = (len(values) + 7) // 8
        null_bitmap = bytearray(null_bitmap_len)
        for i, v in enumerate(values):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))

        header = struct.pack(
            '<IBHq', len(values), bits, null_bitmap_len, offset)
        return header + bytes(null_bitmap) + packed

    def decode(self, data, col_type):
        if len(data) < 15:
            return []
        try:
            n, bits, null_len, offset = struct.unpack_from(
                '<IBHq', data, 0)
        except struct.error:
            return []
        if not (1 <= bits <= 63):
            raise ValueError(f"BitPack 头部 bits 非法: {bits}")
        pos = 15

        if len(data) < pos + null_len:
            return []
        null_bitmap = data[pos:pos + null_len]
        pos += null_len

        try:
            values = _bit_unpack(data[pos:], bits, n)
        except (IndexError, struct.error):
            raise ValueError("BitPack 数据越界")

        result = []
        for i in range(n):
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                result.append(None)
            else:
                v = values[i] + offset
                result.append(_coerce(v, col_type))
        return result

    def estimate(self, values, col_type):
        non_null = [v for v in values
                    if v is not None and isinstance(v, (int, float))]
        if not non_null:
            return 15 + (len(values) + 7) // 8
        offset = min(0, min(int(v) for v in non_null))
        span = max(int(v) for v in non_null) - offset
        bits = max(1, span.bit_length())
        bytes_size = (len(values) * bits + 7) // 8
        return bytes_size + (len(values) + 7) // 8 + 15


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
        """根据数据特征自动选择编码（选择器只负责压缩率，正确性由各编码器保证）"""
        if not values:
            return PlainEncoder()

        n = len(values)
        t = _base_type(col_type)
        non_null = [v for v in values if v is not None]

        # 数值类型
        if t in _INT_TYPES:
            try:
                ints = [int(v) for v in non_null]
            except (TypeError, ValueError):
                return PlainEncoder()
            # 大量连续重复 → RLE（严格优于 Delta；unskip 的
            # tests/test_v4_batch4.py::test_encoder_auto_select 期望如此）
            if _has_many_repeats(values):
                return RLEEncoder()
            if _is_monotonic(ints) and _max_delta_small(ints):
                return DeltaEncoder()
            if ints:
                max_v = max(ints)
                min_v = min(ints)
                if 0 <= min_v and max_v <= 65535:
                    bp_est = BitPackEncoder().estimate(values, col_type)
                    plain_est = PlainEncoder().estimate(values, col_type)
                    if bp_est < plain_est * 0.5:
                        return BitPackEncoder()

        # 字符串
        if t in _STR_TYPES:
            distinct = len(set(str(v) for v in non_null))
            if distinct < n * 0.3:
                return DictEncoder()

        # 重复
        if _has_many_repeats(values):
            return RLEEncoder()

        # 字典（其他类型低基数）
        distinct = len(set(str(v) for v in non_null))
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
