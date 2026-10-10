"""
列段（Segment）
- 一组 Chunk 组成一个段
- 段有独立的元数据和统计
"""
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from .chunk import ColumnChunk, ChunkGroup
from .stats import ColumnStoreStats


@dataclass
class SegmentMeta:
    """段元数据"""
    segment_id: int = 0
    row_count: int = 0
    column_count: int = 0
    size_bytes: int = 0
    created_at: float = 0
    min_key: Optional[int] = None
    max_key: Optional[int] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class Segment:
    """
    列段：一组行的列式存储
    """

    def __init__(self, segment_id: int, columns: List[str],
                 col_types: List[str]):
        self.segment_id = segment_id
        self.columns = columns
        self.col_types = col_types
        self.chunks: Dict[str, List[ColumnChunk]] = {
            c: [] for c in columns
        }
        self.row_count = 0
        self.chunk_size = 4096
        self.meta = SegmentMeta(
            segment_id=segment_id,
            column_count=len(columns),
        )

    # ==================== 写入 ====================

    def append_rows(self, rows: List[List]):
        """追加行"""
        n = len(rows)
        if n == 0:
            return

        for ci, col_name in enumerate(self.columns):
            values = []
            for r in rows:
                values.append(r[ci] if ci < len(r) else None)

            col_chunks = self.chunks[col_name]

            # 追加到最后一个未满的 chunk
            if col_chunks and len(col_chunks[-1].values) < self.chunk_size:
                last = col_chunks[-1]
                remaining = self.chunk_size - len(last.values)
                last.values.extend(values[:remaining])
                last.encode()
                values = values[remaining:]

            # 新 chunk
            while values:
                chunk = ColumnChunk(
                    col_name, self.col_types[ci],
                    values[:self.chunk_size])
                chunk.encode()
                col_chunks.append(chunk)
                values = values[self.chunk_size:]

        self.row_count += n
        self.meta.row_count = self.row_count

    # ==================== 读取 ====================

    def scan(self, columns: List[str] = None) -> Dict[str, List]:
        """扫描指定列"""
        target = columns or self.columns
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
        """扫描行范围"""
        target = columns or self.columns
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

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        total_size = 0
        for col_chunks in self.chunks.values():
            for c in col_chunks:
                total_size += c.compressed_size
        self.meta.size_bytes = total_size
        return {
            'segment_id': self.segment_id,
            'row_count': self.row_count,
            'column_count': len(self.columns),
            'size_bytes': total_size,
            'chunks': {
                c: len(cl) for c, cl in self.chunks.items()
            },
        }

    def is_full(self, max_rows: int = 1_000_000) -> bool:
        return self.row_count >= max_rows


class SegmentManager:
    """段管理器"""

    def __init__(self, name: str, columns: List[dict],
                 max_rows_per_segment: int = 1_000_000):
        self.name = name
        self.columns = columns
        self.col_names = [c['name'] for c in columns]
        self.col_types = [(c.get('type') or 'VARCHAR').upper()
                          for c in columns]
        self.max_rows_per_segment = max_rows_per_segment
        self.segments: List[Segment] = []
        self._next_id = 1

    def append_rows(self, rows: List[List]):
        """追加到当前段，满了就新建段"""
        if not rows:
            return
        if not self.segments:
            self.segments.append(self._new_segment())
        current = self.segments[-1]
        if current.is_full(self.max_rows_per_segment):
            current = self._new_segment()
            self.segments.append(current)
        current.append_rows(rows)

    def _new_segment(self) -> Segment:
        seg = Segment(self._next_id, self.col_names, self.col_types)
        self._next_id += 1
        return seg

    def scan(self, columns: List[str] = None) -> Dict[str, List]:
        """跨段扫描"""
        target = columns or self.col_names
        result = {c: [] for c in target}
        for seg in self.segments:
            data = seg.scan(target)
            for c in target:
                result[c].extend(data.get(c, []))
        return result

    def total_rows(self) -> int:
        return sum(s.row_count for s in self.segments)

    def stats(self) -> Dict:
        return {
            'name': self.name,
            'segments': len(self.segments),
            'total_rows': self.total_rows(),
            'segments_detail': [s.stats() for s in self.segments],
        }