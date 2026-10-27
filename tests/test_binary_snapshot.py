"""P2/P3：二进制一致性快照备份/恢复 的测试。

覆盖：
* manifest 字段完整（版本/表/行数/SHA256/大小/时间/文件清单）且文件哈希正确；
* 快照 -> 删除数据目录 -> 恢复 -> 逐表行数/关键值/SHOW CREATE/自增下一号/WAL=0 一致；
* 损坏 manifest / SHA256 不符 / 版本不兼容 -> 显式失败，且**不触碰目标目录**；
* 跨版本策略：同 schema/format 的版本差异默认拒绝，`allow_version_mismatch`
  放行并 WARNING；格式不兼容即使放行也显式失败；
* 在线热备（quiesce）：真实 MySQLServer + 并发 pymysql 写客户端下，
  进程内写锁快照点一致（无半提交、WAL=0）、快照后写入不丢；跨进程
  suspend 后端管道（monkeypatch 模拟）与失败显式报错；10k 行阻塞/总时长量化；
* 服务在监听时离线快照默认拒绝（真实 MySQLServer 实测），`--force` 告警覆盖；
* 单表快照 -> 恢复到全新目录（只含该表）；目标非空保护；
* 10k/100k 离线快照耗时量化（100k 由 `LIGHTDB_SNAPSHOT_100K=1` 显式开启）。

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


# ==================== P3：跨版本兼容策略（两条路径） ====================

def test_allow_version_mismatch_compatible_format_warns(tmp_path):
    """同 schema/format 的跨版本：默认拒绝，显式 --allow-version-mismatch
    放行并 WARNING，恢复后自检仍通过。"""
    _, snap = _fresh_snapshot(tmp_path)
    manifest_path = snap / S.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['lightdb_version'] = '9.9.9-forged'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    target = tmp_path / 'target'

    with pytest.raises(S.VersionMismatchError):
        S.restore_snapshot(snap, target, check_servers=False, quiet=True)
    assert not target.exists()

    report = S.restore_snapshot(
        snap, target, check_servers=False, quiet=True,
        allow_version_mismatch=True, version=S.lightdb_version())
    assert report['verified'] is True
    vc = report['version_check']
    assert vc['format_compatible'] is True
    assert vc['mismatch_allowed'] is True
    assert vc['warnings'], '放行跨版本必须记录 WARNING'
    assert any('WARNING' in w for w in report['warnings'])


def test_allow_version_mismatch_never_bypasses_incompatible_format(tmp_path):
    """格式不兼容（format_version/schema）即使 --allow-version-mismatch
    也必须显式失败，且不触碰目标目录。"""
    _, snap = _fresh_snapshot(tmp_path)
    manifest_path = snap / S.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['format_version'] = 999                      # 伪造不兼容格式
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    target = tmp_path / 'target'
    with pytest.raises(S.ManifestError):
        S.restore_snapshot(snap, target, check_servers=False, quiet=True,
                           allow_version_mismatch=True)
    assert not target.exists()


# ==================== P3：在线热备 quiesce ====================

def _root_password(data_dir) -> str:
    p = Path(data_dir) / 'root.password'
    return p.read_text(encoding='utf-8').strip() if p.is_file() else ''


def _start_inprocess_server(data_dir):
    """真实 MySQLServer（进程内线程）+ 同一 Database 实例（供 online live_db）。"""
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    db = Database(str(data_dir))
    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', 0
    srv = MySQLServer(cfg, db, Executor(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if S.find_running_servers([port]):
            return db, srv, port
        time.sleep(0.02)
    raise AssertionError('真实 MySQLServer 未在 5s 内开始监听')


def test_online_inprocess_quiesce_consistent_under_concurrent_writers(tmp_path,
                                                                      capsys):
    """真实 server + 并发写客户端：quiesce 窗口内拷贝得到"快照点一致"的数据：
    同一语句写入的成对行不会只出现一半；WAL=0；快照后写入不丢。"""
    pymysql = pytest.importorskip('pymysql')
    from lightdb.db.database import Database

    data = tmp_path / 'data'
    base = Database(str(data))
    base.create_table('pairs', [
        {'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'tag', 'type': 'INT'},
        {'name': 'seq', 'type': 'INT'},
    ])
    base.shutdown()

    db, srv, port = _start_inprocess_server(data)
    password = _root_password(data)
    snap = tmp_path / 'snap'
    stop = threading.Event()
    commits = [0, 0]
    errors = []

    def writer(tag):
        try:
            conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                                   password=password, autocommit=True,
                                   connect_timeout=5)
            cur = conn.cursor()
            base = tag * 100_000_000
            seq = 1
            while not stop.is_set():
                cur.execute(
                    'INSERT INTO pairs VALUES (%s,%s,%s),(%s,%s,%s)',
                    (base + 2 * seq, tag, seq,
                     base + 2 * seq + 1, tag, seq))
                commits[tag - 1] += 1
                seq += 1
            conn.close()
        except Exception as exc:                     # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(t,), daemon=True)
               for t in (1, 2)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.6)
        report = S.create_snapshot(
            data, snap, online=True, live_db=db, server_ports=[port],
            quiet=True, quiesce_wait=10.0)
        online = report['online']
        assert online['enabled'] is True
        assert online['backend'] == 'in-process-write-lock'
        assert online['blocked_window_s'] >= 0.0
        assert report['wal_zero'] is True
        snap_rows_manifest = report['row_total']
        assert snap_rows_manifest % 2 == 0 and snap_rows_manifest > 0, \
            '快照必须落在成对行的语句边界上（无半提交）'

        time.sleep(0.3)                    # 快照后继续写
        stop.set()
        for t in threads:
            t.join(5)
        assert not errors, errors
        assert all(t.is_alive() is False for t in threads)

        # 恢复快照 -> 校验成对行完整
        restored = tmp_path / 'restored'
        rreport = S.restore_snapshot(snap, restored, check_servers=False,
                                     quiet=True)
        assert rreport['verification']['wal_zero'] is True
        db2, ex = _open(restored)
        try:
            rows = _rows(ex, 'SELECT id, tag, seq FROM pairs ORDER BY id')
            assert len(rows) == snap_rows_manifest
            by_pair = {}
            for _id, tag, seq in rows:
                by_pair[(tag, seq)] = by_pair.get((tag, seq), 0) + 1
            assert all(v == 2 for v in by_pair.values()), \
                '存在半提交：某条语句只写了一行'
            assert len({tag for _id, tag, _seq in rows}) >= 1
        finally:
            db2.shutdown()

        # 快照后写入未丢：最终 live 行数 == 2 * 总提交数，且多于快照行数
        total_commits = sum(commits)
        conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                               password=password, autocommit=True,
                               connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM pairs')
            live_rows = cur.fetchone()[0]
        finally:
            conn.close()
        assert live_rows == 2 * total_commits, (live_rows, total_commits)
        assert live_rows > snap_rows_manifest, '快照后应继续有写入'
        with capsys.disabled():
            print('\n[online in-process] 快照 %d 行（%d 事务）/ 最终 %d 行'
                  '（%d 事务）；阻塞窗口 %.4fs（checkpoint %.4fs + 拷贝 '
                  '%.4fs）；总耗时 %.2fs'
                  % (snap_rows_manifest, snap_rows_manifest // 2, live_rows,
                     total_commits, online['blocked_window_s'],
                     online['checkpoint_s'], online['copy_s'],
                     report['duration_s']))
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        db.shutdown()


def test_online_process_suspend_backend_simulated(tmp_path, monkeypatch):
    """跨进程 suspend 后端的管道验证：suspend/resume 由 monkeypatch 模拟
    （本沙箱禁止真实 PROCESS_SUSPEND_RESUME），服务空闲时拷贝应一致。"""
    data = tmp_path / 'data'
    _make_db(data)
    db, srv, port = _start_inprocess_server(data)
    calls = {'suspend': [], 'resume': []}

    def fake_suspend(pid, timeout=10.0):
        calls['suspend'].append(pid)
        return {'pid': pid, 'backend': 'process-suspend', 'suspended': True}

    def fake_resume(pid):
        calls['resume'].append(pid)
        return True

    monkeypatch.setattr(S, 'suspend_process', fake_suspend)
    monkeypatch.setattr(S, 'resume_process', fake_resume)
    snap = tmp_path / 'snap'
    try:
        report = S.create_snapshot(
            data, snap, online=True, server_pid=os.getpid(),
            check_servers=False, quiet=True)
        online = report['online']
        assert online['backend'] == 'process-suspend'
        assert online['recovered_on_copy'] is True
        assert online['resume_ok'] is True
        assert calls['suspend'] and calls['resume']
        assert report['row_total'] == 10
        assert report['wal_zero'] is True
        # 副本已 WAL 恢复 + checkpoint，可正常恢复
        restored = tmp_path / 'restored'
        S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
    finally:
        srv.shutdown()
        srv.server_close()
        db.shutdown()


def test_online_quiesce_failure_is_explicit(tmp_path, monkeypatch):
    """quiesce 后端失败必须显式 OnlineQuiesceError，不产出快照。"""
    data = tmp_path / 'data'
    _make_db(data)
    db, srv, port = _start_inprocess_server(data)
    snap = tmp_path / 'snap'

    def failing_suspend(pid, timeout=10.0):
        raise S.OnlineQuiesceError('模拟沙箱拒绝 PROCESS_SUSPEND_RESUME')

    monkeypatch.setattr(S, 'suspend_process', failing_suspend)
    try:
        with pytest.raises(S.OnlineQuiesceError):
            S.create_snapshot(data, snap, online=True, server_pid=os.getpid(),
                              check_servers=False, quiet=True)
        assert not (snap / S.MANIFEST_NAME).exists()
    finally:
        srv.shutdown()
        srv.server_close()
        db.shutdown()


def test_online_requires_locatable_server(tmp_path):
    """在线模式定位不到服务进程 -> 显式失败（不静默降级为不一致拷贝）。"""
    data = tmp_path / 'data'
    _make_db(data)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(S.OnlineQuiesceError):
            S.create_snapshot(data, tmp_path / 'snap', online=True,
                              server_ports=[port], check_servers=False,
                              quiet=True)
    finally:
        listener.close()


def test_online_10k_rows_blocking_quantified(tmp_path, capsys):
    """10k 行在线热备：量化 quiesce 阻塞窗口与总时长（真实 server，进程内写锁）。"""
    data = tmp_path / 'data'
    t0 = time.perf_counter()
    _make_big_db(data, 10_000)
    t_create = time.perf_counter() - t0
    db, srv, port = _start_inprocess_server(data)
    snap = tmp_path / 'snap'
    try:
        t0 = time.perf_counter()
        report = S.create_snapshot(
            data, snap, online=True, live_db=db, server_ports=[port],
            quiet=True)
        t_total = time.perf_counter() - t0
        online = report['online']
        assert report['row_total'] == 10_000
        assert online['backend'] == 'in-process-write-lock'
        assert report['wal_zero'] is True
        assert online['blocked_window_s'] >= 0.0
        restored = tmp_path / 'restored'
        S.restore_snapshot(snap, restored, check_servers=False, quiet=True)
        with capsys.disabled():
            print('\n[online 10k] 建库 %.2fs / 阻塞窗口 %.4fs'
                  '（checkpoint %.4fs + 拷贝 %.4fs，锁等待 %.4fs）'
                  ' / 快照+后处理 %.2fs / 总数 %.2fs'
                  % (t_create, online['blocked_window_s'],
                     online['checkpoint_s'], online['copy_s'],
                     online['lock_wait_s'], report['duration_s'],
                     report['duration_s'] + t_create))
        assert online['blocked_window_s'] < 300
        assert report['duration_s'] < 300
    finally:
        srv.shutdown()
        srv.server_close()
        db.shutdown()

