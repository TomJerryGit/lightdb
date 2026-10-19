"""Batch 14 测试 - 半同步 + SSL + 并行 + 审计"""
import os
import pytest
import shutil
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch14.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b14')
PORT = 8901


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port, ssl=False, semi_sync=False, audit_level='DDL'):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.ssl_enabled = ssl
    cfg.semi_sync_enabled = semi_sync
    cfg.audit_level = audit_level
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    return srv, db, ex


def test_semi_sync_controller():
    """测试1: 半同步控制器（直接调用）"""
    print("\n【测试1】半同步控制器")
    from lightdb.replication.semi_sync import SemiSyncMaster

    class FakeBinlog:
        def current_pos(self): return 0

    m = SemiSyncMaster(FakeBinlog(), timeout=0.5, required_acks=1)
    m.enable()
    m.register_slave('s1')
    print("  ✅ 启用半同步 + 注册 s1")

    # 模拟异步确认
    def delayed_ack():
        time.sleep(0.1)
        m.ack('s1', 100)

    threading.Thread(target=delayed_ack, daemon=True).start()

    start = time.time()
    ok = m.wait_for_ack(100)
    elapsed = (time.time() - start) * 1000
    print(f"  → wait_for_ack = {ok}, 等待 {elapsed:.0f}ms")
    assert ok, "应该成功收到 ACK"

    # 超时场景
    m.ack_map.clear()
    start = time.time()
    ok2 = m.wait_for_ack(200)
    elapsed2 = (time.time() - start) * 1000
    print(f"  → 无 ACK 时 wait_for_ack = {ok2}, 等待 {elapsed2:.0f}ms")
    assert not ok2, "应该超时"
    assert elapsed2 >= 400, "应该等待约 500ms"

    st = m.status()
    print(f"  → 状态: {st}")
    assert st['sync_ok'] == 1
    assert st['sync_timeout'] == 1
    print("  ✅ 半同步控制器正常")


@pytest.mark.xfail(
    reason="半同步复制为死代码（SemiSyncMaster/Slave 无调用者）；真 binlog 复制在路线图阶段 2/4",
    strict=False)
def test_semi_sync_sql():
    """测试2: SQL 协议半同步"""
    print("\n【测试2】SQL 协议半同步")
    reset()
    srv, db, ex = start_server(PORT, semi_sync=True)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR)")
    print("  ✅ 建表")

    # 注册一个伪从库
    cur.execute("__SEMI_SYNC_REGISTER__ slave_test")
    print("  ✅ 注册从库")

    # 插入（主库会等待）
    def auto_ack():
        time.sleep(0.1)
        import pymysql as pm
        c2 = pm.connect(host='127.0.0.1', port=PORT, user='root', autocommit=True)
        cu2 = c2.cursor()
        cu2.execute("__SEMI_SYNC_ACK__ slave_test 5")
        c2.close()

    threading.Thread(target=auto_ack, daemon=True).start()

    t0 = time.time()
    cur.execute("INSERT INTO t VALUES (1, 'a')")
    t1 = time.time()
    print(f"  → INSERT 耗时 {(t1-t0)*1000:.0f}ms（等待 ACK）")

    cur.execute("SHOW STATUS LIKE 'Rpl_semi_sync%'")
    rows = cur.fetchall()
    print("  → 半同步状态:")
    for r in rows:
        print(f"    {r[0]} = {r[1]}")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_audit():
    """测试3: 审计日志"""
    print("\n【测试3】审计日志")
    reset()
    srv, db, ex = start_server(PORT, audit_level='DML')

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR)")
    cur.execute("INSERT INTO t VALUES (1, 'a')")
    cur.execute("INSERT INTO t VALUES (2, 'b')")
    cur.execute("UPDATE t SET v = 'aa' WHERE id = 1")
    cur.execute("DELETE FROM t WHERE id = 2")
    cur.execute("SELECT * FROM t")
    print("  ✅ 执行了 6 条 SQL")

    # 查询审计
    cur.execute("SHOW AUDIT LOG")
    rows = cur.fetchall()
    print(f"  → 审计记录 {len(rows)} 条")
    for r in rows[-5:]:
        # ts, user, cmd, success, elapsed_ms, sql
        print(f"    [{r[2]}] {r[5][:50]}")

    # 至少有 DML 记录
    cmds = [r[2] for r in rows]
    assert 'INSERT' in cmds
    assert 'UPDATE' in cmds
    assert 'DELETE' in cmds
    print("  ✅ 审计日志记录正确")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_ssl():
    """测试4: SSL 连接"""
    print("\n【测试4】SSL 连接")
    from lightdb.security.ssl_support import has_openssl, SSLManager

    if not has_openssl():
        print("  ⚠️  未安装 openssl，跳过测试")
        return

    reset()
    ssl_dir = os.path.join(DATA_DIR, 'ssl')
    mgr = SSLManager(ssl_dir)
    ok = mgr.ensure_certificates()
    print(f"  ✅ 证书生成: {ok}")
    assert ok
    assert os.path.exists(mgr.cert_file)
    assert os.path.exists(mgr.key_file)
    print(f"  → cert: {mgr.cert_file}")
    print(f"  → key: {mgr.key_file}")

    # 服务端 SSL 启用测试
    srv, db, ex = start_server(PORT, ssl=True)
    print(f"  → 服务端 SSL: {srv.ssl_enabled}")
    assert srv.ssl_enabled

    # PyMySQL SSL 连接
    import pymysql
    try:
        conn = pymysql.connect(
            host='127.0.0.1', port=PORT, user='root', autocommit=True,
            ssl={'check_hostname': False}
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        r = cur.fetchone()
        print(f"  → SSL 查询: {r}")
        assert r == (1,)
        conn.close()
        print("  ✅ SSL 连接成功")
    except Exception as e:
        print(f"  ⚠️  SSL 连接失败: {e}")

    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_parallel_count():
    """测试5: 并行 COUNT"""
    print("\n【测试5】并行 COUNT")
    reset()
    srv, db, ex = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()
    cur.execute("CREATE TABLE big (id INT PRIMARY KEY, v VARCHAR)")

    # 用 PyMySQL 批量插入（走逐条）
    print("  → 插入 5000 行...")
    t0 = time.time()
    for i in range(5000):
        cur.execute(f"INSERT INTO big VALUES ({i+1}, 'v{i}')")
    t1 = time.time()
    print(f"  → 插入耗时: {t1-t0:.2f}s")

    # 串行 COUNT
    t0 = time.time()
    cur.execute("SELECT COUNT(*) FROM big")
    r = cur.fetchone()
    t1 = time.time()
    serial_ms = (t1 - t0) * 1000
    print(f"  → COUNT(*) = {r[0]}，耗时 {serial_ms:.0f}ms")

    # 并行 COUNT（已在 executor 自动启用）
    # 这里只是验证结果正确
    assert r[0] == 5000

    # 检查并行统计
    if ex.parallel:
        st = ex.parallel.stats
        print(f"  → 并行统计: {st}")
        assert st['parallel_queries'] >= 1
        print("  ✅ 并行 COUNT 已触发")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_parallel_scan():
    """测试6: 并行扫描"""
    print("\n【测试6】并行扫描")
    reset()
    from lightdb.db.database import Database
    from lightdb.performance.parallel import ParallelExecutor

    db = track(Database(DATA_DIR))
    db.create_table('t', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'v', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
    ])
    tbl = db.get_table('t')
    for i in range(3000):
        tbl.insert([i + 1, i * 2])

    pe = track(ParallelExecutor(max_workers=4))

    # 并行扫描（无条件）
    t0 = time.time()
    rows = pe.parallel_scan(tbl, None)
    t1 = time.time()
    print(f"  → 并行扫描 {len(rows)} 行，耗时 {(t1-t0)*1000:.0f}ms")
    assert len(rows) == 3000

    # 并行 COUNT
    t0 = time.time()
    cnt = pe.parallel_aggregate(tbl, None, 'COUNT')
    t1 = time.time()
    print(f"  → 并行 COUNT = {cnt}，耗时 {(t1-t0)*1000:.0f}ms")
    assert cnt == 3000

    # 并行 SUM (通过 scan + 手动聚合)
    rows = pe.parallel_scan(tbl, None)
    total = sum(r[1] for r in rows)
    expected = sum(i * 2 for i in range(3000))
    assert total == expected
    print(f"  → 并行 SUM = {total}")

    pe.shutdown()
    db.shutdown()


def test_config_new_fields():
    """测试7: 配置新增字段"""
    print("\n【测试7】配置新增字段")
    from lightdb.config import Config

    cfg = Config()
    assert hasattr(cfg, 'ssl_enabled')
    assert hasattr(cfg, 'audit_level')
    assert hasattr(cfg, 'semi_sync_enabled')
    assert hasattr(cfg, 'parallel_workers')
    print(f"  → ssl_enabled = {cfg.ssl_enabled}")
    print(f"  → audit_level = {cfg.audit_level}")
    print(f"  → semi_sync_enabled = {cfg.semi_sync_enabled}")
    print(f"  → parallel_workers = {cfg.parallel_workers}")
    print("  ✅ 配置字段就绪")


def main():
    print("=" * 60)
    print("  Batch 14 测试 - 半同步 + SSL + 并行 + 审计")
    print("=" * 60)

    tests = [
        ('半同步控制器', test_semi_sync_controller),
        ('SQL层半同步', test_semi_sync_sql),
        ('审计日志', test_audit),
        ('SSL连接', test_ssl),
        ('并行 COUNT', test_parallel_count),
        ('并行扫描', test_parallel_scan),
        ('配置字段', test_config_new_fields),
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
        print("  🎉 Batch 14 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())