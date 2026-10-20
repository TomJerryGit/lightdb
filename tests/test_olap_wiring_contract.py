# -*- coding: utf-8 -*-
"""OLAP 接线契约（4A.2 新契约；取代旧的「未接线/零 import」契约）

旧契约（`tests/test_olap_not_wired.py` 4A.1 版本）把「SQL 路径零 import
OLAP」当作事实锁住。4A.2 起**真的接线**了，契约随之变化：

1. **已接线**：`olap.status.WIRED_TO_SQL_PATH is True`，且
   `Executor._cmd_select` 里确实有 dispatch 钩子（源码级检查）；
2. **默认关闭**：`DEFAULT_ENABLED is False`；不显式开启时：
   * 执行器 `olap_dispatch=False`，`dispatch_stats()['attempted'] == 0`；
   * SQL 查询不 import OLAP/列存引擎模块（延迟 import 锁）；
   * 结果与行存逐字节一致（默认行为不变）。
3. **开关生效**：显式打开后白名单查询真的走向量化（dispatch 计数 >0），
   结果仍与行存逐行一致。
4. **只走白名单 / 无静默改道**：OR、JOIN、子查询、HAVING、窗口、
   DISTINCT、表达式投影、IN 列表等一律 `dispatched == 0` 且回退行存。
5. **实验性历史入口**：`HybridTable.olap_scan` 仍显式报错（报文指向
   `olap_dispatch`），`olap_aggregate` 保留留痕语义。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _context(tmp_path):
    """新库 + 执行器 + 小表（默认分派阈值 1000 行，足够触发/阻断各一次）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    ex.execute_sql('CREATE TABLE c (id INT PRIMARY KEY, g VARCHAR(10), v INT)',
                   session)
    vals = ','.join(
        "(%d,'%s',%d)" % (i, 'g%d' % (i % 3), i * 2 - 10) for i in range(20))
    ex.execute_sql(f'INSERT INTO c VALUES {vals}', session)
    return db, ex, session


# ==================== 1. status 常量与事实一致 ====================

def test_status_constants_match_reality():
    from lightdb.olap import status
    assert status.WIRED_TO_SQL_PATH is True, '已接线：状态必须如实为 True'
    assert status.DEFAULT_ENABLED is False, '接线默认关闭'
    # 4A.5：已有可辩护的基准门禁，状态必须与 benchmarks/gate.py 的事实一致
    assert status.COUNTS_FOR_PERFORMANCE_CLAIMS is True, '4A.5 已加基准门禁'
    from benchmarks import gate as bench_gate
    assert 'olap_parallel_groupby_speedup' in bench_gate.TARGETS
    assert 'olap_dispatch_crosscheck_mismatches' in bench_gate.TARGETS
    assert bench_gate.target_of('olap_parallel_groupby_100k') == \
        'olap_parallel_groupby_speedup'
    assert bench_gate.target_of('olap_dispatch_crosscheck_10000') == \
        'olap_dispatch_crosscheck_mismatches'
    assert '已接线' in status.STATUS_LABEL
    assert '默认关闭' in status.STATUS_LABEL
    assert status.PARALLEL_AUTO_WORKERS >= 1
    dispatch_path = os.path.join(ROOT, status.DISPATCH_MODULE.replace('/', os.sep))
    assert os.path.exists(dispatch_path), dispatch_path


def test_executor_has_dispatch_hook_and_default_is_off(tmp_path):
    """源码级：SELECT 分支有 dispatch 钩子；运行期：默认不派发"""
    import inspect
    from lightdb.olap import sql_dispatch
    from lightdb.sql.executor import Executor

    src = inspect.getsource(Executor._cmd_select)
    assert 'sql_dispatch' in src and 'try_vectorized_select' in src, (
        'Executor._cmd_select 缺少 OLAP dispatch 钩子（状态与事实不符）')

    db, ex, session = _context(tmp_path)
    try:
        assert ex.olap_dispatch is False
        assert ex.olap_dispatch_min_rows == 1000
        sql_dispatch.reset_dispatch_stats()
        for q in ('SELECT COUNT(*), SUM(v) FROM c',
                  'SELECT g, COUNT(*) FROM c GROUP BY g',
                  'SELECT AVG(v) FROM c WHERE v > 0'):
            r = ex.execute_sql(q, session)
            assert r.get('type') == 'resultset', (q, r)
        assert sql_dispatch.dispatch_stats()['attempted'] == 0, (
            '默认关闭时不应尝试分派')
    finally:
        db.shutdown()


def test_default_off_does_not_import_olap_engines(tmp_path):
    """默认关闭时 SQL 查询不加载 OLAP/列存引擎（延迟 import）"""

    class _Block:
        def __init__(self):
            self.blocked = []

        def find_spec(self, fullname, path=None, target=None):
            if (fullname.startswith('lightdb.olap')
                    or fullname.startswith('lightdb.columnar')):
                self.blocked.append(fullname)
                raise ImportError(f'OLAP 引擎被阻断: {fullname}')
            return None

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    blocker = _Block()
    with pytest.MonkeyPatch.context() as mp:
        for name in list(sys.modules):
            if (name.startswith('lightdb.olap')
                    or name.startswith('lightdb.columnar')) \
                    and name != 'lightdb.olap.status':
                mp.delitem(sys.modules, name, raising=False)
        sys.meta_path.insert(0, blocker)
        try:
            db = Database(str(tmp_path / 'db2'))
            ex = Executor(db)
            session = {}
            ex.execute_sql('CREATE TABLE d (id INT PRIMARY KEY, v INT)', session)
            ex.execute_sql('INSERT INTO d VALUES (1, 5)', session)
            for q in ('SELECT SUM(v) FROM d', 'SELECT COUNT(*) FROM d'):
                assert ex.execute_sql(q, session).get('type') == 'resultset'
            assert not blocker.blocked, blocker.blocked
            db.shutdown()
        finally:
            sys.meta_path.remove(blocker)


# ==================== 2. 开关生效 + 结果一致 ====================

def test_switch_on_dispatches_and_preserves_results(tmp_path):
    from lightdb.olap import sql_dispatch
    db, ex, session = _context(tmp_path)
    try:
        queries = [
            'SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM c',
            'SELECT g, COUNT(*), SUM(v) FROM c GROUP BY g',
            'SELECT g, SUM(v) FROM c GROUP BY g ORDER BY SUM(v) DESC LIMIT 2',
            'SELECT COUNT(*) AS n, SUM(v) AS s FROM c WHERE v IS NOT NULL',
        ]
        ex.olap_dispatch = False
        off = [ex.execute_sql(q, session) for q in queries]
        ex.olap_dispatch = True
        ex.olap_dispatch_min_rows = 0
        sql_dispatch.reset_dispatch_stats()
        on = [ex.execute_sql(q, session) for q in queries]
        st = sql_dispatch.dispatch_stats()
        for q, a, b in zip(queries, off, on):
            assert list(a.get('columns') or []) == list(b.get('columns') or []), q
            assert [list(r) for r in a.get('rows') or []] == \
                [list(r) for r in b.get('rows') or []], (q, a, b)
        assert st['dispatched'] == len(queries), st
        assert st['fallback'] == 0 and st['fallback_error'] == 0, st
    finally:
        db.shutdown()


# ==================== 3. 只走白名单 / 无静默改道 ====================

UNSUPPORTED = [
    'SELECT COUNT(*) FROM c JOIN c x ON c.id = x.id',
    "SELECT g, COUNT(*) FROM c GROUP BY g HAVING COUNT(*) > 1",
    'SELECT COUNT(*) FROM c WHERE v > 0 OR id = 3',
    'SELECT COUNT(*) FROM c WHERE id IN (SELECT id FROM c)',
    'SELECT g, COUNT(*), ROW_NUMBER() OVER (ORDER BY g) FROM c GROUP BY g',
    'SELECT SUM(v * 3) FROM c',
    'SELECT COUNT(DISTINCT v) FROM c',
    'SELECT c.g, COUNT(*) FROM c GROUP BY c.g',
    'SELECT * FROM c',
    'SELECT SUM(v) FROM c WHERE id BETWEEN 1 AND 3',
]


@pytest.mark.parametrize('sql', UNSUPPORTED,
                         ids=[f'unsup{i}' for i in range(len(UNSUPPORTED))])
def test_whitelist_only_no_silent_reroute(tmp_path, sql):
    from lightdb.olap import sql_dispatch
    db, ex, session = _context(tmp_path)
    try:
        ex.olap_dispatch = False
        off = ex.execute_sql(sql, session)
        ex.olap_dispatch = True
        ex.olap_dispatch_min_rows = 0
        sql_dispatch.reset_dispatch_stats()
        on = ex.execute_sql(sql, session)
        st = sql_dispatch.dispatch_stats()
        assert on.get('type') == off.get('type'), (sql, on, off)
        if off.get('type') == 'resultset':
            assert list(on.get('columns') or []) == list(off.get('columns') or [])
            assert [list(r) for r in on.get('rows') or []] == \
                [list(r) for r in off.get('rows') or []]
        assert st['dispatched'] == 0, (sql, st)
        assert st['fallback'] >= 1, (sql, st)
    finally:
        db.shutdown()


# ==================== 4. 历史实验入口 / 状态检查点 ====================

def test_hybrid_table_entrypoints_point_to_dispatch(tmp_path):
    from lightdb.db.hybrid_table import HybridTable

    cols = [{'name': 'id', 'type': 'INT'},
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'}]
    # HybridTable 的 BufferPool 不做 mkdir（既有行为），测试先建目录
    os.makedirs(str(tmp_path / 'hyb'), exist_ok=True)
    table = HybridTable('h', cols, str(tmp_path / 'hyb'))
    try:
        with pytest.raises(NotImplementedError) as ei:
            table.olap_scan(['city'])
        msg = str(ei.value)
        assert 'olap_dispatch' in msg, msg
        assert '默认关闭' in msg, msg

        for i in range(3):
            table.insert([i, 'c%d' % i, i * 10])
        table.sync_now()
        rows = table.olap_aggregate({
            'columns': ['city'],
            'group_by': ['city'],
            'aggregations': [
                {'func': 'SUM', 'column': 'amount', 'alias': 'total'}],
        })
        assert len(rows) == 3
    finally:
        table.close()


def test_status_checkpoints_semantics():
    from lightdb.olap import status
    # 已接线：ensure_not_requested_via_sql 变为空操作（不是报错）
    assert status.ensure_not_requested_via_sql('X.y') is None
    with pytest.raises(NotImplementedError) as ei:
        status.raise_experimental_unwired('X.y')
    assert 'olap_dispatch' in str(ei.value)


# ==================== 5. HybridTable 状态常量与 status 同源 ====================

def test_hybrid_table_wired_constant_matches_status():
    from lightdb.db.hybrid_table import HybridTable
    from lightdb.olap import status
    assert HybridTable.WIRED_TO_SQL_PATH == status.WIRED_TO_SQL_PATH is True
