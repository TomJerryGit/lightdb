"""Batch 5 测试 - 优化器 + EXPLAIN + 统计"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b5')


def reset():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def test_stats_collection():
    """测试1: 统计信息收集"""
    print("\n【测试1】统计信息收集")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = Database(DATA_DIR)
    ex = Executor(db)
    db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'name', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'city', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    tbl = db.get_table('users')

    import random
    cities = ['Beijing', 'Shanghai', 'Guangzhou', 'Shenzhen', 'Chengdu']
    for i in range(3000):
        tbl.insert([i + 1, f'user{i}', random.choice(cities)])

    # ANALYZE
    r = ex.execute({'command': 'ANALYZE', 'table': 'users'}, {})
    print(f"  → {r}")

    st = ex.stats_mgr.get('users')
    print(f"  ✅ 行数={st.row_count}, 页数={st.page_count}")
    print(f"  ✅ city 列 distinct={st.col_stats['city']['distinct']}")
    assert st.row_count == 3000
    assert st.col_stats['city']['distinct'] >= 3

    # 选择率
    sel = ex.stats_mgr.estimate_selectivity('users',
        {'column': 'city', 'op': '=', 'value': 'Beijing'})
    print(f"  ✅ city='Beijing' 选择率≈{sel:.4f}")

    sel = ex.stats_mgr.estimate_selectivity('users',
        {'column': 'id', 'op': '=', 'value': 100})
    print(f"  ✅ id=100 选择率≈{sel:.6f}")
    assert sel < 0.01

    db.shutdown()


def test_explain():
    """测试2: EXPLAIN 输出"""
    print("\n【测试2】EXPLAIN 输出")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)  # 复用上次数据
    ex = Executor(db)

    # 全表扫描
    print("\n  → EXPLAIN SELECT * FROM users")
    p = parser.parse("EXPLAIN SELECT * FROM users WHERE city = 'Beijing'")
    r = ex.execute(p, {})
    for line in r['rows']:
        print(f"    {line[0]}")

    # 主键查询
    print("\n  → EXPLAIN SELECT * FROM users WHERE id = 100")
    p = parser.parse("EXPLAIN SELECT * FROM users WHERE id = 100")
    r = ex.execute(p, {})
    for line in r['rows']:
        print(f"    {line[0]}")

    db.shutdown()


def test_index_selection():
    """测试3: CBO 选索引"""
    print("\n【测试3】CBO 索引选择")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    # 主键等值查询应该走 IndexScan
    p = parser.parse("SELECT * FROM users WHERE id = 100")
    plan = ex.optimizer.optimize(p)
    from lightdb.sql.plan import print_plan
    lines = print_plan(plan)
    print("  计划：")
    for l in lines:
        print(f"    {l}")

    # 找到最底层的扫描节点
    scans = ex.optimizer._collect_scans(plan)
    print(f"  → 扫描节点: {scans}")
    assert any(s.name == 'IndexScan' for s in scans), "主键查询应走 IndexScan"

    # 无 WHERE 应该走全表扫描
    p2 = parser.parse("SELECT * FROM users")
    plan2 = ex.optimizer.optimize(p2)
    scans2 = ex.optimizer._collect_scans(plan2)
    print(f"  → 全表查询扫描: {scans2}")
    assert scans2[0].name == 'SeqScan'

    db.shutdown()


def test_predicate_pushdown():
    """测试4: 谓词下推"""
    print("\n【测试4】谓词下推")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    p = parser.parse("SELECT * FROM users WHERE id > 100 AND name = 'x'")
    plan = ex.optimizer.optimize(p)
    from lightdb.sql.plan import print_plan
    lines = print_plan(plan)
    print("  优化后计划：")
    for l in lines:
        print(f"    {l}")

    # 谓词应该合并到扫描节点
    scans = ex.optimizer._collect_scans(plan)
    assert scans[0].filter is not None, "谓词应下推"
    print("  ✅ 谓词已下推到扫描节点")

    db.shutdown()


def test_show_stats():
    """测试5: SHOW STATS"""
    print("\n【测试5】SHOW STATS")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = Database(DATA_DIR)
    ex = Executor(db)
    r = ex.execute({'command': 'SHOW STATS'}, {})
    print(f"  列: {r['columns']}")
    for row in r['rows'][:5]:
        print(f"  → {row}")
    assert len(r['rows']) > 0

    db.shutdown()


def test_query_with_index():
    """测试6: 走索引查询性能"""
    print("\n【测试6】索引查询 vs 全表扫描")
    import time
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    # 主键查询（走索引）
    t0 = time.time()
    for i in [10, 500, 1000, 2000, 3000]:
        p = parser.parse(f"SELECT * FROM users WHERE id = {i}")
        ex.execute(p, {})
    t1 = time.time()
    print(f"  5 次主键查询: {(t1 - t0) * 1000:.2f}ms")

    # 无索引列查询（全扫描）
    t0 = time.time()
    for _ in range(5):
        p = parser.parse("SELECT * FROM users WHERE name = 'user100'")
        ex.execute(p, {})
    t1 = time.time()
    print(f"  5 次非索引列查询: {(t1 - t0) * 1000:.2f}ms")

    db.shutdown()


def main():
    print("=" * 60)
    print("  Batch 5 测试 - 优化器 + EXPLAIN + 统计")
    print("=" * 60)

    tests = [
        ('统计信息收集', test_stats_collection),
        ('EXPLAIN', test_explain),
        ('CBO索引选择', test_index_selection),
        ('谓词下推', test_predicate_pushdown),
        ('SHOW STATS', test_show_stats),
        ('索引查询', test_query_with_index),
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
        print("  🎉 Batch 5 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())