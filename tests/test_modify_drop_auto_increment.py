# -*- coding: utf-8 -*-
"""task-49 验收：`ALTER TABLE ... MODIFY` 移除 AUTO_INCREMENT 不再静默保留

原缺口（studio task-48 实测）：`ALTER TABLE t MODIFY COLUMN id INT NOT NULL`
（原列带 AUTO_INCREMENT）后 `SHOW CREATE TABLE` 仍含 AUTO_INCREMENT ——
MODIFY 给出的"完整新列定义"里没有 AUTO_INCREMENT，引擎却用旧值 OR 回来，
属于静默语义保留。

本文件锁定修复后的 SQL 语义（与 MySQL 一致：MODIFY 就是完整列定义）：

1. 省略 AUTO_INCREMENT = **真正移除**：列元数据、`SHOW CREATE TABLE`、
   `SHOW COLUMNS.Extra`、`SHOW TABLE STATUS.Auto_increment` 全部同步；
   之后省略该列插入 → 1048（NOT NULL），显式值插入仍可。
2. 无自增列的表 `SHOW TABLE STATUS.Auto_increment` 按 MySQL 语义为 NULL
   （不再用"内部下一个主键号"冒充）。
3. 重新加回 `AUTO_INCREMENT`（MODIFY 内带关键字）真实生效，且：
   * 计数从**原分配状态/现存最大值+1**继续（不复用移除前的旧号）；
   * 与 task-45/46 的区间预留持久化协同（`.autoinc` 保留计数）；
   * `ALTER TABLE ... AUTO_INCREMENT = n` 正常设置下一号。
4. 再移除/加回与 TRUNCATE、重启、事务回滚的交互一致；非法场景显式
   1075/1063（非键/非整数/多自增列/无自增列时 ALTER AUTO_INCREMENT）。
"""
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8927
DDL = ('CREATE TABLE t (id INT NOT NULL AUTO_INCREMENT, v INT, '
       'PRIMARY KEY (id))')


# ==================== fixtures ====================

@pytest.fixture(autouse=True)
def _reset_auto_block():
    from lightdb.db.table import Table
    old = Table.auto_increment_block
    try:
        yield
    finally:
        Table.auto_increment_block = old


@pytest.fixture()
def inproc(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    s = {}
    try:
        yield db, ex, s
    finally:
        try:
            db.shutdown()
        except Exception:
            pass


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
    time.sleep(0.4)
    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
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


def ok(ex, sql, s=None):
    r = ex.execute_sql(sql, s if s is not None else {})
    assert r.get('type') != 'error', (sql, r)
    return r


def err(ex, sql, s=None):
    r = ex.execute_sql(sql, s if s is not None else {})
    assert r.get('type') == 'error', (sql, r)
    return r


def rows(ex, sql, s=None):
    return ok(ex, sql, s)['rows']


def _create(ex, s=None):
    ok(ex, DDL, s)
    for i in range(3):
        ok(ex, f'INSERT INTO t (v) VALUES ({i})', s)      # id 1..3


def _meta(ex, s=None):
    """(SHOW CREATE 文本, Extra, SHOW TABLE STATUS 的 Auto_increment)"""
    ddl = rows(ex, 'SHOW CREATE TABLE t', s)[0][1]
    cols = rows(ex, 'SHOW COLUMNS FROM t', s)
    idx = [c[0] for c in cols].index('id')
    extra = [tuple(r) for r in cols][idx][5]
    st = ok(ex, 'SHOW TABLE STATUS', s)
    ci = st['columns'].index('Name')
    ai = st['columns'].index('Auto_increment')
    auto = next(r[ai] for r in st['rows'] if r[ci] == 't')
    return ddl, extra, auto


# ==================== 1. 移除自增：元数据/插入语义 ====================

def test_modify_drop_auto_increment_is_real(inproc):
    _db, ex, s = inproc
    _create(ex, s)
    ddl, extra, auto = _meta(ex, s)
    assert 'id INT NOT NULL AUTO_INCREMENT' in ddl, ddl
    assert extra == 'auto_increment', extra
    assert auto == 4, auto

    r = ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL', s)
    assert 'AUTO_INCREMENT 已移除' in r['message'], r

    ddl, extra, auto = _meta(ex, s)
    assert 'AUTO_INCREMENT' not in ddl, ddl
    assert 'id INT NOT NULL PRIMARY KEY' in ddl, ddl
    assert extra == '', extra                       # Extra 不再报自增
    assert auto is None, auto                       # STATUS = NULL（MySQL 语义）

    # 行数据不变、索引/内容一致（MODIFY 只改列定义）
    assert rows(ex, 'SELECT id, v FROM t ORDER BY id', s) == [
        (1, 0), (2, 1), (3, 2)]

    # 省略 id → NOT NULL 报错（1048），不再自动分配
    r = err(ex, 'INSERT INTO t (v) VALUES (100)', s)
    assert r['code'] == 1048, r
    # 显式 id 仍可写入
    ok(ex, 'INSERT INTO t (id, v) VALUES (10, 10)', s)
    assert rows(ex, "SELECT id FROM t WHERE v=10", s) == [(10,)]
    print(f"\n    去自增：SHOW CREATE/Extra/STATUS 同步；"
          f"省略插入→1048；显式 id=10 可插入")


def test_modify_type_change_without_keyword_also_drops_auto(inproc):
    """MODIFY 换类型但没写 AUTO_INCREMENT：同样按"完整新定义"移除自增"""
    _db, ex, s = inproc
    _create(ex, s)
    ok(ex, 'ALTER TABLE t MODIFY COLUMN id BIGINT NOT NULL', s)
    ddl, extra, auto = _meta(ex, s)
    assert 'AUTO_INCREMENT' not in ddl.upper(), ddl
    assert extra == '', extra
    assert auto is None, auto
    assert err(ex, 'INSERT INTO t (v) VALUES (1)', s)['code'] == 1048


def test_show_status_null_for_plain_int_pk_table(inproc):
    """普通 INT 主键（从无自增）的 Auto_increment 也按 MySQL 语义报 NULL"""
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE p (id INT PRIMARY KEY, v INT)', s)
    ok(ex, 'INSERT INTO p VALUES (7, 7)', s)
    st = ok(ex, 'SHOW TABLE STATUS', s)
    ai = st['columns'].index('Auto_increment')
    ci = st['columns'].index('Name')
    assert next(r[ai] for r in st['rows'] if r[ci] == 'p') is None


# ==================== 2. 重启一致 ====================

def test_drop_auto_increment_persists_across_restart(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_restart')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        _create(ex)
        ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL')
        ok(ex, 'INSERT INTO t (id, v) VALUES (10, 10)')
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        ddl, extra, auto = _meta(ex2)
        assert 'AUTO_INCREMENT' not in ddl, ddl
        assert extra == '' and auto is None, (extra, auto)
        assert err(ex2, 'INSERT INTO t (v) VALUES (200)')['code'] == 1048
        # 重启后仍可重新加回自增，并从现存最大值+1 继续
        ok(ex2, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT')
        ddl, extra, auto = _meta(ex2)
        assert 'AUTO_INCREMENT' in ddl, ddl
        assert extra == 'auto_increment', extra
        assert auto == 11, auto                     # 现存最大 10 → 11
        ok(ex2, 'INSERT INTO t (v) VALUES (300)')
        assert rows(ex2, 'SELECT id FROM t ORDER BY id')[-1] == (11,)
        print(f"\n    去自增重启：元数据保持；加回后从现存最大+1=11 继续，"
              f"新插入 id=11")
    finally:
        db2.shutdown()


# ==================== 3. 重新加回：计数不复用/不撞号 ====================

def test_readd_continues_counter_without_reuse(inproc):
    db, ex, s = inproc
    _create(ex, s)                                  # ids 1..3，旧下一号 4
    # 去掉自增期间显式插入了 10（自增计数不会推进）
    ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL', s)
    ok(ex, 'INSERT INTO t (id, v) VALUES (10, 10)', s)
    # 加回：不能撞 10，也不能复用 1..3 的旧号
    r = ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT', s)
    assert 'AUTO_INCREMENT 已加回' in r['message'], r
    _ddl, _extra, auto = _meta(ex, s)
    assert auto == 11, auto                          # max(旧 4, 现存最大+1=11)
    ok(ex, 'INSERT INTO t (v) VALUES (200)', s)      # 11
    assert rows(ex, "SELECT id FROM t WHERE v=200", s) == [(11,)]

    # ALTER AUTO_INCREMENT = n 仍可设置；再插入从 50 起
    ok(ex, 'ALTER TABLE t AUTO_INCREMENT = 50', s)
    assert _meta(ex, s)[2] == 50
    ok(ex, 'INSERT INTO t (v) VALUES (300)', s)
    assert rows(ex, "SELECT id FROM t WHERE v=300", s) == [(50,)]
    ids = [r[0] for r in rows(ex, 'SELECT id FROM t ORDER BY id', s)]
    assert ids == [1, 2, 3, 10, 11, 50], ids
    print(f"\n    去自增期间显式 id=10 → 加回后下一号=11（不撞号/不复用旧号）；"
          f"ALTER=50 后插入 id=50")


def test_readd_after_restart_and_rollback_no_reuse(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.db.table import Table
    Table.auto_increment_block = 2
    data_dir = str(tmp_path / 'data_re')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        _create(ex)                                  # 1..3
        ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL')
    finally:
        db.shutdown()
    # 重启后加回；再回滚一次插入，号不回退
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        ok(ex2, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT')
        assert _meta(ex2)[2] == 4                    # 从旧计数 4 继续
        s = {}
        ok(ex2, 'BEGIN', s)
        ok(ex2, 'INSERT INTO t (v) VALUES (-1)', s)  # 分配 4（回滚）
        ok(ex2, 'ROLLBACK', s)
        ok(ex2, 'INSERT INTO t (v) VALUES (400)')
        assert rows(ex2, "SELECT id FROM t WHERE v=400") == [(5,)]    # 4 不被复用
    finally:
        db2.shutdown()
        Table.auto_increment_block = None


def test_drop_then_truncate_then_readd_starts_at_one(inproc):
    """TRUNCATE 重置计数（含"曾被移除自增"留下的计数）→ 加回后从 1 开始"""
    _db, ex, s = inproc
    _create(ex, s)
    ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL', s)
    ok(ex, 'TRUNCATE TABLE t', s)
    ok(ex, 'ALTER TABLE t MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT', s)
    assert _meta(ex, s)[2] == 1, _meta(ex, s)[2]
    ok(ex, 'INSERT INTO t (v) VALUES (1)', s)
    assert rows(ex, 'SELECT id FROM t', s) == [(1,)]


# ==================== 4. 非法场景显式拒绝 ====================

def test_readd_auto_increment_invalid_explicit(inproc):
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE u (id INT PRIMARY KEY, v INT)', s)
    # 非键列加自增 → 1075
    r = err(ex, 'ALTER TABLE u MODIFY COLUMN v INT AUTO_INCREMENT', s)
    assert r['code'] == 1075, r
    # 非整数列加自增 → 1063
    ok(ex, 'ALTER TABLE u MODIFY COLUMN v VARCHAR(10)', s)
    r = err(ex, 'ALTER TABLE u MODIFY COLUMN v VARCHAR(10) AUTO_INCREMENT', s)
    assert r['code'] == 1063, r
    # 无自增列时 ALTER AUTO_INCREMENT=n → 显式 1075
    r = err(ex, 'ALTER TABLE u AUTO_INCREMENT = 100', s)
    assert r['code'] == 1075, r
    # 两列自增：第二列加回 → 1075
    _create_multi = 'CREATE TABLE m (id INT PRIMARY KEY, code INT UNIQUE, v INT)'
    ok(ex, _create_multi, s)
    ok(ex, 'ALTER TABLE m MODIFY COLUMN code INT AUTO_INCREMENT', s)
    r = err(ex, 'ALTER TABLE m MODIFY COLUMN id INT AUTO_INCREMENT', s)
    assert r['code'] == 1075, r
    # 失败不改变结构
    ddl = rows(ex, 'SHOW CREATE TABLE u', s)[0][1]
    assert 'AUTO_INCREMENT' not in ddl, ddl


def test_drop_auto_on_unique_non_pk_column(inproc):
    """非主键的 UNIQUE 自增列同样可移除；移除后该列按新定义（可空）"""
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE w (id INT PRIMARY KEY, '
           'code INT AUTO_INCREMENT UNIQUE, v INT)', s)
    ok(ex, 'INSERT INTO w (id, v) VALUES (1, 1)', s)      # code=1
    st = ok(ex, 'SHOW TABLE STATUS', s)
    ai = st['columns'].index('Auto_increment')
    ci = st['columns'].index('Name')
    assert next(r[ai] for r in st['rows'] if r[ci] == 'w') == 2
    ok(ex, 'ALTER TABLE w MODIFY COLUMN code INT', s)
    ddl = rows(ex, 'SHOW CREATE TABLE w', s)[0][1]
    assert 'AUTO_INCREMENT' not in ddl, ddl
    # 非主键列按 MODIFY 的新定义变成可空（不写 NOT NULL 就不强制）
    cols = rows(ex, 'SHOW COLUMNS FROM w', s)
    ci_name = 0
    ci_null = 2
    code_row = [tuple(r) for r in cols
                if r[ci_name] == 'code'][0]
    assert code_row[ci_null] == 'YES', code_row
    # 加回：不撞已存在的显式 code=7，也不复用旧号
    ok(ex, 'INSERT INTO w (id, code, v) VALUES (3, 7, 3)', s)
    ok(ex, 'ALTER TABLE w MODIFY COLUMN code INT AUTO_INCREMENT', s)
    ok(ex, 'INSERT INTO w (id, v) VALUES (4, 4)', s)
    codes = [r[1] for r in rows(ex, 'SELECT id, code FROM w ORDER BY id', s)]
    assert codes == [1, 7, 8], codes
    print(f"\n    非主键 UNIQUE 自增列：移除后按新定义可空；加回后从 max+1=8 继续")


# ==================== 5. 协议路径 ====================

def test_protocol_modify_drop_and_readd(served):
    conn = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        for i in range(3):
            cur.execute('INSERT INTO t (v) VALUES (%s)', (i,))
        cur.execute('SHOW CREATE TABLE t')
        assert 'AUTO_INCREMENT' in cur.fetchone()[1]
        cur.execute('ALTER TABLE t MODIFY COLUMN id INT NOT NULL')
        cur.execute('SHOW CREATE TABLE t')
        assert 'AUTO_INCREMENT' not in cur.fetchone()[1]
        cur.execute('SHOW COLUMNS FROM t')
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        assert row[cols.index('Extra')] == '', row
        cur.execute('SHOW TABLE STATUS')
        tcols = [d[0] for d in cur.description]
        trow = next(r for r in cur.fetchall() if r[tcols.index('Name')] == 't')
        assert trow[tcols.index('Auto_increment')] is None, trow
        with pytest.raises(Exception) as ei:
            cur.execute('INSERT INTO t (v) VALUES (100)')
        msg = str(ei.value)
        assert '1048' in msg or 'cannot be null' in msg.lower(), msg
        cur.execute('INSERT INTO t (id, v) VALUES (10, 10)')
        cur.execute('ALTER TABLE t MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT')
        cur.execute('INSERT INTO t (v) VALUES (200)')
        cur.execute('SELECT id FROM t WHERE v=200')
        assert cur.fetchone()[0] == 11
        print("\n    协议路径：去自增→1048→显式插入→加回后 id=11")
