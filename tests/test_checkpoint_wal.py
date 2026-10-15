"""checkpoint / WAL 轮转测试（阶段 2 第 1 项）

覆盖:
  ① 阈值触发 checkpoint 后 WAL 变短且数据仍可读；
  ② 崩溃恢复（子进程 os._exit(1) 硬杀）后：已提交的行全在、
     未提交的行不在；
  ③ 显式 Database.checkpoint() 幂等；有未提交事务时安全跳过
     （绝不刷未提交脏页、绝不丢它的 WAL 撤销镜像）；
  ④ 恢复后继续写入正常，且 WAL 仍然有界；
  ⑤ （附加，交付项 4）vacuum 能真正回收 autocommit 旧版本并释放
     页内可复用空间。

本文件是新增文件，不改动任何既有测试的断言。
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_checkpoint_wal.py
    from conftest import safe_rmtree, track

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]

# 测试用小阈值：默认 1 MB 在用例里跑太慢，但触发的代码路径完全一样。
TEST_THRESHOLD = 8 * 1024


def fresh_dir(name):
    d = os.path.join(BASE, 'data_ckpt_' + name)
    if os.path.exists(d):
        safe_rmtree(d)
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(d):
    try:
        safe_rmtree(d)
    except Exception:
        pass


def wal_size(d, table='t'):
    p = os.path.join(d, table + '.wal')
    return os.path.getsize(p) if os.path.exists(p) else 0


def dat_size(d, table='t'):
    p = os.path.join(d, table + '.dat')
    return os.path.getsize(p) if os.path.exists(p) else 0


def rows_of(tbl, where=None):
    _cols, rows = tbl.select(['id', 'v'], where)
    return rows


@pytest.fixture
def small_threshold():
    """临时把 checkpoint 阈值降到 8 KB（用完恢复原值）"""
    from lightdb.db.table import Table
    old_b, old_r = Table.wal_checkpoint_bytes, Table.wal_checkpoint_records
    Table.wal_checkpoint_bytes, Table.wal_checkpoint_records = \
        TEST_THRESHOLD, 0
    try:
        yield TEST_THRESHOLD
    finally:
        Table.wal_checkpoint_bytes, Table.wal_checkpoint_records = \
            old_b, old_r


# ==================== ① 阈值触发 ====================

def test_wal_threshold_triggers_checkpoint_and_keeps_data_readable(
        small_threshold):
    from lightdb.db.database import Database

    d = fresh_dir('threshold')
    n = 4000
    try:
        db = track(Database(d))
        tbl = db.get_table('t') if 't' in db.tables else \
            db.create_table('t', COLS)

        for i in range(1, n + 1):
            tbl.insert([i, 1000 + i])

        # 每条 INSERT 的 WAL 记录约 190 B：不做 checkpoint 时约 760 KB。
        unbounded_estimate = n * 190
        size = wal_size(d)
        print(f'    {n} 行 autocommit INSERT 后 WAL={size} B '
              f'(阈值 {small_threshold} B，不轮转约 {unbounded_estimate} B)')
        assert size <= small_threshold + 4096, (
            f'阈值触发后 WAL 必须被限定在阈值附近，实际 {size} B')
        assert size < unbounded_estimate / 4, 'checkpoint 没有生效（WAL 仍在无界增长）'

        # 数据必须全都还在、读得出来
        rows = rows_of(tbl)
        got = {r[0] for r in rows}
        assert len(rows) == n, f'checkpoint 后行数不对: {len(rows)} != {n}'
        assert got == set(range(1, n + 1)), 'checkpoint 后主键集合不对'
        assert rows_of(tbl, {'column': 'id', 'op': '=', 'value': n}) == \
            [[n, 1000 + n]]
        assert dat_size(d) > 0
    finally:
        cleanup(d)


# ==================== ② 崩溃恢复（os._exit 硬杀）====================

CHILD_CODE = r'''
import json, os, sys
sys.path.insert(0, {root!r})
from lightdb.db.database import Database
from lightdb.db.table import Table

# 与父进程一致的小阈值：子进程里也会真的发生 checkpoint
Table.wal_checkpoint_bytes = {threshold!r}
Table.wal_checkpoint_records = 0

db = Database({d!r})
tbl = db.get_table('t')

# 1) 已提交（autocommit）：写入量远超阈值 ⇒ 期间必然发生多次 checkpoint
for i in range(1, {n_committed} + 1):
    tbl.insert([i, 100])

# 2) 显式事务，明确提交
t = db.begin()
tbl.insert([{n_committed} + 1, 200], txn_id=t['txn_id'],
           read_view=t['read_view'])
db.commit(t)

# 3) 未提交事务：写完之后硬杀
t2 = db.begin()
for i in range({n_committed} + 2, {n_committed} + 2 + {n_uncommitted}):
    tbl.insert([i, 999], txn_id=t2['txn_id'], read_view=t2['read_view'])

# 最坏情况：未提交的页改动也被刷进数据文件
tbl.bp.flush()

sys.stdout.write(json.dumps({{
    'wal': os.path.getsize(os.path.join({d!r}, 't.wal')),
    'committed': {n_committed} + 1,
    'uncommitted_min': {n_committed} + 2,
    'uncommitted_max': {n_committed} + 1 + {n_uncommitted},
}}))
sys.stdout.flush()
os._exit(1)          # 硬杀：不 flush WAL 缓冲、不 close、不 truncate
'''


def test_crash_recovery_after_checkpoint_keeps_committed_drops_uncommitted(
        small_threshold):
    from lightdb.db.database import Database

    d = fresh_dir('crash')
    n_committed = 600
    n_uncommitted = 40
    try:
        db = track(Database(d))
        db.create_table('t', COLS)
        db.shutdown()

        root = BASE
        code = CHILD_CODE.format(
            root=root, d=d, threshold=TEST_THRESHOLD,
            n_committed=n_committed, n_uncommitted=n_uncommitted)
        proc = subprocess.run([sys.executable, '-c', code],
                              capture_output=True, text=True)
        print(f'    子进程退出码 {proc.returncode}（os._exit(1)），'
              f'stdout={proc.stdout.strip()!r}')
        assert proc.returncode == 1, proc.stderr
        info = json.loads(proc.stdout.strip())
        # 崩溃时 WAL 里仍带着未提交事务的记录（checkpoint 被安全门挡住，
        # 没有把它们连同旧行镜像一起退休掉）
        assert info['wal'] > 0, '未提交事务的 WAL 记录竟然被 checkpoint 退休了'

        # ---------- 恢复 ----------
        db2 = track(Database(d))
        tbl2 = db2.get_table('t')
        rows = rows_of(tbl2)
        got = {r[0] for r in rows}
        missing = [i for i in range(1, n_committed + 2) if i not in got]
        leaked = [i for i in range(n_committed + 2,
                                  n_committed + 2 + n_uncommitted)
                  if i in got]
        print(f'    恢复后行数 {len(rows)}，丢失已提交 {len(missing)}，'
              f'泄漏未提交 {len(leaked)}')
        assert not missing, f'已提交的行在崩溃后丢失: {missing[:10]}'
        assert not leaked, f'未提交的行崩溃后泄漏成可见行: {leaked[:10]}'
        assert rows_of(tbl2, {'column': 'id', 'op': '=',
                              'value': n_committed + 1}) == \
            [[n_committed + 1, 200]], '显式事务提交的那一行必须还在'

        # ---------- ④ 恢复后继续写入正常，且 WAL 仍然有界 ----------
        new_id = 900001
        for i in range(new_id, new_id + 300):
            tbl2.insert([i, 7])
        tbl2.update({'v': 8}, {'column': 'id', 'op': '=', 'value': new_id})
        assert rows_of(tbl2, {'column': 'id', 'op': '=',
                              'value': new_id}) == [[new_id, 8]]
        assert len(rows_of(tbl2)) == n_committed + 1 + 300
        size = wal_size(d)
        print(f'    恢复后继续写 300 行 + 1 次 UPDATE，WAL={size} B')
        assert size <= TEST_THRESHOLD + 4096, (
            f'恢复后 WAL 仍然必须有界，实际 {size} B')
    finally:
        cleanup(d)


# ==================== ③ 显式 checkpoint 幂等 ====================

def test_explicit_checkpoint_is_idempotent_and_safe_with_open_txn(
        small_threshold):
    from lightdb.db.database import Database

    d = fresh_dir('explicit')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, 501):
            tbl.insert([i, i])

        assert db.checkpoint() == 1, '无活跃事务时 checkpoint 应作用于该表'
        assert wal_size(d) == 0, 'checkpoint 后 WAL 必须为空'
        assert len(rows_of(tbl)) == 500, 'checkpoint 不能丢数据'

        # 幂等：再调用一次同样成功，WAL 仍为空，数据不变
        assert db.checkpoint() == 1
        assert wal_size(d) == 0
        assert len(rows_of(tbl)) == 500

        # 有未提交事务时：**跳过**，WAL 不被退休（旧行镜像必须留着）
        txn = db.begin()
        tbl.insert([1001, 1], txn_id=txn['txn_id'],
                   read_view=txn['read_view'])
        before = wal_size(d)
        assert before > 0
        assert db.checkpoint() == 0, '有未提交事务的表必须被安全跳过'
        assert wal_size(d) == before, '跳过时不得截断 WAL'
        assert tbl.bp is not None

        # 提交后可以正常 checkpoint
        db.commit(txn)
        assert db.checkpoint() == 1
        assert wal_size(d) == 0
        assert rows_of(tbl, {'column': 'id', 'op': '=',
                             'value': 1001}) == [[1001, 1]]
    finally:
        cleanup(d)


# ==================== ⑤ vacuum：autocommit 旧版本回收 ====================

def test_vacuum_reclaims_autocommit_versions_and_reuses_page_space():
    from lightdb.db.database import Database

    d = fresh_dir('vacuum')
    rows, rounds = 500, 20
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, rows + 1):
            tbl.insert([i, 0])
        for r in range(1, rounds + 1):
            for i in range(1, rows + 1):
                tbl.update({'v': r}, {'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        dat_before = dat_size(d)

        reclaimed = db.vacuum()
        tbl.flush()
        freed = tbl.last_vacuum_freed_bytes
        print(f'    {rounds} 轮 × {rows} 次 autocommit UPDATE 后：'
              f'vacuum 回收 {reclaimed} 个版本，页内释放 {freed} B，'
              f'.dat {dat_before} B')

        # autocommit 的墓碑（合成高位号）必须被识别为"已提交删除"，
        # 否则这里恒为 0（旧实现的 bug）
        assert reclaimed >= rows * (rounds - 1), (
            f'vacuum 必须回收 autocommit 旧版本，实际只回收 {reclaimed}')
        assert freed > 0, 'vacuum 后页内必须出现可复用的连续空闲空间'

        # 数据仍完整可读（最新版本 = 第 rounds 轮）
        got = rows_of(tbl)
        assert len(got) == rows
        assert all(r[1] == rounds for r in got)

        # 空间复用：回收后插入大量新行，**堆页**不应线性增长
        # （只数堆页：B+Tree 会为新主键正常分裂，那部分增长是应该的）
        def heap_pages():
            n = 0
            for pid in range(1, tbl.bp.total_pages()):
                p = tbl.bp.get_page(pid)
                if p is not None and p.page_type == 1:
                    n += 1
            return n

        heap_before = heap_pages()
        for i in range(rows + 1, rows + 1 + 1500):
            tbl.insert([i, 5])
        tbl.flush()
        heap_after = heap_pages()
        print(f'    再插 1500 行：堆页 {heap_before} -> {heap_after}')
        assert heap_after - heap_before <= 1, (
            f'vacuum 释放的页内空间没有被后续插入复用'
            f'（堆页 {heap_before} -> {heap_after}）')
        assert len(rows_of(tbl)) == rows + 1500
    finally:
        cleanup(d)
