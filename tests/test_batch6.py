"""Batch 6 测试 - JOIN + 多表查询"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch6.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b6')


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def setup_data():
    """初始化测试数据"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = track(Database(DATA_DIR))
    ex = Executor(db)

    # 用户表
    db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'name', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    # 订单表
    db.create_table('orders', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'user_id', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'amount', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
    ])

    users = db.get_table('users')
    orders = db.get_table('orders')

    for i in range(1, 101):
        users.insert([i, f'user{i}'])

    # 每个用户 0~3 个订单
    oid = 1
    for uid in range(1, 101):
        count = uid % 4
        for k in range(count):
            orders.insert([oid, uid, 100 + oid * 10])
            oid += 1

    return db, ex


def test_inner_join():
    """测试1: INNER JOIN"""
    print("\n【测试1】INNER JOIN")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    sql = ("SELECT users.id, users.name, orders.amount "
           "FROM users INNER JOIN orders ON users.id = orders.user_id "
           "LIMIT 5")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  列: {r['columns']}")
    for row in r['rows']:
        print(f"  → {row}")
    assert len(r['rows']) > 0
    assert len(r['columns']) == 3
    print(f"  ✅ 返回 {len(r['rows'])} 行")

    db.shutdown()


def test_join_count():
    """测试2: JOIN + COUNT"""
    print("\n【测试2】JOIN 结果行数统计")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    sql = "SELECT users.id, users.name, orders.id FROM users JOIN orders ON users.id = orders.user_id"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    # 每个用户 uid%4 个订单，1..100 中 25个订单/类×4 = 0+1+2+3 平均
    # 精确计算: sum(uid % 4 for uid in 1..100) = 25*(0+1+2+3) = 150
    expected = 150
    assert len(r['rows']) == expected, f"期望{expected}行，实际{len(r['rows'])}行"
    print(f"  ✅ 共 {len(r['rows'])} 行（期望{expected}）")

    db.shutdown()


def test_left_join():
    """测试3: LEFT JOIN"""
    print("\n【测试3】LEFT JOIN")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    sql = "SELECT users.id, users.name, orders.id FROM users LEFT JOIN orders ON users.id = orders.user_id"
    p = parser.parse(sql)
    r = ex.execute(p, {})

    # LEFT JOIN: 每个用户至少 1 行（无订单的补 NULL）
    # users有100行，其中 uid%4==0 的有25个无订单，所以 100行 + 75个用户的订单行
    # 有订单：uid%4 in {1,2,3}，有75个用户，共订单150个
    # LEFT JOIN结果 = 150 + 25(NULL) = 175
    print(f"  → 总行数: {len(r['rows'])}")

    # 检查有 NULL 填充行
    null_rows = [row for row in r['rows'] if row[2] is None]
    print(f"  → NULL 填充行: {len(null_rows)}")
    assert len(null_rows) == 25
    print("  ✅ LEFT JOIN 正确填充 NULL")

    db.shutdown()


def test_cross_join():
    """测试4: CROSS JOIN"""
    print("\n【测试4】CROSS JOIN")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    # 小表 × 小表 = 完全笛卡尔积
    sql = "SELECT users.id, orders.id FROM users CROSS JOIN orders LIMIT 20"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 返回: {len(r['rows'])} 行")
    assert len(r['rows']) == 20  # LIMIT

    db.shutdown()


def test_explain_join():
    """测试5: EXPLAIN JOIN"""
    print("\n【测试5】EXPLAIN JOIN")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    sql = "EXPLAIN SELECT * FROM users JOIN orders ON users.id = orders.user_id"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    for row in r['rows']:
        print(f"  {row}")

    # 修正（E 类 #13）：MySQL 的 EXPLAIN 返回的是**表格结果集**，列固定为
    # id/select_type/table/type/key/rows/Extra（引擎同此契约，
    # 见 lightdb/sql/optimizer.py:400-418），第 0 列 id 是 int。
    # 原断言 `'HashJoin' in row[0]`（row[0] 是 int）既语义错又会 TypeError：
    # 文本计划树根本不在 EXPLAIN 的结果列里。
    # 正确契约分两层断言，两层都保留（不放水）：
    #   (1) 结果集必须是 MySQL 形状的表，且覆盖 JOIN 两侧的表名；
    #   (2) 连接算法取引擎计划树的文本（optimizer.explain 的第三个返回值
    #       = print_plan 文本），等值 JOIN 必须仍是 HashJoin ——
    #       删掉这层就抓不住"HashJoin 退化为 NestedLoop/笛卡尔积"的回归。
    assert r['columns'] == ['id', 'select_type', 'table', 'type', 'key',
                            'rows', 'Extra'], f"EXPLAIN 列结构变了: {r['columns']}"
    tables_seen = [row[2] for row in r['rows']]
    assert 'users' in tables_seen and 'orders' in tables_seen, tables_seen
    # Extra 列必须是 str/None（原断言正是把 int 当字符串才崩的，这里显式钉住类型）
    assert all(row[6] is None or isinstance(row[6], str) for row in r['rows'])
    assert all(isinstance(row[0], int) for row in r['rows'])   # id 列是 int

    inner = p.get('inner')
    cols, rows, plan_text = ex.optimizer.explain(ex.optimizer.optimize(inner))
    # print_plan 返回的是行列表（也兼容 str），与 executor._cmd_explain 的用法一致
    plan_lines = (plan_text.splitlines() if isinstance(plan_text, str)
                  else [str(x) for x in (plan_text or [])])
    print("  → 计划树:\n" + '\n'.join(plan_lines))
    has_hashjoin = any('HashJoin' in l for l in plan_lines)
    print(f"  → 使用 HashJoin: {has_hashjoin}")
    assert has_hashjoin, plan_lines

    db.shutdown()


def test_join_performance():
    """测试6: JOIN 性能（1000×1000）"""
    print("\n【测试6】JOIN 性能")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = track(Database(DATA_DIR))
    ex = Executor(db)

    db.create_table('a', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'v', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    db.create_table('b', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'a_id', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'w', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    ta = db.get_table('a')
    tb = db.get_table('b')

    print("  插入 1000 + 2000 行...")
    for i in range(1, 1001):
        ta.insert([i, f'v{i}'])
    for i in range(1, 2001):
        tb.insert([i, (i % 1000) + 1, f'w{i}'])

    t0 = time.time()
    p = parser.parse("SELECT a.id, a.v, b.id, b.w FROM a JOIN b ON a.id = b.a_id LIMIT 100")
    r = ex.execute(p, {})
    t1 = time.time()
    print(f"  → HashJoin 1000×2000 耗时: {(t1 - t0) * 1000:.2f}ms")
    print(f"  → 返回 {len(r['rows'])} 行")

    assert (t1 - t0) < 3.0, "JOIN 太慢"
    print("  ✅ 性能达标")

    db.shutdown()


def test_where_after_join():
    """测试7: JOIN + WHERE"""
    print("\n【测试7】JOIN + WHERE")
    reset()
    db, ex = setup_data()
    from lightdb.sql.parser import parser

    sql = ("SELECT users.id, users.name, orders.amount "
           "FROM users JOIN orders ON users.id = orders.user_id "
           "WHERE orders.amount > 1000 LIMIT 10")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 返回 {len(r['rows'])} 行")
    for row in r['rows'][:5]:
        print(f"    {row}")

    # 验证所有 amount > 1000
    for row in r['rows']:
        assert row[2] > 1000
    print("  ✅ WHERE 过滤正确")

    db.shutdown()


def main():
    print("=" * 60)
    print("  Batch 6 测试 - JOIN + 多表查询")
    print("=" * 60)

    tests = [
        ('INNER JOIN', test_inner_join),
        ('JOIN COUNT', test_join_count),
        ('LEFT JOIN', test_left_join),
        ('CROSS JOIN', test_cross_join),
        ('EXPLAIN JOIN', test_explain_join),
        ('JOIN 性能', test_join_performance),
        ('JOIN + WHERE', test_where_after_join),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 Batch 6 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())