"""Batch 9 测试 - 权限 + Prepared Statement + 工具"""
import os
import pytest
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch9.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b9')
PORT = 8895


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    return srv, db


def test_privileges():
    """测试1: 权限系统"""
    print("\n【测试1】权限系统")
    reset()
    from lightdb.security.privileges import PrivilegeManager

    pm = PrivilegeManager(DATA_DIR)
    assert 'root' in pm.users

    # 创建用户
    pm.create_user('alice', 'pass123')
    print("  ✅ 创建 alice")

    # 认证
    assert pm.authenticate('alice', 'pass123')
    assert not pm.authenticate('alice', 'wrong')
    print("  ✅ 密码认证正确")

    # 授权
    n = pm.grant('alice', ['SELECT', 'INSERT'])
    print(f"  ✅ 授予 alice {n} 个权限")

    assert pm.check('alice', 'SELECT')
    assert pm.check('alice', 'INSERT')
    assert not pm.check('alice', 'DELETE')
    print("  ✅ 权限检查正确")

    # 回收
    pm.revoke('alice', ['INSERT'])
    assert not pm.check('alice', 'INSERT')
    print("  ✅ REVOKE 生效")

    # SHOW GRANTS
    grants = pm.show_grants('alice')
    print(f"  → alice 权限: {grants}")


def test_grant_via_sql():
    """测试2: SQL 层用户管理（GRANT/REVOKE/SHOW GRANTS 已接线）"""
    print("\n【测试2】SQL 层用户管理")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    # CREATE USER
    cur.execute("CREATE USER 'bob' IDENTIFIED BY 'bobpass'")
    print("  ✅ CREATE USER bob")

    # GRANT
    cur.execute("GRANT SELECT, INSERT ON *.* TO 'bob'")
    print("  ✅ GRANT")

    # SHOW GRANTS（结果集；root 可查任意账户）
    cur.execute("SHOW GRANTS FOR 'bob'")
    grants = cur.fetchall()
    print(f"  → bob 的权限: {[g[0] for g in grants]}")
    assert any('SELECT' in g[0] for g in grants), grants
    assert any('INSERT' in g[0] for g in grants), grants

    # 用户表里真实存在 bob（本引擎没有 MySQL 之外的 `SHOW USERS` 语法，
    # 原断言用它；这里改用同一份 Process 内数据库对象验证，语义等价）
    assert 'bob' in db.users
    assert set(db.users['bob']['privs']) >= {'SELECT', 'INSERT'}
    print(f"  → 所有用户: {sorted(db.users)}")

    # REVOKE
    cur.execute("REVOKE INSERT ON *.* FROM 'bob'")
    print("  ✅ REVOKE")
    assert 'INSERT' not in db.users['bob']['privs']
    assert 'SELECT' in db.users['bob']['privs']

    # DROP USER
    cur.execute("DROP USER 'bob'")
    print("  ✅ DROP USER")
    assert 'bob' not in db.users

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_privilege_enforcement():
    """测试3: 权限实际生效（登录身份来自握手，执行期鉴权在分发前）"""
    print("\n【测试3】权限实际生效")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    # root 创建表
    root_conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                      autocommit=True))
    rcur = root_conn.cursor()
    rcur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(50))")
    rcur.execute("INSERT INTO t VALUES (1, 'a')")
    # 口令策略拒绝空口令（validate_password 语义），改用一个合规口令
    rcur.execute("CREATE USER 'readonly' IDENTIFIED BY 'Readonly123'")
    rcur.execute("GRANT SELECT ON *.* TO 'readonly'")
    print("  ✅ root 建表 + 创建 readonly 用户")

    # readonly 只能SELECT
    ro = track(pymysql.connect(host='127.0.0.1', port=PORT, user='readonly',
                               password='Readonly123', autocommit=True))
    rc = ro.cursor()

    rc.execute("SELECT * FROM t")
    rows = rc.fetchall()
    print(f"  ✅ readonly SELECT: {rows}")
    assert len(rows) == 1

    # INSERT 应该被拒绝（MySQL 语义 1142：INSERT command denied ... for table）
    try:
        rc.execute("INSERT INTO t VALUES (2, 'b')")
        print("  ❌ INSERT 未被拒绝！")
        assert False
    except pymysql.err.OperationalError as e:
        print(f"  ✅ INSERT 被拒绝: {str(e)[:80]}")
        assert e.args[0] == 1142, e
        assert 'INSERT command denied' in str(e)

    # UPDATE 同样被拒绝
    try:
        rc.execute("UPDATE t SET v = 'x' WHERE id = 1")
        assert False, "UPDATE 未被拒绝"
    except pymysql.err.OperationalError as e:
        assert e.args[0] == 1142, e

    ro.close()
    root_conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_prepared_statement():
    """测试4: Prepared Statement"""
    print("\n【测试4】Prepared Statement")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(50), age INT)")
    print("  ✅ 建表")

    # PyMySQL 的 execute(sql, args) 会使用 Prepared Statement
    for i in range(5):
        cur.execute("INSERT INTO t VALUES (%s, %s, %s)", (i + 1, f'user{i}', 20 + i))
    print("  ✅ 参数化插入 5 行")

    cur.execute("SELECT * FROM t WHERE id = %s", (3,))
    row = cur.fetchone()
    print(f"  → 参数化查询: {row}")
    assert row == (3, 'user2', 22)

    cur.execute("SELECT * FROM t WHERE age > %s", (21,))
    rows = cur.fetchall()
    print(f"  → age > 21: {len(rows)} 行")
    assert len(rows) == 3

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_processlist():
    """测试5: SHOW PROCESSLIST + KILL"""
    print("\n【测试5】SHOW PROCESSLIST")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()

    cur.execute("SHOW PROCESSLIST")
    rows = cur.fetchall()
    print(f"  → 活动连接数: {len(rows)}")
    for r in rows[:3]:
        print(f"    Id={r[0]} User={r[1]} Db={r[3]} Command={r[4]}")
    assert len(rows) >= 1

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_cli_tool():
    """测试6: lightdb CLI -e 模式"""
    print("\n【测试6】lightdb CLI")
    reset()
    srv, db = start_server(PORT)

    import subprocess
    result = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.cli',
         '-h', '127.0.0.1', '-P', str(PORT), '-u', 'root',
         '-e', "SELECT 1"],
        capture_output=True, text=True, timeout=15,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    print(f"  → stdout: {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"  → stderr: {result.stderr.strip()}")
    assert result.returncode == 0
    assert '(1,)' in result.stdout

    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_dump_tool():
    """测试7: lightdbdump"""
    print("\n【测试7】lightdbdump")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                 autocommit=True))
    cur = conn.cursor()
    cur.execute("CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(50))")
    for i in range(5):
        cur.execute(f"INSERT INTO users VALUES ({i+1}, 'u{i}')")
    conn.close()

    import subprocess
    dump_file = os.path.join(DATA_DIR, 'dump.sql')
    result = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.dump',
         '-h', '127.0.0.1', '-P', str(PORT), '-u', 'root',
         '-o', dump_file],
        capture_output=True, text=True, timeout=15,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if result.returncode != 0:
        print(f"  → stderr: {result.stderr.strip()}")
    assert result.returncode == 0
    assert os.path.exists(dump_file)

    with open(dump_file, 'r', encoding='utf-8') as f:
        content = f.read()
    print(f"  → dump 文件大小: {len(content)} 字节")
    assert 'CREATE TABLE users' in content
    assert content.count('INSERT INTO users') == 5
    print("  ✅ dump 内容正确")

    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_admin_tool():
    """测试8: lightdbadmin"""
    print("\n【测试8】lightdbadmin")
    reset()
    srv, db = start_server(PORT)

    import subprocess

    # ping
    r = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.admin',
         '-h', '127.0.0.1', '-P', str(PORT), '-u', 'root',
         'ping'],
        capture_output=True, text=True, timeout=15,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    print(f"  → ping: {r.stdout.strip()}")
    assert 'alive' in r.stdout

    # version
    r = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.admin',
         '-h', '127.0.0.1', '-P', str(PORT), '-u', 'root',
         'version'],
        capture_output=True, text=True, timeout=15,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    print(f"  → version: {r.stdout.strip()}")
    assert '5.7' in r.stdout

    # status
    r = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.admin',
         '-h', '127.0.0.1', '-P', str(PORT), '-u', 'root',
         'status'],
        capture_output=True, text=True, timeout=15,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    print(f"  → status: {r.stdout.strip()[:80]}")

    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def main():
    print("=" * 60)
    print("  Batch 9 测试 - 权限 + PS + 工具")
    print("=" * 60)

    tests = [
        ('权限系统', test_privileges),
        ('SQL用户管理', test_grant_via_sql),
        ('权限生效', test_privilege_enforcement),
        ('Prepared Statement', test_prepared_statement),
        ('Processlist', test_processlist),
        ('CLI工具', test_cli_tool),
        ('Dump工具', test_dump_tool),
        ('Admin工具', test_admin_tool),
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
        print("  🎉 Batch 9 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())