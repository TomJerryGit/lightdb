# -*- coding: utf-8 -*-
"""task-72 验收：复合主键 + LIKE 前缀索引

复合主键（2 列整数）：物理 B+Tree 键 = order-preserving 64 位编码
`(a+2^31)<<32 | (b+2^31)`，因此 `WHERE a=? AND b=?` 是精确点查、
`WHERE a=?` 是键范围扫描；重复元组 1062、NULL 1048、SHOW/重启/COMPACT/
崩溃恢复/备份恢复一致。

LIKE 前缀索引：`CREATE INDEX idx(col)` 后 `col LIKE 'abc%'` 用索引首列
有序键做范围扫描；`%abc%` / `_` / 纯等值形态保持全表扫描；大小写敏感
（与 WHERE LIKE 求值一致），结果与全表扫描逐行一致。
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


# ==================== 复合主键 ====================

def test_create_composite_primary_key(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE cp (a INT, b INT, v VARCHAR(10), '
           'PRIMARY KEY (a, b))')
    cols = col_meta(ex, 'cp')
    assert cols['a']['Key'] == 'PRI' and cols['b']['Key'] == 'PRI', cols
    assert cols['a']['Null'] == 'NO' and cols['b']['Null'] == 'NO', cols
    pri = [r for r in index_rows(ex, 'cp') if r['Key_name'] == 'PRIMARY']
    assert [(r['Seq_in_index'], r['Column_name']) for r in pri] == \
        [(1, 'a'), (2, 'b')], pri
    assert all(r['Index_type'] == 'BTREE' for r in pri), pri
    ok(ex, "INSERT INTO cp VALUES (1, 1, 'x'), (1, 2, 'y'), (2, 1, 'z')")
    # 第一列重复、第二列不同 → 合法；完全相同 → 1062
    r = err(ex, "INSERT INTO cp VALUES (1, 1, 'dup')")
    assert r['code'] == 1062, r
    # 任一主键列为 NULL → 1048
    assert err(ex, "INSERT INTO cp VALUES (NULL, 3, 'n')")['code'] == 1048
    assert err(ex, "INSERT INTO cp VALUES (3, NULL, 'n')")['code'] == 1048
    # 点查 / 前缀 / 前缀范围
    assert rows(ex, 'SELECT * FROM cp WHERE a=1 AND b=2') == [(1, 2, 'y')]
    assert rows(ex, 'SELECT * FROM cp WHERE a=1 ORDER BY b') == \
        [(1, 1, 'x'), (1, 2, 'y')]
    assert rows(ex, 'SELECT * FROM cp WHERE a=1 AND b>1') == [(1, 2, 'y')]
    stats = _db.get_table('cp').index_usage_stats()
    assert stats.get('pk_composite', 0) > 0, stats
    ddl = ok(ex, 'SHOW CREATE TABLE cp')['rows'][0][1]
    assert 'PRIMARY KEY (a, b)' in ddl, ddl
    print(f"\n    复合主键 CREATE：PRI 两列、点查/前缀/范围可用、重复 1062、"
          f"NULL 1048（pk_composite={stats['pk_composite']}）")


def test_composite_pk_negative_values(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE neg (a INT, b INT, v INT, PRIMARY KEY (a, b))')
    ok(ex, 'INSERT INTO neg VALUES (-2, -3, 1), (-1, 0, 2), (0, 0, 3), '
           '(1, -1, 4)')
    assert rows(ex, 'SELECT v FROM neg WHERE a=-2 AND b=-3') == [(1,)]
    assert rows(ex, 'SELECT v FROM neg WHERE a<=0 ORDER BY a, b') == \
        [(1,), (2,), (3,)]
    r = err(ex, 'INSERT INTO neg VALUES (0, 0, 9)')
    assert r['code'] == 1062, r
    print("\n    复合主键有符号编码：负值/0 点查与前缀范围正确")


def test_alter_add_drop_composite_pk(env):
    _db, ex = env
    # seq 是隐式内部行键（列值必须唯一），(a,b) 允许有重复候选以便测 1062
    ok(ex, 'CREATE TABLE cpa (seq INT, a INT, b INT, v INT)')
    ok(ex, 'INSERT INTO cpa VALUES (1,1,1,10),(2,1,2,20),(3,2,1,30)')
    ok(ex, 'ALTER TABLE cpa ADD PRIMARY KEY (a, b)')
    assert col_meta(ex, 'cpa')['a']['Key'] == 'PRI'
    assert col_meta(ex, 'cpa')['b']['Key'] == 'PRI'
    assert [r for r in index_rows(ex, 'cpa')
            if r['Key_name'] == 'PRIMARY'] != []
    r = err(ex, 'INSERT INTO cpa VALUES (4, 1, 1, 99)')
    assert r['code'] == 1062, r
    ok(ex, 'ALTER TABLE cpa DROP PRIMARY KEY')
    assert col_meta(ex, 'cpa')['a']['Key'] == ''
    assert 'PRIMARY KEY' not in ok(ex, 'SHOW CREATE TABLE cpa')['rows'][0][1]
    assert rows(ex, 'SELECT a,b,v FROM cpa ORDER BY a, b') == \
        [(1, 1, 10), (1, 2, 20), (2, 1, 30)]
    ok(ex, 'ALTER TABLE cpa ADD PRIMARY KEY (a, b)')
    assert rows(ex, 'SELECT a,b,v FROM cpa WHERE a=1 ORDER BY b') == \
        [(1, 1, 10), (1, 2, 20)]
    print("\n    ALTER ADD/DROP 复合主键：SHOW 同步、数据可读、可再 ADD")


def test_alter_composite_pk_errors(env):
    _db, ex = env
    # 已有重复元组 → 1062（seq 是唯一内部键，(a,b) 重复）
    ok(ex, 'CREATE TABLE cpd (seq INT, a INT, b INT)')
    ok(ex, 'INSERT INTO cpd VALUES (1,1,1),(2,1,1),(3,2,2)')
    r = err(ex, 'ALTER TABLE cpd ADD PRIMARY KEY (a, b)')
    assert r['code'] == 1062, r
    # NULL 元组 → 1138
    ok(ex, 'CREATE TABLE cpn (seq INT, a INT, b INT)')
    ok(ex, 'INSERT INTO cpn VALUES (1,1,NULL)')
    r = err(ex, 'ALTER TABLE cpn ADD PRIMARY KEY (a, b)')
    assert r['code'] == 1138, r
    # 5 列 → 1235（v3 最多 4 列）
    ok(ex, 'CREATE TABLE cp5 (a INT, b INT, c INT, d INT, e INT)')
    r = err(ex, 'ALTER TABLE cp5 ADD PRIMARY KEY (a, b, c, d, e)')
    assert r['code'] == 1235, r
    # 已有显式主键 → 1068
    ok(ex, 'CREATE TABLE cpk (a INT PRIMARY KEY, b INT)')
    r = err(ex, 'ALTER TABLE cpk ADD PRIMARY KEY (a, b)')
    assert r['code'] == 1068, r
    print("\n    复合主键错误码：重复 1062 / NULL 1138 / 5 列 1235 / "
          "已有主键 1068")


def test_composite_pk_restart_compact_and_crash(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'cp_data')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE cp (a INT, b INT, v VARCHAR(10), '
               'PRIMARY KEY (a,b))')
        ok(ex, 'INSERT INTO cp VALUES (1,1,\'x\'),(1,2,\'y\'),(2,1,\'z\')')
        db.checkpoint()
        db.compact(tables=['cp'])
        assert rows(ex, 'SELECT * FROM cp WHERE a=1 ORDER BY b') == \
            [(1, 1, 'x'), (1, 2, 'y')]
        r = err(ex, "INSERT INTO cp VALUES (1,1,'dup')")
        assert r['code'] == 1062, r
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert rows(ex2, 'SELECT * FROM cp WHERE a=1 AND b=2') == \
            [(1, 2, 'y')]
        assert rows(ex2, 'SELECT * FROM cp WHERE a=1 ORDER BY b') == \
            [(1, 1, 'x'), (1, 2, 'y')]
        assert col_meta(ex2, 'cp')['b']['Key'] == 'PRI'
        r = err(ex2, "INSERT INTO cp VALUES (1,1,'dup')")
        assert r['code'] == 1062, r
    finally:
        db2.shutdown()

    # 硬崩溃：子进程插入后直接 os._exit(1)，重开必须完整恢复
    child = tmp_path / 'crash_cp.py'
    child.write_text(
        'import os, sys\n'
        f'sys.path.insert(0, {ROOT!r})\n'
        'from lightdb.db.database import Database\n'
        'from lightdb.sql.executor import Executor\n'
        f'db = Database({data_dir!r})\n'
        'ex = Executor(db)\n'
        "print(ex.execute_sql(\"INSERT INTO cp VALUES (3,3,'c')\", "
        "{})['type'])\n"
        'os._exit(1)\n', encoding='utf-8')
    p = subprocess.run([sys.executable, str(child)], capture_output=True,
                       text=True, timeout=120)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    db3 = Database(data_dir)
    ex3 = Executor(db3)
    try:
        assert rows(ex3, 'SELECT * FROM cp ORDER BY a,b') == \
            [(1, 1, 'x'), (1, 2, 'y'), (2, 1, 'z'), (3, 3, 'c')]
        r = err(ex3, "INSERT INTO cp VALUES (1,1,'dup')")
        assert r['code'] == 1062, r
        print("\n    复合主键：checkpoint/COMPACT/重启/硬崩溃后一致")
    finally:
        db3.shutdown()


def test_composite_pk_backup_restore(tmp_path):
    from lightdb.backup.backup import BackupManager
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    src = Database(str(tmp_path / 'src'))
    bak = str(tmp_path / 'bak')
    ex = Executor(src)
    try:
        ok(ex, 'CREATE TABLE cp (a INT, b INT, v VARCHAR(10), '
               'PRIMARY KEY (a,b))')
        ok(ex, "INSERT INTO cp VALUES (1,1,'x'),(1,2,'y'),(2,1,'z')")
        dump = BackupManager(src, bak).dump()
        before = rows(ex, 'SELECT * FROM cp ORDER BY a,b')
    finally:
        src.shutdown()
    dst = Database(str(tmp_path / 'dst'))
    try:
        ok_n, fail = BackupManager(dst, bak).restore(dump)
        assert fail == 0, fail
        ex2 = Executor(dst)
        assert rows(ex2, 'SELECT * FROM cp ORDER BY a,b') == before
        assert col_meta(ex2, 'cp')['a']['Key'] == 'PRI'
        pri = [r for r in index_rows(ex2, 'cp')
               if r['Key_name'] == 'PRIMARY']
        assert [(r['Seq_in_index'], r['Column_name']) for r in pri] == \
            [(1, 'a'), (2, 'b')], pri
        r = err(ex2, "INSERT INTO cp VALUES (1,1,'dup')")
        assert r['code'] == 1062, r
        print("\n    复合主键备份/恢复：DDL/数据/唯一性/索引全部往返一致")
    finally:
        dst.shutdown()


# ==================== LIKE 前缀索引 ====================

def _seed_like(ex, n=3000):
    ok(ex, 'CREATE TABLE lt (id INT PRIMARY KEY, name VARCHAR(30), '
           'cat INT)')
    vals = []
    for i in range(n):
        vals.append(f"({i},'row{i:05d}',{i % 10})")
    for start in range(0, n, 500):
        ok(ex, 'INSERT INTO lt VALUES '
               + ','.join(vals[start:start + 500]))
    ok(ex, "INSERT INTO lt VALUES (9001,'AbcOne',1),(9002,'abcTWO',2),"
           "(9003,'ABCUPPER',3)")
    ok(ex, 'CREATE INDEX idx_name ON lt (name)')


def _force_scan(sql, tbl, ex):
    saved = list(tbl._secondary_indexes)
    try:
        tbl._secondary_indexes = []
        tbl._secondary_dirty = True
        return rows(ex, sql)
    finally:
        tbl._secondary_indexes = saved
        tbl._secondary_dirty = True


def test_like_prefix_uses_index_and_matches_scan(env):
    _db, ex = env
    _seed_like(ex)
    tbl = _db.get_table('lt')
    # 先 warm 一次（首次查询会建索引映射）
    rows(ex, "SELECT id FROM lt WHERE name LIKE 'row001%'")
    before = tbl.index_usage_stats()['like_prefix']
    sql = "SELECT id FROM lt WHERE name LIKE 'row001%'"
    got = rows(ex, sql)
    exp = _force_scan(sql, tbl, ex)
    assert got == exp
    assert len(got) == 100, len(got)
    assert tbl.index_usage_stats()['like_prefix'] > before
    # 前缀 + 其它 AND 条件仍走索引，结果一致
    sql2 = ("SELECT id FROM lt WHERE name LIKE 'row001%' AND cat = 3 "
            "ORDER BY id")
    assert rows(ex, sql2) == _force_scan(sql2, tbl, ex)
    # 多列索引的最左列同样可用
    ok(ex, 'CREATE INDEX idx_name_cat ON lt (name, cat)')
    sql3 = "SELECT id FROM lt WHERE name LIKE 'row002%' ORDER BY id"
    assert rows(ex, sql3) == _force_scan(sql3, tbl, ex)
    print(f"\n    LIKE 'row001%'：索引前缀扫描 = 全扫逐行一致（100 行），"
          f"like_prefix 计数生效")


def test_like_prefix_fallback_and_case(env):
    _db, ex = env
    _seed_like(ex)
    tbl = _db.get_table('lt')
    # %x% / 无通配 / 中间 % → 全表扫描，结果正确
    for sql in ("SELECT id FROM lt WHERE name LIKE '%One'",
                "SELECT id FROM lt WHERE name LIKE '%row001%'",
                "SELECT id FROM lt WHERE name LIKE 'row0010'",
                "SELECT id FROM lt WHERE name LIKE 'r_w0010%'"):
        assert rows(ex, sql) == _force_scan(sql, tbl, ex), sql
    before = tbl.index_usage_stats()['fallbacks']
    rows(ex, "SELECT id FROM lt WHERE name LIKE '%row001%'")
    assert tbl.index_usage_stats()['fallbacks'] > before
    # 大小写敏感：'Abc%' 只匹配 AbcOne；'abc%' 只匹配 abcTWO；'ABC%' 只匹配 ABCUPPER
    assert rows(ex, "SELECT id FROM lt WHERE name LIKE 'Abc%'") == [(9001,)]
    assert rows(ex, "SELECT id FROM lt WHERE name LIKE 'abc%'") == [(9002,)]
    assert rows(ex, "SELECT id FROM lt WHERE name LIKE 'ABC%'") == [(9003,)]
    print("\n    LIKE 回退：%x%/中间通配/无通配全扫一致；前缀大小写敏感")


def test_like_prefix_restart_compact(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'like_data')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        _seed_like(ex, 1500)
        db.checkpoint()
        db.compact(tables=['lt'])
        expected = rows(ex, "SELECT id FROM lt WHERE name LIKE 'row001%'")
        assert len(expected) == 100
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        got = rows(ex2, "SELECT id FROM lt WHERE name LIKE 'row001%'")
        assert got == expected
        tbl = db2.get_table('lt')
        tbl.index_usage_stats()['like_prefix']
        before = tbl.index_usage_stats()['like_prefix']
        rows(ex2, "SELECT id FROM lt WHERE name LIKE 'row002%'")
        assert tbl.index_usage_stats()['like_prefix'] > before
        print("\n    LIKE 前缀索引：重启/COMPACT 后索引重建且前缀加速仍生效")
    finally:
        db2.shutdown()
