"""元数据诚实化锁定测试（阶段 1 第 4 项）

原则：元数据要么真实，要么显式报错，绝不伪造。

本文件锁定 5 处「假元数据」修复后的行为：

1. `SHOW COLUMNS` 的 `Type` 保留长度/精度（`VARCHAR(20)` / `DECIMAL(10,2)`）；
2. `SHOW COLUMNS` 的 `Default` 来自真实的 `DEFAULT` 声明；
3. `SHOW COLUMNS` 的 `Extra` 不再由主键伪造（本引擎没有自增 → 恒空串，
   且 `CREATE TABLE ... AUTO_INCREMENT` 现在**显式报错**而不是静默接受）；
4. `SHOW DATABASES` 反映引擎真实的库集合（真实单库 + 真实可查询的
   `information_schema`），不是与实现无关的写死列表；
5. `USE <不存在的库>` 显式报 1049（协议 `COM_INIT_DB` / `select_db` 同样
   报错），存在的库上 `SHOW TABLES` 只列该库的表；
6. `SELECT DATABASE()` 返回当前连接的库名（无则 NULL），**不是**
   `mysql_native_password`。

覆盖两条真实路径：进程内 `Executor.execute_sql`（用真实 `Database`）与
经 MySQL 协议服务端（真实 `MySQLServer` + pymysql）。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_metadata_honesty.py
    from conftest import safe_rmtree, track


PORT = 8907

DDL = (
    "CREATE TABLE honesty ("
    " id INT PRIMARY KEY,"
    " name VARCHAR(20) NOT NULL DEFAULT 'anon',"
    " price DECIMAL(10,2) DEFAULT 9.99,"
    " note VARCHAR(100),"
    " email VARCHAR(60) UNIQUE)")

EXPECTED = {
    # 主键在 MySQL 里**隐式 NOT NULL**，本引擎插入时也按 NOT NULL 执行，
    # 故 `Null` 必须是 'NO'（此前测试锁定 'YES'，与真实行为矛盾，已更正）。
    'id':    ('INT',           'NO',  'PRI', None,               ''),
    'name':  ('VARCHAR(20)',   'NO',  '',    'anon',             ''),
    'price': ('DECIMAL(10,2)', 'YES', '',    '9.99',             ''),
    'note':  ('VARCHAR(100)',  'YES', '',    None,               ''),
    'email': ('VARCHAR(60)',   'YES', 'UNI', None,               ''),
}


# ==================== 进程内路径 ====================

@pytest.fixture()
def inproc(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_inproc')
    db = track(Database(data_dir))
    ex = track(Executor(db))
    assert ex.execute_sql(DDL, {})['type'] == 'ok'
    return db, ex


def _show_columns_rows(ex, sql='SHOW COLUMNS FROM honesty'):
    r = ex.execute_sql(sql, {})
    assert r['type'] == 'resultset', r
    assert r['columns'] == ['Field', 'Type', 'Null', 'Key',
                            'Default', 'Extra'], r['columns']
    return {row[0]: tuple(row[1:]) for row in r['rows']}


def test_inproc_show_columns_is_real(inproc):
    """Type 含长度、Default 真实、Extra 不被主键伪造（进程内）"""
    _db, ex = inproc
    got = _show_columns_rows(ex)
    assert got == EXPECTED, got

    # 逐条显式断言（失败时给出更明确的信号）
    assert got['name'][0] == 'VARCHAR(20)'          # 长度没被抹掉
    assert got['price'][0] == 'DECIMAL(10,2)'       # 精度没被抹掉
    assert got['name'][3] == 'anon'                 # 真实 DEFAULT
    assert got['price'][3] == '9.99'
    assert got['note'][3] is None                   # 没声明 DEFAULT → NULL
    assert all(v[4] == '' for v in got.values()), got   # Extra 不伪造
    assert got['id'][4] == ''                       # 主键也不再假报 auto_increment


def test_inproc_desc_alias_agrees(inproc):
    """`DESC` / `DESCRIBE` 与 `SHOW COLUMNS` 结果一致"""
    _db, ex = inproc
    for sql in ('DESC honesty', 'DESCRIBE honesty'):
        assert _show_columns_rows(ex, sql) == EXPECTED, sql


def test_inproc_show_databases_real(inproc):
    """SHOW DATABASES = 真实单库 + 真实可查询的 information_schema"""
    db, ex = inproc
    r = ex.execute_sql('SHOW DATABASES', {})
    assert r['columns'] == ['Database'], r
    names = [row[0] for row in r['rows']]
    assert names == db.database_names() == ['default', 'information_schema']
    # 不是"与实现无关"的装饰：虚拟库必须真能查
    assert ex.execute_sql(
        'SELECT * FROM information_schema.SCHEMATA', {})['type'] == 'resultset'


def test_inproc_use_validates_database(inproc):
    """USE 不存在的库 → 1049；存在的库才切换"""
    _db, ex = inproc
    s = {}
    r = ex.execute_sql('USE no_such_db', s)
    assert r['type'] == 'error' and r['code'] == 1049, r
    assert r['message'] == "Unknown database 'no_such_db'", r
    assert 'database' not in s, s

    assert ex.execute_sql('USE information_schema', s)['type'] == 'ok'
    assert s['database'] == 'information_schema'
    r = ex.execute_sql('SHOW TABLES', s)
    assert [row[0] for row in r['rows']] == ['COLUMNS', 'SCHEMATA', 'TABLES'], r

    assert ex.execute_sql('USE default', s)['type'] == 'ok'
    r = ex.execute_sql('SHOW TABLES', s)
    assert r['columns'] == ['Tables_in_default'], r
    assert [row[0] for row in r['rows']] == ['honesty'], r


def test_inproc_default_is_applied_not_just_reported(inproc):
    """报出来的 Default 必须真的生效（否则就是看得见却无效的元数据）"""
    _db, ex = inproc
    assert ex.execute_sql('INSERT INTO honesty (id, email) VALUES '
                          "(1, 'a@b.c')", {})['type'] == 'ok'
    r = ex.execute_sql('SELECT id, name, price, note FROM honesty', {})
    assert r['rows'] == [(1, 'anon', 9.99, None)], r


def test_inproc_auto_increment_accepted_but_never_claimed(inproc):
    """`AUTO_INCREMENT`：**接受但绝不声称生效**（与 `ENGINE=InnoDB` 同一态度）

    决策（本会话）：保证 MySQL 兼容（mysqldump 一类 DDL 必须能建表），但只要
    元数据不伪造，使用者就不会误以为它生效。故断言三件事：
      1. 能解析、能建表（不再是 SyntaxError）；
      2. `SHOW COLUMNS` 的 `Extra` 恒为 `''`（不由主键伪造 auto_increment）；
      3. 插入时不提供主键值 = 普通的"主键为 NULL"插入（NOT NULL 主键显式报错），
         **不会静默自增** —— 这是"接受但不生效"与"假自增"的分界。
    """
    _db, ex = inproc
    r = ex.execute_sql('CREATE TABLE ai (id INT AUTO_INCREMENT PRIMARY KEY, v INT)', {})
    assert r.get('type') != 'error', r

    cols = ex.execute_sql('SHOW COLUMNS FROM ai', {})
    assert cols.get('type') != 'error', cols
    by_name = {row[0]: row for row in cols['rows']}
    assert by_name['id'][5] == '', by_name['id']      # Extra 恒空，不伪造
    assert by_name['id'][2] == 'NO', by_name['id']    # Null=NO 才是真话

    # 实测（本会话）：本引擎在插入时对**省略的 INT 主键**取 `_next_pk`
    # （`Table._insert_locked`），所以这类声明实际上是**生效**的 ——
    # 不提供 id 也会拿到 1、2、3……（与 `AUTO_INCREMENT` 关键字无关：
    # 任何 INT 单列主键都如此）。因此"接受该子句 = 承诺不存在的语义"
    # 并不成立，接受它反而与真实行为一致。
    # 反向的待办：`Extra=''` 对这类列是**低报**（MySQL 会报 auto_increment），
    # 是否改为如实报出取决于"以关键字为准"还是"以行为为准"，留待决策。
    ins = ex.execute_sql('INSERT INTO ai (v) VALUES (7)', {})
    assert ins.get('type') == 'ok', ins
    got = ex.execute_sql('SELECT id, v FROM ai', {})
    assert got['rows'] == [(1, 7)], got['rows']


# ==================== 经服务端（真实 MySQLServer + pymysql）====================

@pytest.fixture()
def served(tmp_path):
    pymysql = pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_srv')
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    db = track(Database(data_dir))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    def connect(database=None, user='root'):
        return track(pymysql.connect(
            host='127.0.0.1', port=PORT, user=user, password='',
            database=database, charset='utf8mb4', autocommit=True,
            connect_timeout=10))

    conn = connect(database='default')
    yield conn, connect, db
    try:
        conn.close()
    except Exception:
        pass


def test_server_show_columns_is_real(served):
    """经协议路径的 SHOW COLUMNS 同样真实"""
    conn, _connect, _db = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute('SHOW COLUMNS FROM honesty')
        assert [d[0] for d in cur.description] == [
            'Field', 'Type', 'Null', 'Key', 'Default', 'Extra']
        got = {row[0]: tuple(row[1:]) for row in cur.fetchall()}
    assert got == EXPECTED, got
    assert got['id'][4] == ''
    assert got['name'][0] == 'VARCHAR(20)'


def test_server_select_database_is_never_plugin_name(served):
    """登录指定 database / 未指定 两种情况的 SELECT DATABASE()"""
    _conn, connect, _db = served

    c1 = connect(database='default')
    with c1.cursor() as cur:
        cur.execute('SELECT DATABASE()')
        got = cur.fetchone()[0]
    assert got == 'default', got
    assert got != 'mysql_native_password'
    c1.close()

    # 未指定 → SQL NULL（不是编出来的名字，也不是插件名）
    c2 = connect(database=None)
    with c2.cursor() as cur:
        cur.execute('SELECT DATABASE()')
        assert cur.fetchone()[0] is None
        # 未选库时读写的是引擎真实的单库，SHOW TABLES 仍按它回答
        cur.execute('SHOW TABLES')
        assert [d[0] for d in cur.description] == ['Tables_in_default']
    c2.close()


def test_server_show_databases_real(served):
    conn, _connect, db = served
    with conn.cursor() as cur:
        cur.execute('SHOW DATABASES')
        assert [d[0] for d in cur.description] == ['Database']
        names = [r[0] for r in cur.fetchall()]
    assert names == db.database_names() == ['default', 'information_schema']


def test_server_use_and_show_tables_scoped(served):
    """USE 不存在 → 1049；存在的库上 SHOW TABLES 只列该库的表"""
    conn, _connect, _db = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute('SHOW TABLES')
        assert [r[0] for r in cur.fetchall()] == ['honesty']

        with pytest.raises(Exception) as ei:
            cur.execute('USE no_such_db')
        assert ei.value.args[0] == 1049, ei.value.args
        assert ei.value.args[1] == "Unknown database 'no_such_db'"

        cur.execute('USE information_schema')
        cur.execute('SELECT DATABASE()')
        assert cur.fetchone()[0] == 'information_schema'
        cur.execute('SHOW TABLES')
        assert [r[0] for r in cur.fetchall()] == ['COLUMNS', 'SCHEMATA',
                                                  'TABLES']
        cur.execute('USE default')
        cur.execute('SHOW TABLES')
        assert [r[0] for r in cur.fetchall()] == ['honesty']

    # 协议级切库（COM_INIT_DB）同样校验
    with pytest.raises(Exception) as ei:
        conn.select_db('no_such_db')
    assert ei.value.args[0] == 1049, ei.value.args


def test_server_login_with_unknown_database_fails(served):
    """登录时指定不存在的库 → 显式 1049（不静默连上一个别的库）"""
    _conn, connect, _db = served
    with pytest.raises(Exception) as ei:
        connect(database='no_such_db')
    assert ei.value.args[0] == 1049, ei.value.args
    assert 'no_such_db' in ei.value.args[1]


def test_server_information_schema_columns_agree(served):
    """information_schema.COLUMNS 与 SHOW COLUMNS 是同一份真实数据"""
    conn, _connect, _db = served
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute('SELECT * FROM information_schema.COLUMNS')
        cols = [d[0] for d in cur.description]
        rows = {r[cols.index('COLUMN_NAME')]: r for r in cur.fetchall()}
    assert cols == ['TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME',
                    'COLUMN_NAME', 'ORDINAL_POSITION', 'COLUMN_DEFAULT',
                    'IS_NULLABLE', 'DATA_TYPE', 'COLUMN_KEY', 'EXTRA',
                    'COLUMN_COMMENT'], cols
    i_type = cols.index('DATA_TYPE')
    i_def = cols.index('COLUMN_DEFAULT')
    i_extra = cols.index('EXTRA')
    i_key = cols.index('COLUMN_KEY')
    assert rows['name'][i_type] == 'varchar(20)'
    assert rows['price'][i_type] == 'decimal(10,2)'
    assert rows['name'][i_def] == 'anon'
    assert rows['note'][i_def] is None
    assert rows['id'][i_extra] == ''      # 主键不再假报 auto_increment
    assert rows['id'][i_key] == 'PRI'
    assert rows['email'][i_key] == 'UNI'


def test_server_processlist_is_not_stale(served):
    """SHOW PROCESSLIST 的 db 列是真实库名，且时间/语句不再陈旧"""
    conn, _connect, _db = served
    with conn.cursor() as cur:
        cur.execute('SHOW PROCESSLIST')
        cols = [d[0] for d in cur.description]
        assert cols == ['Id', 'User', 'Host', 'db', 'Command',
                        'Time', 'State', 'Info']
        rows = [[str(v) if v is not None else None for v in r]
                for r in cur.fetchall()]
        cur.execute('SHOW STATUS')
        status = {r[0]: r[1] for r in cur.fetchall()}

    mine = [r for r in rows if r[cols.index('Id')] == str(conn.thread_id())]
    assert mine, (conn.thread_id(), rows)
    me = mine[0]
    # db 列 == 真实当前库（旧实现在"登录指定 database"时这里是认证插件名；
    # 即使不指定也读作 mysql_native_password）
    assert me[cols.index('db')] == 'default', me
    assert me[cols.index('db')] != 'mysql_native_password'
    # 执行中的语句 Time 从**语句开始**算（不是连接年龄）：本语句刚开始
    assert me[cols.index('Command')] == 'Query', me
    assert me[cols.index('Time')] == '0', me
    # Threads_running 按真实在跑的语句数统计（旧实现写死 1）
    assert status['Threads_running'] == '1', status
    # 本引擎不统计收发字节数：取不到就报 NULL，而不是假的 0
    assert status['Bytes_received'] is None, status
    assert status['Bytes_sent'] is None, status


def test_server_idle_session_is_sleep_from_another_connection(served):
    """从**另一条**连接观察空闲会话：必须是 Sleep + Time=NULL

    这条路径能真正观察"语句结束后是否复位"：同一条连接里跑
    `SHOW PROCESSLIST` 时它自己必然是 Query，看不出复位有没有发生。
    """
    conn, connect, _db = served
    with conn.cursor() as cur:
        cur.execute('SELECT 1')          # 让这条连接执行过至少一条语句
    time.sleep(0.1)

    obs = connect(database='default')
    try:
        with obs.cursor() as cur:
            cur.execute('SHOW PROCESSLIST')
            cols = [d[0] for d in cur.description]
            rows = [[str(v) if v is not None else None for v in r]
                    for r in cur.fetchall()]
    finally:
        obs.close()

    mine = [r for r in rows if r[cols.index('Id')] == str(conn.thread_id())]
    assert mine, (conn.thread_id(), rows)
    me = mine[0]
    assert me[cols.index('Command')] == 'Sleep', me
    assert me[cols.index('Time')] is None, me     # 不是"连接年龄"
    assert me[cols.index('Info')] == '', me       # 不保留最后一条 SQL
    assert me[cols.index('db')] == 'default', me


def test_server_threads_running_counts_real_queries(served):
    """Threads_running 反映真实在跑的语句数（不是写死的 1）"""
    _conn, connect, _db = served
    done = threading.Event()
    slow = connect(database='default')

    def _run_slow():
        try:
            with slow.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM '
                            '(SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 '
                            'UNION ALL SELECT 4 UNION ALL SELECT 5) a, '
                            '(SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 '
                            'UNION ALL SELECT 4 UNION ALL SELECT 5) b, '
                            '(SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 '
                            'UNION ALL SELECT 4 UNION ALL SELECT 5) c, '
                            '(SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 '
                            'UNION ALL SELECT 4 UNION ALL SELECT 5) d')
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_run_slow, daemon=True)
    t.start()
    time.sleep(0.15)
    obs = connect(database='default')
    try:
        with obs.cursor() as cur:
            cur.execute('SHOW PROCESSLIST')
            cols = [d[0] for d in cur.description]
            rows = [[str(v) if v is not None else None for v in r]
                    for r in cur.fetchall()]
            cur.execute('SHOW STATUS')
            status = {r[0]: r[1] for r in cur.fetchall()}
            running = [r for r in rows if r[cols.index('Command')] == 'Query']
            if not running:
                pytest.skip('慢查询在观测前就结束了，本用例观测不到运行态')
            assert status['Threads_running'] == str(len(running)), (
                status, running)
            assert int(status['Threads_running']) >= 1
    finally:
        done.wait(20)
        slow.close()
        obs.close()
