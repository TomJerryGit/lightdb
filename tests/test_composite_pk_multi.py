# -*- coding: utf-8 -*-
"""task-80 验收：3–4 列复合主键（B+Tree v3，128 位 order-preserving 键）

编码：每个分量 signed-32 位 + 2^31 偏置后按 32 位大端拼接：
2 列 64 位、3 列 96 位、4 列 128 位；v3 页用 16 字节大端保存。因此
```
WHERE a=? AND b=? AND c=?       → 精确点查
WHERE a=?                       → 键范围（前缀）
WHERE a=? AND b=? AND c>?       → 前缀+范围
```
全部结果与全表扫描逐行一致；SHOW/备份恢复/重启/COMPACT/硬崩溃一致。
"""
import os
import subprocess
import sys
import tempfile
import shutil

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


def col_meta(ex, table):
    r = ok(ex, f'SHOW COLUMNS FROM {table}')
    return {row[0]: dict(zip(r['columns'], row)) for row in r['rows']}


def index_rows(ex, table):
    r = ok(ex, f'SHOW INDEX FROM {table}')
    return [dict(zip(r['columns'], row)) for row in r['rows']]


# ==================== CREATE / 访问路径 ====================

def test_create_three_column_primary_key(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE c3 (a INT, b INT, c INT, v VARCHAR(10), '
           'PRIMARY KEY (a, b, c))')
    cols = col_meta(ex, 'c3')
    for name in ('a', 'b', 'c'):
        assert cols[name]['Key'] == 'PRI', cols
        assert cols[name]['Null'] == 'NO', cols
    pri = [r for r in index_rows(ex, 'c3') if r['Key_name'] == 'PRIMARY']
    assert [(r['Seq_in_index'], r['Column_name']) for r in pri] == \
        [(1, 'a'), (2, 'b'), (3, 'c')], pri
    ok(ex, "INSERT INTO c3 VALUES (1,1,1,'x'),(1,1,2,'y'),"
           "(1,2,1,'z'),(2,1,1,'w')")
    r = err(ex, "INSERT INTO c3 VALUES (1,1,1,'dup')")
    assert r['code'] == 1062, r
    assert err(ex, "INSERT INTO c3 VALUES (1,1,NULL,'n')")['code'] == 1048
    assert rows(ex, 'SELECT * FROM c3 WHERE a=1 AND b=1 AND c=2') == \
        [(1, 1, 2, 'y')]
    assert rows(ex, 'SELECT * FROM c3 WHERE a=1 ORDER BY b,c') == \
        [(1, 1, 1, 'x'), (1, 1, 2, 'y'), (1, 2, 1, 'z')]
    assert rows(ex, 'SELECT * FROM c3 WHERE a=1 AND b=1 ORDER BY c') == \
        [(1, 1, 1, 'x'), (1, 1, 2, 'y')]
    assert rows(ex, 'SELECT * FROM c3 WHERE a=1 AND b=1 AND c>1') == \
        [(1, 1, 2, 'y')]
    stats = _db.get_table('c3').index_usage_stats()
    assert stats.get('pk_composite', 0) > 0, stats
    ddl = ok(ex, 'SHOW CREATE TABLE c3')['rows'][0][1]
    assert 'PRIMARY KEY (a, b, c)' in ddl, ddl
    print(f"\n    3 列复合主键：PRI×3、点查/前缀/前缀范围可用、重复 1062、"
          f"NULL 1048（pk_composite={stats['pk_composite']}）")


def test_create_four_column_primary_key_and_order(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE c4 (a INT, b INT, c INT, d INT, v INT, '
           'PRIMARY KEY (a, b, c, d))')
    # 乱序插入 + 负值，验证 order-preserving（点查/前缀/范围）
    ok(ex, "INSERT INTO c4 VALUES (2,1,1,1,10),(1,2,3,4,20),"
           "(1,2,3,5,30),(1,-1,0,9,40),(-1,0,0,0,50)")
    assert rows(ex, 'SELECT * FROM c4 WHERE a=1 AND b=2 AND c=3 AND d=4') == \
        [(1, 2, 3, 4, 20)]
    assert rows(ex, 'SELECT v FROM c4 WHERE a=1 AND b=2 ORDER BY c,d') == \
        [(20,), (30,)]
    assert rows(ex, 'SELECT v FROM c4 WHERE a=1 AND b=2 AND c=3 AND d>4') == \
        [(30,)]
    assert rows(ex, 'SELECT v FROM c4 WHERE a<1 ORDER BY a,b,c,d') == \
        [(50,)]
    r = err(ex, "INSERT INTO c4 VALUES (1,2,3,4,99)")
    assert r['code'] == 1062, r
    pri = [r for r in index_rows(ex, 'c4') if r['Key_name'] == 'PRIMARY']
    assert [r['Column_name'] for r in pri] == ['a', 'b', 'c', 'd'], pri
    print("\n    4 列复合主键：64/96/128 位顺序编码，点查/前缀/范围/负值正确")


def test_composite_type_and_column_count_errors(env):
    _db, ex = env
    # 分量必须是 signed-32 位整数：VARCHAR / BIGINT 显式拒绝
    r = err(ex, 'CREATE TABLE bad1 (a INT, b VARCHAR(10), '
                'PRIMARY KEY (a, b))')
    assert r['code'] == 1064 and 'signed-32' in r['message'], r
    r = err(ex, 'CREATE TABLE bad2 (a INT, b BIGINT, '
                'PRIMARY KEY (a, b))')
    assert r['code'] == 1064 and 'signed-32' in r['message'], r
    # 5 列超过 v3 键容量 → 显式 1064，不截断
    r = err(ex, 'CREATE TABLE bad3 (a INT, b INT, c INT, d INT, e INT, '
                'PRIMARY KEY (a,b,c,d,e))')
    assert r['code'] == 1064 and '4 列' in r['message'], r
    # 不存在列 / 重复列
    r = err(ex, 'CREATE TABLE bad4 (a INT, b INT, PRIMARY KEY (a, nope))')
    assert r['code'] == 1072 or r['code'] == 1064, r
    r = err(ex, 'CREATE TABLE bad5 (a INT, b INT, PRIMARY KEY (a, a))')
    assert r['code'] == 1064, r
    print("\n    3–4 列限制：非 signed-32 分量 1064、5 列 1064、重复/缺列显式")


# ==================== ALTER ====================

def test_alter_add_drop_three_and_four_column_pk(env):
    _db, ex = env
    ok(ex, 'CREATE TABLE a3 (seq INT, a INT, b INT, c INT, v INT)')
    ok(ex, 'INSERT INTO a3 VALUES (1,1,1,1,10),(2,1,1,2,20),'
           '(3,1,2,1,30)')
    ok(ex, 'ALTER TABLE a3 ADD PRIMARY KEY (a, b, c)')
    assert col_meta(ex, 'a3')['c']['Key'] == 'PRI'
    assert rows(ex, 'SELECT v FROM a3 WHERE a=1 AND b=1 ORDER BY c') == \
        [(10,), (20,)]
    r = err(ex, 'INSERT INTO a3 VALUES (4,1,1,1,99)')
    assert r['code'] == 1062, r
    ok(ex, 'ALTER TABLE a3 DROP PRIMARY KEY')
    assert col_meta(ex, 'a3')['a']['Key'] == ''
    ok(ex, 'ALTER TABLE a3 ADD PRIMARY KEY (a, b, c)')
    assert rows(ex, 'SELECT v FROM a3 WHERE a=1 ORDER BY b,c') == \
        [(10,), (20,), (30,)]

    ok(ex, 'CREATE TABLE a4 (seq INT, a INT, b INT, c INT, d INT, v INT)')
    ok(ex, 'INSERT INTO a4 VALUES (1,1,2,3,4,10),(2,1,2,3,5,20)')
    ok(ex, 'ALTER TABLE a4 ADD PRIMARY KEY (a, b, c, d)')
    r = err(ex, 'INSERT INTO a4 VALUES (3,1,2,3,4,99)')
    assert r['code'] == 1062, r
    ok(ex, 'ALTER TABLE a4 DROP PRIMARY KEY')
    ok(ex, 'ALTER TABLE a4 ADD PRIMARY KEY (a, b, c, d)')
    assert rows(ex, 'SELECT v FROM a4 WHERE a=1 AND b=2 ORDER BY c,d') == \
        [(10,), (20,)]
    print("\n    ALTER 3/4 列 ADD/DROP/再 ADD：SHOW/唯一性/访问路径一致")


# ==================== 重启 / COMPACT / 硬崩溃 ====================

def test_multi_pk_restart_compact_crash(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'multi_data')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        ok(ex, 'CREATE TABLE c3 (a INT, b INT, c INT, v INT, '
               'PRIMARY KEY (a,b,c))')
        ok(ex, 'CREATE TABLE c4 (a INT, b INT, c INT, d INT, v INT, '
               'PRIMARY KEY (a,b,c,d))')
        ok(ex, 'INSERT INTO c3 VALUES (1,1,1,10),(1,1,2,20),(2,1,1,30)')
        ok(ex, 'INSERT INTO c4 VALUES (1,2,3,4,40),(1,2,3,5,50)')
        db.checkpoint()
        db.compact(tables=['c3', 'c4'])
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        assert rows(ex2, 'SELECT v FROM c3 WHERE a=1 AND b=1 ORDER BY c') == \
            [(10,), (20,)]
        assert rows(ex2, 'SELECT v FROM c4 WHERE a=1 AND b=2 AND c=3 '
                         'ORDER BY d') == [(40,), (50,)]
    finally:
        db2.shutdown()

    child = tmp_path / 'crash_multi.py'
    child.write_text(
        'import os, sys\n'
        f'sys.path.insert(0, {ROOT!r})\n'
        'from lightdb.db.database import Database\n'
        'from lightdb.sql.executor import Executor\n'
        f'db = Database({data_dir!r})\n'
        'ex = Executor(db)\n'
        "print(ex.execute_sql('INSERT INTO c3 VALUES (3,3,3,99)', "
        "{})['type'])\n"
        "print(ex.execute_sql('INSERT INTO c4 VALUES (2,2,2,2,88)', "
        "{})['type'])\n"
        'os._exit(1)\n', encoding='utf-8')
    p = subprocess.run([sys.executable, str(child)], capture_output=True,
                       text=True, timeout=120, encoding='utf-8',
                       errors='replace')
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    db3 = Database(data_dir)
    ex3 = Executor(db3)
    try:
        assert rows(ex3, 'SELECT v FROM c3 ORDER BY a,b,c') == \
            [(10,), (20,), (30,), (99,)]
        assert rows(ex3, 'SELECT v FROM c4 ORDER BY a,b,c,d') == \
            [(40,), (50,), (88,)]
        r = err(ex3, 'INSERT INTO c4 VALUES (1,2,3,4,0)')
        assert r['code'] == 1062, r
        print("\n    3/4 列复合主键：重启/COMPACT/硬崩溃后一致")
    finally:
        db3.shutdown()


# ==================== 备份恢复 ====================

def test_multi_pk_backup_restore(tmp_path):
    from lightdb.backup.backup import BackupManager
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    src = Database(str(tmp_path / 'src'))
    ex = Executor(src)
    bak = str(tmp_path / 'bak')
    try:
        ok(ex, 'CREATE TABLE c3 (a INT, b INT, c INT, v INT, '
               'PRIMARY KEY (a,b,c))')
        ok(ex, 'CREATE TABLE c4 (a INT, b INT, c INT, d INT, v INT, '
               'PRIMARY KEY (a,b,c,d))')
        ok(ex, 'INSERT INTO c3 VALUES (1,1,1,10),(1,1,2,20)')
        ok(ex, 'INSERT INTO c4 VALUES (1,2,3,4,40),(2,2,2,2,50)')
        dump = BackupManager(src, bak).dump()
        before3 = rows(ex, 'SELECT * FROM c3 ORDER BY a,b,c')
        before4 = rows(ex, 'SELECT * FROM c4 ORDER BY a,b,c,d')
    finally:
        src.shutdown()
    dst = Database(str(tmp_path / 'dst'))
    try:
        ok_n, fail = BackupManager(dst, bak).restore(dump)
        assert fail == 0, fail
        ex2 = Executor(dst)
        assert rows(ex2, 'SELECT * FROM c3 ORDER BY a,b,c') == before3
        assert rows(ex2, 'SELECT * FROM c4 ORDER BY a,b,c,d') == before4
        assert col_meta(ex2, 'c3')['b']['Key'] == 'PRI'
        assert col_meta(ex2, 'c4')['d']['Key'] == 'PRI'
        r = err(ex2, 'INSERT INTO c3 VALUES (1,1,1,0)')
        assert r['code'] == 1062, r
        r = err(ex2, 'INSERT INTO c4 VALUES (1,2,3,4,0)')
        assert r['code'] == 1062, r
        print("\n    3/4 列复合主键：备份恢复 DDL/数据/唯一性一致")
    finally:
        dst.shutdown()
