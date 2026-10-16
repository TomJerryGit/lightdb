"""多值 INSERT 的**语句级原子性** + 审计批量 flush（阶段 2 第 2 项，新增文件）

背景（上一轮已定位）：`executor._cmd_insert` 对
`INSERT INTO t VALUES (...),(...),(...)` 的 values_list **逐行**调
`tbl.insert()`，每行各自 autocommit。后果：
  1) 正确性：一条多值 INSERT 中途失败（主键冲突/CHECK/NOT NULL/类型错）
     时前面的行已经提交留下 —— MySQL 里一条 INSERT 语句是原子的；
  2) 性能：每行一次 WAL COMMIT 记录 + 行锁 + 合成事务号，把批量省下的
     解析/审计收益全淹没。

本文件覆盖（全部为新增断言，不改动任何既有测试）：
  ① 多值 INSERT 第 3 行主键冲突 → 表里 **0 行**（改前会留下 2 行）；
  ② CHECK 违规 / NOT NULL 违规 / 类型转换错 中途失败 → 同样 0 行；
  ③ 显式事务里的多值 INSERT 失败 → 该语句无效，但事务**没有被提前
     提交或回滚**：事务仍可继续写入，也可整体 ROLLBACK；
  ④ `os._exit(1)` 硬杀打断多值 INSERT（并且把半截脏页强制刷盘）→
     重开后不留半行，已提交的完整语句仍在（WAL-before-data 未破坏）；
  ⑤ 同一语句内多行省略主键 → 得到连续且互不冲突的值；失败回滚后不留下
     "已消耗"的自增错乱；
  ⑥ 语句提交前，其它连接的快照看不到任何一行（MVCC 可见性）；
  ⑦ 批量写入路径的 WAL 仍然有界（与阶段 2 第 1 项 checkpoint 协同）；
  ⑧ 审计批量 flush：攒批 + 故障重试不丢 + 停机/显式 close 落盘 +
     失败语句立即落盘 + 审计故障不影响语句结果。
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_insert_atomicity.py
    from conftest import safe_rmtree, track

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]


def fresh_dir(name):
    d = os.path.join(BASE, 'data_insatomic_' + name)
    if os.path.exists(d):
        safe_rmtree(d)
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(d):
    try:
        safe_rmtree(d)
    except Exception:
        pass


def make_env(d, ddl=("CREATE TABLE t (id INT PRIMARY KEY, v INT)")):
    """建库 + 建表 + 建执行器（语句走完整 SQL 路径）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = track(Database(d))
    ex = Executor(db)
    for stmt in ([ddl] if isinstance(ddl, str) else ddl):
        r = ex.execute_sql(stmt)
        assert r['type'] == 'ok', r
    return db, ex, db.get_table('t')


def ids(ex):
    r = ex.execute_sql('SELECT id FROM t ORDER BY id')
    assert r['type'] == 'resultset', r
    return [row[0] for row in r['rows']]


def expect_error(ex, sql, code=None):
    r = ex.execute_sql(sql)
    assert r['type'] == 'error', f'应当失败却成功了: {sql} -> {r}'
    if code is not None:
        assert r['code'] == code, f'errno 应为 {code}，实际 {r}'
    return r


# ==================== ① 主键冲突 → 0 行 ====================

def test_multi_value_pk_conflict_rolls_back_whole_statement():
    """第 3 行主键冲突：整条语句回滚，表里 0 行（改前留下 2 行）"""
    d = fresh_dir('pk_conflict')
    try:
        db, ex, tbl = make_env(d)
        r = expect_error(ex, 'INSERT INTO t VALUES (1,1),(2,2),(2,9)', 1062)
        left = ids(ex)
        print(f'    多值 INSERT 第 3 行主键冲突 -> {r["code"]} {r["message"]}；'
              f'表里剩 {len(left)} 行 {left}')
        assert left == [], (
            f'一条多值 INSERT 必须是原子的：第 3 行冲突时表里必须 0 行，'
            f'实际留下 {left}（改前的行为就是这里留下 [1, 2]）')
        # 失败语句不能污染后续写入
        assert ex.execute_sql('INSERT INTO t VALUES (5,5)')['affected'] == 1
        assert ids(ex) == [5]
        print(f'    失败语句之后继续写入正常: {ids(ex)}')
    finally:
        cleanup(d)


# ==================== ② CHECK / NOT NULL / 类型错 ====================

def test_multi_value_check_violation_rolls_back_whole_statement():
    d = fresh_dir('check')
    try:
        db, ex, tbl = make_env(
            d, 'CREATE TABLE t (id INT PRIMARY KEY, v INT CHECK (v > 0))')
        r = expect_error(ex, 'INSERT INTO t VALUES (1,1),(2,2),(3,-1)', 3819)
        left = ids(ex)
        print(f'    CHECK 第 3 行违规 -> {r["code"]} {r["message"]}；'
              f'表里剩 {len(left)} 行 {left}')
        assert left == [], f'CHECK 中途失败必须整体回滚，实际留下 {left}'
        assert ex.execute_sql('INSERT INTO t VALUES (7,7)')['affected'] == 1
        assert ids(ex) == [7]
    finally:
        cleanup(d)


def test_multi_value_not_null_violation_rolls_back_whole_statement():
    d = fresh_dir('notnull')
    try:
        db, ex, tbl = make_env(
            d, 'CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL)')
        r = expect_error(ex, 'INSERT INTO t VALUES (1,1),(2,NULL),(3,3)', 1048)
        left = ids(ex)
        print(f'    NOT NULL 第 2 行违规 -> {r["code"]} {r["message"]}；'
              f'表里剩 {len(left)} 行 {left}')
        assert left == [], f'NOT NULL 中途失败必须整体回滚，实际留下 {left}'
    finally:
        cleanup(d)


def test_multi_value_type_error_rolls_back_whole_statement():
    d = fresh_dir('type')
    try:
        db, ex, tbl = make_env(d)
        r = expect_error(ex, "INSERT INTO t VALUES (1,1),(2,'abc'),(3,3)")
        left = ids(ex)
        print(f'    类型转换第 2 行失败 -> {r["code"]} {r["message"]}；'
              f'表里剩 {len(left)} 行 {left}')
        assert 'abc' in r['message'] and 'INT' in r['message'], r
        assert left == [], f'类型错中途失败必须整体回滚，实际留下 {left}'
    finally:
        cleanup(d)


# ==================== ③ 显式事务不被提前提交/回滚 ====================

def test_failed_multi_value_inside_explicit_txn_keeps_txn_open():
    """显式事务里的多值 INSERT 失败：语句无效，事务仍可继续 / 可回滚"""
    d = fresh_dir('explicit_txn')
    try:
        db, ex, tbl = make_env(d)
        sess = {}
        assert ex.execute_sql('BEGIN', sess)['type'] == 'ok'
        assert ex.execute_sql('INSERT INTO t VALUES (1,1)', sess)['affected'] == 1
        # 同一事务里再来一条多值 INSERT，第 3 行冲突
        bad = ex.execute_sql('INSERT INTO t VALUES (2,2),(3,3),(3,9)', sess)
        assert bad['type'] == 'error' and bad['code'] == 1062, bad
        # 事务没有被提前提交：另一条语句在**事务内**仍看得到 1 行
        assert ex.execute_sql('SELECT id FROM t', sess)['rows'] == [(1,)]
        # 事务也没有被提前回滚：还能继续写
        assert ex.execute_sql('INSERT INTO t VALUES (4,4)', sess)['affected'] == 1
        assert ex.execute_sql('SELECT id FROM t', sess)['rows'] == [(1,), (4,)]
        # 外部（autocommit）读仍然看不到任何未提交行
        print(f'    事务内行数={len(ex.execute_sql("SELECT id FROM t", sess)["rows"])}，'
              f'事务外(autocommit)行数={len(ids(ex))}')
        # 整体 ROLLBACK 后一行不留
        assert ex.execute_sql('ROLLBACK', sess)['type'] == 'ok'
        left = ids(ex)
        print(f'    ROLLBACK 后表里 {len(left)} 行 {left}')
        assert left == []

        # COMMIT 路径：失败语句无效，成功语句提交
        assert ex.execute_sql('BEGIN', sess)['type'] == 'ok'
        ex.execute_sql('INSERT INTO t VALUES (10,10)', sess)
        ex.execute_sql('INSERT INTO t VALUES (11,11),(12,12),(11,99)', sess)
        assert ex.execute_sql('COMMIT', sess)['type'] == 'ok'
        assert ids(ex) == [10], f'COMMIT 后只应有事务里成功的那一行，实际 {ids(ex)}'
        print(f'    COMMIT 后表里 {ids(ex)}（失败语句的那几行没有落进来）')
    finally:
        cleanup(d)


# ==================== ④ os._exit(1) 硬杀 ====================

CRASH_CHILD = r'''
import os, sys
sys.path.insert(0, {root!r})
from lightdb.db.database import Database
from lightdb.sql.executor import Executor
from lightdb.db.table import Table

db = Database({d!r})
ex = Executor(db)

# 1) 3 条完整的多值语句（各 3 行）：都必须留下
for s in range(3):
    lo = s * 3 + 1
    vals = ','.join('(%d,%d)' % (i, i) for i in range(lo, lo + 3))
    r = ex.execute_sql('INSERT INTO t VALUES ' + vals)
    assert r.get('type') != 'error', r

# 2) 第 4 条多值语句：写完第 2 行后，把"半截脏页"强制刷盘再硬杀
#    （最坏情况：未提交的行已经落在数据文件里）
tbl = db.get_table('t')
orig = Table.insert
state = {{'n': 0}}


def spy(self, values, txn_id=0, read_view=None):
    n = orig(self, values, txn_id=txn_id, read_view=read_view)
    state['n'] += 1
    if state['n'] == 2:
        tbl.flush()            # 脏页 + BTree 根 + WAL 缓冲都落盘
        sys.stdout.write('rows_written=%d' % state['n'])
        sys.stdout.flush()
        os._exit(1)            # 硬杀：不 COMMIT、不 close、不 truncate WAL
    return n


Table.insert = spy
ex.execute_sql('INSERT INTO t VALUES (10,10),(11,11),(12,12)')
sys.stdout.write('NOT_REACHED')
sys.stdout.flush()
os._exit(2)
'''


CRASH_CHILD_EXPLICIT = r'''
import os, sys
sys.path.insert(0, {root!r})
os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] = str(10 ** 15)   # 别让 checkpoint 退休掉 WAL
os.environ['LIGHTDB_WAL_CHECKPOINT_RECORDS'] = '0'
from lightdb.db.database import Database
from lightdb.sql.executor import Executor

db = Database({d!r})
ex = Executor(db)
sess = {{}}
assert ex.execute_sql('BEGIN', sess)['type'] == 'ok'
# 这条语句的第 3 行冲突 -> 语句级回滚（事务保持打开）
bad = ex.execute_sql('INSERT INTO t VALUES (1,1),(2,2),(2,9)', sess)
assert bad['type'] == 'error', bad
# 事务继续用，然后整体提交
assert ex.execute_sql('INSERT INTO t VALUES (7,7)', sess)['affected'] == 1
assert ex.execute_sql('COMMIT', sess)['type'] == 'ok'
wal = os.path.getsize(os.path.join({d!r}, 't.wal'))
sys.stdout.write('committed wal=%d' % wal)
sys.stdout.flush()
os._exit(1)          # 硬杀：不 close、不 truncate WAL
'''


def test_crash_mid_multi_value_insert_leaves_no_half_rows():
    from lightdb.db.database import Database

    d = fresh_dir('crash')
    try:
        db = track(Database(d))
        db.create_table('t', COLS)
        db.shutdown()

        proc = subprocess.run(
            [sys.executable, '-c', CRASH_CHILD.format(root=BASE, d=d)],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace')
        print(f'    子进程退出码 {proc.returncode}，stdout={proc.stdout.strip()!r}')
        assert proc.returncode == 1, proc.stderr
        assert proc.stdout.strip() == 'rows_written=2'

        db2 = track(Database(d))
        from lightdb.sql.executor import Executor
        ex2 = Executor(db2)
        got = [r[0] for r in ex2.execute_sql('SELECT id FROM t ORDER BY id')['rows']]
        print(f'    崩溃并重开后表里剩 {len(got)} 行: {got}')
        assert got == list(range(1, 10)), (
            f'已提交的 3 条完整语句必须都在，被硬杀打断的那条一行都不能留；'
            f'实际 {got}')
    finally:
        cleanup(d)


def test_crash_after_commit_does_not_resurrect_failed_statement_rows():
    """显式事务：语句级回滚掉的行，崩溃恢复的 redo 也不能把它们复活

    这是"语句级回滚 + 补偿 WAL"的正确性证明：失败语句写下的 INSERT 记录
    仍在用户的 COMMIT 之前，redo 会照 LSN 重放它们 —— 必须让它们重放后
    也不可见（补偿 DELETE 记录把它们墓碑化）。
    """
    from lightdb.db.database import Database

    d = fresh_dir('explicit_crash')
    try:
        db = track(Database(d))
        db.create_table('t', COLS)
        db.shutdown()

        proc = subprocess.run(
            [sys.executable, '-c', CRASH_CHILD_EXPLICIT.format(root=BASE, d=d)],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace')
        print(f'    子进程退出码 {proc.returncode}，stdout={proc.stdout.strip()!r}')
        assert proc.returncode == 1, proc.stderr
        assert 'committed wal=' in proc.stdout, proc.stdout

        db2 = track(Database(d))
        from lightdb.sql.executor import Executor
        ex2 = Executor(db2)
        got = [r[0] for r in ex2.execute_sql('SELECT id FROM t ORDER BY id')['rows']]
        print(f'    崩溃恢复（redo）后可见行 = {got}')
        assert got == [7], (
            f'语句级回滚掉的行在崩溃恢复后不得复活，实际可见 {got}')
        # 再插一次同样的主键必须成功（墓碑不占位、也没有"索引残留"）
        assert ex2.execute_sql('INSERT INTO t VALUES (1,1)')['affected'] == 1
        assert [r[0] for r in
                ex2.execute_sql('SELECT id FROM t ORDER BY id')['rows']] == [1, 7]
    finally:
        cleanup(d)


# ==================== ⑤ 省略主键：连续且不冲突 ====================

def test_multi_value_omitted_pk_is_sequential_and_rollback_safe():
    d = fresh_dir('next_pk')
    try:
        db, ex, tbl = make_env(d)
        r = ex.execute_sql('INSERT INTO t (v) VALUES (10),(20),(30)')
        assert r['affected'] == 3, r
        got = ex.execute_sql('SELECT id, v FROM t ORDER BY id')['rows']
        print(f'    同一语句内省略主键 -> {got}')
        assert [x[0] for x in got] == [1, 2, 3], '同一条语句内自取主键必须连续'
        assert [x[1] for x in got] == [10, 20, 30]

        # 失败语句话：第 1 行会占到 id=4，第 2 行 NOT NULL 违规 → 整体回滚
        d2 = fresh_dir('next_pk_rollback')
        db2, ex2, tbl2 = make_env(d2, 'CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL)')
        ex2.execute_sql('INSERT INTO t (v) VALUES (10),(20),(30)')
        bad = ex2.execute_sql('INSERT INTO t (v) VALUES (40),(NULL)')
        assert bad['type'] == 'error' and bad['code'] == 1048, bad
        assert [x[0] for x in ex2.execute_sql('SELECT id FROM t ORDER BY id')['rows']] \
            == [1, 2, 3]
        ex2.execute_sql('INSERT INTO t (v) VALUES (60)')
        after = ex2.execute_sql('SELECT id, v FROM t ORDER BY id')['rows']
        print(f'    回滚后继续插入 -> {after}（自取值没有被"消耗"掉）')
        assert after == [(1, 10), (2, 20), (3, 30), (4, 60)], after
    finally:
        cleanup(d)
        cleanup(os.path.join(BASE, 'data_insatomic_next_pk_rollback'))


# ==================== ⑥ 提交前对其它连接不可见 ====================

def test_statement_rows_invisible_to_other_snapshot_until_commit():
    from lightdb.db.table import Table

    d = fresh_dir('mvcc')
    try:
        db, ex, tbl = make_env(d)
        other = db.begin()                       # "另一个连接"在语句前取的快照
        seen = []
        orig = Table.insert

        def spy(self, values, txn_id=0, read_view=None):
            n = orig(self, values, txn_id=txn_id, read_view=read_view)
            seen.append(len(tbl.select(['id'],
                                       read_view=other['read_view'])[1]))
            return n

        Table.insert = spy
        try:
            r = ex.execute_sql('INSERT INTO t VALUES (1,1),(2,2),(3,3)')
        finally:
            Table.insert = orig
        assert r['affected'] == 3, r
        print(f'    语句执行中，其它连接快照每次看到的行数 = {seen}（应为 [0,0,0]）')
        assert seen == [0, 0, 0], (
            f'语句提交前其它连接不得看到任何一行，实际看到 {seen}')
        assert len(tbl.select(['id'], read_view=other['read_view'])[1]) == 0
        fresh = db.begin()
        rows = sorted(x[0] for x in
                      tbl.select(['id'], read_view=fresh['read_view'])[1])
        print(f'    提交后新快照看到 {rows}')
        assert rows == [1, 2, 3]
        db.abort(other)
        db.abort(fresh)
    finally:
        cleanup(d)


# ==================== ⑦ WAL 仍然有界（与 checkpoint 协同）====================

def test_multi_value_insert_still_triggers_wal_checkpoint():
    from lightdb.db.table import Table

    d = fresh_dir('wal_bound')
    threshold = 8 * 1024
    old_b, old_r = Table.wal_checkpoint_bytes, Table.wal_checkpoint_records
    Table.wal_checkpoint_bytes, Table.wal_checkpoint_records = threshold, 0
    try:
        db, ex, tbl = make_env(d)
        n, batch = 4000, 500
        for s in range((n + batch - 1) // batch):
            lo = s * batch + 1
            hi = min(lo + batch - 1, n)
            vals = ','.join(f'({i},{i % 1000})' for i in range(lo, hi + 1))
            r = ex.execute_sql('INSERT INTO t VALUES ' + vals)
            assert r.get('type') != 'error', r
        p = os.path.join(d, 't.wal')
        size = os.path.getsize(p) if os.path.exists(p) else 0
        print(f'    {n} 行（{batch} 行/语句）后 WAL={size} B（阈值 {threshold} B，'
              f'不轮转约 {n * 190} B）')
        assert size <= threshold + 4096, (
            f'批量写入路径的 WAL 必须仍然有界，实际 {size} B')
        assert len(ids(ex)) == n
    finally:
        Table.wal_checkpoint_bytes, Table.wal_checkpoint_records = old_b, old_r
        cleanup(d)


# ==================== ⑧ 审计批量 flush ====================

def _audit_lines(d):
    p = os.path.join(d, 'audit.log')
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8') as f:
        return [json.loads(x) for x in f if x.strip()]


def test_audit_batches_flush_and_never_silently_drops(monkeypatch):
    monkeypatch.setenv('LIGHTDB_AUDIT_LEVEL', 'DML')
    d = fresh_dir('audit')
    try:
        db, ex, tbl = make_env(d)
        sink = ex.audit
        assert sink is not None
        # make_env 里的 CREATE TABLE 也是一条被审计的 DDL
        base = sink.pending
        print(f'    CREATE TABLE 后 pending={base}')
        for i in range(5):
            assert ex.execute_sql(f'INSERT INTO t VALUES ({i},{i})')['type'] == 'ok'
        print(f'    再 5 条 DML 后: 内存待落盘={sink.pending}，'
              f'已落盘={sink.flushed_records}，'
              f'磁盘行数={len(_audit_lines(d))}')
        assert sink.pending == base + 5, '少于一批(32)且在时间窗内，应当还在内存里'
        assert len(_audit_lines(d)) == 0

        # 失败语句立即落盘（审计里最要紧的记录不参与延迟）
        bad = ex.execute_sql('INSERT INTO no_such_table VALUES (1)')
        assert bad['type'] == 'error'
        lines = _audit_lines(d)
        print(f'    失败语句后立即落盘 {len(lines)} 行，'
              f'最后一行 success={lines[-1]["success"]} sql={lines[-1]["sql"]!r}')
        assert len(lines) == base + 6 and lines[-1]['success'] is False
        assert 'no_such_table' in lines[-1]['sql']
        assert sink.pending == 0

        # 显式 flush()：把待落盘批一次写出去
        for i in range(100, 105):
            ex.execute_sql(f'INSERT INTO t VALUES ({i},{i})')
        assert sink.pending == 5
        sink.flush()
        assert len(_audit_lines(d)) == base + 11 and sink.pending == 0
        # 显式 close() 幂等
        ex.execute_sql('INSERT INTO t VALUES (200,200)')
        sink.close()
        assert len(_audit_lines(d)) == base + 12
        sink.close()
        assert len(_audit_lines(d)) == base + 12
        print(f'    flush()/close() 后磁盘行数={len(_audit_lines(d))}，'
              f'pending={sink.pending}')

        # 落盘失败（路径不可写）：记录必须**保留**并告警，不被当已落盘丢掉
        good_path = sink.path
        sink.path = os.path.join(d, 'no_such_dir', 'audit.log')
        r = ex.execute_sql('INSERT INTO t VALUES (201,201)')
        assert r['type'] == 'ok', '审计落盘失败绝不能影响语句结果'
        print(f'    落盘失败后: pending={sink.pending}（记录被保留待重试）')
        assert sink.pending == 1, sink.pending
        sink.path = good_path
        sink.flush()
        assert len(_audit_lines(d)) == base + 13
        print(f'    磁盘恢复后重试成功: 磁盘行数={len(_audit_lines(d))}')

        # 审计器自身报错也不影响语句结果
        monkeypatch.setattr(sink, 'record',
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError('audit boom')))
        r = ex.execute_sql('INSERT INTO t VALUES (202,202)')
        assert r['type'] == 'ok', '审计故障绝不能影响语句结果'
        print('    审计器自身抛错时语句仍然返回 ok')

        # 停机路径：攒着的待落盘批必须由 Database.shutdown() 写出去
        monkeypatch.undo()
        for i in range(300, 304):
            ex.execute_sql(f'INSERT INTO t VALUES ({i},{i})')
        pending_before = sink.pending
        db.shutdown()
        lines = _audit_lines(d)
        print(f'    shutdown() 前 pending={pending_before}，'
              f'之后磁盘行数={len(lines)}，pending={sink.pending}')
        assert pending_before == 4
        assert len(lines) == base + 17 and sink.pending == 0
    finally:
        cleanup(d)
