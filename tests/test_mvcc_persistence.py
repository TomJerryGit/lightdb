"""
持久化 MVCC 版本链实测（Batch: persistent MVCC）

本文件是"实测"而不是设计文档：每个用例都打印**页上真实的版本链**
（xmin/xmax/prev）以及重启前后不同读视图读到的值。

核心验收（任务第 4 节）:
  1. 建表插入 id=1 值为 'old'
  2. 取读视图 R（记录 txn_id / active / min_id / max_id / aborted）
  3. BEGIN; UPDATE id=1 为 'new'; COMMIT
  4. 当前读（新读视图）看到 'new'
  5. db.shutdown(); 重新 Database(dir)（模拟重启）
  6. 用第 2 步参数重建等价读视图 R'，读 id=1 -> **必须仍看到 'old'**
  7. 重启后当前读仍看到 'new'

DELETE 同理；另外覆盖: 重插被删主键后的链、崩溃 redo 后的链、
ROLLBACK 后链的复位、vacuum 回收与缝合。

页/行布局见 lightdb/db/table.py 文件头:
    [0:4] xmin [4:8] xmax [8] flags [9:13] dlen
    [13:17] prev_page [17:21] prev_slot  + JSON 载荷
行头 13 → 21 字节，**旧数据文件不兼容**（本文件的数据目录都是新建的）。

已知限制（不掩盖）:
- 没有 vacuum 时每次 UPDATE/DELETE 都留一个旧版本/墓碑，堆空间只增不减；
  没有 vacuum 时链会变长，回溯代价 O(版本数)。
- vacuum(horizon) 会回收"对所有活跃快照都不可见"的版本，因此
  **重启后重建的老快照如果还要读，就不能对相关版本调用 vacuum**。
- SQL 层（lightdb/sql/plan_executor.py 本批次不允许改动）的 SeqScan
  只看索引头那一版的可见性、不走 prev 链，所以 SQL 语句读不到老快照
  的旧版本；走 Table.select(..., read_view=...) 的 Python API 才行。
"""
import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..',
    'data_mvcc_persist')

COLS = [
    {'name': 'id', 'type': 'INT', 'primary': True,
     'notnull': True, 'unique': False},
    {'name': 'v', 'type': 'VARCHAR', 'primary': False,
     'notnull': False, 'unique': False},
]

ROW_HEADER = 21


# ==================== 工具 ====================

def fresh_dir(name):
    d = os.path.join(DATA_ROOT, name)
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def snapshot(rv):
    """把读视图的全部参数抄下来（重启后要"等价重建"）"""
    return {'txn_id': rv.txn_id, 'active': set(rv.active),
            'min_id': rv.min_id, 'max_id': rv.max_id,
            'aborted': set(rv.aborted)}


def read_view_of(snap):
    """用抄下来的参数重建等价读视图（不登记为活跃事务）"""
    from lightdb.storage.mvcc import ReadView
    return ReadView(snap['txn_id'], snap['active'], snap['min_id'],
                    snap['max_id'], snap['aborted'])


def read(tbl, snap=None):
    rv = read_view_of(snap) if snap else None
    return tbl.select(['*'], read_view=rv)[1]


def show(label, tbl, snap=None):
    rows = read(tbl, snap)
    print(f'    {label}: {rows}')
    return rows


def page_versions(tbl):
    """
    扫全部 heap 页，返回 [(pk, page, slot, values, xmin, xmax, pp, ps)]
    —— 包含不在索引里的历史版本/墓碑。
    """
    from lightdb.storage.page import PAGE_TYPE_HEAP
    out = []
    for pid in range(1, tbl.bp.total_pages()):
        page = tbl.bp.get_page(pid)
        if page is None or page.page_type != PAGE_TYPE_HEAP:
            continue
        for sid, rb in page.iter_rows():
            r = tbl._decode_row_ver(rb)
            if r:
                out.append((r[0][tbl.pk_idx], pid, sid, r[0],
                            r[1], r[2], r[3], r[4]))
    return out


def index_map(tbl):
    return {k: (pid, sid) for k, pid, sid in tbl.bt.scan()}


def show_chain(tbl, label):
    vs = page_versions(tbl)
    print(f'    {label}: heap 版本 {len(vs)} 个')
    for pk, pid, sid, values, xmin, xmax, pp, ps in vs:
        print(f'      pk={pk} @({pid},{sid}) values={values} '
              f'xmin={xmin} xmax={xmax} prev=({pp},{ps})')
    print(f'    索引: {index_map(tbl)}')
    return vs


def crash(db):
    """模拟进程内硬杀：不 flush、不 close，直接丢弃对象"""
    for tbl in list(db.tables.values()):
        try:
            tbl.bp.frames.clear()
        except Exception:
            pass
    db.tables.clear()
    del db


# ==================== 1) UPDATE: 重启后老快照仍读到旧值 ====================

def test_acceptance_update_old_snapshot_survives_restart():
    """
    任务第 4 节的逐步验收：UPDATE 后重启，老快照 R' 必须仍读到 'old'。
    """
    from lightdb.db.database import Database
    d = fresh_dir('accept_update')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')

    # 1) 建表插入 id=1 值为 'old'（显式事务 + COMMIT，确定落盘）
    t1 = db.begin()
    tbl.insert([1, 'old'], txn_id=t1['txn_id'],
               read_view=t1['read_view'])
    db.commit(t1)
    print(f'  1) INSERT id=1 = old (txn {t1["txn_id"]})，'
          f'表内容 {read(tbl)}')

    # 2) 取读视图 R
    R = db.begin()
    snap = snapshot(R['read_view'])
    print(f'  2) 读视图 R = {snap}')
    r_before = show('R 读到', tbl, snap)
    assert r_before == [[1, 'old']], f'R 应看到 old: {r_before}'

    # 3) BEGIN; UPDATE id=1 为 'new'; COMMIT
    t2 = db.begin()
    n = tbl.update({'v': 'new'},
                   {'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t2['txn_id'], read_view=t2['read_view'])
    db.commit(t2)
    assert n == 1
    print(f'  3) UPDATE id=1 = new (txn {t2["txn_id"]}) affected={n}')
    show_chain(tbl, '提交后页上版本链')

    # 4) 当前读看到 'new'；R 仍然看到 'old'（快照隔离）
    r_now = show('当前读', tbl)
    assert r_now == [[1, 'new']], f'当前读应看到 new: {r_now}'
    r_still = show('进程内 R 读到', tbl, snap)
    assert r_still == [[1, 'old']], f'R 应仍看到 old: {r_still}'

    # 5) shutdown + 重开（模拟重启）
    db.shutdown()
    db2 = Database(d)
    tbl2 = db2.get_table('t')
    print('  5) db.shutdown() 后重新 Database(dir)（模拟重启）')

    # 6) 用第 2 步参数重建 R'，必须仍看到 'old'
    r_old = show("重启后 R' 读到", tbl2, snap)
    assert r_old == [[1, 'old']], \
        f'重启后老快照必须仍读到 old（版本链没跨重启重建）: {r_old}'

    # 7) 重启后当前读仍看到 'new'
    r_new = show('重启后当前读', tbl2)
    assert r_new == [[1, 'new']], f'重启后当前读应看到 new: {r_new}'
    show_chain(tbl2, '重启后页上版本链')

    # 额外: 页上确实留着旧版本（xmin=1, xmax=t2），新版本 prev 指向它
    vs = page_versions(tbl2)
    old_ver = [v for v in vs if v[3] == [1, 'old']]
    new_ver = [v for v in vs if v[3] == [1, 'new']]
    assert old_ver and new_ver, f'页上必须同时有旧版本和新版本: {vs}'
    assert old_ver[0][5] == t2['txn_id'], \
        f'旧版本 xmax 必须是淘汰它的事务号: {old_ver}'
    assert (new_ver[0][6], new_ver[0][7]) == (old_ver[0][1], old_ver[0][2]), \
        f'新版本 prev 必须指向旧版本: {new_ver} vs {old_ver}'
    print(f'    校验: 旧版本 xmax={old_ver[0][5]} == UPDATE 事务号 '
          f'{t2["txn_id"]}；新版本 prev 指向旧版本槽位 '
          f'({old_ver[0][1]},{old_ver[0][2]}) ✓')
    db2.shutdown()


# ==================== 2) DELETE: 重启后老快照仍读到被删旧值 ====================

def test_acceptance_delete_old_snapshot_survives_restart():
    from lightdb.db.database import Database
    d = fresh_dir('accept_delete')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')

    t1 = db.begin()
    tbl.insert([1, 'a'], txn_id=t1['txn_id'], read_view=t1['read_view'])
    tbl.insert([2, 'old2'], txn_id=t1['txn_id'],
               read_view=t1['read_view'])
    db.commit(t1)
    print(f'  1) INSERT id=1=a, id=2=old2 -> {read(tbl)}')

    R = db.begin()
    snap = snapshot(R['read_view'])
    print(f'  2) 读视图 R = {snap}，R 读到 {read(tbl, snap)}')
    assert read(tbl, snap) == [[1, 'a'], [2, 'old2']]

    t2 = db.begin()
    n = tbl.delete({'column': 'id', 'op': '=', 'value': 2},
                   txn_id=t2['txn_id'], read_view=t2['read_view'])
    db.commit(t2)
    assert n == 1
    print(f'  3) DELETE id=2 (txn {t2["txn_id"]}) affected={n}')
    show_chain(tbl, '提交后页上版本链')

    r_now = show('当前读', tbl)
    assert r_now == [[1, 'a']], f'当前读不应看到 id=2: {r_now}'
    db.shutdown()

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    print('  5) 重启（shutdown + Database(dir)）')
    r_old = show("重启后 R' 读到", tbl2, snap)
    assert r_old == [[1, 'a'], [2, 'old2']], \
        f'重启后老快照必须仍看到被删的 id=2: {r_old}'
    r_new = show('重启后当前读', tbl2)
    assert r_new == [[1, 'a']], f'重启后当前读不应看到 id=2: {r_new}'

    # 墓碑仍在索引里（不是物理删行），xmax 是真实事务号
    vs = page_versions(tbl2)
    tomb = [v for v in vs if v[3] == [2, 'old2']]
    assert tomb, f'id=2 的旧版本必须留在页上: {vs}'
    assert tomb[0][5] == t2['txn_id'], f'墓碑 xmax 必须是真实事务号: {tomb}'
    assert index_map(tbl2).get(2) == (tomb[0][1], tomb[0][2]), \
        f'索引必须仍指向墓碑，否则老快照找不到它: {index_map(tbl2)}'
    print(f'    校验: 索引仍指向墓碑 ({tomb[0][1]},{tomb[0][2]})，'
          f'xmax={tomb[0][5]} ✓')

    # 4') 重插同一主键：新版本接到墓碑后面，老快照仍看到 'old2'
    t3 = db2.begin()
    tbl2.insert([2, 'brand-new'], txn_id=t3['txn_id'],
                read_view=t3['read_view'])
    db2.commit(t3)
    show_chain(tbl2, '重插 id=2 之后')
    r_new2 = show('重插后当前读', tbl2)
    assert r_new2 == [[1, 'a'], [2, 'brand-new']]
    r_old2 = show("重插后 R' 读到", tbl2, snap)
    assert r_old2 == [[1, 'a'], [2, 'old2']], \
        f'重插主键后老快照必须仍看到 old2: {r_old2}'
    db2.shutdown()


# ==================== 3) 崩溃 redo 后版本链仍然可回溯 ====================

def test_crash_redo_update_keeps_old_version_reachable():
    """
    "已提交的 UPDATE 只落在 WAL、页没刷盘"崩溃后重开：
    redo 必须按新布局重放（旧版本 xmax 置位 + 新版本另写一槽 +
    prev 指向旧版本），这样重建的老快照还能读到被覆盖前的旧值。
    """
    from lightdb.db.database import Database
    d = fresh_dir('crash_redo_chain')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t0 = db.begin()
    tbl.insert([1, 'old'], txn_id=t0['txn_id'],
               read_view=t0['read_view'])
    db.commit(t0)

    R = db.begin()
    snap = snapshot(R['read_view'])

    t2 = db.begin()
    assert tbl.update({'v': 'new'},
                      {'column': 'id', 'op': '=', 'value': 1},
                      txn_id=t2['txn_id'],
                      read_view=t2['read_view']) == 1
    tbl.wal.commit(t2['txn_id'])          # 只落 WAL（fsync），不刷页
    on_disk = open(os.path.join(d, 't.dat'), 'rb').read()
    assert b'"new"' not in on_disk, '本用例要求新值还没落数据页'
    print('    崩溃前数据页上仍是 old，只有 WAL 有 UPDATE+COMMIT')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    show_chain(tbl2, 'redo 之后')
    assert show('redo 后当前读', tbl2) == [[1, 'new']]
    r_old = show("redo 后老快照 R' 读到", tbl2, snap)
    assert r_old == [[1, 'old']], \
        f'redo 后老快照应能沿链读到 old: {r_old}'
    db2.shutdown()


def test_crash_undo_loser_keeps_old_snapshot_readable():
    """
    loser 事务的 UPDATE 页改动已落盘 → 重开必须撤销：
    旧版本 xmax 复位、索引指回旧版本、loser 的新版本不可见。
    """
    from lightdb.db.database import Database
    d = fresh_dir('crash_undo_chain')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t0 = db.begin()
    tbl.insert([1, 'old'], txn_id=t0['txn_id'],
               read_view=t0['read_view'])
    db.commit(t0)

    R = db.begin()
    snap = snapshot(R['read_view'])

    t2 = db.begin()
    tbl.update({'v': 'loser'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t2['txn_id'], read_view=t2['read_view'])
    tbl.bp.flush()                        # 半截改动落盘
    print(f'    loser txn {t2["txn_id"]} 的 UPDATE 已落盘，未提交')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    show_chain(tbl2, 'undo 之后')
    assert show('undo 后当前读', tbl2) == [[1, 'old']], 'loser 的改动必须被撤销'
    assert show("undo 后老快照", tbl2, snap) == [[1, 'old']]
    # loser 写的新版本即使还在页上，也必须不可见（xmax = loser 号）
    for pk, pid, sid, values, xmin, xmax, pp, ps in page_versions(tbl2):
        if xmin == t2['txn_id']:
            assert xmax == t2['txn_id'], \
                f'loser 写的版本必须被标记为死: {(values, xmin, xmax)}'
    print('    loser 写下的新版本在页上已被标记为死（xmax = loser 号）✓')
    db2.shutdown()


# ==================== 4) ROLLBACK 后版本链复位 ====================

@pytest.mark.parametrize('kind', ['update', 'delete'])
def test_rollback_restores_version_chain(kind):
    from lightdb.db.database import Database
    d = fresh_dir(f'rollback_chain_{kind}')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t0 = db.begin()
    tbl.insert([1, 'old'], txn_id=t0['txn_id'],
               read_view=t0['read_view'])
    db.commit(t0)

    before_page = page_versions(tbl)
    before_idx = index_map(tbl)
    print(f'    事务前页上版本: {before_page}')

    t = db.begin()
    if kind == 'update':
        tbl.update({'v': 'tmp'}, {'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    else:
        tbl.delete({'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    during = page_versions(tbl)
    print(f'    事务中页上版本: {during}')
    assert len(during) >= len(before_page)

    db.abort(t)
    after = page_versions(tbl)
    print(f'    ROLLBACK 后页上版本: {after}')
    assert after == before_page, \
        f'回滚后页上版本必须与事务前完全一致: {after} != {before_page}'
    assert index_map(tbl) == before_idx, '回滚后索引必须与事务前一致'
    assert read(tbl) == [[1, 'old']]

    # 被回滚删除的主键已复活，再插同一个主键必须是"主键重复"
    if kind == 'delete':
        t2 = db.begin()
        with pytest.raises(ValueError):
            tbl.insert([1, 'again'], txn_id=t2['txn_id'],
                       read_view=t2['read_view'])
        db.abort(t2)
        print('    ROLLBACK 后同一主键已复活 → 重插报"主键重复" ✓')
        t3 = db.begin()
        tbl.insert([7, 'new-pk'], txn_id=t3['txn_id'],
                   read_view=t3['read_view'])
        db.commit(t3)
        assert read(tbl) == [[1, 'old'], [7, 'new-pk']]
    db.shutdown()


# ==================== 5) 子进程 os._exit(9) 硬杀 ====================

CHILD_CODE = r'''
import os, sys
sys.path.insert(0, {root!r})
from lightdb.db.database import Database
db = Database({d!r})
tbl = db.get_table('t')
t = db.begin()
tbl.insert([9, 'committed'], txn_id=t['txn_id'], read_view=t['read_view'])
db.commit(t)                       # 已提交（COMMIT fsync + 刷页）
t2 = db.begin()
tbl.insert([10, 'uncommitted'], txn_id=t2['txn_id'],
           read_view=t2['read_view'])
tbl.bp.flush()                     # 未提交的页改动也落到数据文件
sys.stdout.write('child-flushed\n')
sys.stdout.flush()
os._exit(9)                        # 硬杀：不 flush WAL、不 close
'''


def test_hard_kill_os_exit_keeps_committed_rows():
    from lightdb.db.database import Database
    d = fresh_dir('hard_kill')
    db = Database(d)
    db.create_table('t', COLS)
    db.shutdown()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = CHILD_CODE.format(root=root, d=d)
    proc = subprocess.run([sys.executable, '-c', code],
                          capture_output=True, text=True)
    print(f'    子进程退出码 {proc.returncode}（os._exit(9)），'
          f'stdout={proc.stdout.strip()!r}')
    assert proc.returncode == 9, proc.stderr

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('硬杀后重开', tbl2)
    assert [9, 'committed'] in rows, f'已提交数据必须还在: {rows}'
    assert [10, 'uncommitted'] not in rows, \
        f'未提交事务不得留半截: {rows}'
    db2.shutdown()


# ==================== 6) vacuum ====================

def test_vacuum_reclaims_dead_versions_and_splices_chain():
    from lightdb.db.database import Database
    d = fresh_dir('vacuum')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')

    t0 = db.begin()
    tbl.insert([1, 'v0'], txn_id=t0['txn_id'], read_view=t0['read_view'])
    db.commit(t0)
    tid1 = t0['txn_id']
    t1 = db.begin()
    tbl.update({'v': 'v1'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t1['txn_id'], read_view=t1['read_view'])
    db.commit(t1)
    tid2 = t1['txn_id']
    t2 = db.begin()
    tbl.update({'v': 'v2'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t2['txn_id'], read_view=t2['read_view'])
    db.commit(t2)
    tid3 = t2['txn_id']
    print(f'    版本: v0(xmin={tid1}) -> v1(xmin={tid2}) -> '
          f'v2(xmin={tid3})')
    show_chain(tbl, 'vacuum 前')

    # 一个"看得见 v1"的历史快照：max_id == tid3 时 v1 的 xmax=tid3
    # 对它不可见（xmax >= max_id → 可见），所以 v1 必须留着
    hist = {'txn_id': 99, 'active': set(), 'min_id': 99,
            'max_id': tid3, 'aborted': set()}
    assert read(tbl, hist) == [[1, 'v1']], '历史快照应看到 v1'

    # horizon = tid2 -> 只有 v0（xmax = tid2）可回收
    removed = tbl.vacuum(horizon=tid2)
    print(f'    vacuum(horizon={tid2}) 回收 {removed} 个版本')
    show_chain(tbl, 'vacuum 后')
    assert removed == 1
    vs = page_versions(tbl)
    assert not [v for v in vs if v[3] == [1, 'v0']], 'v0 必须被回收'
    # 链必须缝合：v1 原来的 prev 指向 v0（已删）→ 现在必须是链起点
    v1 = [v for v in vs if v[3] == [1, 'v1']][0]
    assert (v1[6], v1[7]) == (0, 0), \
        f'被删版本的 prev 必须缝合掉（否则链断）: {v1}'
    assert read(tbl) == [[1, 'v2']]
    assert read(tbl, hist) == [[1, 'v1']], '缝合后历史快照仍应看到 v1'

    # horizon = tid3 -> v1 也可回收，只剩 v2
    removed2 = tbl.vacuum(horizon=tid3)
    print(f'    vacuum(horizon={tid3}) 回收 {removed2} 个版本')
    show_chain(tbl, '再次 vacuum 后')
    assert removed2 == 1
    assert read(tbl) == [[1, 'v2']]
    assert len(page_versions(tbl)) == 1

    db.shutdown()
    db2 = Database(d)
    tbl2 = db2.get_table('t')
    assert show('vacuum + 重启后当前读', tbl2) == [[1, 'v2']]
    db2.shutdown()


def test_vacuum_does_not_touch_versions_needed_by_active_snapshot():
    """有一条活跃老快照时，Database.vacuum() 不得回收它需要的版本"""
    from lightdb.db.database import Database
    d = fresh_dir('vacuum_safe')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t0 = db.begin()
    tbl.insert([1, 'v0'], txn_id=t0['txn_id'], read_view=t0['read_view'])
    db.commit(t0)

    R = db.begin()                       # 活跃老快照（登记在 mvcc 里）
    snap = snapshot(R['read_view'])
    t1 = db.begin()
    tbl.update({'v': 'v1'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t1['txn_id'], read_view=t1['read_view'])
    db.commit(t1)
    assert read(tbl, snap) == [[1, 'v0']]

    removed = db.vacuum()                # horizon = 最小活跃事务号
    print(f'    Database.vacuum() horizon=最小活跃事务号，'
          f'回收 {removed} 个版本')
    assert removed == 0, '活跃快照还需要的版本不能被回收'
    assert read(tbl, snap) == [[1, 'v0']], '老快照必须仍能读到 v0'
    assert read(tbl) == [[1, 'v1']]
    db.shutdown()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-s']))
