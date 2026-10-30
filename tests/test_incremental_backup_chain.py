"""增量备份链（`scripts/backup_chain.py`）的测试。

覆盖：
* 全量基线 → 多次增量 → restore 最新/--to N，恢复数据与源库逐行一致
  （行数/首末主键/全表行摘要/SHOW CREATE/AUTO_INCREMENT/WAL=0）；
* DDL 增量（CREATE/ALTER/DROP TABLE）可回放；
* 边界：无变化增量 noop、仅基线、--to 越界、目标非空需 --force；
* 失败显式且**不破坏目标**：缺段/篡改 sha256/manifest 断链/版本不兼容；
* 保留策略 `--keep` rebase 后链仍连续、恢复仍正确、旧文件被清理；
* 不支持形态（TRUNCATE/RENAME、绕过 binlog 的 schema 变更）显式拒绝；
* 服务运行中检测；
* 10k 行量化（基线/增量大小与耗时、恢复链时间）。
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import backup_chain as B                          # noqa: E402
from lightdb.db.database import Database          # noqa: E402
from lightdb.sql.executor import Executor         # noqa: E402


# ==================== 工具 ====================

def _quiet():
    os.environ.setdefault('LIGHTDB_AUDIT_LEVEL', 'NONE')
    os.environ.setdefault('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                                # pragma: no cover
        pass


def _make_db(data_dir, rows=300, batch=50):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir))
    try:
        ex = Executor(db)
        res = ex.execute_sql(
            'CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, v INT)', {})
        assert res.get('type') != 'error', res
        for start in range(1, rows + 1, batch):
            end = min(rows, start + batch - 1)
            values = ', '.join('(%d)' % i for i in range(start, end + 1))
            res = ex.execute_sql('INSERT INTO t (v) VALUES ' + values, {})
            assert res.get('type') != 'error', res
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    _quiet()
    return data_dir


def _exec(data_dir, db_factory=None):
    """打开数据目录执行 SQL 上下文（返回 db/ex；调用方负责 shutdown）。"""
    db = Database(str(data_dir))
    return db, Executor(db)


def _snapshot_rows(data_dir):
    db, ex = _exec(data_dir)
    try:
        out = {}
        for t in sorted(db.tables):
            rows = ex.execute_sql('SELECT * FROM %s' % t, {})['rows']
            cols = [c['name'] for c in db.tables[t].cols]
            pk = db.tables[t].pk_col
            pk_idx = cols.index(pk)
            rows = sorted(rows, key=lambda r: r[pk_idx])
            show = ex.execute_sql('SHOW CREATE TABLE %s' % t, {})['rows'][0][1]
            out[t] = {
                'rows': rows,
                'show_create': show,
                'auto_increment': dict(db.tables[t].auto_increment_next_values()),
                'wal_bytes': (Path(data_dir) / (t + '.wal')).stat().st_size
                if (Path(data_dir) / (t + '.wal')).is_file() else None,
            }
        return out
    finally:
        db.shutdown()


def _insert_rows(data_dir, values):
    db, ex = _exec(data_dir)
    try:
        for v in values:
            res = ex.execute_sql('INSERT INTO t (v) VALUES (%d)' % v, {})
            assert res.get('type') != 'error', res
        db.checkpoint(force=True)
    finally:
        db.shutdown()


def _update_rows(data_dir, pairs):
    db, ex = _exec(data_dir)
    try:
        for pk, v in pairs:
            res = ex.execute_sql('UPDATE t SET v = %d WHERE id = %d' % (v, pk), {})
            assert res.get('type') != 'error', res
        db.checkpoint(force=True)
    finally:
        db.shutdown()


def _delete_rows(data_dir, pks):
    db, ex = _exec(data_dir)
    try:
        for pk in pks:
            res = ex.execute_sql('DELETE FROM t WHERE id = %d' % pk, {})
            assert res.get('type') != 'error', res
        db.checkpoint(force=True)
    finally:
        db.shutdown()


def _manifest(chain_dir):
    return json.loads((Path(chain_dir) / B.MANIFEST_NAME).read_text(
        encoding='utf-8'))


# ==================== 基本链路 ====================

def test_init_incr_restore_latest_consistent(tmp_path):
    data = _make_db(tmp_path / 'db', rows=300)
    chain = tmp_path / 'chain'
    init = B.chain_init(data, chain)
    assert init['status'] == 'ok' and init['tables'] == 1
    man = _manifest(chain)
    assert man['schema'] == B.CHAIN_SCHEMA
    assert man['format_version'] == B.CHAIN_FORMAT
    assert man['lightdb_version'] == B.LIGHTDB_VERSION
    assert len(man['entries']) == 1 and man['entries'][0]['kind'] == 'full'
    assert man['entries'][0]['binlog_start'] == man['entries'][0]['binlog_end']

    _update_rows(data, [(1, 1001), (2, 1002), (3, 1003)])
    _insert_rows(data, [901, 902, 903, 904, 905])
    r1 = B.chain_incr(data, chain)
    assert r1['status'] == 'ok' and r1['stats']['updates'] == 3
    assert r1['stats']['inserts'] == 5
    entries = _manifest(chain)['entries']
    assert len(entries) == 2
    e = entries[1]
    for field in ('sha256', 'size', 'binlog_start', 'binlog_end', 'state'):
        assert field in e, field
    assert e['binlog_start'] == entries[0]['binlog_end']
    assert (chain / e['file']).is_file()

    _delete_rows(data, [10, 11])
    _insert_rows(data, [910])
    r2 = B.chain_incr(data, chain)
    assert r2['stats']['deletes'] == 2 and r2['stats']['inserts'] == 1

    target = tmp_path / 'restored'
    report = B.chain_restore(chain, target)
    assert report['status'] == 'ok' and report['applied_increments'] == 2
    assert report['state_verified'] is True and report['wal_zero'] is True
    src, dst = _snapshot_rows(data), _snapshot_rows(target)
    assert set(src) == set(dst) == {'t'}
    assert src['t']['rows'] == dst['t']['rows']
    assert src['t']['show_create'] == dst['t']['show_create']
    assert src['t']['auto_increment'] == dst['t']['auto_increment']
    assert dst['t']['wal_bytes'] == 0


def test_restore_to_each_increment(tmp_path):
    data = _make_db(tmp_path / 'db', rows=100)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    states = [dict(_snapshot_rows(data)['t'])]
    _update_rows(data, [(1, 11), (2, 22)])
    B.chain_incr(data, chain)
    states.append(dict(_snapshot_rows(data)['t']))
    _insert_rows(data, list(range(200, 230)))
    B.chain_incr(data, chain)
    states.append(dict(_snapshot_rows(data)['t']))
    _delete_rows(data, [50, 51, 52])
    B.chain_incr(data, chain)
    states.append(dict(_snapshot_rows(data)['t']))
    for n in (0, 1, 2, 3):
        target = tmp_path / ('r%d' % n)
        rep = B.chain_restore(chain, target, to=n, latest=False)
        got = _snapshot_rows(target)['t']
        assert rep['applied_increments'] == n
        assert got['rows'] == states[n]['rows'], n
        assert got['show_create'] == states[n]['show_create']
        assert got['auto_increment'] == states[n]['auto_increment']
        assert got['wal_bytes'] == 0
    with pytest.raises(B.ChainError):
        B.chain_restore(chain, tmp_path / 'bad', to=99, latest=False)


def test_no_change_increment_is_noop(tmp_path):
    data = _make_db(tmp_path / 'db', rows=20)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    r = B.chain_incr(data, chain)
    assert r['status'] == 'noop'
    assert len(_manifest(chain)['entries']) == 1


def test_auto_increment_edge_preserved(tmp_path):
    data = _make_db(tmp_path / 'db', rows=30)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    # 删除当前最大 id -> AUTO_INCREMENT 不应回退；再插入新行
    _delete_rows(data, [30])
    _insert_rows(data, [500])
    B.chain_incr(data, chain)
    target = tmp_path / 'restored'
    B.chain_restore(chain, target)
    src, dst = _snapshot_rows(data)['t'], _snapshot_rows(target)['t']
    assert src['rows'] == dst['rows']
    assert src['auto_increment'] == dst['auto_increment']
    assert 'AUTO_INCREMENT=' in dst['show_create']


def test_ddl_increments_restore(tmp_path):
    data = _make_db(tmp_path / 'db', rows=50)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    db, ex = _exec(data)
    try:
        assert ex.execute_sql(
            'CREATE TABLE other (id INT PRIMARY KEY, note VARCHAR(20))',
            {}).get('type') != 'error'
        assert ex.execute_sql(
            'INSERT INTO other (id, note) VALUES (1, \'a\')',
            {}).get('type') != 'error'
        assert ex.execute_sql(
            'ALTER TABLE other ADD COLUMN extra INT DEFAULT 7',
            {}).get('type') != 'error'
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    r = B.chain_incr(data, chain)
    assert r['stats']['ddl_count'] >= 2
    db, ex = _exec(data)
    try:
        assert ex.execute_sql('DROP TABLE other', {}).get('type') != 'error'
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    r2 = B.chain_incr(data, chain)
    assert r2['stats']['ddl_count'] >= 1
    target = tmp_path / 'restored'
    B.chain_restore(chain, target)
    src, dst = _snapshot_rows(data), _snapshot_rows(target)
    assert set(src) == set(dst) == {'t'}
    assert src['t']['rows'] == dst['t']['rows']

    # --to 1 时 other 表仍存在（CREATE/ALTER 已应用，DROP 在 #2）
    t1 = tmp_path / 'restored_to1'
    B.chain_restore(chain, t1, to=1, latest=False)
    db, ex = _exec(t1)
    try:
        assert 'other' in db.tables
        assert ex.execute_sql('SELECT * FROM other', {})['rows'] == [(1, 'a', 7)]
    finally:
        db.shutdown()


# ==================== 失败显式且不破坏目标 ====================

def _prepare_chain(tmp_path, rows=100):
    data = _make_db(tmp_path / 'db', rows=rows)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    _update_rows(data, [(1, 111)])
    B.chain_incr(data, chain)
    _insert_rows(data, [901, 902])
    B.chain_incr(data, chain)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'sentinel.txt').write_text('must-stay', encoding='utf-8')
    return data, chain, target


def _assert_target_intact(target):
    assert (target / 'sentinel.txt').read_text(encoding='utf-8') == 'must-stay'
    assert not (target / '_meta.json').exists()


def test_missing_increment_file_explicit(tmp_path):
    _data, chain, target = _prepare_chain(tmp_path)
    entry = _manifest(chain)['entries'][-1]
    (chain / entry['file']).unlink()
    with pytest.raises(B.ChainIntegrityError) as ei:
        B.chain_restore(chain, target, force=True)
    assert '缺段' in str(ei.value) or '不存在' in str(ei.value)
    _assert_target_intact(target)


def test_tampered_increment_explicit(tmp_path):
    _data, chain, target = _prepare_chain(tmp_path)
    entry = _manifest(chain)['entries'][-1]
    path = chain / entry['file']
    raw = path.read_bytes()
    path.write_bytes(raw[:-12] + b'0' * 12)
    with pytest.raises(B.ChainIntegrityError) as ei:
        B.chain_restore(chain, target, force=True)
    assert 'sha256' in str(ei.value).lower()
    _assert_target_intact(target)


def test_broken_chain_manifest_explicit(tmp_path):
    _data, chain, target = _prepare_chain(tmp_path)
    man = _manifest(chain)
    man['entries'][-1]['binlog_start'] = 123456789
    (chain / B.MANIFEST_NAME).write_text(
        json.dumps(man, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(B.ChainIntegrityError) as ei:
        B.chain_restore(chain, target, force=True)
    assert '断链' in str(ei.value) or '不连续' in str(ei.value)
    _assert_target_intact(target)


def test_version_incompatible_explicit(tmp_path):
    _data, chain, target = _prepare_chain(tmp_path)
    man = _manifest(chain)
    man['lightdb_version'] = '0.0.0'
    (chain / B.MANIFEST_NAME).write_text(
        json.dumps(man, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(B.ChainVersionError) as ei:
        B.chain_restore(chain, target, force=True)
    assert '不兼容' in str(ei.value)
    _assert_target_intact(target)


def test_target_nonempty_requires_force(tmp_path):
    data, chain, target = _prepare_chain(tmp_path)
    with pytest.raises(B.ChainError) as ei:
        B.chain_restore(chain, target)
    assert '非空' in str(ei.value)
    _assert_target_intact(target)
    # --force 才允许覆盖
    B.chain_restore(chain, target, force=True)
    assert not (target / 'sentinel.txt').exists()
    src = _snapshot_rows(data)['t']
    dst = _snapshot_rows(target)['t']
    assert src['rows'] == dst['rows']


def test_init_refuses_existing_chain(tmp_path):
    data = _make_db(tmp_path / 'db', rows=10)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    with pytest.raises(B.ChainError) as ei:
        B.chain_init(data, chain)
    assert '拒绝覆盖' in str(ei.value)


# ==================== 保留策略 ====================

def test_keep_rebase_and_continue(tmp_path):
    data = _make_db(tmp_path / 'db', rows=100)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    for round_no in range(3):
        _insert_rows(data, [1000 + round_no * 10 + k for k in range(5)])
        B.chain_incr(data, chain)
    assert len(_manifest(chain)['entries']) == 4
    prune = B.chain_prune(chain, 1)
    assert prune['status'] == 'ok'
    man = _manifest(chain)
    assert len(man['entries']) == 2 and man['entries'][0]['kind'] == 'full'
    assert [e['id'] for e in man['entries']] == [0, 1]
    # 旧增量文件已删除，保留的增量存在
    for e in man['entries'][1:]:
        assert (chain / e['file']).is_file()
    target = tmp_path / 'restored'
    B.chain_restore(chain, target)
    src, dst = _snapshot_rows(data)['t'], _snapshot_rows(target)['t']
    assert src['rows'] == dst['rows']

    # prune 后继续增量 + 恢复仍正确（状态缓存已 rebase）
    _update_rows(data, [(1, 7777)])
    B.chain_incr(data, chain)
    assert len(_manifest(chain)['entries']) == 3
    target2 = tmp_path / 'restored2'
    B.chain_restore(chain, target2)
    assert _snapshot_rows(data)['t']['rows'] == _snapshot_rows(target2)['t']['rows']


def test_incr_with_keep_auto_prunes(tmp_path):
    data = _make_db(tmp_path / 'db', rows=50)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    for i in range(3):
        _insert_rows(data, [800 + i])
        B.chain_incr(data, chain, keep=1)
    assert len(_manifest(chain)['entries']) == 2


# ==================== 边界 / 不支持形态 ====================

def test_unsupported_ddl_rejected_unit(tmp_path):
    with pytest.raises(B.ChainUnsupported):
        B._check_ddl(['TRUNCATE TABLE t'])
    with pytest.raises(B.ChainUnsupported):
        B._check_ddl(['RENAME TABLE t TO t2'])
    B._check_ddl(['ALTER TABLE t ADD COLUMN x INT'])


def test_schema_change_without_binlog_ddl_rejected(tmp_path):
    data = _make_db(tmp_path / 'db', rows=10)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    # 绕过 Executor 的 DDL（直接 Table API）不会进 binlog -> 必须显式拒绝
    db = Database(str(data))
    try:
        db.create_table('sneaky', [{'name': 'id', 'type': 'INT',
                                    'primary': True}])
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    with pytest.raises(B.ChainUnsupported) as ei:
        B.chain_incr(data, chain)
    assert 'schema' in str(ei.value).lower() or 'DDL' in str(ei.value)


def test_running_server_detected(tmp_path):
    data = _make_db(tmp_path / 'db', rows=10)
    chain = tmp_path / 'chain'
    port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', port))
        sock.listen(1)
    except OSError:
        pytest.skip('默认服务端口 %d 已被占用（无法构造运行中服务场景）' % port)
    try:
        with pytest.raises(B.ChainError) as ei:
            B.chain_init(data, chain)
        assert '运行中' in str(ei.value) or '拒绝' in str(ei.value)
    finally:
        sock.close()


def test_restore_checks_show_create_and_wal(tmp_path):
    data = _make_db(tmp_path / 'db', rows=20)
    chain = tmp_path / 'chain'
    B.chain_init(data, chain)
    _update_rows(data, [(1, 42)])
    B.chain_incr(data, chain)
    target = tmp_path / 'restored'
    rep = B.chain_restore(chain, target)
    assert rep['state_verified'] and rep['wal_zero']
    for table in _snapshot_rows(target):
        wal = Path(target) / (table + '.wal')
        assert not wal.exists() or wal.stat().st_size == 0


# ==================== 10k 量化 ====================

def test_10k_incremental_quantification(tmp_path):
    data = _make_db(tmp_path / 'db', rows=10_000, batch=500)
    chain = tmp_path / 'chain'
    t0 = time.perf_counter()
    init = B.chain_init(data, chain)
    init_s = time.perf_counter() - t0
    assert init['row_total'] == 10_000
    _update_rows(data, [(i, 5000 + i) for i in range(1, 501)])
    _insert_rows(data, list(range(20_000, 20_500)))
    t0 = time.perf_counter()
    incr = B.chain_incr(data, chain)
    incr_s = time.perf_counter() - t0
    assert incr['status'] == 'ok'
    assert incr['stats']['updates'] == 500
    assert incr['stats']['inserts'] == 500
    t0 = time.perf_counter()
    rep = B.chain_restore(chain, tmp_path / 'restored')
    restore_s = time.perf_counter() - t0
    src = _snapshot_rows(data)['t']
    dst = _snapshot_rows(tmp_path / 'restored')['t']
    assert src['rows'] == dst['rows']
    baseline_bytes = int(init['baseline_bytes'])
    incr_bytes = int(incr['size'])
    assert baseline_bytes > incr_bytes > 0
    # 粗量化断言（真实数字写入 docs；机器负载下只做数量级校验）
    assert init_s > 0 and incr_s > 0 and restore_s > 0
    assert rep['state_verified'] and rep['wal_zero']
    print('\n[10k] baseline=%.1f KB/%.2fs increment=%.1f KB/%.2fs restore=%.2fs'
          % (baseline_bytes / 1024.0, init_s, incr_bytes / 1024.0, incr_s,
             restore_s))
