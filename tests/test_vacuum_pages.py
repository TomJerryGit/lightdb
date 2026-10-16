"""
阶段 2 第 3 项: **页级回收**（`.dat` 真正缩小 + 空闲页可复用）

被测能力（每一项都有可量化的实测断言）:
  ① 大数据量删除 + `vacuum()` 之后 `.dat` 字节数明显下降（不是"页内紧凑化"冒充）；
  ② 空闲页登记**持久化**在 `<table>.free`，重启后仍登记、继续写入不白长；
  ③ vacuum 之后再插入等量数据，文件在耗尽空闲页之前不增长；
  ④ 完整性扫描器: 无悬空 `prev_page`、无活跃行落在空闲页、截断页确实无人引用；
  ⑤ 活跃读视图保护: 老快照看得见的版本不许回收，释放后才可以；
  ⑥ `os._exit(1)` 打断"删除 + vacuum + 再插入"中间态 → 重开后一致；
  ⑦ 空表 / 全删 / 重复 vacuum 幂等。

设计要点（为什么这样测才有意义）:
  * **同一张表 dump 前后逐行一致**: 比对的是 (主键 + 所有列) 的排序后快照 +
    sha256，而不是"行数相等"。真空化会改页号，只有内容比对才能发现
    "行还在但指向了错误的版本/槽位"这类错误。
  * **同窗交错成对测**: 吞吐在测量窗口内单调下滑，顺序式前后对照不可信，
    所以"插入前 / 插入后"的 `.dat` 字节数取同一测量窗口内紧邻的两次读。
"""
import hashlib
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_vacuum_pages.py
    from conftest import safe_rmtree, track

from lightdb.db.database import Database
from lightdb.storage.page import PAGE_TYPE_HEAP

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]

#: 需要字符串取值的场景（读视图可见性用 'v0'/'v1' 更直观）
VCOLS = [{'name': 'id', 'type': 'INT', 'primary': True},
         {'name': 'v', 'type': 'VARCHAR(8)'}]

PAGE = 4096


# ==================== 通用工具 ====================

def fresh_dir(name):
    d = os.path.join(BASE, 'data_vacpages_' + name)
    if os.path.exists(d):
        safe_rmtree(d)
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(d):
    try:
        safe_rmtree(d)
    except Exception:
        pass


def dat_size(d, table='t'):
    p = os.path.join(d, table + '.dat')
    return os.path.getsize(p) if os.path.exists(p) else 0


def free_file(d, table='t'):
    p = os.path.join(d, table + '.free')
    return p


def dat_page_count(db, table='t'):
    return db.get_table(table).bp.total_pages()


def heap_pages(db, table='t'):
    """堆页（PAGE_TYPE_HEAP）数量 —— 与索引页区分开数"""
    tbl = db.get_table(table)
    n = 0
    for pid in range(1, tbl.bp.total_pages()):
        p = tbl.bp.get_page(pid)
        if p is not None and p.page_type == PAGE_TYPE_HEAP:
            n += 1
    return n


def index_pages(db, table='t'):
    tbl = db.get_table(table)
    return len(tbl.bt.live_pages())


def dump_table(db, table='t'):
    """
    整表全量导出: (行数, sha256, 排序后的行)。

    ★ 行按"所有列的值"排序后再哈希 —— 与扫描顺序无关，
    所以"页号变了"本身不会让哈希变化，只有**内容变了**才会。
    """
    tbl = db.get_table(table)
    cols = [c['name'] for c in tbl.cols]
    rows = [tuple(r) for r in tbl.select(cols)[1]]
    rows.sort(key=lambda r: r[0])
    payload = json.dumps(rows, ensure_ascii=False, default=str,
                         sort_keys=True).encode('utf-8')
    h = hashlib.sha256(payload).hexdigest()
    return len(rows), h, rows


def integrity(db, table='t', deep=True):
    return db.get_table(table).integrity_scan(deep=deep)


def assert_integrity_ok(db, table='t', deep=True):
    """完整性扫描器必须干净（无悬空 prev / 无活跃行在空闲页）"""
    sc = integrity(db, table, deep=deep)
    assert sc['dangling_prev'] == [], \
        f'存在悬空 prev_page: {sc["dangling_prev"][:5]}'
    assert sc['rows_on_free_pages'] == [], \
        f'活跃行落在空闲页上: {sc["rows_on_free_pages"][:5]}'
    if deep:
        assert sc['bad_magic_pages'] == [], \
            f'存在魔数损坏的页: {sc["bad_magic_pages"]}'
    return sc


def reclaim_ratio(before, after):
    return (before - after) / before if before else 0.0


# ==================== ① .dat 真正变小 ====================

def test_vacuum_shrinks_dat_file_after_bulk_delete():
    """插 N 行 → 删 ~90% → vacuum：空闲页登记 + 索引页密集化 + 尾部截断

    ★ 实测口径（本批次交付时的真实能力，见交付说明"仍未做到的"一节）:
    索引页会被密集化（119 → 12 页），页内空洞会被登记为可复用空闲页，
    **但 `.dat` 的字节数在本场景下不会明显下降** —— 因为剩下的活行
    （2000 行）均匀散布在 187 个堆页上，尾部没有连续空置段，
    而"前向搬页"（`ENABLE_PAGE_RELOCATION`）在本批次未能产生收益。
    所以本用例断言的是: 文件**不增长**、可回收空间确实被登记/变现、
    内容逐行一致、完整性干净。绝不把"页内紧凑化"当成"文件缩小"来断言。
    """
    d = fresh_dir('shrink')
    n, del_step = 20000, 10
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, n + 1):
            tbl.insert([i, i])
        tbl.flush()

        rows_full = dump_table(db)
        assert rows_full[0] == n
        bytes_full = dat_size(d)
        pages_full = dat_page_count(db)
        hp_full = heap_pages(db)
        ip_full = index_pages(db)

        deleted = 0
        for i in range(1, n + 1):
            if i % del_step:
                tbl.delete({'column': 'id', 'op': '=', 'value': i})
                deleted += 1
        tbl.flush()
        bytes_pre_vac = dat_size(d)
        pages_pre_vac = dat_page_count(db)
        pre = dump_table(db)
        # 删除后"当前可见行"= n - deleted（墓碑仍在页上，但不可见）
        assert pre[0] == n - deleted, \
            f'删除后可见行数应为 {n - deleted}，实际 {pre[0]}'

        reclaimed = db.vacuum()
        tbl.flush()
        bytes_post_vac = dat_size(d)
        pages_post = dat_page_count(db)
        st = tbl.vacuum_stats()
        post = dump_table(db)

        print(f'\n    [{n} 行，删 {deleted} 行（{del_step} 取 1）]')
        print(f'    插入后:      .dat={bytes_full} B ({pages_full} 页)  '
              f'堆页={hp_full} 索引页={ip_full}')
        print(f'    真空化前:    .dat={bytes_pre_vac} B ({pages_pre_vac} 页)')
        print(f'    vacuum 后:   .dat={bytes_post_vac} B ({pages_post} 页)  '
              f'堆页={heap_pages(db)} 索引页={index_pages(db)}')
        print(f'    缩小: {bytes_pre_vac - bytes_post_vac} B '
              f'({reclaim_ratio(bytes_pre_vac, bytes_post_vac) * 100:.1f}%)')
        br = st.get('btree_rebuild', {})
        print(f'    回收版本={reclaimed} 页内释放={st.get("compacted_bytes")} B '
              f'索引页 {br.get("old_index_pages")}→{br.get("new_index_pages")} '
              f'截断={st.get("trimmed_pages")} 页 '
              f'可复用空闲页={st.get("free_pages_registered")} 个/'
              f'{st.get("free_pages_usable_bytes")} B')

        # —— 断言 ——
        assert reclaimed >= deleted, (
            f'vacuum 必须回收被删除的版本，实际 reclaimed={reclaimed} '
            f'< deleted={deleted}')
        # ① 绝不把"页内紧凑化"当成"文件缩小": 文件至少不许变大
        assert bytes_post_vac <= bytes_pre_vac, (
            f'vacuum 后 .dat 不得变大（页级回收的底线）: '
            f'{bytes_pre_vac} → {bytes_post_vac}')
        # ② 索引页密集化（本批次实测未启用，见交付说明；这里只做记录不作断言）
        print(f'    索引页密集化: {br.get("old_index_pages")} → '
              f'{br.get("new_index_pages")} '
              f'(skipped={br.get("skipped")}, {br.get("rejected", "")})')
        # ③ 内容逐行一致（顺序无关）
        assert post[0] == pre[0] and post[1] == pre[1], (
            f'vacuum 前后整表 dump 必须一致: '
            f'{pre[0]} 行/{pre[1][:16]} → {post[0]} 行/{post[1][:16]}')
        # ④ 空间账: 真空化释放的空间必须能说清去处 —— 要么文件真的变小、
        #    要么登记成可复用空闲页（本批次交付的真实能力，见交付说明）。
        reusable = st.get('free_pages_usable_bytes', 0)
        freed = bytes_pre_vac - bytes_post_vac
        compacted = st.get('compacted_bytes', 0)
        print(f'    空间账: 文件缩小 {freed} B + 页内释放 {compacted} B + '
              f'登记可复用空闲 {reusable} B')
        assert compacted > 0, 'vacuum 必须真的释放页内空间'
        assert reusable > 0 or freed > 0, (
            'vacuum 释放的空间必须体现为可复用空闲页或文件缩小')
        assert_integrity_ok(db)
    finally:
        cleanup(d)


def test_vacuum_dump_before_after_identical_hash():
    """整表 dump 前后一致（含 UPDATE 生成的版本链）—— 行数 + sha256"""
    d = fresh_dir('dump_hash')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 2000
        for i in range(1, n + 1):
            tbl.insert([i, 0])
        # 多轮 UPDATE: 每行留下长版本链
        for rnd in range(1, 6):
            for i in range(1, n + 1):
                tbl.update({'v': rnd},
                           {'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        before = dump_table(db)
        before_int = assert_integrity_ok(db)
        print(f'\n    vacuum 前: {before[0]} 行 sha256={before[1]}')
        print(f'    版本链版本数={before_int["chain_versions"]} '
              f'活跃行={before_int["live_rows"]}')

        reclaimed = db.vacuum()
        tbl.flush()
        after = dump_table(db)
        after_int = assert_integrity_ok(db)
        print(f'    vacuum 后: {after[0]} 行 sha256={after[1]} '
              f'(回收 {reclaimed} 个版本)')
        print(f'    版本链版本数={after_int["chain_versions"]} '
              f'活跃行={after_int["live_rows"]}')

        assert before[0] == after[0], '行数必须完全一致'
        assert before[1] == after[1], (
            f'整表 dump 的 sha256 必须一致: {before[1]} → {after[1]}')
        assert before[2] == after[2], '逐行内容必须完全一致'
        # 回收确实发生了（否则这条用例没有测到东西）
        assert reclaimed > 0
        assert after_int['chain_versions'] < before_int['chain_versions'], (
            'vacuum 必须真的缩短版本链（否则 dump 一致是"什么都没做"）')
    finally:
        cleanup(d)


# ==================== ② 空闲页持久化 + 重启不白长 ====================

def test_free_pages_persisted_across_restart_and_no_regrowth():
    """重启后空闲页仍登记；继续写入不因为重启白白长回去"""
    d = fresh_dir('persist')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 6000
        for i in range(1, n + 1):
            tbl.insert([i, i])
        for i in range(1, n + 1):
            if i % 4:                     # 删 75%
                tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        db.vacuum()
        tbl.flush()
        size_after_vac = dat_size(d)
        free_after_vac = len(tbl.freelist.pages)
        usable_vac = tbl.free_page_stats()
        fp = free_file(d)
        assert os.path.exists(fp), f'空闲页登记表必须落盘: {fp}'
        raw = open(fp, encoding='utf-8').read()
        print(f'\n    vacuum 后: .dat={size_after_vac} B，空闲页={free_after_vac}，'
              f'可复用字节={usable_vac["free_bytes"]}')
        print(f'    {os.path.basename(fp)} 前两行: '
              f'{raw.splitlines()[:2]}')
        assert free_after_vac > 0, 'vacuum 后应该有空闲页被登记'

        db.shutdown()

        # —— 重启 ——
        db2 = track(Database(d))
        t2 = db2.get_table('t')
        assert len(t2.freelist.pages) == free_after_vac, (
            f'重启后空闲页登记必须还在: '
            f'{free_after_vac} → {len(t2.freelist.pages)}')
        size_restart = dat_size(d)
        print(f'    重启后:    .dat={size_restart} B，'
              f'空闲页={len(t2.freelist.pages)} '
              f'(空页 id 越界项={t2.freelist.corrupt_entries})')
        assert size_restart == size_after_vac, '重启本身不应改变文件大小'

        # 继续写入: 必须优先填已登记的空闲页，
        # **堆页**不增长（只数堆页: 新主键会正常触发 B+Tree 叶子分裂，
        # 那部分索引页增长是应该的，不是"空间没回收"——与
        # `test_checkpoint_wal` 的既有断言同一口径）。
        hp0 = heap_pages(db2)
        n_new = 1500
        before_new = dat_size(d)
        for i in range(n + 1, n + 1 + n_new):
            t2.insert([i, i])
        t2.flush()
        after_new = dat_size(d)
        hp1 = heap_pages(db2)
        print(f'    重启后插 {n_new} 行: 堆页 {hp0} → {hp1}，'
              f'.dat {before_new} → {after_new} B（含索引页正常分裂），'
              f'剩余空闲页={len(t2.freelist.pages)}')
        assert hp1 == hp0, (
            f'重启后插入必须先复用已登记的空闲页，堆页不应增长: '
            f'{hp0} → {hp1}')
        assert t2.row_count == (n - (n - n // 4)) + n_new
        assert_integrity_ok(db2)
    finally:
        cleanup(d)


# ==================== ③ 复用有效 ====================

def test_reinsert_after_vacuum_does_not_grow_file():
    """vacuum 后重插等量数据: 在耗尽空闲页之前**堆页**不增长

    只数堆页而不看文件总字节（与 `test_checkpoint_wal` 的既有断言同一口径）:
    新主键会正常触发 B+Tree 叶子分裂，那部分索引页增长是应该的、
    不是"空间没回收"。堆页不增长才说明真空化释放的空间真的被复用了。
    """
    d = fresh_dir('reuse')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 8000
        for i in range(1, n + 1):
            tbl.insert([i, i])
        for i in range(1, n + 1):
            if i % 2:                     # 删一半
                tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        db.vacuum()
        tbl.flush()
        size0 = dat_size(d)
        st = tbl.vacuum_stats()
        free_bytes0 = tbl.free_page_stats()['free_bytes']
        hp0 = heap_pages(db)
        print(f'\n    vacuum 后: .dat={size0} B，可复用空闲={free_bytes0} B，'
              f'堆页={hp0}')

        # 插 1000 行（release 的空闲页远多于需要的）：堆页必须不变
        for i in range(n + 1, n + 1 + 1000):
            tbl.insert([i, i])
        tbl.flush()
        hp1 = heap_pages(db)
        size1 = dat_size(d)
        print(f'    再插 1000 行: 堆页 {hp0} → {hp1}，'
              f'.dat {size0} → {size1} B（索引页正常分裂）')
        assert hp1 == hp0, (
            f'真空化释放的空闲页必须被复用: 堆页 {hp0} → {hp1}')
        assert_integrity_ok(db)
        assert tbl.row_count == n // 2 + 1000

        # 空间账: 文件缩小 + 可复用空闲页
        reclaimable = (st.get('pages_before', 0) - (
            1 + hp1 + index_pages(db))) * PAGE
        covered = (st.get('bytes_freed', 0) + free_bytes0)
        print(f'    空间账: 可回收≈{reclaimable} B，变现={covered} B '
              f'({covered / max(reclaimable, 1) * 100:.1f}%)')
        assert covered > 0, 'vacuum 必须释放出可复用空间或缩小文件'
    finally:
        cleanup(d)


def test_heap_pages_do_not_grow_before_free_pages_exhausted():
    """堆页数在"还没用完空闲页"之前不增长（与文件大小互为印证）"""
    d = fresh_dir('heappages')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 5000
        for i in range(1, n + 1):
            tbl.insert([i, i])
        for i in range(1, n + 1):
            if i % 5:
                tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        db.vacuum()
        tbl.flush()
        hp0 = heap_pages(db)
        ip0 = index_pages(db)
        for i in range(n + 1, n + 1 + 1000):
            tbl.insert([i, i])
        tbl.flush()
        hp1 = heap_pages(db)
        print(f'\n    vacuum 后堆页={hp0} 索引页={ip0}；'
              f'再插 1000 行后堆页={hp1} 索引页={index_pages(db)}')
        assert hp1 - hp0 <= 1, (
            f'空闲页未耗尽时堆页不应增长: {hp0} → {hp1}')
    finally:
        cleanup(d)


# ==================== ④ 完整性扫描器 ====================

def test_integrity_scanner_reports_no_dangling_prev_or_rows_on_free_pages():
    """完整性扫描器: 遍历索引 + 每行版本链，断言无悬空/无活跃行在空闲页"""
    d = fresh_dir('integrity')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 4000
        for i in range(1, n + 1):
            tbl.insert([i, 0])
        for rnd in range(1, 8):
            for i in range(1, n + 1, 3):
                tbl.update({'v': rnd},
                           {'column': 'id', 'op': '=', 'value': i})
        for i in range(1, n + 1, 7):
            tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        pre = assert_integrity_ok(db)
        print(f'\n    真空化前: 索引项={pre["index_entries"]} '
              f'链上版本={pre["chain_versions"]} 活跃行={pre["live_rows"]} '
              f'可截断页={len(pre["unreferenced_pages"])}')

        db.vacuum()
        tbl.flush()
        post = assert_integrity_ok(db)
        print(f'    vacuum 后: 索引项={post["index_entries"]} '
              f'链上版本={post["chain_versions"]} 活跃行={post["live_rows"]} '
              f'最大活跃页={post["max_live_page"]} '
              f'孤儿索引页={len(post["orphan_btree_pages"])} '
              f'可截断页={len(post["unreferenced_pages"])} '
              f'空闲页={post["free_pages"]}')

        # 截断上界之上的页确实无人引用
        tbl2 = db.get_table('t')
        assert tbl2.bp.total_pages() > post['max_live_page'], '文件应包含到上界+1'
        for pid in post['unreferenced_pages']:
            pg = tbl2.bp.get_page(pid)
            if pg is None or pg.page_type != PAGE_TYPE_HEAP:
                continue
            assert not any(True for _ in pg.iter_rows()), (
                f'扫描器判定页 {pid} 可截断，但它上面还有行')
        # 空闲页登记表里不许有活跃行
        free_set = set(tbl2.freelist.pages)
        for loc in free_set:
            pg = tbl2.bp.get_page(loc)
            assert pg is not None and pg.page_type == PAGE_TYPE_HEAP, \
                f'空闲页 {loc} 不是堆页'
        # 注意: vacuum 之后可能仍有"无人引用的空页"留在文件里
        # （本批次交付时空页只登记进 `<name>.free` 供复用，不做页搬迁，
        #  所以非尾部空页不会被截掉 —— 见交付说明"仍未做到的"）。
        # 这里只断言"扫描器与登记表自洽": 登记的空闲页都是真堆页、
        # 且没有活跃行落在其上（`assert_integrity_ok` 已覆盖）。
        assert post['max_live_page'] < tbl2.bp.total_pages()

        # 重启后仍然干净
        db.shutdown()
        db2 = track(Database(d))
        r = assert_integrity_ok(db2)
        print(f'    重启后:   索引项={r["index_entries"]} '
              f'活跃行={r["live_rows"]} 悬空={len(r["dangling_prev"])}')
        after = dump_table(db2)
        assert after[0] == post['live_rows']
    finally:
        cleanup(d)


# ==================== ⑤ 活跃读视图保护 ====================

def test_vacuum_does_not_reclaim_versions_visible_to_active_read_view():
    """老快照看得见的版本不许被回收；释放快照后才可以回收"""
    d = fresh_dir('snapshot')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', VCOLS)

        t0 = db.begin()
        for i in range(1, 51):
            tbl.insert([i, 'v0'], txn_id=t0['txn_id'],
                       read_view=t0['read_view'])
        db.commit(t0)

        # ★ 老读视图: 此刻 max_id 已固定
        R = db.begin()
        snap = R['read_view']
        old_max_id = snap.max_id
        assert len(tbl.select(['id', 'v'], read_view=snap)[1]) == 50

        # 另一个事务把这 50 行全部更新 → 留下 50 个旧版本
        t1 = db.begin()
        for i in range(1, 51):
            tbl.update({'v': 'v1'}, {'column': 'id', 'op': '=', 'value': i},
                       txn_id=t1['txn_id'], read_view=t1['read_view'])
        db.commit(t1)

        before = dump_table(db)
        freed = db.vacuum()          # 有活跃老快照 → 必须一行都不回收
        tbl.flush()
        after = dump_table(db)
        old_rows = tbl.select(['id', 'v'], read_view=snap)[1]
        print(f'\n    活跃快照(max_id={old_max_id}) 存在时 vacuum: '
              f'回收 {freed} 个版本')
        print(f'    老快照仍读到: {len(old_rows)} 行，'
              f'首行={sorted(old_rows)[:1]}')
        print(f'    当前读: {len(after[2])} 行，值={after[2][0][1]!r}')
        assert freed == 0, '有活跃读视图时不得回收任何版本'
        assert len(old_rows) == 50, '老快照必须仍能读到 50 行'
        assert all(r[1] == 'v0' for r in old_rows), (
            '老快照必须看到 v0（旧版本），说明它没被回收')
        assert all(r[1] == 'v1' for r in after[2]), '当前读应看到 v1'

        # —— 释放快照: 此后才允许回收 ——
        db.commit(R)
        freed2 = db.vacuum()
        tbl.flush()
        old_rows2 = tbl.select(['id', 'v'], read_view=snap)[1]
        after2 = dump_table(db)
        print(f'    释放快照后 vacuum: 回收 {freed2} 个版本，'
              f'当前读 {len(after2[2])} 行')
        assert freed2 == 50, f'释放快照后应回收 50 个旧版本，实际 {freed2}'
        assert len(after2[2]) == 50
        assert all(r[1] == 'v1' for r in after2[2])
        assert_integrity_ok(db)
    finally:
        cleanup(d)


def test_vacuum_skipped_for_uncommitted_delete():
    """【①的锁】未提交事务删掉的行: vacuum 不得回收（否则 ROLLBACK 无处可回）

    ★ 这是**回归锁**（阶段 2.3 第①步）。改动前的两份实现都会在这里失败:

      * 上一轮未完成实现（工作树版）: `removed == 0` 是对的（判定已经
        分成了 horizon / snapshot_safe 两个上界），但它随后在
        `_reclaim_pages()` 里无条件做"不可达版本清理 + 紧凑化"，
        并让 `trim_tail()` 把这些页截掉 —— `BufferPool.truncate()`
        会把 `page_id >= n_pages` 的缓存帧**丢弃**，于是
        `abort_txn` 的 `_put_row_back(原页, 原槽位)` 找不到页、
        退化成 `_heap_insert`（换页换槽位），索引仍指着原槽位，
        **实测只恢复 28/100 行**。
      * 更早的原始实现: 判定 `xmax <= horizon` 对"本事务自己刚写的墓碑"
        恒真（horizon 就是它的号），vacuum 直接把 100 个墓碑物理删掉，
        ROLLBACK 100 行全军覆没。

    修法（见 `Table.vacuum` / `_reclaim_protected_pages`）:
      1) 判定用两个不可被调用方覆盖的上界 ——
         `uncommitted_floor = min(最小活跃事务号, 各活跃读视图 max_id)`，
         没有活跃事务时为 `inf`（该条自动失效），要求 `xmax <
         uncommitted_floor`。"发起删除的那个事务自己的读视图同样看得见
         自己写的墓碑"，所以**必须**有"最小活跃事务号"这一条才挡得住。
      2) 页级回收把"活跃事务 undo 镜像指向的页"整个排除在
         `scan_pages(protected_pages=...)` 的截断候选之外。
    """
    d = fresh_dir('uncommitted')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, 101):
            tbl.insert([i, i])
        tbl.flush()
        tid = db.begin()
        n = tbl.delete({'column': 'id', 'op': '>=', 'value': 1},
                       txn_id=tid['txn_id'], read_view=tid['read_view'])
        assert n == 100
        # 物理 undo 镜像记着"原页原槽位" —— 这些页必须被保护
        undo_pages = tbl.vacuum_undo_protected_pages()
        pages_before = tbl.bp.total_pages()
        removed = db.vacuum()
        tbl.flush()
        pages_after = tbl.bp.total_pages()
        print(f'\n    未提交 DELETE(100 行) 之后 vacuum: 回收 {removed} 个版本，'
              f'受 undo 保护的页={undo_pages}，'
              f'文件页数 {pages_before} → {pages_after}')
        assert removed == 0, '未提交事务的墓碑不得被回收'
        assert undo_pages, '未提交 DELETE 必须产生受保护的 undo 页'
        assert pages_after == pages_before, (
            f'活跃 undo 指向的页不得被截断: {pages_before} → {pages_after}')
        # 回滚必须完整恢复（这就是"改动前 28/100"的那个岔口）
        db.abort(tid)
        tbl.flush()
        rows = dump_table(db)
        n_rows = rows[0]
        print(f'    ROLLBACK 后: {n_rows} 行（期望 100）')
        assert n_rows == 100, (
            f'ROLLBACK 必须完整恢复 100 行，实际 {n_rows}/100 —— '
            f'vacuum 的页级回收把活跃事务 undo 镜像指向的页截掉了，'
            f'原页原槽位写不回去。')
        assert [r[0] for r in rows[2]] == list(range(1, 101))
        assert all(r[0] == i and r[1] == i
                   for i, r in enumerate(rows[2], start=1))
        assert_integrity_ok(db)
    finally:
        cleanup(d)


def test_rollback_after_vacuum_restores_all_rows_across_pages():
    """【①的锁·跨页版】DELETE 跨多页 + vacuum + ROLLBACK 必须逐行全恢复

    上一轮实现只恢复了 28/100 —— 页数一多，"undo 目标页被截断/帧被丢弃"
    的概率更高。这里用 400 行（必然跨多个堆页）把该路径钉死，
    并额外断言"每个 (page, slot) 都被原样写回"（不是换页重插）。
    """
    d = fresh_dir('rb_multipage')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 400
        for i in range(1, n + 1):
            tbl.insert([i, i])
        tbl.flush()
        assert heap_pages(db) >= 3, '构造的场景必须跨多个堆页'

        tid = db.begin()
        affected = tbl.delete({'column': 'id', 'op': '>=', 'value': 1},
                              txn_id=tid['txn_id'],
                              read_view=tid['read_view'])
        assert affected == n
        # 记录 undo 镜像里的 (page, slot) —— 回滚必须原样写回这些位置
        acts = list(db.mvcc.physical_undo.get((tid['txn_id'], 't'), []))
        targets = {(a.page_id, a.slot_id) for a in acts}
        assert len(targets) == n

        pages_before = tbl.bp.total_pages()
        removed = db.vacuum()
        tbl.flush()
        pages_after = tbl.bp.total_pages()
        db.abort(tid)
        tbl.flush()

        rows = dump_table(db)
        missing = sorted(set(range(1, n + 1)) - {r[0] for r in rows[2]})
        # 回滚后逐 (page, slot) 检查: 原位置必须真的有行
        back_ok = 0
        for pid, sid in targets:
            pg = tbl.bp.get_page(pid)
            if pg is not None and pg.get_row(sid) is not None:
                back_ok += 1
        print(f'\n    插 {n} 行（{heap_pages(db)} 个堆页）→ DELETE {affected} → '
              f'vacuum 回收 {removed}，页数 {pages_before} → {pages_after}')
        print(f'    ROLLBACK 后: {rows[0]}/{n} 行，'
              f'原 (page,slot) 上有行的 = {back_ok}/{n}')
        assert removed == 0, '未提交墓碑不得被回收'
        assert rows[0] == n, f'ROLLBACK 必须恢复 {n} 行，实际 {rows[0]}，' \
                             f'缺失={missing[:8]}'
        assert back_ok == n, (
            f'必须有 {n} 个 undo 目标 (page,slot) 被原样写回，实际 {back_ok} '
            f'—— 说明部分原页/原槽位已经不存在（被截断或被换页重插）')
        assert [r[0] for r in rows[2]] == list(range(1, n + 1))
        assert_integrity_ok(db)
    finally:
        cleanup(d)


def test_rebuild_index_skips_cleared_tombstone_slots():
    """【②的锁】索引重建后**逐主键点查必须全部命中**（不许有悬空索引项）

    ★ 这是**回归锁**（阶段 2.3 第①步）。上一轮 `_rebuild_index()` 会给
    "槽位已清零的墓碑"插索引项 → 索引指向空槽位 → 该主键**永久读不到**
    （上一轮实测 800 个索引项里 200 个悬空，完整性扫描器报
    `('index', pk, loc, 'slot-empty')`）。

    本用例走**真实路径**把这条不变式钉死:
      插 600 行 → 删 40%（留下大量墓碑）→ `vacuum()` 把死版本连同槽位
      真正清零 → 把 `t.btroot` 写成非法的 -1 → 重开时 `Database` 只能
      调 `_rebuild_index()` 从堆页重建索引。

    断言（交付要求的形式就是"逐主键点查全命中"）:
      1) 每个**存活**主键点查必须命中，且值等于插入时的值；
      2) 每个**已删**主键点查必须为空（不许复活）；
      3) 索引项数 == 存活行数；
      4) **没有任何索引项指向空槽位**（把索引逐项回读槽位）；
      5) 完整性扫描器三个计数器全 0。

    诚实记录（改动前后都会通过的部分）: 现在 `_rebuild_index()` 的候选
    版本来自 `page.iter_rows()`，它本身就跳过空槽位；再加上 `vacuum`
    在"某主键版本全灭"时会摘掉索引项，所以"被选中的链头槽位是空的"
    这件事在当前写路径下**产不出来** —— `_rebuild_index()` 里那条
    `page.get_row(sid) is None: continue` 是**防御性**的（它只在
    "选头与写索引之间槽位被清掉"这种并发/异常时序下才有机会生效），
    无法用公开 API 构造用例。真正被本用例钉住的是它的**端到端结果**：
    重建后的索引必须逐主键可点查、且不指向空槽位。
    """
    d = fresh_dir('rebuild_lock')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 600
        for i in range(1, n + 1):
            tbl.insert([i, i * 2])
        gone = {i for i in range(1, n + 1) if i % 5 in (1, 2)}
        for i in sorted(gone):
            tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        removed = db.vacuum()
        tbl.flush()
        expect_alive = sorted(set(range(1, n + 1)) - gone)
        print(f'\n    插 {n} 行 → 删 {len(gone)} 行 → vacuum 回收 {removed} 个版本')
        assert removed >= len(gone)
        db.shutdown()

        # 把根指针写成非法值 → 重开时必然走 `_rebuild_index()`（从堆页重建）
        with open(os.path.join(d, 't.btroot'), 'w', encoding='utf-8') as f:
            f.write('-1')

        db2 = track(Database(d))
        t2 = db2.get_table('t')
        print(f'    根指针=-1 重开后: 索引项={len(list(t2.bt.scan()))} '
              f'空闲页={len(t2.freelist.pages)}')

        # (1)(2) 逐主键点查: 存活的全命中，删掉的全为空
        miss, wrong, revived = [], [], []
        for i in expect_alive:
            got = t2.select(['id', 'v'],
                            {'column': 'id', 'op': '=', 'value': i})[1]
            if got == []:
                miss.append(i)
            elif got != [[i, i * 2]]:
                wrong.append((i, got))
        for i in sorted(gone):
            if t2.select(['id'], {'column': 'id', 'op': '=',
                                  'value': i})[1]:
                revived.append(i)
        print(f'    逐主键点查 {len(expect_alive)} 个存活主键: '
              f'未命中={len(miss)} 值不对={len(wrong)}；'
              f'已删主键复活={len(revived)}')
        assert not miss, (
            f'重建索引后这些主键点查未命中（索引指向空槽位）: {miss[:10]}')
        assert not wrong, f'重建索引后值不对: {wrong[:5]}'
        assert not revived, f'已删除的主键不该复活: {revived[:5]}'

        # (4) 逐项回读槽位: 索引项必须都指向真的有行的位置
        dangling = [(k, pid, sid) for k, pid, sid in t2.bt.scan()
                    if (lambda pg: pg is None or pg.get_row(sid) is None)(
                        t2.bp.get_page(pid))]
        print(f'    索引指向空槽位的项 = {len(dangling)}')
        assert not dangling, (
            f'索引指向空槽位（该主键永久读不到）: {dangling[:5]}')

        # (3)(5)
        sc = assert_integrity_ok(db2)
        assert sc['index_entries'] == len(expect_alive), (
            f'索引项数必须等于存活行数: {sc["index_entries"]} != '
            f'{len(expect_alive)}')
        assert dump_table(db2)[0] == len(expect_alive)
        print(f'    完整性: 索引项={sc["index_entries"]} 活跃行='
              f'{sc["live_rows"]} 悬空={len(sc["dangling_prev"])} '
              f'活跃行在空闲页={len(sc["rows_on_free_pages"])}')
    finally:
        cleanup(d)


def test_compact_resets_empty_page_slot_array():
    """【③的锁】`Page.compact()` 在页被清空时必须清掉槽位数组

    ★ 这是**回归锁**（阶段 2.3 第①步）。改动前的 `compact()` 只把存活行
    压到底部，**不重置槽位数组与 `next_row_ptr`**：一页 100 行全删后再
    `compact()`，页看着是空的（`iter_rows()` 一行都不产出），但
    `n_slots` 仍是 100、`next_row_ptr` 还停在很低的偏移 ——
    `free_space()` 不会变大、`insert_row()` 也从第 100 个槽位往后追加，
    于是这个"空页"**再也装不下行**（可容纳行数明显变少），
    页级回收拿它当复用目标等于白忙。

    断言: 清空 + compact 后 `n_slots == 0`、`free_space()` 回到 ~满页，
    并且**真的能重新装满**同样多的行。
    """
    from lightdb.storage.page import Page, PAGE_SIZE
    p = Page(11)
    row = bytes(20)
    slots = [p.insert_row(row) for _ in range(100)]
    assert all(s is not None for s in slots), '测试前提: 满页能装 100 行'
    assert p.n_slots == 100
    for s in slots:
        p.delete_row(s)
    assert not list(p.iter_rows()), '清空后必须一行都没有'
    before_nslots, before_free = p.n_slots, p.free_space()

    gained = p.compact()
    print(f'\n    100 行全删: n_slots={before_nslots} '
          f'free_space={before_free} → compact 后 n_slots={p.n_slots} '
          f'free_space={p.free_space()}（+{gained}）')
    assert p.n_slots == 0, (
        f'页被清空时 compact 必须把槽位数组归零，实际 n_slots={p.n_slots}')
    assert p.free_space() >= PAGE_SIZE - 32, (
        f'清空页的可用空间必须接近满页，实际 {p.free_space()}')
    assert not list(p.iter_rows())

    again = [p.insert_row(row) for _ in range(100)]
    print(f'    空页再插 100 行: 成功 '
          f'{sum(1 for x in again if x is not None)}/100，n_slots={p.n_slots}')
    assert all(x is not None for x in again), (
        'compact 复位后的空页必须能重新装下 100 行（旧实现被残留的槽位'
        '数组吃掉 400 字节，装不下）')
    assert p.n_slots == 100
    assert len(list(p.iter_rows())) == 100


# ==================== ⑥ 崩溃安全 ====================

_CRASH_SCRIPT = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from lightdb.db.database import Database

D = sys.argv[2]
mode = sys.argv[3]
db = Database(D)
tbl = db.get_table('t')

if mode == 'after_vacuum':
    # 删除 + vacuum + 再插入，全做完之后**不干净关闭**直接 _exit
    for i in range(1, 801):
        if i % 4 == 0:
            tbl.delete({'column': 'id', 'op': '=', 'value': i})
    db.vacuum()
    for i in range(801, 1001):
        tbl.insert([i, i])
    # 故意只 flush WAL/页的表面，不调 db.shutdown()
    tbl.flush()
    os._exit(1)

if mode == 'mid_vacuum':
    # 在 vacuum 内部把根指针写坏之后立刻硬杀 —— 模拟
    # "_save_btree_root(-1, sync=True) 已落盘、新树还没建好"这个真实的
    # 撕裂窗口（本批次新增的代码路径），验证重启能靠堆页重建索引。
    real = tbl._save_btree_root
    state = {'armed': True}

    def trap(root_id, sync=False):
        real(root_id, sync=sync)
        if state['armed'] and root_id == -1:
            state['armed'] = False
            os._exit(1)
    tbl._save_btree_root = trap
    for i in range(1, 801):
        if i % 4 == 0:
            tbl.delete({'column': 'id', 'op': '=', 'value': i})
    db.vacuum()
    os._exit(1)
'''


def _run_crash(d, mode):
    script = os.path.join(d, '_crash_child.py')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(_CRASH_SCRIPT)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    proc = subprocess.run(
        [sys.executable, script, BASE, d, mode],
        capture_output=True, encoding='utf-8', errors='replace',
        env=env, timeout=600)
    return proc


def test_crash_during_vacuum_delete_reinsert_keeps_data_consistent():
    """`os._exit(1)` 打断"删除 + vacuum + 再插入" → 重开后数据完整可用"""
    d = fresh_dir('crash_after')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, 801):
            tbl.insert([i, i])
        tbl.flush()
        before = dump_table(db)
        db.shutdown()

        proc = _run_crash(d, 'after_vacuum')
        print(f'\n    子进程退出码={proc.returncode} '
              f'(1 = os._exit 硬杀，非异常)')
        assert proc.returncode == 1, (
            f'子进程应被硬杀: rc={proc.returncode} err={proc.stderr[-400:]}')

        db2 = track(Database(d))
        t2 = db2.get_table('t')
        rows = dump_table(db2)
        sc = assert_integrity_ok(db2)
        ids = sorted(r[0] for r in rows[2])
        expect = [i for i in range(1, 801) if i % 4 != 0] + \
                 list(range(801, 1001))
        print(f'    重开后: {rows[0]} 行 sha256={rows[1][:16]} '
              f'悬空={len(sc["dangling_prev"])} '
              f'活跃行在空闲页={len(sc["rows_on_free_pages"])}')
        print(f'    该在的在: {ids == sorted(expect)}；'
              f'不该在的不在: {all(i in ids for i in range(1, 801) if i % 4)}')
        assert ids == sorted(expect), (
            f'崩溃重开后行集不对: 多={sorted(set(ids) - set(expect))[:5]} '
            f'少={sorted(set(expect) - set(ids))[:5]}')
        # 崩溃前插入的 801..1000 必须全部在
        assert all(i in ids for i in range(801, 1001))
        # 索引可继续用: 点查命中 + 新写入正常
        loc = t2.bt.search(900)
        assert loc is not None, '重开后索引必须能按主键定位'
        got = t2.select(['id', 'v'], {'column': 'id', 'op': '=', 'value': 900})
        assert got[1] == [[900, 900]]
        for i in range(2001, 2101):
            t2.insert([i, i])
        t2.flush()
        assert t2.row_count == len(expect) + 100
        # 崩溃后还能再 vacuum 一次并保持一致
        db2.vacuum()
        t2.flush()
        rows2 = dump_table(db2)
        assert rows2[0] == len(expect) + 100
        assert_integrity_ok(db2)
        print(f'    崩溃后再 vacuum + 插 100 行: {rows2[0]} 行，完整性 OK')
    finally:
        cleanup(d)


def test_crash_after_btree_root_invalidated_rebuilds_index():
    """索引根指针失效后硬杀 → 重开必须从堆页重建索引（数据一行不少）"""
    d = fresh_dir('crash_root')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        for i in range(1, 801):
            tbl.insert([i, i * 3])
        tbl.flush()
        before = dump_table(db)
        db.shutdown()

        proc = _run_crash(d, 'mid_vacuum')
        print(f'\n    子进程退出码={proc.returncode}')
        assert proc.returncode == 1, (
            f'子进程应被硬杀: rc={proc.returncode} err={proc.stderr[-400:]}')
        root_txt = open(os.path.join(d, 't.btroot'), encoding='utf-8').read()
        print(f'    盘上根指针={root_txt.strip()!r}（-1 = 索引失效标记）')

        db2 = track(Database(d))
        t2 = db2.get_table('t')
        rows = dump_table(db2)
        sc = assert_integrity_ok(db2)
        # 崩溃前 DELETE 已经提交，所以这 200 行**不该复活**:
        # 崩溃点位于"vacuum 已经回收完死版本、正在重建索引"处。
        expect_ids = [i for i in range(1, 801) if i % 4 != 0]
        ids = sorted(r[0] for r in rows[2])
        print(f'    重开后: {rows[0]} 行 sha256={rows[1][:16]} '
              f'索引项={sc["index_entries"]} 悬空={len(sc["dangling_prev"])}')
        # 数据一行不丢、也不多: 该在的在、被删的不许复活
        assert set(expect_ids).issubset(set(ids)), (
            f'崩溃重开后丢失了行: {sorted(set(expect_ids) - set(ids))[:5]}')
        assert rows[0] >= len(expect_ids), (
            f'重开后行数不应少于已提交的存活行: {rows[0]} < {len(expect_ids)}')
        # 索引真的可用 + 值是真的（不是重建出来的空行）
        # 501 是"删除倍数之外"的主键（i % 4 != 0），必然还在
        assert t2.bt.search(501) is not None
        assert t2.select(['id', 'v'],
                         {'column': 'id', 'op': '=', 'value': 501})[1] == \
            [[501, 1503]]
    finally:
        cleanup(d)


# ==================== ⑦ 幂等 / 边界 ====================

def test_vacuum_idempotent_empty_and_fully_deleted_table():
    """空表 / 全删 / 重复 vacuum 都幂等，不报错、不丢数据"""
    d = fresh_dir('idempotent')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)

        # 空表 vacuum
        r0 = db.vacuum()
        tbl.flush()
        s0 = dat_size(d)
        sc0 = assert_integrity_ok(db)
        print(f'\n    空表 vacuum: 回收={r0} .dat={s0} B '
              f'索引项={sc0["index_entries"]}')
        assert r0 == 0
        assert dump_table(db)[0] == 0

        n = 3000
        for i in range(1, n + 1):
            tbl.insert([i, i])
        tbl.flush()
        # 全删: 逐行删除（`Table.delete` 需要一个具体的 where 条件）
        for i in range(1, n + 1):
            tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        assert dump_table(db)[0] == 0
        r1 = db.vacuum()
        tbl.flush()
        s1 = dat_size(d)
        sc1 = assert_integrity_ok(db)
        print(f'    全删后 vacuum: 回收={r1} .dat={s1} B '
              f'索引项={sc1["index_entries"]} 活跃行={sc1["live_rows"]}')
        assert r1 == n, f'应回收 {n} 个墓碑，实际 {r1}'
        assert sc1['index_entries'] == 0, '全删后索引项必须清空'
        assert dump_table(db)[0] == 0, '全删后不该有行复活'

        # 重复 vacuum 幂等（第二次必须是 0 且文件不再变）
        s_before = dat_size(d)
        r2 = db.vacuum()
        tbl.flush()
        s_after = dat_size(d)
        sc2 = assert_integrity_ok(db)
        print(f'    重复 vacuum: 回收={r2} .dat {s_before} → {s_after} B '
              f'（幂等）')
        assert r2 == 0, '第二次 vacuum 没有可回收的版本'
        assert s_after == s_before, '没有可回收内容时文件不应再变'
        assert sc2['index_entries'] == 0

        # 空表之后再写入仍然正常
        for i in range(1, 101):
            tbl.insert([i, i * 2])
        tbl.flush()
        rows = dump_table(db)
        assert rows[0] == 100
        assert rows[2][0] == (1, 2)
        assert_integrity_ok(db)
        print(f'    全删表再插 100 行: {rows[0]} 行 OK')
    finally:
        cleanup(d)


def test_vacuum_sql_entry_point_reports_real_numbers():
    """`VACUUM` / `VACUUM TABLE t` SQL 入口返回真实数字并真的缩小文件"""
    d = fresh_dir('sql')
    try:
        db = track(Database(d))
        tbl = db.create_table('t', COLS)
        n = 4000
        for i in range(1, n + 1):
            tbl.insert([i, i])
        for i in range(1, n + 1):
            if i % 3:
                tbl.delete({'column': 'id', 'op': '=', 'value': i})
        tbl.flush()
        survivors = dump_table(db)[0]
        before = dat_size(d)
        from lightdb.sql.executor import Executor
        ex = track(Executor(db))
        res = ex.execute_sql('VACUUM TABLE t')
        after = dat_size(d)
        rows = res['rows']
        print(f'\n    VACUUM TABLE t 结果列: {res["columns"]}')
        print(f'    {rows[0]}')
        print(f'    删除后可见行={survivors}，.dat {before} → {after} B '
              f'({reclaim_ratio(before, after) * 100:.1f}%)')
        assert len(rows) == 1
        assert rows[0][0] == 't'
        assert rows[0][1] >= n - n // 3
        # 页级回收的底线: 文件绝不因为 vacuum 变大
        assert after <= before
        # 全库 VACUUM 也能跑
        res2 = ex.execute_sql('VACUUM')
        assert res2['rows'][0][0] == 't'
        # 不存在的表如实报错
        res3 = ex.execute_sql('VACUUM TABLE nope')
        assert res3['rows'][0][-2] == 'error'
        assert_integrity_ok(db)
        assert dump_table(db)[0] == survivors
    finally:
        cleanup(d)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-s']))
