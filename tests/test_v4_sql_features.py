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

        # ⑧ 非聚合标量子查询（取第一行）
        _, rows = e.q("SELECT id FROM users u WHERE (SELECT o.user_id FROM "
                      "orders o WHERE o.user_id = u.id) = 1")
        assert [r[0] for r in rows] == [1], rows

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
        #    注意：标量子查询目前只支持出现在 **WHERE/HAVING** 里，
        #    放进 SELECT 列表仍不支持（见文件末尾"已知限制"），
        #    因此这里用 WHERE 逐行判定，而不是把 COUNT 投影出来。
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
        #    注意：WHERE 里的算术谓词（`WHERE salary*2 > 230`）仍不支持，
        #    表达式只支持出现在投影列表里，见文件末尾"已知限制"
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
