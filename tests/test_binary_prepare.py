# -*- coding: utf-8 -*-
"""task-73 验收：MySQL 二进制 PREPARE 协议（COM_STMT_*）

在真实 MySQLServer 上用**字节级协议客户端**验证：
* COM_STMT_PREPARE 响应字段（statement_id / 列数 / 参数数 / 参数列定义）；
* EXECUTE 绑定 int/string/null/decimal/datetime，二进制结果集解码；
* CLOSE 后/未知 id → 1243；参数个数不符 → 1210；RESET 后可继续；
* statement id 连接级隔离与生命周期；大结果集完整返回；
* 与 SQL 文本 PREPARE/EXECUTE 结果一致。

pymysql 兼容：本机安装的 PyMySQL 2.2.8 **没有** `cursor(prepared=True)`
（`Connection.cursor()` 签名为 `(cursor=None)`）；如环境装有支持
server-side prepare 的客户端（mysql-connector-python）则额外端到端验证，
未安装时显式 skip，不把"没装库"伪装成通过。
"""
import inspect
import os
import socket
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lightdb.net import mysql_protocol as mp            # noqa: E402
from tests.test_prepared_statements import PreparedClient  # noqa: E402


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture()
def env(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.root_password = ''
    cfg.allow_empty_password = True
    data_dir = str(tmp_path / 'data_bin_prep')
    db = Database(data_dir)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import time
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    clients = []

    def connect(database='default'):
        c = PreparedClient(port, database=database)
        clients.append(c)
        return c

    yield {'port': port, 'db': db, 'ex': ex, 'connect': connect,
           'data_dir': data_dir}
    for c in clients:
        c.close()
    for fn in (srv.shutdown, srv.server_close):
        try:
            fn()
        except Exception:
            pass
    try:
        db.shutdown()
    except Exception:
        pass


# ==================== PREPARE 响应字段 / 参数类型 ====================

def test_prepare_ok_fields_and_param_column_defs(env):
    c = env['connect']()
    c.query_ok('CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20))')
    c.query_ok("INSERT INTO t VALUES (1,'a'),(2,'b')")
    res = c.prepare('SELECT id, name FROM t WHERE id = ?')
    assert res['type'] == 'prepare', res
    assert res['statement_id'] >= 1
    assert res['num_params'] == 1, res
    assert res['num_columns'] == 2, res
    # 参数列定义：服务端在 PREPARE 阶段如实报 VAR_STRING 占位
    assert res['param_columns'][0]['type'] == mp.MYSQL_TYPE_VAR_STRING, res
    # 结果列定义来自真实表（INT / VARCHAR），不是瞎猜
    assert res['columns'][0]['type'] == mp.MYSQL_TYPE_LONG, res
    assert res['columns'][1]['type'] in (
        mp.MYSQL_TYPE_VAR_STRING, mp.MYSQL_TYPE_VARCHAR,
        mp.MYSQL_TYPE_STRING), res
    # 二进制结果集
    r = c.stmt_execute(res['statement_id'], [(mp.MYSQL_TYPE_LONG, 2)])
    assert r['type'] == 'resultset', r
    assert r['rows'] == [(2, 'b')], r
    # 第二次 EXECUTE 复用已缓存参数类型（new_params_bound_flag=0）
    r2 = c.stmt_execute(res['statement_id'],
                        [(mp.MYSQL_TYPE_LONG, 1)], bind_types=False)
    assert r2['rows'] == [(1, 'a')], r2
    print('\n    PREPARE 响应：id/列数/参数数/参数类型正确；EXECUTE 二进制'
          '结果集与类型缓存复用正确')


def test_prepare_decimal_datetime_params(env):
    c = env['connect']()
    c.query_ok('CREATE TABLE dt (id INT PRIMARY KEY, amount DECIMAL(10,2), '
               'created DATETIME, note VARCHAR(20))')
    ins = c.prepare('INSERT INTO dt VALUES (?, ?, ?, ?)')
    r = c.stmt_execute(ins['statement_id'], [
        (mp.MYSQL_TYPE_LONG, 1),
        (mp.MYSQL_TYPE_NEWDECIMAL, '12.34'),
        (mp.MYSQL_TYPE_DATETIME, (2024, 1, 2, 3, 4, 5)),
        (mp.MYSQL_TYPE_NULL, None),
    ])
    assert r['type'] == 'ok', r
    c.query_ok("INSERT INTO dt VALUES (2, 12.34, '2024-01-02 03:04:05', NULL)")
    rows = c.query_rows('SELECT id, amount, created, note FROM dt '
                        'ORDER BY id')
    assert len(rows) == 2, rows
    assert rows[0][1] == rows[1][1], rows       # DECIMAL 参数与文本相等
    assert rows[0][2] == rows[1][2], rows       # DATETIME 参数与文本相等
    assert rows[0][3] is None and rows[1][3] is None, rows
    print(f'\n    DECIMAL/DATETIME/NULL 二进制参数：写入值与文本 SQL 一致 '
          f'({rows[0][1]!r}, {rows[0][2]!r})')


# ==================== 错误 / CLOSE / RESET ====================

def test_prepare_execute_errors_close_reset(env):
    c = env['connect']()
    c.query_ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    c.query_ok('INSERT INTO t VALUES (1, 10)')
    res = c.prepare('SELECT v FROM t WHERE id = ?')
    sid = res['statement_id']
    # 参数个数不符 → 1210
    bad = c.stmt_execute(sid, [])
    assert bad['type'] == 'error' and bad['code'] == 1210, bad
    # RESET 清参数类型缓存，之后仍可正常执行
    ok = c.stmt_reset(sid)
    assert ok['type'] == 'ok', ok
    good = c.stmt_execute(sid, [(mp.MYSQL_TYPE_LONG, 1)])
    assert good['rows'] == [(10,)], good
    # CLOSE 后执行 → 1243（且 CLOSE 本身无响应包）
    c.stmt_close(sid)
    gone = c.stmt_execute(sid, [(mp.MYSQL_TYPE_LONG, 1)])
    assert gone['type'] == 'error' and gone['code'] == 1243, gone
    # 未知 id → 1243
    unknown = c.stmt_execute(99999, [(mp.MYSQL_TYPE_LONG, 1)])
    assert unknown['code'] == 1243, unknown
    print('\n    错误/生命周期：参数不符 1210、RESET 可复用、CLOSE 后/未知 id '
          '1243')


def test_prepare_rejects_invalid_sql(env):
    c = env['connect']()
    r = c.prepare('SELECT FROM')
    assert r['type'] == 'error' and r['code'] == 1064, r
    r2 = c.prepare('INSERT INTO')
    assert r2['type'] == 'error' and r2['code'] == 1064, r2
    print('\n    PREPARE 阶段语法错误 → 1064（与文本 PREPARE 一致）')


# ==================== 会话隔离 / statement id ====================

def test_statement_is_per_connection(env):
    a = env['connect']()
    b = env['connect']()
    a.query_ok('CREATE TABLE t (id INT PRIMARY KEY)')
    a.query_ok('INSERT INTO t VALUES (1)')
    ra = a.prepare('SELECT id FROM t WHERE id = ?')
    assert ra['type'] == 'prepare'
    # B 会话不能使用 A 的 statement id
    rb = b.stmt_execute(ra['statement_id'], [(mp.MYSQL_TYPE_LONG, 1)])
    assert rb['type'] == 'error' and rb['code'] == 1243, rb
    # 两个连接各自从 1 开始的 id 空间互不影响
    rb2 = b.prepare('SELECT 1 AS one')
    assert rb2['type'] == 'prepare'
    assert a.stmt_execute(ra['statement_id'],
                          [(mp.MYSQL_TYPE_LONG, 1)])['rows'] == [(1,)]
    assert b.stmt_execute(rb2['statement_id'], [])['rows'] == [(1,)]
    # 关闭 A 不影响 B（文本协议常量投影返回文本值 '2'）
    a.close()
    assert b.query_rows('SELECT 2 AS two') == [('2',)]
    print('\n    会话隔离：statement id per-connection，跨会话 1243，互不影响')


# ==================== 大结果集 / 文本 PREPARE 等价 ====================

def test_large_result_and_text_prepare_equivalence(env):
    c = env['connect']()
    c.query_ok('CREATE TABLE big (id INT PRIMARY KEY, name VARCHAR(20))')
    rows = []
    for start in range(0, 3000, 500):
        vals = ','.join(f"({i},'n{i}')" for i in range(start, start + 500))
        c.query_ok('INSERT INTO big VALUES ' + vals)
        rows.extend((i, f'n{i}') for i in range(start, start + 500))
    res = c.prepare('SELECT id, name FROM big ORDER BY id')
    got = c.stmt_execute(res['statement_id'], [])
    assert got['type'] == 'resultset', got
    assert got['rows'] == rows, (len(got['rows']), got['rows'][:3])
    # 文本 PREPARE/EXECUTE 与二进制 PREPARE 结果一致
    c.query_ok("PREPARE tq FROM 'SELECT name FROM big WHERE id = ?'")
    c.query_ok('SET @x = 1234')
    text_rows = c.query_rows('EXECUTE tq USING @x')
    binq = c.prepare('SELECT name FROM big WHERE id = ?')
    bin_rows = c.stmt_execute(binq['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 1234)])['rows']
    assert text_rows == bin_rows == [('n1234',)], (text_rows, bin_rows)
    c.query_ok('DEALLOCATE PREPARE tq')
    print('\n    大结果集 3000 行二进制结果完整；二进制/文本 PREPARE 结果一致')


# ==================== pymysql / 真实客户端兼容性 ====================

def test_pymysql_prepared_capability_or_e2e(env):
    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=env['port'], user='root',
                           password='', database='default', autocommit=True,
                           connect_timeout=10)
    try:
        params = inspect.signature(conn.cursor).parameters
        if 'prepared' not in params:
            pytest.skip(
                '本机 PyMySQL %s 的 cursor() 不支持 prepared=True（它只做'
                '客户端参数内联）；已用上面的字节级协议客户端覆盖 '
                'COM_STMT_*，mysql-connector-python 用例另行 skip'
                % pymysql.__version__)
        cur = conn.cursor(prepared=True)
        try:
            cur.execute('CREATE TABLE py (id INT PRIMARY KEY, name VARCHAR(20))')
            cur.execute('INSERT INTO py VALUES (?, ?)', (1, 'a'))
            cur.execute('INSERT INTO py VALUES (?, ?)', (2, 'b'))
            cur.execute('UPDATE py SET name = ? WHERE id = ?', ('c', 2))
            cur.execute('DELETE FROM py WHERE id = ?', (1,))
            cur.execute('SELECT id, name FROM py')
            assert cur.fetchall() == [(2, 'c')]
            cur.close()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        conn.close()
