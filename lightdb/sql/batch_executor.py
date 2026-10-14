"""
批处理执行器（Batch Executor）
- 一次处理一批行（默认 1024）
- 减少函数调用开销
- 支持向量化过滤/投影

状态（阶段 0 止血，2026-10）：本模块**不在 SQL 执行路径上**。全仓只有
`tests/test_v3_batch2.py`、`tests/regression/test_performance.py` 引用它，
`lightdb/sql/executor.py` 与 `plan_executor` 对它零引用 —— 修好但未接线。

本次修复的静默错值（缺陷 2）：`BatchScan.next_batch()` 原先返回
`Batch([], rows)`，`columns` 恒为空列表，而 `VectorizedFilter.next_batch`
第一件事是 `if self.column not in batch.columns: return batch`，于是整条
向量化过滤永远是空操作：5 万行输入返回 5 万行（应 16,317 行）。
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
    """一批数据

    `exhausted` 区分两种「0 行」：
    - 上游**真的没有更多行**（`exhausted=True`，终态）
    - 本算子把这一批过滤空了，但上游**还有数据**（`exhausted=False`）

    旧实现没有这个区分，`BatchPipeline.execute()` 一见 0 行就 break，
    于是「某一批被过滤空」= 整条流提前结束（静默丢行）。
    """
    columns: List[str]
    rows: List[list]
    exhausted: bool = False

    def __len__(self):
        return len(self.rows)

    @property
    def is_empty(self):
        return len(self.rows) == 0


class BatchScan:
    """批量扫描

    `columns` 是**必需**的（只是允许省略）：源迭代器只给出行（`list`），
    行里没有列名。省略时退化为位置名 `'0'`/`'1'`/…（第 2 行断言用
    `'1'` 指代第 2 列，该退化仍向后兼容）。真正要按列名过滤（如
    `'age' > 50`）时必须显式传入列名。
    """

    def __init__(self, source_iter, columns: List[str] = None,
                 batch_size=BATCH_SIZE):
        self.source = source_iter
        self.columns = list(columns) if columns else []
        self.batch_size = batch_size

    def next_batch(self) -> Batch:
        rows = []
        for row in self.source:
            rows.append(row)
            if len(rows) >= self.batch_size:
                break
        # 修复点：原实现恒为 Batch([], rows)，列名丢失 -> 向量化过滤空操作。
        # exhausted 表示底层迭代器已耗尽（rows 为空时即流结束）。
        return Batch(self._columns_for(rows), rows, exhausted=not rows)

    def _columns_for(self, rows) -> List[str]:
        if self.columns:
            return list(self.columns)
        width = max((len(r) for r in rows), default=0)
        return [str(i) for i in range(width)]


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
        # 批量投影（exhausted 必须透传，否则下游又分不清「空批」与「流结束」）
        idx = self.indices
        new_rows = [[r[i] for i in idx] for r in batch.rows]
        return Batch([batch.columns[i] for i in idx], new_rows,
                     exhausted=batch.exhausted)


class BatchLimit:
    """批量 LIMIT"""

    def __init__(self, child, limit: int):
        self.child = child
        self.limit = limit
        self.emitted = 0

    def next_batch(self) -> Batch:
        if self.emitted >= self.limit:
            # 达到 LIMIT 即视为流结束（终态，不能让下游再往上拉）
            return Batch([], [], exhausted=True)
        batch = self.child.next_batch()
        remaining = self.limit - self.emitted
        if len(batch.rows) > remaining:
            batch = Batch(batch.columns,
                          batch.rows[:remaining],
                          exhausted=batch.exhausted)
        self.emitted += len(batch.rows)
        return batch


# ==================== 向量化算子 ====================

class VectorizedFilter:
    """
    向量化过滤（NumPy 加速）
    适合数值列的大批量过滤
    """

    def __init__(self, child, column, op: str, value):
        self.child = child
        self.column = column
        self.op = op
        self.value = value

    def next_batch(self) -> Batch:
        batch = self.child.next_batch()
        if batch.is_empty or not HAS_NUMPY:
            return batch

        # 先按列名，再按位置名（'0'/'1'/…，兼容纯位置用法）。
        # 都解析不到就**显式报错** —— 旧实现在这里 `return batch`，
        # 把过滤条件整个吞掉，静默返回全量行。
        ci = self._resolve_index(batch)
        if ci is None:
            raise KeyError(
                f"VectorizedFilter: 列 {self.column!r} 不存在，"
                f"可用列 {list(batch.columns)}")

        if self.op not in ('=', '>', '<', '>=', '<=', '!='):
            raise ValueError(
                f"VectorizedFilter: 不支持的比较符 {self.op!r}")

        # 某一批被过滤空**不等于**流结束：上游还有数据时必须继续取下一批，
        # 否则整条流会提前截断（静默丢行）。只有上游真的耗尽才返回空批。
        while True:
            kept = self._filter_one(batch, ci)
            if kept or batch.exhausted:
                return Batch(batch.columns, kept, exhausted=batch.exhausted)
            batch = self.child.next_batch()
            if batch.is_empty:
                return batch
            ci = self._resolve_index(batch)
            if ci is None:
                raise KeyError(
                    f"VectorizedFilter: 列 {self.column!r} 不存在，"
                    f"可用列 {list(batch.columns)}")

    def _filter_one(self, batch: Batch, ci: int) -> List[list]:
        """对单批做向量化比较，返回入选行；保证不静默丢条件。"""
        # NULL 语义：SQL 里 NULL 参与任何比较都是 UNKNOWN，不入选。
        # 因此先把 NULL 摘出去做向量化比较，再单独计入选下标。
        nn_idx = [i for i, r in enumerate(batch.rows)
                  if ci < len(r) and r[ci] is not None]
        if not nn_idx:
            return []
        values = [batch.rows[i][ci] for i in nn_idx]

        try:
            # 显式 dtype：np.array([int...]) 默认 int32，大值比较会先溢出
            all_int = all(isinstance(v, int) and not isinstance(v, bool)
                          for v in values)
            arr = np.array(values, dtype=np.int64 if all_int else None)
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
            else:
                mask = arr != self.value
        except (TypeError, ValueError):
            # 类型不可比较（如 str 与 int）：逐行回退，保证与逐行过滤一致
            return [batch.rows[i] for i in nn_idx
                    if self._row_match(batch.rows[i], ci)]

        return [batch.rows[nn_idx[i]] for i in np.where(mask)[0]]

    def _resolve_index(self, batch) -> int:
        """把 `self.column` 解析成行内下标，解析不到返回 None。"""
        if self.column in batch.columns:
            return batch.columns.index(self.column)
        # 位置名兼容：'1' 指第 2 列（tests/regression/test_performance.py 用法）
        if isinstance(self.column, int):
            return self.column
        try:
            return int(self.column)
        except (TypeError, ValueError):
            return None

    def _row_match(self, row, ci: int) -> bool:
        if ci >= len(row):
            return False
        v = row[ci]
        if v is None:
            return False
        op, target = self.op, self.value
        try:
            if op == '=': return v == target
            if op == '>': return v > target
            if op == '<': return v < target
            if op == '>=': return v >= target
            if op == '<=': return v <= target
            if op == '!=': return v != target
        except TypeError:
            return False
        raise ValueError(f"VectorizedFilter: 不支持的比较符 {op!r}")


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
        """执行全部（物化）

        关键：**过滤算子返回空批不代表流结束**。上游还有数据（
        `exhausted=False`）时继续拉取，否则「某一批被过滤空」会让整条流
        提前结束，静默丢掉后面所有合格行。
        """
        result = []
        stage = self.stages[-1]
        while True:
            batch = stage.next_batch()
            if batch.is_empty and batch.exhausted:
                break
            result.extend(batch.rows)
        return result

    def iter_batches(self) -> Iterator[Batch]:
        """流式执行（空批但不 exhausted 时继续拉，语义同 execute）"""
        stage = self.stages[-1]
        while True:
            batch = stage.next_batch()
            if batch.is_empty and batch.exhausted:
                yield batch
                return
            if not batch.is_empty:
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
    scanner = BatchScan(iter(data), col_names, batch_size=1024)
    pipeline = BatchPipeline(scanner).filter(pred)
    t0 = time.perf_counter()
    result2 = pipeline.execute()
    t1 = time.perf_counter()
    print(f"批处理:   {t1-t0:.4f}s  → {len(result2)} 行")

    assert len(result1) == len(result2)

    # 向量化过滤
    if HAS_NUMPY:
        scanner = BatchScan(iter(data), col_names, batch_size=4096)
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