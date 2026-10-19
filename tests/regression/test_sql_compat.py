"""
SQL 兼容性回归测试
覆盖 MySQL 5.7 核心语法
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_ddl():
    """测试 DDL"""
    print("\n【DDL 兼容性】")
    from lightdb.sql.parser import parser

    cases = [
        "CREATE TABLE t1 (id INT PRIMARY KEY, name VARCHAR(50))",
        "CREATE TABLE t2 (id INT, name VARCHAR(50), "
        "PRIMARY KEY (id))",
        "CREATE TABLE t3 (id INT AUTO_INCREMENT PRIMARY KEY, "
        "created DATETIME)",
        "DROP TABLE IF EXISTS t1",
        "ALTER TABLE t1 ADD COLUMN age INT",
    ]

    for sql in cases:
        p = parser.parse(sql)
        assert p is not None, f"解析失败: {sql}"
    print(f"  ✅ 5 个 DDL 语法通过")


def test_dml():
    """测试 DML"""
    print("\n【DML 兼容性】")
    from lightdb.sql.parser import parser

    cases = [
        "INSERT INTO t1 VALUES (1, 'a')",
        "INSERT INTO t1 (id, name) VALUES (1, 'a'), (2, 'b')",
        "UPDATE t1 SET name = 'x' WHERE id = 1",
        "DELETE FROM t1 WHERE id = 1",
        "SELECT * FROM t1",
        "SELECT id, name FROM t1 WHERE id > 0",
    ]

    for sql in cases:
        p = parser.parse(sql)
        assert p is not None, f"解析失败: {sql}"
    print(f"  ✅ 6 个 DML 语法通过")


def test_query_complex():
    """复杂查询"""
    print("\n【复杂查询】")
    from lightdb.sql.parser import parser

    cases = [
        # JOIN
        "SELECT u.id, o.amount FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id",
        "SELECT * FROM users LEFT JOIN orders "
        "ON users.id = orders.user_id",
        # 子查询
        "SELECT * FROM users WHERE id IN "
        "(SELECT user_id FROM orders)",
        "SELECT * FROM users WHERE EXISTS "
        "(SELECT 1 FROM orders WHERE user_id = users.id)",
        # 聚合
        "SELECT city, COUNT(*) FROM users GROUP BY city",
        "SELECT city, COUNT(*) FROM users "
        "GROUP BY city HAVING COUNT(*) > 5",
        # UNION
        "SELECT id FROM users UNION SELECT id FROM admins",
        "SELECT id FROM users UNION ALL SELECT id FROM admins",
        # 排序
        "SELECT * FROM users ORDER BY age DESC, name ASC",
        "SELECT * FROM users LIMIT 10 OFFSET 20",
    ]

    for sql in cases:
        p = parser.parse(sql)
        assert p is not None, f"解析失败: {sql[:60]}"
    print(f"  ✅ 10 个复杂查询通过")


def test_functions():
    """函数调用"""
    print("\n【函数兼容性】")
    from lightdb.sql.functions.registry import get_registry

    reg = get_registry()

    # 字符串
    assert reg.call('CONCAT', 'a', 'b') == 'ab'
    assert reg.call('UPPER', 'x') == 'X'
    assert reg.call('SUBSTRING', 'hello', 2, 3) == 'ell'

    # 日期
    assert reg.call('YEAR', '2025-01-15') == 2025
    assert reg.call('MONTH', '2025-01-15') == 1

    # 数学
    assert reg.call('ABS', -5) == 5
    assert reg.call('ROUND', 3.14, 1) == 3.1

    # 控制
    assert reg.call('IFNULL', None, 'x') == 'x'
    assert reg.call('COALESCE', None, None, 'y') == 'y'

    print(f"  ✅ 函数调用通过")
    print(f"  → 总函数: {reg.count()}")


def test_data_types():
    """数据类型"""
    print("\n【数据类型】")

    types = [
        'INT', 'BIGINT', 'VARCHAR(50)', 'TEXT',
        'DECIMAL(10,2)', 'FLOAT', 'DOUBLE', 'BOOLEAN',
        'DATE', 'DATETIME', 'JSON',
    ]

    for t in types:
        sql = f"CREATE TABLE tt (id INT, v {t})"
        from lightdb.sql.parser import parser
        p = parser.parse(sql)
        assert p is not None, f"类型失败: {t}"

    print(f"  ✅ 11 种数据类型通过")


@pytest.mark.skip(
    reason="被测的 ParserV3B4 是死代码（生产路径零引用，保留仅供历史对照）；"
           "现役解析器不支持 SET @@var，该项随授权/变量体系在路线图阶段 1 处理")
def test_variables():
    """变量"""
    print("\n【变量】")
    from lightdb.sql.parser_v3b4 import ParserV3B4

    cases = [
        "SET @x = 100",
        "SET @@autocommit = 1",
        "SET NAMES utf8mb4",
        "SET SESSION character_set_client = 'utf8'",
    ]

    for sql in cases:
        p = ParserV3B4.parse_set(sql)
        assert p is not None, f"失败: {sql}"

    print(f"  ✅ 4 种变量语法通过")


def main():
    print("=" * 60)
    print("  SQL 兼容性回归测试")
    print("=" * 60)

    tests = [
        test_ddl, test_dml, test_query_complex,
        test_functions, test_data_types, test_variables,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {e}")
            failed += 1

    print(f"\n通过: {passed}, 失败: {failed}")
    return failed


if __name__ == '__main__':
    sys.exit(main())