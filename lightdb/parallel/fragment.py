"""
查询分片
- 把一个大查询拆成 N 个子任务
- 支持范围分片、哈希分片、块分片
"""
import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# 分片策略
SPLIT_RANGE = 'range'
SPLIT_HASH = 'hash'
SPLIT_CHUNK = 'chunk'
SPLIT_PARTITION = 'partition'


@dataclass
class Fragment:
    """查询分片"""
    fragment_id: int
    total: int                 # 总分片数
    # 分片参数
    split_type: str = SPLIT_RANGE
    start_key: Any = None      # range 用
    end_key: Any = None
    # chunk 用
    offset: int = 0
    limit: int = 0
    # 并行执行上下文
    worker_id: int = -1        # 分配给哪个 worker
    status: str = 'pending'    # pending / running / done / failed
    result: Any = None
    error: str = ''
    started_at: float = 0
    finished_at: float = 0
    rows_processed: int = 0

    def elapsed_ms(self) -> float:
        if not self.started_at:
            return 0
        end = self.finished_at or __import__('time').time()
        return (end - self.started_at) * 1000


class FragmentSplitter:
    """分片器"""

    @staticmethod
    def split_by_range(total_rows: int, n_fragments: int) -> List[Fragment]:
        """按行号范围分片"""
        if n_fragments <= 0:
            n_fragments = 1
        size = (total_rows + n_fragments - 1) // n_fragments
        fragments = []
        for i in range(n_fragments):
            start = i * size
            end = min(start + size, total_rows)
            if start >= end:
                continue
            fragments.append(Fragment(
                fragment_id=i,
                total=n_fragments,
                split_type=SPLIT_RANGE,
                offset=start,
                limit=end - start,
            ))
        return fragments

    @staticmethod
    def split_by_chunks(total_rows: int,
                        chunk_size: int = 65536) -> List[Fragment]:
        """按块大小分片"""
        n = (total_rows + chunk_size - 1) // chunk_size
        fragments = []
        for i in range(n):
            start = i * chunk_size
            end = min(start + chunk_size, total_rows)
            fragments.append(Fragment(
                fragment_id=i,
                total=n,
                split_type=SPLIT_CHUNK,
                offset=start,
                limit=end - start,
            ))
        return fragments

    @staticmethod
    def split_by_partition(partitions: List[Any]) -> List[Fragment]:
        """按已有分区"""
        fragments = []
        for i, p in enumerate(partitions):
            fragments.append(Fragment(
                fragment_id=i,
                total=len(partitions),
                split_type=SPLIT_PARTITION,
                start_key=p.get('start'),
                end_key=p.get('end'),
            ))
        return fragments

    @staticmethod
    def split_by_hash(values: List[Any], n_fragments: int) -> List[Fragment]:
        """按哈希分片"""
        fragments = [Fragment(
            fragment_id=i,
            total=n_fragments,
            split_type=SPLIT_HASH,
        ) for i in range(n_fragments)]
        return fragments

    @staticmethod
    def hash_bucket(value: Any, n: int) -> int:
        """计算哈希桶"""
        s = str(value).encode('utf-8')
        h = int(hashlib.md5(s).hexdigest()[:8], 16)
        return h % n


class FragmentResult:
    """分片结果"""

    def __init__(self, fragment_id: int):
        self.fragment_id = fragment_id
        self.data: Any = None
        self.row_count: int = 0
        self.elapsed_ms: float = 0
        self.error: str = ''

    def __repr__(self):
        return (f"FragmentResult(id={self.fragment_id}, "
                f"rows={self.row_count}, "
                f"elapsed={self.elapsed_ms:.1f}ms)")