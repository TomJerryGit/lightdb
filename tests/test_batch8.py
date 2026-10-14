"""Batch 8 测试 - Binlog + 主从复制 + 备份"""
import os
import shutil
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_batch8.py
    from conftest import safe_rmtree, track

MASTER_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b8_master')
SLAVE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b8_slave')
BACKUP_DIR = os.path.join(os.path.dirname(__file__), '..', 'backups_b8')

MASTER_PORT = 8891


def reset():
    for d in (MASTER_DIR, SLAVE_DIR, BACKUP_DIR):
        if os.path.exists(d):
            safe_rmtree(d)
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

    db = track(Database(MASTER_DIR))
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
    db = track(Database(MASTER_DIR))
    create_schema(db)
    tbl = db.get_table('users')
    for i in range(10):
        tbl.insert([i + 1, f'u{i}', 20 + i])
    db.shutdown()

    # 备份
    db = track(Database(MASTER_DIR))
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
        safe_rmtree(RESTORE_DIR)
    db2 = track(Database(RESTORE_DIR))
    bm2 = BackupManager(db2, BACKUP_DIR)
    ok, fail = bm2.restore(dump_file)
    print(f"  → 恢复: 成功={ok}, 失败={fail}")
    assert fail == 0

    tbl2 = db2.get_table('users')
    assert tbl2.row_count == 10
    print(f"  ✅ 恢复后行数: {tbl2.row_count}")
    db2.shutdown()
    safe_rmtree(RESTORE_DIR)


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
    master_db = track(Database(MASTER_DIR))
    create_schema(master_db)
    master_ex = track(Executor(master_db))

    # 2. 启动主库服务器
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = MASTER_PORT
    master_srv = track(MySQLServer(cfg, master_db, master_ex))
    t = threading.Thread(target=master_srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    print(f"  ✅ 主库启动: 127.0.0.1:{MASTER_PORT}")

    # 3. 准备从库
    slave_db = track(Database(SLAVE_DIR))
    create_schema(slave_db)
    slave = track(SlaveReplicator(slave_db, '127.0.0.1', MASTER_PORT))
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

    master_db = track(Database(MASTER_DIR))
    create_schema(master_db)
    master_ex = track(Executor(master_db))

    # 写点数据
    for i in range(3):
        master_ex.execute(parser.parse(f"INSERT INTO users VALUES ({i+1}, 'u{i}', 20)"), {})

    # 启动服务器
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = MASTER_PORT + 1
    srv = track(MySQLServer(cfg, master_db, master_ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    # 客户端查询
    import pymysql
    conn = track(pymysql.connect(host='127.0.0.1', port=MASTER_PORT + 1,
                                 user='root', autocommit=True))
    cur = conn.cursor()

    cur.execute("SHOW MASTER STATUS")
    row = cur.fetchone()
    print(f"  → SHOW MASTER STATUS: File={row[0]}, Position={row[1]}")
    assert row[0].startswith('binlog.')
    assert row[1] >= 3

    cur.execute("SHOW SLAVE STATUS")
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    print(f"  → SHOW SLAVE STATUS: {len(cols)} 列，"
          f"Slave_IO_Running={row[10]!r}, Slave_SQL_Running={row[11]!r}, "
          f"Slave_IO_State={row[0]!r}")
    # MySQL 原生列顺序：第 1 列是 Slave_IO_State，两个运行标志在第 11/12 列
    assert cols[:12] == [
        'Slave_IO_State', 'Master_Host', 'Master_User', 'Master_Port',
        'Connect_Retry', 'Master_Log_File', 'Read_Master_Log_Pos',
        'Relay_Log_File', 'Relay_Log_Pos', 'Relay_Master_Log_File',
        'Slave_IO_Running', 'Slave_SQL_Running'], cols
    # 真实的运行标志（MySQL 文本语义）：本实例没有配置复制 -> 'No'/'No'
    assert row[10] == 'No' and row[11] == 'No', row
    # 第 1 列是真实的状态说明文本：本实例没有配置复制
    assert row[0] == 'No replication configured', row
    # 未配置复制 -> 连接类字段为 NULL（没有主库连接可报）
    assert row[1] is None and row[3] is None, row

    # 测试 BINLOG_FETCH
    cur.execute("SELECT * FROM __BINLOG_FETCH__ WHERE pos >= 1 LIMIT 100")
    rows = cur.fetchall()
    print(f"  → __BINLOG_FETCH__: {len(rows)} 条事件")
    assert len(rows) >= 3

    conn.close()
    srv.shutdown()
    master_db.shutdown()
    time.sleep(0.5)


def test_slave_status_reports_real_replication():
    """测试5: SHOW SLAVE STATUS 的语义为真（未配置 -> 'No'；已配置 -> 真实位点前进）

    这是本轮新增的锁测试（对应"接通 SlaveReplicator"）：
    * 未配置复制时：`Slave_IO_State='No replication configured'`、
      两个运行标志 `'No'`、主库连接类字段 `NULL`；
    * 启动真实复制器后：`Master_Host`/`Master_Port`/`Master_User` 取自配置，
      位点从复制器真实拉取/应用的事件序号来，主库继续写入后**位点真的前进**
      （这里是数值对照，不是"看起来像"）；
    * 停掉复制后：运行标志回到真实的 `'No'`。
    """
    print("\n【测试5】SHOW SLAVE STATUS 的真实复制状态")
    reset()

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.sql.parser import parser
    from lightdb.net.server import MySQLServer
    from lightdb.config import Config
    from lightdb.replication.slave import SlaveReplicator

    def show_status(ex):
        r = ex.execute(parser.parse('SHOW SLAVE STATUS'), {})
        assert r['type'] == 'resultset', r
        return dict(zip(r['columns'], r['rows'][0]))

    def wait_until(pred, timeout=20.0, interval=0.2):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(interval)
        return False

    # ---------- 1) 未配置复制：这就是当时的真实状态 ----------
    master_db = track(Database(MASTER_DIR))
    create_schema(master_db)
    master_ex = track(Executor(master_db))

    slave_db = track(Database(SLAVE_DIR))
    create_schema(slave_db)
    slave_ex = track(Executor(slave_db))

    st0 = show_status(slave_ex)
    print(f"  → [未配置] IO_State={st0['Slave_IO_State']!r} "
          f"IO={st0['Slave_IO_Running']!r} SQL={st0['Slave_SQL_Running']!r} "
          f"Host={st0['Master_Host']!r} Port={st0['Master_Port']!r} "
          f"Read_Pos={st0['Read_Master_Log_Pos']!r} "
          f"Exec_Pos={st0['Exec_Master_Log_Pos']!r} "
          f"Behind={st0['Seconds_Behind_Master']!r}")
    assert st0['Slave_IO_State'] == 'No replication configured'
    assert st0['Slave_IO_Running'] == 'No'
    assert st0['Slave_SQL_Running'] == 'No'
    for col in ('Master_Host', 'Master_User', 'Master_Port', 'Connect_Retry',
                'Master_Log_File', 'Read_Master_Log_Pos', 'Relay_Log_File',
                'Relay_Log_Pos', 'Relay_Master_Log_File',
                'Exec_Master_Log_Pos', 'Relay_Log_Space',
                'Seconds_Behind_Master', 'Slave_SQL_Running_State',
                'Master_Server_Id', 'Master_UUID'):
        assert st0[col] is None, (col, st0[col])

    # ---------- 2) 启动真实主库 + 真实从库复制器 ----------
    port = MASTER_PORT + 2
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    srv = track(MySQLServer(cfg, master_db, master_ex))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    for i in range(4):
        master_ex.execute(parser.parse(
            f"INSERT INTO users VALUES ({i + 1}, 'r{i}', {20 + i})"), {})
    print("  → 主库写入 4 行")

    slave = track(SlaveReplicator(slave_db, '127.0.0.1', port, user='root'))
    slave.start()

    ok = wait_until(lambda: slave_db.get_table('users').row_count == 4
                    and show_status(slave_ex)['Exec_Master_Log_Pos'] == 4)
    st1 = show_status(slave_ex)
    print(f"  → [已启动] IO_State={st1['Slave_IO_State']!r} "
          f"IO={st1['Slave_IO_Running']!r} SQL={st1['Slave_SQL_Running']!r} "
          f"Host={st1['Master_Host']!r} Port={st1['Master_Port']!r} "
          f"User={st1['Master_User']!r} File={st1['Master_Log_File']!r} "
          f"Read_Pos={st1['Read_Master_Log_Pos']!r} "
          f"Exec_Pos={st1['Exec_Master_Log_Pos']!r} "
          f"Relay={st1['Relay_Log_File']!r}@{st1['Relay_Log_Pos']!r} "
          f"Behind={st1['Seconds_Behind_Master']!r} "
          f"SQL_State={st1['Slave_SQL_Running_State']!r}")
    assert ok, f"复制未在 20s 内追平: {st1}"
    assert st1['Master_Host'] == '127.0.0.1'
    assert st1['Master_Port'] == port
    assert st1['Master_User'] == 'root'
    assert st1['Slave_IO_Running'] in ('Yes', 'Connecting')
    assert st1['Slave_SQL_Running'] == 'Yes'
    assert st1['Master_Log_File'] == 'binlog.000001'
    assert st1['Read_Master_Log_Pos'] == 4
    assert st1['Exec_Master_Log_Pos'] == 4
    assert st1['Relay_Log_File'] == 'relay.log'
    assert st1['Relay_Log_Pos'] > 0 and st1['Relay_Log_Space'] > 0
    assert st1['Seconds_Behind_Master'] == 0      # 真的追平了
    assert st1['Last_IO_Errno'] == 0 and st1['Last_SQL_Errno'] == 0

    # ---------- 3) 主库继续写入 -> 从库位点必须真的前进 ----------
    before = (st1['Read_Master_Log_Pos'], st1['Exec_Master_Log_Pos'])
    for i in range(4, 10):
        master_ex.execute(parser.parse(
            f"INSERT INTO users VALUES ({i + 1}, 'r{i}', {20 + i})"), {})
    print("  → 主库再写入 6 行（总计 10 行）")

    ok = wait_until(lambda: slave_db.get_table('users').row_count == 10
                    and show_status(slave_ex)['Exec_Master_Log_Pos'] == 10)
    st2 = show_status(slave_ex)
    after = (st2['Read_Master_Log_Pos'], st2['Exec_Master_Log_Pos'])
    print(f"  → 位点前进: Read {before[0]} -> {after[0]}, "
          f"Exec {before[1]} -> {after[1]}（从库行数 "
          f"{slave_db.get_table('users').row_count}）")
    assert ok, f"位点未在 20s 内前进: {st2}"
    assert after[0] > before[0] and after[1] > before[1]
    assert after == (10, 10), after
    assert slave_db.get_table('users').row_count == 10
    assert st2['Seconds_Behind_Master'] == 0

    # ---------- 4) 停掉复制：运行标志回到真实的 'No' ----------
    slave.stop()
    st3 = show_status(slave_ex)
    print(f"  → [已停止] IO_State={st3['Slave_IO_State']!r} "
          f"IO={st3['Slave_IO_Running']!r} SQL={st3['Slave_SQL_Running']!r} "
          f"Host={st3['Master_Host']!r} Exec_Pos={st3['Exec_Master_Log_Pos']!r} "
          f"Behind={st3['Seconds_Behind_Master']!r}")
    assert st3['Slave_IO_Running'] == 'No'
    assert st3['Slave_SQL_Running'] == 'No'
    assert st3['Seconds_Behind_Master'] is None
    assert st3['Slave_SQL_Running_State'] is None   # SQL 线程没在跑 -> NULL
    # 停止后配置与最后位点仍然如实保留（MySQL 同：STOP SLAVE 不清配置）
    assert st3['Master_Host'] == '127.0.0.1' and st3['Exec_Master_Log_Pos'] == 10

    srv.shutdown()
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
        ('SHOW SLAVE STATUS 真实状态', test_slave_status_reports_real_replication),
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