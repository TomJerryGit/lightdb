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

        # 相关子查询：原先"明确报错"，任务2 之后改为**真正求值**
        # （下面的断言随之更新为正确结果，不是放宽检查：
        #   `orders.user_id = users.id` 只在 users 1/2 上成立）
        _, rows = e.q(
            "SELECT id FROM users WHERE id IN (SELECT user_id FROM "
            "orders WHERE orders.user_id = users.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows


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


# ==========================================================================
# 以下为后续追加：窗口帧 / LIMIT OFFSET / 递归 CTE / 相关子查询 / NULL 三值
# ==========================================================================

# emp 按 id 的 salary: [120,100,100,110,90,95,95,85]，累计和 795
EMP_SAL = [120, 100, 100, 110, 90, 95, 95, 85]


def _cumsum(n=8):
    out, acc = [], 0
    for v in EMP_SAL[:n]:
        acc += v
        out.append(acc)
    return out


def test_window_frames():
    """窗口帧 ROWS BETWEEN ...：至少覆盖 5 种边界组合

    帧按「分区内排序位置」取物理行（ROWS 语义）。无帧子句时保持原语义
    （有 ORDER BY 累计、无 ORDER BY 整分区），这里一并锁住。
    """
    with _Env() as e:
        # ① ROWS BETWEEN 1 PRECEDING AND CURRENT ROW（滑动窗口和）
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY id ROWS BETWEEN "
            "1 PRECEDING AND CURRENT ROW) AS s FROM emp ORDER BY id")
        expect = [EMP_SAL[0]] + [EMP_SAL[i - 1] + EMP_SAL[i]
                                 for i in range(1, 8)]
        assert [r[1] for r in rows] == expect, rows

        # ② ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY id ROWS BETWEEN "
            "CURRENT ROW AND 1 FOLLOWING) AS s FROM emp ORDER BY id")
        expect = [EMP_SAL[i] + (EMP_SAL[i + 1] if i < 7 else 0)
                  for i in range(8)]
        assert [r[1] for r in rows] == expect, rows

        # ③ ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING（三行滑动和）
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY id ROWS BETWEEN "
            "1 PRECEDING AND 1 FOLLOWING) AS s FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [220, 320, 310, 300, 295, 280, 275, 180], rows

        # ④ 简写 ROWS UNBOUNDED PRECEDING = UNBOUNDED PRECEDING→CURRENT ROW
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY id ROWS UNBOUNDED "
            "PRECEDING) AS s FROM emp ORDER BY id")
        assert [r[1] for r in rows] == _cumsum(), rows

        # ⑤ ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY id ROWS BETWEEN "
            "UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM emp "
            "ORDER BY id")
        assert [r[1] for r in rows] == [795] * 8, rows

        # 帧同样作用于 COUNT / MIN / MAX / AVG
        _, rows = e.q(
            "SELECT COUNT(*) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
            "1 FOLLOWING) AS c, MIN(salary) OVER (ORDER BY id ROWS BETWEEN "
            "1 PRECEDING AND 1 FOLLOWING) AS mn, MAX(salary) OVER (ORDER BY "
            "id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS mx, "
            "AVG(salary) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING "
            "AND UNBOUNDED FOLLOWING) AS av FROM emp ORDER BY id")
        assert [r[0] for r in rows] == [2, 3, 3, 3, 3, 3, 3, 2], rows
        assert [r[1] for r in rows] == [100, 100, 100, 90, 90, 90, 85, 85], rows
        assert [r[2] for r in rows] == [120, 120, 110, 110, 110, 95, 95, 95], rows
        assert [r[3] for r in rows] == [795 / 8] * 8, rows

        # 分区 + 帧：帧只在分区内滑动
        _, rows = e.q(
            "SELECT dept, SUM(salary) OVER (PARTITION BY dept ORDER BY salary "
            "DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s "
            "FROM emp")
        eng = [r[1] for r in rows if r[0] == 'Eng']
        hr = [r[1] for r in rows if r[0] == 'HR']
        sales = [r[1] for r in rows if r[0] == 'Sales']
        assert eng == [120, 220, 320], rows
        assert sales == [110, 200], rows
        assert sorted(hr) == [95, 190, 275], rows

        # ROW_NUMBER/RANK/LAG 忽略帧（标准行为，不能因为带帧就变累计）
        _, rows = e.q(
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id ROWS BETWEEN "
            "1 PRECEDING AND CURRENT ROW) AS rn FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8], rows

        # ---- 无帧时既有行为不得回归 ----
        _, rows = e.q("SELECT id, SUM(salary) OVER (ORDER BY id) AS s "
                      "FROM emp ORDER BY id")
        assert [r[1] for r in rows] == _cumsum(), rows
        _, rows = e.q("SELECT SUM(salary) OVER () AS s FROM emp")
        assert [r[0] for r in rows] == [795] * 8, rows

        # ---- 语法不合法必须明确报错，不能静默忽略 ----
        import pytest
        for bad in (
            "SELECT SUM(salary) OVER (ORDER BY id RANGE BETWEEN 1 PRECEDING "
            "AND CURRENT ROW) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING) "
            "AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id ROWS 3 PRECEDING AND "
            "CURRENT ROW) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id ROWS BETWEEN 2 FOLLOWING "
            "AND 1 FOLLOWING) AS s FROM emp",
        ):
            with pytest.raises(SyntaxError):
                e.parser.parse(bad)


def test_limit_offset():
    """LIMIT ... OFFSET：两种写法 + 普通/高级/JOIN 三条执行路径

    解析器原先只取 LIMIT 的 n，OFFSET 被静默丢掉（`LIMIT 2 OFFSET 6`
    会返回前 2 行而不是最后 2 行）。
    """
    with _Env() as e:
        # 按 salary DESC：120 Alice,110 Dave,100 Bob,100 Carol,
        #                 95 Frank,95 Grace,90 Eve,85 Henry
        # ① LIMIT n OFFSET m
        _, rows = e.q("SELECT name, salary FROM emp ORDER BY salary DESC "
                      "LIMIT 2 OFFSET 1")
        assert rows == [('Dave', 110), ('Bob', 100)], rows

        # ② LIMIT m, n（MySQL 写法，等价于 LIMIT n OFFSET m）
        _, rows = e.q("SELECT name, salary FROM emp ORDER BY salary DESC "
                      "LIMIT 1, 2")
        assert rows == [('Dave', 110), ('Bob', 100)], rows

        # ③ OFFSET 越界取尾部
        _, rows = e.q("SELECT name, salary FROM emp ORDER BY salary DESC "
                      "LIMIT 2 OFFSET 6")
        assert rows == [('Eve', 90), ('Henry', 85)], rows

        # ④ 高级路径（DISTINCT 触发 _finish_select 的裁剪）
        _, rows = e.q("SELECT DISTINCT salary FROM emp ORDER BY salary DESC "
                      "LIMIT 3 OFFSET 2")
        assert rows == [(100,), (95,), (90,)], rows

        # ⑤ 窗口函数路径 + OFFSET
        _, rows = e.q("SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn "
                      "FROM emp ORDER BY id LIMIT 3 OFFSET 2")
        assert rows == [(3, 3), (4, 4), (5, 5)], rows

        # ⑥ JOIN 路径（计划树的 Limit 节点带 offset，不能静默丢弃）
        _, rows = e.q("SELECT u.city, o.amount FROM users u JOIN orders o "
                      "ON u.id = o.user_id ORDER BY o.id LIMIT 2 OFFSET 1")
        assert rows == [('BJ', 20), ('BJ', 30)], rows

        # 不带 OFFSET 的行为不变（注意：普通单表路径要求 ORDER BY 的列
        # 也在 SELECT 列表里，否则存储层排不了序，这是既有行为）
        _, rows = e.q("SELECT name, salary FROM emp ORDER BY salary DESC "
                      "LIMIT 2")
        assert rows == [('Alice', 120), ('Dave', 110)], rows


def test_recursive_cte():
    """递归 CTE：WITH RECURSIVE t AS (base UNION [ALL] recursive)

    迭代求解：第 0 轮算 base，之后每轮把 t 绑定为「上一轮新增的行」再求值
    递归分支并追加，直到无新增行为止。必须有迭代上限兜底。
    """
    with _Env() as e:
        # ① UNION ALL：1..5
        cols, rows = e.q(
            "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 "
            "FROM nums WHERE n < 5) SELECT * FROM nums")
        assert cols == ['n'], cols
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5], rows

        # ② UNION（去重）：同样 1..5
        cols, rows = e.q(
            "WITH RECURSIVE nums AS (SELECT 1 AS n UNION SELECT n + 1 "
            "FROM nums WHERE n < 5) SELECT * FROM nums")
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5], rows

        # ③ 递归体引用自身列 + WHERE + CTE 结果再被外层过滤
        _, rows = e.q(
            "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 2 "
            "FROM nums WHERE n < 9) SELECT n FROM nums WHERE n > 5")
        assert [r[0] for r in rows] == [7, 9], rows

        # ④ 多轮迭代：步长 3 到 10 以内
        _, rows = e.q(
            "WITH RECURSIVE seq AS (SELECT 1 AS n UNION ALL SELECT n + 3 "
            "FROM seq WHERE n < 10) SELECT n FROM seq")
        assert [r[0] for r in rows] == [1, 4, 7, 10], rows

        # ⑤ 无限递归必须返回明确错误，不能死循环
        r = e.ex.execute(e.parser.parse(
            "WITH RECURSIVE inf AS (SELECT 1 AS n UNION ALL SELECT n "
            "FROM inf) SELECT * FROM inf"), {})
        assert r.get('type') == 'error', r
        assert '迭代次数' in r.get('message', ''), r


def test_correlated_subqueries():
    """相关子查询：EXISTS / NOT EXISTS / IN 逐外层行求值

    原先 _check_correlated 一律报错；现在外层行作为作用域注入子查询，
    内层行按子查询缓存（只扫一遍表），因此 20 行规模也是即时完成。

    数据：users 1..3（a/b 属 BJ、c 属 SH），orders.user_id = 1,1,2
    → 有订单的用户 = {1,2}，无订单 = {3}
    """
    with _Env() as e:
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS "
                      "(SELECT 1 FROM orders o WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        _, rows = e.q("SELECT id FROM users u WHERE NOT EXISTS "
                      "(SELECT 1 FROM orders o WHERE o.user_id = u.id)")
        assert [r[0] for r in rows] == [3], rows

        # 非相关 IN（子查询里没有外层引用）不得回归
        _, rows = e.q("SELECT id FROM users u WHERE u.id IN "
                      "(SELECT o.user_id FROM orders o WHERE o.amount > 25)")
        assert [r[0] for r in rows] == [2], rows

        # 相关 IN / 相关 NOT IN
        _, rows = e.q("SELECT id FROM users u WHERE u.id IN "
                      "(SELECT o.user_id FROM orders o WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        _, rows = e.q("SELECT id FROM users u WHERE u.id NOT IN "
                      "(SELECT o.user_id FROM orders o WHERE o.user_id = u.id)")
        assert [r[0] for r in rows] == [3], rows

        # 相关条件与普通条件 AND / OR 混用
        _, rows = e.q("SELECT id FROM users u WHERE u.city = 'BJ' AND EXISTS "
                      "(SELECT 1 FROM orders o WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        _, rows = e.q("SELECT id FROM users u WHERE u.city = 'SH' OR EXISTS "
                      "(SELECT 1 FROM orders o WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows

        # 相关子查询里**引用外层列做聚合**：原先一律"明确报错"，
        # 现在改为外层列常量折叠 + 复用正常执行路径，因此真正求值。
        # 断言随之更新为正确结果（不是放宽检查：
        #   `COUNT(*) ... WHERE o.user_id = u.id` 对 users 1/2 为 2/1 行、
        #   对 users 3 为 0 行，EXISTS 语义下 1/2 命中、3 不命中）
        _, rows = e.q(
            "SELECT id FROM users u WHERE EXISTS (SELECT COUNT(*) FROM "
            "orders o WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows


def test_not_in_null_semantics():
    """NOT IN / IN 的 NULL 三值语义

    子查询返回 {1, NULL}：`id IN (...)` 对 1 为真、对 2 为假；
    `id NOT IN (...)` 因含 NULL 整体为 UNKNOWN → 所有行都不通过。
    字面量列表 `IN (1, 2, NULL)` 同理。
    """
    with _Env() as e:
        e.ex.execute_sql("INSERT INTO orders VALUES (4, NULL, 40)", {})
        _, rows = e.q("SELECT user_id FROM orders")
        assert sorted(r[0] for r in rows if r[0] is not None) == [1, 1, 2], rows
        assert any(r[0] is None for r in rows), rows

        # IN：NULL 不匹配任何值（含 NULL 左值），命中的仍是 1 和 2
        _, rows = e.q("SELECT id FROM users WHERE id IN "
                      "(SELECT user_id FROM orders)")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # NOT IN：结果集含 NULL → 整体 UNKNOWN → 0 行
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN "
                      "(SELECT user_id FROM orders)")
        assert rows == [], rows

        # 字面量列表同理
        _, rows = e.q("SELECT id FROM users WHERE id IN (1, 2, NULL)")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN (1, 2, NULL)")
        assert rows == [], rows

        # 不含 NULL 时行为不变
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN (1, 2)")
        assert [r[0] for r in rows] == [3], rows
        _, rows = e.q("SELECT id FROM users WHERE id IN (1, 3)")
        assert sorted(r[0] for r in rows) == [1, 3], rows


def test_where_top_level_split_by_paren_and_subquery():
    """WHERE 的 AND/OR 只在顶层切分（括号深度 + 字符串字面量感知）

    原实现用裸 `re.split(r'\\s+AND\\s+')`，没有深度概念，导致：
    * 子查询内部的 AND 被外层拆开 → 相关 EXISTS 直接 SyntaxError；
    * `(A OR B) AND C` 里的括号被当成普通字符 → SyntaxError。
    现在先剥平衡外层括号、再按顶层 OR（低优先级）→ 顶层 AND 切分。
    """
    import pytest

    with _Env() as e:
        # ① 相关 EXISTS：子查询里的 AND 属于子查询
        #    数据：orders = (uid 1, amt 10) (uid 1, amt 20) (uid 2, amt 30)
        #    → uid 1 命中 amt=20，uid 2 命中 amt=30，uid 3 没有订单
        _, rows = e.q(
            "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM orders o "
            "WHERE o.user_id = u.id AND o.amount > 15)")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        # 收紧阈值证明是「逐外层行」求值而不是把子查询一次性常量折叠
        _, rows = e.q(
            "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM orders o "
            "WHERE o.user_id = u.id AND o.amount > 25)")
        assert [r[0] for r in rows] == [2], rows
        _, rows = e.q(
            "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM orders o "
            "WHERE o.user_id = u.id AND o.amount > 35)")
        assert rows == [], rows

        # ② 括号分组 + 外层 AND：(Eng OR HR) AND salary >= 100
        _, rows = e.q(
            "SELECT id FROM emp WHERE (dept = 'Eng' OR dept = 'HR') "
            "AND salary >= 100")
        assert [r[0] for r in rows] == [1, 2, 3], rows

        # ③ 括号分组在内层：Eng AND (120 OR 100) → 3 行
        _, rows = e.q(
            "SELECT id FROM emp WHERE dept = 'Eng' "
            "AND (salary = 120 OR salary = 100)")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows
        assert len(rows) == 3, rows

        # ④ OR 与括号分组混用：Alice + HR 中 salary > 90 的 Frank/Grace
        _, rows = e.q(
            "SELECT id FROM emp WHERE name LIKE 'A%' "
            "OR (dept = 'HR' AND salary > 90)")
        assert sorted(r[0] for r in rows) == [1, 6, 7], rows

        # ⑤ 优先级：AND 比 OR 紧 → HR 全部 + Eng 中 salary=120 的 Alice
        _, rows = e.q(
            "SELECT id FROM emp WHERE dept = 'HR' "
            "OR dept = 'Eng' AND salary = 120")
        assert sorted(r[0] for r in rows) == [1, 6, 7, 8], rows

        # ⑥ 字符串字面量里的 AND / OR 不是运算符
        _, rows = e.q("SELECT id FROM emp WHERE name = 'A AND B' "
                      "OR dept = 'HR'")
        assert sorted(r[0] for r in rows) == [6, 7, 8], rows
        # 标识符里的 AND/OR 子串也不能被切开（'band' 是值，不是运算符）
        _, rows = e.q("SELECT id FROM emp WHERE name = 'band' "
                      "AND dept = 'Eng'")
        assert rows == [], rows

        # ⑦ 反例：真语法错误仍然抛 SyntaxError，不静默返回结果
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM emp WHERE dept = ")
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM emp WHERE (a = 1")
        # 多余的右括号同样是错误（不能把 `1)` 当字面量咽下去）
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM emp WHERE a = 1)")

        # ⑧ 既有单条件/多条件形态保持不变
        _, rows = e.q("SELECT id FROM emp WHERE salary >= 100 "
                      "AND dept = 'Eng' OR dept = 'HR'")
        assert sorted(r[0] for r in rows) == [1, 2, 3, 6, 7, 8], rows


def test_from_subquery_alias():
    """FROM (子查询) 别名：物化成内存关系后按普通单表执行

    实现走 CTE 同一条「内存关系」路径：子查询先求值成
    `_CTERelation(别名, columns, rows)`，外层查询的 table 指向该别名，
    WHERE / GROUP BY / HAVING / ORDER BY / LIMIT+OFFSET / 限定列名
    全部复用既有实现，执行完注销临时关系。
    """
    with _Env() as e:
        # ① 外层 WHERE 用别名限定列
        cols, rows = e.q(
            "SELECT * FROM (SELECT id, name, salary FROM emp "
            "WHERE dept = 'Eng') e WHERE e.salary > 100")
        assert cols == ['id', 'name', 'salary'], cols
        assert rows == [(1, 'Alice', 120)], rows

        # ② 外层 GROUP BY + ORDER BY（COUNT 之和 = 8 行）
        cols, rows = e.q(
            "SELECT e.dept, COUNT(*) FROM (SELECT dept, salary FROM emp) e "
            "GROUP BY e.dept ORDER BY e.dept")
        assert cols == ['dept', 'COUNT(*)'], cols
        assert rows == [('Eng', 3), ('HR', 3), ('Sales', 2)], rows
        assert sum(r[1] for r in rows) == 8, rows

        # ③ 子查询内部 GROUP BY + 聚合，外层 ORDER BY 聚合别名
        cols, rows = e.q(
            "SELECT * FROM (SELECT dept, SUM(salary) AS tot FROM emp "
            "GROUP BY dept) t ORDER BY tot DESC")
        assert cols == ['dept', 'tot'], cols
        assert len(rows) == 3, rows
        assert rows[0] == ('Eng', 320), rows

        # ④ 外层 LIMIT + OFFSET（子查询已排序，物化顺序保持）
        _, rows = e.q("SELECT * FROM (SELECT id FROM emp ORDER BY id) x "
                      "LIMIT 2 OFFSET 1")
        assert rows == [(2,), (3,)], rows

        # ⑤ 嵌套 FROM 子查询
        _, rows = e.q("SELECT * FROM (SELECT * FROM (SELECT id FROM emp) a) b "
                      "LIMIT 1")
        assert rows == [(1,)], rows

        # ⑥ 无 GROUP BY 的聚合作用在子查询结果上（整表一组）
        _, rows = e.q("SELECT COUNT(*) FROM (SELECT id FROM emp) x")
        assert rows == [(8,)], rows

        # ⑦ 外层 HAVING
        _, rows = e.q(
            "SELECT dept, COUNT(*) AS c FROM (SELECT dept FROM emp) e "
            "GROUP BY dept HAVING COUNT(*) > 2 ORDER BY dept")
        assert rows == [('Eng', 3), ('HR', 3)], rows

        # ⑧ 尚不支持的形态要明确报错，不能静默错答
        r = e.ex.execute(e.parser.parse(
            "SELECT * FROM (SELECT id FROM emp) e "
            "JOIN orders o ON e.id = o.user_id"), {})
        assert r.get('type') == 'error', r
        assert 'JOIN' in r.get('message', ''), r


def test_range_default_frame_peers():
    """RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW（SQL 默认帧）

    与 ROWS 的差别：RANGE 的 CURRENT ROW 是「对等行组（ORDER BY 值相同的
    整组）」，ROWS 只是一行。本实现只放开这一种 RANGE 形态（含简写
    `RANGE UNBOUNDED PRECEDING`），其它 RANGE 组合与 GROUPS 继续显式报错。
    注意：不写帧子句时的既有语义（有 ORDER BY 就逐行累计）保持不变。
    """
    import pytest

    with _Env() as e:
        # ① 不分区、ORDER BY dept：每个 dept 整组进帧
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY dept RANGE BETWEEN "
            "UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [320, 320, 320, 795, 795, 595, 595, 595], \
            rows

        # ② 简写 RANGE UNBOUNDED PRECEDING 等价于 BETWEEN ... AND CURRENT ROW
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY dept RANGE UNBOUNDED "
            "PRECEDING) AS s FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [320, 320, 320, 795, 795, 595, 595, 595], \
            rows

        # ③ 分区 + 值域 peer：Eng 的 (100,100) 同组 → 两条都是 200
        #    （外层 ORDER BY 只认单列，所以按 id 输出，便于逐行核对）
        _, rows = e.q(
            "SELECT id, dept, salary, SUM(salary) OVER (PARTITION BY dept "
            "ORDER BY salary RANGE BETWEEN UNBOUNDED PRECEDING AND "
            "CURRENT ROW) AS s FROM emp ORDER BY id")
        assert [r[3] for r in rows] == [320, 200, 200, 200, 90, 275, 275, 85], \
            rows

        # ④ ROWS 与 RANGE 的差异被真实区分（不能静默等价）
        _, rows = e.q(
            "SELECT id, SUM(salary) OVER (ORDER BY dept ROWS BETWEEN "
            "UNBOUNDED PRECEDING AND CURRENT ROW) AS rows_s, SUM(salary) "
            "OVER (ORDER BY dept RANGE BETWEEN UNBOUNDED PRECEDING AND "
            "CURRENT ROW) AS range_s FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [120, 220, 320, 705, 795, 415, 510, 595], \
            rows
        assert [r[2] for r in rows] == [320, 320, 320, 795, 795, 595, 595, 595], \
            rows
        assert [r[1] for r in rows] != [r[2] for r in rows], rows

        # ⑤ COUNT(*) / MAX 同样按 peer 组取帧
        _, rows = e.q(
            "SELECT id, COUNT(*) OVER (ORDER BY dept RANGE BETWEEN UNBOUNDED "
            "PRECEDING AND CURRENT ROW) AS c FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [3, 3, 3, 8, 8, 6, 6, 6], rows

        # ⑥ 其它 RANGE 组合 / GROUPS 必须显式报错（不静默降级成 ROWS）
        for bad in (
            "SELECT SUM(salary) OVER (ORDER BY id RANGE BETWEEN 1 PRECEDING "
            "AND CURRENT ROW) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id RANGE BETWEEN CURRENT ROW "
            "AND UNBOUNDED FOLLOWING) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id RANGE BETWEEN UNBOUNDED "
            "PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id GROUPS BETWEEN UNBOUNDED "
            "PRECEDING AND CURRENT ROW) AS s FROM emp",
            "SELECT SUM(salary) OVER (ORDER BY id GROUPS 2 PRECEDING) AS s "
            "FROM emp",
        ):
            with pytest.raises(SyntaxError):
                e.parser.parse(bad)

        # ⑦ 不写帧子句时的既有行为不得回归（逐行累计，不是 peer 整组）
        _, rows = e.q("SELECT id, SUM(salary) OVER (ORDER BY dept) AS s "
                      "FROM emp ORDER BY id")
        assert [r[1] for r in rows] == [120, 220, 320, 705, 795, 415, 510, 595], \
            rows


# ==========================================================================
# 以下为追加：相关子查询（标量/聚合/JOIN/GROUP BY）/ NOT 与 IS NULL /
# 投影算术表达式。数据仍用 _Env：
#   users 1..3（a/b 属 BJ、c 属 SH），orders.user_id = 1,1,2，amount = 10,20,30
#   emp 8 行（Eng 3 / Sales 2 / HR 3），salary 见 EMP_SAL
# ==========================================================================


def test_correlated_scalar_subquery():
    """标量相关子查询：`(SELECT COUNT(*) ... WHERE o.uid = u.id) > n`

    实现方式：对外层每一行把子查询 AST 里的「外层别名.列」折叠成字面量，
    再把折叠后的（已不相关的）子查询交给正常执行路径求值，最后比较标量。
    原先这种形态连解析都过不去（`(SELECT ...) > 1` 不是 _where 认识的谓词）。
    """
    with _Env() as e:
        # ① COUNT(*) > 1：只有 uid=1 有 2 笔订单
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) > 1")
        assert [r[0] for r in rows] == [1], rows

        # ② 逐外层行求值：阈值放宽到 > 0 后 uid 1/2 都命中，uid 3 无订单
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) > 0")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # ③ COUNT(*) = 0 的边界：空集上 COUNT 是 0 而不是 NULL，uid 3 命中
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) = 0")
        assert [r[0] for r in rows] == [3], rows

        # ④ 标量子查询与普通条件 AND / OR 混用
        _, rows = e.q("SELECT id FROM users u WHERE u.city = 'BJ' AND "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) >= 1")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        _, rows = e.q("SELECT id FROM users u WHERE u.city = 'SH' OR "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) > 1")
        assert sorted(r[0] for r in rows) == [1, 3], rows

        # ⑤ 子查询里含 JOIN：JOIN 只是子查询自身的连接，外层引用照样折叠
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS ("
                      "SELECT 1 FROM orders o JOIN users u2 "
                      "ON o.user_id = u2.id WHERE o.user_id = u.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # ⑥ 子查询里含 GROUP BY / HAVING，HAVING 再引用外层列
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS ("
                      "SELECT 1 FROM orders o WHERE o.user_id = u.id "
                      "GROUP BY o.user_id HAVING COUNT(*) >= 2)")
        assert [r[0] for r in rows] == [1], rows
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS ("
                      "SELECT 1 FROM orders o WHERE o.user_id = u.id "
                      "GROUP BY o.user_id HAVING SUM(o.amount) > 15)")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # ⑦ 标量聚合子查询：uid 1/2 各只有一组订单，SUM 都是 30 → 都命中；
        #    uid 3 的 SUM 是空集 → NULL，`NULL > n` 为 UNKNOWN，不通过
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT SUM(o.amount) FROM orders o "
                      "WHERE o.user_id = u.id) > 25")
        assert sorted(r[0] for r in rows) == [1, 2], rows
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT SUM(o.amount) FROM orders o "
                      "WHERE o.user_id = u.id) > 30")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT MAX(o.amount) FROM orders o "
                      "WHERE o.user_id = u.id) = 30")
        assert [r[0] for r in rows] == [2], rows
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT MAX(o.amount) FROM orders o "
                      "WHERE o.user_id = u.id) = 20")
        assert [r[0] for r in rows] == [1], rows

        # ⑧ 非聚合标量子查询：返回多行时**必须显式报错**
        #    （原断言 `assert [r[0] for r in rows] == [1]` 锁定了
        #     「静默取第一行」这个错误行为，已按正确语义改写，见文件末尾
        #     「标量上下文的行数语义」一节）
        msg = _assert_err(
            e, "SELECT id FROM users u WHERE (SELECT o.user_id FROM "
               "orders o WHERE o.user_id = u.id) = 1")
        assert '多行' in msg, msg

        # ⑧b 恰好返回 1 行的非聚合标量子查询仍要正确取值参与比较
        #     （uid=2 只有一笔订单 30；uid=1 的两笔都被 amount > 25 滤掉
        #      → 0 行 → NULL → 不通过）
        _, rows = e.q("SELECT id FROM users u WHERE (SELECT o.user_id FROM "
                      "orders o WHERE o.user_id = u.id AND o.amount > 25) = 2")
        assert [r[0] for r in rows] == [2], rows

        # ⑨ 反例：真语法错误仍然抛 SyntaxError
        import pytest
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM users u WHERE (SELECT COUNT(*) "
                           "FROM orders o WHERE o.user_id = u.id)")


def test_correlated_nested_and_multitable():
    """相关子查询的嵌套 / 多表外层 / 子查询结构不得被静默错答

    覆盖：嵌套子查询里的外层引用（折叠到底）、NOT EXISTS 与 GROUP BY 组合、
    以及"折叠后必须重新执行而非复用内层行缓存"的正确性（同一外层行只算
    一次、取值不同的外层行不能共用结果）。
    """
    with _Env() as e:
        # ① 嵌套子查询：内层子查询也引用最外层列（`user_id = u.id`），
        #    相关性判定必须递归进嵌套层，否则该子查询会被当成非相关
        #    一次性求值 → 恒为空（实测过：静默返回 0 行）
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM "
                      "orders o WHERE o.user_id IN (SELECT user_id FROM "
                      "orders WHERE user_id = u.id))")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # ①b 反例：嵌套子查询里的同名表是**内层自己的表**，不能误判成
        #     相关引用（外层也叫 users，但这里的 users 是嵌套层作用域）
        _, rows = e.q("SELECT id FROM users WHERE id IN (SELECT user_id "
                      "FROM orders WHERE user_id IN (SELECT id FROM users "
                      "WHERE city = 'BJ'))")
        assert sorted(r[0] for r in rows) == [1, 2], rows

        # ② NOT EXISTS + GROUP BY / HAVING：uid 1 的组 COUNT=2 命中 → 排除；
        #    uid 2 的组 COUNT=1 不命中、uid 3 过滤后没有组 → 都保留
        _, rows = e.q("SELECT id FROM users u WHERE NOT EXISTS (SELECT 1 "
                      "FROM orders o WHERE o.user_id = u.id "
                      "GROUP BY o.user_id HAVING COUNT(*) >= 2)")
        assert sorted(r[0] for r in rows) == [2, 3], rows

        # ③ 外层带 LIMIT：相关子查询必须在 LIMIT 之前逐行求值
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM "
                      "orders o WHERE o.user_id = u.id) ORDER BY u.id "
                      "LIMIT 1")
        assert [r[0] for r in rows] == [1], rows

        # ④ 多条相关谓词叠加（AND）：uid 1 有 20（>15）也有 10（<15）→ 命中；
        #    uid 2 只有 30，不满足"同时存在小于 15 的订单"
        _, rows = e.q("SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM "
                      "orders o WHERE o.user_id = u.id AND o.amount > 15) "
                      "AND EXISTS (SELECT 1 FROM orders o2 "
                      "WHERE o2.user_id = u.id AND o2.amount < 15)")
        assert [r[0] for r in rows] == [1], rows

        # ⑤ 逐行结果不同：id 1 → 2 笔、id 2 → 1 笔、id 3 → 0 笔。
        #    注意：这里用 WHERE 逐行判定（单表相关子查询路径）；
        #    投影列表里的标量子查询见文件后面的
        #    test_projection_scalar_subquery_*，JOIN 查询里的标量子查询见
        #    文件末尾的 test_join_*_scalar_subquery_*。
        _, rows = e.q("SELECT id FROM users u WHERE (SELECT COUNT(*) FROM "
                      "orders o WHERE o.user_id = u.id) = 2")
        assert [r[0] for r in rows] == [1], rows
        _, rows = e.q("SELECT id FROM users u WHERE (SELECT COUNT(*) FROM "
                      "orders o WHERE o.user_id = u.id) = 1")
        assert [r[0] for r in rows] == [2], rows

        # ⑥ FROM 子查询上的相关子查询（内存关系作为外层）
        _, rows = e.q("SELECT x.id FROM (SELECT id FROM users) x WHERE "
                      "EXISTS (SELECT 1 FROM orders o "
                      "WHERE o.user_id = x.id)")
        assert sorted(r[0] for r in rows) == [1, 2], rows


def test_not_condition():
    """WHERE NOT ( ... )：整体取反，可与 AND/OR 组合、可嵌套

    实现方式：
    * 解析器在顶层 AND/OR 切分之前处理 `NOT (...)`（要求配对括号正好在
      末尾，因此 AND 比 NOT 结合得更紧），产出 {'not': <子树>}；
    * 执行器 `_normalize_where` 用德摩根律把 NOT 推到叶子：比较条件翻转
      运算符（`=`↔`!=`、`<`↔`>=` …），NOT-AND/OR 互换，这样存储层的
      _match 拿到的永远是它能认识的形态（NOT 本身它不认，会 KeyError）；
    * LIKE 这类没有取反运算符的形态打 negate 标记，由执行器自己的三值
      求值器过滤（`NOT (NULL LIKE ...)` 仍为 UNKNOWN，不能放行）。

    数据：emp 8 行，Eng 3（1/2/3）、Sales 2（4/5）、HR 3（6/7/8）
    """
    with _Env() as e:
        # ① NOT 单个条件 → Sales 2 + HR 3 = 5 行
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept = 'Eng')")
        assert sorted(r[0] for r in rows) == [4, 5, 6, 7, 8], rows

        # ② NOT (A OR B) → 只剩 Sales 2 行
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept = 'Eng' OR "
                      "dept = 'HR')")
        assert sorted(r[0] for r in rows) == [4, 5], rows

        # ③ NOT (A AND B)：德摩根 → 非 A 或 非 B
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept = 'Eng' AND "
                      "salary >= 100)")
        assert sorted(r[0] for r in rows) == [4, 5, 6, 7, 8], rows

        # ④ 嵌套 NOT：取反两次等于原条件
        _, rows = e.q("SELECT id FROM emp WHERE NOT (NOT (dept = 'Eng'))")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows

        # ⑤ NOT 与 AND / OR 组合（AND 比 NOT 结合更紧）
        #    NOT (dept='Eng') = {4,5,6,7,8}，再要求 salary >= 95 → 去掉 Eve(90)、
        #    Henry(85)，剩 Dave(110) + Frank/Grace(95)
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept = 'Eng') AND "
                      "salary >= 95")
        assert sorted(r[0] for r in rows) == [4, 6, 7], rows
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept = 'Eng') OR "
                      "salary = 120")
        assert sorted(r[0] for r in rows) == [1, 4, 5, 6, 7, 8], rows

        # ⑥ 对 LIKE 取反（存储层没有 NOT LIKE，走执行器三值求值）
        _, rows = e.q("SELECT id FROM emp WHERE NOT (name LIKE 'A%')")
        assert sorted(r[0] for r in rows) == [2, 3, 4, 5, 6, 7, 8], rows

        # ⑦ 对 IN 列表 / 子查询取反
        _, rows = e.q("SELECT id FROM emp WHERE NOT (dept IN "
                      "('Eng', 'HR'))")
        assert sorted(r[0] for r in rows) == [4, 5], rows
        _, rows = e.q("SELECT id FROM users u WHERE NOT (u.id IN "
                      "(SELECT o.user_id FROM orders o WHERE "
                      "o.user_id = u.id))")
        assert [r[0] for r in rows] == [3], rows

        # ⑧ 错误语法仍显式报错（`NOT 列 = 值` 不是合法 SQL 谓词）
        import pytest
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM emp WHERE NOT dept = 'Eng'")


def test_is_null_and_is_not_null():
    """WHERE / HAVING 里的 IS NULL 与 IS NOT NULL

    实现方式：解析器把 `col IS NULL` 编码成 `{'column': col, 'op': '=',
    'value': None}`、`IS NOT NULL` 编码成 op='!='，与存储层/统一路径既有
    的 NULL 语义完全一致，因此不需要新增求值分支。含字面量 NULL 的条件
    强制走统一求值路径（存储层的 `_find_pks` 会先用 coerce 走主键/索引
    快路径，把 NULL 当成默认值，导致 IS NULL 恒空、IS NOT NULL 恒全量）。
    """
    with _Env() as e:
        # orders 里插一行 amount 为 NULL 的记录
        e.ex.execute_sql("INSERT INTO orders VALUES (4, 3, NULL)", {})
        _, rows = e.q("SELECT id FROM orders")
        assert len(rows) == 4, rows

        # ① IS NULL / IS NOT NULL 在 WHERE 里
        _, rows = e.q("SELECT id FROM orders WHERE amount IS NULL")
        assert [r[0] for r in rows] == [4], rows
        _, rows = e.q("SELECT id FROM orders WHERE amount IS NOT NULL")
        assert [r[0] for r in rows] == [1, 2, 3], rows

        # ② 与普通条件组合
        _, rows = e.q("SELECT id FROM orders WHERE amount IS NOT NULL "
                      "AND amount > 15")
        assert sorted(r[0] for r in rows) == [2, 3], rows
        _, rows = e.q("SELECT id FROM orders WHERE amount IS NULL "
                      "OR amount > 25")
        assert sorted(r[0] for r in rows) == [3, 4], rows

        # ③ IS NULL 的否定形态：NOT (col IS NULL) 等价于 IS NOT NULL
        _, rows = e.q("SELECT id FROM orders WHERE NOT (amount IS NULL)")
        assert [r[0] for r in rows] == [1, 2, 3], rows

        # ④ HAVING 里作用于聚合值
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING COUNT(*) IS NOT NULL")
        assert sorted(rows) == [('Eng', 3), ('HR', 3), ('Sales', 2)], rows
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING COUNT(*) IS NULL")
        assert rows == [], rows

        # ⑤ HAVING 里作用于分组键
        _, rows = e.q("SELECT dept FROM emp GROUP BY dept "
                      "HAVING dept IS NOT NULL")
        assert sorted(r[0] for r in rows) == ['Eng', 'HR', 'Sales'], rows

        # ⑥ 子查询里也能用 IS NULL
        _, rows = e.q("SELECT id FROM users WHERE id IN (SELECT user_id "
                      "FROM orders WHERE amount IS NULL)")
        assert [r[0] for r in rows] == [3], rows

        # ⑦ 列名不存在仍然是明确报错（不能把 IS NULL 静默当假）
        r = e.ex.execute(e.parser.parse(
            "SELECT id FROM orders WHERE nosuchcol IS NULL"), {})
        assert r.get('type') == 'error' or r.get('rows') == [], r


def test_projection_arithmetic():
    """投影列表里的算术表达式：`列 运算符 数字`，可带 AS 别名

    实现方式：
    * 执行器识别投影项里的二元算术（`_arith_projection_expr`），命中就
      改走 `_select_advanced` → `_project_rows`，复用既有的
      `_parse_arith` / `_eval_arith`（原先只有递归 CTE 的 `n + 1` 用得上，
      且 `SELECT salary*2 AS d` 会被整条下推到存储层，报「列不存在」）；
    * 除零返回 NULL（不抛异常、不崩）；
    * 别名写进输出列名，因此 `FROM (子查询) x WHERE x.d > n` 能按别名过滤。
    """
    with _Env() as e:
        # ① `salary*2 AS d`
        cols, rows = e.q("SELECT id, salary*2 AS d FROM emp")
        assert cols == ['id', 'd'], cols
        assert [r[1] for r in rows] == [2 * v for v in EMP_SAL], rows

        # ② `salary/2 AS half`（真除法，保留小数）
        cols, rows = e.q("SELECT salary/2 AS half FROM emp")
        assert cols == ['half'], cols
        assert [r[0] for r in rows] == [v / 2 for v in EMP_SAL], rows

        # ③ 字面量在左：`2*salary`
        cols, rows = e.q("SELECT 2*salary AS d2 FROM emp")
        assert cols == ['d2'], cols
        assert [r[0] for r in rows] == [2 * v for v in EMP_SAL], rows

        # ④ 加减法
        _, rows = e.q("SELECT salary+10 AS a, salary-10 AS b FROM emp "
                      "ORDER BY id LIMIT 1")
        assert rows == [(130, 110)], rows

        # ⑤ 别名能被外层引用：Alice 240 / Dave 220 超过 200
        cols, rows = e.q("SELECT * FROM (SELECT id, salary*2 AS d FROM emp) x "
                         "WHERE x.d > 200")
        assert cols == ['id', 'd'], cols
        assert sorted(rows) == [(1, 240), (4, 220)], rows

        # ⑥ 子查询内层 SELECT 里的算术（外层据此过滤）
        #    （补注：`WHERE salary*2 > 230` 这类**算术谓词**现在也已支持，
        #     见文件末尾追加的标量表达式用例；此处保留原有断言不动）
        _, rows = e.q("SELECT id FROM (SELECT id, salary*2 AS d FROM emp) x "
                      "WHERE x.d > 230")
        assert [r[0] for r in rows] == [1], rows
        _, rows = e.q("SELECT COUNT(*) FROM (SELECT id, salary*2 AS d "
                      "FROM emp) x WHERE x.d > 200")
        assert rows == [(2,)], rows

        # ⑦ 除零：显式返回 NULL 而不是崩溃
        cols, rows = e.q("SELECT salary/0 AS bad FROM emp")
        assert cols == ['bad'], cols
        assert [r[0] for r in rows] == [None] * 8, rows

        # ⑧ 不带别名时输出列名保持表达式文本
        cols, rows = e.q("SELECT salary*2 FROM emp LIMIT 1")
        assert cols == ['salary*2'], cols
        assert rows == [(240,)], rows


# ==========================================================================
# 以下为追加：投影列表里的标量子查询（逐外层行求值）/ 外层引用被同名表遮蔽
#
# 数据同 _Env：users 1..3（a/b 属 BJ、c 属 SH），orders.user_id = 1,1,2，
# amount = 10,20,30；emp 8 行（salary 见 EMP_SAL）。
# 实现方式见 executor.Executor._eval_projection_scalar / _project_rows。
# ==========================================================================


def _assert_err(e, sql):
    """执行 SQL 并断言返回显式 error，返回错误信息文本"""
    r = e.ex.execute_sql(sql, {})
    assert isinstance(r, dict) and r.get('type') == 'error', \
        f'应当显式报错，实际返回: {r}'
    return str(r.get('message') or '')


def test_projection_scalar_subquery_count():
    """投影里的相关标量子查询：COUNT(*) 逐外层行求值

    原实现把认不出来的投影项静默投影成 NULL（实测三行 cnt 全是 None）。
    现在按外层行折叠外层列后重新执行子查询，uid 3 没有订单 → COUNT 是 0
    （空集上的 COUNT 为 0，不是 NULL）。
    """
    with _Env() as e:
        cols, rows = e.q("SELECT id, (SELECT COUNT(*) FROM orders o "
                         "WHERE o.user_id = users.id) AS cnt FROM users "
                         "ORDER BY id")
        assert cols == ['id', 'cnt'], cols
        assert rows == [(1, 2), (2, 1), (3, 0)], rows

        # 外层带别名（u.id 形式）结果一致
        _, rows = e.q("SELECT u.id, (SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id) AS c FROM users u "
                      "ORDER BY u.id")
        assert rows == [(1, 2), (2, 1), (3, 0)], rows


def test_projection_scalar_subquery_with_join_group_having():
    """投影里的相关标量子查询：子查询内含 JOIN / GROUP BY / HAVING"""
    with _Env() as e:
        # 子查询里 JOIN：orders JOIN users，逐外层行折叠 u.id
        _, rows = e.q("SELECT u.id, (SELECT COUNT(*) FROM orders o "
                      "JOIN users u2 ON o.user_id = u2.id "
                      "WHERE o.user_id = u.id) AS c FROM users u "
                      "ORDER BY u.id")
        assert rows == [(1, 2), (2, 1), (3, 0)], rows

        # 子查询里 GROUP BY / HAVING：uid 3 过滤后没有分组 → 空集 → COUNT 0
        _, rows = e.q("SELECT u.id, (SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id GROUP BY o.user_id "
                      "HAVING COUNT(*) >= 1) AS g FROM users u "
                      "ORDER BY u.id")
        assert rows == [(1, 2), (2, 1), (3, 0)], rows


def test_projection_scalar_subquery_sum_null_row():
    """相关 SUM/MIN/MAX/AVG：某外层行没有匹配 → 该行取 NULL（正确的 NULL）"""
    with _Env() as e:
        _, rows = e.q("SELECT u.id, (SELECT SUM(o.amount) FROM orders o "
                      "WHERE o.user_id = u.id) AS s FROM users u "
                      "ORDER BY u.id")
        assert rows == [(1, 30), (2, 30), (3, None)], rows

        _, rows = e.q(
            "SELECT u.id, "
            "(SELECT MIN(o.amount) FROM orders o WHERE o.user_id = u.id) AS mn, "
            "(SELECT MAX(o.amount) FROM orders o WHERE o.user_id = u.id) AS mx, "
            "(SELECT AVG(o.amount) FROM orders o WHERE o.user_id = u.id) AS av "
            "FROM users u ORDER BY u.id")
        assert rows == [(1, 10, 20, 15.0), (2, 30, 30, 30.0),
                        (3, None, None, None)], rows


def test_projection_noncorrelated_scalar_subquery():
    """非相关标量子查询：整体只求值一次，每行同值；别名可被外层引用"""
    with _Env() as e:
        cols, rows = e.q("SELECT id, (SELECT COUNT(*) FROM orders) AS total "
                         "FROM users ORDER BY id")
        assert cols == ['id', 'total'], cols
        assert rows == [(1, 3), (2, 3), (3, 3)], rows

        # 别名（AS cnt）可被外层 FROM 子查询引用
        cols, rows = e.q(
            "SELECT * FROM (SELECT id, (SELECT COUNT(*) FROM orders o "
            "WHERE o.user_id = users.id) AS cnt FROM users) x "
            "WHERE x.cnt > 0 ORDER BY x.id")
        assert cols == ['id', 'cnt'], cols
        assert rows == [(1, 2), (2, 1)], rows


def test_projection_scalar_subquery_with_group_order_limit():
    """投影标量子查询与 GROUP BY/HAVING/算术/ORDER BY/LIMIT+OFFSET 共存"""
    with _Env() as e:
        # GROUP BY + HAVING + 非相关标量子查询
        _, rows = e.q("SELECT city, COUNT(*) AS c, "
                      "(SELECT COUNT(*) FROM orders) AS t FROM users "
                      "GROUP BY city HAVING COUNT(*) > 0 ORDER BY city")
        assert rows == [('BJ', 2, 3), ('SH', 1, 3)], rows

        # 相关标量子查询挂在 GROUP BY 的键上（u.city 是分组键）
        _, rows = e.q("SELECT u.city, COUNT(*) AS c, (SELECT COUNT(*) FROM "
                      "orders o JOIN users u2 ON o.user_id = u2.id "
                      "WHERE u2.city = u.city) AS oc FROM users u "
                      "GROUP BY u.city ORDER BY u.city")
        assert rows == [('BJ', 2, 3), ('SH', 1, 0)], rows

        # 与投影算术表达式共存
        _, rows = e.q("SELECT id, salary*2 AS d, (SELECT COUNT(*) FROM "
                      "orders) AS t FROM emp ORDER BY id LIMIT 1")
        assert rows == [(1, 240, 3)], rows

        # ORDER BY + LIMIT/OFFSET（标量子查询要在裁剪之前逐行求值）
        _, rows = e.q("SELECT id, (SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = users.id) AS cnt FROM users "
                      "ORDER BY id LIMIT 2 OFFSET 1")
        assert rows == [(2, 1), (3, 0)], rows


def test_projection_scalar_subquery_multi_row_errors():
    """标量子查询返回多行 → 显式报错（绝不随便取一行）"""
    with _Env() as e:
        msg = _assert_err(
            e, "SELECT id, (SELECT o.user_id FROM orders o "
               "WHERE o.user_id = users.id) AS x FROM users")
        assert '多行' in msg, msg


def test_projection_scalar_subquery_multi_column_errors():
    """标量子查询返回多列 → 显式报错"""
    with _Env() as e:
        msg = _assert_err(
            e, "SELECT id, (SELECT o.user_id, o.amount FROM orders o "
               "WHERE o.user_id = users.id) AS x FROM users")
        assert '多列' in msg, msg


def test_projection_unresolvable_item_errors():
    """无法求值的投影项 → 显式报错，不再静默投影成 NULL

    * `u.uid`（限定名指向不存在的列）原实现输出 (None, None)；
    * `LOWER(name)` 原实现被存储层当成聚合，静默返回 1 行 (1, None)。
      （本会话给引擎补上了标量函数能力，`LOWER` 现在是**支持**的函数，
      因此这条改成用仍不支持的函数 `REVERSE` 验证同一个断言意图：
      不支持的函数必须显式报错、并回列支持清单，而不是静默 NULL。）
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT u.uid FROM users u")
        assert 'uid' in msg, msg
        msg = _assert_err(e, "SELECT id, REVERSE(name) FROM users")
        assert 'REVERSE' in msg, msg


def test_ambiguous_outer_ref_same_name_table():
    """外层未起别名 + 子查询里同名表被别名遮蔽 → 显式报错

    原实现：`users.id` 被判成子查询内层引用（子查询里的 users u2），
    整个 EXISTS 被当成非相关子查询一次性求值 → 静默返回空集 []。
    给外层起别名后名字不冲突，结果必须保持正确。
    """
    with _Env() as e:
        msg = _assert_err(
            e, "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM orders o "
               "JOIN users u2 ON o.user_id = u2.id WHERE o.user_id = users.id)")
        assert '无法无歧义' in msg, msg
        assert 'users' in msg, msg

        # 投影位置的同形态也要显式报错（不能静默填 NULL）
        msg = _assert_err(
            e, "SELECT id, (SELECT COUNT(*) FROM orders o JOIN users u2 "
               "ON o.user_id = u2.id WHERE o.user_id = users.id) AS c "
               "FROM users")
        assert '无法无歧义' in msg, msg

        # 带别名（既有正确用法）不能被破坏
        _, rows = e.q("SELECT u1.id FROM users u1 WHERE EXISTS (SELECT 1 "
                      "FROM orders o JOIN users u2 ON o.user_id = u2.id "
                      "WHERE o.user_id = u1.id) ORDER BY u1.id")
        assert rows == [(1,), (2,)], rows


def test_correlated_string_column_folding():
    """相关子查询里「内层列 = 外层字符串列」：折叠后不能给值带 SQL 引号

    原实现把外层字符串渲染成 SQL 文本（含引号）塞进 AST 的 value，
    折叠后条件变成 `u2.city = "'BJ'"` → 永远匹配不上，静默返回空集/0 行。
    """
    with _Env() as e:
        _, rows = e.q("SELECT u.id FROM users u WHERE EXISTS (SELECT 1 "
                      "FROM orders o JOIN users u2 ON o.user_id = u2.id "
                      "WHERE u2.city = u.city) ORDER BY u.id")
        assert rows == [(1,), (2,)], rows


def test_malformed_where_missing_and_is_explicit_error():
    """WHERE 里用逗号代替 AND → 语法错误（原来静默返回空集）"""
    import pytest

    with _Env() as e:
        with pytest.raises(SyntaxError):
            e.parser.parse("SELECT id FROM users WHERE city = 'BJ', id = 1")
        # 合法字符串字面量里含 AND / 逗号不能被误判
        _, rows = e.q("SELECT id FROM users WHERE name = 'A AND B'")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM users WHERE name = 'a,b'")
        assert rows == [], rows


# ==========================================================================
# 以下为追加：**标量上下文的行数语义**（WHERE / HAVING 位置）
#
# 现状（修复前）：`WHERE (SELECT ...) = 5` 这类标量比较在子查询返回多于
# 1 行时静默取第一行、返回多列时静默取第一列参与比较——静默错答。
# 修复后统一为：0 行 → NULL（与任何值比较都是 UNKNOWN → 该行不通过，
# 不是报错）；1 行 1 列 → 取值；多行或多列 → 显式报错。
#
# 数据（与验收脚本完全一致，列名用 uid/amt）：
#   users(id, city)      → (1,'BJ') (2,'BJ') (3,'SH')
#   orders(id, uid, amt) → (1,1,10) (2,1,20) (3,2,30)
# ==========================================================================

SCALAR_DATA_DIR = os.path.join(ROOT, 'data_scalar_semantics_test')


class _ScalarEnv:
    """验收用的最小数据集：users(id, city) / orders(id, uid, amt)"""

    def __enter__(self):
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.sql.parser import parser

        shutil.rmtree(SCALAR_DATA_DIR, ignore_errors=True)
        self.db = Database(SCALAR_DATA_DIR)
        self.ex = Executor(self.db)
        self.parser = parser
        self.ex.execute_sql(
            "CREATE TABLE users (id INT PRIMARY KEY, city VARCHAR(10))", {})
        self.ex.execute_sql(
            "CREATE TABLE orders (id INT PRIMARY KEY, uid INT, amt INT)", {})
        for i, city in [(1, 'BJ'), (2, 'BJ'), (3, 'SH')]:
            self.ex.execute_sql(
                "INSERT INTO users VALUES (%d,'%s')" % (i, city), {})
        for oid, uid, amt in [(1, 1, 10), (2, 1, 20), (3, 2, 30)]:
            self.ex.execute_sql(
                "INSERT INTO orders VALUES (%d,%d,%d)" % (oid, uid, amt), {})
        return self

    def __exit__(self, *exc):
        try:
            self.db.shutdown()
        except Exception:
            pass
        shutil.rmtree(SCALAR_DATA_DIR, ignore_errors=True)
        return False

    def q(self, sql):
        r = self.ex.execute(self.parser.parse(sql), {})
        assert r.get('type') == 'resultset', \
            f'查询失败: {sql} -> {r.get("message")}'
        return r['columns'], [tuple(x) for x in r['rows']]

    def err(self, sql):
        r = self.ex.execute(self.parser.parse(sql), {})
        assert isinstance(r, dict) and r.get('type') == 'error', \
            f'应当显式报错，实际返回: {sql} -> {r}'
        return str(r.get('message') or '')


def test_scalar_predicate_multi_row_errors():
    """验收①：WHERE 标量子查询返回多行 → 显式报错（原来静默取第一行）

    u.id=1 命中的是 orders 里 uid=1 的 2 行（10/20），原来取第一行 10
    参与 `= 10` 的比较后静默返回 [(1,)]——错误结果。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT id FROM users u WHERE "
                    "(SELECT o.amt FROM orders o WHERE o.uid=u.id) = 10")
        assert '多行' in msg, msg
        assert '2' in msg, msg


def test_scalar_predicate_single_row_ok():
    """验收②：子查询只可能返回一行时，标量比较结果正确"""
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT MAX(o.amt) FROM orders o "
                      "WHERE o.uid=u.id) = 30")
        assert rows == [(2,)], rows


def test_scalar_predicate_empty_is_null_not_error():
    """验收③：0 行 → NULL → 与任何值比较都是 UNKNOWN → 不通过，且不报错"""
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT o.amt FROM orders o "
                      "WHERE o.uid=u.id AND o.amt > 1000) = 5")
        assert rows == [], rows
        # 非聚合空集 → NULL；聚合空集按 SQL 语义（COUNT→0，其余→NULL）
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT SUM(o.amt) FROM orders o "
                      "WHERE o.uid=u.id AND o.amt > 1000) = 0")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM users u WHERE "
                      "(SELECT COUNT(*) FROM orders o "
                      "WHERE o.uid=u.id AND o.amt > 1000) = 0")
        assert sorted(r[0] for r in rows) == [1, 2, 3], rows
        # NULL 的比较结果是三值逻辑的 UNKNOWN：NOT 下仍是 UNKNOWN，
        # 不能让该行通过（uid=3 的 MAX 是 NULL）
        _, rows = e.q("SELECT id FROM users u WHERE NOT "
                      "((SELECT MAX(o.amt) FROM orders o "
                      "WHERE o.uid=u.id) = 30)")
        assert [r[0] for r in rows] == [1], rows


def test_scalar_predicate_multi_column_errors():
    """验收④：WHERE 标量子查询返回多列 → 显式报错（原来静默取第一列）"""
    with _ScalarEnv() as e:
        msg = e.err("SELECT id FROM users u WHERE "
                    "(SELECT o.uid, o.amt FROM orders o "
                    "WHERE o.uid=u.id) = 1")
        assert '多列' in msg, msg


def test_scalar_predicate_all_comparison_operators():
    """验收⑤：= != <> < > <= >= 全部比较符都要按同一套行数语义求值

    用「每行最多返回 1 行」的聚合形态（MIN/MAX）保证语义可判定，
    并额外覆盖 `<>` 与 `!=` 的等价性。
    """
    with _ScalarEnv() as e:
        cases = [
            ("SELECT id FROM users u WHERE (SELECT MIN(o.amt) FROM orders o "
             "WHERE o.uid=u.id) = 10", [1]),
            ("SELECT id FROM users u WHERE (SELECT MAX(o.amt) FROM orders o "
             "WHERE o.uid=u.id) != 10", [1, 2]),
            ("SELECT id FROM users u WHERE (SELECT MAX(o.amt) FROM orders o "
             "WHERE o.uid=u.id) <> 10", [1, 2]),
            ("SELECT id FROM users u WHERE (SELECT MIN(o.amt) FROM orders o "
             "WHERE o.uid=u.id) < 25", [1]),
            ("SELECT id FROM users u WHERE (SELECT MIN(o.amt) FROM orders o "
             "WHERE o.uid=u.id) > 25", [2]),
            ("SELECT id FROM users u WHERE (SELECT MIN(o.amt) FROM orders o "
             "WHERE o.uid=u.id) <= 10", [1]),
            ("SELECT id FROM users u WHERE (SELECT MAX(o.amt) FROM orders o "
             "WHERE o.uid=u.id) >= 30", [2]),
        ]
        for sql, want in cases:
            _, rows = e.q(sql)
            assert sorted(r[0] for r in rows) == want, (sql, rows)
        # uid=3 没有任何订单：MIN 空集 → NULL，所有比较都不通过（UNKNOWN）
        for op in ('=', '!=', '<', '>', '<=', '>='):
            _, rows = e.q(
                "SELECT id FROM users u WHERE 3 = u.id AND "
                "(SELECT MAX(o.amt) FROM orders o WHERE o.uid=u.id) "
                f"{op} 30")
            assert rows == [], (op, rows)


def test_having_scalar_subquery():
    """验收⑥：HAVING 位置的标量子查询

    * 引用分组键（`u.city`）→ 逐分组折叠求值，结果正确；
    * 引用非分组键（`u.id`，GROUP BY 之后取不到）→ 显式报错
      （原实现 `_eval_condition` 见到没有左值的 subquery 节点直接
      `return True`，把整个 HAVING 静默丢掉 → 所有分组都被保留）；
    * 非相关标量子查询：多行 → 显式报错，1 行 → 正确判定；
    * 与聚合谓词 AND 混用也要正确。
    """
    with _ScalarEnv() as e:
        # ① 非相关标量子查询返回多行 → 显式报错
        msg = e.err(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "HAVING (SELECT o.amt FROM orders o) > 0")
        assert '多行' in msg, msg

        # ② 非相关标量子查询返回 1 行 → 每个分组都用该标量判定
        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "HAVING (SELECT MAX(o.amt) FROM orders o) = 30")
        assert sorted(rows) == [('BJ', 2), ('SH', 1)], rows

        # ③ 相关（引用分组键 u.city）：BJ 有 3 笔订单、SH 0 笔 → 只留 BJ
        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "HAVING (SELECT COUNT(*) FROM orders o JOIN users u2 "
            "ON o.uid = u2.id WHERE u2.city = u.city) > 0")
        assert rows == [('BJ', 2)], rows

        # ④ 与聚合谓词 AND 混用
        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "HAVING COUNT(*) > 0 AND (SELECT COUNT(*) FROM orders o "
            "JOIN users u2 ON o.uid = u2.id WHERE u2.city = u.city) > 0")
        assert rows == [('BJ', 2)], rows

        # ⑤ 引用非分组键（u.id）：GROUP BY city 之后取不到 → 显式报错，
        #    不能静默把所有分组都保留
        msg = e.err(
            "SELECT u.city, COUNT(*) FROM users u GROUP BY u.city "
            "HAVING (SELECT COUNT(*) FROM orders o WHERE o.uid = u.id) > 0")
        assert 'GROUP BY' in msg, msg


def test_scalar_subquery_fold_cache_and_20_rows():
    """性能/缓存：外层 20+ 行逐行求值瞬时完成，且按折叠值缓存

    子查询只引用 `u.city`，可用折叠值只有 2 种（BJ/SH），因此
    `_fold_and_run` 只应被调用 ≤2 次（同值外层行共享缓存结果）；
    如果缓存失效会退化成 22 次。
    """
    import time

    with _ScalarEnv() as e:
        for i in range(10, 30):
            e.ex.execute_sql(
                "INSERT INTO users VALUES (%d,'%s')"
                % (i, 'BJ' if i % 2 == 0 else 'SH'), {})
        calls = []
        orig = e.ex._fold_and_run

        def counting(*a, **kw):
            calls.append(1)
            return orig(*a, **kw)

        e.ex._fold_and_run = counting
        try:
            start = time.time()
            _, rows = e.q(
                "SELECT u.id FROM users u WHERE "
                "(SELECT COUNT(*) FROM orders o JOIN users u2 "
                "ON o.uid = u2.id WHERE u2.city = u.city) > 0 "
                "ORDER BY u.id")
            elapsed = time.time() - start
        finally:
            e.ex._fold_and_run = orig
        assert [r[0] for r in rows] == [1, 2] + \
            [i for i in range(10, 30) if i % 2 == 0], rows
        assert elapsed < 2.0, f'20+ 行逐行求值过慢: {elapsed:.3f}s'
        assert len(calls) <= 2, f'折叠缓存失效，子查询被执行 {len(calls)} 次'


# ==========================================================================
# 以下为追加：**JOIN 查询里的标量子查询**（最后一处「标量子查询被拒绝」）
#
# 修复前（均显式报错，属功能缺口）：
#   * WHERE/HAVING 位置 → err「JOIN 查询的 WHERE/HAVING 里暂不支持标量子查询比较」
#   * 投影位置        → err「JOIN 查询的投影列表里暂不支持标量子查询」
#
# 实现方式（后置处理，计划执行器只算连接本身）：
#   1. 只要 JOIN 查询里有「必须逐连接行求值」的子查询（WHERE 标量比较 /
#      引用连接表的相关子查询 / HAVING 子查询 / 投影标量子查询），就不再把
#      整条查询交给计划执行器：先构造一个只含 `FROM + JOIN(ON)` 的计划
#      （投影成 `*`，列名保留 `u.id` / `o.uid` 限定前缀），拿到连接结果；
#   2. 结果物化成内存关系（`_CTERelation`），交给 `_select_correlated` 的
#      逐行路径 —— 与单表相关子查询**完全同一套机制**：
#      `_eval_outer_where` 逐连接行折叠「外层限定名.列」→ `_fold_and_run`
#      重新执行折叠后的子查询 → `_scalar_from_rows` 统一收敛
#      （0 行→NULL/COUNT→0、多行→显式报错、多列→显式报错）；
#   3. 折叠结果按「折叠用到的外层列取值」缓存（`_fold_cache_key`），
#      非相关子查询按子查询缓存、整体只算一次；
#   4. HAVING 的子查询在 `_group_rows` 之后逐分组折叠（分组键作为外层行），
#      引用不到分组键的列（GROUP BY 之后取不到）→ 显式报错；
#   5. 反方向：`ON o.uid = u.id`（JOIN 表写在左边）原来会因为连接键解析不到
#      而静默退化成笛卡尔积（3×3=9 行）→ 现已修正（plan_executor
#      `_resolve_join_keys`），否则上面所有验收点都建立在错误的行集上。
#
# 数据同 _ScalarEnv：users(id, city) = (1,'BJ') (2,'BJ') (3,'SH')；
#                  orders(id, uid, amt) = (1,1,10) (2,1,20) (3,2,30)。
# ==========================================================================

JOIN_SQ = "FROM users u JOIN orders o ON o.uid=u.id"


def test_join_where_scalar_subquery_filters_rows():
    """验收①：JOIN 查询的 WHERE 标量子查询逐连接行过滤（原来直接报错）

    uid=1 有 2 笔订单（COUNT>1 成立）→ 该用户的 **2 个连接行**都保留；
    uid=2 只有 1 笔（不成立）→ 连接行被剔除。
    **不做去重**：(1,) 出现两次才是 SQL 的正确语义（JOIN 结果本来就是
    3 行，子查询只做行过滤、不改变行数）；要唯一值必须显式写 DISTINCT，
    这里用第二条断言把这一点也锁住。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT u.id " + JOIN_SQ +
                      " WHERE (SELECT COUNT(*) FROM orders x "
                      "WHERE x.uid=u.id) > 1")
        assert rows == [(1,), (1,)], rows
        _, rows = e.q("SELECT DISTINCT u.id " + JOIN_SQ +
                      " WHERE (SELECT COUNT(*) FROM orders x "
                      "WHERE x.uid=u.id) > 1")
        assert rows == [(1,)], rows


def test_join_projection_scalar_subquery_per_row():
    """验收②：JOIN 查询投影里的标量子查询——每个连接行带自己的计数"""
    with _ScalarEnv() as e:
        cols, rows = e.q(
            "SELECT u.id, (SELECT COUNT(*) FROM orders x WHERE x.uid=u.id) "
            "AS cnt " + JOIN_SQ)
        assert (cols, rows) == (['id', 'cnt'], [(1, 2), (1, 2), (2, 1)])


def test_join_having_scalar_subquery_per_group():
    """验收③：JOIN + GROUP BY 的 HAVING 标量子查询按分组求值

    LEFT JOIN 让 BJ / SH 两个分组都存在；HAVING 里的子查询折叠的是
    **分组键** u.city：BJ 组 → 该城市用户共 3 笔订单（>0 成立，保留）；
    SH 组 → 该城市用户没有订单（0 > 0 不成立，剔除）。
    如果 HAVING 被整体丢掉（原实现）或只按第一个分组求值，都会得到错结果。
    """
    with _ScalarEnv() as e:
        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u LEFT JOIN orders o "
            "ON o.uid=u.id GROUP BY u.city "
            "HAVING (SELECT COUNT(*) FROM orders x JOIN users u2 "
            "ON x.uid=u2.id WHERE u2.city = u.city) > 0")
        assert rows == [('BJ', 3)], rows


def test_join_having_noncorrelated_scalar_subquery():
    """验收③b：HAVING 里的**非相关**标量子查询整体只算一次，每个分组同值"""
    with _ScalarEnv() as e:
        _, rows = e.q(
            "SELECT u.city, COUNT(*) FROM users u LEFT JOIN orders o "
            "ON o.uid=u.id GROUP BY u.city "
            "HAVING (SELECT COUNT(*) FROM orders) = 3")
        assert rows == [('BJ', 3), ('SH', 1)], rows


def test_join_noncorrelated_scalar_subquery_evaluated_once():
    """验收④：非相关标量子查询在 JOIN 查询里只算一次 → 全部连接行保留"""
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT u.id " + JOIN_SQ +
                      " WHERE (SELECT COUNT(*) FROM orders) = 3")
        assert rows == [(1,), (1,), (2,)], rows


def test_join_where_scalar_subquery_multi_row_errors():
    """验收⑤：JOIN 查询的 WHERE 标量子查询返回多行 → 显式报错（绝不取第一行）"""
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.id " + JOIN_SQ +
                    " WHERE (SELECT x.amt FROM orders x "
                    "WHERE x.uid=u.id) = 10")
        assert '多行' in msg and '2 行' in msg, msg


def test_join_scalar_subquery_multi_column_errors():
    """验收⑥：JOIN 查询的标量子查询返回多列 → 显式报错（绝不取第一列）

    WHERE 位置与投影位置都要报（原来一个是「暂不支持」、一个也是「暂不支持」，
    现在统一走 `_scalar_from_rows` 的多列语义）。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.id " + JOIN_SQ +
                    " WHERE (SELECT x.uid, x.amt FROM orders x "
                    "WHERE x.uid=u.id) = 1")
        assert '多列' in msg and '2 列' in msg, msg


def test_join_projection_scalar_subquery_multi_row_and_column_errors():
    """验收⑥b：投影位置的同一套行数/列数语义（多行、多列都显式报错）"""
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.id, (SELECT x.amt FROM orders x "
                    "WHERE x.uid=u.id) AS a " + JOIN_SQ)
        assert '多行' in msg, msg
        msg = e.err("SELECT u.id, (SELECT x.uid, x.amt FROM orders x "
                    "WHERE x.uid=u.id) AS a " + JOIN_SQ)
        assert '多列' in msg, msg


def test_join_query_without_subquery_regression():
    """验收⑦：不含子查询的 JOIN 查询回归（反向 ON / GROUP BY / ORDER BY /
    DISTINCT / LIMIT+OFFSET / LEFT JOIN 全部保持正确）

    反向 ON（`ON o.uid=u.id`）原来会把连接键解析丢 → 静默笛卡尔积
    （3×3=9 行），GROUP BY / ORDER BY 全部跟着算错。
    """
    with _ScalarEnv() as e:
        # 连接本身：3 行（不是 9 行）
        _, rows = e.q("SELECT u.id " + JOIN_SQ)
        assert rows == [(1,), (1,), (2,)], rows
        # ORDER BY（限定列名）+ LIMIT + OFFSET
        _, rows = e.q("SELECT u.id, o.amt " + JOIN_SQ +
                      " ORDER BY o.amt DESC LIMIT 2 OFFSET 1")
        assert rows == [(1, 20), (1, 10)], rows
        # GROUP BY + ORDER BY + LIMIT
        _, rows = e.q("SELECT u.city, COUNT(*) " + JOIN_SQ +
                      " GROUP BY u.city ORDER BY u.city DESC LIMIT 1")
        assert rows == [('BJ', 3)], rows
        # LEFT JOIN
        _, rows = e.q("SELECT u.id, o.amt FROM users u LEFT JOIN orders o "
                      "ON o.uid=u.id ORDER BY u.id")
        assert rows == [(1, 10), (1, 20), (2, 30), (3, None)], rows


def test_join_correlated_exists_row_eval():
    """附加：JOIN 查询里引用连接表的 EXISTS 子查询也逐连接行求值

    原来计划执行器把子查询一次性执行（忽略外层 u.id），静默返回空集。
    与标量子查询共用同一套折叠机制。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT u.id " + JOIN_SQ +
                      " WHERE EXISTS (SELECT 1 FROM orders x "
                      "WHERE x.uid = u.id AND x.amt > 15) ORDER BY u.id")
        assert rows == [(1,), (1,), (2,)], rows


def test_join_having_scalar_subquery_non_group_key_errors():
    """附加：HAVING 里的相关子查询引用了 GROUP BY 之后取不到的列 → 显式报错

    不能静默保留所有分组（原实现 `_eval_condition` 见到没有左值的 subquery
    节点直接 return True）。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.city, COUNT(*) " + JOIN_SQ +
                    " GROUP BY u.city HAVING (SELECT COUNT(*) FROM orders x "
                    "WHERE x.uid=u.id) > 0")
        assert 'GROUP BY' in msg, msg


def test_join_scalar_subquery_residual_outer_ref_errors():
    """附加：外层引用藏在聚合函数里（`MIN(u.id)`）→ 显式报错，绝不静默算错

    `_is_correlated` 只识别「别名.列」的纯列引用，`MIN(u.id)` 这种形态识别
    不到，会被当成非相关子查询一次性执行（内层解析不到该列 → 0 行 →
    COUNT=0 → HAVING 判定为假），属于静默错答；这里显式报错。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.city, COUNT(*) " + JOIN_SQ +
                    " GROUP BY u.city HAVING (SELECT COUNT(*) FROM orders x "
                    "WHERE x.uid=MIN(u.id)) > 0")
        assert '无法安全折叠' in msg, msg


def test_join_select_star_with_scalar_subquery():
    """附加：`SELECT *` + WHERE 标量子查询（后置路径同样只给 3 个连接行）"""
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT * " + JOIN_SQ +
                      " WHERE (SELECT COUNT(*) FROM orders) = 3")
        assert len(rows) == 3, rows


def test_join_ambiguous_bare_column_errors():
    """附加：连接结果里的裸列名有歧义（`u.id` / `o.id` 并存）→ 显式报错

    绝不能任取一侧，也不能静默把条件判成假（结果集会变成空集，用户分不清
    「没有匹配行」与「列名有歧义」）。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.city " + JOIN_SQ +
                    " WHERE id = 1 AND (SELECT COUNT(*) FROM orders) = 3")
        assert '歧义' in msg, msg


def test_join_having_without_group_by_parses():
    """附加：JOIN 查询的 HAVING（没有 GROUP BY）必须能解析

    原来解析器切分 ON 子句时不认识 HAVING，`... JOIN o ON a.x=b.y HAVING ...`
    会把 `HAVING ...` 整段当成 ON 条件 → SyntaxError（同一条查询不带 JOIN
    时可以解析）。HAVING 里的标量子查询与 GROUP BY 形态走同一套逐分组求值。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT COUNT(*) FROM users u JOIN orders o "
                      "ON u.id=o.uid "
                      "HAVING (SELECT COUNT(*) FROM orders) = 3")
        assert rows == [(3,)], rows


# ==========================================================================
# 追加（本会话）：`IN` / `NOT IN` 子查询的多列「静默截断」
#
# 原实现：`id IN (SELECT uid, amt FROM orders)` 对子查询结果的每一行取
# `r[0]`，**静默只用第一列**参与比较，于是返回一份「看起来对」的行集
# （等价于 `id IN (SELECT uid FROM orders)`）。SQL 标准里多列 IN 是
# **行值（tuple）比较**：左右两侧都必须看成行值，本引擎不支持行值比较，
# 因此现在一律**显式报错**——要么算对，要么报错，绝不静默给出可能错误的
# 结果（与上一轮标量上下文 `_scalar_from_rows` 的纪律对齐）。
#
# 覆盖：单表 `_rewrite_subqueries` 路径、逐行非相关路径
# （`_eval_plain_subquery_predicate`）、相关路径
# （`_eval_correlated_predicate`）、HAVING 路径、JOIN 纯计划路径
# （`plan_executor._eval_condition` + `_exec_subquery_values`）。
# 列数判定是「静态 + 动态」双保险：静态看解析期的投影项数
# （空结果集也拦得住），动态看结果列名/结果列数（兜住 `SELECT *`）。
#
# 数据同 _ScalarEnv：users(id, city) = (1,'BJ') (2,'BJ') (3,'SH')；
#                  orders(id, uid, amt) = (1,1,10) (2,1,20) (3,2,30)。
# ==========================================================================

_IN_MULTI_MSG = '只能返回一列'


def test_in_subquery_multi_column_errors():
    """验收①②：`IN` / `NOT IN` 的子查询返回 2 列 → 显式报错

    原实现 `id IN (SELECT uid, amt FROM orders)` 静默只用 uid 一列，
    返回 [(1,), (2,)]（NOT IN 返回 [(3,)]）——看似正确、实际错误。
    """
    with _ScalarEnv() as e:
        for sql in (
            "SELECT id FROM users "
            "WHERE id IN (SELECT uid, amt FROM orders)",
            "SELECT id FROM users "
            "WHERE id NOT IN (SELECT uid, amt FROM orders)",
        ):
            msg = e.err(sql)
            assert _IN_MULTI_MSG in msg, msg
            assert '2 列' in msg, msg
            assert '行值' in msg, msg


def test_in_subquery_multi_column_empty_result_errors():
    """验收③：子查询**命中 0 行**但静态列数 > 1 → 同样显式报错

    空集不能让多列子查询悄悄通过（否则 `NOT IN` 会恒真、`IN` 会恒假，
    用户看到的是「没有匹配行」这种错误答案）。
    `WHERE uid=99` 是必然空集（该表没有 uid=99）；
    `SELECT *` 形态静态判不出列数，由「结果列名个数」兜住。
    """
    with _ScalarEnv() as e:
        for sql in (
            "SELECT id FROM users "
            "WHERE id IN (SELECT uid, amt FROM orders WHERE uid=99)",
            "SELECT id FROM users "
            "WHERE id NOT IN (SELECT uid, amt FROM orders WHERE uid=99)",
            "SELECT id FROM users WHERE id IN (SELECT * FROM orders)",
            "SELECT id FROM users WHERE id NOT IN (SELECT * FROM orders)",
        ):
            msg = e.err(sql)
            assert _IN_MULTI_MSG in msg, msg


def test_in_subquery_single_column_unchanged():
    """验收④：单列子查询行为不变（IN → [(1,),(2,)]，NOT IN → [(3,)]）

    同时锁住字面量列表 `IN (1,2,3)` 不受影响。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT id FROM users "
                      "WHERE id IN (SELECT uid FROM orders)")
        assert rows == [(1,), (2,)], rows
        _, rows = e.q("SELECT id FROM users "
                      "WHERE id NOT IN (SELECT uid FROM orders)")
        assert rows == [(3,)], rows
        _, rows = e.q("SELECT id FROM users WHERE id IN (1,2,3)")
        assert rows == [(1,), (2,), (3,)], rows
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN (1,2,3)")
        assert rows == [], rows


def test_in_subquery_null_three_valued_unchanged():
    """验收⑤：`IN` 的 NULL 三值语义不变（候选集含 NULL 时 NOT IN 不通过）

    orders 里插入一行 uid=NULL 后：
    * `NOT IN (SELECT uid ...)` → 候选集含 NULL → 整体 UNKNOWN → []；
    * `IN (SELECT uid ...)` → NULL 匹配不了任何值 → 仍只剩 [(1,), (2,)]；
    * 字面量列表 `NOT IN (1,2,NULL)` 同样 → []。
    """
    with _ScalarEnv() as e:
        r = e.ex.execute_sql("INSERT INTO orders VALUES (4,NULL,40)", {})
        assert r.get('type') == 'ok', r
        _, rows = e.q("SELECT uid FROM orders")
        assert rows == [(1,), (1,), (2,), (None,)], rows

        _, rows = e.q("SELECT id FROM users "
                      "WHERE id NOT IN (SELECT uid FROM orders)")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM users "
                      "WHERE id IN (SELECT uid FROM orders)")
        assert rows == [(1,), (2,)], rows
        _, rows = e.q("SELECT id FROM users WHERE id NOT IN (1,2,NULL)")
        assert rows == [], rows


def test_in_subquery_correlated_multi_column_errors():
    """相关子查询路径（`_eval_correlated_predicate`）同样拦多列"""
    with _ScalarEnv() as e:
        msg = e.err("SELECT u.id FROM users u WHERE u.id IN "
                    "(SELECT o.uid, o.amt FROM orders o WHERE o.uid=u.id)")
        assert _IN_MULTI_MSG in msg, msg
        assert '2 列' in msg, msg
        # 单列相关 IN 行为不变
        _, rows = e.q("SELECT u.id FROM users u WHERE u.id IN "
                      "(SELECT o.uid FROM orders o WHERE o.uid=u.id)")
        assert rows == [(1,), (2,)], rows


def test_having_in_subquery_multi_column_errors():
    """验收③（HAVING）：`HAVING <col> IN (多列子查询)` 同样显式报错

    HAVING 的子查询在分组之后逐分组求值（`_eval_having_with_subquery`
    → `_eval_outer_where` → `_eval_plain_subquery_predicate`），这条路径
    原来也取 `r[0]`，必须一起收紧。
    """
    with _ScalarEnv() as e:
        msg = e.err("SELECT uid, COUNT(*) FROM orders GROUP BY uid "
                    "HAVING uid IN (SELECT uid, amt FROM orders)")
        assert _IN_MULTI_MSG in msg, msg
        # 单列 HAVING IN 行为不变：uid=1 有 2 行 → 命中
        _, rows = e.q("SELECT uid, COUNT(*) FROM orders GROUP BY uid "
                      "HAVING uid IN (SELECT uid FROM orders)")
        assert (1, 2) in rows and (2, 1) in rows, rows


def test_join_in_subquery_multi_column_errors():
    """验收⑦：JOIN 路径的 `IN` 多列子查询 → 显式报错（不能只比第一列）

    纯计划路径（`plan_executor._eval_condition` → `_exec_subquery_values`）
    原来返回 `{row[0] ...}`，把 `u.id IN (SELECT uid, amt ...)` 静默算成
    `u.id IN (SELECT uid ...)`，JOIN 结果 [(1,), (1,), (2,)] 看似正确、实际错误。
    """
    with _ScalarEnv() as e:
        for sql in (
            "SELECT u.id " + JOIN_SQ +
            " WHERE u.id IN (SELECT uid, amt FROM orders)",
            "SELECT u.id " + JOIN_SQ +
            " WHERE u.id NOT IN (SELECT uid, amt FROM orders)",
            # 空结果集 + 多列同样要报错
            "SELECT u.id " + JOIN_SQ +
            " WHERE u.id IN (SELECT uid, amt FROM orders WHERE uid=99)",
            # 静态判不出列数（SELECT *）→ 由结果列数兜住
            "SELECT u.id " + JOIN_SQ +
            " WHERE u.id IN (SELECT * FROM orders)",
        ):
            msg = e.err(sql)
            assert _IN_MULTI_MSG in msg, msg
        # 回归：单列子查询在 JOIN 路径上行为不变
        _, rows = e.q("SELECT u.id " + JOIN_SQ +
                      " WHERE u.id IN (SELECT uid FROM orders)")
        assert rows == [(1,), (1,), (2,)], rows


def test_row_value_in_and_comparison_explicitly_rejected():
    """验收⑥：行值（多列）`IN` 与行值比较 → 显式报错（明确「不支持」）

    本引擎的存储层 `_match` 与四条求值路径都只支持单列比较，没有元组匹配
    能力，因此选择「显式报错」而不是「只比第一列」（后者是静默错答）。
    原先这两种形态落到兜底的 `WHERE 条件错误`（也是失败，但看不出原因），
    现在给出说明并给出 EXISTS 改写建议。
    """
    with _ScalarEnv() as e:
        for sql in (
            "SELECT id FROM users "
            "WHERE (id, id) IN (SELECT uid, uid FROM orders)",
            "SELECT id FROM users "
            "WHERE (id, id) IN (SELECT uid, amt FROM orders)",
            "SELECT id FROM users WHERE (id, id) = (1, 2)",
            "SELECT id FROM users WHERE (id, city) IN (SELECT uid, uid FROM orders)",
        ):
            try:
                e.parser.parse(sql)
            except SyntaxError as ex:
                assert '不支持行值' in str(ex), str(ex)
            else:
                raise AssertionError(f'行值形态应当显式报错: {sql}')


def test_having_in_list_is_evaluated_not_silently_false():
    """任务③附带：HAVING 里的**字面量** `IN` 列表不再静默判假

    执行器的统一求值器 `_eval_condition` 原先**没有 IN 分支**，字面量列表
    会掉进普通比较逻辑（`r = value` 取不到 → None → `op == '!='` → False），
    实测 `... GROUP BY uid HAVING uid IN (1,2)` 静默返回 0 个分组
    （WHERE 里的字面量列表在 `_cmd_select` 已被 `_rewrite_in_lists` 改写，
    所以只有 HAVING 暴露）。现在按三值语义求值；HAVING 里引用**取不到的
    列**（GROUP BY 后只剩分组键与聚合值）改为显式报错，不静默丢分组。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT uid, COUNT(*) FROM orders GROUP BY uid "
                      "HAVING uid IN (1,2)")
        assert sorted(rows) == [(1, 2), (2, 1)], rows
        _, rows = e.q("SELECT uid, COUNT(*) FROM orders GROUP BY uid "
                      "HAVING uid NOT IN (1)")
        assert rows == [(2, 1)], rows
        # 三值语义：列表含 NULL → NOT IN 整体 UNKNOWN → 无分组通过
        _, rows = e.q("SELECT uid, COUNT(*) FROM orders GROUP BY uid "
                      "HAVING uid NOT IN (1,2,NULL)")
        assert rows == [], rows
        _, rows = e.q("SELECT city, COUNT(*) FROM users GROUP BY city "
                      "HAVING city IN ('BJ')")
        assert rows == [('BJ', 2)], rows
        # 取不到的列 → 显式报错（原来静默判假、整个分组被丢掉）
        msg = e.err("SELECT id, COUNT(*) FROM users GROUP BY id "
                    "HAVING city IN ('BJ')")
        assert '取不到' in msg, msg


def test_join_having_qualified_column_not_silently_false():
    """任务③附带：JOIN 计划路径的 `HAVING <限定列>` 不再静默判假

    `_exec_GroupBy` 把输出列名统一成大写（`U.ID`），而 `_resolve_col` 原先
    区分大小写，于是 `HAVING u.id = 1` / `HAVING u.id IN (1)` 都解析不到列，
    被 `except ValueError: return False` 吞掉 → 静默返回 0 行
    （正确结果是 [(1, 2)]）。标识符按 SQL 惯例改成大小写不敏感后算对；
    HAVING 引用未投影的列仍显式报错。
    """
    with _ScalarEnv() as e:
        _, rows = e.q("SELECT u.id, COUNT(*) " + JOIN_SQ +
                      " GROUP BY u.id HAVING u.id IN (1)")
        assert rows == [(1, 2)], rows
        _, rows = e.q("SELECT u.id, COUNT(*) " + JOIN_SQ +
                      " GROUP BY u.id HAVING u.id = 1")
        assert rows == [(1, 2)], rows
        _, rows = e.q("SELECT u.id, COUNT(*) " + JOIN_SQ +
                      " GROUP BY u.id, u.city HAVING u.id IN (1,2)")
        assert sorted(rows) == [(1, 2), (2, 1)], rows
        msg = e.err("SELECT u.id, COUNT(*) " + JOIN_SQ +
                    " GROUP BY u.id HAVING u.city IN ('BJ')")
        assert '取不到' in msg, msg
        # 多列子查询在 HAVING + JOIN 组合下同样显式报错
        msg = e.err("SELECT u.id, COUNT(*) " + JOIN_SQ +
                    " GROUP BY u.id HAVING u.id IN (SELECT uid, amt FROM orders)")
        assert _IN_MULTI_MSG in msg, msg


# ==========================================================================
# 追加（本会话）：GROUP BY 严格校验（等价 MySQL ONLY_FULL_GROUP_BY）
#
# 原来的静默错答：GROUP BY 之后引用**非分组键、且未被聚合**的裸列时，
#   * 计划路径（`plan_executor._build_group_row`）取「组内第一行」的值；
#   * 统一路径（`executor._group_rows`）把该列从输出里丢掉，再报
#     「列不存在」——报错原文看不出是 GROUP BY 的问题。
# 现在两条路径都改成**显式报错**，错误信息里带列名与修复办法
# （「把 x 加入 GROUP BY 或用聚合函数包裹」）。
#
# 校验实现：`parser.validate_group_by`（静态，只吃 AST）——
#   ① `executor._cmd_select` 最前面（覆盖单表 / JOIN / CTE / FROM 子查询 /
#      UNION 所有分支，报错发生在取数之前）；
#   ② 运行期兜底：`executor._group_rows`（统一路径）与
#      `plan_executor._exec_GroupBy` / `_exec_Aggregate` / `_build_group_row`
#      （计划路径），保证绕过 `_cmd_select` 的内部调用也拦得住。
#
# 逃生口（默认必须是严格）：`Executor.strict_group_by = False`
# （计划路径同名开关 `PlanExecutor.strict_group_by`）或环境变量
# `LIGHTDB_STRICT_GROUP_BY=0`。
# ==========================================================================

_GB_JOIN = ("FROM users u JOIN orders o ON u.id = o.user_id")


def test_group_by_strict_select_bare_column_errors():
    """验收①：SELECT 投影里的非分组键裸列 → 显式报错（含列名与修复办法）

    `SELECT dept, name FROM emp GROUP BY dept` 原来在计划路径上取组内
    第一行的 name（同一份 SQL 换台机器就可能换一组答案）。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT dept, name FROM emp GROUP BY dept")
        assert 'name' in msg, msg
        assert 'GROUP BY' in msg, msg
        assert 'MAX(name)' in msg or '聚合函数' in msg, msg


def test_group_by_strict_valid_forms_not_broken():
    """验收②③④（正常侧）+ 不误伤：分组键 / 聚合 / HAVING / ORDER BY

    * 分组键本身就是裸列（含 `dept, salary` 双键）；
    * 聚合函数作用于任意列；
    * HAVING 引用分组键或聚合值；
    * ORDER BY 引用分组键或**聚合别名**（`tot`）。
    """
    with _Env() as e:
        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept")
        assert len(rows) == 3 and sum(r[1] for r in rows) == 8, rows

        cols, rows = e.q(
            "SELECT dept, salary, COUNT(*) FROM emp GROUP BY dept, salary")
        assert cols == ['dept', 'salary', 'COUNT(*)'], cols
        assert sum(r[2] for r in rows) == 8, rows

        _, rows = e.q("SELECT dept, SUM(salary) FROM emp GROUP BY dept "
                      "HAVING SUM(salary) > 100")
        assert sorted(rows) == [('Eng', 320), ('HR', 275), ('Sales', 200)], rows

        _, rows = e.q("SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                      "HAVING dept = 'HR'")
        assert rows == [('HR', 3)], rows

        _, rows = e.q("SELECT dept, SUM(salary) AS tot FROM emp GROUP BY dept "
                      "ORDER BY tot DESC")
        assert rows[0] == ('Eng', 320) and rows[-1] == ('Sales', 200), rows

        _, rows = e.q("SELECT dept, SUM(salary) FROM emp GROUP BY dept "
                      "ORDER BY dept")
        assert len(rows) == 3, rows

        # 无 GROUP BY 的纯聚合、无聚合的普通查询、DISTINCT 都不受影响
        _, rows = e.q("SELECT COUNT(*) FROM emp")
        assert rows == [(8,)], rows
        _, rows = e.q("SELECT name FROM emp WHERE dept = 'HR'")
        assert len(rows) == 3, rows
        _, rows = e.q("SELECT DISTINCT dept FROM emp")
        assert len(rows) == 3, rows

        # 常量投影与分组键无关，必须继续可用
        _, rows = e.q("SELECT 1 FROM emp GROUP BY dept")
        assert rows == [(1,), (1,), (1,)], rows


def test_group_by_strict_having_bare_column_errors():
    """验收④（报错侧）：HAVING 引用非分组键裸列 → 显式报错

    沿用既有实现的「取不到」措辞（`executor._eval_condition` /
    `plan_executor._eval_condition` 对同一语义本来就报这个），
    因此断言里同时锁住「取不到」与列名。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT dept, SUM(salary) FROM emp "
                             "GROUP BY dept HAVING name = 'Alice'")
        assert '取不到' in msg, msg
        assert 'name' in msg, msg
        assert 'GROUP BY' in msg, msg


def test_group_by_strict_order_by_bare_column_errors():
    """验收⑤：ORDER BY 引用非分组键裸列 → 显式报错（不静默不排序）

    `_finish_select` 对解析不到的 ORDER BY 列是「静默不排序」，那同样是
    错答（用户以为排好了），因此这里必须报错。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT dept, SUM(salary) FROM emp "
                             "GROUP BY dept ORDER BY name")
        assert 'ORDER BY' in msg and 'name' in msg, msg
        assert 'GROUP BY' in msg, msg
        # 同一个非分组键列：HAVING / ORDER BY 都要拦
        msg = _assert_err(e, "SELECT dept, SUM(salary) FROM emp "
                             "GROUP BY dept ORDER BY salary")
        assert 'salary' in msg, msg


def test_group_by_strict_without_group_by_mixed_errors():
    """验收⑥：无 GROUP BY 却混用聚合与非聚合列 → 显式报错

    `SELECT dept, SUM(salary) FROM emp` 原来被优化器静默改写成
    `GROUP BY dept`（见 optimizer.optimize 的 `group_cols = non_agg`），
    用户完全看不出语义被改了。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT dept, SUM(salary) FROM emp")
        assert 'dept' in msg and 'GROUP BY' in msg, msg
        assert '聚合' in msg, msg
        # 纯聚合仍然正常
        _, rows = e.q("SELECT SUM(salary) FROM emp")
        assert rows == [(795,)], rows


def test_group_by_strict_qualified_and_alias_forms():
    """验收⑦：限定名与别名形态

    `u.city` 是分组键（限定名两边写法一致）→ 正常；
    `u.name` 不是 → 报错且错误信息里保留限定名。
    """
    with _Env() as e:
        _, rows = e.q("SELECT u.city, COUNT(*) FROM users u GROUP BY u.city")
        assert sorted(rows) == [('BJ', 2), ('SH', 1)], rows
        # 限定名与裸名混写（GROUP BY u.city，SELECT city）也要认成同一个键
        _, rows = e.q("SELECT city, COUNT(*) FROM users u GROUP BY u.city")
        assert sorted(rows) == [('BJ', 2), ('SH', 1)], rows
        # 别名形态：ORDER BY 引用分组键的别名
        _, rows = e.q("SELECT u.city AS c, COUNT(*) FROM users u "
                      "GROUP BY u.city ORDER BY c")
        assert sorted(rows) == [('BJ', 2), ('SH', 1)], rows

        msg = _assert_err(e, "SELECT u.city, u.name FROM users u "
                             "GROUP BY u.city")
        assert 'u.name' in msg, msg
        assert 'GROUP BY' in msg, msg


def test_group_by_strict_join_plan_path():
    """两条执行路径都覆盖：JOIN 查询走**计划路径**

    `_cmd_select` 的静态校验与 `plan_executor._exec_GroupBy` 的运行期兜底
    都会拦住；HAVING 位置的非分组键列同样拦。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT u.city, u.name, COUNT(*) " + _GB_JOIN +
                             " GROUP BY u.city")
        assert 'u.name' in msg and 'GROUP BY' in msg, msg

        msg = _assert_err(e, "SELECT u.city, COUNT(*) " + _GB_JOIN +
                             " GROUP BY u.city HAVING u.name = 'a'")
        assert 'u.name' in msg and '取不到' in msg, msg

        msg = _assert_err(e, "SELECT u.city, COUNT(*) " + _GB_JOIN +
                             " GROUP BY u.city ORDER BY u.salary")
        assert 'ORDER BY' in msg and 'u.salary' in msg, msg

        # 合法形态不误伤：JOIN + GROUP BY（分组键 / 聚合）
        _, rows = e.q("SELECT u.city, COUNT(*), SUM(o.amount) " + _GB_JOIN +
                      " GROUP BY u.city")
        assert rows == [('BJ', 3, 60)], rows


def test_group_by_strict_window_with_group_by():
    """有 GROUP BY 时的窗口函数：键是分组键 → 正常；否则显式报错

    引擎在 GROUP BY 之后只剩「分组键 + 聚合值」，窗口函数引用别的列
    取不到确定值（`_compute_window` 对解析不到的列会按 -1 索引静默取
    最后一列 —— 典型的静默错答），因此这里显式报错而非静默降级。
    """
    with _Env() as e:
        cols, rows = e.q(
            "SELECT dept, COUNT(*) AS c, ROW_NUMBER() OVER (ORDER BY dept) "
            "AS rn FROM emp GROUP BY dept")
        assert cols == ['dept', 'c', 'rn'], cols
        assert len(rows) == 3, rows

        msg = _assert_err(e, "SELECT dept, ROW_NUMBER() OVER (ORDER BY salary) "
                             "AS rn FROM emp GROUP BY dept")
        assert 'salary' in msg and '窗口' in msg, msg


def test_group_by_strict_escape_hatch():
    """验收⑧：逃生口（默认严格，可关闭回旧行为）

    三种关闭方式都验证：
    ① 实例属性 `ex.strict_group_by = False`；
    ② 类属性 `Executor.strict_group_by = False`；
    ③ 环境变量 `LIGHTDB_STRICT_GROUP_BY=0`。
    关闭后 JOIN 计划路径恢复「组内第一行」的旧行为（BJ 组第一行是 'a'）。
    """
    from lightdb.sql.executor import Executor
    from lightdb.sql.plan_executor import PlanExecutor

    sql = ("SELECT u.city, u.name, COUNT(*) " + _GB_JOIN +
           " GROUP BY u.city")
    with _Env() as e:
        # 默认严格
        _assert_err(e, sql)

        # ① 实例属性
        e.ex.strict_group_by = False
        try:
            _, rows = e.q(sql)
            assert rows == [('BJ', 'a', 3)], rows
        finally:
            del e.ex.strict_group_by
        _assert_err(e, sql)

        # ② 类属性（计划路径同名开关必须跟着一起关掉，否则「关不掉」）
        old_cls = Executor.strict_group_by
        old_plan = PlanExecutor.strict_group_by
        Executor.strict_group_by = False
        try:
            _, rows = e.q(sql)
            assert rows == [('BJ', 'a', 3)], rows
        finally:
            Executor.strict_group_by = old_cls
            PlanExecutor.strict_group_by = old_plan
        _assert_err(e, sql)

        # ③ 环境变量（Executor.strict_group_by 为 None 时生效）
        old_env = os.environ.get('LIGHTDB_STRICT_GROUP_BY')
        os.environ['LIGHTDB_STRICT_GROUP_BY'] = '0'
        try:
            _, rows = e.q(sql)
            assert rows == [('BJ', 'a', 3)], rows
        finally:
            if old_env is None:
                os.environ.pop('LIGHTDB_STRICT_GROUP_BY', None)
            else:
                os.environ['LIGHTDB_STRICT_GROUP_BY'] = old_env
        _assert_err(e, sql)


# ==========================================================================
# 追加：**标量函数（任务 1）与算术谓词（任务 2）**
#
# 现状（修复前）：
#   * `SELECT LOWER(name) FROM emp` 整条投影被下推给存储层，报
#     「列 'LOWER(name)' 不存在」（存储层只认列名）；
#   * `WHERE salary * 2 > 230` 连解析都过不去（SyntaxError）；
#   * `ORDER BY LENGTH(name) DESC` 被解析成「按列 LENGTH 排序」，
#     而且 DESC 被**静默丢掉**（返回升序）；
#   * `GROUP BY UPPER(dept)`：统一路径报「GROUP BY 列不存在」，计划路径
#     把解析不到的键静默跳过 → 整表聚成一组；
#   * `SELECT SUM(salary * 2)` 报「表不存在: '列 SUM(salary * 2) 不存在'」。
#
# 修复后（实现见 lightdb/sql/parser.py 末尾「标量表达式」一节，
# 求值挂在 executor.Executor / plan_executor.PlanExecutor 两层）：
#   * 支持 7 个**单行标量函数**（大小写不敏感）：
#     LOWER / UPPER / LENGTH / TRIM / ABS / ROUND(1~2 参数) /
#     CONCAT(2 个及以上参数)；
#   * 可用位置：SELECT 投影（含 `AS 别名`，别名可被外层查询引用）/
#     WHERE / ORDER BY / GROUP BY / HAVING；可嵌进聚合与算术；
#     可作用于字面量（`LENGTH('abc')`）；
#   * WHERE / HAVING 的左右两侧都可以是「列 运算符 字面量」
#     （`+ - * /`，可任意嵌套）；
#   * NULL 语义按 SQL 标准：任一参数为 NULL → 结果 NULL
#     （CONCAT 也按标准返回 NULL，不用 MySQL 的 CONCAT_WS 语义）；
#   * 除零返回 NULL（与既有 `SELECT salary/0` 的行为一致：不抛异常、不崩溃）；
#   * **不支持的函数名一律显式报错**，并回列支持清单 —— 绝不静默返回
#     NULL，也绝不把函数名当成列名。
#
# 数据仍是 `_Env`：emp 8 行（salary 见 EMP_SAL）、users 3 行、orders 3 行。
# ==========================================================================


def test_scalar_functions_in_projection():
    """验收①②：SELECT 投影里的标量函数（含 AS 别名 / 字面量 / 嵌套 / NULL）"""
    with _Env() as e:
        # ① 逐行给出（验收点 1）
        cols, rows = e.q("SELECT LOWER(name) AS ln, LENGTH(name) AS n, "
                         "salary FROM emp ORDER BY id")
        assert cols == ['ln', 'n', 'salary'], cols
        assert rows == [
            ('alice', 5, 120), ('bob', 3, 100), ('carol', 5, 100),
            ('dave', 4, 110), ('eve', 3, 90), ('frank', 5, 95),
            ('grace', 5, 95), ('henry', 5, 85)], rows

        # 别名能被外层引用（验收点 1 的第二半）
        cols, rows = e.q("SELECT * FROM (SELECT LOWER(name) AS ln FROM emp) x "
                         "WHERE x.ln = 'alice'")
        assert cols == ['ln'], cols
        assert rows == [('alice',)], rows

        # 嵌套调用 + TRIM/CONCAT
        cols, rows = e.q("SELECT TRIM(CONCAT(name, ' ', dept)) AS t FROM emp "
                         "ORDER BY id LIMIT 2")
        assert cols == ['t'], cols
        assert rows == [('Alice Eng',), ('Bob Eng',)], rows

        # 作用于字面量（验收点 7）
        cols, rows = e.q("SELECT LENGTH('abc') FROM emp LIMIT 1")
        assert cols == ["LENGTH('abc')"], cols
        assert rows == [(3,)], rows

        # NULL 语义：任一参数 NULL → 结果 NULL（CONCAT 按标准）
        _, rows = e.q("SELECT LENGTH(NULL) AS n, UPPER(NULL) AS u, "
                      "ABS(NULL) AS a, ROUND(NULL, 1) AS r, "
                      "CONCAT('a', NULL) AS c FROM emp LIMIT 1")
        assert rows == [(None, None, None, None, None)], rows

        # ROUND 必须是 SQL 的「四舍五入」（half away from zero），
        # 不能退化成 Python 银行家舍入（round(2.5) == 2 是错的）
        _, rows = e.q("SELECT ROUND(2.5) AS a, ROUND(0.125, 2) AS b, "
                      "ROUND(-2.5) AS c, ROUND(99.375, 1) AS d "
                      "FROM emp LIMIT 1")
        assert rows == [(3, 0.13, -3, 99.4)], rows


def test_scalar_functions_in_where():
    """验收②：WHERE 谓词里的标量函数（含 OR / NOT 组合）"""
    with _Env() as e:
        _, rows = e.q("SELECT id FROM emp WHERE LOWER(name) = 'alice'")
        assert rows == [(1,)], rows

        _, rows = e.q("SELECT id FROM emp WHERE LENGTH(name) > 3 ORDER BY id")
        assert rows == [(1,), (3,), (4,), (6,), (7,), (8,)], rows

        # 与普通列条件混在 AND / OR 里
        _, rows = e.q("SELECT id FROM emp WHERE LOWER(name) = 'alice' "
                      "OR LENGTH(name) = 3 ORDER BY id")
        assert rows == [(1,), (2,), (5,)], rows
        _, rows = e.q("SELECT id FROM emp WHERE dept = 'Eng' "
                      "AND LOWER(name) = 'bob'")
        assert rows == [(2,)], rows

        # NOT 包裹表达式谓词（三值语义由统一求值器处理）
        _, rows = e.q("SELECT id FROM emp WHERE NOT (LOWER(name) = 'alice') "
                      "ORDER BY id")
        assert rows == [(2,), (3,), (4,), (5,), (6,), (7,), (8,)], rows

        # 与 `NOT (... LIKE ...)`（存储层没有取反运算符 → negate 标记）共存：
        # 整个 WHERE 改由统一求值器逐行过滤，negate 必须**就地展开**，
        # 否则嵌在 AND 里的 NOT 会被当成认不出的节点而静默丢掉
        _, rows = e.q("SELECT id FROM emp WHERE NOT (name LIKE 'A%') "
                      "AND LOWER(dept) = 'eng' ORDER BY id")
        assert rows == [(2,), (3,)], rows

        # 右侧是表达式（列 vs 函数结果）：原来会被当成字符串字面量比较，
        # 静默返回空集；现在逐行求值
        _, rows = e.q("SELECT id FROM emp WHERE name = UPPER(dept) ORDER BY id")
        assert rows == [], rows


def test_scalar_functions_in_order_by():
    """验收③：ORDER BY 里的标量函数（DESC 不能被丢掉）"""
    with _Env() as e:
        _, rows = e.q("SELECT name FROM emp ORDER BY LENGTH(name) DESC LIMIT 3")
        # Alice / Carol / Frank / Grace 都是 5 个字符，稳定排序保持 id 顺序
        assert rows == [('Alice',), ('Carol',), ('Frank',)], rows

        _, rows = e.q("SELECT name FROM emp ORDER BY LENGTH(name) LIMIT 2")
        assert rows == [('Bob',), ('Eve',)], rows

        # 别名排序键与表达式排序键共存（别名走既有路径，表达式走新路径）
        # 注：多列 ORDER BY 只认第一列（`ORDER BY a, b` 的 b 被忽略）是
        # 引擎既有行为，本次未改动，因此这里只写单列排序键。
        _, rows = e.q("SELECT name, LENGTH(name) AS n FROM emp "
                      "ORDER BY n DESC LIMIT 2")
        assert rows == [('Alice', 5), ('Carol', 5)], rows


def test_scalar_functions_in_group_by_and_having():
    """验收④：GROUP BY / HAVING 里的标量函数（含聚合运算的 HAVING）"""
    with _Env() as e:
        # GROUP BY UPPER(dept)
        cols, rows = e.q("SELECT UPPER(dept) AS d, SUM(salary) AS s FROM emp "
                         "GROUP BY UPPER(dept) ORDER BY d")
        assert cols == ['d', 's'], cols
        assert rows == [('ENG', 320), ('HR', 275), ('SALES', 200)], rows

        # HAVING 里同时有聚合比较与标量函数比较（dept 是分组键）
        _, rows = e.q("SELECT dept, SUM(salary) AS s FROM emp GROUP BY dept "
                      "HAVING SUM(salary) > 100 AND LOWER(dept) = 'eng'")
        assert rows == [('Eng', 320)], rows

        # HAVING 里对聚合值做算术（`SUM(...) * 2 > ...`）
        _, rows = e.q("SELECT UPPER(dept) AS d, COUNT(*) AS c FROM emp "
                      "GROUP BY UPPER(dept) HAVING SUM(salary) * 2 > 500 "
                      "ORDER BY d")
        assert rows == [('ENG', 3), ('HR', 3)], rows

        # 分组键本身是标量函数时，SELECT 里的同名表达式直接命中分组列
        _, rows = e.q("SELECT dept, LOWER(dept) AS d FROM emp "
                      "GROUP BY dept ORDER BY dept")
        assert rows == [('Eng', 'eng'), ('HR', 'hr'), ('Sales', 'sales')], rows

        # ★ 非分组键列套函数 → 显式报错（绝不取组内第一行当代表值）
        msg = _assert_err(e, "SELECT LOWER(name) FROM emp GROUP BY dept")
        assert 'GROUP BY 严格模式' in msg, msg

        # ★ `GROUP BY UPPER(dept)` 之后 HAVING 里引用裸 dept：dept 不是分组键
        # （UPPER(dept) 才是），组内取值不唯一 → 显式报错，绝不猜一个值
        msg = _assert_err(
            e, "SELECT UPPER(dept) AS d, SUM(salary) AS s FROM emp "
               "GROUP BY UPPER(dept) HAVING SUM(salary) > 100 "
               "AND LOWER(dept) = 'eng'")
        assert '取不到' in msg and 'GROUP BY' in msg, msg


def test_aggregate_expression_arguments():
    """验收⑤：嵌套进聚合与算术（`SUM(v * 2)`、`ROUND(AVG(v), 1)`）"""
    with _Env() as e:
        cols, rows = e.q("SELECT SUM(salary * 2) AS s2, "
                         "ROUND(AVG(salary), 1) AS av FROM emp")
        assert cols == ['s2', 'av'], cols
        assert rows == [(1590, 99.4)], rows

        # 每个分组一套聚合表达式
        _, rows = e.q("SELECT ROUND(AVG(salary), 1) AS av FROM emp "
                      "GROUP BY dept")
        assert rows == [(106.7,), (100.0,), (91.7,)], rows

        # 聚合套标量函数 / 参数是函数（名字长度均值 35/8 = 4.375 → 4.38）
        _, rows = e.q("SELECT MAX(LENGTH(name)) AS m, "
                      "ROUND(AVG(LENGTH(name)), 2) AS a FROM emp")
        assert rows == [(5, 4.38)], rows

        # 存储层原来对非 5 个基础聚合**静默返回 NULL**（STDDEV/GROUP_CONCAT），
        # 现在由表达式聚合求值器算对
        _, rows = e.q("SELECT COUNT(*) AS c, TOTAL(salary) AS t, "
                      "GROUP_CONCAT(name) AS g FROM emp")
        assert rows[0][0] == 8 and rows[0][1] == 795.0, rows
        assert rows[0][2] == 'Alice,Bob,Carol,Dave,Eve,Frank,Grace,Henry', rows
        _, rows = e.q("SELECT STDDEV(salary) AS sd FROM emp")
        assert rows[0][0] is not None and abs(rows[0][0] - 10.439558) < 1e-5, rows


def test_arithmetic_predicates_in_where():
    """验收⑥：WHERE 里的算术谓词（`+ - * /` 任意嵌套、除零不崩）"""
    with _Env() as e:
        _, rows = e.q("SELECT id FROM emp WHERE salary * 2 > 230 ORDER BY id")
        assert rows == [(1,)], rows          # 120*2=240 > 230

        # 250 不大于 250：严格不等号不能算成「>=」
        _, rows = e.q("SELECT id FROM emp WHERE salary * 2 + 10 > 250 "
                      "ORDER BY id")
        assert rows == [], rows
        _, rows = e.q("SELECT id FROM emp WHERE salary * 2 + 10 > 240 "
                      "ORDER BY id")
        assert rows == [(1,)], rows

        _, rows = e.q("SELECT id FROM emp WHERE salary / 2 = 60 ORDER BY id")
        assert rows == [(1,)], rows          # 真除法：120/2 == 60

        _, rows = e.q("SELECT id FROM emp WHERE ABS(salary - 105) < 6 "
                      "ORDER BY id")
        assert rows == [(2,), (3,), (4,)], rows   # 100/100/110 距 105 小于 6

        # 括号与优先级：`(salary + 10) * 2`
        _, rows = e.q("SELECT id FROM emp WHERE (salary + 10) * 2 > 250 "
                      "ORDER BY id")
        assert rows == [(1,)], rows

        # 除零 → NULL（与既有 `SELECT salary/0` 一致）→ 比较是 UNKNOWN → 不通过
        _, rows = e.q("SELECT id FROM emp WHERE salary / 0 > 1 ORDER BY id")
        assert rows == [], rows

        # 左侧字面量、右侧列
        _, rows = e.q("SELECT id FROM emp WHERE 230 < salary * 2 ORDER BY id")
        assert rows == [(1,)], rows

        # 与普通条件混合（AND/OR 优先级由解析器保证）
        _, rows = e.q("SELECT id FROM emp WHERE dept = 'Eng' "
                      "AND salary * 2 >= 200 ORDER BY id")
        assert rows == [(1,), (2,), (3,)], rows


def test_unsupported_function_errors():
    """验收⑧：不支持的函数必须显式报错并回列支持清单（绝不静默 NULL）"""
    with _Env() as e:
        msg = _assert_err(e, "SELECT REVERSE(name) FROM emp")
        assert 'REVERSE' in msg, msg
        assert 'LOWER' in msg and 'CONCAT' in msg, msg
        assert '不支持' in msg, msg

        # WHERE / HAVING / ORDER BY / GROUP BY 位置同样显式报错
        msg = _assert_err(e, "SELECT id FROM emp WHERE REVERSE(name) = 'x'")
        assert 'REVERSE' in msg, msg
        msg = _assert_err(e, "SELECT dept FROM emp GROUP BY dept "
                             "HAVING REVERSE(dept) = 'x'")
        assert 'REVERSE' in msg, msg
        msg = _assert_err(e, "SELECT dept, COUNT(*) FROM emp GROUP BY dept "
                             "ORDER BY REVERSE(dept)")
        assert 'REVERSE' in msg, msg

        # 参数个数不对也要报（不是静默截断参数）
        msg = _assert_err(e, "SELECT UPPER(name, 1) FROM emp")
        assert '参数个数' in msg, msg
        msg = _assert_err(e, "SELECT CONCAT(name) FROM emp")
        assert '参数个数' in msg, msg
        msg = _assert_err(e, "SELECT ROUND(salary, 1, 2) FROM emp")
        assert '参数个数' in msg, msg


def test_scalar_functions_in_join_plan_path():
    """回归：JOIN 计划路径（plan_executor）上的同一套能力

    JOIN 查询走优化器 + 计划执行器：投影 / WHERE / HAVING / ORDER BY 都在
    计划节点上求值。原来投影里的函数报「列不存在」，WHERE 里的表达式谓词
    更是会被 `col_name is None → return True` **静默丢掉**（返回全部连接行）。
    """
    with _Env() as e:
        cols, rows = e.q("SELECT u.id, LOWER(u.name) AS ln FROM users u "
                         "JOIN orders o ON o.user_id = u.id ORDER BY u.id")
        assert cols == ['id', 'ln'], cols
        assert rows == [(1, 'a'), (1, 'a'), (2, 'b')], rows

        _, rows = e.q("SELECT u.id, o.amount FROM users u JOIN orders o "
                      "ON o.user_id = u.id WHERE LOWER(u.name) = 'a' "
                      "ORDER BY o.id")
        assert rows == [(1, 10), (1, 20)], rows

        _, rows = e.q("SELECT u.id FROM users u JOIN orders o "
                      "ON o.user_id = u.id WHERE o.amount * 2 > 25 "
                      "ORDER BY u.id")
        assert rows == [(1,), (2,)], rows

        _, rows = e.q("SELECT UPPER(u.city) AS c, SUM(o.amount) AS s "
                      "FROM users u JOIN orders o ON o.user_id = u.id "
                      "GROUP BY UPPER(u.city) ORDER BY c")
        assert rows == [('BJ', 60)], rows

        _, rows = e.q("SELECT u.city, COUNT(*) AS c FROM users u "
                      "JOIN orders o ON o.user_id = u.id GROUP BY u.city "
                      "HAVING SUM(o.amount) * 2 > 60")
        assert rows == [('BJ', 3)], rows

        _, rows = e.q("SELECT u.id, u.name FROM users u JOIN orders o "
                      "ON o.user_id = u.id ORDER BY LENGTH(u.name) DESC, u.id")
        assert rows == [(1, 'a'), (1, 'a'), (2, 'b')], rows

        # 不支持的函数在 JOIN 路径上同样显式报错
        msg = _assert_err(e, "SELECT REVERSE(u.name) FROM users u "
                             "JOIN orders o ON o.user_id = u.id")
        assert 'REVERSE' in msg, msg


def test_scalar_function_in_correlated_subquery():
    """相关子查询里也会出现表达式谓词：外层列必须被正确折叠

    漏折叠会让内层解析不到外层列（报「列不存在」或静默 0 行）。这里锁住
    「投影位置」与「WHERE 位置」两种相关子查询的折叠结果。
    """
    with _Env() as e:
        _, rows = e.q("SELECT u.id, (SELECT COUNT(*) FROM orders o "
                      "WHERE o.user_id = u.id AND LENGTH(u.name) = 1) AS c "
                      "FROM users u ORDER BY u.id")
        assert rows == [(1, 2), (2, 1), (3, 0)], rows

        _, rows = e.q("SELECT u.id FROM users u WHERE EXISTS (SELECT 1 "
                      "FROM orders o WHERE o.user_id = u.id "
                      "AND LOWER(u.name) = 'a') ORDER BY u.id")
        assert rows == [(1,)], rows

        # 非相关 EXISTS 的 IN 子查询里也能用表达式谓词
        _, rows = e.q("SELECT u.id FROM users u WHERE u.id IN "
                      "(SELECT o.user_id FROM orders o "
                      "WHERE o.amount * 2 > 25) ORDER BY u.id")
        assert rows == [(1,), (2,)], rows


def test_expression_predicate_in_update_delete():
    """UPDATE / DELETE 的 WHERE 里也能用函数 / 算术条件（按主键回填）

    存储层 `_match` 认不出表达式谓词（会 KeyError → 报「表不存在」，
    甚至可能被当成恒真条件误改全表），这里先在执行器里算出命中行的主键，
    再改写成 `pk = ... OR ...` 下推，语义与逐行判断一致。
    """
    with _Env() as e:
        r = e.ex.execute_sql("UPDATE emp SET salary = 86 "
                             "WHERE LOWER(name) = 'henry'", {})
        assert r.get('type') == 'ok' and r.get('affected') == 1, r
        _, rows = e.q("SELECT id, salary FROM emp WHERE LOWER(name) = 'henry'")
        assert rows == [(8, 86)], rows

        r = e.ex.execute_sql("DELETE FROM emp WHERE salary * 2 > 239", {})
        assert r.get('type') == 'ok' and r.get('affected') == 1, r
        _, rows = e.q("SELECT COUNT(*) FROM emp")
        assert rows == [(7,)], rows
        # 删掉的是 Alice（120*2=240 > 239）
        _, rows = e.q("SELECT id FROM emp WHERE LOWER(name) = 'alice'")
        assert rows == [], rows


def test_from_subquery_join_still_explicit_error():
    """任务 3（未实现）：`FROM (子查询) x JOIN 表 ...` 保持**显式报错**

    实现方式本身可行（把整条查询交给 JOIN 计划路径：优化器的 SubqueryScan
    原生支持「先算子查询再当普通输入参与连接」，实测结果正确），但既有回归
    `test_from_subquery_alias` 第 ⑧ 条**断言这里必须报错**，而本次任务的
    硬约束是「不要改任何既有断言」，因此保持显式报错（绝不静默错答）。
    """
    with _Env() as e:
        r = e.ex.execute(e.parser.parse(
            "SELECT * FROM (SELECT id FROM emp) e "
            "JOIN orders o ON e.id = o.user_id"), {})
        assert r.get('type') == 'error', r
        assert 'FROM 子查询暂不支持再 JOIN' in str(r.get('message')), r


# ==========================================================================
# 多键 ORDER BY（静默错答修复）
#
# 原来 `parsed['order_by']` 是**单个** `(列, desc)` 元组，`ORDER BY a, b`
# 只保留第一个键 —— 第二键及之后被静默丢弃（结果看起来"有序"但排序规则
# 是错的）。现在 parser 产出 `[(键文本, desc), ...]` 列表，执行器的统一
# 排序路径 `_finish_select` 与计划路径 `_exec_Sort` 都按**从最后一个键
# 往前逐个稳定排序**实现字典序多键排序，方向（ASC/DESC）逐键独立。
#
# 键的形态：普通列 / 限定列（`u.city`）/ 投影别名（`ORDER BY tot`）/
# 标量表达式（`LENGTH(name)`、`salary * 2`）/ 聚合表达式 / 列序号
# （`ORDER BY 2`，1 基）；**任何解析不到的键都显式报错**（报出第几个键），
# 绝不静默丢弃。
#
# NULL 定义：**NULL 视为最大值**（与 PostgreSQL 默认一致）——ASC 排最后、
# DESC 排最前，逐键一致、可预测。`NULLS FIRST/LAST` 语法仍不支持
# （键文本会带进去 → 显式报错，不会当成列名静默排序）。
# ==========================================================================


def test_order_by_multi_key_lexicographic():
    """验收 1 / 2 / 6：两键、隐式 ASC、三键都逐键生效

    原实现 `ORDER BY dept ASC, salary DESC` 只按 dept 排（同部门内保持
    插入顺序），第二键 salary DESC 被静默丢弃。
    """
    with _Env() as e:
        # ① dept ASC, salary DESC
        _, rows = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept ASC, salary DESC")
        assert rows == [
            ('Alice', 'Eng', 120), ('Bob', 'Eng', 100),
            ('Carol', 'Eng', 100),
            ('Frank', 'HR', 95), ('Grace', 'HR', 95), ('Henry', 'HR', 85),
            ('Dave', 'Sales', 110), ('Eve', 'Sales', 90),
        ], rows

        # ② 隐式 ASC：dept 升序，同部门内 salary 升序
        _, rows = e.q("SELECT name FROM emp ORDER BY dept, salary")
        assert rows == [('Bob',), ('Carol',), ('Alice',),
                        ('Henry',), ('Frank',), ('Grace',),
                        ('Eve',), ('Dave',)], rows

        # ③ 三键：dept ASC, salary DESC, name ASC
        _, rows = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept ASC, salary DESC, name ASC")
        assert rows == [
            ('Alice', 'Eng', 120), ('Bob', 'Eng', 100),
            ('Carol', 'Eng', 100),
            ('Frank', 'HR', 95), ('Grace', 'HR', 95), ('Henry', 'HR', 85),
            ('Dave', 'Sales', 110), ('Eve', 'Sales', 90),
        ], rows

        # ④ 第三键真的起作用：把第三键换成 name DESC，95/95 与 100/100
        #    这两组同值行的顺序必须翻过来（否则说明第三键被丢弃）
        _, rows = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept ASC, salary DESC, name DESC")
        assert rows == [
            ('Alice', 'Eng', 120), ('Carol', 'Eng', 100),
            ('Bob', 'Eng', 100),
            ('Grace', 'HR', 95), ('Frank', 'HR', 95), ('Henry', 'HR', 85),
            ('Dave', 'Sales', 110), ('Eve', 'Sales', 90),
        ], rows


def test_order_by_direction_per_key_independent():
    """验收 7：方向**逐键独立** —— 同一组数据两个方向对照

    `dept ASC, salary ASC` 与 `dept ASC, salary DESC` 的分组顺序完全一致
    （第一键都是 ASC），但每个分组内 salary 的序列正好相反。
    """
    with _Env() as e:
        _, asc = e.q("SELECT name, dept, salary FROM emp "
                     "ORDER BY dept ASC, salary ASC")
        _, desc = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept ASC, salary DESC")
        # 分组顺序由第一键（ASC）决定，两者必须一致
        assert [r[1] for r in asc] == ['Eng'] * 3 + ['HR'] * 3 + ['Sales'] * 2
        assert [r[1] for r in desc] == [r[1] for r in asc]
        # Eng 组内序列对照：ASC = Bob, Carol, Alice；DESC = Alice, Bob, Carol
        eng_asc = [(r[0], r[2]) for r in asc if r[1] == 'Eng']
        eng_desc = [(r[0], r[2]) for r in desc if r[1] == 'Eng']
        assert eng_asc == [('Bob', 100), ('Carol', 100), ('Alice', 120)]
        assert eng_desc == [('Alice', 120), ('Bob', 100), ('Carol', 100)]
        # 去掉并列（同 salary 的两行）后，两个方向的 salary 序列必须整体相反
        for grp in ('Eng', 'HR', 'Sales'):
            s_asc = [r[2] for r in asc if r[1] == grp]
            s_desc = [r[2] for r in desc if r[1] == grp]
            assert s_asc == list(reversed(s_desc)), (grp, s_asc, s_desc)

        # 混合方向（第一键 DESC、第二键 ASC）：组内同 salary 时按名字升序
        _, rows = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept DESC, salary ASC, name ASC")
        assert [r[1] for r in rows] == ['Sales'] * 2 + ['HR'] * 3 + ['Eng'] * 3
        assert [(r[0], r[2]) for r in rows if r[1] == 'HR'] == \
            [('Henry', 85), ('Frank', 95), ('Grace', 95)], rows


def test_order_by_expression_and_arithmetic_multi_key():
    """验收 3：表达式键（`LENGTH(name)`）与普通键混排

    原来 `ORDER BY LENGTH(name) DESC, name ASC` 只按 `LENGTH(name)` 排，
    同长度的行顺序不确定（插入顺序）。
    """
    with _Env() as e:
        _, rows = e.q("SELECT name FROM emp "
                      "ORDER BY LENGTH(name) DESC, name ASC")
        assert rows == [('Alice',), ('Carol',), ('Frank',), ('Grace',),
                        ('Henry',), ('Dave',), ('Bob',), ('Eve',)], rows

        # 算术表达式 + 普通列：salary*2 DESC 等价 salary DESC，同值时 name ASC
        _, rows = e.q("SELECT name, salary FROM emp "
                      "ORDER BY salary * 2 DESC, name ASC")
        assert rows == [('Alice', 120), ('Dave', 110), ('Bob', 100),
                        ('Carol', 100), ('Frank', 95), ('Grace', 95),
                        ('Eve', 90), ('Henry', 85)], rows

        # 表达式键 + LIMIT/OFFSET 一起用：排序必须在裁剪之前生效
        _, rows = e.q("SELECT name FROM emp "
                      "ORDER BY LENGTH(name) ASC, name DESC LIMIT 2 OFFSET 1")
        # LENGTH 升序：Bob/Eve(3) → Dave(4) → 5 个字母的一批；
        # 长度为 3 的并列按 name DESC → Eve, Bob，OFFSET 1 跳过 Eve
        assert rows == [('Bob',), ('Dave',)], rows


def test_order_by_group_by_alias_and_aggregate_multi_key():
    """验收 4：GROUP BY + 聚合别名 / 聚合表达式作为排序键"""
    with _Env() as e:
        # 聚合别名 tot 作为第一键，分组键作为第二键
        _, rows = e.q("SELECT dept, SUM(salary) AS tot FROM emp "
                      "GROUP BY dept ORDER BY tot DESC, dept ASC")
        assert rows == [('Eng', 320), ('HR', 275), ('Sales', 200)], rows

        # 别名同值（3/3/2）时第二键 dept ASC 决定顺序：Eng 在 HR 之前
        _, rows = e.q("SELECT dept AS d, COUNT(*) AS n FROM emp "
                      "GROUP BY dept ORDER BY n DESC, d ASC")
        assert rows == [('Eng', 3), ('HR', 3), ('Sales', 2)], rows
        # 第二键换成 DESC 必须整体反向（证明第二键真的生效）
        _, rows = e.q("SELECT dept AS d, COUNT(*) AS n FROM emp "
                      "GROUP BY dept ORDER BY n DESC, d DESC")
        assert rows == [('HR', 3), ('Eng', 3), ('Sales', 2)], rows

        # 没有别名、直接写聚合表达式作为排序键
        _, rows = e.q("SELECT dept FROM emp GROUP BY dept "
                      "ORDER BY SUM(salary) DESC, dept ASC")
        assert rows == [('Eng',), ('HR',), ('Sales',)], rows


def test_order_by_join_plan_and_materialized_path():
    """验收 5：JOIN 查询的多键排序（计划路径 + 物化连接路径）

    * `u.city` / `o.amt` 都在投影里 → 计划执行器 `_exec_Sort` 多键排序；
    * 排序键引用**未投影**的列（`SELECT u.id ... ORDER BY o.amt`）→
      计划路径解析不到（原来静默不排序），改走「先物化连接再由
      `_finish_select` 统一排序」的路径，结果必须正确；
    * 未知键 → 显式报错。
    """
    with _ScalarEnv() as e:      # users(id, city) / orders(id, uid, amt)
        _, rows = e.q("SELECT u.city, o.amt FROM users u "
                      "JOIN orders o ON o.uid=u.id "
                      "ORDER BY u.city ASC, o.amt DESC")
        assert rows == [('BJ', 30), ('BJ', 20), ('BJ', 10)], rows
        # 第二键方向独立
        _, rows = e.q("SELECT u.city, o.amt FROM users u "
                      "JOIN orders o ON o.uid=u.id "
                      "ORDER BY u.city ASC, o.amt ASC")
        assert rows == [('BJ', 10), ('BJ', 20), ('BJ', 30)], rows
        # 排序键引用未投影列：原实现静默不排序 → [(1,10),(1,20),(2,30)]
        _, rows = e.q("SELECT u.id FROM users u "
                      "JOIN orders o ON o.uid=u.id "
                      "ORDER BY o.amt DESC, u.id ASC")
        assert rows == [(2,), (1,), (1,)], rows
        # JOIN + 表达式排序键
        _, rows = e.q("SELECT u.city, o.amt FROM users u "
                      "JOIN orders o ON o.uid=u.id "
                      "ORDER BY LENGTH(u.city) DESC, o.amt ASC")
        assert rows == [('BJ', 10), ('BJ', 20), ('BJ', 30)], rows
        # JOIN + 未知键：显式报错
        msg = e.err("SELECT u.id FROM users u JOIN orders o ON o.uid=u.id "
                    "ORDER BY nosuchcol")
        assert '第 1 个排序键' in msg and 'nosuchcol' in msg, msg

    with _Env() as e:            # users(id, name, city) / orders(id, user_id, amount)
        _, rows = e.q("SELECT u.city, o.amount FROM users u "
                      "JOIN orders o ON o.user_id=u.id "
                      "ORDER BY u.city ASC, o.amount DESC")
        assert rows == [('BJ', 30), ('BJ', 20), ('BJ', 10)], rows
        # JOIN + ORDER BY + LIMIT/OFFSET：排序先于裁剪
        _, rows = e.q("SELECT u.city, o.amount FROM users u "
                      "JOIN orders o ON o.user_id=u.id "
                      "ORDER BY o.amount DESC, u.city ASC LIMIT 2 OFFSET 1")
        assert rows == [('BJ', 20), ('BJ', 10)], rows


def test_order_by_ordinal_and_projection_alias():
    """序号（1 基）与投影别名都能作为排序键

    `ORDER BY 2` 原来会被解析成 `{'column': '2'}` 交给存储层（静默不排序），
    现在按输出列序号解析；越界时显式报错。
    """
    with _Env() as e:
        _, rows = e.q("SELECT dept, salary FROM emp "
                      "ORDER BY 2 DESC, 1 ASC LIMIT 4")
        assert rows == [('Eng', 120), ('Sales', 110),
                        ('Eng', 100), ('Eng', 100)], rows
        # 序号与别名混用（别名 salary2 指向 salary）
        _, rows = e.q("SELECT name, salary AS salary2 FROM emp "
                      "ORDER BY salary2 ASC, 1 ASC LIMIT 2")
        assert rows == [('Henry', 85), ('Eve', 90)], rows
        # 序号越界 → 显式报错
        msg = _assert_err(e, "SELECT name, salary FROM emp ORDER BY 3")
        assert '序号' in msg and '3' in msg, msg


def test_order_by_unresolvable_key_explicit_error():
    """验收 8：解析不到的排序键 → 显式报错（第几个键 + 原因）

    原行为是「解析不到就静默不排序」：`ORDER BY nosuchcol` 返回未排序结果，
    `SELECT name FROM emp ORDER BY salary`（salary 没被投影）同样静默不排序。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT name FROM emp ORDER BY nosuchcol")
        assert '第 1 个排序键' in msg and 'nosuchcol' in msg, msg
        assert 'ORDER BY' in msg, msg

        msg = _assert_err(e, "SELECT name FROM emp ORDER BY 1 = 1")
        assert '第 1 个排序键' in msg and '1 = 1' in msg, msg

        # 坏在第二个键上 → 必须指出"第 2 个"
        msg = _assert_err(e, "SELECT name FROM emp "
                             "ORDER BY salary DESC, nosuchcol")
        assert '第 2 个排序键' in msg and 'nosuchcol' in msg, msg

        # 未投影的别名（cnt 既不是输出列也不是表列）
        msg = _assert_err(e, "SELECT dept, SUM(salary) AS tot FROM emp "
                             "GROUP BY dept ORDER BY tot DESC, cnt ASC")
        assert '第 2 个排序键' in msg and 'cnt' in msg, msg

        # 不支持的形态也不能被当成列名静默排序
        msg = _assert_err(e, "SELECT name FROM emp ORDER BY salary NULLS LAST")
        assert '第 1 个排序键' in msg and 'NULLS LAST' in msg, msg

        # `SELECT *` 的未知列同样必须报错（存储层 out_cols 是表列；
        # 原来 `except: pass` 会静默返回未排序结果）
        msg = _assert_err(e, "SELECT * FROM emp ORDER BY nosuchcol")
        assert '第 1 个排序键' in msg and 'nosuchcol' in msg, msg


def test_order_by_uncorrelated_subquery_and_cte_multi_key():
    """CTE / FROM 子查询 / 带子查询的查询里多键排序同样生效"""
    with _Env() as e:
        _, rows = e.q("WITH t AS (SELECT name, dept, salary FROM emp) "
                      "SELECT name, dept FROM t "
                      "ORDER BY dept ASC, salary DESC, name ASC")
        assert rows == [('Alice', 'Eng'), ('Bob', 'Eng'), ('Carol', 'Eng'),
                        ('Frank', 'HR'), ('Grace', 'HR'), ('Henry', 'HR'),
                        ('Dave', 'Sales'), ('Eve', 'Sales')], rows

        _, rows = e.q("SELECT d, s FROM (SELECT dept AS d, salary AS s "
                      "FROM emp) x ORDER BY d ASC, s DESC")
        assert rows == [('Eng', 120), ('Eng', 100), ('Eng', 100),
                        ('HR', 95), ('HR', 95), ('HR', 85),
                        ('Sales', 110), ('Sales', 90)], rows

        _, rows = e.q("SELECT name, dept FROM emp WHERE id IN "
                      "(SELECT id FROM emp WHERE salary >= 95) "
                      "ORDER BY dept ASC, name DESC")
        assert rows == [('Carol', 'Eng'), ('Bob', 'Eng'), ('Alice', 'Eng'),
                        ('Grace', 'HR'), ('Frank', 'HR'),
                        ('Dave', 'Sales')], rows


def test_order_by_distinct_multi_key_keeps_sort_values_aligned():
    """DISTINCT + 多键 ORDER BY：去重后排序键值必须跟着行一起重排

    旧实现在 DISTINCT 之后按「源行下标」对齐的表达式排序值会整体错位
    （长度不等时直接回退到按列名查找，查不到就静默不排序）。
    """
    with _Env() as e:
        _, rows = e.q("SELECT DISTINCT salary FROM emp "
                      "ORDER BY salary DESC LIMIT 3 OFFSET 2")
        assert rows == [(100,), (95,), (90,)], rows
        _, rows = e.q("SELECT DISTINCT dept FROM emp "
                      "ORDER BY LENGTH(dept) DESC, dept ASC")
        # LENGTH: Sales(5) > Eng(3) > HR(2)
        assert rows == [('Sales',), ('Eng',), ('HR',)], rows


def test_order_by_multi_key_null_is_largest():
    """NULL 排序定义：**NULL 视为最大值**（ASC 最后、DESC 最前），逐键一致"""
    with _Env() as e:
        e.ex.execute_sql("INSERT INTO emp VALUES (9,'Ivy','Eng',NULL)", {})
        _, rows = e.q("SELECT name, salary FROM emp "
                      "ORDER BY salary ASC, name ASC")
        assert rows[-1] == ('Ivy', None), rows
        _, rows = e.q("SELECT name, salary FROM emp "
                      "ORDER BY salary DESC, name ASC")
        assert rows[0] == ('Ivy', None), rows

        # 多键时 NULL 只在**它所属的键**上体现：Eng 组内 salary 升序，
        # Ivy 的 NULL 排在该组最后（但仍在 HR/Sales 之前，因为 dept 键优先）
        _, rows = e.q("SELECT name, dept, salary FROM emp "
                      "ORDER BY dept ASC, salary ASC, name ASC")
        assert [r[0] for r in rows if r[1] == 'Eng'] == \
            ['Bob', 'Carol', 'Alice', 'Ivy'], rows
        assert [r[1] for r in rows] == ['Eng'] * 4 + ['HR'] * 3 + ['Sales'] * 2


def test_order_by_single_key_and_window_regression():
    """验收 9：单键 ORDER BY + LIMIT/OFFSET 与窗口内 OVER (ORDER BY ...) 不回归

    窗口的 `OVER (ORDER BY ...)` 是**另一套逻辑**（`_compute_window`），
    本次只改顶层 ORDER BY 的解析与排序，窗口结果必须逐字节不变。
    """
    with _Env() as e:
        _, rows = e.q("SELECT name FROM emp "
                      "ORDER BY salary DESC LIMIT 3 OFFSET 2")
        assert rows == [('Bob',), ('Carol',), ('Frank',)], rows
        _, rows = e.q("SELECT name, salary FROM emp "
                      "ORDER BY salary DESC LIMIT 1, 2")
        assert rows == [('Dave', 110), ('Bob', 100)], rows
        _, rows = e.q("SELECT name, salary FROM emp "
                      "ORDER BY salary DESC LIMIT 2 OFFSET 6")
        assert rows == [('Eve', 90), ('Henry', 85)], rows

        # 窗口：窗口内部排序键原样（单键）。这里不写顶层 ORDER BY，
        # 行的物理顺序就是插入顺序，因此按 rn 排序后再比对。
        _, rows = e.q("SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) "
                      "AS rn FROM emp")
        assert sorted((r[1], r[0]) for r in rows) == [
            (1, 'Alice'), (2, 'Dave'), (3, 'Bob'), (4, 'Carol'),
            (5, 'Frank'), (6, 'Grace'), (7, 'Eve'), (8, 'Henry')], rows
        _, rows = e.q("SELECT name, DENSE_RANK() OVER (ORDER BY salary DESC) "
                      "AS drk FROM emp ORDER BY drk ASC, name ASC")
        assert rows == [('Alice', 1), ('Dave', 2), ('Bob', 3), ('Carol', 3),
                        ('Frank', 4), ('Grace', 4), ('Eve', 5),
                        ('Henry', 6)], rows

        # 窗口内的多键排序键（另一套逻辑，走 window.py 的分区排序）
        _, rows = e.q("SELECT name, dept, ROW_NUMBER() OVER "
                      "(PARTITION BY dept ORDER BY salary DESC, name ASC) "
                      "AS rn FROM emp ORDER BY dept ASC, rn ASC")
        assert rows == [('Alice', 'Eng', 1), ('Bob', 'Eng', 2),
                        ('Carol', 'Eng', 3),
                        ('Frank', 'HR', 1), ('Grace', 'HR', 2),
                        ('Henry', 'HR', 3),
                        ('Dave', 'Sales', 1), ('Eve', 'Sales', 2)], rows


# ==========================================================================
# 验收：JOIN 的 ON 不是「左右两表各一列」的可用等值连接键时，
#       按 SQL 标准回退成「笛卡尔积 + ON 作为连接后过滤条件」
#
# 根因（两处互相叠加的静默吞错）：
#   ① `plan_executor._resolve_join_keys` 两轮解析都失败时返回 (None, None)，
#      `_exec_HashJoin` 直接把 ON **整个丢掉**、吐笛卡尔积（恒真的
#      `ON o.id = o.id` 只是碰巧算对；`ON o.uid = 1`、`ON u.id = u.city`
#      这类非恒真形态会静默给出错误行集）；
#   ② 比较右侧的**列引用**被解析器当成字符串字面量
#      （`WHERE o.user_id = u.id` → `row[o.user_id] == 'u.id'`），恒不成立 →
#      静默返回空集。这才是「ON 写不出等值键 → 跨表 WHERE 算出 0 行」的直接原因，
#      单表 `WHERE user_id = id` 同样返回 0（同一类吞错）。
#
# 修法：`{'value_column': True}` 标记 + 求值器按列取值；ON 不可用作等值键时
# 由 `_exec_join_with_on_filter` 逐候选行求值 ON（含 LEFT/RIGHT 补 NULL），
# ON / WHERE 里引用不存在的列**显式报错**，绝不静默给空集。
# ==========================================================================

_ON_TAUTOLOGY = "FROM users u JOIN orders o ON o.id = o.id"
_ON_SELF_LEFT = "FROM users u JOIN orders o ON u.id = u.id"
_ON_EQUI = "FROM users u JOIN orders o ON o.user_id = u.id"


def test_join_on_self_compare_where_cross_table():
    """验收 1-3：ON 是自比较（不可用作等值键）时，跨表 WHERE 必须算对

    `ON o.id = o.id` / `ON u.id = u.id` 都不是连接键：标准语义下 ON 退化成
    连接后过滤（恒真 → 笛卡尔积 9 行），其上的 `WHERE o.user_id = u.id`
    按**列引用**比较 → 3 行。修复前这里返回 0 行且不报错（静默错答）。
    """
    with _Env() as e:
        # ③ 单独的 ON（恒真）仍是 9 行（笛卡尔积）
        _, rows = e.q("SELECT COUNT(*) " + _ON_TAUTOLOGY)
        assert rows == [(9,)], rows
        _, rows = e.q("SELECT COUNT(*) " + _ON_SELF_LEFT)
        assert rows == [(9,)], rows
        # ① ON o.id = o.id + 跨表 WHERE：3（修复前 0，且不报错）
        _, rows = e.q("SELECT COUNT(*) " + _ON_TAUTOLOGY +
                      " WHERE o.user_id = u.id")
        assert rows == [(3,)], rows
        # ② ON u.id = u.id + 跨表 WHERE：3（ON 在左表内部自比较）
        _, rows = e.q("SELECT COUNT(*) " + _ON_SELF_LEFT +
                      " WHERE o.user_id = u.id")
        assert rows == [(3,)], rows
        # WHERE 求值必须发生在**连接之后**：(u1,o2) 与 (u2,o3) 满足
        # `o.amount > u.id * 10`，(u1,o1) 的 10 > 10 不成立 → 2 行
        _, rows = e.q("SELECT COUNT(*) " + _ON_TAUTOLOGY +
                      " WHERE o.user_id = u.id AND o.amount > u.id * 10")
        assert rows == [(2,)], rows


def test_join_on_fallback_regression_equi_left_group_by():
    """验收 4-5：正常等值连接 / GROUP BY / LEFT JOIN 补 NULL 不回归

    正常等值 ON 仍走 HashJoin 快路径（性能不变），LEFT JOIN 的 ON 是连接
    条件（不是外层 WHERE），未匹配的左侧行必须补 NULL。
    """
    with _Env() as e:
        # ④ 正常等值连接 + GROUP BY
        cols, rows = e.q("SELECT u.city, COUNT(*) " + _ON_EQUI +
                         " GROUP BY u.city")
        assert rows == [('BJ', 3)], rows
        # 正常等值连接的行数
        _, rows = e.q("SELECT COUNT(*) " + _ON_EQUI)
        assert rows == [(3,)], rows
        # 反方向 ON（JOIN 进来的表写在左边）同样 3 行
        _, rows = e.q("SELECT COUNT(*) "
                      "FROM users u JOIN orders o ON o.id = o.user_id")
        assert rows == [(3,)], rows
        # ⑤ LEFT JOIN + 不可用作键的 ON（恒真）→ 9 行（右侧恒匹配，无补 NULL）
        _, rows = e.q("SELECT COUNT(*) FROM users u LEFT JOIN orders o "
                      "ON o.id = o.id")
        assert rows == [(9,)], rows
        # LEFT JOIN + 该 ON + 跨表 WHERE → 3 行
        _, rows = e.q("SELECT COUNT(*) FROM users u LEFT JOIN orders o "
                      "ON o.id = o.id WHERE o.user_id = u.id")
        assert rows == [(3,)], rows
        # LEFT JOIN 未匹配补 NULL：users 3 行 + 1 个用户没订单
        _, rows = e.q("SELECT u.id, o.amount FROM users u LEFT JOIN orders o "
                      "ON o.user_id = u.id ORDER BY u.id, o.amount")
        assert rows == [(1, 10), (1, 20), (2, 30), (3, None)], rows


def test_join_on_constant_and_non_equi_conditions():
    """验收 6：常量 ON / 非等值 ON 按标准语义当连接后过滤条件求值

    `ON 1 = 1`（恒真）→ 9 行；`ON o.amount > 15` → 2 笔订单 × 3 个用户 = 6 行；
    `ON u.id = 5` → 没有任何连接行（过滤后 0 行）。
    `ON o.uid > 0` 这类非等值形态修复前是解析期 SyntaxError，现在按标准
    算对（仍是显式失败 → 算对的**改进**，不会静默）。
    """
    with _Env() as e:
        _, rows = e.q("SELECT COUNT(*) " + _ON_TAUTOLOGY.replace(
            "o.id = o.id", "1 = 1"))
        assert rows == [(9,)], rows
        _, rows = e.q("SELECT COUNT(*) FROM users u JOIN orders o "
                      "ON o.amount > 15")
        assert rows == [(6,)], rows
        _, rows = e.q("SELECT COUNT(*) FROM users u JOIN orders o "
                      "ON u.id = 5")
        assert rows == [(0,)], rows
        # 非等值 ON 的 LEFT JOIN：右侧全不匹配 → 每行左侧补 NULL（3 行）
        _, rows = e.q("SELECT COUNT(*) FROM users u LEFT JOIN orders o "
                      "ON o.amount > 1000")
        assert rows == [(3,)], rows
        # 非等值 ON + LEFT JOIN（有匹配）：9 行，无补 NULL
        _, rows = e.q("SELECT COUNT(*) FROM users u LEFT JOIN orders o "
                      "ON o.amount > 0")
        assert rows == [(9,)], rows


def test_join_on_unknown_column_errors():
    """ON / WHERE 里引用不存在的列 → **显式报错**，绝不静默给空集

    修复前 `_exec_HashJoin` 解析不到键就丢 ON，`_eval_condition` 解析不到列
    就 `return False`，两种都会静默返回空集；这里全部收紧成显式错误。
    """
    with _Env() as e:
        msg = _assert_err(e, "SELECT COUNT(*) FROM users u JOIN orders o "
                             "ON o.nosuch = u.id")
        assert 'nosuch' in msg, msg
        msg = _assert_err(e, "SELECT COUNT(*) FROM users u JOIN orders o "
                             "ON o.id = u.nosuch")
        assert 'nosuch' in msg, msg
        # 裸列名在连接结果里解析不到（不是字面量比较）→ 报错
        msg = _assert_err(e, "SELECT COUNT(*) FROM users u JOIN orders o "
                             "ON nosuch = 1")
        assert 'nosuch' in msg, msg
        # WHERE 里带限定符的未知列 → 报错（原来静默 0 行）
        msg = _assert_err(e, "SELECT COUNT(*) FROM users u JOIN orders o "
                             "ON o.user_id = u.id WHERE o.nosuch = u.id")
        assert 'nosuch' in msg, msg


def test_join_where_column_reference_comparison():
    """附加：WHERE 的**列引用**比较（`a = b` / `IN (b)` / `NOT (a = b)`）算对

    单表 `WHERE user_id = id` 修复前同样返回 0 行（同一类吞错）。
    """
    with _Env() as e:
        # 单表：orders(id, user_id) → user_id = id 只有第 1 行
        _, rows = e.q("SELECT COUNT(*) FROM orders WHERE user_id = id")
        assert rows == [(1,)], rows
        _, rows = e.q("SELECT COUNT(*) FROM orders WHERE id IN (user_id)")
        assert rows == [(1,)], rows
        _, rows = e.q("SELECT COUNT(*) FROM orders WHERE id = user_id")
        assert rows == [(1,)], rows
        # 连接后逐行比较：o.user_id 与 u.id 相等的连接行 3 行
        _, rows = e.q("SELECT COUNT(*) " + _ON_EQUI +
                      " WHERE o.user_id = u.id")
        assert rows == [(3,)], rows
        _, rows = e.q("SELECT COUNT(*) " + _ON_EQUI +
                      " WHERE o.user_id IN (u.id)")
        assert rows == [(3,)], rows
        _, rows = e.q("SELECT COUNT(*) " + _ON_EQUI +
                      " WHERE NOT (o.user_id = u.id)")
        assert rows == [(0,)], rows
        # 右侧带引号仍是字符串字面量（不是列引用）
        _, rows = e.q("SELECT COUNT(*) FROM users WHERE city = 'SH'")
        assert rows == [(1,)], rows
        # 单表自比较（恒真）→ 3 行
        _, rows = e.q("SELECT COUNT(*) FROM users WHERE city = city")
        assert rows == [(3,)], rows
        # UPDATE / DELETE 的条件里带列引用同样不能静默改 0 行
        # （orders 的 (1,1) 与新增的 (4,4) 满足 user_id = id → 2 行）
        e.ex.execute_sql("INSERT INTO orders VALUES (4,4,40)", {})
        r = e.ex.execute_sql("UPDATE orders SET amount = 0 "
                             "WHERE user_id = id", {})
        assert r.get('affected') == 2, r
        r = e.ex.execute_sql("DELETE FROM orders WHERE user_id = id", {})
        assert r.get('affected') == 2, r


def test_join_order_by_qualified_columns_multi_key():
    """验收 7：JOIN + 限定列名多键 ORDER BY + LIMIT（顺序正确）

    投影 `SELECT u.id, o.id` 的输出列名都被压成裸名 `id`，计划路径的 Sort
    只看得到 `['id','id']`：两个排序键都会落到第一列（第二键静默算错），
    限定名 `u.id` 还会被判成歧义 → 报错。这里改走「物化连接 + 统一排序」，
    按连接结果的限定列名逐键求值。
    """
    with _Env() as e:
        _, rows = e.q("SELECT u.id, o.id " + _ON_TAUTOLOGY +
                      " WHERE o.user_id = u.id ORDER BY u.id, o.id LIMIT 3")
        assert rows == [(1, 1), (1, 2), (2, 3)], rows
        # 第二键方向独立：o.id DESC
        _, rows = e.q("SELECT u.id, o.id " + _ON_TAUTOLOGY +
                      " WHERE o.user_id = u.id ORDER BY u.id ASC, o.id DESC")
        assert rows == [(1, 2), (1, 1), (2, 3)], rows
        # 计划路径（等值 ON，投影里没有重名）仍然正确
        _, rows = e.q("SELECT u.id, o.amount " + _ON_EQUI +
                      " ORDER BY o.amount DESC, u.id ASC LIMIT 2")
        assert rows == [(2, 30), (1, 20)], rows


# ==========================================================================
# MySQL 兼容面：复制状态 / 审计 / 表检查 / 引擎状态 / DCL 口令策略 / JSON 路径
#
# 本节的 6 个用例覆盖本次新增的 6 项语句。全部走**单实例**
# `Executor.execute(p, {})`（不启服务端线程、不碰 pymysql），因此可稳定重复；
# pymysql 入口的等价验证在既有用例里（tests/test_batch8.py::
# test_master_slave_status、tests/test_batch14.py::test_audit、
# tests/test_batch15.py 的 test_check_optimize / test_show_engine /
# test_security_via_sql / test_full_sql_suite）。
# ==========================================================================


def _exec(e, sql):
    """执行一条语句并返回执行器结果字典（不要求是结果集）"""
    p = e.parser.parse(sql)
    assert p is not None, f'解析失败: {sql}'
    return e.ex.execute(p, {})


def test_show_master_status_reports_real_wal_position():
    """SHOW MASTER STATUS / SHOW SLAVE STATUS / __BINLOG_FETCH__

    位点来自**真实 WAL 记录**（本引擎没有独立 binlog 文件），
    不是硬编码常量：新增写入后位点必须继续增长。
    """
    with _Env() as e:
        r = _exec(e, 'SHOW MASTER STATUS')
        assert r['type'] == 'resultset', r
        assert r['columns'] == ['File', 'Position', 'Binlog_Do_DB',
                                'Binlog_Ignore_DB', 'Executed_Gtid_Set']
        row = r['rows'][0]
        assert row[0] == 'binlog.000001'
        # _Env 建了 8(emp)+3(users)+3(orders) = 14 条真实 INSERT
        assert row[1] >= 14, row
        assert row[4] is None            # GTID 无对应概念 -> NULL

        ev = _exec(e, 'SELECT * FROM __BINLOG_FETCH__ WHERE pos >= 1 LIMIT 100')
        assert ev['type'] == 'resultset', ev
        assert [c for c in ev['columns']] == [
            'pos', 'lsn', 'txn_id', 'event_type', 'table_name', 'data']
        assert len(ev['rows']) == row[1]
        assert ev['rows'][0][3] == 'INSERT'

        _exec(e, "INSERT INTO emp VALUES (99, 'Zoe', 'Eng', 70)")
        pos2 = _exec(e, 'SHOW MASTER STATUS')['rows'][0][1]
        assert pos2 == row[1] + 1, (row[1], pos2)

        r = _exec(e, 'SHOW SLAVE STATUS')
        assert r['type'] == 'resultset'
        # MySQL 原生列序：第 1 列 Slave_IO_State，运行标志在第 11/12 列
        assert r['columns'][:12] == [
            'Slave_IO_State', 'Master_Host', 'Master_User', 'Master_Port',
            'Connect_Retry', 'Master_Log_File', 'Read_Master_Log_Pos',
            'Relay_Log_File', 'Relay_Log_Pos', 'Relay_Master_Log_File',
            'Slave_IO_Running', 'Slave_SQL_Running'], r['columns']
        # 真实的运行标志（MySQL 文本语义）：本实例没有配置复制 -> 'No'
        assert r['rows'][0][10] == 'No' and r['rows'][0][11] == 'No'
        assert r['rows'][0][0] == 'No replication configured'   # 真实说明文本
        assert 'Seconds_Behind_Master' in r['columns']


def test_show_audit_log_records_real_statements():
    """SHOW AUDIT LOG：真实语句审计（时间/用户/命令/成败/耗时/SQL 原文）"""
    with _Env() as e:
        r = _exec(e, 'SHOW AUDIT LOG')
        assert r['type'] == 'resultset', r
        assert r['columns'] == ['ts', 'user', 'cmd', 'success',
                                'elapsed_ms', 'sql']
        cmds = [x[2] for x in r['rows']]
        assert 'CREATE TABLE' in cmds and 'INSERT' in cmds, cmds
        # SQL 原文真的记下来了（不是命令名占位）
        assert any(str(x[5]).startswith('CREATE TABLE emp')
                   for x in r['rows'])
        # 失败的 DML 也记录（success=0），不静默丢
        bad = _exec(e, 'INSERT INTO no_such_table VALUES (1)')
        assert bad['type'] == 'error'
        last = _exec(e, 'SHOW AUDIT LOG')['rows'][-1]
        assert last[3] == 0 and 'no_such_table' in last[5], last
        assert len(_exec(e, 'SHOW AUDIT LOG LIMIT 2')['rows']) == 2


def test_check_and_optimize_table_verify_real_storage():
    """CHECK TABLE 真去逐个主键定位堆行；OPTIMIZE TABLE 真回收+刷盘"""
    with _Env() as e:
        r = _exec(e, 'CHECK TABLE emp')
        assert r['type'] == 'resultset', r
        # MySQL 原生列序 (Table, Op, Msg_type, Msg_text)
        assert r['columns'] == ['Table', 'Op', 'Msg_type', 'Msg_text']
        assert r['rows'][0] == ('emp', 'check', 'status', 'OK'), r['rows']
        # 第二行是真实校验统计（索引项数来自真的走了一遍索引）
        assert r['rows'][1][1] == 'check' and r['rows'][1][2] == 'info'
        assert '8' in r['rows'][1][3], r['rows']

        r = _exec(e, 'CHECK TABLE no_such_table')
        assert r['rows'][0][1] == 'check' and r['rows'][0][2] == 'error'
        assert 'exist' in r['rows'][0][3]

        r = _exec(e, 'OPTIMIZE TABLE emp')
        assert r['rows'][0] == ('emp', 'optimize', 'status', 'OK'), r['rows']
        assert r['rows'][1][1] == 'optimize' and r['rows'][1][2] == 'info'
        _, rows = e.q('SELECT COUNT(*) FROM emp')
        assert rows[0][0] == 8            # 优化后数据仍在
        r = _exec(e, 'OPTIMIZE TABLE no_such_table')
        assert r['rows'][0][1] == 'optimize' and r['rows'][0][2] == 'error'


def test_show_engine_innodb_status_reports_real_stats():
    """SHOW ENGINE INNODB：真实表/页/BTree/WAL 统计；不支持的形态显式报错"""
    with _Env() as e:
        r = _exec(e, 'SHOW ENGINE INNODB')
        assert r['type'] == 'resultset', r
        assert r['columns'] == ['Type', 'Name', 'Status']
        assert r['rows'][0][0] == 'InnoDB' and r['rows'][0][1] == ''
        text = r['rows'][0][2]
        assert 'TABLE emp' in text and 'rows=8' in text, text
        assert 'Log records (data events):' in text
        assert 'btree_height=' in text

        for sql in ('SHOW ENGINE INNODB MUTEX', 'SHOW ENGINE MYISAM'):
            r = _exec(e, sql)
            assert r['type'] == 'error', (sql, r)
            assert '不支持' in r['message']


def test_create_user_password_policy_rejects_weak():
    """CREATE USER 弱口令必须被真的拒绝，强口令落地为哈希；策略可调"""
    with _Env() as e:
        r = _exec(e, "CREATE USER 'weak' IDENTIFIED BY '123'")
        assert r['type'] == 'error', r
        assert r['code'] == 1819, r
        assert 'policy' in r['message'] and '长度' in r['message'], r
        assert 'weak' not in e.db.users          # 拒绝时不产生用户

        r = _exec(e, "CREATE USER 'strong' IDENTIFIED BY 'Strong123'")
        assert r['type'] == 'ok', r
        h = e.db.users['strong']['auth_hash']
        from lightdb.db.database import hash_native_password
        assert h == hash_native_password('Strong123')
        assert len(h) == 40 and 'Strong123' not in h    # 只存哈希，不存明文

        # 策略真的可改，并且立刻影响后续 CREATE USER
        assert _exec(e, 'SET PASSWORD POLICY min_length = 12')['type'] == 'ok'
        r = _exec(e, "CREATE USER 'mid' IDENTIFIED BY 'Strong123'")
        assert r['type'] == 'error' and r['code'] == 1819, r
        assert '12' in r['message'], r
        assert _exec(
            e, "CREATE USER 'mid' IDENTIFIED BY 'Str0ngPassw0rd'")['type'] \
            == 'ok'
        assert 'mid' in e.db.users

        # 重复创建 / DROP USER
        r = _exec(e, "CREATE USER 'strong' IDENTIFIED BY 'Other123'")
        assert r['type'] == 'error' and r['code'] == 1396, r
        assert _exec(e, "DROP USER 'strong'")['type'] == 'ok'
        assert 'strong' not in e.db.users

        # SHOW SECURITY 反映真实策略
        vals = dict(_exec(e, 'SHOW SECURITY')['rows'])
        assert vals['password_policy.min_length'] == '12'
        assert vals['authentication_plugin'] == 'mysql_native_password'


def test_json_path_operator_extract_null_and_errors():
    """`col->'$.k'` / `col->>'$.k'`：投影与 WHERE 都可用，缺键 -> NULL"""
    with _Env() as e:
        assert _exec(
            e, 'CREATE TABLE docs (id INT PRIMARY KEY, data JSON)')['type'] \
            == 'ok'
        _exec(e, """INSERT INTO docs VALUES (1, '{"k": "v", "n": 3}')""")
        _exec(e, """INSERT INTO docs VALUES (2, '{"k": "w"}')""")
        _exec(e, """INSERT INTO docs VALUES (3, '{"k": null}')""")

        # 投影：-> 取 JSON 值，->> 转文本
        _, rows = e.q("SELECT id, data->'$.k' AS k FROM docs ORDER BY id")
        assert [r[1] for r in rows] == ['v', 'w', None], rows
        _, rows = e.q("SELECT id, data->>'$.n' AS n FROM docs ORDER BY id")
        assert rows[0][1] == '3', rows

        # WHERE：命中 1 行；键不存在 -> NULL -> 不命中（标准三值语义）
        _, rows = e.q("SELECT id FROM docs WHERE data->'$.k' = 'v'")
        assert rows == [(1,)], rows
        _, rows = e.q("SELECT id FROM docs WHERE data->'$.missing' = 'v'")
        assert rows == [], rows
        _, rows = e.q("SELECT id, data->'$.missing' AS m FROM docs "
                      "WHERE id = 2")
        assert rows == [(2, None)], rows

        # 非法形态显式报错（不静默当字符串/空集）
        for bad in ("SELECT * FROM docs WHERE data->'no-dollar' = 'v'",
                    'SELECT * FROM docs WHERE data->$.k = 1',
                    "SELECT MATCH(data) AGAINST('v') FROM docs"):
            try:
                e.parser.parse(bad)
            except SyntaxError:
                continue
            raise AssertionError(f'应当显式报语法错误: {bad}')


# ==========================================================================
# MySQL 原生列序 / 列名加锁（追加于 2026-10：本引擎曾为迁就既有断言而刻意
# 偏离 MySQL —— ①SHOW SLAVE STATUS 把运行标志挪到第 1、2 列；②CHECK TABLE
# 写成 (Table, Msg_text, Msg_type, Op)；③`data->'$.k'` 显示改写后的
# JSON_EXTRACT(...)。三处已按 MySQL 语义恢复，这里把真实列序/列名锁死，
# 避免将来又被改回去。
# ==========================================================================

#: MySQL 5.7 Reference Manual 13.7.5.34「SHOW SLAVE STATUS Statement」`\G`
#: 输出的完整列清单（57 列）。MySQL 8.0 只在其后追加 Master_public_key_path /
#: Get_master_public_key / Network_Namespace，前 57 列名称与顺序完全相同。
SHOW_SLAVE_STATUS_COLUMNS = [
    'Slave_IO_State', 'Master_Host', 'Master_User', 'Master_Port',
    'Connect_Retry', 'Master_Log_File', 'Read_Master_Log_Pos',
    'Relay_Log_File', 'Relay_Log_Pos', 'Relay_Master_Log_File',
    'Slave_IO_Running', 'Slave_SQL_Running',
    'Replicate_Do_DB', 'Replicate_Ignore_DB',
    'Replicate_Do_Table', 'Replicate_Ignore_Table',
    'Replicate_Wild_Do_Table', 'Replicate_Wild_Ignore_Table',
    'Last_Errno', 'Last_Error', 'Skip_Counter', 'Exec_Master_Log_Pos',
    'Relay_Log_Space', 'Until_Condition', 'Until_Log_File', 'Until_Log_Pos',
    'Master_SSL_Allowed', 'Master_SSL_CA_File', 'Master_SSL_CA_Path',
    'Master_SSL_Cert', 'Master_SSL_Cipher', 'Master_SSL_Key',
    'Seconds_Behind_Master', 'Master_SSL_Verify_Server_Cert',
    'Last_IO_Errno', 'Last_IO_Error', 'Last_SQL_Errno', 'Last_SQL_Error',
    'Replicate_Ignore_Server_Ids', 'Master_Server_Id', 'Master_UUID',
    'Master_Info_File', 'SQL_Delay', 'SQL_Remaining_Delay',
    'Slave_SQL_Running_State', 'Master_Retry_Count', 'Master_Bind',
    'Last_IO_Error_Timestamp', 'Last_SQL_Error_Timestamp',
    'Master_SSL_Crl', 'Master_SSL_Crlpath', 'Retrieved_Gtid_Set',
    'Executed_Gtid_Set', 'Auto_Position', 'Replicate_Rewrite_DB',
    'Channel_name', 'Master_TLS_Version',
]


def test_show_slave_status_uses_mysql_native_column_order():
    """SHOW SLAVE STATUS 必须按 MySQL 原生列序输出（锁定，不许挪列）"""
    with _Env() as e:
        r = _exec(e, 'SHOW SLAVE STATUS')
        assert r['type'] == 'resultset', r
        assert r['columns'] == SHOW_SLAVE_STATUS_COLUMNS, r['columns']
        assert len(r['rows']) == 1
        row = r['rows'][0]
        vals = dict(zip(r['columns'], row))
        # 两个运行标志是真值（MySQL 文本语义 'No'），且必须在 MySQL 的位置上
        assert r['columns'][10] == 'Slave_IO_Running' and row[10] == 'No'
        assert r['columns'][11] == 'Slave_SQL_Running' and row[11] == 'No'
        # 第 1 列是真实的状态说明文本：本实例没有配置复制
        assert r['columns'][0] == 'Slave_IO_State' and \
            row[0] == 'No replication configured'
        # 没有对应概念的列一律 NULL，绝不编造
        for col in ('Master_Host', 'Master_Port', 'Connect_Retry',
                    'Master_Log_File', 'Read_Master_Log_Pos',
                    'Relay_Log_File', 'Relay_Log_Pos',
                    'Relay_Master_Log_File', 'Exec_Master_Log_Pos',
                    'Seconds_Behind_Master', 'Master_Server_Id',
                    'Master_Info_File', 'Master_Retry_Count',
                    'SQL_Remaining_Delay', 'Slave_SQL_Running_State',
                    'Retrieved_Gtid_Set', 'Executed_Gtid_Set'):
            assert vals[col] is None, (col, vals[col])
        # 「错误号 0 + 空串 = 没有错误」（文档语义）
        assert vals['Last_Errno'] == 0 and vals['Last_Error'] == ''
        assert vals['Last_IO_Errno'] == 0 and vals['Last_IO_Error'] == ''
        assert vals['Last_SQL_Errno'] == 0 and vals['Last_SQL_Error'] == ''
        assert vals['Until_Condition'] == 'None'
        assert vals['Auto_Position'] == 0

        # SHOW REPLICA STATUS 是同一份结果（MySQL 8.0 同列名同顺序）
        r2 = _exec(e, 'SHOW REPLICA STATUS')
        assert r2['columns'] == SHOW_SLAVE_STATUS_COLUMNS
        assert r2['rows'] == r['rows']


def test_check_optimize_table_use_mysql_native_columns():
    """CHECK/OPTIMIZE TABLE 必须是 (Table, Op, Msg_type, Msg_text)"""
    with _Env() as e:
        r = _exec(e, 'CHECK TABLE emp')
        assert r['columns'] == ['Table', 'Op', 'Msg_type', 'Msg_text']
        assert r['rows'][0] == ('emp', 'check', 'status', 'OK'), r['rows']
        assert r['rows'][1][1] == 'check' and r['rows'][1][2] == 'info'
        assert r['rows'][1][3] and r['rows'][1][3] != 'OK'   # 人可读统计文本

        r = _exec(e, 'CHECK TABLE no_such_table')
        assert r['columns'] == ['Table', 'Op', 'Msg_type', 'Msg_text']
        assert r['rows'][0] == ('no_such_table', 'check', 'error',
                                "Table 'no_such_table' doesn't exist")

        r = _exec(e, 'OPTIMIZE TABLE emp')
        assert r['columns'] == ['Table', 'Op', 'Msg_type', 'Msg_text']
        assert r['rows'][0] == ('emp', 'optimize', 'status', 'OK'), r['rows']
        assert r['rows'][1][1] == 'optimize' and r['rows'][1][2] == 'info'

        r = _exec(e, 'OPTIMIZE TABLE no_such_table')
        assert r['rows'][0][1] == 'optimize' and r['rows'][0][2] == 'error'
        assert r['rows'][0][3] == "Table 'no_such_table' doesn't exist"


def test_json_arrow_projection_column_name_is_source_text():
    """`data->'$.k'` / `->>` 无别名投影的输出列名 = 用户写的**原文**"""
    with _Env() as e:
        assert _exec(e, 'CREATE TABLE docs (id INT PRIMARY KEY, data JSON)')[
            'type'] == 'ok'
        _exec(e, """INSERT INTO docs VALUES (1, '{"k": "v"}')""")

        cols, rows = e.q("SELECT id, data->'$.k' FROM docs")
        assert cols == ['id', "data->'$.k'"], cols
        assert rows == [(1, 'v')], rows

        cols, _ = e.q("SELECT data->>'$.k' FROM docs")
        assert cols == ["data->>'$.k'"], cols

        # 别名优先（MySQL 同）：列名是别名，不是原文
        cols, _ = e.q("SELECT data->'$.k' AS k FROM docs")
        assert cols == ['k'], cols

        # 手写 JSON_EXTRACT(...) 的列名保持手写形态（不被反改成 -> ）
        cols, _ = e.q("SELECT JSON_EXTRACT(data, '$.k') FROM docs")
        assert cols == ["JSON_EXTRACT(data, '$.k')"], cols

        # 两项同形时逐项定位：第一项显示原文、第二项保持手写形态
        # （不能靠「改写结果 -> 原文」的文本字典命中两项）
        cols, vals = e.q(
            "SELECT data->'$.k', JSON_EXTRACT(data, '$.k') FROM docs")
        assert cols == ["data->'$.k'", "JSON_EXTRACT(data, '$.k')"], cols
        assert vals == [('v', 'v')], vals

        # ORDER BY 仍按表达式求值（列名改了不影响排序语义）
        _exec(e, """INSERT INTO docs VALUES (2, '{"k": "a"}')""")
        cols, rows = e.q("SELECT id, data->'$.k' FROM docs "
                         "ORDER BY data->'$.k'")
        assert cols == ['id', "data->'$.k'"]
        assert rows == [(2, 'a'), (1, 'v')], rows
        _, rows = e.q("SELECT id, data->'$.k' FROM docs "
                      "ORDER BY data->'$.k' DESC")
        assert rows == [(1, 'v'), (2, 'a')], rows


# ==========================================================================
# SHOW SLAVE STATUS 的语义为真（追加）：上一轮为了迁就断言把两个运行标志留成
# 整数 0/1、状态文本恒 NULL，根因是**执行器看不到本进程的 SlaveReplicator**。
# 本轮复制器在构造时注册到 Database（`db/database.py::attach_replicator`），
# 执行器经 `Database.replication_status()` 读它自报的真实快照。这条用例锁住：
#   ① 未配置复制 -> 'No'/'No' + 'No replication configured' + 连接位点类 NULL；
#   ② 登记复制器后 -> Master_Host/Port/User 取自真实配置，位点/错误/状态文本
#      全部来自复制器真实跟踪的值；
#   ③ 主库写入后，从库侧位点**真的前进**（Exec_Master_Log_Pos 4 -> 10）。
#
# 说明（诚实披露，不含糊）：本用例所在的文件按设计是"单实例、不起服务端线程"，
# 因此第 ③ 步的**主库侧事件**取自同进程内真实主库 Database 的
# `__BINLOG_FETCH__`（真实 WAL 记录），再交给复制器真实的 SQL 线程重放；
# 网络拉取那一段（io 线程 + pymysql + MySQLServer）由
# `tests/test_batch8.py::test_slave_status_reports_real_replication` 用真实 TCP
# 端到端覆盖。这里 io 线程连的是一个**确实没有监听者**的端口，所以它如实报
# 'Connecting' + 真实 errno —— 这本身就验证了错误类字段不是编造的。
# ==========================================================================


def test_show_slave_status_reflects_registered_replicator():
    """执行器必须读到本进程真实复制器的状态（未配置 / 已配置 / 位点前进）"""
    import time

    with _Env() as e:
        from lightdb.replication.slave import SlaveReplicator
        from lightdb.sql.executor import Executor
        from lightdb.db.database import Database

        def status_vals():
            r = _exec(e, 'SHOW SLAVE STATUS')
            assert r['type'] == 'resultset', r
            assert r['columns'] == SHOW_SLAVE_STATUS_COLUMNS
            return dict(zip(r['columns'], r['rows'][0]))

        def wait_until(pred, timeout=20.0, interval=0.1):
            end = time.time() + timeout
            while time.time() < end:
                if pred():
                    return True
                time.sleep(interval)
            return False

        # ---------- ① 未配置复制：真实状态就是"没在跑" ----------
        vals = status_vals()
        assert vals['Slave_IO_State'] == 'No replication configured'
        assert vals['Slave_IO_Running'] == 'No'
        assert vals['Slave_SQL_Running'] == 'No'
        for col in ('Master_Host', 'Master_User', 'Master_Port', 'Connect_Retry',
                    'Master_Log_File', 'Read_Master_Log_Pos', 'Relay_Log_File',
                    'Relay_Log_Pos', 'Relay_Master_Log_File',
                    'Exec_Master_Log_Pos', 'Relay_Log_Space',
                    'Seconds_Behind_Master', 'Slave_SQL_Running_State'):
            assert vals[col] is None, (col, vals[col])

        # ---------- ② 登记真实复制器（端口 1 确实没有监听者）----------
        rep = SlaveReplicator(e.db, '127.0.0.1', 1, user='repl')
        try:
            vals = status_vals()
            assert vals['Master_Host'] == '127.0.0.1'
            assert vals['Master_Port'] == 1
            assert vals['Master_User'] == 'repl'
            assert vals['Connect_Retry'] == 1          # io 线程真实重连间隔
            assert vals['Slave_IO_Running'] == 'No'    # 线程还没起来
            assert vals['Slave_SQL_Running'] == 'No'
            assert vals['Slave_IO_State'] == \
                'Replication configured but not started'
            # 还没连过主库 -> 位点未知，必须是 NULL（不是编造的 0）
            assert vals['Master_Log_File'] is None
            assert vals['Read_Master_Log_Pos'] is None
            assert vals['Exec_Master_Log_Pos'] is None
            assert vals['Seconds_Behind_Master'] is None

            # ---------- ③ 启动后：真实线程状态 + 真实连接错误 ----------
            rep.start()
            assert wait_until(lambda: status_vals()['Last_IO_Errno'] != 0)
            vals = status_vals()
            assert vals['Slave_IO_Running'] == 'Connecting', vals
            assert vals['Slave_SQL_Running'] == 'Yes'
            assert vals['Last_IO_Errno'] == 2003, vals   # 连不上主库的真实 errno
            assert vals['Last_IO_Error'], vals
            assert vals['Last_IO_Error_Timestamp'], vals
            assert vals['Seconds_Behind_Master'] is None   # 主库位点未知

            # ---------- ④ 主库写入 -> 从库位点真的前进 ----------
            master_dir = os.path.join(ROOT, 'data_slave_status_master_test')
            shutil.rmtree(master_dir, ignore_errors=True)
            master_db = Database(master_dir)
            master_ex = Executor(master_db)
            try:
                master_ex.execute_sql(
                    'CREATE TABLE repl_t (id INT PRIMARY KEY, v VARCHAR(20))', {})
                _exec(e, 'CREATE TABLE repl_t (id INT PRIMARY KEY, '
                         'v VARCHAR(20))')
                for i in range(1, 5):
                    master_ex.execute_sql(
                        f"INSERT INTO repl_t VALUES ({i}, 'v{i}')", {})

                def feed(master_ex, after_pos):
                    """把主库真实事件交给复制器的中继日志（真实 SQL 线程重放）"""
                    r = master_ex.execute(
                        e.parser.parse(
                            'SELECT * FROM __BINLOG_FETCH__ '
                            f'WHERE pos > {after_pos} LIMIT 500'), {})
                    assert r['type'] == 'resultset', r
                    for row in r['rows']:
                        rep.relay.append(row)
                    return len(r['rows'])

                assert feed(master_ex, 0) == 4
                assert wait_until(
                    lambda: status_vals()['Exec_Master_Log_Pos'] == 4), \
                    status_vals()
                vals = status_vals()
                before_pos = (vals['Read_Master_Log_Pos'],
                              vals['Exec_Master_Log_Pos'],
                              vals['Relay_Log_Pos'])
                assert vals['Exec_Master_Log_Pos'] == 4
                assert vals['Relay_Log_File'] == 'relay.log'
                assert vals['Relay_Log_Pos'] > 0
                assert e.db.get_table('repl_t').row_count == 4

                for i in range(5, 11):
                    master_ex.execute_sql(
                        f"INSERT INTO repl_t VALUES ({i}, 'v{i}')", {})
                assert feed(master_ex, 4) == 6
                assert wait_until(
                    lambda: status_vals()['Exec_Master_Log_Pos'] == 10), \
                    status_vals()
                vals = status_vals()
                assert vals['Exec_Master_Log_Pos'] == 10
                assert e.db.get_table('repl_t').row_count == 10
                # 位点与中继日志字节数都真的前进了（不是同一个常量）
                assert vals['Exec_Master_Log_Pos'] > before_pos[1]
                assert vals['Relay_Log_Pos'] > before_pos[2]
            finally:
                master_db.shutdown()
                shutil.rmtree(master_dir, ignore_errors=True)

            # ---------- ⑤ 停止后：运行标志回 'No'，配置与位点仍如实保留 ----------
            rep.stop()
            vals = status_vals()
            assert vals['Slave_IO_Running'] == 'No'
            assert vals['Slave_SQL_Running'] == 'No'
            assert vals['Slave_IO_State'] == 'Slave IO thread has been stopped'
            assert vals['Seconds_Behind_Master'] is None
            assert vals['Slave_SQL_Running_State'] is None   # 线程没在跑 -> NULL
            assert vals['Master_Host'] == '127.0.0.1'
            assert vals['Exec_Master_Log_Pos'] == 10
        finally:
            rep.stop()
            e.db.detach_replicator(rep)

        # 注销后回到真实的"未配置复制"
        vals = status_vals()
        assert vals['Slave_IO_State'] == 'No replication configured'
        assert vals['Master_Host'] is None
        assert vals['Slave_IO_Running'] == 'No'


# ==========================================================================
# 存储函数（CREATE FUNCTION / DROP FUNCTION / SHOW FUNCTIONS）—— 追加
# ==========================================================================
#
# 覆盖：简式 `RETURN <expr>` / 复合式 `BEGIN ... END`（DECLARE + IF/ELSE +
# SET）、投影 / WHERE / HAVING / ORDER BY 四个调用位置、函数调用函数、
# 函数体内用标量函数、重启（关闭 Database 后重新打开同一目录）后仍可调用、
# 参数个数不匹配与调用不存在的函数的**显式报错**。
#
# 断言只依赖真实执行结果（不 mock）；这份回归一旦失败就说明存储函数真的
# 坏了，而不是"某条消息文案变了"。

STORED_FN_DIR = os.path.join(ROOT, 'data_sql_features_fn_test')


class _FnEnv:
    """独立数据目录的环境：`reopen()` 用于验证**持久化**"""

    def __enter__(self):
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.sql.parser import parser

        shutil.rmtree(STORED_FN_DIR, ignore_errors=True)
        self.db = Database(STORED_FN_DIR)
        self.ex = Executor(self.db)
        self.parser = parser
        self.ex.execute_sql(
            "CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20), a INT)", {})
        for i, (n, a) in enumerate(
                [('Alice', 1), ('bob', 4), ('Carol', 3), ('dave', 8)], start=1):
            self.ex.execute_sql(
                "INSERT INTO t VALUES (%d,'%s',%d)" % (i, n, a), {})
        return self

    def reopen(self):
        """关闭 Database 再用**同一目录**打开（持久化验证）"""
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor

        self.db.shutdown()
        self.db = Database(STORED_FN_DIR)
        self.ex = Executor(self.db)
        return self

    def __exit__(self, *exc):
        try:
            if self.db is not None:
                self.db.shutdown()
        except Exception:
            pass
        shutil.rmtree(STORED_FN_DIR, ignore_errors=True)
        return False

    def run(self, sql):
        return self.ex.execute_sql(sql, {})

    def ok(self, sql):
        r = self.run(sql)
        assert r.get('type') == 'ok', \
            f'语句应成功: {sql} -> {r.get("message")}'
        return r

    def err(self, sql):
        r = self.run(sql)
        assert r.get('type') == 'error', \
            f'语句应显式报错（绝不静默返回 NULL/0）: {sql} -> {r}'
        return str(r.get('message') or '')

    def q(self, sql):
        r = self.run(sql)
        assert r.get('type') == 'resultset', \
            f'查询失败: {sql} -> {r.get("message")}'
        return r['columns'], [tuple(x) for x in r['rows']]


def test_stored_function_simple_form():
    """简式 `CREATE FUNCTION f(x INT) RETURNS INT RETURN <expr>`"""
    with _FnEnv() as e:
        r = e.ok("CREATE FUNCTION double_it(x INT) RETURNS INT RETURN x * 2")
        assert 'double_it' in r['message']
        # 投影
        cols, rows = e.q("SELECT id, double_it(a) AS d FROM t")
        assert cols == ['id', 'd']
        assert rows == [(1, 2), (2, 8), (3, 6), (4, 16)]
        # WHERE（表达式谓词位置）
        assert e.q("SELECT id FROM t WHERE double_it(a) = 6")[1] == [(3,)]
        # ORDER BY
        assert e.q("SELECT id FROM t ORDER BY double_it(a) DESC")[1] == \
            [(4,), (2,), (3,), (1,)]
        # 函数名不区分大小写（MySQL 语义）
        assert e.q("SELECT DOUBLE_IT(a) FROM t ORDER BY id")[1] == \
            [(2,), (8,), (6,), (16,)]


def test_stored_function_compound_form():
    """复合式 `BEGIN DECLARE/IF/ELSE/SET ... RETURN ... END`"""
    with _FnEnv() as e:
        e.ok("""CREATE FUNCTION grade(a INT) RETURNS VARCHAR(10)
        BEGIN
            DECLARE g VARCHAR(10) DEFAULT 'low';
            IF a >= 5 THEN
                SET g = 'high';
            ELSE
                SET g = 'low';
            END IF;
            RETURN g;
        END""")
        assert e.q("SELECT id, grade(a) AS g FROM t")[1] == \
            [(1, 'low'), (2, 'low'), (3, 'low'), (4, 'high')]
        # 复合式函数同样能在 WHERE 里用
        assert e.q("SELECT id FROM t WHERE grade(a) = 'high'")[1] == [(4,)]
        # HAVING（函数套聚合）：真过滤，真命中
        e.ok("CREATE FUNCTION times3(n INT) RETURNS INT RETURN n * 3")
        assert e.q("SELECT a FROM t GROUP BY a HAVING times3(COUNT(*)) >= 3"
                   )[1] == [(1,), (4,), (3,), (8,)]
        assert e.q("SELECT a FROM t GROUP BY a HAVING times3(COUNT(*)) > 3"
                   )[1] == []
        e.ok("INSERT INTO t VALUES (5, 'eve', 1)")
        assert e.q("SELECT a, COUNT(*) FROM t GROUP BY a "
                   "HAVING times3(COUNT(*)) >= 6")[1] == [(1, 2)]


def test_stored_function_nested_and_scalar():
    """函数调用另一个函数；函数体内用已实现的标量函数"""
    with _FnEnv() as e:
        e.ok("CREATE FUNCTION double_it(x INT) RETURNS INT RETURN x * 2")
        e.ok("CREATE FUNCTION quad(x INT) RETURNS INT "
             "RETURN double_it(double_it(x))")
        assert e.q("SELECT id, quad(a) AS q FROM t")[1] == \
            [(1, 4), (2, 16), (3, 12), (4, 32)]
        # 存储函数套标量函数 + 标量函数套存储函数（双向都通）
        assert e.q("SELECT id, CONCAT(double_it(a), '|', LOWER(name)) AS c "
                   "FROM t ORDER BY id")[1] == \
            [(1, '2|alice'), (2, '8|bob'), (3, '6|carol'), (4, '16|dave')]
        # 函数体内部用标量函数
        e.ok("""CREATE FUNCTION shout(x VARCHAR(20)) RETURNS VARCHAR(40)
        BEGIN
            DECLARE s VARCHAR(40);
            SET s = CONCAT(LOWER(x), '-', UPPER(x));
            RETURN s;
        END""")
        assert e.q("SELECT shout(name) FROM t ORDER BY id")[1] == \
            [('alice-ALICE',), ('bob-BOB',), ('carol-CAROL',), ('dave-DAVE',)]


def test_stored_function_in_join_query():
    """JOIN 查询（计划执行器路径）的投影/WHERE/HAVING 也能调用存储函数"""
    with _FnEnv() as e:
        e.ok("CREATE TABLE jo (id INT PRIMARY KEY, uid INT, amt INT)")
        for i, (uid, amt) in enumerate([(1, 5), (1, 7), (2, 3)], start=1):
            e.ok("INSERT INTO jo VALUES (%d,%d,%d)" % (i, uid, amt))
        e.ok("CREATE FUNCTION dbl(x INT) RETURNS INT RETURN x * 2")
        # 投影（计划执行器的逐行表达式求值路径）
        assert e.q("SELECT t.id, dbl(jo.amt) AS d FROM t "
                   "JOIN jo ON t.id = jo.uid ORDER BY t.id, d")[1] == \
            [(1, 10), (1, 14), (2, 6)]
        # WHERE
        assert e.q("SELECT t.id FROM t JOIN jo ON t.id = jo.uid "
                   "WHERE dbl(jo.amt) > 10")[1] == [(1,)]
        # GROUP BY + 聚合 + HAVING
        assert e.q("SELECT t.id, SUM(dbl(jo.amt)) AS s FROM t "
                   "JOIN jo ON t.id = jo.uid GROUP BY t.id ORDER BY t.id"
                   )[1] == [(1, 24), (2, 6)]


def test_stored_function_persists_after_reopen():
    """函数定义随元数据持久化：关闭 Database、重开同一目录后仍可调用"""
    with _FnEnv() as e:
        e.ok("CREATE FUNCTION double_it(x INT) RETURNS INT RETURN x * 2")
        e.ok("CREATE FUNCTION grade(a INT) RETURNS VARCHAR(10) "
             "BEGIN DECLARE g VARCHAR(10) DEFAULT 'x'; "
             "IF a >= 5 THEN SET g = 'high'; END IF; RETURN g; END")
        e.reopen()
        assert e.q("SELECT id, double_it(a) AS d, grade(a) AS g FROM t")[1] == \
            [(1, 2, 'x'), (2, 8, 'x'), (3, 6, 'x'), (4, 16, 'high')]
        assert e.q("SELECT id FROM t WHERE double_it(a) >= 8 ORDER BY id"
                   )[1] == [(2,), (4,)]
        names = [r[0] for r in e.q("SHOW FUNCTIONS")[1]]
        assert 'double_it' in names and 'grade' in names


def test_stored_function_errors_are_explicit():
    """函数相关错误全部显式：参数个数 / 不存在 / 函数体不支持 / 类型 / OUT"""
    with _FnEnv() as e:
        e.ok("CREATE FUNCTION double_it(x INT) RETURNS INT RETURN x * 2")

        # 1) 参数个数不匹配
        msg = e.err("SELECT double_it(1, 2) FROM t")
        assert '参数个数' in msg and '需要 1' in msg and '实际给了 2' in msg

        # 2) 调用不存在的函数（不被当成列名、不返回 NULL）
        msg = e.err("SELECT nope_fn(a) FROM t")
        assert '不支持的函数' in msg and 'NOPE_FN' in msg

        # 3) 函数体含不支持的语句：游标
        msg = e.err("""CREATE FUNCTION bad_cur() RETURNS INT
        BEGIN
            DECLARE cur CURSOR FOR SELECT id FROM t;
            RETURN 1;
        END""")
        assert '函数体' in msg and 'bad_cur' in msg
        # 3b) 循环
        msg = e.err("""CREATE FUNCTION bad_loop(x INT) RETURNS INT
        BEGIN
            DECLARE i INT DEFAULT 0;
            WHILE i < x DO
                SET i = i + 1;
            END WHILE;
            RETURN i;
        END""")
        assert 'WHILE' in msg
        # 3c) 改数据的 SQL
        msg = e.err("""CREATE FUNCTION bad_ins(x INT) RETURNS INT
        BEGIN
            INSERT INTO t VALUES (99, 'x', x);
            RETURN x;
        END""")
        assert 'INSERT' in msg
        # 三个坏函数都没被注册
        names = [r[0] for r in e.q("SHOW FUNCTIONS")[1]]
        for bad in ('bad_cur', 'bad_loop', 'bad_ins'):
            assert bad not in names, names

        # 4) 不支持的 RETURNS 类型
        msg = e.err("CREATE FUNCTION bad_rt() RETURNS CURSOR RETURN 1")
        assert "RETURNS" in msg and 'CURSOR' in msg

        # 5) OUT/INOUT 参数显式拒绝（不是静默按 IN 处理）
        msg = e.err("CREATE FUNCTION bad_out(OUT x INT) RETURNS INT RETURN x")
        assert 'OUT' in msg and 'IN' in msg

        # 6) 递归过深（显式报错，不是栈溢出/静默 NULL）
        e.ok("CREATE FUNCTION rec(x INT) RETURNS INT RETURN rec(x + 1)")
        msg = e.err("SELECT rec(a) FROM t")
        assert '递归过深' in msg


def test_drop_function_and_if_exists():
    """`DROP FUNCTION` 与 `DROP FUNCTION IF EXISTS`"""
    with _FnEnv() as e:
        e.ok("CREATE FUNCTION double_it(x INT) RETURNS INT RETURN x * 2")
        e.ok("DROP FUNCTION double_it")
        assert 'double_it' not in [r[0] for r in e.q("SHOW FUNCTIONS")[1]]
        # 已删除后调用 → 显式报错（不是 NULL）
        assert '不支持的函数' in e.err("SELECT double_it(a) FROM t")
        # IF EXISTS：不存在也算成功
        e.ok("DROP FUNCTION IF EXISTS double_it")
        # 不带 IF EXISTS：不存在 → 显式报错
        msg = e.err("DROP FUNCTION double_it")
        assert 'does not exist' in msg or '不存在' in msg
        # IF NOT EXISTS 语义（MySQL 位置：CREATE FUNCTION IF NOT EXISTS ...）：
        # 已存在时不覆盖原定义
        e.ok("CREATE FUNCTION keep(x INT) RETURNS INT RETURN x + 1")
        r = e.run("CREATE FUNCTION IF NOT EXISTS keep(x INT) RETURNS INT "
                  "RETURN x + 100")
        assert r.get('type') == 'ok' and 'already exists' in r['message'], r
        assert e.q("SELECT keep(1) FROM t LIMIT 1")[1] == [(2,)]
        # 把 IF NOT EXISTS 写到 FUNCTION 之后是错误的语法 → 显式报语法错误
        try:
            e.parser.parse("CREATE FUNCTION keep IF NOT EXISTS (x INT) "
                           "RETURNS INT RETURN 1")
        except SyntaxError:
            pass
        else:
            raise AssertionError('IF NOT EXISTS 位置错误应显式报语法错误')

