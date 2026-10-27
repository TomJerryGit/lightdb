# -*- coding: utf-8 -*-
"""task-58 验收：SHOW CREATE PROCEDURE / FUNCTION（Navicat 备份 1064）

复现（2026-10-27，pymysql → 127.0.0.1:8898，修复前）：

    SHOW CREATE PROCEDURE `probe_p2`
      → (1064, "You have an error in your SQL syntax near
                'SHOW CREATE PROCEDURE probe_p2'")

修复：
* `SHOW CREATE PROCEDURE/FUNCTION name`：MySQL 兼容 6 列
  （Procedure/Function, sql_mode, Create Procedure/Function,
   character_set_client, collation_connection, Database Collation）；
  名称支持反引号与 `db.name`（未知库 1049、未知对象 1305）。
* 内容来自真实 `_stored.json`（参数/返回类型/过程体），重建 DDL 可被本引擎
  再次 CREATE 并保持行为一致（测试做往返）。
* `SHOW CREATE TRIGGER/EVENT`：有真实对象返回对应 MySQL 列；无对象时显式
  1360（不是 1064），兼容 Navicat 备份序列。
"""
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ==================== fixtures ====================

@pytest.fixture()
def inproc(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    try:
        yield db, ex
    finally:
        try:
            db.shutdown()
        except Exception:
            pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture()
def served(tmp_path):
    pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    import pymysql

    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    cfg.root_password = ''
    cfg.allow_empty_password = True
    db = Database(str(tmp_path / 'data_srv'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password='', database='default',
                           charset='utf8mb4', autocommit=True,
                           connect_timeout=10)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        for fn in (srv.shutdown, srv.server_close):
            try:
                fn()
            except Exception:
                pass
        try:
            db.shutdown()
        except Exception:
            pass


def ok(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') != 'error', (sql, r)
    return r


def err(ex, sql):
    r = ex.execute_sql(sql, {})
    assert r.get('type') == 'error', (sql, r)
    return r


def _seed(ex):
    ok(ex, 'CREATE PROCEDURE p_demo(IN x INT, OUT y INT) '
           'BEGIN SET y = x + 1; END')
    ok(ex, 'CREATE FUNCTION f_demo(x INT) RETURNS INT RETURN x * 2')


# ==================== 一、SHOW CREATE PROCEDURE / FUNCTION ====================

def test_show_create_procedure_columns_and_definition(inproc):
    _db, ex = inproc
    _seed(ex)
    r = ok(ex, 'SHOW CREATE PROCEDURE `p_demo`')
    assert r['columns'] == ['Procedure', 'sql_mode', 'Create Procedure',
                            'character_set_client', 'collation_connection',
                            'Database Collation'], r['columns']
    cols = r['columns']
    row = dict(zip(cols, r['rows'][0]))
    assert row['Procedure'] == 'p_demo', row
    assert 'STRICT_TRANS_TABLES' in row['sql_mode'], row
    ddl = row['Create Procedure']
    assert 'CREATE PROCEDURE `p_demo`(IN x INT, OUT y INT)' in ddl, ddl
    assert 'SET y = x + 1' in ddl, ddl
    assert row['character_set_client'] == 'utf8mb4', row
    assert row['Database Collation'], row
    print(f"\n    SHOW CREATE PROCEDURE：6 列 MySQL 兼容，内容来自真实"
          f"定义（{ddl.splitlines()[0]}）")


def test_show_create_function_columns_and_definition(inproc):
    _db, ex = inproc
    _seed(ex)
    r = ok(ex, 'SHOW CREATE FUNCTION `f_demo`')
    assert r['columns'] == ['Function', 'sql_mode', 'Create Function',
                            'character_set_client', 'collation_connection',
                            'Database Collation'], r['columns']
    row = dict(zip(r['columns'], r['rows'][0]))
    assert row['Function'] == 'f_demo'
    ddl = row['Create Function']
    assert 'CREATE FUNCTION `f_demo`(IN x INT) RETURNS INT' in ddl, ddl
    assert 'RETURN x * 2' in ddl, ddl
    print(f"\n    SHOW CREATE FUNCTION：6 列 + 真实返回类型/函数体"
          f"（{ddl.splitlines()[0]}）")


def test_show_create_routine_roundtrip_executable(inproc):
    _db, ex = inproc
    _seed(ex)
    proc_ddl = dict(zip(*[
        ok(ex, 'SHOW CREATE PROCEDURE p_demo')['columns'],
        ok(ex, 'SHOW CREATE PROCEDURE p_demo')['rows'][0]]))["Create Procedure"]
    func_ddl = dict(zip(*[
        ok(ex, 'SHOW CREATE FUNCTION f_demo')['columns'],
        ok(ex, 'SHOW CREATE FUNCTION f_demo')['rows'][0]]))["Create Function"]
    # 先删掉原对象，再把 SHOW CREATE 文本原样 CREATE 回来（可执行性）
    ex._stored.drop_procedure('p_demo')
    ex._stored.drop_function('f_demo')
    ok(ex, proc_ddl)
    ok(ex, func_ddl)
    # 函数行为一致
    r = ok(ex, 'SELECT f_demo(21)')
    assert [tuple(x) for x in r['rows']] == [(42,)], r['rows']
    # 过程用 OUT 参数可 CALL，回填的用户变量值一致（同一会话）
    sess = {}
    r = ex.execute_sql('CALL p_demo(4, @p_out)', sess)
    assert r.get('type') != 'error', r
    r = ex.execute_sql('SELECT @p_out', sess)
    assert r.get('type') != 'error', r
    assert [tuple(x) for x in r['rows']] == [(5,)], r['rows']
    # 往返后 SHOW CREATE 文本仍一致
    again = dict(zip(*[
        ok(ex, 'SHOW CREATE PROCEDURE p_demo')['columns'],
        ok(ex, 'SHOW CREATE PROCEDURE p_demo')['rows'][0]]))["Create Procedure"]
    assert again == proc_ddl
    print("\n    SHOW CREATE 文本原样重建：函数 f(21)=42、过程 @p_out=5，"
          "行为与重建前一致")


def test_show_create_routine_name_variants_and_errors(inproc):
    _db, ex = inproc
    _seed(ex)
    # 反引号 / 限定名
    for sql in ('SHOW CREATE PROCEDURE `p_demo`',
                'SHOW CREATE PROCEDURE default.p_demo',
                'SHOW CREATE PROCEDURE `default`.`p_demo`'):
        r = ok(ex, sql)
        assert r['rows'][0][0] == 'p_demo', (sql, r['rows'])
    for sql in ('SHOW CREATE FUNCTION `f_demo`',
                'SHOW CREATE FUNCTION `default`.`f_demo`'):
        assert ok(ex, sql)['rows'][0][0] == 'f_demo', sql
    # 未知对象 → 1305
    r = err(ex, 'SHOW CREATE PROCEDURE `no_such_proc`')
    assert r['code'] == 1305, r
    assert 'PROCEDURE' in str(r['message']).upper(), r
    r = err(ex, 'SHOW CREATE FUNCTION `no_such_func`')
    assert r['code'] == 1305, r
    # 未知库 → 1049
    r = err(ex, 'SHOW CREATE PROCEDURE `no_such_db`.`p_demo`')
    assert r['code'] == 1049, r
    print("\n    名称变体（反引号/限定名）OK；未知对象 1305、未知库 1049")


# ==================== 二、SHOW CREATE TRIGGER / EVENT ====================

def test_show_create_trigger_and_event_never_1064(inproc):
    _db, ex = inproc
    ok(ex, 'CREATE TABLE tr_t (id INT PRIMARY KEY, v INT)')
    ok(ex, 'CREATE TRIGGER tr_demo BEFORE INSERT ON tr_t '
           'FOR EACH ROW SET NEW.v = NEW.id')
    r = ok(ex, 'SHOW CREATE TRIGGER `tr_demo`')
    cols = r['columns']
    assert 'Trigger' in cols and 'SQL Original Statement' in cols, cols
    row = dict(zip(cols, r['rows'][0]))
    stmt = row['SQL Original Statement']
    assert 'TRIGGER `tr_demo`' in stmt and 'BEFORE INSERT' in stmt, stmt
    # 不存在的触发器/事件 → 显式 1360（不是 1064）
    r = err(ex, 'SHOW CREATE TRIGGER `no_such_trigger`')
    assert r['code'] == 1360, r
    r = err(ex, 'SHOW CREATE EVENT `no_such_event`')
    assert r['code'] == 1360, r
    print("\n    SHOW CREATE TRIGGER 真实列/语句；未知 Trigger/Event 显式 "
          "1360，不再 1064")


# ==================== 三、pymysql 真实备份序列 ====================

def test_pymysql_backup_show_create_sequence(served):
    conn = served
    with conn.cursor() as cur:
        cur.execute('CREATE PROCEDURE p_bak(IN x INT) '
                    'BEGIN SET @bak_out = x + 1; END')
        cur.execute('CREATE FUNCTION f_bak(x INT) RETURNS INT '
                    'RETURN x + 10')
        # Navicat 备份序列：先 STATUS 列对象，再逐个 SHOW CREATE
        cur.execute("SHOW PROCEDURE STATUS WHERE Db='default'")
        cols = [d[0] for d in cur.description]
        procs = [r[cols.index('Name')] for r in cur.fetchall()]
        assert 'p_bak' in procs, procs
        cur.execute("SHOW FUNCTION STATUS WHERE Db='default'")
        cols = [d[0] for d in cur.description]
        funcs = [r[cols.index('Name')] for r in cur.fetchall()]
        assert 'f_bak' in funcs, funcs
        cur.execute('SHOW CREATE PROCEDURE `p_bak`')
        pcols = [d[0] for d in cur.description]
        prow = cur.fetchone()
        assert pcols == ['Procedure', 'sql_mode', 'Create Procedure',
                         'character_set_client', 'collation_connection',
                         'Database Collation'], pcols
        assert prow[0] == 'p_bak' and 'CREATE PROCEDURE `p_bak`' in prow[2]
        cur.execute('SHOW CREATE FUNCTION `f_bak`')
        fcols = [d[0] for d in cur.description]
        frow = cur.fetchone()
        assert fcols == ['Function', 'sql_mode', 'Create Function',
                         'character_set_client', 'collation_connection',
                         'Database Collation'], fcols
        assert frow[0] == 'f_bak' and 'RETURNS INT' in frow[2]
        # 限定名
        cur.execute("SHOW CREATE PROCEDURE `default`.`p_bak`")
        assert cur.fetchone()[0] == 'p_bak'
        # 备份页对 trigger/event 的探测：无对象 → 1360 而非 1064
        for sql in ('SHOW CREATE TRIGGER `nope_t`',
                    'SHOW CREATE EVENT `nope_e`'):
            with pytest.raises(Exception) as ei:
                cur.execute(sql)
            assert '1360' in str(ei.value) or 'does not exist' in \
                str(ei.value), (sql, ei.value)
        # 备份序列之后仍可继续正常查询
        cur.execute('SELECT 1')
        assert cur.fetchone()[0] == 1
    print("\n    pymysql 备份序列：STATUS → SHOW CREATE PROCEDURE/FUNCTION "
          "（含限定名）全部通过；trigger/event 探测返回 1360，不再 1064")
