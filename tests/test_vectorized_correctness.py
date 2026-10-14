"""向量化 / 批处理路径正确性锁测试（阶段 0 止血）

背景（见 `docs/review/01_engine.md` §2.3/§2.4、`docs/FAILURE_TRIAGE.md` §9）：
这两条路径原先属于「成功返回错结果」的 P0 静默错值：

1. `VectorizedAggregate`：`np.array(list[int])` 默认 int32，10 万行 `SUM`
   返回 704982704.0（正确值 4999950000）；列里含 NULL 时 `np.array` 变
   object dtype，`SUM/AVG/MIN/MAX` 直接抛 `TypeError`；`COUNT` 把 NULL
   计入总数。
2. 批处理 `VectorizedFilter`：`BatchScan.next_batch()` 返回
   `Batch([], rows)`（`columns` 恒空），过滤算子首行即 `return batch`，
   等于空操作 —— 5 万行输入返回 5 万行（应 16,317 行）。
3. 同一条链路的第二个静默丢行点：`BatchPipeline.execute()` 一见 0 行就
   视为流结束，于是「某一批被过滤空」= 整条流提前截断。

本文件是**锁测试**：断言均为「向量化结果 == 逐行/Python 结果」的对照，
不依赖任何性能阈值，也不改动既有测试的任何断言。

注意：这两条路径目前**不在 SQL 执行路径上**（`Executor` /
`plan_executor` 对 `olap` / `VectorizedAggregate` / `ColumnStore` 零引用）。
本文件只锁正确性，验证「修好但未接线」。
"""
import math
import tempfile

import pytest

from lightdb.columnar.column_store import ColumnStore
from lightdb.olap.vectorized_engine import (
    VectorizedAggregate, VectorizedFilter, VectorizedScan)
from lightdb.sql.batch_executor import (
    BatchPipeline, BatchScan, VectorizedFilter as BatchVectorizedFilter)

OPS = {
    '>': lambda v, t: v > t,
    '<': lambda v, t: v < t,
    '>=': lambda v, t: v >= t,
    '<=': lambda v, t: v <= t,
    '=': lambda v, t: v == t,
    '!=': lambda v, t: v != t,
}


# ==================== 工具 ====================

def _store(tmpdir, name, col_type, values):
    store = ColumnStore('t', [{'name': name, 'type': col_type}], tmpdir)
    if values:
        store.append_rows([[v] for v in values])
    return store


def _agg(store, col, func):
    return VectorizedAggregate(
        VectorizedScan(store, [col]), col, func).execute()


def _py_agg(values, func):
    """Python 逐行参照实现（SQL 语义：聚合忽略 NULL，COUNT(col) 只计非 NULL）"""
    nn = [v for v in values if v is not None]
    if func == 'COUNT':
        return len(nn)
    if not nn:
        return None
    if func == 'SUM':
        return sum(nn)
    if func == 'AVG':
        return sum(nn) / len(nn)
    if func == 'MIN':
        return min(nn)
    if func == 'MAX':
        return max(nn)
    raise AssertionError(f'unknown func {func}')


def _run_batch_filter(rows, columns, column, op, value, batch_size):
    """真正排空整条过滤流（逐批拉取直到 upstream exhausted）"""
    pipeline = BatchPipeline(BatchScan(iter(rows), columns, batch_size=batch_size))
    pipeline.stages.append(
        BatchVectorizedFilter(pipeline.stages[-1], column, op, value))
    return pipeline.execute()


# ==================== ① 10 万行 SUM == Python 逐行求和 ====================

def test_sum_100k_equals_python_rowwise():
    """锁：10 万行 SUM 必须等于 Python 逐行求和（原为 704982704.0）"""
    n = 100000
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'amount', 'INT', list(range(n)))
        expected = sum(range(n))                      # 4999950000
        got = _agg(store, 'amount', 'SUM')
        assert got == expected, f'SUM 溢出/错值: got={got} expect={expected}'
        # 必须是精确整数，不能被塞进 float32/int32
        assert got == 4999950000
        assert isinstance(got, int)

        # 同一批数据上的其余聚合也必须正确
        assert _agg(store, 'amount', 'COUNT') == n
        assert _agg(store, 'amount', 'MIN') == 0
        assert _agg(store, 'amount', 'MAX') == n - 1
        assert math.isclose(_agg(store, 'amount', 'AVG'),
                            sum(range(n)) / n, rel_tol=1e-12)


def test_sum_100k_batch_boundary_invariance():
    """锁：批大小变化不得改变 SUM 结果（含跨批累加）"""
    n = 100000
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'v', 'INT', list(range(n)))
        expected = sum(range(n))
        for batch_size in (1, 4096, 65536, 200000):
            scan = VectorizedScan(store, ['v'], batch_size=batch_size)
            got = VectorizedAggregate(scan, 'v', 'SUM').execute()
            assert got == expected, f'batch_size={batch_size}: {got} != {expected}'


# ==================== ② 含 NULL 的 SUM/COUNT 语义 ====================

def test_aggregates_with_null_semantics():
    """锁：SUM/AVG/MIN/MAX 忽略 NULL；COUNT(*) 计行，COUNT(col) 只计非 NULL"""
    values = [1, None, 3, None, 5, 10000000]
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'v', 'INT', values)

        for func in ('SUM', 'AVG', 'MIN', 'MAX'):
            got = _agg(store, 'v', func)
            expected = _py_agg(values, func)
            assert got == expected, f'{func} 含 NULL 错值: {got} != {expected}'

        assert _agg(store, 'v', 'SUM') == 10000009
        assert _agg(store, 'v', 'COUNT') == 4          # 非 NULL 计数
        # COUNT(*) 计行数
        assert VectorizedAggregate(
            VectorizedScan(store, ['v']), '*', 'COUNT').execute() == len(values)


def test_aggregates_all_null_and_empty():
    """锁：全 NULL / 空输入不抛异常，语义明确"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'v', 'INT', [None, None, None])
        assert _agg(store, 'v', 'SUM') is None
        assert _agg(store, 'v', 'AVG') is None
        assert _agg(store, 'v', 'MIN') is None
        assert _agg(store, 'v', 'MAX') is None
        assert _agg(store, 'v', 'COUNT') == 0
        assert VectorizedAggregate(
            VectorizedScan(store, ['v']), '*', 'COUNT').execute() == 3

    with tempfile.TemporaryDirectory() as tmp:
        store = ColumnStore('t', [{'name': 'v', 'type': 'INT'}], tmp)
        for func in ('SUM', 'AVG', 'MIN', 'MAX'):
            assert _agg(store, 'v', func) is None
        assert _agg(store, 'v', 'COUNT') == 0


@pytest.mark.parametrize('col_type,values', [
    ('INT', [i for i in range(1000)]),
    ('INT', [i if i % 7 else None for i in range(1000)]),
    ('DOUBLE', [i * 1.5 for i in range(1000)]),
    ('DOUBLE', [None if i % 11 == 0 else i * 1.5 for i in range(1000)]),
])
def test_aggregates_matrix_matches_python(col_type, values):
    """锁：聚合 x dtype(INT/DOUBLE) x 含 NULL x 全部函数 与 Python 一致"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'v', col_type, values)
        for func in ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX'):
            got = _agg(store, 'v', func)
            expected = _py_agg(values, func)
            if isinstance(expected, float):
                assert math.isclose(got, expected, rel_tol=1e-12), \
                    f'{func} {col_type}: {got} != {expected}'
            else:
                assert got == expected, f'{func} {col_type}: {got} != {expected}'


def test_unsupported_aggregate_raises():
    """锁：不支持的聚合函数显式报错，不返回 None 冒充成功"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, 'v', 'INT', [1, 2, 3])
        for func in ('MEDIAN', 'STDDEV', 'NOPE'):
            with pytest.raises(ValueError):
                _agg(store, 'v', func)


# ==================== ③ 向量化过滤 == 逐行过滤 ====================

def test_vectorized_filter_matches_rowwise_multibatch():
    """锁：多批（含含 NULL 列）过滤行数与逐行完全一致（原为返回全量）"""
    n = 20000
    rows = [[i,
             None if i % 7 == 0 else i,
             None if i % 11 == 0 else i * 1.5]
            for i in range(n)]
    columns = ['id', 'age', 'score']

    for column, ci in (('id', 0), ('age', 1), ('score', 2)):
        for op, fn in OPS.items():
            for value in (0, 137.5, 9999):
                expected = [r for r in rows
                            if r[ci] is not None and fn(r[ci], value)]
                # 1 覆盖「一批只装得下 1 行」的边界，7/4096/65536 覆盖多批
                for batch_size in (1, 7, 4096, 65536):
                    got = _run_batch_filter(rows, columns, column, op,
                                            value, batch_size)
                    assert got == expected, (
                        f'{column} {op} {value} bs={batch_size}: '
                        f'{len(got)} 行 != {len(expected)} 行')


def test_vectorized_filter_50000_rows_expect_16317():
    """锁：缺陷 2 的原始失败用例规模 —— 5 万行应得 16,317 行（原 50,000）"""
    data = [[i, f'name_{i}', i % 100, i * 1.5] for i in range(50000)]
    columns = ['id', 'name', 'age', 'score']

    rowwise = [r for r in data if r[2] > 50 and r[3] < 50000]
    assert len(rowwise) == 16317

    pipeline = BatchPipeline(
        BatchScan(iter(data), columns, batch_size=4096))
    pipeline.stages.append(
        BatchVectorizedFilter(pipeline.stages[-1], 'age', '>', 50))
    pipeline.stages.append(
        BatchVectorizedFilter(pipeline.stages[-1], 'score', '<', 50000))
    got = pipeline.execute()

    assert len(got) == 16317
    assert got == rowwise


def test_vectorized_filter_empty_and_single_batch():
    """锁：空批 / 单批 / 恰好一整批 / 多批 边界"""
    cases = [
        ([], 1024),
        ([[0, 0]], 1024),
        ([[i, i] for i in range(3)], 1),
        ([[i, i] for i in range(4096)], 4096),
        ([[i, i] for i in range(4097)], 4096),
        ([[i, i] for i in range(10000)], 8192),
    ]
    for rows, batch_size in cases:
        expected = [r for r in rows if r[1] > 5]
        got = _run_batch_filter(rows, ['id', 'v'], 'v', '>', 5, batch_size)
        assert got == expected, f'rows={len(rows)} bs={batch_size}'


def test_vectorized_filter_keeps_pulling_after_emptied_batch():
    """锁：某一批被过滤空不得提前结束整条流（静默丢行）"""
    # 第 1 批（bs=2）全部不入选，后面还有合格行
    rows = [[0, 0], [1, 1], [2, 99], [3, 98], [4, 97]]
    got = _run_batch_filter(rows, ['id', 'v'], 'v', '>', 50, 2)
    assert got == [[2, 99], [3, 98], [4, 97]]


def test_vectorized_filter_null_column_yields_zero_rows():
    """锁：NULL 参与比较为 UNKNOWN，全 NULL 列过滤结果 0 行"""
    rows = [[i, None] for i in range(1000)]
    for op in OPS:
        for batch_size in (1, 512, 4096):
            got = _run_batch_filter(rows, ['id', 'v'], 'v', op, 5, batch_size)
            assert got == [], f'{op} bs={batch_size} 应为 0 行'


def test_vectorized_filter_position_and_name_agree():
    """锁：位置名（'1'）与显式列名（'b'）解析结果一致"""
    rows = [[i, i * 2, i * 1.5] for i in range(20000)]
    by_pos = _run_batch_filter(rows, None, '1', '>', 19000, 4096)
    by_name = _run_batch_filter(rows, ['a', 'b', 'c'], 'b', '>', 19000, 4096)
    assert by_pos == by_name == [r for r in rows if r[1] > 19000]


def test_vectorized_filter_unresolvable_column_raises():
    """锁：列解析不到必须显式报错，绝不静默返回全量行"""
    rows = [[i, i] for i in range(10)]
    with pytest.raises(KeyError):
        _run_batch_filter(rows, ['a', 'b'], 'nope', '>', 1, 4)


def test_vectorized_filter_unsupported_op_raises():
    """锁：不支持的比较符显式报错"""
    rows = [[i, i] for i in range(10)]
    with pytest.raises(ValueError):
        _run_batch_filter(rows, ['a', 'b'], 'b', 'LIKE', 'x', 4)


# ==================== 附：OLAP 侧过滤算子不回退成空操作 ====================

def test_olap_vectorized_filter_unknown_column_raises():
    """锁：lightdb/olap 的 VectorizedFilter 也不得把未知列当空操作"""
    from lightdb.olap.vectorized_engine import VectorBatch

    class _Scan:
        def __init__(self):
            self.done = False

        def next_batch(self):
            if self.done:
                return None
            self.done = True
            return VectorBatch(columns=['a'], data={'a': [1, 2, 3]}, size=3)

    filt = VectorizedFilter(_Scan(), 'missing', '>', 1)
    with pytest.raises(KeyError):
        filt.next_batch()
