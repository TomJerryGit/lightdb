# -*- coding: utf-8 -*-
"""task-67 验收：视图 JOIN 定义内联（消除整视图物化开销）

内联路径：单层、无聚合/DISTINCT/GROUP BY/UNION/ORDER BY/LIMIT、投影为
`*` 或裸列的视图，直接把视图定义展开进 JOIN AST，走计划执行器的
Hash Join/谓词下推；结果必须与等价手写基表 SQL 逐行一致。
复杂视图（聚合/投影别名/ORDER BY/LIMIT/右侧 LEFT JOIN 带谓词等）
自动回退到 task-59 的物化路径。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    try:
        yield db, ex
    finally:
        try:
            db.shutdown()
        except Exception:
            pass


def ok(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') != 'error', (sql, r)
    return r


def rows(ex, sql, session=None):
    return [tuple(r) for r in
            ex.execute_sql(sql, session or {})['rows']]


def _sorted(ex, sql):
    rs = rows(ex, sql)
    return sorted(rs, key=lambda r: tuple((v is None, str(v)) for v in r))


def _inline_info(ex, sql):
    from lightdb.sql.parser import parser
    return ex._try_inline_view_join(parser.parse(sql), {})


def _seed(ex):
    ok(ex, 'CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(20), '
           'dept VARCHAR(10))')
    ok(ex, 'CREATE TABLE orders (id INT PRIMARY KEY, uid INT, amt INT)')
    ok(ex, "INSERT INTO users VALUES (1,'a','eng'), (2,'b','eng'), "
           "(3,'c','sales'), (4,'d',NULL), (5,'e','eng')")
    ok(ex, 'INSERT INTO orders VALUES (10,1,100), (11,1,50), (12,2,70), '
           '(13,3,200), (14,5,20)')
    # 可内联视图：* / 裸列投影 + 简单 WHERE
    ok(ex, 'CREATE VIEW v_users AS SELECT * FROM users WHERE id > 0')
    ok(ex, 'CREATE VIEW v_eng AS SELECT id, name, dept FROM users '
           "WHERE dept = 'eng'")
    ok(ex, 'CREATE VIEW v_orders AS SELECT id, uid, amt FROM orders '
           'WHERE amt >= 50')
    # 复杂视图：聚合 / 投影别名 / ORDER BY+LIMIT → 必须回退物化
    ok(ex, 'CREATE VIEW v_agg AS SELECT dept, COUNT(*) AS c FROM users '
           'GROUP BY dept')
    ok(ex, 'CREATE VIEW v_alias AS SELECT id AS uid, name AS person '
           'FROM users')
    ok(ex, 'CREATE VIEW v_lim AS SELECT * FROM users ORDER BY id LIMIT 2')


# ==================== 内联正确性 ====================

def test_inline_inner_join_matches_handwritten(env):
    _db, ex = env
    _seed(ex)
    view = ("SELECT v.id, v.name, o.amt FROM v_users v "
            "JOIN orders o ON v.id = o.uid WHERE o.amt > 60 "
            "ORDER BY v.id, o.amt")
    hand = ("SELECT u.id, u.name, o.amt FROM users u "
            "JOIN orders o ON u.id = o.uid WHERE o.amt > 60 "
            "ORDER BY u.id, o.amt")
    r = _inline_info(ex, view)
    assert r is not None, '该形态应走内联而不是物化'
    assert rows(ex, view) == rows(ex, hand)
    # 视图在 JOIN 右侧 + 别名
    view2 = ("SELECT u.id, v.amt FROM users u "
             "JOIN v_orders v ON u.id = v.uid ORDER BY u.id, v.amt")
    hand2 = ("SELECT u.id, o.amt FROM users u "
             "JOIN orders o ON u.id = o.uid WHERE o.amt >= 50 "
             "ORDER BY u.id, o.amt")
    assert _inline_info(ex, view2) is not None
    assert rows(ex, view2) == rows(ex, hand2)
    print("\n    INNER JOIN 内联：主/右侧视图、别名、WHERE/ORDER 与手写逐行一致")


def test_inline_left_and_cross_join(env):
    _db, ex = env
    _seed(ex)
    # 主视图在 LEFT JOIN 左侧：视图谓词可安全并入 WHERE
    view = ("SELECT v.id, o.amt FROM v_users v "
            "LEFT JOIN orders o ON v.id = o.uid ORDER BY v.id, o.amt")
    hand = ("SELECT u.id, o.amt FROM users u "
            "LEFT JOIN orders o ON u.id = o.uid WHERE u.id > 0 "
            "ORDER BY u.id, o.amt")
    r = _inline_info(ex, view)
    assert r is not None
    assert rows(ex, view) == rows(ex, hand)
    # CROSS JOIN
    view2 = ("SELECT v.id, o.id FROM v_users v CROSS JOIN orders o "
             "WHERE v.id <= 2 AND o.amt >= 70 ORDER BY v.id, o.id")
    hand2 = ("SELECT u.id, o.id FROM users u CROSS JOIN orders o "
             "WHERE u.id > 0 AND u.id <= 2 AND o.amt >= 70 "
             "ORDER BY u.id, o.id")
    assert _inline_info(ex, view2) is not None
    assert rows(ex, view2) == rows(ex, hand2)
    print("\n    LEFT JOIN（视图在左侧）/ CROSS JOIN 内联结果与手写一致")


def test_inline_where_order_limit_alias(env):
    _db, ex = env
    _seed(ex)
    view = ("SELECT x.id, x.name FROM v_eng x "
            "JOIN orders o ON x.id = o.uid WHERE o.amt > 60 "
            "ORDER BY x.id DESC LIMIT 2")
    hand = ("SELECT u.id, u.name FROM users u "
            "JOIN orders o ON u.id = o.uid "
            "WHERE u.dept = 'eng' AND o.amt > 60 "
            "ORDER BY u.id DESC LIMIT 2")
    assert _inline_info(ex, view) is not None
    assert rows(ex, view) == rows(ex, hand)
    print("\n    列投影子集（v_eng）+ 别名 + WHERE/ORDER BY/LIMIT 内联正确")


def test_inline_view_plus_view(env):
    _db, ex = env
    _seed(ex)
    view = ("SELECT a.id, b.amt FROM v_eng a "
            "JOIN v_orders b ON a.id = b.uid ORDER BY a.id, b.amt")
    hand = ("SELECT u.id, o.amt FROM users u "
            "JOIN orders o ON u.id = o.uid "
            "WHERE u.dept = 'eng' AND o.amt >= 50 ORDER BY u.id, o.amt")
    assert _inline_info(ex, view) is not None
    assert rows(ex, view) == rows(ex, hand)
    # 视图自连接（不同别名）
    view2 = ("SELECT a.id, b.id FROM v_users a "
             "JOIN v_users b ON a.id = b.id WHERE a.id <= 3 "
             "ORDER BY a.id")
    hand2 = ("SELECT a.id, b.id FROM users a JOIN users b "
             "ON a.id = b.id WHERE a.id > 0 AND a.id <= 3 ORDER BY a.id")
    assert _inline_info(ex, view2) is not None
    assert rows(ex, view2) == rows(ex, hand2)
    print("\n    视图+视图、视图自连接：两条视图都内联，结果与手写一致")


# ==================== 回退物化（复杂视图） ====================

def test_aggregate_view_falls_back_to_materialization(env):
    _db, ex = env
    _seed(ex)
    sql = ("SELECT a.dept, a.c, u.id FROM v_agg a "
           "JOIN users u ON a.dept = u.dept ORDER BY a.dept, u.id")
    assert _inline_info(ex, sql) is None, '聚合视图必须回退物化'
    got = rows(ex, sql)
    # 预期：dept 聚合结果 × users 同 dept 行
    agg = rows(ex, 'SELECT dept, COUNT(*) FROM users '
                   'GROUP BY dept ORDER BY dept')
    users = rows(ex, 'SELECT id, dept FROM users ORDER BY id')
    expect = []
    for dept, cnt in agg:
        for uid, udept in users:
            if udept == dept and dept is not None:
                expect.append((dept, cnt, uid))
    expect.sort(key=lambda r: (str(r[0]), r[2]))
    assert got == expect
    print("\n    聚合视图：显式回退物化，结果与聚合×用户手算一致")


def test_projection_alias_view_fallback(env):
    _db, ex = env
    _seed(ex)
    sql = ("SELECT a.person FROM v_alias a "
           "JOIN orders o ON a.uid = o.uid ORDER BY a.person")
    assert _inline_info(ex, sql) is None, '投影别名视图必须回退物化'
    hand = ("SELECT u.name FROM users u "
            "JOIN orders o ON u.id = o.uid ORDER BY u.name")
    assert rows(ex, sql) == rows(ex, hand)
    print("\n    投影别名视图：回退物化，结果与手写一致")


def test_order_limit_view_fallback(env):
    _db, ex = env
    _seed(ex)
    sql = ("SELECT v.id, o.amt FROM v_lim v "
           "JOIN orders o ON v.id = o.uid ORDER BY v.id, o.amt")
    assert _inline_info(ex, sql) is None, 'ORDER BY/LIMIT 视图必须回退物化'
    hand = ("SELECT u.id, o.amt FROM users u "
            "JOIN orders o ON u.id = o.uid WHERE u.id <= 2 "
            "ORDER BY u.id, o.amt")
    assert rows(ex, sql) == rows(ex, hand)
    print("\n    ORDER BY/LIMIT 视图：回退物化，结果与等价手写一致")


def test_star_over_subset_view_falls_back(env):
    _db, ex = env
    _seed(ex)
    sql = ("SELECT * FROM v_eng v "
           "JOIN orders o ON v.id = o.uid ORDER BY v.id, o.amt")
    assert _inline_info(ex, sql) is None, '对子集视图 SELECT * 必须回退'
    got = rows(ex, sql)
    # 等价手写：显式列出视图暴露的 3 列 + orders 列
    hand = ("SELECT u.id, u.name, u.dept, o.id, o.uid, o.amt "
            "FROM users u JOIN orders o ON u.id = o.uid "
            "WHERE u.dept = 'eng' ORDER BY u.id, o.amt")
    assert got == rows(ex, hand)
    print("\n    子集视图 SELECT *：回退物化，列集合与手写一致")


def test_left_join_right_side_view_with_predicate_fallback(env):
    _db, ex = env
    _seed(ex)
    # 视图在 LEFT JOIN 右侧且带谓词：不能并进外层 WHERE（会误删空匹配行）
    sql = ("SELECT u.id, v.id, v.amt FROM users u "
           "LEFT JOIN v_orders v ON u.id = v.uid ORDER BY u.id, v.id")
    assert _inline_info(ex, sql) is None, 'LEFT 右侧带谓词视图必须回退物化'
    # 等价手写：视图谓词放在 ON 里
    hand = ("SELECT u.id, o.id, o.amt FROM users u "
            "LEFT JOIN orders o ON u.id = o.uid AND o.amt >= 50 "
            "ORDER BY u.id, o.id")
    assert rows(ex, sql) == rows(ex, hand)
    print("\n    LEFT JOIN 右表视图（带谓词）：安全回退物化，空匹配行保留")


# ==================== 重启加载 / 缓存无关 ====================

def test_restart_reloads_view_and_still_inlines(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_restart')
    db = Database(data_dir)
    ex = Executor(db)
    try:
        _seed(ex)
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        view = ("SELECT v.id, v.name, o.amt FROM v_users v "
                "JOIN orders o ON v.id = o.uid WHERE o.amt > 60 "
                "ORDER BY v.id, o.amt")
        hand = ("SELECT u.id, u.name, o.amt FROM users u "
                "JOIN orders o ON u.id = o.uid WHERE o.amt > 60 "
                "ORDER BY u.id, o.amt")
        assert _inline_info(ex2, view) is not None
        assert rows(ex2, view) == rows(ex2, hand)
        print("\n    重启后视图定义正常加载，仍走内联路径且结果一致")
    finally:
        db2.shutdown()
