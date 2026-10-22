# -*- coding: utf-8 -*-
"""E3 引擎兼容第三批验收（视图 / SQL 层 PREPARE / SHOW BINARY LOGS / 字符集）

覆盖（对应 docs/GUI_NAVICAT_PARITY_ROADMAP.md §3 E3）：

1. **视图**：CREATE [OR REPLACE] VIEW（含列别名）、SHOW CREATE VIEW、
   DROP VIEW [IF EXISTS]、SHOW FULL TABLES Table_type=VIEW、
   SHOW TABLES/TABLE STATUS 中的视图行、SELECT 展开（WHERE/聚合/
   多视图嵌套）、元数据持久化（重启后仍可用）；重名 1050 / 缺失基表 1146 /
   缺失视图 1051 / 循环依赖 1356 / 不支持的 JOIN 显式 1105。
2. **SQL 层 PREPARE/EXECUTE/DEALLOCATE**：字符串字面量与 `FROM @var`
   两种来源、`USING @a,@b` 参数替换、结果集与直接执行一致、参数个数
   1210 / 未知语句 1243 / 语法错误 1064、会话级（新连接不可见）。
3. **SHOW BINARY LOGS / SHOW MASTER LOGS**：真实 binlog 文件名 + 磁盘
   实际大小，与 SHOW MASTER STATUS 的 File 一致；binlog 关闭时显式 1381
   （不编造文件清单）。
4. **旧客户端字符集**：SET NAMES gbk/latin1 真正转码（入：SQL 字节按
   声明编码严格解码；出：结果集按声明编码回写），gbk 中文往返正确，
   非法字节 → 1366 且不落坏数据；utf8/utf8mb4 语义不变。
"""
import os
import struct
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8915


# ==================== fixtures ====================

@pytest.fixture()
def inproc(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    try:
        yield db, ex, session
    finally:
        db.shutdown()


@pytest.fixture()
def served(tmp_path):
    pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    cfg.root_password = ''
    cfg.allow_empty_password = True
    db = Database(str(tmp_path / 'data_srv'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    conns = []

    def connect(charset='utf8mb4'):
        import pymysql
        c = pymysql.connect(
            host='127.0.0.1', port=PORT, user='root', password='',
            database='default', charset=charset,
            autocommit=True, connect_timeout=10)
        conns.append(c)
        return c

    conn = connect()
    try:
        yield conn, connect, db, ex
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        for closer in (srv.shutdown, srv.server_close, db.shutdown):
            try:
                closer()
            except Exception:
                pass


def run(ex, sql, session=None):
    return ex.execute_sql(sql, session if session is not None else {})


def ok(ex, sql, session=None):
    r = run(ex, sql, session)
    assert r.get('type') != 'error', (sql, r)
    return r


def err(ex, sql, session=None):
    r = run(ex, sql, session)
    assert r.get('type') == 'error', (sql, r)
    return r


def rows(ex, sql, session=None):
    return ok(ex, sql, session)['rows']


# ==================== 1. 视图 ====================

def test_view_create_show_full_tables_select(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(20), '
           'age INT)', s)
    ok(ex, "INSERT INTO users VALUES (1,'alice',30),(2,'bob',20),"
           "(3,'carol',40)", s)
    ok(ex, 'CREATE VIEW adult AS SELECT id, name, age FROM users '
           'WHERE age >= 30', s)
    ok(ex, 'CREATE VIEW stats AS SELECT COUNT(*) AS n, SUM(age) AS total '
           'FROM users', s)

    full = ok(ex, 'SHOW FULL TABLES', s)
    types = {r[0]: r[1] for r in full['rows']}
    assert types['users'] == 'BASE TABLE'
    assert types['adult'] == 'VIEW' and types['stats'] == 'VIEW', types
    tables = [r[0] for r in rows(ex, 'SHOW TABLES', s)]
    assert 'adult' in tables and 'users' in tables, tables

    show = ok(ex, 'SHOW CREATE VIEW adult', s)
    assert show['columns'] == ['View', 'Create View',
                               'character_set_client',
                               'collation_connection']
    assert show['rows'][0][0] == 'adult'
    assert 'CREATE' in show['rows'][0][1] and 'VIEW' in show['rows'][0][1]
    assert 'SELECT id, name, age FROM users' in show['rows'][0][1]

    # SELECT 展开：WHERE 过滤 + 外层 WHERE/ORDER BY
    assert rows(ex, 'SELECT * FROM adult', s) == [
        (1, 'alice', 30), (3, 'carol', 40)]
    assert rows(ex, 'SELECT name FROM adult WHERE age > 30', s) == [
        ('carol',)]
    # 聚合视图
    assert rows(ex, 'SELECT * FROM stats', s) == [(3, 90)]

    status = ok(ex, 'SHOW TABLE STATUS', s)
    cols = status['columns']
    row = next(r for r in status['rows'] if r[cols.index('Name')] == 'adult')
    assert row[cols.index('Comment')] == 'VIEW', row
    assert row[cols.index('Engine')] is None, row


def test_view_replace_alias_drop_and_errors(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)', s)
    ok(ex, 'INSERT INTO t VALUES (1, 10), (2, 20)', s)
    ok(ex, 'CREATE VIEW v1 AS SELECT id, v FROM t', s)
    # 重名（无 OR REPLACE）→ 1050
    r = err(ex, 'CREATE VIEW v1 AS SELECT id FROM t', s)
    assert r['code'] == 1050, r
    # OR REPLACE 真实替换定义
    ok(ex, 'CREATE OR REPLACE VIEW v1 AS SELECT id FROM t WHERE id = 1', s)
    assert rows(ex, 'SELECT * FROM v1', s) == [(1,)]
    # 列别名视图
    ok(ex, 'CREATE VIEW v2 (a, b) AS SELECT id, v FROM t ORDER BY id', s)
    assert ok(ex, 'SHOW CREATE VIEW v2', s)['rows'][0][0] == 'v2'
    assert rows(ex, 'SELECT a, b FROM v2', s) == [(1, 10), (2, 20)]
    # DROP VIEW 缺失 → 1051；IF EXISTS 成功
    r = err(ex, 'DROP VIEW nope', s)
    assert r['code'] == 1051, r
    ok(ex, 'DROP VIEW IF EXISTS nope', s)
    ok(ex, 'DROP VIEW v1, v2', s)
    assert 'v1' not in [r[0] for r in rows(ex, 'SHOW TABLES', s)]


def test_view_missing_base_and_cycles_are_explicit(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY)', s)
    # 基表不存在 → 1146（显式，不建出一个永远报错的视图）
    r = err(ex, 'CREATE VIEW bad AS SELECT * FROM no_such', s)
    assert r['code'] == 1146, r
    # 自引用 → 1356
    r = err(ex, 'CREATE VIEW cyc AS SELECT * FROM cyc', s)
    assert r['code'] == 1356, r
    # 互引用（通过 OR REPLACE 形成环）→ 1356
    ok(ex, 'CREATE VIEW a AS SELECT * FROM t', s)
    ok(ex, 'CREATE VIEW b AS SELECT * FROM a', s)
    r = err(ex, 'CREATE OR REPLACE VIEW a AS SELECT * FROM b', s)
    assert r['code'] == 1356, r
    # 基表被删后查询视图：显式错误（不是静默空结果）
    ok(ex, 'DROP VIEW a, b', s)
    ok(ex, 'CREATE VIEW c AS SELECT * FROM t', s)
    ok(ex, 'DROP TABLE t', s)
    r = err(ex, 'SELECT * FROM c', s)
    assert r['code'] == 1146, r
    # 视图与表同名（CREATE TABLE 走自己的 1050；CREATE VIEW 也 1050）
    ok(ex, 'CREATE TABLE real_t (id INT PRIMARY KEY)', s)
    r = err(ex, 'CREATE VIEW real_t AS SELECT * FROM real_t', s)
    assert r['code'] == 1050, r


def test_view_join_is_explicitly_unsupported(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE a (id INT PRIMARY KEY)', s)
    ok(ex, 'CREATE TABLE b (id INT PRIMARY KEY)', s)
    ok(ex, 'CREATE VIEW va AS SELECT id FROM a', s)
    r = err(ex, 'SELECT * FROM va JOIN b ON va.id = b.id', s)
    assert r['code'] == 1105 and '视图' in r['message'], r


def test_view_persists_after_restart(inproc):
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)', s)
    ok(ex, 'INSERT INTO t VALUES (1, 5), (2, 7)', s)
    ok(ex, 'CREATE VIEW pv AS SELECT id, v FROM t WHERE v > 5', s)
    data_dir = str(db.data_dir)
    db.shutdown()
    from lightdb.db.database import Database as _DB
    from lightdb.sql.executor import Executor as _Ex
    db2 = _DB(data_dir)
    ex2 = _Ex(db2)
    try:
        full = ok(ex2, 'SHOW FULL TABLES', {})
        assert ('pv', 'VIEW') in full['rows'], full['rows']
        assert rows(ex2, 'SELECT * FROM pv', {}) == [(2, 7)]
        show = ok(ex2, 'SHOW CREATE VIEW pv', {})
        assert 'VIEW' in show['rows'][0][1]
    finally:
        db2.shutdown()


def test_view_through_protocol(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO t VALUES (1, 10), (2, 20)')
        cur.execute('CREATE VIEW pv AS SELECT id, v FROM t')
        cur.execute('SHOW FULL TABLES')
        types = {r[0]: r[1] for r in cur.fetchall()}
        assert types.get('pv') == 'VIEW', types
        cur.execute('SELECT * FROM pv ORDER BY id')
        assert list(cur.fetchall()) == [(1, 10), (2, 20)]
        cur.execute('SHOW CREATE VIEW pv')
        row = cur.fetchone()
        assert 'VIEW' in row[1], row
        cur.execute('DROP VIEW pv')
        cur.execute('SHOW FULL TABLES')
        assert 'pv' not in {r[0] for r in cur.fetchall()}


# ==================== 2. SQL 层 PREPARE / EXECUTE / DEALLOCATE ====================

def test_prepare_execute_results_and_params(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20), '
           'age INT)', s)
    ok(ex, "INSERT INTO t VALUES (1,'alice',30),(2,'bob',20)", s)

    ok(ex, "PREPARE q1 FROM 'SELECT id, name FROM t WHERE age >= ? "
           "ORDER BY id'", s)
    ok(ex, 'SET @min_age = 25', s)
    got = ok(ex, 'EXECUTE q1 USING @min_age', s)
    assert got['type'] == 'resultset'
    assert got['rows'] == [(1, 'alice')], got
    # 参数化字符串
    ok(ex, "PREPARE q2 FROM 'SELECT id FROM t WHERE name = ?'", s)
    ok(ex, "SET @who = 'bob'", s)
    assert ok(ex, 'EXECUTE q2 USING @who', s)['rows'] == [(2,)]
    # 参数化 INSERT
    ok(ex, 'PREPARE ins FROM "INSERT INTO t VALUES (?, ?, ?)"', s)
    ok(ex, 'SET @i = 9', s)
    ok(ex, "SET @n = 'zed'", s)
    ok(ex, 'SET @a = 55', s)
    ok(ex, 'EXECUTE ins USING @i, @n, @a', s)
    assert rows(ex, 'SELECT name FROM t WHERE id = 9', s) == [('zed',)]
    # 参数个数错误 → 1210（且后续仍可正常执行）
    r = err(ex, 'EXECUTE q1', s)
    assert r['code'] == 1210, r
    ok(ex, 'EXECUTE q1 USING @min_age', s)
    # 从用户变量准备
    ok(ex, "SET @sql = 'SELECT COUNT(*) AS n FROM t'", s)
    ok(ex, 'PREPARE q3 FROM @sql', s)
    assert ok(ex, 'EXECUTE q3', s)['rows'] == [(3,)]


def test_prepare_errors_and_deallocate(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY)', s)
    # 语法错误 → 1064（PREPARE 阶段就拒绝）
    r = err(ex, "PREPARE bad FROM 'SELECT FROM'", s)
    assert r['code'] == 1064, r
    # 未知 EXECUTE / DEALLOCATE → 1243
    r = err(ex, 'EXECUTE nope', s)
    assert r['code'] == 1243, r
    r = err(ex, 'DEALLOCATE PREPARE nope', s)
    assert r['code'] == 1243, r
    # 同名 PREPARE 隐式替换（MySQL 语义）
    ok(ex, 'PREPARE p FROM "SELECT 1 AS x"', s)
    ok(ex, 'PREPARE p FROM "SELECT 2 AS x"', s)
    assert ok(ex, 'EXECUTE p', s)['rows'] == [(2,)]
    ok(ex, 'DEALLOCATE PREPARE p', s)
    r = err(ex, 'EXECUTE p', s)
    assert r['code'] == 1243, r
    # 会话隔离：另一会话看不到本会话的 PREPARE
    other = {}
    r = err(ex, 'EXECUTE p', other)
    assert r['code'] == 1243, r
    # 类型错误在执行期显式（不是静默写入）
    ok(ex, 'CREATE TABLE ti (id INT PRIMARY KEY, n INT)', s)
    ok(ex, 'PREPARE pint FROM "INSERT INTO ti VALUES (?, ?)"', s)
    ok(ex, "SET @bad = 'abc'", s)
    ok(ex, 'SET @one = 1', s)
    r = err(ex, 'EXECUTE pint USING @one, @bad', s)
    assert r['code'] in (1366, 1265), r
    assert rows(ex, 'SELECT COUNT(*) FROM ti', s) == [(0,)]


def test_prepare_through_protocol(served):
    conn, connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO t VALUES (1, 10), (2, 20)')
        cur.execute("PREPARE p FROM 'SELECT id, v FROM t WHERE id = ?'")
        cur.execute('SET @x = 2')
        cur.execute('EXECUTE p USING @x')
        assert list(cur.fetchall()) == [(2, 20)]
        cur.execute('DEALLOCATE PREPARE p')
        with pytest.raises(Exception) as ei:
            cur.execute('EXECUTE p')
        assert getattr(ei.value, 'args', (None,))[0] == 1243, ei.value
    # 会话级：新连接看不到上一条连接的 prepared statement
    conn2 = connect()
    with conn2.cursor() as cur:
        with pytest.raises(Exception) as ei:
            cur.execute('EXECUTE p')
        assert getattr(ei.value, 'args', (None,))[0] == 1243, ei.value


# ==================== 3. SHOW BINARY LOGS ====================

def test_show_binary_logs_real_files(inproc):
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY)', s)
    ok(ex, 'INSERT INTO t VALUES (1)', s)
    got = ok(ex, 'SHOW BINARY LOGS', s)
    assert got['columns'] == ['Log_name', 'File_size']
    assert got['rows'], got
    name, size = got['rows'][-1]
    assert name.startswith('binlog.'), name
    assert size > 0, size
    # 与 SHOW MASTER STATUS 的 File 一致；大小 = 磁盘真实大小
    master = ok(ex, 'SHOW MASTER STATUS', s)
    assert name == master['rows'][0][0]
    from pathlib import Path
    disk = Path(str(db.data_dir)) / 'binlog' / name
    assert disk.exists() and disk.stat().st_size == size
    assert ok(ex, 'SHOW MASTER LOGS', s)['rows'] == got['rows']


def test_show_binary_logs_disabled_is_explicit(tmp_path, monkeypatch):
    """binlog 关闭（LIGHTDB_BINLOG_ENABLED=0）→ 1381，不编造文件清单"""
    monkeypatch.setenv('LIGHTDB_BINLOG_ENABLED', '0')
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data_off'))
    ex = Executor(db)
    try:
        for sql in ('SHOW BINARY LOGS', 'SHOW MASTER LOGS'):
            r = err(ex, sql, {})
            assert r['code'] == 1381, (sql, r)
            assert 'binary logging' in r['message'], r
        # SHOW MASTER STATUS 在关闭时是空结果集（既有语义，不编造位点）
        r = ex.execute_sql('SHOW MASTER STATUS', {})
        assert r.get('type') == 'resultset' and r['rows'] == [], r
    finally:
        db.shutdown()


def test_show_binary_logs_through_protocol(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
        cur.execute('INSERT INTO t VALUES (1)')
        cur.execute('SHOW BINARY LOGS')
        assert [d[0] for d in cur.description] == ['Log_name', 'File_size']
        got = cur.fetchall()
        assert got and got[0][0].startswith('binlog.') and got[0][1] > 0, got


# ==================== 4. 旧客户端字符集（gbk / latin1 真正转码） ====================

def test_set_names_gbk_transcodes_roundtrip(served):
    conn, connect, db, _ex = served
    gbk = connect(charset='gbk')
    with gbk.cursor() as cur:
        cur.execute('SELECT @@character_set_client')
        assert cur.fetchone()[0] == 'gbk'
        cur.execute('CREATE TABLE g (id INT PRIMARY KEY, name VARCHAR(30))')
        cur.execute("INSERT INTO g VALUES (1, '中文测试')")
        cur.execute('SELECT id, name FROM g')
        assert cur.fetchall() == ((1, '中文测试'),)
        # 会话级 charset 在后续语句保持
        cur.execute('SELECT @@character_set_client')
        assert cur.fetchone()[0] == 'gbk'
    # utf8mb4 连接读到同一份真实 Unicode 数据（不是乱码字节）
    with conn.cursor() as cur:
        cur.execute('SELECT name FROM g WHERE id = 1')
        assert cur.fetchone()[0] == '中文测试'
    # 非法 GBK 字节 → 1366，且不落坏数据
    try:
        gbk.write_packet(b'\x03INSERT INTO g VALUES (2, ' + b"'\xff\xfe')")
        gbk._read_packet()
        raise AssertionError('非法 GBK 字节必须返回 ERR 包')
    except Exception as e:
        assert getattr(e, 'args', (None,))[0] == 1366, e
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM g')
        assert cur.fetchone()[0] == 1


def test_set_names_latin1_transcodes_roundtrip(served):
    conn, connect, _db, _ex = served
    lat = connect(charset='latin1')
    with lat.cursor() as cur:
        cur.execute('SELECT @@character_set_client')
        assert cur.fetchone()[0] == 'latin1'
        cur.execute('CREATE TABLE l (id INT PRIMARY KEY, s VARCHAR(20))')
        cur.execute("INSERT INTO l VALUES (1, 'caf\xe9')")
        cur.execute('SELECT s FROM l WHERE id = 1')
        assert cur.fetchone()[0] == 'caf\xe9'
        cur.execute('SET NAMES latin1 COLLATE latin1_bin')
        cur.execute('SELECT s FROM l WHERE id = 1')
        assert cur.fetchone()[0] == 'caf\xe9'
        with pytest.raises(Exception) as ei:
            cur.execute('SET NAMES latin1 COLLATE utf8mb4_bin')
        assert getattr(ei.value, 'args', (None,))[0] == 1273, ei.value
    # utf8mb4 连接读到同一个字符（转码正确，不是字节冒充）
    with conn.cursor() as cur:
        cur.execute('SELECT s FROM l WHERE id = 1')
        assert cur.fetchone()[0] == 'café'


def test_set_names_inproc_and_unknown_charset(inproc):
    _db, ex, s = inproc
    assert ok(ex, 'SET NAMES gbk', s).get('type') == 'ok'
    assert s['variables']['character_set_client'] == 'gbk'
    assert ok(ex, 'SET NAMES latin1 COLLATE latin1_bin', s).get('type') == 'ok'
    r = err(ex, 'SET NAMES koi8r', s)
    assert r['code'] == 1115, r
    r = err(ex, 'SET NAMES gbk COLLATE utf8mb4_bin', s)
    assert r['code'] == 1273, r


# ==================== 5. Navicat 典型序列（视图/PREPARE/BINARY LOGS） ====================

def test_navicat_view_prepare_binlog_sequence(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE base (id INT PRIMARY KEY, name VARCHAR(10))')
        cur.execute("INSERT INTO base VALUES (1,'a'),(2,'b')")
        cur.execute('CREATE VIEW vw AS SELECT id, name FROM base WHERE id >= 1')
        cur.execute('SHOW FULL TABLES')
        rows_ = {r[0]: r[1] for r in cur.fetchall()}
        assert rows_.get('vw') == 'VIEW', rows_
        cur.execute('SHOW CREATE VIEW vw')
        assert 'VIEW' in cur.fetchone()[1]
        cur.execute('SELECT * FROM vw ORDER BY id')
        assert list(cur.fetchall()) == [(1, 'a'), (2, 'b')]
        cur.execute("PREPARE nv FROM 'SELECT name FROM vw WHERE id = ?'")
        cur.execute('SET @id = 2')
        cur.execute('EXECUTE nv USING @id')
        assert list(cur.fetchall()) == [('b',)]
        cur.execute('DEALLOCATE PREPARE nv')
        cur.execute('SHOW BINARY LOGS')
        files = cur.fetchall()
        assert files and files[0][0].startswith('binlog.'), files
        cur.execute('DROP VIEW vw')
