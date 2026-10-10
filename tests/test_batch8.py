"""Batch 8 测试 - Binlog + 主从复制 + 备份"""
import os
import shutil
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

MASTER_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b8_master')
SLAVE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b8_slave')
BACKUP_DIR = os.path.join(os.path.dirname(__file__), '..', 'backups_b8')

MASTER_PORT = 8891


def reset():
    for d in (MASTER_DIR, SLAVE_DIR, BACKUP_DIR):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)


def create_schema(db):
    db.create_table('users', [
        {'name': 'id', 'type': 'INT', 'primary': True, 'notnull': True, 'unique': False},
        {'name': 'name', 'type': 'VARCHAR', 'primary': False, 'notnull': False, 'unique': False},
        {'name': 'age', 'type': 'INT', 'primary': False, 'notnull': False, 'unique': False},
    ])


def test_binlog():
    """测试1: Binlog 写入/读取"""
    print("\n【测试1】Binlog 写入/读取")
    reset()
    from lightdb.db.database import Database

    db = Database(MASTER_DIR)
    create_schema(db)
    tbl = db.get_table('users')

    for i in range(5):
        tbl.insert([i + 1, f'user{i}', 20 + i])
        db.binlog.append(0, 'INSERT', 'users', {'row': [i + 1, f'user{i}', 20 + i]})
    db.binlog.flush()

    print(f"  → 当前 binlog 位置: {db.binlog.current_pos()}")
    assert db.binlog.current_pos() >= 5

    events = db.binlog.read_from(1, limit=100)
    print(f"  → 读取 {len(events)} 条事件")
    for e in events[:3]:
        print(f"    pos={e.pos} type={e.type} table={e.table}")
    assert len(events) >= 5

    db.shutdown()


def test_logical_backup():
    """测试2: 逻辑备份 + 恢复"""
    print("\n【测试2】逻辑备份 + 恢复")
    reset()
    from lightdb.db.database import Database
    from lightdb.backup.backup import BackupManager

    # 写数据
    db = Database(MASTER_DIR)
    create_schema(db)
    tbl = db.get_table('users')
    for i in range(10):
        tbl.insert([i + 1, f'u{i}', 20 + i])
    db.shutdown()

    # 备份
    db = Database(MASTER_DIR)
    bm = BackupManager(db, BACKUP_DIR)
    dump_file = bm.dump()
    print(f"  → 备份文件: {dump_file}")
    assert os.path.exists(dump_file)

    size = os.path.getsize(dump_file)
    print(f"  → 文件大小: {size} 字节")

    with open(dump_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'CREATE TABLE users' in content
    assert content.count('INSERT INTO users') == 10
    print(f"  → 包含 10 条 INSERT")

    db.shutdown()

    # 恢复到新库
    RESTORE_DIR = MASTER_DIR + '_restore'
    if os.path.exists(RESTORE_DIR):
        shutil.rmtree(RESTORE_DIR)
    db2 = Database(RESTORE_DIR)
    bm2 = BackupManager(db2, BACKUP_DIR)
    ok, fail = bm2.restore(dump_file)
    print(f"  → 恢复: 成功={ok}, 失败={fail}")
    assert fail == 0

    tbl2 = db2.get_table('users')
    assert tbl2.row_count == 10
    print(f"  ✅ 恢复后行数: {tbl2.row_count}")
    db2.shutdown()
    shutil.rmtree(RESTORE_DIR, ignore_errors=True)


def test_replication():
    """测试3: 主从复制"""
    print("\n【测试3】主从复制")
    reset()

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser
    from lightdb.net.server import MySQLServer
    from lightdb.config import Config
    from lightdb.replication.slave import SlaveReplicator

    # 1. 准备主库
    master_db = Database(MASTER_DIR)
    create_schema(master_db)
    master_ex = Executor(master_db)

    # 2. 启动主库服务器
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = MASTER_PORT
    master_srv = MySQLServer(cfg, master_db, master_ex)
    t = threading.Thread(target=master_srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    print(f"  ✅ 主库启动: 127.0.0.1:{MASTER_PORT}")

    # 3. 准备从库
    slave_db = Database(SLAVE_DIR)
    create_schema(slave_db)
    slave = SlaveReplicator(slave_db, '127.0.0.1', MASTER_PORT)
    slave.start()
    time.sleep(0.5)
    print("  ✅ 从库复制已启动")

    # 4. 主库写入 10 行
    for i in range(10):
        p = parser.parse(f"INSERT INTO users VALUES ({i+1}, 'user{i}', {20+i})")
        master_ex.execute(p, {})
    print("  → 主库写入 10 行")

    # 5. 等待复制
    time.sleep(2.0)

    st = slave.status()
    print(f"  → 从库状态: IO={st['io_events']}, SQL={st['sql_events']}, Pos={st['last_pos']}")

    # 6. 验证从库数据
    slave_tbl = slave_db.get_table('users')
    print(f"  → 从库行数: {slave_tbl.row_count}")
    assert slave_tbl.row_count == 10, f"从库应该有10行，实际{slave_tbl.row_count}"

    # 7. 主库更新
    master_ex.execute(parser.parse("UPDATE users SET age = 99 WHERE id = 3"), {})
    master_ex.execute(parser.parse("DELETE FROM users WHERE id = 7"), {})
    print("  → 主库: UPDATE id=3, DELETE id=7")

    time.sleep(1.5)

    # 8. 验证从库
    cols, rows = slave_tbl.select(['*'],
                                   {'column': 'id', 'op': '=', 'value': 3})
    assert rows[0][2] == 99, f"从库更新未同步: {rows}"
    print(f"  ✅ 更新同步: id=3, age={rows[0][2]}")

    cols, rows = slave_tbl.select(['*'],
                                   {'column': 'id', 'op': '=', 'value': 7})
    assert len(rows) == 0, "从库删除未同步"
    print("  ✅ 删除同步")

    # 9. 清理
    slave.stop()
    master_srv.shutdown()
    master_db.shutdown()
    slave_db.shutdown()
    time.sleep(0.5)


def test_master_slave_status():
    """测试4: SHOW MASTER STATUS / SHOW SLAVE STATUS"""
    print("\n【测试4】SHOW MASTER/SLAVE STATUS")
    reset()

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser
    from lightdb.net.server import MySQLServer
    from lightdb.config import Config

    master_db = Database(MASTER_DIR)
    create_schema(master_db)
    master_ex = Executor(master_db)

    # 写点数据
    for i in range(3):
        master_ex.execute(parser.parse(f"INSERT INTO users VALUES ({i+1}, 'u{i}', 20)"), {})

    # 启动服务器
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = MASTER_PORT + 1
    srv = MySQLServer(cfg, master_db, master_ex)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    # 客户端查询
    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=MASTER_PORT + 1,
                           user='root', autocommit=True)
    cur = conn.cursor()

    cur.execute("SHOW MASTER STATUS")
    row = cur.fetchone()
    print(f"  → SHOW MASTER STATUS: File={row[0]}, Position={row[1]}")
    assert row[0].startswith('binlog.')
    assert row[1] >= 3

    cur.execute("SHOW SLAVE STATUS")
    row = cur.fetchone()
    print(f"  → SHOW SLAVE STATUS: Running={row[0]}")
    assert row[0] == 0

    # 测试 BINLOG_FETCH
    cur.execute("SELECT * FROM __BINLOG_FETCH__ WHERE pos >= 1 LIMIT 100")
    rows = cur.fetchall()
    print(f"  → __BINLOG_FETCH__: {len(rows)} 条事件")
    assert len(rows) >= 3

    conn.close()
    srv.shutdown()
    master_db.shutdown()
    time.sleep(0.5)


def main():
    print("=" * 60)
    print("  Batch 8 测试 - Binlog + 主从复制 + 备份")
    print("=" * 60)

    tests = [
        ('Binlog', test_binlog),
        ('逻辑备份恢复', test_logical_backup),
        ('主从复制', test_replication),
        ('SHOW STATUS', test_master_slave_status),
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
        print("  🎉 Batch 8 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())