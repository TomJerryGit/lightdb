# -*- coding: utf-8 -*-
"""生产化：增量复制（task-81）——持续追平 / 指标 / 断点续传 / 显式错误

覆盖：
1. `--follow` 持续增量：主库持续写入 → 从库追平且数据一致；
   `/metrics` 的 replica 指标真实变化（applied_events/lag/last_apply_ts），
   追平后 lag=0；`SHOW SLAVE STATUS` 的位点/延迟与状态文件一致。
2. 失败保留断点（不跳过）：apply 失败 → 显式 ReplicaApplyError、lsn 不前进、
   errors 落盘；修复目标 schema 后从同一位点继续追平。
3. 瞬态失败重试：follow 对可重试错误按退避重试，不丢事件、不跳位点。
4. binlog 位点缺口：显式 `ReplicaBinlogGap`（errno 1236，MySQL 风格）+
   恢复策略；`--reset-position` 显式推进位点（不自动跳过）。
5. 中途杀进程：CLI `--follow` 子进程在 apply 中途被 kill → 状态文件原子/
   校验通过、断点有效；重启 CLI 从断点续传、数据一致、无重复。
6. 状态文件原子写 + checksum 校验（篡改/损坏显式拒绝）。
7. 1k 事件增量延迟量化（打印，不设硬门禁）。
8. 未配置复制时 `/metrics` replica 指标输出 0 + 注释。

原型限制（单主、无自动 failover、单线程 apply）见 docs/PRODUCTION_READINESS.md。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
    from tests.test_replication_prototype import (
        MasterEnv, _fetch, _proto, _target)
except ImportError:                       # pragma: no cover - 直接运行
    from conftest import track
    from test_replication_prototype import (
        MasterEnv, _fetch, _proto, _target)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_KEYS = ('LIGHTDB_METRICS_PORT', 'LIGHTDB_METRICS_HOST',
            'LIGHTDB_POOL_ENABLED', 'LIGHTDB_BINLOG_ENABLED')


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LIGHTDB_POOL_ENABLED', '0')   # 减少背景线程噪声
    yield


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_until(pred, timeout=30.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def metrics_text(port: int) -> str:
    return urllib.request.urlopen(
        f'http://127.0.0.1:{port}/metrics', timeout=10).read().decode()


def metric_value(text: str, name: str) -> float:
    for line in text.splitlines():
        if line.startswith(name + ' '):
            return float(line[len(name) + 1:])
    return 0.0


class TargetEnv:
    """目标节点（从库侧）：Database + Executor + MySQLServer(/metrics 动态端口)"""

    def __init__(self, tmp_path, monkeypatch, name='replica'):
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.net.server import MySQLServer
        from lightdb.sql.executor import Executor

        monkeypatch.setenv('LIGHTDB_METRICS_PORT', '0')
        monkeypatch.setenv('LIGHTDB_METRICS_HOST', '127.0.0.1')
        self.db_dir = str(tmp_path / name)
        self.db = track(Database(self.db_dir))
        self.ex = track(Executor(self.db))
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = free_port()
        self.cfg = cfg
        self.server = track(MySQLServer(cfg, self.db, self.ex))
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        time.sleep(0.2)
        self.metrics_port = int(
            self.server.metrics_server.server_address[1])
        self._conn = None

    def connect(self):
        import pymysql
        if self._conn is None:
            self._conn = track(pymysql.connect(
                host='127.0.0.1', port=self.cfg.port, user='root',
                autocommit=True, connect_timeout=5))
        return self._conn

    def show_slave_status(self) -> dict:
        cur = self.connect().cursor()
        cur.execute('SHOW SLAVE STATUS')
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))

    def metrics(self) -> str:
        return metrics_text(self.metrics_port)

    def cleanup(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.db.shutdown()
        except Exception:
            pass


def _write_rows(master, start, end):
    conn = master.connect()
    cur = conn.cursor()
    for i in range(start, end + 1):
        cur.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")


# ==================== 1. 持续写入 → 追平一致 + 指标/SHOW 一致 ====================

def test_follow_continuous_writes_catch_up_and_consistent(
        tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    stop = threading.Event()
    values = {}
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
        conn.close()

        from lightdb.net.replica_proto import ReplicaPrototype
        proto = track(ReplicaPrototype('127.0.0.1', master.cfg.port,
                                       target.db))
        state_path = proto.state_path
        th = threading.Thread(
            target=lambda: values.setdefault('follow', proto.follow(
                interval=0.02, batch=100, stop_event=stop,
                retry_backoff=0.02, max_backoff=0.1)),
            daemon=True)
        th.start()
        # 主库分三波持续写入（follow 同时进行）
        for wave in range(3):
            _write_rows(master, wave * 20 + 1, (wave + 1) * 20)
            time.sleep(0.25)

        assert wait_until(
            lambda: len(_fetch(target.db, 't')) == 60, timeout=20), \
            len(_fetch(target.db, 't'))
        assert wait_until(lambda: proto._caught_up, timeout=10)
        # 指标真实变化：追平后 lag=0、applied_events>=60、last_apply_ts>0
        text = target.metrics()
        applied_events = metric_value(
            text, 'lightdb_replica_applied_events_total')
        assert applied_events >= 60, applied_events
        assert metric_value(text, 'lightdb_replica_lag_lsn') == 0
        assert metric_value(text, 'lightdb_replica_lag_seconds') == 0
        assert metric_value(text, 'lightdb_replica_events_behind') == 0
        assert metric_value(text, 'lightdb_replica_caught_up') == 1
        assert metric_value(text, 'lightdb_replica_last_apply_ts') > 0
        assert metric_value(
            text, 'lightdb_replica_apply_errors_total') == 0
        # SHOW SLAVE STATUS：位点与状态文件一致、延迟 0、无错误
        row = target.show_slave_status()
        assert row['Master_Log_File'] == proto.state['master_file']
        assert row['Exec_Master_Log_Pos'] == proto.state['lsn']
        assert row['Read_Master_Log_Pos'] == proto.state['lsn']
        assert row['Seconds_Behind_Master'] == 0.0
        assert row['Last_Errno'] == 0 and row['Last_IO_Errno'] == 0
        assert row['Slave_IO_Running'] == 'Yes'
        assert row['Slave_SQL_Running'] == 'Yes'
        assert row['Slave_IO_State'].startswith('Replica polling')
        # 数据一致
        assert _fetch(target.db, 't') == master.rows('t')
    finally:
        stop.set()
        time.sleep(0.1)
        target.cleanup()
        master.cleanup()


def test_metrics_replica_zero_when_not_configured(tmp_path, monkeypatch):
    target = TargetEnv(tmp_path, monkeypatch, name='plain')
    try:
        text = target.metrics()
        assert metric_value(
            text, 'lightdb_replica_applied_events_total') == 0
        assert metric_value(text, 'lightdb_replica_lag_lsn') == 0
        assert metric_value(text, 'lightdb_replica_last_apply_ts') == 0
        assert '增量复制未配置/无数据' in text
        # 未注册原型：SHOW SLAVE STATUS 为真实"未配置复制"
        row = target.show_slave_status()
        assert row['Slave_IO_State'] == 'No replication configured'
        assert row['Exec_Master_Log_Pos'] is None
        assert row['Seconds_Behind_Master'] is None
    finally:
        target.cleanup()


# ==================== 2. 失败显式 + 保留断点（不跳过） ====================

def test_apply_failure_explicit_checkpoint_retained(
        tmp_path, monkeypatch):
    from lightdb.net.replica_proto import ReplicaApplyError, ReplicaPrototype

    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
        for i in range(1, 6):
            cur.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
        conn.close()
        # 目标表结构不匹配（v 是 INT，主库 v 是字符串 'v1'）：
        # DDL 事件幂等跳过，第一条 INSERT 类型转换失败 → 显式错误
        target.db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True,
             'notnull': True, 'unique': False},
            {'name': 'v', 'type': 'INT', 'primary': False,
             'notnull': False, 'unique': False}])

        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        with pytest.raises(ReplicaApplyError) as ei:
            proto.run_once(limit=100)
        assert 'INSERT' in str(ei.value) or 'INT' in str(ei.value), ei.value
        checkpoint = proto.state['lsn']
        assert checkpoint > 0            # 已成功的 DDL 事件位点保留
        assert proto.state['errors'] >= 1
        # 第二次仍从同一位点失败（不跳过、不前进）
        with pytest.raises(ReplicaApplyError):
            proto.run_once(limit=100)
        assert proto.state['lsn'] == checkpoint, (proto.state['lsn'],
                                                  checkpoint)
        # 重新加载状态文件：错误计数/断点持久化
        reloaded = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        assert reloaded.state['lsn'] == checkpoint
        assert reloaded.state['errors'] >= 1
        # 修复目标 schema（改成与主库一致）后从断点续传成功、数据一致
        target.db.drop_table('t')
        target.db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True,
             'notnull': True, 'unique': False},
            {'name': 'v', 'type': 'VARCHAR', 'primary': False,
             'notnull': False, 'unique': False}])
        reloaded.run_until_caught_up(batch=100)
        assert _fetch(target.db, 't') == master.rows('t')
        assert reloaded.state['errors'] >= 1   # 失败历史保留
    finally:
        target.cleanup()
        master.cleanup()


def test_follow_retries_transient_error_and_keeps_checkpoint(
        tmp_path, monkeypatch):
    from lightdb.net.replica_proto import (
        ReplicaPrototype, ReplicaRetryableError)

    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    stop = threading.Event()
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 4):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()

        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        real_run_once = proto.run_once
        failures = {'n': 0}

        def flaky_run_once(*args, **kwargs):
            if failures['n'] < 2:
                failures['n'] += 1
                raise ReplicaRetryableError('模拟瞬态主库失败')
            return real_run_once(*args, **kwargs)

        proto.run_once = flaky_run_once
        result = proto.follow(interval=0.01, batch=50, stop_event=stop,
                              max_events=4, retry_backoff=0.01,
                              max_backoff=0.05)
        assert failures['n'] == 2
        assert result['retries'] >= 2, result
        assert proto.state['lsn'] > 0
        assert _fetch(target.db, 't') == master.rows('t')
    finally:
        stop.set()
        target.cleanup()
        master.cleanup()


# ==================== 3. binlog 位点缺口（1236）+ 恢复策略 ====================

def test_binlog_gap_1236_explicit_and_reset(tmp_path, monkeypatch):
    from lightdb.net.replica_proto import ReplicaBinlogGap, ReplicaPrototype

    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 4):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()

        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        # 伪造"起始 binlog 已被主库清理"的状态文件
        proto.state['master_file'] = 'binlog.999999'
        proto.state['lsn'] = 1
        proto.save_state()
        with pytest.raises(ReplicaBinlogGap) as ei:
            proto.run_once(limit=100)
        err = ei.value
        assert err.errno == 1236
        assert '恢复策略' in str(err) or 'reset-position' in str(err)
        assert err.available_logs
        assert err.local_lsn == 1
        # 断点未被自动推进/跳过
        assert proto.state['lsn'] == 1
        # 显式 reset 后才推进到主库当前可用末尾
        reset = proto.reset_position()
        assert reset['new_lsn'] >= 1
        assert proto.state['lsn'] == reset['new_lsn']
        stats = proto.run_once(limit=100)
        assert stats['caught_up'] is True
    finally:
        target.cleanup()
        master.cleanup()


def test_cli_gap_error_1236_style(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
        cur.execute('INSERT INTO t VALUES (1)')
        conn.close()
        target_dir = str(tmp_path / 'gap_replica')
        os.makedirs(target_dir, exist_ok=True)
        state = os.path.join(target_dir, 'state.json')
        # 先让 CLI 生成合法状态，再伪造缺口（file/checksum 由 proto 维护）
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.net.replica_proto import ReplicaPrototype
        db = Database(target_dir)
        track(Executor(db))
        p = ReplicaPrototype('127.0.0.1', master.cfg.port, db,
                             state_path=state)
        p.state['master_file'] = 'binlog.999999'
        p.state['lsn'] = 1
        p.save_state()
        p.close()
        db.shutdown()

        script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
        proc = subprocess.run(
            [sys.executable, script, '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir,
             '--state-file', state, '--limit', '100'],
            capture_output=True, text=True, timeout=120, cwd=ROOT,
            encoding='utf-8', errors='replace',
            env={**os.environ, 'LIGHTDB_ALLOW_EMPTY_PASSWORD': '1',
                 'LIGHTDB_POOL_ENABLED': '0',
                 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        assert proc.returncode == 1, (proc.returncode, proc.stdout)
        assert 'ERROR 1236' in proc.stderr, proc.stderr
        assert '恢复' in proc.stderr
        # 状态文件仍保留原断点（未被跳过）
        st = json.load(open(state, encoding='utf-8'))
        assert st['lsn'] == 1
        # --reset-position 显式恢复
        proc2 = subprocess.run(
            [sys.executable, script, '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir,
             '--state-file', state, '--reset-position', '--json'],
            capture_output=True, text=True, timeout=120, cwd=ROOT,
            encoding='utf-8', errors='replace',
            env={**os.environ, 'LIGHTDB_ALLOW_EMPTY_PASSWORD': '1',
                 'LIGHTDB_POOL_ENABLED': '0',
                 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        assert proc2.returncode == 0, (proc2.returncode, proc2.stderr)
        reset_out = json.loads(proc2.stdout)
        assert reset_out['reset']['new_lsn'] >= 1
    finally:
        master.cleanup()


# ==================== 4. 中途杀进程 → 断点续传 ====================

def test_kill_process_resume_from_checkpoint(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(1, 41):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()
        target_dir = str(tmp_path / 'killed_replica')
        state = os.path.join(target_dir, 'replica_state.json')
        script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
        env = {**os.environ, 'LIGHTDB_ALLOW_EMPTY_PASSWORD': '1',
               'LIGHTDB_POOL_ENABLED': '0',
               'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}

        proc = subprocess.Popen(
            [sys.executable, script, '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir,
             '--state-file', state,
             '--follow', '--interval', '0.02', '--limit', '5'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=ROOT, env=env)

        def applied_count():
            raw = json.load(open(state, encoding='utf-8'))
            return int(raw.get('applied') or 0)
        assert wait_until(lambda: applied_count() >= 10, timeout=60), \
            applied_count()
        proc.kill()                       # 模拟进程被硬杀
        proc.wait(timeout=30)
        # 状态文件必须是完整的（原子写 + checksum）：能解析且 lsn>0
        raw = json.load(open(state, encoding='utf-8'))
        assert raw['lsn'] > 0
        assert 'checksum' in raw
        killed_lsn = int(raw['lsn'])

        # 主库继续写；重启 CLI 从断点续传
        conn = master.connect()
        cur = conn.cursor()
        for i in range(41, 61):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        conn.close()
        proc2 = subprocess.run(
            [sys.executable, script, '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir,
             '--state-file', state, '--json', '--limit', '100'],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            encoding='utf-8', errors='replace', env=env)
        assert proc2.returncode == 0, proc2.stderr
        out = json.loads(proc2.stdout)
        assert int(out['state']['lsn']) >= killed_lsn
        # 目标数据与主库一致（无重复/无缺失；INSERT 主键幂等）
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        db = Database(target_dir)
        track(Executor(db))
        rows = sorted(tuple(r) for r in db.get_table('t')
                      .select(['*'], None)[1])
        assert rows == master.rows('t'), (len(rows), len(master.rows('t')))
        db.shutdown()
    finally:
        master.cleanup()


# ==================== 5. 状态文件原子/校验 ====================

def test_state_file_atomic_checksum_validation(tmp_path, monkeypatch):
    from lightdb.net.replica_proto import ReplicaError, ReplicaPrototype

    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
        cur.execute('INSERT INTO t VALUES (1)')
        conn.close()
        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        proto.run_once(limit=100)
        raw = json.load(open(proto.state_path, encoding='utf-8'))
        assert 'checksum' in raw
        assert not os.path.exists(proto.state_path + '.tmp')
        # 篡改 checksum → 显式拒绝
        bad = dict(raw)
        bad['checksum'] = 'deadbeef'
        json.dump(bad, open(proto.state_path, 'w', encoding='utf-8'))
        with pytest.raises(ReplicaError) as ei:
            ReplicaPrototype('127.0.0.1', master.cfg.port, target.db)
        assert '校验失败' in str(ei.value) or 'checksum' in str(ei.value)
        # 恢复合法文件后可继续
        json.dump(raw, open(proto.state_path, 'w', encoding='utf-8'))
        proto2 = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        assert proto2.state['lsn'] == proto.state['lsn']
        # 非法版本/负 lsn 也显式拒绝
        bad2 = dict(raw)
        bad2['version'] = 999
        bad2['checksum'] = ReplicaPrototype._state_checksum(bad2)
        json.dump(bad2, open(proto.state_path, 'w', encoding='utf-8'))
        with pytest.raises(ReplicaError):
            ReplicaPrototype('127.0.0.1', master.cfg.port, target.db)
    finally:
        target.cleanup()
        master.cleanup()


# ==================== 6. 1k 事件延迟量化 ====================

def test_1k_events_incremental_latency(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target = TargetEnv(tmp_path, monkeypatch)
    stop = threading.Event()
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        from lightdb.net.replica_proto import ReplicaPrototype
        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target.db))
        th = threading.Thread(
            target=lambda: proto.follow(interval=0.02, batch=500,
                                        stop_event=stop),
            daemon=True)
        th.start()
        time.sleep(0.2)
        t0 = time.perf_counter()
        for i in range(1, 1001):
            cur.execute(f'INSERT INTO t VALUES ({i}, {i})')
        write_s = time.perf_counter() - t0
        assert wait_until(
            lambda: len(_fetch(target.db, 't')) >= 1000, timeout=90)
        apply_s = time.perf_counter() - t0
        # 端到端 = 主库写入 + 并发 follow 追平；apply 侧 ≈ 追平总时长 - 写入
        apply_phase = max(apply_s - write_s, 0.001)
        print(f'REPL_INC_1K write={write_s:.3f}s total={apply_s:.3f}s '
              f'apply_phase={apply_phase:.3f}s '
              f'rate={1000 / apply_phase:.1f} events/s')
        assert len(_fetch(target.db, 't')) >= 1000
        text = target.metrics()
        assert metric_value(
            text, 'lightdb_replica_applied_events_total') >= 1000
        # 宽松上界：只是防止明显失控（机器负载相关，不设硬性性能门禁）
        assert apply_s < 120, apply_s
        conn.close()
    finally:
        stop.set()
        target.cleanup()
        master.cleanup()
