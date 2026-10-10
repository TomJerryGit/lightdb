"""
批处理执行器（Batch Executor）
- 一次处理一批行（默认 1024）
- 减少函数调用开销
- 支持向量化过滤/投影
"""
from dataclasses import dataclass
from typing import Any, Iterator, List

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


BATCH_SIZE = 1024


@dataclass
class Batch:
    """一批数据"""
    columns: List[str]
    rows: List[list]

    def __len__(self):
        return len(self.rows)

    @property
    def is_empty(self):
        return len(self.rows) == 0


class BatchScan:
    """批量扫描（基类）"""

    def __init__(self, source_iter, batch_size=BATCH_SIZE):
        self.source = source_iter
        self.batch_size = batch_size

    def next_batch(self) -> Batch:
        rows = []
        for row in self.source:
            rows.append(row)
            if len(rows) >= self.batch_size:
                break
        return Batch([], rows)


class BatchFilter:
    """批量过滤"""

    def __init__(self, child, predicate_fn):
        self.child = child
        self.predicate_fn = predicate_fn

    def next_batch(self) -> Batch:
        batch = self.child.next_batch()
        if batch.is_empty:
            return batch
        # 一次性过滤整批
        filtered = [r for r in batch.rows if self.predicate_fn(r)]
        return Batch(batch.columns, filtered)


class BatchProject:
    """批量投影"""

    def __init__(self, child, indices: List[int]):
        self.child = child
        self.indices = indices

    def next_batch(self) -> Batch:
        batch = self.child.next_batch()
        if batch.is_empty:
            return batch
        # 批量投影
        idx = self.indices
        new_rows = [[r[i] for i in idx] for r in batch.rows]
        return Batch([batch.columns[i] for i in idx], new_rows)


class BatchLimit:
    """批量 LIMIT"""

    def __init__(self, child, limit: int):
        self.child = child
        self.limit = limit
        self.emitted = 0

    def next_batch(self) -> Batch:
        if self.emitted >= self.limit:
            return Batch([], [])
        batch = self.child.next_batch()
        remaining = self.limit - self.emitted
        if len(batch.rows) > remaining:
            batch = Batch(batch.columns,
                          batch.rows[:remaining])
        self.emitted += len(batch.rows)
        return batch


# ==================== 向量化算子 ====================

class VectorizedFilter:
    """
    向量化过滤（NumPy 加速）
    适合数值列的大批量过滤
    """

    def __init__(self, child, column: str, op: str, value):
        self.child = child
        self.column = column
        self.op = op
        self.value = value

    def next_batch(self) -> Batch:
        batch = self.child.next_batch()
        if batch.is_empty or not HAS_NUMPY:
            return batch

        if self.column not in batch.columns:
            return batch

        ci = batch.columns.index(self.column)

        try:
            arr = np.array([r[ci] for r in batch.rows])
            if self.op == '=':
                mask = arr == self.value
            elif self.op == '>':
                mask = arr > self.value
            elif self.op == '<':
                mask = arr < self.value
            elif self.op == '>=':
                mask = arr >= self.value
            elif self.op == '<=':
                mask = arr <= self.value
            elif self.op == '!=':
                mask = arr != self.value
            else:
                return batch

            indices = np.where(mask)[0]
            new_rows = [batch.rows[i] for i in indices]
            return Batch(batch.columns, new_rows)
        except Exception:
            return batch


class VectorizedProject:
    """向量化投影"""

    def __init__(self, child, indices: List[int]):
        self.child = child
        self.indices = indices

    def next_batch(self) -> Batch:
        batch = self.child.next_batch()
        if batch.is_empty:
            return batch
        idx = self.indices
        # 使用列表推导更快（在多数场景下）
        new_rows = [[r[i] for i in idx] for r in batch.rows]
        new_cols = [batch.columns[i] for i in idx]
        return Batch(new_cols, new_rows)


# ==================== 完整批处理管道 ====================

class BatchPipeline:
    """批处理管道：Scan → Filter → Project → Limit"""

    def __init__(self, scanner):
        self.stages = [scanner]

    def filter(self, predicate_fn):
        self.stages.append(BatchFilter(self.stages[-1], predicate_fn))
        return self

    def project(self, indices):
        self.stages.append(BatchProject(self.stages[-1], indices))
        return self

    def limit(self, n):
        self.stages.append(BatchLimit(self.stages[-1], n))
        return self

    def execute(self) -> List[list]:
        """执行全部（物化）"""
        result = []
        stage = self.stages[-1]
        while True:
            batch = stage.next_batch()
            if batch.is_empty:
                break
            result.extend(batch.rows)
        return result

    def iter_batches(self) -> Iterator[Batch]:
        """流式执行"""
        stage = self.stages[-1]
        while True:
            batch = stage.next_batch()
            if batch.is_empty:
                yield batch
                return
            yield batch


# ==================== 性能对比 ====================

def benchmark_batch_vs_row():
    """对比批处理 vs 逐行处理"""
    import time

    # 生成 100000 行数据
    N = 100000
    data = [[i, f'name_{i}', i % 100, i * 1.5] for i in range(N)]
    col_names = ['id', 'name', 'age', 'score']

    # 条件：age > 50 且 score < 100000
    def pred(row):
        return row[2] > 50 and row[3] < 100000

    # 逐行过滤
    t0 = time.perf_counter()
    result1 = [r for r in data if pred(r)]
    t1 = time.perf_counter()
    print(f"逐行过滤: {t1-t0:.4f}s  → {len(result1)} 行")

    # 批处理过滤
    scanner = BatchScan(iter(data), batch_size=1024)
    pipeline = BatchPipeline(scanner).filter(pred)
    t0 = time.perf_counter()
    result2 = pipeline.execute()
    t1 = time.perf_counter()
    print(f"批处理:   {t1-t0:.4f}s  → {len(result2)} 行")

    assert len(result1) == len(result2)

    # 向量化过滤
    if HAS_NUMPY:
        scanner = BatchScan(iter(data), batch_size=4096)
        pipeline = BatchPipeline(scanner)
        pipeline.stages.append(
            VectorizedFilter(pipeline.stages[-1], 'age', '>', 50))
        pipeline.stages.append(
            VectorizedFilter(pipeline.stages[-1], 'score', '<', 100000))
        t0 = time.perf_counter()
        result3 = pipeline.execute()
        t1 = time.perf_counter()
        print(f"向量化:   {t1-t0:.4f}s  → {len(result3)} 行")


if __name__ == '__main__':
    benchmark_batch_vs_row()