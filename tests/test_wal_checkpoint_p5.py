# -*- coding: utf-8 -*-
"""P5 引擎可靠性验收：WAL checkpoint/轮转 + 数据压实（COMPACT）

覆盖：

1. **WAL 有界 + checkpoint 触发**：
   * 按 WAL 记录条数阈值触发（小阈值可观测），WAL 段落被截断、数据仍可读；
   * 按 WAL 字节数阈值触发（默认 1 MB，长跑 6000 行后 WAL 仍 < 2 MB）；
   * 按**时间**阈值触发（P5 新增维度，含 `Last_checkpoint_reason='time'`）；
   * `SHOW WAL STATUS` 暴露真实状态（字节/条数/阈值/次数/最近原因/`.dat` 大小）；
   * checkpoint 后重启：数据正确、WAL 为空、可继续写入。
2. **数据压实（COMPACT / VACUUM FULL）**：
   * 多轮 UPDATE 使 `.dat` 膨胀后，压实**真实缩小**文件，前后行/索引一致；
   * 压实后重启数据仍一致；重复压实幂等（无收益时显式 skipped=no_gain）；
   * 有未提交事务时显式跳过（不冒险重写文件）；
   * 崩溃注入（子进程在"pending 已写 / 已替换部分文件"时 `os._exit`）→
     重开后表完整、pending 清除、数据一致（`_pre_recover_compact` 补完）。
3. **崩溃恢复**：checkpoint 之后的已提交数据在硬杀后不丢（复用既有风格）。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _db(tmp_path):
    from lightdb.db.database import Database
    return Database(str(tmp_path / 'data'))


def _dat_size(d, table='t'):
    p = os.path.join(str(d), table + '.dat')
    return os.path.getsize(p) if os.path.exists(p) else 0


def _wal_size(d, table='t'):
    p = os.path.join(str(d), table + '.wal')
    return os.path.getsize(p) if os.path.exists(p) else 0


def _digest(rows):
    h = hashlib.sha256()
    for row in sorted((tuple(r) for r in rows), key=repr):
        h.update(repr(row).encode('utf-8'))
    return h.hexdigest()[:16]


def _dump(tbl):
    rows = [r for _pk, r in tbl._live_rows()]
    return len(rows), _digest(rows)


@pytest.fixture(autouse=True)
def _reset_checkpoint_attrs():
    """类属性会跨用例生效：每个用例前后恢复默认，避免相互污染"""
    from lightdb.db.table import Table
    saved = (Table.wal_checkpoint_bytes, Table.wal_checkpoint_records,
             Table.wal_checkpoint_seconds)
    yield
    (Table.wal_checkpoint_bytes, Table.wal_checkpoint_records,
     Table.wal_checkpoint_seconds) = saved


# ==================== 1. WAL checkpoint / 轮转 ====================

def test_wal_record_threshold_triggers_checkpoint(tmp_path):
    """按记录条数触发：WAL 段落被截断，checkpoint 计数/原因真实"""
    from lightdb.db.table import Table
    Table.wal_checkpoint_records = 100
    Table.wal_checkpoint_bytes = 0
    db = _db(tmp_path)
    try:
        tbl = db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True},
            {'name': 'v', 'type': 'INT'}])
        max_records = 0
        for i in range(400):
            tbl.insert([i, i])
            max_records = max(max_records, tbl.wal.record_count())
        st = tbl.wal_status()
        assert st['checkpoints'] >= 3, st
        assert st['last_checkpoint_reason'] == 'records', st
        # 有界: 触发后段落清零，块内最多积累到阈值附近（单行语句）
        assert max_records <= 100, (max_records, st)
        assert tbl.row_count == 400
        # 数据在 checkpoint 后仍可读（页已落盘、只退休日志）
        got = [tuple(r) for r in tbl.select(
            ['v'], {'column': 'id', 'op': '=', 'value': 399})[1]]
        assert got == [(399,)], got
        print(f"\n    记录阈值=100: 400 行插入后 WAL 最大={max_records} 条，"
              f"checkpoint={st['checkpoints']} 次，原因={st['last_checkpoint_reason']}，"
              f"WAL 文件={_wal_size(db.data_dir)} B")
    finally:
        db.shutdown()


def test_wal_growth_bounded_long_run(tmp_path):
    """长跑 6000 行：默认 1 MB 阈值下 WAL 有界，不做无限增长的日志"""
    from lightdb.db.table import Table
    Table.wal_checkpoint_bytes = 1 * 1024 * 1024
    Table.wal_checkpoint_records = 5000
    Table.wal_checkpoint_seconds = 0
    db = _db(tmp_path)
    try:
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v INT)', {})
        tbl = db.get_table('t')
        peak_wal = 0
        for batch in range(12):
            values = ','.join(f'({batch * 500 + i}, {i})'
                              for i in range(500))
            r = ex.execute_sql(f'INSERT INTO t VALUES {values}', {})
            assert r.get('type') != 'error', r
            peak_wal = max(peak_wal, tbl.wal.size_bytes())
        final_wal = tbl.wal.size_bytes()
        st = tbl.wal_status()
        assert st['checkpoints'] >= 1, st
        # 有界: 单批 500 行的提交点最多让 WAL 越过阈值一个批次
        assert peak_wal <= 1 * 1024 * 1024 + 600_000, peak_wal
        assert tbl.row_count == 6000
        print(f"\n    6000 行: WAL 峰值={peak_wal} B，最终={final_wal} B，"
              f"checkpoint={st['checkpoints']} 次"
              f"（阈值 {st['limit_bytes']} B），.dat={_dat_size(db.data_dir)} B")
    finally:
        db.shutdown()


def test_time_based_checkpoint_trigger(tmp_path):
    """时间维度触发（P5 新增）：距上次 checkpoint 超过阈值后下一次写入触发"""
    from lightdb.db.table import Table
    Table.wal_checkpoint_bytes = 0
    Table.wal_checkpoint_records = 0
    Table.wal_checkpoint_seconds = 0.1
    db = _db(tmp_path)
    try:
        tbl = db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True}])
        for i in range(20):
            tbl.insert([i])
        assert tbl.wal_status()['checkpoints'] == 0
        assert tbl.wal.record_count() > 0
        time.sleep(0.15)
        tbl.insert([1000])
        st = tbl.wal_status()
        assert st['checkpoints'] >= 1, st
        assert st['last_checkpoint_reason'] == 'time', st
        assert st['wal_records'] == 0, st
        assert tbl.select(['id'], None)[0] == ['id']
        # 数据在时间触发 checkpoint 后仍完整
        assert len(tbl.select(['id'], None)[1]) == 21
        print(f"\n    时间阈值=0.1s: sleep 后写入触发 checkpoint="
              f"{st['checkpoints']} 次（原因={st['last_checkpoint_reason']}），"
              f"WAL 退休量={st['last_checkpoint_wal_bytes']} B")
    finally:
        db.shutdown()


def test_show_wal_status_is_real(tmp_path):
    """`SHOW WAL STATUS` 的列与取值来自真实 WAL/磁盘，不是编造"""
    from lightdb.db.table import Table
    Table.wal_checkpoint_records = 1000
    Table.wal_checkpoint_bytes = 0
    Table.wal_checkpoint_seconds = 0
    db = _db(tmp_path)
    try:
        tbl = db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True}])
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        for i in range(30):
            tbl.insert([i])
        r = ex.execute_sql('SHOW WAL STATUS', {})
        assert r.get('type') == 'resultset', r
        cols = r['columns']
        assert cols == ['Table', 'Wal_bytes', 'Wal_records', 'Limit_bytes',
                        'Limit_records', 'Limit_seconds', 'Checkpoints',
                        'Last_checkpoint_reason',
                        'Last_checkpoint_age_seconds',
                        'Last_checkpoint_wal_bytes',
                        'Last_checkpoint_wal_records', 'Dat_bytes',
                        'Free_pages', 'Rows'], cols
        row = dict(zip(cols, r['rows'][0]))
        st = tbl.wal_status()
        assert row['Wal_bytes'] == st['wal_bytes'] == tbl.wal.size_bytes()
        assert row['Wal_records'] == st['wal_records'] == tbl.wal.record_count()
        assert row['Dat_bytes'] == st['dat_bytes'] == _dat_size(db.data_dir)
        assert row['Limit_records'] == 1000 and row['Limit_seconds'] == 0
        assert row['Rows'] == 30
        # 显式 checkpoint 后：次数/原因/退休量如实变化
        tbl.checkpoint(force=True, reason='manual')
        row = dict(zip(cols, ex.execute_sql('SHOW WAL STATUS', {})['rows'][0]))
        assert row['Checkpoints'] == 1
        assert row['Last_checkpoint_reason'] == 'manual'
        assert row['Wal_bytes'] == 0 and row['Wal_records'] == 0
        assert row['Last_checkpoint_wal_records'] > 0
        print(f"\n    SHOW WAL STATUS: checkpoint={row['Checkpoints']} 次，"
              f"最近原因={row['Last_checkpoint_reason']}，"
              f"退休={row['Last_checkpoint_wal_bytes']} B / "
              f"{row['Last_checkpoint_wal_records']} 条，"
              f".dat={row['Dat_bytes']} B")
    finally:
        db.shutdown()


def test_checkpoint_then_restart_data_correct(tmp_path):
    """显式 checkpoint → 干净重启：数据/索引正确，WAL 为空"""
    db = _db(tmp_path)
    try:
        tbl = db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True},
            {'name': 'v', 'type': 'INT'}])
        for i in range(500):
            tbl.insert([i, i * 2])
        before = _dump(tbl)
        n = db.checkpoint(force=True)
        assert n == 1
        assert _wal_size(db.data_dir) == 0
        data_dir = str(db.data_dir)
    finally:
        db.shutdown()
    from lightdb.db.database import Database
    db2 = Database(data_dir)
    try:
        tbl2 = db2.get_table('t')
        assert _dump(tbl2) == before
        assert _wal_size(data_dir) == 0
        assert tbl2.wal.size_bytes() == 0
        print(f"\n    checkpoint 后重启: {before[0]} 行 sha={before[1]}，"
              f"WAL={tbl2.wal.size_bytes()} B")
    finally:
        db2.shutdown()


# ==================== 2. 数据压实（COMPACT / VACUUM FULL） ====================

def _make_inflated_table(tmp_path, rows=400, rounds=8):
    db = _db(tmp_path)
    tbl = db.create_table('t', [
        {'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}])
    for i in range(rows):
        tbl.insert([i, 0])
    for rnd in range(1, rounds + 1):
        tbl.update({'v': rnd}, None)
    tbl.flush()
    return db, tbl


def test_compact_shrinks_inflated_dat_and_keeps_data(tmp_path):
    """多轮 UPDATE 膨胀后 COMPACT 真实缩小 .dat，前后行/索引一致"""
    db, tbl = _make_inflated_table(tmp_path)
    try:
        before_bytes = _dat_size(db.data_dir)
        before_int = tbl.integrity_scan(deep=True)
        before_dump = _dump(tbl)
        before_ddl = None
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        before_ddl = ex.execute_sql('SHOW CREATE TABLE t', {})['rows'][0][1]

        stats = db.compact()
        assert stats['t']['skipped'] is False, stats
        st = stats['t']
        after_bytes = _dat_size(db.data_dir)
        new_tbl = db.get_table('t')
        after_int = new_tbl.integrity_scan(deep=True)
        after_dump = _dump(new_tbl)
        after_ddl = ex.execute_sql('SHOW CREATE TABLE t', {})['rows'][0][1]

        assert st['index_entries_after'] == before_int['index_entries'] == 400
        assert after_dump == before_dump, (before_dump, after_dump)
        assert after_ddl == before_ddl
        assert after_int['live_rows'] == 400
        assert after_int['index_entries'] == 400
        assert after_int['chain_versions'] == 400, after_int
        assert not after_int['dangling_prev']
        # 真实文件下降（实测 400 行/8 轮约 132 KB → 约 32 KB）
        assert after_bytes < before_bytes, (before_bytes, after_bytes)
        ratio = after_bytes / before_bytes if before_bytes else 1.0
        assert ratio <= 0.65, (before_bytes, after_bytes, ratio)
        print(f"\n    {before_int['chain_versions']} 个版本 → COMPACT: "
              f".dat {before_bytes} → {after_bytes} B "
              f"（-{before_bytes - after_bytes} B，"
              f"{ratio * 100:.1f}%），索引项 400→400，"
              f"dump sha={after_dump[1]}")
    finally:
        db.shutdown()


def test_compact_via_sql_and_restart(tmp_path):
    """SQL `VACUUM FULL` 走同一压实路径；压实后重启数据/索引一致"""
    db, tbl = _make_inflated_table(tmp_path, rows=300, rounds=6)
    db.checkpoint(force=True)
    before_dump = _dump(tbl)
    data_dir = str(db.data_dir)
    try:
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        r = ex.execute_sql('VACUUM FULL', {})
        assert r.get('type') == 'resultset', r
        assert r['columns'][:5] == ['Table', 'Rows', 'Dat_before',
                                    'Dat_after', 'Bytes_freed']
        row = dict(zip(r['columns'], r['rows'][0]))
        assert row['Table'] == 't' and row['Rows'] == 300
        assert row['Dat_after'] < row['Dat_before'], row
        after_dump = _dump(db.get_table('t'))
        assert after_dump == before_dump
    finally:
        db.shutdown()
    from lightdb.db.database import Database
    db2 = Database(data_dir)
    try:
        tbl2 = db2.get_table('t')
        assert _dump(tbl2) == before_dump
        ints = tbl2.integrity_scan(deep=True)
        assert ints['index_entries'] == 300 and ints['live_rows'] == 300
        print(f"\n    VACUUM FULL → 重启: 行/索引一致，"
              f".dat={_dat_size(data_dir)} B，"
              f"index_entries={ints['index_entries']}")
    finally:
        db2.shutdown()


def test_compact_idempotent_and_skips_with_active_txn(tmp_path):
    """重复压实幂等；有未提交事务时显式跳过（绝不冒险重写文件）"""
    db, tbl = _make_inflated_table(tmp_path, rows=200, rounds=4)
    try:
        first = db.compact()['t']
        assert first['skipped'] is False, first
        size_after_first = _dat_size(db.data_dir)
        # COMPACT 会替换内存 Table 对象（旧句柄已关闭）：调用方需重新 get_table
        tbl = db.get_table('t')
        second = db.compact()['t']
        assert second['skipped'] is True and \
            second['reason'] == 'no_gain', second
        assert _dat_size(db.data_dir) == size_after_first

        # 未提交事务：跳过并保持数据可回滚
        tbl = db.get_table('t')
        tbl.insert([10000, 1])
        txn = db.begin()
        tbl.update({'v': 999}, {'column': 'id', 'op': '=', 'value': 1},
                   txn_id=txn['txn_id'], read_view=txn['read_view'])
        skipped = db.compact()['t']
        assert skipped['skipped'] is True, skipped
        assert '未提交事务' in skipped['reason'], skipped
        db.abort(txn)
        got = [tuple(r) for r in tbl.select(
            ['v'], {'column': 'id', 'op': '=', 'value': 1})[1]]
        assert got == [(4,)], got
        print(f"\n    重复 COMPACT: 第一次释放 {first['bytes_freed']} B，"
              f"第二次 skipped={second['reason']}，"
              f"有未提交事务时 skipped（{skipped['reason']}）")
    finally:
        db.shutdown()


# ==================== 3. 崩溃安全 ====================

def test_compact_crash_recovery(tmp_path):
    """子进程在 COMPACT 的 pending/替换阶段硬杀 → 重开后补完且数据完整

    两种注入点：
      * before：`os.replace` 第一次被调用时（pending 已写、文件未切换）；
      * after_one：替换完第一个文件后（`.dat` 已是新文件、`.btroot` 还是旧）。
    恢复路径 `_pre_recover_compact()` 必须在打开文件前把剩余临时文件切换到位。
    """
    data_dir = str(tmp_path / 'data_crash')
    code = r'''
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
import lightdb.db.database as dbmod

data = os.environ["DATA_DIR"]
db = Database(data)
t = db.create_table("t", [
    {"name": "id", "type": "INT", "primary": True},
    {"name": "v", "type": "INT"}])
for i in range(100):
    t.insert([i, i])
for rnd in range(1, 4):
    t.update({"v": rnd}, None)
db.checkpoint(force=True)
real_replace = os.replace
state = {"n": 0}
def boom(src, dst):
    # 只拦截 COMPACT 的数据文件替换；meta 写入等其它 os.replace 照常放行
    if not str(dst).endswith((".dat", ".btroot", ".free")):
        return real_replace(src, dst)
    state["n"] += 1
    if os.environ.get("CRASH_MODE") == "before":
        os._exit(7)
    real_replace(src, dst)
    os._exit(7)
dbmod.os.replace = boom
db.compact(["t"])
os._exit(0)
'''
    for mode in ('before', 'after_one'):
        d = data_dir + '_' + mode
        shutil.rmtree(d, ignore_errors=True)
        env = dict(os.environ)
        env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
        env['DATA_DIR'] = d
        proc = subprocess.run([sys.executable, '-c', code], env=env,
                              capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=180)
        assert proc.returncode == 7, (mode, proc.returncode,
                                      proc.stdout[-500:], proc.stderr[-800:])
        # 重开：启动恢复必须补完文件切换，数据/索引完整
        from lightdb.db.database import Database
        db = Database(d)
        try:
            assert db._pending_ddl is None, (mode, db._pending_ddl)
            tbl = db.get_table('t')
            assert tbl.row_count == 100, (mode, tbl.row_count)
            vals = tbl.select(['v'], None)[1]
            assert vals and all(v[0] == 3 for v in vals), (mode, vals[:3])
            ints = tbl.integrity_scan(deep=True)
            assert ints['index_entries'] == 100 and ints['live_rows'] == 100, \
                (mode, ints)
            assert not ints['dangling_prev'], (mode, ints)
            assert _wal_size(d) == 0, (mode, _wal_size(d))
            print(f"\n    崩溃注入[{mode}] → 重开: 100 行完整、"
                  f"索引项={ints['index_entries']}、pending 已清、"
                  f".dat={_dat_size(d)} B")
        finally:
            db.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def test_crash_after_checkpoint_keeps_committed_rows(tmp_path):
    """checkpoint 之后硬杀：已提交行不丢（WAL 已退休、数据页已 fsync）"""
    data_dir = str(tmp_path / 'data_kill')
    code = r'''
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
db = Database(os.environ["DATA_DIR"])
t = db.create_table("t", [
    {"name": "id", "type": "INT", "primary": True},
    {"name": "v", "type": "INT"}])
for i in range(200):
    t.insert([i, i])
db.checkpoint(force=True)
for i in range(200, 260):
    t.insert([i, i])
os._exit(9)
'''
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['DATA_DIR'] = data_dir
    proc = subprocess.run([sys.executable, '-c', code], env=env,
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    assert proc.returncode == 9, (proc.returncode, proc.stderr[-800:])
    from lightdb.db.database import Database
    db = Database(data_dir)
    try:
        tbl = db.get_table('t')
        # checkpoint 保证 200 行；之后 60 行是 autocommit 已 flush 到 OS 的
        # 隐式提交，进程被硬杀也能恢复（≥200 且完整有序）
        assert tbl.row_count >= 200, tbl.row_count
        vals = sorted(v for (v,) in tbl.select(['id'], None)[1])
        assert vals == list(range(len(vals))), vals[:5]
        assert tbl.integrity_scan(deep=True)['index_entries'] == len(vals)
        print(f"\n    checkpoint 后硬杀: 恢复 {len(vals)} 行"
              f"（checkpoint 200 + 之后已 flush 的 {len(vals) - 200}）")
    finally:
        db.shutdown()
