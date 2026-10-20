"""OLAP 接线契约（4A.2 迁移后的**新契约**，旧「未接线/零 import」契约已废弃）

> 历史：本文件在 4A.1 之前锁的是「SQL 路径零 import OLAP / `WIRED_TO_SQL_PATH
> 为 False`」。4A.2 起 OLAP 已**显式接入** SQL 路径（默认关闭），旧的
> 反向 import 锁已经不成立，故重写为新契约。完整契约见
> `tests/test_olap_wiring_contract.py`，本文件保留为最小回归入口。

新契约：
1. `olap.status.WIRED_TO_SQL_PATH is True`（已接线）且 `DEFAULT_ENABLED is
   False`（默认行存）；
2. 默认关闭时 SQL 查询**不尝试分派**（`dispatch_stats()['attempted']==0`），
   结果与接线前一致；
3. 显式打开后，白名单聚合查询**确实**走向量化（`dispatched>0`），且结果
   与行存逐行一致；白名单外语法 `dispatched==0`（无静默改道）；
4. `COUNTS_FOR_PERFORMANCE_CLAIMS` 在 4A.5 基准门禁之前保持 False。

删除的是旧契约里「`lightdb/sql/**` 对 olap 零 import」这一条：现在
`Executor._cmd_select` 会在开关打开时 import
`lightdb/olap/sql_dispatch.py`，这是**预期行为**（且默认关闭时不会发生）。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_status_says_wired_but_disabled_by_default():
    from lightdb.olap import status
    assert status.WIRED_TO_SQL_PATH is True
    assert status.DEFAULT_ENABLED is False
    assert status.COUNTS_FOR_PERFORMANCE_CLAIMS is False


def test_default_executor_does_not_dispatch(tmp_path):
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
