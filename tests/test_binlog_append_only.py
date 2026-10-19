# -*- coding: utf-8 -*-
"""真 append-only binlog + 主从复制闭环（决策 D 项）

覆盖：
1. 文件格式与位点：文件存在、`SHOW MASTER STATUS.Position` 是**真实字节
   位点**且单调递增；`SHOW BINLOG EVENTS` 的 Pos/End_log_pos 是真实字节
   偏移（记录头 + payload + CRC 的字节数）。
2. 不受 WAL 影响：checkpoint / ALTER / checkpoint 之后位点**不回退**、
   旧事件仍可读（对照旧实现的"重扫 WAL、位点回退"缺陷）。
3. 完整性：手工改坏一个字节 → `BinlogReader`/`SHOW BINLOG EVENTS` 显式
   报错（`BinlogCorrupted`），绝不静默跳过；末尾截断 → `BinlogTruncated`。
4. 事务分组：显式事务写 `BEGIN → rows… → COMMIT`；ROLLBACK 写
   `BEGIN → ROLLBACK` 且不含行事件；autocommit 语句自成一组。
5. `BinlogReader.read_from(pos)` 从任意位点重读一致。
6. 主从闭环：主库 DDL+DML → 从库按持久位点应用（DDL 也复制）→ 数据一致；
   从库重启（重建 SlaveReplicator）后从 master.info 位点续传，不重复。
7. 控制语句：未配置时 START/STOP SLAVE 报 1190；CHANGE MASTER TO 后可
   START/STOP SLAVE。

本文件不依赖多进程（沙箱禁命名管道），全部进程内完成。
"""
import json
import os
import shutil
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_binlog_append_only.py
    from conftest import safe_rmtree, track

from lightdb.replication.append_only_binlog import (
    AppendOnlyBinlog, BinlogReader, BinlogCorrupted, BinlogTruncated,
    iter_records_bytes,
    FILE_HEADER_SIZE, RECORD_HEADER_SIZE, TYPE_BEGIN, TYPE_COMMIT,
    TYPE_ROLLBACK, TYPE_INSERT, TYPE_UPDATE, TYPE_DELETE, TYPE_DDL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================== 工具 ====================

@pytest.fixture()
def env(tmp_path):
    """一个 Database + Executor（数据目录用 tmp_path，自动清理）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    yield db, ex
    try:
        db.shutdown()
    except Exception:
        pass


def ex_run(ex, sql, session=None):
    """执行一条 SQL 并返回执行器结果字典"""
    from lightdb.sql.parser import parser
    p = parser.parse(sql)
    assert p is not None, f'解析失败: {sql}'
    return ex.execute(p, session if session is not None else {})


def show_master(ex):
    return ex_run(ex, 'SHOW MASTER STATUS')


def binlog_path(db):
    return os.path.join(str(db.data_dir), 'binlog', 'binlog.000001')


def records(db):
    """直接读 binlog 文件的全部记录（含事务边界/格式事件）"""
    rec = AppendOnlyBinlog(str(db.data_dir))
    try:
        return rec.read_all()
    finally:
        rec.close()


def write_example(db):
    """建表 + 3 条 DML，返回 (表名, 记录列表)"""
    ex = db_executor(db)
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    ex_run(ex, "INSERT INTO t VALUES (1, 'a')")
    ex_run(ex, "UPDATE t SET v='a2' WHERE id=1")
    ex_run(ex, 'DELETE FROM t WHERE id=1')
    return records(db)


def db_executor(db):
    from lightdb.sql.executor import Executor
    ex = getattr(db, '_test_executor', None)
    if ex is None:
        ex = Executor(db)
        db._test_executor = ex
    return ex


# ==================== 1. 文件存在 + 位点单调 ====================

def test_binlog_file_created_and_position_monotonic(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    # binlog 目录/文件在第一次写入时创建
    assert os.path.exists(binlog_path(db)), binlog_path(db)
    r = show_master(ex)
    assert r['type'] == 'resultset', r
    cols = r['columns']
    assert cols == ['File', 'Position', 'Binlog_Do_DB',
                    'Binlog_Ignore_DB', 'Executed_Gtid_Set']
    name, pos = r['rows'][0][0], r['rows'][0][1]
    assert name == 'binlog.000001'
    assert pos >= FILE_HEADER_SIZE + RECORD_HEADER_SIZE, pos

    positions = [pos]
    for i in range(1, 6):
        ex_run(ex, f'INSERT INTO t VALUES ({i})')
        p = show_master(ex)['rows'][0][1]
        positions.append(p)
    # 严格单调递增（真实字节位点，每写一组 BEGIN/row/COMMIT 都增长）
    assert all(b > a for a, b in zip(positions, positions[1:])), positions
    # 与文件真实大小一致
    assert positions[-1] == os.path.getsize(binlog_path(db))


def test_show_binlog_events_reports_real_byte_offsets(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    ex_run(ex, 'INSERT INTO t VALUES (1)')
    r = ex_run(ex, 'SHOW BINLOG EVENTS')
    assert r['type'] == 'resultset', r
    assert r['columns'] == ['Log_name', 'Pos', 'Event_type', 'Server_id',
                            'End_log_pos', 'Info']
    rows = r['rows']
    assert rows, rows
    # 第一条记录是 FORMAT_DESCRIPTION，位于文件头之后
    assert rows[0][2] == 'FORMAT_DESCRIPTION', rows[0]
    assert rows[0][1] == FILE_HEADER_SIZE
    types = [row[2] for row in rows]
    assert 'DDL' in types and 'INSERT' in types
    for i, row in enumerate(rows):
        assert row[3]  # Server_id：本进程真实 server_id（非 NULL）
        assert row[4] > row[1]  # End_log_pos > Pos
        if i:
            # 记录首尾相接：上一条的 End_log_pos == 下一条的 Pos
            assert rows[i - 1][4] == row[1], (rows[i - 1], row)
    # Info 不是占位：DDL 行含真实 SQL
    ddl_info = [row[5] for row in rows if row[2] == 'DDL'][0]
    assert 'CREATE TABLE t' in ddl_info, ddl_info


# ==================== 2. 不随 checkpoint/ALTER 回退 ====================

def test_position_never_regresses_after_checkpoint_and_alter(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    for i in range(1, 20):
        ex_run(ex, f"INSERT INTO t VALUES ({i}, 'v{i}')")
    r = ex_run(ex, 'SHOW BINLOG EVENTS')
    before = r['rows']
    pos_before = show_master(ex)['rows'][0][1]
    assert pos_before > 0

    # 触发所有表 WAL 退休（旧实现这里会让"位点"回退/事件消失）
    db.checkpoint()
    ex_run(ex, 'ALTER TABLE t ADD COLUMN extra INT')
    db.checkpoint()

    pos_after = show_master(ex)['rows'][0][1]
    assert pos_after > pos_before, (pos_before, pos_after)
    after = ex_run(ex, 'SHOW BINLOG EVENTS')['rows']
    # 旧事件一条不少（只增不减）
    assert len(after) > len(before)
    assert [row[1] for row in before] == [row[1] for row in after[:len(before)]]

    # 旧事件仍可通过 fetch 读到（位点语义稳定）
    fetch = ex_run(ex, 'SELECT * FROM __BINLOG_FETCH__ WHERE pos >= 1 LIMIT 1000')
    assert fetch['type'] == 'resultset'
    assert len(fetch['rows']) >= 20    # 19 INSERT + CREATE/ALTER
    assert fetch['rows'][0][3] == 'DDL'
    assert fetch['rows'][1][3] == 'INSERT'


def test_reader_can_reread_from_arbitrary_position(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    for i in range(1, 8):
        ex_run(ex, f'INSERT INTO t VALUES ({i})')
    rec = AppendOnlyBinlog(str(db.data_dir))
    try:
        all_records = rec.read_all()
        assert len(all_records) >= 8
        # 从第 3 条记录的起始位点重读：应从它开始，完整到末尾
        third = all_records[2]
        subset = rec.read_from(third.pos)
        assert [r.pos for r in subset] == [r.pos for r in all_records[2:]]
        # 逐条重读（任意 pos）一致
        for r0 in all_records:
            one = rec.read_from(r0.pos, limit=1)
            assert len(one) == 1 and one[0].pos == r0.pos
            assert one[0].payload == r0.payload
    finally:
        rec.close()


# ==================== 3. CRC 损坏 / 截断显式报错 ====================

def test_crc_corruption_is_reported_not_skipped(env):
    db, ex = env
    write_example(db)
    path = binlog_path(db)
    raw = bytearray(open(path, 'rb').read())
    assert len(raw) > FILE_HEADER_SIZE + RECORD_HEADER_SIZE + 8
    # 改坏第 2 条记录 payload 中的一个字节（保留记录头/长度不变）
    second = AppendOnlyBinlog(str(db.data_dir))
    try:
        recs = second.read_all()
    finally:
        second.close()
    target = recs[1]
    offset = target.pos + RECORD_HEADER_SIZE + 2
    raw[offset] ^= 0xFF
    with open(path, 'wb') as f:
        f.write(raw)

    # 1) 写端打开：中间记录损坏 → 拒绝（不静默修复）
    with pytest.raises(BinlogCorrupted):
        AppendOnlyBinlog(str(db.data_dir))
    # 2) 查询路径：SHOW BINLOG EVENTS 显式报错（不是空结果）
    r = ex_run(ex, 'SHOW BINLOG EVENTS')
    assert r['type'] == 'error', r
    assert '损坏' in r['message'] or 'CRC' in r['message'], r
    # 3) 从库拉取同样显式报错
    r = ex_run(ex, 'SELECT * FROM __BINLOG_FETCH__ WHERE pos >= 1 LIMIT 10')
    assert r['type'] == 'error', r

    # 恢复文件，避免影响后续（同一用例的 db.shutdown 需要正常关闭）
    raw[offset] ^= 0xFF
    with open(path, 'wb') as f:
        f.write(raw)
    assert ex_run(ex, 'SHOW BINLOG EVENTS')['type'] == 'resultset'


def test_tail_truncation_reported_and_repaired(env):
    db, ex = env
    write_example(db)
    path = binlog_path(db)
    size = os.path.getsize(path)
    # 模拟崩溃时写了半条记录：截掉最后 5 字节
    with open(path, 'r+b') as f:
        f.truncate(size - 5)
    raw = open(path, 'rb').read()
    # 只读视角（不打开写端）：末尾不完整 → BinlogTruncated 显式报错
    with pytest.raises(BinlogTruncated):
        list(iter_records_bytes(raw))
    # 写端打开：只修复**末尾**半条，恢复到最后一条完整记录并告警
    rec2 = AppendOnlyBinlog(str(db.data_dir))
    try:
        assert rec2.current_pos < size
        assert rec2.read_all(), '修复后旧事件必须仍可读（不允许静默丢中间记录）'
    finally:
        rec2.close()
    assert ex_run(ex, 'SHOW MASTER STATUS')['type'] == 'resultset'


# ==================== 4. 事务分组 ====================

def test_explicit_transaction_writes_begin_rows_commit(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, "INSERT INTO t VALUES (1, 'a')", s)
    ex_run(ex, "INSERT INTO t VALUES (2, 'b')", s)
    # 未提交：binlog 里不应出现这两行（提交时成组写）
    rec = AppendOnlyBinlog(str(db.data_dir))
    try:
        mid = rec.read_all()
    finally:
        rec.close()
    mid_inserts = [r for r in mid if r.type == TYPE_INSERT]
    assert mid_inserts == [], '未提交事务的 DML 不应提前落盘'
    ex_run(ex, 'COMMIT', s)

    recs = records(db)
    inserts = [i for i, r in enumerate(recs) if r.type == TYPE_INSERT]
    assert len(inserts) == 2, [r.type_name for r in recs]
    # 成组：两条 INSERT 之间/前后必须有 BEGIN 与 COMMIT，且连续
    assert recs[inserts[0] - 1].type == TYPE_BEGIN
    assert recs[inserts[1] + 1].type == TYPE_COMMIT
    # 同组 txn_id 一致且非 0
    txn_ids = {r.txn_id for r in recs[inserts[0] - 1:inserts[1] + 2]}
    assert len(txn_ids) == 1 and list(txn_ids)[0] not in (None, 0), txn_ids
    # 行镜像正确
    p = recs[inserts[0]].payload['data']
    assert p['row'] == [1, 'a'], p


def test_rollback_writes_begin_rollback_without_row_events(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, 'INSERT INTO t VALUES (7)', s)
    ex_run(ex, 'ROLLBACK', s)
    recs = records(db)
    assert [r for r in recs if r.type == TYPE_INSERT] == []
    assert [r for r in recs if r.type == TYPE_ROLLBACK], \
        [r.type_name for r in recs]
    # ROLLBACK 组不携带行事件（数据从未落盘）
    rb_idx = [i for i, r in enumerate(recs) if r.type == TYPE_ROLLBACK][0]
    assert recs[rb_idx - 1].type == TYPE_BEGIN
    assert recs[rb_idx].payload.get('discarded') == 1
    assert ex_run(ex, 'SELECT * FROM t')['rows'] == []


def test_autocommit_statement_is_its_own_group(env):
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    ex_run(ex, 'INSERT INTO t VALUES (1)')
    ex_run(ex, 'INSERT INTO t VALUES (2)')
    recs = records(db)
    types = [r.type for r in recs]
    # 数据记录布局：FORMAT, (BEGIN,INSERT,COMMIT) ×2
    groups = [(i, t) for i, t in enumerate(types)
              if t in (TYPE_BEGIN, TYPE_COMMIT)]
    assert len(groups) >= 4, types
    for i, t in enumerate(types):
        if t == TYPE_INSERT:
            assert types[i - 1] == TYPE_BEGIN, types
            assert types[i + 1] == TYPE_COMMIT, types


# ==================== 5. 主从闭环（DDL 也复制 + 从库重启续传）====================

def _start_master_server(db, port):
    from lightdb.config import Config
    from lightdb.net.server import MySQLServer
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    srv = MySQLServer(cfg, db, db_executor(db))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    return srv


def _wait(pred, timeout=25.0, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _table_rows(db, name):
    tbl = db.get_table(name)
    cols, rows = tbl.select(['*'], None)
    return [tuple(r) for r in rows]


def test_master_slave_closed_loop_with_ddl_and_restart(tmp_path):
    from lightdb.db.database import Database
    from lightdb.replication.slave import SlaveReplicator

    master_dir = str(tmp_path / 'master')
    slave_dir = str(tmp_path / 'slave')
    port = 8931

    master_db = track(Database(master_dir))
    master_ex = db_executor(master_db)
    slave_db = track(Database(slave_dir))
    srv = track(_start_master_server(master_db, port))

    # 主库：建表 + 写数据（DDL 必须进 binlog 并被从库重放）
    ex_run(master_ex, 'CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(20))')
    for i in range(1, 6):
        ex_run(master_ex, f"INSERT INTO users VALUES ({i}, 'u{i}')")

    slave = SlaveReplicator(slave_db, '127.0.0.1', port, user='root', password='')
    try:
        slave.start()
        ok = _wait(lambda: ('users' in slave_db.tables)
                   and len(_table_rows(slave_db, 'users')) == 5)
        assert ok, f'从库未在超时内同步: tables={sorted(slave_db.tables)} ' \
                   f'status={slave.show_status()}'
        assert _table_rows(slave_db, 'users') == [
            (1, 'u1'), (2, 'u2'), (3, 'u3'), (4, 'u4'), (5, 'u5')]

        # 主库继续 DML（UPDATE/DELETE）→ 从库跟随
        ex_run(master_ex, "UPDATE users SET name='n3' WHERE id=3")
        ex_run(master_ex, 'DELETE FROM users WHERE id=1')
        ok = _wait(lambda: len(_table_rows(slave_db, 'users')) == 4
                   and [r for r in _table_rows(slave_db, 'users')
                        if r[0] == 3][0][1] == 'n3')
        assert ok, f'增量 DML 未同步: {_table_rows(slave_db, "users")}'
        # 位点在推进，且没有 SQL 错误
        st = slave.show_status()
        assert st['Last_SQL_Errno'] == 0, st
        assert st['Slave_IO_Running'] in ('Yes', 'Connecting')
        exec_pos_before = st['Exec_Master_Log_Pos']
        assert exec_pos_before and exec_pos_before >= 4
    finally:
        slave.stop()

    # 主库再写 3 行（从库停着）
    for i in range(6, 9):
        ex_run(master_ex, f"INSERT INTO users VALUES ({i}, 'u{i}')")
    # 位点持久化文件存在
    info_path = os.path.join(slave_dir, 'master.info')
    assert os.path.exists(info_path), info_path
    info = json.loads(open(info_path, encoding='utf-8').read())
    assert info['exec_pos'] == exec_pos_before
    assert info['master_port'] == port

    # 从库重启：新实例从 master.info 的位点续传 → 只补 3 行，不重复/不报错
    slave2 = SlaveReplicator(slave_db, '127.0.0.1', port, user='root', password='')
    try:
        assert slave2.read_master_log_pos == info['read_pos']
        assert slave2.last_pos == info['exec_pos']
        slave2.start()
        ok = _wait(lambda: len(_table_rows(slave_db, 'users')) == 7)
        assert ok, f'重启续传失败: {_table_rows(slave_db, "users")} ' \
                   f'{slave2.show_status()}'
        rows = _table_rows(slave_db, 'users')
        assert [r[0] for r in rows] == [2, 3, 4, 5, 6, 7, 8], rows
        assert slave2.show_status()['Last_SQL_Errno'] == 0
    finally:
        slave2.stop()
        srv.shutdown()
        master_db.shutdown()
        slave_db.shutdown()
        time.sleep(0.2)


# ==================== 6. 控制语句 ====================

def test_start_stop_slave_require_configuration(env):
    db, ex = env
    r = ex_run(ex, 'START SLAVE')
    assert r['type'] == 'error' and r['code'] == 1190, r
    r = ex_run(ex, 'STOP SLAVE')
    assert r['type'] == 'error' and r['code'] == 1190, r
    # SHOW SLAVE STATUS 仍是"未配置复制"的真实状态
    r = ex_run(ex, 'SHOW SLAVE STATUS')
    assert r['type'] == 'resultset'
    d = dict(zip(r['columns'], r['rows'][0]))
    assert d['Slave_IO_State'] == 'No replication configured'
    assert d['Slave_IO_Running'] == 'No' and d['Slave_SQL_Running'] == 'No'


def test_change_master_then_start_stop(env):
    db, ex = env
    r = ex_run(ex, 'CHANGE MASTER TO MASTER_HOST=127.0.0.1, '
                   'MASTER_PORT=9399, MASTER_USER=repl, MASTER_PASSWORD=')
    assert r['type'] == 'ok', r
    rep = db.replicator
    assert rep is not None
    assert rep.master_host == '127.0.0.1' and rep.master_port == 9399
    assert rep.user == 'repl'
    r = ex_run(ex, 'START SLAVE')
    assert r['type'] == 'ok', r
    d = dict(zip(ex_run(ex, 'SHOW SLAVE STATUS')['columns'],
                 ex_run(ex, 'SHOW SLAVE STATUS')['rows'][0]))
    assert d['Slave_SQL_Running'] == 'Yes'
    assert d['Master_Host'] == '127.0.0.1' and d['Master_Port'] == 9399
    r = ex_run(ex, 'STOP SLAVE')
    assert r['type'] == 'ok', r
    d = dict(zip(ex_run(ex, 'SHOW SLAVE STATUS')['columns'],
                 ex_run(ex, 'SHOW SLAVE STATUS')['rows'][0]))
    assert d['Slave_IO_Running'] == 'No' and d['Slave_SQL_Running'] == 'No'
    # 语法/选项错误显式报错（不静默忽略未知选项）
    with pytest.raises(SyntaxError):
        ex_run(ex, "CHANGE MASTER TO MASTER_HOST='h', MASTER_SSL=1")
    r = ex_run(ex, 'CHANGE MASTER TO MASTER_PORT=1')
    assert r['type'] == 'error' and r['code'] == 1064, r


def test_nested_select_does_not_discard_outer_dml_group(env):
    """触发器/嵌套语句里的 SELECT 不能丢弃外层 DML 正在缓冲的组

    回归场景：autocommit INSERT 的 AFTER 触发器体里先执行一条 SELECT，
    再执行一条 DML —— 旧收尾逻辑对任何语句都 `pop('_binlog_gid')`，
    SELECT 会把外层已缓冲的行事件当"失败语句"丢掉（静默数据丢失）。
    这里直接锁住该不变量：缓冲 → 嵌套 SELECT → 组仍在 → 提交后行事件存在。
    """
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    rec = db.binlog_log
    gid = rec.new_group_id()
    s = {'_binlog_gid': gid}
    ex._binlog_record(s, 'INSERT', 't', {'row': [42]})
    ex_run(ex, 'SELECT 1', s)          # 嵌套 SELECT：不得丢弃外层组
    assert s.get('_binlog_gid') == gid, '嵌套 SELECT 丢弃了外层 DML 的组'
    rec.commit_group(gid)
    rows = [r for r in records(db) if r.type == TYPE_INSERT]
    assert len(rows) == 1 and rows[0].payload['data']['row'] == [42], rows


def test_slave_replay_rejects_unknown_or_bad_events(env):
    """从库重放：未知事件类型/坏 DDL 必须显式报错（不静默跳过）"""
    from lightdb.replication.slave import SlaveReplicator

    db, ex = env
    rep = SlaveReplicator(db, '127.0.0.1', 1, user='repl')
    try:
        # 未知事件类型
        with pytest.raises(RuntimeError):
            rep._apply_event((1, 100, 0, 'SOMETHING_NEW', None, '{}'))
        # DDL 事件缺少 SQL / SQL 无法解析
        with pytest.raises(RuntimeError):
            rep._apply_event((2, 200, 0, 'DDL', None, '{}'))
        with pytest.raises(RuntimeError):
            rep._apply_event((3, 300, 0, 'DDL', None,
                              json.dumps({'sql': 'NOT A VALID DDL ???'})))
        # 重放失败被记成非 0 的 Last_SQL_Errno（E2 语义）
        rep._record_sql_error(RuntimeError('DDL 重放失败（拒绝静默跳过）'))
        st = rep.show_status()
        assert st['Last_SQL_Errno'] != 0 and st['Last_SQL_Error'], st
    finally:
        db.detach_replicator(rep)


def test_show_binlog_events_supports_from_and_limit_clauses(env):
    """SHOW BINLOG EVENTS [IN 'file'] [FROM pos] [LIMIT n]"""
    db, ex = env
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    for i in range(1, 4):
        ex_run(ex, f'INSERT INTO t VALUES ({i})')
    all_rows = ex_run(ex, 'SHOW BINLOG EVENTS')['rows']
    assert len(all_rows) > 3
    # LIMIT 只返回前 n 条
    limited = ex_run(ex, 'SHOW BINLOG EVENTS LIMIT 2')['rows']
    assert len(limited) == 2 and limited == all_rows[:2]
    # FROM pos：从第 3 条记录起（位点是真实字节偏移）
    start = all_rows[2][1]
    from_rows = ex_run(ex, f"SHOW BINLOG EVENTS FROM {start}")['rows']
    assert from_rows[0][1] == start
    assert [r[1] for r in from_rows] == [r[1] for r in all_rows[2:]]
    # IN 'file' 也接受（当前只有 binlog.000001，语义相同）
    same = ex_run(ex, "SHOW BINLOG EVENTS IN 'binlog.000001' LIMIT 2")['rows']
    assert same == limited


# ==================== 7. binlog 可关闭（如实空结果，不编造）====================

def test_binlog_disabled_returns_empty_master_status(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    monkeypatch.setenv('LIGHTDB_BINLOG_ENABLED', '0')
    db = track(Database(str(tmp_path / 'off')))
    ex = track(db_executor(db))
    ex_run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    ex_run(ex, 'INSERT INTO t VALUES (1)')
    r = ex_run(ex, 'SHOW MASTER STATUS')
    assert r['type'] == 'resultset' and r['rows'] == [], r
    assert not os.path.exists(binlog_path(db))
    db.shutdown()
