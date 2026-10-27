# -*- coding: utf-8 -*-
"""task-57 验收：BINARY 一元表达式 + information_schema.EVENTS/TRIGGERS/ROUTINES

复现（2026-10-27，pymysql → 127.0.0.1:8898，修复前）：

    SELECT EVENT_SCHEMA, EVENT_NAME FROM information_schema.EVENTS
      WHERE BINARY EVENT_SCHEMA='default'
      → (1064, "SQL 解析错误: WHERE 条件错误: BINARY EVENT_SCHEMA='default'")
    SELECT TRIGGER_SCHEMA, TRIGGER_NAME FROM information_schema.TRIGGERS
      WHERE BINARY event_object_schema='default'
      → 1064（同上）
    SELECT ROUTINE_SCHEMA, ROUTINE_NAME FROM information_schema.ROUTINES
      WHERE BINARY ROUTINE_SCHEMA='default'
      → 1064（同上）

修复：
1. 解析器剥离 `BINARY <expr>` 一元前缀（引号外、AND/OR/NOT/比较两侧均可）。
   MySQL BINARY 语义 = 按字节、大小写敏感；本引擎默认 utf8mb4_bin
   （Python 码点比较）本已大小写敏感，因此等价成立。
2. information_schema.EVENTS / TRIGGERS / ROUTINES 提供完整 MySQL 列，
   本引擎无对应对象行时返回**空集**（不是 1146/1054，也不编造行）；
   COUNT(*) 等聚合与投影/过滤在虚拟表上同样生效。
"""
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ==================== fixtures ====================

@pytest.fixture()
def inproc(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    try:
        yield db, ex
    finally:
        try:
            db.shutdown()
        except Exception:
            pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture()
def served(tmp_path):
    pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    import pymysql

    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.root_password = ''
    cfg.allow_empty_password = True
    db = Database(str(tmp_path / 'data_srv'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password='', database='default',
                           charset='utf8mb4', autocommit=True,
                           connect_timeout=10)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        for fn in (srv.shutdown, srv.server_close):
            try:
                fn()
            except Exception:
                pass
        try:
            db.shutdown()
        except Exception:
            pass


def ok(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') != 'error', (sql, r)
    return r


def rows(ex, sql):
    return [tuple(r) for r in ok(ex, sql)['rows']]


def ids(ex, sql):
    return [r[0] for r in rows(ex, sql)]


def _seed(ex):
    ok(ex, 'CREATE TABLE b (id INT PRIMARY KEY, name VARCHAR(20), '
           'grp VARCHAR(10))')
    ok(ex, "INSERT INTO b VALUES (1, 'default', 'g1'), "
           "(2, 'DEFAULT', 'g1'), (3, 'Default', 'g2'), "
           "(4, NULL, 'g2')")


# ==================== 一、BINARY 表达式 / 大小写敏感 ====================

def test_binary_equality_is_case_sensitive(inproc):
    _db, ex = inproc
    _seed(ex)
    # 本引擎默认 utf8mb4_bin：不加 BINARY 也已大小写敏感
    assert ids(ex, "SELECT id FROM b WHERE name = 'default'") == [1]
    # BINARY 前缀可出现在比较左侧（Navicat 形态）
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = 'default'") == [1]
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = 'DEFAULT'") == [2]
    # 'DEFAULT' 与 'default' 不互相匹配（大小写敏感锁定）
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = 'DEFAULT'") != [1]
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = 'default'") != [2]
    # 两侧都可以加 BINARY；字面量在左
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = BINARY 'DEFAULT'") \
        == [2]
    assert ids(ex, "SELECT id FROM b WHERE 'DEFAULT' = BINARY name") == [2]
    # NULL 行不会被等值匹配
    assert ids(ex, "SELECT id FROM b WHERE BINARY name = NULL") == []
    print("\n    BINARY 等值：'DEFAULT' 只匹配 'DEFAULT'，不匹配 'default'；"
          "比较两侧/字面量左侧均可用")


def test_binary_with_and_or_not_like(inproc):
    _db, ex = inproc
    _seed(ex)
    # AND / OR 组合
    assert ids(ex, "SELECT id FROM b WHERE BINARY name='default' AND "
                   "grp='g1'") == [1]
    assert ids(ex, "SELECT id FROM b WHERE BINARY name='default' OR "
                   "BINARY name='DEFAULT' ORDER BY id") == [1, 2]
    # NOT BINARY / NOT (BINARY ...)
    assert ids(ex, "SELECT id FROM b WHERE NOT BINARY name='DEFAULT' "
                   "ORDER BY id") == [1, 3]
    assert ids(ex, "SELECT id FROM b WHERE NOT (BINARY name LIKE 'd%') "
                   "ORDER BY id") == [2, 3]
    # LIKE 前缀形式（大小写敏感）
    assert ids(ex, "SELECT id FROM b WHERE BINARY name LIKE 'd%'") == [1]
    assert ids(ex, "SELECT id FROM b WHERE BINARY name LIKE 'D%' "
                   "ORDER BY id") == [2, 3]
    # 字符串字面量里的 "BINARY" 不应被当成运算符剥离
    ok(ex, "INSERT INTO b VALUES (5, 'BINARY x', 'g3')")
    assert ids(ex, "SELECT id FROM b WHERE name = 'BINARY x'") == [5]
    print("\n    BINARY 与 AND/OR/NOT/LIKE 组合正确；字符串里的 BINARY "
          "不被误改写")


def test_binary_in_dml_where(inproc):
    _db, ex = inproc
    _seed(ex)
    ok(ex, "UPDATE b SET grp='new' WHERE BINARY name='DEFAULT'")
    assert rows(ex, "SELECT id, grp FROM b WHERE id=2") == [(2, 'new')]
    assert rows(ex, "SELECT grp FROM b WHERE id=1") == [('g1',)]
    ok(ex, "DELETE FROM b WHERE BINARY name='DEFAULT'")
    assert ids(ex, "SELECT id FROM b ORDER BY id") == [1, 3, 4]
    r = ok(ex, "SELECT id FROM b WHERE BINARY name='x'")
    assert r['rows'] == []
    print("\n    BINARY 前缀在 UPDATE/DELETE 的 WHERE 中同样生效")


# ==================== 二、information_schema 对象表 ====================

#: Navicat 备份页实际引用的关键列（完整 MySQL 列的超集检查）
EVENTS_NEED = ('EVENT_CATALOG', 'EVENT_SCHEMA', 'EVENT_NAME', 'DEFINER',
               'EVENT_TYPE', 'STATUS', 'ON_COMPLETION', 'CREATED',
               'LAST_ALTERED', 'EVENT_DEFINITION')
TRIGGERS_NEED = ('TRIGGER_CATALOG', 'TRIGGER_SCHEMA', 'TRIGGER_NAME',
                 'EVENT_MANIPULATION', 'EVENT_OBJECT_SCHEMA',
                 'EVENT_OBJECT_TABLE', 'ACTION_STATEMENT', 'ACTION_TIMING',
                 'DEFINER')
ROUTINES_NEED = ('SPECIFIC_NAME', 'ROUTINE_CATALOG', 'ROUTINE_SCHEMA',
                 'ROUTINE_NAME', 'ROUTINE_TYPE', 'DATA_TYPE',
                 'ROUTINE_DEFINITION', 'IS_DETERMINISTIC', 'SECURITY_TYPE',
                 'DEFINER')


def test_information_schema_object_tables_exist_with_columns(inproc):
    _db, ex = inproc
    for table, need in (('EVENTS', EVENTS_NEED),
                        ('TRIGGERS', TRIGGERS_NEED),
                        ('ROUTINES', ROUTINES_NEED)):
        r = ok(ex, f'SELECT * FROM information_schema.{table}')
        for c in need:
            assert c in r['columns'], (table, c, r['columns'])
        assert r['rows'] == [], (table, r['rows'])
        # 无对象时 COUNT(*) = 0（不是 1146/1054，也不是零列零行）
        r = ok(ex, f'SELECT COUNT(*) FROM information_schema.{table}')
        assert r['columns'] == ['COUNT(*)'], r['columns']
        assert [tuple(x) for x in r['rows']] == [(0,)], r['rows']
    print("\n    EVENTS/TRIGGERS/ROUTINES：MySQL 列齐全、空集、"
          "COUNT(*)=0")


def test_navicat_binary_backup_queries(inproc):
    _db, ex = inproc
    # 逐条复现 Navicat 备份序列；修复前第一条就是 1064
    q_events = ("SELECT EVENT_SCHEMA, EVENT_NAME, EVENT_TYPE, DEFINER, "
                "STATUS, LAST_ALTERED FROM information_schema.EVENTS "
                "WHERE BINARY EVENT_SCHEMA='default'")
    q_triggers = ("SELECT TRIGGER_SCHEMA, TRIGGER_NAME, EVENT_OBJECT_TABLE, "
                  "ACTION_TIMING, DEFINER FROM information_schema.TRIGGERS "
                  "WHERE BINARY event_object_schema='default'")
    q_routines = ("SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE, "
                  "DATA_TYPE, DEFINER FROM information_schema.ROUTINES "
                  "WHERE BINARY ROUTINE_SCHEMA='default'")
    for sql in (q_events, q_triggers, q_routines):
        r = ok(ex, sql)
        assert r['rows'] == [], (sql, r)
    # 大小写敏感：'DEFAULT' 不应命中（对象为空，两者都空，但解析/执行不报错）
    for sql in (
            "SELECT * FROM information_schema.EVENTS "
            "WHERE BINARY EVENT_SCHEMA='DEFAULT'",
            "SELECT * FROM information_schema.TRIGGERS "
            "WHERE BINARY EVENT_OBJECT_SCHEMA='DEFAULT'",
            "SELECT * FROM information_schema.ROUTINES "
            "WHERE BINARY ROUTINE_SCHEMA='DEFAULT'"):
        assert ok(ex, sql)['rows'] == [], sql
    # 未知 information_schema 表仍显式 1146（不静默空集）
    r = ex.execute_sql('SELECT * FROM information_schema.NO_SUCH_TABLE', {})
    assert r.get('type') == 'error' and r.get('code') == 1146, r
    print("\n    Navicat 备份序列：EVENTS/TRIGGERS/ROUTINES 的 "
          "BINARY <schema>='default' 查询全部 OK、空集")


# ==================== 三、pymysql 真实 server（Navicat 备份序列） ====================

def test_pymysql_binary_and_object_tables(served):
    conn = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE bx (id INT PRIMARY KEY, name VARCHAR(20))')
        cur.execute("INSERT INTO bx VALUES (1, 'default'), (2, 'DEFAULT')")
        cur.execute("SELECT id FROM bx WHERE BINARY name='default'")
        assert [r[0] for r in cur.fetchall()] == [1]
        cur.execute("SELECT id FROM bx WHERE BINARY name='DEFAULT'")
        assert [r[0] for r in cur.fetchall()] == [2]
        cur.execute("SELECT id FROM bx WHERE NOT BINARY name='DEFAULT'")
        assert [r[0] for r in cur.fetchall()] == [1]
        cur.execute("SELECT id FROM bx WHERE BINARY name='default' OR "
                    "BINARY name='DEFAULT' ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == [1, 2]
        # Navicat 备份/对象树序列（原 1064）
        seq = [
            "SELECT EVENT_SCHEMA, EVENT_NAME, EVENT_TYPE, DEFINER, STATUS "
            "FROM information_schema.EVENTS "
            "WHERE BINARY EVENT_SCHEMA='default'",
            "SELECT TRIGGER_SCHEMA, TRIGGER_NAME, EVENT_OBJECT_TABLE "
            "FROM information_schema.TRIGGERS "
            "WHERE BINARY event_object_schema='default'",
            "SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE "
            "FROM information_schema.ROUTINES "
            "WHERE BINARY ROUTINE_SCHEMA='default'",
            "SELECT COUNT(*) FROM information_schema.EVENTS",
            "SELECT COUNT(*) FROM information_schema.TRIGGERS",
            "SELECT COUNT(*) FROM information_schema.ROUTINES",
        ]
        for sql in seq:
            cur.execute(sql)
            cols = [d[0] for d in cur.description or []]
            got = cur.fetchall()
            assert cols, sql
            if sql.startswith('SELECT COUNT(*)'):
                assert [tuple(r) for r in got] == [(0,)], (sql, got)
            else:
                assert list(got) == [], (sql, got)
    print("\n    pymysql 真实 server：BINARY 大小写 + EVENTS/TRIGGERS/"
          "ROUTINES 备份序列全部通过（无 1064/1146/1054）")
