# -*- coding: utf-8 -*-
"""task-90：Studio 备份中心（链解析/参数/确认/失败/真实 CLI 一致性）。"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_BINLOG_ENABLED', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

_APP = None


def qapp():
    from PySide6.QtWidgets import QApplication
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.setenv('LIGHTDB_BINLOG_ENABLED', '1')
    qapp()
    return tmp_path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def make_chain(tmp_path, incr_rows=3):
    chain = tmp_path / 'chains' / 'chain-a'
    full = chain / 'full_0000'
    full.mkdir(parents=True)
    (full / 'manifest.json').write_text(json.dumps({'ok': True}),
                                        encoding='utf-8')
    raw = (json.dumps({'schema': 'x', 'id': 1}) + '\n').encode()
    incr_file = chain / 'incr_0001_1.json'
    incr_file.write_bytes(raw)
    manifest = {
        'schema': 'lightdb-backup-chain/1', 'format_version': 1,
        'lightdb_version': 'x', 'created_at': '2026-11-01 10:00:00',
        'source_data_dir': str(tmp_path / 'data'),
        'entries': [
            {'id': 0, 'kind': 'full', 'dir': 'full_0000',
             'created_at': '2026-11-01 10:00:00', 'binlog_start': 10,
             'binlog_end': 10, 'snapshot_bytes': 2048,
             'snapshot_files': 3, 'snapshot_row_total': 100,
             'state': {'users': {'row_count': 100}},
             'manifest_sha256': _sha(b'{}')},
            {'id': 1, 'kind': 'incr', 'file': incr_file.name,
             'created_at': '2026-11-01 10:05:00', 'binlog_start': 10,
             'binlog_end': 20, 'size': len(raw), 'sha256': _sha(raw),
             'stats': {'tables_changed': 1, 'inserts': incr_rows,
                       'updates': 2, 'deletes': 1, 'ddl_count': 0},
             'state': {'users': {'row_count': 100 + incr_rows}},
             'binlog_gap': False},
        ],
    }
    (chain / 'chain_manifest.json').write_text(
        json.dumps(manifest), encoding='utf-8')
    return chain


def test_chain_parse_preview_and_invalid(home, tmp_path):
    from lightdb_studio.ui import backup_center as bc
    chain = make_chain(tmp_path)
    rows = bc.chain_rows(chain)
    assert rows['ok'] is True
    assert [r['kind'] for r in rows['rows']] == ['full', 'incr']
    assert rows['rows'][1]['verified'] is True
    assert rows['rows'][1]['size'] > 0
    listing = bc.list_chains(tmp_path / 'chains')
    assert listing['ok'] and len(listing['chains']) == 1
    p = bc.preview_restore(chain, 1)
    assert p['ok'] is True
    assert p['preview']['applied_increments'] == 1
    assert p['preview']['inserts'] == 3
    assert p['preview']['rows_total'] == 103
    assert p['preview']['target_segment'] == 1
    # 篡改增量 → 校验失败，restore 预览必须拒绝
    (chain / 'incr_0001_1.json').write_bytes(b'broken')
    bad = bc.chain_rows(chain)
    assert bad['ok'] is False and bad['problems']
    assert bc.preview_restore(chain, 1)['ok'] is False
    # 不存在链显式错误
    missing = bc.chain_rows(tmp_path / 'no-such-chain')
    assert missing['ok'] is False and 'manifest' in missing['error']


def test_cli_args_and_invalid_action():
    from lightdb_studio.ui.backup_center import build_cli_args, run_chain_cli
    args = build_cli_args('restore', chain_dir='c', target='t', to=2,
                          force=True, json_path='r.json')
    assert 'restore' in args and '--chain-dir' in args
    assert args[args.index('--to') + 1] == '2'
    assert '--force' in args and '--json' in args
    args2 = build_cli_args('prune', chain_dir='c', keep=3)
    assert args2[args2.index('--keep') + 1] == '3'
    bad = run_chain_cli('bogus')
    assert bad['ok'] is False


def test_backup_center_dialog_confirm_and_verify(home, tmp_path,
                                                 monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from lightdb_studio.ui import backup_center as bc
    chain = make_chain(tmp_path)
    dlg = bc.BackupCenterDialog()
    try:
        dlg.root_edit.setText(str(tmp_path / 'chains'))
        dlg.refresh_chains()
        assert dlg.chain_table.rowCount() == 2
        assert dlg.chain_combo.count() == 1
        dlg.chain_combo.setCurrentIndex(0)
        dlg.segment_combo.setCurrentIndex(1)
        dlg.target_edit.setText(str(tmp_path / 'restore-target'))
        p = dlg.preview_restore()
        assert p['ok'] is True
        assert dlg.preview_table.rowCount() >= 8
        # 取消二次确认 → 不启动 worker/不执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.No))
        dlg.restore_selected()
        assert '取消' in dlg.restore_status.text()
        assert dlg.worker is None
        # 成功恢复结果 → 逐表校验表填充
        dlg._show_verify({'ok': True, 'report': {
            'tables': 1, 'row_total': 103, 'state_verified': True,
            'wal_zero': True, 'total_s': 0.2},
            'chain_dir': str(chain)})
        assert dlg.verify_table.rowCount() == 1
        assert '状态校验通过' in dlg.restore_status.text()
        # 失败展示显式
        warnings = []
        monkeypatch.setattr(QMessageBox, 'warning',
                            staticmethod(lambda *a, **k: warnings.append(a[2])))
        dlg._on_done({'ok': False, 'returncode': 2,
                      'error': '链不存在', 'report': {}})
        assert '失败' in dlg.chain_status.text()
        assert warnings and '链不存在' in warnings[0]
    finally:
        dlg.close()


def test_real_cli_init_incr_restore_consistency(home, tmp_path):
    """真实 CLI 子进程：init → 修改数据 → incr → restore → 行一致。"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb_studio.ui.backup_center import run_chain_cli

    data = tmp_path / 'data'
    data.mkdir()
    db = Database(str(data))
    try:
        ex = Executor(db)
        res = ex.execute_sql(
            'CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, v INT)', {})
        assert res.get('type') != 'error', res
        for i in range(10):
            res = ex.execute_sql('INSERT INTO t (v) VALUES (%d)' % i, {})
            assert res.get('type') != 'error', res
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    chain = tmp_path / 'chain'
    init = run_chain_cli('init', data_dir=str(data), chain_dir=str(chain),
                         timeout=180)
    assert init['ok'] is True, init
    assert init['report'].get('status') == 'ok'
    # 源库改数据，生成真实增量
    db = Database(str(data))
    try:
        ex = Executor(db)
        for i in range(10, 15):
            ex.execute_sql('INSERT INTO t (v) VALUES (%d)' % i, {})
        ex.execute_sql('UPDATE t SET v = 999 WHERE id = 1', {})
        db.checkpoint(force=True)
    finally:
        db.shutdown()
    incr = run_chain_cli('incr', data_dir=str(data), chain_dir=str(chain),
                         timeout=180)
    assert incr['ok'] is True, incr
    # 源库当前行（真实值）
    db = Database(str(data))
    try:
        ex = Executor(db)
        cols, rows = ex.execute_sql('SELECT id, v FROM t', {}).get(
            'columns', []), ex.execute_sql('SELECT id, v FROM t', {}).get('rows', [])
    finally:
        db.shutdown()
    target = tmp_path / 'restored'
    restore = run_chain_cli('restore', chain_dir=str(chain),
                            target=str(target), force=True, timeout=180)
    assert restore['ok'] is True, restore
    assert restore['report'].get('state_verified') is True
    assert restore['report'].get('wal_zero') is True
    db = Database(str(target))
    try:
        ex = Executor(db)
        res = ex.execute_sql('SELECT id, v FROM t ORDER BY id', {})
        assert res.get('type') != 'error', res
        restored = res.get('rows')
    finally:
        db.shutdown()
    source = sorted(rows)
    assert sorted(restored) == source, (restored, source)
    # 篡改/不存在链错误显式（真实 CLI）
    bad = run_chain_cli('restore', chain_dir=str(tmp_path / 'none'),
                        target=str(tmp_path / 'x'), force=True, timeout=60)
    assert bad['ok'] is False and bad['returncode'] not in (None, 0)
    assert bad['error']
