"""协议层事务真实性锁定测试（P1-B：连接级 session / 断连回滚 / SET / SQLSTATE）

修复前（见 docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md §B）经 MySQL 协议的
事务是**假的**：`lightdb/net/server.py` 每条语句都新建一个临时 session dict，
`BEGIN` 写进去的 `txn` 下一条语句就丢 —— 表现是：

* `BEGIN; INSERT; ROLLBACK` 不回滚，未提交行对其它连接立即可见；
* 断连不回滚，`MVCCManager.active_txns` 只增不减（vacuum horizon 被钉住）；
* `SET autocommit=0` / `SET NAMES ...` 一律只回 OK、毫无效果；
* 所有错误的 SQLSTATE 硬编码 `HY000`（表不存在与未知列被压成同一类）。

本文件用**真实 MySQLServer + pymysql**（进程内路径已由 test_txn_atomicity /
test_mvcc_persistence 覆盖）锁定修复后的行为，包括直接断言 ERR 包里的
SQLSTATE 字节（不只看 errno）。
"""
import os
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_wire_transaction.py
    from conftest import track


PORT = 8936
TABLE = 'wire_txn'


# ==================== 工具 ====================

def fetch(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def count(conn, table=TABLE):
    return fetch(conn, f'SELECT COUNT(*) FROM {table}')[0][0]


def wait_until(predicate, timeout=5.0, interval=0.02):
    """轮询等待 predicate() 为真（用于观察异步的断连/回滚后果）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def raw_error_packet(conn, sql):
    """发送 COM_QUERY 并原样返回第一个响应包的 payload（不经过 pymysql 异常层）

    pymysql 的异常只保证暴露 errno，不同版本对 SQLSTATE 的保留策略不同；
    这里直接读协议字节，断言 ERR 包里的 SQLSTATE 字段 —— 这才是客户端
    真正收到的东西（MySQL ERR 包格式：0xFF + errno(2) + '#' + sqlstate(5) + message）。
    """
    from pymysql.constants import COMMAND
    conn._execute_command(COMMAND.COM_QUERY, sql)
    header = conn._read_bytes(4)
    length = header[0] | (header[1] << 8) | (header[2] << 16)
    return conn._read_bytes(length)


def parse_err_packet(payload):
    """解析 ERR 包 → (errno, sqlstate, message)"""
    assert payload and payload[0] == 0xFF, payload[:32]
    errno = struct.unpack('<H', payload[1:3])[0]
    assert payload[3:4] == b'#', payload[:32]
    sqlstate = payload[4:9].decode('ascii')
    message = payload[9:].decode('utf-8', errors='replace')
    return errno, sqlstate, message


# ==================== fixture：真实服务端 + pymysql ====================

@pytest.fixture()
def served(tmp_path):
    pymysql = pytest.importorskip('pymysql')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data_dir = str(tmp_path / 'data_wire_txn')
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    db = track(Database(data_dir))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    opened = []

    def connect(database='default', autocommit=True):
        conn = track(pymysql.connect(
            host='127.0.0.1', port=PORT, user='root', password='',
            database=database, charset='utf8mb4',
            autocommit=autocommit, connect_timeout=10))
        opened.append(conn)
        return conn

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            f'CREATE TABLE {TABLE} (id INT PRIMARY KEY, v VARCHAR(20))')
    yield conn, connect, db
    for c in opened:
        try:
            c.close()
        except Exception:
            pass


# ==================== 1. 显式事务跨语句生效 ====================

def test_begin_insert_rollback_reverts_row(served):
    """`BEGIN; INSERT; ROLLBACK`：本连接与**另一连接**都必须看到 0 行

    修复前：ROLLBACK 在新 session 里执行、找不到 txn → 静默 no-op，
    插入的行以 autocommit 提交且立刻对其它连接可见。
    """
    conn, connect, db = served
    other = connect()

    baseline_active = db.mvcc.stats()['active']
    conn.begin()
    fetch(conn, f"INSERT INTO {TABLE} VALUES (1, 'rollback-me')")
    conn.rollback()

    # ROLLBACK 必须真正释放事务（MVCC active 回落），而不只是回 OK
    assert db.mvcc.stats()['active'] == baseline_active, (
        db.mvcc.stats(), baseline_active)

    assert count(conn) == 0, '本连接在 ROLLBACK 后仍看到被回滚的行'
    assert count(other) == 0, '另一连接看到了被回滚的行'
    other.close()


def test_begin_insert_commit_persists_row(served):
    """`BEGIN; INSERT; COMMIT`：两连接都看到 1 行"""
    conn, connect, _db = served
    other = connect()

    conn.begin()
    fetch(conn, f"INSERT INTO {TABLE} VALUES (1, 'commit-me')")
    conn.commit()

    assert fetch(conn, f'SELECT id, v FROM {TABLE}') == ((1, 'commit-me'),)
    assert fetch(other, f'SELECT id, v FROM {TABLE}') == ((1, 'commit-me'),)
    other.close()


def test_uncommitted_rows_invisible_to_other_connection(served):
    """未提交行只对**本连接**可见，其它连接必须看不到（隔离性真实）"""
    conn, connect, _db = served
    other = connect()

    conn.begin()
    fetch(conn, f"INSERT INTO {TABLE} VALUES (7, 'uncommitted')")

    # 本连接 read-your-writes
    assert count(conn) == 1, '本事务看不到自己刚插入的行'
    # 另一连接看不到未提交版本（修复前它看到 1 —— 因为是 autocommit 提交）
    assert count(other) == 0, '未提交行对其它连接可见（事务隔离是假的）'

    conn.rollback()
    assert count(conn) == 0
    assert count(other) == 0
    other.close()


# ==================== 2. SET autocommit 真实生效 ====================

def test_set_autocommit_off_rollback_and_commit(served):
    """`SET autocommit=0`：DML 进入隐式事务，ROLLBACK 回滚 / COMMIT 提交"""
    conn, connect, _db = served
    other = connect()

    fetch(conn, 'SET autocommit = 0')
    assert str(fetch(conn, 'SELECT @@autocommit')[0][0]) == '0', \
        'SET autocommit=0 后 @@autocommit 仍报 1（SET 只是 ACK）'
    show = dict(fetch(conn, "SHOW VARIABLES LIKE 'autocommit'"))
    assert show.get('autocommit') == 'OFF', show

    # 第一条 DML 应自动进入事务：本连接可见、其它连接不可见
    fetch(conn, f"INSERT INTO {TABLE} VALUES (10, 'ac0-rollback')")
    assert count(conn) == 1
    assert count(other) == 0, 'SET autocommit=0 后 DML 仍立即提交（未生效）'

    conn.rollback()                                # 结束隐式事务
    assert count(conn) == 0, 'autocommit=0 下 ROLLBACK 没有回滚隐式事务'
    assert count(other) == 0

    # 下一次 DML 重新隐式开事务；COMMIT 后对所有连接可见
    fetch(conn, f"INSERT INTO {TABLE} VALUES (11, 'ac0-commit')")
    conn.commit()
    assert count(other) == 1
    assert fetch(other, f'SELECT id, v FROM {TABLE}') == ((11, 'ac0-commit'),)

    # 恢复自动提交后，DML 不 COMMIT 也应立即可见
    fetch(conn, 'SET autocommit = 1')
    assert str(fetch(conn, 'SELECT @@autocommit')[0][0]) == '1'
    show = dict(fetch(conn, "SHOW VARIABLES LIKE 'autocommit'"))
    assert show.get('autocommit') == 'ON', show
    fetch(conn, f"INSERT INTO {TABLE} VALUES (12, 'ac1')")
    assert count(other) == 2, 'SET autocommit=1 后 DML 未自动提交'
    other.close()


# ==================== 3. 断连回滚（不泄漏 MVCC active） ====================

def test_disconnect_rolls_back_and_releases_mvcc(served):
    """未提交事务随连接断开自动回滚，且 MVCC active 回落（不泄漏）

    这是原缺陷里代价最大的一条：`BEGIN` 后直接断连的连接会让
    `MVCCManager.active_txns` 永久增加，vacuum horizon 被钉死。
    用"不发 COM_QUIT 直接关 socket"模拟真实异常断连（客户端崩溃/网络断）。
    """
    conn, connect, db = served
    observer = connect()
    fetch(conn, f"INSERT INTO {TABLE} VALUES (1, 'committed-seed')")
    assert count(observer) == 1

    baseline = db.mvcc.stats()['active']
    dying = connect()
    dying.begin()
    fetch(dying, f"INSERT INTO {TABLE} VALUES (2, 'lost-on-disconnect')")
    assert db.mvcc.stats()['active'] == baseline + 1, (
        'BEGIN 后 MVCC active 没有登记事务', db.mvcc.stats())

    # 异常断连：不发送 COM_QUIT，直接关客户端 socket（服务端读侧得到 EOF）
    dying._force_close()

    assert wait_until(
        lambda: db.mvcc.stats()['active'] == baseline), (
            '断连后 MVCC active 未回落（事务/行锁泄漏）', db.mvcc.stats())

    # 未提交行必须不可见（回滚真的发生了，不是只清了计数）
    assert fetch(observer, f'SELECT id, v FROM {TABLE}') == (
        (1, 'committed-seed'),), '断连的未提交行留在表里'
    observer.close()


# ==================== 4. BEGIN 对残留事务隐式提交（不泄漏） ====================

def test_begin_implicitly_commits_previous_transaction(served):
    """START TRANSACTION/BEGIN 在 MySQL 里会隐式提交上一个事务

    修复前执行器 `_cmd_begin` 直接覆盖 `s['txn']`（旧事务句柄丢失 →
    MVCC active/行锁泄漏）；现在服务端在转发前先提交残留事务。
    """
    conn, connect, db = served
    observer = connect()

    baseline = db.mvcc.stats()['active']
    conn.begin()
    fetch(conn, f"INSERT INTO {TABLE} VALUES (20, 'first-txn')")
    conn.begin()                    # 隐式提交第一个事务，再开新的
    assert db.mvcc.stats()['active'] == baseline + 1, (
        '重复 BEGIN 后保留了两个活跃事务（旧事务泄漏）', db.mvcc.stats())

    conn.rollback()                 # 只回滚第二个（空）事务
    assert db.mvcc.stats()['active'] == baseline, db.mvcc.stats()
    # 第一个事务的数据已被隐式提交
    assert fetch(observer, f'SELECT id, v FROM {TABLE}') == (
        (20, 'first-txn'),)
    observer.close()


# ==================== 5. SET NAMES 被记录并能被观察到 ====================

def test_set_names_is_recorded_and_observable(served):
    """`SET NAMES latin1` 后 `SELECT @@character_set_client` / SHOW VARIABLES 反映"""
    conn, _connect, _db = served

    fetch(conn, 'SET NAMES latin1')
    assert str(fetch(conn, 'SELECT @@character_set_client')[0][0]) == 'latin1'
    show = dict(fetch(conn, "SHOW VARIABLES LIKE 'character_set_client'"))
    assert show.get('character_set_client') == 'latin1', show

    fetch(conn, 'SET NAMES utf8mb4 COLLATE utf8mb4_general_ci')
    assert str(fetch(conn, 'SELECT @@character_set_client')[0][0]) == 'utf8mb4'
    show = dict(fetch(conn, "SHOW VARIABLES LIKE 'collation_connection'"))
    assert show.get('collation_connection') == 'utf8mb4_general_ci', show


# ==================== 6. 用户变量随连接级 session 保持（且按连接隔离） ====================

def test_set_user_variable_persists_across_statements(served):
    """`SET @x = ...` 写在连接级 session 里：后续语句读得到，别的连接读不到

    修复前每条语句新建 session，`SET @x` 的结果无处可存（而且服务端对
    `SELECT @x` 只回 OK 包），经协议的用户变量完全不可用。
    """
    conn, connect, _db = served
    other = connect()

    fetch(conn, 'SET @answer = 42')
    assert fetch(conn, 'SELECT @answer') == ((42,),)
    # 另一条连接有自己的 session：不能看到本连接设的用户变量
    assert fetch(other, 'SELECT @answer') == ((None,),)
    other.close()


# ==================== 7. SQLSTATE 透传（直接读 ERR 包字节） ====================

def test_sqlstate_passthrough_in_err_packet(served):
    """错误回包不再一律 HY000：1146→42S02、1062→23000、1064→42000

    直接断言 ERR 包里的 SQLSTATE 字段（'#' 后 5 字节），而不是只看 errno：
    修复前 errno 本来就是对的部分，假的是 SQLSTATE 恒 'HY000'。
    """
    conn, _connect, _db = served

    errno, sqlstate, _msg = parse_err_packet(
        raw_error_packet(conn, 'SELECT * FROM no_such_table'))
    assert (errno, sqlstate) == (1146, '42S02')

    fetch(conn, 'CREATE TABLE wire_dup (id INT PRIMARY KEY)')
    fetch(conn, 'INSERT INTO wire_dup VALUES (1)')
    errno, sqlstate, _msg = parse_err_packet(
        raw_error_packet(conn, 'INSERT INTO wire_dup VALUES (1)'))
    assert (errno, sqlstate) == (1062, '23000')

    errno, sqlstate, _msg = parse_err_packet(
        raw_error_packet(conn, 'THIS IS NOT SQL'))
    assert errno == 1064 and sqlstate == '42000', (errno, sqlstate)

    # 连接在直接读原始 ERR 包之后仍然可用（协议状态没有被读坏）
    assert fetch(conn, 'SELECT 1') == ((1,),)
    assert str(fetch(conn, 'SELECT @@autocommit')[0][0]) == '1'
