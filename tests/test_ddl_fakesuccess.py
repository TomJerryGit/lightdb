"""DDL 假成功（P1 修复 F）：非整型主键 + 触发器坏体

修复前的缺陷（见 docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md §F）：

* **F1**：`CREATE TABLE vp (name VARCHAR(20) PRIMARY KEY, n INT)` 返回 OK，
  但该表之后**任何 INSERT 永久 1105**、SELECT 空集 —— 主键 B+Tree 的键按
  uint32 落盘，字符串主键在 `struct.pack` 处失败。定义的"成功"是假的。
* **F2**：`CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET @x=1`
  返回 OK，之后该表任何写入都在触发路径解析失败 → 永久 1105；且旧版没有
  `DROP TRIGGER` 语法，用户无法用 SQL 自救。

本文件锁定修复后的契约：

1. **定义期**拒绝非整型主键（1064，报文含「主键仅支持整数类型」），
   且表**根本没有被创建**；整数主键（INT/BIGINT/表级 `PRIMARY KEY (id)`）
   完全不受影响；
2. 坏体触发器 `CREATE` 显式失败（1064 / 引用不存在的列 1054）、**不写入**
  （`SHOW TRIGGERS` 为空），且该表此后仍可正常写入；
3. 合法触发器（`SET NEW.列` / `IF ... THEN ... END IF` / 普通 SQL 体）
   仍能创建、真实 fire；`DROP TRIGGER [IF EXISTS]` 与 `SHOW TRIGGERS`
   正确反映；
4. 全部断言走真实的 `Executor.execute_sql`。

修复前存在、修复后仍保留的边界：**没有声明主键**时引擎仍把第一列当内部
行键（隐式实现细节，不属于用户声明的主键），因此本文件只断言"显式声明
的非整型主键"被拒绝。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_ddl_fakesuccess.py
    from conftest import track


# ==================== 公共工具 ====================

class Env:
    """独立数据目录 + Database/Executor（真实 SQL 执行路径）"""

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

    def err(self, statement, code=None, session=None):
        r = self.run(statement, session)
        assert r.get('type') == 'error', (
            f'应当显式失败却成功了: {statement} -> {r}')
        if code is not None:
            assert r.get('code') == code, (
                f'{statement} 的 errno 应为 {code}，实际 {r}')
        return r

    def rs(self, statement, session=None):
        r = self.run(statement, session)
        assert r.get('type') == 'resultset', (statement, r)
        return r

    def rows(self, statement, session=None):
        return self.rs(statement, session)['rows']

    def triggers(self, session=None):
        r = self.rs('SHOW TRIGGERS', session)
        return [dict(zip(r['columns'], row)) for row in r['rows']]


# ==================== F1：非整型主键定义期拒绝 ====================

def test_varchar_primary_key_rejected_at_definition_time(tmp_path):
    """`VARCHAR PRIMARY KEY` → 定义期 1064，且表不存在（不再假成功）"""
    with Env(tmp_path / 'data') as e:
        r = e.err('CREATE TABLE vp (name VARCHAR(20) PRIMARY KEY, n INT)',
                  code=1064)
        assert '主键仅支持整数类型' in r['message'], r
        assert 'VARCHAR' in r['message'] and 'UNIQUE' in r['message'], r

        # 表根本没有被创建：SHOW TABLES 里没有，SHOW COLUMNS 报 1146
        assert 'vp' not in [row[0] for row in e.rows('SHOW TABLES')]
        e.err('SHOW COLUMNS FROM vp', code=1146)
        # 也没有留下半套写入痕迹：后续建同名整数主键表可以成功
        e.ok('CREATE TABLE vp (id INT PRIMARY KEY, n INT)')
        e.ok('INSERT INTO vp VALUES (1, 10)')
        assert e.rows('SELECT * FROM vp') == [(1, 10)]
        print("  → VARCHAR PRIMARY KEY 定义期 1064，表不存在，整数主键同名表可建")


def test_table_level_and_other_non_integer_primary_keys_rejected(tmp_path):
    """表级 `PRIMARY KEY (name)` / TEXT / JSON 主键同样定义期 1064"""
    with Env(tmp_path / 'data') as e:
        for ddl in (
                'CREATE TABLE t1 (name VARCHAR(20), n INT, PRIMARY KEY (name))',
                'CREATE TABLE t2 (note TEXT PRIMARY KEY, n INT)',
                'CREATE TABLE t3 (doc JSON PRIMARY KEY, n INT)',
                'CREATE TABLE t4 (price DECIMAL(10,2) PRIMARY KEY, n INT)'):
            r = e.err(ddl, code=1064)
            assert '主键仅支持整数类型' in r['message'], (ddl, r)
        assert [row[0] for row in e.rows('SHOW TABLES')] == []
        print('  → 表级/TEXT/JSON/DECIMAL 主键均定义期 1064，0 张表被创建')


def test_integer_primary_keys_unaffected(tmp_path):
    """INT / BIGINT / 表级 PRIMARY KEY (INT 列) 完全不受 F1 影响"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE a (id INT PRIMARY KEY, name VARCHAR(20))')
        e.ok("INSERT INTO a VALUES (1, 'x')")
        e.ok("INSERT INTO a VALUES (2147483647, 'y')")

        e.ok('CREATE TABLE b (id BIGINT PRIMARY KEY, v INT)')
        # 注：BIGINT 列声明本身允许（F1 只拒绝非整型主键）；但主键 B+Tree
        # 的键按 uint32 落盘，本用例只验证"声明与普通取值"不受 F1 影响
        # （> 4294967295 的 BIGINT 主键仍受既有存储限制，见交付说明）。
        e.ok('INSERT INTO b VALUES (100, 1)')

        e.ok('CREATE TABLE c (id INT, v INT, PRIMARY KEY (id))')
        e.ok('INSERT INTO c VALUES (7, 7)')
        # 主键约束本身仍真实生效（重复插入 1062）
        e.err('INSERT INTO a VALUES (1, \'dup\')', code=1062)
        assert e.rows('SELECT id, name FROM a ORDER BY id') == [
            (1, 'x'), (2147483647, 'y')]
        assert e.rows('SELECT * FROM c') == [(7, 7)]
        print('  → INT/BIGINT/表级整数主键正常，主键重复仍 1062')


# ==================== F2：触发器坏体不再锁表 ====================

def test_bad_trigger_body_rejected_and_table_stays_writable(tmp_path):
    """坏体 `CREATE TRIGGER` → 定义期失败、不写入、表之后仍可写"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        e.ok('CREATE TABLE logs (id INT PRIMARY KEY, msg VARCHAR(50))')

        # 分析报告里的原始坏体：SET @x=1（PL 词法器会吃掉 @，运行期才炸）
        r = e.err('CREATE TRIGGER bad BEFORE INSERT ON t FOR EACH ROW '
                  'SET @x=1', code=1064)
        assert '触发器体' in r['message'], r
        # IF 缺少 END IF / 引用不存在的列 同样拒绝
        e.err('CREATE TRIGGER bad2 BEFORE INSERT ON t FOR EACH ROW '
              'BEGIN IF NEW.v IS NULL THEN SET NEW.v = 1; END', code=1064)
        r3 = e.err('CREATE TRIGGER bad3 BEFORE INSERT ON t FOR EACH ROW '
                   'SET NEW.nope = 1', code=1054)
        assert 'nope' in r3['message'], r3
        # 执行期不存在的形态（SET @变量 / 无法解析的 SQL）也定义期拒绝
        e.err('CREATE TRIGGER bad4 BEFORE INSERT ON t FOR EACH ROW '
              'INSERT INTO logs VALUES (NEW.id', code=1064)

        # 一个坏触发器都没落库
        assert e.triggers() == []
        # 表之后仍可正常写入（这正是原缺陷："坏触发器把表锁死"）
        e.ok('INSERT INTO t VALUES (1, 10)')
        e.ok('INSERT INTO t (id, v) VALUES (2, 20)')
        e.ok('INSERT INTO logs VALUES (1, \'manual\')')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [(1, 10), (2, 20)]
        assert e.rows('SELECT * FROM logs') == [(1, 'manual')]
        print('  → 坏体触发器定义期 1064/1054，不落库，表仍可写')


def test_valid_trigger_create_fire_drop_and_show(tmp_path):
    """合法触发器 CREATE / fire / SHOW TRIGGERS / DROP TRIGGER 全链路"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        e.ok('CREATE TABLE logs (id INT PRIMARY KEY, msg VARCHAR(50))')

        # BEFORE：IF ... THEN SET NEW.v = 18; END IF（batch12 的既有合法形态）
        e.ok('CREATE TRIGGER fill_v BEFORE INSERT ON t FOR EACH ROW '
             'BEGIN IF NEW.v IS NULL THEN SET NEW.v = 18; END IF; END')
        # AFTER：普通 SQL 体（batch12/batch15 的既有合法形态）
        e.ok('CREATE TRIGGER log_ins AFTER INSERT ON t FOR EACH ROW '
             'BEGIN INSERT INTO logs VALUES (NEW.id, \'inserted\'); END')

        shown = e.triggers()
        assert [t['Trigger'] for t in shown] == ['fill_v', 'log_ins'], shown
        by_name = {t['Trigger']: t for t in shown}
        assert by_name['fill_v']['Timing'] == 'BEFORE'
        assert by_name['fill_v']['Event'] == 'INSERT'
        assert by_name['fill_v']['Table'] == 't'
        assert 'SET NEW.v' in by_name['fill_v']['Statement'], shown
        assert by_name['log_ins']['Timing'] == 'AFTER'
        assert 'INSERT INTO logs' in by_name['log_ins']['Statement'], shown

        # 真实 fire：NULL → BEFORE 改成 18；AFTER 写 logs
        e.ok('INSERT INTO t VALUES (1, NULL)')
        assert e.rows('SELECT id, v FROM t') == [(1, 18)]
        assert e.rows('SELECT * FROM logs') == [(1, 'inserted')]

        # DROP 后不再 fill；DROP IF EXISTS 不存在算成功；不存在时报 1360
        e.ok('DROP TRIGGER fill_v')
        e.ok('DROP TRIGGER IF EXISTS no_such_trigger')
        r = e.err('DROP TRIGGER no_such_trigger', code=1360)
        assert 'no_such_trigger' in r['message'], r
        e.ok('INSERT INTO t VALUES (2, NULL)')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [(1, 18), (2, None)]
        # 只剩 log_ins
        assert [t['Trigger'] for t in e.triggers()] == ['log_ins']
        e.ok('DROP TRIGGER log_ins')
        assert e.triggers() == []
        # 全部删除后继续写入正常
        e.ok('INSERT INTO t VALUES (3, 30)')
        assert e.rows('SELECT id, v FROM t ORDER BY id') == [
            (1, 18), (2, None), (3, 30)]
        print('  → 合法触发器 CREATE/fire/SHOW/DROP 全链路正确')


def test_after_trigger_cannot_assign_new_row_rejected(tmp_path):
    """`AFTER` 触发器体里 `SET NEW.列` → 定义期 1064（运行期只允许 BEFORE 改行）"""
    with Env(tmp_path / 'data') as e:
        e.ok('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        r = e.err('CREATE TRIGGER bad AFTER UPDATE ON t FOR EACH ROW '
                  'SET NEW.v = 1', code=1064)
        assert 'BEFORE' in r['message'], r
        assert e.triggers() == []
        e.ok('INSERT INTO t VALUES (1, 1)')
        e.ok('UPDATE t SET v = 2 WHERE id = 1')
        assert e.rows('SELECT * FROM t') == [(1, 2)]
        print('  → AFTER 触发器 SET NEW 被定义期拒绝，表不受影响')
