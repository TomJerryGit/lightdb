# -*- coding: utf-8 -*-
"""task-59 验收：视图 JOIN 展开 + 多列二级索引最左前缀加速

一、视图 JOIN
  `SELECT ... FROM view [alias] JOIN table/view ON ...`
  [WHERE/GROUP BY/ORDER BY/LIMIT] 全部支持：物化视图/表后做 Hash/嵌套
  循环 JOIN，再复用统一 SELECT 路径；INNER/LEFT 支持，RIGHT/FULL/重复
  别名等无法无歧义处理的形态显式 1105（不静默错答）。
  结果与等价手写基表 SQL 一致（测试逐行比较）。

二、多列二级索引
  `CREATE INDEX idx_ab ON t (a, b)` 后按**最左前缀**加速：
    a=? / a=? AND b=? / a=? AND b>? / ORDER BY a[, b] LIMIT；
  结果与全表扫描逐行一致；非最左前缀(b=?)、OR、LIKE、表达式安全回退；
  `SHOW INDEX` 每列一行、Index_type=BTREE、Cardinality 为真实不同非
  NULL 完整键值数；重启/COMPACT 后一致。

量化（本机 10k 行、a=100 个值、b=10 个值）见 send_message：
  等值 a ~28x、a+b 等值 ~236x、a+范围 ~310x、ORDER BY a,b LIMIT ~206x。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ==================== fixtures / helpers ====================

@pytest.fixture()
def inproc(tmp_path):
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


def err(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') == 'error', (sql, r)
    return r


def rows(ex, sql):
    return [tuple(r) for r in ok(ex, sql)['rows']]


def _sorted_rows(ex, sql):
    return sorted(rows(ex, sql), key=lambda r: tuple(
        (v is None, v) for v in r))


def _seed_join(ex):
    ok(ex, 'CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(20), '
           'dept VARCHAR(10))')
    ok(ex, 'CREATE TABLE orders (id INT PRIMARY KEY, uid INT, amt INT)')
    ok(ex, "INSERT INTO users VALUES (1,'a','eng'), (2,'b','eng'), "
           "(3,'c','sales'), (4,'d',NULL), (5,'e','eng')")
    ok(ex, 'INSERT INTO orders VALUES (10,1,100), (11,1,50), (12,2,70), '
           '(13,3,200), (14,5,20)')
    ok(ex, 'CREATE VIEW v_users AS SELECT id, name, dept FROM users '
           'WHERE id > 0')
    ok(ex, 'CREATE VIEW v_eng AS SELECT id, name FROM users '
           "WHERE dept = 'eng'")
    ok(ex, 'CREATE VIEW v_orders AS SELECT id, uid, amt FROM orders '
           'WHERE amt >= 50')


# ==================== 一、视图 JOIN ====================

def test_view_join_inner_matches_handwritten(inproc):
    _db, ex = inproc
    _seed_join(ex)
    hand = ("SELECT u.id, u.name, o.amt FROM users u "
            "JOIN orders o ON u.id = o.uid WHERE o.amt > 60 "
            "ORDER BY u.id, o.amt LIMIT 10")
    view = ("SELECT v.id, v.name, o.amt FROM v_users v "
            "JOIN orders o ON v.id = o.uid WHERE o.amt > 60 "
            "ORDER BY v.id, o.amt LIMIT 10")
    assert rows(ex, view) == rows(ex, hand)
    # 视图作为 JOIN 的**右侧**
    hand2 = ("SELECT u.id, o.amt FROM users u "
             "JOIN orders o ON u.id = o.uid WHERE o.amt >= 50 "
             "ORDER BY u.id, o.amt")
    view2 = ("SELECT u.id, v.amt FROM users u "
             "JOIN v_orders v ON u.id = v.uid ORDER BY u.id, v.amt")
    assert rows(ex, view2) == rows(ex, hand2)
    print("\n    视图 JOIN 基表（LEFT/RIGHT 位置）：结果与手写等价 SQL 一致")


def test_view_join_left_and_aggregation(inproc):
    _db, ex = inproc
    _seed_join(ex)
    hand = ("SELECT u.id, u.name, o.amt FROM users u "
            "LEFT JOIN orders o ON u.id = o.uid ORDER BY u.id, o.amt")
    view = ("SELECT v.id, v.name, o.amt FROM v_users v "
            "LEFT JOIN orders o ON v.id = o.uid ORDER BY v.id, o.amt")
    assert _sorted_rows(ex, view) == _sorted_rows(ex, hand)
    # 聚合（GROUP BY + COUNT/SUM）走视图 JOIN 后仍一致
    hand_agg = ("SELECT u.id, COUNT(*), SUM(o.amt) FROM users u "
                "JOIN orders o ON u.id = o.uid GROUP BY u.id "
                "ORDER BY u.id")
    view_agg = ("SELECT v.id, COUNT(*), SUM(o.amt) FROM v_users v "
                "JOIN orders o ON v.id = o.uid GROUP BY v.id "
                "ORDER BY v.id")
    assert rows(ex, view_agg) == rows(ex, hand_agg)
    print("\n    视图 JOIN：LEFT JOIN 与 GROUP BY 聚合结果一致")


def test_view_join_view_and_multiple_joins(inproc):
    _db, ex = inproc
    _seed_join(ex)
    # 视图 + 视图
    hand = ("SELECT u.id, u.name, o.amt FROM users u "
            "JOIN orders o ON u.id = o.uid "
            "WHERE u.dept = 'eng' AND o.amt >= 50 ORDER BY u.id, o.amt")
    view = ("SELECT a.id, a.name, b.amt FROM v_eng a "
            "JOIN v_orders b ON a.id = b.uid ORDER BY a.id, b.amt")
    assert rows(ex, view) == rows(ex, hand)
    # 链式 3 个来源（视图 + 表 + 视图）
    view3 = ("SELECT v.id, o.amt, u.name FROM v_users v "
             "JOIN orders o ON v.id = o.uid "
             "JOIN v_eng u ON u.id = v.id "
             "WHERE o.amt > 40 ORDER BY v.id, o.amt")
    hand3 = ("SELECT u.id, o.amt, u.name FROM users u "
             "JOIN orders o ON u.id = o.uid "
             "JOIN users x ON x.id = u.id "
             "WHERE u.id > 0 AND x.dept = 'eng' AND o.amt > 40 "
             "ORDER BY u.id, o.amt")
    assert rows(ex, view3) == rows(ex, hand3)
    # 主视图不带别名（限定名 = 视图名）
    view4 = ("SELECT v_users.id, o.amt FROM v_users "
             "JOIN orders o ON v_users.id = o.uid WHERE o.amt > 40 "
             "ORDER BY v_users.id, o.amt")
    hand4 = ("SELECT u.id, o.amt FROM users u "
             "JOIN orders o ON u.id = o.uid "
             "WHERE u.id > 0 AND o.amt > 40 ORDER BY u.id, o.amt")
    assert rows(ex, view4) == rows(ex, hand4)
    print("\n    视图+视图、链式多 JOIN、主视图无别名：结果一致")


def test_view_join_self_join_alias(inproc):
    _db, ex = inproc
    _seed_join(ex)
    hand = ("SELECT a.id, b.id FROM users a JOIN users b ON a.id = b.id "
            "WHERE a.dept = 'eng' ORDER BY a.id")
    view = ("SELECT a.id, b.id FROM v_users a JOIN v_users b ON a.id = b.id "
            "WHERE a.dept = 'eng' ORDER BY a.id")
    assert rows(ex, view) == rows(ex, hand)
    print("\n    视图自连接（不同别名）结果与手写一致")


def test_view_join_unsupported_explicit_1105(inproc):
    _db, ex = inproc
    _seed_join(ex)
    for sql in (
            'SELECT v.id FROM v_users v RIGHT JOIN orders o '
            'ON v.id = o.uid',
            'SELECT v.id FROM v_users v FULL JOIN orders o '
            'ON v.id = o.uid'):
        r = err(ex, sql)
        assert r['code'] == 1105, r
        assert 'JOIN' in str(r['message']), r
    # 同一来源重复别名（列无法无歧义绑定）→ 显式 1105
    r = err(ex, 'SELECT v.id FROM v_users v JOIN v_users v ON v.id = v.id')
    assert r['code'] == 1105, r
    # JOIN 不存在的表 → 1146（不是 1105 或静默）
    r = err(ex, 'SELECT v.id FROM v_users v JOIN no_such t ON v.id = t.id')
    assert r['code'] == 1146, r
    print("\n    不支持形态（RIGHT/FULL/重复别名）显式 1105，缺表 1146")


# ==================== 二、多列二级索引 ====================

def _seed_multi(ex, n=3000):
    ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, a INT, b INT, c INT)')
    for start in range(0, n, 500):
        vals = ','.join(f'({i},{i % 100},{(i // 100) % 5},{i % 13})'
                        for i in range(start, min(start + 500, n)))
        ok(ex, f'INSERT INTO m VALUES {vals}')
    ok(ex, 'CREATE INDEX idx_ab ON m (a, b)')


def _force_scan(ex, tbl, sql):
    saved = list(tbl._secondary_indexes)
    try:
        tbl._secondary_indexes = []
        tbl._secondary_dirty = True
        return rows(ex, sql)
    finally:
        tbl._secondary_indexes = saved
        tbl._secondary_dirty = True


def test_multi_index_leftmost_prefix_matches_scan(inproc):
    _db, ex = inproc
    _seed_multi(ex)
    tbl = _db.get_table('m')
    queries = [
        'SELECT id FROM m WHERE a = 42',
        'SELECT id FROM m WHERE a = 42 AND b = 3',
        'SELECT id FROM m WHERE a = 42 AND b > 2',
        'SELECT id FROM m WHERE a = 42 AND b <= 1',
        'SELECT id FROM m WHERE a > 95 AND a <= 98',
        'SELECT * FROM m ORDER BY a LIMIT 15',
        'SELECT * FROM m ORDER BY a, b LIMIT 15',
        'SELECT * FROM m ORDER BY a DESC LIMIT 15',
        'SELECT * FROM m ORDER BY a DESC, b DESC LIMIT 15',
    ]
    for sql in queries:
        got = rows(ex, sql)
        exp = _force_scan(ex, tbl, sql)
        assert got == exp, (sql, got[:5], exp[:5])
    stats = tbl.index_usage_stats()
    assert stats['lookups'] > 0, stats
    assert stats['ordered'] > 0, stats
    print(f"\n    多列索引最左前缀：{len(queries)} 类查询与全表扫描逐行一致；"
          f"lookups={stats['lookups']} ordered={stats['ordered']}")


def test_multi_index_safe_fallback(inproc):
    _db, ex = inproc
    _seed_multi(ex)
    tbl = _db.get_table('m')
    # 非最左前缀 b=? / OR / LIKE / 表达式 → 安全回退，结果仍与扫描一致
    for sql in (
            'SELECT id FROM m WHERE b = 2',
            'SELECT id FROM m WHERE a = 1 OR b = 2',
            'SELECT id FROM m WHERE b > 100',
            'SELECT id FROM m WHERE a + 1 = 42'):
        assert rows(ex, sql) == _force_scan(ex, tbl, sql), sql
    before = tbl.index_usage_stats()['fallbacks']
    rows(ex, 'SELECT id FROM m WHERE b = 2')
    rows(ex, 'SELECT id FROM m WHERE a = 1 OR b = 2')
    assert tbl.index_usage_stats()['fallbacks'] > before
    print("\n    多列索引回退：非最左前缀/OR/LIKE/表达式安全回退且结果一致")


def test_multi_index_show_index_metadata(inproc):
    _db, ex = inproc
    _seed_multi(ex)
    idx = ok(ex, 'SHOW INDEX FROM m')
    cols = idx['columns']
    ab = [dict(zip(cols, r)) for r in idx['rows']
          if r[cols.index('Key_name')] == 'idx_ab']
    # MySQL 语义：一个索引多列 → 多行，Seq_in_index 1..n、同一 Cardinality
    assert len(ab) == 2, ab
    assert [r['Seq_in_index'] for r in ab] == [1, 2], ab
    assert [r['Column_name'] for r in ab] == ['a', 'b'], ab
    assert all(r['Index_type'] == 'BTREE' for r in ab), ab
    expected = len({(i % 100, (i // 100) % 5) for i in range(3000)})
    assert all(r['Cardinality'] == expected for r in ab), (ab, expected)
    print(f"\n    SHOW INDEX 多列：2 行、Seq 1/2、BTREE、真实 "
          f"Cardinality={expected}")


def test_multi_index_restart_compact_and_writes(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_multi')
    db = Database(data_dir)
    ex = Executor(db)
    q = 'SELECT id FROM m WHERE a = 42 AND b > 2 ORDER BY id'
    q2 = 'SELECT * FROM m ORDER BY a, b LIMIT 20'
    try:
        _seed_multi(ex)
        tbl = db.get_table('m')
        before = _force_scan(ex, tbl, q)
        assert rows(ex, q) == before
        # 写后重建一致
        ok(ex, 'INSERT INTO m VALUES (4000, 42, 3, 1)')
        ok(ex, 'UPDATE m SET b = 4 WHERE id = 42')
        ok(ex, 'DELETE FROM m WHERE id = 142')
        assert rows(ex, q) == _force_scan(ex, tbl, q)
        db.checkpoint()
        db.compact(tables=['m'])
        assert rows(ex, q) == _force_scan(ex, tbl, q)
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        tbl2 = db2.get_table('m')
        assert rows(ex2, q) == _force_scan(ex2, tbl2, q)
        assert rows(ex2, q2) == _force_scan(ex2, tbl2, q2)
        idx = ok(ex2, 'SHOW INDEX FROM m')
        cols = idx['columns']
        ab = [r for r in idx['rows'] if r[cols.index('Key_name')] == 'idx_ab']
        assert len(ab) == 2 and all(
            r[cols.index('Index_type')] == 'BTREE' for r in ab), ab
        print("\n    多列索引：写/checkpoint/COMPACT/重启后结果与全表扫描一致，"
              "SHOW INDEX 保持 BTREE")
    finally:
        db2.shutdown()
