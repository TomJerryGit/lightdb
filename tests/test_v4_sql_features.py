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

        # 不支持的相关形态必须明确报错（不能静默错答）
        r = e.ex.execute(e.parser.parse(
            "SELECT id FROM users u WHERE EXISTS (SELECT COUNT(*) FROM "
            "orders o WHERE o.user_id = u.id)"), {})
        assert r.get('type') == 'error', r
        assert '聚合' in r.get('message', ''), r


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
