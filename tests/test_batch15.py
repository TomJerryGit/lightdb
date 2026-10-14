"""Batch 15 测试 - 安全 + 运维 + 基准 + v2.0 发布"""
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch15.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b15')
PORT = 8905


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port, **cfg_overrides):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    return srv, db, ex


def test_password_policy():
    """测试1: 密码策略"""
    print("\n【测试1】密码策略")
    from lightdb.security.password_policy import PasswordPolicy

    pp = PasswordPolicy(min_length=8, require_digit=True,
                        require_upper=True, deny_weak=True)

    ok, err = pp.validate('123')
    assert not ok
    print(f"  ✅ 短密码拒绝: {err}")

    ok, err = pp.validate('password')
    assert not ok
    print(f"  ✅ 弱密码拒绝: {err}")

    ok, err = pp.validate('abcdefgh')
    assert not ok
    print(f"  ✅ 无数字拒绝: {err}")

    ok, err = pp.validate('Abcd1234')
    assert ok
    print(f"  ✅ 强密码通过")

    print(f"  → 强度评分 'Abcd1234' = {pp.strength('Abcd1234')}")
    print(f"  → 强度评分 '123456'   = {pp.strength('123456')}")


def test_connection_limiter():
    """测试2: 连接限流"""
    print("\n【测试2】连接限流")
    from lightdb.security.connection_limit import ConnectionLimiter

    lim = ConnectionLimiter(max_per_ip=3, max_failed=3,
                            ban_seconds=2, window_seconds=10)

    # 连接数限制
    for _ in range(3):
        ok, err = lim.check_connection('10.0.0.1')
        assert ok
        lim.on_connect('10.0.0.1')

    ok, err = lim.check_connection('10.0.0.1')
    assert not ok
    print(f"  ✅ 第4个连接被拒: {err}")

    # 释放
    for _ in range(3):
        lim.on_disconnect('10.0.0.1')
    ok, err = lim.check_connection('10.0.0.1')
    assert ok
    print("  ✅ 释放后允许")

    # 登录失败
    for i in range(3):
        banned = lim.record_login_failure('10.0.0.2')
    print(f"  → 3次失败后封禁: {banned}")
    assert banned

    ok, err = lim.check_connection('10.0.0.2')
    assert not ok
    print(f"  ✅ 封禁生效: {err}")

    # 解封
    lim.unban('10.0.0.2')
    ok, err = lim.check_connection('10.0.0.2')
    assert ok
    print("  ✅ 解封成功")


def test_ip_whitelist():
    """测试3: IP 白名单"""
    print("\n【测试3】IP 白名单")
    from lightdb.security.ip_whitelist import IPWhitelist

    wl = IPWhitelist(enabled=True, entries=['127.0.0.1', '192.168.1.0/24'])

    assert wl.allow('127.0.0.1')
    print("  ✅ 127.0.0.1 通过")

    assert wl.allow('192.168.1.100')
    print("  ✅ 192.168.1.100 通过 (CIDR)")

    assert not wl.allow('10.0.0.1')
    print("  ✅ 10.0.0.1 拒绝")

    wl.add('10.0.0.0/8')
    assert wl.allow('10.0.0.1')
    print("  ✅ 添加 10.0.0.0/8 后允许")

    print(f"  → 白名单: {wl.list_all()}")


def test_explain_analyze():
    """测试4: EXPLAIN ANALYZE"""
    print("\n【测试4】EXPLAIN ANALYZE")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR)")
    for i in range(100):
        cur.execute(f"INSERT INTO t VALUES ({i+1}, 'v{i}')")

    cur.execute("EXPLAIN ANALYZE SELECT * FROM t WHERE id = 50")
    rows = cur.fetchall()
    print("  → EXPLAIN ANALYZE 输出:")
    for r in rows[:8]:
        print(f"    {r[0]}")
    assert any('实际耗时' in r[0] for r in rows)
    print("  ✅ EXPLAIN ANALYZE 生效")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.2)


def test_check_optimize():
    """测试5: CHECK TABLE / OPTIMIZE TABLE"""
    print("\n【测试5】CHECK / OPTIMIZE TABLE")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR)")
    for i in range(50):
        cur.execute(f"INSERT INTO t VALUES ({i+1}, 'v{i}')")

    cur.execute("CHECK TABLE t")
    cols = [d[0] for d in cur.description]
    r = cur.fetchone()
    print(f"  → CHECK TABLE: {r}")
    # MySQL 原生列序 (Table, Op, Msg_type, Msg_text)：Op='check' 在第 2 列
    assert cols == ['Table', 'Op', 'Msg_type', 'Msg_text'], cols
    assert r[0] == 't' and r[1] == 'check', r
    assert r[2] == 'status' and r[3] == 'OK', r
    # 第二行是真实校验统计（Msg_type='info'，Msg_text 是人可读文本）
    info = cur.fetchone()
    print(f"  → CHECK TABLE info: {info}")
    assert info[1] == 'check' and info[2] == 'info', info
    assert '50' in str(info[3]), info        # 50 个索引项真的走了一遍

    cur.execute("OPTIMIZE TABLE t")
    r = cur.fetchone()
    print(f"  → OPTIMIZE TABLE: {r}")
    assert r[1] == 'optimize' and r[2] == 'status' and r[3] == 'OK', r
    info = cur.fetchone()
    assert info[1] == 'optimize' and info[2] == 'info', info
    assert str(info[3]).strip(), info

    # 验证数据还在
    cur.execute("SELECT COUNT(*) FROM t")
    assert cur.fetchone()[0] == 50
    print("  ✅ 数据完整")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.2)


def test_show_engine():
    """测试6: SHOW ENGINE"""
    print("\n【测试6】SHOW ENGINE")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    cur.execute("SHOW ENGINE INNODB")
    rows = cur.fetchall()
    print("  → SHOW ENGINE INNODB:")
    for r in rows[:6]:
        print(f"    {r[0]}")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.2)


def test_security_via_sql():
    """测试7: 安全命令 via SQL"""
    print("\n【测试7】安全命令")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    # 密码策略
    cur.execute("SET PASSWORD POLICY min_length = 8")
    print("  ✅ 修改密码策略")

    # 尝试创建弱密码用户
    try:
        cur.execute("CREATE USER 'weak' IDENTIFIED BY '123'")
        print("  ❌ 弱密码未被拒绝")
        assert False
    except Exception as e:
        print(f"  ✅ 弱密码拒绝: {str(e)[:50]}")

    # 强密码
    cur.execute("CREATE USER 'strong' IDENTIFIED BY 'Strong123'")
    print("  ✅ 强密码创建成功")

    # IP 白名单
    cur.execute("ADD IP WHITELIST 10.0.0.0/8")
    cur.execute("ADD IP WHITELIST 192.168.1.1")
    cur.execute("SHOW IP WHITELIST")
    rows = cur.fetchall()
    print(f"  → 白名单: {rows}")

    # SHOW SECURITY
    cur.execute("SHOW SECURITY")
    rows = cur.fetchall()
    print("  → SHOW SECURITY:")
    for r in rows[:8]:
        print(f"    {r[0]} = {r[1]}")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.2)


def test_benchmark():
    """测试8: 性能基准"""
    print("\n【测试8】性能基准")
    reset()
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.performance.benchmark import BenchmarkRunner, format_results

    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    runner = BenchmarkRunner(db, ex)

    results = runner.run_all(n_rows=2000)
    print(format_results(results))

    assert results['batch_insert']['rows'] == 2000
    assert results['point_select']['qps'] > 100
    print("  ✅ 基准通过")

    db.shutdown()


def test_full_sql_suite():
    """测试9: v2.0 全特性抽查"""
    print("\n【测试9】v2.0 全特性抽查")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 charset='utf8mb4', autocommit=True))
    cur = conn.cursor()

    # 1. 基础
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR, age INT)")
    cur.execute("INSERT INTO t VALUES (1, 'Alice', 30)")
    cur.execute("INSERT INTO t VALUES (2, 'Bob', 25)")
    print("  ✅ 基础 SQL")

    # 2. 窗口函数
    cur.execute("SELECT name, ROW_NUMBER() OVER (ORDER BY age DESC) AS rn FROM t")
    rows = cur.fetchall()
    assert rows[0][1] == 1
    print("  ✅ 窗口函数")

    # 3. CTE
    cur.execute("WITH adults AS (SELECT * FROM t WHERE age >= 18) SELECT * FROM adults")
    assert len(cur.fetchall()) == 2
    print("  ✅ CTE")

    # 4. JSON
    cur.execute("CREATE TABLE docs (id INT PRIMARY KEY, data JSON)")
    cur.execute("INSERT INTO docs VALUES (1, '{\"k\": \"v\"}')")
    cur.execute("SELECT * FROM docs WHERE data->'$.k' = 'v'")
    assert len(cur.fetchall()) == 1
    print("  ✅ JSON")

    # 5. 全文
    cur.execute("CREATE TABLE arts (id INT PRIMARY KEY, content TEXT)")
    cur.execute("INSERT INTO arts VALUES (1, 'Python is great')")
    cur.execute("CREATE FULLTEXT INDEX ft ON arts (content)")
    cur.execute("SELECT * FROM arts WHERE MATCH(content) AGAINST('python')")
    assert len(cur.fetchall()) == 1
    print("  ✅ 全文索引")

    # 6. 存储过程
    cur.execute("""CREATE PROCEDURE p1(IN x INT)
    BEGIN
        INSERT INTO t VALUES (x, 'proc', 99);
    END""")
    cur.execute("CALL p1(99)")
    cur.execute("SELECT * FROM t WHERE id = 99")
    assert len(cur.fetchall()) == 1
    print("  ✅ 存储过程")

    # 7. 触发器
    cur.execute("CREATE TABLE log_t (id INT PRIMARY KEY, msg VARCHAR)")
    cur.execute("""CREATE TRIGGER trg1 AFTER INSERT ON t FOR EACH ROW
    BEGIN
        INSERT INTO log_t VALUES (NEW.id, 'inserted');
    END""")
    cur.execute("INSERT INTO t VALUES (100, 'trg_test', 40)")
    cur.execute("SELECT * FROM log_t WHERE id = 100")
    assert len(cur.fetchall()) == 1
    print("  ✅ 触发器")

    # 8. 分区
    cur.execute("CREATE TABLE parted (id INT PRIMARY KEY, v VARCHAR) PARTITION BY HASH(id) PARTITIONS 4")
    for i in range(20):
        cur.execute(f"INSERT INTO parted VALUES ({i+1}, 'v{i}')")
    cur.execute("SHOW PARTITIONS FROM parted")
    assert len(cur.fetchall()) == 4
    print("  ✅ 分区表")

    # 9. 生成列
    cur.execute("""CREATE TABLE gen (id INT PRIMARY KEY, a VARCHAR,
        b VARCHAR GENERATED ALWAYS AS (UPPER(a)) STORED)""")
    cur.execute("INSERT INTO gen (id, a) VALUES (1, 'hi')")
    cur.execute("SELECT b FROM gen WHERE id = 1")
    assert cur.fetchone()[0] == 'HI'
    print("  ✅ 生成列")

    # 10. EXPLAIN ANALYZE
    cur.execute("EXPLAIN ANALYZE SELECT * FROM t WHERE id = 1")
    assert len(cur.fetchall()) > 0
    print("  ✅ EXPLAIN ANALYZE")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.2)


def main():
    print("=" * 60)
    print("  Batch 15 测试 - v2.0 收官")
    print("=" * 60)

    tests = [
        ('密码策略', test_password_policy),
        ('连接限流', test_connection_limiter),
        ('IP白名单', test_ip_whitelist),
        ('EXPLAIN ANALYZE', test_explain_analyze),
        ('CHECK/OPTIMIZE', test_check_optimize),
        ('SHOW ENGINE', test_show_engine),
        ('安全命令', test_security_via_sql),
        ('性能基准', test_benchmark),
        ('全特性抽查', test_full_sql_suite),
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
        print("  🎉 Batch 15 全部通过！")
        print("  🚀 LightDB v2.0 正式发布！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())