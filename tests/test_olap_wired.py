# -*- coding: utf-8 -*-
"""OLAP 显式接线（4A.2）端到端对拍 + 4A.3 并行分片/微基准

契约（见 `lightdb/olap/status.py` 与 `lightdb/olap/sql_dispatch.py`）：

* `WIRED_TO_SQL_PATH=True`：SQL 执行器里**真的有**分派钩子；
* `DEFAULT_ENABLED=False`：默认走行存；只有 `config.olap_dispatch=True`
  （或 env `LIGHTDB_OLAP_DISPATCH=1`）才会走向量化；
* 分派只走白名单语法，其余返回 None 走行存（禁止静默改道）；
* 开关前后 `columns`/`rows` **逐行一致**；每类断言同时校验「确实发生了
  分派」（`dispatch_stats()`），避免「没走也通过」。

本文件同时输出 4A.3 的微基准（1M 行向量化聚合 1→4 线程、dispatch 开/关
A/B、min_rows 阈值影响），**只打印不断言阈值**（阈值由 Lead 在 4A.5 定）。
"""
import os
import statistics
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 数据集：NULL / 全 NULL 段 / >2^31 / >2^40 / 负数 / 浮点 / 中文 / 空串 /
# 重复值 全部覆盖
ROWS = [
    (1, 'a', 1, 1.5, 'x'),
    (2, 'a', None, None, ''),
    (3, 'b', 2147483648, 2.25, '中文'),
    (4, 'b', -5, -1.5, 'x'),
    (5, 'c', 0, 0.0, None),
    (6, 'c', 10, 3.5, '中文'),
    (7, 'c', 10, 3.5, 'x'),
    (8, 'a', 2 ** 40 + 1, None, ''),
    (9, 'b', None, 4.75, 'y'),
    (10, 'c', -2147483649, -0.5, None),
    (11, 'd', 2 ** 40 + 2, None, ''),
    (12, 'd', None, None, None),
]

_DDL = ('CREATE TABLE t (id INT PRIMARY KEY, g VARCHAR(10), '
        'v BIGINT, f DOUBLE, s VARCHAR(20))')


def _lit(v):
    if v is None:
        return 'NULL'
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def _values_sql(rows):
    return ','.join('(' + ','.join(_lit(x) for x in r) + ')' for r in rows)


# ==================== fixtures ====================

@pytest.fixture()
def db_exec(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    ex.execute_sql(_DDL, session)
    ex.execute_sql(f'INSERT INTO t VALUES {_values_sql(ROWS)}', session)
    ex.execute_sql('CREATE TABLE e (id INT PRIMARY KEY, g VARCHAR(10), '
                   'v BIGINT)', session)
    try:
        yield db, ex, session
    finally:
        db.shutdown()


@pytest.fixture()
def dispatch(db_exec):
    """强制开启分派（测试用小表：min_rows=0），返回 stats 访问器"""
    from lightdb.olap import sql_dispatch
    db, ex, session = db_exec
    ex.olap_dispatch = True
    ex.olap_dispatch_min_rows = 0
    ex.olap_crosscheck = False
    sql_dispatch.reset_dispatch_stats()
    return sql_dispatch


# ==================== 对拍矩阵 ====================

SUPPORTED_QUERIES = [
    # 单聚合
    'SELECT COUNT(*) FROM t',
    'SELECT COUNT(v) FROM t',
    'SELECT SUM(v) FROM t',
    'SELECT AVG(v) FROM t',
    'SELECT MIN(v) FROM t',
    'SELECT MAX(v) FROM t',
    'SELECT SUM(f), AVG(f), MIN(f), MAX(f) FROM t',
    # 多聚合
    'SELECT COUNT(*), COUNT(v), SUM(v), AVG(v), MIN(v), MAX(v) FROM t',
    'SELECT COUNT(*), SUM(f) FROM t',
    # WHERE
    'SELECT COUNT(*), SUM(v) FROM t WHERE v > 0',
    "SELECT COUNT(*), SUM(v) FROM t WHERE v >= 0 AND g = 'c'",
    "SELECT COUNT(*), SUM(v) FROM t WHERE g < 'c'",
    'SELECT COUNT(*), SUM(v) FROM t WHERE v IS NULL',
    'SELECT COUNT(*), SUM(v) FROM t WHERE v IS NOT NULL',
    'SELECT COUNT(*), SUM(f) FROM t WHERE f IS NULL',
    'SELECT COUNT(*), SUM(v) FROM t WHERE v > 2147483647',
    "SELECT COUNT(*), SUM(v) FROM t WHERE s = '中文'",
    # 别名
    'SELECT COUNT(*) AS c, SUM(v) AS total FROM t',
    'SELECT COUNT(*) AS c, SUM(v) AS total FROM t WHERE v >= 0',
    # GROUP BY（1-2 列）
    'SELECT g, COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t GROUP BY g',
    'SELECT COUNT(*), COUNT(v), SUM(v) FROM t',
    "SELECT g, COUNT(*) FROM t WHERE v IS NOT NULL GROUP BY g",
    'SELECT g, s, COUNT(*), SUM(v) FROM t GROUP BY g, s',
    'SELECT g AS gg, COUNT(*) AS c, SUM(v) AS s FROM t GROUP BY g',
    # ORDER BY / LIMIT
    'SELECT g, COUNT(*), SUM(v) FROM t GROUP BY g ORDER BY g',
    'SELECT g, COUNT(*), SUM(v) FROM t GROUP BY g ORDER BY g DESC',
    'SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY c DESC',
    'SELECT g, SUM(v) FROM t GROUP BY g ORDER BY SUM(v) DESC',
    'SELECT g, COUNT(*) FROM t GROUP BY g ORDER BY g DESC LIMIT 2',
    'SELECT g, COUNT(*) FROM t GROUP BY g ORDER BY g LIMIT 1 OFFSET 1',
    # 空表 / 全 NULL
    'SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM e',
    'SELECT SUM(v) AS sv FROM e',
    'SELECT COUNT(*) FROM e WHERE v > 0',
    'SELECT g, COUNT(*) FROM e GROUP BY g',
    # 单行 / 大整数 / 浮点 / 中文分组键
    'SELECT COUNT(*), SUM(v) FROM t WHERE id = 1',
    'SELECT s, COUNT(*) FROM t GROUP BY s',
    # 无 GROUP BY 的 LIMIT/OFFSET（行存存储层路径的特殊切片语义）
    'SELECT SUM(v) FROM t LIMIT 0',
    'SELECT SUM(v) FROM t LIMIT 1',
    'SELECT SUM(v) FROM t LIMIT 1 OFFSET 1',
    'SELECT COUNT(*), SUM(v) FROM t LIMIT 1 OFFSET 1',
    'SELECT SUM(v) AS s FROM t LIMIT 0',
]


@pytest.mark.parametrize('sql', SUPPORTED_QUERIES,
                         ids=[f'q{i}' for i in range(len(SUPPORTED_QUERIES))])
def test_dispatch_on_off_parity(db_exec, dispatch, sql):
    """开关前后 columns/rows 逐行一致（含顺序），且确实发生了分派"""
    db, ex, session = db_exec
    ex.olap_dispatch = False
    off = ex.execute_sql(sql, session)
    ex.olap_dispatch = True
    dispatch.reset_dispatch_stats()
    on = ex.execute_sql(sql, session)
    st = dispatch.dispatch_stats()

    assert on.get('type') == off.get('type'), (sql, on, off)
    assert on.get('type') == 'resultset', (sql, on)
    assert list(on.get('columns') or []) == list(off.get('columns') or []), (
        sql, on.get('columns'), off.get('columns'))
    assert [list(r) for r in on.get('rows') or []] == \
        [list(r) for r in off.get('rows') or []], (sql, on.get('rows'),
                                                   off.get('rows'))
    # 每类都断言「确实派发」，避免测试通过但根本没走向量化
    assert st['attempted'] >= 1, sql
    assert st['dispatched'] == 1, (sql, st)
    assert st['fallback'] == 0 and st['fallback_error'] == 0, (sql, st)
    assert st['last']['table'] in ('t', 'e'), st['last']


def test_dispatch_result_follows_row_generator_semantics(db_exec, dispatch):
    """聚合查询在空集上的 SQL 语义（COUNT=0、其余 NULL；空表 0 行）"""
    db, ex, session = db_exec
    ex.olap_dispatch = True
    r = ex.execute_sql('SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) '
                       'FROM e', session)
    assert [list(x) for x in r['rows']] == [[0, None, None, None, None]]
    r = ex.execute_sql('SELECT COUNT(*) AS c, SUM(v) AS s FROM e', session)
    assert list(r['columns']) == ['c', 's']
    assert r['rows'] == []


# ==================== 不支持的语法：必须走行存 ====================

UNSUPPORTED_QUERIES = [
    'SELECT COUNT(*) FROM t x JOIN t y ON x.id = y.id',
    "SELECT g, COUNT(*) FROM t GROUP BY g HAVING COUNT(*) > 1",
    'SELECT COUNT(*) FROM t WHERE v > 0 OR id = 5',
    'SELECT COUNT(*) FROM t WHERE id IN (SELECT id FROM t WHERE v > 0)',
    'SELECT g, COUNT(*), ROW_NUMBER() OVER (ORDER BY g) FROM t GROUP BY g',
    'SELECT SUM(v * 2) FROM t',
    'SELECT COUNT(DISTINCT v) FROM t',
    'SELECT t.g, COUNT(*) FROM t GROUP BY t.g',
    'SELECT * FROM t',
    'SELECT COUNT(*) c FROM t',
    'SELECT SUM(v) FROM t WHERE id BETWEEN 1 AND 5',
    'SELECT COUNT(*) FROM t WHERE id IN (1, 2, 3)',
]


@pytest.mark.parametrize('sql', UNSUPPORTED_QUERIES,
                         ids=[f'u{i}' for i in range(len(UNSUPPORTED_QUERIES))])
def test_unsupported_forms_stay_row_store(db_exec, dispatch, sql):
    """白名单外语法：结果与行存一致，且**没有**发生分派（无静默改道）"""
    db, ex, session = db_exec
    ex.olap_dispatch = False
    off = ex.execute_sql(sql, session)
    ex.olap_dispatch = True
    dispatch.reset_dispatch_stats()
    on = ex.execute_sql(sql, session)
    st = dispatch.dispatch_stats()

    assert on.get('type') == off.get('type'), (sql, on, off)
    if off.get('type') == 'resultset':
        assert list(on.get('columns') or []) == list(off.get('columns') or [])
        assert [list(r) for r in on.get('rows') or []] == \
            [list(r) for r in off.get('rows') or []]
    else:
        assert on.get('code') == off.get('code')
    assert st['dispatched'] == 0, (sql, st)
    assert st['fallback'] >= 1, (sql, st)


# ==================== 可见性 / 回退 / 对拍 / 阈值 ====================

def test_dispatch_is_visible_and_counted(db_exec, dispatch):
    db, ex, session = db_exec
    ex.olap_dispatch = True
    dispatch.reset_dispatch_stats()
    ex.execute_sql('SELECT COUNT(*), SUM(v) FROM t', session)
    st = dispatch.dispatch_stats()
    assert st['dispatched'] == 1
    last = st['last']
    assert last['table'] == 't' and last['mode'] == 'aggregate'
    assert last['aggs'] == 2 and last['rows'] == len(ROWS)
    assert last['elapsed_ms'] >= 0

    dispatch.reset_dispatch_stats()
    ex.execute_sql('SELECT g, COUNT(*) FROM t GROUP BY g', session)
    last = dispatch.dispatch_stats()['last']
    assert last['mode'] == 'group_by'


def test_min_rows_threshold_blocks_small_tables(db_exec, dispatch):
    db, ex, session = db_exec
    ex.olap_dispatch = True
    ex.olap_dispatch_min_rows = 1000          # 默认阈值 > 12 行
    dispatch.reset_dispatch_stats()
    off = ex.execute_sql('SELECT SUM(v) FROM t', session)
    st = dispatch.dispatch_stats()
    assert st['dispatched'] == 0 and st['fallback'] == 1
    ex.olap_dispatch_min_rows = 0
    dispatch.reset_dispatch_stats()
    on = ex.execute_sql('SELECT SUM(v) FROM t', session)
    assert on == off
    assert dispatch.dispatch_stats()['dispatched'] == 1


def test_dispatch_internal_error_falls_back_explicitly(db_exec, dispatch,
                                                       monkeypatch):
    """分派内部异常 → 显式回退行存，结果仍正确（不返回错值）"""
    db, ex, session = db_exec
    ex.olap_dispatch = True
    off = ex.execute_sql('SELECT COUNT(*), SUM(v) FROM t', session)

    def _boom(*a, **k):
        raise ValueError('注入的分派失败')

    monkeypatch.setattr(dispatch, '_execute_plan', _boom)
    dispatch.reset_dispatch_stats()
    on = ex.execute_sql('SELECT COUNT(*), SUM(v) FROM t', session)
    st = dispatch.dispatch_stats()
    assert on == off
    assert st['fallback_error'] == 1 and st['dispatched'] == 0


def test_crosscheck_mode_passes_and_detects_mismatch(db_exec, dispatch,
                                                     monkeypatch):
    db, ex, session = db_exec
    ex.olap_dispatch = True
    ex.olap_crosscheck = True
    dispatch.reset_dispatch_stats()
    r = ex.execute_sql('SELECT g, COUNT(*), SUM(v) FROM t GROUP BY g', session)
    assert r.get('type') == 'resultset'
    assert dispatch.dispatch_stats()['crosscheck_ok'] == 1

    # 人为制造一条不一致结果：crosscheck 必须显式报错（不是静默选一侧）
    real = dispatch._execute_plan

    def _wrong(executor, parsed, session, plan):
        res = real(executor, parsed, session, plan)
        if res.get('type') == 'resultset' and res.get('rows'):
            res = dict(res)
            res['rows'] = [tuple([-999] * len(res['columns']))]
        return res

    monkeypatch.setattr(dispatch, '_execute_plan', _wrong)
    dispatch.reset_dispatch_stats()
    # 不一致时 dispatch 抛 RuntimeError；执行器 `execute()` 的统一异常处理
    # 会把它变成**显式错误结果**（客户端看到 1105 + crosscheck 报文），
    # 两种形态都算「显式报错」，绝不静默选一侧。
    r = ex.execute_sql('SELECT COUNT(*), SUM(v) FROM t', session)
    assert r.get('type') == 'error', r
    assert 'crosscheck' in str(r.get('message', '')).lower(), r
    assert dispatch.dispatch_stats()['crosscheck_failed'] == 1


def test_parallel_workers_same_result_as_serial(db_exec, dispatch):
    """4A.3：多/单线程同一查询结果完全一致（经 dispatch 对拍 + 逐行比较）"""
    db, ex, session = db_exec
    ex.olap_dispatch = True
    ex.olap_crosscheck = False
    queries = [
        'SELECT COUNT(*), COUNT(v), SUM(v), AVG(v), MIN(v), MAX(v) FROM t',
        'SELECT g, COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t GROUP BY g',
        'SELECT g, s, COUNT(*), SUM(v) FROM t GROUP BY g, s',
        'SELECT COUNT(*) FROM t WHERE v > 0',
    ]
    ex.olap_parallel_workers = 1
    serial = [ex.execute_sql(q, session) for q in queries]
    ex.olap_parallel_workers = 4
    parallel = [ex.execute_sql(q, session) for q in queries]
    for q, a, b in zip(queries, serial, parallel):
        assert list(a.get('columns') or []) == list(b.get('columns') or []), q
        assert [list(r) for r in a.get('rows') or []] == \
            [list(r) for r in b.get('rows') or []], (q, a, b)


# ==================== 默认关闭：不加载 OLAP 引擎模块 ====================

class _BlockOlapEngineImports:
    """meta_path 阻断器：让 `lightdb.olap` / `lightdb.columnar` 引擎 import 硬失败"""

    def __init__(self):
        self.blocked = []

    def find_module(self, fullname, path=None):    # pragma: no cover - 兼容
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith('lightdb.olap')
                or fullname.startswith('lightdb.columnar')):
            self.blocked.append(fullname)
            raise ImportError(f'OLAP 引擎模块被测试阻断: {fullname}')
        return None


def test_default_off_does_not_load_olap_engines(monkeypatch):
    """默认（不设 env）时 SQL 查询不得 import/加载 OLAP/列存引擎模块"""
    import tempfile
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    blocker = _BlockOlapEngineImports()
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.MonkeyPatch.context() as mp:
            # 清掉已加载的 olap/列存模块（保留 status：它不是引擎）
            for name in list(sys.modules):
                if (name.startswith('lightdb.olap')
                        or name.startswith('lightdb.columnar')) \
                        and name != 'lightdb.olap.status':
                    mp.delitem(sys.modules, name, raising=False)
            sys.meta_path.insert(0, blocker)
            try:
                db = Database(os.path.join(tmp, 'db'))
                ex = Executor(db)              # 默认 olap_dispatch=False
                session = {}
                ex.execute_sql(_DDL, session)
                ex.execute_sql('INSERT INTO t VALUES (1, \'a\', 5, 1.5, \'x\')',
                               session)
                looks = [
                    'SELECT COUNT(*), SUM(v) FROM t',
                    'SELECT g, COUNT(*) FROM t GROUP BY g',
                    'SELECT AVG(v) FROM t WHERE v > 0',
                ]
                for q in looks:
                    r = ex.execute_sql(q, session)
                    assert r.get('type') == 'resultset', (q, r)
                assert not blocker.blocked, blocker.blocked
                db.shutdown()
            finally:
                sys.meta_path.remove(blocker)


# ==================== config / status 契约 ====================

def test_config_fields_and_env_mapping(monkeypatch):
    from lightdb.config import Config
    cfg = Config()
    assert cfg.olap_dispatch is False
    assert cfg.olap_dispatch_min_rows == 1000
    assert cfg.olap_crosscheck is False
    assert cfg.olap_parallel_workers == 0

    monkeypatch.setenv('LIGHTDB_OLAP_DISPATCH', '1')
    monkeypatch.setenv('LIGHTDB_OLAP_DISPATCH_MIN_ROWS', '77')
    monkeypatch.setenv('LIGHTDB_OLAP_CROSSCHECK', 'true')
    monkeypatch.setenv('LIGHTDB_OLAP_PARALLEL_WORKERS', '4')
    cfg2 = Config.from_env()
    assert cfg2.olap_dispatch is True
    assert cfg2.olap_dispatch_min_rows == 77
    assert cfg2.olap_crosscheck is True
    assert cfg2.olap_parallel_workers == 4
    assert not cfg2.validate()          # 配置合法


def test_env_enables_executor_dispatch(monkeypatch, tmp_path):
    """env LIGHTDB_OLAP_DISPATCH=1 → 新建 Executor 默认开启分派"""
    import tempfile
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_OLAP_DISPATCH', '1')
    monkeypatch.setenv('LIGHTDB_OLAP_DISPATCH_MIN_ROWS', '0')
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, 'db'))
        ex = Executor(db)
        assert ex.olap_dispatch is True
        assert ex.olap_dispatch_min_rows == 0
        db.shutdown()


# ==================== 4A.3 微基准（只打印，不断言阈值） ====================

def _median_time(fn, warmup=1, repeat=3):
    """返回 (中位耗时, 最后一次返回值)；warmup+中位数，避免冷启动抖动"""
    last = None
    for _ in range(warmup):
        last = fn()
    xs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        last = fn()
        xs.append(time.perf_counter() - t0)
    return statistics.median(xs), last


class _ListSource:
    def __init__(self, data):
        self._data = data
        self.col_names = list(data)
        self.row_count = len(next(iter(data.values())))

    def scan(self, columns=None):
        target = list(columns) if columns else list(self.col_names)
        return {c: self._data[c] for c in target}

    def scan_range(self, columns, start, end):
        d = self.scan(columns)
        return {c: v[int(start):int(end)] for c, v in d.items()}


def test_microbenchmark_1m_parallel_and_dispatch_ab(db_exec, capsys):
    """报告 1M 行聚合 1→4 线程、dispatch 开/关 A/B、min_rows 阈值影响"""
    db, ex, session = db_exec
    from lightdb.olap.vectorized_engine import (
        VectorizedAggregateSet, VectorizedScan)

    n = 1_000_000
    source = _ListSource({'v': list(range(n))})
    specs = [
        {'func': 'COUNT', 'column': '*', 'alias': 'c'},
        {'func': 'SUM', 'column': 'v', 'alias': 's'},
        {'func': 'AVG', 'column': 'v', 'alias': 'a'},
        {'func': 'MIN', 'column': 'v', 'alias': 'mn'},
        {'func': 'MAX', 'column': 'v', 'alias': 'mx'},
    ]

    def run_agg(workers):
        scan = VectorizedScan(source, ['v'])
        return VectorizedAggregateSet(scan, specs, workers=workers).execute()

    t1, r1 = _median_time(lambda: run_agg(1), warmup=1, repeat=2)
    t4, r4 = _median_time(lambda: run_agg(4), warmup=1, repeat=2)
    assert r1 == r4, '并行与串行结果必须逐值一致'

    # dispatch A/B（同一条 SQL，开关两种状态，warmup+中位数）
    ex.olap_dispatch = False
    off_t, off_r = _median_time(
        lambda: ex.execute_sql(
            'SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t',
            session), warmup=1, repeat=3)
    ex.olap_dispatch = True
    ex.olap_dispatch_min_rows = 0
    on_t, on_r = _median_time(
        lambda: ex.execute_sql(
            'SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t',
            session), warmup=1, repeat=3)
    assert off_r == on_r

    # min_rows 阈值：500 行表 < 默认 1000 → 不派发
    ex.execute_sql('CREATE TABLE s500 (id INT PRIMARY KEY, v INT)', session)
    ex.execute_sql('INSERT INTO s500 VALUES ' + ','.join(
        f'({i},{i})' for i in range(500)), session)
    ex.olap_dispatch_min_rows = 1000
    from lightdb.olap.sql_dispatch import reset_dispatch_stats, dispatch_stats
    reset_dispatch_stats()
    _median_time(lambda: ex.execute_sql('SELECT SUM(v) FROM s500', session))
    st500 = dispatch_stats()

    lines = [
        '[bench] 4A.3 1M 行 5 聚合 workers=1: %.1f ms' % (t1 * 1000),
        '[bench] 4A.3 1M 行 5 聚合 workers=4: %.1f ms 加速比 %.2fx 结果一致=%s'
        % (t4 * 1000, t1 / max(t4, 1e-9), r1 == r4),
        '[bench] dispatch 开/关（12 行小表，开销基线） off=%.2fms on=%.2fms '
        'ratio=%.2fx 结果一致=%s' % (
            off_t * 1000, on_t * 1000, off_t / max(on_t, 1e-9),
            off_r == on_r),
        '[bench] min_rows 阈值（500 行表 / 阈值 1000）: dispatched=%d（应为 0）, '
        '走行存' % st500['dispatched'],
        '[bench] 自动并行档 AUTO_PARALLEL_WORKERS=%d（<1.2x 时保持串行）'
        % __import__('lightdb.olap.vectorized_engine',
                     fromlist=['AUTO_PARALLEL_WORKERS']).AUTO_PARALLEL_WORKERS,
    ]
    with capsys.disabled():
        print('\n'.join(lines))
    # 阈值语义断言（性能数字不设门禁，阈值行为是契约）
    assert st500['dispatched'] == 0
    assert t1 > 0 and t4 > 0
