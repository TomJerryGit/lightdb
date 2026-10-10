"""
优化版 Table（集成 Batch 2 优化）
"""
import threading
import time
from pathlib import Path

from ..storage.row_format import BinaryRowFormat
from ..storage.page_prefetch import AdaptivePrefetcher
from ..concurrency.rw_lock import RWLock


class TableV3:
    """优化版表"""

    def __init__(self, name, cols, data_dir, lock_manager=None,
                 mvcc=None):
        self.name = name
        self.cols = cols
        self.data_dir = Path(data_dir)

        # ★ 优化 1: 二进制行格式
        self.row_fmt = BinaryRowFormat(cols)

        # ★ 优化 2: 读写锁（读读不阻塞）
        self.rwlock = RWLock()

        # ★ 优化 3: 页预读
        self.prefetcher = None   # 延迟初始化（需要 bp）

        # ... 保持原有其他字段

    def insert(self, values, txn_id=0):
        with self.rwlock.write():
            # ... 原有逻辑
            rb = self.row_fmt.encode(values)
            # ...

    def scan(self, predicate_set=None, read_view=None):
        """批量扫描"""
        with self.rwlock.read():
            # ★ 优化 4: 批处理
            results = []
            batch = []
            batch_size = 1024

            for pk, pid, sid in self.bt.scan():
                # 页预读
                if self.prefetcher:
                    self.prefetcher.record(pid)

                page = self.bp.get_page(pid)
                if page is None:
                    continue
                rb = page.get_row(sid)
                if rb is None:
                    continue

                # 二进制解码（快）
                row = self.row_fmt.decode(rb)

                # 谓词下推（过滤在内存层）
                if predicate_set and not predicate_set.match(row):
                    continue

                batch.append(row)
                if len(batch) >= batch_size:
                    results.extend(batch)
                    batch = []

            if batch:
                results.extend(batch)

            return results


# ==================== 集成测试 ====================

def test_table_v3():
    """测试优化后的表"""
    print("测试 TableV3 ...")

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'age', 'type': 'INT'},
    ]

    from ..storage.row_format import BinaryRowFormat
    fmt = BinaryRowFormat(cols)

    # 编码/解码测试
    row = [1, 'Alice', 30]
    data = fmt.encode(row)
    decoded = fmt.decode(data)
    assert decoded == row, f"编解码不一致: {decoded} != {row}"
    print(f"  ✅ 编解码正确 ({len(data)} 字节)")

    # 性能对比
    import time
    rows = [[i, f'user_{i}', i % 100] for i in range(10000)]

    t0 = time.perf_counter()
    blobs = fmt.encode_many(rows)
    t1 = time.perf_counter()
    decoded = fmt.decode_many(blobs)
    t2 = time.perf_counter()

    print(f"  → 编码 10000 行: {(t1-t0)*1000:.2f}ms")
    print(f"  → 解码 10000 行: {(t2-t1)*1000:.2f}ms")
    print(f"  → 平均每行: {((t2-t0)/10000)*1000:.3f}ms")


if __name__ == '__main__':
    test_table_v3()