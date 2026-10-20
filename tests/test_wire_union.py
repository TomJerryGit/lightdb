"""MySQL 文本协议回归：UNION / UNION ALL 结果集必须经协议返回。

2026-10-20 全量 GUI+DB 验证发现：`Executor` 内部执行 UNION 正常（返回
resultset），但 `net/server.py::_send_execution_result` 只登记了
`cmd == 'SELECT'`，UNION 落到"其他"分支回 OK 包 → 真实客户端
（pymysql/MySQL 协议）得到 description=None、0 行。本测试锁住修复。
"""
import os
import sys
import threading
import time

import pymysql
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from tests.conftest import safe_rmtree
except ImportError:                          # 直接 python tests/test_xxx.py
    from conftest import safe_rmtree

DATA_DIR = os.path.join(ROOT, 'data_union_wire')
PORT = 8899


def _reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


@pytest.fixture()
def server():
    _reset()
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    db = Database(DATA_DIR)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            c = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                password='', autocommit=True,
                                connect_timeout=1)
            c.close()
            break
        except Exception:                     # noqa: BLE001
            time.sleep(0.1)
    else:
        raise RuntimeError('server did not start')
    try:
        yield db, ex
    finally:
        try:
            srv.shutdown()
        except Exception:                     # noqa: BLE001
            pass
        try:
            db.shutdown()
        except Exception:                     # noqa: BLE001
            pass
        time.sleep(0.2)


def _conn():
    return pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           password='', autocommit=True, charset='utf8mb4')


def test_union_and_union_all_over_wire(server):
    _db, _ex = server
    c = _conn()
    cur = c.cursor()
    try:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(10))')
        cur.execute("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'a')")

        cur.execute('SELECT 1 UNION SELECT 2')
        assert cur.description is not None, 'UNION 必须返回结果集，而不是 OK 包'
        assert cur.fetchall() == ((1,), (2,))

        cur.execute('SELECT v FROM t WHERE id <= 2 UNION ALL '
                    'SELECT v FROM t WHERE id = 3')
        assert cur.fetchall() == (('a',), ('b',), ('a',))

        cur.execute('SELECT v FROM t UNION SELECT v FROM t ORDER BY v')
        assert cur.fetchall() == (('a',), ('b',))
    finally:
        cur.close()
        c.close()


def test_union_multi_column_and_limit(server):
    _db, _ex = server
    c = _conn()
    cur = c.cursor()
    try:
        cur.execute('CREATE TABLE u (id INT PRIMARY KEY, g VARCHAR(5))')
        cur.execute("INSERT INTO u VALUES (1,'x'),(2,'y')")
        cur.execute("SELECT id, g FROM u WHERE id=1 UNION "
                    "SELECT id, g FROM u WHERE id=2 ORDER BY id")
        rows = cur.fetchall()
        assert rows == ((1, 'x'), (2, 'y')), rows
        cur.execute('SELECT id FROM u UNION SELECT id FROM u ORDER BY id LIMIT 1')
        assert cur.fetchall() == ((1,),)
    finally:
        cur.close()
        c.close()
