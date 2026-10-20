"""可跑的性能测量项（阶段 2 第 5 项）。

每个 case = **一次可重复的测量**，产出 `benchmarks.results.json` 里的
一条记录。case 名带档位后缀（例如 `insert_rowwise_table_2000`），
门禁按前缀匹配（见 `gate.target_of`）。

套件分档：

* `quick` —— 目标 < 60 s，用于 CI / 提交前自检；行数都取小档。
* `full`  —— 目标 < 10 min，用于"要不要发版/要不要改阈值"的判断；
  含 TCP 点查与 1/2/4 客户端并发档（**只报告，不判定**）。
"""
from __future__ import annotations

import time

from . import concurrency as C
from . import heap_insert as H

#: 各 case 的元信息（谁跑、什么口径、单位）
CASE_META = {
    'insert_rowwise_table_2000': {
        'metric': 'INSERT 吞吐（Table API，逐行 autocommit，2,000 行档）',
        'unit': 'rows/s', 'gate': 'insert_rowwise_table'},
    'insert_rowwise_table_5000': {
        'metric': 'INSERT 吞吐（Table API，逐行 autocommit，5,000 行档）',
        'unit': 'rows/s', 'gate': 'insert_rowwise_table'},
    'insert_rowwise_sql_1000': {
        'metric': 'INSERT 吞吐（完整 SQL 路径，逐行，1,000 行档）',
        'unit': 'rows/s', 'gate': None},
    'insert_batch_speedup_5000': {
        'metric': '多值批量 vs 等价逐行语句的吞吐比（同窗交错成对，5,000 行档）',
        'unit': 'x', 'gate': 'insert_batch_speedup',
        'note': '一侧 = ceil(n/500) 条多值 INSERT，另一侧 = n 条单行 INSERT；'
                '同一时间窗内交替跑 3 对取比值中位数',
    },
    'insert_batch_speedup_2000': {
        'metric': '多值批量 vs 等价逐行语句的吞吐比（同窗交错成对，2,000 行档）',
        'unit': 'x', 'gate': 'insert_batch_speedup',
        'note': 'quick 档用；同样 3 对交错取中位比值。实测该档 ~1.6x，'
                '5,000 行档 ~1.49x（档位越小、语句固定开销占比越高）'},
    'insert_batch_single_stmt_2000': {
        'metric': 'INSERT 吞吐（单条多值 INSERT，500 行/语句，2,000 行档）',
        'unit': 'rows/s', 'gate': None,
        'note': '仅报告（abs 值跨档/跨窗不可比）；批量门禁看 '
                'insert_batch_speedup_5000'},
    'insert_batch_single_stmt_5000': {
        'metric': 'INSERT 吞吐（单条多值 INSERT，500 行/语句，5,000 行档）',
        'unit': 'rows/s', 'gate': None,
        'note': '与 insert_rowwise_table_5000 同档成对，用于批量加速比'},
    'point_inproc_2000': {
        'metric': '点查吞吐（进程内 Executor.execute_sql，2,000 行表）',
        'unit': 'q/s', 'gate': 'point_inproc'},
    'point_inproc_5000': {
        'metric': '点查吞吐（进程内 Executor.execute_sql，5,000 行表）',
        'unit': 'q/s', 'gate': 'point_inproc'},
    'point_tcp_2000': {
        'metric': '点查吞吐（真实 MySQL 协议 + pymysql，2,000 行表）',
        'unit': 'q/s', 'gate': None,
        'note': '仅报告：TCP 往返 + 服务端线程调度，抖动大'},
    'wal_bounded_5000': {
        'metric': '写入 5,000 行期间的 WAL 峰值（checkpoint 走引擎默认阈值）',
        'unit': 'bytes', 'gate': 'wal_bounded'},
    'wal_bounded_20000': {
        'metric': '写入 20,000 行期间的 WAL 峰值（checkpoint 走引擎默认阈值）',
        'unit': 'bytes', 'gate': 'wal_bounded'},
    'crash_recovery_2000': {
        'metric': '崩溃恢复墙钟（硬杀子进程 + 未提交事务，2,000 行已提交）',
        'unit': 's', 'gate': 'crash_recovery'},
    'crash_recovery_8000': {
        'metric': '崩溃恢复墙钟（硬杀子进程 + 未提交事务，8,000 行已提交）',
        'unit': 's', 'gate': 'crash_recovery'},
    'recovery_missing_rows': {
        'metric': '崩溃恢复后丢失的**已提交**行数（必须为 0）',
        'unit': 'rows', 'gate': 'recovery_missing_rows'},
    'recovery_leaked_rows': {
        'metric': '崩溃恢复后泄漏的**未提交**行数（必须为 0）',
        'unit': 'rows', 'gate': 'recovery_leaked_rows'},
    'concurrency_point_1': {
        'metric': '进程内并发点查总吞吐（1 客户端，3 s 窗口）',
        'unit': 'q/s', 'gate': None},
    'concurrency_point_4': {
        'metric': '进程内并发点查总吞吐（4 客户端，3 s 窗口）',
        'unit': 'q/s', 'gate': None},
    'concurrency_range_1': {
        'metric': '进程内并发范围查总吞吐（1 客户端，3 s 窗口）',
        'unit': 'q/s', 'gate': None},
    'concurrency_range_4': {
        'metric': '进程内并发范围查总吞吐（4 客户端，3 s 窗口）',
        'unit': 'q/s', 'gate': None},
    # ---------- OLAP 4A.5：门禁化（阈值来自 2026-10-20 实测基线） ----------
    'olap_parallel_groupby_100k': {
        'metric': '向量化 GROUP BY 分片并行加速比（100k 行，w1 vs w2，同进程成对）',
        'unit': 'x', 'gate': 'olap_parallel_groupby_speedup',
        'note': '实测 w1 中位 ~2.75 s → w2 ~1.00 s = 2.75x（2026-10-20 本机 8 核）；'
                '门禁取 1.2x 只抓"并行完全失效/负加速"'},
    'olap_dispatch_crosscheck_10000': {
        'metric': 'OLAP dispatch 开关前后逐行对拍的不一致行数（必须为 0）',
        'unit': 'rows', 'gate': 'olap_dispatch_crosscheck_mismatches',
        'note': '走真实 Executor：同一批 SQL 在 dispatch 关/开（crosscheck 模式）'
                '下逐行比较；任何不一致即门禁失败'},
    'olap_dispatch_agg_10000': {
        'metric': 'OLAP dispatch 开关同 SQL 的耗时比（on/off，仅报告）',
        'unit': 'x', 'gate': None,
        'note': '当前 dispatch 无性能收益（实测 0.44–1.10x），仅作事后判读；'
                '默认保持关闭，不据此启用'},
}


def _median(xs):
    import statistics
    return statistics.median(xs)


# ==================== 各 case 的测量函数 ====================
# 统一约定：函数返回一个 result dict（含 `metric_median` 等字段，形状见
# `heap_insert.summarize`），门禁只读 `metric_median`，其余字段用于事后判读。
# 崩溃恢复 case 例外：它返回 {'envelope': ..., 'extra_cases': {...}}，
# 由 `run_case` 拆成 3 条记录（恢复耗时 + 2 条硬判据）。

def _case_insert_rowwise(size=2000, rounds=2, variant='table', cfg='default',
                         data_dir=None):
    H.prepare_env(cfg)
    dd = data_dir or H.DEFAULT_DATA_DIR
    samples = [H.rowwise_round(size, variant, dd) for _ in range(rounds)]
    out = H.summarize(samples, 'rows_s')
    out['median_wal_bytes'] = _median([s['wal_after'] for s in samples])
    out['median_checkpoints'] = _median([s['checkpoints'] for s in samples])
    return out


def _case_insert_batch(size=2000, rounds=2, cfg='default', data_dir=None):
    H.prepare_env(cfg)
    dd = data_dir or H.DEFAULT_DATA_DIR
    samples = [H.batch_round(size, H.BATCH, dd) for _ in range(rounds)]
    out = H.summarize(samples, 'rows_s')
    out['median_wal_bytes'] = _median([s['wal_after'] for s in samples])
    out['median_checkpoints'] = _median([s['checkpoints'] for s in samples])
    return out


def _case_insert_batch_speedup(size=5000, data_dir=None):
    """同窗交错成对的"多值批量 vs 等价逐行语句"比值（门禁项）"""
    H.prepare_env('default')
    dd = data_dir or H.DEFAULT_DATA_DIR
    r = H.batch_vs_individual_round(size, H.BATCH, dd)
    return {'metric_median': r['ratio'],
            'min': min(p['ratio'] for p in r['pairs']),
            'max': max(p['ratio'] for p in r['pairs']),
            'n_samples': len(r['pairs']),
            'median_secs': None,
            'batch_rows_s': r['batch_rows_s'],
            'individual_rows_s': r['individual_rows_s'],
            'n': size, 'batch': r['batch'],
            'samples': r['pairs']}


def _case_point_inproc(nrows=2000, rounds=2, nq=600, data_dir=None):
    H.prepare_env('default')
    dd = data_dir or H.DEFAULT_DATA_DIR
    samples = [H.point_inproc_round(nrows, nq, dd) for _ in range(rounds)]
    return H.summarize(samples, 'q_s')


def _case_point_tcp(nrows=2000, rounds=1, nq=400, port=8977, data_dir=None):
    H.prepare_env('default')
    dd = data_dir or H.DEFAULT_DATA_DIR
    samples = [H.point_tcp_round(nrows, nq, port, dd) for _ in range(rounds)]
    return H.summarize(samples, 'q_s', warmup_drop=False)


def _case_wal_bounded(size=5000, cfg='default', data_dir=None):
    """在**引擎默认 checkpoint 阈值**下写 size 行，报告 WAL 峰值。

    峰值 = 每行写完后读一次 `t.wal` 的字节数（stat 一次 ~µs 级，
    对 1,000+ rows/s 的插入循环有可测量的扰动，所以**抽样而非每行**：
    默认每 25 行采一次，并由快照间隔覆盖整个写入过程）。

    本 case 因为要 stat WAL，**单独测量**（不与吞吐 case 共用结果），
    以免污染吞吐数字。
    """
    H.prepare_env(cfg)
    dd = data_dir or H.DEFAULT_DATA_DIR
    H.fresh_dir(dd)
    db, tbl = H.new_table(dd)
    peak, wal, wal_samples, secs = 0, 0, [], 0.0
    try:
        t0 = time.perf_counter()
        for i in range(1, size + 1):
            tbl.insert([i, i % 1000])
            if i % 25 == 0:                 # 抽样而不是每行（见 docstring）
                wal = H.wal_size(dd)
                wal_samples.append(wal)
                peak = max(peak, wal)
        secs = time.perf_counter() - t0
        wal = H.wal_size(dd)                # 循环结束后的最终值必采
        peak = max(peak, wal)
        limit = tbl_limit()
    finally:
        db.shutdown()
    return {'metric_median': float(peak),
            'min': float(min(wal_samples + [wal])), 'max': float(peak),
            'n_samples': 1, 'median_secs': secs, 'rows': size,
            'wal_final': wal, 'checkpoint_bytes': limit,
            'samples': wal_samples}


def tbl_limit():
    try:
        from lightdb.db.table import (WAL_CHECKPOINT_BYTES_DEFAULT,
                                      WAL_CHECKPOINT_RECORDS_DEFAULT)
        return {'bytes': WAL_CHECKPOINT_BYTES_DEFAULT,
                'records': WAL_CHECKPOINT_RECORDS_DEFAULT}
    except Exception:                                # pragma: no cover
        return None


def _case_crash_recovery(n_committed=2000, n_uncommitted=50,
                         threshold=256 * 1024, data_dir=None):
    """崩溃恢复：返回包装 dict（envelope），见 `run_case` 的说明"""
    dd = data_dir or H.DEFAULT_DATA_DIR
    H.prepare_env('default')
    r = H.crash_recovery_round(n_committed, n_uncommitted, threshold, dd)
    res = {'metric_median': r['recover_secs'], 'min': r['recover_secs'],
           'max': r['recover_secs'], 'n_samples': 1,
           'median_secs': r['recover_secs'], 'samples': [r],
           'rows_seen': r['rows_seen'],
           'rows_expected': r['rows_expected'],
           'wal_at_crash': r['wal_at_crash'], 'wal_after': r['wal_after']}
    integrity = {
        'recovery_missing_rows': {
            'metric_median': float(r['missing']), 'min': 0.0,
            'max': float(r['missing']), 'n_samples': 1, 'samples': [],
            'detail': f"已提交 {r['rows_expected']} 行中丢失 {r['missing']} 行"},
        'recovery_leaked_rows': {
            'metric_median': float(r['leaked']), 'min': 0.0,
            'max': float(r['leaked']), 'n_samples': 1, 'samples': [],
            'detail': f"未提交 {r['n_uncommitted']} 行中泄漏 {r['leaked']} 行"},
    }
    return {'envelope': res, 'extra_cases': integrity}


def _case_concurrency(kind='point', clients=1, seconds=3.0, nrows=2000,
                      data_dir=None):
    H.prepare_env('default')
    dd = data_dir or C.DEFAULT_DATA_DIR
    r = C.concurrency_round(kind, clients, seconds, nrows, 0.5, dd)
    return {'metric_median': r['q_s'], 'min': r['q_s'], 'max': r['q_s'],
            'n_samples': 1, 'median_secs': r['wall_s'],
            'p50_ms': r['p50_ms'], 'p95_ms': r['p95_ms'],
            'per_client_qps': r['per_client_qps'],
            'scaling_ratio': r['scaling_ratio'],
            'errors': r['errors'], 'samples': []}


# ==================== OLAP 4A.5 门禁化 case（2026-10-20） ====================

def _olap_tempdir(data_dir):
    """OLAP case 的临时目录（ColumnStore 是内存列存，仍需要一个目录标识）。"""
    import tempfile
    if data_dir:
        return tempfile.mkdtemp(prefix='olap_bench_', dir=data_dir)
    return tempfile.mkdtemp(prefix='olap_bench_')


def _case_olap_parallel_groupby(size=100_000, rounds=3, data_dir=None):
    """4A.3/4A.5：向量化 GROUP BY 分片线程池加速比（w1 vs w2，同进程成对）。

    * 结果先做单/多线程逐值一致性校验（不一致直接抛错，不产出"加速"数字）；
    * 计时取中位（warmup 兼一致性校验 + rounds 轮），门禁只看 metric_median。
    """
    import shutil
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedGroupBy)

    dd = _olap_tempdir(data_dir)
    try:
        store = ColumnStore('t', [
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'},
        ], dd)
        store.append_rows([[f'c{i % 100}', i] for i in range(size)])
        aggs = [{'func': 'SUM', 'column': 'amount', 'alias': 'total'},
                {'func': 'COUNT', 'column': '*', 'alias': 'cnt'}]

        def one(workers):
            gb = VectorizedGroupBy(
                VectorizedScan(store, ['city', 'amount']),
                ['city'], aggs, workers=workers)
            t0 = time.perf_counter()
            rows = gb.execute()
            return rows, time.perf_counter() - t0

        rows1, dt1 = one(1)                # warmup 兼一致性校验
        rows2, _ = one(2)
        if rows1 != rows2:
            raise RuntimeError('并行 GROUP BY 结果与串行不一致（门禁拒绝产出数字）')
        serial_samples = [dt1]
        parallel_samples = []
        for _ in range(rounds):
            _, dt = one(2)
            parallel_samples.append(dt)
        for _ in range(rounds):
            _, dt = one(1)
            serial_samples.append(dt)
        serial = _median(serial_samples)
        parallel = _median(parallel_samples)
        ratio = serial / parallel if parallel > 0 else 0.0
        return {'metric_median': ratio,
                'min': min(serial_samples) / max(parallel_samples),
                'max': max(serial_samples) / min(parallel_samples),
                'n_samples': len(parallel_samples),
                'median_secs': parallel,
                'serial_median_secs': serial,
                'parallel_workers': 2, 'rows': size,
                'samples': {'serial': serial_samples,
                            'parallel': parallel_samples}}
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def _case_olap_dispatch_crosscheck(size=10_000, data_dir=None):
    """4A.2/4A.5：dispatch 开关前后逐行对拍（真实 Executor，不一致行数必须 0）。"""
    import shutil
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    dd = _olap_tempdir(data_dir)
    try:
        db = Database(dd)
        ex = Executor(db)
        try:
            ex.execute_sql(
                'CREATE TABLE t (id INT PRIMARY KEY, v INT, city VARCHAR(20))',
                {})
            for i in range(size):
                ex.execute_sql(
                    f"INSERT INTO t VALUES ({i}, {i % 97}, 'c{i % 10}')", {})

            queries = [
                'SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t',
                'SELECT COUNT(*), SUM(v) FROM t WHERE id < 500',
                'SELECT city, COUNT(*), SUM(v) FROM t GROUP BY city',
                'SELECT city, SUM(v) AS s FROM t GROUP BY city '
                'ORDER BY s DESC LIMIT 5',
                'SELECT COUNT(*), SUM(v) FROM t WHERE city = \'c3\' '
                'AND v >= 10',
            ]
            mismatches = 0
            dispatched = 0
            from lightdb.olap.sql_dispatch import (
                dispatch_stats, reset_dispatch_stats)
            for sql in queries:
                ex.olap_dispatch = False
                ex.olap_crosscheck = False
                ref = ex.execute_sql(sql, {})
                ex.olap_dispatch = True
                ex.olap_crosscheck = True
                ex.olap_dispatch_min_rows = 0
                reset_dispatch_stats()
                got = ex.execute_sql(sql, {})
                dispatched += dispatch_stats()['dispatched']
                if (ref.get('columns'), ref.get('rows')) != (
                        got.get('columns'), got.get('rows')):
                    mismatches += 1
            if dispatched == 0:
                raise RuntimeError('对拍用例一次 dispatch 都没发生（门禁拒绝产出 0）')
            return {'metric_median': float(mismatches), 'min': 0.0,
                    'max': float(mismatches), 'n_samples': 1,
                    'median_secs': None, 'rows_crosschecked': len(queries),
                    'dispatched': dispatched, 'samples': []}
        finally:
            db.shutdown()
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def _case_olap_dispatch_agg(size=10_000, data_dir=None):
    """dispatch 开关同 SQL 耗时比（仅报告；当前无收益，不据此启用）。"""
    import shutil
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    dd = _olap_tempdir(data_dir)
    try:
        db = Database(dd)
        ex = Executor(db)
        try:
            ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v INT)', {})
            for i in range(size):
                ex.execute_sql(f'INSERT INTO t VALUES ({i}, {i % 97})', {})
            sql = 'SELECT COUNT(*), SUM(v), AVG(v) FROM t'

            def timed(enable):
                ex.olap_dispatch = enable
                ex.olap_dispatch_min_rows = 0
                ex.olap_crosscheck = False
                samples = []
                for _ in range(3):
                    t0 = time.perf_counter()
                    ex.execute_sql(sql, {})
                    samples.append(time.perf_counter() - t0)
                return _median(samples)

            off = timed(False)
            on = timed(True)
            ratio = on / off if off > 0 else 0.0
            return {'metric_median': ratio, 'min': ratio, 'max': ratio,
                    'n_samples': 1, 'median_secs': on, 'off_secs': off,
                    'rows': size, 'samples': []}
        finally:
            db.shutdown()
    finally:
        shutil.rmtree(dd, ignore_errors=True)


# ==================== 套件定义 ====================
# 每项: (case_name, callable)。callable 收到 `data_dir=None` 关键字参数
# （`benchmarks.run --data-dir` / 干净 worktree 隔离时用到）。
SUITES = {
    'quick': [
        ('insert_rowwise_table_2000',
         lambda **kw: _case_insert_rowwise(2000, 3, **kw)),
        ('insert_batch_single_stmt_2000',
         lambda **kw: _case_insert_batch(2000, 3, **kw)),
        ('point_inproc_2000',
         lambda **kw: _case_point_inproc(2000, 3, **kw)),
        ('wal_bounded_5000', lambda **kw: _case_wal_bounded(5000, **kw)),
        ('crash_recovery_2000', lambda **kw: _case_crash_recovery(2000, **kw)),
        ('insert_batch_speedup_2000',
         lambda **kw: _case_insert_batch_speedup(2000, **kw)),
        # OLAP 4A.5 门禁项（阈值见 benchmarks/gate.py）
        ('olap_parallel_groupby_100k',
         lambda **kw: _case_olap_parallel_groupby(100_000, 2, **kw)),
        ('olap_dispatch_crosscheck_2000',
         lambda **kw: _case_olap_dispatch_crosscheck(2000, **kw)),
        ('olap_dispatch_agg_2000',
         lambda **kw: _case_olap_dispatch_agg(2000, **kw)),
    ],
    'full': [
        ('insert_rowwise_table_2000',
         lambda **kw: _case_insert_rowwise(2000, 3, **kw)),
        ('insert_rowwise_table_5000',
         lambda **kw: _case_insert_rowwise(5000, 4, **kw)),
        ('insert_rowwise_sql_1000',
         lambda **kw: _case_insert_rowwise(1000, 2, 'sql', **kw)),
        ('insert_batch_single_stmt_2000',
         lambda **kw: _case_insert_batch(2000, 3, **kw)),
        ('insert_batch_single_stmt_5000',
         lambda **kw: _case_insert_batch(5000, 4, **kw)),
        ('point_inproc_2000', lambda **kw: _case_point_inproc(2000, 3, **kw)),
        ('point_inproc_5000',
         lambda **kw: _case_point_inproc(5000, 2, 400, **kw)),
        ('point_tcp_2000', lambda **kw: _case_point_tcp(2000, 1, 400, **kw)),
        ('wal_bounded_20000', lambda **kw: _case_wal_bounded(20000, **kw)),
        ('crash_recovery_8000', lambda **kw: _case_crash_recovery(8000, **kw)),
        ('insert_batch_speedup_5000',
         lambda **kw: _case_insert_batch_speedup(5000, **kw)),
        ('insert_batch_speedup_2000',
         lambda **kw: _case_insert_batch_speedup(2000, **kw)),
        ('concurrency_point_1',
         lambda **kw: _case_concurrency('point', 1, **kw)),
        ('concurrency_point_4',
         lambda **kw: _case_concurrency('point', 4, **kw)),
        ('concurrency_range_1',
         lambda **kw: _case_concurrency('range', 1, **kw)),
        ('concurrency_range_4',
         lambda **kw: _case_concurrency('range', 4, **kw)),
    ],
}


def case_names(suite: str):
    return [name for name, _fn in SUITES[suite]]


def all_case_names():
    return sorted({n for s in SUITES.values() for n in case_names(s)})


def run_case(name: str, data_dir=None) -> dict:
    """跑一个 case，返回 `(results_dict, meta)`。

    `results_dict` 里可能有**多条**记录：崩溃恢复 case 会额外产出
    `recovery_missing_rows` / `recovery_leaked_rows` 两条硬判据。
    同一 case 出现在多个套件里时，以 `quick` 优先（quick 档行数更小、
    更快，被测的是同一段代码）。
    """
    for suite in ('quick', 'full'):
        for cname, fn in SUITES[suite]:
            if cname == name:
                t0 = time.perf_counter()
                out = fn(data_dir=data_dir)
                secs = time.perf_counter() - t0
                if 'envelope' in out:
                    # 包装形式：主指标在 envelope 里，另有硬判据子 case
                    results = {name: out['envelope']}
                    results.update(out.get('extra_cases') or {})
                else:
                    results = {name: out}
                return results, {'case': name, 'wall_s': secs}
    raise KeyError(f'未知的 case: {name}（可选: {all_case_names()}）')


def run_cases(names, data_dir=None, progress=None) -> dict:
    """按给定顺序跑若干 case，返回 `{case_name: result, '_timings': {...}}`"""
    out, timings = {}, {}
    for name in names:
        if progress:
            progress(name)
        res, meta = run_case(name, data_dir=data_dir)
        out.update(res)
        timings[name] = meta['wall_s']
    out['_timings'] = timings
    return out


def run_suite(suite: str, only=None, data_dir=None, progress=None) -> dict:
    """跑一个套件，返回 `{case_name: result}`（含派生出的硬判据 case）"""
    names = case_names(suite)
    if only:
        unknown = [n for n in only if n not in names]
        if unknown:
            raise KeyError(f'--only 里有不属于 suite={suite} 的 case: {unknown}')
        names = [n for n in names if n in only]
    return run_cases(names, data_dir=data_dir, progress=progress)


def run_quick_cases(data_dir=None, only=None) -> dict:
    """给 `tests/test_benchmarks.py` 与 `benchmarks.run` 共用的入口。

    case 集合与 `--suite quick` **逐字相同**，所以 CI 里断言的就是
    命令行会跑的东西。
    """
    return run_suite('quick', only=only, data_dir=data_dir)
