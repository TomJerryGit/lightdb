"""声明式类型约束校验（P1 修复 C）：错误码 / 边界 / 原子性

修复前的缺陷（见 docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md §C）：
`Table.coerce()` 只做 `int()/float()/str()`，于是 **INT 越界、VARCHAR(n)
超长、非法 DATETIME、DECIMAL 精度溢出全部静默入库并原样读回**；PK /
NOT NULL 列收 NULL 也被接受。本文件锁定修复后的契约：

1. 错误码按 MySQL：1264/22003（整数越界、DECIMAL 精度）、1406/22001
   （VARCHAR/CHAR 超长，按**字符数**不按字节数）、1292/22007（非法日期）、
   1366/HY000（无法转成整数的字符串）、1048/23000（PK / NOT NULL 收 NULL）；
2. 边界值本身必须正常写入并原样读回（修复不是"一律拒绝"）；
3. PK 的 NULL 只有**显式**写 NULL 才报 1048；`INSERT INTO t (v) VALUES ...`
   省略主键时沿用引擎既有的"自取主键"能力（tests/test_insert_atomicity.py ⑤）；
4. 多值 `INSERT` 中途任一行违反约束 → 整条语句 0 行（语句原子性把异常
   向上传播、由执行器回滚，不落半行）；
5. 全部断言走真实的 `Executor.execute_sql`（与客户端看到的
   `{'type':'error','code':...}` 是同一份结果），不直接调 `coerce`。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_type_constraints.py
    from conftest import track


# ==================== 公共工具 ====================

class Env:
    """独立数据目录 + Database/Executor（走真实 SQL 执行路径）"""

    def __init__(self, data_dir):
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        self.db = track(Database(str(data_dir)))
        self.ex = track(Executor(self.db))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.db.shutdown()
        except Exception:
            pass
        return False

    def run(self, statement, session=None):
        return self.ex.execute_sql(
            statement, {} if session is None else session)

    def ok(self, statement, session=None):
        r = self.run(statement, session)
        assert r.get('type') == 'ok', (statement, r)
        return r

    def err(self, statement, code=None, sqlstate=None, session=None):
        r = self.run(statement, session)
        assert r.get('type') == 'error', (
            f'应当显式失败却成功了: {statement} -> {r}')
        if code is not None:
            assert r.get('code') == code, (
                f'{statement} 的 errno 应为 {code}，实际 {r}')
        if sqlstate is not None:
            assert r.get('sqlstate') == sqlstate, (
                f'{statement} 的 sqlstate 应为 {sqlstate}，实际 {r}')
        return r

    def rs(self, statement, session=None):
        r = self.run(statement, session)
        assert r.get('type') == 'resultset', (statement, r)
        return r

    def rows(self, statement, session=None):
        return self.rs(statement, session)['rows']


# ==================== ① 整数边界 ====================

def test_int_boundary_ok_and_out_of_range_rejected(tmp_path):
    """INT 边界 ±1 个值：min/max 正常，越界 ±1 → 1264/22003"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')

        # 边界值本身必须正常写入（修复不是"一律拒绝"）
        e.ok('INSERT INTO t VALUES (1, -2147483648)')
        e.ok('INSERT INTO t VALUES (2, 2147483647)')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [
            (1, -2147483648), (2, 2147483647)]

        # ±1 越界 → Out of range（1264 / 22003），且不落行
        r = e.err('INSERT INTO t VALUES (3, 2147483648)',
                  code=1264, sqlstate='22003')
        assert 'Out of range' in r['message'], r
        e.err('INSERT INTO t VALUES (4, -2147483649)',
              code=1264, sqlstate='22003')
        # 主键列同样受范围约束
        e.err('INSERT INTO t VALUES (2147483648, 0)',
              code=1264, sqlstate='22003')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [
            (1, -2147483648), (2, 2147483647)]
        print(f"  → INT 边界正常，越界 {r['code']}/{r['sqlstate']}")


def test_bigint_boundary_ok_and_out_of_range_rejected(tmp_path):
    """BIGINT 边界：±9223372036854775808 之外 → 1264"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v BIGINT)')
        e.ok('INSERT INTO t VALUES (1, -9223372036854775808)')
        e.ok('INSERT INTO t VALUES (2, 9223372036854775807)')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [
            (1, -9223372036854775808), (2, 9223372036854775807)]

        e.err('INSERT INTO t VALUES (3, 9223372036854775808)',
              code=1264, sqlstate='22003')
        e.err('INSERT INTO t VALUES (4, -9223372036854775809)',
              code=1264, sqlstate='22003')
        assert [row[0] for row in e.rows('SELECT id FROM t ORDER BY id')] \
            == [1, 2]
        print('  → BIGINT 边界正常，越界 → 1264')


# ==================== ② VARCHAR 长度：按字符数不是字节数 ====================

def test_varchar_length_counts_characters(tmp_path):
    """VARCHAR(3)：'abcdef' → 1406；'中文'（2 字符/6 字节）通过；
    '中文字符'（4 字符）→ 1406 —— 按字符数，不是 UTF-8 字节数"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, s VARCHAR(3))')

        # 2 个中文字符 = 6 字节，但只有 2 个字符 → 合法（MySQL 语义）
        e.ok("INSERT INTO t VALUES (1, '中文')")
        e.ok("INSERT INTO t VALUES (2, 'abc')")
        assert e.rows('SELECT id, s FROM t ORDER BY id') == [
            (1, '中文'), (2, 'abc')]

        r = e.err("INSERT INTO t VALUES (3, 'abcdef')",
                  code=1406, sqlstate='22001')
        assert 'Data too long' in r['message'], r
        e.err("INSERT INTO t VALUES (4, 'abcd')",
              code=1406, sqlstate='22001')
        e.err("INSERT INTO t VALUES (5, '中文字符')",
              code=1406, sqlstate='22001')
        # 字符数恰好等于上限仍然通过
        e.ok("INSERT INTO t VALUES (6, '中文字')")
        assert [row[0] for row in e.rows('SELECT id FROM t ORDER BY id')] \
            == [1, 2, 6]
        print(f"  → VARCHAR 按字符数校验：'中文' 通过、越界 {r['code']}")


# ==================== ③ 日期 / 时间格式 ====================

def test_invalid_datetime_rejected_and_valid_values_kept(tmp_path):
    """非法 DATE/DATETIME → 1292/22007；合法值原样读回"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, d DATE, dt DATETIME)')

        e.ok("INSERT INTO t VALUES (1, '2024-02-29', '2024-01-02 03:04:05')")
        got = e.rows('SELECT d, dt FROM t WHERE id = 1')
        assert got == [('2024-02-29', '2024-01-02 03:04:05')], got

        for bad in ('not-a-date', '2020-13-01', '2023-02-29'):
            r = e.err(f"INSERT INTO t VALUES (2, '{bad}', NULL)",
                      code=1292, sqlstate='22007')
            assert 'Incorrect datetime value' in r['message'], r
        e.err("INSERT INTO t VALUES (3, '2024-01-02', 'not-a-date')",
              code=1292, sqlstate='22007')
        e.err("INSERT INTO t VALUES (4, '2024-01-02', '2024-13-01 00:00:00')",
              code=1292, sqlstate='22007')
        # 失败的行一行都没有留下
        assert e.rows('SELECT id FROM t') == [(1,)]
        print("  → 非法日期 → 1292/22007，合法日期原样读回")


# ==================== ④ DECIMAL 精度 / 标度 ====================

def test_decimal_precision_and_scale(tmp_path):
    """DECIMAL(5,2)：123.45 通过；1.999（小数位超限）/1234.56（整数位
    超限）→ 1264/22003"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v DECIMAL(5,2))')

        e.ok('INSERT INTO t VALUES (1, 123.45)')
        e.ok('INSERT INTO t VALUES (2, -999.99)')
        e.ok('INSERT INTO t VALUES (3, 0.5)')
        assert e.rows('SELECT v FROM t WHERE id = 1') == [(123.45,)]

        for bad in ('1.999', '1234.56', '1000', '-1000.01'):
            r = e.err(f'INSERT INTO t VALUES (9, {bad})',
                      code=1264, sqlstate='22003')
            assert 'Out of range' in r['message'], r
        # 边界：999.99 合法
        e.ok('INSERT INTO t VALUES (4, 999.99)')
        assert sorted(row[0] for row in e.rows('SELECT id FROM t')) \
            == [1, 2, 3, 4]
        print("  → DECIMAL(5,2) 精度/标度越界 → 1264/22003")


# ==================== ⑤ 非数字字符串进 INT ====================

def test_non_numeric_string_to_int_reports_1366(tmp_path):
    """'abc' 进 INT → 1366/HY000（不再按报文猜成 1105/1062）"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        e.ok("INSERT INTO t VALUES (1, '42')")   # 数字字符串仍可转换
        assert e.rows('SELECT v FROM t WHERE id = 1') == [(42,)]

        r = e.err("INSERT INTO t VALUES (2, 'abc')", code=1366, sqlstate='HY000')
        assert 'abc' in r['message'] and 'INT' in r['message'], r
        e.err("INSERT INTO t VALUES (3, '12x')", code=1366)
        assert e.rows('SELECT id FROM t') == [(1,)]
        print(f"  → 非数字字符串进 INT → {r['code']}/{r['sqlstate']}")


# ==================== ⑥ PK / NOT NULL 收 NULL ====================

def test_pk_and_not_null_reject_explicit_null_on_insert(tmp_path):
    """显式 NULL 写 PK / NOT NULL 列 → 1048/23000；省略主键仍可自取"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL, note INT)')

        r = e.err('INSERT INTO t VALUES (NULL, 1, NULL)',
                  code=1048, sqlstate='23000')
        assert "'id'" in r['message'] and '不允许为空' in r['message'], r
        e.err('INSERT INTO t (id, v) VALUES (1, NULL)', code=1048)
        # 主键 / NOT NULL 列在 INSERT 里省略时同样不允许（没有 DEFAULT）
        e.err('INSERT INTO t (id) VALUES (2)', code=1048)
        assert e.rows('SELECT id FROM t') == []

        # 合法行正常写入；nullable 列收 NULL 不受影响
        e.ok('INSERT INTO t VALUES (10, 1, NULL)')
        e.ok('INSERT INTO t (id, v) VALUES (11, 2)')
        # 省略主键（列清单里没给 id）沿用既有"自取主键"能力
        e.ok('INSERT INTO t (v) VALUES (3)')
        got = e.rows('SELECT id, v FROM t ORDER BY id')
        assert got == [(10, 1), (11, 2), (12, 3)], got
        print(f"  → PK/NOT NULL 收 NULL → {r['code']}；省略主键自取正常")


def test_pk_and_not_null_reject_null_on_update(tmp_path):
    """UPDATE 把 NOT NULL 列 / 主键改成 NULL → 1048；nullable 列允许"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL, note INT)')
        e.ok('INSERT INTO t VALUES (1, 5, 6)')
        e.ok('INSERT INTO t VALUES (2, 7, 8)')

        r = e.err('UPDATE t SET v = NULL WHERE id = 1',
                  code=1048, sqlstate='23000')
        assert "'v'" in r['message'] and '不允许为空' in r['message'], r
        e.err('UPDATE t SET id = NULL WHERE id = 1', code=1048)
        # 全表 UPDATE 中途违规 → 一行都不改（语句原子性）
        e.err('UPDATE t SET v = NULL', code=1048)
        assert e.rows('SELECT id, v, note FROM t ORDER BY id') == [
            (1, 5, 6), (2, 7, 8)]

        # nullable 列改成 NULL 仍允许（不误伤合法 UPDATE）
        e.ok('UPDATE t SET note = NULL WHERE id = 1')
        assert e.rows('SELECT note FROM t WHERE id = 1') == [(None,)]
        print(f"  → UPDATE 收 NULL → {r['code']}，合法 UPDATE 不受影响")


# ==================== ⑦ 多值 INSERT 原子性 ====================

def test_multi_value_insert_violation_leaves_no_partial_rows(tmp_path):
    """多值 INSERT 第 3 行违反类型/NOT NULL 约束 → 整条语句 0 行"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL)')

        r = e.err('INSERT INTO t VALUES (1, 10), (2, 20), (3, 2147483648)',
                  code=1264, sqlstate='22003')
        assert e.rows('SELECT id FROM t') == [], (
            f'类型越界中途失败必须整体回滚，实际留下 '
            f'{e.rows("SELECT id FROM t")}')
        r2 = e.err('INSERT INTO t VALUES (1, 10), (2, NULL), (3, 30)',
                   code=1048, sqlstate='23000')
        assert e.rows('SELECT id FROM t') == [], (
            f'NOT NULL 中途失败必须整体回滚（{r2["code"]}）')

        # 失败语句不污染后续写入（自取主键没被失败语句"消耗"错位）
        e.ok('INSERT INTO t VALUES (1, 10), (2, 20)')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [(1, 10), (2, 20)]
        print(f"  → 多值 INSERT 失败整体回滚：{r['code']}/{r2['code']}，"
              f"0 行落地")


# ==================== ⑧ 直接 API 的模块契约 ====================

def test_typecheck_module_contract_and_omitted_pk_flag():
    """`typecheck.validate_value` 的契约：None 原样、未知类型保持 str、
    非法值异常自带 errno/sqlstate（供执行器优先采用）"""
    from lightdb.db.typecheck import (
        TypeCheckError, validate_value)

    assert validate_value(None, 'INT') is None
    assert validate_value('42', 'INT') == 42
    assert validate_value('中文', 'VARCHAR(3)') == '中文'
    assert validate_value('anything', 'UNKNOWN_TYPE') == 'anything'

    cases = [
        ('2147483648', 'INT', 1264, '22003'),
        ('abcdef', 'VARCHAR(3)', 1406, '22001'),
        ('not-a-date', 'DATETIME', 1292, '22007'),
        ('abc', 'INT', 1366, 'HY000'),
    ]
    for value, col_type, errno, sqlstate in cases:
        try:
            validate_value(value, col_type)
        except TypeCheckError as exc:
            assert exc.errno == errno, (value, col_type, exc.errno)
            assert exc.sqlstate == sqlstate, (value, col_type, exc.sqlstate)
            assert isinstance(exc, ValueError), '必须兼容旧的 ValueError 捕获'
        else:
            raise AssertionError(f'应当抛 TypeCheckError: {value!r}/{col_type}')
    print('  → typecheck 模块契约（errno/sqlstate/None/未知类型）通过')
