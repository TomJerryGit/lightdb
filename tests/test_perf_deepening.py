# -*- coding: utf-8 -*-
"""P5.1 验收：WAL 命名多段轮转 + 非唯一二级索引真实加速

一、WAL（`lightdb/storage/wal.py`）
    * 活动段仍是 `<table>.wal`（兼容旧观测/测试），超过可配置段大小后
      改名为 `<table>-wal-NNNNNN.log` 归档，再开新活动段；LSN 跨段单调。
    * checkpoint 后只保留最近 K 段（默认 0 = 旧语义"清空"），更旧段删除；
      量化上界：`总数 <= K*(segment_bytes + 单条记录上限) (+活动段)`。
    * 启动按段序重放，跨段崩溃恢复成立；恢复成功后才轮转/退休 WAL。
二、二级索引（`lightdb/db/table.py`）
    * 单列非唯一索引的真实内存映射：等值 / 范围 / ORDER BY col LIMIT
      加速，结果与全表扫描逐行一致；写后标脏、查询时重建，崩溃/重启后
      按数据重建；有活跃事务或不支持的表达式（OR/LIKE/表达式/非最左
      前缀）安全回退全表扫描。
    * `SHOW INDEX`：单列/多列索引 Index_type=BTREE（task-59 起多列按
      最左前缀加速）；Cardinality 是真实不同非 NULL 键值个数。
    * 量化（本机 10k 行，见 send_message 报告）：点查 ~40x、范围 ~10x、
      ORDER BY LIMIT ~219x。
三、量化（tests 内不跑重基准，只锁定语义与上界；数字由 probe/harness 产出）。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8947
SLACK = 2048          # 单条 WAL 记录最大余量（测试用宽松上界）


# ==================== fixtures / helpers ====================

@pytest.fixture(autouse=True)
def _reset_wal_index_config():
    from lightdb.db.table import Table
    saved = (Table.wal_segment_bytes, Table.wal_keep_segments,
             Table.wal_checkpoint_bytes, Table.wal_checkpoint_records,
             Table.wal_checkpoint_seconds)
    try:
        yield
    finally:
        (Table.wal_segment_bytes, Table.wal_keep_segments,
         Table.wal_checkpoint_bytes, Table.wal_checkpoint_records,
         Table.wal_checkpoint_seconds) = saved


def _open(path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(path))
    return db, Executor(db)


def _ok(ex, sql, s=None):
    r = ex.execute_sql(sql, s if s is not None else {})
    assert r.get('type') != 'error', (sql, r)
    return r


def _rows(ex, sql, s=None):
    return [tuple(r) for r in _ok(ex, sql, s)['rows']]


def _disable_checkpoint():
    from lightdb.db.table import Table
    Table.wal_checkpoint_bytes = 10 ** 15
    Table.wal_checkpoint_records = 0
    Table.wal_checkpoint_seconds = 0


def _seed_region_table(ex, n=3000, batch=500):
    _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, region INT, val INT)')
    for start in range(0, n, batch):
        vals = ','.join(f'({i},{i % 100},{i * 3})'
                        for i in range(start, min(start + batch, n)))
        _ok(ex, f'INSERT INTO t VALUES {vals}')


CASES = [
    'SELECT * FROM t WHERE region = 42',
    'SELECT * FROM t WHERE region = 42 AND val = 126',
    'SELECT id, val FROM t WHERE region >= 98',
    'SELECT id FROM t WHERE region <= 2',
    'SELECT id FROM t WHERE region BETWEEN 10 AND 12 ORDER BY id',
    'SELECT id, region FROM t ORDER BY region LIMIT 20',
    'SELECT id, region FROM t ORDER BY region DESC LIMIT 20',
    'SELECT * FROM t WHERE region = 12345',
]


def _scan_rows(ex, sql, tbl):
    """强制全表扫描路径的同一查询结果（临时摘掉二级索引声明）"""
    saved = list(tbl._secondary_indexes)
    try:
        tbl._secondary_indexes = []
        tbl._secondary_dirty = True
        return [tuple(r) for r in _ok(ex, sql)['rows']]
    finally:
        tbl._secondary_indexes = saved
        tbl._secondary_dirty = True


# ==================== 一、WAL 分段轮转 ====================

def test_wal_segments_rotate_named_and_bounded(tmp_path):
    from lightdb.db.table import Table
    Table.wal_segment_bytes = 512
    Table.wal_keep_segments = 0
    _disable_checkpoint()
    db, ex = _open(tmp_path / 'data_seg')
    try:
        _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(200):
            _ok(ex, f'INSERT INTO t VALUES ({i}, {i})')
        tbl = db.get_table('t')
        wal = tbl.wal
        st = wal.stats()
        assert st['archive_count'] >= 2, st
        names = [os.path.basename(p) for p in wal.segment_files()]
        archives = [n for n in names if n != 't.wal']
        assert archives, names
        assert all(re.fullmatch(r't-wal-\d{6}\.log', n) for n in archives), names
        assert st['segment_bytes'] == 512
        wal.flush_to_os()               # 让活动段缓冲落到 OS，文件大小才等于计量
        assert st['total_bytes'] == sum(
            os.path.getsize(p) for p in wal.segment_files())
        assert st['records'] == len(wal.read_all())
        # 每个归档段 <= 段大小 + 单条记录余量；活动段同样有界
        for p in st['archives']:
            assert os.path.getsize(p) <= 512 + SLACK, (p, os.path.getsize(p))
        assert wal.active_size_bytes() <= 512 + SLACK, wal.active_size_bytes()
        # LSN 跨段严格递增（read_all 按段序 + 段内顺序）
        lsns = [r.lsn for r in wal.read_all()]
        assert lsns == sorted(lsns) and len(set(lsns)) == len(lsns)
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 200
        print(f"\n    WAL 轮转：{len(archives)} 个归档段 + 活动段，"
              f"总 {st['total_bytes']}B，单段≤{512 + SLACK}B，"
              f"记录 {st['records']} 条 LSN 单调")
    finally:
        db.shutdown()


def test_checkpoint_retains_k_segments_and_prunes(tmp_path):
    from lightdb.db.table import Table
    Table.wal_segment_bytes = 512
    Table.wal_keep_segments = 2
    _disable_checkpoint()
    db, ex = _open(tmp_path / 'data_keep')
    try:
        _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(120):
            _ok(ex, f'INSERT INTO t VALUES ({i}, {i})')
        tbl = db.get_table('t')
        wal = tbl.wal
        assert wal.stats()['archive_count'] >= 3
        db.checkpoint()
        st = wal.stats()
        assert st['archive_count'] == 2, st
        assert st['active_bytes'] == 0, st
        assert st['total_bytes'] <= 2 * (512 + SLACK), st
        first = [os.path.basename(p) for p in st['archives']]
        for i in range(120, 160):
            _ok(ex, f'INSERT INTO t VALUES ({i}, {i})')
        db.checkpoint()
        st2 = wal.stats()
        assert st2['archive_count'] == 2, st2
        second = [os.path.basename(p) for p in st2['archives']]
        assert second != first, (first, second)
        # 更旧的归档确实被删
        for n in first:
            if n not in second:
                assert not (tmp_path / 'data_keep' / n).exists(), n
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 160
        # 保留的 K 段会在下次启动被重放（已 flush 的数据必须幂等，不重不丢）
        db.shutdown()
        db, ex = _open(tmp_path / 'data_keep')
        try:
            assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 160
            _ok(ex, 'INSERT INTO t VALUES (160, 160)')
            assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 161
        finally:
            db.shutdown()
        print(f"\n    checkpoint 保留 K=2 段并删更旧段：{first} -> {second}，"
              f"总量 {st2['total_bytes']}B ≤ {2 * (512 + SLACK)}B；"
              f"重启重放保留段幂等（数据 160 行不变）")
    finally:
        try:
            db.shutdown()
        except Exception:
            pass


CHILD_WAL_INSERT = r'''
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.db.table import Table
from lightdb.sql.executor import Executor
Table.wal_segment_bytes = int(os.environ.get("SEG", "512"))
Table.wal_checkpoint_bytes = 10 ** 15
Table.wal_checkpoint_records = 0
Table.wal_checkpoint_seconds = 0
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
mode = os.environ.get("MODE", "commit")
if mode == "txn_loser":
    sess = {}
    ex.execute_sql("BEGIN", sess)
    for i in range(300, 520):
        ex.execute_sql("INSERT INTO t VALUES (%d, %d)" % (i, i), sess)
    os._exit(1)
for i in range(300, 520):
    ex.execute_sql("INSERT INTO t VALUES (%d, %d)" % (i, i), {})
os._exit(1)
'''


def _run_wal_child(data_dir, mode='commit'):
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['DATA_DIR'] = str(data_dir)
    env['MODE'] = mode
    env['SEG'] = '512'
    env['LIGHTDB_ALLOW_EMPTY_PASSWORD'] = '1'
    proc = subprocess.run([sys.executable, '-c', CHILD_WAL_INSERT],
                          env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-800:])


def test_cross_segment_crash_recovery(tmp_path):
    from lightdb.db.table import Table
    data_dir = tmp_path / 'data_crash'
    db, ex = _open(data_dir)
    try:
        _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(300):
            _ok(ex, f'INSERT INTO t VALUES ({i}, {i})')
    finally:
        db.shutdown()
    _run_wal_child(data_dir, mode='commit')
    # 崩溃现场：WAL 已跨多段（归档段 + 活动段），重启后必须全部重放
    crash_files = [p for p in os.listdir(str(data_dir))
                   if re.fullmatch(r't-wal-\d{6}\.log', p)]
    assert crash_files, os.listdir(str(data_dir))
    db, ex = _open(data_dir)
    try:
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 520
        assert _rows(ex, 'SELECT MAX(id) FROM t')[0][0] == 519
        scan = db.get_table('t').integrity_scan(deep=True)
        assert scan['index_entries'] == 520, scan
        # 恢复成功后才退休/轮转 WAL（保留段 ≤ K，默认 K=0）
        assert len(db.get_table('t').wal.segment_files()) <= 2
        _ok(ex, 'INSERT INTO t VALUES (520, 520)')
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 521
        print(f"\n    跨段崩溃恢复：崩溃现场 {len(crash_files)} 个归档段，"
              f"重放后 520 行完整、索引 {scan['index_entries']} 项，"
              f"恢复后可继续写")
    finally:
        db.shutdown()


def test_cross_segment_loser_undo_and_recovery(tmp_path):
    from lightdb.db.table import Table
    data_dir = tmp_path / 'data_loser'
    db, ex = _open(data_dir)
    try:
        _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        for i in range(300):
            _ok(ex, f'INSERT INTO t VALUES ({i}, {i})')
    finally:
        db.shutdown()
    _run_wal_child(data_dir, mode='txn_loser')
    db, ex = _open(data_dir)
    try:
        # 未提交事务跨段写入 → 恢复后一行都不能泄漏
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 300
        assert _rows(ex, 'SELECT MAX(id) FROM t')[0][0] == 299
        assert db._pending_ddl is None
        print("\n    跨段落 loser undo：未提交 220 行全部撤销，已提交 300 行保留")
    finally:
        db.shutdown()


def test_wal_segment_bound_under_default_checkpoint(tmp_path):
    """checkpoint 阈值生效时：归档段数/总量都有界（量化上界）"""
    from lightdb.db.table import Table
    Table.wal_segment_bytes = 1024
    Table.wal_keep_segments = 2
    Table.wal_checkpoint_bytes = 8 * 1024
    Table.wal_checkpoint_records = 0
    Table.wal_checkpoint_seconds = 0
    db, ex = _open(tmp_path / 'data_bound')
    try:
        _ok(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        peak_archives = 0
        peak_total = 0
        wal = db.get_table('t').wal
        for start in range(0, 4000, 200):
            vals = ','.join(f'({i},{i})' for i in range(start, start + 200))
            _ok(ex, f'INSERT INTO t VALUES {vals}')
            peak_archives = max(peak_archives, wal.stats()['archive_count'])
            peak_total = max(peak_total, wal.size_bytes())
        db.checkpoint()
        st = wal.stats()
        # checkpoint 保留 K=2；总量 <= K 段 + 活动段（活动段已空）
        assert st['archive_count'] <= 2, st
        assert st['total_bytes'] <= 2 * (1024 + SLACK), st
        assert wal.size_bytes() <= 2 * (1024 + SLACK), st
        # 运行中（未 checkpoint 时）总量受阈值 + 单批余量约束
        assert peak_total <= 8 * 1024 + 512 * 1024, (peak_total, peak_archives)
        assert peak_archives <= 300, peak_archives
        assert _rows(ex, 'SELECT COUNT(*) FROM t')[0][0] == 4000
        print(f"\n    阈值 8KB/段 1KB：运行中归档峰值 {peak_archives} 段、"
              f"总量峰值 {peak_total}B；checkpoint 后 K=2 段 "
              f"{st['total_bytes']}B ≤ {2 * (1024 + SLACK)}B")
    finally:
        db.shutdown()


# ==================== 二、二级索引正确性/加速语义 ====================

def test_secondary_index_matches_full_scan(tmp_path):
    from lightdb.db.table import Table
    db, ex = _open(tmp_path / 'data_idx')
    try:
        _seed_region_table(ex, 2000)
        tbl = db.get_table('t')
        before = {sql: _scan_rows(ex, sql, tbl) for sql in CASES}
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
        after = {sql: _rows(ex, sql) for sql in CASES}
        for sql in CASES:
            assert after[sql] == before[sql], (sql, before[sql][:3],
                                               after[sql][:3])
        stats = tbl.index_usage_stats()
        assert stats['lookups'] > 0, stats
        assert stats['ordered'] > 0, stats
        assert stats['rebuilds'] >= 1, stats
        print(f"\n    二级索引：{len(CASES)} 类查询与全表扫描逐行一致；"
              f"lookups={stats['lookups']} ordered={stats['ordered']} "
              f"rebuilds={stats['rebuilds']}")
    finally:
        db.shutdown()


def test_secondary_index_point_range_order_values(tmp_path):
    db, ex = _open(tmp_path / 'data_idx_vals')
    try:
        _seed_region_table(ex, 1000)
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
        tbl = db.get_table('t')
        # 等值：region=42 的 10 行
        got = _rows(ex, 'SELECT id FROM t WHERE region = 42 ORDER BY id')
        assert len(got) == 10 and got == [(42 + 100 * k,) for k in range(10)]
        # 范围：region in [10,12]；BETWEEN 会被下推成 AND 比较，走索引
        before_lookup = tbl.index_usage_stats()['lookups']
        got = _rows(ex, 'SELECT id FROM t WHERE region BETWEEN 10 AND 12 '
                        'ORDER BY id')
        assert len(got) == 30
        assert tbl.index_usage_stats()['lookups'] > before_lookup
        # ORDER BY col LIMIT：按 region 升/降序，NULL/并列稳定
        asc = _rows(ex, 'SELECT region FROM t ORDER BY region LIMIT 5')
        desc = _rows(ex, 'SELECT region FROM t ORDER BY region DESC LIMIT 5')
        assert asc == [(0,), (0,), (0,), (0,), (0,)] and \
            desc == [(99,), (99,), (99,), (99,), (99,)]
        # 空结果
        assert _rows(ex, 'SELECT * FROM t WHERE region = 100000') == []
    finally:
        db.shutdown()


def test_secondary_index_safe_fallback(tmp_path):
    db, ex = _open(tmp_path / 'data_idx_fb')
    try:
        _seed_region_table(ex, 500)
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
        tbl = db.get_table('t')
        scan_eq = _scan_rows(ex, 'SELECT id FROM t WHERE region = 42', tbl)
        assert _rows(ex, 'SELECT id FROM t WHERE region = 42') == scan_eq
        # OR / LIKE / 表达式 / 非索引列 → 全部安全回退且结果与全表扫描一致
        fallback_sqls = [
            'SELECT id FROM t WHERE region = 1 OR region = 2',
            'SELECT id FROM t WHERE val > 100',
            'SELECT id FROM t WHERE region + 1 = 42',
        ]
        for sql in fallback_sqls:
            assert _rows(ex, sql) == _scan_rows(ex, sql, tbl), sql
        before = tbl.index_usage_stats()['fallbacks']
        # 显式事务（有未提交写入）→ 索引映射不承担 MVCC 可见性，回退
        s = {}
        _ok(ex, 'BEGIN', s)
        _ok(ex, 'INSERT INTO t (id, region, val) VALUES (9000, 42, 0)', s)
        got = _rows(ex, 'SELECT id FROM t WHERE region = 42', s)
        assert 9000 in [r[0] for r in got]
        _ok(ex, 'ROLLBACK', s)
        assert tbl.index_usage_stats()['fallbacks'] > before
        assert 9000 not in [r[0] for r in
                            _rows(ex, 'SELECT id FROM t WHERE region = 42')]
    finally:
        db.shutdown()


def test_secondary_index_updates_delete_restart_compact(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_idx_life')
    db, ex = _open(data_dir)
    try:
        _seed_region_table(ex, 600)
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
        tbl = db.get_table('t')
        _ok(ex, 'UPDATE t SET region = 7 WHERE id = 42')
        _ok(ex, 'DELETE FROM t WHERE id = 43')
        _ok(ex, 'INSERT INTO t (id, region, val) VALUES (600, 42, 1)')
        for sql in ('SELECT id FROM t WHERE region = 42 ORDER BY id',
                    'SELECT id FROM t WHERE region >= 98 ORDER BY id',
                    'SELECT id FROM t ORDER BY region LIMIT 15'):
            assert _rows(ex, sql) == _scan_rows(ex, sql, tbl), sql
        before = _rows(ex, 'SELECT id FROM t WHERE region = 7')
        assert (42,) in before
        db.checkpoint()
        db.compact(tables=['t'])
    finally:
        db.shutdown()
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        tbl2 = db2.get_table('t')
        for sql in ('SELECT id FROM t WHERE region = 7',
                    'SELECT id FROM t WHERE region = 42 ORDER BY id',
                    'SELECT id FROM t ORDER BY region LIMIT 15'):
            assert _rows(ex2, sql) == _scan_rows(ex2, sql, tbl2), sql
        stats = tbl2.index_usage_stats()
        assert stats['lookups'] > 0
        print(f"\n    写/删/重启/COMPACT 后索引结果与全表扫描一致；"
              f"lookups={stats['lookups']} rebuilds={stats['rebuilds']}")
    finally:
        db2.shutdown()


CHILD_INDEX_WRITE = r'''
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
for i in range(700, 760):
    ex.execute_sql("INSERT INTO t VALUES (%d, %d, %d)" % (i, i % 100, i), {})
os._exit(1)
'''


def test_crash_recovery_index_consistency(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    data_dir = str(tmp_path / 'data_idx_crash')
    db, ex = _open(data_dir)
    try:
        _seed_region_table(ex, 700)
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
    finally:
        db.shutdown()
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['DATA_DIR'] = data_dir
    env['LIGHTDB_ALLOW_EMPTY_PASSWORD'] = '1'
    proc = subprocess.run([sys.executable, '-c', CHILD_INDEX_WRITE],
                          env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-800:])
    db2 = Database(data_dir)
    ex2 = Executor(db2)
    try:
        tbl2 = db2.get_table('t')
        assert _rows(ex2, 'SELECT COUNT(*) FROM t')[0][0] == 760
        for sql in ('SELECT id FROM t WHERE region = 42 ORDER BY id',
                    'SELECT id FROM t WHERE region >= 98 ORDER BY id'):
            assert _rows(ex2, sql) == _scan_rows(ex2, sql, tbl2), sql
        idx = _ok(ex2, 'SHOW INDEX FROM t')['rows']
        card = {r[2]: r[6] for r in idx}
        expect = len({i % 100 for i in range(760)})
        assert card['idx_region'] == expect, (card, expect)
        print(f"\n    崩溃恢复后索引：760 行结果与扫描一致、Cardinality="
              f"{card['idx_region']}（真实不同非 NULL 值）")
    finally:
        db2.shutdown()


def test_multi_index_choice_and_empty_result(tmp_path):
    """多个候选索引时选择其一，结果仍与全表扫描一致（含空结果）"""
    db, ex = _open(tmp_path / 'data_multi_idx')
    try:
        _seed_region_table(ex, 1000)
        _ok(ex, 'CREATE INDEX idx_region ON t (region)')
        _ok(ex, 'CREATE INDEX idx_val ON t (val)')
        tbl = db.get_table('t')
        for sql in ('SELECT id FROM t WHERE region = 42 AND val = 126',
                    'SELECT id FROM t WHERE region >= 98 AND val < 300',
                    'SELECT id FROM t WHERE region = 42 OR val = 126',
                    'SELECT id FROM t WHERE region = 100000'):
            assert _rows(ex, sql) == _scan_rows(ex, sql, tbl), sql
        # 二级索引存在时，结果仍按主键升序（与全表扫描一致）
        eq = _rows(ex, 'SELECT id FROM t WHERE region = 7 ORDER BY id')
        assert eq == sorted(eq)
        print("\n    多索引选择/OR/空结果：结果与全表扫描一致")
    finally:
        db.shutdown()


def test_show_index_metadata_real_and_type(tmp_path):
    db, ex = _open(tmp_path / 'data_meta')
    try:
        _ok(ex, 'CREATE TABLE m (id INT PRIMARY KEY, region INT, val INT, '
                'note VARCHAR(10))')
        vals = ','.join(f'({i},{i % 7}, {i % 3}, NULL)'
                        for i in range(1, 51))
        _ok(ex, f'INSERT INTO m VALUES {vals}')
        _ok(ex, 'CREATE INDEX idx_region ON m (region)')
        _ok(ex, 'CREATE INDEX idx_multi ON m (region, val)')
        idx = _ok(ex, 'SHOW INDEX FROM m')
        cols = idx['columns']
        by = {r[cols.index('Key_name')]: dict(zip(cols, r)) for r in idx['rows']}
        # 真实 cardinality：region 有 7 个不同值（不含 NULL）
        assert by['idx_region']['Cardinality'] == 7, by['idx_region']
        assert by['idx_region']['Index_type'] == 'BTREE'
        # 多列索引：真实不同 (region,val) 非 NULL 组合数 = 7*3=21
        assert by['idx_multi']['Cardinality'] == 21, by['idx_multi']
        assert by['idx_multi']['Index_type'] == 'BTREE'
        # 全部 NULL 的列：真实基数 0，而不是编造一个数字
        _ok(ex, 'CREATE INDEX idx_note ON m (note)')
        idx2 = _ok(ex, 'SHOW INDEX FROM m')
        cols2 = idx2['columns']
        note = next(r for r in idx2['rows']
                    if r[cols2.index('Key_name')] == 'idx_note')
        assert note[cols2.index('Cardinality')] == 0, (cols2, note)
        print(f"\n    SHOW INDEX：单列 BTREE/Cardinality=7、"
              f"多列 BTREE/Cardinality=21、NULL 列=0（真实值）")
    finally:
        db.shutdown()
