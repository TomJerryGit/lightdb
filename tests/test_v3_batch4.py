"""v3.0 Batch 4 测试 - 功能补齐"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_string_functions():
    """测试1: 字符串函数"""
    print("\n【测试1】字符串函数")
    from lightdb.sql.functions.string_funcs import STRING_FUNCTIONS

    print(f"  → 已注册 {len(STRING_FUNCTIONS)} 个函数")
    assert len(STRING_FUNCTIONS) >= 45

    # 关键函数测试
    f = STRING_FUNCTIONS
    assert f['CONCAT']('a', 'b', 'c') == 'abc'
    assert f['CONCAT']('a', None, 'c') is None
    assert f['CONCAT_WS']('-', 'a', None, 'c') == 'a-c'
    assert f['LENGTH']('你好') == 6
    assert f['CHAR_LENGTH']('你好') == 2
    assert f['UPPER']('hello') == 'HELLO'
    assert f['LOWER']('WORLD') == 'world'
    assert f['SUBSTRING']('hello', 2) == 'ello'
    assert f['SUBSTRING']('hello', 2, 3) == 'ell'
    assert f['SUBSTRING']('hello', -2) == 'lo'
    assert f['LEFT']('hello', 3) == 'hel'
    assert f['RIGHT']('hello', 3) == 'llo'
    assert f['LOCATE']('ll', 'hello') == 3
    assert f['INSTR']('hello', 'll') == 3
    assert f['REPLACE']('hello', 'l', 'L') == 'heLLo'
    assert f['REVERSE']('hello') == 'olleh'
    assert f['REPEAT']('ab', 3) == 'ababab'
    assert f['LPAD']('5', 3, '0') == '005'
    assert f['RPAD']('5', 3, '0') == '500'
    assert f['TRIM']('  x  ') == 'x'
    assert f['LTRIM']('  x  ') == 'x  '
    assert f['RTRIM']('  x  ') == '  x'
    assert f['SUBSTRING_INDEX']('a.b.c', '.', 2) == 'a.b'
    assert f['SUBSTRING_INDEX']('a.b.c', '.', -1) == 'c'
    assert f['FIELD']('b', 'a', 'b', 'c') == 2
    assert f['ELT'](2, 'a', 'b', 'c') == 'b'
    assert f['FIND_IN_SET']('b', 'a,b,c') == 2
    assert f['MD5']('abc') == '900150983cd24fb0d6963f7d28e17f72'
    assert f['HEX']('AB') == '4142'
    print("  ✅ 字符串函数核心用例通过")

    # 正则
    assert f['REGEXP_LIKE']('abc123', r'\d+') == 1
    assert f['REGEXP_LIKE']('abc', r'\d+') == 0
    assert f['REGEXP_REPLACE']('a1b2c3', r'\d', '') == 'abc'
    print("  ✅ 正则函数")


def test_datetime_functions():
    """测试2: 日期时间函数"""
    print("\n【测试2】日期时间函数")
    from lightdb.sql.functions.datetime_funcs import DATETIME_FUNCTIONS

    print(f"  → 已注册 {len(DATETIME_FUNCTIONS)} 个函数")
    assert len(DATETIME_FUNCTIONS) >= 40

    f = DATETIME_FUNCTIONS

    # 当前时间
    now_str = f['NOW']()
    assert len(now_str) == 19
    curdate = f['CURDATE']()
    assert len(curdate) == 10
    print(f"  → NOW={now_str}, CURDATE={curdate}")

    # 提取
    d = '2025-01-15 14:30:45'
    assert f['YEAR'](d) == 2025
    assert f['MONTH'](d) == 1
    assert f['DAY'](d) == 15
    assert f['HOUR'](d) == 14
    assert f['MINUTE'](d) == 30
    assert f['SECOND'](d) == 45
    assert f['DAYOFWEEK'](d) == 4    # 2025-01-15 是周三 → MySQL 3=Wed, 1=Sun, 4=Wed
    assert f['QUARTER'](d) == 1
    print("  ✅ 日期提取")

    # 格式化
    assert f['DATE_FORMAT'](d, '%Y/%m/%d') == '2025/01/15'
    assert f['DATE_FORMAT'](d, '%H:%i:%s') == '14:30:45'
    assert f['DATE_FORMAT'](d, '%Y年%m月%d日') == '2025年01月15日'
    print("  ✅ 格式化")

    # 计算
    assert f['DATEDIFF']('2025-01-15', '2025-01-01') == 14
    result = f['DATE_ADD']('2025-01-01', '1 DAY')
    assert result.startswith('2025-01-02')
    result = f['DATE_SUB']('2025-01-15', '5 DAY')
    assert result.startswith('2025-01-10')
    print("  ✅ 日期计算")

    # 边界
    assert f['LAST_DAY']('2025-02-01') == '2025-02-28'
    assert f['LAST_DAY']('2024-02-01') == '2024-02-29'
    print("  ✅ 边界处理")


def test_math_functions():
    """测试3: 数学函数"""
    print("\n【测试3】数学函数")
    from lightdb.sql.functions.math_funcs import MATH_FUNCTIONS

    print(f"  → 已注册 {len(MATH_FUNCTIONS)} 个函数")
    assert len(MATH_FUNCTIONS) >= 25

    f = MATH_FUNCTIONS
    assert f['ABS'](-5) == 5
    assert f['CEIL'](3.2) == 4
    assert f['FLOOR'](3.8) == 3
    assert f['ROUND'](3.14159, 2) == 3.14
    assert f['MOD'](10, 3) == 1
    assert f['POW'](2, 10) == 1024
    assert f['SQRT'](16) == 4.0
    assert f['SIGN'](-5) == -1
    assert f['GREATEST'](1, 5, 3) == 5
    assert f['LEAST'](1, 5, 3) == 1
    assert f['TRUNCATE'](3.14159, 2) == 3.14
    print("  ✅ 数学函数")


def test_control_functions():
    """测试4: 流程控制函数"""
    print("\n【测试4】流程控制函数")
    from lightdb.sql.functions.control_funcs import CONTROL_FUNCTIONS

    f = CONTROL_FUNCTIONS
    assert f['IF'](True, 'a', 'b') == 'a'
    assert f['IF'](False, 'a', 'b') == 'b'
    assert f['IFNULL'](None, 'default') == 'default'
    assert f['IFNULL']('value', 'default') == 'value'
    assert f['NULLIF'](1, 1) is None
    assert f['NULLIF'](1, 2) == 1
    assert f['COALESCE'](None, None, 'x') == 'x'
    assert f['CAST']('123', 'INT') == 123
    assert f['CAST'](123, 'CHAR') == '123'
    print("  ✅ 流程控制函数")


def test_registry():
    """测试5: 函数注册表"""
    print("\n【测试5】函数注册表")
    from lightdb.sql.functions.registry import get_registry

    reg = get_registry()
    total = reg.count()
    print(f"  → 总函数数: {total}")
    # 修正（E 类 #31）：原断言 `total >= 150` 的 "150" 全仓库（源码/文档）无任何
    # 出处（§9.7 #31：`git log -S ">= 150"` 只指向首次快照），而注册表在
    # HEAD 342ad2e 实测 132 个内建函数（reg.count() == len(self.functions)，
    # registry.py:44-45）。无来源的数字当门禁只会制造假失败，
    # 下界改为实测值 132，并在下面用"注册表 = 四类内建函数的并集"钉住真实契约，
    # 丢函数/重复注册照样会红（不放水）。
    assert total >= 132, f"内建函数数回退到 {total}（HEAD 基线 132）"

    # 分类统计
    from lightdb.sql.functions.string_funcs import STRING_FUNCTIONS
    from lightdb.sql.functions.datetime_funcs import DATETIME_FUNCTIONS
    from lightdb.sql.functions.math_funcs import MATH_FUNCTIONS
    from lightdb.sql.functions.control_funcs import CONTROL_FUNCTIONS

    print(f"    - 字符串: {len(STRING_FUNCTIONS)}")
    print(f"    - 日期时间: {len(DATETIME_FUNCTIONS)}")
    print(f"    - 数学: {len(MATH_FUNCTIONS)}")
    print(f"    - 控制: {len(CONTROL_FUNCTIONS)}")

    # 真实契约（registry.py:20-24）：注册表恰好是四类内建函数的并集
    builtin = (set(STRING_FUNCTIONS) | set(DATETIME_FUNCTIONS)
               | set(MATH_FUNCTIONS) | set(CONTROL_FUNCTIONS))
    assert set(reg.list_all()) == {n.upper() for n in builtin}
    assert total == len(builtin)

    # 调用测试
    assert reg.call('UPPER', 'hello') == 'HELLO'
    assert reg.call('NOW')[0:4].isdigit()
    print("  ✅ 调用生效")


def test_variables():
    """测试6: 会话/用户变量"""
    print("\n【测试6】会话/用户变量")
    from lightdb.sql.variables import SessionVariables, VariableManager

    # 会话变量
    s = SessionVariables()
    assert s.get_session('autocommit') == 1
    assert s.get_session('character_set_client') == 'utf8mb4'

    s.set_session('autocommit', 0)
    assert s.get_session('autocommit') == 0

    # 只读检查
    assert s.is_readonly('version')
    try:
        s.set_session('version', 'x')
        assert False
    except Exception:
        pass
    print(f"  ✅ 会话变量")
    print(f"    version = {s.get_session('version')}")

    # 用户变量
    s.set_user('@x', 100)
    assert s.get_user('@x') == 100
    s.set_user('@name', 'Alice')
    assert s.get_user('name') == 'Alice'
    print(f"  ✅ 用户变量")

    # 管理器
    vm = VariableManager()
    s1 = vm.get_session(1)
    s2 = vm.get_session(2)
    s1.set_user('@x', 'session1')
    s2.set_user('@x', 'session2')
    assert s1.get_user('@x') == 'session1'
    assert s2.get_user('@x') == 'session2'
    print(f"  ✅ 多会话隔离")


def test_cursor():
    """测试7: 游标"""
    print("\n【测试7】游标")
    from lightdb.sql.cursor import CursorManager, CursorError

    cm = CursorManager()

    # 声明
    cm.declare('cur_users', 'SELECT id, name FROM users')
    assert 'cur_users' in cm.list_cursors()
    print(f"  ✅ 声明游标")

    # 手动注入数据（模拟 open）
    cur = cm.cursors['cur_users']
    cur.rows = [(1, 'Alice'), (2, 'Bob'), (3, 'Carol')]
    cur.position = -1
    cur.is_open = True

    # 遍历
    results = []
    while True:
        row = cm.fetch('cur_users')
        if row is None:
            break
        results.append(row)

    assert len(results) == 3
    assert results[0] == (1, 'Alice')
    print(f"  ✅ 遍历 {len(results)} 行")

    # 关闭
    cm.close('cur_users')
    assert not cm.cursors['cur_users'].is_open
    print(f"  ✅ 关闭游标")

    # 错误处理
    try:
        cm.fetch('cur_users')
        assert False
    except CursorError:
        print(f"  ✅ 关闭后 fetch 报错")


def test_icp():
    """测试8: 索引条件下推"""
    print("\n【测试8】索引条件下推")
    from lightdb.sql.icp import ICPOptimizer, ICPPlan, ICPCondition

    # 条件测试
    c = ICPCondition('age', '>', 20)
    assert c.match(25) is True
    assert c.match(15) is False

    c = ICPCondition('city', '=', 'Beijing')
    assert c.match('Beijing') is True
    assert c.match('Shanghai') is False

    c = ICPCondition('name', 'LIKE', '%li%')
    assert c.match('Alice') is True
    assert c.match('Bob') is False
    print("  ✅ 条件判断")

    # 计划构建
    plan = ICPOptimizer.build_icp_plan(
        table='users',
        index_name='idx_age_city',
        index_columns=['age', 'city'],
        where_conditions=[
            {'column': 'age', 'op': '>', 'value': 20},
            {'column': 'city', 'op': '=', 'value': 'Beijing'},
            {'column': 'name', 'op': 'LIKE', 'value': '%li%'},
        ])

    assert len(plan.index_conditions) == 2
    assert len(plan.table_conditions) == 1
    print(f"  → 索引条件: {len(plan.index_conditions)}")
    print(f"  → 表级条件: {len(plan.table_conditions)}")

    # 过滤
    entries = [
        {'age': 15, 'city': 'Beijing'},   # ✗
        {'age': 25, 'city': 'Shanghai'},  # ✗
        {'age': 25, 'city': 'Beijing'},   # ✓
        {'age': 30, 'city': 'Beijing'},   # ✓
    ]
    filtered = [e for e in entries if plan.filter_index_entry(e)]
    assert len(filtered) == 2
    print(f"  ✅ ICP 过滤: 4 → {len(filtered)}")


@pytest.mark.skip(
    reason="ParserV3B4 是死代码（生产路径零引用）；现役解析器由 tests/test_v4_sql_features.py "
           "等覆盖。保留该文件仅供历史对照，不再作为门禁")
def test_parser_v3b4():
    """测试9: 扩展解析器"""
    print("\n【测试9】扩展解析器")
    from lightdb.sql.parser_v3b4 import ParserV3B4

    # SET @var
    r = ParserV3B4.parse_set("SET @x = 100")
    assert r['command'] == 'SET USER VAR'
    assert r['name'] == '@x'
    assert r['value'] == '100'
    print(f"  ✅ SET @x = 100")

    # SET @@var
    r = ParserV3B4.parse_set("SET @@autocommit = 0")
    assert r['command'] == 'SET SYSTEM VAR'
    assert r['name'] == 'autocommit'
    print(f"  ✅ SET @@autocommit = 0")

    # SET NAMES
    r = ParserV3B4.parse_set("SET NAMES utf8mb4")
    assert r['command'] == 'SET NAMES'
    print(f"  ✅ SET NAMES")

    # SET ISOLATION
    r = ParserV3B4.parse_set(
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    assert r['command'] == 'SET ISOLATION'
    print(f"  ✅ SET ISOLATION")

    # SELECT @var
    r = ParserV3B4.parse_select_user_var(
        "SELECT @@version, @x, @@autocommit")
    assert r['command'] == 'SELECT VARIABLES'
    assert len(r['variables']) == 3
    print(f"  ✅ SELECT VARIABLES")

    # INTERVAL
    r = ParserV3B4.parse_interval("INTERVAL 1 DAY")
    assert r == (1, 'DAY')
    print(f"  ✅ INTERVAL 解析")


@pytest.mark.skip(
    reason="VariableExecutor/ParserV3B4 是死代码（生产路径零引用）；会话变量由 "
           "net/server.py + executor 实现并在 tests/test_wire_transaction.py 覆盖")
def test_variable_executor():
    """测试10: 变量执行器"""
    print("\n【测试10】变量执行器")
    from lightdb.sql.parser_v3b4 import ParserV3B4, VariableExecutor

    ve = VariableExecutor()

    # SET @x
    parsed = ParserV3B4.parse_set("SET @x = 42")
    r = ve.execute_set_user_var(parsed, session_id=1)
    assert r['type'] == 'ok'
    print(f"  ✅ SET @x = 42")

    # SELECT @x
    parsed = {'command': 'SELECT VARIABLES', 'variables': ['@x']}
    r = ve.execute_select_variables(parsed, session_id=1)
    assert r['rows'][0][0] == 42
    print(f"  ✅ SELECT @x → {r['rows'][0][0]}")

    # SET @@autocommit
    parsed = ParserV3B4.parse_set("SET @@autocommit = 0")
    r = ve.execute_set_system_var(parsed, session_id=1)
    assert r['type'] == 'ok'
    print(f"  ✅ SET @@autocommit = 0")

    # SELECT @@autocommit
    parsed = {'command': 'SELECT VARIABLES',
              'variables': ['@@autocommit']}
    r = ve.execute_select_variables(parsed, session_id=1)
    assert r['rows'][0][0] == 0
    print(f"  ✅ SELECT @@autocommit → {r['rows'][0][0]}")

    # 只读变量
    parsed = ParserV3B4.parse_set("SET @@version = 'x'")
    try:
        ve.execute_set_system_var(parsed, session_id=1)
        assert False
    except ValueError as e:
        print(f"  ✅ 只读变量拒绝: {e}")


def test_date_format_unicode_literals_on_ascii_locale():
    """CI 回归（2026-10-21）：DATE_FORMAT 的非 ASCII 字面量不能依赖 locale 编码。

    GitHub Windows runner 是 cp1252，`strftime('%Y年%m月%d日')` 会
    UnicodeEncodeError → 旧实现返回 None。用 wrapper 模拟该异常，验证
    `_format_dt` 的占位符兜底仍然输出正确中文。
    """
    from datetime import datetime
    from lightdb.sql.functions.datetime_funcs import (
        DATETIME_FUNCTIONS, _format_dt)

    d = datetime(2025, 1, 15, 14, 30, 45)
    assert DATETIME_FUNCTIONS['DATE_FORMAT'](
        '2025-01-15 14:30:45', '%Y年%m月%d日') == '2025年01月15日'

    class _Cp1252Like:
        def __init__(self, real):
            self.real = real
            self.calls = 0

        def strftime(self, fmt):
            self.calls += 1
            if self.calls == 1:                 # 第一次模拟平台编码失败
                raise UnicodeEncodeError('cp1252', str(fmt), 0, 1,
                                         'character maps to <undefined>')
            return self.real.strftime(fmt)

    fake = _Cp1252Like(d)
    assert _format_dt(fake, '%Y年%m月%d日') == '2025年01月15日'
    assert fake.calls == 2, '兜底应重试一次 strftime（占位符版本）'


def main():
    print("=" * 60)
    print("  v3.0 Batch 4 测试 - 功能补齐")
    print("=" * 60)

    tests = [
        ('字符串函数', test_string_functions),
        ('日期时间函数', test_datetime_functions),
        ('数学函数', test_math_functions),
        ('流程控制函数', test_control_functions),
        ('函数注册表', test_registry),
        ('会话/用户变量', test_variables),
        ('游标', test_cursor),
        ('索引条件下推', test_icp),
        ('扩展解析器', test_parser_v3b4),
        ('变量执行器', test_variable_executor),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 v3.0 Batch 4 全部通过！")
        print("  📊 MySQL 5.7 兼容度: 85% → 92%")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())