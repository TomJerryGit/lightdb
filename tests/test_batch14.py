"""Batch 14 测试 - 半同步（已移除）/ SSL / 并行 / 审计"""
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


def start_server(port, ssl=False, audit_level='DDL'):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.ssl_enabled = ssl
    cfg.audit_level = audit_level
    if ssl:
        # 服务端 TLS 已接线（2026-10-21 P2 安全项）：显式给出证书路径，
        # 避免依赖 <data_dir>/ssl 或环境里的 openssl CLI 路径。
        from lightdb.security.ssl_support import SSLManager
        mgr = SSLManager(os.path.join(DATA_DIR, 'ssl'))
        assert mgr.ensure_certificates(), 'SSL 证书生成失败'
        cfg.ssl_cert = str(mgr.cert_file)
        cfg.ssl_key = str(mgr.key_file)
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    return srv, db, ex


def test_semi_sync_removed():
    """测试1: 半同步已移除（决策 D 阶段 2 —— 不实现）"""
    print("\n【测试1】半同步已移除（决策：不实现）")
    import lightdb.replication as rep_pkg
    # 导出已清理：包上不再有半同步类
    assert not hasattr(rep_pkg, 'SemiSyncMaster')
    assert not hasattr(rep_pkg, 'SemiSyncSlave')
    # 历史模块已物理删除：导入会显式报 ImportError（不是悄悄给空壳）
    with pytest.raises(ImportError):
        from lightdb.replication import semi_sync  # noqa: F401
    # 配置项已删除（不再有"配了却没人用"的半同步开关）
    from lightdb.config import Config
    assert not hasattr(Config(), 'semi_sync_enabled')
    print("  ✅ 半同步死代码/导出/配置项均已移除，包导入无残留")


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

    # 服务端 SSL 启用 + PyMySQL SSL 端到端
    # 2026-10-21：服务端 TLS 已接线（CLIENT_SSL/SSLRequest → wrap socket →
    # 继续认证）；这里恢复端到端断言。本机没有 openssl CLI 时上面已提前返回，
    # 有 openssl 的环境（含 CI）必须真实通过，不得再用 skip 掩盖。
    srv, db, ex = start_server(PORT, ssl=True)
    print(f"  → 服务端 SSL: {srv.ssl_enabled}")
    assert srv.ssl_enabled
    assert srv.ssl_context is not None

    import pymysql
    conn = track(pymysql.connect(
        host='127.0.0.1', port=PORT, user='root', autocommit=True,
        ssl={'check_hostname': False}
    ))
    cur = conn.cursor()
    cur.execute("SELECT 1")
    r = cur.fetchone()
    print(f"  → SSL 查询: {r}")
    assert r == (1,)
    conn.close()
    print("  ✅ SSL 连接成功")

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

    # 本用例验证的是旧 `Executor.parallel` 执行器的统计；OLAP dispatch 自
    # 2026-10-20 起默认开启，会先接管大表聚合（结果相同但 parallel 统计为 0）。
    # 这里显式关闭 dispatch，保留原用例对 parallel 路径的断言。
    ex.olap_dispatch = False

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
    assert not hasattr(cfg, 'semi_sync_enabled')   # 决策 D 阶段 2：已移除
    assert hasattr(cfg, 'parallel_workers')
    print(f"  → ssl_enabled = {cfg.ssl_enabled}")
    print(f"  → audit_level = {cfg.audit_level}")
    print(f"  → parallel_workers = {cfg.parallel_workers}")
    print("  ✅ 配置字段就绪（半同步配置项已按决策移除）")


def main():
    print("=" * 60)
    print("  Batch 14 测试 - 半同步（已移除）/ SSL / 并行 / 审计")
    print("=" * 60)

    tests = [
        ('半同步已移除', test_semi_sync_removed),
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