# -*- coding: utf-8 -*-
"""P4-A / E1 第三方客户端兼容验收（SHOW 元数据 / 静默语义 / 字符集 / SAVEPOINT）

覆盖（对应 docs/GUI_NAVICAT_PARITY_ROADMAP.md §3 E1）：

1. **SHOW 元数据兼容**（Navicat/mysql CLI 高频序列，列名对齐 MySQL、
   取值真实不编造）：SHOW TABLE STATUS / FULL COLUMNS / FULL TABLES /
   COLLATION / CHARSET / CHARACTER SET / ENGINES / WARNINGS / GRANTS /
   SHOW CREATE TABLE；`SHOW CREATE VIEW` 显式报错（视图是 E2）。
2. **静默语义审计**：`PARTITION BY`、`CREATE PROCEDURE`/`CALL`、
   `ENGINE=`/`CHARSET=`/`COLLATE=`/`AUTO_INCREMENT=`/`ROW_FORMAT=`/
   `ON UPDATE`/生成列 —— 支持的有真实用例，不支持的一律**显式错误**，
   绝无"接受但未生效"。
3. **字符集严格化**：非法 UTF-8 字节 → **1366**（不存 U+FFFD）；
   `SET NAMES` 只接受 utf8/utf8mb4/utf8mb3（gbk/latin1 → 1115）；
   不支持 `_ci` 排序规则（→1273）。
4. **SAVEPOINT**：SAVEPOINT / RELEASE SAVEPOINT / ROLLBACK TO SAVEPOINT
   的嵌套/重名/不存在语义，与 BEGIN/COMMIT/ROLLBACK/MVCC 协作。

测试同时用两层：进程内 `Executor`（快、判错误码）与真实
in-process `MySQLServer` + pymysql（协议/客户端路径）。
"""
import os
import re
import socket
import struct
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8907


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
    pymysql = pytest.importorskip('pymysql')
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

    def connect(autocommit=True, database='default'):
        c = pymysql.connect(
            host='127.0.0.1', port=PORT, user='root', password='',
            database=database, charset='utf8mb4',
            autocommit=autocommit, connect_timeout=10)
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
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
        try:
            db.shutdown()
        except Exception:
            pass


def _rows(cur):
    cur.execute('SHOW WARNINGS')
    return cur.fetchall()


DDL = (
    "CREATE TABLE nav (id INT PRIMARY KEY, name VARCHAR(20) COMMENT '姓名', "
    "amount INT, up VARCHAR(40) GENERATED ALWAYS AS (UPPER(name)) STORED, "
    "note VARCHAR(50)) ENGINE=LightDB DEFAULT CHARSET=utf8mb4 "
    "COLLATE=utf8mb4_bin COMMENT='导航测试表'"
)


# ==================== 1. SHOW 元数据（协议路径 / Navicat 序列）====================

def test_navicat_connection_show_sequence(served):
    """Navicat/mysql CLI 连接后常发的 SHOW 序列：全部有结果，不 1064"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("INSERT INTO nav (id, name, amount, note) "
                    "VALUES (1, 'abc', 5, NULL), (2, '中文', NULL, 'x')")

        sequence = [
            'SELECT VERSION()',
            'SELECT @@version_comment',
            'SELECT @@character_set_client',
            'SELECT DATABASE()',
            'SHOW DATABASES',
            'SHOW TABLES',
            'SHOW FULL TABLES',
            "SHOW FULL TABLES LIKE 'nav%'",
            'SHOW TABLE STATUS',
            "SHOW TABLE STATUS LIKE 'nav%'",
            'SHOW FULL COLUMNS FROM nav',
            'SHOW COLUMNS FROM nav',
            'SHOW INDEX FROM nav',
            'SHOW CREATE TABLE nav',
            'SHOW COLLATION',
            'SHOW CHARSET',
            'SHOW CHARACTER SET',
            'SHOW ENGINES',
            'SHOW WARNINGS',
            'SHOW GRANTS',
            'SHOW VARIABLES LIKE "version%"',
            'SHOW STATUS LIKE "Threads%"',
        ]
        for sql in sequence:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            assert cols, (sql, '没有列元数据')
            assert sql.startswith('SHOW') or rows is not None, sql

        # 只读元数据查询不应把连接搞坏
        cur.execute('SELECT COUNT(*) FROM nav')
        assert cur.fetchone()[0] == 2

        # 视图是 E2：SHOW CREATE VIEW 必须**显式报错**，不能 500/空结果
        with pytest.raises(Exception) as ei:
            cur.execute('SHOW CREATE VIEW no_view')
        assert '视图' in str(ei.value) or 'VIEW' in str(ei.value), ei.value


def test_show_metadata_values_are_real(served):
    """SHOW 的取值来自真实元数据（Engine/Rows/Comment/Generated/Charset）"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("INSERT INTO nav (id, name, amount) VALUES (1, 'abc', 5)")

        cur.execute('SHOW TABLE STATUS')
        cols = [d[0] for d in cur.description]
        idx = {c: i for i, c in enumerate(cols)}
        row = next(r for r in cur.fetchall() if r[idx['Name']] == 'nav')
        assert row[idx['Engine']] == 'LightDB', row
        assert row[idx['Rows']] == 1, row
        assert row[idx['Collation']] == 'utf8mb4_bin', row
        assert row[idx['Comment']] == '导航测试表', row
        # 不可用字段填 NULL（不编造）
        assert row[idx['Index_length']] is None, row
        assert row[idx['Update_time']] is None, row

        cur.execute('SHOW FULL COLUMNS FROM nav')
        fcols = [d[0] for d in cur.description]
        assert fcols == ['Field', 'Type', 'Collation', 'Null', 'Key',
                         'Default', 'Extra', 'Privileges', 'Comment'], fcols
        by = {r[0]: r for r in cur.fetchall()}
        assert by['name'][2] == 'utf8mb4_bin'      # 字符串列真实 collation
        assert by['id'][2] is None                 # 数值列无 collation
        assert by['name'][8] == '姓名'             # 列注释真实
        assert by['up'][6] == 'STORED GENERATED'   # 生成列 Extra 真实

        cur.execute('SHOW FULL TABLES')
        assert [d[0] for d in cur.description] == [
            'Tables_in_default', 'Table_type']
        assert ('nav', 'BASE TABLE') in cur.fetchall()

        cur.execute('SHOW CREATE TABLE nav')
        ddl = cur.fetchone()[1]
        assert 'ENGINE=LightDB' in ddl
        assert "COMMENT='导航测试表'" in ddl
        assert "COMMENT '姓名'" in ddl
        assert 'GENERATED ALWAYS AS (UPPER(name)) STORED' in ddl
        assert 'DEFAULT CHARSET=utf8mb4' in ddl
        assert 'COLLATE=utf8mb4_bin' in ddl


def test_show_collation_charset_engines_warnings(served):
    """COLLATION/CHARSET/ENGINES/WARNINGS：真实支持集合，不硬编码 MySQL 假数据"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('SHOW COLLATION')
        assert [d[0] for d in cur.description][:4] == [
            'Collation', 'Charset', 'Id', 'Default']
        collations = {r[0] for r in cur.fetchall()}
        assert collations == {'utf8mb4_bin', 'utf8_bin'}, collations

        cur.execute('SHOW CHARSET')
        cols = [d[0] for d in cur.description]
        assert cols == ['Charset', 'Description', 'Default collation',
                        'Maxlen']
        charsets = {r[0] for r in cur.fetchall()}
        assert charsets == {'utf8mb4', 'utf8'}, charsets

        cur.execute('SHOW ENGINES')
        engines = cur.fetchall()
        assert len(engines) == 1 and engines[0][0] == 'LightDB', engines
        assert engines[0][1] == 'DEFAULT'

        cur.execute('SHOW WARNINGS')
        assert [d[0] for d in cur.description] == ['Level', 'Code', 'Message']
        assert list(cur.fetchall()) == []

        cur.execute('SHOW GRANTS')
        grants = [r[0] for r in cur.fetchall()]
        assert grants and all('GRANT' in g for g in grants), grants


# ==================== 2. 静默语义审计 ====================

@pytest.mark.parametrize('ddl,code', [
    ("CREATE TABLE e_eng (id INT PRIMARY KEY) ENGINE=InnoDB", 1064),
    ("CREATE TABLE e_cs (id INT PRIMARY KEY) DEFAULT CHARSET=gbk", 1115),
    ("CREATE TABLE e_co (id INT PRIMARY KEY) COLLATE=utf8mb4_general_ci",
     1273),
    ("CREATE TABLE e_ai (id INT PRIMARY KEY) AUTO_INCREMENT=100", 1064),
    ("CREATE TABLE e_rf (id INT PRIMARY KEY) ROW_FORMAT=COMPRESSED", 1064),
    ("CREATE TABLE e_ou (id INT PRIMARY KEY, "
     "ts INT ON UPDATE CURRENT_TIMESTAMP)", 1064),
])
def test_unsupported_table_options_are_explicit(inproc, ddl, code):
    """不支持的表选项/列属性 → 显式错误（不再是"接受后丢掉"）"""
    _db, ex, s = inproc
    r = ex.execute_sql(ddl, s)
    assert r.get('type') == 'error', r
    assert r.get('code') == code, (ddl, r)
    assert r.get('message'), r


def test_supported_table_options_take_effect_and_persist(inproc, tmp_path):
    """支持的 ENGINE/CHARSET/COLLATE/COMMENT 真实生效，且重启后回显"""
    from lightdb.db.database import Database
    db, ex, s = inproc
    r = ex.execute_sql(
        "CREATE TABLE opt (id INT PRIMARY KEY, v INT) ENGINE=LightDB "
        "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='选项表'", s)
    assert r.get('type') == 'ok', r
    status = ex.execute_sql('SHOW TABLE STATUS', s)
    row = status['rows'][0]
    assert row[1] == 'LightDB'            # Engine
    assert row[14] == 'utf8mb4_bin'       # Collation
    assert row[17] == '选项表'             # Comment
    show = ex.execute_sql('SHOW CREATE TABLE opt', s)
    ddl = show['rows'][0][1]
    assert 'ENGINE=LightDB' in ddl and "COMMENT='选项表'" in ddl, ddl
    # 持久化：重启（关闭再打开）后 SHOW CREATE 仍回显表选项/注释
    db.shutdown()
    from lightdb.db.database import Database as _DB2
    from lightdb.sql.executor import Executor as _Ex2
    db2 = _DB2(str(tmp_path / 'db'))
    ex2 = _Ex2(db2)
    try:
        ddl2 = ex2.execute_sql('SHOW CREATE TABLE opt', {})['rows'][0][1]
        assert 'ENGINE=LightDB' in ddl2 and "COMMENT='选项表'" in ddl2, ddl2
    finally:
        db2.shutdown()


def test_partition_and_generated_are_not_silent(inproc):
    """PARTITION BY / 生成列：真实生效并出现在 SHOW CREATE（不再静默丢弃）"""
    _db, ex, s = inproc
    r = ex.execute_sql(
        'CREATE TABLE pt (id INT PRIMARY KEY, v INT) '
        'PARTITION BY HASH(id) PARTITIONS 4', s)
    assert r.get('type') == 'ok', r
    ddl = ex.execute_sql('SHOW CREATE TABLE pt', s)['rows'][0][1]
    assert 'PARTITION BY HASH(id) PARTITIONS 4' in ddl, ddl
    parts = ex.execute_sql('SHOW PARTITIONS FROM pt', s)
    assert len(parts['rows']) == 4, parts

    r = ex.execute_sql(
        'CREATE TABLE gc (id INT PRIMARY KEY, name VARCHAR(10), '
        'up VARCHAR(10) GENERATED ALWAYS AS (UPPER(name)) STORED)', s)
    assert r.get('type') == 'ok', r
    r = ex.execute_sql("INSERT INTO gc (id, name) VALUES (1, 'abc')", s)
    assert r.get('type') == 'ok', r
    got = ex.execute_sql('SELECT id, name, up FROM gc', s)
    assert got['rows'] == [(1, 'abc', 'ABC')], got
    # 显式给生成列赋值 = MySQL 3105（旧实现静默存用户值）
    r = ex.execute_sql(
        "INSERT INTO gc (id, name, up) VALUES (2, 'x', 'y')", s)
    assert r.get('type') == 'error' and r.get('code') == 3105, r


def test_procedure_call_result_set_not_silently_dropped(inproc):
    """CALL 的过程内结果集：单个 SELECT 返回结果；多结果集显式 1235"""
    _db, ex, s = inproc
    assert ex.execute_sql(
        'CREATE PROCEDURE p1() BEGIN SELECT 1 AS n; END', s)['type'] == 'ok'
    r = ex.execute_sql('CALL p1()', s)
    assert r.get('type') == 'resultset', r
    assert r['columns'] == ['n'] and r['rows'] == [(1,)], r

    assert ex.execute_sql(
        'CREATE PROCEDURE p2() BEGIN SELECT 1; SELECT 2; END', s)[
            'type'] == 'ok'
    r = ex.execute_sql('CALL p2()', s)
    assert r.get('type') == 'error' and r.get('code') == 1235, r
    assert '结果集' in r.get('message', ''), r

    # DML 过程仍正常（不因审计而回退）
    assert ex.execute_sql(
        'CREATE TABLE pt2 (id INT PRIMARY KEY)', s)['type'] == 'ok'
    assert ex.execute_sql(
        "CREATE PROCEDURE p3(IN x INT) BEGIN INSERT INTO pt2 VALUES (x); "
        "END", s)['type'] == 'ok'
    assert ex.execute_sql('CALL p3(7)', s)['type'] == 'ok'
    assert ex.execute_sql('SELECT id FROM pt2', s)['rows'] == [(7,)]


# ==================== 3. 字符集严格化 ====================

def test_set_names_supported_and_rejected(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('SET NAMES utf8')
        cur.execute('SET NAMES utf8mb4')
        cur.execute('SET NAMES utf8mb4 COLLATE utf8mb4_bin')
        # 既有协议测试锁定了连接元数据可记录 `_general_ci`（不改比较语义）
        cur.execute('SET NAMES utf8mb4 COLLATE utf8mb4_general_ci')
        cur.execute('SELECT @@character_set_client')
        assert cur.fetchone()[0] in ('utf8', 'utf8mb4')

        with pytest.raises(pymysql_err()) as ei:
            cur.execute('SET NAMES gbk')
        assert ei.value.args[0] == 1115, ei.value
        with pytest.raises(pymysql_err()) as ei:
            cur.execute('SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci')
        assert ei.value.args[0] == 1273, ei.value


def _packet_errno(pkt) -> int:
    """从 pymysql 原始包里取错误码（ERR 包：0xFF + 2 字节 errno）"""
    data = pkt._data
    assert data and data[0] == 0xFF, ('不是 ERR 包', data[:16])
    return struct.unpack('<H', data[1:3])[0]


def test_invalid_utf8_bytes_return_1366_not_replacement(served):
    """原始非法 UTF-8 字节：1366 错误，且不落 U+FFFD"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE cs (id INT PRIMARY KEY, s VARCHAR(20))')

    bad_sql = ("INSERT INTO cs (id, s) VALUES (1, '".encode('utf-8')
               + b'\xff\xfe' + b"')")
    # 走 pymysql 底层写包（仍然是在协议层发送**原始非法字节**，
    # 只是让客户端库管理 packet 序号，便于随后读响应）
    conn.write_packet(b'\x03' + bad_sql)
    pymysql = pytest.importorskip('pymysql')
    with pytest.raises(pymysql.err.DataError) as ei:
        conn._read_packet()          # pymysql 对 ERR 包会直接抛异常
    assert ei.value.args[0] == 1366, ei.value

    # 合法 UTF-8 写入/读回正常；表里没有 U+FFFD
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cs VALUES (2, '中文')")
    with conn.cursor() as cur:
        cur.execute('SELECT s FROM cs ORDER BY id')
        vals = [r[0] for r in cur.fetchall()]
    assert vals == ['中文'], vals
    assert all('\ufffd' not in str(v) for v in vals), vals


def pymysql_err():
    pymysql = pytest.importorskip('pymysql')
    return pymysql.err.OperationalError


# ==================== 4. SAVEPOINT ====================

def test_savepoint_basic_and_errors(inproc):
    _db, ex, s = inproc
    ex.execute_sql('CREATE TABLE sp (id INT PRIMARY KEY, v INT)', s)
    # 无活动事务：SAVEPOINT 为空操作（MySQL 语义），RELEASE/ROLLBACK TO → 1305
    assert ex.execute_sql('SAVEPOINT a', s)['type'] == 'ok'
    for sql in ('RELEASE SAVEPOINT a', 'ROLLBACK TO SAVEPOINT a'):
        r = ex.execute_sql(sql, s)
        assert r.get('type') == 'error' and r.get('code') == 1305, (sql, r)

    ex.execute_sql('BEGIN', s)
    ex.execute_sql('INSERT INTO sp VALUES (1, 1)', s)
    assert ex.execute_sql('SAVEPOINT a', s)['type'] == 'ok'
    ex.execute_sql('INSERT INTO sp VALUES (2, 2)', s)
    r = ex.execute_sql('ROLLBACK TO SAVEPOINT a', s)
    assert r.get('type') == 'ok', r
    assert ex.execute_sql('SELECT id FROM sp', s)['rows'] == [(1,)]
    # ROLLBACK TO 保留命名保存点本身（仍可再用；其后保存点被删）
    assert ex.execute_sql('SAVEPOINT b', s)['type'] == 'ok'
    assert ex.execute_sql('ROLLBACK TO SAVEPOINT a', s)['type'] == 'ok'
    r = ex.execute_sql('ROLLBACK TO SAVEPOINT b', s)
    assert r.get('type') == 'error' and r.get('code') == 1305, r
    assert ex.execute_sql('RELEASE SAVEPOINT a', s)['type'] == 'ok'
    r = ex.execute_sql('RELEASE SAVEPOINT a', s)
    assert r.get('type') == 'error' and r.get('code') == 1305, r
    assert ex.execute_sql('COMMIT', s)['type'] == 'ok'
    assert ex.execute_sql('SELECT id FROM sp', s)['rows'] == [(1,)]


def test_savepoint_nested_and_update_rollback(inproc):
    _db, ex, s = inproc
    ex.execute_sql('CREATE TABLE sp2 (id INT PRIMARY KEY, v INT)', s)
    ex.execute_sql('BEGIN', s)
    ex.execute_sql('INSERT INTO sp2 VALUES (1, 10)', s)
    ex.execute_sql('SAVEPOINT a', s)
    ex.execute_sql('INSERT INTO sp2 VALUES (2, 20)', s)
    ex.execute_sql('SAVEPOINT b', s)
    ex.execute_sql('UPDATE sp2 SET v = 99 WHERE id = 1', s)
    ex.execute_sql('INSERT INTO sp2 VALUES (3, 30)', s)
    # 回滚到 a：2/3 消失、id=1 的值仍是 10（UPDATE 真回滚）
    assert ex.execute_sql('ROLLBACK TO SAVEPOINT a', s)['type'] == 'ok'
    rows = ex.execute_sql('SELECT id, v FROM sp2 ORDER BY id', s)['rows']
    assert rows == [(1, 10)], rows
    assert ex.execute_sql('RELEASE SAVEPOINT a', s)['type'] == 'ok'
    assert ex.execute_sql('COMMIT', s)['type'] == 'ok'
    assert ex.execute_sql('SELECT id, v FROM sp2', s)['rows'] == [(1, 10)]


def test_savepoint_through_protocol(served):
    """协议路径（pymysql，autocommit=False）：SAVEPOINT 全链路可用"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE spx (id INT PRIMARY KEY, v INT)')
        cur.execute('BEGIN')
        cur.execute('INSERT INTO spx VALUES (1, 1)')
        cur.execute('SAVEPOINT sp1')
        cur.execute('INSERT INTO spx VALUES (2, 2)')
        cur.execute('ROLLBACK TO SAVEPOINT sp1')
        cur.execute('SELECT id FROM spx')
        assert cur.fetchall() == ((1,),)
        cur.execute('RELEASE SAVEPOINT sp1')
        cur.execute('COMMIT')
        cur.execute('SELECT id FROM spx')
        assert cur.fetchall() == ((1,),)
        with pytest.raises(pymysql_err()) as ei:
            cur.execute('ROLLBACK TO SAVEPOINT nope')
        assert ei.value.args[0] == 1305


# ==================== 5. 原始字节 / 老客户端兼容 ====================

def test_raw_socket_invalid_utf8_gets_1366(served):
    """协议层原始非法 COM_QUERY 字节 → 1366（不 U+FFFD）"""
    conn, connect, _db, _ex = served
    # 用一条新连接，避免污染 fixture 主连接的状态机
    c = connect()
    payload = b'\x03SELECT \xff\xfe'
    c.write_packet(payload)
    pymysql = pytest.importorskip('pymysql')
    with pytest.raises(pymysql.err.DataError) as ei:
        c._read_packet()
    assert ei.value.args[0] == 1366, ei.value
