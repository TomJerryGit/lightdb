# -*- coding: utf-8 -*-
"""task-53 验收：`ALTER TABLE ADD/DROP COLUMN`（Navicat 设计器增删列）

Navicat 表设计器的"增删列"保存会生成：

    ALTER TABLE t ADD COLUMN c <类型> [NULL|NOT NULL] [DEFAULT ...]
                              [UNIQUE] [AUTO_INCREMENT] [FIRST | AFTER col]
    ALTER TABLE t DROP COLUMN c

本文件锁定引擎侧的真实语义：

1. ADD：尾部 / `FIRST` / `AFTER col` 列顺序真实生效；已有行按 DEFAULT 回填，
   无 DEFAULT 且 NOT NULL 的非空表显式 1138；`ADD ... AUTO_INCREMENT`
   （整数 + UNIQUE 键）按主键升序回填 1..N 并持久化计数；
   重复列 1060 / 未知类型 1064 / ADD PRIMARY 1068 显式拒绝。
2. DROP：行载荷（含历史版本）真正去掉该列；主键/UNIQUE/索引/CHECK/分区/
   全文索引列显式拒绝；删除列后清掉其自增计数残留。
3. SHOW CREATE / SHOW COLUMNS / SELECT * 列顺序一致；重启一致。
4. 崩溃注入（pending 已写 / 行已改写）后启动恢复补完 ADD/DROP。
5. 与 AUTO_INCREMENT 持久化、COMPACT、checkpoint、backup 交互正确。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8937


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


def names(ex, table):
    return [r[0] for r in rows(ex, f'SHOW COLUMNS FROM {table}')]


def ddl(ex, table):
    return rows(ex, f'SHOW CREATE TABLE {table}')[0][1]


def dump(ex, table):
    return [tuple(r) for r in rows(ex, f'SELECT * FROM {table} ORDER BY id')]


def status_auto(ex, table):
    r = ok(ex, 'SHOW TABLE STATUS')
    ci = r['columns'].index('Name')
    ai = r['columns'].index('Auto_increment')
    return next(rr[ai] for rr in r['rows'] if rr[ci] == table)


# ==================== 1. ADD：尾部 / 默认值 / NOT NULL ====================

def test_add_column_tail_default_and_not_null(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20))')
    ok(ex, "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    ok(ex, 'ALTER TABLE t ADD COLUMN status INT NOT NULL DEFAULT 7')
    assert names(ex, 't') == ['id', 'name', 'status']
    assert dump(ex, 't') == [(1, 'a', 7), (2, 'b', 7), (3, 'c', 7)]
    ddl_text = ddl(ex, 't')
    assert 'status INT NOT NULL DEFAULT 7' in ddl_text, ddl_text
    cols = rows(ex, 'SHOW COLUMNS FROM t')
    by = {r[0]: r for r in cols}
    assert by['status'][1] == 'INT' and by['status'][2] == 'NO'
    assert by['status'][4] == '7'
    # 后续插入仍按默认值
    ok(ex, "INSERT INTO t (id, name) VALUES (4, 'd')")
    assert [tuple(r) for r in rows(
        ex, 'SELECT status FROM t WHERE id=4')] == [(7,)]
    # 无 DEFAULT → NULL
    ok(ex, 'ALTER TABLE t ADD COLUMN note VARCHAR(10)')
    assert names(ex, 't') == ['id', 'name', 'status', 'note']
    assert all(r[3] is None for r in dump(ex, 't'))
    print(f"\n    ADD 尾部：默认值 7 回填 {len(dump(ex, 't'))} 行，"
          f"SHOW COLUMNS/SELECT 一致")


def test_add_not_null_without_default_is_explicit_error(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    ok(ex, 'INSERT INTO t VALUES (1), (2)')
    before_cols = names(ex, 't')
    before_ddl = ddl(ex, 't')
    r = err(ex, 'ALTER TABLE t ADD COLUMN n INT NOT NULL')
    assert r['code'] == 1138, r
    assert names(ex, 't') == before_cols and ddl(ex, 't') == before_ddl
    # 空表允许加 NOT NULL（没有既有行需要填）
    ok(ex, 'CREATE TABLE e (id INT PRIMARY KEY)')
    ok(ex, 'ALTER TABLE e ADD COLUMN n INT NOT NULL')
    r = err(ex, 'INSERT INTO e (id) VALUES (1)')
    assert r['code'] == 1048, r
    ok(ex, 'INSERT INTO e (id, n) VALUES (1, 5)')
    print(f"\n    NOT NULL 无默认：非空表→1138，空表可加、省略插入→1048")


# ==================== 2. ADD：FIRST / AFTER 位置 ====================

def test_add_first_and_after_positions(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, a INT UNIQUE, b INT)')
    ok(ex, 'INSERT INTO t VALUES (1, 10, 100), (2, 20, 200)')
    ok(ex, 'ALTER TABLE t ADD COLUMN code INT DEFAULT 0 FIRST')
    assert names(ex, 't') == ['code', 'id', 'a', 'b']
    ddl_text = ddl(ex, 't')
    assert ddl_text.index('code') < ddl_text.index('id') < \
        ddl_text.index('a') < ddl_text.index('b'), ddl_text
    assert dump(ex, 't') == [(0, 1, 10, 100), (0, 2, 20, 200)]
    # 主键位置后移后，WHERE/UPDATE/UNIQUE 仍按列名正确工作
    ok(ex, 'UPDATE t SET code = 5 WHERE id = 2')
    assert dump(ex, 't') == [(0, 1, 10, 100), (5, 2, 20, 200)]
    assert err(ex, 'INSERT INTO t (id, a, b) VALUES (3, 10, 300)')['code'] == 1062
    # AFTER
    ok(ex, 'ALTER TABLE t ADD COLUMN mid VARCHAR(8) DEFAULT \'m\' AFTER a')
    assert names(ex, 't') == ['code', 'id', 'a', 'mid', 'b']
    assert dump(ex, 't')[0][3] == 'm'
    # AFTER 未知列显式 1054 且结构不变
    before = ddl(ex, 't')
    r = err(ex, 'ALTER TABLE t ADD COLUMN x INT AFTER nope')
    assert r['code'] == 1054, r
    assert ddl(ex, 't') == before
    # 新列插到主键前后，INSERT 省略列按默认值补齐
    ok(ex, 'INSERT INTO t (id, a, b) VALUES (3, 30, 300)')
    got = [tuple(r) for r in rows(ex, 'SELECT * FROM t WHERE id=3')]
    assert got == [(0, 3, 30, 'm', 300)], got
    print(f"\n    ADD FIRST/AFTER：列顺序 {names(ex, 't')}，"
          f"行载荷/UNIQUE/WHERE/UPDATE 均一致")


# ==================== 3. ADD：重复列 / 未知类型 / 主键 ====================

def test_add_column_invalid_definitions_explicit(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    before = ddl(ex, 't')
    r = err(ex, 'ALTER TABLE t ADD COLUMN v INT')
    assert r['code'] == 1060, r
    r = err(ex, 'ALTER TABLE t ADD COLUMN weird BLOB')
    assert r['code'] == 1064, r
    r = err(ex, 'ALTER TABLE t ADD COLUMN pk2 INT PRIMARY KEY')
    assert r['code'] == 1068, r
    # 多动作变体仍显式"未实现"
    r = err(ex, 'ALTER TABLE t ADD COLUMN a INT, ADD COLUMN b INT')
    assert r['code'] in (1064, 1235), r
    r = err(ex, 'ALTER TABLE t ADD COLUMN IF NOT EXISTS a INT')
    assert r['code'] in (1064, 1235), r
    assert ddl(ex, 't') == before


# ==================== 4. ADD：AUTO_INCREMENT 列 ====================

def test_add_auto_increment_unique_column(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE a (id INT PRIMARY KEY, v INT)')
    ok(ex, 'INSERT INTO a VALUES (1, 10), (2, 20), (3, 30)')
    r = ok(ex, 'ALTER TABLE a ADD COLUMN seq INT AUTO_INCREMENT UNIQUE')
    assert r.get('affected') == 0
    assert 'AUTO_INCREMENT 回填/下一号=4' in r['message'], r
    assert names(ex, 'a') == ['id', 'v', 'seq']
    assert dump(ex, 'a') == [(1, 10, 1), (2, 20, 2), (3, 30, 3)]
    ddl_text = ddl(ex, 'a')
    assert 'seq INT NOT NULL UNIQUE AUTO_INCREMENT' in ddl_text, ddl_text
    assert 'AUTO_INCREMENT=4' in ddl_text, ddl_text
    assert status_auto(ex, 'a') == 4
    cols = {r[0]: r for r in rows(ex, 'SHOW COLUMNS FROM a')}
    assert cols['seq'][5] == 'auto_increment', cols['seq']
    # 省略自增列分配 4；显式大值推进计数器
    ok(ex, 'INSERT INTO a (id, v) VALUES (4, 40)')
    ok(ex, 'INSERT INTO a (id, v, seq) VALUES (5, 50, 100)')
    ok(ex, 'INSERT INTO a (id, v) VALUES (6, 60)')
    assert dump(ex, 'a') == [
        (1, 10, 1), (2, 20, 2), (3, 30, 3),
        (4, 40, 4), (5, 50, 100), (6, 60, 101)]
    assert status_auto(ex, 'a') == 102
    print(f"\n    ADD AUTO_INCREMENT(UNIQUE)：既有行回填 1..3，"
          f"下一号 4；显式 100 推进后新插入 101")


def test_add_auto_increment_invalid_explicit(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE b (id INT PRIMARY KEY, v INT)')
    before = ddl(ex, 'b')
    r = err(ex, 'ALTER TABLE b ADD COLUMN s VARCHAR(5) AUTO_INCREMENT UNIQUE')
    assert r['code'] == 1063, r
    r = err(ex, 'ALTER TABLE b ADD COLUMN s INT AUTO_INCREMENT')
    assert r['code'] == 1075, r
    ok(ex, 'CREATE TABLE c (id INT PRIMARY KEY, u INT AUTO_INCREMENT UNIQUE, v INT)')
    r = err(ex, 'ALTER TABLE c ADD COLUMN s INT AUTO_INCREMENT UNIQUE')
    assert r['code'] == 1075, r
    assert ddl(ex, 'b') == before


# ==================== 5. DROP COLUMN ====================

def test_drop_column_removes_data_rows_and_metadata(inproc):
    _db, ex, = inproc
    ok(ex, 'CREATE TABLE d (id INT PRIMARY KEY, a INT, '
           'b VARCHAR(20), c INT)')
    ok(ex, "INSERT INTO d VALUES (1, 10, 'SECRETMARK', 100), "
           "(2, 20, 'SECRETMARK', 200)")
    ok(ex, 'ALTER TABLE d DROP COLUMN b')
    assert names(ex, 'd') == ['id', 'a', 'c']
    assert dump(ex, 'd') == [(1, 10, 100), (2, 20, 200)]
    assert 'b' not in [c.split()[0] for c in ddl(ex, 'd').splitlines()[1:-1]]
    # 数据文件里不能还能读到被删值（槽位/残字节清零）
    dat = _db.data_dir / 'd.dat'
    assert b'SECRETMARK' not in dat.read_bytes()
    # 后续 DML 正常
    ok(ex, 'INSERT INTO d VALUES (3, 30, 300)')
    ok(ex, 'UPDATE d SET c = 999 WHERE id = 3')
    assert dump(ex, 'd')[-1] == (3, 30, 999)
    ok(ex, 'DELETE FROM d WHERE id = 1')
    assert dump(ex, 'd') == [(2, 20, 200), (3, 30, 999)]
    print(f"\n    DROP 普通列：行/元数据同步，.dat 中已读不到被删值，"
          f"后续 INSERT/UPDATE/DELETE 正常")


def test_drop_column_rejections_are_explicit(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE e (id INT PRIMARY KEY, code INT UNIQUE, '
           'v INT, w INT, CHECK (w > 0))')
    ok(ex, 'CREATE INDEX idx_v ON e (v)')
    ok(ex, 'INSERT INTO e VALUES (1, 1, 1, 1)')
    cases = {
        'ALTER TABLE e DROP COLUMN id': (1091, 1553),
        'ALTER TABLE e DROP COLUMN code': (1091, 1553),
        'ALTER TABLE e DROP COLUMN v': (1091, 1553),
        'ALTER TABLE e DROP COLUMN w': (3959,),
        'ALTER TABLE e DROP COLUMN nope': (1054,),
    }
    before_cols = names(ex, 'e')
    before_ddl = ddl(ex, 'e')
    for sql, codes in cases.items():
        r = err(ex, sql)
        assert r['code'] in codes, (sql, r)
        assert names(ex, 'e') == before_cols and ddl(ex, 'e') == before_ddl
    # 最后一列
    ok(ex, 'CREATE TABLE one (id INT PRIMARY KEY)')
    r = err(ex, 'ALTER TABLE one DROP COLUMN id')
    assert r['code'] in (1090, 1091, 1553), r
    assert names(ex, 'one') == ['id']


def test_drop_column_clears_stale_auto_counter(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE z (id INT PRIMARY KEY, '
           'code INT AUTO_INCREMENT UNIQUE, v INT)')
    ok(ex, 'INSERT INTO z (id, v) VALUES (1, 10), (2, 20)')
    assert dump(ex, 'z')[0][1] == 1
    assert status_auto(ex, 'z') == 3
    # 去掉唯一索引 → 去掉自增 → 此时 code 是普通列；DROP 应清掉计数残留
    ok(ex, 'DROP INDEX code ON z')
    ok(ex, 'ALTER TABLE z MODIFY COLUMN code INT')
    autoinc = _db.data_dir / 'z.autoinc'
    before = json.loads(autoinc.read_text(encoding='utf-8')) if \
        autoinc.exists() else {}
    assert 'code' in before, before
    r = ok(ex, 'ALTER TABLE z DROP COLUMN code')
    assert '清理' in r['message'], r
    assert names(ex, 'z') == ['id', 'v']
    after = json.loads(autoinc.read_text(encoding='utf-8')) if \
        autoinc.exists() else {}
    assert 'code' not in after, after
    # 删除自增列本身（主键/唯一列）显式拒绝
    ok(ex, 'CREATE TABLE f (id INT NOT NULL AUTO_INCREMENT, v INT, '
           'PRIMARY KEY (id))')
    r = err(ex, 'ALTER TABLE f DROP COLUMN id')
    assert r['code'] in (1091, 1553), r


# ==================== 6. 重启 / COMPACT / backfill 一致性 ====================

def test_add_drop_restart_and_compact_consistent(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_restart')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, a INT UNIQUE)')
        ok(ex, "INSERT INTO t VALUES (1, 10), (2, 20)")
        ok(ex, 'ALTER TABLE t ADD COLUMN note VARCHAR(10) DEFAULT \'n\' AFTER id')
        ok(ex, 'ALTER TABLE t ADD COLUMN code INT DEFAULT 3 FIRST')
        ok(ex, 'ALTER TABLE t DROP COLUMN note')
        ok(ex, 'ALTER TABLE t ADD COLUMN seq INT AUTO_INCREMENT UNIQUE')
        after_ddl = ddl(ex, 't')
        after_cols = rows(ex, 'SHOW COLUMNS FROM t')
        after_rows = dump(ex, 't')
        ok(ex, 'INSERT INTO t (id, a) VALUES (3, 30)')
        assert status_auto(ex, 't') == 4
        db.checkpoint()
        db.compact(tables=['t'])
        assert ddl(ex, 't') == after_ddl or 'AUTO_INCREMENT' in ddl(ex, 't')
        assert dump(ex, 't')[:2] == after_rows
        assert status_auto(ex, 't') == 4
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert rows(ex2, 'SHOW COLUMNS FROM t') == after_cols
        assert dump(ex2, 't')[:2] == after_rows
        assert status_auto(ex2, 't') == 4
        ok(ex2, 'INSERT INTO t (id, a) VALUES (4, 40)')
        assert dump(ex2, 't')[-1][-1] == 4
        print(f"\n    ADD/DROP + checkpoint + COMPACT + 重启："
              f"列/行/自增下一号(4) 全部保持")
    finally:
        db2.shutdown()


# ==================== 7. 崩溃注入：pending 已写 / 行已改写 ====================

CHILD_CODE = r'''
import json, os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
from lightdb.db import database as dbmod
from lightdb.db.table import Table
mode = os.environ.get("CRASH_MODE", "pending")
db = Database(os.environ["DATA_DIR"])
if mode == "pending":
    orig_begin = dbmod.Database._ddl_begin
    def boom_begin(self, entry):
        orig_begin(self, entry)
        os._exit(7)
    dbmod.Database._ddl_begin = boom_begin
else:
    orig_rw = Table.rewrite_row_payloads
    def boom_rw(self, *a, **k):
        result = orig_rw(self, *a, **k)
        os._exit(7)
        return result
    Table.rewrite_row_payloads = boom_rw
ex = Executor(db)
r = ex.execute_sql(os.environ["DDL_SQL"], {})
print(json.dumps({"type": r.get("type"), "code": r.get("code")}))
sys.stdout.flush()
os._exit(0)
'''


def _run_crash_child(data_dir, ddl_sql, mode):
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['DATA_DIR'] = str(data_dir)
    env['DDL_SQL'] = ddl_sql
    env['CRASH_MODE'] = mode
    env['LIGHTDB_ALLOW_EMPTY_PASSWORD'] = '1'
    proc = subprocess.run([sys.executable, '-c', CHILD_CODE], env=env,
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    assert proc.returncode == 7, (mode, ddl_sql, proc.returncode,
                                  proc.stdout[-300:], proc.stderr[-1000:])
    return proc


@pytest.mark.parametrize('mode', ['pending', 'after_rewrite'])
def test_crash_recovery_add_column(tmp_path, mode):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / f'data_add_{mode}')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        ok(ex, "INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)")
    finally:
        db.shutdown()
    _run_crash_child(data_dir, 'ALTER TABLE t ADD COLUMN note VARCHAR(10) '
                              "DEFAULT 'n' AFTER id", mode)
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert db2._pending_ddl is None
        assert names(ex2, 't') == ['id', 'note', 'v'], names(ex2, 't')
        assert dump(ex2, 't') == [
            (1, 'n', 10), (2, 'n', 20), (3, 'n', 30)], dump(ex2, 't')
        ok(ex2, "INSERT INTO t VALUES (4, 'x', 40)")
        assert dump(ex2, 't')[-1] == (4, 'x', 40)
        print(f"\n    ADD COLUMN 崩溃恢复[{mode}]：pending 清空、"
              f"列顺序 {names(ex2, 't')}、回填默认值/可继续写入")
    finally:
        db2.shutdown()


@pytest.mark.parametrize('mode', ['pending', 'after_rewrite'])
def test_crash_recovery_drop_column(tmp_path, mode):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / f'data_drop_{mode}')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT)')
        ok(ex, 'INSERT INTO t VALUES (1, 10, 100), (2, 20, 200)')
    finally:
        db.shutdown()
    _run_crash_child(data_dir, 'ALTER TABLE t DROP COLUMN a', mode)
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert db2._pending_ddl is None
        assert names(ex2, 't') == ['id', 'b'], names(ex2, 't')
        assert dump(ex2, 't') == [(1, 100), (2, 200)], dump(ex2, 't')
        ok(ex2, 'INSERT INTO t VALUES (3, 300)')
        assert dump(ex2, 't')[-1] == (3, 300)
        print(f"\n    DROP COLUMN 崩溃恢复[{mode}]：pending 清空、"
              f"行载荷去掉列后可继续写入")
    finally:
        db2.shutdown()


def test_crash_recovery_add_auto_increment_column(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_add_auto')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        ok(ex, 'INSERT INTO t VALUES (1, 10), (2, 20)')
    finally:
        db.shutdown()
    _run_crash_child(data_dir, 'ALTER TABLE t ADD COLUMN seq INT '
                              'AUTO_INCREMENT UNIQUE', 'pending')
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert db2._pending_ddl is None
        assert names(ex2, 't') == ['id', 'v', 'seq']
        got = [tuple(r) for r in rows(ex2, 'SELECT * FROM t ORDER BY id')]
        assert [g[2] for g in got] == [1, 2], got
        assert status_auto(ex2, 't') == 3
        ok(ex2, 'INSERT INTO t (id, v) VALUES (3, 30)')
        assert [tuple(r) for r in rows(
            ex2, 'SELECT seq FROM t WHERE id=3')] == [(3,)]
        print(f"\n    ADD AUTO_INCREMENT 崩溃恢复：回填 1..2、下一号 3、"
              f"重启后可继续分配")
    finally:
        db2.shutdown()


# ==================== 8. 协议路径 ====================

def test_protocol_add_drop_column(served):
    conn = served
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        cur.execute('INSERT INTO t VALUES (1, 10), (2, 20)')
        cur.execute("ALTER TABLE t ADD COLUMN note VARCHAR(8) "
                    "DEFAULT 'n' AFTER id")
        cur.execute('SHOW COLUMNS FROM t')
        assert [r[0] for r in cur.fetchall()] == ['id', 'note', 'v']
        cur.execute('SELECT * FROM t ORDER BY id')
        assert [tuple(r) for r in cur.fetchall()] == [
            (1, 'n', 10), (2, 'n', 20)]
        cur.execute('ALTER TABLE t DROP COLUMN v')
        cur.execute('SHOW CREATE TABLE t')
        create = cur.fetchone()[1]
        assert 'note' in create and 'v INT' not in create
        cur.execute('SELECT * FROM t ORDER BY id')
        assert [tuple(r) for r in cur.fetchall()] == [(1, 'n'), (2, 'n')]
        print("\n    协议路径：ADD AFTER 默认值 + DROP 列，SELECT/SHOW 一致")
