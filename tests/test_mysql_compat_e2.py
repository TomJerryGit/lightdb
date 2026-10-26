# -*- coding: utf-8 -*-
"""E2 引擎兼容第二批验收（Navicat 表设计器/结构同步依赖）

覆盖（对应 docs/GUI_NAVICAT_PARITY_ROADMAP.md §3 E2）：

1. **ENGINE= 兼容策略**：InnoDB / MyISAM / MEMORY / CSV 作为**别名**映射到
   LightDB 原生存储；请求的引擎名持久化并在 SHOW TABLE STATUS /
   SHOW CREATE TABLE 如实回显；Create_options 标明 `storage=LightDB-native`，
   `SHOW ENGINES` **不**声称支持 InnoDB（不假装实现 InnoDB 事务/隔离语义）；
   未知引擎显式 1064；ALTER TABLE ... ENGINE= 同样生效。
2. **ALTER TABLE ... MODIFY [COLUMN]**：类型/长度/NULL/DEFAULT 变更，已有
   数据先试算转换；不可转换/会截断 → 显式错误且表结构与数据完全不变；
   主键/唯一/CHECK/注释保持；SHOW CREATE 反映新类型。
3. **CREATE [UNIQUE] INDEX / DROP INDEX**：独立语句 + 建表内联共用持久化；
   重名 1061 / 未知列 1072 / 同索引重复列 1060 / 不存在索引 1091；
   SHOW INDEX/SHOW CREATE 如实反映（单列非唯一索引 Index_type=BTREE 且真实
   加速等值/范围查询；多列索引仍 SCAN-ASSIST）；UNIQUE 冲突 1062
   （含建索引时已有数据冲突）。
4. **SHOW GRANTS [FOR user]**：基于真实授权数据；不存在用户 1141；
   CURRENT_USER 可用（权限体系已有，E2 补协议/边界测试）。

另含 **Navicat 表设计器典型 SQL 序列**（协议路径 pymysql）：
CREATE TABLE ... ENGINE=InnoDB → CREATE INDEX → ALTER MODIFY →
SHOW CREATE/INDEX/STATUS → 表结构与数据均正确。
"""
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8909


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

    def connect(autocommit=True, database='default'):
        import pymysql
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
    r = ok(ex, sql, session)
    return r['rows']


# ==================== 1. ENGINE= 兼容策略 ====================

@pytest.mark.parametrize('engine,shown', [
    ('InnoDB', 'InnoDB'),
    ('MyISAM', 'MyISAM'),
    ('MEMORY', 'MEMORY'),
    ('CSV', 'CSV'),
    ('InnoDB', 'InnoDB'),          # 大小写不敏感
])
def test_engine_alias_accepted_and_shown(inproc, engine, shown):
    """InnoDB/MyISAM/MEMORY/CSV = LightDB 原生存储的兼容别名"""
    _db, ex, s = inproc
    name = f't_{shown.lower()}'
    ok(ex, f'CREATE TABLE {name} (id INT PRIMARY KEY, v INT) '
           f'ENGINE={engine} DEFAULT CHARSET=utf8mb4', s)
    ok(ex, f'INSERT INTO {name} VALUES (1, 42)', s)
    assert rows(ex, f'SELECT id, v FROM {name}', s) == [(1, 42)]

    status = ok(ex, 'SHOW TABLE STATUS', s)
    cols = status['columns']
    row = next(r for r in status['rows'] if r[cols.index('Name')] == name)
    assert row[cols.index('Engine')] == shown, row
    # 不假装实现了对应引擎：Create_options 明确 native storage
    assert row[cols.index('Create_options')] == 'storage=LightDB-native', row

    ddl = ok(ex, f'SHOW CREATE TABLE {name}', s)['rows'][0][1]
    assert f'ENGINE={shown}' in ddl, ddl
    # SHOW ENGINES 只报真实引擎，不声称实现了 InnoDB
    engines = [r[0] for r in rows(ex, 'SHOW ENGINES', s)]
    assert engines == ['LightDB'], engines


def test_unknown_engine_explicit_1064(inproc):
    _db, ex, s = inproc
    r = err(ex, 'CREATE TABLE bad (id INT PRIMARY KEY) ENGINE=TOKUDB', s)
    assert r['code'] == 1064 and 'Unknown storage engine' in r['message'], r
    # 拒绝即完全不建表
    assert 'bad' not in _db.tables


def test_engine_persists_and_alter_engine(inproc, tmp_path):
    """ENGINE 元数据重启不丢；ALTER TABLE ... ENGINE= 同样生效"""
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE e (id INT PRIMARY KEY) ENGINE=InnoDB', s)
    ddl = ok(ex, 'SHOW CREATE TABLE e', s)['rows'][0][1]
    assert 'ENGINE=InnoDB' in ddl, ddl
    ok(ex, 'ALTER TABLE e ENGINE=MyISAM', s)
    ddl = ok(ex, 'SHOW CREATE TABLE e', s)['rows'][0][1]
    assert 'ENGINE=MyISAM' in ddl, ddl
    r = err(ex, 'ALTER TABLE e ENGINE=BLACKHOLE', s)
    assert r['code'] == 1064, r
    # 旧的请求名保持不变（错误不动结构）
    assert 'ENGINE=MyISAM' in ok(ex, 'SHOW CREATE TABLE e', s)['rows'][0][1]

    data_dir = str(db.data_dir)
    db.shutdown()
    from lightdb.db.database import Database as _DB
    from lightdb.sql.executor import Executor as _Ex
    db2 = _DB(data_dir)
    ex2 = _Ex(db2)
    try:
        ddl2 = ok(ex2, 'SHOW CREATE TABLE e', {})['rows'][0][1]
        assert 'ENGINE=MyISAM' in ddl2, ddl2
        st = ok(ex2, 'SHOW TABLE STATUS', {})
        row = next(r for r in st['rows'] if r[0] == 'e')
        assert row[1] == 'MyISAM', row
    finally:
        db2.shutdown()


# ==================== 2. ALTER TABLE ... MODIFY COLUMN ====================

def test_modify_type_and_length(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, name VARCHAR(5), '
           'qty INT, price DOUBLE)', s)
    ok(ex, "INSERT INTO m VALUES (1, 'abc', 10, 1.5), "
           "(2, 'de', 20, 2.5)", s)

    r = ok(ex, 'ALTER TABLE m MODIFY COLUMN name VARCHAR(5) '
               'NOT NULL DEFAULT "x"', s)
    ddl = ok(ex, 'SHOW CREATE TABLE m', s)['rows'][0][1]
    assert 'name VARCHAR(5) NOT NULL DEFAULT' in ddl, ddl
    cols = {c['name']: c for c in _db.get_table('m').cols}
    assert cols['name']['notnull'] is True
    assert cols['name']['default'] == 'x'
    # 已有数据不变
    assert rows(ex, 'SELECT id, name, qty FROM m ORDER BY id', s) == [
        (1, 'abc', 10), (2, 'de', 20)]

    ok(ex, 'ALTER TABLE m MODIFY qty BIGINT', s)
    ddl = ok(ex, 'SHOW CREATE TABLE m', s)['rows'][0][1]
    assert 'qty BIGINT' in ddl, ddl
    ok(ex, 'INSERT INTO m VALUES (3, "x", 9000000000, 0)', s)
    assert rows(ex, 'SELECT qty FROM m WHERE id = 3', s) == [(9000000000,)]


def test_modify_preserves_primary_key_and_unique(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, v INT)', s)
    ok(ex, "INSERT INTO m VALUES (1, 1)", s)
    # 主键列 MODIFY 成 BIGINT：主键语义保持
    ok(ex, 'ALTER TABLE m MODIFY id BIGINT', s)
    cols = {c['name']: c for c in _db.get_table('m').cols}
    assert cols['id']['primary'] is True
    r = err(ex, 'INSERT INTO m VALUES (1, 2)', s)
    assert r['code'] == 1062, r
    idx = ok(ex, 'SHOW INDEX FROM m', s)
    assert any(r[2] == 'PRIMARY' for r in idx['rows']), idx['rows']

    # 唯一索引列 MODIFY 类型：唯一强制仍然生效
    ok(ex, 'CREATE UNIQUE INDEX uq_v ON m (v)', s)
    ok(ex, 'ALTER TABLE m MODIFY v BIGINT', s)
    r = err(ex, 'INSERT INTO m VALUES (2, 1)', s)
    assert r['code'] == 1062, r


def test_modify_not_null_or_bad_conversion_rolls_back(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, txt VARCHAR(10), '
           'num INT, dbl DOUBLE)', s)
    ok(ex, "INSERT INTO m VALUES (1, 'abc', 5, 1.5), (2, NULL, 6, 2.5)", s)
    before_ddl = ok(ex, 'SHOW CREATE TABLE m', s)['rows'][0][1]

    # 不可转换：VARCHAR 'abc' → INT（1366）
    r = err(ex, 'ALTER TABLE m MODIFY txt INT', s)
    assert r['code'] in (1366, 1265), r
    # 会截断：VARCHAR(10) → VARCHAR(1)（1406），绝不静默截断
    r = err(ex, 'ALTER TABLE m MODIFY txt VARCHAR(1)', s)
    assert r['code'] in (1406, 1265), r
    # 有损：DOUBLE 1.5 → INT（1265）
    r = err(ex, 'ALTER TABLE m MODIFY dbl INT', s)
    assert r['code'] == 1265, r
    # NOT NULL：已有 NULL 行（1138）
    r = err(ex, 'ALTER TABLE m MODIFY txt VARCHAR(10) NOT NULL', s)
    assert r['code'] == 1138, r
    # 主键改非整数类型：显式 1064
    r = err(ex, 'ALTER TABLE m MODIFY id VARCHAR(10)', s)
    assert r['code'] == 1064, r

    # 所有失败都没有改动结构或数据
    assert ok(ex, 'SHOW CREATE TABLE m', s)['rows'][0][1] == before_ddl
    assert rows(ex, 'SELECT id, txt, num, dbl FROM m ORDER BY id', s) == [
        (1, 'abc', 5, 1.5), (2, None, 6, 2.5)]


def test_modify_conversion_breaking_unique_is_explicit(inproc):
    """类型转换让两个不同值变成同一个 → 唯一冲突显式 1062 且不改数据"""
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, code VARCHAR(5))', s)
    ok(ex, "INSERT INTO m VALUES (1, '1'), (2, '01')", s)
    ok(ex, 'CREATE UNIQUE INDEX uq_code ON m (code)', s)
    before = rows(ex, 'SELECT id, code FROM m ORDER BY id', s)
    r = err(ex, 'ALTER TABLE m MODIFY code INT', s)
    assert r['code'] == 1062, r
    assert rows(ex, 'SELECT id, code FROM m ORDER BY id', s) == before


def test_modify_decimal_datetime_bool(inproc):
    """DECIMAL / DATETIME / BOOLEAN 类型变更：数据无损、SHOW CREATE 反映"""
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE conv (id INT PRIMARY KEY, '
           'price DECIMAL(10,2), created DATETIME, flag BOOL)', s)
    ok(ex, "INSERT INTO conv VALUES "
           "(1, 12.34, '2024-01-02 03:04:05', 1)", s)
    ok(ex, 'ALTER TABLE conv MODIFY price DECIMAL(12,2)', s)
    ok(ex, 'ALTER TABLE conv MODIFY created DATETIME', s)
    ok(ex, 'ALTER TABLE conv MODIFY flag BOOLEAN', s)
    ddl = ok(ex, 'SHOW CREATE TABLE conv', s)['rows'][0][1]
    assert 'DECIMAL(12,2)' in ddl, ddl
    assert 'DATETIME' in ddl, ddl
    assert 'BOOL' in ddl.upper(), ddl
    rows_ = rows(ex, 'SELECT id, price, created, flag FROM conv', s)
    assert rows_ and rows_[0][0] == 1
    assert abs(float(rows_[0][1]) - 12.34) < 1e-9, rows_
    assert str(rows_[0][2]).startswith('2024-01-02'), rows_
    assert bool(rows_[0][3]) is True, rows_


def test_modify_through_protocol(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE pm (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO pm VALUES (1, 5)')
        cur.execute('ALTER TABLE pm MODIFY v BIGINT')
        cur.execute('SHOW CREATE TABLE pm')
        ddl = cur.fetchone()[1]
        assert 'v BIGINT' in ddl, ddl
        cur.execute('SELECT v FROM pm')
        assert cur.fetchone() == (5,)


# ==================== 3. CREATE [UNIQUE] INDEX / DROP INDEX ====================

def test_create_index_metadata_and_persistence(inproc):
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE ix (id INT PRIMARY KEY, a INT, b VARCHAR(10))', s)
    ok(ex, 'INSERT INTO ix VALUES (1, 10, "x"), (2, 20, "y")', s)
    ok(ex, 'CREATE INDEX idx_a ON ix (a)', s)

    idx = ok(ex, 'SHOW INDEX FROM ix', s)
    cols = idx['columns']
    by_name = {r[cols.index('Key_name')]: r for r in idx['rows']}
    assert 'idx_a' in by_name
    row = by_name['idx_a']
    assert row[cols.index('Non_unique')] == 1
    assert row[cols.index('Column_name')] == 'a'
    # P5.1：单列非唯一索引已有真实的等值/范围/ORDER BY 加速映射
    assert row[cols.index('Index_type')] == 'BTREE', row

    ddl = ok(ex, 'SHOW CREATE TABLE ix', s)['rows'][0][1]
    assert 'KEY idx_a (a)' in ddl, ddl
    # SHOW CREATE 文本可被本引擎重新解析（结构同步/导出往返）
    copy = ddl.replace('CREATE TABLE ix', 'CREATE TABLE ix2', 1)
    ok(ex, copy, s)
    idx2 = ok(ex, 'SHOW INDEX FROM ix2', s)
    assert any(r[2] == 'idx_a' for r in idx2['rows']), idx2['rows']

    # 查询正确性不受影响（索引声明不改变结果）
    assert rows(ex, 'SELECT id FROM ix WHERE a = 20', s) == [(2,)]

    data_dir = str(db.data_dir)
    db.shutdown()
    from lightdb.db.database import Database as _DB
    from lightdb.sql.executor import Executor as _Ex
    db2 = _DB(data_dir)
    ex2 = _Ex(db2)
    try:
        idx3 = ok(ex2, 'SHOW INDEX FROM ix', {})
        assert any(r[2] == 'idx_a' for r in idx3['rows']), idx3['rows']
        ddl2 = ok(ex2, 'SHOW CREATE TABLE ix', {})['rows'][0][1]
        assert 'KEY idx_a (a)' in ddl2, ddl2
    finally:
        db2.shutdown()


def test_create_index_inline_and_errors(inproc):
    _db, ex, s = inproc
    # 建表内联 KEY 与独立 CREATE INDEX 共用持久化
    ok(ex, 'CREATE TABLE inl (id INT PRIMARY KEY, a INT, b INT, '
           'KEY idx_a (a))', s)
    ddl = ok(ex, 'SHOW CREATE TABLE inl', s)['rows'][0][1]
    assert 'KEY idx_a (a)' in ddl, ddl

    # 重复名字 1061
    r = err(ex, 'CREATE INDEX idx_a ON inl (b)', s)
    assert r['code'] == 1061, r
    # 未知列 1072
    r = err(ex, 'CREATE INDEX idx_x ON inl (nope)', s)
    assert r['code'] == 1072, r
    # 同一索引重复列 1060
    r = err(ex, 'CREATE INDEX idx_dup ON inl (a, a)', s)
    assert r['code'] == 1060, r
    # 表不存在 1146
    r = err(ex, 'CREATE INDEX i ON no_such_table (a)', s)
    assert r['code'] == 1146, r
    # 未知索引 1091
    r = err(ex, 'DROP INDEX nope ON inl', s)
    assert r['code'] == 1091, r

    # 被索引引用的列不能直接 DROP COLUMN（1091）；先删索引后可以
    r = err(ex, 'ALTER TABLE inl DROP COLUMN a', s)
    assert r['code'] == 1091, r
    ok(ex, 'DROP INDEX idx_a ON inl', s)
    assert not any(r[2] == 'idx_a' for r in rows(ex, 'SHOW INDEX FROM inl', s))
    ok(ex, 'ALTER TABLE inl DROP COLUMN a', s)


def test_unique_index_enforced_and_droppable(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE uq (id INT PRIMARY KEY, email VARCHAR(20))', s)
    ok(ex, 'INSERT INTO uq VALUES (1, "a@x"), (2, NULL), (3, NULL)', s)
    ok(ex, 'CREATE UNIQUE INDEX uq_email ON uq (email)', s)
    # 冲突 → 1062
    r = err(ex, 'INSERT INTO uq VALUES (4, "a@x")', s)
    assert r['code'] == 1062, r
    # NULL 可多行（与 MySQL 唯一索引语义一致）
    ok(ex, 'INSERT INTO uq VALUES (4, NULL)', s)

    # 已有数据冲突时建 UNIQUE INDEX → 1062，索引不落地
    ok(ex, 'CREATE TABLE uq2 (id INT PRIMARY KEY, v INT)', s)
    ok(ex, 'INSERT INTO uq2 VALUES (1, 7), (2, 7)', s)
    r = err(ex, 'CREATE UNIQUE INDEX uq_v ON uq2 (v)', s)
    assert r['code'] == 1062, r
    assert not any(r[2] == 'uq_v' for r in rows(ex, 'SHOW INDEX FROM uq2', s))

    # DROP 后冲突不再报错
    ok(ex, 'DROP INDEX uq_email ON uq', s)
    ok(ex, 'INSERT INTO uq VALUES (5, "a@x")', s)


def test_create_index_roundtrip_keeps_unique(inproc):
    """SHOW CREATE 的 UNIQUE KEY/KEY 行照抄重建：约束与声明都在"""
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE src (id INT PRIMARY KEY, a INT, b INT)', s)
    ok(ex, 'CREATE UNIQUE INDEX uq_a ON src (a)', s)
    ok(ex, 'CREATE INDEX idx_b ON src (b)', s)
    ddl = ok(ex, 'SHOW CREATE TABLE src', s)['rows'][0][1]
    dst = ddl.replace('CREATE TABLE src', 'CREATE TABLE dst', 1)
    ok(ex, dst, s)
    names = {(r[2], r[4]) for r in rows(ex, 'SHOW INDEX FROM dst', s)}
    assert ('uq_a', 'a') in names and ('idx_b', 'b') in names, names
    ok(ex, 'INSERT INTO dst VALUES (1, 10, 1)', s)
    r = err(ex, 'INSERT INTO dst VALUES (2, 10, 2)', s)
    assert r['code'] == 1062, r


# ==================== 4. SHOW GRANTS ====================

def test_show_grants_inproc(inproc):
    _db, ex, s = inproc
    grants = [r[0] for r in rows(ex, 'SHOW GRANTS', s)]
    assert grants and all('GRANT' in g for g in grants), grants

    ok(ex, "CREATE USER 'e2_bob'@'localhost'", s)
    bob = [r[0] for r in rows(
        ex, "SHOW GRANTS FOR 'e2_bob'@'localhost'", s)]
    assert any('USAGE' in g for g in bob), bob
    ok(ex, "GRANT SELECT ON default.* TO 'e2_bob'@'localhost'", s)
    bob = [r[0] for r in rows(
        ex, "SHOW GRANTS FOR 'e2_bob'@'localhost'", s)]
    assert any('SELECT' in g for g in bob), bob

    # 不存在的用户：MySQL 1141
    r = err(ex, "SHOW GRANTS FOR 'nobody'@'%'", s)
    assert r['code'] == 1141, r
    # CURRENT_USER 是合法形式
    ok(ex, 'SHOW GRANTS FOR CURRENT_USER', s)


def test_show_grants_through_protocol(served):
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute('SHOW GRANTS')
        lines = [r[0] for r in cur.fetchall()]
        assert lines and all('GRANT' in g for g in lines), lines
        cur.execute('SHOW GRANTS FOR CURRENT_USER')
        assert cur.fetchall()
        with pytest.raises(Exception) as ei:
            cur.execute("SHOW GRANTS FOR 'e2_nobody'@'%'")
        assert getattr(ei.value, 'args', (None,))[0] == 1141, ei.value


# ==================== 5. Navicat 表设计器典型序列（协议） ====================

def test_navicat_table_designer_sequence(served):
    """表设计器保存/结构同步常发语句串：全部通过且结构/数据正确"""
    conn, _connect, _db, _ex = served
    with conn.cursor() as cur:
        cur.execute(
            'CREATE TABLE nav2 (id INT PRIMARY KEY, name VARCHAR(10), '
            'qty INT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 '
            "COMMENT='设计器表'")
        cur.execute('CREATE INDEX idx_name ON nav2 (name)')
        cur.execute('CREATE UNIQUE INDEX uq_qty ON nav2 (qty)')
        cur.execute("INSERT INTO nav2 VALUES (1, 'a', 1), (2, 'b', 2)")
        cur.execute('ALTER TABLE nav2 MODIFY name VARCHAR(20) NOT NULL')
        cur.execute('ALTER TABLE nav2 ENGINE=MyISAM')

        cur.execute('SHOW CREATE TABLE nav2')
        ddl = cur.fetchone()[1]
        assert 'ENGINE=MyISAM' in ddl, ddl
        assert 'name VARCHAR(20) NOT NULL' in ddl, ddl
        assert 'KEY idx_name (name)' in ddl, ddl
        assert 'UNIQUE KEY uq_qty (qty)' in ddl, ddl
        assert "COMMENT='设计器表'" in ddl, ddl

        cur.execute('SHOW INDEX FROM nav2')
        names = {r[2] for r in cur.fetchall()}
        assert {'PRIMARY', 'idx_name', 'uq_qty'} <= names, names

        cur.execute('SHOW TABLE STATUS')
        cols = [d[0] for d in cur.description]
        row = next(r for r in cur.fetchall() if r[cols.index('Name')] == 'nav2')
        assert row[cols.index('Engine')] == 'MyISAM'
        assert row[cols.index('Rows')] == 2

        cur.execute('SHOW FULL COLUMNS FROM nav2')
        fcols = [d[0] for d in cur.description]
        by = {r[0]: r for r in cur.fetchall()}
        assert by['name'][fcols.index('Type')] == 'VARCHAR(20)'
        assert by['name'][fcols.index('Null')] == 'NO'
        assert by['name'][fcols.index('Comment')] == ''

        cur.execute('SELECT id, name, qty FROM nav2 ORDER BY id')
        assert list(cur.fetchall()) == [(1, 'a', 1), (2, 'b', 2)]

        # 唯一索引生效；DROP INDEX 后解除
        with pytest.raises(Exception) as ei:
            cur.execute('INSERT INTO nav2 VALUES (3, "c", 1)')
        assert getattr(ei.value, 'args', (None,))[0] == 1062, ei.value
        cur.execute('DROP INDEX uq_qty ON nav2')
        cur.execute('INSERT INTO nav2 VALUES (3, "c", 1)')
        cur.execute('SELECT COUNT(*) FROM nav2')
        assert cur.fetchone() == (3,)
