"""OLAP 接线契约（4A.2 迁移后的**新契约**，旧「未接线/零 import」契约已废弃）

> 历史：本文件在 4A.1 之前锁的是「SQL 路径零 import OLAP / `WIRED_TO_SQL_PATH
> 为 False`」。4A.2 起 OLAP 已**显式接入** SQL 路径；2026-10-20 起按用户要求
> **默认开启**（`DEFAULT_ENABLED=True`），可用 `LIGHTDB_OLAP_DISPATCH=0` 关闭。
> 完整契约见 `tests/test_olap_wiring_contract.py`，本文件保留为最小回归入口。

新契约：
1. `olap.status.WIRED_TO_SQL_PATH is True`（已接线）且 `DEFAULT_ENABLED is
   True`（默认开启；显式关闭用 `LIGHTDB_OLAP_DISPATCH=0`）；
2. 显式关闭时 SQL 查询**不尝试分派**（`dispatch_stats()['attempted']==0`），
   且不 import 任何 OLAP/列存引擎模块；
3. 默认开启时，白名单聚合查询**确实**走向量化（`dispatched>0`，受
   `olap_dispatch_min_rows` 阈值影响），且结果与行存逐行一致；白名单外语法
   `dispatched==0`（无静默改道）；
4. `COUNTS_FOR_PERFORMANCE_CLAIMS` 自 4A.5 起为 True（已有可辩护的基准门禁，
   见 `benchmarks/gate.py: olap_parallel_groupby_speedup` /
   `olap_dispatch_crosscheck_mismatches`）。

删除的是旧契约里「`lightdb/sql/**` 对 olap 零 import」这一条：现在
`Executor._cmd_select` 会在开关打开时 import
`lightdb/olap/sql_dispatch.py`，这是**预期行为**（显式关闭时不会发生）。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_status_says_wired_and_enabled_by_default():
    from lightdb.olap import status
    assert status.WIRED_TO_SQL_PATH is True
    assert status.DEFAULT_ENABLED is True, '2026-10-20 起默认开启'
    assert status.COUNTS_FOR_PERFORMANCE_CLAIMS is True, '4A.5 已加基准门禁'


def test_explicit_env_off_does_not_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_OLAP_DISPATCH', '0')
    from lightdb.db.database import Database
    from lightdb.olap import sql_dispatch
    from lightdb.sql.executor import Executor

    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    try:
        ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v INT)', session)
        ex.execute_sql('INSERT INTO t VALUES (1, 10), (2, 20)', session)
        assert ex.olap_dispatch is False
        sql_dispatch.reset_dispatch_stats()
        for q in ('SELECT COUNT(*), SUM(v) FROM t',
                  'SELECT AVG(v) FROM t WHERE v > 0',
                  'SELECT v, COUNT(*) FROM t GROUP BY v'):
            r = ex.execute_sql(q, session)
            assert r.get('type') == 'resultset', (q, r)
        assert sql_dispatch.dispatch_stats()['attempted'] == 0
    finally:
        db.shutdown()


def test_default_executor_dispatches_and_matches_row_path(tmp_path):
    from lightdb.db.database import Database
    from lightdb.olap import sql_dispatch
    from lightdb.sql.executor import Executor

    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    try:
        ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, g VARCHAR(5), '
                       'v INT)', session)
        ex.execute_sql("INSERT INTO t VALUES (1,'a',10),(2,'a',20),"
                       "(3,'b',NULL),(4,'b',-5)", session)
        assert ex.olap_dispatch is True, '默认开启'
        q = 'SELECT g, COUNT(*), SUM(v), AVG(v) FROM t GROUP BY g'
        ex.olap_dispatch = False
        off = ex.execute_sql(q, session)
        ex.olap_dispatch = True
        ex.olap_dispatch_min_rows = 0        # 小表也派发（测试阈值行为）
        sql_dispatch.reset_dispatch_stats()
        on = ex.execute_sql(q, session)
        assert sql_dispatch.dispatch_stats()['dispatched'] == 1
        assert [list(r) for r in on.get('rows') or []] == \
            [list(r) for r in off.get('rows') or []]
    finally:
        db.shutdown()


def test_switch_on_dispatches_without_changing_results(tmp_path):
    from lightdb.db.database import Database
    from lightdb.olap import sql_dispatch
    from lightdb.sql.executor import Executor

    db = Database(str(tmp_path / 'db'))
    ex = Executor(db)
    session = {}
    try:
        ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, g VARCHAR(5), '
                       'v INT)', session)
        ex.execute_sql("INSERT INTO t VALUES (1,'a',10),(2,'a',20),"
                       "(3,'b',NULL),(4,'b',-5)", session)
        q = 'SELECT g, COUNT(*), SUM(v), AVG(v) FROM t GROUP BY g'
        off = ex.execute_sql(q, session)
        ex.olap_dispatch = True
        ex.olap_dispatch_min_rows = 0
        sql_dispatch.reset_dispatch_stats()
        on = ex.execute_sql(q, session)
        assert sql_dispatch.dispatch_stats()['dispatched'] == 1
        assert list(on.get('columns') or []) == list(off.get('columns') or [])
        assert [list(r) for r in on.get('rows') or []] == \
            [list(r) for r in off.get('rows') or []]

        # 白名单外：不派发（无静默改道）
        sql_dispatch.reset_dispatch_stats()
        r = ex.execute_sql('SELECT COUNT(*) FROM t WHERE v > 0 OR id = 1',
                           session)
        assert r.get('type') == 'resultset'
        assert sql_dispatch.dispatch_stats()['dispatched'] == 0
        assert sql_dispatch.dispatch_stats()['fallback'] >= 1
    finally:
        db.shutdown()


if __name__ == '__main__':
    sys.exit(pytest.main([os.path.abspath(__file__), '-q']))
