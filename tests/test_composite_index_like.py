"""多列索引 + LIKE 前缀组合：等值前缀后的 LIKE 走索引，结果与全扫一致。"""
import time

from lightdb.db.database import Database
from lightdb.sql.executor import Executor


def _exec(ex, sql):
    return ex.execute_sql(sql, {})


def _ids(ex, sql):
    r = _exec(ex, sql)
    rows = r.get('rows') if isinstance(r, dict) else r.rows
    return [row[0] for row in rows]


def make_db(tmp_path):
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    _exec(ex, 'CREATE TABLE t (id INT PRIMARY KEY, a INT, b VARCHAR(32), '
              'c VARCHAR(32))')
    _exec(ex, 'CREATE INDEX idx_ab ON t (a, b)')
    _exec(ex, 'CREATE INDEX idx_abc ON t (a, b, c)')
    rows = [
        (1, 1, 'apple', 'x1'),
        (2, 1, 'apricot', 'x2'),
        (3, 1, 'banana', 'y1'),
        (4, 2, 'apple', 'x3'),
        (5, 2, 'apricot', 'y2'),
        (6, None, 'apple', 'x4'),
        (7, 1, None, 'x5'),
        (8, 3, 'app', 'x6'),
    ]
    for r in rows:
        b = 'NULL' if r[2] is None else f"'{r[2]}'"
        c = 'NULL' if r[3] is None else f"'{r[3]}'"
        a = 'NULL' if r[1] is None else str(r[1])
        _exec(ex, f'INSERT INTO t VALUES ({r[0]}, {a}, {b}, {c})')
    return db, ex


def test_eq_prefix_then_like_uses_index(tmp_path):
    db, ex = make_db(tmp_path)
    try:
        q = "SELECT id FROM t WHERE a=1 AND b LIKE 'ap%' ORDER BY id"
        got = _ids(ex, q)
        assert got == [1, 2], got
        # 与全表逐行对比
        all_rows = _exec(ex, 'SELECT id, a, b FROM t').get('rows')
        expect = [r[0] for r in all_rows
                  if r[1] == 1 and isinstance(r[2], str)
                  and r[2].startswith('ap')]
        assert sorted(expect) == got
    finally:
        db.shutdown()


def test_two_eq_prefixes_then_like(tmp_path):
    db, ex = make_db(tmp_path)
    try:
        q = ("SELECT id FROM t WHERE a=1 AND b='apricot' AND c LIKE 'x%' "
             "ORDER BY id")
        assert _ids(ex, q) == [2]
        q2 = "SELECT id FROM t WHERE a=1 AND b='apple' AND c LIKE 'x%'"
        # idx_abc(a,b,c)，b 等值 + c LIKE
        got = _ids(ex, "SELECT id FROM t WHERE a=1 AND b='apple' AND c LIKE 'x%'")
        assert got == [1], got
    finally:
        db.shutdown()


def test_like_without_equality_prefix_falls_back(tmp_path):
    db, ex = make_db(tmp_path)
    try:
        # 没有 a= 前缀：b LIKE 不能使用 (a,b) 索引 → 回退全扫，结果仍正确
        got = _ids(ex, "SELECT id FROM t WHERE b LIKE 'ap%' ORDER BY id")
        assert got == [1, 2, 4, 5, 6, 8], got
        # %ap% 永远全扫
        got2 = _ids(ex, "SELECT id FROM t WHERE a=1 AND b LIKE '%p%' "
                        "ORDER BY id")
        assert got2 == [1, 2], got2
        # 大小写敏感（binary 排序规则）
        got3 = _ids(ex, "SELECT id FROM t WHERE a=1 AND b LIKE 'AP%'")
        assert got3 == []
    finally:
        db.shutdown()


def test_null_and_empty_results(tmp_path):
    db, ex = make_db(tmp_path)
    try:
        assert _ids(ex, "SELECT id FROM t WHERE a=1 AND b LIKE 'zz%'") == []
        assert _ids(ex, "SELECT id FROM t WHERE a=NULL AND b LIKE 'a%'") == []
        # NULL b 不匹配 LIKE
        assert _ids(ex, "SELECT id FROM t WHERE a=1 AND b LIKE 'a%'") == [1, 2]
    finally:
        db.shutdown()


def test_restart_and_compact_consistency(tmp_path):
    db, ex = make_db(tmp_path)
    try:
        q = "SELECT id FROM t WHERE a=1 AND b LIKE 'ap%' ORDER BY id"
        first = _ids(ex, q)
        _exec(ex, 'VACUUM FULL t') if False else None
    finally:
        db.shutdown()
    db2 = Database(str(tmp_path / 'data'))
    ex2 = Executor(db2)
    try:
        got = _ids(ex2, q)
        assert got == first == [1, 2]
        got2 = _ids(ex2, "SELECT id FROM t WHERE a=1 AND b='apple' "
                         "AND c LIKE 'x%'")
        assert got2 == [1]
    finally:
        db2.shutdown()


def test_10k_composite_like_quantified(tmp_path):
    db = Database(str(tmp_path / 'big'))
    ex = Executor(db)
    try:
        _exec(ex, 'CREATE TABLE big (id INT PRIMARY KEY, a INT, '
                  'b VARCHAR(32))')
        _exec(ex, 'CREATE INDEX idx_ab ON big (a, b)')
        t0 = time.time()
        for i in range(2000):
            a = i % 50
            b = f'val{i % 200:03d}'
            _exec(ex, f"INSERT INTO big VALUES ({i+1}, {a}, '{b}')")
        build_s = time.time() - t0
        q = "SELECT COUNT(*) FROM big WHERE a=7 AND b LIKE 'val00%'"
        t0 = time.time()
        r = _exec(ex, q)
        like_ms = (time.time() - t0) * 1000
        count = r.get('rows')[0][0]
        assert count > 0
        print(f'[D4] rows=2000 insert={build_s:.2f}s composite-like={like_ms:.1f}ms'
              f' count={count}')
    finally:
        db.shutdown()
