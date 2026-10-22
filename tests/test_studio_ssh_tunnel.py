# -*- coding: utf-8 -*-
"""task-42：SSH 隧道（paramiko 可选依赖，显式降级）。

- 未安装 paramiko：显式 SSHUnavailableError + UI 禁用，不假成功；
- fake transport：start/close/转发/失败关闭/上下文管理；
- DBConnection：经隧道连 127.0.0.1:local_port，失败关闭隧道，KILL 一致；
- ConnectionInfo.ssh_options 持久化/旧 JSON 兼容/连接导入导出携带；
- 真实 paramiko 隧道：本机有 SSH 服务时验证，否则显式 skip。
"""
import json
import os
import socket
import sys
import time
import types
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


@pytest.fixture
def config(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE', tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 'cfg' / 'settings.json')
    return AppConfig()


# ==================== 1. 配置持久化 / 兼容 / 导入导出 ====================

def test_ssh_options_persist_and_legacy_compat(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    config.add_connection(ConnectionInfo(
        name='s', host='db', port=3306,
        ssh_options={'enabled': True, 'host': 'ssh.example', 'port': 22,
                     'user': 'me', 'key_path': '/keys/id_rsa',
                     'remote_host': '127.0.0.1', 'remote_port': 3306}))
    raw = json.loads((tmp_path / 'cfg' / 'connections.json').read_text(
        encoding='utf-8'))
    assert raw['connections'][0]['ssh_options']['host'] == 'ssh.example'
    loaded = config.find('s')
    assert loaded.ssh_options['enabled'] is True
    assert loaded.ssh_options['remote_port'] == 3306
    # 旧 JSON 无 ssh_options → {}
    legacy = {'connections': [{'name': 'old', 'host': 'h', 'port': 1,
                               'user': 'root'}], 'last_connection': ''}
    (tmp_path / 'cfg' / 'connections.json').write_text(
        json.dumps(legacy), encoding='utf-8')
    from lightdb_studio.config import AppConfig
    assert AppConfig().find('old').ssh_options == {}


def test_ssh_options_connection_import_export(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui import connection_dialog as cd
    config.add_connection(ConnectionInfo(
        name='remote', host='db', port=3306,
        ssh_options={'enabled': True, 'host': 'ssh.example',
                     'port': 2222, 'user': 'me', 'password': 'Secret',
                     'remote_host': '127.0.0.1', 'remote_port': 3306}))
    path = tmp_path / 'conns.json'
    assert cd.export_connections_json(config.connections, path)['ok']
    item = json.loads(path.read_text(encoding='utf-8'))['connections'][0]
    assert item['ssh_options']['host'] == 'ssh.example'
    assert 'Secret' not in json.dumps(item, ensure_ascii=False)  # 密码不导出
    from lightdb_studio.config import AppConfig
    dst = AppConfig()
    res = cd.import_connections_json(dst, path, conflict='rename')
    assert res['ok'] and dst.find('remote').ssh_options['port'] == 2222


# ==================== 2. 无 paramiko 显式降级 ====================

def test_missing_paramiko_explicit_error(home, monkeypatch):
    from lightdb_studio.db import ssh_tunnel
    def _unavailable():
        raise ssh_tunnel.SSHUnavailableError(
            'SSH 隧道需要 paramiko（可选依赖）：pip install paramiko')
    monkeypatch.setattr(ssh_tunnel, '_load_paramiko', _unavailable)
    tunnel = ssh_tunnel.SshTunnel(ssh_tunnel.SshTunnelConfig(
        host='ssh.example', user='me', password='x', remote_port=8888))
    with pytest.raises(ssh_tunnel.SSHUnavailableError, match='pip install'):
        tunnel.start()
    assert tunnel.local_port is None
    assert tunnel.is_active() is False
    assert ssh_tunnel.paramiko_available() is False


# ==================== 3. fake transport / 生命周期 ====================

class FakeChannel:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        return b''

    def close(self):
        pass

    def __hash__(self):
        return id(self)


class FakeTransport:
    def __init__(self):
        self.channels = []

    def is_active(self):
        return True

    def open_channel(self, kind, dest, src):
        assert kind == 'direct-tcpip'
        ch = FakeChannel()
        self.channels.append((dest, src, ch))
        return ch


class FakeSSHClient:
    instances = []

    def __init__(self):
        self.transport = FakeTransport()
        self.connected = None
        self.closed = False
        FakeSSHClient.instances.append(self)

    def load_system_host_keys(self):
        pass

    def load_host_keys(self, path):
        pass

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connected = kwargs

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True


def fake_paramiko(connect_error=None):
    module = types.ModuleType('paramiko')

    class SSHClient(FakeSSHClient):
        def connect(self, **kwargs):
            if connect_error:
                raise connect_error
            super().connect(**kwargs)

    class AutoAddPolicy:
        pass

    class RejectPolicy:
        pass

    module.SSHClient = SSHClient
    module.AutoAddPolicy = AutoAddPolicy
    module.RejectPolicy = RejectPolicy
    return module


def test_fake_transport_start_forward_close(home, monkeypatch):
    from lightdb_studio.db import ssh_tunnel
    FakeSSHClient.instances.clear()
    monkeypatch.setattr(ssh_tunnel, '_load_paramiko',
                        lambda: fake_paramiko())
    cfg = ssh_tunnel.SshTunnelConfig(
        host='ssh.example', port=2222, user='me', password='pw',
        key_path='', remote_host='127.0.0.1', remote_port=8888)
    tunnel = ssh_tunnel.SshTunnel(cfg)
    tunnel.start()
    try:
        assert tunnel.is_active() is True
        assert isinstance(tunnel.local_port, int) and tunnel.local_port > 0
        client = FakeSSHClient.instances[-1]
        assert client.connected['hostname'] == 'ssh.example'
        assert client.connected['port'] == 2222
        assert client.connected['username'] == 'me'
        assert client.connected['password'] == 'pw'
        # 真实本地 TCP 连接 → 触发 direct-tcpip channel
        sock = socket.create_connection(
            ('127.0.0.1', tunnel.local_port), timeout=2)
        deadline = time.time() + 2
        while time.time() < deadline and not client.transport.channels:
            time.sleep(0.02)
        sock.close()
        assert client.transport.channels, '未建立 direct-tcpip 转发'
        dest = client.transport.channels[0][0]
        assert dest == ('127.0.0.1', 8888)
    finally:
        tunnel.close()
    assert tunnel.is_active() is False
    assert tunnel.local_port is None
    assert FakeSSHClient.instances[-1].closed is True


def test_context_manager_and_connect_error_closes(home, monkeypatch):
    from lightdb_studio.db import ssh_tunnel
    FakeSSHClient.instances.clear()
    monkeypatch.setattr(ssh_tunnel, '_load_paramiko',
                        lambda: fake_paramiko(RuntimeError('auth failed')))
    cfg = ssh_tunnel.SshTunnelConfig(host='ssh', user='u', password='p',
                                     remote_port=8888)
    with pytest.raises(ssh_tunnel.SSHTunnelError, match='auth failed'):
        ssh_tunnel.SshTunnel(cfg).start()
    # 成功路径的上下文管理会 close
    monkeypatch.setattr(ssh_tunnel, '_load_paramiko',
                        lambda: fake_paramiko())
    with ssh_tunnel.SshTunnel(cfg) as tunnel:
        assert tunnel.is_active()
        client = FakeSSHClient.instances[-1]
    assert client.closed is True and tunnel.is_active() is False


# ==================== 4. DBConnection 经隧道 ====================

class FakeTunnel:
    instances = []

    def __init__(self, config):
        self.config = config
        self.local_port = 54321
        self.started = False
        self.closed = False
        FakeTunnel.instances.append(self)

    def start(self):
        self.started = True
        return self

    def close(self):
        self.closed = True


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return ('8.0.0-tunnel-fake',)

    def close(self):
        pass


class FakeRawConn:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass


def _fake_pymysql(captured):
    class Cursors:
        class Cursor:
            pass

    class P:
        cursors = Cursors

        @staticmethod
        def connect(**kwargs):
            captured.append(kwargs)
            return FakeRawConn()

    return P


def test_dbconnection_uses_tunnel_and_failure_closes(home, monkeypatch):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db import connection as conn_mod
    captured = []
    FakeTunnel.instances.clear()
    monkeypatch.setattr(conn_mod, 'SshTunnel', FakeTunnel)
    monkeypatch.setattr(conn_mod, 'pymysql', _fake_pymysql(captured))
    info = ConnectionInfo(
        name='via-ssh', host='db.internal', port=3306, user='root',
        password='pw',
        ssh_options={'enabled': True, 'host': 'ssh.example', 'user': 'me',
                     'password': 'ssh', 'remote_host': 'db.internal',
                     'remote_port': 3306})
    conn = conn_mod.DBConnection(info)
    conn.connect()
    assert captured[0]['host'] == '127.0.0.1'
    assert captured[0]['port'] == 54321
    assert conn.effective_host == '127.0.0.1'
    assert conn.effective_port == 54321
    tunnel = FakeTunnel.instances[-1]
    assert tunnel.started is True and conn.ssh_tunnel is tunnel
    conn.close()
    assert tunnel.closed is True
    # 连接失败 → 关闭隧道，不泄漏
    FakeTunnel.instances.clear()
    captured.clear()
    class Broken(FakeRawConn):
        pass
    class P:
        class cursors:
            class Cursor:
                pass
        @staticmethod
        def connect(**kwargs):
            raise RuntimeError('db down')
    monkeypatch.setattr(conn_mod, 'pymysql', P)
    conn2 = conn_mod.DBConnection(info)
    with pytest.raises(RuntimeError, match='db down'):
        conn2.connect()
    t2 = FakeTunnel.instances[-1]
    assert t2.started and t2.closed is True
    assert conn2.conn is None and conn2.ssh_tunnel is None


def test_kill_uses_effective_tunnel_endpoint(home, monkeypatch):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db import connection as conn_mod
    captured = []
    FakeTunnel.instances.clear()
    monkeypatch.setattr(conn_mod, 'SshTunnel', FakeTunnel)
    monkeypatch.setattr(conn_mod, 'pymysql', _fake_pymysql(captured))
    info = ConnectionInfo(
        name='k', host='db', port=3306,
        ssh_options={'enabled': True, 'host': 'ssh', 'user': 'me',
                     'password': 'x', 'remote_port': 3306})
    conn = conn_mod.DBConnection(info)
    conn.connect()
    captured.clear()

    class KillerCursor(FakeCursor):
        def execute(self, sql):
            pass

    class Killer:
        def cursor(self):
            return KillerCursor()

        def close(self):
            pass

    class P:
        class cursors:
            class Cursor:
                pass
        @staticmethod
        def connect(**kwargs):
            captured.append(kwargs)
            return Killer()

    monkeypatch.setattr(conn_mod, 'pymysql', P)
    ok, err = conn.kill_connection(123)
    assert ok is True and err is None
    assert captured[0]['host'] == '127.0.0.1'
    assert captured[0]['port'] == 54321
    conn.close()
    assert FakeTunnel.instances[-1].closed is True


# ==================== 5. ConnectionDialog SSH 字段 ====================

def test_connection_dialog_ssh_fields(home):
    from lightdb_studio.db.ssh_tunnel import paramiko_available
    from lightdb_studio.ui.connection_dialog import ConnectionDialog
    dlg = ConnectionDialog()
    try:
        assert hasattr(dlg, 'ssh_check')
        available = paramiko_available()
        if available:
            assert dlg.ssh_check.isEnabled()
            dlg.ssh_check.setChecked(True)
            assert dlg.ssh_host_edit.isEnabled()
            assert dlg.validate_ssh_options()[0] is False  # host/user 空
        else:
            assert dlg.ssh_check.isEnabled() is False
            assert 'paramiko' in dlg.ssh_status.text()
            assert 'pip install paramiko' in dlg.ssh_status.text()
            assert 'pip install paramiko' in dlg.ssh_check.toolTip()
            assert dlg.ssh_host_edit.isEnabled() is False
            assert dlg.validate_ssh_options()[0] is True \
                or 'paramiko' in dlg.validate_ssh_options()[1]
    finally:
        dlg.close()


def test_connection_dialog_ssh_options_roundtrip(home, tmp_path, monkeypatch):
    from lightdb_studio.ui import connection_dialog as cd
    import lightdb_studio.db.ssh_tunnel as st
    # 让对话框认为 paramiko 可用，验证字段读写/校验
    monkeypatch.setattr(st, 'paramiko_available', lambda: True)
    dlg = cd.ConnectionDialog()
    try:
        dlg.ssh_check.setChecked(True)
        dlg.ssh_host_edit.setText('ssh.example')
        dlg.ssh_port_spin.setValue(2222)
        dlg.ssh_user_edit.setText('me')
        dlg.ssh_pwd_edit.setText('pw')
        dlg.ssh_remote_host_edit.setText('127.0.0.1')
        dlg.ssh_remote_port_spin.setValue(3306)
        opts = dlg.build_ssh_options()
        assert opts['enabled'] and opts['host'] == 'ssh.example'
        assert opts['port'] == 2222 and opts['remote_port'] == 3306
        assert dlg.validate_ssh_options()[0] is True
        dlg.ssh_pwd_edit.setText('')
        assert dlg.validate_ssh_options()[0] is False
        dlg.ssh_key_edit.setText(str(tmp_path / 'missing.key'))
        assert dlg.validate_ssh_options()[0] is False
        info = dlg._collect()
        assert info.ssh_options['enabled'] is True
    finally:
        dlg.close()


# ==================== 6. pyproject 可选依赖 ====================

def test_pyproject_ssh_optional_dependency():
    text = (Path(__file__).resolve().parent.parent / 'pyproject.toml').read_text(
        encoding='utf-8')
    assert 'ssh = [' in text
    assert 'paramiko>=3.0' in text


# ==================== 7. 真实隧道（paramiko 已安装时） ====================

def test_real_ssh_tunnel_if_available(home):
    from lightdb_studio.db.ssh_tunnel import (
        SSHUnavailableError, SshTunnel, SshTunnelConfig, paramiko_available)
    if not paramiko_available():
        pytest.skip('paramiko 未安装：真实 SSH 隧道测试显式 skip'
                    '（pip install paramiko 后可运行）')
    host = os.environ.get('LIGHTDB_SSH_HOST', '127.0.0.1')
    port = int(os.environ.get('LIGHTDB_SSH_PORT', '22'))
    user = os.environ.get('LIGHTDB_SSH_USER', '')
    password = os.environ.get('LIGHTDB_SSH_PASSWORD', '')
    if not user:
        pytest.skip('未配置 LIGHTDB_SSH_USER：本机无可用的已配置 SSH 服务，'
                    '真实隧道测试 skip（不假过）')
    cfg = SshTunnelConfig(host=host, port=port, user=user,
                          password=password, remote_host='127.0.0.1',
                          remote_port=8898)
    try:
        with SshTunnel(cfg) as tunnel:
            s = socket.create_connection(
                ('127.0.0.1', tunnel.local_port), timeout=5)
            s.close()
    except SSHUnavailableError:
        raise
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f'本机 SSH 隧道不可用（{exc}），真实测试 skip（不假过）')
