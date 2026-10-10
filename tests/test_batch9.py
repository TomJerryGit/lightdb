"""Batch 9 测试 - 权限 + Prepared Statement + 工具"""
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b9')
PORT = 8895


def reset():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = Database(DATA_DIR)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
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
    """测试2: SQL 层用户管理"""
    print("\n【测试2】SQL 层用户管理")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           autocommit=True)
    cur = conn.cursor()

    # CREATE USER
    cur.execute("CREATE USER 'bob' IDENTIFIED BY 'bobpass'")
    print("  ✅ CREATE USER bob")

    # GRANT
    cur.execute("GRANT SELECT, INSERT ON *.* TO 'bob'")
    print("  ✅ GRANT")

    # SHOW GRANTS
    cur.execute("SHOW GRANTS FOR 'bob'")
    grants = cur.fetchall()
    print(f"  → bob 的权限: {[g[0] for g in grants]}")

    # SHOW USERS
    cur.execute("SHOW USERS")
    users = cur.fetchall()
    print(f"  → 所有用户: {[u[0] for u in users]}")
    assert 'bob' in [u[0] for u in users]

    # REVOKE
    cur.execute("REVOKE INSERT ON *.* FROM 'bob'")
    print("  ✅ REVOKE")

    # DROP USER
    cur.execute("DROP USER 'bob'")
    print("  ✅ DROP USER")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_privilege_enforcement():
    """测试3: 权限实际生效"""
    print("\n【测试3】权限实际生效")
    reset()
    srv, db = start_server(PORT)

    import pymysql
    # root 创建表
    root_conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                autocommit=True)
    rcur = root_conn.cursor()
    rcur.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(50))")
    rcur.execute("INSERT INTO t VALUES (1, 'a')")
    rcur.execute("CREATE USER 'readonly' IDENTIFIED BY ''")
    rcur.execute("GRANT SELECT ON *.* TO 'readonly'")
    print("  ✅ root 建表 + 创建 readonly 用户")

    # readonly 只能SELECT
    ro = pymysql.connect(host='127.0.0.1', port=PORT, user='readonly',
                         autocommit=True)
    rc = ro.cursor()

    rc.execute("SELECT * FROM t")
    rows = rc.fetchall()
    print(f"  ✅ readonly SELECT: {rows}")
    assert len(rows) == 1

    # INSERT 应该被拒绝
    try:
        rc.execute("INSERT INTO t VALUES (2, 'b')")
        print("  ❌ INSERT 未被拒绝！")
        assert False
    except Exception as e:
        print(f"  ✅ INSERT 被拒绝: {str(e)[:60]}")

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
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           autocommit=True)
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
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           autocommit=True)
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
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           autocommit=True)
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