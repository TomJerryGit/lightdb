# -*- coding: utf-8 -*-
"""G3：结果表虚拟化 / 数据生成器 / 快捷键自定义 / SSL 真实 TLS。"""
import datetime as dt
import ipaddress
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
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


# ==================== 1. 结果表虚拟化 ====================

@pytest.fixture(scope='module')
def big_rows():
    return [[i, round(i * 1.5, 2), f'row-{i:06d}'] for i in range(100000)]


def make_virtual(rows):
    from lightdb_studio.ui.result_table import ResultTable
    rt = ResultTable()
    rt.show()
    rt.show_result(['id', 'amount', 'name'], rows, 1.0)
    qapp().processEvents()
    return rt


def test_virtual_100k_no_widget_items(home, big_rows):
    t0 = time.perf_counter()
    rt = make_virtual(big_rows)
    elapsed = (time.perf_counter() - t0) * 1000
    try:
        assert rt.is_virtual() is True
        assert rt.virtual_model.rowCount() == 100000
        assert rt.table.rowCount() == 0, '虚拟化下不应创建 QTableWidgetItem'
        assert rt.virtual_view.isVisible()
        assert rt.rows_loaded == 100000
        print(f'  → 10 万行模型构建+显示 {elapsed:.0f}ms')
        assert elapsed < 5000, elapsed
        # 只有视图请求的单元格才取数据
        idx = rt.virtual_model.index(99999, 2)
        assert rt.virtual_model.data(idx).startswith('row-099999')
    finally:
        rt.clear()
        rt.close()


def test_virtual_sort_filter_visibility_copy(home, big_rows):
    from PySide6.QtCore import Qt
    rt = make_virtual(big_rows)
    try:
        rt.sort_by(1, Qt.DescendingOrder)
        assert rt.sort_indicator()[0] == 1
        first = rt.virtual_model.data(rt.virtual_model.index(0, 1))
        assert float(first) == round(99999 * 1.5, 2)
        assert rt.filter_by_column(2, 'row-000123') == 1
        rt.clear_filter()
        assert rt.apply_value_filter(0, ['1']) == 1
        assert rt.virtual_visible_rows() == 1
        rt.clear_filter()
        rt.clear_value_filters()
        assert rt.virtual_visible_rows() == 100000
        rt.hide_column(1)
        assert 1 not in rt.visible_columns()
        rt.reset_column_visibility()
        assert rt.visible_columns() == [0, 1, 2]
        # 复制当前选中行（含表头）
        rt.virtual_view.selectRow(0)
        text = rt.copy_with_headers()
        assert text.splitlines()[0] == 'id\tamount\tname'
        assert 'row-' in text
        # 列统计仍基于当前结果集
        stats = rt.column_statistics(1)
        assert stats['count'] == 100000 and stats['non_null'] == 100000
        assert stats['sum'] == pytest.approx(
            sum(round(i * 1.5, 2) for i in range(100000)))
    finally:
        rt.clear()
        rt.close()


def test_virtual_selected_export_real_file(home, big_rows, tmp_path):
    rt = make_virtual(big_rows)
    try:
        rt.virtual_view.selectRow(123)
        csv_res = rt.export_selected(tmp_path / 'one.csv', fmt='csv')
        assert csv_res['ok'] and csv_res['rows'] == 1
        assert (tmp_path / 'one.csv').read_text(encoding='utf-8').strip()
        xlsx_res = rt.export_selected(tmp_path / 'one.xlsx', fmt='xlsx')
        assert xlsx_res['ok'] and xlsx_res['rows'] == 1
        import openpyxl
        assert openpyxl.load_workbook(tmp_path / 'one.xlsx').active.max_row == 2
    finally:
        rt.clear()
        rt.close()


def test_virtual_append_and_switch_back(home, big_rows):
    rt = make_virtual(big_rows[:6000])
    try:
        rt.append_rows([[6000, 1.0, 'extra']], truncated=True)
        assert rt.virtual_model.rowCount() == 6001
        assert rt.rows_loaded == 6001
        # 小结果集回到 QTableWidget 路径（现有 1200 行 UI 行为不变）
        rt.show_result(['a', 'b'], [[1, 2], [3, 4]], 1.0)
        assert rt.is_virtual() is False
        assert rt.table.rowCount() == 2
        assert not rt.virtual_view.isVisible()
    finally:
        rt.clear()
        rt.close()


def test_1200_rows_still_widget_path(home):
    rows = [[i, i * 1.5, f'row-{i}'] for i in range(1200)]
    rt = make_virtual(rows)
    try:
        assert rt.is_virtual() is False
        assert rt.table.rowCount() == 1200
        rt.sort_by(1)
        assert rt.table.item(0, 1).text() == '0.0'
        assert rt.apply_filter('row-1199', column=2) == 1
    finally:
        rt.clear()
        rt.close()


# ==================== 2. 数据生成器 ====================

def test_generate_rows_strategies_and_null(home):
    from lightdb_studio.ui.data_editor import (
        generate_rows, default_strategy_for_type, GENERATOR_STRATEGIES)
    cols = [{'name': 'id', 'type': 'INT'},
            {'name': 'amount', 'type': 'DECIMAL(10,2)'},
            {'name': 'name', 'type': 'VARCHAR(32)'},
            {'name': 'email', 'type': 'VARCHAR(64)'},
            {'name': 'created', 'type': 'DATETIME'}]
    rows = generate_rows(cols, 50, strategies={
        'id': 'sequence', 'amount': 'random_int', 'name': 'name',
        'email': 'email', 'created': 'date'}, null_ratio=0.2, seed=7)
    assert len(rows) == 50 and all(len(r) == 5 for r in rows)
    assert rows[0][0] == 1 and rows[-1][0] == 50
    assert any(r[1] is None for r in rows), 'NULL 比例应生效'
    assert rows[0][2] and ' ' in rows[0][2]
    assert rows[0][3].endswith('@example.test')
    # 相同 seed 可复现
    rows2 = generate_rows(cols, 50, strategies={
        'id': 'sequence', 'amount': 'random_int', 'name': 'name',
        'email': 'email', 'created': 'date'}, null_ratio=0.2, seed=7)
    assert rows2 == rows
    assert default_strategy_for_type('INT') == 'sequence'
    assert 'null' in GENERATOR_STRATEGIES


class FakeInsertConn:
    def __init__(self, fail_at=None):
        self.batches = []
        self.fail_at = fail_at

    def execute_many(self, sql, params):
        self.batches.append((sql, len(params)))
        if self.fail_at is not None and len(self.batches) >= self.fail_at:
            return 0, 'fake insert failure'
        return len(params), None


def test_insert_generated_rows_batches_and_failure(home):
    from lightdb_studio.ui.data_editor import generate_rows, insert_generated_rows
    cols = [{'name': 'id', 'type': 'INT'}, {'name': 'name', 'type': 'VARCHAR(20)'}]
    rows = generate_rows(cols, 25, strategies={'id': 'sequence',
                                               'name': 'name'}, seed=1)
    conn = FakeInsertConn()
    res = insert_generated_rows(conn, 't1', ['id', 'name'], rows, batch_size=10)
    assert res['ok'] and res['inserted'] == 25
    assert [b[1] for b in conn.batches] == [10, 10, 5]
    assert 'INSERT INTO `t1`' in conn.batches[0][0]
    # 失败显式（第 2 批失败；后续批次仍尝试并显式汇总）
    bad = FakeInsertConn(fail_at=2)
    fail = insert_generated_rows(bad, 't1', ['id', 'name'], rows,
                                 batch_size=10)
    assert fail['ok'] is False and fail['failed'] == 15
    assert fail['inserted'] == 10
    assert fail['errors'] and 'fake insert failure' in fail['errors'][0]['error']
    # 取消：第一批提交后停止，不写后续批
    state = {'n': 0}

    def _cancel():
        state['n'] += 1
        return state['n'] > 1

    res = insert_generated_rows(
        FakeInsertConn(), 't1', ['id', 'name'], rows, batch_size=10,
        cancel_cb=_cancel)
    assert res['cancelled'] is True and res['inserted'] == 10


def test_data_generator_dialog_preview_and_sync(home):
    from lightdb_studio.ui.data_editor import DataGeneratorDialog
    cols = [{'name': 'id', 'type': 'INT'},
            {'name': 'name', 'type': 'VARCHAR(32)'},
            {'name': 'amount', 'type': 'DECIMAL(10,2)'}]
    conn = FakeInsertConn()
    dlg = DataGeneratorDialog(conn, 't1', cols)
    try:
        dlg.count_spin.setValue(12)
        rows = dlg.refresh_preview()
        assert len(rows) == 12
        assert dlg.preview.rowCount() == 12
        dlg.count_spin.setValue(25)
        result = dlg.generate_sync()
        assert result['ok'] and result['inserted'] == 25
        assert '插入 25 行' in dlg.status.text()
    finally:
        dlg.close()


# ==================== 3. 快捷键自定义 ====================

def test_shortcut_defaults_validation_and_compat(home):
    from lightdb_studio.ui.shortcuts import (
        DEFAULT_SHORTCUTS, default_shortcuts, format_default_table,
        load_shortcuts, validate_shortcuts)
    assert 'Ctrl+N' in default_shortcuts().values()
    assert 'Ctrl+H' in default_shortcuts().values()
    assert 'LightDB Studio 默认快捷键' in format_default_table()
    assert validate_shortcuts(default_shortcuts())['ok'] is True
    conflict = validate_shortcuts({'find': 'Ctrl+F', 'replace': 'Ctrl+F'})
    assert conflict['ok'] is False and conflict['conflicts']
    assert {i for i in conflict['conflicts'][0]['ids']} == {'find', 'replace'}
    # 旧版中文 label 键兼容
    assert load_shortcuts({'查找': 'Ctrl+Shift+F'})['find'] == 'Ctrl+Shift+F'
    assert load_shortcuts({'run': 'Ctrl+R'})['run'] == 'Ctrl+R'
    assert validate_shortcuts({'find': 'Ctrl+'})['ok'] is False


def test_shortcut_settings_dialog_persists(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import Settings
    from lightdb_studio.ui.settings_dialog import SettingsDialog
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 'settings.json')
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path)
    qapp()
    settings = Settings()
    dlg = SettingsDialog(settings)
    try:
        # 找到 find 行并修改
        idx = dlg._shortcut_ids.index('find')
        dlg.shortcut_table.item(idx, 1).setText('Ctrl+Shift+F')
        result = dlg.apply_settings()
        assert result['ok'] is True
        assert settings.shortcuts['find'] == 'Ctrl+Shift+F'
        assert Settings.load().shortcuts['find'] == 'Ctrl+Shift+F'
        # 冲突显式阻止保存
        seen = []
        monkeypatch.setattr(QMessageBox, 'warning',
                            staticmethod(lambda *a, **k: seen.append(a[2])))
        idx2 = dlg._shortcut_ids.index('replace')
        dlg.shortcut_table.item(idx2, 1).setText('Ctrl+Shift+F')
        bad = dlg.apply_settings()
        assert bad['ok'] is False and bad['conflicts']
        assert seen and '重复/非法' in seen[0]
        # 恢复默认
        dlg._reset_shortcuts()
        assert dlg.shortcut_table.item(idx, 1).text() == 'Ctrl+F'
    finally:
        dlg.close()


def test_shortcuts_apply_to_window_and_editor(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
    from lightdb_studio.ui.shortcuts import apply_to_editor
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE', tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 'cfg' / 'settings.json')
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    cfg.settings.shortcuts = {'new_query': 'Ctrl+Shift+Q',
                              'find': 'Ctrl+Shift+F'}
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None
    try:
        result = win._apply_shortcut_settings()
        assert result['ok'] is True and result['menu_applied'] > 0
        from PySide6.QtGui import QAction
        actions = {a.text(): a for a in win.findChildren(QAction)}
        assert actions['新建查询'].shortcut().toString() == 'Ctrl+Shift+Q'
        editor = win.tabs.widget(0).widget(0)
        assert apply_to_editor(editor, {'find': 'Ctrl+Shift+F'}) >= 1
        assert editor._shortcut_refs['find'].key().toString() == 'Ctrl+Shift+F'
    finally:
        win.close()
        qapp().processEvents()


# ==================== 4. SSL 真实 TLS 端到端 ====================

def _make_cert(tmp, prefix):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'lightdb.local')])
    now = dt.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = tmp / f'{prefix}.crt'
    key_path = tmp / f'{prefix}.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cert_path, key_path


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _shutdown(res):
    if res is None:
        return
    for name in ('shutdown', 'server_close', 'stop', 'close'):
        fn = getattr(res, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def test_real_tls_ca_and_verify_off_and_wrong_ca(home, tmp_path):
    pytest.importorskip('cryptography')
    pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    cert, key = _make_cert(tmp_path, 'server')
    wrong_ca, _wk = _make_cert(tmp_path, 'wrong')
    port = _free_port()
    data_dir = str(tmp_path / 'data')
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.ssl_enabled = True
    cfg.ssl_cert = str(cert)
    cfg.ssl_key = str(key)
    db = ex = srv = None
    try:
        db = Database(data_dir)
        ex = Executor(db)
        srv = MySQLServer(cfg, db, ex)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        deadline = time.time() + 8
        ready = False
        while time.time() < deadline:
            try:
                s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
                s.close()
                ready = True
                break
            except OSError:
                time.sleep(0.05)
        assert ready, 'TLS server 8s 内未监听'
        from lightdb_studio.config import ConnectionInfo
        from lightdb_studio.db.connection import DBConnection
        # 1) 关闭校验：真实 TLS 连接 + 查询成功
        info_off = ConnectionInfo(name='tls-off', host='127.0.0.1',
                                  port=port, user='root', password='',
                                  ssl=True, ssl_options={})
        c_off = DBConnection(info_off)
        c_off.connect()
        cols, rows = c_off.query('SELECT 1 AS ok')
        assert rows == [(1,)]
        c_off.close()
        # 2) CA + required 校验：真实 TLS 连接 + 查询成功
        info_ca = ConnectionInfo(
            name='tls-ca', host='127.0.0.1', port=port, user='root',
            password='', ssl=True,
            ssl_options={'ca': str(cert), 'verify_mode': 'required'})
        c_ca = DBConnection(info_ca)
        c_ca.connect()
        assert c_ca.query('SELECT 2 AS ok')[1] == [(2,)]
        c_ca.close()
        # 3) 错误 CA：显式失败，不降级明文
        info_bad = ConnectionInfo(
            name='tls-bad', host='127.0.0.1', port=port, user='root',
            password='', ssl=True,
            ssl_options={'ca': str(wrong_ca), 'verify_mode': 'required'})
        c_bad = DBConnection(info_bad)
        with pytest.raises(Exception) as excinfo:
            c_bad.connect()
        assert c_bad.conn is None
        assert 'certificate' in str(excinfo.value).lower() or \
               'ssl' in str(excinfo.value).lower()
    finally:
        for res in (srv, db):
            _shutdown(res)
