"""Batch 13 测试 - JSON + 全文索引 + 分区 + 生成列 + CHECK"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b13')


def reset():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def test_json_functions():
    """测试1: JSON 函数"""
    print("\n【测试1】JSON 函数")
    from lightdb.sql.json_func import (
        json_extract, json_set_func, json_keys, json_length, json_type, json_valid,
    )

    doc = '{"name": "Alice", "age": 30, "tags": ["a", "b", "c"]}'

    assert json_extract(doc, '$.name') == 'Alice'
    assert json_extract(doc, '$.age') == 30
    assert json_extract(doc, '$.tags[1]') == 'b'
    print("  ✅ json_extract")

    assert json_length(doc) == 3
    print("  ✅ json_length")

    assert json_type(doc) == 'OBJECT'
    assert json_valid(doc) is True
    assert json_valid('{invalid') is False
    print("  ✅ json_type / json_valid")

    result = json_set_func(doc, '$.name', 'Bob')
    assert '"Bob"' in result
    print(f"  ✅ json_set → {result[:60]}...")

    keys = json_keys(doc)
    assert 'name' in keys and 'age' in keys
    print("  ✅ json_keys")


def test_json_in_table():
    """测试2: 表中存 JSON"""
    print("\n【测试2】表中存 JSON")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    db.create_table('docs', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'data', 'type': 'JSON', 'primary': False, 'notnull': False, 'unique': False},
    ])
    tbl = db.get_table('docs')

    tbl.insert([1, '{"name": "Alice", "age": 30}'])
    tbl.insert([2, '{"name": "Bob", "age": 25}'])
    print("  ✅ 插入两条 JSON")

    # SELECT with -> 
    sql = "SELECT id, data FROM docs WHERE data->'$.name' = 'Alice'"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 结果: {r['rows']}")
    assert len(r['rows']) == 1
    assert r['rows'][0][0] == 1
    print("  ✅ JSON 路径查询生效")

    db.shutdown()


def test_fulltext():
    """测试3: 全文索引"""
    print("\n【测试3】全文索引")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    db.create_table('articles', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'content', 'type': 'TEXT', 'primary': False, 'notnull': False, 'unique': False},
    ])
    tbl = db.get_table('articles')

    tbl.insert([1, 'Python is a programming language'])
    tbl.insert([2, 'Java is also a programming language'])
    tbl.insert([3, 'Cooking is a hobby'])
    tbl.insert([4, 'Python and Java are both popular'])
    print("  ✅ 插入 4 篇文档")

    # 建全文索引
    tbl.create_fulltext_index('content')
    print("  ✅ 创建全文索引")

    # 搜索
    sql = "SELECT id FROM articles WHERE MATCH(content) AGAINST('python')"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → 搜索 'python': {r['rows']}")
    ids = [row[0] for row in r['rows']]
    assert 1 in ids and 4 in ids
    assert 3 not in ids
    print("  ✅ 全文检索 'python' 命中")

    sql = "SELECT id FROM articles WHERE MATCH(content) AGAINST('cooking hobby')"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    ids = [row[0] for row in r['rows']]
    print(f"  → 搜索 'cooking hobby': {ids}")
    assert 3 in ids
    print("  ✅ 全文检索 'cooking hobby' 命中")

    db.shutdown()


def test_partition_hash():
    """测试4: HASH 分区"""
    print("\n【测试4】HASH 分区")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = "CREATE TABLE logs (id INT PRIMARY KEY, msg VARCHAR) PARTITION BY HASH(id) PARTITIONS 4"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    tbl = db.get_table('logs')
    assert tbl._partition.enabled
    print(f"  ✅ 分区数: {len(tbl._partition.partitions)}")

    for i in range(20):
        tbl.insert([i + 1, f'msg{i}'])
    print(f"  ✅ 插入 20 行")

    # 查分区分布
    for p in tbl._partition.partitions:
        pks = [pk for pk, _, _ in tbl.bt.scan() if p.match(pk)]
        print(f"    {p.name}: {len(pks)} 行")

    # SHOW PARTITIONS
    p = parser.parse("SHOW PARTITIONS FROM logs")
    r = ex.execute(p, {})
    print(f"  → SHOW PARTITIONS: {r['rows']}")

    db.shutdown()


def test_partition_range():
    """测试5: RANGE 分区"""
    print("\n【测试5】RANGE 分区")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = ("CREATE TABLE sales (id INT PRIMARY KEY, amount INT) "
           "PARTITION BY RANGE(id) ("
           "PARTITION p0 VALUES LESS THAN (100), "
           "PARTITION p1 VALUES LESS THAN (200), "
           "PARTITION p2 VALUES LESS THAN MAXVALUE"
           ")")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    tbl = db.get_table('sales')
    for i in [50, 150, 250]:
        tbl.insert([i, i * 10])
    print("  ✅ 插入 3 行 (50, 150, 250)")

    # 分区裁剪
    pruned = tbl._partition.prune({'column': 'id', 'op': '=', 'value': 150})
    print(f"  → WHERE id=150 裁剪到: {pruned}")
    assert pruned == ['p1']

    db.shutdown()


def test_generated_column():
    """测试6: 生成列"""
    print("\n【测试6】生成列")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = ("CREATE TABLE users ("
           "  id INT PRIMARY KEY, "
           "  name VARCHAR, "
           "  name_upper VARCHAR GENERATED ALWAYS AS (UPPER(name)) STORED, "
           "  age INT"
           ")")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    tbl = db.get_table('users')
    tbl.insert([1, 'alice', None, 30])
    tbl.insert([2, 'bob', None, 25])
    print("  ✅ 插入 2 行（生成列自动填充）")

    cols, rows = tbl.select(['*'])
    print(f"  → 结果:")
    for r in rows:
        print(f"    {r}")
    assert rows[0][2] == 'ALICE'
    assert rows[1][2] == 'BOB'
    print("  ✅ 生成列 UPPER(name) 生效")

    db.shutdown()


def test_check_constraint():
    """测试7: CHECK 约束"""
    print("\n【测试7】CHECK 约束")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = Database(DATA_DIR)
    ex = Executor(db)

    sql = ("CREATE TABLE products ("
           "  id INT PRIMARY KEY, "
           "  price INT CHECK (price > 0), "
           "  stock INT"
           ")")
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    tbl = db.get_table('products')
    tbl.insert([1, 100, 10])
    print("  ✅ 合法插入成功")

    # 非法插入
    try:
        tbl.insert([2, -5, 10])
        print("  ❌ CHECK 未生效！")
        assert False
    except ValueError as e:
        print(f"  ✅ CHECK 拦截: {str(e)[:60]}")

    db.shutdown()


def test_json_with_fulltext():
    """测试8: 组合场景 - JSON + 全文"""
    print("\n【测试8】组合场景")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.parser import parser
    from lightdb.sql.executor import Executor

    db = Database(DATA_DIR)
    ex = Executor(db)

    db.create_table('blog', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'title', 'type': 'TEXT', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'meta', 'type': 'JSON', 'primary': False, 'notnull': False, 'unique': False},
    ])
    tbl = db.get_table('blog')
    tbl.insert([1, 'Python tutorial', '{"author": "Alice", "year": 2023}'])
    tbl.insert([2, 'Database design', '{"author": "Bob", "year": 2022}'])
    tbl.insert([3, 'Advanced Python', '{"author": "Alice", "year": 2024}'])
    tbl.create_fulltext_index('title')

    # MATCH + JSON 组合
    sql = "SELECT id, meta FROM blog WHERE MATCH(title) AGAINST('python')"
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → MATCH 'python': {r['rows']}")
    assert len(r['rows']) == 2

    db.shutdown()


def main():
    print("=" * 60)
    print("  Batch 13 测试 - JSON + 全文 + 分区 + 生成列 + CHECK")
    print("=" * 60)

    tests = [
        ('JSON 函数', test_json_functions),
        ('表中 JSON', test_json_in_table),
        ('全文索引', test_fulltext),
        ('HASH 分区', test_partition_hash),
        ('RANGE 分区', test_partition_range),
        ('生成列', test_generated_column),
        ('CHECK 约束', test_check_constraint),
        ('组合场景', test_json_with_fulltext),
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
        print("  🎉 Batch 13 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())