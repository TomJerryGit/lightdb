# -*- coding: utf-8 -*-
"""SSH 本地端口转发（可选依赖 paramiko；未安装时显式报错，不伪造成功）。

用法::

    cfg = SshTunnelConfig(host='ssh.example', user='me', password='...',
                          remote_host='127.0.0.1', remote_port=8888)
    with SshTunnel(cfg) as tunnel:
        conn = pymysql.connect(host='127.0.0.1', port=tunnel.local_port, ...)

设计：
- paramiko **惰性导入**：只有真正 start() 时才 import；缺失抛
  `SSHUnavailableError`（提示 pip install paramiko），UI 据此禁用字段；
- start() 建立 SSHClient/Transport，在 127.0.0.1 上监听随机端口，把每个本地
  TCP 连接经 `direct-tcpip` channel 转发到 remote_host:remote_port；
- close()/上下文管理确保监听 socket、后台线程、SSH 客户端全部关闭；
- 失败显式抛异常，不返回“假隧道”。
"""
import socket
import threading
import time
from dataclasses import dataclass, field


class SSHUnavailableError(RuntimeError):
    """paramiko 未安装（显式依赖缺失，不是静默失败）。"""


class SSHTunnelError(RuntimeError):
    """SSH 连接/转发建立失败。"""


def _load_paramiko():
    try:
        import paramiko  # noqa: F401
        return paramiko
    except ImportError as exc:
        raise SSHUnavailableError(
            'SSH 隧道需要 paramiko（可选依赖）：pip install paramiko  '
            '或 pip install ".[ssh]"') from exc


def paramiko_available():
    try:
        _load_paramiko()
        return True
    except Exception:
        return False


@dataclass
class SshTunnelConfig:
    host: str = ''
    port: int = 22
    user: str = ''
    password: str = ''
    key_path: str = ''
    local_port: int = 0          # 0 = 自动分配
    remote_host: str = '127.0.0.1'
    remote_port: int = 8888
    timeout: float = 10.0
    auto_add_host_key: bool = True
    known_hosts: str = ''


class SshTunnel:
    def __init__(self, config: SshTunnelConfig = None, **kwargs):
        self.config = config or SshTunnelConfig(**kwargs)
        self.local_port = None
        self._client = None
        self._transport = None
        self._listener = None
        self._accept_thread = None
        self._stop = threading.Event()
        self._handlers = []

    # ---------- 生命周期 ----------

    def start(self):
        if self.config.host and not self.config.user:
            raise SSHTunnelError('SSH 用户名不能为空')
        paramiko = _load_paramiko()
        try:
            client = paramiko.SSHClient()
            try:
                client.load_system_host_keys()
            except Exception:
                pass
            if self.config.known_hosts:
                client.load_host_keys(self.config.known_hosts)
            if self.config.auto_add_host_key:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            connect_kwargs = {
                'hostname': self.config.host or '127.0.0.1',
                'port': int(self.config.port or 22),
                'username': self.config.user,
                'password': self.config.password or None,
                'timeout': float(self.config.timeout or 10.0),
                'banner_timeout': float(self.config.timeout or 10.0),
                'auth_timeout': float(self.config.timeout or 10.0),
                'allow_agent': False,
                'look_for_keys': False,
            }
            if self.config.key_path:
                connect_kwargs['key_filename'] = self.config.key_path
            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is None or not getattr(transport, 'is_active', lambda: True)():
                raise SSHTunnelError('SSH 连接已建立但 transport 不可用')
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', int(self.config.local_port or 0)))
            listener.listen(8)
            listener.settimeout(0.3)
            self._client = client
            self._transport = transport
            self._listener = listener
            self.local_port = listener.getsockname()[1]
            self._stop.clear()
            self._accept_thread = threading.Thread(
                target=self._accept_loop, name='ssh-tunnel-accept',
                daemon=True)
            self._accept_thread.start()
            return self
        except SSHUnavailableError:
            self.close()
            raise
        except Exception as exc:                      # noqa: BLE001
            self.close()
            if isinstance(exc, SSHTunnelError):
                raise
            raise SSHTunnelError(f'SSH 隧道建立失败: {exc}') from exc

    def _accept_loop(self):
        listener = self._listener
        while not self._stop.is_set():
            try:
                client_sock, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._forward, args=(client_sock, addr),
                name='ssh-tunnel-forward', daemon=True)
            thread.start()
            self._handlers.append(thread)

    def _forward(self, client_sock, addr):
        channel = None
        try:
            channel = self._transport.open_channel(
                'direct-tcpip',
                (self.config.remote_host, int(self.config.remote_port)),
                addr)
            while not self._stop.is_set():
                import select
                readable, _w, x = select.select([client_sock, channel], [], [],
                                               0.3)
                if x:
                    break
                if client_sock in readable:
                    data = client_sock.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    client_sock.sendall(data)
        except Exception:
            pass
        finally:
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            try:
                client_sock.close()
            except Exception:
                pass

    def close(self):
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._transport = None
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        self.local_port = None
        return True

    def is_active(self):
        return bool(self._listener is not None and self._transport is not None)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def from_connection_info(info):
    """从 ConnectionInfo.ssh_options 构造隧道配置（未启用返回 None）。"""
    options = dict(getattr(info, 'ssh_options', None) or {})
    if not options.get('enabled'):
        return None
    return SshTunnelConfig(
        host=str(options.get('host') or ''),
        port=int(options.get('port') or 22),
        user=str(options.get('user') or ''),
        password=str(options.get('password') or ''),
        key_path=str(options.get('key_path') or ''),
        local_port=int(options.get('local_port') or 0),
        remote_host=str(options.get('remote_host') or '127.0.0.1'),
        remote_port=int(options.get('remote_port')
                        or getattr(info, 'port', 8888)),
        timeout=float(options.get('timeout') or 10.0),
        auto_add_host_key=bool(options.get('auto_add_host_key', True)),
        known_hosts=str(options.get('known_hosts') or ''),
    )
