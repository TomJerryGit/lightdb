"""列级 `col ... UNIQUE`：从"声明了但不生效"改成**真强制**

锁定的契约（MySQL 语义）：

1. `CREATE TABLE t (email VARCHAR(50) UNIQUE)` 等价于自动建一个
   **名为列名** 的唯一索引（`email`）；
2. 重复 INSERT / UPDATE → **1062**，报文含列名与取值
   （`Duplicate entry 'x' for key 'email'`）；
3. 多个 NULL 允许共存（NULL 不参与唯一性判定）；
4. 多值 INSERT 中途冲突 → 整条语句 **0 行**（复用语句原子性）；
5. `SHOW INDEX` 如实列出（`Non_unique=0`、`Index_type='UNIQUE-CHECK'`，
   不谎称 BTREE）；`SHOW CREATE TABLE` 内联 `UNIQUE` 且可往返；
6. `_meta.json` 记录 `origin='column'` 的 `unique_indexes` 条目，重启后
   仍强制；
7. 老 `_meta.json` 只有 `cols[i].unique=true`、没有 `unique_indexes`
   条目时，打开后自动推导出唯一索引并继续强制；
8. 与表级 `UNIQUE KEY` 共存时不重复报错、两条约束都生效；
9. `ALTER TABLE ... ADD COLUMN ... UNIQUE` 同样立刻强制；若用非 NULL
   DEFAULT 回填多行会制造重复，则定义期直接 1062，不留下坏结构。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_column_unique.py
    from conftest import track


# ==================== 公共工具 ====================

def open_db(data_dir):
    from lightdb.db.database import Database
    return track(Database(str(data_dir)))


def open_executor(db):
    from lightdb.sql.executor import Executor
    return track(Executor(db))


def sql(db, statement, session=None):
    from lightdb.sql.executor import Executor
    return Executor(db).execute_sql(
        statement, {} if session is None else session)


def ok(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'ok', (statement, r)
    return r


def err(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'error', (statement, r)
    return r


def rs(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'resultset', (statement, r)
    return r


def index_rows(db, table):
    r = rs(db, f'SHOW INDEX FROM {table}')
    return [dict(zip(r['columns'], row)) for row in r['rows']]


def show_create(db, table):
    return rs(db, f'SHOW CREATE TABLE {table}')['rows'][0][1]


def column_names(db, table):
    return [c['name'] for c in db.get_table(table).cols]


@pytest.fixture()
def env(tmp_path):
    db = open_db(tmp_path / 'data')
    return db, open_executor(db)


# ==================== 1. 解析 / 注册 ====================

def test_parser_marks_column_unique_and_create_registers_equivalent_spec(
        tmp_path):
    from lightdb.sql.parser import parser
    p = parser.parse(
        'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE, '
        'note VARCHAR(10))')
    assert p['columns'][1]['unique'] is True
    assert p['columns'][2]['unique'] is False
    # 列级 UNIQUE 不是表级声明：解析结果的 unique_indexes 仍为空
    assert p['unique_indexes'] == [], p.get('unique_indexes')

    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, '
           'email VARCHAR(50) UNIQUE, note VARCHAR(10))')
    tbl = db.get_table('t')
    assert tbl.unique_indexes() == [
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}]
    assert tbl.table_unique_indexes() == []
    assert tbl.unique_columns() == {'email'}
    print(f"  → 列级 UNIQUE 注册为 {tbl.unique_indexes()}")


# ==================== 2. INSERT / UPDATE 1062 ====================

def test_insert_duplicate_returns_1062_with_column_name(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a@x.com')")
    r = err(db, "INSERT INTO t VALUES (2, 'a@x.com')")
    assert r['code'] == 1062, r
    assert "Duplicate entry 'a@x.com' for key 'email'" in r['message'], r
    assert rs(db, 'SELECT id, email FROM t')['rows'] == [(1, 'a@x.com')]
    print(f"  → 重复 INSERT {r['code']}: {r['message'][:80]}")


def test_update_duplicate_returns_1062_and_changes_nothing(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    r = err(db, "UPDATE t SET email='a' WHERE id=2")
    assert r['code'] == 1062, r
    assert "Duplicate entry 'a' for key 'email'" in r['message'], r
    assert rs(db, 'SELECT id, email FROM t')['rows'] == [(1, 'a'), (2, 'b')]

    # 一条 UPDATE 把多行改成同一个值 → 语句内部冲突，同样 1062 且 0 行改动
    r = err(db, "UPDATE t SET email='z'")
    assert r['code'] == 1062, r
    assert "'z'" in r['message'], r
    assert rs(db, 'SELECT id, email FROM t')['rows'] == [(1, 'a'), (2, 'b')]

    ok(db, "UPDATE t SET email='c' WHERE id=2")
    assert rs(db, 'SELECT id, email FROM t')['rows'] == [(1, 'a'), (2, 'c')]
    print(f"  → UPDATE 冲突 {r['code']}，非重复 UPDATE 正常")


def test_multiple_null_values_coexist(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, 'INSERT INTO t VALUES (1, NULL), (2, NULL), (3, NULL)')
    ok(db, "INSERT INTO t VALUES (4, 'x')")
    assert rs(db, 'SELECT COUNT(*) FROM t')['rows'] == [(4,)]
    r = err(db, "INSERT INTO t VALUES (5, 'x')")
    assert r['code'] == 1062, r
    print(f"  → 多个 NULL 共存，非 NULL 重复仍 {r['code']}")


def test_multi_row_insert_is_atomic_on_column_unique_conflict(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a')")
    r = err(db, "INSERT INTO t VALUES (2, 'b'), (3, 'a'), (4, 'c')")
    assert r['code'] == 1062, r
    # 整条语句 0 行：b / c 也不能留下
    assert rs(db, 'SELECT id, email FROM t')['rows'] == [(1, 'a')]
    print(f"  → 多值 INSERT 中途冲突 {r['code']}，整条语句 0 行")


# ==================== 3. SHOW INDEX / SHOW CREATE 往返 ====================

def test_show_index_lists_column_unique_as_unique_check(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, NULL)")
    rows = {d['Key_name']: d for d in index_rows(db, 't')}
    assert set(rows) == {'PRIMARY', 'email'}, rows
    email = rows['email']
    assert email['Non_unique'] == 0, email
    assert email['Column_name'] == 'email' and email['Seq_in_index'] == 1, email
    assert email['Index_type'] == 'UNIQUE-CHECK', email      # 不谎称 BTREE
    assert email['Cardinality'] == 2, email                  # 不同非 NULL 值
    print(f"  → SHOW INDEX email: Non_unique={email['Non_unique']}, "
          f"Index_type={email['Index_type']}, "
          f"Cardinality={email['Cardinality']}")


def test_show_create_table_roundtrips_column_unique(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, '
           'email VARCHAR(50) UNIQUE, note VARCHAR(10))')
    ok(db, "INSERT INTO t VALUES (1, 'a', 'n')")
    ddl = show_create(db, 't')
    # 列级 UNIQUE 内联；不能同时再写一条同名的表级 UNIQUE KEY（会 1061）
    assert 'email VARCHAR(50) NULL UNIQUE' in ddl, ddl
    assert 'UNIQUE KEY email (email)' not in ddl, ddl

    copy = ddl.replace('CREATE TABLE t', 'CREATE TABLE t_copy', 1)
    ok(db, copy)
    tbl = db.get_table('t_copy')
    assert [c['unique'] for c in tbl.cols if c['name'] == 'email'] == [True]
    assert tbl.unique_indexes() == [
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}]
    # 照抄重建出来的表同样强制
    ok(db, "INSERT INTO t_copy VALUES (1, 'a', 'n')")
    r = err(db, "INSERT INTO t_copy VALUES (2, 'a', 'm')")
    assert r['code'] == 1062 and "'email'" in r['message'], r
    print(f"  → SHOW CREATE 内联 UNIQUE 并可往返；重建后重复 INSERT "
          f"{r['code']}")


# ==================== 4. 持久化 / 老元数据兼容 ====================

def test_column_unique_survives_restart(tmp_path):
    data = tmp_path / 'data'
    db = open_db(data)
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a')")
    with open(os.path.join(str(data), '_meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    assert meta['tables']['t']['unique_indexes'] == [
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}], \
        meta['tables']['t'].get('unique_indexes')
    db.shutdown()

    db2 = open_db(data)
    assert db2.get_table('t').unique_indexes() == [
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}]
    rows = {d['Key_name']: d for d in index_rows(db2, 't')}
    assert rows['email']['Index_type'] == 'UNIQUE-CHECK', rows
    r = err(db2, "INSERT INTO t VALUES (2, 'a')")
    assert r['code'] == 1062, r
    db2.shutdown()
    print(f"  → 重启后仍强制；_meta.json unique_indexes="
          f"{meta['tables']['t']['unique_indexes']}")


def test_old_meta_without_unique_indexes_derives_column_unique(tmp_path):
    """老库：`_meta.json` 只有 `cols[i].unique=true`，没有 unique_indexes"""
    data = tmp_path / 'data'
    db = open_db(data)
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    ok(db, "INSERT INTO t VALUES (1, 'a')")
    db.shutdown()

    meta_path = os.path.join(str(data), '_meta.json')
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    meta['tables']['t'].pop('unique_indexes', None)      # 模拟老版本元数据
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    db2 = open_db(data)
    tbl = db2.get_table('t')
    assert tbl.unique_indexes() == [
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}]
    rows = {d['Key_name']: d for d in index_rows(db2, 't')}
    assert 'email' in rows and rows['email']['Index_type'] == 'UNIQUE-CHECK'
    r = err(db2, "INSERT INTO t VALUES (2, 'a')")
    assert r['code'] == 1062, r
    ok(db2, "INSERT INTO t VALUES (3, 'b')")
    db2.shutdown()
    print(f"  → 老元数据（无 unique_indexes）自动推导并强制：{r['code']}")


def test_old_meta_name_collision_keeps_ddl_restorable(tmp_path):
    """老元数据里表级索引名恰好占用了某个 UNIQUE 列的名字（旧版允许）

    加载时列级自动名（列名）优先，表级规格让名成 `a_2`；否则
    `SHOW CREATE TABLE` 会写出两个同名约束，照抄重建必 1061。
    """
    data = tmp_path / 'data'
    db = open_db(data)
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT UNIQUE, b INT)')
    db.shutdown()

    meta_path = os.path.join(str(data), '_meta.json')
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    # 模拟旧版：列级 UNIQUE 只记 unique=true，表级索引可以叫 a
    meta['tables']['t']['unique_indexes'] = [
        {'name': 'a', 'columns': ['b']}]
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    db2 = open_db(data)
    specs = db2.get_table('t').unique_indexes()
    assert {'name': 'a', 'columns': ['a'], 'origin': 'column'} in specs, specs
    assert {'name': 'a_2', 'columns': ['b']} in specs, specs
    ddl = show_create(db2, 't')
    assert 'a INT NULL UNIQUE' in ddl, ddl
    assert 'UNIQUE KEY a_2 (b)' in ddl and 'UNIQUE KEY a (b)' not in ddl, ddl
    ok(db2, ddl.replace('CREATE TABLE t', 'CREATE TABLE t_copy', 1))
    ok(db2, 'INSERT INTO t_copy VALUES (1, 1, 10)')
    r = err(db2, 'INSERT INTO t_copy VALUES (2, 1, 20)')
    assert r['code'] == 1062 and "'a'" in r['message'], r
    r = err(db2, 'INSERT INTO t_copy VALUES (3, 2, 10)')
    assert r['code'] == 1062 and "'a_2'" in r['message'], r
    db2.shutdown()
    print(f"  → 旧元数据撞名 → 表级改名 a_2；DDL 可恢复，两条约束都强制")


# ==================== 5. 与表级 UNIQUE 共存 ====================

def test_logical_dump_restore_preserves_column_unique(tmp_path):
    from lightdb.sql.logical_dump import (build_dump, check_ddl,
                                          restore_dump)
    src = open_db(tmp_path / 'src')
    ok(src, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE, '
            'a INT, UNIQUE KEY u (a))')
    ok(src, "INSERT INTO t VALUES (1, 'x', 10), (2, 'y', 20)")
    dump = build_dump(src, tables=['t'])
    assert 'email VARCHAR(50) NULL UNIQUE' in dump, dump
    assert 'UNIQUE KEY u (a)' in dump, dump

    dst = open_db(tmp_path / 'dst')
    ok_n, fail_n, report = restore_dump(dst, content=dump)
    assert fail_n == 0 and report.ok, report.errors
    assert check_ddl(dst, 't') == check_ddl(src, 't')
    tbl = dst.get_table('t')
    assert tbl.unique_indexes() == [
        {'name': 'u', 'columns': ['a']},
        {'name': 'email', 'columns': ['email'], 'origin': 'column'}], \
        tbl.unique_indexes()
    r = err(dst, "INSERT INTO t VALUES (3, 'x', 30)")
    assert r['code'] == 1062 and "'email'" in r['message'], r
    r = err(dst, "INSERT INTO t VALUES (4, 'z', 10)")
    assert r['code'] == 1062 and "'u'" in r['message'], r
    print(f"  → dump/restore 后两类 UNIQUE 都保留；重复 INSERT "
          f"{r['code']}（{ok_n} 条语句成功）")


def test_column_unique_coexists_with_table_level_unique_without_double_error(
        env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT UNIQUE, b INT, '
           'UNIQUE KEY u (a))')
    specs = db.get_table('t').unique_indexes()
    assert {'name': 'a', 'columns': ['a'], 'origin': 'column'} in specs, specs
    assert {'name': 'u', 'columns': ['a']} in specs, specs

    rows = {d['Key_name']: d for d in index_rows(db, 't')}
    assert set(rows) == {'PRIMARY', 'a', 'u'}, rows
    assert rows['a']['Index_type'] == 'UNIQUE-CHECK', rows['a']
    assert rows['u']['Index_type'] == 'UNIQUE-CHECK', rows['u']

    ok(db, 'INSERT INTO t VALUES (1, 10, 100)')
    r = err(db, 'INSERT INTO t VALUES (2, 10, 200)')
    assert r['code'] == 1062, r
    # 两条约束覆盖同一列：只报一次冲突，不能重复抛/重复计错
    assert r['message'].count('Duplicate entry') == 1, r
    ok(db, 'INSERT INTO t VALUES (2, 20, 100)')
    r = err(db, 'UPDATE t SET a=10 WHERE id=2')
    assert r['code'] == 1062, r
    assert rs(db, 'SELECT id, a, b FROM t')['rows'] == [(1, 10, 100),
                                                         (2, 20, 100)]
    print(f"  → 列级 a 与表级 u 共存，冲突只报一次：{r['code']}")


# ==================== 6. ALTER ADD COLUMN UNIQUE ====================

def test_alter_add_column_unique_is_enforced_immediately(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    ok(db, 'INSERT INTO t VALUES (1, 10), (2, 20)')
    ok(db, 'ALTER TABLE t ADD COLUMN email VARCHAR(50) UNIQUE')
    # 已有行回填 NULL，多个 NULL 合法
    assert rs(db, 'SELECT email FROM t')['rows'] == [(None,), (None,)]
    rows = {d['Key_name']: d for d in index_rows(db, 't')}
    assert 'email' in rows and rows['email']['Index_type'] == 'UNIQUE-CHECK'

    ok(db, "INSERT INTO t VALUES (3, 30, 'a')")
    r = err(db, "INSERT INTO t VALUES (4, 40, 'a')")
    assert r['code'] == 1062, r
    print(f"  → ALTER ADD COLUMN UNIQUE 立即强制：{r['code']}")


def test_alter_add_column_unique_with_duplicate_default_is_rejected(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    ok(db, 'INSERT INTO t VALUES (1, 10), (2, 20)')
    r = err(db, "ALTER TABLE t ADD COLUMN code VARCHAR(10) UNIQUE DEFAULT 'x'")
    assert r['code'] == 1062, r
    assert "'x'" in r['message'] and "'code'" in r['message'], r
    # 显式失败且不动结构
    assert 'code' not in column_names(db, 't')
    print(f"  → 非 NULL DEFAULT 回填会造成重复，定义期 {r['code']}："
          f"{r['message'][:70]}")


def test_drop_column_with_column_unique_is_still_guarded(env):
    db, _ex = env
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, email VARCHAR(50) UNIQUE)')
    r = err(db, 'ALTER TABLE t DROP COLUMN email')
    assert r['code'] == 1091, r
    assert 'UNIQUE' in r['message'], r
    print(f"  → DROP 列级 UNIQUE 列 → {r['code']}: {r['message'][:70]}")
