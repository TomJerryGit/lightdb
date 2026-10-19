"""SQL 缺口回归：BETWEEN / COUNT(DISTINCT ...) / IN-NOT IN 的 NULL 边界

本文件锁住三类此前**不可用**（或会静默错答）的 SQL 能力：

* `expr [NOT] BETWEEN low AND high`：WHERE / HAVING / SELECT 投影 / JOIN ON
  四个位置；AND 优先级（`BETWEEN` 内部的 AND 不能被当连接词切开）；
  三值逻辑（NULL → UNKNOWN → WHERE 不命中）；闭区间；类型强制转换与
  **不可比较类型的显式报错**。
* `COUNT(DISTINCT expr)` / `COUNT(DISTINCT a, b)` / `SUM/AVG/MIN/MAX(
  DISTINCT x)`：忽略 NULL；GROUP BY / HAVING / 无 GROUP BY / JOIN 计划
  路径都覆盖。单表统一路径与计划路径此前表现不同（单表报「列不存在」，
  JOIN 路径**静默返回 0**）——后者是静默错答，这里专门钉住。
* `IN / NOT IN` 的 NULL 三值语义、`IN ()` 空列表语法错误、
  `1 IN ('1')` 的混合类型强制转换。

红线：要么算对，要么显式报错；本文件里任何「应报错」的用例都断言
`type == 'error'`（而不是返回 0 行 / NULL 冒充结果）。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data_sql_gaps_test')


class _Env:
    def __enter__(self):
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.sql.parser import parser

        shutil.rmtree(DATA_DIR, ignore_errors=True)
        self.db = Database(DATA_DIR)
        self.ex = Executor(self.db)
        self.parser = parser
        self.run("CREATE TABLE t (id INT PRIMARY KEY, g VARCHAR(10), "
                 "v INT, name VARCHAR(10))")
        self.run("CREATE TABLE pre (id INT PRIMARY KEY, a INT, b INT)")
        self.run("CREATE TABLE u (lo INT PRIMARY KEY, hi INT)")
        self.run("CREATE TABLE pair (id INT PRIMARY KEY, a INT, b INT)")
        for sql in [
            "INSERT INTO t VALUES (1,'a',1,'x')",
            "INSERT INTO t VALUES (2,'a',2,'y')",
            "INSERT INTO t VALUES (3,'b',2,'z')",
            "INSERT INTO t VALUES (4,'b',NULL,'w')",
            "INSERT INTO t VALUES (5,'c',3,'10')",
            "INSERT INTO t VALUES (6,'c',1,'abc')",
            "INSERT INTO t VALUES (7,'d',4,'q')",
            "INSERT INTO pre VALUES (1,1,3)",
            "INSERT INTO pre VALUES (2,2,3)",
            "INSERT INTO pre VALUES (3,2,4)",
            "INSERT INTO pre VALUES (4,3,3)",
            "INSERT INTO pre VALUES (5,1,5)",
            "INSERT INTO u VALUES (2,3)",
            "INSERT INTO u VALUES (4,4)",
            "INSERT INTO u VALUES (7,8)",
            "INSERT INTO pair VALUES (1,1,1)",
            "INSERT INTO pair VALUES (2,1,1)",
            "INSERT INTO pair VALUES (3,2,NULL)",
            "INSERT INTO pair VALUES (4,NULL,2)",
            "INSERT INTO pair VALUES (5,2,3)",
        ]:
            self.run(sql)
        return self

    def __exit__(self, *exc):
        try:
            self.db.shutdown()
        except Exception:
            pass
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        return False

    def run(self, sql):
        r = self.ex.execute_sql(sql, {})
        assert isinstance(r, dict), f'执行结果不是 dict: {sql} -> {r!r}'
        return r

    def q(self, sql):
        r = self.run(sql)
        assert r.get('type') == 'resultset', \
            f'查询失败: {sql} -> {r.get("code")}: {r.get("message")}'
        return [tuple(x) for x in (r.get('rows') or [])]

    def err(self, sql):
        r = self.run(sql)
        assert r.get('type') == 'error', \
            f'本应显式报错，实际成功: {sql} -> {r!r}'
        return int(r.get('code') or 0), str(r.get('message') or '')


# ==========================================================================
# A. BETWEEN
# ==========================================================================

def test_between_where_closed_interval_and_not_between():
    with _Env() as e:
        assert e.q("SELECT id FROM t WHERE id BETWEEN 2 AND 4") == [
            (2,), (3,), (4,)]
        # 闭区间：等于两端都命中
        assert e.q("SELECT id FROM t WHERE id BETWEEN 1 AND 3") == [
            (1,), (2,), (3,)]
        # lo == hi
        assert e.q("SELECT id FROM t WHERE v BETWEEN 2 AND 2") == [(2,), (3,)]
        # NOT BETWEEN
        assert e.q("SELECT id FROM t WHERE v NOT BETWEEN 1 AND 1") == [
            (2,), (3,), (5,), (7,)]
        # 两侧都不是裸列（表达式 / 字面量）
        assert e.q("SELECT id FROM t WHERE id + 1 BETWEEN 3 AND 4") == [
            (2,), (3,)]
        assert e.q("SELECT id FROM t WHERE NULL BETWEEN 1 AND 2") == []


def test_between_and_precedence_is_pinned_in_ast():
    """`a BETWEEN 1 AND 2 AND b = 3` 必须是 (a BETWEEN 1 AND 2) AND b=3

    只断言结果不够（错的树很可能直接报错）；这里同时钉住解析树形状：
    顶层 only 一个 AND，两个孩子分别是 BETWEEN 与 b = 3。
    """
    with _Env() as e:
        sql = "SELECT a FROM pre WHERE a BETWEEN 1 AND 2 AND b = 3"
        assert e.q(sql) == [(1,), (2,)]
        where = e.parser.parse(sql)['where']
        assert 'and' in where, f'顶层不是 AND，实际 AST={where!r}'
        assert len(where['and']) == 2, \
            f'BETWEEN 内部的 AND 被当成了连接词: {where!r}'
        first, second = where['and']
        assert first.get('expr_pred', {}).get('op') == 'BETWEEN', \
            f'第一个子节点不是 BETWEEN: {first!r}'
        assert second.get('column') == 'b' and str(second.get('value')) == '3', \
            f'第二个子节点不是 b = 3: {second!r}'
        # 反过来也要能用 OR 组合
        assert e.q("SELECT id FROM t WHERE id BETWEEN 1 AND 1 "
                   "OR id BETWEEN 7 AND 7") == [(1,), (7,)]


def test_between_three_valued_null_semantics():
    with _Env() as e:
        # v IS NULL 的行（id=4）不命中；v 不在区间的也不命中
        assert e.q("SELECT id FROM t WHERE v BETWEEN 1 AND 2") == [
            (1,), (2,), (3,), (6,)]
        # NULL BETWEEN ... / ... BETWEEN NULL ... → UNKNOWN → 不命中
        assert e.q("SELECT id FROM t WHERE NULL BETWEEN 1 AND 2") == []
        assert e.q("SELECT id FROM t WHERE 1 BETWEEN NULL AND 2") == []
        assert e.q("SELECT id FROM t WHERE 1 BETWEEN 1 AND NULL") == []
        # NULL NOT BETWEEN ... → UNKNOWN（不是 TRUE）：id=4 不能被放行
        assert e.q("SELECT id FROM t WHERE v NOT BETWEEN 1 AND 3") == [(7,)]
        assert e.q("SELECT id FROM t WHERE NULL NOT BETWEEN 1 AND 2") == []
        # 投影里：TRUE/FALSE → 1/0，UNKNOWN → NULL（与 MySQL 一致）
        assert e.q("SELECT id, id BETWEEN 2 AND 3 FROM t ORDER BY id") == [
            (1, 0), (2, 1), (3, 1), (4, 0), (5, 0), (6, 0), (7, 0)]
        assert e.q("SELECT id, id NOT BETWEEN 2 AND 3 FROM t ORDER BY id") == [
            (1, 1), (2, 0), (3, 0), (4, 1), (5, 1), (6, 1), (7, 1)]
        assert e.q("SELECT v BETWEEN 1 AND 2 FROM t WHERE id = 4") == [(None,)]
        assert e.q("SELECT v NOT BETWEEN 1 AND 2 FROM t WHERE id = 4") == [
            (None,)]
        assert e.q("SELECT 1 NOT BETWEEN 1 AND 3 FROM t WHERE id = 1") == [(0,)]


def test_between_works_in_having_projection_and_join_on():
    with _Env() as e:
        # HAVING（聚合谓词）
        assert e.q("SELECT g, COUNT(*) FROM t GROUP BY g "
                   "HAVING COUNT(*) BETWEEN 1 AND 2 ORDER BY g") == [
            ('a', 2), ('b', 2), ('c', 2), ('d', 1)]
        # SELECT 投影（聚合表达式参与 BETWEEN）
        assert e.q("SELECT COUNT(*) BETWEEN 2 AND 7 FROM t") == [(1,)]
        # JOIN ON（非等值连接，走 NestedLoopJoin + ON 后置过滤）
        assert e.q("SELECT t.id, u.lo FROM t JOIN u "
                   "ON t.id BETWEEN u.lo AND u.hi ORDER BY u.lo") == [
            (2, 2), (3, 2), (4, 4), (7, 7)]
        # JOIN 的 WHERE 里也能用
        assert e.q("SELECT t.id FROM t JOIN u ON u.lo = t.id "
                   "WHERE t.id BETWEEN 2 AND 4 ORDER BY t.id") == [(2,), (4,)]


def test_between_type_coercion_and_incomparable_error():
    with _Env() as e:
        # 既有的数字/字符串/小数 coerce 规则
        assert e.q("SELECT id FROM t WHERE v BETWEEN '1' AND '2'") == [
            (1,), (2,), (3,), (6,)]
        assert e.q("SELECT id FROM t WHERE 1 BETWEEN '1' AND '2'") == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,)]
        assert e.q("SELECT id FROM t WHERE id BETWEEN 1.5 AND 3.5") == [
            (2,), (3,)]
        # 不可比较的类型必须显式报错，绝不静默返回 FALSE / 空集
        code, msg = e.err("SELECT id FROM t WHERE name BETWEEN 1 AND 2")
        assert code in (1105, 1366), (code, msg)
        assert '无法比较' in msg or '类型不兼容' in msg, msg


# ==========================================================================
# B. COUNT(DISTINCT ...) 及其它聚合的 DISTINCT
# ==========================================================================

def test_count_distinct_ignores_null_and_keeps_count_star_semantics():
    with _Env() as e:
        assert e.q("SELECT COUNT(DISTINCT g) FROM t") == [(4,)]
        # v = 1,2,2,NULL,3,1,4 -> 4 个不同非 NULL 值
        assert e.q("SELECT COUNT(DISTINCT v) FROM t") == [(4,)]
        # 与既有 COUNT(*) / COUNT(col) 不冲突
        assert e.q("SELECT COUNT(*), COUNT(v), COUNT(DISTINCT v) FROM t") == [
            (7, 6, 4)]
        # WHERE 之后仍然正确
        assert e.q("SELECT COUNT(DISTINCT g) FROM t WHERE g <> 'a'") == [(3,)]
        # 参数是表达式
        assert e.q("SELECT COUNT(DISTINCT LENGTH(g)) FROM t") == [(1,)]


def test_count_distinct_multi_arg_ignores_rows_with_any_null():
    with _Env() as e:
        # pair: (1,1),(1,1),(2,NULL),(NULL,2),(2,3)
        # 忽略含 NULL 的两行；去重后只有 (1,1) 与 (2,3)
        assert e.q("SELECT COUNT(DISTINCT a, b) FROM pair") == [(2,)]
        assert e.q("SELECT COUNT(DISTINCT b, a) FROM pair") == [(2,)]
        # t: (a,1),(a,2),(b,2),(b,NULL),(c,3),(c,1),(d,4) -> 6 个非 NULL 组合
        assert e.q("SELECT COUNT(DISTINCT g, v) FROM t") == [(6,)]


def test_sum_avg_min_max_distinct_are_real():
    with _Env() as e:
        assert e.q("SELECT SUM(DISTINCT v) FROM t") == [(10,)]
        assert e.q("SELECT AVG(DISTINCT v) FROM t") == [(2.5,)]
        assert e.q("SELECT MIN(DISTINCT v), MAX(DISTINCT v) FROM t") == [
            (1, 4)]
        # 与不带 DISTINCT 的聚合逐项对照（v: 1,2,2,NULL,3,1,4）
        assert e.q("SELECT SUM(v), AVG(v) FROM t") == [(13, 13 / 6)]
        assert e.q("SELECT g, SUM(DISTINCT v) FROM t GROUP BY g ORDER BY g") == [
            ('a', 3), ('b', 2), ('c', 4), ('d', 4)]


def test_count_distinct_with_group_by_and_having():
    with _Env() as e:
        assert e.q("SELECT g, COUNT(DISTINCT v) FROM t "
                   "GROUP BY g ORDER BY g") == [
            ('a', 2), ('b', 1), ('c', 2), ('d', 1)]
        assert e.q("SELECT g FROM t GROUP BY g "
                   "HAVING COUNT(DISTINCT v) = 1 ORDER BY g") == [
            ('b',), ('d',)]
        assert e.q("SELECT g FROM t GROUP BY g "
                   "HAVING COUNT(DISTINCT v) BETWEEN 2 AND 3 ORDER BY g") == [
            ('a',), ('c',)]


def test_count_distinct_on_join_plan_path_never_silently_zero():
    """JOIN 走 plan_executor：此前 COUNT(DISTINCT ...) 静默返回 0（错答）。

    这里同时断言非零结果，防止「修成显式报错」也算过关 —— 本能力要求
    真的算对。
    """
    with _Env() as e:
        assert e.q("SELECT COUNT(DISTINCT t.g) FROM t JOIN u "
                   "ON u.lo = t.id") == [(3,)]
        assert e.q("SELECT u.lo, COUNT(DISTINCT t.g) FROM t JOIN u "
                   "ON u.lo = t.id GROUP BY u.lo ORDER BY u.lo") == [
            (2, 1), (4, 1), (7, 1)]
        assert e.q("SELECT COUNT(DISTINCT t.g, u.lo) FROM t JOIN u "
                   "ON u.lo = t.id") == [(3,)]


def test_distinct_on_unsupported_shapes_is_explicit_error():
    with _Env() as e:
        # 标量函数上的 DISTINCT：不静默忽略修饰符
        code, msg = e.err("SELECT LOWER(DISTINCT g) FROM t")
        assert 'DISTINCT' in msg, (code, msg)
        # COUNT(DISTINCT *) 无意义：显式报错
        code, msg = e.err("SELECT COUNT(DISTINCT *) FROM t")
        assert 'DISTINCT' in msg, (code, msg)
        # SUM(DISTINCT a, b)：MySQL 也不接受；本引擎不静默只算第一列
        code, msg = e.err("SELECT SUM(DISTINCT a, b) FROM pair")
        assert '一个参数' in msg or '参数' in msg, (code, msg)
        # 普通 COUNT(a, b) 同样显式报错（不静默只数 a）
        code, msg = e.err("SELECT COUNT(a, b) FROM pair")
        assert 'COUNT' in msg and '参数' in msg, (code, msg)


# ==========================================================================
# C. IN / NOT IN 的 NULL 边界
# ==========================================================================

def test_in_and_not_in_null_three_valued():
    with _Env() as e:
        # IN (NULL) → UNKNOWN → 0 行（不能把 NULL 当值去等值匹配）
        assert e.q("SELECT id FROM t WHERE v IN (NULL)") == []
        # NOT IN (NULL) → UNKNOWN（**不是 TRUE**）→ 0 行
        assert e.q("SELECT id FROM t WHERE v NOT IN (NULL)") == []
        # IN (1, NULL)：命中 1 时为 TRUE；未命中时为 UNKNOWN
        assert e.q("SELECT id FROM t WHERE v IN (1, NULL)") == [(1,), (6,)]
        assert e.q("SELECT id FROM t WHERE v IN (2, NULL)") == [(2,), (3,)]
        # 左值为 NULL → UNKNOWN（即使列表里也有不匹配的值）
        assert e.q("SELECT id FROM t WHERE v IN (1)") == [(1,), (6,)]
        assert e.q("SELECT id FROM t WHERE v NOT IN (1)") == [
            (2,), (3,), (5,), (7,)]


def test_not_in_subquery_with_null_is_unknown_not_true():
    with _Env() as e:
        # 子查询返回 {2, NULL}：v=1/3/4 都因 NULL 得到 UNKNOWN → 0 行。
        # 若把 NOT UNKNOWN 当 TRUE，会错误返回 (1,),(5,),(7,)。
        assert e.q("SELECT id FROM t WHERE v NOT IN "
                   "(SELECT v FROM t WHERE g = 'b')") == []
        # 正例：IN 子查询里的 NULL 不参与匹配，但 2 命中
        assert e.q("SELECT id FROM t WHERE v IN "
                   "(SELECT v FROM t WHERE g = 'b')") == [(2,), (3,)]
        # 子查询无 NULL 时 NOT IN 正常：{1,3} -> v=2 的 (2,) (3,) 与 v=4 的 (7,)
        assert e.q("SELECT id FROM t WHERE v NOT IN "
                   "(SELECT v FROM t WHERE g = 'c')") == [(2,), (3,), (7,)]
        # 子查询为空 → NOT IN 恒真（NULL 左值也包含在内，SQL 标准语义）
        assert e.q("SELECT id FROM t WHERE v NOT IN "
                   "(SELECT v FROM t WHERE v > 100)") == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,)]


def test_empty_in_list_is_syntax_error_1064():
    with _Env() as e:
        code, msg = e.err("SELECT id FROM t WHERE id IN ()")
        assert code == 1064, (code, msg)
        code, msg = e.err("SELECT id FROM t WHERE id NOT IN ()")
        assert code == 1064, (code, msg)


def test_in_mixed_type_list_uses_existing_coerce_rules():
    with _Env() as e:
        # 字面量左值 1 与字符串 '1'：按既有 coerce 规则相等
        assert e.q("SELECT id FROM t WHERE 1 IN ('1')") == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,)]
        assert e.q("SELECT id FROM t WHERE 1 NOT IN ('1')") == []
        assert e.q("SELECT id FROM t WHERE '1' IN (1)") == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,)]
        # 列左值同样走 coerce（INT 列 v vs 字符串字面量）
        assert e.q("SELECT id FROM t WHERE v IN ('1')") == [(1,), (6,)]
        assert e.q("SELECT id FROM t WHERE v NOT IN ('2')") == [
            (1,), (5,), (6,), (7,)]
