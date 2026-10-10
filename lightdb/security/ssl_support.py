"""
TLS/SSL 支持
- 自签名证书生成
- 服务端 SSL 包装
- 客户端连接（供从库使用）
"""
import os
import ssl
import subprocess
import socket
from pathlib import Path


class SSLManager:
    def __init__(self, cert_dir: str):
        self.cert_dir = Path(cert_dir)
        self.cert_dir.mkdir(parents=True, exist_ok=True)

        self.cert_file = self.cert_dir / 'server.crt'
        self.key_file = self.cert_dir / 'server.key'
        self.ca_file = self.cert_dir / 'ca.crt'

        self._ctx = None

    def ensure_certificates(self):
        """确保有自签名证书"""
        if self.cert_file.exists() and self.key_file.exists():
            return True

        try:
            self._generate_self_signed()
            return True
        except Exception as e:
            print(f"[SSL] 证书生成失败: {e}")
            return False

    def _generate_self_signed(self):
        """用 openssl 生成自签名证书"""
        cmd = [
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', str(self.key_file),
            '-out', str(self.cert_file),
            '-days', '365', '-nodes',
            '-subj', '/CN=lightdb.local/O=LightDB',
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        # CA 就用自身证书
        import shutil
        shutil.copy(str(self.cert_file), str(self.ca_file))

    def make_server_context(self):
        if self._ctx is not None:
            return self._ctx
        if not self.ensure_certificates():
            return None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.cert_file), str(self.key_file))
        # 允许客户端连接
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ctx = ctx
        return ctx

    def make_client_context(self, verify=False):
        if not self.ensure_certificates():
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if verify:
            ctx.load_verify_locations(str(self.ca_file))
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def wrap_socket(self, sock, server_side=True):
        ctx = self.make_server_context() if server_side else self.make_client_context()
        if ctx is None:
            return sock
        return ctx.wrap_socket(sock, server_side=server_side)


def has_openssl() -> bool:
    try:
        subprocess.run(['openssl', 'version'], capture_output=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False