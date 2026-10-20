# -*- coding: utf-8 -*-
"""决策 4 阶段 2：真 binlog 收尾（轮转 / 写失败拒提交 / 半同步移除 / 镜像优化）

全部进程内（不依赖多进程原语）。覆盖：

1. **轮转**：小 max_bytes 触发 binlog.000002…；旧文件只读保留、可
   `SHOW BINLOG EVENTS IN '…'` 读；`SHOW MASTER STATUS` 报当前文件名 +
   文件内位点；写端重启后扫描最新文件继续追加，旧文件字节不变。
2. **跨文件全局位点**：`__BINLOG_FETCH__.pos`（全局逻辑序号）连续、
   `.lsn`（跨文件全局字节偏移）严格单调；轮转边界两侧仍单调。
3. **从库跨轮转续传**：主库小阈值轮转后从库仍能同步；停止 → 新写（再
   轮转）→ 重新构造 SlaveReplicator 从 master.info 位点续传，不重复。
4. **写失败即拒绝提交**：monkeypatch binlog 写/fsync 失败 → COMMIT 显式
   报 1180/HY000、数据不可见（回滚）、恢复后重试成功；`LIGHTDB_BINLOG_
   STRICT=0` 退化为告警（提交成功、数据可见）。
5. **半同步移除**：包不再导出半同步类、历史模块导入显式 ImportError、
   配置项删除、源码无残留引用（net/** 为不可改文件，单独允许）。
6. **GTID 决策**：`Executed_Gtid_Set` 恒 NULL（无 GTID，位点复制）。
7. **UPDATE/DELETE 镜像**：binlog 里的 old/new/pk 与手工 SELECT 逐列一致；
   显式事务与 autocommit 两条路径都覆盖（镜像不再靠语句前额外 SELECT）。
8. **镜像优化 A/B 计时（5k 行，本机实测，报告不设门禁）**

   方法：同一张 5000 行表（`id INT PRIMARY KEY, v INT, pad VARCHAR(40)`），
   分别执行
   `UPDATE ... WHERE v=42`（命中 50 行）、`DELETE ... WHERE v=7`（50 行）、
   `UPDATE t SET v=777`（整表 4950 行）；`LIGHTDB_BINLOG_MIRROR=legacy`
   退回阶段 1（语句前 SELECT）；两种模式在同一脚本里交替多轮执行。

   结果（本机多轮中较稳的两组，绝对值受同机并发负载影响较大）：

   =========================== 阶段1 legacy   阶段2 WAL 路径
   UPDATE WHERE v=42（50 行）   0.28 ~ 0.38 s  0.11 ~ 0.15 s（~2-3×）
   DELETE WHERE v=7（50 行）    0.28 ~ 0.35 s  0.14 ~ 0.15 s（~2×）
   UPDATE 整表（4950 行）       5.4 ~ 5.8 s    2.5 ~ 3.2 s（~1.8×）
   =========================== ============== =============

   确定性证据（不依赖计时）：`test_update_delete_mirror_avoids_extra_select`
   用 `tbl.select` 调用计数断言 WAL 路径为 **0 次**、legacy 路径 >=1 次；
   两种模式写出的 UPDATE/DELETE 镜像逐字段相等。
"""
import importlib
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
except ImportError:                       # 直接 python tests/test_binlog_phase2.py
    from conftest import safe_rmtree, track

from lightdb.replication.append_only_binlog import (
    AppendOnlyBinlog, BinlogCorrupted, BinlogWriteError,
    FILE_HEADER_SIZE, RECORD_HEADER_SIZE, MAX_BYTES_DEFAULT,
    file_name_for, file_index,
    TYPE_FORMAT_DESCRIPTION, TYPE_INSERT, TYPE_UPDATE, TYPE_DELETE,
    TYPE_DDL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SMALL_MAX = 4096          # 小阈值：每 4KB 轮转（便于在测试里跨多文件）
PORT_BASE = 8951


# ==================== 公共工具 ====================

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Database + Executor（默认 32MB 阈值；需要轮转的用例自行设环境变量）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.delenv('LIGHTDB_BINLOG_MAX_BYTES', raising=False)
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    yield db, ex
    try:
        db.shutdown()
    except Exception:
        pass


def ex_run(ex, sql, session=None):
    from lightdb.sql.parser import parser
    p = parser.parse(sql)
    assert p is not None, f'解析失败: {sql}'
    return ex.execute(p, session if session is not None else {})


def binlog_dir(db):
    return os.path.join(str(db.data_dir), 'binlog')


def binlog_files(db):
    d = binlog_dir(db)
    return sorted(n for n in os.listdir(d) if n.startswith('binlog.'))


def current_binlog(db, ex):
    r = ex_run(ex, 'SHOW MASTER STATUS')
    assert r['type'] == 'resultset' and r['rows'], r
    return r['rows'][0][0], r['rows'][0][1]


def fetch_rows(ex, limit=10000):
    r = ex_run(ex, f'SELECT * FROM __BINLOG_FETCH__ LIMIT {limit}')
    assert r['type'] == 'resultset', r
    return r['rows']


def show_events(ex, file=None, from_pos=None, limit=None):
    sql = 'SHOW BINLOG EVENTS'
    if file:
        sql += f" IN '{file}'"
    if from_pos is not None:
        sql += f' FROM {from_pos}'
    if limit is not None:
        sql += f' LIMIT {limit}'
    r = ex_run(ex, sql)
    assert r['type'] == 'resultset', r
    return r


def make_table(ex, name='t'):
    ex_run(ex, f'CREATE TABLE {name} (id INT PRIMARY KEY, v INT, s VARCHAR(20))')


def seed_rows(ex, n, name='t'):
    for i in range(1, n + 1):
        ex_run(ex, f"INSERT INTO {name} VALUES ({i}, {i * 10}, 's{i:03d}')")


# ==================== 1. 轮转基础 ====================

def test_rotation_creates_new_files_and_keeps_old_readable(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    make_table(ex)
    seed_rows(ex, 80)

    files = binlog_files(db)
    assert len(files) >= 3, files
    assert files[0] == 'binlog.000001'
    # 序号连续（000001..00000N）
    assert [file_index(f) for f in files] == list(range(1, len(files) + 1))
    # 老文件非空且都带文件头 + FORMAT_DESCRIPTION
    for f in files[:-1]:
        size = os.path.getsize(os.path.join(binlog_dir(db), f))
        assert size >= FILE_HEADER_SIZE + RECORD_HEADER_SIZE, (f, size)

    # SHOW MASTER STATUS = 当前文件名 + **文件内**位点（MySQL 语义）
    cur_file, cur_pos = current_binlog(db, ex)
    assert cur_file == files[-1], (cur_file, files[-1])
    assert cur_pos == os.path.getsize(os.path.join(binlog_dir(db), cur_file))
    # 位点绝不是全局值（轮转后全局位点远大于单文件位点）
    lsn = [r[1] for r in fetch_rows(ex)]
    assert lsn[-1] > cur_pos

    # 旧文件可用 IN 读取：首条是 FORMAT_DESCRIPTION@16，Pos 是文件内偏移
    old = files[0]
    ev = show_events(ex, file=old, limit=5)
    assert ev['rows'][0][0] == old
    assert ev['rows'][0][1] == FILE_HEADER_SIZE
    assert ev['rows'][0][2] == 'FORMAT_DESCRIPTION'
    for i, row in enumerate(ev['rows'][1:], start=1):
        assert row[4] > row[1]
    # 默认（无 IN）读当前文件
    cur_ev = show_events(ex, limit=3)
    assert cur_ev['rows'][0][0] == cur_file
    assert cur_ev['rows'][0][2] == 'FORMAT_DESCRIPTION'
    # 不存在的文件 → 显式报错（不悄悄替换成当前文件）
    r = ex_run(ex, "SHOW BINLOG EVENTS IN 'binlog.999999'")
    assert r['type'] == 'error' and r['code'] == 1105, r
    db.shutdown()


def test_show_binlog_events_from_pos_in_old_file(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    make_table(ex)
    seed_rows(ex, 80)
    old = binlog_files(db)[0]
    full = show_events(ex, file=old)
    assert len(full['rows']) >= 4
    # FROM pos：文件内偏移 >= pos
    target = full['rows'][2][1]
    part = show_events(ex, file=old, from_pos=target)
    assert part['rows'][0][1] == target
    assert [r[1] for r in part['rows']] == [r[1] for r in full['rows'][2:]]
    # LIMIT
    limited = show_events(ex, file=old, limit=2)
    assert limited['rows'] == full['rows'][:2]
    db.shutdown()


# ==================== 2. 跨文件全局位点 ====================

def test_global_lsn_monotonic_across_rotation(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    make_table(ex)
    seed_rows(ex, 80)
    files = binlog_files(db)
    assert len(files) >= 3

    rows = fetch_rows(ex)
    seqs = [r[0] for r in rows]
    lsns = [r[1] for r in rows]
    # pos：全局逻辑序号，连续 1..N（跨文件连续）
    assert seqs == list(range(1, len(rows) + 1)), seqs[:20]
    # lsn：跨文件全局字节偏移，严格单调
    assert all(b > a for a, b in zip(lsns, lsns[1:])), lsns
    assert len(set(lsns)) == len(lsns)

    # 轮转边界：后一个文件的记录 lsn 全部 > 前一个文件的全部记录 lsn
    for i in range(len(files) - 1):
        prev_ev = show_events(ex, file=files[i])
        next_ev = show_events(ex, file=files[i + 1])
        base_next = AppendOnlyBinlog(str(db.data_dir), max_bytes=SMALL_MAX)
        try:
            base = base_next.file_base(files[i + 1])
        finally:
            base_next.close()
        # 下一文件内第一条记录（FORMAT）的全局偏移 = base + 16
        assert base + next_ev['rows'][0][1] == base + FILE_HEADER_SIZE
        # 用 reader 直接核对全局位点：旧文件最后一条 < 新文件第一条
        reader = AppendOnlyBinlog(str(db.data_dir), max_bytes=SMALL_MAX)
        try:
            recs = reader.read_global_from(0)
        finally:
            reader.close()
        globals_ = [r.global_pos for r in recs]
        assert all(b > a for a, b in zip(globals_, globals_[1:])), globals_
    db.shutdown()


def test_reopen_scans_latest_file_and_appends(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    d = str(tmp_path / 'data')
    db = Database(d)
    ex = Executor(db)
    make_table(ex)
    seed_rows(ex, 60)
    files_before = binlog_files(db)
    sizes_before = {f: os.path.getsize(os.path.join(binlog_dir(db), f))
                    for f in files_before}
    count_before = len(fetch_rows(ex))
    db.shutdown()

    # 重新打开同一数据目录：扫描最新文件、沿用全局序号继续追加
    db2 = track(Database(d))
    ex2 = track(Executor(db2))
    for i in range(100, 120):
        ex_run(ex2, f"INSERT INTO t VALUES ({i}, {i}, 'n{i}')")
    files_after = binlog_files(db2)
    assert len(files_after) >= len(files_before)
    # 旧文件（除重开前**正在写**的最后那个可继续增长/被轮转外）字节不变
    for f in files_before[:-1]:
        assert os.path.getsize(os.path.join(binlog_dir(db2), f)) == \
            sizes_before[f], f
    # 重开前正在写的文件：要么原样保留，要么作为"已轮转的旧文件"保留
    last = files_before[-1]
    assert os.path.getsize(os.path.join(binlog_dir(db2), last)) >= \
        sizes_before[last]
    rows = fetch_rows(ex2)
    assert len(rows) > count_before
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    db2.shutdown()


def test_writer_only_repairs_latest_tail_old_files_readonly(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    d = str(tmp_path / 'data')
    db = Database(d)
    ex = Executor(db)
    make_table(ex)
    seed_rows(ex, 80)
    files = binlog_files(db)
    assert len(files) >= 3
    old_path = os.path.join(binlog_dir(db), files[0])
    old_size = os.path.getsize(old_path)
    cur_path = os.path.join(binlog_dir(db), files[-1])
    cur_size = os.path.getsize(cur_path)
    db.shutdown()

    # 1) 最新文件末尾截断（崩溃半条）→ 重新打开时只修复最新文件
    with open(cur_path, 'r+b') as f:
        f.truncate(cur_size - 5)
    db2 = track(Database(d))
    ex2 = track(Executor(db2))
    assert os.path.getsize(cur_path) < cur_size
    assert os.path.getsize(old_path) == old_size
    # 修复后旧事件仍可读（fetch 跨文件）
    assert len(fetch_rows(ex2)) >= 70
    db2.shutdown()

    # 2) 旧文件 CRC 损坏 → 打开写端必须显式报错（只读文件不允许静默修复）
    with open(old_path, 'r+b') as f:
        f.seek(FILE_HEADER_SIZE + RECORD_HEADER_SIZE + 2)
        b = f.read(1)
        f.seek(FILE_HEADER_SIZE + RECORD_HEADER_SIZE + 2)
        f.write(bytes([b[0] ^ 0xFF]))
    with pytest.raises(BinlogCorrupted):
        AppendOnlyBinlog(d, max_bytes=SMALL_MAX)


# ==================== 3. 从库跨轮转续传 ====================

def _wait(pred, timeout=30.0, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _start_master(db, ex, port):
    from lightdb.config import Config
    from lightdb.net.server import MySQLServer
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    return srv


def _rows(db, name='t'):
    tbl = db.get_table(name)
    cols, rows = tbl.select(['*'], None)
    return sorted(tuple(r) for r in rows)


def test_slave_syncs_and_resumes_across_rotation(tmp_path, monkeypatch):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.replication.slave import SlaveReplicator

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    master_dir = str(tmp_path / 'master')
    slave_dir = str(tmp_path / 'slave')
    port = PORT_BASE + 1
    master_db = track(Database(master_dir))
    master_ex = track(Executor(master_db))
    slave_db = track(Database(slave_dir))
    srv = track(_start_master(master_db, master_ex, port))

    make_table(master_ex)
    seed_rows(master_ex, 80)              # 主库侧会跨多个 binlog 文件
    assert len(binlog_files(master_db)) >= 3

    slave = SlaveReplicator(slave_db, '127.0.0.1', port, user='root',
                            password='')
    try:
        slave.start()
        ok = _wait(lambda: len(_rows(slave_db)) == 80)
        assert ok, (len(_rows(slave_db)), slave.show_status())
        assert _rows(slave_db) == _rows(master_db)
        st = slave.show_status()
        assert st['Last_SQL_Errno'] == 0, st
        assert st['Master_Log_File'].startswith('binlog.'), st
        # 逻辑序号：1 条 DDL（CREATE TABLE 由复制建表）+ 80 条 INSERT
        exec_before = st['Exec_Master_Log_Pos']
        assert exec_before == 81, st
    finally:
        slave.stop()

    # 主库继续写 → 再触发轮转（从库停着）
    for i in range(81, 141):
        ex_run(master_ex, f"INSERT INTO t VALUES ({i}, {i * 10}, 's{i:03d}')")
    assert len(binlog_files(master_db)) >= 3
    info_path = os.path.join(slave_dir, 'master.info')
    assert os.path.exists(info_path)
    info = json.loads(open(info_path, encoding='utf-8').read())
    assert info['exec_pos'] == exec_before
    assert info['read_byte_pos'] > 0      # 跨文件全局 lsn 已持久化

    # 从库重启：新实例按持久位点续传（跨轮转），不重复不报错
    slave2 = SlaveReplicator(slave_db, '127.0.0.1', port, user='root',
                             password='')
    try:
        assert slave2.read_master_log_pos == info['read_pos']
        assert slave2.last_pos == info['exec_pos']
        slave2.start()
        ok = _wait(lambda: len(_rows(slave_db)) == 140)
        if not ok:
            got = _rows(slave_db)
            ids = {r[0] for r in got}
            master_rows = _rows(master_db)
            fetch = ex_run(master_ex,
                           'SELECT * FROM __BINLOG_FETCH__ LIMIT 1000')
            print('DEBUG slave rows:', len(got),
                  'missing:', [i for i in range(1, 141) if i not in ids])
            print('DEBUG master rows:', len(master_rows),
                  'fetch events:', len(fetch['rows']),
                  'slave2 stats:', slave2.stats)
            print('DEBUG slave2 status:', slave2.show_status())
            raise AssertionError(
                f'重启续传失败: rows={len(got)} '
                f'missing={[i for i in range(1, 141) if i not in ids]} '
                f'status={slave2.show_status()} stats={slave2.stats}')
        assert _rows(slave_db) == _rows(master_db)
        assert [r[0] for r in _rows(slave_db)] == list(range(1, 141))
        st = slave2.show_status()
        assert st['Last_SQL_Errno'] == 0, st
    finally:
        slave2.stop()
        srv.shutdown()
        master_db.shutdown()
        slave_db.shutdown()
        time.sleep(0.2)


# ==================== 4. binlog 写失败即拒绝提交 ====================

def test_default_threshold_is_32mb():
    """默认单文件阈值 = 32MB（模块常量与 Config 一致，可环境变量覆盖）"""
    from lightdb.config import Config
    assert MAX_BYTES_DEFAULT == 32 * 1024 * 1024
    assert Config().binlog_max_bytes == MAX_BYTES_DEFAULT
    assert Config().binlog_strict is True


def test_rotation_creation_failure_rejects_commit_and_rolls_back(
        tmp_path, monkeypatch):
    """轮转建新文件失败 → 整组未写 → strict 提交拒绝并回滚（写前轮转）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', '512')   # 很快触发轮转
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    make_table(ex)
    seed_rows(ex, 3)                       # 已触发轮转
    assert len(binlog_files(db)) >= 2
    rec = db.binlog_log
    real_create = rec._create_file

    def bad_create(path):
        raise BinlogWriteError('模拟轮转建文件失败（测试注入）', path.name)

    monkeypatch.setattr(rec, '_create_file', bad_create)
    # 强制"下一次写前一定轮转"：把当前文件位点推到阈值
    rec._local_pos = rec.max_bytes
    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, "INSERT INTO t VALUES (100, 1, 'x')", s)
    r = ex_run(ex, 'COMMIT', s)
    assert r['type'] == 'error' and r['code'] == 1180, r
    cur = ex_run(ex, 'SELECT COUNT(*) FROM t WHERE id = 100')
    assert cur['rows'][0][0] == 0, '轮转失败后数据必须回滚'
    # 恢复后重试成功
    monkeypatch.setattr(rec, '_create_file', real_create)
    s2 = {}
    ex_run(ex, 'BEGIN', s2)
    ex_run(ex, "INSERT INTO t VALUES (100, 1, 'x')", s2)
    assert ex_run(ex, 'COMMIT', s2)['type'] == 'ok'
    cur = ex_run(ex, 'SELECT COUNT(*) FROM t WHERE id = 100')
    assert cur['rows'][0][0] == 1


def test_old_files_frozen_after_more_writes(tmp_path, monkeypatch):
    """轮转后旧文件字节不再变化（只读保留），新事件全部追加到当前文件"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    monkeypatch.setenv('LIGHTDB_BINLOG_MAX_BYTES', str(SMALL_MAX))
    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    make_table(ex)
    seed_rows(ex, 60)
    files = binlog_files(db)
    assert len(files) >= 2
    frozen = {f: os.path.getsize(os.path.join(binlog_dir(db), f))
              for f in files[:-1]}
    cur_file, cur_pos = current_binlog(db, ex)
    seq_before = len(fetch_rows(ex))
    for i in range(200, 260):
        ex_run(ex, f"INSERT INTO t VALUES ({i}, {i}, 'x{i}')")
    for f, size in frozen.items():
        assert os.path.getsize(os.path.join(binlog_dir(db), f)) == size, f
    rows = fetch_rows(ex)
    assert len(rows) == seq_before + 60
    assert rows[-1][0] == seq_before + 60
    db.shutdown()


def _patch_write_failure(monkeypatch, rec, times=1):
    """让 recorder 的组提交写盘失败指定次数（之后恢复真实实现）"""
    orig = rec._write_blob
    state = {'left': times}

    def failing(blob):
        if state['left'] > 0:
            state['left'] -= 1
            raise BinlogWriteError('模拟磁盘写失败（测试注入）')
        return orig(blob)

    monkeypatch.setattr(rec, '_write_blob', failing)
    return state


def test_binlog_write_failure_rejects_commit_and_rolls_back(env, monkeypatch):
    db, ex = env
    make_table(ex)
    rec = db.binlog_log
    assert rec is not None
    _patch_write_failure(monkeypatch, rec, times=1)

    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, "INSERT INTO t VALUES (1, 10, 'a')", s)
    r = ex_run(ex, 'COMMIT', s)
    # 显式错误：1180（ER_ERROR_DURING_COMMIT）+ HY000，报文点明 binlog
    assert r['type'] == 'error', r
    assert r['code'] == 1180, r
    assert 'binlog' in r['message'], r
    assert (r.get('sqlstate') or 'HY000') == 'HY000'
    # 回滚后数据不可见（新会话/新读视图）
    chk = ex_run(ex, 'SELECT COUNT(*) FROM t')
    assert chk['type'] == 'resultset' and chk['rows'][0][0] == 0, chk
    # 恢复后重试成功，数据可见
    s2 = {}
    ex_run(ex, 'BEGIN', s2)
    ex_run(ex, "INSERT INTO t VALUES (2, 20, 'b')", s2)
    r = ex_run(ex, 'COMMIT', s2)
    assert r['type'] == 'ok', r
    chk = ex_run(ex, 'SELECT id FROM t')
    assert [x[0] for x in chk['rows']] == [2], chk
    # 后续 autocommit 写也恢复
    r = ex_run(ex, "INSERT INTO t VALUES (3, 30, 'c')")
    assert r['type'] == 'ok', r


def test_multirow_insert_binlog_failure_rolls_back_statement(env, monkeypatch):
    db, ex = env
    make_table(ex)
    rec = db.binlog_log
    _patch_write_failure(monkeypatch, rec, times=1)
    # 多值 INSERT 走语句级内部事务（commit_statement）→ 失败整条语句回滚
    r = ex_run(ex, "INSERT INTO t VALUES (1, 10, 'a'), (2, 20, 'b')")
    assert r['type'] == 'error', r
    assert r['code'] == 1180, r
    chk = ex_run(ex, 'SELECT COUNT(*) FROM t')
    assert chk['rows'][0][0] == 0, chk
    # 恢复后重试
    r = ex_run(ex, "INSERT INTO t VALUES (1, 10, 'a'), (2, 20, 'b')")
    assert r['type'] == 'ok', r
    assert ex_run(ex, 'SELECT COUNT(*) FROM t')['rows'][0][0] == 2


def test_fsync_failure_rejects_commit(env, monkeypatch):
    db, ex = env
    make_table(ex)
    rec = db.binlog_log
    rec.fsync = True

    def bad_sync():
        raise BinlogWriteError('模拟 fsync 失败（测试注入）')

    monkeypatch.setattr(rec, '_sync', bad_sync)
    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, "INSERT INTO t VALUES (9, 90, 'x')", s)
    r = ex_run(ex, 'COMMIT', s)
    assert r['type'] == 'error' and r['code'] == 1180, r
    assert 'fsync' in r['message'] or 'binlog' in r['message'], r
    assert ex_run(ex, 'SELECT COUNT(*) FROM t')['rows'][0][0] == 0
    # 恢复 fsync 后重试
    monkeypatch.undo()
    rec.fsync = False
    s2 = {}
    ex_run(ex, 'BEGIN', s2)
    ex_run(ex, "INSERT INTO t VALUES (9, 90, 'x')", s2)
    assert ex_run(ex, 'COMMIT', s2)['type'] == 'ok'
    assert ex_run(ex, 'SELECT id FROM t')['rows'] == [(9,)]


def test_binlog_strict_disabled_degrades_to_warning(env, monkeypatch):
    db, ex = env
    make_table(ex)
    # 重新按 strict=0 打开一个独立库（环境变量在建库时读取）
    monkeypatch.setenv('LIGHTDB_BINLOG_STRICT', '0')
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    d2 = str(db.data_dir) + '_lax'
    db2 = track(Database(d2))
    ex2 = track(Executor(db2))
    make_table(ex2)
    rec = db2.binlog_log
    _patch_write_failure(monkeypatch, rec, times=1)
    s = {}
    ex_run(ex2, 'BEGIN', s)
    ex_run(ex2, "INSERT INTO t VALUES (1, 10, 'a')", s)
    r = ex_run(ex2, 'COMMIT', s)
    # 降级模式：提交成功（只告警），数据可见
    assert r['type'] == 'ok', r
    assert ex_run(ex2, 'SELECT id FROM t')['rows'] == [(1,)]
    db2.shutdown()


# ==================== 5. 半同步移除 / GTID 决策 ====================

def test_semi_sync_removed_from_codebase():
    import lightdb.replication as rep_pkg

    assert not hasattr(rep_pkg, 'SemiSyncMaster')
    assert not hasattr(rep_pkg, 'SemiSyncSlave')
    with pytest.raises(ImportError):
        importlib.import_module('lightdb.replication.semi_sync')
    # 配置项已删除
    from lightdb.config import Config
    assert not hasattr(Config(), 'semi_sync_enabled')
    # 源码无残留引用（允许两处例外并说明）：
    #   * semi_sync.py 墓碑文件本身（历史导入路径，显式 ImportError）；
    #   * lightdb/net/worker.py 的 `self.semi_sync = None` —— net/** 是
    #     本次不可改范围（另一成员在做 COM_STMT），且只是属性占位。
    pat = ('SemiSyncMaster', 'SemiSyncSlave', 'from .semi_sync',
           'semi_sync import', 'semi_sync_enabled')
    offenders = []
    for base, dirs, names in os.walk(os.path.join(ROOT, 'lightdb')):
        dirs[:] = [x for x in dirs if x != '__pycache__']
        for name in names:
            if not name.endswith('.py'):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace('\\', '/')
            if rel == 'lightdb/replication/semi_sync.py':
                continue
            try:
                text = open(path, encoding='utf-8', errors='ignore').read()
            except OSError:
                continue
            for token in pat:
                if token in text:
                    offenders.append((rel, token))
    assert offenders == [], offenders
    # net/worker.py 的占位属性（不可改文件）单独确认存在且不在本次修改范围
    worker = open(os.path.join(ROOT, 'lightdb', 'net', 'worker.py'),
                  encoding='utf-8', errors='ignore').read()
    assert 'semi_sync' not in worker or 'self.semi_sync = None' in worker


def test_gtid_decision_position_based(env):
    db, ex = env
    make_table(ex)
    seed_rows(ex, 5)
    r = ex_run(ex, 'SHOW MASTER STATUS')
    assert r['columns'] == ['File', 'Position', 'Binlog_Do_DB',
                            'Binlog_Ignore_DB', 'Executed_Gtid_Set']
    assert r['rows'][0][4] is None, r        # 无 GTID（决策：不实现）
    st = ex_run(ex, 'SHOW SLAVE STATUS')
    d = dict(zip(st['columns'], st['rows'][0]))
    assert d['Retrieved_Gtid_Set'] is None
    assert d['Executed_Gtid_Set'] is None
    assert d['Auto_Position'] == 0
    # 模块文档注明决策（grep 语义，不编造 GTID）
    doc = importlib.import_module(
        'lightdb.replication.append_only_binlog').__doc__
    assert '无 GTID' in doc or '不实现 GTID' in doc, doc[:200]


# ==================== 6. UPDATE/DELETE 镜像一致性 ====================

def _events_by_type(ex, etype):
    return [json.loads(r[5]) for r in fetch_rows(ex) if r[3] == etype]


def _select_rows(ex, name='t'):
    r = ex_run(ex, f'SELECT * FROM {name} ORDER BY id')
    cols = r['columns']
    return [dict(zip(cols, row)) for row in r['rows']]


def test_update_delete_mirror_matches_manual_select(env):
    db, ex = env
    make_table(ex)
    seed_rows(ex, 20)
    before = {r['id']: r for r in _select_rows(ex)}

    # autocommit UPDATE（无触发器 → 镜像来自 WAL 增量，不再语句前 SELECT）
    r = ex_run(ex, "UPDATE t SET v = 999, s = 'upd' "
                   "WHERE id >= 3 AND id <= 7")
    assert r['type'] == 'ok', r
    after = {r['id']: r for r in _select_rows(ex)}
    ups = [d for d in _events_by_type(ex, 'UPDATE') if d['pk'] in (3, 4, 5, 6, 7)]
    assert len(ups) == 5, ups
    for d in ups:
        pk = d['pk']
        old = dict(zip(d['cols'], d['old']))
        new = dict(zip(d['cols'], d['new']))
        assert old == before[pk], (pk, old, before[pk])
        assert new == after[pk], (pk, new, after[pk])

    # autocommit DELETE
    pre_del = {r['id']: r for r in _select_rows(ex)}
    r = ex_run(ex, 'DELETE FROM t WHERE id >= 10 AND id <= 12')
    assert r['type'] == 'ok', r
    dels = [d for d in _events_by_type(ex, 'DELETE') if d['pk'] in (10, 11, 12)]
    assert len(dels) == 3, dels
    for d in dels:
        old = dict(zip(d['cols'], d['old']))
        assert old == pre_del[d['pk']], (d['pk'], old, pre_del[d['pk']])

    # 整表 UPDATE（大范围）：镜像数与 affected 一致、内容与 SELECT 一致
    pre_all = {r['id']: r for r in _select_rows(ex)}
    r = ex_run(ex, 'UPDATE t SET v = 777')
    assert r['type'] == 'ok' and r['affected'] == len(pre_all), r
    all_ups = _events_by_type(ex, 'UPDATE')[-len(pre_all):]
    assert len(all_ups) == len(pre_all)
    post_all = {r['id']: r for r in _select_rows(ex)}
    for d in all_ups:
        assert dict(zip(d['cols'], d['old'])) == pre_all[d['pk']]
        assert dict(zip(d['cols'], d['new'])) == post_all[d['pk']]


def test_update_delete_mirror_in_explicit_transaction(env):
    db, ex = env
    make_table(ex)
    seed_rows(ex, 10)
    before = {r['id']: r for r in _select_rows(ex)}
    s = {}
    ex_run(ex, 'BEGIN', s)
    ex_run(ex, 'UPDATE t SET s = \'txn\' WHERE id = 2', s)
    ex_run(ex, 'DELETE FROM t WHERE id = 3', s)
    ex_run(ex, 'COMMIT', s)
    ups = [d for d in _events_by_type(ex, 'UPDATE') if d['pk'] == 2]
    dels = [d for d in _events_by_type(ex, 'DELETE') if d['pk'] == 3]
    assert len(ups) == 1 and len(dels) == 1, (ups, dels)
    assert dict(zip(ups[0]['cols'], ups[0]['old'])) == before[2]
    assert ups[0]['new'][2] == 'txn'
    assert dict(zip(dels[0]['cols'], dels[0]['old'])) == before[3]
    # 事务组边界：BEGIN → UPDATE/DELETE → COMMIT 且 txn_id 非 0
    rec = AppendOnlyBinlog(str(db.data_dir), max_bytes=MAX_BYTES_DEFAULT)
    try:
        recs = rec.read_all()
    finally:
        rec.close()
    data_idx = [i for i, r in enumerate(recs)
                if r.type in (TYPE_UPDATE, TYPE_DELETE)]
    assert recs[data_idx[0] - 1].type == 2       # BEGIN
    assert recs[data_idx[-1] + 1].type == 3      # COMMIT
    txn_ids = {recs[i].txn_id for i in range(data_idx[0] - 1,
                                             data_idx[-1] + 2)}
    assert len(txn_ids) == 1 and list(txn_ids)[0] not in (None, 0)


def test_update_delete_mirror_avoids_extra_select(env, monkeypatch):
    """阶段 2 核心断言：WAL 镜像路径**不再执行语句前的 SELECT**

    用 `tbl.select` 调用计数做确定性证据（不依赖计时）：

    * 默认（WAL 路径）：UPDATE/DELETE 语句执行期间 select 计数为 0
      （旧行镜像直接读 WAL 增量）；
    * `LIGHTDB_BINLOG_MIRROR=legacy`：select 计数 >= 1（阶段 1 行为，
      供 A/B 对照），且两种模式产出的镜像内容完全一致。
    """
    db, ex = env
    make_table(ex)
    seed_rows(ex, 20)
    tbl = db.get_table('t')
    orig_select = tbl.select
    calls = {'n': 0}

    def counting_select(*a, **kw):
        calls['n'] += 1
        return orig_select(*a, **kw)

    monkeypatch.setattr(tbl, 'select', counting_select)
    # WAL 路径：UPDATE/DELETE 都不再调用 tbl.select
    r = ex_run(ex, 'UPDATE t SET v = 555 WHERE id >= 5 AND id <= 9')
    assert r['type'] == 'ok' and r['affected'] == 5, r
    assert calls['n'] == 0, f'UPDATE 仍执行了 {calls["n"]} 次 tbl.select'
    r = ex_run(ex, 'DELETE FROM t WHERE id >= 15 AND id <= 17')
    assert r['type'] == 'ok' and r['affected'] == 3, r
    assert calls['n'] == 0, f'DELETE 仍执行了 {calls["n"]} 次 tbl.select'
    wal_ups = [d for d in _events_by_type(ex, 'UPDATE') if d['pk'] in
               (5, 6, 7, 8, 9)]
    wal_dels = [d for d in _events_by_type(ex, 'DELETE') if d['pk'] in
                (15, 16, 17)]
    assert len(wal_ups) == 5 and len(wal_dels) == 3, (wal_ups, wal_dels)

    # legacy 对照：同一个库另开实例？—— 用独立数据目录，镜像内容应完全一致
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    monkeypatch.setenv('LIGHTDB_BINLOG_MIRROR', 'legacy')
    db2 = track(Database(str(db.data_dir) + '_legacy'))
    ex2 = track(Executor(db2))
    make_table(ex2)
    seed_rows(ex2, 20)
    calls2 = {'n': 0}
    tbl2 = db2.get_table('t')
    orig2 = tbl2.select

    def counting2(*a, **kw):
        calls2['n'] += 1
        return orig2(*a, **kw)

    monkeypatch.setattr(tbl2, 'select', counting2)
    ex_run(ex2, 'UPDATE t SET v = 555 WHERE id >= 5 AND id <= 9')
    ex_run(ex2, 'DELETE FROM t WHERE id >= 15 AND id <= 17')
    assert calls2['n'] >= 2, 'legacy 模式应当为更新/删除各做一次预先 SELECT'
    leg_ups = [d for d in _events_by_type(ex2, 'UPDATE') if d['pk'] in
               (5, 6, 7, 8, 9)]
    leg_dels = [d for d in _events_by_type(ex2, 'DELETE') if d['pk'] in
                (15, 16, 17)]
    assert leg_ups == wal_ups, (leg_ups[:1], wal_ups[:1])
    assert leg_dels == wal_dels, (leg_dels[:1], wal_dels[:1])
    db2.shutdown()


def test_server_strict_commit_error_visible_to_client(env, monkeypatch):
    """服务端（pymysql）路径：binlog 写失败 → 客户端收到 1180 错误"""
    import pymysql

    db, ex = env
    make_table(ex)
    rec = db.binlog_log
    _patch_write_failure(monkeypatch, rec, times=1)
    port = PORT_BASE + 5
    srv = track(_start_master(db, ex, port))
    conn = track(pymysql.connect(host='127.0.0.1', port=port, user='root',
                                 autocommit=True, connect_timeout=5))
    cur = conn.cursor()
    cur.execute('BEGIN')
    cur.execute("INSERT INTO t VALUES (1, 10, 'a')")
    with pytest.raises(pymysql.err.OperationalError) as ei:
        cur.execute('COMMIT')
    assert ei.value.args[0] == 1180, ei.value.args
    assert 'binlog' in str(ei.value.args[1]), ei.value.args
    # 数据不可见（事务已回滚）
    cur.execute('SELECT COUNT(*) FROM t')
    assert cur.fetchone()[0] == 0
    # 恢复后重试成功
    cur.execute('BEGIN')
    cur.execute("INSERT INTO t VALUES (2, 20, 'b')")
    cur.execute('COMMIT')
    cur.execute('SELECT id FROM t')
    assert [r[0] for r in cur.fetchall()] == [2]
    conn.close()
    srv.shutdown()
