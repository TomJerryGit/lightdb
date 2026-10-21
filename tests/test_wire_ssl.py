"""服务端 TLS（MySQL SSLRequest 协商）端到端回归。

2026-10-21：服务端此前只解析了 `ssl_enabled` 配置，从未 wrap socket / 宣告
CLIENT_SSL（已列入 Known limitations，CI 首跑 test_batch14::test_ssl 暴露）。
本测试用 `cryptography` 生成自签证书（不依赖 openssl CLI），起真实
`MySQLServer`，验证：

1. pymysql 带 `ssl={...}` 能完成 TLS 握手并执行查询；
2. 不启用 TLS 的客户端仍可明文连接（MySQL TLS 为可选语义）；
3. `ssl_enabled=True` 但证书缺失/不可用时，构造 MySQLServer 必须**显式报错**
   而不是静默降级成明文。
"""
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pymysql = pytest.importorskip('pymysql')
crypto = pytest.importorskip('cryptography')

from cryptography import x509                        # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa          # noqa: E402
from cryptography.x509.oid import NameOID            # noqa: E402
import datetime as _dt                               # noqa: E402

PORT = 8891


def _write_cert(tmp_path):
    """生成自签证书（CN=localhost，含 SAN IP:127.0.0.1）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('localhost'),
            x509.IPAddress(__import__('ipaddress').ip_address('127.0.0.1')),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / 'server.crt'
    key_file = tmp_path / 'server.key'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(cert_file), str(key_file)


def _wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=1)
            s.close()
            return True
        except Exception:                        # noqa: BLE001
            time.sleep(0.1)
    return False


@pytest.fixture()
def tls_server(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    cert, key = _write_cert(tmp_path)
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    cfg.ssl_enabled = True
    cfg.ssl_cert = cert
    cfg.ssl_key = key
    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    assert _wait_port(PORT), 'TLS server did not start'
    try:
        yield srv, db
    finally:
        try:
            srv.shutdown()
        except Exception:                    # noqa: BLE001
            pass
        try:
            db.shutdown()
        except Exception:                    # noqa: BLE001
            pass
        time.sleep(0.2)


def test_tls_connect_and_query(tls_server):
    srv, _db = tls_server
    assert srv.ssl_enabled is True
    assert srv.ssl_context is not None

    conn = pymysql.connect(
        host='127.0.0.1', port=PORT, user='root', password='',
        autocommit=True, charset='utf8mb4', connect_timeout=15,
        ssl={'check_hostname': False})
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)
        cur.execute('CREATE TABLE tls_t (id INT PRIMARY KEY, v VARCHAR(10))')
        cur.execute("INSERT INTO tls_t VALUES (1, 'a')")
        cur.execute('SELECT v FROM tls_t WHERE id = 1')
        assert cur.fetchone() == ('a',)
    finally:
        conn.close()


def test_plain_client_still_works_when_tls_optional(tls_server):
    """TLS 是可选语义：不发 SSLRequest 的客户端仍可明文连接。"""
    # 直接连端口探测（_wait_port 已建过明文 TCP 连接）；这里用完整协议验证
    assert _wait_port(PORT)
    conn = pymysql.connect(
        host='127.0.0.1', port=PORT, user='root', password='',
        autocommit=True, charset='utf8mb4', connect_timeout=15)
    try:
        cur = conn.cursor()
        cur.execute('SELECT 2')
        assert cur.fetchone() == (2,)
    finally:
        conn.close()


def test_ssl_enabled_without_cert_fails_explicitly(tmp_path):
    """ssl_enabled 但既无 ssl_cert/ssl_key 又无法生成 → 启动失败（不静默明文）。"""
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = 0                            # 不会真正绑定（构造时先失败）
    cfg.ssl_enabled = True
    cfg.ssl_cert = ''
    cfg.ssl_key = ''
    cfg.data_dir = str(tmp_path / 'data_no_certs')
    db = Database(str(tmp_path / 'db2'))
    ex = Executor(db)
    try:
        with pytest.raises(RuntimeError) as ei:
            MySQLServer(cfg, db, ex)
        assert 'ssl_enabled' in str(ei.value) or '证书' in str(ei.value)
    finally:
        db.shutdown()
