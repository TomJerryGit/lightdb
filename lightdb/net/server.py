"""
LightDB MySQL 兼容服务器（修复版）

修复:
- 全局会话表线程安全（遍历加锁）
- 列类型推断（根据实际值返回正确 MYSQL_TYPE）
- 每会话 connected_at
- SHOW STATUS / SHOW VARIABLES / SHOW PROCESSLIST / KILL
- 查询语句识别更严谨
"""
import os
import re
import socket
import socketserver
import struct
import threading
import time
import weakref
from datetime import datetime
from typing import Optional

from ..config import Config
from ..logger import logger
# `SHOW COLUMNS` 的列元数据来自与 information_schema / 进程内执行器**同一份**
# 实现（db/database.py），避免三条路径各编一套元数据。
from ..db.database import describe_columns
from . import mysql_protocol as mp


#: 登录失败后的固定延迟（秒）。它让暴力破解/口令扫描变慢，同时不改变
#: 正确登录路径的耗时。0.5s 足够拖垮在线爆破，又不会影响人的交互体验。
LOGIN_FAILURE_DELAY = 0.5


#: 错误码 → SQLSTATE 兜底映射。
#:
#: 执行器只在部分路径（DDL / CHECK 约束，见 `sql/executor.py::_err`）上带
#: `sqlstate` 字段；其余错误只有 errno。旧实现把**所有**错误回包的 SQLSTATE
#: 硬编码成 'HY000'（通用错误），于是客户端拿不到 MySQL 语义分类：
#: 表不存在（42S02）与未知列（42S22）被说成同一类"未知错误"。
#: 这里按 MySQL 官方错误码表补齐常用错误的 SQLSTATE；未收录的一律 HY000。
_SQLSTATE_BY_ERRNO = {
    1048: '23000',   # ER_BAD_NULL_ERROR：列不允许 NULL
    1049: '42000',   # ER_BAD_DB_ERROR：未知数据库
    1054: '42S22',   # ER_BAD_FIELD_ERROR：未知列
    1062: '23000',   # ER_DUP_ENTRY：主键/唯一键冲突
    1064: '42000',   # ER_PARSE_ERROR：语法错误
    1105: 'HY000',   # ER_UNKNOWN_ERROR：通用错误
    1146: '42S02',   # ER_NO_SUCH_TABLE：表不存在
    1264: '22003',   # ER_WARN_DATA_OUT_OF_RANGE：数值越界
    1292: '22007',   # ER_TRUNCATED_WRONG_VALUE：日期/时间值非法
    1366: '22007',   # ER_WRONG_VALUE：取值不合法（类型转换失败）
    1406: '22001',   # ER_DATA_TOO_LONG：数据超长
}


def classify_sqlstate(code) -> str:
    """按 MySQL 语义把 errno 映射成 SQLSTATE（未知码按 'HY000' 兜底）"""
    try:
        return _SQLSTATE_BY_ERRNO.get(int(code), 'HY000')
    except (TypeError, ValueError):
        return 'HY000'


#: `SET <var> = <value>` 里本引擎**认可为会话级可写**的系统变量白名单。
#: 只把这些变量写进 `session['variables']` 并让 `SELECT @@x` / `SHOW VARIABLES`
#: 反映出来；不在名单里的（如只读的 @@version，或本引擎没有的变量）只记录
#: 原文到 `session['set_raw']`，不伪造效果 —— 否则 `SET @@version='x'` 会让
#: 客户端读到假版本号。
_SESSION_SETTABLE_VARS = frozenset({
    'character_set_client',
    'character_set_connection',
    'character_set_database',
    'character_set_results',
    'collation_connection',
    'collation_database',
    'default_storage_engine',
    'explicit_defaults_for_timestamp',
    'foreign_key_checks',
    'group_concat_max_len',
    'interactive_timeout',
    'max_allowed_packet',
    'net_read_timeout',
    'net_write_timeout',
    'optimizer_switch',
    'sql_mode',
    'sql_safe_updates',
    'sql_select_limit',
    'time_zone',
    'transaction_isolation',
    'transaction_read_only',
    'tx_isolation',
    'tx_read_only',
    'unique_checks',
    'wait_timeout',
})


# ==================== 会话管理 ====================

_sessions = {}          # server_id -> info
_sessions_lock = threading.Lock()
_next_server_id = [1]
_id_lock = threading.Lock()


def _alloc_server_id() -> int:
    with _id_lock:
        sid = _next_server_id[0]
        _next_server_id[0] += 1
        return sid


def _add_session(sid, info):
    with _sessions_lock:
        _sessions[sid] = info


def _remove_session(sid):
    with _sessions_lock:
        _sessions.pop(sid, None)


def _get_sessions_snapshot():
    with _sessions_lock:
        return dict(_sessions)


def _session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


# ==================== Handler ====================

class MySQLHandler(socketserver.BaseRequestHandler):
    """处理单个 MySQL 客户端连接"""

    def setup(self):
        self.sock = self.request
        try:
            self.sock.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.sock.settimeout(600)

        self.seq = 0
        self.user = None
        # 未指定库时为 None（`SELECT DATABASE()` 因此返回 SQL NULL）。
        # 旧实现默认 `"default"`，把"没选库"伪装成"选了 default"。
        self.database = None
        # ★ 连接级会话（P1-B）：**每条连接一个** session dict，不能每条语句
        # 新建。执行器把事务句柄写进 `session['txn']`（`_cmd_begin`）并在
        # `_cmd_commit/_cmd_rollback` 里弹出、在 INSERT/UPDATE/DELETE 里读取；
        # 旧实现每条语句都新建临时 dict，于是 BEGIN 写的 txn 下一条语句就丢，
        # `BEGIN; INSERT; ROLLBACK` 实际上每条语句各自 autocommit —— 回滚
        # 静默失效、未提交行对其它连接立即可见、断连还会泄漏 MVCC active。
        self.session = {
            'user': None,
            'database': self.database,
            'txn': None,
            # 会话级 autocommit（不是全局 self.db.autocommit）。
            # True = 每条 DML 自动提交；False = 首次 DML 时隐式开事务，
            # 直到显式 COMMIT/ROLLBACK（MySQL 语义）。
            'autocommit': True,
            # `SET` 语句写入的会话变量（SHOW VARIABLES / SELECT @@x 读它）
            'variables': {},
            # `SET @x = ...` 写入的用户变量（执行器 SELECT @x 读它）
            'user_vars': {},
            # 原样记录未识别的 SET 语句，便于排障（不参与语义）
            'set_raw': [],
        }
        self.server_id = _alloc_server_id()
        self.auth_seed = os.urandom(20)
        self.closed = False
        self.db = self.server.db
        self.executor = self.server.executor
        self.connected_at = time.time()

    # ==================== 包收发 ====================

    def _recv_exact(self, n: int) -> Optional[bytes]:
        data = b''
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except (socket.timeout, ConnectionResetError,
                    ConnectionAbortedError):
                # 客户端在握手前后直接断开（端口扫描/探活很常见）属于正常
                # 情况：按"连接结束"处理，不要让 traceback 刷屏。
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _read_packet(self):
        header = self._recv_exact(4)
        if not header:
            return None
        length = struct.unpack('<I', header[:3] + b'\x00')[0]
        self.seq = (header[3] + 1) & 0xff
        if length == 0:
            return b''
        return self._recv_exact(length)

    def _send_packet(self, payload: bytes):
        pkt = mp.build_packet(payload, self.seq)
        try:
            self.sock.sendall(pkt)
        except (BrokenPipeError, ConnectionResetError):
            self.closed = True
            return
        self.seq = (self.seq + 1) & 0xff

    # ==================== 握手 ====================

    def do_handshake(self) -> bool:
        handshake = mp.make_handshake_v10(
            self.server_id, self.auth_seed)
        self._send_packet(handshake)

        response_data = self._read_packet()
        if not response_data:
            return False

        try:
            resp = mp.HandshakeResponse(response_data)
        except Exception as e:
            logger.warn(f"握手包解析失败: {e}")
            return False

        self.user = resp.username
        # 连接级会话的 user 与真实登录身份一致（审计 / SHOW PROCESSLIST 用）
        self.session['user'] = self.user

        # 用户校验
        user_info = self.db.users.get(self.user)
        if not user_info:
            return self._auth_failure(
                1045, "28000",
                f"Access denied for user '{self.user}'@"
                f"'{self.client_address[0]}'")

        # 密码校验
        stored_hash = b''
        if user_info.get('auth_hash'):
            try:
                stored_hash = bytes.fromhex(
                    user_info['auth_hash'])
            except Exception:
                stored_hash = b''

        if not mp.mysql_native_password(
                resp.auth_response, self.auth_seed, stored_hash):
            return self._auth_failure(
                1045, "28000",
                f"Access denied for user '{self.user}'@"
                f"'{self.client_address[0]}' "
                f"(using password: YES)")

        if resp.database:
            # 登录时指定的 database 必须**校验**：不存在的库要显式报错，
            # 否则客户端以为连上了那个库（`SELECT DATABASE()` 又会报出
            # 一个不存在的名字，或旧实现那种错位值）。
            resolved, err = self._resolve_database(resp.database)
            if err is not None:
                self._send_packet(err)
                return False
            self.database = resolved
            self.session['database'] = resolved
            # 会话表里的 db 列与真实会话状态保持一致
            self._update_session(db=resolved)

        self._send_packet(self._ok_packet())
        logger.info(
            f"用户 {self.user} 从 {self.client_address[0]} "
            f"登录成功 (sid={self.server_id})")
        return True

    def _auth_failure(self, code: int, sql_state: str, message: str):
        """登录失败：回 ERR 包 + WARN 日志 + 固定小延迟（抗暴力破解）。

        延迟只发生在失败路径（`LOGIN_FAILURE_DELAY`），正确登录的耗时
        完全不变；日志里带来源 IP，方便发现口令扫描。
        """
        self._send_packet(mp.make_err_packet(code, sql_state, message))
        logger.warn(
            f"登录失败 {self.user or '?'}@{self.client_address[0]} "
            f"(sid={self.server_id}): {message[:160]}")
        try:
            time.sleep(LOGIN_FAILURE_DELAY)
        except Exception:                     # pragma: no cover - 防御
            pass
        return False

    # ==================== 主循环 ====================

    def handle(self):
        if not self.do_handshake():
            return

        _add_session(self.server_id, {
            'user': self.user,
            'host': self.client_address,
            'start': self.connected_at,
            'db': self.database,
            'command': 'Sleep',
            # KILL 必须能真正触达目标连接：只登记元数据的话，
            # `_handle_kill` 除了谎报"已终止"什么也做不了。
            'sock': self.sock,
            'handler': self,
        })

        try:
            while not self.closed:
                packet = self._read_packet()
                if packet is None:
                    break
                if len(packet) == 0:
                    continue
                self._dispatch(packet)
        except (socket.timeout, ConnectionResetError,
                ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            logger.error(f"会话异常 (sid={self.server_id}): {e}")
        finally:
            # ★ 断连回滚（P1-B）：连接上还挂着的未提交事务必须真正回滚。
            # 否则执行器里 `Database.begin()` 登记的 MVCC active 事务永不释放
            # （vacuum horizon 被永久钉住，空间/内存只增不减），而且已写入
            # 的未提交行会留在存储层。MySQL 的语义正是"连接断开 = 未提交事务
            # 回滚"，这里用 `db.abort(txn)` 走与 ROLLBACK 相同的物理撤销路径。
            self._rollback_session_txn()
            self._clear_connection_session()
            _remove_session(self.server_id)
            logger.info(
                f"用户 {self.user} 断开连接 "
                f"(sid={self.server_id})")

    # ==================== 已知库解析 ====================

    def _database_names(self):
        """引擎真实支持的库名列表（`SHOW DATABASES` 的依据）

        取自 `Database.database_names()`（真实单库 + 真实可查询的
        `information_schema`）；多进程下的 `RemoteDB` 也实现了同一个方法。
        """
        try:
            return list(self.db.database_names())
        except Exception as e:               # pragma: no cover - 防御
            logger.warn(f"读取库列表失败: {e}")
            return []

    def _current_database(self):
        """当前会话的库：`(规范名, None)`；未选/已失效则 `(None, ERR)`

        未选库时（登录未指定 database 且没执行过 `USE`）用引擎自己的
        单库名兜底 —— 那是这张连接**真的**在读写的库，不是编出来的。
        """
        if not self.database:
            names = self._database_names()
            if not names:
                return None, mp.make_err_packet(
                    1049, "42000", "No database selected")
            return names[0], None
        resolved, err = self._resolve_database(self.database)
        return (resolved, None) if err is None else (None, err)

    def _visible_tables(self, db_name):
        """给定库下可见的表（真实表 + `information_schema` 虚拟表）"""
        try:
            return list(self.db.visible_table_names(db_name))
        except Exception as e:                # pragma: no cover - 防御
            logger.warn(f"读取表列表失败 ({db_name}): {e}")
            return []

    def _resolve_database(self, name):
        """把客户端给的库名解析成**引擎真实存在**的库名

        返回 `(规范名, None)`；库不存在时返回 `(None, ERR 包)`（1049，
        与 MySQL 的 `Unknown database 'x'` 同码同文）。

        依据是 `Database.database_names()`（真实单库 + 真实可查询的
        `information_schema`），不是写死的字符串比较——库名换了、模型
        变了，这里跟着变。
        """
        known = []
        try:
            known = list(self.db.database_names())
        except Exception:                    # pragma: no cover - 防御
            known = []
        want = str(name or '').strip()
        for real in known:
            if want.lower() == str(real).lower():
                return real, None
        return None, mp.make_err_packet(
            1049, "42000", f"Unknown database '{want}'")

    # ==================== 命令分发 ====================

    def _dispatch(self, packet: bytes):
        cmd = packet[0]
        body = packet[1:]

        if cmd == mp.COM_QUIT:
            self.closed = True

        elif cmd == mp.COM_PING:
            self._send_packet(self._ok_packet())

        elif cmd == mp.COM_INIT_DB:
            db_name = body.decode('utf-8', errors='replace')
            # `COM_INIT_DB` 就是 `USE <db>` 的协议形态。旧实现直接赋值 +
            # 回 OK，切到不存在的库也"成功"（pymysql 的
            # `connection.select_db('no_such_db')` 因此静默通过）。
            resolved, err = self._resolve_database(db_name)
            if err is not None:
                self._send_packet(err)
                return
            self.database = resolved
            # 连接级会话与 handler 状态保持一致（`USE` 语句路径同样会同步）
            self.session['database'] = resolved
            self._update_session(db=resolved)
            self._send_packet(self._ok_packet())

        elif cmd == mp.COM_QUERY:
            sql = body.decode('utf-8', errors='replace')
            self._handle_query(sql)

        elif cmd == mp.COM_FIELD_LIST:
            self._handle_field_list(body)

        else:
            self._send_packet(mp.make_err_packet(
                1047, "08S01",
                f"Unknown command 0x{cmd:02x}"))

    # ==================== 查询处理 ====================

    def _handle_query(self, sql: str):
        """执行一条 SQL，并在**执行结束后**把会话状态复位

        复位很重要：`SHOW PROCESSLIST` 的 `Command` / `Info` / `Time` 必须
        反映"现在"的状态。旧实现只在语句开始时把 `Command` 设成 `Query`
        且从不复位，于是空闲连接永远显示 `Command=Query` + 最后一条 SQL，
        `Time` 又在涨（见 `_show_processlist`）——性能面板里"有查询在跑"
        是假的。这里用 try/finally 保证任何分支（含早返回/异常）都会复位。
        """
        self._update_session(command='Query', info=sql[:200],
                             stmt_started=time.time())
        try:
            self._handle_query_inner(sql)
        finally:
            self._update_session(command='Sleep', info='', stmt_started=None)

    def _handle_query_inner(self, sql: str):
        sql = sql.strip().rstrip(';').strip()
        if not sql:
            self._send_packet(self._ok_packet())
            return

        upper = sql.upper()

        # ---------- 内置查询 ----------
        if upper in ("SELECT 1", "SELECT 1 FROM DUAL"):
            self._send_resultset(
                [("1", mp.MYSQL_TYPE_LONGLONG, 1)],
                [(1,)])
            return

        if upper.startswith("SELECT VERSION()"):
            self._send_resultset(
                [("VERSION()", mp.MYSQL_TYPE_VAR_STRING, 64)],
                [("5.7.30-LightDB",)])
            return

        if upper.startswith("SELECT DATABASE()"):
            # 当前连接的库；**没有**选库时返回 SQL NULL（MySQL 语义）。
            # 旧实现在握手解析错位时会把这个值取成认证插件名
            # （`mysql_native_password`），那是明显的错值。
            self._send_resultset(
                [("DATABASE()", mp.MYSQL_TYPE_VAR_STRING, 64)],
                [(self.database,)])
            return

        if (upper.startswith("SELECT USER()") or
                upper.startswith("SELECT CURRENT_USER")):
            val = f"{self.user}@localhost"
            col = ("USER()" if "USER()" in upper
                   else "CURRENT_USER()")
            self._send_resultset(
                [(col, mp.MYSQL_TYPE_VAR_STRING, 64)],
                [(val,)])
            return

        if upper.startswith("SELECT CONNECTION_ID()"):
            self._send_resultset(
                [("CONNECTION_ID()", mp.MYSQL_TYPE_LONGLONG, 20)],
                [(self.server_id,)])
            return

        if upper.startswith("SELECT @@"):
            var = sql[8:].strip()
            value = self._get_system_variable(var)
            self._send_resultset(
                [(f"@@{var}", mp.MYSQL_TYPE_VAR_STRING, 255)],
                [(value,)])
            return

        if upper == "SHOW DATABASES":
            self._send_resultset(
                [("Database", mp.MYSQL_TYPE_VAR_STRING, 64)],
                [(name,) for name in self._database_names()])
            return

        if upper.startswith("SHOW TABLES"):
            # 按**当前库**过滤（旧实现直读 self.db.tables，与所选库无关）。
            # 库不存在时显式报 1049，而不是列别的库的表。
            db_name, err = self._current_database()
            if err is not None:
                self._send_packet(err)
                return
            rows = [(name,) for name in self._visible_tables(db_name)]
            self._send_resultset(
                [(f"Tables_in_{db_name}",
                  mp.MYSQL_TYPE_VAR_STRING, 64)],
                rows)
            return

        if upper.startswith("SHOW VARIABLES"):
            self._show_variables(sql)
            return

        if upper.startswith("SHOW STATUS"):
            self._show_status()
            return

        if upper.startswith("SHOW PROCESSLIST"):
            self._show_processlist()
            return

        if upper.startswith("KILL "):
            self._handle_kill(sql)
            return

        if (upper.startswith("SHOW COLUMNS") or
                upper.startswith("DESC ") or
                upper.startswith("DESCRIBE ")):
            self._show_columns(sql)
            return

        # ---------- 会话设置语句 ----------
        # 标准 MySQL 客户端在登录成功后固定发送 `SET NAMES <charset>`
        # （字符集协商）以及 `SET AUTOCOMMIT = 1`（pymysql 的 autocommit 参数），
        # 这些必须真实写进连接级会话，不能只 ACK：
        # 旧实现对任何 `SET ...` 一律回 OK，于是 `SET autocommit=0` 看起来
        # 成功却毫无效果 —— 客户端以为自己在事务里，服务端每条 DML 各自提交。
        if upper == "SET" or upper.startswith("SET "):
            self._handle_set(sql)
            return

        # ---------- 通用 SQL ----------
        try:
            from ..sql.parser import parser
            parsed = parser.parse(sql)
        except Exception as e:
            self._send_packet(mp.make_err_packet(
                1064, "42000",
                f"SQL 解析错误: {str(e)[:200]}"))
            return

        if parsed is None:
            self._send_packet(mp.make_err_packet(
                1064, "42000",
                f"You have an error in your SQL syntax near "
                f"'{sql[:50]}'"))
            return

        # 事务边界预处理（autocommit=0 的隐式事务、BEGIN 的隐式提交）：
        # 必须在 execute 之前完成，且失败时不能把语句交给执行器。
        prep_err = self._prepare_transaction(parsed)
        if prep_err is not None:
            code, sqlstate, message = prep_err
            self._send_packet(mp.make_err_packet(
                code, sqlstate, message[:200]))
            return

        try:
            # ★ 复用连接级 session（不再是每条语句新建的临时 dict）：
            # BEGIN 写的 txn 因此能活到下一条语句；`s['database']` 的切库结果
            # 也自动保留。这是 P1-B 事务修复的核心。
            result = self.executor.execute(parsed, self.session)
        except Exception as e:
            logger.error(f"SQL执行错误: {e}")
            code = 1105
            self._send_packet(mp.make_err_packet(
                code, classify_sqlstate(code), str(e)[:200]))
            return

        # `USE <库>` 由执行器校验（不存在 → 1049）。成功时必须把会话状态
        # 同步回 handler：否则后续的 `SELECT DATABASE()` / `SHOW TABLES`
        # 仍按旧库回答 —— 与"切库成功"的回报直接矛盾。
        if parsed.get('command') == 'USE' and not (
                isinstance(result, dict)
                and result.get('type') == 'error'):
            self.database = (
                self.session.get('database') or self.database)
            self.session['database'] = self.database
            self._update_session(db=self.database)

        self._send_execution_result(result, parsed)

    def _update_session(self, **kwargs):
        with _sessions_lock:
            s = _sessions.get(self.server_id)
            if s:
                s.update(kwargs)

    # ==================== 连接级会话 / 事务生命周期 ====================

    def _session_autocommit(self) -> bool:
        """会话级 autocommit（缺省 True；不回退到全局 `db.autocommit`）"""
        session = getattr(self, 'session', None)
        if not isinstance(session, dict):
            return True
        return bool(session.get('autocommit', True))

    def _status_flags(self) -> int:
        """OK/EOF 包里的 server_status：autocommit 与"事务中"如实上报。

        MySQL 客户端（pymysql 的 `get_autocommit()` 等）读的是这个标志位，
        不是 `SELECT @@autocommit`。旧实现固定发 SERVER_STATUS_AUTOCOMMIT，
        等于永远告诉客户端"你在自动提交模式"—— 与 `SET autocommit=0` 的
        真实会话状态矛盾。
        """
        flags = 0
        if self._session_autocommit():
            flags |= mp.SERVER_STATUS_AUTOCOMMIT
        session = getattr(self, 'session', None)
        if isinstance(session, dict) and session.get('txn'):
            flags |= mp.SERVER_STATUS_IN_TRANS
        return flags

    def _ok_packet(self, affected_rows: int = 0,
                   last_insert_id: int = 0, message: str = "") -> bytes:
        """带真实会话状态的 OK 包（autocommit / in-trans 标志）"""
        return mp.make_ok_packet(
            affected_rows=affected_rows,
            last_insert_id=last_insert_id,
            status_flags=self._status_flags(),
            message=message)

    def _begin_session_txn(self):
        """在连接级 session 上开事务（autocommit=0 的隐式事务起点）

        返回 None 表示成功；失败返回 `(errno, sqlstate, message)`。
        """
        try:
            txn = self.db.begin()
        except Exception as e:                # pragma: no cover - 防御
            logger.error(f"开启会话事务失败: {e}")
            return (1105, 'HY000', f"开启事务失败: {e}")
        self.session['txn'] = txn
        return None

    def _commit_pending_txn(self):
        """提交连接上残留事务（`SET autocommit=1` / BEGIN 的隐式提交）"""
        txn = self.session.get('txn') if isinstance(
            self.session, dict) else None
        if not txn:
            if isinstance(self.session, dict):
                self.session['txn'] = None
            return None
        # 先摘掉句柄再提交：即使提交中途抛异常，也不会有语句再用到这个
        # 已经处于半提交状态的事务句柄（交给上面的异常路径兜底记录）。
        self.session['txn'] = None
        try:
            self.db.commit(txn)
        except Exception as e:                # pragma: no cover - 防御
            logger.error(f"提交事务失败: {e}")
            return (1105, 'HY000', f"提交事务失败: {e}")
        try:
            self.executor.stats['commits'] += 1
        except Exception:                     # pragma: no cover - 防御
            pass
        return None

    def _rollback_session_txn(self):
        """回滚连接上的未提交事务（断连 / 语句异常兜底）

        与执行器 `_cmd_rollback` 走同一条物理撤销路径（`db.abort`），
        保证数据真的回去、MVCC active 与行锁真正释放。
        """
        session = getattr(self, 'session', None)
        if not isinstance(session, dict):
            return
        txn = session.get('txn')
        session['txn'] = None
        if not txn:
            return
        try:
            self.db.abort(txn)
            logger.info(
                f"连接断开：已回滚未提交事务 "
                f"(txn_id={txn.get('txn_id')}, sid={self.server_id})")
        except Exception as e:                # pragma: no cover - 防御
            logger.error(
                f"断连回滚事务失败 (sid={self.server_id}): {e}")
        finally:
            # 释放会话里其它可能引用事务的对象（读视图/undo 句柄）
            session.pop('txn', None)

    def _clear_connection_session(self):
        """连接结束：清空连接级 session（释放事务/读视图/用户变量的引用）"""
        session = getattr(self, 'session', None)
        if isinstance(session, dict):
            session.clear()

    def _prepare_transaction(self, parsed):
        """语句执行前的事务边界处理

        * `BEGIN`：MySQL 语义下会**隐式提交**当前事务（`START TRANSACTION`
          同样），否则旧 txn 的 MVCC active/行锁会永久泄漏；
        * `INSERT/UPDATE/DELETE` 且会话 `autocommit=0` 且当前没有事务：
          先开一个事务并挂到 session 上 —— 这就是 MySQL 在关闭自动提交后
          "语句自动进入事务、直到 COMMIT/ROLLBACK"的行为。

        返回 None 或 `(errno, sqlstate, message)`。
        """
        cmd = str(parsed.get('command') or '')
        if cmd == 'BEGIN':
            return self._commit_pending_txn()
        if (cmd in ('INSERT', 'UPDATE', 'DELETE')
                and not self._session_autocommit()
                and not self.session.get('txn')):
            return self._begin_session_txn()
        return None

    # ---------- SET 语句 ----------

    @staticmethod
    def _split_set_assignments(text: str):
        """按顶层逗号切分 SET 赋值列表（引号/括号内的逗号不算分隔符）"""
        parts, buf, depth, quote = [], [], 0, None
        for ch in text:
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == '(':
                depth += 1
                buf.append(ch)
            elif ch == ')':
                depth = max(0, depth - 1)
                buf.append(ch)
            elif ch == ',' and depth == 0:
                parts.append(''.join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append(''.join(buf).strip())
        return [p for p in parts if p]

    @staticmethod
    def _strip_set_literal(raw: str) -> str:
        """去掉 SET 值两侧的引号（SHOW VARIABLES / @@var 用原始字面值即可）"""
        s = str(raw).strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        return s

    @staticmethod
    def _set_bool_value(raw: str):
        """解析 ON/OFF/TRUE/FALSE/1/0；无法识别返回 None"""
        low = MySQLHandler._strip_set_literal(raw).lower()
        if low in ('1', 'on', 'true', 'default'):
            return True
        if low in ('0', 'off', 'false'):
            return False
        return None

    def _handle_set(self, sql: str):
        """处理 `SET ...`：真实写进连接级会话（不再是"一律回 OK"）

        覆盖：`SET [NAMES|CHARACTER SET] ...`、`SET [@@][SESSION.]autocommit = 0|1`
        （含多赋值语句 `SET a=1, b=2`）、`SET @user_var = ...` 与普通会话变量。
        不支持的 SET（例如 `SET GLOBAL ...`、`SET PASSWORD ...`）保持旧行为：
        回 OK（客户端连接阶段的兼容性），但把原文记录到 `session['set_raw']`
        便于排障 —— 绝不因为"不认识"就在连接阶段报 1064。
        """
        session = self.session
        body = re.sub(r'^SET\b', '', sql.strip(), count=1,
                      flags=re.IGNORECASE).strip()

        # ---- SET NAMES <charset> [COLLATE <collation>] / SET CHARACTER SET ----
        m = re.match(
            r'(?is)^NAMES\s+(?:(DEFAULT)|([\w-]+))'
            r'(?:\s+COLLATE\s+([\w-]+))?\s*$', body)
        if m is None:
            m = re.match(
                r'(?is)^CHARACTER\s+SET\s+(?:(DEFAULT)|([\w-]+))'
                r'(?:\s+COLLATE\s+([\w-]+))?\s*$', body)
        if m is not None:
            charset = (m.group(2) or 'utf8mb4').lower()
            collation = (m.group(3) or '').lower()
            # 只记录、不改变协议编码（服务端始终以 UTF-8 收发）；
            # 但 @@character_set_client / SHOW VARIABLES 必须如实反映，
            # 否则客户端会以为字符集协商生效了。
            session['variables']['character_set_client'] = charset
            session['variables']['character_set_connection'] = charset
            session['variables']['character_set_results'] = charset
            if collation:
                session['variables']['collation_connection'] = collation
            self._send_packet(self._ok_packet())
            return

        if not body:
            self._send_packet(self._ok_packet())
            return

        for part in self._split_set_assignments(body):
            err = self._apply_set_assignment(part)
            if err is not None:
                code, sqlstate, message = err
                self._send_packet(mp.make_err_packet(
                    code, sqlstate, message[:200]))
                return

        # 未识别的 SET 原文留档（有界，避免被恶意连接撑爆内存）
        raw = session.setdefault('set_raw', [])
        if len(raw) < 50:
            raw.append(body[:200])

        self._send_packet(self._ok_packet())

    def _apply_set_assignment(self, part: str):
        """应用一条 SET 赋值；返回 None 或 `(errno, sqlstate, message)`"""
        if '=' not in part:
            # 非赋值型 SET（`SET TRANSACTION ISOLATION LEVEL ...` 等）：
            # 本引擎没有对应可执行语义，保持 ACK（旧行为），原文留档。
            return None
        name_raw, value_raw = part.split('=', 1)
        name = name_raw.strip()
        scope = None
        sm = re.match(r'(?is)^(GLOBAL|SESSION|LOCAL)\s+(.*)$', name)
        if sm:
            scope, name = sm.group(1).upper(), sm.group(2).strip()
        low = name.lower()
        if low.startswith('@@'):
            low = low[2:].strip()
            for prefix in ('session.', 'local.', 'global.'):
                if low.startswith(prefix):
                    scope = prefix[:-1].upper()
                    low = low[len(prefix):].strip()
                    break

        session = self.session

        # ---- 用户变量 `SET @x = ...`（执行器 `SELECT @x` 读 session.user_vars）----
        if low.startswith('@'):
            uname = low[1:].strip()
            if re.fullmatch(r'[\w$]+', uname):
                session.setdefault('user_vars', {})[uname] = (
                    self._set_literal_value(value_raw))
            return None

        if not re.fullmatch(r'[a-z_][\w.]*', low):
            # 形如 `SET PASSWORD FOR 'u'@'h' = ...` 的语句：不是会话变量，
            # 不在此实现（保持旧 ACK），也不当作变量记录
            return None

        # GLOBAL 变量：本引擎没有全局变量表，且改全局不应影响本会话；
        # 保持旧行为回 OK（同时原文留档），不做假效果。
        if scope == 'GLOBAL':
            return None

        if low == 'autocommit':
            enabled = self._set_bool_value(value_raw)
            if enabled is None:
                return (1231, 'HY000',
                        f"Variable 'autocommit' can't be set to the "
                        f"value of '{self._strip_set_literal(value_raw)[:60]}'")
            if enabled:
                # `SET autocommit=1` 在 MySQL 里会提交当前事务
                err = self._commit_pending_txn()
                if err is not None:
                    return err
            session['autocommit'] = enabled
            return None

        # 其它会话变量：仅对白名单内的可写系统变量如实保存
        # （SHOW VARIABLES / SELECT @@x 读它）；其余只留原文，不伪造效果。
        if low in _SESSION_SETTABLE_VARS:
            session.setdefault('variables', {})[low] = (
                self._strip_set_literal(value_raw))
        return None

    @staticmethod
    def _set_literal_value(raw: str):
        """把 SET 右值转成 Python 字面值（用户变量 @x 用）"""
        s = str(raw).strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        low = s.lower()
        if low == 'null':
            return None
        if re.fullmatch(r'[-+]?\d+', s):
            return int(s)
        if re.fullmatch(r'[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?', s):
            return float(s)
        return s

    # ==================== 结果集 ====================

    def _send_resultset(self, columns: list, rows: list):
        """
        columns: [(name, type, length), ...]
        rows: [(val1, val2, ...), ...]
        """
        # 列数
        self._send_packet(
            mp.write_lenenc_int(len(columns)))

        # 列定义
        for name, ctype, length in columns:
            self._send_packet(
                mp.make_column_definition(name, ctype, length))

        # EOF（带真实会话状态：autocommit / in-trans 标志）
        self._send_packet(
            mp.make_eof_packet(status_flags=self._status_flags()))

        # 数据行
        for row in rows:
            self._send_packet(mp.make_text_row(list(row)))

        # 结束 EOF
        self._send_packet(
            mp.make_eof_packet(status_flags=self._status_flags()))

    def _send_execution_result(self, result, parsed):
        cmd = parsed['command']

        # 执行器把异常转成错误字典返回而不抛出（见 sql/executor.py:107 _err），
        # 因此这里必须优先翻译成 ERR 包；否则所有失败都会以 OK 包回给客户端，
        # 客户端（以及测试）会把失败当成成功。
        if isinstance(result, dict) and result.get('type') == 'error':
            code = int(result.get('code') or 1105)
            # ★ SQLSTATE 透传（P1-B）：执行器只在部分路径（DDL/CHECK）带
            # `sqlstate`，其余错误只有 errno；旧实现**硬编码 'HY000'**，
            # 于是"表不存在(42S02)"与"未知列(42S22)"被压成同一类。
            # 优先用执行器给的真实值，缺失时按 errno 兜底映射。
            sqlstate = result.get('sqlstate') or classify_sqlstate(code)
            self._send_packet(mp.make_err_packet(
                code,
                sqlstate,
                str(result.get('message', 'execution error'))[:200]))
            return

        if cmd == 'INSERT':
            affected = self._extract_affected(result)
            self._send_packet(
                self._ok_packet(affected_rows=affected))
            return

        if cmd in ('UPDATE', 'DELETE'):
            affected = self._extract_affected(result)
            self._send_packet(
                self._ok_packet(affected_rows=affected))
            return

        if cmd in ('CREATE TABLE', 'DROP TABLE',
                   'CREATE INDEX', 'DROP INDEX',
                   'TRUNCATE', 'BEGIN', 'COMMIT',
                   'ROLLBACK', 'GRANT', 'REVOKE',
                   'SET PASSWORD', 'ALTER TABLE',
                   'USE', 'ANALYZE'):
            self._send_packet(self._ok_packet())
            return

        if cmd == 'EXPLAIN':
            if result.get('type') == 'resultset':
                self._send_resultset(
                    self._infer_columns(
                        result['columns'], result['rows']),
                    result['rows'])
            else:
                self._send_packet(self._ok_packet())
            return

        if cmd in ('SELECT', 'SELECT VARIABLES'):
            # `SELECT @x`（用户变量）与普通 SELECT 一样必须把结果集发回去。
            # 旧实现只认 cmd == 'SELECT'，于是 `SELECT @x` 落到"其他"分支回
            # OK 包（pymysql 的 description=None、fetchall() 空）—— 用户变量
            # 经协议永远读不到。连接级 session 修好后它读的是本连接
            # `session['user_vars']`，这里补上结果集序列化路径。
            if result.get('type') != 'resultset':
                self._send_packet(self._ok_packet())
                return

            cols, rows = self._extract_result(result, parsed)
            self._send_resultset(
                self._infer_columns(cols, rows), rows)
            return

        # 其他：显示消息
        self._send_packet(self._ok_packet(
            message=str(result)[:200]))

    def _extract_affected(self, result) -> int:
        import re
        if isinstance(result, int):
            return result
        if isinstance(result, dict):
            affected = result.get('affected', 0)
            try:
                return int(affected)
            except (ValueError, TypeError):
                pass
        m = re.search(r'(\d+)', str(result))
        return int(m.group(1)) if m else 0

    def _extract_result(self, result, parsed):
        """从执行结果提取 (columns, rows)"""
        if result.get('type') == 'resultset':
            return result['columns'], result['rows']

        # 文本降级
        text = str(result)
        if not text or text == "Empty set.":
            cols = parsed.get('columns') or ['?']
            if cols == ['*']:
                table = self.db.tables.get(parsed.get('table'))
                cols = ([c['name'] for c in table.cols]
                        if table else ['?'])
            return cols, []

        rows = []
        cols = []
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('|') and \
                    not line.startswith('|---'):
                parts = [
                    p.strip() for p in line.split('|')[1:-1]
                ]
                if parts and parts[0] != '---':
                    if not cols:
                        cols = parts
                        continue
                    converted = []
                    for p in parts:
                        if p in ('None', 'NULL'):
                            converted.append(None)
                        else:
                            try:
                                converted.append(int(p))
                            except ValueError:
                                try:
                                    converted.append(float(p))
                                except ValueError:
                                    converted.append(p)
                    rows.append(tuple(converted))

        if not cols:
            pcols = parsed.get('columns') or ['?']
            if pcols == ['*']:
                table = self.db.tables.get(parsed.get('table'))
                cols = ([c['name'] for c in table.cols]
                        if table else ['?'])
            else:
                cols = pcols

        return cols, rows

    def _infer_columns(self, columns, rows):
        """★ 根据实际值推断列类型"""
        if not columns:
            return []
        if not rows:
            return [(c, mp.MYSQL_TYPE_VAR_STRING, 255)
                    for c in columns]

        result = []
        n = len(columns)

        # 采样前 100 行
        sample = rows[:100]

        for ci in range(n):
            values = [
                r[ci] for r in sample
                if ci < len(r) and r[ci] is not None
            ]
            if not values:
                result.append(
                    (columns[ci], mp.MYSQL_TYPE_NULL, 0))
                continue

            v0 = values[0]
            if isinstance(v0, bool):
                ctype = mp.MYSQL_TYPE_TINY
                length = 1
            elif isinstance(v0, int):
                if -2147483648 <= v0 <= 2147483647:
                    ctype = mp.MYSQL_TYPE_LONG
                    length = 11
                else:
                    ctype = mp.MYSQL_TYPE_LONGLONG
                    length = 20
            elif isinstance(v0, float):
                ctype = mp.MYSQL_TYPE_DOUBLE
                length = 22
            elif isinstance(v0, bytes):
                ctype = mp.MYSQL_TYPE_BLOB
                length = 65535
            else:
                ctype = mp.MYSQL_TYPE_VAR_STRING
                max_len = max(
                    (len(str(v)) for v in values), default=255)
                length = min(max(max_len * 4, 64), 65535)

            result.append((columns[ci], ctype, length))

        return result

    # ==================== SHOW 系列 ====================

    def _get_system_variable(self, var: str) -> str:
        name = str(var).strip().lower().lstrip('@')
        # ★ 会话级覆盖优先：`SET NAMES latin1` / `SET sql_mode=...` 写进
        # `session['variables']` 后，`SELECT @@character_set_client` 等必须
        # 读到本连接的**真实**值，而不是全局默认（旧实现读全局/写死值）。
        session = getattr(self, 'session', None)
        if isinstance(session, dict):
            sess_vars = session.get('variables')
            if isinstance(sess_vars, dict) and name in sess_vars:
                return sess_vars[name]
        variables = {
            'version': '5.7.30-LightDB',
            'version_comment': 'LightDB Enterprise Server',
            'character_set_client': 'utf8mb4',
            'character_set_connection': 'utf8mb4',
            'character_set_results': 'utf8mb4',
            'character_set_server': 'utf8mb4',
            'collation_connection': 'utf8mb4_general_ci',
            'collation_server': 'utf8mb4_general_ci',
            # 会话级 autocommit（不是全局 db.autocommit）
            'autocommit': '1' if self._session_autocommit() else '0',
            'transaction_isolation': 'READ-COMMITTED',
            'tx_isolation': 'READ-COMMITTED',
            'max_allowed_packet': '67108864',
            'max_connections': '151',
            'sql_mode': (
                'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES'),
            'time_zone': 'SYSTEM',
            'system_time_zone': 'CST',
            'lower_case_table_names': '0',
            'wait_timeout': '28800',
            'interactive_timeout': '28800',
            'license': 'GPL',
            'have_ssl': 'DISABLED',
            'protocol_version': '10',
            'port': str(self.server.config.port),
            'hostname': socket.gethostname(),
        }
        return variables.get(name, '')

    def _show_variables(self, sql: str):
        variables = {
            'auto_increment_increment': '1',
            'auto_increment_offset': '1',
            # 会话级 autocommit：`SET autocommit=0` 后这里必须显示 OFF
            'autocommit': 'ON' if self._session_autocommit() else 'OFF',
            'character_set_client': 'utf8mb4',
            'character_set_connection': 'utf8mb4',
            'character_set_database': 'utf8mb4',
            'character_set_results': 'utf8mb4',
            'character_set_server': 'utf8mb4',
            'collation_connection': 'utf8mb4_general_ci',
            'collation_database': 'utf8mb4_general_ci',
            'collation_server': 'utf8mb4_general_ci',
            'have_innodb': 'YES',
            'innodb_buffer_pool_size': '134217728',
            'innodb_page_size': '16384',
            'max_allowed_packet': '67108864',
            'max_connections': '151',
            'port': str(self.server.config.port),
            'protocol_version': '10',
            'query_cache_size': '0',
            'query_cache_type': 'OFF',
            'sql_mode': 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES',
            'system_time_zone': 'CST',
            'time_zone': 'SYSTEM',
            'transaction_isolation': 'READ-COMMITTED',
            'version': '5.7.30-LightDB',
            'version_comment': 'LightDB Enterprise Server',
        }
        # 会话级 SET 覆盖（SET NAMES / SET SESSION x=... 写入）优先
        session = getattr(self, 'session', None)
        if isinstance(session, dict):
            sess_vars = session.get('variables')
            if isinstance(sess_vars, dict):
                variables.update(
                    {k: str(v) for k, v in sess_vars.items()})
        # 按变量名排序（MySQL 的 SHOW VARIABLES 输出同样有序）
        variable_rows = sorted(variables.items())

        m = re.search(r"LIKE\s+'([^']+)'", sql,
                      re.IGNORECASE)
        if m:
            import fnmatch
            pattern = m.group(1).replace('%', '*')
            variable_rows = [
                v for v in variable_rows
                if fnmatch.fnmatch(v[0], pattern)
            ]

        self._send_resultset(
            [("Variable_name", mp.MYSQL_TYPE_VAR_STRING, 64),
             ("Value", mp.MYSQL_TYPE_VAR_STRING, 1024)],
            variable_rows)

    def _show_status(self):
        stats = getattr(self.executor, 'stats', {})
        session_count = _session_count()

        rows = [
            # 本引擎**不统计**字节数（协议层没有计数器）。原先这里写死
            # '0' —— 那是在宣称"一条字节都没收发"，而事实是"没有这个
            # 计数器"。取不到就报 NULL，让使用者去别处找（进程 IO 统计），
            # 而不是拿一个假的 0 当答案。
            ('Aborted_clients', '0'),
            ('Aborted_connects', '0'),
            ('Bytes_received', None),
            ('Bytes_sent', None),
            ('Connections', str(session_count)),
            ('Queries', str(stats.get('queries', 0))),
            ('Questions', str(stats.get('queries', 0))),
            ('Threads_connected', str(session_count)),
            ('Threads_running', str(self._threads_running())),
            ('Uptime',
             str(int(time.time() - self.server.start_time))),
        ]
        self._send_resultset(
            [("Variable_name", mp.MYSQL_TYPE_VAR_STRING, 64),
             ("Value", mp.MYSQL_TYPE_VAR_STRING, 1024)],
            rows)

    @staticmethod
    def _threads_running() -> int:
        """真实在跑语句的会话数（命令列为 Query 的那些）

        原来这里写死 '1'（"总有 1 个线程在跑"）。空载时那是假的：
        没有任何会话在执行语句。改为按真实会话状态统计。
        """
        with _sessions_lock:
            return sum(1 for info in _sessions.values()
                       if str(info.get('command', '')).lower() == 'query')

    def _show_processlist(self):
        cols = ['Id', 'User', 'Host', 'db', 'Command',
                'Time', 'State', 'Info']
        rows = []
        now = time.time()
        with _sessions_lock:
            items = list(_sessions.items())
        for sid, info in items:
            # `Time` 是"在当前状态里待了多久"（MySQL 语义）。会话空闲
            # （Sleep）时没有"当前语句"，如实报 NULL；正在执行语句时报
            # 该语句已运行的秒数。原来的实现用 `now - 连接建立时刻`，
            # 于是空闲连接的 Time 一直在涨，看起来像"有查询跑了 300 秒"。
            stmt_start = info.get('stmt_started')
            running = str(info.get('command', '')).lower() == 'query'
            elapsed = (int(now - stmt_start)
                       if (running and stmt_start) else None)
            rows.append((
                sid,
                info.get('user', ''),
                str(info.get('host', ('', 0))[0]),
                info.get('db', ''),
                info.get('command', 'Sleep'),
                elapsed,
                '',
                info.get('info', ''),
            ))

        self._send_resultset(
            [(c, mp.MYSQL_TYPE_VAR_STRING, 255)
             for c in cols],
            rows)

    def _handle_kill(self, sql: str):
        import re
        # `KILL QUERY <id>`：本引擎没有"只中止单条语句"的机制（语句在
        # 执行器里同步跑完，没有可中断点）。**显式拒绝**，不要伪装成功。
        mq = re.match(r'KILL\s+QUERY\s+(\d+)', sql, re.IGNORECASE)
        if mq:
            self._send_packet(mp.make_err_packet(
                1105, "HY000",
                f"KILL QUERY {mq.group(1)} 不支持：本引擎无法只中止单条语句，"
                f"请用 KILL CONNECTION {mq.group(1)} 终止整个连接"))
            return

        m = re.match(r'KILL\s+(?:CONNECTION\s+)?(\d+)',
                     sql, re.IGNORECASE)
        if not m:
            self._send_packet(mp.make_err_packet(
                1064, "42000", "KILL 语法错误"))
            return

        try:
            target_sid = int(m.group(1))
        except ValueError:
            self._send_packet(mp.make_err_packet(
                1064, "42000", "KILL 参数错误"))
            return

        # 杀自己：MySQL 语义是"语句正常返回后连接结束"，因此先回 OK，
        # 再把本连接标记为已关闭（外层 `while not self.closed` 会退出）。
        if target_sid == self.server_id:
            self.closed = True
            self._send_packet(self._ok_packet(
                message=f"Connection {target_sid} killed"))
            return

        info = _get_sessions_snapshot().get(target_sid)
        if info is None:
            self._send_packet(mp.make_err_packet(
                1094, "HY000",
                f"Unknown thread id: {target_sid}"))
            return

        # 真正终止：先置 `closed`（目标线程下一轮循环即退出），再关闭它的
        # socket，使阻塞中的 recv 立刻失败 —— 而不是只回一个 OK 了事。
        handler = info.get('handler')
        if handler is not None:
            handler.closed = True
        sock = info.get('sock')
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        _remove_session(target_sid)
        self._send_packet(self._ok_packet(
            message=f"Connection {target_sid} killed"))

    def _show_columns(self, sql: str):
        import re
        m = re.search(
            r'(?:FROM|DESC(?:RIBE)?)\s+[`"]?(\w+)[`"]?',
            sql, re.IGNORECASE)
        if not m:
            self._send_packet(mp.make_err_packet(
                1064, "42000", "Syntax error"))
            return

        table_name = m.group(1)
        if table_name not in self.db.tables:
            self._send_packet(mp.make_err_packet(
                1146, "42S02",
                f"Table '{self.database}.{table_name}' "
                f"doesn't exist"))
            return

        table = self.db.tables[table_name]
        # 列元数据来自**真实列定义**（与 information_schema / 进程内执行器
        # 共用 db/database.py::describe_columns）：
        #   Type   保留长度/精度（VARCHAR(20) / DECIMAL(10,2)）
        #   Default 真实默认值，没声明就是 NULL
        #   Extra  本引擎没有 auto_increment / on update → 恒为 ''
        # 旧实现把 Default 写死 None、把 Extra 由主键伪造成 auto_increment。
        rows = [
            (c['name'], c['type'], c['null'], c['key'],
             c['default'], c['extra'])
            for c in describe_columns(table)
        ]

        self._send_resultset([
            ("Field", mp.MYSQL_TYPE_VAR_STRING, 64),
            ("Type", mp.MYSQL_TYPE_VAR_STRING, 64),
            ("Null", mp.MYSQL_TYPE_VAR_STRING, 3),
            ("Key", mp.MYSQL_TYPE_VAR_STRING, 3),
            ("Default", mp.MYSQL_TYPE_VAR_STRING, 255),
            ("Extra", mp.MYSQL_TYPE_VAR_STRING, 64),
        ], rows)

    def _handle_field_list(self, body: bytes):
        try:
            table = body.split(b'\x00')[0].decode(
                'utf-8', errors='replace')
        except Exception:
            table = ''

        # 与 `SHOW COLUMNS` 走同一份真实列定义（`describe_columns`）：
        # 旧实现直接回 `MYSQL_TYPE_VAR_STRING` + 长度 255，客户端拿到的
        # 列类型是假的（mysql-connector / 部分 ORM 会用这个包）。
        if table and table in self.db.tables:
            for c in describe_columns(self.db.tables[table]):
                self._send_packet(
                    mp.make_column_definition(
                        c['name'],
                        self._mysql_type_for(c['type']),
                        self._mysql_len_for(c['type'])))

        self._send_packet(mp.make_eof_packet())

    #: 本引擎类型 → MySQL 协议列类型（`COM_FIELD_LIST` 用）。
    #: 只映射真实知道的类型；认不出就如实报 VAR_STRING（宽字符），
    #: 不猜一个"看起来专业"的二进制类型。
    @staticmethod
    def _mysql_type_for(col_type: str) -> int:
        t = str(col_type or '').upper()
        base = t.split('(')[0].strip()
        return {
            'INT': mp.MYSQL_TYPE_LONG,
            'BIGINT': mp.MYSQL_TYPE_LONGLONG,
            'FLOAT': mp.MYSQL_TYPE_FLOAT,
            'DOUBLE': mp.MYSQL_TYPE_DOUBLE,
            'DECIMAL': mp.MYSQL_TYPE_NEWDECIMAL,
            'BOOLEAN': mp.MYSQL_TYPE_TINY,
            'DATE': mp.MYSQL_TYPE_DATE,
            'TIME': mp.MYSQL_TYPE_TIME,
            'DATETIME': mp.MYSQL_TYPE_DATETIME,
            'JSON': mp.MYSQL_TYPE_JSON,
            'BLOB': mp.MYSQL_TYPE_BLOB,
            'TEXT': mp.MYSQL_TYPE_BLOB,
        }.get(base, mp.MYSQL_TYPE_VAR_STRING)

    @staticmethod
    def _mysql_len_for(col_type: str) -> int:
        """列显示长度：优先用声明里的长度/精度，取不到才给协议默认值"""
        import re
        m = re.search(r'\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)',
                      str(col_type or ''))
        if m:
            if m.group(2):
                # `DECIMAL(10,2)`：MySQL 的 display length ≈ 精度 + 3
                return int(m.group(1)) + int(m.group(2)) + 3
            return int(m.group(1))
        base = str(col_type or '').upper().split('(')[0].strip()
        return {'INT': 11, 'BIGINT': 20, 'FLOAT': 12, 'DOUBLE': 22,
                'DECIMAL': 11, 'BOOLEAN': 1, 'DATE': 10, 'TIME': 10,
                'DATETIME': 19, 'JSON': 1024, 'BLOB': 65535,
                'TEXT': 65535}.get(base, 255)


# ==================== Server ====================

class MySQLServer(socketserver.ThreadingTCPServer):
    """MySQL 兼容服务器

    除监听/服务外，本类负责**真正的连接准入执行**（P0-A 安全修复）：
    * `max_connections`：全局并发连接上限（默认 100）；
    * `max_conn_per_ip`：单个 IP 的并发连接上限（默认 20）；
    * `ip_whitelist_enabled` / `ip_whitelist`：IP 白名单。

    超限或不在白名单的连接在**握手之前**直接被回 ERR 包并关闭
    （MySQL 协议允许服务端首包就是 ERR），客户端（pymysql 等）会看到
    明确的 1040/1130 错误码，而不是"连上之后被莫名断开"。计数在
    `verify_request` 里增加、在 `shutdown_request` 里减少，两者都由
    socketserver 对每个已接受连接成对调用。
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, config: Config, db, executor):
        self.config = config
        self.db = db
        self.executor = executor
        self.start_time = time.time()
        # ---------- 连接准入状态 ----------
        self._admission_lock = threading.Lock()
        self._active_connections = 0
        self._active_by_ip = {}
        # 已准入的 socket -> 来源 IP。用弱引用键：即使某个连接的
        # shutdown_request 因异常没走到，也不会在计数/映射上留下悬挂引用。
        self._admitted = weakref.WeakKeyDictionary()

        self.max_connections = self._int_cfg(
            'max_connections', 100)
        self.max_conn_per_ip = self._int_cfg(
            'max_conn_per_ip', 20)

        # ---------- IP 白名单 ----------
        self.ip_whitelist = None
        try:
            from ..security.ip_whitelist import IPWhitelist
            self.ip_whitelist = IPWhitelist(
                enabled=bool(getattr(config, 'ip_whitelist_enabled', False)),
                entries=list(getattr(config, 'ip_whitelist', None) or []))
            if self.ip_whitelist.enabled and not self.ip_whitelist.entries:
                logger.warn(
                    "IP 白名单已启用但规则为空：所有连接都会被拒绝"
                    "（在 config.json 的 ip_whitelist / "
                    "LIGHTDB_IP_WHITELIST_ENABLED 配置）")
        except Exception as e:               # pragma: no cover - 防御
            logger.warn(f"初始化 IP 白名单失败（按未启用处理）: {e}")
            self.ip_whitelist = None

        super().__init__(
            (config.host, config.port),
            MySQLHandler)

    def _int_cfg(self, name: str, default: int) -> int:
        """读取整型配置，非法值回退默认值（0/负数视为非法）"""
        try:
            val = int(getattr(self.config, name, default))
        except (TypeError, ValueError):
            return default
        return val if val >= 1 else default

    # ==================== 准入检查 ====================

    def _is_ip_allowed(self, ip: str) -> bool:
        """IP 是否允许连接。

        优先用**执行器里的白名单对象**：`ADD/REMOVE IP WHITELIST` 改的是
        它，那才是运行期的真实规则（`SHOW IP WHITELIST` 也读它）。两边都
        按同一份 config 初始化时结果一致；只有运行时增删规则会分化——此时
        必须以执行器为准，否则"看得见的新规则"不会生效。执行器没有该属性
        时（多进程 RemoteDB）回退到本服务器按配置构建的白名单。
        """
        ex_wl = getattr(self.executor, 'ip_whitelist', None)
        if ex_wl is not None and getattr(ex_wl, 'enabled', False):
            return ex_wl.allow(ip)
        wl = self.ip_whitelist
        if wl is not None and getattr(wl, 'enabled', False):
            return wl.allow(ip)
        return True

    def verify_request(self, request, client_address):
        """socketserver 在握手前调用：返回 False = 拒绝该连接。

        检查顺序：IP 白名单 → 全局连接数 → 单 IP 连接数。计数与准入在
        同一个锁里完成，避免并发连接同时"看到没满"而一起挤进来。
        """
        ip = client_address[0] if client_address else ''

        # 1) IP 白名单
        if not self._is_ip_allowed(ip):
            logger.warn(f"拒绝连接 {ip}：不在 IP 白名单内")
            self._reject_connection(
                request, 1130, "HY000",
                f"Host '{ip}' is not allowed to connect "
                f"to this LightDB server")
            return False

        # 2) 连接数上限（总量 + 单 IP）
        reject_reason = None
        with self._admission_lock:
            total = self._active_connections
            per_ip = self._active_by_ip.get(ip, 0)
            if total >= self.max_connections:
                reject_reason = (
                    f"Too many connections (max_connections="
                    f"{self.max_connections})")
            elif per_ip >= self.max_conn_per_ip:
                reject_reason = (
                    f"Too many connections from this IP "
                    f"(max_conn_per_ip={self.max_conn_per_ip})")
            else:
                self._active_connections = total + 1
                self._active_by_ip[ip] = per_ip + 1
                self._admitted[request] = ip

        if reject_reason is not None:
            logger.warn(f"拒绝连接 {ip}：{reject_reason}")
            self._reject_connection(
                request, 1040, "08004", reject_reason)
            return False
        return True

    def shutdown_request(self, request):
        """连接结束：先减计数，再走 socketserver 的关闭流程。"""
        ip = None
        with self._admission_lock:
            try:
                ip = self._admitted.pop(request)
            except KeyError:
                ip = None
            if ip is not None:
                self._active_connections = max(
                    0, self._active_connections - 1)
                left = self._active_by_ip.get(ip, 0) - 1
                if left > 0:
                    self._active_by_ip[ip] = left
                else:
                    self._active_by_ip.pop(ip, None)
        try:
            super().shutdown_request(request)
        except Exception:                    # pragma: no cover - 防御
            pass

    def _reject_connection(self, request, code: int, sql_state: str,
                           message: str):
        """握手前直接回 ERR 包（MySQL 服务端首包允许是 ERR）"""
        try:
            request.sendall(mp.build_packet(
                mp.make_err_packet(code, sql_state, message), 0))
        except OSError:
            pass

    def connection_stats(self) -> dict:
        """当前连接计数（调试/测试/监控用）"""
        with self._admission_lock:
            return {
                'active_connections': self._active_connections,
                'active_by_ip': dict(self._active_by_ip),
                'max_connections': self.max_connections,
                'max_conn_per_ip': self.max_conn_per_ip,
            }

    def start(self):
        logger.info(
            f"LightDB MySQL Server 监听 "
            f"{self.config.host}:{self.config.port}")
        logger.info(
            f"  连接限制: 总量 {self.max_connections} / "
            f"每 IP {self.max_conn_per_ip}")
        if (self.ip_whitelist is not None
                and self.ip_whitelist.enabled):
            logger.info(
                f"  IP 白名单: 启用，"
                f"{len(self.ip_whitelist.entries)} 条规则")
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务器正在关闭...")
            self.shutdown()