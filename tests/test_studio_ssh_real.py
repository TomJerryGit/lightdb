# -*- coding: utf-8 -*-
"""task-61：真实 SSH 协议端到端（进程内 paramiko SSH server + direct-tcpip）。

不依赖系统 sshd：测试内用 cryptography 生成 RSA 主机密钥，paramiko 实现
ServerInterface（密码/公钥认证 + direct-tcpip），ThreadingTCPServer 监听；
真实 SshTunnel 经本地转发访问 echo TCP server；失败路径（错误密码、目标
端口关闭）显式断言，不降级明文、不假成功。
"""
import io
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

paramiko = pytest.importorskip('paramiko')
cryptography = pytest.importorskip('cryptography')

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from lightdb_studio.db.ssh_tunnel import (  # noqa: E402
    SSHUnavailableError, SSHTunnelError, SshTunnel, SshTunnelConfig)

PASSWORD = 'ssh-real-pass'
USERNAME = 'tester'


def _make_host_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    return paramiko.RSAKey.from_private_key(io.StringIO(pem.decode()))


HOST_KEY = _make_host_key()
USER_KEY = paramiko.RSAKey.generate(2048)


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ==================== 进程内真实 TCP echo server ====================

class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                self.request.sendall(b'ECHO:' + data)
        except OSError:
            pass
        finally:
            try:
                self.request.close()
            except OSError:
                pass


class EchoTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ==================== 进程内真实 SSH server ====================

def _relay(chan, sock):
    def pump_chan():
        try:
            while True:
                data = chan.recv(4096)
                if not data:
                    break
                sock.sendall(data)
        except Exception:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def pump_sock():
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
        except Exception:
            pass
        finally:
            try:
                chan.shutdown_write()
            except Exception:
                pass

    t1 = threading.Thread(target=pump_chan, daemon=True)
    t2 = threading.Thread(target=pump_sock, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for obj in (chan, sock):
        try:
            obj.close()
        except Exception:
            pass


class _SSHServer(paramiko.ServerInterface):
    def __init__(self):
        self.destinations = {}
        self.lock = threading.Lock()
        self.auth_attempts = []

    def check_auth_password(self, username, password):
        self.auth_attempts.append(('password', username, password))
        if username == USERNAME and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.auth_attempts.append(('publickey', username, key.asbytes()))
        ok = (username == USERNAME and
              key.asbytes() == USER_KEY.asbytes())
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password,publickey'

    def check_channel_request(self, kind, chanid):
        if kind in ('session', 'direct-tcpip'):
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        host, port = destination
        try:
            target = socket.create_connection((host, int(port)), timeout=3)
        except OSError:
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        with self.lock:
            self.destinations[chanid] = target
        return paramiko.OPEN_SUCCEEDED


class _SSHHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = _SSHServer()
        transport = paramiko.Transport(self.request)
        transport.add_server_key(HOST_KEY)
        try:
            transport.start_server(server=server)
            self.server.auth_servers.append(server)
            while transport.is_active():
                chan = transport.accept(0.3)
                if chan is None:
                    continue
                with server.lock:
                    target = server.destinations.pop(chan.get_id(), None)
                if target is None:
                    chan.close()
                    continue
                threading.Thread(target=_relay, args=(chan, target),
                                 daemon=True).start()
        except Exception:
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass


class SSHTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.auth_servers = []


@pytest.fixture(scope='module')
def echo_server():
    server = EchoTCPServer(('127.0.0.1', 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'server': server, 'port': server.server_address[1]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture(scope='module')
def ssh_server():
    server = SSHTCPServer(('127.0.0.1', 0), _SSHHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'server': server, 'port': server.server_address[1]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _tunnel_config(ssh_server, remote_port, **overrides):
    cfg = dict(host='127.0.0.1', port=ssh_server['port'],
               user=USERNAME, password=PASSWORD,
               remote_host='127.0.0.1', remote_port=int(remote_port),
               timeout=5)
    cfg.update(overrides)
    return SshTunnelConfig(**cfg)


def _roundtrip(tunnel, payload=b'hello'):
    sock = socket.create_connection(('127.0.0.1', tunnel.local_port),
                                    timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(payload)
        data = sock.recv(4096)
        return data
    finally:
        sock.close()


def _wait_for(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ==================== 1. 密码认证 + 数据往返 ====================

def test_real_ssh_password_auth_echo(ssh_server, echo_server):
    cfg = _tunnel_config(ssh_server, echo_server['port'])
    with SshTunnel(cfg) as tunnel:
        assert tunnel.is_active() is True
        assert tunnel.local_port and tunnel.local_port > 0
        assert _roundtrip(tunnel, b'hello') == b'ECHO:hello'
        assert _roundtrip(tunnel, b'world') == b'ECHO:world'
    assert tunnel.is_active() is False
    assert tunnel.local_port is None


# ==================== 2. 私钥认证 + 数据往返 ====================

def test_real_ssh_publickey_auth_echo(ssh_server, echo_server, tmp_path):
    key_path = tmp_path / 'id_rsa'
    USER_KEY.write_private_key_file(str(key_path))
    cfg = _tunnel_config(ssh_server, echo_server['port'], password='',
                         key_path=str(key_path))
    with SshTunnel(cfg) as tunnel:
        assert tunnel.is_active() is True
        assert _roundtrip(tunnel, b'key-auth') == b'ECHO:key-auth'


# ==================== 3. 错误密码显式失败 ====================

def test_real_ssh_wrong_password_explicit(ssh_server, echo_server):
    cfg = _tunnel_config(ssh_server, echo_server['port'], password='wrong')
    tunnel = SshTunnel(cfg)
    with pytest.raises(SSHTunnelError) as ei:
        tunnel.start()
    msg = str(ei.value)
    assert 'SSH 隧道建立失败' in msg
    assert tunnel.local_port is None
    assert tunnel.is_active() is False
    # 不降级明文：没有本地监听端口
    assert tunnel._listener is None


# ==================== 4. direct-tcpip 目标端口关闭显式失败 ====================

def test_real_ssh_closed_target_port_explicit(ssh_server):
    closed_port = _free_port()   # bind 后立即关闭，确保无监听
    cfg = _tunnel_config(ssh_server, closed_port)
    tunnel = SshTunnel(cfg)
    try:
        tunnel.start()   # SSH 认证本身成功，失败发生在 direct-tcpip
        assert tunnel.is_active() is True
        try:
            sock = socket.create_connection(
                ('127.0.0.1', tunnel.local_port), timeout=3)
            sock.settimeout(3)
            sock.sendall(b'x')
            try:
                data = sock.recv(100)
            except socket.timeout:
                data = b''
            sock.close()
            assert data == b''
        except OSError:
            pass
        assert _wait_for(lambda: bool(tunnel.forward_errors)), \
            'direct-tcpip 建立失败必须显式记录，不能静默'
        assert 'direct-tcpip' in str(tunnel.last_forward_error())
    finally:
        tunnel.close()


# ==================== 5. paramiko 缺失显式（模拟） ====================

def test_paramiko_missing_explicit(monkeypatch):
    from lightdb_studio.db import ssh_tunnel
    monkeypatch.setattr(
        ssh_tunnel, '_load_paramiko',
        lambda: (_ for _ in ()).throw(
            SSHUnavailableError('SSH 隧道需要 paramiko（可选依赖）：pip install paramiko')))
    tunnel = SshTunnel(SshTunnelConfig(host='127.0.0.1', user='u',
                                       password='p', remote_port=1))
    with pytest.raises(SSHUnavailableError, match='pip install paramiko'):
        tunnel.start()
    assert tunnel.local_port is None


# ==================== 6. 真实 DBConnection 经 SSH 隧道（可选 8898） ====================

def _port_open(port, host='127.0.0.1', timeout=0.3):
    s = socket.socket()
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_real_dbconnection_via_ssh_select1(ssh_server):
    if not _port_open(8898):
        pytest.skip('8898 LightDB 服务未运行；已用 echo server 完成真实隧道测试')
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    info = ConnectionInfo(
        name='ssh-real-db', host='127.0.0.1', port=8898, user='root',
        password=os.environ.get('LIGHTDB_STUDIO_TEST_PASSWORD', ''),
        ssh_options={
            'enabled': True, 'host': '127.0.0.1',
            'port': ssh_server['port'], 'user': USERNAME,
            'password': PASSWORD, 'remote_host': '127.0.0.1',
            'remote_port': 8898,
        })
    conn = DBConnection(info)
    try:
        try:
            conn.connect()
        except Exception as exc:                      # noqa: BLE001
            pytest.skip(f'8898 服务认证/连接不可用: {exc}')
        assert conn.ssh_tunnel is not None
        _cols, rows = conn.query('SELECT 1')
        assert rows and int(rows[0][0]) == 1, rows
    finally:
        conn.close()
