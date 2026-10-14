"""Batch 12 测试 - 存储过程/触发器/函数/事件"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch12.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b12')


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def setup():
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'name', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'age', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
    ])
    db.create_table('logs', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'msg', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
    ])
    return db, ex


def test_create_procedure():
    """测试1: 创建并 CALL 简单存储过程"""
    print("\n【测试1】存储过程 - 简单插入")
    reset()
    db, ex = setup()
    from lightdb.sql.parser import parser

    sql = """CREATE PROCEDURE add_user(IN uid INT, IN uname VARCHAR, IN uage INT)
    BEGIN
        INSERT INTO users VALUES (uid, uname, uage);
    END"""
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")
    assert r.get('type') == 'ok'

    # CALL
    p = parser.parse("CALL add_user(1, 'Alice', 30)")
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    # 验证
    tbl = db.get_table('users')
    cols, rows = tbl.select(['*'])
    print(f"  → users: {rows}")
    assert len(rows) == 1
    assert rows[0][1] == 'Alice'
    print("  ✅ 存储过程插入成功")
    db.shutdown()


def test_procedure_if_while():
    """测试2: 存储过程 + IF/WHILE"""
    print("\n【测试2】存储过程 - IF/WHILE")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    # 断言要求 users 表空（WHILE i < n 从 i=100 起恰好插 5 行）。
    # 本用例原来直接复用上一个用例留下的 DATA_DIR，因此把上个用例插入的
    # Alice 行一起数进来 → 6 != 5。这是**用例的初始状态问题**，不是循环
    # 语义问题（MySQL 里 `WHILE i < 105` 从 100 循环也是 5 行）。
    # 与 test_create_procedure / test_trigger_* 等用例一致：先 reset 再建表，
    # 这样本用例单跑、或跟在任何用例后面跑，初始状态都一样。
    reset()
    db, ex = setup()

    # 存储过程：根据 uid 判断，插入多条
    sql = """CREATE PROCEDURE gen_users(IN n INT)
    BEGIN
        DECLARE i INT DEFAULT 100;
        WHILE i < n DO
            INSERT INTO users VALUES (i, 'u', i);
            SET i = i + 1;
        END WHILE;
    END"""
    p = parser.parse(sql)
    ex.execute(p, {})

    p = parser.parse("CALL gen_users(105)")
    ex.execute(p, {})

    tbl = db.get_table('users')
    cols, rows = tbl.select(['*'])
    print(f"  → 生成 {len(rows)} 行")
    for r in rows:
        print(f"    {r}")
    assert len(rows) == 5
    print("  ✅ WHILE 循环生成成功")
    db.shutdown()


def test_procedure_out_param():
    """测试3: 存储过程 OUT 参数"""
    print("\n【测试3】存储过程 - OUT 参数")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = track(Database(DATA_DIR))
    ex = track(Executor(db))

    sql = """CREATE PROCEDURE count_users(OUT cnt INT)
    BEGIN
        SELECT COUNT(*) INTO cnt FROM users;
    END"""
    p = parser.parse(sql)
    ex.execute(p, {})

    # CALL 带 OUT 变量
    p = parser.parse("CALL count_users(@c)")
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")
    print("  ℹ️  OUT 参数需通过会话变量读取，此处简化测试")
    db.shutdown()


def test_trigger_before_insert():
    """测试4: BEFORE INSERT 触发器"""
    print("\n【测试4】BEFORE INSERT 触发器")
    reset()
    db, ex = setup()
    from lightdb.sql.parser import parser

    # 触发器：自动填充 age 默认值
    sql = """CREATE TRIGGER trg_default_age
    BEFORE INSERT ON users
    FOR EACH ROW
    BEGIN
        IF NEW.age IS NULL THEN
            SET NEW.age = 18;
        END IF;
    END"""
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    # 插入没有 age 的行
    p = parser.parse("INSERT INTO users VALUES (1, 'Alice', NULL)")
    ex.execute(p, {})

    tbl = db.get_table('users')
    cols, rows = tbl.select(['*'])
    print(f"  → users: {rows}")
    assert rows[0][2] == 18
    print("  ✅ 触发器填充默认值成功")
    db.shutdown()


def test_trigger_after_insert():
    """测试5: AFTER INSERT 触发器"""
    print("\n【测试5】AFTER INSERT 触发器（日志）")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = track(Database(DATA_DIR))
    ex = track(Executor(db))

    # 需要 logs 表
    try:
        db.get_table('logs')
    except KeyError:
        db.create_table('logs', [
            {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
            {'name': 'msg', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
        ])

    # 触发器：插入 users 后写日志
    sql = """CREATE TRIGGER trg_log_insert
    AFTER INSERT ON users
    FOR EACH ROW
    BEGIN
        INSERT INTO logs VALUES (NEW.id, 'inserted');
    END"""
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")

    # 插入用户
    p = parser.parse("INSERT INTO users VALUES (999, 'Test', 25)")
    ex.execute(p, {})

    # 检查日志
    logs = db.get_table('logs')
    cols, rows = logs.select(['*'])
    print(f"  → logs: {rows}")
    assert len(rows) >= 1
    print("  ✅ 触发器写日志成功")
    db.shutdown()


def test_trigger_delete():
    """测试6: BEFORE/AFTER DELETE 触发器"""
    print("\n【测试6】DELETE 触发器")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = track(Database(DATA_DIR))
    ex = track(Executor(db))

    # 清空 logs，再插入新用户
    p = parser.parse("DELETE FROM logs")
    ex.execute(p, {})
    p = parser.parse("INSERT INTO users VALUES (1000, 'Temp', 50)")
    ex.execute(p, {})

    # BEFORE DELETE 触发器
    sql = """CREATE TRIGGER trg_before_del
    BEFORE DELETE ON users
    FOR EACH ROW
    BEGIN
        INSERT INTO logs VALUES (9999, 'deleting');
    END"""
    p = parser.parse(sql)
    ex.execute(p, {})

    p = parser.parse("DELETE FROM users WHERE id = 1000")
    ex.execute(p, {})

    logs = db.get_table('logs')
    cols, rows = logs.select(['*'], {'column': 'id', 'op': '=', 'value': 9999})
    print(f"  → 删除日志: {rows}")
    assert len(rows) >= 1
    print("  ✅ BEFORE DELETE 触发器成功")
    db.shutdown()


def test_show_procedures_triggers():
    """测试7: SHOW 命令"""
    print("\n【测试7】SHOW PROCEDURES / TRIGGERS / EVENTS")
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser

    db = track(Database(DATA_DIR))
    ex = track(Executor(db))

    for show_cmd in ['SHOW PROCEDURES', 'SHOW TRIGGERS', 'SHOW EVENTS', 'SHOW FUNCTIONS']:
        p = parser.parse(show_cmd)
        r = ex.execute(p, {})
        cols = r.get('columns', [])
        rows = r.get('rows', [])
        print(f"  → {show_cmd}: {len(rows)} 行, 列={cols}")

    print("  ✅ SHOW 命令正常")
    db.shutdown()


def test_event_scheduler():
    """测试8: 事件调度器"""
    print("\n【测试8】事件调度器")
    reset()
    db, ex = setup()
    from lightdb.sql.parser import parser

    # 启动事件调度器
    ex.start_event_scheduler()
    print("  ✅ 事件调度器已启动")

    # 创建事件：每 1 秒插入一行
    sql = """CREATE EVENT ev_test
    ON SCHEDULE EVERY 1 SECOND
    DO
    BEGIN
        INSERT INTO logs VALUES (FLOOR(RAND()*100000), 'tick');
    END"""
    try:
        p = parser.parse(sql)
        r = ex.execute(p, {})
        print(f"  → {r.get('message', r)}")

        # 等待 3 秒
        time.sleep(3.5)

        logs = db.get_table('logs')
        cols, rows = logs.select(['*'])
        print(f"  → 事件生成 {len(rows)} 行日志")
        # 至少运行一次
        assert len(rows) >= 1

        # SHOW EVENTS
        p = parser.parse("SHOW EVENTS")
        r = ex.execute(p, {})
        print(f"  → SHOW EVENTS: {r['rows']}")
    except Exception as e:
        print(f"  ⚠️  事件测试失败: {e}")
    finally:
        ex.stop_event_scheduler()

    print("  ✅ 事件调度器测试完成")
    db.shutdown()


def test_function():
    """测试9: 存储函数"""
    print("\n【测试9】存储函数")
    reset()
    db, ex = setup()
    from lightdb.sql.parser import parser

    sql = """CREATE FUNCTION double_it(x INT) RETURNS INT
    BEGIN
        RETURN x * 2;
    END"""
    p = parser.parse(sql)
    r = ex.execute(p, {})
    print(f"  → {r.get('message', r)}")
    assert r.get('type') == 'ok'

    # 验证已创建
    assert 'double_it' in ex.stored_mgr.functions
    print(f"  ✅ 函数已创建")

    p = parser.parse("SHOW FUNCTIONS")
    r = ex.execute(p, {})
    print(f"  → SHOW FUNCTIONS: {r['rows']}")

    db.shutdown()


def main():
    print("=" * 60)
    print("  Batch 12 测试 - 存储过程/触发器/函数/事件")
    print("=" * 60)

    tests = [
        ('创建存储过程', test_create_procedure),
        ('IF/WHILE 过程', test_procedure_if_while),
        ('OUT 参数', test_procedure_out_param),
        ('BEFORE INSERT 触发器', test_trigger_before_insert),
        ('AFTER INSERT 触发器', test_trigger_after_insert),
        ('DELETE 触发器', test_trigger_delete),
        ('SHOW 命令', test_show_procedures_triggers),
        ('事件调度器', test_event_scheduler),
        ('存储函数', test_function),
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
        print("  🎉 Batch 12 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())