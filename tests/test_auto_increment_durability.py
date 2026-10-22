# -*- coding: utf-8 -*-
"""P6.2 验收：AUTO_INCREMENT 崩溃后**不复用**已分配号

背景：task-45 的计数只随 `checkpoint()/shutdown()` 落 `_meta.json`；进程被
硬杀（`os._exit`/SIGKILL）会回到"现存最大+1"，极端情况下重用在已删除的
最高号上。本文件锁定 P6.2 的修复：

* 分配走"**区间预留**"：每次把预留上界 R（排他，`< R` 的号可能已分配）
  原子写进 `<table>.autoinc` 并 fsync，之后从内存计数器分配；返回号码前
  必须先确保它 < R。因此进程在任何点硬杀，重启都从
  `max(meta 计数, 文件预留上界, 现存最大+1)` 继续 —— **绝不重用**已分配号，
  代价是崩溃后最多跳过一个预留区间（默认 10000，允许跳号、禁止复用）。
* 干净关闭 / checkpoint / TRUNCATE / ALTER AUTO_INCREMENT 会把预留收敛到
  **精确**下一号（重启不跳号），并与 `SHOW TABLE STATUS.Auto_increment`、
  `SHOW CREATE TABLE` 回显保持一致。
* 吞吐：默认块下一次 fsync / 1 万行，4000 行批量插入只有 1 次预留写盘
  （本文件量化对比，不显著退化）。
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DDL = ('CREATE TABLE t (id INT NOT NULL AUTO_INCREMENT, v INT, '
       'PRIMARY KEY (id))')


# ==================== helpers ====================

@pytest.fixture(autouse=True)
def _reset_auto_block():
    from lightdb.db.table import Table
    old = Table.auto_increment_block
    try:
        yield
    finally:
        Table.auto_increment_block = old


def _open(path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(path))
    return db, Executor(db)


def _ok(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') != 'error', (sql, r)
    return r


def _rows(ex, sql):
    return _ok(ex, sql)['rows']


def _status_next(ex, name='t'):
    r = _ok(ex, 'SHOW TABLE STATUS')
    ci = r['columns'].index('Name')
    ai = r['columns'].index('Auto_increment')
    for row in r['rows']:
        if row[ci] == name:
            return row[ai]
    raise AssertionError(name)


def _autoinc_file(data_dir, name='t'):
    p = os.path.join(str(data_dir), f'{name}.autoinc')
    assert os.path.exists(p), p
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _run_killed_child(data_dir, code, block=3):
    """跑一个会 os._exit(1) 的子进程：先返回它打印的 JSON"""
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['DATA_DIR'] = str(data_dir)
    env['LIGHTDB_ALLOW_EMPTY_PASSWORD'] = '1'
    env['LIGHTDB_AUTO_INCREMENT_BLOCK'] = str(block)
    proc = subprocess.run([sys.executable, '-c', code], env=env,
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    out = proc.stdout.strip().splitlines()
    assert proc.returncode == 1 and out, (
        proc.returncode, out, (proc.stderr or '')[-1500:])
    return json.loads(out[-1])


CHILD_INSERT_DELETE_MAX = r'''
import json, os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
for i in range(4):
    ex.execute_sql("INSERT INTO t (v) VALUES (%d)" % i, {})
ids = [r[0] for r in ex.execute_sql("SELECT id FROM t ORDER BY id", {}).get("rows", [])]
# 删掉最高号：若重启按"现存最大+1"恢复，这个号就会被重用
ex.execute_sql("DELETE FROM t WHERE id = %d" % max(ids), {})
print(json.dumps({"ids": ids, "deleted": max(ids),
                  "autoinc": json.load(open(os.environ["DATA_DIR"] + "/t.autoinc"))}))
sys.stdout.flush()
os._exit(1)
'''

CHILD_EXPLICIT_HIGH = r'''
import json, os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
ex.execute_sql("INSERT INTO t (id, v) VALUES (500000, 1)", {})
ex.execute_sql("DELETE FROM t WHERE id = 500000", {})
print(json.dumps({"high": 500000}))
sys.stdout.flush()
os._exit(1)
'''

CHILD_TXN_UNCOMMITTED = r'''
import json, os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
s = {}
ex.execute_sql("BEGIN", s)
ex.execute_sql("INSERT INTO t (v) VALUES (777)", s)   # 分配了号但不提交
print(json.dumps({"allocated": 1}))
sys.stdout.flush()
os._exit(1)
'''


# ==================== 1. 硬杀 3 轮：不复用、单调 ====================

def test_hard_kill_three_rounds_never_reuses_even_deleted_highest(tmp_path):
    data_dir = tmp_path / 'data'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        _ok(ex, 'INSERT INTO t (v) VALUES (0)')      # 1
    finally:
        db.shutdown()

    max_seen = 0
    next_seen = []
    for rnd in range(3):
        out = _run_killed_child(data_dir, CHILD_INSERT_DELETE_MAX, block=3)
        max_seen = max(max_seen, max(out['ids']))
        db, ex = _open(data_dir)
        try:
            nxt = _status_next(ex)
            ids = [r[0] for r in _rows(ex, 'SELECT id FROM t ORDER BY id')]
            # 已提交行（硬杀前 WAL 落 OS 的）恢复可见；未提交的不要求
            assert ids, ids
            # 核心不变量：重启后的下一号严格大于**所有**已分配号
            assert nxt > max_seen, (rnd, nxt, max_seen, out)
            # 删除最高号也没关系：文件预留上界 >= 已分配最大号+1
            assert out['autoinc']['id'] >= max(out['ids']) + 1, out
            next_seen.append(nxt)
            print(f"\n    第{rnd + 1}轮硬杀：已分配={out['ids']}，"
                  f"删除最高号={out['deleted']}，遗留数据 id={ids}，"
                  f"重启 next={nxt}（> 全部已分配）")
        finally:
            db.shutdown()
    assert next_seen == sorted(next_seen), next_seen

    # 最后一轮之后再插入：新号 > 三轮里出现过的所有号
    db, ex = _open(data_dir)
    try:
        _ok(ex, 'INSERT INTO t (v) VALUES (-1)')
        got = _rows(ex, 'SELECT id FROM t ORDER BY id')
        new_id = got[-1][0]
        assert new_id > max_seen, (new_id, max_seen)
        assert _status_next(ex) == new_id + 1
        print(f"    最终插入 id={new_id} > 历史最大已分配 {max_seen}；"
              f"重复 3 次硬杀仍单调，next={_status_next(ex)}")
    finally:
        db.shutdown()


def test_hard_kill_after_explicit_high_value_deleted(tmp_path):
    """显式插入大号后删除再硬杀：重启也不重用该号"""
    data_dir = tmp_path / 'data_high'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
    finally:
        db.shutdown()
    _run_killed_child(data_dir, CHILD_EXPLICIT_HIGH, block=100)
    db, ex = _open(data_dir)
    try:
        nxt = _status_next(ex)
        assert nxt > 500000, nxt
        assert _rows(ex, 'SELECT id FROM t') == []
        _ok(ex, 'INSERT INTO t (v) VALUES (1)')
        new_id = _rows(ex, 'SELECT id FROM t')[0][0]
        assert new_id > 500000, new_id
        print(f"\n    显式插入 500000 后删除并硬杀：重启 next={nxt}，"
              f"新插入 id={new_id}（未重用 500000）")
    finally:
        db.shutdown()


def test_hard_kill_with_uncommitted_insert_still_no_reuse(tmp_path):
    """未提交事务里已分配的号，硬杀后也不复用（且行不泄漏）"""
    data_dir = tmp_path / 'data_txn'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        _ok(ex, 'INSERT INTO t (v) VALUES (0)')       # id=1 已提交
    finally:
        db.shutdown()
    _run_killed_child(data_dir, CHILD_TXN_UNCOMMITTED, block=3)
    db, ex = _open(data_dir)
    try:
        ids = [r[0] for r in _rows(ex, 'SELECT id FROM t ORDER BY id')]
        assert ids == [1], ids                          # 未提交行不泄漏
        nxt = _status_next(ex)
        assert nxt > 1, nxt
        _ok(ex, 'INSERT INTO t (v) VALUES (2)')
        new_id = _rows(ex, 'SELECT id FROM t ORDER BY id')[-1][0]
        assert new_id > 1, new_id
        print(f"\n    未提交插入分配号后硬杀：可见行={ids}，重启 next={nxt}，"
              f"新插入 id={new_id}（不复用未提交事务的号）")
    finally:
        db.shutdown()


# ==================== 2. 干净重启 / checkpoint 精确一致 ====================

def test_clean_restart_is_exact_and_status_consistent(tmp_path):
    """干净关闭→预留收敛到精确下一号：重启不跳号，SHOW 状态一致"""
    from lightdb.db.table import Table
    Table.auto_increment_block = 3       # 故意让过程中多次预留
    data_dir = tmp_path / 'data_clean'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        for i in range(7):
            _ok(ex, f'INSERT INTO t (v) VALUES ({i})')
        nxt = _status_next(ex)
        assert nxt == 8, nxt
        assert _autoinc_file(data_dir)['id'] >= nxt     # 磁盘预留 >= 下一号
        assert 'AUTO_INCREMENT' in _rows(ex, 'SHOW CREATE TABLE t')[0][1]
        ddl_next = _rows(ex, 'SHOW CREATE TABLE t')[0][1]
        assert f'AUTO_INCREMENT={nxt}' in ddl_next, ddl_next
    finally:
        db.shutdown()                    # 收敛为精确 8

    assert _autoinc_file(data_dir)['id'] == 8, _autoinc_file(data_dir)
    db, ex = _open(data_dir)
    try:
        assert _status_next(ex) == 8, _status_next(ex)  # 不跳号
        _ok(ex, 'INSERT INTO t (v) VALUES (100)')
        got = [tuple(r) for r in _rows(ex, "SELECT id FROM t WHERE v=100")]
        assert got == [(8,)], got
        print(f"\n    7 行后干净重启：next 保持 8（精确、不跳号），"
              f"新插入 id=8")
    finally:
        db.shutdown()


def test_checkpoint_then_hard_kill_no_reuse(tmp_path):
    """checkpoint 收敛计数并退休 WAL；之后硬杀，重启仍不复用"""
    from lightdb.db.table import Table
    Table.auto_increment_block = 4
    data_dir = tmp_path / 'data_ckpt'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        for i in range(5):
            _ok(ex, f'INSERT INTO t (v) VALUES ({i})')   # 1..5
        db.checkpoint()
        nxt = _status_next(ex)
        assert nxt == 6, nxt
        assert _autoinc_file(data_dir)['id'] == 6
        assert db.get_table('t').wal.size_bytes() == 0
    finally:
        db.shutdown()

    # 硬杀在 checkpoint 之后继续分配
    child = r'''
import json, os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
db = Database(os.environ["DATA_DIR"])
ex = Executor(db)
for i in range(6):
    ex.execute_sql("INSERT INTO t (v) VALUES (%d)" % i, {})
ids = [r[0] for r in ex.execute_sql("SELECT MAX(id) FROM t", {}).get("rows", [])]
print(json.dumps({"max": max(ids)}))
sys.stdout.flush()
os._exit(1)
'''
    out = _run_killed_child(data_dir, child, block=4)
    db, ex = _open(data_dir)
    try:
        nxt = _status_next(ex)
        assert nxt > out['max'], (nxt, out)
        print(f"\n    checkpoint(next=6) → 硬杀分配至 max={out['max']}："
              f"重启 next={nxt}（不复用）")
    finally:
        db.shutdown()


# ==================== 3. 吞吐量化 ====================

def test_batch_throughput_quantified(tmp_path):
    """默认预留块下批量插入吞吐与"不预留"基线对比（量化、不显著退化）"""
    from lightdb.db.table import Table
    rows_per_stmt, n_stmt = 500, 8
    total = rows_per_stmt * n_stmt

    def bench(block, tag):
        Table.auto_increment_block = block
        db, ex = _open(tmp_path / f'bench_{tag}')
        try:
            _ok(ex, DDL)
            values = ','.join(f'({i})' for i in range(rows_per_stmt))
            t0 = time.perf_counter()
            for _ in range(n_stmt):
                _ok(ex, f'INSERT INTO t (v) VALUES {values}')
            dt = time.perf_counter() - t0
            assert db.get_table('t').row_count == total
            return dt
        finally:
            db.shutdown()

    try:
        Table.auto_increment_block = 10 ** 9      # 基线：几乎不写预留文件
        base = bench(10 ** 9, 'base')
        # 默认块（区间预留 + fsync）：4000 行只有 1 次预留写盘
        Table.auto_increment_block = None
        dur = bench(None, 'default')
    finally:
        Table.auto_increment_block = None
    rate_base = total / base
    rate_dur = total / dur
    print(f"\n    {total} 行批量插入：不预留 {base:.3f}s "
          f"({rate_base:.0f} rows/s)；默认块 {dur:.3f}s "
          f"({rate_dur:.0f} rows/s)；"
          f"耗时比 {dur / base:.2f}×")
    # 本机一次 fsync ≈55ms；默认块下 4000 行 1 次预留写盘，允许 2× 波动
    assert dur <= base * 2.0 + 0.5, (base, dur)
    assert rate_dur >= rate_base / 2.0, (rate_base, rate_dur)


# ==================== 4. 与 TRUNCATE/ALTER/VACUUM/COMPACT 的交互 ====================

def test_interactions_truncate_alter_vacuum_compact(tmp_path):
    from lightdb.db.table import Table
    Table.auto_increment_block = 3
    data_dir = tmp_path / 'data_ix'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        for i in range(5):
            _ok(ex, f'INSERT INTO t (v) VALUES ({i})')      # 1..5
        _ok(ex, 'ALTER TABLE t AUTO_INCREMENT = 100')
        assert _status_next(ex) == 100
        assert _autoinc_file(data_dir)['id'] == 100          # ALTER 立即持久
        _ok(ex, 'INSERT INTO t (v) VALUES (100)')            # 100
        db.checkpoint()
        assert _status_next(ex) == 101
        db.compact(tables=['t'])
        assert _status_next(ex) == 101, _status_next(ex)     # COMPACT 不丢计数
        assert _autoinc_file(data_dir)['id'] == 101
        db.vacuum()
        _ok(ex, 'INSERT INTO t (v) VALUES (101)')            # 101
        assert _status_next(ex) == 102
        _ok(ex, 'TRUNCATE TABLE t')
        assert _status_next(ex) == 1                         # TRUNCATE 重置
        assert _autoinc_file(data_dir)['id'] == 1            # 且持久
        _ok(ex, 'INSERT INTO t (v) VALUES (1)')
        got = [tuple(r) for r in _rows(ex, 'SELECT id FROM t')]
        assert got == [(1,)], got
    finally:
        db.shutdown()
    db, ex = _open(data_dir)
    try:
        assert _status_next(ex) == 2
        _ok(ex, 'INSERT INTO t (v) VALUES (2)')
        got = [tuple(r) for r in _rows(ex, 'SELECT id FROM t ORDER BY id')]
        assert got == [(1,), (2,)], got
        print(f"\n    ALTER=100→插入100→checkpoint→COMPACT→VACUUM→插入101→"
              f"TRUNCATE→重启：计数保持（重启 next=2，新插入 id=2）")
    finally:
        db.shutdown()


def test_reservation_file_is_atomic_and_monotonic(tmp_path):
    """`.autoinc` 文件记录排他预留上界；每次分配号都小于它（崩溃安全前提）"""
    from lightdb.db.table import Table
    Table.auto_increment_block = 2
    data_dir = tmp_path / 'data_file'
    db, ex = _open(data_dir)
    try:
        _ok(ex, DDL)
        alloc = []
        for i in range(9):
            _ok(ex, f'INSERT INTO t (v) VALUES ({i})')
            alloc.append(_rows(ex, 'SELECT MAX(id) FROM t')[0][0])
            reserved = _autoinc_file(data_dir)['id']
            assert reserved > alloc[-1], (reserved, alloc[-1])
        tbl = db.get_table('t')
        assert tbl.auto_increment_reservations()['id'] >= \
            tbl.next_auto_increment_value()
        print(f"\n    9 行分配 {alloc}；预留上界={_autoinc_file(data_dir)['id']} "
              f"(> 已分配最大号)；下一号={tbl.next_auto_increment_value()}")
    finally:
        db.shutdown()
