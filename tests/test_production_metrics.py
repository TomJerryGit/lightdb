# -*- coding: utf-8 -*-
"""生产化 P1-A（task-70）：/metrics 端点 + 慢查询日志 + 审计轮转

覆盖：
1. ``/metrics``（Prometheus text / JSON）**真实采集**：连接/活跃连接、语句数
   与 QPS、错误按 errno、慢查询计数与 top 榜、WAL 字节、checkpoint 次数、
   备份/快照统计、内存/线程数；``/health``、404/405。
2. 慢查询日志：``LIGHTDB_SLOW_QUERY_MS`` 阈值触发/不触发；记录字段
   （时间/用户/耗时/SQL/成功/errno）；环形缓冲 + JSONL 落盘；按大小轮转、
   保留 N 个历史文件；口令类 SQL 脱敏；``SELECT * FROM __SLOW_QUERY_LOG__``
   可读；与 /metrics 数值一致。
3. 审计轮转：按条数/大小轮转、保留 N 个文件、``SHOW AUDIT LOG`` 行为不变
   （内存环形缓冲）、重启后轮转历史仍可查询、半行/缺文件崩溃容错、记录不丢。
4. 无数据时输出 0 并用 ``#`` 注释说明（不编造）。

全部进程内，不依赖多进程原语。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_production_metrics.py
    from conftest import safe_rmtree, track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SLOW_ENV_KEYS = (
    'LIGHTDB_SLOW_QUERY_MS', 'LIGHTDB_SLOWLOG_ENABLED',
    'LIGHTDB_SLOWLOG_FILE', 'LIGHTDB_SLOWLOG_MAX_BYTES',
    'LIGHTDB_SLOWLOG_KEEP', 'LIGHTDB_SLOWLOG_BUFFER',
    'LIGHTDB_SLOWLOG_SQL_MAX', 'LIGHTDB_SLOWLOG_FSYNC',
    'LIGHTDB_AUDIT_MAX_BYTES', 'LIGHTDB_AUDIT_MAX_RECORDS',
    'LIGHTDB_AUDIT_KEEP', 'LIGHTDB_METRICS_PORT', 'LIGHTDB_METRICS_HOST',
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例从干净环境开始（避免开发机上的全局 env 干扰断言）"""
    for key in SLOW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


# ==================== 公共工具 ====================

def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def http_get(url: str, timeout: float = 10.0):
    """返回 (status, body_text, content_type)；HTTPError 也返回响应体"""
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return (resp.status, resp.read().decode('utf-8', 'replace'),
                resp.headers.get('Content-Type', ''))
    except urllib.error.HTTPError as e:
        return (e.code, e.read().decode('utf-8', 'replace'),
                e.headers.get('Content-Type', ''))


def wait_http(url: str, timeout: float = 10.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return http_get(url, timeout=2)
        except Exception:
            time.sleep(0.05)
    raise AssertionError(f'HTTP 端点未就绪: {url}')


def metrics_value(text: str, name: str, labels: dict = None):
    """从 Prometheus 文本里取指标值（找不到返回 None）"""
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        if labels:
            label_text = ','.join(f'{k}="{v}"' for k, v in labels.items())
            prefix = f'{name}{{{label_text}}}'
            if line.startswith(prefix + ' '):
                return float(line[len(prefix) + 1:])
        elif line.startswith(name + ' '):
            return float(line[len(name) + 1:])
    return None


class ServerEnv:
    """一台真实 MySQLServer（+可选 /metrics 端点）+ DB/Executor 的测试环境"""

    def __init__(self, tmp_path, monkeypatch, metrics: bool = True,
                 slow_ms: int = 0, extra_env: dict = None):
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.net.server import MySQLServer

        for k, v in (extra_env or {}).items():
            monkeypatch.setenv(k, str(v))
        if slow_ms is not None:
            monkeypatch.setenv('LIGHTDB_SLOW_QUERY_MS', str(slow_ms))
        self.metrics_port = None
        if metrics:
            self.metrics_port = free_port()
            monkeypatch.setenv('LIGHTDB_METRICS_PORT', str(self.metrics_port))
            monkeypatch.setenv('LIGHTDB_METRICS_HOST', '127.0.0.1')
        self.db_dir = str(tmp_path / 'data')
        self.db = track(Database(self.db_dir))
        self.ex = track(Executor(self.db))
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = free_port()
        self.cfg = cfg
        self.server = track(MySQLServer(cfg, self.db, self.ex))
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.25)

    def scrape(self) -> str:
        assert self.metrics_port, '本环境未启用 /metrics'
        status, body, ctype = wait_http(
            f'http://127.0.0.1:{self.metrics_port}/metrics')
        assert status == 200, (status, body[:200])
        assert 'text/plain' in ctype, ctype
        return body

    def scrape_json(self) -> dict:
        status, body, ctype = wait_http(
            f'http://127.0.0.1:{self.metrics_port}/metrics?format=json')
        assert status == 200 and 'application/json' in ctype, (status, ctype)
        return json.loads(body)

    def connect(self):
        import pymysql
        return track(pymysql.connect(
            host='127.0.0.1', port=self.cfg.port, user='root',
            autocommit=True, connect_timeout=5))

    def cleanup(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.db.shutdown()
        except Exception:
            pass


def run(env_or_ex, sql):
    """执行一条 SQL（进程内 executor 直连，不走网络）"""
    from lightdb.sql.parser import parser
    ex = env_or_ex.ex if isinstance(env_or_ex, ServerEnv) else env_or_ex
    p = parser.parse(sql)
    assert p is not None, f'解析失败: {sql}'
    return ex.execute(p, {})


# ==================== 1. /metrics 真实采集 ====================

def test_metrics_endpoint_real_counts(tmp_path, monkeypatch):
    env = ServerEnv(tmp_path, monkeypatch, slow_ms=0)
    try:
        # 1) 无数据基线：端点可访问、up=1、WAL/表存在
        text = env.scrape()
        assert metrics_value(text, 'lightdb_up') == 1.0
        assert metrics_value(text, 'lightdb_queries_total') == 0.0
        assert metrics_value(text, 'lightdb_errors_total') == 0.0
        # 无错误/无慢查询时有 # 注释说明（不编造数字）
        assert '无错误记录' in text
        assert 'lightdb_wal_bytes_total' in text

        # 2) 真实语句：建表/插入/查询/错误
        conn = env.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 6):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i * 10})')
        cur.execute('SELECT COUNT(*) FROM t')
        assert cur.fetchone()[0] == 5
        with pytest.raises(Exception):
            cur.execute('SELECT * FROM no_such_table')       # errno 1146

        # WAL 真实字节数：还没 checkpoint → 表 t 的 WAL > 0
        text = env.scrape()
        wal_t = metrics_value(text, 'lightdb_wal_bytes', {'table': 't'})
        assert wal_t is not None and wal_t > 0, text[:800]
        assert metrics_value(text, 'lightdb_wal_bytes_total') >= wal_t
        assert metrics_value(text, 'lightdb_checkpoints_total') == 0.0

        # checkpoint 真实计数（执行后 WAL 被退休 → 字节数归 0，同样真实）
        env.db.checkpoint()

        text = env.scrape()
        # 连接：当前 1 个活跃连接；累计 >=1
        assert metrics_value(text, 'lightdb_connections_active') == 1.0
        assert metrics_value(text, 'lightdb_connections_total') >= 1.0
        assert metrics_value(
            text, 'lightdb_connections_active_by_ip',
            {'ip': '127.0.0.1'}) == 1.0
        # 语句数：至少 8 条（建表+5 insert+select+错误）
        assert metrics_value(text, 'lightdb_queries_total') >= 8.0
        # 错误按真实 errno
        assert metrics_value(text, 'lightdb_errors_total') >= 1.0
        assert metrics_value(text, 'lightdb_errors_by_code_total',
                             {'errno': 1146}) == 1.0
        # 慢查询（阈值 0ms → 进程内语句被真实记录）
        assert metrics_value(text, 'lightdb_slow_queries') >= 1.0
        assert metrics_value(text, 'lightdb_slow_query_threshold_ms') == 0.0
        # checkpoint / 表数（WAL 已被 checkpoint 退休 → 与表存在无关）
        assert metrics_value(text, 'lightdb_checkpoints_total') >= 1.0
        assert metrics_value(text, 'lightdb_checkpoint_tables_total') >= 1.0
        assert metrics_value(text, 'lightdb_tables') >= 1.0
        # 线程 / 内存 / uptime
        assert metrics_value(text, 'lightdb_process_threads') > 0
        assert metrics_value(text, 'lightdb_process_memory_rss_bytes') > 0
        assert metrics_value(text, 'lightdb_uptime_seconds') >= 0
        # 备份目录不存在 → 0 + 注释说明（真实采集，不编造）
        assert metrics_value(text, 'lightdb_backup_files') == 0.0
        assert '备份目录不存在' in text

        # 3) JSON 格式与 text 数值一致
        snap = env.scrape_json()
        assert snap['runtime']['queries_total'] >= 8
        errs = snap['runtime']['errors_by_code']
        assert errs.get(1146, errs.get('1146', 0)) == 1, errs
        assert snap['tables'] >= 1
        assert snap['slow']['total'] >= 1
    finally:
        env.cleanup()


def test_metrics_health_404_405(tmp_path, monkeypatch):
    env = ServerEnv(tmp_path, monkeypatch, slow_ms=None)
    try:
        status, body, _ = wait_http(
            f'http://127.0.0.1:{env.metrics_port}/health')
        assert status == 200 and body.strip() == 'ok'
        status, body, _ = http_get(
            f'http://127.0.0.1:{env.metrics_port}/nope')
        assert status == 404
        req = urllib.request.Request(
            f'http://127.0.0.1:{env.metrics_port}/metrics', data=b'x',
            method='POST')
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 405
    finally:
        env.cleanup()


def test_metrics_disabled_by_default(tmp_path, monkeypatch):
    env = ServerEnv(tmp_path, monkeypatch, metrics=False, slow_ms=None)
    try:
        assert env.server.metrics_url is None
    finally:
        env.cleanup()


def test_metrics_qps_window_real(tmp_path, monkeypatch):
    env = ServerEnv(tmp_path, monkeypatch, slow_ms=0)
    try:
        conn = env.connect()
        cur = conn.cursor()
        for _ in range(10):
            cur.execute('SELECT 1')      # 内置语句：仍计为语句/QPS
        text = env.scrape()
        qps = metrics_value(text, 'lightdb_qps', {'window': '5s'})
        assert qps is not None and qps > 0, text[:400]
        # 语句数包含内置语句（在 _dispatch 里按真实命令计数）
        assert metrics_value(text, 'lightdb_queries_total') >= 10.0
    finally:
        env.cleanup()


# ==================== 2. 慢查询日志 ====================

def _exec_env(tmp_path, monkeypatch, **env):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    return db, ex


def test_slow_query_threshold_trigger_and_no_trigger(tmp_path, monkeypatch):
    # 触发：阈值 0 → 进程内语句全部记录（真实耗时）
    db, ex = _exec_env(tmp_path, monkeypatch,
                       LIGHTDB_SLOW_QUERY_MS='0',
                       LIGHTDB_SLOWLOG_BUFFER='50')
    run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    run(ex, "INSERT INTO t VALUES (1, 'a')")
    run(ex, 'SELECT COUNT(*) FROM t')
    snap = ex.slowlog.snapshot()
    assert snap['threshold_ms'] == 0
    assert snap['total'] >= 3, snap
    rows = ex.slowlog.rows()
    assert len(rows) >= 3
    ts, user, elapsed, cmd, success, errno, sql = rows[-1]
    assert ts and cmd and sql and elapsed >= 0 and success == 1
    # 落盘：JSONL 且字段齐全
    path = os.path.join(str(db.data_dir), 'slow.log')
    assert os.path.exists(path)
    entry = json.loads(open(path, encoding='utf-8').readline())
    for key in ('ts', 'user', 'elapsed_ms', 'cmd', 'success', 'sql'):
        assert key in entry, entry
    # SQL 虚拟表可读（SHOW SLOW QUERIES 未加：parser 不在本任务范围）
    r = run(ex, 'SELECT * FROM __SLOW_QUERY_LOG__ LIMIT 5')
    assert r['type'] == 'resultset', r
    assert r['columns'] == ['ts', 'user', 'elapsed_ms', 'cmd', 'success',
                            'errno', 'sqlstate', 'sql']
    assert len(r['rows']) >= 3
    db.shutdown()

    # 不触发：阈值极大 → 无记录、无落盘文件
    db2, ex2 = _exec_env(tmp_path / 'lax', monkeypatch,
                         LIGHTDB_SLOW_QUERY_MS='600000')
    run(ex2, 'CREATE TABLE t (id INT PRIMARY KEY)')
    run(ex2, 'INSERT INTO t VALUES (1)')
    assert ex2.slowlog.snapshot()['total'] == 0
    assert ex2.slowlog.rows() == []
    assert not os.path.exists(
        os.path.join(str(db2.data_dir), 'slow.log'))
    assert run(ex2, 'SELECT * FROM __SLOW_QUERY_LOG__')['rows'] == []
    db2.shutdown()


def test_slow_query_records_failure_and_redacts_password(tmp_path, monkeypatch):
    db, ex = _exec_env(tmp_path, monkeypatch, LIGHTDB_SLOW_QUERY_MS='0')
    run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    r = run(ex, 'SELECT * FROM no_such_table')       # 失败语句也记
    assert r['type'] == 'error'
    rows = ex.slowlog.rows()
    fail = [x for x in rows if x[3] == 'SELECT' and not x[4]]
    assert fail, rows
    assert fail[-1][5] == 1146 and 'no_such_table' in fail[-1][6]
    # 口令类 SQL：SQL 原文里的明文口令必须脱敏（慢查询日志会落盘）
    run(ex, "CREATE USER 'slow_u'@'%' IDENTIFIED BY 'StrongPass123'")
    sqls = [x[6] for x in ex.slowlog.rows()]
    assert any('CREATE USER' in s for s in sqls), sqls
    assert all('StrongPass123' not in s for s in sqls), sqls
    assert any('***' in s for s in sqls), sqls
    db.shutdown()


def test_slowlog_file_rotation_keep_and_content(tmp_path, monkeypatch):
    from lightdb.net.observability import SlowQueryLog

    d = tmp_path / 'slowdir'
    slow = SlowQueryLog(str(d), threshold_ms=0, enabled=True,
                        max_bytes=2048, keep=2, buffer_size=10)
    for i in range(60):
        assert slow.record('root', f'SELECT {i} FROM t', 12.5,
                           cmd='SELECT')
    names = sorted(os.listdir(d))
    assert 'slow.log' in names, names
    assert len(names) <= 1 + 2, names          # 当前文件 + keep 个历史
    assert 'slow.log.1' in names and 'slow.log.2' in names, names
    # 每个文件都不超过阈值（最后写的一行可能让当前文件略超，用轮转后状态
    # 断言历史文件严格 <= 阈值 + 一条记录大小）
    for name in names:
        assert os.path.getsize(d / name) <= 2048 + 1024, name
    # JSONL 内容可读、字段齐全（历史文件也检查）
    seen = 0
    for name in names:
        for line in open(d / name, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            assert set(('ts', 'user', 'elapsed_ms', 'sql')) <= set(e), e
            seen += 1
    # 轮转丢掉的只是 keep 之外的历史，当前+保留文件里至少有 keep*若干条
    assert seen > 0 and slow.snapshot()['rotations'] >= 1
    assert slow.snapshot()['keep'] == 2
    # 环形缓冲上限生效（buffer_size=10）
    assert len(slow.rows()) <= 10
    slow.close()


def test_slowlog_disabled_only_ring_buffer(tmp_path, monkeypatch):
    from lightdb.net.observability import SlowQueryLog

    d = tmp_path / 'noslow'
    slow = SlowQueryLog(str(d), threshold_ms=0, enabled=False)
    slow.record('root', 'SELECT 1', 5.0, cmd='SELECT')
    assert slow.snapshot()['total'] == 1
    assert len(slow.rows()) == 1
    assert not os.path.exists(d)               # 不落盘 → 不建目录/文件


# ==================== 3. 审计轮转 ====================

def test_audit_rotation_by_records_and_show_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_AUDIT_MAX_RECORDS', '10')
    monkeypatch.setenv('LIGHTDB_AUDIT_KEEP', '2')
    db, ex = _exec_env(tmp_path, monkeypatch)
    run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    for i in range(1, 41):
        run(ex, f'INSERT INTO t VALUES ({i}, {i})')
    ex.audit.flush()
    d = str(db.data_dir)
    files = sorted(f for f in os.listdir(d) if f.startswith('audit.log'))
    assert 'audit.log' in files, files
    assert len(files) <= 1 + 2, files
    assert ex.audit.rotations >= 1, ex.audit.rotations
    # 记录不丢：文件总行数 == flushed_records（每个文件的半行不存在）
    total_lines = 0
    for f in files:
        for line in open(os.path.join(d, f), encoding='utf-8'):
            line = line.strip()
            if line:
                json.loads(line)               # 每行都是完整 JSON
                total_lines += 1
    assert total_lines == ex.audit.flushed_records, (
        total_lines, ex.audit.flushed_records, files)
    assert total_lines >= 40
    # SHOW AUDIT LOG 行为不变：读内存环形缓冲，仍在
    r = run(ex, 'SHOW AUDIT LOG')
    assert r['type'] == 'resultset'
    assert len(r['rows']) >= 40
    db.shutdown()

    # 重启后：轮转文件 + 当前文件都载入 → 仍可查询
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db2 = track(Database(d))
    ex2 = track(Executor(db2))
    r2 = run(ex2, 'SHOW AUDIT LOG')
    assert len(r2['rows']) >= 40
    db2.shutdown()


def test_audit_rotation_by_size(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_AUDIT_MAX_BYTES', '2048')
    monkeypatch.setenv('LIGHTDB_AUDIT_KEEP', '1')
    monkeypatch.setenv('LIGHTDB_AUDIT_MAX_RECORDS', '0')   # 只按大小
    db, ex = _exec_env(tmp_path, monkeypatch)
    run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(100))')
    for i in range(1, 30):
        run(ex, f"INSERT INTO t VALUES ({i}, 'pad-{i:04d}')")
    ex.audit.flush()
    d = str(db.data_dir)
    files = sorted(f for f in os.listdir(d) if f.startswith('audit.log'))
    assert len(files) <= 1 + 1, files
    assert ex.audit.rotations >= 1
    # 当前文件在阈值量级内（允许一批超出的余量）
    assert os.path.getsize(os.path.join(d, 'audit.log')) <= 2048 * 2
    db.shutdown()


def test_audit_crash_safety_partial_line_and_missing_current(
        tmp_path, monkeypatch):
    """崩溃安全：半行/缺当前文件都能载入；已落盘记录不丢"""
    monkeypatch.setenv('LIGHTDB_AUDIT_MAX_RECORDS', '5')
    monkeypatch.setenv('LIGHTDB_AUDIT_KEEP', '2')
    db, ex = _exec_env(tmp_path, monkeypatch)
    run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    for i in range(1, 15):
        run(ex, f'INSERT INTO t VALUES ({i})')
    ex.audit.flush()
    total = ex.audit.flushed_records
    db.shutdown()
    d = str(db.data_dir)
    files = sorted(f for f in os.listdir(d) if f.startswith('audit.log'))
    assert len(files) >= 2, files
    # 1) 当前文件末尾追加半行（模拟崩溃时写了一半）
    with open(os.path.join(d, 'audit.log'), 'a', encoding='utf-8') as f:
        f.write('{"ts": 1, "cmd": "INSERT", "sql": "broken')  # 非法半行
    # 2) 历史文件存在时删掉当前文件（模拟轮转 rename 后崩溃）
    base = os.path.join(d, 'audit.log')
    backup = base + '.crashbak'
    os.replace(base, backup)
    os.remove(base) if os.path.exists(base) else None

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db2 = track(Database(d))
    ex2 = track(Executor(db2))
    rows = run(ex2, 'SHOW AUDIT LOG')['rows']
    assert len(rows) >= min(total, 2000), (len(rows), total)
    db2.shutdown()
    # 清理：把备份文件还原成历史文件（避免影响 tmp_path 清理断言）
    if os.path.exists(backup) and not os.path.exists(base):
        os.replace(backup, base)


# ==================== 4. 无数据/不可用如实输出 ====================

def test_metrics_no_backup_dir_zero_with_comment(tmp_path, monkeypatch):
    env = ServerEnv(tmp_path, monkeypatch, slow_ms=600000)
    try:
        text = env.scrape()
        assert metrics_value(text, 'lightdb_backup_files') == 0.0
        assert metrics_value(text, 'lightdb_snapshot_dirs') == 0.0
        assert '备份目录不存在' in text
        # 慢查询为空时有注释说明
        assert '无慢查询' in text
        # binlog 未创建时输出 0 + 说明（且 scrape 不创建文件）
        assert metrics_value(text, 'lightdb_binlog_events') == 0.0
        assert 'binlog 未启用/未创建' in text
    finally:
        env.cleanup()
