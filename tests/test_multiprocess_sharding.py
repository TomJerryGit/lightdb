"""多进程只读查询分片原型（`lightdb/net/sharding.py`）的测试。

覆盖：
* 分片结果与单进程基线**逐行一致**（含乱序数据、主键排序、列子集）；
* 边界：空表、单行、lo==hi、跨片、workers > 行数、lo>hi（空区间）、
  1..max 边界值；
* 不支持形态显式 1105（JOIN/聚合/无 BETWEEN/非主键/非整数边界/ORDER BY/
  LIMIT/DISTINCT/子查询/复合主键/无主键/不选主键/非 SELECT）；
* worker 崩溃 / 超时显式报错（`ShardWorkerError`），不静默返回部分结果；
* 多进程 vs 进程内线程 vs 单进程三者的行结果一致。

运行：python -m pytest tests/test_multiprocess_sharding.py -q
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.net import sharding as S  # noqa: E402

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]
NROWS = 2000


def _quiet():
    os.environ.setdefault('LIGHTDB_AUDIT_LEVEL', 'NONE')
    os.environ.setdefault('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                                # pragma: no cover
        pass


def _make_db(data_dir, rows=NROWS, cols=COLS):
    from lightdb.db.database import Database
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir))
    tbl = db.create_table('t', cols)
    for i in range(1, rows + 1):
        tbl.insert([i, (i * 37) % 1000])
    db.checkpoint(force=True)
    db.shutdown()
    _quiet()
    return data_dir


@pytest.fixture(scope='module')
def dataset(tmp_path_factory):
    _quiet()
    dd = tmp_path_factory.mktemp('shard_data') / 'db'
    return _make_db(dd, NROWS)


@pytest.fixture(scope='module')
def empty_dataset(tmp_path_factory):
    _quiet()
    dd = tmp_path_factory.mktemp('shard_empty') / 'db'
    return _make_db(dd, 0)


SQL = 'SELECT id, v FROM t WHERE id BETWEEN 100 AND 900'


# ==================== 结果一致性 ====================

def test_sharded_rows_match_single_process(dataset):
    base = S.execute_single(SQL, dataset)
    assert base['row_count'] == 801
    for workers in (1, 2, 4):
        r = S.execute_sharded(SQL, dataset, workers=workers, timeout_s=180)
        assert r['row_count'] == base['row_count'], workers
        assert r['rows'] == base['rows'], workers
        assert r['columns'] == ['id', 'v']
        assert [x[0] for x in r['rows']] == sorted(x[0] for x in r['rows'])
        assert r['consistency']['mode'] == 'snapshot-copy-per-worker'
        assert r['consistency']['writes_allowed'] is False
        assert r['timing']['total_s'] > 0


def test_sharded_select_star_and_subset_columns(dataset):
    for sql in ('SELECT * FROM t WHERE id BETWEEN 1 AND 50',
                'SELECT v, id FROM t WHERE id BETWEEN 1 AND 50'):
        base = S.execute_single(sql, dataset)
        r = S.execute_sharded(sql, dataset, workers=3, timeout_s=180)
        assert r['rows'] == base['rows'], sql
        assert r['columns'] == base['columns'], sql


def test_sharded_and_threads_agree(dataset):
    base = S.execute_single(SQL, dataset)
    r_shard = S.execute_sharded(SQL, dataset, workers=4, timeout_s=180)
    r_thr = S.execute_threads(SQL, dataset, workers=4)
    assert r_shard['rows'] == base['rows']
    assert r_thr['rows'] == base['rows']


# ==================== 边界 ====================

def test_empty_table(empty_dataset):
    r = S.execute_sharded('SELECT id, v FROM t WHERE id BETWEEN 1 AND 100',
                          empty_dataset, workers=4, timeout_s=120)
    assert r['row_count'] == 0
    assert r['rows'] == []
    assert r['shards']  # 空表仍按请求范围切片，只是每片结果为空


def test_single_row_and_lo_eq_hi(dataset):
    r = S.execute_sharded('SELECT id, v FROM t WHERE id BETWEEN 42 AND 42',
                          dataset, workers=4, timeout_s=120)
    assert len(r['shards']) == 1
    assert list(r['rows'][0]) == [42, (42 * 37) % 1000]


def test_cross_shard_and_min_max_boundaries(dataset):
    r = S.execute_sharded('SELECT id, v FROM t WHERE id BETWEEN 1 AND 2000',
                          dataset, workers=4, timeout_s=180)
    assert r['row_count'] == 2000
    ids = [x[0] for x in r['rows']]
    assert ids == list(range(1, 2001))
    for sql in ('SELECT id, v FROM t WHERE id BETWEEN 1 AND 1',
                'SELECT id, v FROM t WHERE id BETWEEN 2000 AND 2000',
                'SELECT id, v FROM t WHERE id BETWEEN 1990 AND 2010'):
        base = S.execute_single(sql, dataset)
        assert S.execute_sharded(sql, dataset, workers=3,
                                 timeout_s=120)['rows'] == base['rows'], sql


def test_workers_more_than_rows(dataset):
    r = S.execute_sharded('SELECT id, v FROM t WHERE id BETWEEN 1 AND 3',
                          dataset, workers=8, timeout_s=120)
    assert r['row_count'] == 3
    assert len(r['shards']) == 3           # 被行数截断，不会切片到空区间
    assert [x[0] for x in r['rows']] == [1, 2, 3]


def test_empty_range_returns_no_workers(dataset):
    r = S.execute_sharded('SELECT id, v FROM t WHERE id BETWEEN 10 AND 5',
                          dataset, workers=4, timeout_s=30)
    assert r['row_count'] == 0 and r['shards'] == []
    assert r['timing']['snapshot_s'] == 0      # 空区间短路，不做快照


# ==================== 不支持形态：显式 1105 ====================

@pytest.mark.parametrize('sql', [
    'SELECT COUNT(*) FROM t WHERE id BETWEEN 1 AND 9',
    'SELECT SUM(v) FROM t WHERE id BETWEEN 1 AND 9',
    'SELECT * FROM t JOIN t2 ON t.id = t2.id WHERE t.id BETWEEN 1 AND 9',
    'SELECT * FROM t',
    'SELECT * FROM t WHERE id > 5',
    'SELECT * FROM t WHERE v BETWEEN 1 AND 9',
    'SELECT * FROM t WHERE id BETWEEN 1.5 AND 9',
    'SELECT * FROM t WHERE id BETWEEN 1 AND 9 ORDER BY id',
    'SELECT * FROM t WHERE id BETWEEN 1 AND 9 LIMIT 5',
    'SELECT DISTINCT id FROM t WHERE id BETWEEN 1 AND 9',
    'SELECT v FROM t WHERE id BETWEEN 1 AND 9',
    'SELECT * FROM (SELECT id FROM t) x WHERE id BETWEEN 1 AND 9',
    'UPDATE t SET v = 1 WHERE id BETWEEN 1 AND 9',
])
def test_unsupported_forms_raise_1105(dataset, sql):
    with pytest.raises(S.UnsupportedShardQuery) as ei:
        S.execute_sharded(sql, dataset, workers=2, timeout_s=60)
    assert ei.value.code == S.MYSQL_UNKNOWN_ERROR == 1105
    assert '不支持的分片查询' in str(ei.value)


def test_missing_table_and_bad_data_dir(dataset, tmp_path):
    with pytest.raises(S.ShardingError):
        S.execute_sharded('SELECT id FROM nope WHERE id BETWEEN 1 AND 9',
                          dataset, workers=2, timeout_s=60)
    with pytest.raises(S.ShardingError):
        S.execute_sharded(SQL, tmp_path / 'not_a_db', workers=2, timeout_s=60)


def test_composite_or_missing_pk_rejected(tmp_path):
    from lightdb.db.database import Database
    _quiet()
    dd = tmp_path / 'composite'
    db = Database(str(dd))
    db.create_table('c', [{'name': 'a', 'type': 'INT', 'primary': True},
                          {'name': 'b', 'type': 'INT', 'primary': True},
                          {'name': 'v', 'type': 'INT'}])
    db.create_table('n', [{'name': 'a', 'type': 'INT'},
                          {'name': 'v', 'type': 'INT'}])
    db.checkpoint(force=True)
    db.shutdown()
    for sql in ('SELECT * FROM c WHERE a BETWEEN 1 AND 9',
                'SELECT * FROM n WHERE a BETWEEN 1 AND 9'):
        with pytest.raises(S.ShardingError) as ei:
            S.execute_sharded(sql, dd, workers=2, timeout_s=60)
        assert ei.value.code == 1105


# ==================== worker 崩溃 / 超时：显式失败 ====================

def test_worker_crash_is_explicit(dataset):
    with pytest.raises(S.ShardWorkerError) as ei:
        S.execute_sharded(SQL, dataset, workers=2, timeout_s=60,
                          _worker_inject={'crash': True})
    assert 'exitcode=7' in str(ei.value)
    assert ei.value.code == 1105


def test_worker_timeout_is_explicit(dataset):
    with pytest.raises(S.ShardWorkerError) as ei:
        S.execute_sharded(SQL, dataset, workers=2, timeout_s=1,
                          _worker_inject={'sleep': 8})
    assert '超时' in str(ei.value)
    assert ei.value.code == 1105


def test_worker_execution_error_is_explicit(dataset):
    with pytest.raises(S.ShardWorkerError) as ei:
        S.execute_sharded(SQL, dataset, workers=1, timeout_s=60,
                          _worker_inject={'error': 'injected worker failure'})
    assert 'injected worker failure' in str(ei.value)


# ==================== task-85：服务端分片引擎（快照复用/worker 池/回退） ====================

def _engine(dataset, tmp_path, workers=2, min_rows=100, timeout_s=120):
    return S.ShardedQueryEngine(
        data_dir=str(dataset), workers=workers, min_rows=min_rows,
        timeout_s=timeout_s, pool_root=str(tmp_path / 'engine_pool'))


def test_engine_snapshot_reuse_and_worker_pool(dataset, tmp_path):
    """快照复用：数据未变时第二个查询不重建快照（原型每查询都建）"""
    engine = _engine(dataset, tmp_path)
    try:
        sql = 'SELECT id, v FROM t WHERE id BETWEEN 100 AND 900'
        base = S.execute_single(sql, dataset)
        r1 = engine.route(sql)
        assert r1 is not None and r1['type'] == 'resultset'
        assert [tuple(x) for x in r1['rows']] == base['rows']
        m1 = engine.metrics_snapshot()
        assert m1['sharded_total'] == 1
        assert m1['snapshot_created_total'] == 1
        r2 = engine.route(sql)
        assert [tuple(x) for x in r2['rows']] == base['rows']
        m2 = engine.metrics_snapshot()
        assert m2['snapshot_created_total'] == 1        # 复用，不重建
        assert m2['snapshot_reused_total'] >= 1
        assert m2['workers'] == 2
        assert m2['workers_created_total'] >= 2
        assert m2['query_latency_ms_last'] > 0
        assert engine._workers and all(w.alive for w in engine._workers)
    finally:
        engine.close()


def test_engine_fallbacks_and_threshold(dataset, tmp_path):
    engine = _engine(dataset, tmp_path, min_rows=100)
    try:
        assert engine.route('SELECT COUNT(*) FROM t '
                            'WHERE id BETWEEN 1 AND 9') is None
        assert engine.route('SELECT * FROM t WHERE id BETWEEN 1 AND 9') is None
        assert engine.route('SELECT id FROM t WHERE id BETWEEN 1 AND 9',
                            session={'txn': 1}) is None
        m = engine.metrics_snapshot()
        assert m['fallbacks_by_reason']['unsupported'] >= 1
        assert m['fallbacks_by_reason']['below_threshold'] >= 1
        assert m['fallbacks_by_reason']['session_transaction'] >= 1
        assert m['sharded_total'] == 0
    finally:
        engine.close()


def test_engine_crash_and_timeout_explicit(dataset, tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_SHARDING_TEST_WORKER_CRASH', '1')
    engine = _engine(dataset, tmp_path / 'crash', workers=1,
                     min_rows=10)
    try:
        with pytest.raises(S.ShardWorkerError) as ei:
            engine.route('SELECT id, v FROM t WHERE id BETWEEN 1 AND 100')
        assert ei.value.code == 1105
        assert 'worker' in str(ei.value).lower()
    finally:
        engine.close()

    monkeypatch.delenv('LIGHTDB_SHARDING_TEST_WORKER_CRASH', raising=False)
    monkeypatch.setenv('LIGHTDB_SHARDING_TEST_WORKER_SLEEP', '5')
    engine2 = _engine(dataset, tmp_path / 'timeout', workers=1,
                      min_rows=10, timeout_s=0.8)
    try:
        with pytest.raises(S.ShardWorkerError) as ei:
            engine2.route('SELECT id, v FROM t WHERE id BETWEEN 1 AND 100')
        assert ei.value.code == 1105
        assert '超时' in str(ei.value)
        assert engine2.metrics_snapshot()['worker_errors_total'] >= 1
    finally:
        engine2.close()


def test_engine_refresh_after_out_of_band_write(tmp_path, monkeypatch):
    """带外写入（不经过服务端）：文件指纹变化 → 下次查询刷新快照"""
    monkeypatch.delenv('LIGHTDB_SHARDING_TEST_WORKER_SLEEP', raising=False)
    from lightdb.db.database import Database
    _quiet()
    dd = tmp_path / 'oob'
    db = Database(str(dd))
    tbl = db.create_table('t', COLS)
    for i in range(1, 401):
        tbl.insert([i, i])
    db.checkpoint(force=True)
    db.shutdown()
    engine = _engine(dd, tmp_path, workers=1, min_rows=50)
    try:
        r1 = engine.route('SELECT id, v FROM t WHERE id BETWEEN 1 AND 400')
        assert len(r1['rows']) == 400
        created = engine.metrics_snapshot()['snapshot_created_total']
        db2 = Database(str(dd))
        db2.get_table('t').insert([999, 1])
        db2.checkpoint(force=True)
        db2.shutdown()
        r2 = engine.route('SELECT id, v FROM t WHERE id BETWEEN 1 AND 999')
        assert (999, 1) in [tuple(x) for x in r2['rows']]
        assert engine.metrics_snapshot()['snapshot_created_total'] > created
    finally:
        engine.close()
