# -*- coding: utf-8 -*-
"""
OLAP / 列存 / 并行模块「行存对拍矩阵」（正确性止血验收，2026-10-20）

背景：`lightdb/olap/**`、`lightdb/columnar/**`、`lightdb/parallel/**` 在
SQL 执行路径上**零引用**（实验性，见 `lightdb/olap/status.py`）。本文件
不改变这个状态，只回答一个问题：**这些模块自己算得对不对**。

对拍方式：同一批数据
    * 行存参考 = `lightdb.db.table.Table`（真实存储层：BTree + JSON 行 +
      `_aggregate`），必要时叠加 Python 参考实现；
    * 被测 = 列存 `ColumnStore` / 向量化引擎 / OLAPOptimizer /
      ParallelExecutor；
逐项断言两者结果一致（NULL、大整数 >2^31、负数、浮点、中文/空串、
空表、单行、重复值、COUNT(*)/COUNT(col)/SUM/AVG/MIN/MAX/GROUP BY、
= / < / IS NULL / AND 过滤）。

微基准用例（`test_microbenchmark_report`）只**打印**耗时与比值，不做
门禁断言（任务要求：报告即可）。

运行：``python -m pytest tests/test_olap_correctness.py -q``
"""
import os
import statistics
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lightdb.columnar.chunk import ColumnChunk  # noqa: E402
from lightdb.columnar.column_store import ColumnStore  # noqa: E402
from lightdb.columnar.encoders import (  # noqa: E402
    BitPackEncoder, DeltaEncoder, DictEncoder, EncoderRegistry,
    PlainEncoder, RLEEncoder)
from lightdb.olap.aggregator import Aggregator, VectorizedAggregator  # noqa: E402
from lightdb.olap.olap_optimizer import OLAPOptimizer  # noqa: E402
from lightdb.olap.scanner import ColumnScanner, PredicateScanner  # noqa: E402
from lightdb.olap.vectorized_engine import (  # noqa: E402
    VectorizedAggregate, VectorizedFilter, VectorizedGroupBy,
    VectorizedScan, VectorizedSort)


# ==================== 数据集（对拍矩阵的输入）====================

#: 列存列定义（`ColumnStore` 只读 cols 的 name/type）
COLS = [
    {'name': 'id', 'type': 'INT'},
    {'name': 'grp', 'type': 'VARCHAR'},
    {'name': 'ival', 'type': 'BIGINT'},
    {'name': 'fval', 'type': 'FLOAT'},
    {'name': 'sval', 'type': 'VARCHAR'},
]

#: 行存列定义（`Table` 需要 primary 标记）
TABLE_COLS = [
    {'name': 'id', 'type': 'INT', 'primary': True},
    {'name': 'grp', 'type': 'VARCHAR'},
    {'name': 'ival', 'type': 'BIGINT'},
    {'name': 'fval', 'type': 'FLOAT'},
    {'name': 'sval', 'type': 'VARCHAR'},
]

#: 对拍数据：NULL / 大整数(>2^31、>2^40) / 负数 / 浮点 / 中文 / 空串 /
#: 全 NULL 段 / 重复值 都覆盖到
ROWS = [
    [1, 'a', 1, 1.5, 'x'],
    [2, 'a', None, None, ''],
    [3, 'b', 2147483648, 2.25, '中文'],       # > INT32 上界
    [4, 'b', -5, -1.5, 'x'],
    [5, 'c', 0, 0.0, None],
    [6, 'c', 10, 3.5, '中文'],
    [7, 'c', 10, 3.5, 'x'],                   # 重复
    [8, 'a', 2 ** 40 + 1, None, ''],
    [9, 'b', None, 4.75, 'y'],
    [10, 'c', -2147483649, -0.5, None],       # 小于 INT32 下界
]

COL_IDX = {c['name']: i for i, c in enumerate(COLS)}


def _ref_rows():
    return [list(r) for r in ROWS]


def _ref_col(name):
    i = COL_IDX[name]
    return [r[i] for r in ROWS]


def _ref_agg(func, col):
    """行存语义参考（ROWS 数据集）：COUNT(*) 计行，其余忽略 NULL"""
    return _ref_agg_rows(ROWS, func, col)


def _ref_agg_rows(rows, func, col):
    """行存语义参考：COUNT(*) 计行，COUNT(col) 计非 NULL，其余忽略 NULL"""
    func = func.upper()
    if func == 'COUNT' and col == '*':
        return len(rows)
    i = COL_IDX[col]
    vals = [r[i] for r in rows if r[i] is not None]
    if func == 'COUNT':
        return len(vals)
    if not vals:
        return None
    if func == 'SUM':
        return sum(vals)
    if func == 'AVG':
        return sum(vals) / len(vals)
    if func == 'MIN':
        return min(vals)
    if func == 'MAX':
        return max(vals)
    raise AssertionError(f'未知聚合 {func}')


def _ref_match(row, where):
    if 'and' in where:
        return all(_ref_match(row, c) for c in where['and'])
    idx = COL_IDX[where['column']]
    v = row[idx]
    op = str(where.get('op', '=')).upper()
    val = where.get('value')
    if op == 'IS NULL':
        return v is None
    if op == 'IS NOT NULL':
        return v is not None
    if v is None or val is None:
        return False
    if op == '=':
        return v == val
    if op in ('!=', '<>'):
        return v != val
    if op == '>':
        return v > val
    if op == '<':
        return v < val
    if op == '>=':
        return v >= val
    if op == '<=':
        return v <= val
    raise AssertionError(f'未知比较符 {op}')


def _ref_filter(where):
    return [list(r) for r in ROWS if _ref_match(r, where)]


# ==================== fixtures ====================

@pytest.fixture(scope='module')
def row_table(tmp_path_factory):
    """行存参考表：真实存储层（BTree + JSON 行）写同一批数据"""
    from lightdb.db.table import Table
    data_dir = tmp_path_factory.mktemp('row_ref')
    tbl = Table('ref', TABLE_COLS, str(data_dir))
    try:
        for r in ROWS:
            tbl.insert(list(r))
        yield tbl
    finally:
        tbl.close()


@pytest.fixture()
def store(tmp_path):
    """列存：同一批数据按行写入"""
    st = ColumnStore('t', COLS, str(tmp_path / 'cs'))
    st.append_rows([list(r) for r in ROWS])
    return st


@pytest.fixture()
def scan(store):
    return VectorizedScan(store, [c['name'] for c in COLS], batch_size=3)


def _collect_batches(node, name=''):
    out = []
    while True:
        b = node.next_batch()
        if b is None:
            break
        out.append(b)
    return out


def _batch_rows(batch):
    """批次 → Python 行列表（NaN/掩码还原成 None）"""
    names = batch.column_names()
    rows = []
    for i in range(batch.size):
        row = []
        for n in names:
            nulls = batch.null_mask(n)
            v = batch.data[n][i]
            if nulls is not None and nulls[i]:
                v = None
            elif isinstance(v, float) and v != v:      # NaN
                v = None
            row.append(v)
        rows.append(row)
    return names, rows


# ==================== 1. 列存 ↔ 行存 ====================

def test_column_store_scan_matches_row_store(store, row_table):
    cols, rows = row_table.select(['*'])
    assert cols == [c['name'] for c in COLS]
    assert rows == _ref_rows()

    data = store.scan()
    got = [[data[c][i] for c in cols] for i in range(len(data[cols[0]]))]
    assert got == _ref_rows()


def test_scan_range_matches_slice(store):
    data = store.scan_range(['id', 'ival'], 3, 8)
    assert data['id'] == [r[0] for r in ROWS[3:8]]
    assert data['ival'] == [r[2] for r in ROWS[3:8]]


def test_column_store_persistence_roundtrip(tmp_path):
    d = str(tmp_path / 'cs_persist')
    st = ColumnStore('p', COLS, d)
    st.append_rows([list(r) for r in ROWS])
    before = st.scan()
    before_stats = {n: st.get_column_stats(n) for n in COL_IDX}
    st.flush()

    st2 = ColumnStore('p', COLS, d)
    after = st2.scan()
    assert after == before, 'flush 后重载的列数据必须与内存态逐值一致'
    for n in COL_IDX:
        s = st2.get_column_stats(n)
        assert s['min'] == before_stats[n]['min']
        assert s['max'] == before_stats[n]['max']
        assert s['null_count'] == before_stats[n]['null_count']
        assert s['total_count'] == before_stats[n]['total_count']
        assert s['sum'] == before_stats[n]['sum']
        if before_stats[n]['avg'] is None:
            assert s['avg'] is None
        else:
            assert s['avg'] == pytest.approx(before_stats[n]['avg'])


def test_column_store_stats_match_reference(store):
    for name in ('ival', 'fval'):
        vals = [v for v in _ref_col(name) if v is not None]
        s = store.get_column_stats(name)
        assert s['min'] == min(vals)
        assert s['max'] == max(vals)
        assert s['null_count'] == len(_ref_col(name)) - len(vals)
        assert s['total_count'] == len(ROWS)
        assert s['sum'] == pytest.approx(float(sum(vals)))
        assert s['avg'] == pytest.approx(sum(vals) / len(vals))


# ==================== 2. 编码器往返矩阵 ====================

ENCODER_CASES = [
    (PlainEncoder(), 'INT',
     [5, 3, None, 3, -7, 0, 2147483648, -2147483649, 12]),
    (PlainEncoder(), 'BIGINT', [1, None, 2 ** 40, -2 ** 40, 0, 7]),
    (PlainEncoder(), 'VARCHAR', ['abc', '', '中文', None, 'abc', 'x' * 3]),
    (PlainEncoder(), 'FLOAT', [1.5, None, -0.25, 3.0, 0.0, 2.5]),
    (PlainEncoder(), 'BOOLEAN', [True, False, None, True]),
    (DictEncoder(), 'INT',
     [5, 3, None, 3, -7, 0, 2147483648, -2147483649, 12]),
    (DictEncoder(), 'BIGINT', [1, None, 2 ** 40, -2 ** 40, 0, 7]),
    (DictEncoder(), 'VARCHAR', ['abc', '', '中文', None, 'abc', 'x' * 3]),
    (DictEncoder(), 'FLOAT', [1.5, None, -0.25, 3.0, 0.0, 2.5]),
    (DictEncoder(), 'BOOLEAN', [True, False, None, True]),
    (RLEEncoder(), 'INT', [100] * 5 + [None] * 3 + [100, -7, 100]),
    (RLEEncoder(), 'BIGINT', [2 ** 40] * 4 + [None, -2 ** 40, 0]),
    (RLEEncoder(), 'VARCHAR', ['中文'] * 3 + ['', None, '中文', 'x']),
    (RLEEncoder(), 'FLOAT', [1.5, 1.5, None, -0.25, 0.0]),
    (RLEEncoder(), 'BOOLEAN', [True, True, None, False]),
    (DeltaEncoder(), 'INT', list(range(100, 120)) + [None, -20, -10, -3]),
    (DeltaEncoder(), 'BIGINT', [2 ** 40, 2 ** 40 + 1, None, 0, -2 ** 40]),
    (BitPackEncoder(), 'INT', [0, 1, 5, None, 65535, 2147483648]),
    (BitPackEncoder(), 'BIGINT', [2 ** 40, None, 2 ** 40 + 1, -3]),
]


@pytest.mark.parametrize(
    'encoder,col_type,values', ENCODER_CASES,
    ids=[f'{type(e).__name__}-{t}' for e, t, _ in ENCODER_CASES])
def test_encoder_roundtrip_matrix(encoder, col_type, values):
    data = encoder.encode(values, col_type)
    decoded = encoder.decode(data, col_type)
    assert decoded == values, (
        f'{type(encoder).__name__} 对 {col_type} 往返不一致: '
        f'{decoded!r} != {values!r}')


def test_encoder_registry_select_roundtrip():
    datasets = [
        ('INT', [i for i in range(1000)]),
        ('INT', [100] * 500),
        ('INT', [i % 100 for i in range(1000)]),
        ('INT', [3, None, 3, -1, 0, 9]),
        ('BIGINT', [2 ** 40 + i for i in range(100)]),
        ('VARCHAR', ['A', 'B', 'A', 'C'] * 50),
        ('VARCHAR', ['中文', '', '中文', None]),
        ('FLOAT', [0.5, 1.5, None, -2.5]),
    ]
    for col_type, values in datasets:
        enc = EncoderRegistry.select(col_type, values)
        decoded = enc.decode(enc.encode(values, col_type), col_type)
        assert decoded == values, (
            f'自动选择的 {type(enc).__name__} 对 {col_type} 往返不一致')


def test_column_chunk_roundtrip_and_stats():
    values = [r[2] for r in ROWS]          # 含 NULL / 大整数 / 负数
    chunk = ColumnChunk('ival', 'BIGINT', values)
    raw = chunk.encode()
    chunk2 = ColumnChunk('ival', 'BIGINT')
    assert chunk2.decode(raw) == values
    vals = [v for v in values if v is not None]
    assert chunk2.stats.min_value == min(vals)
    assert chunk2.stats.max_value == max(vals)
    assert chunk2.stats.null_count == len(values) - len(vals)


# ==================== 3. 向量化扫描 / 过滤 ====================

def test_vectorized_scan_matches_reference(scan):
    batches = _collect_batches(scan)
    assert sum(b.size for b in batches) == len(ROWS)
    names = batches[0].column_names()
    rows = []
    for b in batches:
        n, rs = _batch_rows(b)
        assert n == names
        rows.extend(rs)
    assert [rs[0] for rs in rows] == [r[0] for r in ROWS]
    assert [rs[2] for rs in rows] == _ref_col('ival')


FILTER_CASES = [
    ({'column': 'ival', 'op': '>', 'value': 0}, '单列 >'),
    ({'column': 'ival', 'op': '<', 'value': 0}, '单列 <'),
    ({'column': 'ival', 'op': '=', 'value': 10}, '单列 ='),
    ({'column': 'fval', 'op': '>=', 'value': 0.0}, '浮点 >='),
    ({'column': 'ival', 'op': 'IS NULL', 'value': None}, 'IS NULL'),
    ({'column': 'ival', 'op': 'IS NOT NULL', 'value': None}, 'IS NOT NULL'),
    ({'column': 'sval', 'op': '=', 'value': '中文'}, '字符串 = 中文'),
    ({'column': 'sval', 'op': '=', 'value': ''}, '字符串 = 空串'),
]


@pytest.mark.parametrize('where,label', FILTER_CASES,
                         ids=[x[1] for x in FILTER_CASES])
def test_vectorized_filter_matches_reference(store, where, label):
    scan = VectorizedScan(store, [c['name'] for c in COLS], batch_size=3)
    node = VectorizedFilter(scan, where['column'], where['op'],
                            where.get('value'))
    batches = _collect_batches(node)
    got = []
    for b in batches:
        _, rs = _batch_rows(b)
        got.extend(rs)
    exp = _ref_filter(where)
    assert got == exp, f'{label}: {got!r} != {exp!r}'


def test_vectorized_filter_and_chain(store):
    scan = VectorizedScan(store, ['id', 'grp', 'ival'], batch_size=4)
    node = VectorizedFilter(scan, 'ival', '>', -10)
    node = VectorizedFilter(node, 'grp', '=', 'c')
    got = []
    for b in _collect_batches(node):
        _, rs = _batch_rows(b)
        got.extend(rs)
    exp = [r[0:3] for r in _ref_rows()
           if r[1] == 'c' and r[2] is not None and r[2] > -10]
    assert got == exp


def test_vectorized_filter_never_treats_null_as_true(store):
    # `NULL != 5` 在 SQL 里是 UNKNOWN（不入选），NaN 占位不能让它变 True
    scan = VectorizedScan(store, ['id', 'ival'])
    node = VectorizedFilter(scan, 'ival', '!=', 5)
    got = []
    for b in _collect_batches(node):
        _, rs = _batch_rows(b)
        got.extend(rs)
    exp = [[r[0], r[2]] for r in _ref_rows()
           if r[2] is not None and r[2] != 5]
    assert got == exp


def test_vectorized_filter_missing_column_is_explicit(store):
    scan = VectorizedScan(store, ['id'])
    with pytest.raises(KeyError):
        VectorizedFilter(scan, 'nope', '=', 1).next_batch()


def test_vectorized_scan_missing_column_is_explicit(store):
    with pytest.raises(KeyError):
        VectorizedScan(store, ['nope'])


# ==================== 4. 向量化聚合 ====================

AGG_CASES = [
    ('COUNT', '*'), ('COUNT', 'ival'), ('COUNT', 'fval'),
    ('SUM', 'ival'), ('SUM', 'fval'),
    ('AVG', 'ival'), ('AVG', 'fval'),
    ('MIN', 'ival'), ('MIN', 'fval'),
    ('MAX', 'ival'), ('MAX', 'fval'),
    ('MIN', 'sval'), ('MAX', 'sval'),
]


@pytest.mark.parametrize('func,col', AGG_CASES,
                         ids=[f'{f}-{c}' for f, c in AGG_CASES])
def test_vectorized_aggregate_matches_row_store(store, row_table, func, col):
    scan = VectorizedScan(store, [c['name'] for c in COLS], batch_size=3)
    got = VectorizedAggregate(scan, col, func).execute()
    exp = _ref_agg(func, col)
    assert got == exp or (isinstance(got, float) and isinstance(exp, float)
                          and got == pytest.approx(exp)), (
        f'{func}({col}): {got!r} != {exp!r}')

    # 与真实行存 `Table._aggregate` 对拍（独立于 Python 参考）
    expr = f'{func}({col})'
    _cols, rows = row_table.select([expr])
    assert rows[0][0] == got or (isinstance(got, float)
                                 and pytest.approx(got) == rows[0][0])


def test_vectorized_aggregate_int_overflow(tmp_path):
    """10 万行 0..99999 的 SUM 必须精确（int64/Python int，不是 int32/float）"""
    d = str(tmp_path / 'bigsum')
    st = ColumnStore('big', [{'name': 'v', 'type': 'INT'}], d)
    st.append_rows([[i] for i in range(100000)])
    got = VectorizedAggregate(
        VectorizedScan(st, ['v']), 'v', 'SUM').execute()
    assert got == sum(range(100000)) == 4999950000
    got_avg = VectorizedAggregate(
        VectorizedScan(st, ['v']), 'v', 'AVG').execute()
    assert got_avg == pytest.approx(49999.5)


def test_vectorized_aggregate_null_and_empty(tmp_path):
    # 全 NULL 列
    st = ColumnStore('n', [{'name': 'g', 'type': 'VARCHAR'},
                           {'name': 'v', 'type': 'BIGINT'}],
                     str(tmp_path / 'allnull'))
    st.append_rows([['a', None], ['b', None], ['c', None]])
    assert VectorizedAggregate(VectorizedScan(st, ['v']), 'v', 'SUM').execute() is None
    assert VectorizedAggregate(VectorizedScan(st, ['v']), 'v', 'AVG').execute() is None
    assert VectorizedAggregate(VectorizedScan(st, ['v']), 'v', 'MIN').execute() is None
    assert VectorizedAggregate(VectorizedScan(st, ['v']), 'v', 'COUNT').execute() == 0
    assert VectorizedAggregate(VectorizedScan(st, ['v']), '*', 'COUNT').execute() == 3

    # 空表
    empty = ColumnStore('e', [{'name': 'v', 'type': 'INT'}],
                        str(tmp_path / 'empty'))
    for func in ('SUM', 'AVG', 'MIN', 'MAX'):
        assert VectorizedAggregate(
            VectorizedScan(empty, ['v']), 'v', func).execute() is None
    assert VectorizedAggregate(
        VectorizedScan(empty, ['v']), '*', 'COUNT').execute() == 0


def test_vectorized_aggregate_unsupported_is_explicit(store):
    with pytest.raises(ValueError):
        VectorizedAggregate(VectorizedScan(store, ['ival']), 'ival', 'STDDEV')
    with pytest.raises(ValueError):
        # SUM(*) / SUM(None) 不是合法聚合形态
        VectorizedAggregate(VectorizedScan(store, ['ival']), '*', 'SUM')


def test_aggregator_module_semantics():
    assert Aggregator.aggregate([1, None, 3], 'COUNT') == 2
    assert Aggregator.aggregate([1, None, 3], 'COUNT_STAR') == 3
    assert Aggregator.aggregate([1, None, 3], 'SUM') == 4
    assert Aggregator.aggregate([None, None], 'SUM') is None
    with pytest.raises(ValueError):
        Aggregator.aggregate([1], 'NOPE')

    va = VectorizedAggregator()
    va.add_batch([2 ** 40, 2 ** 40], 'SUM')
    assert va.finalize('SUM') == 2 ** 41          # 不丢精度、不 float 化
    assert va.finalize('COUNT') == 2
    assert va.finalize('COUNT_STAR') == 2
    with pytest.raises(ValueError):
        va.finalize('NOPE')


# ==================== 5. 向量化分组 ====================

def test_vectorized_groupby_matches_reference(store):
    aggs = [
        {'func': 'SUM', 'column': 'ival', 'alias': 's'},
        {'func': 'COUNT', 'column': '*', 'alias': 'c'},
        {'func': 'COUNT', 'column': 'ival', 'alias': 'cv'},
        {'func': 'AVG', 'column': 'ival', 'alias': 'a'},
        {'func': 'MIN', 'column': 'ival', 'alias': 'mn'},
        {'func': 'MAX', 'column': 'ival', 'alias': 'mx'},
    ]
    gb = VectorizedGroupBy(
        VectorizedScan(store, ['grp', 'ival'], batch_size=3), ['grp'], aggs)
    got = {row[0]: row[1:] for row in gb.execute()}

    groups = {}
    for r in _ref_rows():
        groups.setdefault(r[1], []).append(r)
    assert set(got) == set(groups)
    for g, rs in groups.items():
        vals = [r[2] for r in rs if r[2] is not None]
        exp = [
            sum(vals) if vals else None,
            len(rs),
            len(vals),
            (sum(vals) / len(vals)) if vals else None,
            min(vals) if vals else None,
            max(vals) if vals else None,
        ]
        assert got[g] == exp or (
            isinstance(exp[3], float) and got[g][3] == pytest.approx(exp[3])), \
            f'分组 {g}: {got[g]!r} != {exp!r}'


def test_vectorized_groupby_null_group_key(store):
    """GROUP BY 的 NULL 键要并成一组（不是丢掉或每行一组）"""
    gb = VectorizedGroupBy(VectorizedScan(store, ['sval', 'ival']),
                           ['sval'], [
                               {'func': 'COUNT', 'column': '*', 'alias': 'c'},
                               {'func': 'SUM', 'column': 'ival', 'alias': 's'},
                           ])
    got = {row[0]: row[1:] for row in gb.execute()}
    exp = {}
    for r in _ref_rows():
        exp.setdefault(r[4], []).append(r)
    assert set(got) == set(exp)
    for k, rs in exp.items():
        vals = [r[2] for r in rs if r[2] is not None]
        assert got[k] == [len(rs), sum(vals) if vals else None], (
            f'NULL/重复分组键 {k!r}: {got[k]!r}')


def test_vectorized_groupby_empty_and_missing(tmp_path):
    empty = ColumnStore('e', [{'name': 'g', 'type': 'VARCHAR'},
                              {'name': 'v', 'type': 'INT'}],
                        str(tmp_path / 'empty_g'))
    gb = VectorizedGroupBy(VectorizedScan(empty, ['g', 'v']), ['g'],
                           [{'func': 'COUNT', 'column': '*', 'alias': 'c'}])
    assert gb.execute() == []
    with pytest.raises(ValueError):
        VectorizedGroupBy(VectorizedScan(empty, ['g', 'v']), ['g'],
                          [{'func': 'STDDEV', 'column': 'v', 'alias': 'x'}])


# ==================== 6. 排序 ====================

def test_vectorized_sort_matches_reference(store):
    node = VectorizedSort(
        VectorizedScan(store, ['id', 'ival'], batch_size=3), 'ival', False)
    batch = node.execute()
    rows = []
    for i in range(batch.size):
        nulls = batch.null_mask('ival')
        v = batch.data['ival'][i]
        if (nulls is not None and nulls[i]) or (
                isinstance(v, float) and v != v):
            v = None
        rows.append([batch.data['id'][i], v])
    vals = _ref_col('ival')
    ids = _ref_col('id')
    # 契约：ASC 时 NULL 在前（MySQL 口径）
    exp = [[ids[i], None] for i in range(len(ids)) if vals[i] is None]
    exp += [[ids[i], vals[i]] for i in sorted(
        (i for i in range(len(ids)) if vals[i] is not None),
        key=lambda i: vals[i])]
    assert rows == exp

    node = VectorizedSort(
        VectorizedScan(store, ['id', 'ival'], batch_size=3), 'ival', True)
    batch = node.execute()
    got = []
    for i in range(batch.size):
        nulls = batch.null_mask('ival')
        v = batch.data['ival'][i]
        if (nulls is not None and nulls[i]) or (
                isinstance(v, float) and v != v):
            v = None
        got.append([batch.data['id'][i], v])
    exp_desc = [[ids[i], vals[i]] for i in sorted(
        (i for i in range(len(ids)) if vals[i] is not None),
        key=lambda i: vals[i], reverse=True)]
    exp_desc += [[ids[i], None] for i in range(len(ids)) if vals[i] is None]
    assert got == exp_desc


# ==================== 7. OLAPOptimizer 计划 ====================

def test_optimizer_filter_plan_matches_reference(store):
    opt = OLAPOptimizer(store)
    plan = opt.optimize({
        'columns': ['id', 'ival'],
        'where': {'column': 'ival', 'op': '>', 'value': 0},
    })
    got = plan.execute()
    exp = [[r[0], r[2]] for r in _ref_rows()
           if r[2] is not None and r[2] > 0]
    assert got == exp


def test_optimizer_groupby_plan_matches_reference(store):
    opt = OLAPOptimizer(store)
    plan = opt.optimize({
        'columns': ['grp'],
        'group_by': ['grp'],
        'aggregations': [
            {'func': 'SUM', 'column': 'ival', 'alias': 's'},
            {'func': 'COUNT', 'column': '*', 'alias': 'c'},
            {'func': 'AVG', 'column': 'ival', 'alias': 'a'},
        ],
        'order_by': ('s', True),
    })
    rows = plan.execute()
    exp = []
    for g in sorted({r[1] for r in _ref_rows()}):
        rs = [r for r in _ref_rows() if r[1] == g]
        vals = [r[2] for r in rs if r[2] is not None]
        exp.append([g, sum(vals) if vals else None, len(rs),
                    (sum(vals) / len(vals)) if vals else None])
    # order_by s DESC，NULL 在最后
    exp_sorted = sorted([r for r in exp if r[1] is not None],
                        key=lambda r: r[1], reverse=True)
    exp_sorted += [r for r in exp if r[1] is None]
    assert [[r[0], r[2]] for r in rows] == [[r[0], r[2]] for r in exp_sorted]
    assert [r[1] for r in rows] == [r[1] for r in exp_sorted]


def test_optimizer_streaming_sort_plan(store):
    opt = OLAPOptimizer(store)
    plan = opt.optimize({'columns': ['id'], 'order_by': ('ival', False)})
    rows = plan.execute()
    vals = _ref_col('ival')
    ids = _ref_col('id')
    exp = [ids[i] for i in range(len(ids)) if vals[i] is None]
    exp += [ids[i] for i in sorted(
        (i for i in range(len(ids)) if vals[i] is not None),
        key=lambda i: vals[i])]
    assert [r[0] for r in rows] == exp


def test_optimizer_or_predicate_is_explicit(store):
    opt = OLAPOptimizer(store)
    with pytest.raises(NotImplementedError):
        opt.optimize({'columns': ['id'],
                      'where': {'or': [
                          {'column': 'id', 'op': '=', 'value': 1},
                          {'column': 'id', 'op': '=', 'value': 2}]}})


def test_optimizer_can_skip_scan(store):
    opt = OLAPOptimizer(store)
    assert opt.can_skip_scan({'column': 'ival', 'op': '>', 'value': 10 ** 18})
    assert not opt.can_skip_scan({'column': 'ival', 'op': '>', 'value': 0})
    assert opt.can_skip_scan({'column': 'ival', 'op': '<',
                              'value': -10 ** 18})
    assert not opt.can_skip_scan({'column': 'ival', 'op': '<', 'value': 0})


# ==================== 8. PredicateScanner ====================

@pytest.mark.parametrize('where,label', FILTER_CASES,
                         ids=[x[1] for x in FILTER_CASES])
def test_predicate_scanner_matches_reference(store, where, label):
    ps = PredicateScanner(ColumnScanner(store), [where])
    data = ps.scan()
    n = len(data['id'])
    got = [[data[c][i] for c in [x['name'] for x in COLS]]
           for i in range(n)]
    assert got == _ref_filter(where), f'{label} 不一致'


def test_predicate_scanner_and_chain(store):
    ps = PredicateScanner(ColumnScanner(store), [
        {'column': 'ival', 'op': '>', 'value': -10},
        {'column': 'grp', 'op': '=', 'value': 'c'},
    ])
    data = ps.scan()
    got = [[data['id'][i], data['grp'][i], data['ival'][i]]
           for i in range(len(data['id']))]
    exp = [[r[0], r[1], r[2]] for r in _ref_rows()
           if r[1] == 'c' and r[2] is not None and r[2] > -10]
    assert got == exp


def test_predicate_scanner_unknown_column_is_explicit(store):
    ps = PredicateScanner(ColumnScanner(store),
                          [{'column': 'nope', 'op': '=', 'value': 1}])
    with pytest.raises(KeyError):
        ps.scan()


# ==================== 9. 并行执行器 ====================

BIG_ROWS = [list(r) for r in ROWS] * 20       # 200 行，跨多个分片


@pytest.fixture()
def big_store(tmp_path):
    st = ColumnStore('big', COLS, str(tmp_path / 'big'))
    st.append_rows(BIG_ROWS)
    return st


@pytest.mark.parametrize('func,col', [
    ('COUNT', '*'), ('COUNT', 'ival'), ('COUNT', 'fval'),
    ('SUM', 'ival'), ('SUM', 'fval'), ('AVG', 'ival'), ('AVG', 'fval'),
    ('MIN', 'ival'), ('MIN', 'fval'), ('MAX', 'ival'), ('MAX', 'fval'),
])
def test_parallel_aggregate_matches_serial_and_row_store(big_store, func, col):
    from lightdb.parallel.executor import ParallelExecutor
    ex = ParallelExecutor(max_workers=4)
    try:
        par = ex.parallel_aggregate(big_store, col, func, workers=4)
        ser = ex.parallel_aggregate(big_store, col, func, workers=1)
        ref = _ref_agg_rows(BIG_ROWS, func, col)
        assert par == ser, f'{func}({col}) 并行 {par!r} != 串行 {ser!r}'
        if isinstance(ref, float):
            assert par == pytest.approx(ref)
        else:
            assert par == ref
    finally:
        ex.shutdown()


def test_parallel_aggregate_with_where(big_store):
    from lightdb.parallel.executor import ParallelExecutor
    ex = ParallelExecutor(max_workers=4)
    try:
        where = {'column': 'ival', 'op': '>', 'value': 0}
        par = ex.parallel_aggregate(big_store, 'ival', 'SUM',
                                    where=where, workers=4)
        ser = ex.parallel_aggregate(big_store, 'ival', 'SUM',
                                    where=where, workers=1)
        exp = sum(r[2] for r in BIG_ROWS
                  if r[2] is not None and r[2] > 0)
        assert par == ser == exp
        cnt = ex.parallel_aggregate(big_store, '*', 'COUNT',
                                    where=where, workers=4)
        exp_cnt = sum(1 for r in BIG_ROWS
                      if r[2] is not None and r[2] > 0)
        assert cnt == exp_cnt
    finally:
        ex.shutdown()


def test_parallel_group_by_matches_reference(big_store):
    from lightdb.parallel.executor import ParallelExecutor
    ex = ParallelExecutor(max_workers=4)
    try:
        aggs = [
            {'func': 'SUM', 'column': 'ival', 'alias': 's'},
            {'func': 'COUNT', 'column': '*', 'alias': 'c'},
            {'func': 'AVG', 'column': 'ival', 'alias': 'a'},
            {'func': 'MIN', 'column': 'ival', 'alias': 'mn'},
            {'func': 'MAX', 'column': 'ival', 'alias': 'mx'},
        ]
        par = ex.parallel_group_by(big_store, ['grp'], aggs, workers=4)
        ser = ex.parallel_group_by(big_store, ['grp'], aggs, workers=1)
        assert sorted(par) == sorted(ser), '并行与串行分组结果必须一致'
        # 形状：聚合值是标量（不是单元素列表）
        for row in par:
            assert not any(isinstance(v, list) for v in row[1:])

        exp = {}
        for r in BIG_ROWS:
            exp.setdefault(r[1], []).append(r)
        for row in par:
            g = row[0]
            rs = exp[g]
            vals = [r[2] for r in rs if r[2] is not None]
            assert row[1] == (sum(vals) if vals else None)
            assert row[2] == len(rs)
            if vals:
                assert row[3] == pytest.approx(sum(vals) / len(vals))
            else:
                assert row[3] is None
            assert row[4] == (min(vals) if vals else None)
            assert row[5] == (max(vals) if vals else None)
    finally:
        ex.shutdown()


def test_parallel_scan_with_where_matches_serial(big_store):
    from lightdb.parallel.executor import ParallelExecutor
    ex = ParallelExecutor(max_workers=4)
    try:
        where = {'column': 'grp', 'op': '=', 'value': 'a'}
        par = ex.parallel_scan(big_store, ['id', 'grp', 'ival'],
                               where=where, workers=4)
        ser = ex.parallel_scan(big_store, ['id', 'grp', 'ival'],
                               where=where, workers=1)
        assert par == ser
        exp = [[r[0], r[1], r[2]] for r in BIG_ROWS if r[1] == 'a']
        n = len(par['id'])
        got = [[par['id'][i], par['grp'][i], par['ival'][i]]
               for i in range(n)]
        assert got == exp
    finally:
        ex.shutdown()


def test_parallel_aggregate_unsupported_is_explicit(big_store):
    from lightdb.parallel.executor import ParallelExecutor
    ex = ParallelExecutor(max_workers=2)
    try:
        with pytest.raises(ValueError):
            ex.parallel_aggregate(big_store, 'ival', 'STDDEV', workers=2)
        with pytest.raises(ValueError):
            ex.parallel_aggregate(big_store, '*', 'SUM', workers=2)
        with pytest.raises(KeyError):
            ex.parallel_aggregate(big_store, 'nope', 'SUM', workers=2)
        with pytest.raises(ValueError):
            ex.parallel_group_by(big_store, ['grp'],
                                 [{'func': 'STDDEV', 'column': 'ival',
                                   'alias': 'x'}], workers=2)
    finally:
        ex.shutdown()


# ==================== 10. 接线状态（4A.2 起契约更新） ====================

def test_wiring_status_matches_4a2_contract():
    """4A.2 起：已接线（WIRED=True）但**默认关闭**；性能声明仍待 4A.5"""
    from lightdb.olap import status
    assert status.WIRED_TO_SQL_PATH is True
    assert status.DEFAULT_ENABLED is False
    assert status.COUNTS_FOR_PERFORMANCE_CLAIMS is False
    from lightdb.parallel.executor import ParallelExecutor
    assert ParallelExecutor.WIRED_TO_SQL_PATH is True


# ==================== 11. 微基准（只报告，不设门禁）====================

def _median_time(fn, warmup=1, repeat=3):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def test_microbenchmark_report(tmp_path, capsys):
    """列存扫描/聚合 vs 行存同数据耗时（报告用，不断言比值）"""
    from lightdb.db.table import Table

    n = 2000
    values = [i * 3 - 5000 for i in range(n)]
    group_vals = [f'g{i % 10}' for i in range(n)]

    st = ColumnStore('bench', [{'name': 'v', 'type': 'INT'},
                               {'name': 'g', 'type': 'VARCHAR'}],
                     str(tmp_path / 'bench_cs'))
    st.append_rows([[values[i], group_vals[i]] for i in range(n)])

    tbl = Table('bench_ref', [
        {'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'},
        {'name': 'g', 'type': 'VARCHAR'},
    ], str(tmp_path / 'bench_row'))
    try:
        for i in range(n):
            tbl.insert([i, values[i], group_vals[i]])

        col_scan = _median_time(lambda: st.scan(['v']))
        row_scan = _median_time(lambda: tbl.select(['v']))

        def _col_agg():
            return VectorizedAggregate(
                VectorizedScan(st, ['v']), 'v', 'SUM').execute()

        def _row_agg():
            return tbl.select(['SUM(v)'])

        col_agg = _median_time(_col_agg)
        row_agg = _median_time(_row_agg)

        # 基准里同时验证被测结果正确（避免"测得很快但算错"）
        assert _col_agg() == sum(values)
        assert _row_agg()[1][0][0] == sum(values)

        lines = [
            f'[bench] 同数据 {n} 行',
            f'[bench] 列存 scan                 {col_scan*1000:9.3f} ms',
            f'[bench] 行存 Table.select         {row_scan*1000:9.3f} ms',
            f'[bench] 列存向量化 SUM            {col_agg*1000:9.3f} ms',
            f'[bench] 行存 _aggregate SUM       {row_agg*1000:9.3f} ms',
            f'[bench] 扫描耗时比 row/col        '
            f'{row_scan / max(col_scan, 1e-12):8.2f}x',
            f'[bench] 聚合耗时比 row/col        '
            f'{row_agg / max(col_agg, 1e-12):8.2f}x',
        ]
        with capsys.disabled():
            print('\n'.join(lines))
        for line in lines:
            assert isinstance(line, str)
    finally:
        tbl.close()
