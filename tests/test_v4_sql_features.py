"""SQL 特性回归：窗口函数 / CTE / UNION / GROUP BY / DISTINCT

这些能力原先大多不可用（UNION 缺执行器分支、CTE 完全没实现、窗口函数不存在、
GROUP BY 与 DISTINCT 被静默忽略）。本文件把它们锁住。

全部在**同一个 Database 实例**内完成，因此不依赖跨实例持久化。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data_sql_features_test')

EMP = [
    (1, 'Alice', 'Eng', 120), (2, 'Bob', 'Eng', 100),
    (3, 'Carol', 'Eng', 100), (4, 'Dave', 'Sales', 110),
    (5, 'Eve', 'Sales', 90), (6, 'Frank', 'HR', 95),
    (7, 'Grace', 'HR', 95), (8, 'Henry', 'HR', 85),
]


class _Env:
    def __enter__(self):
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.sql.parser import parser

        shutil.rmtree(DATA_DIR, ignore_errors=True)
        self.db = Database(DATA_DIR)
        self.ex = Executor(self.db)
        self.parser = parser
        self.ex.execute_sql(
            "CREATE TABLE emp (id INT PRIMARY KEY, name VARCHAR(20), "
            "dept VARCHAR(10), salary INT)", {})
        for r in EMP:
            self.ex.execute_sql(
                "INSERT INTO emp VALUES (%d,'%s','%s',%d)" % r, {})
        # 供 JOIN 用例使用：两表都有 id，用于验证限定名不歧义
        self.ex.execute_sql(
            "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(20), "
            "city VARCHAR(20))", {})
        self.ex.execute_sql(
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, "
            "amount INT)", {})
        i = 0
        for name, city in [('a', 'BJ'), ('b', 'BJ'), ('c', 'SH')]:
            i += 1
            self.ex.execute_sql(
                "INSERT INTO users VALUES (%d,'%s','%s')" % (i, name, city),
                {})
        oid = 0
        for uid in (1, 1, 2):
            oid += 1
            self.ex.execute_sql(
                "INSERT INTO orders VALUES (%d,%d,%d)" % (oid, uid, oid * 10),
                {})
        return self

    def __exit__(self, *exc):
        try:
            self.db.shutdown()
        except Exception:
            pass
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        return False

    def q(self, sql):
        r = self.ex.execute(self.parser.parse(sql), {})
        assert r.get('type') == 'resultset', \
            f'查询失败: {sql} -> {r.get("message")}'
        return r['columns'], [tuple(x) for x in r['rows']]


def test_window_functions():
    with _Env() as e:
        _, rows = e.q(
            "SELECT id, name, dept, ROW_NUMBER() OVER "
            "(PARTITION BY dept ORDER BY salary DESC) AS rn FROM emp")
        eng = [r for r in rows if r[2] == 'Eng']
        assert eng[0][3] == 1 and eng[1][3] == 2

        _, rows = e.q(
            "SELECT name, RANK() OVER (ORDER BY salary DESC) AS rk, "
            "DENSE_RANK() OVER (ORDER BY salary DESC) AS drk "
            "FROM emp ORDER BY salary DESC")
        assert rows[0][1] == 1 and rows[0][2] == 1

        _, rows = e.q(
            "SELECT name, LAG(salary, 1) OVER (ORDER BY id) AS prev, "
            "LEAD(salary, 1) OVER (ORDER BY id) AS nxt "
            "FROM emp ORDER BY id LIMIT 3")
        assert rows[0][1] is None and rows[0][2] == 100

        _, rows = e.q(
            "SELECT dept, SUM(salary) OVER (PARTITION BY dept "
            "ORDER BY salary DESC) AS running FROM emp")
        eng = [r for r in rows if r[0] == 'Eng']
        assert eng[0][1] == 120 and eng[1][1] == 220


def test_cte():
    with _Env() as e:
        _, rows = e.q(
            "WITH eng AS (SELECT id, name, salary FROM emp "
            "WHERE dept='Eng') SELECT * FROM eng WHERE salary > 100")
        assert len(rows) == 1 and rows[0][1] == 'Alice'

        # 多 CTE + 后面的 CTE 引用前面的 CTE
        _, rows = e.q(
            "WITH eng AS (SELECT id, name, salary FROM emp WHERE dept='Eng'), "
            "sales AS (SELECT id, name, salary FROM emp WHERE dept='Sales') "
            "SELECT * FROM eng UNION ALL SELECT * FROM sales")
        assert len(rows) == 5

        # CTE + 窗口函数：列名要带出来（rn）
        cols, rows = e.q(
            "WITH ranked AS (SELECT name, dept, salary, ROW_NUMBER() OVER "
            "(PARTITION BY dept ORDER BY salary DESC) AS rn FROM emp) "
            "SELECT * FROM ranked WHERE rn = 1")
        assert cols == ['name', 'dept', 'salary', 'rn']
        assert len(rows) == 3

        # CTE + 聚合
        _, rows = e.q(
            "WITH e AS (SELECT dept, SUM(salary) AS tot FROM emp "
            "GROUP BY dept) SELECT * FROM e ORDER BY tot DESC")
        assert rows[0] == ('Eng', 320) and len(rows) == 3


def test_union():
    with _Env() as e:
        _, rows = e.q(
            "SELECT dept FROM emp WHERE dept='Eng' UNION "
            "SELECT dept FROM emp WHERE dept='Sales'")
        assert len(rows) == 2, 'UNION 必须去重'

        _, rows = e.q(
            "SELECT dept FROM emp WHERE dept='Eng' UNION ALL "
            "SELECT dept FROM emp WHERE dept='Eng'")
        assert len(rows) == 6, 'UNION ALL 不能去重'

        # 同一分支内部的重复也要被 UNION 去掉
        _, rows = e.q("SELECT dept FROM emp UNION SELECT dept FROM emp")
        assert len(rows) == 3

        _, rows = e.q(
            "SELECT dept FROM emp UNION SELECT dept FROM emp "
            "UNION SELECT dept FROM emp")
        assert len(rows) == 3


def test_group_by_and_distinct():
    with _Env() as e:
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept")
        assert len(rows) == 3 and sum(r[1] for r in rows) == 8

        _, rows = e.q(
            "SELECT dept, SUM(salary) AS tot FROM emp GROUP BY dept "
            "ORDER BY tot DESC")
        assert rows[0] == ('Eng', 320) and rows[-1] == ('Sales', 200)

        _, rows = e.q("SELECT DISTINCT dept FROM emp")
        assert len(rows) == 3

        # 表别名限定名（限定符需要被归一化）
        _, rows = e.q(
            "SELECT u.dept, SUM(u.salary) AS tot FROM emp u "
            "GROUP BY u.dept ORDER BY tot DESC")
        assert rows[0] == ('Eng', 320)


def test_plain_path_not_broken():
    """普通查询（不走新路径）必须保持原行为"""
    with _Env() as e:
        _, rows = e.q("SELECT COUNT(*) FROM emp")
        assert rows == [(8,)]

        _, rows = e.q("SELECT * FROM emp")
        assert len(rows) == 8

        _, rows = e.q(
            "SELECT name FROM emp ORDER BY salary DESC LIMIT 2")
        assert len(rows) == 2 and rows[0][0] == 'Alice'

        _, rows = e.q("SELECT name FROM emp WHERE dept='HR'")
        assert len(rows) == 3


def test_aliased_join_and_aggregate():
    """带表别名的 JOIN 与 JOIN 聚合

    原先解析器不认表别名，`FROM users u JOIN orders o ON ...` 的 JOIN
    会被整个丢弃，查询静默退化成单表聚合（返回看似正常但错误的结果）。
    """
    with _Env() as e:
        # JOIN 聚合：a 有 2 笔订单、b 有 1 笔，都属 BJ；SH 无订单故不出现在 INNER JOIN
        _, rows = e.q(
            "SELECT u.city, COUNT(*), SUM(o.amount) FROM users u "
            "JOIN orders o ON u.id = o.user_id GROUP BY u.city")
        assert rows == [('BJ', 3, 60)], rows

        # 两表同名列：限定名必须精确解析，不能报歧义
        _, rows = e.q(
            "SELECT u.id, o.user_id FROM users u JOIN orders o "
            "ON u.id = o.user_id")
        assert len(rows) == 3

        # LEFT JOIN 保留未匹配行
        _, rows = e.q(
            "SELECT u.name, o.amount FROM users u LEFT JOIN orders o "
            "ON u.id = o.user_id")
        assert len(rows) == 4 and ('c', None) in rows, rows

        # 单表 + 别名：WHERE / ORDER BY / GROUP BY 里的限定名
        _, rows = e.q(
            "SELECT u.name FROM users u WHERE u.city = 'BJ'")
        assert sorted(r[0] for r in rows) == ['a', 'b']

        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "ORDER BY u.city")
        assert rows == [('BJ', 2), ('SH', 1)], rows

        # 无别名写法必须保持原行为
        _, rows = e.q(
            "SELECT * FROM users JOIN orders "
            "ON users.id = orders.user_id")
        assert len(rows) == 3


def test_in_not_in_exists_subquery():
    """WHERE 里的子查询条件：IN / NOT IN / EXISTS

    解析器早就能产出这些条件，缺的是求值接线：执行器现在先把子查询跑一次，
    再把条件改写成存储层能下推的等值 or / and 链（或常量真/假）。

    本用例数据：users 1..3；orders 的 user_id = 1,1,2
    → 有订单的用户 = {1,2}，无订单 = {3}
    """
    with _Env() as e:
        # IN (SELECT ...)
        _, rows = e.q("SELECT id FROM users "
                      "WHERE id IN (SELECT user_id FROM orders)")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # NOT IN (SELECT ...)
        _, rows = e.q("SELECT id FROM users "
                      "WHERE id NOT IN (SELECT user_id FROM orders)")
        assert [r[0] for r in rows] == [3], rows

        # EXISTS：子查询非空 → 全真，users 3 行都要返回
        _, rows = e.q("SELECT id FROM users "
                      "WHERE EXISTS (SELECT 1 FROM orders)")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows

        # NOT EXISTS：子查询非空 → 全假
        _, rows = e.q("SELECT id FROM users "
                      "WHERE NOT EXISTS (SELECT 1 FROM orders)")
        assert rows == [], rows

        # 空结果集的两条边界：IN 恒假、NOT IN（含 NULL 左值语义）恒真
        _, rows = e.q("SELECT id FROM users WHERE id IN "
                      "(SELECT user_id FROM orders WHERE user_id > 100)")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN "
                      "(SELECT user_id FROM orders WHERE user_id > 100)")
        assert len(rows) == 3, rows

        # 子查询里还能有子查询
        _, rows = e.q(
            "SELECT id FROM users WHERE id IN (SELECT user_id FROM orders "
            "WHERE user_id IN (SELECT id FROM users WHERE city = 'BJ'))")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # 子查询条件与普通条件 AND / OR 混用（选择性必须仍然生效）
        _, rows = e.q(
            "SELECT id FROM users WHERE city = 'SH' "
            "AND id IN (SELECT user_id FROM orders)")
        assert rows == [], rows
        _, rows = e.q(
            "SELECT id FROM users WHERE city = 'SH' "
            "OR id IN (SELECT user_id FROM orders)")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows

        # 相关子查询必须明确报错，不能静默给错答案
        r = e.ex.execute(
            e.parser.parse(
                "SELECT id FROM users WHERE id IN (SELECT user_id FROM "
                "orders WHERE orders.user_id = users.id)"), {})
        assert r.get('type') == 'error', r
        assert '相关子查询' in r.get('message', ''), r


def test_having_aggregate_predicate():
    """HAVING：左值为聚合函数的谓词 + 引用非聚合列

    原先 `HAVING COUNT(*) > 1` 连解析都过不去（_where 只认普通列名，
    直接 SyntaxError）。现在解析成 {'agg': 'COUNT(*)', 'op': '>', ...}，
    由分组阶段用聚合求值后比较。

    本用例 emp：Eng 3 人 / Sales 2 人 / HR 3 人
    """
    with _Env() as e:
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING COUNT(*) > 2")
        assert sorted(rows) == [('Eng', 3), ('HR', 3)], rows

        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING COUNT(*) = 2")
        assert rows == [('Sales', 2)], rows

        # HAVING 里的聚合可以不在 SELECT 列表里
        _, rows = e.q("SELECT dept FROM emp GROUP BY dept "
                      "HAVING SUM(salary) >= 300")
        assert rows == [('Eng',)], rows

        # HAVING 引用非聚合列
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING dept = 'HR'")
        assert rows == [('HR', 3)], rows

        # HAVING 里的 AND / OR 组合
        _, rows = e.q("SELECT dept FROM emp GROUP BY dept "
                      "HAVING COUNT(*) > 2 AND SUM(salary) > 300")
        assert rows == [('Eng',)], rows
        _, rows = e.q("SELECT dept FROM emp GROUP BY dept "
                      "HAVING COUNT(*) = 2 OR SUM(salary) < 300")
        assert sorted(r[0] for r in rows) == ['HR', 'Sales'], rows

        # 没有 GROUP BY 的 HAVING：整表算一行再过滤
        _, rows = e.q("SELECT COUNT(*) FROM emp HAVING COUNT(*) > 5")
        assert rows == [(8,)], rows
        _, rows = e.q("SELECT COUNT(*) FROM emp HAVING COUNT(*) > 50")
        assert rows == [], rows

        # GROUP BY + HAVING 与 ORDER BY / LIMIT / 限定列名共存
        _, rows = e.q("SELECT e.dept, COUNT(*) FROM emp e GROUP BY e.dept "
                      "HAVING COUNT(*) >= 3 ORDER BY e.dept")
        assert rows == [('Eng', 3), ('HR', 3)], rows
