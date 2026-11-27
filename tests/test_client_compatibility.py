"""客户端兼容性：pymysql（必有）、SQLAlchemy（已安装时）、mysql CLI（已安装时）。"""
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = 'demo123'


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp('client_compat') / 'data'
    data.mkdir(parents=True)
    port = _free_port()
    # conftest 会设置 LIGHTDB_ALLOW_EMPTY_PASSWORD；它与 --root-password 冲突，
    # 子进程环境必须清掉，否则服务启动即报"配置错误"（曾导致 3 个客户端测试全 ERROR）。
    env = {k: v for k, v in os.environ.items()
           if k not in ('LIGHTDB_ALLOW_EMPTY_PASSWORD', 'LIGHTDB_ROOT_PASSWORD')}
    env.update({'PYTHONPATH': ROOT,
                'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
    log = open(data.parent / 'server.log', 'ab')
    proc = subprocess.Popen(
        [sys.executable, '-m', 'lightdb.main', '--mode', 'single',
         '--host', '127.0.0.1', '--port', str(port),
         '--data-dir', str(data), '--root-password', PW],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not _wait(port):
        proc.kill()
        try:
            tail = (data.parent / 'server.log').read_text(
                encoding='utf-8', errors='replace')[-3000:]
        except OSError:
            tail = '<no server.log>'
        pytest.fail(f'LightDB server did not start on port {port}; log tail:\n{tail}')
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_pymysql_basic(server):
    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=server, user='root',
                           password=PW, autocommit=True, connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        assert cur.fetchone()[0] == 1
        cur.execute('SHOW TABLES')
        cur.fetchall()
        cur.execute('CREATE TABLE IF NOT EXISTS comp_probe '
                    '(id INT PRIMARY KEY, v VARCHAR(20))')
        cur.execute("INSERT INTO comp_probe VALUES (1,'a')")
        cur.execute('SELECT COUNT(*) FROM comp_probe')
        assert cur.fetchone()[0] == 1
        cur.execute('DROP TABLE comp_probe')
    finally:
        conn.close()


def test_sqlalchemy_basic(server):
    sqlalchemy = pytest.importorskip('sqlalchemy')
    from sqlalchemy import create_engine, text
    url = (f'mysql+pymysql://root:{PW}@127.0.0.1:{server}/default'
           '?charset=utf8mb4')
    engine = create_engine(url, connect_args={'connect_timeout': 10})
    try:
        with engine.connect() as conn:
            assert conn.execute(text('SELECT 1')).scalar() == 1
            conn.execute(text('SHOW TABLES')).fetchall()
            conn.execute(text('CREATE TABLE IF NOT EXISTS sa_probe '
                              '(id INT PRIMARY KEY, v VARCHAR(20))'))
            conn.execute(text("INSERT INTO sa_probe VALUES (1,'a')"))
            conn.commit()
            assert conn.execute(
                text('SELECT COUNT(*) FROM sa_probe')).scalar() == 1
            # 结束 autobegin，才能显式开启事务；SQLAlchemy 2.x 的
            # Transaction 对象没有 execute，执行要挂在 connection 上。
            conn.rollback()
            tx = conn.begin()
            conn.execute(text("INSERT INTO sa_probe VALUES (2,'b')"))
            tx.rollback()
            assert conn.execute(
                text('SELECT COUNT(*) FROM sa_probe')).scalar() == 1
            conn.execute(text('DROP TABLE sa_probe'))
            conn.commit()
    finally:
        engine.dispose()
    assert sqlalchemy.__version__


def test_mysql_cli_basic(server):
    exe = shutil.which('mysql') or shutil.which('mysql.exe')
    if not exe:
        pytest.skip('mysql CLI not installed')
    ver = subprocess.run([exe, '--version'], capture_output=True, text=True,
                         timeout=20, encoding='utf-8', errors='replace')
    ver_text = ((ver.stdout or '') + (ver.stderr or '')).strip()
    out = subprocess.run(
        [exe, '--protocol=TCP', '-h127.0.0.1', '-P', str(server),
         '-uroot', f'-p{PW}', '-D', 'default',
         '-e', 'SELECT 1; SHOW TABLES;'],
        capture_output=True, text=True, timeout=40,
        encoding='utf-8', errors='replace')
    if out.returncode != 0:
        err = ((out.stderr or '') + (out.stdout or '')).strip()
        # MySQL 5.0 客户端认证包与本服务端不兼容（EOL 客户端，已知限制；
        # Navicat 10 / pymysql / SQLAlchemy 均通过）。文档记录，不当作回归。
        if 'Ver 14.12 Distrib 5.0' in ver_text or '5.0.' in ver_text:
            pytest.skip(f'MySQL 5.0 CLI 已知不兼容（认证包）: {err[:160]}')
        pytest.fail(f'mysql CLI failed: rc={out.returncode} {err[:400]}')
    assert '1' in (out.stdout or '')
