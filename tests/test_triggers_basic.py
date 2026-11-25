# -*- coding: utf-8 -*-
"""task-94 验收：触发器 MVP

覆盖：BEFORE/AFTER INSERT 副作用、BEFORE UPDATE 改 NEW、UPDATE OLD/NEW、
DELETE OLD、BEGIN...END 多语句、失败回滚、递归抑制、DDL/子查询拒绝、
SHOW TRIGGERS/SHOW CREATE TRIGGER/DROP、重启与备份恢复。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    try:
        yield db, ex, str(tmp_path / 'data')
    finally:
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
    return [tuple(r) for r in ok(ex, sql, s)['rows']]


def setup_tables(ex):
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, age INT, qty INT, price INT, '
           'total INT)')
    ok(ex, 'CREATE TABLE log_t (id INT PRIMARY KEY AUTO_INCREMENT, '
           'msg VARCHAR(100), old_v INT, new_v INT)')
    ok(ex, 'CREATE TABLE missing_child (id INT PRIMARY KEY)')


# ==================== BEFORE/AFTER INSERT ====================

def test_before_after_insert(env):
    _db, ex, _d = env
    setup_tables(ex)
    ok(ex, 'CREATE TRIGGER trg_bi BEFORE INSERT ON t FOR EACH ROW '
           'BEGIN IF NEW.age IS NULL THEN SET NEW.age = 18; END IF; END')
    ok(ex, 'CREATE TRIGGER trg_ai AFTER INSERT ON t FOR EACH ROW '
           "INSERT INTO log_t (msg, new_v) VALUES ('ins', NEW.id)")
    ok(ex, 'INSERT INTO t (id, age) VALUES (1, NULL)')
    assert rows(ex, 'SELECT id, age FROM t') == [(1, 18)]
    assert rows(ex, 'SELECT msg, new_v FROM log_t') == [('ins', 1)]
    print('\n    BEFORE INSERT 改 NEW.age=18；AFTER INSERT 写入日志')


# ==================== UPDATE NEW/OLD ====================

def test_update_new_old(env):
    _db, ex, _d = env
    setup_tables(ex)
    ok(ex, 'INSERT INTO t VALUES (2, 20, NULL, NULL, NULL)')
    ok(ex, 'CREATE TRIGGER trg_bu BEFORE UPDATE ON t FOR EACH ROW '
           'SET NEW.total = NEW.qty * NEW.price')
    ok(ex, 'CREATE TRIGGER trg_au AFTER UPDATE ON t FOR EACH ROW '
           "INSERT INTO log_t (msg, old_v, new_v) VALUES "
           "('upd', OLD.total, NEW.total)")
    ok(ex, 'UPDATE t SET qty=3, price=10 WHERE id=2')
    assert rows(ex, 'SELECT qty, price, total FROM t') == [(3, 10, 30)]
    assert rows(ex, 'SELECT msg, old_v, new_v FROM log_t') == \
        [('upd', None, 30)]
    ok(ex, 'UPDATE t SET qty=4 WHERE id=2')
    assert rows(ex, 'SELECT total FROM t') == [(40,)]
    assert rows(ex, 'SELECT old_v, new_v FROM log_t ORDER BY id') == \
        [(None, 30), (30, 40)]
    print('\n    BEFORE UPDATE NEW 赋值；AFTER UPDATE 同时读到 OLD/NEW')


# ==================== DELETE OLD / 多语句 ====================

def test_delete_old_and_multistatement(env):
    _db, ex, _d = env
    setup_tables(ex)
    ok(ex, 'INSERT INTO t VALUES (1, 10, 1, 1, 1)')
    ok(ex, 'CREATE TRIGGER trg_bd BEFORE DELETE ON t FOR EACH ROW '
           'BEGIN '
           "INSERT INTO log_t (msg, old_v) VALUES ('del', OLD.id); "
           "INSERT INTO log_t (msg, old_v) VALUES ('del2', OLD.age); "
           'END')
    ok(ex, 'DELETE FROM t WHERE id=1')
    assert rows(ex, 'SELECT msg, old_v FROM log_t ORDER BY id') == \
        [('del', 1), ('del2', 10)]
    print('\n    DELETE OLD + BEGIN...END 多条语句真实生效')


# ==================== 失败回滚 ====================

def test_trigger_failure_rolls_back_statement(env):
    _db, ex, _d = env
    setup_tables(ex)
    ok(ex, "CREATE TRIGGER trg_fail AFTER INSERT ON t FOR EACH ROW "
           "INSERT INTO not_exists VALUES (NEW.id)")
    r = err(ex, 'INSERT INTO t (id, age) VALUES (1, 20)')
    assert 'trg_fail' in r['message'], r
    assert rows(ex, 'SELECT COUNT(*) FROM t') == [(0,)]
    # 触发器内失败后，表仍可正常写入（坏触发器不锁表；删掉后恢复）
    ok(ex, 'DROP TRIGGER trg_fail')
    ok(ex, 'INSERT INTO t (id, age) VALUES (2, 20)')
    assert rows(ex, 'SELECT id FROM t') == [(2,)]
    print('\n    触发器失败：主语句回滚为 0 行，错误信息含触发器名，表可继续写')


# ==================== 递归抑制 / 校验拒绝 ====================

def test_recursion_suppressed_and_invalid_bodies(env):
    _db, ex, _d = env
    ok(ex, 'CREATE TABLE rec (id INT PRIMARY KEY, v INT)')
    ok(ex, 'CREATE TRIGGER trg_rec AFTER INSERT ON rec FOR EACH ROW '
           'INSERT INTO rec VALUES (999, 1)')
    ok(ex, 'INSERT INTO rec VALUES (1, 0)')
    # 触发器内再插入 999 时不得再次触发（只一层）→ 正好两行
    assert rows(ex, 'SELECT id, v FROM rec ORDER BY id') == \
        [(1, 0), (999, 1)]
    # 子查询 / DDL / SELECT 显式拒绝，且不落库
    for sql, needle in [
            ('CREATE TRIGGER bad1 AFTER INSERT ON rec FOR EACH ROW '
             'INSERT INTO rec SELECT 1, 2', '子查询'),
            ('CREATE TRIGGER bad2 AFTER INSERT ON rec FOR EACH ROW '
             'CREATE TABLE x (id INT)', '不支持'),
            ('CREATE TRIGGER bad3 AFTER INSERT ON rec FOR EACH ROW '
             'SELECT 1', '不支持')]:
        r = err(ex, sql)
        assert r['code'] == 1064 and needle in r['message'], r
    names = [r[0] for r in ok(ex, 'SHOW TRIGGERS')['rows']]
    assert names == ['trg_rec'], names
    print('\n    递归抑制（一层）；子查询/DDL/SELECT 显式 1064 且不落库')


# ==================== SHOW / DROP / _meta.json ====================

def test_show_drop_and_metadata(env):
    db, ex, data_dir = env
    setup_tables(ex)
    ok(ex, 'CREATE TRIGGER trg_show BEFORE INSERT ON t FOR EACH ROW '
           'SET NEW.age = 21')
    trs = ok(ex, 'SHOW TRIGGERS')['rows']
    assert len(trs) == 1 and trs[0][0] == 'trg_show', trs
    ddl = ok(ex, 'SHOW CREATE TRIGGER trg_show')['rows'][0][2]
    assert 'TRIGGER `trg_show`' in ddl and 'SET NEW.age = 21' in ddl, ddl
    # 持久化到 _meta.json
    with open(os.path.join(data_dir, '_meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    assert any(t.get('name') == 'trg_show'
               for t in (meta.get('triggers') or [])), meta.get('triggers')
    ok(ex, 'DROP TRIGGER trg_show')
    assert ok(ex, 'SHOW TRIGGERS')['rows'] == []
    ok(ex, 'DROP TRIGGER IF EXISTS trg_show')       # 幂等
    r = err(ex, 'DROP TRIGGER trg_show')
    assert r['code'] == 1360, r
    print('\n    SHOW/SHOW CREATE/DROP + _meta.json 持久化检查')


# ==================== 重启 / 备份恢复 ====================

def test_trigger_restart_and_backup_roundtrip(tmp_path):
    from lightdb.backup.backup import BackupManager
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'src')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        setup_tables(ex)
        ok(ex, 'CREATE TRIGGER trg_bi BEFORE INSERT ON t FOR EACH ROW '
               'BEGIN IF NEW.age IS NULL THEN SET NEW.age = 18; END IF; END')
        ok(ex, 'CREATE TRIGGER trg_ai AFTER INSERT ON t FOR EACH ROW '
               "INSERT INTO log_t (msg, new_v) VALUES ('ins', NEW.id)")
        dump = BackupManager(db, str(tmp_path / 'bak')).dump()
        ok(ex, 'INSERT INTO t (id, age) VALUES (1, NULL)')
        assert rows(ex, 'SELECT age FROM t') == [(18,)]
        assert rows(ex, 'SELECT COUNT(*) FROM log_t') == [(1,)]
    finally:
        db.shutdown()

    # 重启后触发器仍生效
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert [r[0] for r in ex2.execute_sql('SHOW TRIGGERS', {})['rows']] \
            == ['trg_ai', 'trg_bi']
        ok(ex2, 'INSERT INTO t (id, age) VALUES (2, NULL)')
        assert rows(ex2, 'SELECT age FROM t WHERE id=2') == [(18,)]
        assert rows(ex2, 'SELECT COUNT(*) FROM log_t') == [(2,)]
    finally:
        db2.shutdown()

    # 备份恢复后触发器仍生效
    dst = Database(str(tmp_path / 'dst'))
    try:
        ok_n, fail = BackupManager(dst, str(tmp_path / 'bak')).restore(dump)
        assert fail == 0, fail
        ex3 = Executor(dst)
        names = [r[0] for r in ex3.execute_sql('SHOW TRIGGERS', {})['rows']]
        assert sorted(names) == ['trg_ai', 'trg_bi'], names
        ok(ex3, 'INSERT INTO t (id, age) VALUES (10, NULL)')
        assert rows(ex3, 'SELECT age FROM t WHERE id=10') == [(18,)]
        assert rows(ex3, 'SELECT COUNT(*) FROM log_t') == [(1,)]
        print('\n    重启/备份恢复：触发器元数据与真实触发行为均保留')
    finally:
        dst.shutdown()


# ==================== 事务 ====================

def test_trigger_transaction_rollback(env):
    _db, ex, _d = env
    setup_tables(ex)
    ok(ex, 'CREATE TRIGGER trg_ai AFTER INSERT ON t FOR EACH ROW '
           "INSERT INTO log_t (msg, new_v) VALUES ('ins', NEW.id)")
    s = {}
    ok(ex, 'BEGIN', s)
    ok(ex, 'INSERT INTO t (id, age) VALUES (1, 20)', s)
    ok(ex, 'ROLLBACK', s)
    assert rows(ex, 'SELECT COUNT(*) FROM t') == [(0,)]
    assert rows(ex, 'SELECT COUNT(*) FROM log_t') == [(0,)]
    ok(ex, 'INSERT INTO t (id, age) VALUES (2, 20)')
    assert rows(ex, 'SELECT COUNT(*) FROM t') == [(1,)]
    assert rows(ex, 'SELECT COUNT(*) FROM log_t') == [(1,)]
    print('\n    事务：触发器副作用随主语句一起回滚；提交后一起生效')
