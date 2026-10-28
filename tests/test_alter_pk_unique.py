# -*- coding: utf-8 -*-
"""task-62 验收：ALTER 主键 / 唯一约束变更

覆盖：
* `ADD PRIMARY KEY (col)`：隐式内部键表 → 显式主键；重复值 1062、
  NULL 值 1138、缺列 1072、已有主键 1068、复合 1235、非整数 1064；
  SHOW COLUMNS Key / SHOW INDEX / SHOW CREATE 同步；
* `DROP PRIMARY KEY`：移除显式约束（列保留 NOT NULL）、数据可读、
  可再 ADD；无主键 1091；AUTO_INCREMENT 依赖 1075；
* 换列 ADD PRIMARY KEY（`(id, code)` 中 code 唯一）：主键 B+Tree 真实
  重键，插入重复 code 1062，重启/COMPACT/备份后一致；
* `ADD UNIQUE [INDEX|KEY] [name] (cols)`：已有数据重复 1062、写入重复
  1062、命名/多列、SHOW INDEX 真实；`DROP INDEX` 后可重建。
"""
import os
import shutil
import subprocess
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
    r = ex.execute_sql(sql, s or {})
    assert r.get('type') != 'error', (sql, r)
    return r


def err(ex, sql, s=None):
    r = ex.execute_sql(sql, s or {})
    assert r.get('type') == 'error', (sql, r)
    return r


def rows(ex, sql):
    return [tuple(r) for r in ok(ex, sql)['rows']]


def col_meta(ex, table):
    r = ok(ex, f'SHOW COLUMNS FROM {table}')
    return {row[0]: dict(zip(r['columns'], row)) for row in r['rows']}


def index_rows(ex, table):
    r = ok(ex, f'SHOW INDEX FROM {table}')
    return [dict(zip(r['columns'], row)) for row in r['rows']]


# ==================== ADD PRIMARY KEY ====================

def test_add_primary_key_implicit_table(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE t (id INT, v INT)')
    ok(ex, 'INSERT INTO t VALUES (1,10),(2,20),(3,30)')
    ok(ex, 'ALTER TABLE t ADD PRIMARY KEY (id)')
    cols = col_meta(ex, 't')
    assert cols['id']['Key'] == 'PRI' and cols['id']['Null'] == 'NO', cols
    idx = index_rows(ex, 't')
    assert any(r['Key_name'] == 'PRIMARY' and r['Column_name'] == 'id'
               and r['Index_type'] == 'BTREE' for r in idx), idx
    ddl = ok(ex, 'SHOW CREATE TABLE t')['rows'][0][1]
    assert 'PRIMARY KEY' in ddl, ddl

    r = err(ex, 'INSERT INTO t VALUES (1, 99)')
    assert r['code'] == 1062, r
    r = err(ex, 'ALTER TABLE t ADD PRIMARY KEY (id)')
    assert r['code'] == 1068, r
    print("\n    ADD PRIMARY KEY(id)：元数据/索引/唯一性全部同步，重复 1068")


def test_add_primary_key_errors(env):
    _db, ex = env
    # 重复值 1062
    ok(ex, 'CREATE TABLE dup (id INT, x INT)')
    ok(ex, 'INSERT INTO dup VALUES (1,7),(2,7),(3,8)')
    r = err(ex, 'ALTER TABLE dup ADD PRIMARY KEY (x)')
    assert r['code'] == 1062, r
    # NULL 值 1138
    ok(ex, 'CREATE TABLE nn (id INT, x INT)')
    ok(ex, 'INSERT INTO nn VALUES (1,NULL),(2,5)')
    r = err(ex, 'ALTER TABLE nn ADD PRIMARY KEY (x)')
    assert r['code'] == 1138, r
    # 缺列 1072
    r = err(ex, 'ALTER TABLE nn ADD PRIMARY KEY (nope)')
    assert r['code'] == 1072, r
    # 超过 2 列的复合主键：物理键是 64 位整数 → 显式 1235
    r = err(ex, 'ALTER TABLE nn ADD PRIMARY KEY (id, x, id)')
    assert r['code'] == 1235, r
    # 非整数类型 1064
    ok(ex, 'CREATE TABLE s (id INT, name VARCHAR(20))')
    r = err(ex, 'ALTER TABLE s ADD PRIMARY KEY (name)')
    assert r['code'] == 1064, r
    print("\n    ADD PK 错误码：重复 1062 / NULL 1138 / 缺列 1072 / "
          "复合 1235 / 非整数 1064")


# ==================== DROP PRIMARY KEY ====================

def test_drop_primary_key_and_readd(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    ok(ex, 'INSERT INTO t VALUES (1,10),(2,20)')
    ok(ex, 'ALTER TABLE t DROP PRIMARY KEY')
    cols = col_meta(ex, 't')
    assert cols['id']['Key'] == '', cols
    assert rows(ex, 'SELECT * FROM t ORDER BY id') == [(1, 10), (2, 20)]
    assert not any(r['Key_name'] == 'PRIMARY'
                   for r in index_rows(ex, 't'))
    ddl = ok(ex, 'SHOW CREATE TABLE t')['rows'][0][1]
    assert 'PRIMARY KEY' not in ddl, ddl
    # 删除后可再 ADD，数据/索引继续可用
    ok(ex, 'ALTER TABLE t ADD PRIMARY KEY (id)')
    assert col_meta(ex, 't')['id']['Key'] == 'PRI'
    ok(ex, 'INSERT INTO t VALUES (3, 30)')
    r = err(ex, 'INSERT INTO t VALUES (3, 31)')
    assert r['code'] == 1062, r
    # 再次 DROP 也允许
    ok(ex, 'ALTER TABLE t DROP PRIMARY KEY')
    # 没有显式主键 → 1091
    ok(ex, 'CREATE TABLE plain (id INT, v INT)')
    r = err(ex, 'ALTER TABLE plain DROP PRIMARY KEY')
    assert r['code'] == 1091, r
    print("\n    DROP PRIMARY KEY：SHOW CREATE/COLUMNS/INDEX 同步、数据可读、"
          "可再 ADD；无主键 1091")


def test_drop_primary_key_auto_increment_1075(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE ai (id INT AUTO_INCREMENT PRIMARY KEY, v INT)')
    ok(ex, 'INSERT INTO ai (v) VALUES (1)')
    r = err(ex, 'ALTER TABLE ai DROP PRIMARY KEY')
    assert r['code'] == 1075, r
    # 主键仍在，自增继续可用
    ok(ex, 'INSERT INTO ai (v) VALUES (2)')
    assert rows(ex, 'SELECT id, v FROM ai ORDER BY id') == [(1, 1), (2, 2)]
    print("\n    AUTO_INCREMENT 依赖主键：DROP PRIMARY KEY 显式 1075，"
          "自增/主键均未被破坏")


# ==================== 换列重键（真实 B+Tree 重建） ====================

def test_add_primary_key_rekeys_btree(env):
    _db, ex = env
    # id 是内部行键（隐式），code 唯一 → ADD PRIMARY KEY(code) 必须重键
    ok(ex, 'CREATE TABLE r (id INT, code INT, note VARCHAR(10))')
    ok(ex, 'INSERT INTO r VALUES (1, 42, "a"), (2, 7, "b"), '
           '(3, 99, "c")')
    ok(ex, 'CREATE INDEX idx_id ON r (id)')
    ok(ex, 'ALTER TABLE r ADD PRIMARY KEY (code)')
    cols = col_meta(ex, 'r')
    assert cols['code']['Key'] == 'PRI', cols
    assert cols['id']['Key'] == '', cols
    idx = index_rows(ex, 'r')
    assert any(r['Key_name'] == 'PRIMARY' and r['Column_name'] == 'code'
               for r in idx), idx
    # 二级索引仍存在且查询可用
    assert any(r['Key_name'] == 'idx_id' for r in idx), idx
    assert rows(ex, 'SELECT id FROM r WHERE code = 42') == [(1,)]
    r = err(ex, 'INSERT INTO r VALUES (4, 42, "dup")')
    assert r['code'] == 1062, r
    ok(ex, 'INSERT INTO r VALUES (4, 8, "d")')
    assert rows(ex, 'SELECT id, code FROM r ORDER BY code') == \
        [(2, 7), (4, 8), (1, 42), (3, 99)]
    # 重键后再 DROP / 再 ADD
    ok(ex, 'ALTER TABLE r DROP PRIMARY KEY')
    ok(ex, 'ALTER TABLE r ADD PRIMARY KEY (code)')
    print("\n    ADD PK 换列：B+Tree 真实重键、二级索引保留、重复 1062、"
          "可再 DROP/ADD")


# ==================== ADD UNIQUE / DROP INDEX ====================

def test_add_unique_constraint(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE u (id INT PRIMARY KEY, name VARCHAR(20), '
           'a INT, b INT)')
    ok(ex, "INSERT INTO u VALUES (1,'x',1,1),(2,'y',1,2)")
    # 命名唯一索引，已有数据无重复 → 成功
    ok(ex, 'ALTER TABLE u ADD UNIQUE KEY uq_name (name)')
    idx = index_rows(ex, 'u')
    uq = [r for r in idx if r['Key_name'] == 'uq_name']
    assert len(uq) == 1 and uq[0]['Non_unique'] == 0, uq
    assert uq[0]['Index_type'] == 'UNIQUE-CHECK', uq
    assert col_meta(ex, 'u')['name']['Key'] == 'UNI'
    ddl = ok(ex, 'SHOW CREATE TABLE u')['rows'][0][1]
    assert 'UNIQUE KEY uq_name (name)' in ddl, ddl
    # 写入重复值 1062
    r = err(ex, "INSERT INTO u VALUES (3,'x',2,2)")
    assert r['code'] == 1062, r
    # 多列唯一：前缀重复允许，整组重复 1062
    ok(ex, 'ALTER TABLE u ADD UNIQUE INDEX uq_ab (a, b)')
    ok(ex, 'INSERT INTO u VALUES (4, "z", 1, 3)')
    r = err(ex, 'INSERT INTO u VALUES (5, "w", 1, 2)')
    assert r['code'] == 1062, r
    # 已有数据重复 → 1062（定义期拒绝，不写入假约束）
    ok(ex, 'CREATE TABLE ud (id INT PRIMARY KEY, n VARCHAR(10))')
    ok(ex, "INSERT INTO ud VALUES (1,'x'),(2,'x')")
    r = err(ex, 'ALTER TABLE ud ADD UNIQUE (n)')
    assert r['code'] == 1062, r
    # 重名 1061
    r = err(ex, 'ALTER TABLE u ADD UNIQUE KEY uq_name (a)')
    assert r['code'] == 1061, r
    # DROP INDEX：唯一约束真的消失，可再次重建
    ok(ex, 'ALTER TABLE u DROP INDEX uq_name')
    assert not any(r['Key_name'] == 'uq_name'
                   for r in index_rows(ex, 'u'))
    ok(ex, 'ALTER TABLE u ADD UNIQUE KEY uq_name (name)')
    r = err(ex, 'ALTER TABLE u DROP INDEX no_such')
    assert r['code'] == 1091, r
    print("\n    ADD UNIQUE：命名/多列、旧数据与写入重复均 1062、重名 1061；"
          "DROP INDEX 后约束消失并可重建")


# ==================== 重启 / COMPACT / 备份 ====================

def test_pk_unique_restart_compact(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_pk')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, code INT, name VARCHAR(10))')
        ok(ex, 'INSERT INTO t VALUES (1, 42, "a"), (2, 7, "b")')
        ok(ex, 'ALTER TABLE t DROP PRIMARY KEY')
        ok(ex, 'ALTER TABLE t ADD PRIMARY KEY (code)')
        ok(ex, 'ALTER TABLE t ADD UNIQUE KEY uq_name (name)')
        ok(ex, 'INSERT INTO t VALUES (3, 8, "c")')
        db.checkpoint()
        db.compact(tables=['t'])
        assert rows(ex, 'SELECT code, name FROM t ORDER BY code') == \
            [(7, 'b'), (8, 'c'), (42, 'a')]
        assert col_meta(ex, 't')['code']['Key'] == 'PRI'
        assert any(r['Key_name'] == 'uq_name' for r in index_rows(ex, 't'))
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert rows(ex2, 'SELECT code, name FROM t ORDER BY code') == \
            [(7, 'b'), (8, 'c'), (42, 'a')]
        assert col_meta(ex2, 't')['code']['Key'] == 'PRI'
        assert any(r['Key_name'] == 'uq_name' for r in index_rows(ex2, 't'))
        r = err(ex2, 'INSERT INTO t VALUES (9, 7, "d")')
        assert r['code'] == 1062, r
        r = err(ex2, 'INSERT INTO t VALUES (9, 9, "a")')
        assert r['code'] == 1062, r
    finally:
        db2.shutdown()
    print("\n    重启/COMPACT：换列主键、UNIQUE 索引、数据与冲突检测全部保持")


def test_pk_unique_backup_restore(tmp_path):
    from lightdb.backup.backup import BackupManager
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    src_dir = str(tmp_path / 'src')
    dst_dir = str(tmp_path / 'dst')
    bak = str(tmp_path / 'bak')
    src = Database(src_dir)
    ex = Executor(src)
    try:
        ok(ex, 'CREATE TABLE b (id INT, code INT, name VARCHAR(10))')
        ok(ex, 'INSERT INTO b VALUES (1, 42, "a"), (2, 7, "b")')
        ok(ex, 'ALTER TABLE b ADD PRIMARY KEY (code)')
        ok(ex, 'ALTER TABLE b ADD UNIQUE KEY uq_name (name)')
        ok(ex, 'INSERT INTO b VALUES (3, 8, "c")')
        # 另一张表显式 DROP PRIMARY KEY：备份 DDL 不应凭空写回主键
        ok(ex, 'CREATE TABLE pd (id INT PRIMARY KEY, v INT)')
        ok(ex, 'INSERT INTO pd VALUES (1, 100), (2, 200)')
        ok(ex, 'ALTER TABLE pd DROP PRIMARY KEY')
        dump = BackupManager(src, bak).dump()
        before = rows(ex, 'SELECT code, name FROM b ORDER BY code')
        before_pd = rows(ex, 'SELECT * FROM pd ORDER BY id')
    finally:
        src.shutdown()
    dst = Database(dst_dir)
    try:
        ok_n, fail = BackupManager(dst, bak).restore(dump)
        assert fail == 0, fail
        ex2 = Executor(dst)
        cols = col_meta(ex2, 'b')
        assert cols['code']['Key'] == 'PRI', cols
        assert cols['name']['Key'] == 'UNI', cols
        assert rows(ex2, 'SELECT code, name FROM b ORDER BY code') == before
        assert rows(ex2, 'SELECT * FROM pd ORDER BY id') == before_pd
        assert 'PRIMARY KEY' not in \
            ok(ex2, 'SHOW CREATE TABLE pd')['rows'][0][1]
        r = err(ex2, 'INSERT INTO b VALUES (4, 42, "x")')
        assert r['code'] == 1062, r
        r = err(ex2, 'INSERT INTO b VALUES (4, 9, "a")')
        assert r['code'] == 1062, r
        print("\n    备份/恢复：换列主键 + UNIQUE 索引 + 数据全部往返一致")
    finally:
        dst.shutdown()


def test_add_primary_key_survives_hard_crash(tmp_path):
    """ADD PRIMARY KEY 返回后立即 os._exit(1)：重启必须看到完整生效的元数据"""
    data_dir = str(tmp_path / 'crash_data')
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    child = tmp_path / 'crash_child.py'
    child.write_text(
        'import os, sys\n'
        f'sys.path.insert(0, {ROOT!r})\n'
        'from lightdb.db.database import Database\n'
        'from lightdb.sql.executor import Executor\n'
        f'db = Database({data_dir!r})\n'
        'ex = Executor(db)\n'
        "print(ex.execute_sql('ALTER TABLE t ADD PRIMARY KEY (id)', "
        "{})['type'])\n"
        "print(ex.execute_sql('ALTER TABLE r ADD PRIMARY KEY (code)', "
        "{})['type'])\n"
        'os._exit(1)\n', encoding='utf-8')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE t (id INT, v INT)')
        ok(ex, 'INSERT INTO t VALUES (1,10),(2,20)')
        ok(ex, 'CREATE TABLE r (id INT, code INT)')
        ok(ex, 'INSERT INTO r VALUES (1, 42), (2, 7)')
    finally:
        db.shutdown()
    p = subprocess.run([sys.executable, str(child)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert col_meta(ex2, 't')['id']['Key'] == 'PRI'
        assert rows(ex2, 'SELECT * FROM t ORDER BY id') == [(1, 10), (2, 20)]
        r = err(ex2, 'INSERT INTO t VALUES (1, 99)')
        assert r['code'] == 1062, r
        # 换列重键也在返回前完成 flush/根指针/元数据落盘
        assert col_meta(ex2, 'r')['code']['Key'] == 'PRI'
        assert rows(ex2, 'SELECT id, code FROM r ORDER BY code') == \
            [(2, 7), (1, 42)]
        r = err(ex2, 'INSERT INTO r VALUES (3, 42)')
        assert r['code'] == 1062, r
        print("\n    硬崩溃恢复：ADD PRIMARY KEY（含换列重键）后 os._exit，"
              "重启主键/索引/数据完整")
    finally:
        db2.shutdown()
