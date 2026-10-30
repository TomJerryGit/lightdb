# -*- coding: utf-8 -*-
"""P4（task-85）：MySQL 协议只读分片路由 + 快照复用 + worker 池

覆盖：
1. 真实 pymysql：可安全分片的 SELECT 走分片，结果与单进程执行**逐行一致**
   （全量/列子集/`SELECT *`/单行/边界/空范围）；
2. 快照复用：数据未变时同一快照被后续查询复用（snapshot_created 不增长）；
   写入后（服务端写入纪元/文件指纹变化）自动刷新，结果包含新行；
3. 回退：分片关闭、低于阈值、不支持形态（聚合/JOIN/LIMIT/ORDER BY/非主键）、
   会话事务、写语句 → 回退普通执行且结果正确（不静默错答）；
4. 并发多客户端各自结果正确；
5. `/metrics` 暴露分片指标（requests/sharded/fallbacks/snapshot/worker/latency）；
6. worker 崩溃/超时 → 显式 1105（不是成功/空结果）；
7. 边界：动态端口 teardown 关闭 metrics socket（复用 task-79 模式）。
"""
import os
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # pragma: no cover
    from conftest import track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_KEYS = ('LIGHTDB_SHARDING_ENABLED', 'LIGHTDB_SHARDING_WORKERS',
            'LIGHTDB_SHARDING_MIN_ROWS', 'LIGHTDB_SHARDING_TIMEOUT',
            'LIGHTDB_SHARDING_SNAPSHOT_TTL', 'LIGHTDB_SHARDING_POOL_ROOT',
            'LIGHTDB_SHARDING_TEST_WORKER_CRASH',
            'LIGHTDB_SHARDING_TEST_WORKER_SLEEP',
            'LIGHTDB_METRICS_PORT', 'LIGHTDB_METRICS_HOST',
            'LIGHTDB_POOL_ENABLED', 'LIGHTDB_AUDIT_LEVEL')
SHARD_KEY = ('LIGHTDB_SHARDING_')


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _save_env():
    return {k: os.environ.get(k) for k in ENV_KEYS}


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _quiet():
    os.environ['LIGHTDB_AUDIT_LEVEL'] = 'NONE'
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                     # pragma: no cover
        pass


class Served:
    """真实 MySQLServer（分片开启/关闭）+ 独立 Database 数据目录"""

    def __init__(self, data_dir, sharding=True, rows=1200,
                 min_rows=100, workers=2, timeout_s=30.0):
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.net.server import MySQLServer
        from lightdb.sql.executor import Executor

        _quiet()
        os.makedirs(str(data_dir), exist_ok=True)
        db = Database(str(data_dir))
        if 't' not in db.tables:
            tbl = db.create_table('t', [
                {'name': 'id', 'type': 'INT', 'primary': True},
                {'name': 'v', 'type': 'INT'}])
            for i in range(1, rows + 1):
                tbl.insert([i, (i * 37) % 1000])
            db.create_table('t2', [
                {'name': 'id', 'type': 'INT', 'primary': True},
                {'name': 'w', 'type': 'INT'}])
            for i in range(1, 51):
                db.get_table('t2').insert([i, i])
            db.checkpoint(force=True)
        ex = Executor(db)
        self.db, self.ex = db, ex
        self.env_saved = _save_env()
        os.environ['LIGHTDB_SHARDING_ENABLED'] = '1' if sharding else '0'
        os.environ['LIGHTDB_SHARDING_WORKERS'] = str(workers)
        os.environ['LIGHTDB_SHARDING_MIN_ROWS'] = str(min_rows)
        os.environ['LIGHTDB_SHARDING_TIMEOUT'] = str(timeout_s)
        os.environ['LIGHTDB_METRICS_PORT'] = '0'
        os.environ['LIGHTDB_METRICS_HOST'] = '127.0.0.1'
        os.environ['LIGHTDB_POOL_ENABLED'] = '0'
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = free_port()
        self.cfg = cfg
        self.server = MySQLServer(cfg, db, ex)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        time.sleep(0.25)
        self.metrics_port = int(
            self.server.metrics_server.server_address[1])
        self._conns = []

    def connect(self, autocommit=True):
        import pymysql
        conn = pymysql.connect(host='127.0.0.1', port=self.cfg.port,
                               user='root', autocommit=autocommit,
                               connect_timeout=10)
        self._conns.append(conn)
        return conn

    def baseline(self, sql):
        from lightdb.sql.parser import parser
        r = self.ex.execute(parser.parse(sql), {})
        assert r['type'] == 'resultset', r
        return [tuple(row) for row in r['rows']], list(r['columns'])

    def query(self, sql):
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()] if cur.description \
                else []
            cols = [d[0] for d in (cur.description or [])]
            return rows, cols
        finally:
            conn.close()

    def metrics(self) -> str:
        return urllib.request.urlopen(
            f'http://127.0.0.1:{self.metrics_port}/metrics',
            timeout=10).read().decode()

    def close(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.db.shutdown()
        except Exception:
            pass
        _restore_env(self.env_saved)


def _metric(text, name):
    for line in text.splitlines():
        if line.startswith(name + ' '):
            return float(line[len(name) + 1:])
    return 0.0


@pytest.fixture(scope='module')
def served(tmp_path_factory):
    env = Served(tmp_path_factory.mktemp('sharding_router') / 'db')
    try:
        yield env
    finally:
        env.close()


# ==================== 1. 正确性（真实协议） ====================

def test_sharded_select_matches_single_process(served):
    cases = [
        'SELECT id, v FROM t WHERE id BETWEEN 100 AND 900',
        'SELECT * FROM t WHERE id BETWEEN 1 AND 1200',
        'SELECT v, id FROM t WHERE id BETWEEN 10 AND 10',
        'SELECT * FROM t WHERE id BETWEEN 1 AND 1',
        'SELECT * FROM t WHERE id BETWEEN 1190 AND 1210',
        'SELECT * FROM t WHERE id BETWEEN 500 AND 499',   # 空区间
    ]
    for sql in cases:
        expected_rows, expected_cols = served.baseline(sql)
        rows, cols = served.query(sql)
        assert cols == expected_cols, (sql, cols, expected_cols)
        assert rows == expected_rows, (sql, len(rows), len(expected_rows))
        assert served.server.sharding is not None
    snap = served.server.sharding.metrics_snapshot()
    assert snap['sharded_total'] >= 1
    assert snap['requests_total'] >= snap['sharded_total']


def test_snapshot_reused_across_queries(served):
    sql = 'SELECT id, v FROM t WHERE id BETWEEN 200 AND 800'
    served.query(sql)
    m1 = served.server.sharding.metrics_snapshot()
    assert m1['snapshot_created_total'] >= 1
    created = m1['snapshot_created_total']
    reused = m1['snapshot_reused_total']
    served.query(sql)
    served.query(sql)
    m2 = served.server.sharding.metrics_snapshot()
    # 数据未变：不生成新快照，复用计数增长
    assert m2['snapshot_created_total'] == created
    assert m2['snapshot_reused_total'] >= reused + 2
    text = served.metrics()
    assert _metric(text, 'lightdb_sharding_sharded_total') >= 1
    assert _metric(text, 'lightdb_sharding_enabled') == 1
    assert _metric(text, 'lightdb_sharding_workers') == 2
    assert _metric(text, 'lightdb_sharding_min_rows') == 100
    assert _metric(text, 'lightdb_sharding_snapshot_age_seconds') >= 0
    assert _metric(text, 'lightdb_sharding_query_latency_ms_last') > 0


# ==================== 2. 回退：关闭 / 形态 / 阈值 / 事务 ====================

def test_sharding_disabled_falls_back(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        if k.startswith(SHARD_KEY):
            monkeypatch.setenv(k, '0')
    monkeypatch.setenv('LIGHTDB_SHARDING_ENABLED', '0')
    env = Served(tmp_path / 'off', sharding=False)
    try:
        assert env.server.sharding is None
        sql = 'SELECT id, v FROM t WHERE id BETWEEN 100 AND 900'
        expected, cols = env.baseline(sql)
        rows, got_cols = env.query(sql)
        assert got_cols == cols and rows == expected
        text = env.metrics()
        assert _metric(text, 'lightdb_sharding_enabled') == 0
        assert _metric(text, 'lightdb_sharding_sharded_total') == 0
    finally:
        env.close()


def test_unsupported_forms_fall_back_correctly(served):
    forms = [
        'SELECT COUNT(*) FROM t WHERE id BETWEEN 1 AND 100',   # 聚合
        'SELECT * FROM t WHERE id BETWEEN 1 AND 100 LIMIT 5',  # LIMIT
        'SELECT * FROM t WHERE id BETWEEN 1 AND 100 ORDER BY id',
        'SELECT * FROM t WHERE v BETWEEN 1 AND 100',           # 非主键
        'SELECT * FROM t JOIN t2 ON t.id = t2.id '
        'WHERE t.id BETWEEN 1 AND 50',                          # JOIN
        'SELECT v FROM t WHERE id BETWEEN 1 AND 100',          # 不含主键
    ]
    before = served.server.sharding.metrics_snapshot()['fallbacks_total']
    for sql in forms:
        expected, cols = served.baseline(sql)
        rows, got_cols = served.query(sql)
        assert got_cols == cols, (sql, got_cols, cols)
        assert rows == expected, sql
    after = served.server.sharding.metrics_snapshot()
    assert after['fallbacks_total'] >= before + len(forms)
    reasons = after['fallbacks_by_reason']
    assert reasons['unsupported'] >= len(forms)


def test_below_threshold_falls_back(served):
    sql = 'SELECT id, v FROM t WHERE id BETWEEN 10 AND 20'   # span=11 < 100
    before = served.server.sharding.metrics_snapshot()
    rows, cols = served.query(sql)
    expected, expected_cols = served.baseline(sql)
    assert rows == expected and cols == expected_cols
    after = served.server.sharding.metrics_snapshot()
    assert after['fallbacks_by_reason']['below_threshold'] >= \
        before['fallbacks_by_reason']['below_threshold'] + 1


def test_session_transaction_falls_back(tmp_path, monkeypatch):
    env = Served(tmp_path / 'txn', sharding=True, rows=400, min_rows=50)
    try:
        conn = env.connect(autocommit=False)
        cur = conn.cursor()
        cur.execute('INSERT INTO t VALUES (9999, 1)')       # 未提交
        cur.execute('SELECT id, v FROM t WHERE id BETWEEN 1 AND 9999')
        rows = [tuple(r) for r in cur.fetchall()]
        assert (9999, 1) in rows          # 回退普通执行，能看到未提交行
        conn.commit()
        cur.execute('SELECT id, v FROM t WHERE id BETWEEN 1 AND 9999')
        rows2 = [tuple(r) for r in cur.fetchall()]
        assert (9999, 1) in rows2
        conn.close()
    finally:
        env.close()


# ==================== 3. 并发 ====================

def test_concurrent_clients_correct(served):
    ranges = [(1, 300), (200, 600), (500, 900), (800, 1200)]

    def worker(lo, hi):
        out = []
        conn = served.connect()
        try:
            cur = conn.cursor()
            for _ in range(5):
                cur.execute('SELECT id, v FROM t WHERE id BETWEEN %s AND %s',
                            (lo, hi))
                out.append([tuple(r) for r in cur.fetchall()])
        finally:
            conn.close()
        return out

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda a: worker(*a), ranges))
    for (lo, hi), got_list in zip(ranges, results):
        expected, _ = served.baseline(
            f'SELECT id, v FROM t WHERE id BETWEEN {lo} AND {hi}')
        for got in got_list:
            assert got == expected, (lo, hi, len(got), len(expected))


# ==================== 4. 写入后翻新快照 + 可见性 ====================

def test_write_triggers_snapshot_refresh_and_visibility(
        tmp_path, monkeypatch):
    env = Served(tmp_path / 'refresh', sharding=True, rows=300,
                 min_rows=50)
    try:
        sql = 'SELECT id, v FROM t WHERE id BETWEEN 1 AND 300'
        rows, _ = env.query(sql)
        assert len(rows) == 300
        created = env.server.sharding.metrics_snapshot()[
            'snapshot_created_total']
        conn = env.connect()
        cur = conn.cursor()
        cur.execute('INSERT INTO t VALUES (301, 7)')
        conn.close()
        rows2, _ = env.query('SELECT id, v FROM t WHERE id BETWEEN 1 AND 301')
        assert (301, 7) in rows2 and len(rows2) == 301
        after = env.server.sharding.metrics_snapshot()
        assert after['snapshot_created_total'] > created   # 已刷新
        # 刷新后的只读查询复用新快照
        reused = after['snapshot_reused_total']
        env.query('SELECT id, v FROM t WHERE id BETWEEN 1 AND 301')
        assert env.server.sharding.metrics_snapshot()[
            'snapshot_reused_total'] >= reused + 1
    finally:
        env.close()


# ==================== 5. worker 崩溃 / 超时：显式 1105 ====================

def test_worker_crash_is_explicit_1105(tmp_path, monkeypatch):
    import pymysql
    monkeypatch.setenv('LIGHTDB_SHARDING_TEST_WORKER_CRASH', '1')
    env = Served(tmp_path / 'crash', sharding=True, rows=300,
                 min_rows=50, timeout_s=20)
    try:
        with pytest.raises(pymysql.err.OperationalError) as ei:
            env.query('SELECT id, v FROM t WHERE id BETWEEN 1 AND 300')
        assert int(ei.value.args[0]) == 1105, ei.value
        assert 'worker' in str(ei.value.args[1]).lower(), ei.value
        # 显式错误后连接仍可用（普通查询/回退路径不受影响）
        rows, _ = env.query('SELECT COUNT(*) FROM t WHERE id BETWEEN 1 AND 10')
        assert rows
    finally:
        env.close()


def test_worker_timeout_is_explicit_1105(tmp_path, monkeypatch):
    import pymysql
    monkeypatch.setenv('LIGHTDB_SHARDING_TEST_WORKER_SLEEP', '5')
    env = Served(tmp_path / 'timeout', sharding=True, rows=300,
                 min_rows=50, timeout_s=1.0)
    try:
        with pytest.raises(pymysql.err.OperationalError) as ei:
            env.query('SELECT id, v FROM t WHERE id BETWEEN 1 AND 300')
        assert int(ei.value.args[0]) == 1105, ei.value
        assert '超时' in str(ei.value.args[1]), ei.value
        assert env.server.sharding.metrics_snapshot()[
            'worker_errors_total'] >= 1
    finally:
        env.close()
