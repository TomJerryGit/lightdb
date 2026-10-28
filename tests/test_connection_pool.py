# -*- coding: utf-8 -*-
"""生产化 B（task-71）：服务端连接池

覆盖：
1. worker 线程复用（顺序短连接：created 远小于 assignments）、健康检查计数；
2. 超限策略：`reject`（握手前显式 1040）与 `queue`（等待后成功 / 超时 1040）；
3. 断连清理（连接关闭后 busy/active 归零，槽位可复用）；
4. 长事务不被复用：事务连接独占 worker（不被抢占/换线程），并发连接会话隔离；
5. `LIGHTDB_POOL_ENABLED=0` 回退每连接一线程；
6. `/metrics` 与 `SHOW STATUS` 暴露真实池指标；
7. 空闲缩容（idle_timeout 后 worker 退出，再次连接自动补）；
8. 1/4/8 客户端吞吐对比（量化，不设门禁）。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # pragma: no cover - 直接运行
    from conftest import track

POOL_ENV = (
    'LIGHTDB_POOL_ENABLED', 'LIGHTDB_POOL_MAX_WORKERS',
    'LIGHTDB_POOL_MIN_IDLE', 'LIGHTDB_POOL_IDLE_TIMEOUT',
    'LIGHTDB_POOL_OVERFLOW', 'LIGHTDB_POOL_QUEUE_TIMEOUT',
    'LIGHTDB_POOL_QUEUE_SIZE', 'LIGHTDB_POOL_HEALTH_INTERVAL',
    'LIGHTDB_POOL_LONG_RUNNING_S', 'LIGHTDB_METRICS_PORT',
    'LIGHTDB_METRICS_HOST',
)


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    for key in POOL_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LIGHTDB_POOL_MIN_IDLE', '0')
    monkeypatch.setenv('LIGHTDB_POOL_IDLE_TIMEOUT', '60')
    yield


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


class PoolEnv:
    def __init__(self, tmp_path, monkeypatch, env: dict = None,
                 metrics: bool = False):
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.net.server import MySQLServer
        from lightdb.sql.executor import Executor

        for k, v in (env or {}).items():
            monkeypatch.setenv(k, str(v))
        self.metrics_port = None
        if metrics:
            self.metrics_port = free_port()
            monkeypatch.setenv('LIGHTDB_METRICS_PORT', str(self.metrics_port))
            monkeypatch.setenv('LIGHTDB_METRICS_HOST', '127.0.0.1')
        self.db = track(Database(str(tmp_path / 'data')))
        self.ex = track(Executor(self.db))
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = free_port()
        self.cfg = cfg
        self.server = track(MySQLServer(cfg, self.db, self.ex))
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.2)

    def connect(self, **kw):
        import pymysql
        kw.setdefault('host', '127.0.0.1')
        kw.setdefault('port', self.cfg.port)
        kw.setdefault('user', 'root')
        kw.setdefault('autocommit', True)
        kw.setdefault('connect_timeout', 5)
        return pymysql.connect(**kw)

    def snap(self):
        assert self.server.pool is not None
        return self.server.pool.snapshot()

    def cleanup(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.db.shutdown()
        except Exception:
            pass


def wait_until(pred, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ==================== 1. 复用 / 健康检查 ====================

def test_pool_reuses_worker_threads(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 2,
                                          'LIGHTDB_POOL_MIN_IDLE': 0})
    try:
        for _ in range(6):
            conn = env.connect()
            cur = conn.cursor()
            cur.execute('SELECT 1')
            assert cur.fetchone()[0] == 1
            conn.close()
        assert wait_until(lambda: env.snap()['workers_busy'] == 0)
        s = env.snap()
        assert s['assignments'] == 6, s
        assert s['workers_created'] <= 2, s
        assert s['reused'] >= 4, s
        assert s['health_checks'] >= 1, s
        assert s['dead_workers'] == 0, s
        assert s['jobs_completed'] == 6, s
    finally:
        env.cleanup()


def test_pool_shows_in_show_status(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 2})
    try:
        conn = env.connect()
        cur = conn.cursor()
        cur.execute('SHOW STATUS')
        status = dict(cur.fetchall())
        for key in ('Pool_workers_total', 'Pool_workers_busy',
                    'Pool_workers_idle', 'Pool_max_workers',
                    'Pool_workers_created', 'Pool_assignments',
                    'Pool_reused', 'Pool_jobs_queued', 'Pool_rejected',
                    'Pool_queue_timeouts', 'Pool_idle_shrinks',
                    'Pool_dead_workers', 'Pool_overflow_policy'):
            assert key in status, (key, status)
        assert status['Pool_max_workers'] == '2'
        assert int(status['Pool_assignments']) >= 1
        assert status['Pool_overflow_policy'] == 'reject'
        conn.close()
    finally:
        env.cleanup()


# ==================== 2. 超限策略 ====================

def test_pool_overflow_reject_explicit_1040(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 1,
                                          'LIGHTDB_POOL_OVERFLOW': 'reject'})
    try:
        c1 = env.connect()
        cur = c1.cursor()
        cur.execute('SELECT 1')
        with pytest.raises(Exception) as ei:
            env.connect()
        # MySQL 语义：握手前 ERR 1040
        msg = str(ei.value)
        assert '1040' in msg or 'Too many connections' in msg, msg
        assert env.snap()['rejected'] >= 1
        # 原连接仍可用，关闭后可再建
        cur.execute('SELECT 2')
        assert cur.fetchone()[0] == 2
        c1.close()
        assert wait_until(lambda: env.snap()['workers_busy'] == 0)
        c2 = env.connect()
        cur2 = c2.cursor()
        cur2.execute('SELECT 3')
        assert cur2.fetchone()[0] == 3
        c2.close()
    finally:
        env.cleanup()


def test_pool_overflow_queue_waits_then_succeeds(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {
        'LIGHTDB_POOL_MAX_WORKERS': 1,
        'LIGHTDB_POOL_OVERFLOW': 'queue',
        'LIGHTDB_POOL_QUEUE_TIMEOUT': 8,
        'LIGHTDB_POOL_QUEUE_SIZE': 8,
    })
    try:
        c1 = env.connect()
        c1.cursor().execute('SELECT 1')
        result = {}

        def _second():
            try:
                c2 = env.connect()
                cur2 = c2.cursor()
                cur2.execute('SELECT 42')
                result['rows'] = cur2.fetchall()
                c2.close()
                result['ok'] = True
            except Exception as e:                # pragma: no cover - 诊断
                result['error'] = repr(e)

        th = threading.Thread(target=_second, daemon=True)
        th.start()
        # 第二连接应在排队（尚未握手/未拿到 worker）
        assert wait_until(
            lambda: env.snap()['jobs_pending_accept'] >= 1
            or env.snap()['queue_waits'] >= 1, timeout=3), env.snap()
        c1.close()                                # 释放唯一 worker
        th.join(timeout=10)
        assert result.get('ok') is True, result
        assert env.snap()['queue_timeouts'] == 0
        assert env.snap()['rejected'] == 0
    finally:
        env.cleanup()


def test_pool_overflow_queue_timeout_rejects(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {
        'LIGHTDB_POOL_MAX_WORKERS': 1,
        'LIGHTDB_POOL_OVERFLOW': 'queue',
        'LIGHTDB_POOL_QUEUE_TIMEOUT': 1,
    })
    try:
        c1 = env.connect()
        c1.cursor().execute('SELECT 1')
        started = time.time()
        with pytest.raises(Exception):
            env.connect(connect_timeout=6)
        waited = time.time() - started
        assert waited >= 0.8, waited
        assert env.snap()['queue_timeouts'] >= 1, env.snap()
        c1.close()
    finally:
        env.cleanup()


# ==================== 3. 断连清理 / 空闲缩容 ====================

def test_pool_disconnect_cleanup(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 2})
    try:
        conns = [env.connect() for _ in range(2)]
        for c in conns:
            c.cursor().execute('SELECT 1')
        assert wait_until(lambda: env.snap()['workers_busy'] == 2)
        for c in conns:
            c.close()
        assert wait_until(lambda: env.snap()['workers_busy'] == 0)
        assert env.server.connection_stats()['active_connections'] == 0
        # 槽位可复用
        c = env.connect()
        c.cursor().execute('SELECT 1')
        c.close()
        assert wait_until(lambda: env.snap()['jobs_completed'] >= 3,
                          timeout=5), env.snap()
    finally:
        env.cleanup()


def test_pool_idle_shrink(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {
        'LIGHTDB_POOL_MAX_WORKERS': 2,
        'LIGHTDB_POOL_MIN_IDLE': 0,
        'LIGHTDB_POOL_IDLE_TIMEOUT': 1,
    })
    try:
        c = env.connect()
        c.cursor().execute('SELECT 1')
        c.close()
        assert wait_until(lambda: env.snap()['workers_busy'] == 0)
        assert wait_until(lambda: env.snap()['idle_shrinks'] >= 1,
                          timeout=6), env.snap()
        assert env.snap()['workers_total'] == 0, env.snap()
        # 缩容后再来连接：自动补 worker
        c = env.connect()
        c.cursor().execute('SELECT 1')
        c.close()
        assert env.snap()['assignments'] >= 2
    finally:
        env.cleanup()


# ==================== 4. 长事务 / 会话隔离 ====================

def test_pool_long_transaction_not_reused_and_isolated(
        tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {
        'LIGHTDB_POOL_MAX_WORKERS': 2,
        'LIGHTDB_POOL_LONG_RUNNING_S': 0.2,
    })
    try:
        setup = env.connect()
        cur = setup.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(10))')
        setup.close()

        a = env.connect()
        a.autocommit(False)
        ca = a.cursor()
        ca.execute('SELECT CONNECTION_ID()')
        sid_a = ca.fetchone()[0]
        ca.execute('INSERT INTO t VALUES (1, \'txn\')')   # 未提交

        b = env.connect()
        cb = b.cursor()
        cb.execute('SELECT CONNECTION_ID()')
        sid_b = cb.fetchone()[0]
        assert sid_a != sid_b                    # 每连接独立会话
        cb.execute('SELECT COUNT(*) FROM t')
        assert cb.fetchone()[0] == 0             # 看不到 A 未提交行（会话隔离）
        # A 的长事务占用一个 worker，且不被回收/复用
        assert wait_until(
            lambda: env.snap()['long_running'] >= 1, timeout=5), env.snap()
        snap = env.snap()
        assert snap['workers_busy'] >= 2 and snap['assignments'] >= 2, snap
        # B 关闭不影响 A 的事务
        b.close()
        ca.execute('COMMIT')
        ca.execute('SELECT COUNT(*) FROM t')
        assert ca.fetchone()[0] == 1
        a.close()
        assert wait_until(lambda: env.snap()['workers_busy'] == 0)
        # 提交后（连接已关）后续连接读到已提交数据
        c = env.connect()
        cc = c.cursor()
        cc.execute('SELECT COUNT(*) FROM t')
        assert cc.fetchone()[0] == 1
        c.close()
    finally:
        env.cleanup()


def test_pool_session_isolation_user_vars(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 2})
    try:
        a = env.connect()
        b = env.connect()
        ca, cb = a.cursor(), b.cursor()
        ca.execute('SET @pool_x = 123')
        ca.execute('SELECT @pool_x')
        assert ca.fetchone()[0] == 123
        cb.execute('SELECT @pool_x')
        assert cb.fetchone()[0] is None          # 会话变量不跨连接/worker
        cb.execute('SET @pool_x = 456')
        cb.execute('SELECT @pool_x')
        assert cb.fetchone()[0] == 456
        a.close()
        b.close()
    finally:
        env.cleanup()


# ==================== 5. 关闭池 / 指标端点 ====================

def test_pool_disabled_falls_back(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_ENABLED': 0})
    try:
        assert env.server.pool is None
        for _ in range(3):
            c = env.connect()
            c.cursor().execute('SELECT 1')
            c.close()
        cur = env.connect().cursor()
        cur.execute('SHOW STATUS')
        status = dict(cur.fetchall())
        assert 'Pool_workers_total' not in status
    finally:
        env.cleanup()


def test_metrics_endpoint_exposes_pool_metrics(tmp_path, monkeypatch):
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 3},
                  metrics=True)
    try:
        c = env.connect()
        c.cursor().execute('SELECT 1')
        assert env.metrics_port
        body = urllib.request.urlopen(
            f'http://127.0.0.1:{env.metrics_port}/metrics',
            timeout=10).read().decode()
        assert 'lightdb_pool_workers{state="total"} ' in body
        assert 'lightdb_pool_max_workers 3' in body
        assert 'lightdb_pool_assignments_total' in body
        assert 'lightdb_pool_reused_total' in body
        assert 'lightdb_pool_rejected_total' in body
        c.close()
    finally:
        env.cleanup()


# ==================== 6. 吞吐量化（1/4/8 客户端） ====================

def test_pool_throughput_1_4_8_clients(tmp_path, monkeypatch):
    """连接池下 1/4/8 客户端 SELECT 1 吞吐对比（量化，不设门禁）

    打印 results 供交付说明引用；断言只保证"有真实吞吐"（>0）。
    """
    env = PoolEnv(tmp_path, monkeypatch, {'LIGHTDB_POOL_MAX_WORKERS': 8,
                                          'LIGHTDB_POOL_MIN_IDLE': 4})
    try:
        results = {}

        def bench(n_clients: int, per_client: int = 200):
            def worker():
                c = env.connect()
                cur = c.cursor()
                t0 = time.perf_counter()
                for _ in range(per_client):
                    cur.execute('SELECT 1')
                    cur.fetchone()
                dt = time.perf_counter() - t0
                c.close()
                results.setdefault(n_clients, []).append(dt)

            ths = [threading.Thread(target=worker) for _ in range(n_clients)]
            t0 = time.perf_counter()
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            wall = time.perf_counter() - t0
            qps = (n_clients * per_client) / wall
            results.setdefault('qps', {})[n_clients] = round(qps, 1)
            results.setdefault('wall', {})[n_clients] = round(wall, 3)

        for n in (1, 4, 8):
            bench(n)
        print('POOL_THROUGHPUT', json.dumps(
            {'qps': results['qps'], 'wall_s': results['wall']}))
        assert all(q > 0 for q in results['qps'].values())
        assert results['qps'][1] > 0
    finally:
        env.cleanup()
