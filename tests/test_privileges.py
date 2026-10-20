"""P1 阶段 1.1：权限体系（GRANT/REVOKE/SHOW GRANTS + 执行期鉴权）。

范围/语义（与 `security/privileges.py` 的权限模型一致）：

* 事实来源：`Database.users[user]['privs']`（与握手共用同一份用户表，
  随 `_meta.json` 持久化）。root 内置 `['ALL']`。
* 支持权限：ALL / SELECT / INSERT / UPDATE / DELETE / CREATE / DROP /
  ALTER / INDEX；作用域只支持 `*.*`（全局）与 `db.*`（库级，单库模型下
  库名必须是真实库）；列级/表级作用域显式 1064。
* 执行期鉴权在分发前完成，错误码照 MySQL 语义：
    - 表级拒绝：1142 `<CMD> command denied to user 'u'@'host' for table 't'`
    - 库级拒绝：1044 `Access denied for user 'u'@'host' to database 'db'`
    - 管理语句：1227 `Access denied; you need ... privilege(s)`
    - 语法/不支持形态：1064
  全部走真实 `Executor`；网络部分走真实 `MySQLServer` + pymysql。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_privileges.py
    from conftest import track

HOST = '127.0.0.1'
ROOT = {'user': 'root', 'host': HOST}
BOB = {'user': 'bob', 'host': HOST}

pymysql = pytest.importorskip('pymysql')


# ==================== 工具 ====================

def _make_db(tmp_path, name='data'):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = track(Database(str(tmp_path / name),
                        allow_empty_password=True))
    ex = track(Executor(db))
    return db, ex


def _run(ex, sql, session=ROOT):
    """执行一条 SQL（每次传新的 session dict，避免用例间串状态）"""
    return ex.execute_sql(sql, dict(session))


def _err(result, code):
    return (isinstance(result, dict) and result.get('type') == 'error'
            and int(result.get('code') or 0) == code)


def _rows(result):
    assert isinstance(result, dict) and result.get('type') == 'resultset', result
    return result['rows']


def _start_server(tmp_path, name='netdata'):
    """真实 MySQLServer（随机端口 + 后台线程），返回 (srv, db, ex, cfg)"""
    from lightdb.config import Config
    from lightdb.net.server import MySQLServer

    db, ex = _make_db(tmp_path, name)
    cfg = Config()
    cfg.host = HOST
    cfg.port = 0
    srv = track(MySQLServer(cfg, db, ex))
    cfg.port = srv.server_address[1]
    started = threading.Event()

    def _serve():
        started.set()
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    assert started.wait(5), 'serve_forever 线程没有启动'
    return srv, db, ex, cfg


def _connect(port, user='root', password=''):
    return track(pymysql.connect(
        host=HOST, port=port, user=user, password=password,
        autocommit=True, charset='utf8mb4',
        connect_timeout=5, read_timeout=10, write_timeout=10))


def _pymysql_err(exc_info):
    args = getattr(exc_info.value, 'args', ())
    return args[0] if args else None


# ==================== 进程内：权限矩阵 ====================

def test_root_has_all_privileges(tmp_path):
    """root 保持 ['ALL']：DDL/DML/DCL/SHOW GRANTS 全部可用"""
    db, ex = _make_db(tmp_path)
    assert db.users['root']['privs'] == ['ALL']

    assert _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)'
                ).get('type') == 'ok'
    assert _run(ex, 'INSERT INTO t VALUES (1, 10)').get('type') == 'ok'
    assert _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'"
                ).get('type') == 'ok'
    assert _run(ex, "GRANT SELECT ON *.* TO 'bob'").get('type') == 'ok'
    assert _run(ex, 'SELECT * FROM t').get('type') == 'resultset'
    assert _run(ex, "SHOW GRANTS FOR 'bob'").get('type') == 'resultset'
    assert _run(ex, 'CREATE TABLE t2 (id INT)').get('type') == 'ok'
    assert _run(ex, 'DROP TABLE t2').get('type') == 'ok'
    assert _run(ex, "DROP USER 'bob'").get('type') == 'ok'


def test_denied_matrix(tmp_path):
    """无权限账户：每类命令的 MySQL 语义错误码"""
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    _run(ex, 'INSERT INTO t VALUES (1, 10)')
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")

    assert db.users['bob']['privs'] == []

    # ---- 表级 1142 ----
    cases_1142 = [
        'SELECT * FROM t',
        "INSERT INTO t VALUES (2, 20)",
        'UPDATE t SET v = 1 WHERE id = 1',
        'DELETE FROM t WHERE id = 1',
        'CREATE TABLE t_new (id INT)',
        'DROP TABLE t',
        'ALTER TABLE t ADD COLUMN c INT',
        'TRUNCATE TABLE t',
        'DESC t',
        'SHOW COLUMNS FROM t',
        'EXPLAIN SELECT * FROM t',
        'ANALYZE TABLE t',
    ]
    for sql in cases_1142:
        r = _run(ex, sql, BOB)
        assert _err(r, 1142), (sql, r)
        assert 'command denied to user' in r['message'], r
        assert r.get('sqlstate') == '42000', r

    # ---- 库级 1044 ----
    for sql in ('SHOW TABLES', 'USE default'):
        r = _run(ex, sql, BOB)
        assert _err(r, 1044), (sql, r)
        assert 'to database' in r['message'], r

    # ---- 管理语句 1227 ----
    for sql in ("GRANT SELECT ON *.* TO 'root'",
                "REVOKE SELECT ON *.* FROM 'root'",
                "CREATE USER 'x' IDENTIFIED BY 'Xpass123'",
                'SHOW SECURITY',
                "SET PASSWORD POLICY min_length = 12"):
        r = _run(ex, sql, BOB)
        assert _err(r, 1227), (sql, r)

    # ---- information_schema / 系统库保持最小可用 ----
    assert _run(ex, 'SELECT * FROM information_schema.TABLES',
                BOB).get('type') == 'resultset'
    # 内部 binlog 接口只给管理员
    assert _err(_run(ex, 'SELECT * FROM __BINLOG_FETCH__', BOB), 1227)
    assert _run(ex, 'SELECT * FROM __BINLOG_FETCH__',
                ROOT).get('type') == 'resultset'

    # ---- 未知用户：按未授权处理（不静默放行） ----
    r = _run(ex, 'SELECT * FROM t', {'user': 'ghost', 'host': HOST})
    assert _err(r, 1142), r


def test_grant_select_then_revoke(tmp_path):
    """GRANT SELECT 后可读不可写；GRANT INSERT 后可写；REVOKE 后重新被拒"""
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    _run(ex, 'INSERT INTO t VALUES (1, 10)')
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")

    assert _err(_run(ex, 'SELECT * FROM t', BOB), 1142)
    assert _run(ex, "GRANT SELECT ON *.* TO 'bob'").get('type') == 'ok'
    r = _run(ex, 'SELECT * FROM t', BOB)
    assert r.get('type') == 'resultset' and list(r['rows']) == [(1, 10)], r
    assert _err(_run(ex, "INSERT INTO t VALUES (2, 20)", BOB), 1142)

    assert _run(ex, "GRANT INSERT ON *.* TO 'bob'").get('type') == 'ok'
    assert _run(ex, "INSERT INTO t VALUES (2, 20)", BOB).get('type') == 'ok'
    assert _rows(_run(ex, 'SELECT * FROM t')) == [(1, 10), (2, 20)]

    assert _run(ex, "REVOKE INSERT ON *.* FROM 'bob'").get('type') == 'ok'
    assert _err(_run(ex, "INSERT INTO t VALUES (3, 30)", BOB), 1142)
    # 权限已写入用户表（随 _meta.json 持久化）
    assert 'INSERT' not in db.users['bob']['privs']
    assert 'SELECT' in db.users['bob']['privs']


def test_db_scoped_grant_not_admin(tmp_path):
    """库级 GRANT ALL 只在该库全权，**不会**变成管理员"""
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    _run(ex, 'INSERT INTO t VALUES (1, 10)')
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")

    assert _err(_run(ex, 'SHOW TABLES', BOB), 1044)
    assert _run(ex, "GRANT ALL ON default.* TO 'bob'").get('type') == 'ok'

    # 库内全权（DML/DDL/查询/元数据）
    assert _run(ex, 'SELECT * FROM t', BOB).get('type') == 'resultset'
    assert _run(ex, "INSERT INTO t VALUES (2, 20)", BOB).get('type') == 'ok'
    assert _run(ex, 'UPDATE t SET v = 99 WHERE id = 1',
                BOB).get('type') == 'ok'
    assert _run(ex, 'CREATE TABLE t2 (id INT)', BOB).get('type') == 'ok'
    assert _run(ex, 'DROP TABLE t2', BOB).get('type') == 'ok'
    assert _run(ex, 'SHOW TABLES', BOB).get('type') == 'resultset'
    assert _run(ex, 'USE default', BOB).get('type') == 'ok'
    # 库级 ALL 不是管理员：DCL 仍然 1227
    assert _err(_run(ex, "CREATE USER 'x' IDENTIFIED BY 'Xpass123'",
                     BOB), 1227)
    assert _err(_run(ex, "GRANT SELECT ON *.* TO 'root'", BOB), 1227)

    lines = [row[0] for row in _rows(
        _run(ex, "SHOW GRANTS FOR 'bob'"))]
    assert any('ALL PRIVILEGES' in ln and '`default`' in ln
               for ln in lines), lines


def test_show_grants_matrix(tmp_path):
    """SHOW GRANTS：自己/root/他人/不存在用户的真实行为"""
    db, ex = _make_db(tmp_path)
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")
    _run(ex, "GRANT SELECT ON *.* TO 'bob'")
    _run(ex, "GRANT INSERT ON default.* TO 'bob'")

    # root 看 bob：两行（全局 SELECT + 库级 INSERT）
    r = _run(ex, "SHOW GRANTS FOR 'bob'")
    assert r['columns'] == ['Grants for bob@%'], r
    lines = [row[0] for row in r['rows']]
    assert any(ln.startswith('GRANT SELECT ON *.*') for ln in lines), lines
    assert any('INSERT' in ln and '`default`' in ln for ln in lines), lines

    # bob 看自己：允许；列名是 "Grants for bob@<host>"
    r = _run(ex, 'SHOW GRANTS', BOB)
    assert r.get('type') == 'resultset', r
    b_lines = [row[0] for row in r['rows']]
    assert any('SELECT' in ln for ln in b_lines), b_lines
    assert any('INSERT' in ln for ln in b_lines), b_lines

    # bob 看 root：1142（只能看自己的）
    assert _err(_run(ex, "SHOW GRANTS FOR 'root'", BOB), 1142)

    # root 自己的 ALL
    root_lines = [row[0] for row in _rows(_run(ex, 'SHOW GRANTS'))]
    assert root_lines == [
        "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%'"], root_lines

    # 不存在的用户：1141
    r = _run(ex, "SHOW GRANTS FOR 'nobody'")
    assert _err(r, 1141), r
    assert 'no such grant' in r['message'].lower(), r


def test_grants_persist_after_reopen(tmp_path):
    """权限随 _meta.json 持久化：重开 Database 后仍生效"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    data = str(tmp_path / 'persist')
    db = Database(data, allow_empty_password=True)
    try:
        ex = Executor(db)
        _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
        _run(ex, 'INSERT INTO t VALUES (1, 10)')
        _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")
        _run(ex, "GRANT SELECT ON *.* TO 'bob'")
        _run(ex, "GRANT INSERT ON default.* TO 'bob'")
    finally:
        db.shutdown()

    db2 = track(Database(data, allow_empty_password=True))
    ex2 = track(Executor(db2))
    # 全局 SELECT：可读
    assert _run(ex2, 'SELECT * FROM t', BOB).get('type') == 'resultset'
    # 库级 INSERT：可写
    assert _run(ex2, "INSERT INTO t VALUES (2, 20)", BOB).get('type') == 'ok'
    # 未授予的 DELETE 仍被拒
    assert _err(_run(ex2, 'DELETE FROM t WHERE id = 1', BOB), 1142)
    lines = [row[0] for row in _rows(_run(ex2, "SHOW GRANTS FOR 'bob'"))]
    assert any('SELECT' in ln for ln in lines)
    assert any('INSERT' in ln and '`default`' in ln for ln in lines)


def test_parse_and_semantic_rejections(tmp_path):
    """不支持的 GRANT/REVOKE 形态显式 1064（绝不静默忽略）"""
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")

    cases = [
        "GRANT SELECT(id) ON *.* TO 'bob'",          # 列级
        "GRANT SELECT ON default.t TO 'bob'",        # 表级作用域
        "GRANT SUPER ON *.* TO 'bob'",               # 未知权限
        "GRANT SELECT ON *.* TO 'bob' WITH GRANT OPTION",
        "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'bob'",
        "GRANT SELECT TO 'bob'",                     # 缺 ON
        "REVOKE SELECT *.* FROM 'bob'",              # 缺 ON
    ]
    for sql in cases:
        r = _run(ex, sql)
        assert _err(r, 1064), (sql, r)
        assert 'SQL 解析错误' in r['message'], (sql, r)

    # GRANT 隐式建号：MySQL 8 语义 1410（不会偷偷创建空口令账户）
    r = _run(ex, "GRANT SELECT ON *.* TO 'nobody'")
    assert _err(r, 1410), r
    assert 'nobody' not in db.users
    # REVOKE 不存在的授权：1141
    r = _run(ex, "REVOKE SELECT ON *.* FROM 'nobody'")
    assert _err(r, 1141), r

    # root 的全局 ALL 是救援入口：不允许被回收
    r = _run(ex, "REVOKE ALL ON *.* FROM 'root'")
    assert _err(r, 1227), r
    assert db.users['root']['privs'] == ['ALL']


def test_internal_session_is_trusted_not_a_network_bypass(tmp_path):
    """进程内调用（无 user 键）保持可用；网络身份一定来自握手"""
    db, ex = _make_db(tmp_path)
    # 内部路径（空 session）：照旧可用（大量既有测试/内部组件依赖）
    assert _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)',
                session={}).get('type') == 'ok'
    assert _run(ex, 'INSERT INTO t VALUES (1)', session={}).get('type') == 'ok'
    assert _run(ex, 'SELECT * FROM t', session={}).get('type') == 'resultset'
    # 但一旦给出未知用户名，就按未授权处理
    assert _err(_run(ex, 'SELECT * FROM t', {'user': 'ghost'}), 1142)


def test_internal_service_identity_system(tmp_path):
    """内部服务身份 'system'（事件调度器用）不按未知用户处理

    见 `sql/event.py`：事件调度器以 `{'user': 'system'}` 执行事件体。
    只有当同名数据库账户不存在时它才是"引擎内部服务身份"；一旦真的创建
    了数据库账户 'system'，就按普通账户鉴权（未授权即拒）。
    """
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE t (id INT PRIMARY KEY)')
    assert _run(ex, 'INSERT INTO t VALUES (1)',
                {'user': 'system'}).get('type') == 'ok'
    assert _run(ex, 'SELECT * FROM t',
                {'user': 'system'}).get('type') == 'resultset'

    _run(ex, "CREATE USER 'system' IDENTIFIED BY 'SystemPass123'")
    assert _err(_run(ex, 'SELECT * FROM t', {'user': 'system'}), 1142)


def test_union_and_join_require_select(tmp_path):
    """UNION / JOIN 的每个数据源都要有 SELECT"""
    db, ex = _make_db(tmp_path)
    _run(ex, 'CREATE TABLE a (id INT PRIMARY KEY)')
    _run(ex, 'CREATE TABLE b (id INT PRIMARY KEY)')
    _run(ex, 'INSERT INTO a VALUES (1)')
    _run(ex, 'INSERT INTO b VALUES (1)')
    _run(ex, "CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")
    _run(ex, "GRANT SELECT ON *.* TO 'bob'")

    r = _run(ex, 'SELECT id FROM a UNION SELECT id FROM b', BOB)
    assert r.get('type') == 'resultset', r
    r = _run(ex, 'SELECT a.id FROM a JOIN b ON a.id = b.id', BOB)
    assert r.get('type') == 'resultset', r

    _run(ex, "REVOKE SELECT ON *.* FROM 'bob'")
    assert _err(_run(ex, 'SELECT id FROM a UNION SELECT id FROM b',
                     BOB), 1142)
    assert _err(_run(ex, 'SELECT a.id FROM a JOIN b ON a.id = b.id',
                     BOB), 1142)


# ==================== 真实 MySQLServer + pymysql 端到端 ====================

def test_network_end_to_end(tmp_path):
    """真实握手 + 真实连接：GRANT/REVOKE 立即影响现有连接"""
    srv, db, ex, cfg = _start_server(tmp_path)
    port = cfg.port

    root = _connect(port, 'root', '')
    rcur = root.cursor()
    rcur.execute('CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    rcur.execute('INSERT INTO t VALUES (1, 10)')
    rcur.execute("CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")
    root_id = None
    rcur.execute('SELECT CONNECTION_ID()')
    root_id = rcur.fetchone()[0]

    bob = _connect(port, 'bob', 'BobPass123')
    bcur = bob.cursor()

    # 无权限：SELECT 1142 / INSERT 1142 / SHOW TABLES 1044
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute('SELECT * FROM t')
    assert _pymysql_err(ei) == 1142, ei.value
    assert 'command denied to user' in str(ei.value)
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute('INSERT INTO t VALUES (2, 20)')
    assert _pymysql_err(ei) == 1142
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute('SHOW TABLES')
    assert _pymysql_err(ei) == 1044, ei.value
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bob.select_db('default')
    assert _pymysql_err(ei) == 1044, ei.value

    # SHOW DATABASES 过滤 / SHOW GRANTS 只看自己 / SHOW PROCESSLIST 只看自己
    bcur.execute('SHOW DATABASES')
    assert [row[0] for row in bcur.fetchall()] == ['information_schema']
    bcur.execute('SHOW GRANTS')
    assert any('USAGE' in row[0] for row in bcur.fetchall())
    bcur.execute('SHOW PROCESSLIST')
    assert len(bcur.fetchall()) == 1

    # 非管理员不能 GRANT / KILL 别人
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute("GRANT SELECT ON *.* TO 'root'")
    assert _pymysql_err(ei) == 1227, ei.value
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute(f'KILL {root_id}')
    assert _pymysql_err(ei) == 1227, ei.value

    # root GRANT SELECT：bob 的现有连接**立即**可读（无需重连）
    rcur.execute("GRANT SELECT ON default.* TO 'bob'")
    bcur.execute('SELECT * FROM t')
    assert list(bcur.fetchall()) == [(1, 10)]
    bob.select_db('default')
    bcur.execute('SHOW TABLES')
    assert [row[0] for row in bcur.fetchall()] == ['t']

    # 仍不可写；GRANT INSERT 后可写；REVOKE 后复拒
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute('INSERT INTO t VALUES (2, 20)')
    assert _pymysql_err(ei) == 1142
    rcur.execute("GRANT INSERT ON *.* TO 'bob'")
    bcur.execute('INSERT INTO t VALUES (2, 20)')
    rcur.execute("REVOKE INSERT ON *.* FROM 'bob'")
    with pytest.raises(pymysql.err.OperationalError) as ei:
        bcur.execute('INSERT INTO t VALUES (3, 30)')
    assert _pymysql_err(ei) == 1142

    # root 可以看到 bob 的授权；root 的 SHOW GRANTS 是 ALL
    rcur.execute("SHOW GRANTS FOR 'bob'")
    bob_lines = [row[0] for row in rcur.fetchall()]
    assert any('SELECT' in ln and '`default`' in ln
               for ln in bob_lines), bob_lines
    rcur.execute('SHOW GRANTS')
    assert rcur.fetchone()[0].startswith('GRANT ALL PRIVILEGES')

    # 不存在的用户连接失败（1045 行为保持）
    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(port, 'nobody', 'whatever')
    assert _pymysql_err(ei) == 1045, ei.value


def test_network_grants_persist_after_reopen(tmp_path):
    """网络授权落盘：同一数据目录重开 Database 后权限仍在"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    data = str(tmp_path / 'netpersist')
    srv, db, ex, cfg = _start_server(tmp_path, name='netpersist')
    root = _connect(cfg.port, 'root', '')
    cur = root.cursor()
    cur.execute('CREATE TABLE t (id INT PRIMARY KEY)')
    cur.execute('INSERT INTO t VALUES (1)')
    cur.execute("CREATE USER 'bob' IDENTIFIED BY 'BobPass123'")
    cur.execute("GRANT SELECT ON *.* TO 'bob'")
    root.close()

    # 重开同一数据目录（父目录已由 track 的 srv/db 持有，先手动关闭）
    srv.shutdown()
    srv.server_close()
    db.shutdown()

    db2 = track(Database(data, allow_empty_password=True))
    ex2 = track(Executor(db2))
    r = ex2.execute_sql('SELECT * FROM t', dict(BOB))
    assert r.get('type') == 'resultset' and list(r['rows']) == [(1,)], r
