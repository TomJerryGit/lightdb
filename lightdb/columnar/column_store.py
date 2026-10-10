"""
列存储管理器（修复版）

修复:
- _load() 从 meta 取真实 col_type（不再硬编码 VARCHAR）
- _save() / _load() 格式一致
- 数据类型映射
- 列扫描边界
"""
import os
import pickle
import struct
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .chunk import ColumnChunk, ChunkGroup
from .stats import ColumnStoreStats


class ColumnStore:
    """列存储（一表多列）"""

    def __init__(self, name: str, cols: List[dict],
                 data_dir: str):
        self.name = name
        self.cols = cols
        self.col_names = [c['name'] for c in cols]
        self.col_types = [(c.get('type') or 'VARCHAR').upper()
                          for c in cols]
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.chunks: Dict[str, List[ColumnChunk]] = {
            n: [] for n in self.col_names}

        self.meta_path = self.data_dir / f'{name}.colmeta'
        self.data_path = self.data_dir / f'{name}.coldata'
        self.row_count = 0
        self.chunk_size = 4096
        self.lock = threading.RLock()
        self.stats = ColumnStoreStats()

        if self.meta_path.exists():
            self._load()

    # ==================== 写入 ====================

    def append_rows(self, rows: List[List]):
        """追加行"""
        if not rows:
            return
        n = len(rows)

        with self.lock:
            for ci, col_name in enumerate(self.col_names):
                col_values = []
                for r in rows:
                    col_values.append(
                        r[ci] if ci < len(r) else None)

                current = self.chunks[col_name]

                # 追加到最后一个未满的 chunk
                if current and len(current[-1].values) < self.chunk_size:
                    last = current[-1]
                    remaining = self.chunk_size - len(last.values)
                    last.values.extend(col_values[:remaining])
                    last.encode()
                    col_values = col_values[remaining:]

                # 新 chunk
                col_type = self.col_types[ci]
                while col_values:
                    chunk = ColumnChunk(
                        col_name, col_type,
                        col_values[:self.chunk_size])
                    chunk.encode()
                    current.append(chunk)
                    col_values = col_values[self.chunk_size:]

            self.row_count += n
            self.stats.rows_added += n

    def flush(self):
        with self.lock:
            self._save()

    # ==================== 读取 ====================

    def scan(self, columns: List[str] = None) -> Dict[str, List]:
        with self.lock:
            target = columns or self.col_names
            result = {}
            for col in target:
                if col not in self.chunks:
                    result[col] = []
                    continue
                values = []
                for chunk in self.chunks[col]:
                    values.extend(chunk.values)
                result[col] = values
            return result

    def scan_range(self, columns: List[str],
                   start: int, end: int) -> Dict[str, List]:
        with self.lock:
            target = columns or self.col_names
            result = {}
            for col in target:
                if col not in self.chunks:
                    result[col] = []
                    continue
                values = []
                pos = 0
                for chunk in self.chunks[col]:
                    clen = len(chunk.values)
                    if pos + clen <= start:
                        pos += clen
                        continue
                    if pos >= end:
                        break
                    ls = max(0, start - pos)
                    le = min(clen, end - pos)
                    values.extend(chunk.values[ls:le])
                    pos += clen
                result[col] = values
            return result

    def get_column_stats(self, col_name: str) -> Dict:
        with self.lock:
            chunks = self.chunks.get(col_name, [])
            if not chunks:
                return {}
            min_v, max_v = None, None
            null_count = 0
            total_count = 0
            sum_val = 0.0
            for c in chunks:
                s = c.stats
                if s.min_value is not None:
                    if min_v is None or s.min_value < min_v:
                        min_v = s.min_value
                if s.max_value is not None:
                    if max_v is None or s.max_value > max_v:
                        max_v = s.max_value
                null_count += s.null_count
                total_count += s.total_count
                sum_val += s.sum_value
            non_null = max(1, total_count - null_count)
            return {
                'min': min_v,
                'max': max_v,
                'null_count': null_count,
                'total_count': total_count,
                'sum': sum_val,
                'avg': sum_val / non_null,
                'chunks': len(chunks),
            }

    # ==================== 持久化 ====================

    def _save(self):
        meta = {
            'name': self.name,
            'cols': self.cols,
            'col_types': self.col_types,
            'row_count': self.row_count,
            'chunks': {
                n: [c.to_dict() for c in cl]
                for n, cl in self.chunks.items()
            },
        }
        tmp = self.meta_path.with_suffix('.tmp')
        with open(tmp, 'wb') as f:
            pickle.dump(meta, f)
        tmp.replace(self.meta_path)

        tmp = self.data_path.with_suffix('.tmp')
        with open(tmp, 'wb') as f:
            for n in self.col_names:
                for chunk in self.chunks[n]:
                    encoded = chunk.encode()
                    f.write(struct.pack('<I', len(encoded)))
                    f.write(encoded)
        tmp.replace(self.data_path)

    def _load(self):
        """★ 修复: 从 meta 取真实类型"""
        try:
            with open(self.meta_path, 'rb') as f:
                meta = pickle.load(f)

            self.row_count = meta.get('row_count', 0)

            # 从 meta 恢复类型
            col_types_meta = meta.get('col_types')
            if col_types_meta and len(col_types_meta) == len(
                    self.col_names):
                self.col_types = [
                    t.upper() for t in col_types_meta
                ]
            # 否则按 cols 重算
            elif meta.get('cols'):
                self.cols = meta['cols']
                self.col_types = [
                    (c.get('type') or 'VARCHAR').upper()
                    for c in self.cols
                ]

            # 读数据
            if not self.data_path.exists():
                return

            with open(self.data_path, 'rb') as f:
                for ci, n in enumerate(self.col_names):
                    n_chunks = len(
                        meta.get('chunks', {}).get(n, []))
                    col_type = self.col_types[ci]
                    for _ in range(n_chunks):
                        len_bytes = f.read(4)
                        if len(len_bytes) < 4:
                            break
                        data_len = struct.unpack(
                            '<I', len_bytes)[0]
                        raw = f.read(data_len)
                        # ★ 使用真实类型
                        chunk = ColumnChunk(n, col_type)
                        chunk.decode(raw)
                        self.chunks[n].append(chunk)

        except Exception as e:
            # 加载失败：保留空列
            import traceback
            from ..logger import logger
            logger.warn(
                f"列存储加载 {self.name} 失败: {e}\n"
                f"{traceback.format_exc()[:500]}")

    # ==================== 统计 ====================

    def stats_report(self) -> Dict:
        with self.lock:
            total_size = 0
            encoded_size = 0
            uncompressed = 0
            for n in self.col_names:
                for c in self.chunks[n]:
                    encoded_size += c.compressed_size
                    uncompressed += len(c.values) * 10
            return {
                'name': self.name,
                'row_count': self.row_count,
                'columns': len(self.col_names),
                'chunks_per_col': {
                    n: len(cl)
                    for n, cl in self.chunks.items()
                },
                'encoded_size': encoded_size,
                'uncompressed_estimate': uncompressed,
                'compression_ratio': (
                    uncompressed / max(1, encoded_size)),
            }