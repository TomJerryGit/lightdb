"""任务 A：快照完整性——备份失败必须失败（不许静默产出空 schema）。

背景（docs/review/03_integration_and_perf.md:147 实测）：
`core/snapshot.py` 的 `create_snapshot` 在 `SHOW CREATE TABLE` 报 1064 时被
`except: pass` 吞掉，快照文件里**一个 CREATE TABLE 都没有**却仍返回"成功"。

本文件对着真实引擎（lightdb MySQL 协议服务端）跑，断言三件事：
1. 正常路径：两张表 → ok=True，文件里**每张表都有 CREATE TABLE**；
2. 强制失败：`SHOW CREATE TABLE` 失败（executor 桩抛错 / 不存在的表）→
   ok=False，且失败清单里能读到表名 / 语句 / 错误原文；
3. **反向断言**：绝不允许出现"0 个 CREATE TABLE 但 ok=True"。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_snapshot_integrity.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_snap_integrity')
PORT = 8931
REAL_TABLES = ('users', 'orders')


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    return srv, db, ex


def connect(port, name='snap_test'):
    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.config import ConnectionInfo

    info = ConnectionInfo(name=name, host='127.0.0.1', port=port, user='root',
                          password='')
    conn = DBConnection(info)
    conn.connect()
    return track(conn)


def make_two_tables(conn):
    assert conn.execute("CREATE TABLE users (id INT PRIMARY KEY, "
                        "name VARCHAR(50))")[1] is None
    assert conn.execute("CREATE TABLE orders (id INT PRIMARY KEY, "
                        "amount INT)")[1] is None
    assert conn.execute("INSERT INTO users VALUES (1, 'alice')")[1] is None
    assert conn.execute("INSERT INTO users VALUES (2, 'bob')")[1] is None
    assert conn.execute("INSERT INTO orders VALUES (10, 100)")[1] is None


def count_create_table(path, tname):
    """独立统计实现（不复用产品代码），避免"用被测逻辑验证被测逻辑"。"""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    low = text.lower()
    return low.count(f"create table `{tname.lower()}`") + \
        low.count(f"create table {tname.lower()}")


class BoomCreateTableConn:
    """executor 桩：get_tables 正常，SHOW CREATE TABLE 抛错（模拟 1064/1066）。"""

    def __init__(self, inner, boom_tables=REAL_TABLES):
        self._inner = inner
        self.config = inner.config
        self.boom_tables = set(boom_tables)
        self.calls = []

    def get_tables(self, db=None):
        return self._inner.get_tables()

    def get_columns(self, table):
        raise RuntimeError(f"stub: SHOW COLUMNS '{table}' 也不可用")

    def query(self, sql, params=None):
        self.calls.append(sql)
        for t in self.boom_tables:
            if f'`{t}`' in sql:
                # 与 pymysql 异常同形：args=(errno, message) + sqlstate
                class _Err(Exception):
                    errno = 1064
                    sqlstate = '42000'

                raise _Err(1064, f"You have an error in your SQL syntax "
                                 f"near 'SHOW CREATE TABLE {t}'")
        return self._inner.query(sql, params)

    def execute(self, sql, params=None):
        return self._inner.execute(sql, params)


class GhostTableConn:
    """get_tables 多报一张不存在的表（模拟"表刚被删"），其余走真实连接。"""

    def __init__(self, inner, ghost='ghost_missing_table'):
        self._inner = inner
        self.config = inner.config
        self.ghost = ghost

    def get_tables(self, db=None):
        return list(self._inner.get_tables()) + [self.ghost]

    def get_columns(self, table):
        return self._inner.get_columns(table)

    def query(self, sql, params=None):
        return self._inner.query(sql, params)

    def execute(self, sql, params=None):
        return self._inner.execute(sql, params)


# ==================== 1. 正常路径 ====================

def test_snapshot_normal_path_all_tables_have_create_table(tmp_path):
    """正常：两张表 → ok=True，且文件里每张表都有 CREATE TABLE。"""
    print("\n【测试1】快照正常路径")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    mgr = SnapshotManager(dir=str(tmp_path))
    result = mgr.create_snapshot(conn, 'snap_ok')

    print(f"  → ok={result.ok} error={result.error!r}")
    print(f"  → 失败清单={[str(f) for f in result.failures]}")
    print(f"  → CREATE TABLE 计数={result.create_table_counts}")
    print(f"  → 文件={result.file}")

    assert result.ok is True, f"正常路径应成功，实际失败: {result.summary()}"
    assert result.failures == [], f"正常路径不应有失败: {result.failures}"
    assert result.incomplete_tables == []
    assert result.snapshot is not None
    assert os.path.exists(result.file)

    for t in REAL_TABLES:
        n = count_create_table(result.file, t)
        print(f"  → 文件中 {t} 的 CREATE TABLE 条数: {n}")
        assert n >= 1, f"{t} 在快照文件里没有 CREATE TABLE（备份静默失效）"

    with open(result.file, 'r', encoding='utf-8') as f:
        text = f.read()
    assert 'INSERT INTO `users`' in text
    assert 'INSERT INTO `orders`' in text

    # 索引进了一条成功记录，且可以从管理器里读回
    assert mgr.get('snap_ok') is not None
    print("  ✅ 正常路径：ok=True 且每张表都有 CREATE TABLE")


def test_snapshot_roundtrip_restore_reports_success(tmp_path):
    """正常路径的快照能恢复，且 restore 返回 ok=True。"""
    print("\n【测试2】快照恢复（正常）")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    mgr = SnapshotManager(dir=str(tmp_path))
    result = mgr.create_snapshot(conn, 'snap_rt')
    assert result.ok is True, result.summary()

    assert conn.execute("DROP TABLE orders")[1] is None
    restored = mgr.restore_snapshot(conn, result.snapshot, drop_existing=True)
    print(f"  → restore: ok={restored['ok']} executed={restored['executed']} "
          f"failed={restored['failed']}")
    assert restored['ok'] is True, restored
    assert restored['failed'] == 0
    _c, rows = conn.query("SELECT * FROM orders")
    assert len(rows) == 1
    print("  ✅ 恢复成功且 ok=True")


# ==================== 2. 强制失败路径 ====================

def test_snapshot_fails_when_show_create_table_raises(tmp_path):
    """强制失败：SHOW CREATE TABLE 抛错（1064 桩）→ ok=False + 可读失败清单。"""
    print("\n【测试3】SHOW CREATE TABLE 报 1064")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    stub = BoomCreateTableConn(conn)
    mgr = SnapshotManager(dir=str(tmp_path))
    result = mgr.create_snapshot(stub, 'snap_1064')

    print(f"  → ok={result.ok} error={result.error!r}")
    for f in result.failures:
        print(f"  → 失败: table={f.table!r} op={f.operation!r} "
              f"stmt={f.statement!r} errno={f.errno} sqlstate={f.sqlstate!r} "
              f"error={f.error!r}")

    assert result.ok is False, "底层语句失败却报了成功（静默失效未修复）"
    assert result.failures, "必须附带失败清单"
    assert {f.table for f in result.failures} >= set(REAL_TABLES)

    f0 = result.failures[0]
    assert f0.operation == 'schema'
    assert 'SHOW CREATE TABLE' in f0.statement
    assert f0.errno == 1064
    assert f0.sqlstate == '42000'
    assert 'SQL syntax' in f0.error
    assert '1064' in f0.error_text

    # 失败时绝不落盘、绝不进索引
    assert result.snapshot is None
    assert result.file == ''
    assert mgr.get('snap_1064') is None
    assert not list(tmp_path.glob('snap_1064*.sql')), "失败却留下了快照文件"
    assert result.summary()
    print("  ✅ 失败清单含 表名/语句/errno/sqlstate/错误原文，且未落盘")


def test_snapshot_fails_for_missing_table_schema(tmp_path):
    """强制失败：表清单里有不存在的表 → 该表拿不到 CREATE TABLE → ok=False。"""
    print("\n【测试4】不存在的表取结构（真实 1146）")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    stub = GhostTableConn(conn)
    mgr = SnapshotManager(dir=str(tmp_path))
    result = mgr.create_snapshot(stub, 'snap_1146')

    print(f"  → ok={result.ok} error={result.error!r}")
    print(f"  → incomplete_tables={result.incomplete_tables}")
    for f in result.failures:
        print(f"  → 失败: {f}")

    assert result.ok is False, "拿不到 CREATE TABLE 却报成功"
    assert 'ghost_missing_table' in result.incomplete_tables
    assert any(f.table == 'ghost_missing_table' for f in result.failures)
    ghost = [f for f in result.failures if f.table == 'ghost_missing_table']
    assert any(f.errno == 1146 for f in ghost), \
        f"应记录 1146 原始错误码，实际: {[(f.errno, f.error) for f in ghost]}"
    assert mgr.get('snap_1146') is None
    assert not list(tmp_path.glob('snap_1146*.sql'))
    print("  ✅ 不完整的表被标记，ok=False，无半成品文件")


def test_snapshot_fails_when_table_list_unavailable(tmp_path):
    """表清单本身取不到 → 绝不产出"0 表但成功"的快照。"""
    print("\n【测试5】SHOW TABLES 失败")

    class NoTablesConn:
        class _Cfg:
            name = 'dead'
            database = ''

        config = _Cfg()

        def get_tables(self, db=None):
            raise RuntimeError("连接已断开")

    from lightdb_studio.core.snapshot import SnapshotManager

    mgr = SnapshotManager(dir=str(tmp_path))
    result = mgr.create_snapshot(NoTablesConn(), 'snap_notables')
    print(f"  → ok={result.ok} error={result.error!r} "
          f"failures={[str(f) for f in result.failures]}")
    assert result.ok is False
    assert result.failures[0].operation == 'list_tables'
    assert '连接已断开' in result.failures[0].error_text
    assert result.snapshot is None
    assert mgr.get('snap_notables') is None
    print("  ✅ 表清单失败 → ok=False")


def test_snapshot_strict_false_raises_instead_of_faking_success(tmp_path):
    """旧调用方式（strict=False）也必须失败 —— 抛错，而不是返回空快照对象。"""
    print("\n【测试6】strict=False 的失败行为")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    mgr = SnapshotManager(dir=str(tmp_path))
    stub = BoomCreateTableConn(conn)
    try:
        snap = mgr.create_snapshot(stub, 'snap_legacy', strict=False)
        raise AssertionError(f"strict=False 竟返回了快照对象: {snap}")
    except RuntimeError as e:
        print(f"  → 如期抛错: {str(e).splitlines()[0]}")
        assert '未落盘' in str(e)
    assert mgr.get('snap_legacy') is None
    assert not list(tmp_path.glob('snap_legacy*.sql'))
    print("  ✅ strict=False 抛 RuntimeError，不留文件")


def test_index_write_failure_is_not_silent(tmp_path):
    """索引写不进去 = 快照在列表里看不见（又一种静默失效）→ 必须 ok=False。

    做法：把 SnapshotManager 的索引路径指到一个"目录"上，open(..., 'w') 必失败。
    """
    print("\n【测试6b】快照索引写入失败")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    mgr = SnapshotManager(dir=str(tmp_path))
    bad_index = tmp_path / 'index_is_a_dir'
    bad_index.mkdir()
    mgr._index_path = lambda: bad_index       # open(dir, 'w') → 必然抛错

    result = mgr.create_snapshot(conn, 'snap_noidx')
    print(f"  → ok={result.ok} error={result.error!r}")
    for f in result.failures:
        print(f"  → 失败: {f}")

    assert result.ok is False
    assert any(f.operation == 'index' for f in result.failures)
    assert mgr.get('snap_noidx') is None, "索引失败却仍留在内存列表里（假成功）"
    assert not (tmp_path / 'snap_noidx.sql').exists(), \
        "索引失败却留下了用户看不见的快照文件"
    print("  ✅ 索引失败 → ok=False，文件与内存列表都回滚")

    # strict=False 也必须抛错，而不是返回一个查不到的 Snapshot
    try:
        snap = mgr.create_snapshot(conn, 'snap_noidx2', strict=False)
        raise AssertionError(
            f"strict=False 竟在索引失败时返回了快照对象: {snap}")
    except AssertionError:
        raise
    except Exception as e:
        print(f"  → strict=False 如期抛错 {type(e).__name__}: {str(e)[:80]}")
    assert mgr.get('snap_noidx2') is None
    assert not (tmp_path / 'snap_noidx2.sql').exists()
    print("  ✅ strict=False 索引失败也抛错（不返回查不到的快照对象）")


# ==================== 3. 反向断言 ====================

def test_no_zero_create_table_snapshot_ever_reports_ok(tmp_path):
    """**反向断言**：绝不允许「0 个 CREATE TABLE 但 ok:True」。

    对同一批表分别用 正常连接 / 报错桩 / 幽灵表桩 / 断连桩 跑一遍：
    只要文件里 CREATE TABLE 计数为 0（或少于表数），就**必须** ok=False；
    只要 ok=True，就**必须**每张表都有 CREATE TABLE。
    """
    print("\n【测试7】反向断言：0 个 CREATE TABLE 不允许 ok=True")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    make_two_tables(conn)

    from lightdb_studio.core.snapshot import SnapshotManager

    seen = []
    cases = [
        ('normal', conn),
        ('boom_1064', BoomCreateTableConn(conn)),
        ('ghost_1146', GhostTableConn(conn)),
    ]
    for label, c in cases:
        mgr = SnapshotManager(dir=str(tmp_path / label))
        r = mgr.create_snapshot(c, f'snap_{label}')
        zero_or_partial = (not r.file) or any(
            n <= 0 for n in r.create_table_counts.values()) or (
                len(r.create_table_counts) < len(r.tables))
        print(f"  → {label}: ok={r.ok} counts={r.create_table_counts} "
              f"tables={r.tables} zero_or_partial={zero_or_partial}")
        seen.append((label, r.ok, zero_or_partial))
        if r.ok:
            assert r.file and os.path.exists(r.file)
            for t in r.tables:
                n = count_create_table(r.file, t)
                assert n > 0, f"{label}: ok=True 但 {t} 没有 CREATE TABLE"
            assert not zero_or_partial, "ok=True 却存在缺 CREATE TABLE 的表"
        else:
            assert r.file == '' or not os.path.exists(r.file)

    # 失败用例确实产出过"0 个 CREATE TABLE"的意图（即真的覆盖了该场景）
    assert any(not ok for _l, ok, _z in seen), "反向断言没有覆盖到失败场景"

    # 直接构造"看起来成功但 0 个 CREATE TABLE"的文件，确认自检能识破
    mgr = SnapshotManager(dir=str(tmp_path / 'selftest'))
    bad = tmp_path / 'selftest' / 'bad.sql'
    bad.write_text("-- 空快照，一个 CREATE TABLE 都没有\n", encoding='utf-8')
    missing = mgr.validate_snapshot_file(bad, list(REAL_TABLES))
    print(f"  → 自检识别出缺失: {missing}")
    assert missing == list(REAL_TABLES), missing
    print("  ✅ 反向断言成立：0 个 CREATE TABLE 无法通过自检")


if __name__ == '__main__':
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        test_snapshot_normal_path_all_tables_have_create_table(base / 'a')
        test_snapshot_roundtrip_restore_reports_success(base / 'b')
        test_snapshot_fails_when_show_create_table_raises(base / 'c')
        test_snapshot_fails_for_missing_table_schema(base / 'd')
        test_snapshot_fails_when_table_list_unavailable(base / 'e')
        test_snapshot_strict_false_raises_instead_of_faking_success(base / 'f')
        test_index_write_failure_is_not_silent(base / 'f2')
        test_no_zero_create_table_snapshot_ever_reports_ok(base / 'g')
    print('\nALL OK')
