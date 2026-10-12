"""
事务原子性 / 持久化 / 崩溃恢复 实测（Batch: txn atomicity）

本文件是"实测"而不是"设计文档"，每个用例都打印恢复/回滚前后的真实表内容。
覆盖任务要求的 6 点：
  1. ROLLBACK 真原子: BEGIN; INSERT/UPDATE/DELETE; ROLLBACK 后表内容与
     事务前**完全一致**，且被回滚的主键可以再次插入
  2. COMMIT 持久: 提交后立刻 Database.shutdown()，重开数据仍在
  3. 崩溃恢复:
     a) 已提交事务"WAL 已落盘、页未刷盘"（模拟硬杀：丢弃对象、不 flush）
        → 重开后已提交数据必须被 redo 出来
     b) 未提交事务的页改动已经落盘（模拟崩溃前脏页被驱逐/刷盘）
        → 重开后必须被 loser undo 撤销，不得留下半截改动
     c) txn_id=0 的 autocommit 写入同样必须能重放
  4. xmax 真实: 写路径会把真实事务号写进行头（不再恒为 0），
     并发读视图看不到未提交版本
  5. 锁不泄漏: autocommit INSERT 后 BEGIN; UPDATE 同一主键不能超时

数据目录用 data_txn_atomicity/ 下的独立子目录，避免和其他测试文件
（data_b4/data_b5 等）互相干扰。

已知限制（本文件不掩盖，见 README 之外的回答说明）:
- UPDATE 走"旧版本墓碑化 + 新版本另写一槽"：页上确实留着旧版本
  （行头 xmax = 淘汰它的事务号），但它不在索引里、任何读视图都
  不可见。没有 vacuum，所以页空间会随更新增长。
- 重启后内存 MVCC 链清空，老快照无法回查被覆盖前的旧版本内容
  （页上的墓碑只能告诉你"它被谁淘汰了"，拿不到往更早的回溯）。
"""
import os
import shutil
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATA_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..',
    'data_txn_atomicity')

COLS = [
    {'name': 'id', 'type': 'INT', 'primary': True,
     'notnull': True, 'unique': False},
    {'name': 'v', 'type': 'VARCHAR', 'primary': False,
     'notnull': False, 'unique': False},
]

ROW_HEADER = 13


# ==================== 工具 ====================

def fresh_dir(name):
    d = os.path.join(DATA_ROOT, name)
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def rows_of(tbl):
    return tbl.select(['*'])[1]


def show(label, tbl):
    r = rows_of(tbl)
    print(f'    {label}: {r}')
    return r


def row_headers(tbl):
    """返回 {pk: (xmin, xmax)}，直接读页上行头"""
    out = {}
    for pk, pid, sid in tbl.bt.scan():
        page = tbl.bp.get_page(pid)
        rb = page.get_row(sid) if page else None
        if not rb or len(rb) < ROW_HEADER:
            continue
        xmin, xmax, _flags, _n = struct.unpack_from('<IIBI', rb, 0)
        out[pk] = (xmin, xmax)
    return out


def all_page_versions(tbl):
    """
    扫全部 heap 页，返回 [(pk, xmin, xmax)]，**包含**不在索引里的
    墓碑版本（DELETE / UPDATE 淘汰掉的旧版本）。
    """
    from lightdb.storage.page import PAGE_TYPE_HEAP
    out = []
    for pid in range(1, tbl.bp.total_pages()):
        page = tbl.bp.get_page(pid)
        if not page or page.page_type != PAGE_TYPE_HEAP:
            continue
        for sid, rb in page.iter_rows():
            r = tbl._decode_row_full(rb)
            if r:
                row, xmin, xmax = r
                out.append((row[tbl.pk_idx], xmin, xmax))
    return out


def crash(db):
    """
    模拟"进程被硬杀"：不调用 flush()/close()，直接丢弃对象。
    BufferPool 没有 __del__ 回写，未 flush 的脏页随对象一起消失。
    """
    for tbl in list(db.tables.values()):
        try:
            tbl.bp.frames.clear()      # 脏页只存在于内存，崩溃即丢
        except Exception:
            pass
    db.tables.clear()
    del db


def build_base(dirname, base_rows=((1, 'a'), (2, 'b'))):
    """用显式事务 + COMMIT 建一份"确定已落盘"的基数据"""
    from lightdb.db.database import Database
    d = fresh_dir(dirname)
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    for pk, v in base_rows:
        t = db.begin()
        tbl.insert([pk, v], txn_id=t['txn_id'], read_view=t['read_view'])
        db.commit(t)
    db.shutdown()
    return d


# ==================== 1) ROLLBACK 真原子 ====================

@pytest.mark.parametrize('kind', ['insert', 'update', 'delete'])
def test_rollback_is_atomic(kind):
    from lightdb.db.database import Database
    d = fresh_dir(f'rollback_{kind}')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    tbl.insert([1, 'a'])
    tbl.insert([2, 'b'])
    before = show('事务前', tbl)

    t = db.begin()
    if kind == 'insert':
        tbl.insert([3, 'c'], txn_id=t['txn_id'],
                   read_view=t['read_view'])
    elif kind == 'update':
        tbl.update({'v': 'B'},
                   {'column': 'id', 'op': '=', 'value': 2},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    else:
        tbl.delete({'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    during = show('事务中(未提交)', tbl)
    assert during != before, '事务内应当能看到自己的修改'

    db.abort(t)
    after = show('ROLLBACK 后', tbl)
    assert after == before, f'回滚后必须与事务前完全一致: {after} != {before}'

    if kind == 'insert':
        # 回滚后的主键必须能再次插入（索引项没有残留）
        tbl.insert([3, 'c-again'])
        again = show('主键重插后', tbl)
        assert again == before + [[3, 'c-again']]
    db.shutdown()


# ==================== 2) COMMIT 持久 ====================

def test_commit_survives_shutdown():
    from lightdb.db.database import Database
    d = fresh_dir('commit_durable')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t = db.begin()
    tbl.insert([7, 'seven'], txn_id=t['txn_id'],
               read_view=t['read_view'])
    db.commit(t)
    show('提交后(内存)', tbl)
    db.shutdown()

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('shutdown + 重开后', tbl2)
    assert rows == [[7, 'seven']], f'提交后重开数据丢失: {rows}'
    db2.shutdown()


# ==================== 3a) 已提交事务的崩溃 redo ====================

def test_crash_redo_committed_wal_only():
    """
    "写完 WAL 但不 flush 页"：事务提交记录已落 WAL，
    页改动只在内存。模拟硬杀后重开，数据必须被 redo 出来。
    """
    from lightdb.db.database import Database
    d = build_base('crash_redo')

    db = Database(d)
    tbl = db.get_table('t')
    t = db.begin()
    tbl.insert([9, 'wal-only'], txn_id=t['txn_id'],
               read_view=t['read_view'])
    tbl.wal.commit(t['txn_id'])          # 只落 WAL（fsync），不刷页
    del t
    data_path = os.path.join(d, 't.dat')
    on_disk = open(data_path, 'rb').read()
    assert b'wal-only' not in on_disk, '本用例要求提交的行还没落数据页'
    print('    崩溃前数据页上确实没有 wal-only（只有 WAL 有）')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(redo 自 WAL)', tbl2)
    assert [9, 'wal-only'] in rows, f'已提交事务未被 redo: {rows}'
    db2.shutdown()


# ==================== 3b) 未提交事务的 loser undo ====================

def test_crash_undo_loser_after_pages_flushed():
    """
    未提交事务的页改动已经落盘（崩溃前脏页被刷/驱逐），
    重开后必须被撤销：已提交的旧行要回来，loser 的行要消失。
    """
    from lightdb.db.database import Database
    d = build_base('crash_loser')

    db = Database(d)
    tbl = db.get_table('t')
    t = db.begin()
    tid = t['txn_id']
    tbl.insert([3, 'loser'], txn_id=tid, read_view=t['read_view'])
    tbl.update({'v': 'B'}, {'column': 'id', 'op': '=', 'value': 2},
               txn_id=tid, read_view=t['read_view'])
    tbl.delete({'column': 'id', 'op': '=', 'value': 1},
               txn_id=tid, read_view=t['read_view'])
    show('事务中(未提交)', tbl)
    data_path = os.path.join(d, 't.dat')
    tbl.bp.flush()                        # ★ 半截改动已经落盘
    on_disk = open(data_path, 'rb').read()
    assert b'loser' in on_disk, '本用例要求 loser 的改动已经落到数据页'
    print('    崩溃前数据页上确实带着 loser 的半截改动')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(loser undo)', tbl2)
    assert rows == [[1, 'a'], [2, 'b']], f'loser 事务未被撤销: {rows}'
    db2.shutdown()


# ==================== 3c) txn_id=0 的 autocommit 重放 ====================

def test_crash_replay_autocommit_txn0():
    """
    直接写 txn_id=0 的 autocommit WAL 记录（executor 的 INSERT 路径
    在无显式事务时就是 txn_id=0），不刷页、丢弃对象、重开，
    必须能重放。修复前 wal.find_committed_transactions() 会
    `if r.txn_id == 0: continue` 直接丢掉这条记录。
    """
    from lightdb.db.database import Database
    d = build_base('crash_txn0')

    db = Database(d)
    tbl = db.get_table('t')
    tbl.wal.log(0, 'INSERT', {
        'table': 't', 'pk': 42, 'row': [42, 'txn0']})
    tbl.wal.flush_to_os()
    on_disk = open(os.path.join(d, 't.dat'), 'rb').read()
    assert b'txn0' not in on_disk
    print('    WAL 里写了 txn_id=0 的 INSERT，数据页上没有它')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(txn_id=0 重放)', tbl2)
    assert [42, 'txn0'] in rows, f'txn_id=0 的 autocommit 写入未被重放: {rows}'
    db2.shutdown()


def test_crash_replay_autocommit_insert_path():
    """走真实代码路径的 autocommit INSERT（tbl.insert 不传 txn_id）"""
    from lightdb.db.database import Database
    d = build_base('crash_autocommit')

    db = Database(d)
    tbl = db.get_table('t')
    tbl.insert([5, 'ac'])                 # autocommit，未 flush 页
    tbl.bp.frames.clear()                 # 模拟崩溃丢掉脏页
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(autocommit 重放)', tbl2)
    assert [5, 'ac'] in rows, f'autocommit 写入丢失: {rows}'
    db2.shutdown()


# ==================== 4) 行头 xmax / 读视图过滤 ====================

def test_row_header_has_real_txn_marks():
    """
    写路径必须把真实事务号写进行头（不再恒为 0）：
    - 新插入/新版本的 xmin == 写入事务号（非 0 实数）
    - 未提交版本对另一个事务的读视图不可见
    """
    from lightdb.db.database import Database
    d = fresh_dir('xmax')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t1 = db.begin()
    tbl.insert([1, 'orig'], txn_id=t1['txn_id'],
               read_view=t1['read_view'])
    db.commit(t1)

    hdr = row_headers(tbl)
    assert hdr[1][0] == t1['txn_id'], f'xmin 不是真实事务号: {hdr}'
    print(f'    行头 xmin={hdr[1][0]} == 提交事务号 {t1["txn_id"]}')

    t2 = db.begin()
    tbl.update({'v': 'changed'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t2['txn_id'], read_view=t2['read_view'])
    hdr2 = row_headers(tbl)
    assert hdr2[1][0] == t2['txn_id'], f'未提交新版本的 xmin 不真实: {hdr2}'
    print(f'    更新后行头 xmin={hdr2[1][0]} == 更新事务号 {t2["txn_id"]}')

    # 另一个事务的读视图看不到未提交的新版本
    t3 = db.begin()
    seen = tbl.select(['*'], {'column': 'id', 'op': '=', 'value': 1},
                      read_view=t3['read_view'])[1]
    print(f'    并发事务 t3 看到: {seen}')
    assert seen == [[1, 'orig']], f'未提交版本泄漏给其他事务: {seen}'
    db.abort(t2)

    t4 = db.begin()
    seen4 = tbl.select(['*'], read_view=t4['read_view'])[1]
    print(f'    abort 后看到: {seen4}')
    assert seen4 == [[1, 'orig']]
    db.shutdown()


# ==================== 4b) DELETE/UPDATE 的旧版本墓碑 ====================

def test_update_and_delete_leave_real_xmax_tombstones():
    """
    旧版本不再被原地覆写/物理抹掉：DELETE 与 UPDATE 都把**真实事务号**
    写进被淘汰旧版本的行头 xmax，并把它作为墓碑留在页上
    （不在索引里、任何读视图都不可见）。回滚后墓碑被清掉、xmax 复位 0。
    """
    from lightdb.db.database import Database
    d = fresh_dir('xmax_tombstone')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    for pk, v in ((1, 'a'), (2, 'b'), (3, 'c')):
        tbl.insert([pk, v])

    base = all_page_versions(tbl)
    print(f'    初始页上版本: {base}')
    assert all(xmax == 0 for (_pk, _xmin, xmax) in base), \
        '没有删除/更新过，不应有非 0 xmax'

    # ---- DELETE 留墓碑 ----
    t = db.begin()
    tid = t['txn_id']
    tbl.delete({'column': 'id', 'op': '=', 'value': 3},
               txn_id=tid, read_view=t['read_view'])
    vers = all_page_versions(tbl)
    print(f'    事务 {tid} DELETE id=3 后页上版本: {vers}')
    tombstones = [v for v in vers if v[2] != 0]
    assert tombstones, 'DELETE 必须在页上留下带真实 xmax 的旧版本'
    assert tombstones[0][0] == 3 and tombstones[0][2] == tid, \
        f'墓碑的 xmax 应为真实事务号 {tid}: {tombstones}'
    # 墓碑不在索引里 → 对外不可见
    assert [3, 'c'] not in rows_of(tbl)

    db.abort(t)
    after = all_page_versions(tbl)
    print(f'    ROLLBACK 后页上版本: {after}')
    assert all(xmax == 0 for (_pk, _xmin, xmax) in after), \
        f'回滚后 xmax 应全部复位为 0: {after}'
    assert [3, 'c'] in rows_of(tbl), '回滚后该行必须复活'

    # ---- UPDATE 留墓碑（旧版本 xmax = 淘汰它的事务号，新版本 xmin = 同号）----
    t2 = db.begin()
    tid2 = t2['txn_id']
    tbl.update({'v': 'B'}, {'column': 'id', 'op': '=', 'value': 2},
               txn_id=tid2, read_view=t2['read_view'])
    vers2 = all_page_versions(tbl)
    print(f'    事务 {tid2} UPDATE id=2 后页上版本: {vers2}')
    dead = [v for v in vers2 if v[2] == tid2]
    live = [v for v in vers2 if v[2] == 0 and v[1] == tid2]
    assert dead, f'被淘汰的旧版本 xmax 应为 {tid2}: {vers2}'
    assert live, f'新版本 xmin 应为 {tid2}: {vers2}'
    # 事务自己的读视图看到新版本，看不到墓碑
    assert [2, 'B'] in rows_of(tbl)
    db.abort(t2)
    after2 = all_page_versions(tbl)
    print(f'    ROLLBACK 后页上版本: {after2}')
    assert all(xmax == 0 for (_pk, _xmin, xmax) in after2), \
        f'UPDATE 回滚后不应留下非 0 xmax: {after2}'
    assert [2, 'b'] in rows_of(tbl)
    db.shutdown()


def test_tombstone_not_resurrected_by_index_rebuild():
    """
    BTree 根丢失后 _rebuild_index() 全量重建索引时，必须跳过墓碑
    （xmax != 0 的旧版本），否则已删除的行会"复活"。
    """
    from lightdb.db.database import Database
    d = fresh_dir('tombstone_rebuild')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    for pk, v in ((1, 'a'), (2, 'b'), (3, 'c')):
        tbl.insert([pk, v])
    tbl.delete({'column': 'id', 'op': '=', 'value': 2})
    before = show('删除 id=2 后(落盘)', tbl)
    assert [2, 'b'] not in before
    db.shutdown()

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    os.remove(tbl2.btroot_path)                 # 模拟索引根丢失
    from lightdb.storage.btree import BTree
    tbl2.bt = BTree(tbl2.bp, root_page_id=None)
    tbl2._rebuild_index()

    rebuilt = show('索引重建后', tbl2)
    assert rebuilt == [[1, 'a'], [3, 'c']], \
        f'墓碑不该被重建进索引（已删除的行不能复活）: {rebuilt}'
    db2.shutdown()


def test_rollback_same_row_multiple_updates_lifo():
    """
    同一行在一个事务里被改多次，回滚必须**倒序**回放 undo，
    才能回到事务前状态（LIFO）。
    """
    from lightdb.db.database import Database
    d = fresh_dir('rollback_lifo')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    tbl.insert([1, 'a'])
    before = show('事务前', tbl)

    t = db.begin()
    for newv in ('b', 'c', 'd'):
        tbl.update({'v': newv}, {'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    show('事务中(连改 3 次)', tbl)
    db.abort(t)
    after = show('ROLLBACK 后', tbl)
    assert after == before, f'多次更新回滚后必须一致: {after} != {before}'

    # 主键与索引都必须是干净的
    t2 = db.begin()
    tbl.update({'v': 'z'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t2['txn_id'], read_view=t2['read_view'])
    db.commit(t2)
    assert show('再次更新后', tbl) == [[1, 'z']]
    db.shutdown()


# ==================== 3d) 已提交 UPDATE/DELETE 的崩溃 redo ====================

def test_crash_redo_committed_update_wal_only():
    """
    审计结论里的缺陷: 旧的 recover_from_wal() 只重放 INSERT，
    忽略 UPDATE/DELETE。这里构造"已提交的 UPDATE 只落在 WAL、
    页没刷盘"，模拟硬杀后重开必须是新值。
    """
    from lightdb.db.database import Database
    d = build_base('crash_redo_update')

    db = Database(d)
    tbl = db.get_table('t')
    t = db.begin()
    n = tbl.update({'v': 'B'}, {'column': 'id', 'op': '=', 'value': 2},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    assert n == 1, 'UPDATE 应命中 1 行'
    tbl.wal.commit(t['txn_id'])            # 只落 WAL（fsync），不刷页
    del t
    on_disk = open(os.path.join(d, 't.dat'), 'rb').read()
    assert b'"B"' not in on_disk, '本用例要求新值还没落数据页'
    print('    崩溃前数据页上仍是旧值，只有 WAL 有 UPDATE+COMMIT')
    crash(db)

    db2 = Database(d)
    rows = show('重开后(redo UPDATE)', db2.get_table('t'))
    assert rows == [[1, 'a'], [2, 'B']], f'已提交 UPDATE 未被 redo: {rows}'
    db2.shutdown()


def test_crash_redo_committed_delete_wal_only():
    """同理: 已提交的 DELETE 只在 WAL 时也必须被 redo（行必须消失）"""
    from lightdb.db.database import Database
    d = build_base('crash_redo_delete')

    db = Database(d)
    tbl = db.get_table('t')
    t = db.begin()
    n = tbl.delete({'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    assert n == 1, 'DELETE 应命中 1 行'
    tbl.wal.commit(t['txn_id'])            # 只落 WAL（fsync），不刷页
    del t
    on_disk = open(os.path.join(d, 't.dat'), 'rb').read()
    assert b'[1, "a"]' in on_disk, '本用例要求被删的行还在数据页上'
    print('    崩溃前数据页上仍有 id=1，只有 WAL 有 DELETE+COMMIT')
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(redo DELETE)', tbl2)
    assert rows == [[2, 'b']], f'已提交 DELETE 未被 redo: {rows}'
    # 墓碑行头必须带真实删除事务号
    assert any(xmax != 0 for (_pk, _xmin, xmax) in all_page_versions(tbl2)), \
        'DELETE 的旧版本在页上应留下带真实 xmax 的墓碑'
    db2.shutdown()


# ==================== 5) 锁不泄漏 ====================

def test_no_lock_leak_after_autocommit_insert():
    """
    executor 的 INSERT 不传 txn_id（=0），修复前锁挂在 0 号事务上
    永不释放，之后 BEGIN; UPDATE 同一主键必然 5 秒超时。
    """
    from lightdb.db.database import Database
    d = fresh_dir('lock_leak')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    tbl.insert([1, 'a'])
    detail = db.lock_manager.stats()['locks_detail']
    print(f'    autocommit INSERT 之后仍持有的锁: {detail}')
    assert not detail, f'autocommit 之后不应残留锁: {detail}'

    t = db.begin()
    n = tbl.update({'v': 'A'}, {'column': 'id', 'op': '=', 'value': 1},
                   txn_id=t['txn_id'], read_view=t['read_view'])
    db.commit(t)
    print(f'    BEGIN; UPDATE 同一主键 affected={n}')
    assert n == 1
    assert show('提交后', tbl) == [[1, 'A']]
    db.shutdown()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-s']))

# ======================================================================
# 追加：持久化版本链批次（行头 13 → 21 字节，新增 prev_page/prev_slot）
#
# 上面文件头里"已知限制"的一条已经被修掉了：
#   旧: "重启后内存 MVCC 链清空，老快照无法回查被覆盖前的旧版本内容"
#   新: 旧版本的定位(prev_page/prev_slot)落在页上行头里，DELETE 的墓碑
#        也保留在索引里，所以重启后只要重建等价 ReadView 就能沿链回溯。
# 详见 lightdb/db/table.py 文件头与 tests/test_mvcc_persistence.py。
# 下面这条用例只覆盖**崩溃恢复路径**与链布局的自洽性（其余在
# test_mvcc_persistence.py 里）。
# ======================================================================

ROW_HEADER_V2 = 21


def read_versions(tbl):
    """扫堆页返回 [(pk, page, slot, values, xmin, xmax, pp, ps)]"""
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


def test_update_old_version_and_prev_pointer_persist():
    """
    新布局的落盘证据：UPDATE 之后页上必须同时有
      - 旧版本: xmin = 旧事务号, xmax = 更新事务号, prev 保留
      - 新版本: xmin = 更新事务号, xmax = 0, prev 指向旧版本槽位
    且索引指向新版本（版本链的头）。重启后这条链仍然可读。
    """
    from lightdb.db.database import Database
    d = fresh_dir('prev_pointer_persist')
    db = Database(d)
    db.create_table('t', COLS)
    tbl = db.get_table('t')
    t1 = db.begin()
    tbl.insert([1, 'old'], txn_id=t1['txn_id'],
               read_view=t1['read_view'])
    db.commit(t1)

    t2 = db.begin()
    tbl.update({'v': 'new'}, {'column': 'id', 'op': '=', 'value': 1},
               txn_id=t2['txn_id'], read_view=t2['read_view'])
    db.commit(t2)
    vers = read_versions(tbl)
    print(f'    UPDATE 后页上版本: {vers}')
    old = [v for v in vers if v[3] == [1, 'old']][0]
    new = [v for v in vers if v[3] == [1, 'new']][0]
    assert old[5] == t2['txn_id'], f'旧版本 xmax 应为更新事务号: {old}'
    assert new[4] == t2['txn_id'], f'新版本 xmin 应为更新事务号: {new}'
    assert (new[6], new[7]) == (old[1], old[2]), \
        f'新版本 prev 应指向旧版本槽位: {new} vs {old}'
    assert tbl.bt.search(1) == (new[1], new[2]), '索引应指向最新版本'

    db.shutdown()
    db2 = Database(d)
    vers2 = read_versions(db2.get_table('t'))
    print(f'    重启后页上版本: {vers2}')
    assert vers2 == vers, '版本链必须原样落盘、重启后不变'
    db2.shutdown()


def test_crash_redo_update_rebuilds_version_chain():
    """
    "已提交 UPDATE 只在 WAL、页未刷盘"崩溃后，redo 必须按新布局
    重放：旧版本 xmax 置位 + 新版本另写一槽且 prev 指向旧版本。
    这样重建的老快照重启后还能读到被覆盖前的旧值。
    """
    from lightdb.db.database import Database
    from lightdb.storage.mvcc import ReadView
    d = build_base('crash_redo_prev')      # id=1'a', id=2'b' 已提交落盘

    db = Database(d)
    tbl = db.get_table('t')
    R = db.begin()
    snap = (R['read_view'].txn_id, set(R['read_view'].active),
            R['read_view'].min_id, R['read_view'].max_id)

    t = db.begin()
    assert tbl.update({'v': 'B'},
                      {'column': 'id', 'op': '=', 'value': 2},
                      txn_id=t['txn_id'], read_view=t['read_view']) == 1
    tbl.wal.commit(t['txn_id'])            # 只落 WAL，不刷页
    del t
    crash(db)

    db2 = Database(d)
    tbl2 = db2.get_table('t')
    rows = show('重开后(redo UPDATE)', tbl2)
    assert rows == [[1, 'a'], [2, 'B']], f'已提交 UPDATE 未被 redo: {rows}'
    vers = read_versions(tbl2)
    print(f'    redo 后页上版本: {vers}')
    old = [v for v in vers if v[3] == [2, 'b']]
    new = [v for v in vers if v[3] == [2, 'B']]
    assert old and new, f'redo 后必须新旧版本都在页上: {vers}'
    assert (new[0][6], new[0][7]) == (old[0][1], old[0][2]), \
        f'redo 的新版本 prev 必须指向旧版本: {new} vs {old}'

    rv = ReadView(snap[0], snap[1], snap[2], snap[3])
    seen = tbl2.select(['*'], read_view=rv)[1]
    print(f'    redo 后重建的老快照读到: {seen}')
    assert seen == [[1, 'a'], [2, 'b']], \
        f'redo 后老快照必须沿链读到被覆盖前的旧值: {seen}'
    db2.shutdown()
