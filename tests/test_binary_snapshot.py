"""P2：二进制一致性快照备份/恢复 的测试。

覆盖：
* manifest 字段完整（版本/表/行数/SHA256/大小/时间/文件清单）且文件哈希正确；
* 快照 -> 删除数据目录 -> 恢复 -> 逐表行数/关键值/SHOW CREATE/自增下一号/WAL=0 一致；
* 损坏 manifest / SHA256 不符 / 版本不兼容 -> 显式失败，且**不触碰目标目录**；
* 服务在监听时默认拒绝（真实 MySQLServer 实测），`--force` 显式覆盖并告警；
* 单表快照 -> 恢复到全新目录（只含该表）；
* 目标目录非空：默认拒绝，--force 把旧目录改名保留；
* 10k 行耗时默认量化；100k 行量化由 `LIGHTDB_SNAPSHOT_100K=1` 显式开启
  （本机引擎写入速度受负载影响，默认跑会拖慢全套；实测数字见 docs/SNAPSHOT_BACKUP.md）。

运行：python -m pytest tests/test_binary_snapshot.py -q
"""
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.backup import snapshot as S  # noqa: E402

USERS = [
    (None, '张三', 90),
    (None, 'bob', 80),
    (None, 'carol', 70),
]
ORDERS = [(i, (i % 3) + 1, i * 10) for i in range(1, 8)]


@pytest.fixture(autouse=True)
def _quiet_engine(monkeypatch):
    monkeypatch.setenv('LIGHTDB_AUDIT_LEVEL', 'NONE')
    monkeypatch.setenv('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                              # pragma: no cover
        pass


def _make_db(path, n_users=3, n_orders=7):
    """建一个两表小库（含中文/自增），返回期望数据。"""
    from lightdb.db.database import Database

    db = Database(str(path))
    users = db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'auto_increment': True},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'score', 'type': 'INT'},
    ])
    orders = db.create_table('orders', [
        {'name': 'oid', 'type': 'INT', 'primary': True},
        {'name': 'uid', 'type': 'INT'},
        {'name': 'amount', 'type': 'INT'},
    ])
    for row in USERS[:n_users]:
        users.insert(list(row))
    for row in ORDERS[:n_orders]:
        orders.insert(list(row))
    db.shutdown()
    return {'users': USERS[:n_users], 'orders': ORDERS[:n_orders]}


def _open(path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(path))
    return db, Executor(db)


def _rows(ex, sql):
    result = ex.execute_sql(sql)
    assert isinstance(result, dict) and result.get('type') == 'resultset', result
    return result['rows']


# ==================== manifest / 文件哈希 ====================

def test_manifest_fields_and_file_hashes(tmp_path):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    _make_db(data)
    report = S.create_snapshot(data, snap, check_servers=False, quiet=True)

    manifest = json.loads((snap / S.MANIFEST_NAME).read_text(encoding='utf-8'))
    assert manifest['schema'] == S.SCHEMA
    assert manifest['format_version'] == S.FORMAT_VERSION
    assert manifest['lightdb_version'] == S.lightdb_version()
    assert manifest['created_at']
    assert manifest['mode'] == 'full'
    assert manifest['tables_count'] == 2
    assert manifest['row_total'] == 3 + 7
    assert manifest['checkpoint']['all_wal_zero'] is True

    tables = {t['name']: t for t in manifest['tables']}
    assert set(tables) == {'users', 'orders'}
    users = tables['users']
    assert users['row_count'] == 3
    assert users['show_create'].startswith('CREATE TABLE users')
    assert users['auto_increment_next'] == {'id': 4}
    assert users['sample_first'] and users['sample_last']
    assert users['files'] and all(f in manifest for f in []) is True

    # 文件清单：存在 + 大小/SHA256 正确，且 WAL 全部为 0 字节
    assert manifest['total_files'] == len(manifest['files'])
    assert manifest['total_bytes'] == sum(f['size'] for f in manifest['files'])
    for record in manifest['files']:
        path = snap / record['path']
        assert path.is_file(), record
        assert path.stat().st_size == record['size']
        assert S.sha256_file(path) == record['sha256']
        assert len(record['sha256']) == 64
        if record['name'].endswith('.wal'):
            assert record['size'] == 0
    assert report['duration_s'] >= 0
    assert report['wal_zero'] is True


# ==================== 快照 -> 删除 -> 恢复 ====================

def test_snapshot_delete_restore_roundtrip(tmp_path):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    restored = tmp_path / 'restored'
    expected = _make_db(data)
    S.create_snapshot(data, snap, check_servers=False, quiet=True)

    # 模拟灾难：数据目录整个删除
    shutil.rmtree(data)
    assert not data.exists()

    report = S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
    assert report['verified'] is True
    assert report['verification']['wal_zero'] is True
    assert report['verification']['tables_verified'] == 2

    db, ex = _open(restored)
    try:
        assert sorted(r[0] for r in _rows(ex, 'SHOW TABLES')) == ['orders', 'users']
        assert sorted(r[0] for r in _rows(ex, 'SELECT id FROM users')) == [1, 2, 3]
        assert [tuple(r) for r in
                _rows(ex, 'SELECT id, name, score FROM users ORDER BY id')] == \
            [(1, '张三', 90), (2, 'bob', 80), (3, 'carol', 70)]
        assert _rows(ex, 'SELECT COUNT(*) FROM orders')[0][0] == 7
        create = _rows(ex, 'SHOW CREATE TABLE users')[0][1]
        assert create.startswith('CREATE TABLE users')
        # 自增计数一致：恢复后下一条仍是 4
        assert db.get_table('users').auto_increment_next_values() == {'id': 4}
    finally:
        db.shutdown()
    # shutdown 后 WAL 归零
    for t in ('users', 'orders'):
        wal = restored / (t + '.wal')
        assert (not wal.is_file()) or wal.stat().st_size == 0


def test_auto_increment_continues_after_restore(tmp_path):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    restored = tmp_path / 'restored'
    _make_db(data)
    S.create_snapshot(data, snap, check_servers=False, quiet=True)
    shutil.rmtree(data)
    S.restore_snapshot(snap, restored, check_servers=False, quiet=True)

    from lightdb.db.database import Database
    db = Database(str(restored))
    try:
        tbl = db.get_table('users')
        tbl.insert([None, 'dave', 60])
        tbl.insert([None, 'eve', 50])
    finally:
        db.shutdown()
    db, ex = _open(restored)
    try:
        ids = [r[0] for r in _rows(ex, 'SELECT id FROM users WHERE name IN '
                                       "('dave','eve') ORDER BY id")]
        assert ids == [4, 5], '恢复后 AUTO_INCREMENT 应从快照记录的下一号继续'
    finally:
        db.shutdown()


# ==================== 损坏 / 不兼容 -> 显式失败 ====================

def _fresh_snapshot(tmp_path):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    _make_db(data)
    S.create_snapshot(data, snap, check_servers=False, quiet=True)
    return data, snap


def test_restore_rejects_tampered_file_and_leaves_target_untouched(tmp_path):
    _, snap = _fresh_snapshot(tmp_path)
    target = tmp_path / 'target'
    victim = snap / S.DATA_SUBDIR / 'users.dat'
    with open(victim, 'ab') as f:
        f.write(b'X')
    with pytest.raises(S.SnapshotIntegrityError) as ei:
        S.restore_snapshot(snap, target, check_servers=False, quiet=True)
    assert 'SHA256' in str(ei.value) or '大小' in str(ei.value)
    assert not target.exists(), '校验失败时绝不能触碰目标目录'


def test_restore_rejects_corrupt_manifest(tmp_path):
    _, snap = _fresh_snapshot(tmp_path)
    manifest_path = snap / S.MANIFEST_NAME
    manifest_path.write_text('{not-json', encoding='utf-8')
    with pytest.raises(S.ManifestError):
        S.restore_snapshot(snap, tmp_path / 't1', check_servers=False, quiet=True)

    manifest_path.write_text(json.dumps({
        'schema': 'other-schema', 'format_version': 1,
        'lightdb_version': '4.0.0', 'created_at': 'x',
        'tables': [], 'files': []}), encoding='utf-8')
    with pytest.raises(S.ManifestError):
        S.restore_snapshot(snap, tmp_path / 't2', check_servers=False, quiet=True)


def test_restore_rejects_version_mismatch(tmp_path):
    _, snap = _fresh_snapshot(tmp_path)
    manifest_path = snap / S.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['lightdb_version'] = '9.9.9'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    target = tmp_path / 'target'
    with pytest.raises(S.VersionMismatchError):
        S.restore_snapshot(snap, target, check_servers=False, quiet=True)
    assert not target.exists()
    # 显式放宽后才允许
    report = S.restore_snapshot(snap, target, check_servers=False, quiet=True,
                                allow_version_mismatch=True,
                                version=S.lightdb_version())
    assert report['version_check']['mismatch_allowed'] is True


def test_restore_refuses_nonempty_target_without_force(tmp_path):
    data, snap = _fresh_snapshot(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'keep-me.txt').write_text('old', encoding='utf-8')
    with pytest.raises(S.SnapshotError):
        S.restore_snapshot(snap, target, check_servers=False, quiet=True)
    report = S.restore_snapshot(snap, target, check_servers=False, quiet=True,
                                force=True)
    assert report['replaced_to'] and Path(report['replaced_to']).is_dir()
    assert (Path(report['replaced_to']) / 'keep-me.txt').is_file()
    assert (target / 'users.dat').is_file()


# ==================== 服务运行中拒绝 ====================

def test_find_running_servers_unit():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        found = S.find_running_servers([port])
        assert any(item['port'] == port for item in found)
        closed = S.find_running_servers([1])          # 通常没有服务
        assert not any(item['port'] == 1 for item in closed)
    finally:
        listener.close()


def test_snapshot_refuses_when_real_server_listening(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data = tmp_path / 'data'
    _make_db(data)
    db = Database(str(data))
    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', 0
    srv = MySQLServer(cfg, db, Executor(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        time.sleep(0.4)
        assert S.find_running_servers([port]), '测试前置：服务未监听'
        out = tmp_path / 'snap'
        with pytest.raises(S.SnapshotRefusedError) as ei:
            S.create_snapshot(data, out, server_ports=[port], quiet=True)
        assert str(port) in str(ei.value)
        assert not (out / S.MANIFEST_NAME).exists(), '拒绝时不得产出快照'
    finally:
        srv.shutdown()
        srv.server_close()
        db.shutdown()


def test_force_overrides_detection_with_warning(tmp_path):
    """--force 场景用 dummy 监听器（不真正占用数据目录，结果确定）。"""
    data = tmp_path / 'data'
    _make_db(data)
    snap = tmp_path / 'snap'
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        report = S.create_snapshot(data, snap, server_ports=[port],
                                   force=True, quiet=True)
        assert report['warnings'], 'force 必须记录 WARNING'
        assert 'WARNING' in report['warnings'][0]
        assert (snap / S.MANIFEST_NAME).is_file()
    finally:
        listener.close()


# ==================== 单表快照 ====================

def test_single_table_snapshot_and_restore(tmp_path):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap_users'
    restored = tmp_path / 'restored'
    _make_db(data)
    report = S.create_snapshot(data, snap, table='users',
                               check_servers=False, quiet=True)
    assert report['mode'] == 'single_table'
    assert report['tables_count'] == 1
    sha = json.loads((snap / S.MANIFEST_NAME).read_text(encoding='utf-8'))
    assert sha['table_filter'] == 'users'
    assert not any(f['name'].startswith('orders') for f in sha['files'])

    shutil.rmtree(data)
    S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
    db, ex = _open(restored)
    try:
        assert [r[0] for r in _rows(ex, 'SHOW TABLES')] == ['users']
        assert _rows(ex, 'SELECT COUNT(*) FROM users')[0][0] == 3
    finally:
        db.shutdown()


# ==================== 量化：10k 默认 / 100k 显式开启 ====================

def _make_big_db(path, rows):
    from lightdb.db.database import Database
    db = Database(str(path))
    tbl = db.create_table('big', [
        {'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'},
    ])
    for i in range(1, rows + 1):
        tbl.insert([i, i % 1000])
    db.shutdown()


def test_snapshot_restore_10k_rows_timing(tmp_path, capsys):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    restored = tmp_path / 'restored'
    t0 = time.perf_counter()
    _make_big_db(data, 10_000)
    t_create = time.perf_counter() - t0
    t0 = time.perf_counter()
    report = S.create_snapshot(data, snap, check_servers=False, quiet=True)
    t_snap = time.perf_counter() - t0
    shutil.rmtree(data)
    t0 = time.perf_counter()
    rreport = S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
    t_restore = time.perf_counter() - t0
    with capsys.disabled():
        print('\n[snapshot 10k] 建库 %.2fs / 快照 %.2fs (%.1f KB) / 恢复+自检 %.2fs'
              % (t_create, t_snap, report['total_bytes'] / 1024.0, t_restore))
    assert rreport['verification']['tables_verified'] == 1
    assert rreport['row_total'] == 10_000
    assert rreport['verification']['wal_zero'] is True
    # 只做"数量级"级别的宽阈值判断，避免机器负载导致假红
    assert t_create < 120 and t_snap < 120 and t_restore < 120


@pytest.mark.skipif(
    os.environ.get('LIGHTDB_SNAPSHOT_100K') != '1',
    reason='100k 行量化默认跳过（引擎写入速度受机器负载影响）；'
           '用 LIGHTDB_SNAPSHOT_100K=1 显式运行')
def test_snapshot_restore_100k_rows_timing(tmp_path, capsys):
    data = tmp_path / 'data'
    snap = tmp_path / 'snap'
    restored = tmp_path / 'restored'
    t0 = time.perf_counter()
    _make_big_db(data, 100_000)
    t_create = time.perf_counter() - t0
    t0 = time.perf_counter()
    report = S.create_snapshot(data, snap, check_servers=False, quiet=True)
    t_snap = time.perf_counter() - t0
    shutil.rmtree(data)
    t0 = time.perf_counter()
    rreport = S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
    t_restore = time.perf_counter() - t0
    with capsys.disabled():
        print('\n[snapshot 100k] 建库 %.2fs / 快照 %.2fs (%.1f KB, %d 文件) / '
              '恢复+自检 %.2fs'
              % (t_create, t_snap, report['total_bytes'] / 1024.0,
                 report['total_files'], t_restore))
    assert rreport['row_total'] == 100_000
    assert rreport['verification']['tables_verified'] == 1
    assert rreport['verification']['wal_zero'] is True
    assert t_snap < 300 and t_restore < 300
