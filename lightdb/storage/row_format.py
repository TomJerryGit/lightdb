"""
二进制行格式（紧凑编码）
替代 JSON 序列化，性能提升 3-5x

编码格式:
  [null_bitmap (1B per 8 cols)] [col1_len col1_data] [col2_len col2_data] ...
"""
import struct
from typing import List, Tuple, Any


# ==================== 类型编码 ====================

T_INT = 1
T_BIGINT = 2
T_FLOAT = 3
T_DOUBLE = 4
T_BOOL = 5
T_STRING = 6      # VARCHAR/TEXT
T_BYTES = 7       # BLOB
T_NULL = 8

STRUCT_INT = struct.Struct('<i')
STRUCT_BIGINT = struct.Struct('<q')
STRUCT_FLOAT = struct.Struct('<f')
STRUCT_DOUBLE = struct.Struct('<d')
STRUCT_LEN = struct.Struct('<H')     # 长度用 2 字节
STRUCT_LEN32 = struct.Struct('<I')   # 长文本用 4 字节


class BinaryRowFormat:
    """二进制行编解码"""

    def __init__(self, columns: List[dict]):
        """
        columns: [{'name': 'id', 'type': 'INT'}, ...]
        """
        self.columns = columns
        self.n_cols = len(columns)
        # 预计算类型编码
        self.types = []
        for c in columns:
            t = (c.get('type') or 'VARCHAR').upper()
            if 'BIGINT' in t:
                self.types.append(T_BIGINT)
            elif 'INT' in t:
                self.types.append(T_INT)
            elif 'BOOLEAN' in t or 'BOOL' in t:
                self.types.append(T_BOOL)
            elif 'DOUBLE' in t:
                self.types.append(T_DOUBLE)
            elif 'FLOAT' in t:
                self.types.append(T_FLOAT)
            elif 'DECIMAL' in t or 'NUMERIC' in t:
                # 定点数用 double 近似编码（该二进制行格式只用于实验/Cython
                # fallback，不是生产行格式）。旧实现把 DECIMAL 落到下面的
                # T_STRING 分支，`bytes(1.5)` 直接 TypeError。
                self.types.append(T_DOUBLE)
            elif 'TEXT' in t or 'BLOB' in t:
                self.types.append(T_STRING)  # 大文本
            else:
                self.types.append(T_STRING)
        # null bitmap 长度
        self.null_bitmap_len = (self.n_cols + 7) // 8

    # ==================== 编码 ====================

    def encode(self, row: List[Any]) -> bytes:
        """编码一行"""
        parts = []
        null_bitmap = bytearray(self.null_bitmap_len)
        body = bytearray()

        for i, v in enumerate(row):
            if v is None:
                null_bitmap[i // 8] |= (1 << (i % 8))
                continue

            t = self.types[i]
            if t == T_INT:
                body += STRUCT_INT.pack(int(v))
            elif t == T_BIGINT:
                body += STRUCT_BIGINT.pack(int(v))
            elif t == T_FLOAT:
                body += STRUCT_FLOAT.pack(float(v))
            elif t == T_DOUBLE:
                body += STRUCT_DOUBLE.pack(float(v))
            elif t == T_BOOL:
                body += bytes([1 if v else 0])
            elif t == T_STRING:
                if isinstance(v, str):
                    b = v.encode('utf-8')
                else:
                    b = bytes(v)
                if len(b) < 65535:
                    body += STRUCT_LEN.pack(len(b))
                    body += b
                else:
                    # 大文本标记
                    body += STRUCT_LEN.pack(65535)
                    body += STRUCT_LEN32.pack(len(b))
                    body += b
            else:
                # fallback: 字符串
                if isinstance(v, str):
                    b = v.encode('utf-8')
                else:
                    b = str(v).encode('utf-8')
                body += STRUCT_LEN.pack(len(b))
                body += b

        return bytes(null_bitmap) + bytes(body)

    # ==================== 解码 ====================

    def decode(self, data: bytes) -> List[Any]:
        """解码一行"""
        result = [None] * self.n_cols
        if len(data) < self.null_bitmap_len:
            return result

        null_bitmap = data[:self.null_bitmap_len]
        offset = self.null_bitmap_len

        for i in range(self.n_cols):
            is_null = (null_bitmap[i // 8] >> (i % 8)) & 1
            if is_null:
                continue

            t = self.types[i]
            if t == T_INT:
                v = STRUCT_INT.unpack_from(data, offset)[0]
                offset += 4
                result[i] = v
            elif t == T_BIGINT:
                v = STRUCT_BIGINT.unpack_from(data, offset)[0]
                offset += 8
                result[i] = v
            elif t == T_FLOAT:
                v = STRUCT_FLOAT.unpack_from(data, offset)[0]
                offset += 4
                result[i] = v
            elif t == T_DOUBLE:
                v = STRUCT_DOUBLE.unpack_from(data, offset)[0]
                offset += 8
                result[i] = v
            elif t == T_BOOL:
                result[i] = data[offset] != 0
                offset += 1
            elif t == T_STRING:
                l = STRUCT_LEN.unpack_from(data, offset)[0]
                offset += 2
                if l == 65535:
                    # 大文本
                    l = STRUCT_LEN32.unpack_from(data, offset)[0]
                    offset += 4
                b = data[offset:offset + l]
                offset += l
                try:
                    result[i] = b.decode('utf-8')
                except UnicodeDecodeError:
                    result[i] = b
            else:
                l = STRUCT_LEN.unpack_from(data, offset)[0]
                offset += 2
                b = data[offset:offset + l]
                offset += l
                try:
                    result[i] = b.decode('utf-8')
                except UnicodeDecodeError:
                    result[i] = b

        return result

    # ==================== 批量 ====================

    def encode_many(self, rows: List[List]) -> List[bytes]:
        return [self.encode(r) for r in rows]

    def decode_many(self, blobs: List[bytes]) -> List[List]:
        return [self.decode(b) for b in blobs]


# ==================== 性能测试 ====================

def benchmark_vs_json():
    """对比 JSON 与二进制格式"""
    import json
    import time

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'amount', 'type': 'DECIMAL'},
        {'name': 'created_at', 'type': 'DATETIME'},
    ]

    fmt = BinaryRowFormat(cols)

    # 构造 10000 行
    rows = []
    for i in range(10000):
        rows.append([
            i,
            f'user_{i}',
            123.45 + i * 0.01,
            '2025-01-15 12:00:00',
        ])

    # JSON
    t0 = time.perf_counter()
    json_blobs = [json.dumps(r, ensure_ascii=False).encode()
                   for r in rows]
    t1 = time.perf_counter()
    _ = [json.loads(b.decode()) for b in json_blobs]
    t2 = time.perf_counter()

    print(f"JSON:  encode {t1-t0:.3f}s, decode {t2-t1:.3f}s, "
          f"size {sum(len(b) for b in json_blobs)/1024:.1f}KB")

    # Binary
    t0 = time.perf_counter()
    bin_blobs = fmt.encode_many(rows)
    t1 = time.perf_counter()
    _ = fmt.decode_many(bin_blobs)
    t2 = time.perf_counter()

    print(f"Binary: encode {t1-t0:.3f}s, decode {t2-t1:.3f}s, "
          f"size {sum(len(b) for b in bin_blobs)/1024:.1f}KB")

    print(f"加速: encode {(t1-t0)/(t1-t0):.2f}x, "
          f"size {len(json.dumps(rows[0]))/len(bin_blobs[0]):.1f}x 压缩")


if __name__ == '__main__':
    benchmark_vs_json()