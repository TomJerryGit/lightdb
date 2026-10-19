"""阶段 0 完整性锁测试：CHECK 约束

锁住 docs/review/04_ROADMAP.md 的 0.3「假成功」缺陷：

* **CHECK 约束完全不生效** —— `CHECK (price > 0)` 拦不住 `price = -5`，
  SQL 路径与 `Table.insert` 直写两条路径都放行（建表时表达式被丢弃 /
  从来没人读）。

统一判据：**要么生效，要么显式报错，绝不静默放行**。

（原「联邦策略 id 撞号」用例随 `lightdb_studio/core/federated_gov.py`
在 2026-10-19 Studio 死代码清理中一并删除。）
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_phase0_integrity.py
    from conftest import safe_rmtree, track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data_phase0_test')


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def _open():
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = track(Database(DATA_DIR))
    return db, Executor(db)


def _rows(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r['type'] == 'resultset', r
    return [tuple(row) for row in r['rows']]


# ==================== A. CHECK 约束 ====================

def test_check_rejects_sql_and_direct_write():
    """两条写入路径（SQL / Table 直写 × INSERT / UPDATE）都必须拒绝"""
    print("\n【A1】CHECK 拦截 SQL 与直写两条路径")
    reset()
    db, ex = _open()

    r = ex.execute_sql(
        "CREATE TABLE products (id INT PRIMARY KEY, "
        "price INT CHECK (price > 0), stock INT)", {})
    assert r['type'] == 'ok', r

    # ---- SQL INSERT ----
    r = ex.execute_sql("INSERT INTO products VALUES (1, -5, 10)", {})
    assert r['type'] == 'error', r
    assert r['code'] == 3819, r
    assert r['sqlstate'] == 'HY000', r
    assert '3819' in r['message'] and 'price' in r['message'], r
    print(f"  ✅ SQL INSERT 被拒: {r['message'][:70]}")

    # ---- Table.insert 直写 ----
    tbl = db.get_table('products')
    with pytest.raises(ValueError) as ei:
        tbl.insert([2, -5, 10])
    assert '3819' in str(ei.value)
    print(f"  ✅ Table.insert 被拒: {str(ei.value)[:70]}")

    # ---- SQL UPDATE ----
    assert ex.execute_sql(
        "INSERT INTO products VALUES (3, 100, 10)", {})['type'] == 'ok'
    r = ex.execute_sql("UPDATE products SET price = -1 WHERE id = 3", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    print(f"  ✅ SQL UPDATE 被拒: {r['message'][:70]}")

    # ---- Table.update 直写 ----
    with pytest.raises(ValueError) as ei:
        tbl.update({'price': -1}, {'column': 'id', 'op': '=', 'value': 3})
    assert '3819' in str(ei.value)
    print(f"  ✅ Table.update 被拒: {str(ei.value)[:70]}")

    # 违规的语句一条行都没留下（校验在拿锁/写 WAL 之前）
    assert _rows(ex, "SELECT id, price, stock FROM products") == [(3, 100, 10)]
    db.shutdown()


def test_check_rejects_inside_transaction():
    """事务内违规同样显式报错，且不留脏行（BEGIN/COMMIT 语义不变）"""
    print("\n【A2】事务内 CHECK 拦截")
    reset()
    db, ex = _open()
    ex.execute_sql(
        "CREATE TABLE t (id INT PRIMARY KEY, price INT CHECK (price > 0))", {})

    s = {}
    assert ex.execute_sql('BEGIN', s)['type'] == 'ok'
    r = ex.execute_sql("INSERT INTO t VALUES (1, -9)", s)
    assert r['type'] == 'error' and r['code'] == 3819, r
    assert ex.execute_sql('COMMIT', s)['type'] == 'ok'
    assert _rows(ex, "SELECT id FROM t") == []
    print(f"  ✅ 事务内 INSERT 被拒且未落行: {r['message'][:60]}")
    db.shutdown()


def test_check_null_is_unknown_and_passes():
    """SQL 三值逻辑：NULL 参与 CHECK → UNKNOWN → 放行（MySQL 同）"""
    print("\n【A3】NULL → UNKNOWN → 放行")
    reset()
    db, ex = _open()
    ex.execute_sql(
        "CREATE TABLE t (id INT PRIMARY KEY, "
        "price INT CHECK (price > 0), "
        "CHECK (NOT (price > 0)))", {})

    # 列级 CHECK (price > 0) 与 `NOT (price > 0)` 在 NULL 上都是 UNKNOWN
    assert ex.execute_sql("INSERT INTO t VALUES (1, NULL)", {})['type'] == 'ok'
    tbl = db.get_table('t')
    tbl.insert([2, None])
    assert _rows(ex, "SELECT id FROM t") == [(1,), (2,)]
    print("  ✅ SQL 与直写两条路径的 NULL 都放行")

    # 非 NULL 值仍必须被拦（不能被 NULL 的放行"顺带"放过）
    r = ex.execute_sql("INSERT INTO t VALUES (3, -1)", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    print(f"  ✅ 非 NULL 违规仍被拒: {r['message'][:60]}")
    db.shutdown()


def test_check_survives_restart():
    """CHECK 定义随元数据持久化：shutdown 重开后违规仍被拒、NULL 仍放行"""
    print("\n【A4】重启后 CHECK 仍生效")
    reset()
    db, ex = _open()
    r = ex.execute_sql(
        "CREATE TABLE t (id INT PRIMARY KEY, "
        "qty INT CHECK (qty >= 0), "
        "CONSTRAINT named_ck CHECK (qty <= 100))", {})
    assert r['type'] == 'ok', r
    names = {c['name'] for c in db.get_table('t').check_constraints()}
    assert names == {'t_chk_1', 'named_ck'}, names
    db.shutdown()

    db2, ex2 = _open()
    tbl2 = db2.get_table('t')
    assert len(tbl2.check_constraints()) == 2
    assert {c['expr'] for c in tbl2.check_constraints()} == \
        {'qty >= 0', 'qty <= 100'}

    r = ex2.execute_sql("INSERT INTO t VALUES (1, -1)", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    r = ex2.execute_sql("INSERT INTO t VALUES (2, 1000)", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    with pytest.raises(ValueError):
        tbl2.insert([3, -1])
    assert ex2.execute_sql("INSERT INTO t VALUES (4, NULL)", {})['type'] == 'ok'
    assert ex2.execute_sql("INSERT INTO t VALUES (5, 50)", {})['type'] == 'ok'
    assert _rows(ex2, "SELECT id FROM t") == [(4,), (5,)]
    print("  ✅ 重开后 列级 + 表级约束都仍生效，NULL 仍放行")
    db2.shutdown()


def test_check_definition_errors_are_explicit():
    """不支持的 CHECK 形态必须显式报错，绝不静默丢弃"""
    print("\n【A5】定义期 / 解析期显式报错")
    reset()
    db, ex = _open()

    # 引用不存在的列
    r = ex.execute_sql(
        "CREATE TABLE bad1 (id INT PRIMARY KEY, CHECK (nope > 0))", {})
    assert r['type'] == 'error' and r['code'] == 3813, r
    # 不支持的函数
    r = ex.execute_sql(
        "CREATE TABLE bad2 (id INT PRIMARY KEY, CHECK (MD5(id) > 'a'))", {})
    assert r['type'] == 'error' and r['code'] == 3813, r
    # 不支持的谓词形态
    r = ex.execute_sql(
        "CREATE TABLE bad3 (id INT PRIMARY KEY, CHECK (id IS TRUE))", {})
    assert r['type'] == 'error' and r['code'] == 3813, r
    print(f"  ✅ 定义期报错: {r['message'][:60]}")

    # 聚合函数：CHECK 是逐行求值、没有分组上下文 → 定义期就拒绝
    r = ex.execute_sql(
        "CREATE TABLE bad_agg (id INT PRIMARY KEY, a INT, "
        "CHECK (SUM(a) > 0))", {})
    assert r['type'] == 'error' and r['code'] == 3813, r
    # 子查询：不支持 → 同样定义期显式报错（不是静默丢掉这条约束）
    r = ex.execute_sql(
        "CREATE TABLE bad_sub (id INT PRIMARY KEY, a INT, "
        "CHECK (a IN (SELECT id FROM bad1)))", {})
    assert r['type'] == 'error' and r['code'] == 3813, r
    print("  ✅ 聚合函数 / 子查询在定义期被拒")

    # 建表失败不留半张表
    assert not ({'bad1', 'bad2', 'bad3', 'bad_agg', 'bad_sub'} & set(db.tables))

    from lightdb.sql.parser import parser
    # `NOT ENFORCED`：MySQL 解析后忽略（一律强制），本引擎选择显式拒绝，
    # 绝不"用户说不生效、系统仍生效且不告诉他"
    with pytest.raises(SyntaxError) as ei:
        parser.parse("CREATE TABLE bad4 (id INT PRIMARY KEY, "
                     "CHECK (id > 0) NOT ENFORCED)")
    assert 'NOT ENFORCED' in str(ei.value)
    # 多余尾巴 / 括号不配对：不再静默丢弃
    with pytest.raises(SyntaxError):
        parser.parse("CREATE TABLE bad5 (id INT PRIMARY KEY, "
                     "CHECK (id > 0) GARBAGE)")
    with pytest.raises(SyntaxError):
        parser.parse("CREATE TABLE bad6 (id INT PRIMARY KEY, "
                     "qty INT CHECK (qty > 0)")
    print("  ✅ 解析期报错: NOT ENFORCED / 多余尾巴 / 括号不配对")

    # ALTER TABLE ... ADD CHECK：ALTER 整体不支持 → 必须显式报错
    r = ex.execute_sql("ALTER TABLE bad1 ADD CHECK (id > 0)", {})
    assert r['type'] == 'error', r
    assert r['code'] in (1064, 1105), r
    print(f"  ✅ ALTER TABLE ADD CHECK 显式报错: {r['message'][:60]}")
    db.shutdown()


def test_check_update_is_all_or_nothing():
    """多行 UPDATE：任一行违规 → 整条语句不写任何行（不许部分更新）"""
    print("\n【A6】UPDATE 整条语句原子拒绝")
    reset()
    db, ex = _open()
    ex.execute_sql(
        "CREATE TABLE t (id INT PRIMARY KEY, cap INT, qty INT, "
        "CHECK (qty <= cap))", {})
    assert ex.execute_sql(
        "INSERT INTO t VALUES (1, 10, 1), (2, 1, 1)", {})['type'] == 'ok'

    # id=1 的 cap=10 → qty=5 合法；id=2 的 cap=1 → qty=5 违规。
    # 若逐行边写边校验，id=1 会先被改掉（部分更新）。
    r = ex.execute_sql("UPDATE t SET qty = 5", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    assert _rows(ex, "SELECT id, qty FROM t") == [(1, 1), (2, 1)]
    print(f"  ✅ 违规 UPDATE 被拒且零行被改: {r['message'][:60]}")

    # 全部合法时正常生效
    assert ex.execute_sql("UPDATE t SET qty = 0", {})['type'] == 'ok'
    assert _rows(ex, "SELECT id, qty FROM t") == [(1, 0), (2, 0)]
    db.shutdown()


def test_check_supported_expression_forms():
    """AND/OR/NOT/IN/BETWEEN/LIKE/函数/算术 都真求值"""
    print("\n【A7】CHECK 支持的表达式形态")
    reset()
    db, ex = _open()
    r = ex.execute_sql(
        "CREATE TABLE t (id INT PRIMARY KEY, a INT, b INT, name TEXT, "
        "CHECK (a > 0 AND b > 0), "
        "CHECK (LENGTH(name) >= 2), "
        "CHECK (a IN (1, 2, 3)), "
        "CHECK (b BETWEEN 10 AND 20), "
        "CHECK (name LIKE 'x%'), "
        "CHECK (a * 2 + b >= 4 OR b = 0))", {})
    assert r['type'] == 'ok', r
    bad = ["(1, 0, 15, 'xy')", "(2, 1, 15, 'x')", "(3, 9, 15, 'xy')",
           "(4, 1, 99, 'xy')", "(5, 1, 15, 'yy')", "(6, 1, 1, 'xy')"]
    for values in bad:
        r = ex.execute_sql(f"INSERT INTO t VALUES {values}", {})
        assert r['type'] == 'error' and r['code'] == 3819, (values, r)
    print(f"  ✅ {len(bad)} 种违规形态全部被拒")
    assert ex.execute_sql(
        "INSERT INTO t VALUES (7, 1, 15, 'xy')", {})['type'] == 'ok'
    db.shutdown()


