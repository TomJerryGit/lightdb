"""Batch 7 测试 - 子查询 + UNION + GROUP BY"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b7')


def reset():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def setup():
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = Database(DATA_DIR)
    ex = Executor(db)

    db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'name', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'city', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    db.create_table('orders', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'user_id', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'amount', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
    ])

    tu = db.get_table('users')
    to = db.get_table('orders')

    cities = ['Beijing', 'Shanghai', 'Beijing', 'Beijing', 'Shenzhen']
    for i in range(1, 21):
        tu.insert([i, f'user{i}', cities[i % len(cities)]])

    oid = 1
    for uid in range(1, 21):
        cnt = uid % 4
        for k in range(cnt):
            to.insert([oid, uid, 100 * cnt + oid])
            oid += 1

    return db, ex


def test_group_by():
    """测试1: GROUP BY + COUNT"""
    print("\n【测试1】GROUP BY")
    reset()
    db, ex = setup()
    from lightdb.sql.parser import parser

    sql = "SELECT city, COUNT(*) FROM users GROUP BY city"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  列: {r['columns']}")
    for row in r['rows']:
        print(f"  → {row}")

    total = sum(row[1] for row in r['rows'])
    assert total == 20
    print(f"  ✅ 分组总数 = 20")

    db.shutdown()


def test_group_by_having():
    """测试2: GROUP BY + HAVING"""
    print("\n【测试2】GROUP BY + HAVING")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = "SELECT city, COUNT(*) FROM users GROUP BY city HAVING COUNT(*) > 5"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r['rows']}")
    assert all(row[1] > 5 for row in r['rows'])
    print("  ✅ HAVING 生效")

    db.shutdown()


def test_distinct():
    """测试3: DISTINCT"""
    print("\n【测试3】DISTINCT")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = "SELECT DISTINCT city FROM users"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 城市: {r['rows']}")
    cities = {row[0] for row in r['rows']}
    assert len(cities) <= 5
    print(f"  ✅ 去重后 {len(cities)} 个城市")

    db.shutdown()


def test_in_subquery():
    """测试4: IN 子查询"""
    print("\n【测试4】IN 子查询")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    # 找出有订单的用户
    sql = ("SELECT id, name FROM users "
           "WHERE id IN (SELECT user_id FROM orders)")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 有订单的用户数: {len(r['rows'])}")

    # 订单用户: uid % 4 != 0
    expected = sum(1 for u in range(1, 21) if u % 4 != 0)
    assert len(r['rows']) == expected, f"期望{expected}, 实际{len(r['rows'])}"
    print(f"  ✅ 命中 {expected} 个用户")

    db.shutdown()


def test_not_in():
    """测试5: NOT IN"""
    print("\n【测试5】NOT IN")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = ("SELECT id, name FROM users "
           "WHERE id NOT IN (SELECT user_id FROM orders)")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 无订单用户数: {len(r['rows'])}")
    expected = sum(1 for u in range(1, 21) if u % 4 == 0)
    assert len(r['rows']) == expected

    db.shutdown()


def test_exists():
    """测试6: EXISTS"""
    print("\n【测试6】EXISTS")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = ("SELECT id, name FROM users "
           "WHERE EXISTS (SELECT 1 FROM orders)")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    # 子查询有结果 → 全部返回
    assert len(r['rows']) == 20
    print("  ✅ EXISTS（简化版）生效")

    db.shutdown()


def test_union():
    """测试7: UNION"""
    print("\n【测试7】UNION")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = "SELECT city FROM users WHERE city='Beijing' UNION SELECT city FROM users WHERE city='Shanghai'"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r['rows']}")
    cities = {row[0] for row in r['rows']}
    assert 'Beijing' in cities and 'Shanghai' in cities
    assert len(r['rows']) == 2
    print("  ✅ UNION 去重生效")

    db.shutdown()


def test_union_all():
    """测试8: UNION ALL"""
    print("\n【测试8】UNION ALL")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = "SELECT city FROM users WHERE city='Beijing' UNION ALL SELECT city FROM users WHERE city='Beijing'"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 返回 {len(r['rows'])} 行（不去重）")
    # 期望值必须由 setup() 的同一份数据推导：cities 列表里 'Beijing'
    # 出现在下标 1..4 中的 2/3/4 三处，所以 i % 5 ∈ {2,3,4} 都是 Beijing
    # （12 行），不是只有 i % 5 == 2（4 行）。原断言把 12 行当成 4 行，
    # 因此恒为 8，属于测试本身的算术错误。
    cities = ['Beijing', 'Shanghai', 'Beijing', 'Beijing', 'Shenzhen']
    expected = sum(1 for u in range(1, 21)
                   if cities[u % len(cities)] == 'Beijing') * 2
    assert len(r['rows']) == expected, f"期望 {expected}, 实际 {len(r['rows'])}"
    print("  ✅ UNION ALL 不去重")

    db.shutdown()


def test_tpch_simplified():
    """测试9: TPC-H 风格查询"""
    print("\n【测试9】TPC-H 风格聚合")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    # Q1 简化版：城市订单统计
    sql = ("SELECT u.city, COUNT(*), SUM(o.amount) "
           "FROM users u JOIN orders o ON u.id = o.user_id "
           "GROUP BY u.city")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  列: {r['columns']}")
    for row in r['rows']:
        print(f"  → {row}")
    assert len(r['rows']) > 0

    db.shutdown()


def main():
    print("=" * 60)
    print("  Batch 7 测试 - 子查询 + UNION + GROUP BY")
    print("=" * 60)

    tests = [
        ('GROUP BY', test_group_by),
        ('GROUP BY + HAVING', test_group_by_having),
        ('DISTINCT', test_distinct),
        ('IN 子查询', test_in_subquery),
        ('NOT IN', test_not_in),
        ('EXISTS', test_exists),
        ('UNION', test_union),
        ('UNION ALL', test_union_all),
        ('TPC-H 聚合', test_tpch_simplified),
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
        print("  🎉 Batch 7 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())