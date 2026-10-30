# -*- coding: utf-8 -*-
"""P4（task-86）：复制协议层 —— COM_BINLOG_DUMP 流 + 协议客户端拉取

覆盖：
1. 协议字节级：原始 socket 握手 → COM_BINLOG_DUMP 请求字段 → 响应
   `0x00+JSON` 流（stream_start/event/eof）+ 标准 EOF 包；事件字段与
   `__BINLOG_FETCH__` 同口径（pos/lsn/txn_id/event_type/table_name/data/
   file/next_pos）；流结束后连接仍可用（COM_PING）。
2. 参数/权限/能力校验：非法 filename/pos/flags/server_id/短包 → 显式
   ERR（1236/1105）；binlog 未启用 → 1381；无权限用户 → 1044/1227；
   GTID dump → 显式 1105。
3. 端到端：主库写 DDL+CRUD（含轮转）→ 协议拉取 apply → 与内部导出模式
   结果逐行一致、lsn/applied 一致。
4. 可靠性：只取 N 条主动断开 → 从状态位点续传（不重复/不丢）；binlog
   文件缺失 → ReplicaBinlogGap(1236)；`--reset-position` 显式恢复；CLI
   `--protocol` 端到端可用。
"""
import json
import os
import socket
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
    from tests.test_replication_prototype import MasterEnv, _target
except ImportError:                       # pragma: no cover - 直接运行
    from conftest import track
    from test_replication_prototype import MasterEnv, _target

from lightdb.net import mysql_protocol as mp  # noqa: E402
from lightdb.net.replica_proto import (       # noqa: E402
    BINLOG_DUMP_SCHEMA, BinlogDumpClient, ReplicaBinlogGap, ReplicaError,
    ReplicaPrototype)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_KEYS = ('LIGHTDB_BINLOG_ENABLED', 'LIGHTDB_BINLOG_MAX_BYTES',
            'LIGHTDB_POOL_ENABLED')


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LIGHTDB_POOL_ENABLED', '0')
    yield


# ==================== 原始 socket 协议助手 ====================

def _recv_exact(sock, n):
    chunks = []
    got = 0
    while got < n:
        part = sock.recv(n - got)
        if not part:
            raise ConnectionError('connection closed')
        chunks.append(part)
        got += len(part)
    return b''.join(chunks)


def raw_read_packet(sock) -> bytes:
    header = _recv_exact(sock, 4)
    length = header[0] | (header[1] << 8) | (header[2] << 16)
    if length <= 0:
        return b''
    return _recv_exact(sock, length)


def raw_send_packet(sock, payload: bytes, seq: int = 0):
    n = len(payload)
    sock.sendall(bytes([n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff,
                        seq & 0xff]) + payload)


def raw_connect(port: int, user: str = 'root', password: str = ''):
    """明文 4.1 握手（复用仓库 mysql_protocol 的客户端 helper）"""
    sock = socket.create_connection(('127.0.0.1', port), timeout=10)
    try:
        info = mp.parse_handshake_v10(raw_read_packet(sock))
        raw_send_packet(sock, mp.make_handshake_response(
            user, password, info['auth_seed']), seq=1)
        auth = raw_read_packet(sock)
    except Exception:
        sock.close()
        raise
    assert auth[:1] == b'\x00', mp.parse_err_packet(auth)
    return sock


def raw_dump_request(sock, binlog_pos: int = 4, flags: int = 1,
                     server_id: int = 1234, filename: str = ''):
    payload = (bytes([mp.COM_BINLOG_DUMP])
               + mp.make_binlog_dump_request(
                   binlog_pos=binlog_pos, flags=flags,
                   server_id=server_id, filename=filename))
    raw_send_packet(sock, payload, seq=0)


def read_dump_stream(sock):
    """读取一条 dump 流：返回 (header_json, events, eof_json, eof_packet)"""
    packets = []
    while True:
        pkt = raw_read_packet(sock)
        packets.append(pkt)
        if pkt[:1] == b'\xff':
            return None, [], None, pkt
        if pkt[:1] == bytes([mp.BINLOG_STREAM_EOF]):
            break
    header = json.loads(packets[0][1:].decode('utf-8'))
    events = []
    eof = None
    for pkt in packets[1:-1]:
        obj = json.loads(pkt[1:].decode('utf-8'))
        if obj.get('type') == 'event':
            events.append(obj)
        elif obj.get('type') == 'eof':
            eof = obj
    return header, events, eof, packets[-1]


def _first_binlog(master) -> str:
    conn = master.connect()
    try:
        cur = conn.cursor()
        cur.execute('SHOW BINARY LOGS')
        rows = cur.fetchall()
        return str(rows[0][0])
    finally:
        conn.close()


def _write_fixture_data(master):
    conn = master.connect()
    cur = conn.cursor()
    cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    for i in range(1, 6):
        cur.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    cur.execute("UPDATE t SET v='upd' WHERE id=3")
    cur.execute('DELETE FROM t WHERE id=5')
    cur.execute('CREATE TABLE t2 (id INT PRIMARY KEY)')
    cur.execute('INSERT INTO t2 VALUES (7)')
    conn.close()


def _err(payload):
    return mp.parse_err_packet(payload) or (None, None, '')


# ==================== 1. 字节级请求/响应 ====================

def test_protocol_byte_level_request_response(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        _write_fixture_data(master)
        first = _first_binlog(master)
        sock = raw_connect(master.cfg.port)
        try:
            raw_dump_request(sock, binlog_pos=16, flags=1, server_id=4321,
                             filename=first)
            header, events, eof, last = read_dump_stream(sock)
            assert header is not None, last
            assert header['type'] == 'stream_start'
            assert header['schema'] == BINLOG_DUMP_SCHEMA
            assert header['file'] == first and header['pos'] == 16
            assert header['server_id'] == 4321 and header['flags'] == 1
            assert header['global_lsn'] == 16
            # 每个事件包都是 0x00 + JSON；字段与内部导出口径一致
            assert len(events) == 10, len(events)
            for ev in events:
                assert set(('pos', 'lsn', 'txn_id', 'event_type',
                            'table_name', 'data', 'file',
                            'next_pos')) <= set(ev)
                assert isinstance(ev['lsn'], int) and ev['lsn'] > 0
                assert isinstance(ev['next_pos'], int) and ev['next_pos'] > 0
                assert isinstance(ev['data'], dict)
            assert [e['pos'] for e in events] == list(range(1, 11))
            assert [e['event_type'] for e in events] == [
                'DDL', 'INSERT', 'INSERT', 'INSERT', 'INSERT', 'INSERT',
                'UPDATE', 'DELETE', 'DDL', 'INSERT']
            assert eof is not None and eof['type'] == 'eof'
            assert eof['count'] == len(events) == 10
            assert eof['caught_up'] is True
            assert eof['last_lsn'] == events[-1]['lsn']
            assert eof['last_file'] == first
            assert eof['last_pos'] == events[-1]['next_pos']
            # 最后一包是标准 MySQL EOF（0xfe + warnings + status）
            assert last == mp.make_eof_packet(), last
            # 流结束后连接仍可继续发命令（服务端回到命令循环）
            raw_send_packet(sock, bytes([mp.COM_PING]), seq=0)
            ok = raw_read_packet(sock)
            assert ok[:1] == b'\x00', ok
        finally:
            sock.close()
    finally:
        master.cleanup()


def test_protocol_continues_across_binlog_rotation(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch,
                       env={'LIGHTDB_BINLOG_MAX_BYTES': '4096'})
    try:
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
        for i in range(1, 121):
            cur.execute(f"INSERT INTO t VALUES ({i}, 'v{i:04d}')")
        conn.close()
        logs = []
        conn = master.connect()
        cur = conn.cursor()
        cur.execute('SHOW BINARY LOGS')
        logs = [r[0] for r in cur.fetchall()]
        conn.close()
        assert len(logs) >= 2, logs          # 已真实轮转
        sock = raw_connect(master.cfg.port)
        try:
            raw_dump_request(sock, binlog_pos=16, flags=1,
                             filename=logs[0])
            header, events, eof, last = read_dump_stream(sock)
            assert header['file'] == logs[0]
            files_seen = {e['file'] for e in events}
            assert len(files_seen) >= 2, files_seen
            assert len(events) == 121, len(events)
            assert [e['pos'] for e in events] == list(range(1, 122))
            assert eof['last_file'] in files_seen
            # 轮转后全局 lsn 严格递增
            lsns = [e['lsn'] for e in events]
            assert lsns == sorted(lsns) and len(set(lsns)) == len(lsns)
        finally:
            sock.close()
    finally:
        master.cleanup()


# ==================== 2. 参数/权限/能力校验 ====================

def test_protocol_parameter_errors_explicit(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        _write_fixture_data(master)
        first = _first_binlog(master)
        cases = [
            # (kwargs, expected_errno)
            ({'filename': 'binlog.999999'}, 1236),
            ({'filename': first, 'binlog_pos': 0}, 1236),
            ({'filename': first, 'binlog_pos': 17}, 1236),  # 非事件边界
            ({'filename': first, 'binlog_pos': 10 ** 7}, 1236),
            ({'filename': first, 'flags': 0x04}, 1105),      # 非法 flags
            ({'filename': first, 'flags': 0x02}, 1105),      # annotate rows
            ({'filename': first, 'server_id': 0}, 1105),
        ]
        for kwargs, want in cases:
            sock = raw_connect(master.cfg.port)
            try:
                params = {'binlog_pos': 16, 'flags': 1, 'server_id': 7,
                          'filename': first}
                params.update(kwargs)
                raw_dump_request(sock, **params)
                pkt = raw_read_packet(sock)
                err = _err(pkt)
                assert err[0] == want, (kwargs, err)
                assert err[2], (kwargs, err)
            finally:
                sock.close()
        # 参数不足的短请求体 → 显式 1105
        sock = raw_connect(master.cfg.port)
        try:
            raw_send_packet(sock, bytes([mp.COM_BINLOG_DUMP]) + b'\x01\x02',
                            seq=0)
            err = _err(raw_read_packet(sock))
            assert err[0] == 1105, err
        finally:
            sock.close()
        # COM_BINLOG_DUMP_GTID → 显式 1105（未实现，不假装兼容）
        sock = raw_connect(master.cfg.port)
        try:
            raw_send_packet(sock, bytes([mp.COM_BINLOG_DUMP_GTID]) + b'\x00' * 8,
                            seq=0)
            err = _err(raw_read_packet(sock))
            assert err[0] == 1105, err
            assert 'GTID' in err[2], err
        finally:
            sock.close()
    finally:
        master.cleanup()


def test_protocol_binlog_disabled_1381(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch,
                       env={'LIGHTDB_BINLOG_ENABLED': '0'})
    try:
        sock = raw_connect(master.cfg.port)
        try:
            raw_dump_request(sock, binlog_pos=16, flags=1, filename='')
            err = _err(raw_read_packet(sock))
            assert err[0] == 1381, err
            assert 'binary logging' in err[2].lower(), err
        finally:
            sock.close()
    finally:
        master.cleanup()


def test_protocol_permission_denied(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        _write_fixture_data(master)
        conn = master.connect()
        cur = conn.cursor()
        cur.execute("CREATE USER 'repl_plain'@'%' "
                    "IDENTIFIED BY 'ReplPass123!'")
        conn.close()
        sock = raw_connect(master.cfg.port, user='repl_plain',
                           password='ReplPass123!')
        try:
            raw_dump_request(sock, binlog_pos=16, flags=1,
                             filename=_first_binlog(master))
            err = _err(raw_read_packet(sock))
            assert err[0] in (1044, 1227), err
        finally:
            sock.close()
    finally:
        master.cleanup()


def test_protocol_empty_binlog_streams_eof(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        sock = raw_connect(master.cfg.port)
        try:
            raw_dump_request(sock, binlog_pos=16, flags=1, filename='')
            header, events, eof, last = read_dump_stream(sock)
            # 还没有数据事件：无论 binlog 文件是否已惰性创建（只有头/格式
            # 事件），都必须以 0 事件 + EOF 结束，不能阻塞/编造事件
            assert header['type'] == 'stream_start'
            assert events == []
            assert eof['count'] == 0 and eof['caught_up'] is True
            assert last == mp.make_eof_packet()
            # 若已有当前文件，header 如实报告它；没有则是 None
            assert header['file'] is None or isinstance(header['file'], str)
        finally:
            sock.close()
    finally:
        master.cleanup()


# ==================== 3. 端到端：协议 vs 内部导出一致 ====================

def _apply_protocol(master, target_db, batch=100):
    proto = track(ReplicaPrototype(
        '127.0.0.1', master.cfg.port, target_db, source='protocol'))
    stats = proto.run_until_caught_up(batch=batch, source='protocol')
    return proto, stats


def _rows(db, table):
    _cols, rows = db.get_table(table).select(['*'], None)
    return sorted(tuple(r) for r in rows)


def test_protocol_matches_export_and_master(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    proto_db = _target(tmp_path, monkeypatch, 'proto_target')
    export_db = _target(tmp_path, monkeypatch, 'export_target')
    try:
        _write_fixture_data(master)
        proto, pstats = _apply_protocol(master, proto_db)
        exp = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, export_db, source='export'))
        estats = exp.run_until_caught_up(batch=100)
        # 数据逐行一致：协议 vs 导出 vs 主库
        assert _rows(proto_db, 't') == master.rows('t')
        assert _rows(proto_db, 't') == _rows(export_db, 't')
        assert _rows(proto_db, 't2') == _rows(export_db, 't2')
        # 位点/应用条数一致（同一批数据事件）
        assert pstats['caught_up'] is True
        assert int(proto.state['lsn']) == int(exp.state['lsn'])
        assert int(proto.state['applied']) == int(exp.state['applied'])
        # 协议模式独有：精确续传位点
        assert proto.state['protocol_file'] is not None
        assert int(proto.state['protocol_pos']) > 0
        assert proto.stats()['source'] == 'protocol'
        assert proto.stats()['protocol_file'] == proto.state['protocol_file']
        # 再跑一次：0 新事件（EOF 无事件、状态位点不变）
        before_pos = (proto.state['protocol_file'],
                      int(proto.state['protocol_pos']))
        again = proto.run_once(limit=100)
        assert again['events'] == 0
        assert (proto.state['protocol_file'],
                int(proto.state['protocol_pos'])) == before_pos
    finally:
        master.cleanup()


def test_protocol_disconnect_resume(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target_db = _target(tmp_path, monkeypatch, 'resume_target')
    try:
        _write_fixture_data(master)
        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target_db, source='protocol'))
        first = proto.run_once(limit=2, source='protocol')
        assert first['events'] == 2
        assert first['protocol_truncated'] is True
        pos_after_first = (proto.state['protocol_file'],
                           int(proto.state['protocol_pos']))
        assert pos_after_first[0] and pos_after_first[1] > 0
        # 新实例（模拟进程重启/重连）从持久位点续传
        proto2 = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target_db, source='protocol'))
        assert (proto2.state['protocol_file'],
                int(proto2.state['protocol_pos'])) == pos_after_first
        stats = proto2.run_until_caught_up(batch=100, source='protocol')
        assert stats['caught_up'] is True
        assert int(proto2.state['applied']) == 10          # 不重复、不丢
        assert _rows(target_db, 't') == master.rows('t')
        # 断点续传位点单调前进
        assert int(proto2.state['protocol_pos']) > pos_after_first[1]
    finally:
        master.cleanup()


def test_protocol_binlog_gap_explicit_and_reset(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    target_db = _target(tmp_path, monkeypatch, 'gap_target')
    try:
        _write_fixture_data(master)
        proto = track(ReplicaPrototype(
            '127.0.0.1', master.cfg.port, target_db, source='protocol'))
        proto.state['protocol_file'] = 'binlog.999999'
        proto.state['protocol_pos'] = 16
        proto.save_state()
        with pytest.raises(ReplicaBinlogGap) as ei:
            proto.run_once(limit=100, source='protocol')
        assert ei.value.errno == 1236
        assert 'Could not find first log file' in str(ei.value)
        # 状态文件断点未被自动跳过
        assert proto.state['protocol_file'] == 'binlog.999999'
        # 显式 reset → 推进到主库当前末尾（协议位点同步重置）
        reset = proto.reset_position()
        assert reset['new_lsn'] >= 1
        assert proto.state['protocol_file'] is not None
        assert int(proto.state['protocol_pos']) >= 16
        stats = proto.run_once(limit=100, source='protocol')
        assert stats['caught_up'] is True
    finally:
        master.cleanup()


def test_protocol_retryable_when_master_down(tmp_path, monkeypatch):
    target_db = _target(tmp_path, monkeypatch, 'down_target')
    proto = track(ReplicaPrototype(
        '127.0.0.1', _free_port(), target_db, source='protocol'))
    with pytest.raises(ReplicaError) as ei:
        proto.run_once(limit=10, source='protocol')
    # 连接类失败必须可重试（ReplicaRetryableError 是 ReplicaError 子类）
    assert '连接主库失败' in str(ei.value) or '主库' in str(ei.value)


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ==================== 4. CLI --protocol ====================

def test_cli_protocol_end_to_end(tmp_path, monkeypatch):
    master = MasterEnv(tmp_path, monkeypatch)
    try:
        _write_fixture_data(master)
        target_dir = str(tmp_path / 'cli_proto')
        script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
        env = {**os.environ, 'LIGHTDB_ALLOW_EMPTY_PASSWORD': '1',
               'LIGHTDB_POOL_ENABLED': '0'}
        proc = subprocess.run(
            [sys.executable, script, '--protocol',
             '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir, '--json'],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            encoding='utf-8', errors='replace', env=env)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out['summary']['events'] == 10, out
        assert out['summary']['applied'] == 10
        assert out['summary']['caught_up'] is True
        assert out['state']['source'] == 'protocol'
        assert out['state']['protocol_file']
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        db = track(Database(target_dir))
        track(Executor(db))
        assert _rows(db, 't') == master.rows('t')
        db.shutdown()
        # 第二次运行：无新事件、位点不变
        proc2 = subprocess.run(
            [sys.executable, script, '--protocol',
             '--master-host', '127.0.0.1',
             '--master-port', str(master.cfg.port),
             '--target-data-dir', target_dir, '--json'],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            encoding='utf-8', errors='replace', env=env)
        assert proc2.returncode == 0, proc2.stderr
        out2 = json.loads(proc2.stdout)
        assert out2['summary']['events'] == 0
        assert out2['summary']['caught_up'] is True
    finally:
        master.cleanup()
