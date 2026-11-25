# -*- coding: utf-8 -*-
"""task-93 验收：外键 MVP（单列 RESTRICT/NO ACTION）

覆盖：
* CREATE TABLE 内联/命名外键；SHOW CREATE TABLE 回显；
* 子表 INSERT/UPDATE：非 NULL 值必须命中父行（1452），NULL 允许；
* 父表 DELETE/UPDATE：被引用且 RESTRICT → 1451；无引用可删；
* 父表/父列缺失、父列无键、子列缺失 → 1822 风格定义期错误；
* CASCADE / SET NULL / SET DEFAULT → 显式 1064（不静默降级）；
* 重启 / 备份恢复后约束仍生效；事务回滚后不残留写入。
"""
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
        yield db, ex
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


def make_parent_child(ex, child_ddl='FOREIGN KEY (pid) REFERENCES parent(id)'):
    ok(ex, 'CREATE TABLE parent (id INT PRIMARY KEY, name VARCHAR(20))')
    ok(ex, f'CREATE TABLE child (id INT PRIMARY KEY, pid INT, '
           f'v VARCHAR(10), {child_ddl})')
    ok(ex, "INSERT INTO parent VALUES (1,'a'),(2,'b')")


# ==================== 定义 / SHOW ====================

def test_create_named_fk_and_show_create(env):
    _db, ex = env
    make_parent_child(
        ex, 'CONSTRAINT fk_child_parent FOREIGN KEY (pid) '
            'REFERENCES parent(id)')
    ddl = ok(ex, 'SHOW CREATE TABLE child')['rows'][0][1]
    assert 'CONSTRAINT fk_child_parent FOREIGN KEY (pid) ' \
           'REFERENCES parent (id)' in ddl, ddl
    # 匿名外键自动命名并持久化回显
    ok(ex, 'CREATE TABLE c2 (id INT PRIMARY KEY, pid INT, '
           'FOREIGN KEY (pid) REFERENCES parent(id))')
    ddl2 = ok(ex, 'SHOW CREATE TABLE c2')['rows'][0][1]
    assert 'CONSTRAINT c2_ibfk_1 FOREIGN KEY (pid)' in ddl2, ddl2
    print('\n    CREATE/SHOW CREATE：命名与匿名外键均真实回显')


def test_fk_definition_errors(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE parent (id INT PRIMARY KEY, name VARCHAR(20))')
    # 父表不存在
    r = err(ex, 'CREATE TABLE bad1 (id INT PRIMARY KEY, pid INT, '
                'FOREIGN KEY (pid) REFERENCES nope(id))')
    assert r['code'] == 1822, r
    # 父列不存在
    r = err(ex, 'CREATE TABLE bad2 (id INT PRIMARY KEY, pid INT, '
                'FOREIGN KEY (pid) REFERENCES parent(nope))')
    assert r['code'] == 1822, r
    # 父列无主键/唯一/索引
    r = err(ex, 'CREATE TABLE bad3 (id INT PRIMARY KEY, pid INT, '
                'FOREIGN KEY (pid) REFERENCES parent(name))')
    assert r['code'] == 1822, r
    # 子列不存在
    r = err(ex, 'CREATE TABLE bad4 (id INT PRIMARY KEY, x INT, '
                'FOREIGN KEY (pid) REFERENCES parent(id))')
    assert r['code'] == 1822, r
    # 父列有 UNIQUE 索引则可以
    ok(ex, 'CREATE TABLE p2 (id INT PRIMARY KEY, code INT UNIQUE)')
    ok(ex, 'CREATE TABLE good (id INT PRIMARY KEY, code INT, '
           'FOREIGN KEY (code) REFERENCES p2(code))')
    print('\n    定义期：父表/父列/父键/子列错误均 1822；UNIQUE 父列可引用')


# ==================== 子表写入 ====================

def test_child_insert_update_and_null(env):
    _db, ex = env
    make_parent_child(ex)
    ok(ex, 'INSERT INTO child VALUES (10,1,NULL),(11,NULL,NULL)')
    r = err(ex, 'INSERT INTO child VALUES (12,99,NULL)')
    assert r['code'] == 1452 and 'foreign key constraint fails' in r['message']
    ok(ex, 'UPDATE child SET pid=2 WHERE id=10')
    r = err(ex, 'UPDATE child SET pid=99 WHERE id=10')
    assert r['code'] == 1452, r
    ok(ex, 'UPDATE child SET pid=NULL WHERE id=10')       # NULL 允许
    assert rows(ex, 'SELECT id, pid FROM child ORDER BY id') == \
        [(10, None), (11, None)]
    print('\n    子表 INSERT/UPDATE：非法父值 1452、NULL 允许、合法更新成功')


# ==================== 父表删除/更新 RESTRICT ====================

def test_parent_delete_update_restrict(env):
    _db, ex = env
    make_parent_child(ex)
    ok(ex, 'INSERT INTO child VALUES (10,1,NULL)')
    r = err(ex, 'DELETE FROM parent WHERE id=1')
    assert r['code'] == 1451 and 'parent row' in r['message'], r
    r = err(ex, 'UPDATE parent SET id=9 WHERE id=1')
    assert r['code'] == 1451, r
    # 改非被引用列不受限制
    ok(ex, "UPDATE parent SET name='aa' WHERE id=1")
    # 无引用父行可删
    ok(ex, 'DELETE FROM parent WHERE id=2')
    # 删掉子行后可删父行
    ok(ex, 'DELETE FROM child WHERE id=10')
    ok(ex, 'DELETE FROM parent WHERE id=1')
    print('\n    父表 RESTRICT：删除/改键 1451；无引用/删子行后可操作')


# ==================== 不支持的级联动作 ====================

@pytest.mark.parametrize('action', [
    'ON DELETE CASCADE', 'ON UPDATE CASCADE',
    'ON DELETE SET NULL', 'ON UPDATE SET NULL', 'ON DELETE SET DEFAULT',
])
def test_cascade_actions_are_explicit_1064(env, action):
    _db, ex = env
    ok(ex, 'CREATE TABLE parent (id INT PRIMARY KEY)')
    r = err(ex, f'CREATE TABLE c (id INT PRIMARY KEY, pid INT, '
                f'FOREIGN KEY (pid) REFERENCES parent(id) {action})')
    assert r['code'] == 1064 and (
        'CASCADE' in r['message'] or 'SET NULL' in r['message']
        or 'SET DEFAULT' in r['message']), r
    # 显式 RESTRICT / NO ACTION 接受
    ok(ex, 'CREATE TABLE ok1 (id INT PRIMARY KEY, pid INT, '
           'FOREIGN KEY (pid) REFERENCES parent(id) '
           'ON DELETE RESTRICT ON UPDATE NO ACTION)')
    print(f'\n    {action} 显式 1064；RESTRICT/NO ACTION 接受')


# ==================== 重启持久化 ====================

def test_fk_survives_restart(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    d = str(tmp_path / 'data_restart')
    db = Database(d)
    ex = Executor(db)
    try:
        make_parent_child(ex)
        ok(ex, 'INSERT INTO child VALUES (10,1,NULL)')
    finally:
        db.shutdown()
    db2 = Database(d)
    ex2 = Executor(db2)
    try:
        r = err(ex2, 'INSERT INTO child VALUES (11,99,NULL)')
        assert r['code'] == 1452, r
        r = err(ex2, 'DELETE FROM parent WHERE id=1')
        assert r['code'] == 1451, r
        assert 'FOREIGN KEY (pid)' in \
            ok(ex2, 'SHOW CREATE TABLE child')['rows'][0][1]
        print('\n    重启后：外键元数据/子写校验/父删限制全部仍生效')
    finally:
        db2.shutdown()


# ==================== 备份恢复 ====================

def test_fk_backup_restore_roundtrip(tmp_path):
    from lightdb.backup.backup import BackupManager
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    src = Database(str(tmp_path / 'src'))
    ex = Executor(src)
    bak = str(tmp_path / 'bak')
    try:
        make_parent_child(ex)
        ok(ex, 'INSERT INTO child VALUES (10,1,NULL)')
        dump = BackupManager(src, bak).dump()
    finally:
        src.shutdown()
    dst = Database(str(tmp_path / 'dst'))
    try:
        ok_n, fail = BackupManager(dst, bak).restore(dump)
        assert fail == 0, fail
        ex2 = Executor(dst)
        ddl = ok(ex2, 'SHOW CREATE TABLE child')['rows'][0][1]
        assert 'FOREIGN KEY (pid) REFERENCES parent (id)' in ddl, ddl
        r = err(ex2, 'INSERT INTO child VALUES (11,99,NULL)')
        assert r['code'] == 1452, r
        r = err(ex2, 'DELETE FROM parent WHERE id=1')
        assert r['code'] == 1451, r
        assert rows(ex2, 'SELECT COUNT(*) FROM child') == [(1,)]
        print('\n    备份恢复：外键 DDL/数据往返，恢复后约束仍真实生效')
    finally:
        dst.shutdown()


# ==================== 事务回滚一致性 ====================

def test_fk_transaction_rollback(env):
    _db, ex = env
    make_parent_child(ex)
    s = {}
    ok(ex, 'BEGIN', s)
    r = err(ex, 'INSERT INTO child VALUES (10,99,NULL)', s)  # FK 失败
    assert r['code'] == 1452, r
    ok(ex, 'ROLLBACK', s)
    assert rows(ex, 'SELECT COUNT(*) FROM child') == [(0,)]
    # 事务内插入父+子、回滚：两表都不留
    s2 = {}
    ok(ex, 'BEGIN', s2)
    ok(ex, 'INSERT INTO parent VALUES (3,\'c\')', s2)
    ok(ex, 'INSERT INTO child VALUES (12,3,NULL)', s2)
    ok(ex, 'ROLLBACK', s2)
    assert rows(ex, 'SELECT COUNT(*) FROM parent') == [(2,)]
    assert rows(ex, 'SELECT COUNT(*) FROM child') == [(0,)]
    # 失败的外键语句不污染后续写入
    ok(ex, 'INSERT INTO child VALUES (13,2,NULL)')
    assert rows(ex, 'SELECT pid FROM child WHERE id=13') == [(2,)]
    print('\n    事务：外键失败可回滚；父+子同事务回滚均不残留')
