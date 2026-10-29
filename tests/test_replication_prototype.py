# -*- coding: utf-8 -*-
"""生产化 B（task-71）：binlog 复制原型

覆盖：
1. `SHOW BINARY LOGS`（server 内建）真实文件清单/大小/当前文件；binlog 未启用
   时返回空结果集（不编造）。
2. 全量单向复制：主库 CREATE/INSERT/UPDATE/DELETE → 目标库 apply → 数据一致；
   DDL 也走同一条链路（幂等预判：CREATE 已存在/TRUNCATE 重放）。
3. 断点续传：状态文件持久化；新实例从持久 lsn 继续，只拉增量；重复运行
   caught_up=True 且不重复应用。
4. 幂等：同一事件重放（INSERT 主键冲突、UPDATE/DELETE affected=0、
   CREATE TABLE 已存在）返回 idempotent 而非报错；表不存在/未知事件类型/
   损坏 JSON/列数非法 → ReplicaApplyError 显式。
5. 复制 1k 事件延迟量化（打印 events_per_sec；不设门禁）。
6. CLI `scripts/replica_apply.py`：--help 可运行；端到端一次性 apply 三类事件
   并用 --json 输出摘要。

原型限制（单主/无 failover/单线程 apply）在 docs/PRODUCTION_READINESS.md。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # pragma: no cover
    from conftest import track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPL_ENV = ('LIGHTDB_BINLOG_ENABLED', 'LIGHTDB_BINLOG_MAX_BYTES',
            'LIGHTDB_POOL_ENABLED')


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in REPL_ENV:
        monkeypatch.delenv(key, raising=False)
    # 本文件专注复制链路：关掉连接池减少背景线程噪声（连接池另有测试）
    monkeypatch.setenv('LIGHTDB_POOL_ENABLED', '0')
    yield


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_until(pred, timeout=30.0, interval=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _fetch(db, table):
    """读取目标库某表全部行（Table.select 返回 (cols, rows)）"""
    _cols, rows = db.get_table(table).select(['*'], None)
    return sorted(tuple(r) for r in rows)


class MasterEnv:
    """主库：Database + Executor + 真实 MySQLServer（binlog 默认开启）"""

    def __init__(self, tmp_path, monkeypatch, env: dict = None):
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.net.server import MySQLServer
        from lightdb.sql.executor import Executor

        for k, v in (env or {}).items():
            monkeypatch.setenv(k, str(v))
        self.db_dir = str(tmp_path / 'master')
        self.db = track(Database(self.db_dir))
        self.ex = track(Executor(self.db))
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = free_port()
        self.cfg = cfg
        self.server = track(MySQLServer(cfg, self.db, self.ex))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def connect(self, **kw):
        import pymysql
        kw.setdefault('host', '127.0.0.1')
        kw.setdefault('port', self.cfg.port)
        kw.setdefault('user', 'root')
        kw.setdefault('autocommit', True)
        kw.setdefault('connect_timeout', 5)
        return pymysql.connect(**kw)

    def cleanup(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.db.shutdown()
        except Exception:
            pass

    def rows(self, table):
        cols, rows = self.db.get_table(table).select(['*'], None)
        return sorted(tuple(r) for r in rows)


def _target(tmp_path, monkeypatch, name='replica'):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = track(Database(str(tmp_path / name)))
    track(Executor(db))
    return db


def _proto(master, target_db, state_path=None):
    from lightdb.net.replica_proto import ReplicaPrototype
    return ReplicaPrototype('127.0.0.1', master.cfg.port, target_db,
                            state_path=state_path)


# ==================== 1. SHOW BINARY LOGS ====================

def test_show_binary_logs_real_files(tmp_path, monkeypatch):
    """复用既有 E3 实现：2 列（Log_name/File_size），binlog 未启用 1381"""
    import pymysql
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        # 列契约沿用 E3 实现：2 列（Log_name/File_size）；无写入时
        # binlog 惰性创建 → 可能返回空行或 size=0 的当前文件，不编造数据
        cur.execute('SHOW BINARY LOGS')
        assert [d[0] for d in cur.description] == ['Log_name', 'File_size']
        early = cur.fetchall()
        assert all(len(r) == 2 and int(r[1]) >= 0 for r in early), early

        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO t VALUES (1, 10)')
        cur.execute('SHOW MASTER STATUS')
        status = cur.fetchone()
        cur.execute('SHOW BINARY LOGS')
        assert [d[0] for d in cur.description] == ['Log_name', 'File_size']
        logs = cur.fetchall()
        assert logs, logs
        names = [r[0] for r in logs]
        assert names == sorted(names)
        assert names[0].startswith('binlog.'), names
        assert status[0] in names, (status, names)
        current = [r for r in logs if r[0] == status[0]][0]
        assert int(current[1]) > 0
        # 原型消费同一接口：二进制日志清单/大小
        proto = _proto(master, _target(tmp_path, monkeypatch, 'probe_target'))
        plogs = proto.binary_logs()
        assert [x['name'] for x in plogs] == names
        assert all(x['size'] > 0 for x in plogs)
        conn.close()
    finally:
        master.cleanup()


def test_show_binary_logs_disabled_errors_1381(tmp_path, monkeypatch):
    import pymysql
    master = MasterEnv(tmp_path, monkeypatch,
                       {'LIGHTDB_BINLOG_ENABLED': '0'})
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
        cur.execute('INSERT INTO t VALUES (1)')
        with pytest.raises(pymysql.err.MySQLError) as ei:
            cur.execute('SHOW BINARY LOGS')
        assert ei.value.args[0] == 1381, ei.value
        conn.close()
    finally:
        master.cleanup()


# ==================== 2. 全量单向复制 ====================

def test_full_sync_ddl_and_dml(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
        cur.executemany('INSERT INTO t VALUES (%s, %s)',
                        [(i, f'v{i}') for i in range(1, 11)])
        cur.execute("UPDATE t SET v='updated' WHERE id=3")
        cur.execute('DELETE FROM t WHERE id=7')
        conn.close()

        proto = _proto(master, target)
        stats = proto.run_until_caught_up(batch=200)
        assert stats['caught_up'] is True, stats
        assert stats['applied'] >= 11, stats       # 1 DDL + 10 INSERT+UPD+DEL
        assert 't' in target.tables
        assert len(_fetch(target, 't')) == 9
        assert _fetch(target, 't') == master.rows('t')
        assert proto.state['lsn'] > 0 and proto.state['master_file']
        # 状态文件已持久化
        assert os.path.exists(proto.state_path)
        st = json.loads(open(proto.state_path, encoding='utf-8').read())
        assert st['lsn'] == proto.state['lsn'] and st['version'] == 1
    finally:
        master.cleanup()


# ==================== 3. 断点续传 / 幂等 ====================

def test_resume_from_persisted_position(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 6):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')

        proto1 = _proto(master, target)
        s1 = proto1.run_until_caught_up(batch=100)
        assert s1['events'] >= 6
        lsn_after_first = proto1.state['lsn']

        # 主库继续写；新实例（模拟进程重启）从持久位点继续
        for i in range(6, 11):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()

        proto2 = _proto(master, target)
        assert proto2.state['lsn'] == lsn_after_first
        s2 = proto2.run_until_caught_up(batch=100)
        assert s2['events'] == 5, s2                 # 只拉增量
        assert s2['applied'] == 5, s2
        assert _fetch(target, 't') == master.rows('t')

        # 追平后再跑：无新事件、不重复应用
        proto3 = _proto(master, target)
        s3 = proto3.run_until_caught_up(batch=100)
        assert s3['events'] == 0 and s3['caught_up'] is True
        assert proto3.state['applied'] == proto2.state['applied']
    finally:
        master.cleanup()


def test_idempotent_replay_dml_and_ddl(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO t VALUES (1, 10)')
        cur.close()
        conn.close()

        proto = _proto(master, target)
        events = proto.fetch_events(0, 100)
        assert events
        # 首次应用全部事件
        for ev in events:
            proto.apply_event(ev)
        assert _fetch(target, 't') == [(1, 10)]
        # 重放同一批事件：全部幂等，不报错、不重复插入
        for ev in events:
            res = proto.apply_event(ev)
            assert res['result'] in ('idempotent', 'applied'), res
        rows = _fetch(target, 't')
        assert rows == [(1, 10)], rows
        assert proto.state['idempotent'] >= 1, proto.state
    finally:
        master.cleanup()


# ==================== 4. 损坏/非法事件显式 ====================

def test_corrupt_events_explicit(tmp_path, monkeypatch):
    from lightdb.net.replica_proto import ReplicaApplyError, ReplicaError

    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
        conn.close()
        proto = _proto(master, target)

        cases = [
            (1, 1, 0, 'MYSTERY', None, '{}'),          # 未知事件类型
            (1, 1, 0, 'INSERT', 't', '{not json'),     # payload 损坏
            (1,),                                      # 列数非法
            (1, 1, 0, 'INSERT', 't', '"just a string"'),  # 非 JSON 对象
        ]
        for bad in cases:
            with pytest.raises(ReplicaApplyError):
                proto.apply_event(bad)
        # 目标缺表：显式（不是静默跳过）
        with pytest.raises(ReplicaApplyError):
            proto.apply_event((1, 1, 0, 'INSERT', 'no_such', '{"row": [1]}'))
        # 主库不可达：ReplicaError（无自动 failover）
        from lightdb.net.replica_proto import ReplicaPrototype
        bad_proto = ReplicaPrototype('127.0.0.1', free_port(), target,
                                     connect_timeout=1)
        with pytest.raises(ReplicaError):
            bad_proto.master_status()
        # 状态文件损坏：拒绝从不确定位点继续
        state = os.path.join(str(target.data_dir), 'bad_state.json')
        open(state, 'w', encoding='utf-8').write('{broken json')
        with pytest.raises(ReplicaError):
            _proto(master, target, state_path=state)
    finally:
        master.cleanup()


def test_ddl_apply_failure_explicit(tmp_path, monkeypatch):
    from lightdb.net.replica_proto import ReplicaApplyError

    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        proto = _proto(master, target)
        # ALTER 目标表不存在：显式失败（DDL 顺序错误）
        with pytest.raises(ReplicaApplyError):
            proto.apply_event((1, 100, 0, 'DDL', None,
                               json.dumps({'sql': 'ALTER TABLE no_such '
                                                  'ADD COLUMN x INT'})))
        # 真正生效的 DDL 正常 apply
        proto.apply_event((2, 200, 0, 'DDL', None,
                           json.dumps({'sql': 'CREATE TABLE ok '
                                              '(id INT PRIMARY KEY)'})))
        assert 'ok' in target.tables
    finally:
        master.cleanup()


# ==================== 5. 1k 事件延迟量化 ====================

def test_1k_events_replication_latency(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        t0 = time.perf_counter()
        for i in range(1, 1001):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        write_s = time.perf_counter() - t0
        conn.close()

        proto = _proto(master, target)
        stats = proto.run_until_caught_up(batch=500)
        apply_s = stats['elapsed_ms'] / 1000.0
        print(f'REPL_1K write={write_s:.3f}s apply={apply_s:.3f}s '
              f'events={stats["events"]} applied={stats["applied"]} '
              f'rate={stats["events_per_sec"]} ev/s')
        assert stats['events'] >= 1000, stats
        assert stats['applied'] >= 1000, stats
        rows = target.get_table('t').select(['*'], None)[1]
        assert len(rows) == 1000
        # 不设硬门禁（机器负载相关），但给一个宽松上界说明"不是明显失控"
        assert apply_s < 120, apply_s
    finally:
        master.cleanup()


# ==================== 6. CLI ====================

def test_cli_help_runs():
    script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
    proc = subprocess.run([sys.executable, script, '--help'],
                          capture_output=True, text=True, timeout=60,
                          encoding='utf-8', errors='replace')
    assert proc.returncode == 0, proc.stderr
    assert 'replica_apply' in (proc.stdout + proc.stderr)
    assert '--target-data-dir' in proc.stdout


def test_cli_end_to_end_json(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
        cur.executemany('INSERT INTO t VALUES (%s, %s)',
                        [(i, f'v{i}') for i in range(1, 4)])
        cur.execute("UPDATE t SET v='u2' WHERE id=2")
        cur.execute('DELETE FROM t WHERE id=3')
        conn.close()

        target_dir = str(tmp_path / 'cli_replica')
        state = os.path.join(target_dir, 'state.json')
        script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
        proc = subprocess.run(
            [sys.executable, script,
             '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir,
             '--state-file', state,
             '--json'],
            capture_output=True, text=True, timeout=180,
            encoding='utf-8', errors='replace',
            cwd=ROOT,
            env={**os.environ,
                 'LIGHTDB_ALLOW_EMPTY_PASSWORD': '1',
                 'LIGHTDB_POOL_ENABLED': '0'})
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out['summary']['events'] >= 5, out
        assert out['summary']['applied'] >= 5
        assert os.path.exists(state)
        # 目标数据目录里表已建好、数据一致
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        db = track(Database(target_dir))
        track(Executor(db))
        rows = _fetch(db, 't')
        assert rows == [(1, 'v1'), (2, 'u2')], rows
        db.shutdown()
    finally:
        master.cleanup()


# ==================== 7. 状态快照 / SHOW SLAVE STATUS 一致性（task-81） ====================

def test_show_status_snapshot_matches_state_file(tmp_path, monkeypatch):
    """原型注册后：SHOW SLAVE STATUS 的位点/延迟与状态文件一致（task-81）"""
    from lightdb.net.replica_proto import ReplicaPrototype

    master = MasterEnv(tmp_path, monkeypatch)
    target = _target(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 6):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()

        proto = track(ReplicaPrototype('127.0.0.1', master.cfg.port,
                                       target))
        stats = proto.run_until_caught_up(batch=100)
        assert stats['caught_up'] is True
        # db.replication_status() 即 show_status() 快照（同一份数据）
        status = target.replication_status()
        assert status is not None
        assert status['Replica_Is_Prototype'] is True
        assert status['Replica_Applied_LSN'] == proto.state['lsn']
        assert status['Replica_Lag_LSN'] == 0
        assert status['Replica_Lag_Seconds'] == 0
        assert status['Replica_Caught_Up'] == 1
        # 校验和存在且可重算
        raw = json.loads(open(proto.state_path, encoding='utf-8').read())
        assert raw['checksum'] == ReplicaPrototype._state_checksum(raw)
        # SHOW SLAVE STATUS 的延迟/位点取自同一份状态
        from lightdb.sql.parser import parser
        r = proto.executor.execute(parser.parse('SHOW SLAVE STATUS'), {})
        assert r['type'] == 'resultset'
        row = dict(zip(r['columns'], r['rows'][0]))
        assert row['Exec_Master_Log_Pos'] == proto.state['lsn']
        assert row['Read_Master_Log_Pos'] == proto.state['lsn']
        assert row['Master_Log_File'] == proto.state['master_file']
        assert row['Seconds_Behind_Master'] == 0.0
        assert row['Last_Errno'] == 0
        # 无数据（新 proto、未运行）时数值列 0，不编造
        fresh = track(ReplicaPrototype('127.0.0.1', master.cfg.port,
                                       _target(tmp_path, monkeypatch,
                                               'fresh_target')))
        st = fresh.show_status()
        assert st['Replica_Applied_LSN'] == 0
        assert st['Replica_Lag_LSN'] == 0
        assert st['Seconds_Behind_Master'] == 0.0
        assert st['Last_Errno'] == 0
    finally:
        master.cleanup()
