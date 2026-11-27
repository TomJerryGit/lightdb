"""客户端兼容矩阵：用真实客户端跑同一组冒烟 SQL，输出 JSON 矩阵。

用法：
  python scripts/client_matrix.py            # 启动临时 LightDB 服务并测试
  python scripts/client_matrix.py --json client_matrix.json
  python scripts/client_matrix.py --port 8910 --data-dir <已启动服务的数据目录>  # 复用运行中的服务

覆盖：pymysql（必有）、SQLAlchemy（已安装时）、mysql CLI（已安装时）；
DBeaver / Navicat 为 GUI 客户端，只做安装检测并给出人工核对清单。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
PW = 'demo123'


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def start_server(data_dir: Path, port: int):
    env = {k: v for k, v in os.environ.items()
           if k not in ('LIGHTDB_ALLOW_EMPTY_PASSWORD', 'LIGHTDB_ROOT_PASSWORD')}
    env.update({'PYTHONPATH': str(REPO),
                'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
    log = open(data_dir.parent / 'server.log', 'ab')
    proc = subprocess.Popen(
        [PYTHON, '-m', 'lightdb.main', '--mode', 'single',
         '--host', '127.0.0.1', '--port', str(port),
         '--data-dir', str(data_dir), '--root-password', PW],
        cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not wait_port(port):
        proc.kill()
        raise RuntimeError('server did not start')
    return proc


SQL_STEPS = [
    ('select1', 'SELECT 1'),
    ('show_tables', 'SHOW TABLES'),
    ('create', 'CREATE TABLE IF NOT EXISTS client_probe (id INT PRIMARY KEY, v VARCHAR(20))'),
    ('insert', "INSERT INTO client_probe VALUES (1,'a')"),
    ('count', 'SELECT COUNT(*) FROM client_probe'),
]


def probe_pymysql(port: int) -> dict:
    import pymysql
    steps = []
    conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password=PW, autocommit=True, connect_timeout=10)
    try:
        cur = conn.cursor()
        for name, sql in SQL_STEPS:
            cur.execute(sql)
            cur.fetchall()
            steps.append(name)
        # 事务回滚
        cur.execute('BEGIN')
        cur.execute("INSERT INTO client_probe VALUES (2,'b')")
        conn.rollback()
        steps.append('rollback')
        cur.execute('SELECT COUNT(*) FROM client_probe')
        rc = cur.fetchone()[0]
        assert rc == 1, rc
        steps.append('rollback_effect')
        cur.execute('DROP TABLE client_probe')
        steps.append('drop')
    finally:
        conn.close()
    return {'client': 'pymysql', 'version': pymysql.__version__,
            'ok': True, 'steps': steps}


def probe_sqlalchemy(port: int) -> dict:
    import sqlalchemy
    from sqlalchemy import create_engine, text
    url = (f'mysql+pymysql://root:{PW}@127.0.0.1:{port}/default'
           '?charset=utf8mb4')
    engine = create_engine(url, connect_args={'connect_timeout': 10},
                           pool_pre_ping=False)
    steps = []
    try:
        with engine.connect() as conn:
            val = conn.execute(text('SELECT 1')).scalar()
            assert val == 1, val
            steps.append('select1')
            conn.execute(text('SHOW TABLES')).fetchall()
            steps.append('show_tables')
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS sa_probe (id INT PRIMARY KEY, v VARCHAR(20))'))
            conn.execute(text("INSERT INTO sa_probe VALUES (1,'a')"))
            conn.commit()
            steps.append('ddl_dml')
            conn.rollback()          # 结束 autobegin，再显式开事务
            tx = conn.begin()
            conn.execute(text("INSERT INTO sa_probe VALUES (2,'b')"))
            tx.rollback()
            steps.append('tx_rollback')
            cnt = conn.execute(text('SELECT COUNT(*) FROM sa_probe')).scalar()
            assert cnt == 1, cnt
            conn.execute(text('DROP TABLE sa_probe'))
            conn.commit()
            steps.append('cleanup')
    finally:
        engine.dispose()
    return {'client': 'sqlalchemy', 'version': sqlalchemy.__version__,
            'ok': True, 'steps': steps, 'url': url}


def probe_mysql_cli(port: int) -> dict:
    exe = shutil.which('mysql') or shutil.which('mysql.exe')
    if not exe:
        return {'client': 'mysql-cli', 'ok': None, 'skipped': True,
                'reason': 'mysql client not found'}
    try:
        out = subprocess.run(
            [exe, '--protocol=TCP', '-h127.0.0.1', '-P', str(port),
             '-uroot', f'-p{PW}', '-D', 'default',
             '-e', 'SELECT 1 AS v; SHOW TABLES;'],
            capture_output=True, text=True, timeout=40,
            encoding='utf-8', errors='replace')
        ok = out.returncode == 0 and '1' in (out.stdout or '')
        known_incompatible = False
        if not ok:
            ver = subprocess.run(
                [exe, '--version'], capture_output=True, text=True,
                timeout=20, encoding='utf-8', errors='replace')
            ver_text = ((ver.stdout or '') + (ver.stderr or ''))
            if 'Distrib 5.0' in ver_text or '5.0.' in ver_text:
                # MySQL 5.0 CLI（EOL）认证包不兼容；GUI 客户端（Navicat 10）/
                # pymysql/SQLAlchemy 均通过。记录为已知限制，不算回归。
                known_incompatible = True
        return {'client': 'mysql-cli', 'ok': ok, 'rc': out.returncode,
                'known_incompatible': known_incompatible,
                'stdout_tail': (out.stdout or '').strip().splitlines()[-3:],
                'stderr_tail': (out.stderr or '').strip().splitlines()[-3:]}
    except Exception as exc:
        return {'client': 'mysql-cli', 'ok': False, 'error': str(exc)}


def detect_gui_clients() -> list[dict]:
    found = []
    candidates = [
        ('Navicat', [r'C:\Program Files\PremiumSoft', r'C:\Program Files (x86)\PremiumSoft',
                     r'C:\Program Files\Navicat', r'D:\Navicat']),
        ('DBeaver', [r'C:\Program Files\DBeaver', r'C:\Program Files (x86)\DBeaver',
                     r'D:\DBeaver', r'C:\Users\Administrator\AppData\Local\DBeaver']),
    ]
    for name, paths in candidates:
        hit = next((p for p in paths if Path(p).exists()), None)
        found.append({'client': name, 'installed': bool(hit),
                      'path': hit, 'mode': 'manual',
                      'checklist': [
                          '新建 MySQL 连接：127.0.0.1:<port>，用户 root，口令 demo123',
                          '对象树能列出 13 张表/默认库',
                          '执行 SELECT 1 / SHOW TABLES 成功',
                          '打开表数据（分页 200 行）正常',
                          '建表/改表（设计器）提交后 SHOW CREATE 一致']})
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='LightDB 客户端兼容矩阵')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--data-dir', default='')
    ap.add_argument('--json', default='')
    args = ap.parse_args(argv)
    tmp = None
    proc = None
    port = args.port
    try:
        if args.data_dir:
            data = Path(args.data_dir)
        else:
            tmp = Path(tempfile.mkdtemp(prefix='lightdb-client-matrix-'))
            data = tmp / 'data'
            data.mkdir(parents=True)
        if not port:
            port = free_port()
        if not args.port or not args.data_dir:
            proc = start_server(data, port)
        results = []
        for fn in (probe_pymysql, probe_sqlalchemy, probe_mysql_cli):
            try:
                results.append(fn(port))
            except ImportError as exc:
                results.append({'client': fn.__name__, 'ok': None,
                                'skipped': True, 'reason': f'not installed: {exc}'})
            except Exception as exc:
                results.append({'client': fn.__name__, 'ok': False,
                                'error': f'{type(exc).__name__}: {exc}'})
        gui = detect_gui_clients()
        matrix = {
            'schema': 'lightdb-client-matrix/1',
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'host': f'127.0.0.1:{port}',
            'automated': results,
            'gui_manual': gui,
            'ok': all(r.get('ok') is not False or r.get('known_incompatible') for r in results),
        }
        text = json.dumps(matrix, ensure_ascii=False, indent=2)
        print(text)
        if args.json:
            Path(args.json).write_text(text, encoding='utf-8')
            print(f'[matrix] written: {args.json}', file=sys.stderr)
        return 0 if matrix['ok'] else 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
