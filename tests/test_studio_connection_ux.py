# -*- coding: utf-8 -*-
"""task-87：连接对话框体验 / 默认端口 / SSH·SSL 真实状态 / 测试连接。"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
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


def _free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]; s.close(); return p


def _start_engine(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    port = _free_port()
    cfg = Config(); cfg.host = '127.0.0.1'; cfg.port = port
    db = Database(str(tmp_path / 'data'))
    srv = MySQLServer(cfg, db, Executor(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=.2)
            s.close(); break
        except OSError:
            time.sleep(.05)
    return port, srv


def test_default_port_from_last_connection_and_fallback(home, tmp_path,
                                                       monkeypatch):
    from lightdb_studio.config import AppConfig, ConnectionInfo
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    from lightdb_studio.ui.main_window import MainWindow

    class Dummy:
        pass

    cfg = AppConfig()
    cfg.connections = []
    cfg.last_connection = ''
    dummy = Dummy(); dummy.config = cfg
    assert MainWindow._suggest_connection_port(dummy) == 8898
    cfg.connections = [ConnectionInfo(name='old', host='h', port=8899)]
    dummy.config = cfg
    assert MainWindow._suggest_connection_port(dummy) == 8899
    cfg.add_connection(ConnectionInfo(name='last', host='h', port=3306))
    cfg.last_connection = 'last'
    dummy.config = cfg
    assert MainWindow._suggest_connection_port(dummy) == 3306
    # 对话框显式 default_port（主窗口传入上次连接/8898）
    dlg = ConnectionDialog(default_port=8898)
    try:
        assert dlg.port_spin.value() == 8898
        assert dlg.auto_connect_requested() is True
        assert dlg.auto_connect_check.isChecked()
        dlg.auto_connect_check.setChecked(False)
        assert dlg.auto_connect_requested() is False
    finally:
        dlg.close()


def test_validation_password_toggle_ssh_ssl_status(home, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    from PySide6.QtCore import Qt
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda *a, **k: warnings.append(a[2])))
    dlg = ConnectionDialog(default_port=8898)
    try:
        dlg.name_edit.setText('x')
        dlg.host_edit.setText('')
        ok, error = dlg._validate_fields()
        assert ok is False and '主机' in error
        dlg.host_edit.setText('127.0.0.1')
        ok, error = dlg._validate_fields()
        assert ok is True
        dlg.port_spin.setValue(65535)
        assert dlg._validate_fields()[0] is True
        # 口令显示切换
        dlg.pwd_edit.setText('secret')
        dlg.pwd_toggle_btn.setChecked(True)
        from PySide6.QtWidgets import QLineEdit
        assert dlg.pwd_edit.echoMode() == QLineEdit.Normal
        assert dlg.pwd_toggle_btn.text() == '隐藏'
        dlg.pwd_toggle_btn.setChecked(False)
        assert dlg.pwd_edit.echoMode() == QLineEdit.Password
        # SSH 状态真实（paramiko 已安装环境）
        if not dlg._ssh_available:
            pytest.skip('paramiko 未安装，显式跳过 SSH 可用状态断言')
        dlg.ssh_check.setChecked(True)
        assert 'paramiko' in dlg.ssh_status.text()
        assert '已启用' in dlg.ssh_status.text()
        dlg.ssh_key_edit.setText(str(tmp_path / 'missing.key'))
        dlg._update_ssh_controls()
        assert '不存在' in dlg.ssh_status.text()
        # SSL 状态真实：文件不存在显式
        dlg.ssh_check.setChecked(False)
        dlg.ssl_check.setChecked(True)
        dlg.ssl_ca_edit.setText(str(tmp_path / 'missing-ca.pem'))
        dlg._update_ssl_controls()
        assert '不存在' in dlg.ssl_status.text()
        assert dlg.validate_ssl_paths()[0] is False
        # 校验失败时 _on_accept 不关闭
        from PySide6.QtWidgets import QDialog
        dlg.name_edit.setText('x')
        dlg._on_accept()
        assert dlg.result() != QDialog.Accepted
        assert warnings
    finally:
        dlg.close()


def test_memory_last_values(home):
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    ConnectionDialog._LAST_FORM = {
        'host': 'remembered.local', 'port': 8898, 'user': 'u',
        'database': 'db_remembered', 'charset': 'utf8mb4'}
    try:
        dlg = ConnectionDialog()
        assert dlg.host_edit.text() == 'remembered.local'
        assert dlg.user_edit.text() == 'u'
        assert dlg.db_edit.text() == 'db_remembered'
        assert dlg.port_spin.value() == 8898
        assert dlg.pwd_edit.text() == ''  # 口令不记忆
        dlg.close()
    finally:
        ConnectionDialog._LAST_FORM = {}


def test_real_test_connection_and_error(home, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    port, srv = _start_engine(tmp_path)
    infos, errors = [], []
    monkeypatch.setattr(QMessageBox, 'information',
                        staticmethod(lambda *a, **k: infos.append(a[2])))
    import lightdb_studio.ui.connection_dialog as cd
    monkeypatch.setattr(cd, 'show_connection_error',
                        lambda parent, exc, host=None, port=None,
                        title='失败': errors.append(str(exc)))
    try:
        dlg = ConnectionDialog(default_port=port)
        try:
            dlg.name_edit.setText('real')
            dlg.host_edit.setText('127.0.0.1')
            dlg.port_spin.setValue(port)
            dlg.user_edit.setText('root')
            dlg._test_connection()
            assert infos and '连接成功' in infos[0], infos
            # 关闭端口失败 → 真实错误进入可行动提示，不假成功
            closed = _free_port()
            dlg.port_spin.setValue(closed)
            dlg._test_connection()
            assert errors, '失败没有走 show_connection_error'
        finally:
            dlg.close()
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass


def test_default_port_ssh_remote_linked():
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    dlg = ConnectionDialog(default_port=8898)
    try:
        assert dlg.ssh_remote_port_spin.value() == 8898
        dlg.port_spin.setValue(8896)
        # 主端口变化后远程端口不强制覆盖用户值；默认初始联动一致即可
        assert dlg.port_spin.value() == 8896
    finally:
        dlg.close()
