# -*- coding: utf-8 -*-
"""task-56 验收：Navicat 10 真实点击发现的 3 个 SQL 缺口

复现（2026-10-27，pymysql 连本地 8898，修复前）：

    SELECT 1 FROM information_schema.PARTITIONS
      WHERE NOT ISNULL(PARTITION_NAME)
      → (1064, 'SQL 解析错误: NOT 后面必须是一段括号条件: NOT ISNULL(...)')
    SHOW PROCEDURE STATUS WHERE Db='default'
      → (1064, "... error ... near 'SHOW PROCEDURE STATUS WHERE Db=default'")
    SHOW COLUMNS FROM `default`.`q`
      → (1146, "Table 'default.default' doesn't exist")
      （server 的 SHOW COLUMNS 正则把库名 default 当成表名）
    SHOW KEYS FROM `q` FROM `default`
    SHOW CREATE TABLE `default`.`q`
    SELECT * FROM `default`.`q`
      → 1064 / 1146（限定名未归一）

本文件锁定修复后的语义：

1. 通用 `NOT <predicate>`：NOT ISNULL(x) / NOT (expr) / NOT col IS NULL /
   NOT col = v / NOT expr LIKE，**完整三值逻辑**（NOT UNKNOWN = UNKNOWN，
   NULL 行不会被 NOT 放行）；可嵌套 AND/OR/NOT。
2. `SHOW PROCEDURE STATUS` / `SHOW FUNCTION STATUS [LIKE 'x'] [WHERE Db=...]`
   返回 MySQL 兼容列（Name/Db/Type/Definer/...），过滤真实生效。
3. 限定名：`` `db`.`tbl` ``、`db.tbl`、`SHOW ... FROM tbl FROM db`、
   `SHOW CREATE TABLE db.tbl`、SHOW COLUMNS/INDEX/KEYS/FULL COLUMNS/
   PARTITIONS、DML 限定名；未知库显式 1049。
4. `information_schema.PARTITIONS` 真实可用：MySQL 列清单；非分区表一行
   且 PARTITION_NAME=NULL；分区表按真实分区出行；WHERE（含 NOT ISNULL）
   真的过滤。
5. 网络协议路径（pymysql）同样成立（server 的 SHOW COLUMNS 限定名修复）。
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
    pymysql = pytest.importorskip('pymysql')
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
    db = Database(str(tmp_path / 'data_srv'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    srv.daemon_threads = True
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


def err(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') == 'error', (sql, r)
    return r


def rows(ex, sql):
    return [tuple(r) for r in ok(ex, sql)['rows']]


def ids(ex, sql):
    return [r[0] for r in rows(ex, sql)]


def _seed_null_table(ex):
    ok(ex, 'CREATE TABLE n (id INT PRIMARY KEY, a INT, s VARCHAR(10))')
    ok(ex, "INSERT INTO n VALUES (1, 1, 'x'), (2, 2, 'y'), "
           "(3, NULL, NULL), (4, 5, 'x')")


# ==================== 一、NOT 通用谓词 + 三值逻辑 ====================

def test_not_isnull_and_general_not_predicates(inproc):
    _db, ex = inproc
    _seed_null_table(ex)
    assert ids(ex, 'SELECT id FROM n WHERE NOT ISNULL(a) ORDER BY id') == \
        [1, 2, 4]
    assert ids(ex, 'SELECT id FROM n WHERE NOT a IS NULL ORDER BY id') == \
        [1, 2, 4]
    assert ids(ex, 'SELECT id FROM n WHERE NOT (a IS NULL) ORDER BY id') == \
        [1, 2, 4]
    assert ids(ex, 'SELECT id FROM n WHERE NOT a = 2 ORDER BY id') == [1, 4]
    assert ids(ex, 'SELECT id FROM n WHERE NOT (a > 2) ORDER BY id') == [1, 2]
    # LIKE 的 NULL 行（id=3）是 UNKNOWN → NOT 后也不能放行
    assert ids(ex, "SELECT id FROM n WHERE NOT s LIKE 'x%' ORDER BY id") == [2]
    assert ids(ex, 'SELECT id FROM n WHERE NOT (NOT a = 2) ORDER BY id') == [2]
    assert ids(ex, 'SELECT id FROM n WHERE NOT (a = 2 OR a = 5) '
                   'ORDER BY id') == [1]
    # 混合 AND/OR/NOT 嵌套（NULL 行按 UNKNOWN 处理）
    assert ids(ex, 'SELECT id FROM n WHERE NOT ISNULL(a) AND a > 1 '
                   'ORDER BY id') == [2, 4]
    assert ids(ex, 'SELECT id FROM n WHERE a IS NULL OR NOT ISNULL(a) '
                   'ORDER BY id') == [1, 2, 3, 4]
    print(f"\n    NOT 通用谓词：ISNULL/IS NULL/=/>/LIKE/嵌套 AND-OR 全部正确，"
          f"NULL 行 3 不会被 NOT 放行")


def test_not_three_valued_logic_with_null_column(inproc):
    """整列 NULL 时 `NOT (col = 2)` 必须是 UNKNOWN → 0 行（不是全表）"""
    _db, ex = inproc
    ok(ex, 'CREATE TABLE z (id INT PRIMARY KEY, a INT, s VARCHAR(5))')
    ok(ex, 'INSERT INTO z VALUES (1, NULL, NULL), (2, NULL, NULL)')
    assert ids(ex, 'SELECT id FROM z WHERE NOT (a = 2)') == []
    assert ids(ex, 'SELECT id FROM z WHERE NOT (a > 2)') == []
    assert ids(ex, "SELECT id FROM z WHERE NOT (s LIKE 'x%')") == []
    assert ids(ex, 'SELECT id FROM z WHERE NOT a IS NULL') == []
    # NOT ISNULL 是确定谓词：NULL 列 NOT ISNULL=false
    assert ids(ex, 'SELECT id FROM z WHERE NOT ISNULL(a)') == []
    print("\n    三值逻辑：NULL 列的 NOT 比较 → UNKNOWN → 0 行；"
          "NOT ISNULL(NULL)=false")


def test_not_predicate_in_subquery_and_metadata_paths(inproc):
    _db, ex = inproc
    _seed_null_table(ex)
    # 子查询里同样走统一求值路径
    assert ids(ex, 'SELECT id FROM n WHERE id IN '
                   '(SELECT id FROM n WHERE NOT ISNULL(a)) ORDER BY id') == \
        [1, 2, 4]


# ==================== 二、SHOW PROCEDURE / FUNCTION STATUS ====================

def _seed_routines(ex):
    ok(ex, 'CREATE PROCEDURE p_alpha(IN x INT) BEGIN SELECT x; END')
    ok(ex, 'CREATE PROCEDURE p_beta() BEGIN SELECT 1; END')
    ok(ex, 'CREATE FUNCTION f_alpha(x INT) RETURNS INT RETURN x + 1')


def test_show_procedure_status_filters(inproc):
    _db, ex = inproc
    _seed_routines(ex)
    r = ok(ex, "SHOW PROCEDURE STATUS WHERE Db='default'")
    cols = r['columns']
    for c in ('Db', 'Name', 'Type', 'Definer', 'Modified', 'Created',
              'Security_type', 'Comment'):
        assert c in cols, cols
    typed = [tuple(x) for x in r['rows']]
    names = [x[cols.index('Name')] for x in typed]
    assert names == ['p_alpha', 'p_beta'], typed
    assert all(x[cols.index('Type')] == 'PROCEDURE' for x in typed)
    assert all(x[cols.index('Db')] == 'default' for x in typed)
    # LIKE 过滤
    r = ok(ex, "SHOW PROCEDURE STATUS LIKE 'p_a%'")
    assert [x[0] for x in r['rows']] == ['p_alpha'], r['rows']
    # WHERE Db 过滤：未知库 → 空集（不是把别的库结果返回）
    r = ok(ex, "SHOW PROCEDURE STATUS WHERE Db='nope'")
    assert r['rows'] == []
    # WHERE Name 过滤
    r = ok(ex, "SHOW PROCEDURE STATUS WHERE Db='default' AND Name='p_beta'")
    assert [x[0] for x in r['rows']] == ['p_beta'], r['rows']
    print(f"\n    SHOW PROCEDURE STATUS：列 {cols[:4]}...，"
          f"2 个过程按 Db/LIKE/Name 过滤真实生效")


def test_show_function_status_filters(inproc):
    _db, ex = inproc
    _seed_routines(ex)
    r = ok(ex, "SHOW FUNCTION STATUS WHERE Db='default'")
    cols = r['columns']
    names = [x[cols.index('Name')] for x in r['rows']]
    assert names == ['f_alpha'], names
    assert r['rows'][0][cols.index('Type')] == 'FUNCTION'
    r = ok(ex, "SHOW FUNCTION STATUS LIKE 'f_%'")
    assert [x[0] for x in r['rows']] == ['f_alpha']


# ==================== 三、限定名解析 ====================

def _seed_q(ex):
    ok(ex, 'CREATE TABLE q (id INT PRIMARY KEY, v INT)')
    ok(ex, 'INSERT INTO q VALUES (1, 10), (2, 20)')


def test_qualified_table_names_select_and_dml(inproc):
    _db, ex = inproc
    _seed_q(ex)
    assert rows(ex, 'SELECT COUNT(*) FROM `default`.`q`')[0][0] == 2
    assert ids(ex, 'SELECT id FROM `default`.`q` WHERE id > 0') == [1, 2]
    assert ids(ex, 'SELECT id FROM default.q ORDER BY id') == [1, 2]
    ok(ex, 'INSERT INTO `default`.`q` VALUES (3, 30)')
    ok(ex, 'UPDATE `default`.`q` SET v = 11 WHERE id = 1')
    ok(ex, 'DELETE FROM `default`.`q` WHERE id = 3')
    assert rows(ex, 'SELECT v FROM default.q WHERE id=1')[0][0] == 11
    # JOIN 限定名
    ok(ex, 'CREATE TABLE q2 (id INT PRIMARY KEY, w INT)')
    ok(ex, 'INSERT INTO q2 VALUES (1, 100), (2, 200)')
    got = rows(ex, 'SELECT q.id, q2.w FROM `default`.`q` q '
                   'JOIN `default`.`q2` q2 ON q.id = q2.id ORDER BY q.id')
    assert got == [(1, 100), (2, 200)], got
    print(f"\n    限定名：SELECT/INSERT/UPDATE/DELETE/JOIN 的 "
          f"`default`.`q` / default.q 全部正确")


def test_qualified_show_statements(inproc):
    _db, ex = inproc
    _seed_q(ex)
    # SHOW CREATE TABLE
    for sql in ('SHOW CREATE TABLE `default`.`q`',
                'SHOW CREATE TABLE default.q',
                'SHOW CREATE TABLE `default` . `q`'):
        r = ok(ex, sql)
        assert 'CREATE TABLE q' in r['rows'][0][1], (sql, r['rows'])
    # SHOW KEYS / INDEX
    for sql in ('SHOW KEYS FROM `q` FROM `default`',
                'SHOW KEYS FROM `default`.`q`',
                'SHOW INDEX FROM `q` FROM `default`',
                'SHOW INDEX FROM `default`.`q`'):
        r = ok(ex, sql)
        assert any(x[2] == 'PRIMARY' for x in r['rows']), (sql, r['rows'])
    # SHOW COLUMNS 变体
    for sql in ('SHOW COLUMNS FROM `default`.`q`',
                'SHOW COLUMNS FROM `q` FROM `default`',
                'DESC `default`.`q`',
                'DESCRIBE default.q'):
        r = ok(ex, sql)
        assert [x[0] for x in r['rows']] == ['id', 'v'], (sql, r['rows'])
    # SHOW FULL COLUMNS 变体
    for sql in ('SHOW FULL COLUMNS FROM `default`.`q`',
                'SHOW FULL COLUMNS FROM `q` FROM `default`'):
        r = ok(ex, sql)
        assert 'Collation' in r['columns']
        assert [x[0] for x in r['rows']] == ['id', 'v'], (sql, r['rows'])
    # SHOW PARTITIONS
    r = ok(ex, 'SHOW PARTITIONS FROM `default`.`q`')
    assert 'Partition' in r['columns']
    # 未知库显式 1049（不静默查默认库）
    r = err(ex, 'SHOW CREATE TABLE `no_such_db`.`q`')
    assert r['code'] == 1049, r
    p = err(ex, 'SHOW KEYS FROM `q` FROM `no_such_db`')
    assert p['code'] == 1049, p
    print("\n    限定名 SHOW：CREATE/KEYS/INDEX/COLUMNS/FULL COLUMNS/"
          "PARTITIONS 全部可用；未知库显式 1049")


# ==================== 四、information_schema.PARTITIONS ====================

PARTITION_COLUMNS = (
    'TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'PARTITION_NAME',
    'PARTITION_ORDINAL_POSITION', 'PARTITION_METHOD', 'PARTITION_EXPRESSION',
    'PARTITION_DESCRIPTION')


def test_information_schema_partitions(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE plain_t (id INT PRIMARY KEY)')
    ok(ex, 'CREATE TABLE part_t (id INT PRIMARY KEY, v INT) '
           'PARTITION BY HASH(id) PARTITIONS 4')
    # 列清单与 MySQL 兼容（Navicat 用的 PARTITION_NAME/PARTITION_METHOD 等）
    r = ok(ex, 'SELECT * FROM information_schema.PARTITIONS')
    for c in PARTITION_COLUMNS:
        assert c in r['columns'], r['columns']
    # 非分区表：一行且 PARTITION_NAME=NULL（MySQL 语义）
    r = ok(ex, "SELECT TABLE_NAME, PARTITION_NAME FROM "
               "information_schema.PARTITIONS WHERE TABLE_NAME='plain_t'")
    assert [tuple(x) for x in r['rows']] == [('plain_t', None)], r['rows']
    # Navicat 的真实过滤：NOT ISNULL(PARTITION_NAME) → 非分区表 0 行
    r = ok(ex, "SELECT * FROM information_schema.PARTITIONS "
               "WHERE TABLE_SCHEMA='default' AND TABLE_NAME='plain_t' "
               "AND NOT ISNULL(PARTITION_NAME)")
    assert r['rows'] == [], r['rows']
    # 分区表：真实 4 个 HASH 分区（分区名/方法/表达式来自真实定义）
    r = ok(ex, "SELECT TABLE_NAME, PARTITION_NAME, PARTITION_METHOD, "
               "PARTITION_EXPRESSION FROM information_schema.PARTITIONS "
               "WHERE TABLE_SCHEMA='default' AND TABLE_NAME='part_t' "
               "AND NOT ISNULL(PARTITION_NAME) "
               "ORDER BY PARTITION_ORDINAL_POSITION")
    got = [tuple(x) for x in r['rows']]
    assert [g[1] for g in got] == ['p0', 'p1', 'p2', 'p3'], got
    assert all(g[2] == 'HASH' and g[3] == 'id' for g in got), got
    print("\n    information_schema.PARTITIONS：非分区表 1 行(NULL)，"
          "HASH 4 分区真实 p0..p3；Navicat 的 NOT ISNULL 过滤生效")


# ==================== 五、网络协议路径（真实 server） ====================

def test_pymysql_navicat_sequence(served):
    conn = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE nav (id INT PRIMARY KEY, name VARCHAR(20))')
        cur.execute("INSERT INTO nav VALUES (1, 'a'), (2, 'b'), (3, NULL)")
        # ① NOT ISNULL
        cur.execute("SELECT id FROM nav WHERE NOT ISNULL(name) ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == [1, 2]
        cur.execute("SELECT id FROM nav WHERE NOT name LIKE 'a%' ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == [2]
        # ② SHOW PROCEDURE / FUNCTION STATUS
        cur.execute('CREATE PROCEDURE px() BEGIN SELECT 1; END')
        cur.execute("SHOW PROCEDURE STATUS WHERE Db='default'")
        cols = [d[0] for d in cur.description]
        got = cur.fetchall()
        assert 'Name' in cols and 'Db' in cols and 'Type' in cols, cols
        assert any(r[cols.index('Name')] == 'px' and
                   r[cols.index('Type')] == 'PROCEDURE' for r in got), got
        cur.execute("SHOW PROCEDURE STATUS LIKE 'zzz%'")
        assert list(cur.fetchall()) == []
        cur.execute("SHOW FUNCTION STATUS WHERE Db='default'")
        assert 'Name' in [d[0] for d in cur.description]
        # ③ 限定名（server 的 SHOW COLUMNS 曾把 default 当表名）
        cur.execute('SHOW CREATE TABLE `default`.`nav`')
        assert 'CREATE TABLE nav' in cur.fetchone()[1]
        cur.execute('SHOW KEYS FROM `nav` FROM `default`')
        assert any(r[2] == 'PRIMARY' for r in cur.fetchall())
        cur.execute('SHOW COLUMNS FROM `default`.`nav`')
        assert [r[0] for r in cur.fetchall()] == ['id', 'name']
        cur.execute('SHOW COLUMNS FROM `nav` FROM `default`')
        assert [r[0] for r in cur.fetchall()] == ['id', 'name']
        cur.execute('SELECT * FROM `default`.`nav` ORDER BY id')
        assert [tuple(r) for r in cur.fetchall()] == [
            (1, 'a'), (2, 'b'), (3, None)]
        cur.execute('SELECT COUNT(*) FROM default.nav')
        assert cur.fetchone()[0] == 3
        # ④ information_schema.PARTITIONS（含 NOT ISNULL）
        cur.execute(
            "SELECT TABLE_NAME, PARTITION_NAME FROM "
            "information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA='default' AND TABLE_NAME='nav' "
            "AND NOT ISNULL(PARTITION_NAME)")
        assert list(cur.fetchall()) == []
    print("\n    pymysql 真实 server：NOT/SHOW STATUS/限定名/"
          "PARTITIONS 序列全部通过（1146 default.default 不再出现）")
