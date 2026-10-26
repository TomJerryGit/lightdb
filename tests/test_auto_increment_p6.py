# -*- coding: utf-8 -*-
"""P6 引擎缺口修复验收：AUTO_INCREMENT 真实自增

背景（AI 建表端到端实测）：`id INT NOT NULL AUTO_INCREMENT` 曾被静默接受
却不生效 —— `SHOW CREATE TABLE` 不显示、`INSERT (name,email)` 报 1048。
本文件锁定修复后的完整语义：

1. **建表/元数据**：`SHOW CREATE TABLE` 回显 `AUTO_INCREMENT`（下一号 >1 时
   同时回显表级 `AUTO_INCREMENT=n`）；`SHOW COLUMNS.Extra='auto_increment'`；
   `SHOW TABLE STATUS.Auto_increment` = 真实下一号。
2. **分配**：首次从 1 起；省略列 / 显式 NULL / 多行 INSERT 逐个分配；
   显式值推进计数器（小值不回退）。
3. **持久/回滚**：回滚与删除**不回收**已分配号；干净重启后计数保持
   （不因"最大行被删"而重用旧号）；TRUNCATE 重置回 1。
4. **`ALTER TABLE ... AUTO_INCREMENT = n`** 真实生效并回显（低于现存
   最大号+1 时抬高到该下限）。
5. **非法场景显式 errno**：非整数 1063 / 非键 1075 / 多列自增 1075 /
   ADD COLUMN AUTO_INCREMENT 1235；绝不静默降级。
6. **并发**：多线程（进程内）与多连接（真实 MySQLServer）并发插入不重号。
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8917

TASK_DDL = (
    "CREATE TABLE users_ai (id INT NOT NULL AUTO_INCREMENT, "
    "name VARCHAR(64) NOT NULL, email VARCHAR(128) UNIQUE, "
    "created DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (id))"
)


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
    import time
    time.sleep(0.4)

    conns = []

    def connect():
        import pymysql
        c = pymysql.connect(
            host='127.0.0.1', port=PORT, user='root', password='',
            database='default', charset='utf8mb4', autocommit=True,
            connect_timeout=10)
        conns.append(c)
        return c

    conn = connect()
    try:
        yield conn, connect, db
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


def _status_auto_increment(ex, table, session=None):
    r = ok(ex, 'SHOW TABLE STATUS', session)
    cols = r['columns']
    i_name = cols.index('Name')
    i_auto = cols.index('Auto_increment')
    for row in r['rows']:
        if row[i_name] == table:
            return row[i_auto]
    raise AssertionError(f'SHOW TABLE STATUS 里没有 {table}')


def _create_task_table(ex, session=None):
    ok(ex, TASK_DDL, session)
    return 'users_ai'


# ==================== 1. 建表 / 元数据回显 ====================

def test_create_show_create_and_status(inproc):
    """任务实测 SQL：建表成功、SHOW CREATE 保留 AUTO_INCREMENT、STATUS 真实"""
    _db, ex, s = inproc
    _create_task_table(ex, s)

    ddl = ok(ex, 'SHOW CREATE TABLE users_ai', s)['rows'][0][1]
    assert 'AUTO_INCREMENT' in ddl, ddl
    assert 'id INT NOT NULL AUTO_INCREMENT' in ddl, ddl
    # 刚建表时下一号=1：MySQL 不显示表级 AUTO_INCREMENT=1
    assert 'AUTO_INCREMENT=1' not in ddl, ddl

    cols = ok(ex, 'SHOW COLUMNS FROM users_ai', s)
    by = {r[0]: r for r in cols['rows']}
    # Extra 列（下标 5）如实报 auto_increment；Null=NO
    assert by['id'][5] == 'auto_increment', by['id']
    assert by['id'][2] == 'NO', by['id']

    assert _status_auto_increment(ex, 'users_ai', s) == 1

    # 实测原缺口：INSERT (name,email) 必须成功（不再 1048）
    r = ok(ex, "INSERT INTO users_ai (name, email) VALUES "
               "('alice', 'alice@x')", s)
    assert r.get('affected') == 1, r
    got = rows(ex, 'SELECT id, name FROM users_ai', s)
    assert got == [(1, 'alice')], got
    assert _status_auto_increment(ex, 'users_ai', s) == 2
    ddl2 = ok(ex, 'SHOW CREATE TABLE users_ai', s)['rows'][0][1]
    assert 'AUTO_INCREMENT=2' in ddl2, ddl2
    print(f"\n    实测 SQL: CREATE PASS，SHOW CREATE 含 AUTO_INCREMENT，"
          f"INSERT(name,email) → id={got[0][0]}，"
          f"下一号={_status_auto_increment(ex, 'users_ai', s)}")


def test_omitted_null_and_multirow_allocation(inproc):
    """省略 / 显式 NULL / 多行 INSERT 逐个分配；显式值推进计数"""
    _db, ex, s = inproc
    _create_task_table(ex, s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('a')", s)          # 1
    ok(ex, "INSERT INTO users_ai VALUES (NULL, 'b', NULL, NULL)", s)  # 2
    ok(ex, "INSERT INTO users_ai (name) VALUES ('c'), ('d')", s)      # 3,4
    ok(ex, "INSERT INTO users_ai (id, name) VALUES (10, 'e')", s)    # 显式 10
    ok(ex, "INSERT INTO users_ai (name) VALUES ('f')", s)            # 11
    ids = [r[0] for r in rows(ex, 'SELECT id FROM users_ai ORDER BY id', s)]
    assert ids == [1, 2, 3, 4, 10, 11], ids
    assert _status_auto_increment(ex, 'users_ai', s) == 12
    # 显式小值不回退计数器
    ok(ex, "INSERT INTO users_ai (id, name) VALUES (5, 'g')", s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('h')", s)
    assert rows(ex, "SELECT id FROM users_ai WHERE name='h'", s) == [(12,)]
    print(f"\n    分配序列 {ids}，显式值 10 推进后下一号=11；"
          f"显式小值 5 不回退（后续=12）")


def test_auto_increment_on_unique_non_pk(inproc):
    """AUTO_INCREMENT 声明在 UNIQUE 键（非主键）列上：真实分配且唯一"""
    _db, ex, s = inproc
    ok(ex, 'CREATE TABLE uq_ai (id INT PRIMARY KEY, '
           'code INT AUTO_INCREMENT UNIQUE, v INT)', s)
    ok(ex, 'INSERT INTO uq_ai (id, v) VALUES (1, 10)', s)   # code=1
    ok(ex, 'INSERT INTO uq_ai (id, v) VALUES (2, 20)', s)   # code=2
    ok(ex, 'INSERT INTO uq_ai (id, code, v) VALUES (3, 88, 30)', s)
    ok(ex, 'INSERT INTO uq_ai (id, v) VALUES (4, 40)', s)   # code=89
    got = rows(ex, 'SELECT id, code FROM uq_ai ORDER BY id', s)
    assert got == [(1, 1), (2, 2), (3, 88), (4, 89)], got


# ==================== 2. 回滚 / 重启 / TRUNCATE ====================

def test_rollback_and_delete_do_not_reuse(inproc):
    """回滚/删除**不回收**已分配号（MySQL 语义：只留空洞）"""
    _db, ex, s = inproc
    _create_task_table(ex, s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('a')", s)     # 1
    ok(ex, 'BEGIN', s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('rolled')", s)  # 2（回滚）
    ok(ex, 'ROLLBACK', s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('b')", s)     # 3，不复用 2
    got = [(r[0], r[1]) for r in rows(
        ex, 'SELECT id, name FROM users_ai ORDER BY id', s)]
    assert got == [(1, 'a'), (3, 'b')], got

    ok(ex, 'DELETE FROM users_ai WHERE id = 3', s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('c')", s)     # 4，不复用 3
    assert rows(ex, "SELECT id FROM users_ai WHERE name='c'", s) == [(4,)]
    print(f"\n    回滚/删除后序列: {got} → 下一次插入 id=4（不复用）")


def test_restart_preserves_counter_even_after_delete(tmp_path):
    """干净重启后计数保持：删掉最大行/回滚的号也不会在重启后重用"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_restart')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT NOT NULL AUTO_INCREMENT, v INT, '
               'PRIMARY KEY (id))')
        for i in range(5):
            ok(ex, f'INSERT INTO t (v) VALUES ({i})')           # 1..5
        ok(ex, 'INSERT INTO t (id, v) VALUES (100, 100)')      # 显式 100
        ok(ex, 'BEGIN')
        ok(ex, 'INSERT INTO t (v) VALUES (999)')               # 101 回滚
        ok(ex, 'ROLLBACK')
        ok(ex, 'DELETE FROM t WHERE id = 5')                   # 删掉普通行
        ok(ex, 'DELETE FROM t WHERE id = 100')                 # 删掉最大号
        assert _status_auto_increment(ex, 't') == 102
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        # 重启后下一号仍是 102（不会因为最大行已删而回到 5/100）
        assert _status_auto_increment(ex2, 't') == 102
        ok(ex2, 'INSERT INTO t (v) VALUES (1000)')
        assert rows(ex2, "SELECT id FROM t WHERE v=1000") == [(102,)]
        ddl = ok(ex2, 'SHOW CREATE TABLE t')['rows'][0][1]
        assert 'AUTO_INCREMENT' in ddl, ddl
        print(f"\n    重启前删掉最大号；重启后下一号=102（不回退），"
              f"插入得到 id=102")
    finally:
        db2.shutdown()


def test_truncate_resets_counter(inproc):
    """TRUNCATE 是 DDL：重置自增回 1（与 DELETE 不同）"""
    _db, ex, s = inproc
    _create_task_table(ex, s)
    for i in range(3):
        ok(ex, f"INSERT INTO users_ai (name) VALUES ('n{i}')", s)
    assert _status_auto_increment(ex, 'users_ai', s) == 4
    ok(ex, 'TRUNCATE TABLE users_ai', s)
    assert _status_auto_increment(ex, 'users_ai', s) == 1
    ok(ex, "INSERT INTO users_ai (name) VALUES ('after_truncate')", s)
    assert rows(ex, 'SELECT id FROM users_ai', s) == [(1,)]


# ==================== 3. ALTER TABLE AUTO_INCREMENT = n ====================

def test_alter_auto_increment_set_and_floor(inproc):
    """ALTER AUTO_INCREMENT=n 真实生效并回显；低于下限时抬高"""
    _db, ex, s = inproc
    _create_task_table(ex, s)
    ok(ex, "INSERT INTO users_ai (name) VALUES ('a')", s)     # 1
    ok(ex, 'ALTER TABLE users_ai AUTO_INCREMENT = 500', s)
    assert _status_auto_increment(ex, 'users_ai', s) == 500
    ddl = ok(ex, 'SHOW CREATE TABLE users_ai', s)['rows'][0][1]
    assert 'AUTO_INCREMENT=500' in ddl, ddl
    ok(ex, "INSERT INTO users_ai (name) VALUES ('b')", s)
    assert rows(ex, "SELECT id FROM users_ai WHERE name='b'", s) == [(500,)]

    # 设到现存最大值以下 → 实际生效值抬到 max+1（不假装设置成功）
    r = ok(ex, 'ALTER TABLE users_ai AUTO_INCREMENT = 10', s)
    assert '501' in r['message'], r
    assert _status_auto_increment(ex, 'users_ai', s) == 501


# ==================== 4. 非法场景：显式 errno ====================

def test_invalid_auto_increment_definitions(inproc):
    """非整数 1063 / 非键 1075 / 多列 1075（显式拒绝）"""
    _db, ex, s = inproc
    # 非整数 + 唯一键：不能自增
    r = err(ex, 'CREATE TABLE bad_int (a VARCHAR(10) AUTO_INCREMENT UNIQUE)', s)
    assert r['code'] == 1063, r
    # 整数但没声明为键
    r = err(ex, 'CREATE TABLE bad_key (a INT AUTO_INCREMENT, b INT)', s)
    assert r['code'] == 1075, r
    # 多列自增
    r = err(ex, 'CREATE TABLE bad_multi (a INT AUTO_INCREMENT PRIMARY KEY, '
                'b INT AUTO_INCREMENT UNIQUE)', s)
    assert r['code'] == 1075, r
    # 全失败不残留半套表
    assert rows(ex, 'SHOW TABLES', s) == []

    ok(ex, 'CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, v INT)', s)
    # task-53 起 ADD COLUMN AUTO_INCREMENT（整数 + UNIQUE 键）已实现；
    # 但一张表只能有一个自增列，第二列仍显式 1075。
    r = err(ex, 'ALTER TABLE t ADD COLUMN c INT AUTO_INCREMENT UNIQUE', s)
    assert r['code'] == 1075, r
    # 显式拒绝后表结构没变
    cols = [c['name'] for c in _db.get_table('t').cols]
    assert cols == ['id', 'v'], cols


# ==================== 5. 并发不重号 ====================

def test_concurrent_threads_unique_ids(inproc):
    """多线程（同一 Database/Table）并发 INSERT：号唯一且连续"""
    db, ex, s = inproc
    ok(ex, 'CREATE TABLE c (id INT NOT NULL AUTO_INCREMENT, v INT, '
           'PRIMARY KEY (id))', s)
    n_threads, per_thread = 8, 40
    errors = []
    barrier = threading.Barrier(n_threads)

    def worker(k):
        try:
            barrier.wait(timeout=10)
            for i in range(per_thread):
                r = ex.execute_sql(
                    f'INSERT INTO c (v) VALUES ({k * 1000 + i})', {})
                assert r.get('type') == 'ok', r
        except Exception as e:                    # pragma: no cover - 防御
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(k,))
               for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors[:3]
    assert not any(t.is_alive() for t in threads)
    ids = [r[0] for r in rows(ex, 'SELECT id FROM c ORDER BY id', s)]
    total = n_threads * per_thread
    assert len(ids) == total, (len(ids), total)
    assert len(set(ids)) == total, '并发下出现重复自增号'
    assert ids == list(range(1, total + 1)), ids[:5] + ids[-5:]
    assert _status_auto_increment(ex, 'c', s) == total + 1
    print(f"\n    {n_threads} 线程 × {per_thread} 行并发：id 1..{total} 唯一，"
          f"下一号={total + 1}")


def test_concurrent_multi_connections_unique_ids(served):
    """多连接（真实协议）并发插入：自增号不重复"""
    conn, connect, _db = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE mc (id INT NOT NULL AUTO_INCREMENT, '
                    'v INT, PRIMARY KEY (id))')
    n_conns, per_conn = 4, 25
    errors = []
    barrier = threading.Barrier(n_conns)

    def worker(k):
        c = connect()
        try:
            barrier.wait(timeout=10)
            cur = c.cursor()
            for i in range(per_conn):
                cur.execute('INSERT INTO mc (v) VALUES (%s)', (k * 1000 + i,))
            cur.close()
        except Exception as e:                    # pragma: no cover - 防御
            errors.append(repr(e))
        finally:
            try:
                c.close()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, args=(k,))
               for k in range(n_conns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors[:3]
    with conn.cursor() as cur:
        cur.execute('SELECT id FROM mc ORDER BY id')
        ids = [r[0] for r in cur.fetchall()]
        cur.execute('SHOW TABLE STATUS')
        cols = [d[0] for d in cur.description]
        row = next(r for r in cur.fetchall() if r[cols.index('Name')] == 'mc')
        next_auto = row[cols.index('Auto_increment')]
    total = n_conns * per_conn
    assert len(ids) == total and len(set(ids)) == total, (len(ids), total)
    assert ids == list(range(1, total + 1)), ids[:5] + ids[-5:]
    assert next_auto == total + 1, next_auto
    print(f"\n    {n_conns} 连接 × {per_conn} 行：id 1..{total} 唯一，"
          f"SHOW STATUS 下一号={next_auto}")


# ==================== 6. 协议路径 ====================

def test_protocol_end_to_end_task_scenario(served):
    """任务实测场景经真实协议重放：不再 1048，SHOW 元数据一致"""
    conn, _connect, _db = served
    with conn.cursor() as cur:
        cur.execute(
            'CREATE TABLE users_ai (id INT NOT NULL AUTO_INCREMENT, '
            'name VARCHAR(64) NOT NULL, email VARCHAR(128) UNIQUE, '
            'created DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (id))')
        cur.execute("INSERT INTO users_ai (name, email) "
                    "VALUES ('alice', 'alice@x')")
        cur.execute("INSERT INTO users_ai (name, email) "
                    "VALUES ('bob', 'bob@x')")
        cur.execute('SELECT id, name FROM users_ai ORDER BY id')
        assert list(cur.fetchall()) == [(1, 'alice'), (2, 'bob')]

        cur.execute('SHOW CREATE TABLE users_ai')
        ddl = cur.fetchone()[1]
        assert 'AUTO_INCREMENT' in ddl, ddl

        cur.execute('SHOW COLUMNS FROM users_ai')
        cols = [d[0] for d in cur.description]
        row = next(r for r in cur.fetchall() if r[0] == 'id')
        assert row[cols.index('Extra')] == 'auto_increment', row

        cur.execute('SHOW TABLE STATUS')
        tcols = [d[0] for d in cur.description]
        trow = next(r for r in cur.fetchall()
                    if r[tcols.index('Name')] == 'users_ai')
        assert trow[tcols.index('Auto_increment')] == 3, trow

        # ALTER 经协议同样生效
        cur.execute('ALTER TABLE users_ai AUTO_INCREMENT = 50')
        cur.execute("INSERT INTO users_ai (name) VALUES ('carol')")
        cur.execute("SELECT id FROM users_ai WHERE name='carol'")
        assert cur.fetchone()[0] == 50
