# -*- coding: utf-8 -*-
"""task-83：Studio 生产化面板（复制状态 / metrics 摘要 / 快照 CLI）offscreen。"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
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
    qapp()
    return tmp_path


class FakeReplConn:
    def __init__(self, fail=False, empty=False):
        self.fail = fail
        self.empty = empty

    def query(self, sql, params=None):
        if self.fail:
            raise RuntimeError('SHOW SLAVE STATUS 不支持')
        if self.empty:
            return ['Master_Host'], []
        return (['Master_Host', 'Master_Port', 'Master_Log_File',
                 'Read_Master_Log_Pos', 'Exec_Master_Log_Pos',
                 'Seconds_Behind_Master', 'Slave_IO_Running',
                 'Slave_SQL_Running', 'Last_Error'],
                [('master.local', 3306, 'bin.000003', 1024, 900, 12,
                  'Yes', 'Yes', '')])


SAMPLE_METRICS = """\
# HELP lightdb_queries_per_second QPS
# TYPE lightdb_queries_per_second gauge
lightdb_queries_per_second 12.5
lightdb_connections_current 7
lightdb_errors_total{type="query"} 3
lightdb_slow_queries_total 2
lightdb_wal_bytes 4096
lightdb_checkpoint_total 5
lightdb_buffer_pool_pages_free 128
"""


def test_replication_show_status_and_missing_fields(home):
    from lightdb_studio.ui.production_panel import replication_status
    status = replication_status(FakeReplConn())
    assert status['configured'] is True
    assert status['fields']['master_host']['value'] == 'master.local'
    assert status['fields']['master_port']['value'] == '3306'
    assert status['fields']['read_pos']['value'] == '1024'
    assert status['fields']['exec_pos']['value'] == '900'
    assert status['fields']['lag_seconds']['value'] == '12'
    assert status['fields']['last_error']['value'] == '—'
    # SHOW 失败 + 无文件 → 未配置显式提示，不伪造
    missing = replication_status(FakeReplConn(fail=True), '')
    assert missing['configured'] is False
    assert '未配置复制' in missing['notice']


def test_replication_state_file_incremental_fields(home, tmp_path):
    from lightdb_studio.ui.production_panel import replication_status
    state = {
        'master_host': 'm1', 'master_port': 3307,
        'master_file': 'bin.0009', 'last_seq': 77, 'lsn': 77,
        'lag_ms': 2500, 'applied': 42, 'idempotent': 3, 'errors': 1,
        'last_error': 'boom', 'last_event_type': 'ROW',
        'new_incremental_field': 'compat',
    }
    path = tmp_path / 'replica_state.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    status = replication_status(None, str(path))
    assert status['configured'] is True
    fields = status['fields']
    assert fields['master_host']['value'] == 'm1'
    assert fields['exec_pos']['value'] == '77'
    assert fields['lag_seconds']['value'] == '2.5'
    assert fields['applied']['value'] == '42'
    assert fields['idempotent']['value'] == '3'
    assert fields['errors']['value'] == '1'
    assert fields['last_event_type']['value'] == 'ROW'
    # 未提供/未知字段不崩，未知字段显式列出
    assert 'unknown_fields' in status
    assert 'new_incremental_field' in status['unknown_fields']


def test_metrics_parser_and_panel_retry(home, monkeypatch):
    from lightdb_studio.ui import production_panel as pp
    summary = pp.parse_prometheus(SAMPLE_METRICS)
    assert summary['qps']['value'] == 12.5
    assert summary['connections']['value'] == 7
    assert summary['errors']['value'] == 3
    assert summary['slow']['value'] == 2
    assert summary['wal']['value'] == 4096
    assert summary['checkpoint']['value'] == 5
    assert summary['pool']['value'] == 128
    missing = pp.parse_prometheus('lightdb_queries_total 3\n')
    assert missing['connections']['value'] is None
    assert missing['connections']['missing'] == '—'
    calls = {'n': 0}

    def fake_fetch(url, timeout=5.0):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('connection refused')
        return SAMPLE_METRICS

    monkeypatch.setattr(pp, 'fetch_metrics', fake_fetch)
    panel = pp.MetricsPanel('http://metrics.local/metrics')
    try:
        assert panel.refresh() is None
        assert '不可达' in panel.status.text()
        result = panel.refresh()  # 重试
        assert result['qps']['value'] == 12.5
        assert panel.table.rowCount() == 7
        assert '已拉取' in panel.status.text()
        assert 'lightdb_queries_per_second' in panel.raw.toPlainText()
    finally:
        panel.close()


def test_replication_and_snapshot_panels(home, monkeypatch):
    from lightdb_studio.ui import production_panel as pp
    conn = FakeReplConn()
    rp = pp.ReplicationPanel(lambda: conn)
    try:
        status = rp.refresh()
        assert status['configured'] is True
        assert rp.table.rowCount() >= 9
        rp.conn_getter = lambda: FakeReplConn(empty=True)
        rp.state_edit.setText('')
        empty = rp.refresh()
        assert empty['configured'] is False
        assert '未配置复制' in rp.info.text()
    finally:
        rp.close()

    calls = []

    def fake_cli(mode, **kwargs):
        calls.append((mode, kwargs))
        return {'ok': True, 'returncode': 0, 'command': ['snapshot'],
                'stdout': 'snapshot done', 'stderr': '', 'error': ''}

    monkeypatch.setattr(pp, 'run_snapshot_cli', fake_cli)
    sp = pp.SnapshotCLIPanel()
    try:
        sp.data_edit.setText('/data')
        sp.out_edit.setText('/snap')
        result = sp.run_backup()
        assert result['ok'] is True
        assert calls and calls[0][0] == 'backup'
        assert 'snapshot done' in sp.log.toPlainText()
        # 恢复：取消则不调用 CLI
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.No))
        sp.snapshot_edit.setText('/snap')
        res = sp.run_restore()
        assert res.get('cancelled') is True
        assert len(calls) == 1
        # 确认后才执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        res2 = sp.run_restore()
        assert res2['ok'] is True and calls[-1][0] == 'restore'
    finally:
        sp.close()


def test_production_panel_tabs_and_main_menu(home, tmp_path, monkeypatch):
    from lightdb_studio.ui.production_panel import ProductionPanel
    panel = ProductionPanel(conn_getter=lambda: None)
    try:
        tabs = panel.findChild(__import__(
            'PySide6.QtWidgets', fromlist=['QTabWidget']).QTabWidget)
        assert tabs.count() == 3
        assert [tabs.tabText(i) for i in range(3)] == [
            '复制状态', '指标摘要', '快照备份/恢复']
    finally:
        panel.close()
