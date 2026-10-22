"""阶段 3 · P1：DDL 缺口（ALTER ADD/DROP COLUMN、TRUNCATE、SHOW CREATE TABLE、SHOW INDEX）

本文件锁定的是**用户天天用、此前直接失败或返回 None** 的那几件事，因此
每一条都同时覆盖四件事（缺一条就等于"看起来做了、其实没做"）：

1. **真正生效**：行数据真的被改写（不是只改 `cols` 元数据）；
2. **持久化**：重启（新 `Database` 实例）后结构与数据一致；
3. **崩溃安全**：DDL 中途 `os._exit(1)`（真子进程硬杀）后重开，结果只会是
   "完整生效"或"完全没生效"，不会停在半套；
4. **经协议可用**：真实 `MySQLServer` + pymysql 走一遍，返回报文/结果集与
   进程内一致。

外加两条工程约束：

* **不许静默部分成功**：所有未实现的 ALTER 变体必须显式报错（错误码 +
  明确报文），且表结构**一点都没变**；
* **GUI 兼容**：`lightdb_studio` 消费这些语句的约定必须被满足
  （`SHOW CREATE TABLE` 取 `rows[0][1]`、`SHOW INDEX` 按列名找
  `Key_name` / `Column_name` / `Seq_in_index`）。GUI 代码只读不改。

现有元数据口径（`SHOW COLUMNS` 的 Type/Default/Extra，主键 `Null=NO`，
`Extra` 恒空串）在本文件里也做了回归断言，防止 P1 把阶段 1 的诚实化改回去。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_ddl_p1.py
    from conftest import safe_rmtree, track


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8931


# ==================== 公共工具 ====================

def open_db(data_dir):
    from lightdb.db.database import Database
    return track(Database(str(data_dir)))


def executor(db):
    from lightdb.sql.executor import Executor
    return track(Executor(db))


def sql(db, statement, session=None):
    """进程内执行一条 SQL（用真实 Database + Executor）

    注意：会话字典必须原样传下去 —— 不能写 `session or {}`，空 dict 是
    falsy，`BEGIN` 写进去的 `txn` 会落在一个临时字典里丢掉（`execute_sql`
    的注释里已记录过这个坑）。
    """
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


def names(db, table):
    return [c['name'] for c in db.get_table(table).cols]


def rows_of(db, table, columns='*', session=None):
    r = rs(db, f'SELECT {columns} FROM {table}', session)
    return r['columns'], r['rows']


BASE_DDL = (
    "CREATE TABLE t ("
    " id INT PRIMARY KEY,"
    " name VARCHAR(20) NOT NULL DEFAULT 'anon',"
    " secret VARCHAR(40),"
    " age INT,"
    " CONSTRAINT age_pos CHECK (age > 0))"
)


def seed(db):
    ok(db, BASE_DDL)
    ok(db, "INSERT INTO t (id, name, secret, age) VALUES "
           "(1, 'a', 'S3CRET-ONE', 5)")
    ok(db, "INSERT INTO t (id, name, secret, age) VALUES "
           "(2, 'b', 'S3CRET-TWO', 6)")
    ok(db, "INSERT INTO t (id, name, secret, age) VALUES "
           "(3, 'c', 'S3CRET-THREE', 7)")


@pytest.fixture()
def fresh(tmp_path):
    """一个建好并填了数据的库（每个用例独立目录）"""
    db = open_db(tmp_path / 'data')
    seed(db)
    return db


# ==================== 1. SHOW CREATE TABLE：内容必须真实 ====================

def test_show_create_table_has_real_types_defaults_unique_and_checks():
    """`SHOW CREATE TABLE` 必须含真实类型（带长度/精度）、NOT NULL、DEFAULT、
    PRIMARY KEY、UNIQUE、CHECK，并且与 `check_ddl` 是**同一份**文本"""
    import tempfile
    d = tempfile.mkdtemp(prefix='p1_ddl_')
    try:
        db = open_db(d)
        ok(db, "CREATE TABLE pricey ("
               " id INT PRIMARY KEY,"
               " name VARCHAR(20) NOT NULL DEFAULT 'anon',"
               " price DECIMAL(10,2) DEFAULT 9.99,"
               " email VARCHAR(60) UNIQUE,"
               " qty INT,"
               " CONSTRAINT qty_ok CHECK (qty >= 0))")
        r = rs(db, 'SHOW CREATE TABLE pricey')
        assert r['columns'] == ['Table', 'Create Table'], r['columns']
        assert len(r['rows']) == 1
        table_col, ddl = r['rows'][0]
        assert table_col == 'pricey'
        print('  → SHOW CREATE TABLE pricey:')
        for line in ddl.splitlines():
            print(f'      {line}')

        # 真实列类型（长度/精度不能被抹掉）
        assert 'name VARCHAR(20) NOT NULL DEFAULT' in ddl, ddl
        assert 'price DECIMAL(10,2)' in ddl, ddl
        # NOT NULL / DEFAULT / PRIMARY KEY / UNIQUE / CHECK 全部在位
        assert 'id INT' in ddl and 'PRIMARY KEY' in ddl, ddl
        assert "DEFAULT 'anon'" in ddl, ddl
        assert 'DEFAULT 9.99' in ddl, ddl
        assert 'UNIQUE' in ddl, ddl
        assert 'CHECK (qty >= 0)' in ddl, ddl

        # 与 `check_ddl`（逻辑 dump / 备份共用的唯一实现）逐字符一致
        from lightdb.sql.logical_dump import check_ddl
        assert ddl == check_ddl(db, 'pricey'), '两条 DDL 生成路径漂移了'

        # 表不存在 → 1146（不是空结果集）
        r = err(db, 'SHOW CREATE TABLE nope')
        assert r['code'] == 1146, r
        print(f"  → 不存在的表: {r['code']} {r['message'][:60]}")
        db.shutdown()
    finally:
        safe_rmtree(d)


def test_show_create_table_roundtrips_through_the_parser():
    """`SHOW CREATE TABLE` 的输出必须是**可执行**的：照抄它去建同构表，
    列名/列序/类型/默认值/约束一条不少（旧实现漏 DEFAULT/UNIQUE，
    且把长度抹成 `VARCHAR`）"""
    import tempfile
    d = tempfile.mkdtemp(prefix='p1_rt_')
    try:
        db = open_db(d)
        ok(db, "CREATE TABLE src ("
               " id INT PRIMARY KEY,"
               " name VARCHAR(20) NOT NULL DEFAULT 'anon',"
               " price DECIMAL(10,2) DEFAULT 9.99,"
               " email VARCHAR(60) UNIQUE,"
               " CONSTRAINT price_ok CHECK (price > 0))")
        ddl = rs(db, 'SHOW CREATE TABLE src')['rows'][0][1]
        copy = ddl.replace('CREATE TABLE src', 'CREATE TABLE src2', 1)
        ok(db, copy)
        a = {c['name']: (c['type'], c['notnull'], c['has_default'],
                         c['default'], c['unique'], c['primary'])
             for c in db.get_table('src').cols}
        b = {c['name']: (c['type'], c['notnull'], c['has_default'],
                         c['default'], c['unique'], c['primary'])
             for c in db.get_table('src2').cols}
        assert a == b, (a, b)
        assert [c['expr'] for c in db.get_table('src2').check_constraints()] == \
            [c['expr'] for c in db.get_table('src').check_constraints()]
        print(f'  → 照抄 DDL 建出的 src2 与 src 结构完全一致: {sorted(a)}')
        db.shutdown()
    finally:
        safe_rmtree(d)


# ==================== 2. SHOW INDEX：列形状与语义 ====================

def test_show_index_shape_and_values(fresh):
    """`SHOW INDEX FROM t` 的列名/列序按 MySQL；主键索引一行，取值真实"""
    r = rs(fresh, 'SHOW INDEX FROM t')
    assert r['columns'] == [
        'Table', 'Non_unique', 'Key_name', 'Seq_in_index', 'Column_name',
        'Collation', 'Cardinality', 'Sub_part', 'Packed', 'Null',
        'Index_type', 'Comment', 'Index_comment', 'Visible', 'Expression'], \
        r['columns']
    print(f"  → 列: {r['columns']}")
    for row in r['rows']:
        print(f"  → 行: {row}")
    assert len(r['rows']) == 1, r['rows']
    d = dict(zip(r['columns'], r['rows'][0]))
    assert d['Table'] == 't'
    assert d['Non_unique'] == 0            # 主键唯一
    assert d['Key_name'] == 'PRIMARY'
    assert d['Seq_in_index'] == 1
    assert d['Column_name'] == 'id'
    assert d['Index_type'] == 'BTREE'      # 真实实现：B+Tree（Scheme A）
    # Cardinality 是**真实**的索引条目数（不是编的估计值）
    assert d['Cardinality'] == 3, d

    # `SHOW KEYS` / `SHOW INDEXES` 是同一份实现
    for alias in ('SHOW KEYS FROM t', 'SHOW INDEXES FROM t'):
        r2 = rs(fresh, alias)
        assert r2['columns'] == r['columns']
        assert r2['rows'] == r['rows']

    # 表不存在 → 1146（客户端不能把"表没了"当成"没有索引"）
    r = err(fresh, 'SHOW INDEX FROM nope')
    assert r['code'] == 1146, r
    print(f"  → 不存在的表: {r['code']} {r['message'][:50]}")


def test_show_index_expands_multicolumn_index_into_multiple_rows(fresh):
    """一个索引多列 → 多行（`Seq_in_index` 递增）

    本引擎目前**没有**能建多列索引的 DDL（`ADD INDEX` 显式报错），所以这里
    直接把 `Table.index_specs()` 换成一个两列索引规格，验证 `SHOW INDEX`
    的展开逻辑本身（这是"列形状与语义对得上"里唯一无法用 SQL 触发的部分）。
    """
    tbl = fresh.get_table('t')
    real = tbl.index_specs
    try:
        tbl.index_specs = lambda: [{'name': 'PRIMARY', 'unique': True,
                                    'columns': ['id', 'name'],
                                    'cardinality': 3}]
        from lightdb.sql.executor import Executor
        r = Executor(fresh)._show_index({'table': 't'}, {})
    finally:
        tbl.index_specs = real
    assert r['type'] == 'resultset', r
    assert [(row[2], row[3], row[4]) for row in r['rows']] == [
        ('PRIMARY', 1, 'id'), ('PRIMARY', 2, 'name')], r['rows']
    print(f"  → 两列索引展开为 {len(r['rows'])} 行: {r['rows']}")


def test_show_index_does_not_fabricate_a_unique_index(fresh):
    """列级 `UNIQUE` 现在真强制（等价于"索引名 = 列名"的唯一索引），
    因此 `SHOW INDEX` 如实列出它，但 `Index_type` 报 `UNIQUE-CHECK`
    （不谎称 BTREE）；重复写入必须 1062"""
    ok(fresh, 'ALTER TABLE t ADD COLUMN email VARCHAR(60) UNIQUE')
    r = rs(fresh, 'SHOW INDEX FROM t')
    key_names = {row[2] for row in r['rows']}
    assert key_names == {'PRIMARY', 'email'}, r['rows']
    email = [row for row in r['rows'] if row[2] == 'email'][0]
    assert email[1] == 0, email                    # Non_unique=0
    assert email[4] == 'email', email              # Column_name
    assert email[10] == 'UNIQUE-CHECK', email      # 不谎称 BTREE
    # 列元数据里 UNIQUE 声明仍如实保留（SHOW COLUMNS 的 Key = UNI）
    cols = {row[0]: row for row in rs(fresh, 'SHOW COLUMNS FROM t')['rows']}
    assert cols['email'][3] == 'UNI', cols['email']
    ok(fresh, "INSERT INTO t (id, name, secret, age, email) "
              "VALUES (4, 'd', 's', 8, 'x@y.z')")
    dup = err(fresh, "INSERT INTO t (id, name, secret, age, email) "
                     "VALUES (5, 'e', 's', 9, 'x@y.z')")
    assert dup['code'] == 1062 and "'x@y.z'" in dup['message'], dup
    print(f"  → SHOW INDEX 列出列级 UNIQUE {key_names}，"
          f"email(Non_unique=0, UNIQUE-CHECK)；重复 INSERT {dup['code']}")


# ==================== 3. ALTER TABLE ... ADD COLUMN ====================

def test_add_column_backfills_with_default(fresh):
    """有 DEFAULT → 已有行按 DEFAULT 回填，并且真的写进了行载荷"""
    r = ok(fresh, 'ALTER TABLE t ADD COLUMN c INT DEFAULT 42')
    print(f"  → {r['message']}")
    assert names(fresh, 't') == ['id', 'name', 'secret', 'age', 'c']
    cols, rows = rows_of(fresh, 't')
    assert cols == ['id', 'name', 'secret', 'age', 'c']
    assert rows == [(1, 'a', 'S3CRET-ONE', 5, 42),
                    (2, 'b', 'S3CRET-TWO', 6, 42),
                    (3, 'c', 'S3CRET-THREE', 7, 42)], rows
    # 新插入的行也拿到同一个默认值（元数据与写入路径一致）
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES (4,'d','x',8)")
    assert rows_of(fresh, 't')[1][-1] == (4, 'd', 'x', 8, 42)
    # SHOW COLUMNS 的口径
    sc = {row[0]: row for row in rs(fresh, 'SHOW COLUMNS FROM t')['rows']}
    assert sc['c'][1] == 'INT' and sc['c'][4] == '42', sc['c']
    print(f"  → SELECT * FROM t: {rows_of(fresh, 't')[1]}")


def test_add_column_without_default_fills_null(fresh):
    """无 DEFAULT 且允许 NULL → 已有行回填 NULL（不是 0/''）"""
    ok(fresh, 'ALTER TABLE t ADD COLUMN note VARCHAR(50)')
    cols, rows = rows_of(fresh, 't')
    assert cols[-1] == 'note'
    assert all(row[-1] is None for row in rows), rows
    print(f"  → 回填 NULL: {rows}")


def test_add_column_not_null_without_default_is_explicit_error(fresh):
    """`NOT NULL` 且无 DEFAULT 且表非空 → 1138，**而且什么都没变**"""
    before_cols = names(fresh, 't')
    before_rows = rows_of(fresh, 't')[1]
    r = err(fresh, 'ALTER TABLE t ADD COLUMN x INT NOT NULL')
    assert r['code'] == 1138, r
    assert r['sqlstate'] == '22004', r
    print(f"  → {r['code']} {r['sqlstate']}: {r['message'][:110]}")
    # 不许"报错但结构已经动了"
    assert names(fresh, 't') == before_cols
    assert rows_of(fresh, 't')[1] == before_rows
    ddl = rs(fresh, 'SHOW CREATE TABLE t')['rows'][0][1]
    assert ' x ' not in ddl, ddl

    # 空表 + NOT NULL 无 DEFAULT：MySQL 允许（没有行需要回填）
    ok(fresh, 'CREATE TABLE empty_t (id INT PRIMARY KEY)')
    r = ok(fresh, 'ALTER TABLE empty_t ADD COLUMN x INT NOT NULL')
    assert names(fresh, 'empty_t') == ['id', 'x'], r
    print('  → 空表上 NOT NULL 无 DEFAULT 被允许（只需不回填任何行）')

    # NOT NULL 但带 DEFAULT → 允许
    ok(fresh, 'ALTER TABLE t ADD COLUMN y INT NOT NULL DEFAULT 7')
    assert all(row[-1] == 7 for row in rows_of(fresh, 't')[1])
    print('  → NOT NULL + DEFAULT 成功回填 7')


def test_add_column_error_codes(fresh):
    """列重复 → 1060；再加主键 → 1068"""
    r = err(fresh, 'ALTER TABLE t ADD COLUMN id INT')
    assert r['code'] == 1060, r
    print(f"  → {r['code']} {r['message']}")
    r = err(fresh, 'ALTER TABLE t ADD COLUMN p INT PRIMARY KEY')
    assert r['code'] == 1068, r
    print(f"  → {r['code']} {r['message']}")
    r = err(fresh, 'ALTER TABLE nope ADD COLUMN p INT')
    assert r['code'] == 1146, r
    print(f"  → {r['code']} {r['message']}")


def test_add_column_keeps_index_and_version_chain_intact(fresh):
    """加列不得破坏索引/版本链：先做多轮 UPDATE 造出长版本链，再加列，
    然后查完整性、查 SHOW INDEX 的条目数、并让 DELETE/UPDATE 继续可用"""
    for i in (1, 2, 3):
        # 每行两轮 UPDATE → 每行 3 个版本（活版本 + 2 个墓碑），
        # 版本链与索引都必须在这之后仍然自洽
        ok(fresh, f'UPDATE t SET age = {10 + i} WHERE id = {i}')
        ok(fresh, f'UPDATE t SET age = {20 + i} WHERE id = {i}')
    tbl = fresh.get_table('t')
    before = tbl.integrity_scan(deep=True)
    assert not before['dangling_prev'], before
    n_chain_before = before['chain_versions']
    print(f"  → 加列前: 索引项={before['index_entries']} "
          f"版本数={n_chain_before} 活跃行={before['live_rows']}")

    ok(fresh, 'ALTER TABLE t ADD COLUMN c INT DEFAULT 1')

    after = tbl.integrity_scan(deep=True)
    print(f"  → 加列后: 索引项={after['index_entries']} "
          f"版本数={after['chain_versions']} 活跃行={after['live_rows']} "
          f"悬空 prev={after['dangling_prev'] or '无'}")
    assert not after['dangling_prev'], after['dangling_prev']
    assert after['index_entries'] == before['index_entries']
    assert after['chain_versions'] == n_chain_before
    assert after['live_rows'] == before['live_rows']

    # 每个**历史版本**都必须补齐列数（否则老快照读回时列会错位）
    bad = []
    for pid in range(1, tbl.bp.total_pages()):
        page = tbl.bp.get_page(pid)
        if page is None:
            continue
        for _sid, rb in page.iter_rows():
            r = tbl._decode_row_ver(rb)
            if r is not None and len(r[0]) != 5:
                bad.append(r[0])
    assert not bad, bad
    print('  → 全部行版本（含墓碑/老版本）都是 5 列')

    # 索引指向的链头仍然可读
    assert rows_of(fresh, 't', 'id, age, c')[1] == [
        (1, 21, 1), (2, 22, 1), (3, 23, 1)]
    ok(fresh, 'DELETE FROM t WHERE id = 2')
    ok(fresh, "UPDATE t SET c = 99 WHERE id = 3")
    assert rows_of(fresh, 't', 'id, c')[1] == [(1, 1), (3, 99)]
    print(f"  → 加列后 DELETE/UPDATE 仍正常: {rows_of(fresh, 't', 'id, c')[1]}")


# ==================== 4. ALTER TABLE ... DROP COLUMN ====================

def test_drop_column_really_erases_the_data(fresh):
    """被删列的数据必须**真的不可再读到**：SQL 读不到，而且直接读数据文件
    也找不到原始字节（不是只从元数据隐藏）"""
    ok(fresh, 'TRUNCATE TABLE t')
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES "
              "(1, 'a', 'ZZSECRETZZ', 5)")
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES "
              "(2, 'b', 'ZZSECRETZZ', 6)")
    tbl = fresh.get_table('t')
    tbl.flush()
    blob = open(tbl.heap_path, 'rb').read()
    assert b'ZZSECRETZZ' in blob, '前置条件：删之前数据确实在数据文件里'

    r = ok(fresh, 'ALTER TABLE t DROP COLUMN secret')
    print(f"  → {r['message']}")
    assert names(fresh, 't') == ['id', 'name', 'age']
    cols, rows = rows_of(fresh, 't')
    assert cols == ['id', 'name', 'age'], cols
    assert rows == [(1, 'a', 5), (2, 'b', 6)], rows

    tbl.flush()
    blob = open(tbl.heap_path, 'rb').read()
    assert b'ZZSECRETZZ' not in blob, \
        '被删除列的数据仍然留在 .dat 里（只从元数据隐藏，不是真删）'
    wal = open(tbl.wal_path, 'rb').read() if os.path.exists(tbl.wal_path) else b''
    assert b'ZZSECRETZZ' not in wal, '被删除列的数据仍留在 WAL 里'
    print(f"  → .dat({len(blob)}B) 与 .wal({len(wal)}B) 里都不再有 "
          f"'ZZSECRETZZ'；SELECT 结果 = {rows}")
    assert not tbl.integrity_scan(deep=True)['dangling_prev']


def test_drop_column_guards_are_explicit(fresh):
    """主键 / UNIQUE / 被 CHECK 引用 / 分区列 / 最后一列 → 显式报错且不动结构"""
    before = names(fresh, 't')
    cases = [
        ('ALTER TABLE t DROP COLUMN id', 1091),          # 主键
        ('ALTER TABLE t DROP COLUMN age', 3959),         # 被 CHECK 引用
        ('ALTER TABLE t DROP COLUMN nope', 1054),        # 未知列
        ('ALTER TABLE nope DROP COLUMN id', 1146),       # 未知表
    ]
    for statement, code in cases:
        r = err(fresh, statement)
        assert r['code'] == code, (statement, r)
        print(f"  → {statement} → {r['code']}: {r['message'][:90]}")
    assert names(fresh, 't') == before, '报错路径改变了结构'

    # UNIQUE 列
    ok(fresh, 'ALTER TABLE t ADD COLUMN email VARCHAR(60) UNIQUE')
    r = err(fresh, 'ALTER TABLE t DROP COLUMN email')
    assert r['code'] == 1091, r
    print(f"  → UNIQUE 列 → {r['code']}: {r['message'][:90]}")

    # 最后一列（1090 Can't remove all the columns）
    ok(fresh, 'CREATE TABLE one_col (id INT PRIMARY KEY)')
    r = err(fresh, 'ALTER TABLE one_col DROP COLUMN id')
    assert r['code'] in (1090, 1091), r
    print(f"  → 最后一列 → {r['code']}: {r['message'][:90]}")


def test_drop_column_before_pk_shifts_pk_index_correctly(fresh):
    """删掉主键**前面**的列后，主键下标必须跟着前移（否则整表按错列做主键）"""
    ok(fresh, 'CREATE TABLE shifted (pre INT, id INT PRIMARY KEY, v INT)')
    ok(fresh, 'INSERT INTO shifted VALUES (100, 1, 10)')
    ok(fresh, 'INSERT INTO shifted VALUES (200, 2, 20)')
    ok(fresh, 'ALTER TABLE shifted DROP COLUMN pre')
    tbl = fresh.get_table('shifted')
    assert tbl.pk_col == 'id' and tbl.pk_idx == 0, (tbl.pk_col, tbl.pk_idx)
    assert rows_of(fresh, 'shifted')[1] == [(1, 10), (2, 20)]
    assert rs(fresh, 'SHOW INDEX FROM shifted')['rows'][0][4] == 'id'
    # 主键冲突检测仍按 id（不是按 v）
    r = err(fresh, 'INSERT INTO shifted VALUES (1, 99)')
    assert r['code'] == 1062, r
    print(f"  → 删列后 pk_col={tbl.pk_col} 下标={tbl.pk_idx}，"
          f"主键冲突检测仍生效: {r['message']}")


# ==================== 5. TRUNCATE TABLE ====================

def test_truncate_keeps_definition_and_can_insert_again(fresh):
    """清空所有行、**保留**表定义（列/约束/索引结构），并能继续插入"""
    before_ddl = rs(fresh, 'SHOW CREATE TABLE t')['rows'][0][1]
    r = ok(fresh, 'TRUNCATE TABLE t')
    print(f"  → {r['message']}")
    assert rows_of(fresh, 't')[1] == []
    assert rs(fresh, 'SELECT COUNT(*) FROM t')['rows'] == [(0,)]
    # 表定义逐字符不变（含 CHECK）
    assert rs(fresh, 'SHOW CREATE TABLE t')['rows'][0][1] == before_ddl
    # CHECK 仍然生效
    r = err(fresh, "INSERT INTO t (id, name, secret, age) VALUES (7,'x','s',-1)")
    assert r['code'] == 3819, r
    print(f"  → 清空后 CHECK 仍生效: {r['code']}")
    # 索引结构仍在（同一棵主键 B+Tree，条目数为 0）
    idx = rs(fresh, 'SHOW INDEX FROM t')['rows']
    assert len(idx) == 1 and idx[0][2] == 'PRIMARY' and idx[0][6] == 0, idx
    # 主键计数器复位（MySQL 的 AUTO_INCREMENT 重置语义）
    assert fresh.get_table('t')._next_pk == 1
    # 可以继续插入
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES (1,'z','s',3)")
    assert rows_of(fresh, 't')[1] == [(1, 'z', 's', 3)]
    print(f"  → 清空后重新插入成功: {rows_of(fresh, 't')[1]}")


def test_truncate_vs_delete_semantics(fresh):
    """TRUNCATE 与 DELETE 的差别（MySQL 语义）：前者是 DDL（隐式提交、
    不可回滚、重置主键计数器），后者是 DML（可回滚、不重置）"""
    tbl = fresh.get_table('t')
    # DELETE 可回滚（事务内必须用**同一个会话**读，否则读的是 autocommit 视图）
    s = {}
    ok(fresh, 'BEGIN', s)
    ok(fresh, 'DELETE FROM t', s)
    assert rows_of(fresh, 't', session=s)[1] == []   # 事务内自己看得到
    ok(fresh, 'ROLLBACK', s)
    assert len(rows_of(fresh, 't')[1]) == 3, 'DELETE 必须可回滚'

    # TRUNCATE 隐式提交：把它前面的写入一起提交掉（而不是留在事务里）
    s2 = {}
    ok(fresh, 'BEGIN', s2)
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES (9,'i','s',1)", s2)
    ok(fresh, 'TRUNCATE TABLE t', s2)            # DDL：隐式提交
    ok(fresh, 'ROLLBACK', s2)                    # 已经没有事务可回滚
    assert rows_of(fresh, 't')[1] == [], 'TRUNCATE 后必须仍然是空的'

    # 主键计数器：DELETE 后不复位，TRUNCATE 后复位
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES (5,'a','s',1)")
    ok(fresh, 'DELETE FROM t')
    assert tbl._next_pk == 6, tbl._next_pk
    ok(fresh, 'TRUNCATE TABLE t')
    assert tbl._next_pk == 1, tbl._next_pk
    print('  → DELETE 可回滚 / 不重置主键计数器；'
          'TRUNCATE 隐式提交 / 重置为 1')


def test_ddl_guard_refuses_when_another_txn_is_open(fresh):
    """表上存在**别的**未提交事务 → DDL 显式报错（本引擎没有元数据锁）"""
    s = {}
    ok(fresh, 'BEGIN', s)
    ok(fresh, "INSERT INTO t (id, name, secret, age) VALUES (4,'d','s',8)", s)
    r = err(fresh, 'ALTER TABLE t ADD COLUMN c INT')
    assert r['code'] == 1105 and '未提交事务' in r['message'], r
    print(f"  → ADD COLUMN: {r['code']} {r['message'][:80]}")
    r = err(fresh, 'TRUNCATE TABLE t')
    assert r['code'] == 1105 and '未提交事务' in r['message'], r
    print(f"  → TRUNCATE: {r['code']} {r['message'][:80]}")
    r = err(fresh, 'ALTER TABLE t DROP COLUMN secret')
    assert r['code'] == 1105 and '未提交事务' in r['message'], r
    # 结构/数据一点没动
    assert names(fresh, 't') == ['id', 'name', 'secret', 'age']
    assert len(rows_of(fresh, 't')[1]) == 3
    ok(fresh, 'ROLLBACK', s)
    ok(fresh, 'ALTER TABLE t ADD COLUMN c INT')
    print('  → ROLLBACK 之后 DDL 恢复正常')


def test_fulltext_index_interaction_with_truncate_and_drop_column():
    """全文索引是**派生**数据：TRUNCATE 必须清空它但保留索引定义；
    删掉带全文索引的列必须显式报错（否则倒排索引会引用不存在的列）"""
    import tempfile
    d = tempfile.mkdtemp(prefix='p1_ft_')
    try:
        db = open_db(d)
        ok(db, 'CREATE TABLE docs (id INT PRIMARY KEY, body VARCHAR(200))')
        ok(db, "INSERT INTO docs VALUES (1, 'lightdb fulltext hello')")
        ok(db, 'CREATE FULLTEXT INDEX ft_body ON docs (body)')
        tbl = db.get_table('docs')
        assert tbl.has_fulltext_index('body')
        hit = tbl.fulltext_search('body', 'hello')
        assert hit, hit
        print(f"  → TRUNCATE 前 MATCH hello 命中 {sorted(hit.items())}")

        r = err(db, 'ALTER TABLE docs DROP COLUMN body')
        assert r['code'] == 1091, r
        print(f"  → 删全文索引列: {r['code']} {r['message'][:70]}")

        ok(db, 'TRUNCATE TABLE docs')
        assert tbl.has_fulltext_index('body'), \
            'TRUNCATE 不该把全文索引的定义弄没（否则 MATCH 会误报"没有索引"）'
        assert not tbl.fulltext_search('body', 'hello'), '清空后不该还能搜到旧行'
        ok(db, "INSERT INTO docs VALUES (2, 'another hello world')")
        assert tbl.fulltext_search('body', 'hello'), '清空后新行必须能被搜到'
        print('  → TRUNCATE 后：索引定义保留、旧行搜不到、新行搜得到')
        db.shutdown()
    finally:
        safe_rmtree(d)


def test_concurrent_insert_during_add_column_never_mixes_arity(tmp_path):
    """并发边界：ADD COLUMN 期间其他线程一直在插入

    行载荷是**按列位置**的数组，所以"表级锁 + 换个不停"必须保证不会出现
    "行已按新列数改写、列定义还是旧的"（或反过来）那种错位。判据：结束后
    每一行的字段数都等于列数，且索引/版本链自洽。
    """
    d = tmp_path / 'data'
    db = open_db(d)
    ok(db, 'CREATE TABLE cc (id INT PRIMARY KEY, v INT)')
    from lightdb.sql.executor import Executor
    stop = threading.Event()
    errors = []

    def writer(base):
        ex = Executor(db)
        i = 0
        while not stop.is_set() and i < 300:
            r = ex.execute_sql(
                f'INSERT INTO cc (id, v) VALUES ({base + i}, {i})', {})
            if r.get('type') != 'ok':
                errors.append(r)
                return
            i += 1

    threads = [threading.Thread(target=writer, args=(k * 100000,),
                                daemon=True) for k in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    ok(db, 'ALTER TABLE cc ADD COLUMN c INT DEFAULT 5')
    stop.set()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), '写入线程没有结束'
    assert not errors, errors[:3]

    cols, rows = rows_of(db, 'cc')
    assert cols == ['id', 'v', 'c'], cols
    bad = [row for row in rows if len(row) != 3]
    assert not bad, bad[:3]
    assert rows, '并发插入一行都没成功，用例失去意义'
    scan = db.get_table('cc').integrity_scan(deep=True)
    assert not scan['dangling_prev'], scan['dangling_prev']
    assert scan['index_entries'] == len(rows), scan
    print(f"  → 加列期间并发插入共 {len(rows)} 行，全部 3 字段、"
          f"索引项 {scan['index_entries']} == 行数，无悬空 prev")
    db.shutdown()


# ==================== 6. 不支持的 ALTER 变体：显式报错 ====================

UNSUPPORTED_ALTER = [
    # E2 起 MODIFY COLUMN / ENGINE= 兼容别名已实现，不再属于"不支持变体"
    # （见 tests/test_mysql_compat_e2.py）。
    'ALTER TABLE t CHANGE COLUMN age years BIGINT',
    'ALTER TABLE t ALTER COLUMN age SET DEFAULT 1',
    'ALTER TABLE t ADD INDEX idx_age (age)',
    'ALTER TABLE t ADD KEY idx_age (age)',
    'ALTER TABLE t ADD UNIQUE KEY uq (name)',
    'ALTER TABLE t ADD CONSTRAINT ck CHECK (age > 0)',
    'ALTER TABLE t ADD CHECK (age > 0)',
    'ALTER TABLE t ADD PRIMARY KEY (age)',
    'ALTER TABLE t ADD FOREIGN KEY (age) REFERENCES t(id)',
    'ALTER TABLE t DROP PRIMARY KEY',
    'ALTER TABLE t DROP INDEX idx_age',
    'ALTER TABLE t DROP CHECK age_pos',
    'ALTER TABLE t RENAME COLUMN age TO years',
    'ALTER TABLE t COMMENT = "x"',
    'ALTER TABLE t CONVERT TO CHARACTER SET utf8mb4',
    'ALTER TABLE t ORDER BY id',
    # P6：AUTO_INCREMENT = n 已实现（设置下一自增号），不再属于不支持变体
    'ALTER TABLE t ADD COLUMN a INT, ADD COLUMN b INT',
    'ALTER TABLE t ADD COLUMN a INT FIRST',
    'ALTER TABLE t ADD COLUMN a INT AFTER id',
    'ALTER TABLE t ADD COLUMN IF NOT EXISTS a INT',
]


@pytest.mark.parametrize('statement', UNSUPPORTED_ALTER)
def test_unsupported_alter_variants_are_explicit_and_change_nothing(
        fresh, statement):
    """所有未实现的 ALTER 变体 → 显式错误码 + 明确报文，且结构一点不变"""
    before_cols = names(fresh, 't')
    before_rows = rows_of(fresh, 't')[1]
    before_ddl = rs(fresh, 'SHOW CREATE TABLE t')['rows'][0][1]
    r = err(fresh, statement)
    assert r['code'] in (1064, 1105, 1235), r
    assert r['message'], r
    # 报文必须点出"这是未实现的变体"，而不是笼统的语法错误
    assert ('未实现' in r['message'] or 'unsupported' in r['message'].lower()
            or 'ADD COLUMN / DROP COLUMN / RENAME TO' in r['message']), r
    assert names(fresh, 't') == before_cols, statement
    assert rows_of(fresh, 't')[1] == before_rows, statement
    assert rs(fresh, 'SHOW CREATE TABLE t')['rows'][0][1] == before_ddl
    print(f"  → {statement}\n      {r['code']}: {r['message'][:100]}")


def test_truncate_multiple_tables_is_explicit():
    """`TRUNCATE t1, t2`（MariaDB 扩展）显式拒绝，不做"清了一半" """
    import tempfile
    d = tempfile.mkdtemp(prefix='p1_tr_')
    try:
        db = open_db(d)
        ok(db, 'CREATE TABLE a (id INT PRIMARY KEY)')
        ok(db, 'CREATE TABLE b (id INT PRIMARY KEY)')
        ok(db, 'INSERT INTO a VALUES (1)')
        ok(db, 'INSERT INTO b VALUES (1)')
        r = err(db, 'TRUNCATE TABLE a, b')
        assert r['code'] == 1064, r
        print(f"  → {r['code']} {r['message'][:100]}")
        # 两张表都**没有**被清空（拒绝了就一点都不做）
        assert rows_of(db, 'a')[1] == [(1,)]
        assert rows_of(db, 'b')[1] == [(1,)]
        db.shutdown()
    finally:
        safe_rmtree(d)


# ==================== 7. 持久化（重启后一致） ====================

def test_add_drop_truncate_survive_restart(tmp_path):
    """ADD / DROP / TRUNCATE 之后干净关闭再重开：结构 + 数据与执行后一致"""
    d = tmp_path / 'data'
    db = open_db(d)
    seed(db)
    ok(db, 'ALTER TABLE t ADD COLUMN c INT DEFAULT 42')
    ok(db, 'ALTER TABLE t ADD COLUMN note VARCHAR(50)')
    ok(db, 'ALTER TABLE t DROP COLUMN secret')
    ddl_after = rs(db, 'SHOW CREATE TABLE t')['rows'][0][1]
    rows_after = rows_of(db, 't')[1]
    db.shutdown()
    print(f"  → 关闭前: cols={rows_of(db, 't') if False else ddl_after.splitlines()[1:]}")

    db2 = open_db(d)
    assert names(db2, 't') == ['id', 'name', 'age', 'c', 'note']
    assert rs(db2, 'SHOW CREATE TABLE t')['rows'][0][1] == ddl_after
    assert rows_of(db2, 't')[1] == rows_after
    print(f"  → 重开后 SELECT *: {rows_of(db2, 't')}")

    ok(db2, 'TRUNCATE TABLE t')
    db2.shutdown()
    db3 = open_db(d)
    assert rows_of(db3, 't')[1] == []
    assert rs(db3, 'SHOW CREATE TABLE t')['rows'][0][1] == ddl_after
    assert len(rs(db3, 'SHOW INDEX FROM t')['rows']) == 1
    ok(db3, "INSERT INTO t (id, name, age, c, note) VALUES (1,'z',1,2,'n')")
    assert rows_of(db3, 't')[1] == [(1, 'z', 1, 2, 'n')]
    print(f"  → TRUNCATE 重开后: 空表 + 定义保留 + 可再插入 "
          f"{rows_of(db3, 't')[1]}")
    db3.shutdown()


def test_alter_survives_dirty_pages_and_wal_replay(tmp_path):
    """改列之后**不**调用 shutdown（模拟"页还没全刷、WAL 还在"），
    重开时必须靠自身的一致性走通（新增行与旧行字段数一致）"""
    d = tmp_path / 'data'
    db = open_db(d)
    seed(db)
    ok(db, 'ALTER TABLE t ADD COLUMN c INT DEFAULT 5')
    tbl = db.get_table('t')
    before = rows_of(db, 't')[1]
    tbl.flush()                       # 只刷页，不 retire WAL
    db.shutdown()
    db2 = open_db(d)
    assert names(db2, 't') == ['id', 'name', 'secret', 'age', 'c']
    assert rows_of(db2, 't')[1] == before, rows_of(db2, 't')[1]
    assert not db2.get_table('t').integrity_scan(deep=True)['dangling_prev']
    print(f"  → 页刷盘 + WAL 重放后一致: {rows_of(db2, 't')[1]}")
    db2.shutdown()


# ==================== 8. 崩溃恢复（真 os._exit(1) 子进程） ====================

CRASH_CHILD = r'''
import json
import os
import sys

repo, point, data, sql = sys.argv[1:5]
sys.path.insert(0, repo)

from lightdb.db.database import Database
from lightdb.storage.buffer_pool import BufferPool
from lightdb.sql.executor import Executor

if point == 'intent_written':
    # 刚写完"进行中的结构变更"意图就崩（WAL/页都还是旧结构）
    orig = Database._ddl_begin

    def patched(self, entry):
        orig(self, entry)
        os._exit(1)
    Database._ddl_begin = patched
elif point == 'rows_rewritten_not_committed':
    # 行已全部改写、页已 fsync，但元数据还没换（提交点之前）就崩
    def patched(self, tbl):
        os._exit(1)
    Database._ddl_commit = patched
elif point == 'partial_flush':
    # 正在把改写后的页刷盘的中途崩（盘上是**半套**页）
    orig = BufferPool._write_page
    state = {'n': 0}

    def patched(self, page):
        orig(self, page)
        state['n'] += 1
        if state['n'] >= 1:
            os._exit(1)
    BufferPool._write_page = patched

db = Database(data)
ex = Executor(db)
print(json.dumps(ex.execute_sql(sql, {}), default=str)[:200])
db.shutdown()
print('NO-CRASH')
'''


@pytest.mark.parametrize('point', ['intent_written',
                                   'rows_rewritten_not_committed',
                                   'partial_flush'])
@pytest.mark.parametrize('ddl,expect_cols,expect_rows', [
    ('ALTER TABLE t ADD COLUMN c INT DEFAULT 42',
     ['id', 'name', 'secret', 'age', 'c'],
     [(1, 'a', 'S1', 5, 42), (2, 'b', 'S2', 6, 42), (3, 'c', 'S3', 7, 42)]),
    ('ALTER TABLE t DROP COLUMN secret',
     ['id', 'name', 'age'],
     [(1, 'a', 5), (2, 'b', 6), (3, 'c', 7)]),
    ('TRUNCATE TABLE t',
     ['id', 'name', 'secret', 'age'],
     []),
])
def test_ddl_crash_recovery_is_all_or_nothing(tmp_path, point, ddl,
                                              expect_cols, expect_rows):
    """DDL 中途 `os._exit(1)`：重开后的状态只能是"完整生效"（本引擎采用
    roll-forward：`_meta.json` 里的 `pending_ddl` 是权威意图，重开时把
    剩余的行补完）—— 绝不允许"结构换了、数据没换"这类半套状态"""
    d = tmp_path / 'data'
    db = open_db(d)
    ok(db, BASE_DDL.replace('secret', 'secret'))
    for i, (pk, nm) in enumerate([(1, 'a'), (2, 'b'), (3, 'c')], start=1):
        ok(db, f"INSERT INTO t (id, name, secret, age) VALUES "
               f"({pk}, '{nm}', 'S{pk}', {pk + 4})")
    db.shutdown()

    script = tmp_path / 'crash_child.py'
    script.write_text(CRASH_CHILD, encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    p = subprocess.run(
        [sys.executable, '-X', 'utf8', str(script), REPO, point, str(d), ddl],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=env, timeout=300)
    assert p.returncode == 1, (point, p.returncode, p.stdout[-500:],
                               p.stderr[-500:])
    print(f"  → 子进程在 {point} 处 os._exit(1)（rc={p.returncode}）")

    # 崩后盘上的元数据：结构变更还没提交（pending 记录在，列还是旧的）
    with open(os.path.join(str(d), '_meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    print(f"  → 崩溃瞬间 _meta.json: cols="
          f"{[c['name'] for c in meta['tables']['t']['cols']]} "
          f"pending={'pending_ddl' in meta and meta['pending_ddl'] is not None}")

    db2 = open_db(d)
    assert names(db2, 't') == expect_cols, names(db2, 't')
    got = rows_of(db2, 't')[1]
    assert got == expect_rows, (got, expect_rows)
    # 索引/版本链必须自洽
    scan = db2.get_table('t').integrity_scan(deep=True)
    assert not scan['dangling_prev'], scan['dangling_prev']
    assert scan['live_rows'] == len(expect_rows), scan
    with open(os.path.join(str(d), '_meta.json'), encoding='utf-8') as f:
        meta2 = json.load(f)
    assert meta2.get('pending_ddl') is None, meta2.get('pending_ddl')
    print(f"  → 重开后: cols={expect_cols} rows={got} "
          f"index_entries={scan['index_entries']}（pending 已清）")
    db2.shutdown()


def test_rename_table_keeps_data_and_is_durable(tmp_path):
    """`ALTER TABLE t RENAME TO t2`：数据/结构/索引一起搬过去并持久化"""
    d = tmp_path / 'data'
    db = open_db(d)
    seed(db)
    ddl = rs(db, 'SHOW CREATE TABLE t')['rows'][0][1]
    cols = rows_of(db, 't')[0]
    rows = rows_of(db, 't')[1]
    ok(db, 'ALTER TABLE t RENAME TO t2')
    assert 't' not in db.tables and 't2' in db.tables
    assert rows_of(db, 't2')[1] == rows
    assert rows_of(db, 't2')[0] == cols
    assert rs(db, 'SHOW CREATE TABLE t2')['rows'][0][1] == \
        ddl.replace('CREATE TABLE t ', 'CREATE TABLE t2 ')
    r = err(db, 'SELECT * FROM t')
    assert r['code'] == 1146, r
    # 目标名已存在 / 名字非法 → 显式报错
    ok(db, 'CREATE TABLE t3 (id INT PRIMARY KEY)')
    assert err(db, 'ALTER TABLE t2 RENAME TO t3')['code'] == 1050
    ok(db, 'ALTER TABLE t2 RENAME TO t4')
    r = err(db, 'ALTER TABLE t4 RENAME TO `bad name`')
    assert r['code'] in (1103, 1064), r
    db.shutdown()

    db2 = open_db(d)
    assert 't4' in db2.tables and 't2' not in db2.tables
    assert rows_of(db2, 't4')[1] == rows
    print(f"  → 改名并重开后: t4 rows={rows_of(db2, 't4')[1]}")
    db2.shutdown()


# ==================== 9. GUI 兼容（只读 GUI 代码的约定） ====================
#
# 约定来源（GUI 代码只读不改）：
#   * `lightdb_studio/db/metadata.py::create_table_sql` →
#     `cols, rows = conn.query("SHOW CREATE TABLE `t`")`，取 `rows[0][1]`；
#   * `lightdb_studio/ui/object_tree.py` → `SHOW CREATE TABLE \`t\`` 直接塞进
#     查询窗口（结果集必须能显示）；
#   * `lightdb_studio/core/health_score.py::_check_indexes` 与
#     `lightdb/monitor/index_advisor.py` → `SHOW INDEX FROM \`t\``，按**列名**
#     找 `Key_name` / `Column_name` / `Seq_in_index`；
#   * `lightdb_studio/ui/object_tree.py` / `import_export.py` →
#     `conn.execute("TRUNCATE TABLE \`t\`")`，靠 `(0, None)` 判断成功。

def test_gui_column_and_shape_conventions(fresh):
    r = rs(fresh, 'SHOW CREATE TABLE t')                 # metadata.py
    assert len(r['rows']) == 1 and len(r['rows'][0]) > 1
    ddl = r['rows'][0][1]                                # rows[0][1]
    assert isinstance(ddl, str) and ddl.startswith('CREATE TABLE')
    print(f"  → metadata.create_table_sql 拿到的 DDL: {ddl.splitlines()[0]}")

    r = rs(fresh, 'SHOW INDEX FROM t')                   # health_score.py
    lowered = [str(c).lower() for c in r['columns']]
    for need in ('key_name', 'column_name', 'seq_in_index', 'non_unique',
                 'table'):
        assert need in lowered, (need, lowered)
    idx_name = r['rows'][0][lowered.index('key_name')]
    col_name = r['rows'][0][lowered.index('column_name')]
    seq = int(r['rows'][0][lowered.index('seq_in_index')])
    assert idx_name and col_name and seq == 1, r['rows']
    print(f"  → health_score/index_advisor 按名取值: "
          f"key_name={idx_name} column_name={col_name} seq_in_index={seq}")

    # GUI 的 TRUNCATE：`execute()` 返回 (rowcount, err)，err 为 None 才算成功
    assert ok(fresh, 'TRUNCATE TABLE t')['type'] == 'ok'


# ==================== 10. 经真实 MySQL 协议（pymysql） ====================

@pytest.fixture()
def served(tmp_path):
    pymysql = pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_srv')
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    db = track(__import__('lightdb.db.database', fromlist=['Database'])
               .Database(data_dir))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    conn = track(pymysql.connect(
        host='127.0.0.1', port=PORT, user='root', password='',
        database='default', charset='utf8mb4', autocommit=True,
        connect_timeout=10))
    yield conn, db
    try:
        conn.close()
    except Exception:
        pass


def test_protocol_ddl_end_to_end(served):
    """经真实服务端 + pymysql：SHOW CREATE TABLE / SHOW INDEX / ALTER /
    TRUNCATE 全部可用，且返回形状与进程内一致"""
    conn, db = served
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE p (id INT PRIMARY KEY, "
                    "name VARCHAR(20) NOT NULL DEFAULT 'anon', age INT, "
                    "CHECK (age > 0))")
        cur.execute("INSERT INTO p (id, name, age) VALUES (1,'a',5)")
        cur.execute("INSERT INTO p (id, name, age) VALUES (2,'b',6)")

        # --- SHOW CREATE TABLE：取第一行第二列（GUI 的约定） ---
        cur.execute('SHOW CREATE TABLE p')
        assert [d[0] for d in cur.description] == ['Table', 'Create Table']
        row = cur.fetchone()
        assert row[0] == 'p' and row[1].startswith('CREATE TABLE p')
        assert 'name VARCHAR(20) NOT NULL DEFAULT' in row[1], row[1]
        assert 'CHECK (age > 0)' in row[1], row[1]
        print(f'  → [协议] SHOW CREATE TABLE p: cols='
              f'{["Table", "Create Table"]} 第一行第一列={row[0]!r}')
        print('      ' + '\n      '.join(row[1].splitlines()))
        assert cur.fetchone() is None

        # --- SHOW INDEX ---
        cur.execute('SHOW INDEX FROM p')
        idx_cols = [d[0] for d in cur.description]
        idx_rows = cur.fetchall()
        assert idx_cols[:5] == ['Table', 'Non_unique', 'Key_name',
                                'Seq_in_index', 'Column_name'], idx_cols
        d = dict(zip(idx_cols, idx_rows[0]))
        assert (d['Key_name'], d['Column_name'], d['Non_unique'],
                d['Index_type']) == ('PRIMARY', 'id', 0, 'BTREE'), d
        print(f'  → [协议] SHOW INDEX FROM p: cols={idx_cols}')
        print(f'      rows={idx_rows}')

        # --- ALTER ADD COLUMN ---
        cur.execute('ALTER TABLE p ADD COLUMN c INT DEFAULT 42')
        assert cur.rowcount == 0
        cur.execute('SELECT * FROM p')
        assert [d[0] for d in cur.description] == ['id', 'name', 'age', 'c']
        got = cur.fetchall()
        assert got == ((1, 'a', 5, 42), (2, 'b', 6, 42)), got
        print(f'  → [协议] ADD COLUMN 后 SELECT *: {list(got)}')

        # --- ALTER DROP COLUMN ---
        cur.execute('ALTER TABLE p DROP COLUMN c')
        cur.execute('SELECT * FROM p')
        assert [d[0] for d in cur.description] == ['id', 'name', 'age']
        print(f'  → [协议] DROP COLUMN 后 SELECT *: {list(cur.fetchall())}')

        # --- TRUNCATE ---
        cur.execute('TRUNCATE TABLE p')
        cur.execute('SELECT COUNT(*) FROM p')
        assert cur.fetchone() == (0,)
        cur.execute('SHOW CREATE TABLE p')
        assert 'CHECK (age > 0)' in cur.fetchone()[1]
        cur.execute("INSERT INTO p (id, name, age) VALUES (9,'z',1)")
        cur.execute('SELECT * FROM p')
        assert list(cur.fetchall()) == [(9, 'z', 1)]
        print('  → [协议] TRUNCATE 后为空、定义保留、可再插入')


def test_protocol_errors_match_inproc(served):
    """经协议时错误码/报文与进程内一致（不是被压成笼统的 1064）"""
    conn, db = served

    def run(cur, statement):
        """返回 errno；成功返回 None"""
        try:
            cur.execute(statement)
            return None
        except Exception as e:                       # noqa: BLE001
            return getattr(e, 'args', (None,))[0]

    with conn.cursor() as cur:
        assert run(cur, "CREATE TABLE e (id INT PRIMARY KEY, age INT)") is None
        assert run(cur, "INSERT INTO e (id, age) VALUES (1, 5)") is None

        cases = [
            ("ALTER TABLE e ADD COLUMN x INT NOT NULL", 1138),   # 已有行 + 无默认
            ("ALTER TABLE e MODIFY COLUMN age BIGINT", None),    # E2：已实现
            ("ALTER TABLE e CHANGE COLUMN age years BIGINT", 1064),
            ("ALTER TABLE e ADD INDEX i (age)", 1064),
            ("ALTER TABLE e ADD CONSTRAINT ck CHECK (age > 0)", 1064),
            ("ALTER TABLE e ADD COLUMN a INT, ADD COLUMN b INT", 1064),
            ("ALTER TABLE e DROP COLUMN id", 1091),
            ("ALTER TABLE e DROP COLUMN nope", 1054),
            ("TRUNCATE TABLE e, e2", 1064),
            ("SHOW INDEX FROM nope", 1146),
            ("SHOW CREATE TABLE nope", 1146),
        ]
        for statement, code in cases:
            got = run(cur, statement)
            assert got == code, (statement, got)
            print(f"  → [协议] {statement} → {got}")

        # 报错路径不能改结构
        cur.execute('SHOW COLUMNS FROM e')
        assert [r[0] for r in cur.fetchall()] == ['id', 'age']
        cur.execute('SHOW CREATE TABLE e')
        ddl = cur.fetchone()[1]
        assert ' x ' not in ddl and 'INDEX' not in ddl, ddl
        cur.execute('SELECT COUNT(*) FROM e')
        assert cur.fetchone() == (1,)
        print('  → [协议] 所有报错路径都没有改动 e 的结构/数据')


def test_protocol_persistence_after_restart(tmp_path):
    """经协议做完 DDL → 停服务 → 重开（新 Database + 新服务端）→ 一致"""
    pymysql = pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_p')
    port = PORT + 1

    def bring_up():
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = port
        db = Database(data_dir)
        ex = Executor(db)
        srv = MySQLServer(cfg, db, ex)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.5)
        conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                               password='', database='default',
                               charset='utf8mb4', autocommit=True,
                               connect_timeout=10)
        return db, srv, conn

    db, srv, conn = bring_up()
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE r (id INT PRIMARY KEY, v VARCHAR(10))')
            cur.execute("INSERT INTO r VALUES (1,'x')")
            cur.execute("INSERT INTO r VALUES (2,'y')")
            cur.execute('ALTER TABLE r ADD COLUMN c INT DEFAULT 7')
            cur.execute('ALTER TABLE r DROP COLUMN v')
            cur.execute('SHOW CREATE TABLE r')
            ddl = cur.fetchone()[1]
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()
        db.shutdown()

    db2, srv2, conn2 = bring_up()
    try:
        with conn2.cursor() as cur:
            cur.execute('SHOW CREATE TABLE r')
            assert cur.fetchone()[1] == ddl
            cur.execute('SELECT * FROM r ORDER BY id')
            assert list(cur.fetchall()) == [(1, 7), (2, 7)]
            cur.execute('TRUNCATE TABLE r')
        print(f'  → [协议] 重启后 SHOW CREATE TABLE 与执行后逐字符一致，'
              f'数据 {[(1, 7), (2, 7)]}')
    finally:
        conn2.close()
        srv2.shutdown()
        srv2.server_close()
        db2.shutdown()


# ==================== 11. 既有口径不能被 P1 弄坏 ====================

def test_show_columns_honesty_unchanged(fresh):
    """阶段 1 的诚实化口径（Type 带长度、Default 真实、Extra 恒空、
    主键 Null=NO）在 DDL 之后仍然成立"""
    ok(fresh, 'ALTER TABLE t ADD COLUMN price DECIMAL(10,2) DEFAULT 9.99')
    got = {row[0]: tuple(row[1:])
           for row in rs(fresh, 'SHOW COLUMNS FROM t')['rows']}
    assert got['id'] == ('INT', 'NO', 'PRI', None, ''), got['id']
    assert got['name'] == ('VARCHAR(20)', 'NO', '', 'anon', ''), got['name']
    assert got['price'] == ('DECIMAL(10,2)', 'YES', '', '9.99', ''), \
        got['price']
    assert got['secret'] == ('VARCHAR(40)', 'YES', '', None, ''), got['secret']
    assert all(v[4] == '' for v in got.values()), got
    print(f"  → SHOW COLUMNS: {got}")
