"""表级索引声明（`CREATE TABLE` 内的 PRIMARY KEY / UNIQUE）回归测试

本文件锁定 2026-10 收尾时交付的行为（此前这些声明会被**静默丢弃**，
或者被当成一根假列建出来）：

1. 表级单列 `PRIMARY KEY (col)` 真正成为主键 B+Tree，重复插入 1062；
2. 表级 `UNIQUE KEY/INDEX [名字] (col...)`（单列/多列）、
   `CONSTRAINT <名字> UNIQUE` 由写入路径**真正强制**：
   INSERT/UPDATE 冲突 → 1062（报文含键名/值），非重复写入正常；
3. 不支持的非唯一 `KEY/INDEX`、`FULLTEXT`、`SPATIAL`、`FOREIGN KEY`、
   复合 `PRIMARY KEY (a,b)`、前缀/表达式索引 → **显式 1064**，
   不静默丢弃、不建出假列；
4. 持久化：重启后唯一性仍强制，`SHOW INDEX` / `SHOW CREATE TABLE`
   与关闭前逐字一致，`_meta.json` 里能读到约束；
5. 崩溃安全：`os._exit(1)` 打断"重复值插入"或 DDL 后重开，不会出现
   半套状态（数据、索引、唯一约束三者一致）；
6. 并发：两个真实连接同时插入同一个唯一值，恰好一个成功、另一个 1062，
   表数据/索引无损坏；
7. 逻辑备份：`build_dump` → `restore_dump` 往返后唯一约束仍在。

约定：本引擎没有独立二级索引结构，表级 UNIQUE 的唯一性由写入路径
全表检查强制，因此 `SHOW INDEX.Index_type` 如实报 `UNIQUE-CHECK`
（**不谎称 BTREE**）；列级 `col ... UNIQUE` 仍保持原有口径（声明保留、
`SHOW COLUMNS.Key='UNI'`、写入路径不强制）——需要强制唯一性请用表级
`UNIQUE KEY`。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import require_thread_stacks, track
except ImportError:                       # 直接 python tests/test_index_declarations.py
    from conftest import require_thread_stacks, track


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================== 公共工具 ====================

def open_db(data_dir):
    from lightdb.db.database import Database
    return track(Database(str(data_dir)))


def open_executor(db):
    from lightdb.sql.executor import Executor
    return track(Executor(db))


def sql(db, statement, session=None):
    """进程内执行一条 SQL（用真实 Database + Executor）"""
    from lightdb.sql.executor import Executor
    return Executor(db).execute_sql(
        statement, {} if session is None else session)


def ok(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'ok', (statement, r)
    return r


def rs(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'resultset', (statement, r)
    return r


def err(db, statement, session=None):
    r = sql(db, statement, session)
    assert r.get('type') == 'error', (statement, r)
    return r


def dict_rows(result):
    return [dict(zip(result['columns'], row)) for row in result['rows']]


def index_rows(db, table):
    return dict_rows(rs(db, f'SHOW INDEX FROM {table}'))


def show_create(db, table):
    return rs(db, f'SHOW CREATE TABLE {table}')['rows'][0][1]


def column_keys(db, table):
    return {row[0]: row[3] for row in rs(db, f'SHOW COLUMNS FROM {table}')['rows']}


# ==================== 1. 表级单列主键 ====================

def test_table_level_single_column_primary_key_is_really_enforced(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE pk_t (id INT, a INT, PRIMARY KEY (a))')

    rows = index_rows(db, 'pk_t')
    assert len(rows) == 1, rows
    d = rows[0]
    assert (d['Key_name'], d['Column_name'], d['Non_unique'],
            d['Index_type']) == ('PRIMARY', 'a', 0, 'BTREE'), d
    ddl = show_create(db, 'pk_t')
    assert 'a INT NULL PRIMARY KEY' in ddl, ddl

    ok(db, 'INSERT INTO pk_t VALUES (1, 10)')
    r = err(db, 'INSERT INTO pk_t VALUES (2, 10)')
    assert r['code'] == 1062, r
    assert '10' in r['message'], r
    assert rs(db, 'SELECT * FROM pk_t')['rows'] == [(1, 10)]
    print(f"  → 表级 PRIMARY KEY (a): INSERT 重复 → {r['code']} {r['message']}")


# ==================== 2. UNIQUE 的等价声明写法 ====================

UNIQUE_FORMS = [
    ('UNIQUE KEY uq (a)', 'uq'),
    ('UNIQUE INDEX uq (a)', 'uq'),
    ('UNIQUE (a)', 'a'),
    ('CONSTRAINT uq UNIQUE (a)', 'uq'),
    ('CONSTRAINT uq UNIQUE KEY uk_a (a)', 'uk_a'),
]


@pytest.mark.parametrize('decl,key_name', UNIQUE_FORMS)
def test_unique_declaration_forms_are_enforced(tmp_path, decl, key_name):
    db = open_db(tmp_path / 'data')
    ok(db, f'CREATE TABLE t (id INT PRIMARY KEY, a INT, {decl})')

    rows = index_rows(db, 't')
    by_name = {d['Key_name']: d for d in rows}
    assert key_name in by_name, rows
    assert by_name[key_name]['Non_unique'] == 0, by_name[key_name]
    assert by_name[key_name]['Index_type'] == 'UNIQUE-CHECK', by_name[key_name]
    # 声明被原样写回 DDL（名字以解析/建表规则为准）
    ddl = show_create(db, 't')
    assert f'UNIQUE KEY {key_name} (a)' in ddl, ddl

    ok(db, 'INSERT INTO t VALUES (1, 7)')
    r = err(db, 'INSERT INTO t VALUES (2, 7)')
    assert r['code'] == 1062, (decl, r)
    assert 'Duplicate entry' in r['message'] and key_name in r['message'], r
    assert "'7'" in r['message'], r
    print(f"  → {decl} → key={key_name}，重复 INSERT {r['code']}: "
          f"{r['message']}")


def test_multiple_unique_constraints_are_all_enforced(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, '
           'UNIQUE KEY ua (a), UNIQUE KEY ub (b))')
    ok(db, 'INSERT INTO t VALUES (1, 10, 100)')

    r = err(db, 'INSERT INTO t VALUES (2, 10, 200)')
    assert r['code'] == 1062 and "'ua'" in r['message'], r
    r = err(db, 'INSERT INTO t VALUES (3, 30, 100)')
    assert r['code'] == 1062 and "'ub'" in r['message'], r
    ok(db, 'INSERT INTO t VALUES (4, 30, 300)')
    show = show_create(db, 't')
    assert 'UNIQUE KEY ua (a)' in show and 'UNIQUE KEY ub (b)' in show, show
    print(f"  → 一条表两个 UNIQUE 约束分别强制："
          f"{[d['Key_name'] for d in index_rows(db, 't')]}")


# ==================== 3. 多列 UNIQUE ====================

def test_multi_column_unique_is_supported_and_enforced(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, '
           'UNIQUE KEY u (a, b))')
    ddl = show_create(db, 't')
    assert 'UNIQUE KEY u (a, b)' in ddl, ddl

    ok(db, 'INSERT INTO t VALUES (1, 1, 10), (2, 1, 20)')
    r = err(db, 'INSERT INTO t VALUES (3, 1, 10)')
    assert r['code'] == 1062 and "'1-10'" in r['message'] and 'u' in r['message']
    print(f"  → 复合 UNIQUE 重复 INSERT {r['code']}: {r['message']}")

    # UPDATE 撞已有组合：必须 1062，且一行都不改
    r = err(db, 'UPDATE t SET b = 10 WHERE id = 2')
    assert r['code'] == 1062 and "'1-10'" in r['message'], r
    assert rs(db, 'SELECT * FROM t')['rows'] == [(1, 1, 10), (2, 1, 20)]

    # 非重复 UPDATE 正常
    r = ok(db, 'UPDATE t SET b = 30 WHERE id = 2')
    assert r['affected'] == 1, r
    ok(db, 'INSERT INTO t VALUES (3, 2, 30)')

    # 唯一键里含 NULL → 不参与唯一性（MySQL 语义：可多行共存）
    ok(db, 'INSERT INTO t VALUES (4, 1, NULL), (5, 1, NULL)')

    rows = index_rows(db, 't')
    u_rows = [d for d in rows if d['Key_name'] == 'u']
    assert [(d['Seq_in_index'], d['Column_name']) for d in u_rows] == [
        (1, 'a'), (2, 'b')], u_rows
    assert all(d['Index_type'] == 'UNIQUE-CHECK' for d in u_rows)
    print(f"  → 复合 UNIQUE 生效；SHOW INDEX u 展开为 "
          f"{[(d['Seq_in_index'], d['Column_name']) for d in u_rows]}，"
          f"全部 Index_type=UNIQUE-CHECK")


# ==================== 4. 复合主键：显式拒绝 ====================

@pytest.mark.parametrize('decl', [
    'PRIMARY KEY (a, b)',
    'CONSTRAINT pk_ab PRIMARY KEY (a, b)',
])
def test_composite_primary_key_is_explicit_1064(tmp_path, decl):
    db = open_db(tmp_path / 'data')
    r = err(db, f'CREATE TABLE cpk (a INT, b INT, {decl})')
    assert r['code'] == 1064, r
    assert '复合主键' in r['message'], r
    assert '单列' in r['message'], r
    # 报错后表**没有**被建出来（不做半套 DDL）
    assert err(db, 'SHOW INDEX FROM cpk')['code'] == 1146
    print(f"  → {decl} → {r['code']}: {r['message'][:110]}")


# ==================== 5. 不支持的索引形态：显式 1064 ====================

UNSUPPORTED_DECLS = [
    ('KEY k (a)', '二级索引'),
    ('INDEX k (a)', '二级索引'),
    ('FULLTEXT KEY ft (body)', 'FULLTEXT'),
    ('FULLTEXT (body)', 'FULLTEXT'),
    ('SPATIAL KEY sp (body)', 'SPATIAL'),
    ('FOREIGN KEY (a) REFERENCES other(id)', '外键'),
]


@pytest.mark.parametrize('decl,needle', UNSUPPORTED_DECLS)
def test_unsupported_index_declarations_are_explicit_1064(
        tmp_path, decl, needle):
    db = open_db(tmp_path / 'data')
    r = err(
        db,
        f'CREATE TABLE t (id INT PRIMARY KEY, a INT, body TEXT, {decl})')
    assert r['code'] == 1064, (decl, r)
    assert needle in r['message'], (decl, r)
    # 声明未被静默丢弃：表根本没有被建出来
    assert err(db, 'SHOW INDEX FROM t')['code'] == 1146, decl
    assert rs(db, 'SHOW TABLES')['rows'] == [] or \
        't' not in [row[0] for row in rs(db, 'SHOW TABLES')['rows']]
    print(f"  → {decl} → {r['code']}: {r['message'][:100]}")


# ==================== 6. 定义期错误（显式码 + 表不落地） ====================

DEFINITION_ERRORS = [
    ('CREATE TABLE t (id INT PRIMARY KEY, UNIQUE KEY u (nope))',
     1072, 'nope'),
    ('CREATE TABLE t (id INT PRIMARY KEY, a INT, '
     'UNIQUE KEY u (a), UNIQUE KEY u (id))', 1061, 'u'),
    ('CREATE TABLE t (id INT PRIMARY KEY, a INT, UNIQUE KEY u (a, a))',
     1060, '重复列'),
    ('CREATE TABLE t (id INT PRIMARY KEY, a INT, UNIQUE KEY u (a(2)))',
     1064, '前缀'),
    ('CREATE TABLE t (id INT PRIMARY KEY, a INT, '
     'UNIQUE KEY u (LOWER(a)))', 1064, '表达式'),
    ('CREATE TABLE t (id INT PRIMARY KEY, a INT, '
     'UNIQUE KEY u (a) USING HASH)', 1064, 'USING'),
]


@pytest.mark.parametrize('ddl,code,needle', DEFINITION_ERRORS)
def test_bad_unique_definitions_are_explicit_and_create_nothing(
        tmp_path, ddl, code, needle):
    db = open_db(tmp_path / 'data')
    r = err(db, ddl)
    assert r['code'] == code, (ddl, r)
    assert needle in r['message'], (ddl, r)
    assert err(db, 'SHOW INDEX FROM t')['code'] == 1146, ddl
    print(f"  → 坏定义 → {r['code']}: {r['message'][:100]}")


# ==================== 7. INSERT：1062 / 语句原子性 / 值释放 ====================

def test_unique_insert_conflict_1062_and_statement_atomicity(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t VALUES (1, 10)')

    r = err(db, 'INSERT INTO t VALUES (2, 20), (3, 10)')
    assert r['code'] == 1062 and 'u' in r['message'] and "'10'" in r['message']
    # 多值 INSERT 是原子的：冲突行之前那行也不能留下
    assert rs(db, 'SELECT * FROM t')['rows'] == [(1, 10)], r
    print(f"  → 多值 INSERT 冲突 {r['code']}，整条语句 0 行落地: "
          f"{rs(db, 'SELECT * FROM t')['rows']}")

    # DELETE 之后旧值可以重新使用
    ok(db, 'DELETE FROM t WHERE id = 1')
    ok(db, 'INSERT INTO t VALUES (4, 10)')
    # UPDATE 改走旧值之后，旧值也可以重新使用
    ok(db, 'UPDATE t SET a = 30 WHERE id = 4')
    ok(db, 'INSERT INTO t VALUES (5, 10)')
    ok(db, 'INSERT INTO t VALUES (6, 20)')
    print(f"  → DELETE / UPDATE 后旧值可重新使用: "
          f"{rs(db, 'SELECT * FROM t')['rows']}")


# ==================== 8. UPDATE：1062 与非重复更新 ====================

def test_update_unique_conflict_1062_and_non_duplicate_update_ok(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t VALUES (1, 10), (2, 20)')

    r = err(db, 'UPDATE t SET a = 10 WHERE id = 2')
    assert r['code'] == 1062, r
    assert 'Duplicate entry' in r['message'], r
    assert "'u'" in r['message'] and "'10'" in r['message'], r
    assert rs(db, 'SELECT * FROM t')['rows'] == [(1, 10), (2, 20)]
    print(f"  → UPDATE 改成重复值 → {r['code']}: {r['message']}")

    # 一条 UPDATE 把多行改成同一个值（语句内部互相冲突）→ 1062，且 0 行改动
    r = err(db, 'UPDATE t SET a = 7')
    assert r['code'] == 1062 and "'7'" in r['message'], r
    assert rs(db, 'SELECT * FROM t')['rows'] == [(1, 10), (2, 20)]
    print(f"  → 多行 UPDATE 内部冲突 → {r['code']}: {r['message']}")

    # 非重复 UPDATE 正常
    r = ok(db, 'UPDATE t SET a = 30 WHERE id = 2')
    assert r['affected'] == 1, r
    assert rs(db, 'SELECT * FROM t')['rows'] == [(1, 10), (2, 30)]
    # 更新成自己原值（不是与别人冲突）正常
    ok(db, 'UPDATE t SET a = 10 WHERE id = 1')
    # 原来的 20 已经释放
    ok(db, 'INSERT INTO t VALUES (3, 20)')
    # 改成 NULL 也允许（唯一索引里的 NULL 不参与唯一性，可多行共存）
    ok(db, 'UPDATE t SET a = NULL WHERE id = 1')
    ok(db, 'INSERT INTO t VALUES (7, NULL)')
    assert (1, None) in rs(db, 'SELECT * FROM t')['rows']
    print(f"  → 非重复 UPDATE OK，改走的旧值可复用；改成 NULL 正常: "
          f"{rs(db, 'SELECT * FROM t')['rows']}")


def test_update_unique_column_that_is_also_primary_key_reports_1062(
        tmp_path):
    """唯一列恰好也是（自动）主键时，冲突必须先报 1062，不能被 1105 盖掉

    `CREATE TABLE t2 (a INT, b INT, UNIQUE KEY u (a))` 的第一列会被引擎
    自动选为主键；`UPDATE t2 SET a=1 WHERE a=2` 既是"改主键"又是"撞唯一
    约束"。用户需要看到唯一冲突（键名 + 值），而不是一句与唯一性无关的
    通用 1105。无冲突的改主键仍受本引擎既有存储限制（B+Tree 不换 key）
    显式拒绝 —— 这是另一条限制，不是唯一性检查失败。
    """
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t2 (a INT, b INT, UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t2 VALUES (1, 10), (2, 20)')

    r = err(db, 'UPDATE t2 SET a = 1 WHERE a = 2')
    assert r['code'] == 1062, r
    assert 'Duplicate entry' in r['message'], r
    assert "'u'" in r['message'] and "'1'" in r['message'], r
    assert rs(db, 'SELECT * FROM t2')['rows'] == [(1, 10), (2, 20)]
    print(f"  → 唯一列==主键：重复 UPDATE → {r['code']}: {r['message']}")

    # 既有限制：主键本身不能改（显式 1105，绝不静默成功/半改）
    r = err(db, 'UPDATE t2 SET a = 99 WHERE a = 2')
    assert r['code'] == 1105 and '主键' in r['message'], r
    assert rs(db, 'SELECT * FROM t2')['rows'] == [(1, 10), (2, 20)]
    print(f"  → 非重复的**改主键**仍显式拒绝（既有存储限制）: "
          f"{r['code']} {r['message']}")


# ==================== 9. 持久化 / SHOW 元数据 ====================

def test_unique_constraint_survives_restart(tmp_path):
    data = tmp_path / 'data'
    db = open_db(data)
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, '
           'UNIQUE KEY u (a, b))')
    ok(db, 'INSERT INTO t VALUES (1, 1, 10), (2, 1, 20)')
    idx_before = rs(db, 'SHOW INDEX FROM t')['rows']
    ddl_before = show_create(db, 't')
    db.shutdown()

    db2 = open_db(data)
    assert rs(db2, 'SHOW INDEX FROM t')['rows'] == idx_before
    assert show_create(db2, 't') == ddl_before
    # 重启后唯一性仍然强制（INSERT / UPDATE 两条写入路径）
    r = err(db2, 'INSERT INTO t VALUES (3, 1, 10)')
    assert r['code'] == 1062 and "'1-10'" in r['message'], r
    r = err(db2, 'UPDATE t SET b = 20 WHERE id = 1')
    assert r['code'] == 1062 and "'1-20'" in r['message'], r
    ok(db2, 'INSERT INTO t VALUES (3, 1, 30)')

    with open(os.path.join(str(data), '_meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    assert meta['tables']['t']['unique_indexes'] == [
        {'name': 'u', 'columns': ['a', 'b']}], \
        meta['tables']['t'].get('unique_indexes')
    print(f"  → 重启后 SHOW INDEX / SHOW CREATE 逐字一致，"
          f"唯一性仍强制；_meta.json unique_indexes="
          f"{meta['tables']['t']['unique_indexes']}")


def test_show_create_roundtrip_recreates_the_same_unique_constraint(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, '
           'UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t VALUES (1, 10)')
    ddl = show_create(db, 't')
    ok(db, 'DROP TABLE t')
    ok(db, ddl)

    rows = index_rows(db, 't')
    assert [d['Key_name'] for d in rows] == ['PRIMARY', 'u'], rows
    assert rows[1]['Index_type'] == 'UNIQUE-CHECK'
    ok(db, 'INSERT INTO t VALUES (1, 10)')
    r = err(db, 'INSERT INTO t VALUES (2, 10)')
    assert r['code'] == 1062, r
    print(f"  → SHOW CREATE → DROP → 照抄重建：UNIQUE KEY u (a) 仍在，"
          f"重复 INSERT {r['code']}")


def test_show_index_reports_real_cardinality_and_does_not_fabricate(
        tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, '
           'UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t VALUES (1, 10), (2, 20), (3, NULL)')

    rows = {d['Key_name']: d for d in index_rows(db, 't')}
    assert rows['u']['Cardinality'] == 2, rows['u']   # 不同非 NULL 值 = 2
    assert rows['PRIMARY']['Cardinality'] == 3, rows['PRIMARY']
    assert rows['u']['Non_unique'] == 0
    assert rows['u']['Index_type'] == 'UNIQUE-CHECK'  # 不谎称 BTREE

    # 列级 UNIQUE 仍是"声明保留、不强制、不假装有索引"的原有口径
    ok(db, 'CREATE TABLE cu (id INT PRIMARY KEY, '
           'email VARCHAR(50) UNIQUE, a INT)')
    cu_rows = {d['Key_name'] for d in index_rows(db, 'cu')}
    assert cu_rows == {'PRIMARY'}, cu_rows
    assert column_keys(db, 'cu')['email'] == 'UNI'
    # 表级 UNIQUE 覆盖的列在 SHOW COLUMNS 里如实报 UNI
    assert column_keys(db, 't')['a'] == 'UNI'
    print(f"  → SHOW INDEX: u(Non_unique=0, Index_type=UNIQUE-CHECK, "
          f"Cardinality=2)，PRIMARY Cardinality=3；列级 UNIQUE 不编造索引行")


# ==================== 10. DDL 之后约束仍在 ====================

def test_unique_survives_alter_and_rename_and_blocks_drop_column(tmp_path):
    db = open_db(tmp_path / 'data')
    ok(db, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, '
           'UNIQUE KEY u (a))')
    ok(db, 'INSERT INTO t VALUES (1, 10, 1), (2, 20, 2)')

    # 被表级 UNIQUE 覆盖的列不能直接 DROP（不做"连带删约束"的静默破坏）
    r = err(db, 'ALTER TABLE t DROP COLUMN a')
    assert r['code'] == 1091, r
    assert 'UNIQUE' in r['message'], r
    print(f"  → DROP 被 UNIQUE 覆盖的列 → {r['code']}: {r['message']}")

    ok(db, 'ALTER TABLE t ADD COLUMN c INT DEFAULT 5')
    ok(db, 'ALTER TABLE t RENAME TO t2')
    rows = {d['Key_name']: d for d in index_rows(db, 't2')}
    assert 'u' in rows and rows['u']['Index_type'] == 'UNIQUE-CHECK', rows
    assert 'UNIQUE KEY u (a)' in show_create(db, 't2')
    r = err(db, 'INSERT INTO t2 VALUES (3, 10, 9, 9)')
    assert r['code'] == 1062, r
    ok(db, 'INSERT INTO t2 VALUES (3, 30, 9, 9)')
    print(f"  → ALTER ADD COLUMN / RENAME 后 UNIQUE 仍强制；"
          f"重复 INSERT {r['code']}")


# ==================== 11. 逻辑备份往返 ====================

def test_logical_dump_restore_preserves_unique_constraint(tmp_path):
    src = open_db(tmp_path / 'src')
    ok(src, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, '
            'UNIQUE KEY u (a, b))')
    ok(src, 'INSERT INTO t VALUES (1, 1, 10), (2, 1, 20)')

    from lightdb.sql.logical_dump import (build_dump, check_ddl,
                                          restore_dump)
    dump = build_dump(src, tables=['t'])
    assert 'UNIQUE KEY u (a, b)' in dump, dump
    ddl_src = check_ddl(src, 't')

    dst = open_db(tmp_path / 'dst')
    ok_n, fail_n, report = restore_dump(dst, content=dump)
    assert (ok_n, fail_n) == (4, 0), (ok_n, fail_n)
    assert report.ok, report.errors
    assert check_ddl(dst, 't') == ddl_src
    assert dst.get_table('t').unique_indexes() == [
        {'name': 'u', 'columns': ['a', 'b']}]
    r = err(dst, 'INSERT INTO t VALUES (3, 1, 10)')
    assert r['code'] == 1062 and "'1-10'" in r['message'], r
    ok(dst, 'INSERT INTO t VALUES (3, 1, 30)')
    print(f"  → 逻辑 dump 往返：restore ok={ok_n} fail={fail_n}，"
          f"恢复后重复 INSERT {r['code']}，DDL 一致")


# ==================== 12. 崩溃安全（真 os._exit(1) 子进程） ====================

CRASH_CHILD = r'''
import os
import sys

repo, point, data = sys.argv[1:4]
sys.path.insert(0, repo)

from lightdb.db.database import Database
from lightdb.db.table import Table
from lightdb.sql.executor import Executor

db = Database(data)
ex = Executor(db)


def run(statement):
    ex.execute_sql(statement, {})


run('CREATE TABLE t (id INT PRIMARY KEY, a INT, UNIQUE KEY u (a))')
run('INSERT INTO t VALUES (1, 10), (2, 20)')

if point == 'duplicate_insert':
    # 冲突已被识别、马上就要抛 1062 的瞬间硬杀：盘上不能留下半行
    def patched(self, spec, tup):
        os._exit(1)
    Table._raise_unique = patched
    run('INSERT INTO t VALUES (3, 10)')
elif point == 'ddl_add_column':
    # 结构变更意图已写进 _meta.json、行还没改完时硬杀：重开应补完 DDL
    orig = Database._ddl_begin

    def patched(self, entry):
        orig(self, entry)
        os._exit(1)
    Database._ddl_begin = patched
    run('ALTER TABLE t ADD COLUMN c INT DEFAULT 5')

os._exit(1)
'''


@pytest.mark.parametrize('point,expect_cols,expect_rows', [
    ('duplicate_insert', ['id', 'a'], [(1, 10), (2, 20)]),
    ('ddl_add_column', ['id', 'a', 'c'], [(1, 10, 5), (2, 20, 5)]),
])
def test_crash_recovery_keeps_unique_constraint_and_no_half_state(
        tmp_path, point, expect_cols, expect_rows):
    data = tmp_path / 'data'
    os.makedirs(str(data))
    script = tmp_path / 'crash_child.py'
    script.write_text(CRASH_CHILD, encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    p = subprocess.run(
        [sys.executable, '-X', 'utf8', str(script), REPO, point, str(data)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=env, timeout=300)
    assert p.returncode == 1, (point, p.returncode, p.stdout[-500:],
                               p.stderr[-500:])

    db = open_db(data)
    got_cols = [c['name'] for c in db.get_table('t').cols]
    assert got_cols == expect_cols, (point, got_cols)
    assert rs(db, f"SELECT {', '.join(expect_cols)} FROM t")['rows'] == \
        expect_rows, point

    rows = {d['Key_name']: d for d in index_rows(db, 't')}
    assert 'u' in rows and rows['u']['Index_type'] == 'UNIQUE-CHECK', rows
    assert 'UNIQUE KEY u (a)' in show_create(db, 't')
    if point == 'ddl_add_column':
        r = err(db, 'INSERT INTO t VALUES (3, 10, 9)')
    else:
        r = err(db, 'INSERT INTO t VALUES (3, 10)')
    assert r['code'] == 1062, r
    scan = db.get_table('t').integrity_scan(deep=True)
    assert not scan['dangling_prev'], scan
    assert scan['index_entries'] == len(expect_rows), scan
    print(f"  → 崩在 {point} 后重开：cols={got_cols} rows={expect_rows}，"
          f"唯一性仍强制（{r['code']}），无悬空版本")


# ==================== 13. 两连接并发插同一个唯一值 ====================

def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_concurrent_insert_same_unique_value_exactly_one_wins(tmp_path):
    pymysql = pytest.importorskip('pymysql')
    require_thread_stacks()
    from lightdb.config import Config
    from lightdb.net.server import MySQLServer

    data = tmp_path / 'data'
    db = open_db(data)
    ex = open_executor(db)
    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    srv = track(MySQLServer(cfg, db, ex))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    setup = track(pymysql.connect(
        host='127.0.0.1', port=port, user='root', password='',
        database='default', charset='utf8mb4', autocommit=True,
        connect_timeout=10))
    with setup.cursor() as cur:
        cur.execute('CREATE TABLE t (id INT PRIMARY KEY, u VARCHAR(20), '
                    'UNIQUE KEY uk (u))')
    setup.close()

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def worker(i):
        conn = None
        try:
            conn = pymysql.connect(
                host='127.0.0.1', port=port, user='root', password='',
                database='default', charset='utf8mb4', autocommit=True,
                connect_timeout=10)
            with conn.cursor() as cur:
                barrier.wait(timeout=60)
                try:
                    cur.execute(
                        'INSERT INTO t (id, u) VALUES (%s, %s)',
                        (i, 'same'))
                    with results_lock:
                        results.append(('ok', i))
                except Exception as e:              # noqa: BLE001
                    with results_lock:
                        results.append(('err', i, e.args))
        except Exception as e:                      # noqa: BLE001
            with results_lock:
                results.append(('fatal', i, repr(e)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    print(f'  → 两连接并发结果: {results}')

    wins = [x for x in results if x[0] == 'ok']
    losses = [x for x in results if x[0] == 'err']
    assert len(results) == 2, results
    assert len(wins) == 1, results
    assert len(losses) == 1, results
    assert losses[0][2][0] == 1062, losses
    assert 'Duplicate entry' in losses[0][2][1], losses
    print(f"  → 恰好一个成功、另一个 {losses[0][2][0]}: {losses[0][2][1]}")

    check = track(pymysql.connect(
        host='127.0.0.1', port=port, user='root', password='',
        database='default', charset='utf8mb4', autocommit=True,
        connect_timeout=10))
    with check.cursor() as cur:
        cur.execute('SELECT id, u FROM t')
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][1] == 'same', rows
        cur.execute('SELECT COUNT(*) FROM t WHERE u = %s', ('same',))
        assert cur.fetchone() == (1,), '同一个唯一值只能有一行'
    check.close()

    tbl = db.get_table('t')
    scan = tbl.integrity_scan(deep=True)
    assert not scan['dangling_prev'], scan
    assert scan['index_entries'] == 1, scan
    r = err(db, "INSERT INTO t VALUES (3, 'same')")
    assert r['code'] == 1062
    print(f"  → 最终数据 {rows}，完整性: 索引项={scan['index_entries']} "
          f"悬空={scan['dangling_prev'] or '无'}；后续重复插入仍 {r['code']}")
