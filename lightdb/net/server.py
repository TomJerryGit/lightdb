"""
LightDB MySQL 兼容服务器（修复版）

修复:
- 全局会话表线程安全（遍历加锁）
- 列类型推断（根据实际值返回正确 MYSQL_TYPE）
- 每会话 connected_at
- SHOW STATUS / SHOW VARIABLES / SHOW PROCESSLIST / KILL
- 查询语句识别更严谨
"""
import math
import os
import re
import socket
import socketserver
import ssl
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
# ★ 生产可观测性（task-70）：真实运行期指标 + 内嵌 /metrics 端点
from .observability import ServerRuntimeMetrics
from .metrics_server import maybe_start_metrics
# ★ 生产化 B（task-71）：服务端连接池（worker 线程复用/超限策略/健康检查）
from .pool import ConnectionPool


#: 登录失败后的固定延迟（秒）。它让暴力破解/口令扫描变慢，同时不改变
#: 正确登录路径的耗时。0.5s 足够拖垮在线爆破，又不会影响人的交互体验。
LOGIN_FAILURE_DELAY = 0.5


#: 单连接 prepared statement 数量上限（对应 MySQL 的
#: `max_prepared_stmt_count` 语义；这里按连接计，防止恶意/失控客户端只
#: PREPARE 不 CLOSE 把服务端内存吃光 —— 每条语句都保存一份 SQL 原文）。
MAX_PREPARED_STMTS = 4096


#: 错误码 → SQLSTATE 兜底映射。
#:
#: 执行器只在部分路径（DDL / CHECK 约束，见 `sql/executor.py::_err`）上带
#: `sqlstate` 字段；其余错误只有 errno。旧实现把**所有**错误回包的 SQLSTATE
#: 硬编码成 'HY000'（通用错误），于是客户端拿不到 MySQL 语义分类：
#: 表不存在（42S02）与未知列（42S22）被说成同一类"未知错误"。
#: 这里按 MySQL 官方错误码表补齐常用错误的 SQLSTATE；未收录的一律 HY000。
_SQLSTATE_BY_ERRNO = {
    1044: '42000',   # ER_DBACCESS_DENIED_ERROR：库级访问被拒
    1048: '23000',   # ER_BAD_NULL_ERROR：列不允许 NULL
    1049: '42000',   # ER_BAD_DB_ERROR：未知数据库
    1054: '42S22',   # ER_BAD_FIELD_ERROR：未知列
    1062: '23000',   # ER_DUP_ENTRY：主键/唯一键冲突
    1064: '42000',   # ER_PARSE_ERROR：语法错误
    1105: 'HY000',   # ER_UNKNOWN_ERROR：通用错误
    1130: 'HY000',   # ER_HOST_NOT_PRIVILEGED：主机不允许连接
    1141: '42000',   # ER_NONEXISTING_GRANT：没有该授权记录
    1142: '42000',   # ER_TABLEACCESS_DENIED_ERROR：表级访问被拒
    1146: '42S02',   # ER_NO_SUCH_TABLE：表不存在
    1227: '42000',   # ER_SPECIFIC_ACCESS_DENIED：需要管理员权限
    1264: '22003',   # ER_WARN_DATA_OUT_OF_RANGE：数值越界
    1292: '22007',   # ER_TRUNCATED_WRONG_VALUE：日期/时间值非法
    1366: '22007',   # ER_WRONG_VALUE：取值不合法（类型转换失败）
    1406: '22001',   # ER_DATA_TOO_LONG：数据超长
    1410: '42000',   # ER_CANT_CREATE_USER_WITH_GRANT：GRANT 不能隐式建号
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
        self.tls_active = False
        self.db = self.server.db
        self.executor = self.server.executor
        self.connected_at = time.time()
        # ★ 连接级预处理语句注册表（COM_STMT_*）：statement_id 从 1 递增。
        # 与 MySQL 一致，prepared statement 是 **per-connection** 的：
        # 断连即全部释放（见 handle() 的 finally），不能用全局表。
        self.prepared_stmts = {}
        self._next_stmt_id = 1

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
        # ★ task-70：所有 ERR 包（鉴权/权限/执行/语法）都在这里经过，
        #   按真实 errno 计数（比在每个错误分支手写计数更完整）。
        if payload[:1] == b'\xff' and len(payload) >= 3:
            try:
                self.server.runtime_metrics.on_error(
                    struct.unpack_from('<H', payload, 1)[0])
            except Exception:                     # pragma: no cover - 防御
                pass
        pkt = mp.build_packet(payload, self.seq)
        try:
            self.sock.sendall(pkt)
        except (BrokenPipeError, ConnectionResetError):
            self.closed = True
            return
        self.seq = (self.seq + 1) & 0xff

    # ==================== 握手 ====================

    def do_handshake(self) -> bool:
        ssl_enabled = bool(getattr(self.server, 'ssl_enabled', False))
        handshake = mp.make_handshake_v10(
            self.server_id, self.auth_seed, ssl_enabled=ssl_enabled)
        self._send_packet(handshake)

        response_data = self._read_packet()
        if not response_data:
            return False

        # ---------- TLS（MySQL SSLRequest 协商） ----------
        # 启用 TLS 时客户端若带 CLIENT_SSL 能力，会先发一个 32 字节
        # SSLRequest（capabilities+max_packet+charset+reserved），然后才在
        # TLS 通道里发送真正的 HandshakeResponse。这里先做 TLS 握手，再读
        # 认证包；不启用 TLS 的连接（客户端未带 CLIENT_SSL）继续明文，保持
        # 向后兼容（MySQL 的 TLS 是"可选"语义）。
        if (ssl_enabled and getattr(self.server, 'ssl_context', None)
                is not None and len(response_data) == 32):
            caps = struct.unpack('<I', response_data[:4])[0]
            if caps & mp.CLIENT_SSL:
                try:
                    self.sock = self.server.ssl_context.wrap_socket(
                        self.sock, server_side=True)
                    self.request = self.sock
                    self.tls_active = True
                    response_data = self._read_packet()
                except Exception as e:            # noqa: BLE001
                    logger.warn(f"TLS 握手失败: {e}")
                    return False
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
        # 来源地址：权限拒绝报文（1142/1044）里的 '<user>'@'<host>' 用真实
        # 客户端 IP，而不是编一个 '%'。
        try:
            self.session['host'] = str(self.client_address[0])
        except Exception:                     # pragma: no cover - 防御
            self.session['host'] = '%'

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
        try:
            # ★ task-70：失败计数（真实登录失败次数，不含准入拒绝）
            self.server.runtime_metrics.on_auth_failure()
        except Exception:                         # pragma: no cover - 防御
            pass
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
            # 预处理语句也是连接级资源：断连必须全部释放（不跨连接泄漏）
            self.prepared_stmts.clear()
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
        """给定库下可见的表（真实表 + `information_schema` 虚拟表 + 视图）"""
        try:
            names = list(self.db.visible_table_names(db_name))
            # E3：SHOW TABLES（网络内置分支）同样包含视图（MySQL 语义）。
            if self.db is not None and str(db_name).lower() == str(
                    getattr(self.db, 'name', '')).lower():
                names.extend(getattr(self.db, 'list_views', lambda: [])())
            return sorted(set(names))
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

    #: E3：连接字符集 → Python codec。SET NAMES gbk/latin1 后，客户端
    #: 发来的 SQL 字节按该编码严格解码，结果集/错误文本按
    #: `character_set_results` 编码回写 —— 真正的入/出转码。
    _CHARSET_CODECS = {
        'utf8': 'utf-8', 'utf8mb3': 'utf-8', 'utf8mb4': 'utf-8',
        'latin1': 'latin-1', 'gbk': 'gbk',
    }

    def _session_charset(self, key: str) -> str:
        try:
            variables = (self.session or {}).get('variables') or {}
            return str(variables.get(key) or 'utf8mb4').lower()
        except Exception:                         # pragma: no cover - 防御
            return 'utf8mb4'

    def _client_codec(self) -> str:
        cs = self._session_charset('character_set_client')
        return self._CHARSET_CODECS.get(cs, 'utf-8')

    def _result_codec(self) -> str:
        cs = self._session_charset('character_set_results')
        return self._CHARSET_CODECS.get(cs, 'utf-8')

    def _decode_client_utf8(self, raw: bytes, what: str = 'query'):
        """按会话字符集**严格**解码客户端字节流；失败返回 `(None, ERR 1366)`

        E1 起拒绝 `decode(errors='replace')` 静默写入 U+FFFD；E3 起按
        SET NAMES 声明（utf8/utf8mb4/latin1/gbk）真正解码——非法序列显式
        1366，错误分类/信息与 MySQL `Incorrect string value` 对齐。
        """
        codec = self._client_codec()
        try:
            return bytes(raw).decode(codec), None
        except UnicodeDecodeError as e:
            raw = bytes(raw)
            bad = raw[max(0, e.start):e.start + 16]
            shown = ''.join('\\x%02X' % b for b in bad)
            charset = self._session_charset('character_set_client')
            return None, mp.make_err_packet(
                1366, 'HY000',
                f"Incorrect string value: '{shown}' for column 'query' "
                f"（{what} 字节序列不是合法的 {charset} 编码；本引擎"
                f"不会用替换字符静默写入）",
                encoding=self._result_codec())

    def _dispatch(self, packet: bytes):
        cmd = packet[0]
        body = packet[1:]

        # ★ task-70：语句数/QPS 真实计数（COM_QUERY 与预处理 EXECUTE 都是
        #   "客户端发起的语句"；PREPARE/CLOSE 等协议管理命令不计入 QPS）。
        if cmd in (mp.COM_QUERY, mp.COM_STMT_EXECUTE):
            try:
                self.server.runtime_metrics.on_query()
            except Exception:                     # pragma: no cover - 防御
                pass

        if cmd == mp.COM_QUIT:
            self.closed = True

        elif cmd == mp.COM_PING:
            self._send_packet(self._ok_packet())

        elif cmd == mp.COM_INIT_DB:
            db_name, decode_err = self._decode_client_utf8(
                body, 'COM_INIT_DB')
            if decode_err is not None:
                self._send_packet(decode_err)
                return
            # `COM_INIT_DB` 就是 `USE <db>` 的协议形态。旧实现直接赋值 +
            # 回 OK，切到不存在的库也"成功"（pymysql 的
            # `connection.select_db('no_such_db')` 因此静默通过）。
            resolved, err = self._resolve_database(db_name)
            if err is not None:
                self._send_packet(err)
                return
            # 权限：切库需要该库上的任意权限（1044）
            access_err = self._access_error_packet(
                None, database=resolved)
            if access_err is not None:
                self._send_packet(access_err)
                return
            self.database = resolved
            # 连接级会话与 handler 状态保持一致（`USE` 语句路径同样会同步）
            self.session['database'] = resolved
            self._update_session(db=resolved)
            self._send_packet(self._ok_packet())

        elif cmd == mp.COM_QUERY:
            sql, decode_err = self._decode_client_utf8(body, 'COM_QUERY')
            if decode_err is not None:
                self._send_packet(decode_err)
                return
            self._handle_query(sql)

        elif cmd == mp.COM_FIELD_LIST:
            self._handle_field_list(body)

        # ---------- 预处理语句（COM_STMT_*） ----------
        elif cmd == mp.COM_STMT_PREPARE:
            self._handle_stmt_prepare(body)

        elif cmd == mp.COM_STMT_EXECUTE:
            self._handle_stmt_execute(body)

        elif cmd == mp.COM_STMT_CLOSE:
            # 协议规定 COM_STMT_CLOSE **没有响应包**（客户端发完不读）：
            # 这里绝不能回 OK，否则下一个命令的响应会和这个多余 OK 错位。
            self._handle_stmt_close(body)

        elif cmd == mp.COM_STMT_RESET:
            self._handle_stmt_reset(body)

        elif cmd == mp.COM_STMT_SEND_LONG_DATA:
            # 未实现：按协议该命令无响应。这里只登记"本语句收到过长数据"，
            # 后续 EXECUTE 显式报 1235（而不是拿着缺失/半截参数静默写错）。
            self._handle_stmt_send_long_data(body)

        elif cmd == mp.COM_STMT_FETCH:
            # 游标（cursor）未实现：EXECUTE 从不返回 CURSOR_EXISTS，客户端
            # 正常情况下不会发 FETCH。显式报 1235 而不是回错包/静默忽略。
            self._send_packet(mp.make_err_packet(
                1235, '42000',
                'COM_STMT_FETCH（游标）暂不支持：本引擎的预处理语句'
                '在 EXECUTE 时直接返回全量结果集（未实现 CURSOR 形态）'))

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

    # ==================== 权限（P1 阶段 1.1） ====================

    def _access_error_packet(self, priv, database=None, table=None):
        """用执行器的权限模型做一次准入检查 → ERR 包 / None

        内置分支（SHOW TABLES / SHOW COLUMNS / DESC / COM_INIT_DB /
        COM_FIELD_LIST / KILL）不经过 `Executor.execute()`，但同样受权限
        约束：这里复用 `Executor.check_access`（与执行期鉴权**同一份**模型），
        避免出现"普通语句被拒、SHOW 却能看全部元数据"的旁路。
        """
        check = getattr(self.executor, 'check_access', None)
        if not callable(check) or not isinstance(self.session, dict):
            # 没有权限模型的执行器（多进程 worker 的 RemoteExecutor）
            # 不做本地判断：真正的鉴权在 Master 侧执行器上完成。
            return None
        try:
            err = check(self.session, priv, database=database, table=table)
        except Exception as e:                # pragma: no cover - 防御
            logger.warn(f"权限检查失败（按拒绝处理）: {e}")
            return mp.make_err_packet(1044, '42000', 'Access denied')
        if not err:
            return None
        return mp.make_err_packet(
            int(err.get('code') or 1105),
            err.get('sqlstate') or classify_sqlstate(err.get('code')),
            str(err.get('message') or 'Access denied')[:200])

    def _session_is_admin(self) -> bool:
        """当前连接是否管理员（root / 全局 ALL）"""
        fn = getattr(self.executor, 'is_admin_session', None)
        if not callable(fn):
            return True
        try:
            return bool(fn(self.session))
        except Exception:                     # pragma: no cover - 防御
            return False

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
            # 非管理员只列出自己有权限的库（信息泄露最小化）；
            # `information_schema` 作为系统库对已登录账户保持可见。
            visible = getattr(self.executor, 'visible_databases', None)
            try:
                names = (visible(self.session) if callable(visible)
                         else self._database_names())
            except Exception:                 # pragma: no cover - 防御
                names = self._database_names()
            self._send_resultset(
                [("Database", mp.MYSQL_TYPE_VAR_STRING, 64)],
                [(name,) for name in names])
            return

        if upper.startswith("SHOW TABLES"):
            # 按**当前库**过滤（旧实现直读 self.db.tables，与所选库无关）。
            # 库不存在时显式报 1049，而不是列别的库的表。
            db_name, err = self._current_database()
            if err is not None:
                self._send_packet(err)
                return
            access_err = self._access_error_packet(
                'SELECT', database=db_name)
            if access_err is not None:
                self._send_packet(access_err)
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
        self._execute_sql_text(sql)

    def _execute_sql_text(self, sql: str, send_result=None):
        """解析并执行一条 SQL（**文本协议与预处理 EXECUTE 共用**）

        文本协议（COM_QUERY）与预处理语句（COM_STMT_EXECUTE 把参数安全内联
        之后）在执行语义上必须完全一致：同一份 parser、同一份事务边界处理
        （`_prepare_transaction`）、同一个连接级 session、同一份权限/错误契约。
        因此这里把"解析 → 事务预处理 → 执行 → 回包"抽成一条公共路径。

        `send_result`：可选回包函数 `(result, parsed) -> None`。文本协议用
        `_send_execution_result`（文本结果集）；预处理语句用
        `_send_prepared_result`（二进制结果集，EXECUTE 协议要求）。
        """
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

        if send_result is None:
            self._send_execution_result(result, parsed)
        else:
            send_result(result, parsed)

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
            message=message,
            encoding=self._result_codec())

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
            # E3：SET NAMES gbk/latin1 真正转码（入：SQL 字节按该编码严格
            # 解码，非法→1366；出：结果集按该编码回写）。utf8/utf8mb4
            # 语义与 E1 一致；其它未支持字符集仍显式 1115。
            if charset not in ('utf8', 'utf8mb4', 'utf8mb3', 'latin1',
                               'gbk'):
                self._send_packet(mp.make_err_packet(
                    1115, '42000',
                    f"Unknown character set: '{charset}'",
                    encoding=self._result_codec()))
                return
            if charset == 'utf8mb3':
                charset = 'utf8'
            # 排序规则：只接受与声明字符集匹配的已知 collation；bin 是真实
            # 比较语义，`_general_ci`/`_swedish_ci`/`_chinese_ci` 仅作为
            # 连接元数据记录（不改比较语义）。
            allowed_collations = {
                'utf8mb4': ('utf8mb4_bin', 'utf8mb4_general_ci',
                            'utf8mb4_unicode_ci'),
                'utf8': ('utf8_bin', 'utf8_general_ci',
                         'utf8_unicode_ci'),
                'latin1': ('latin1_bin', 'latin1_swedish_ci',
                           'latin1_general_ci'),
                'gbk': ('gbk_bin', 'gbk_chinese_ci'),
            }.get(charset, ())
            if collation and collation not in allowed_collations:
                self._send_packet(mp.make_err_packet(
                    1273, 'HY000',
                    f"Unknown collation: '{collation}'"
                    f"（字符集 {charset} 支持: "
                    f"{', '.join(allowed_collations) or '无'}）",
                    encoding=self._result_codec()))
                return
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
        # E3：结果集按会话 `character_set_results` 编码（latin1/gbk 老客户端
        # 拿到自己声明的字节；utf8mb4 默认路径与旧行为逐字节一致）。
        enc = self._result_codec()
        # 列数
        self._send_packet(
            mp.write_lenenc_int(len(columns)))

        # 列定义
        for name, ctype, length in columns:
            self._send_packet(
                mp.make_column_definition(name, ctype, length,
                                          encoding=enc))

        # EOF（带真实会话状态：autocommit / in-trans 标志）
        self._send_packet(
            mp.make_eof_packet(status_flags=self._status_flags()))

        # 数据行
        for row in rows:
            self._send_packet(mp.make_text_row(list(row), encoding=enc))

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
                str(result.get('message', 'execution error'))[:200],
                encoding=self._result_codec()))
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
                   'USE', 'ANALYZE',
                   # E3：视图 DDL / SQL 层 PREPARE 回 OK
                   'CREATE VIEW', 'DROP VIEW', 'PREPARE',
                   'DEALLOCATE PREPARE', 'SET USER VARIABLE'):
            self._send_packet(self._ok_packet())
            return

        if cmd == 'EXECUTE PREPARED':
            # SQL 层 `EXECUTE ... USING`：内层语句若返回结果集，按**文本
            # 协议**结果集回包（与直接执行该语句完全一致）。
            if result.get('type') == 'resultset':
                self._send_resultset(
                    self._infer_columns(
                        result['columns'], result['rows']),
                    result['rows'])
            else:
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

        if cmd in ('SELECT', 'SELECT VARIABLES', 'UNION'):
            # `SELECT @x`（用户变量）与普通 SELECT 一样必须把结果集发回去。
            # 旧实现只认 cmd == 'SELECT'，于是 `SELECT @x` 落到"其他"分支回
            # OK 包（pymysql 的 description=None、fetchall() 空）—— 用户变量
            # 经协议永远读不到。连接级 session 修好后它读的是本连接
            # `session['user_vars']`，这里补上结果集序列化路径。
            # `UNION` 同理：执行器返回 resultset，但协议层没登记这个 command，
            # 经真实 MySQL 协议查询会得到 OK/0 行（pymysql description=None）。
            # 2026-10-20 全量 GUI/DB 验证发现，补上。
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
        # ★ task-71：连接池真实指标（与 /metrics 的 lightdb_pool_* 同源）
        pool = getattr(self.server, 'pool', None)
        if pool is not None:
            try:
                ps = pool.snapshot()
                rows.extend([
                    ('Pool_workers_total', str(ps['workers_total'])),
                    ('Pool_workers_busy', str(ps['workers_busy'])),
                    ('Pool_workers_idle', str(ps['workers_idle'])),
                    ('Pool_max_workers', str(ps['max_workers'])),
                    ('Pool_workers_created', str(ps['workers_created'])),
                    ('Pool_assignments', str(ps['assignments'])),
                    ('Pool_reused', str(ps['reused'])),
                    ('Pool_jobs_queued', str(ps['jobs_queued'])),
                    ('Pool_jobs_pending_accept',
                     str(ps['jobs_pending_accept'])),
                    ('Pool_queue_waits', str(ps['queue_waits'])),
                    ('Pool_queue_timeouts', str(ps['queue_timeouts'])),
                    ('Pool_rejected', str(ps['rejected'])),
                    ('Pool_idle_shrinks', str(ps['idle_shrinks'])),
                    ('Pool_dead_workers', str(ps['dead_workers'])),
                    ('Pool_long_running', str(ps['long_running'])),
                    ('Pool_overflow_policy', str(ps['overflow'])),
                ])
            except Exception as e:                # pragma: no cover - 防御
                rows.append(('Pool_error', str(e)[:120]))
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
        # MySQL 语义：没有 PROCESS 权限的账户只看得到**自己的**线程；
        # 管理员（root / 全局 ALL）才看全部。旧实现任何账户都能看所有
        # 连接的 SQL 文本（信息泄露）。
        admin = self._session_is_admin()
        with _sessions_lock:
            items = list(_sessions.items())
        for sid, info in items:
            if not admin and sid != self.server_id:
                continue
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

        # 杀别人需要 PROCESS/SUPER（本模型用管理员=全局 ALL 表达）；普通
        # 账户只能 KILL 自己，旧实现里任何登录用户都能踢掉任意连接。
        if not self._session_is_admin():
            self._send_packet(mp.make_err_packet(
                1227, "42000",
                "Access denied; you need (at least one of) the "
                "PROCESS privilege(s) for this operation"))
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
        # Navicat 10：`SHOW COLUMNS FROM `db`.`tbl`` / `... FROM tbl FROM db`
        # / `DESC `db`.`tbl`` 都会发。旧正则只抓 `(\w+)`，于是把库名
        # `default` 当成表名 → 1146 "Table 'default.default' doesn't exist"。
        m = re.search(
            r'(?:FROM|DESC(?:RIBE)?)\s+'
            r'(?:[`"]?(\w+)[`"]?\s*\.\s*)?[`"]?(\w+)[`"]?'
            r'(?:\s+(?:FROM|IN)\s+[`"]?(\w+)[`"]?)?'
            r'(?:\s+LIKE\s+(.+?))?\s*;?\s*$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            self._send_packet(mp.make_err_packet(
                1064, "42000", "Syntax error"))
            return

        table_name = m.group(2)
        db_name = m.group(1) or m.group(3)
        like_raw = (m.group(4) or '').strip()
        if db_name:
            resolved, err = self._resolve_database(db_name)
            if err is not None:
                self._send_packet(err)
                return
        else:
            resolved = self.database or 'default'
        # 权限先于存在性判断：否则无权限账户能靠 1146/1142 的差异探测
        # "这个表存不存在"（MySQL 对无权限的表同样先报 1142）。
        access_err = self._access_error_packet(
            'SELECT', table=table_name)
        if access_err is not None:
            self._send_packet(access_err)
            return
        if table_name not in self.db.tables:
            self._send_packet(mp.make_err_packet(
                1146, "42S02",
                f"Table '{resolved}.{table_name}' doesn't exist"))
            return

        table = self.db.tables[table_name]
        # LIKE 过滤：`SHOW COLUMNS ... LIKE 'x%'`（Navicat 元数据刷新会带）
        like = None
        if like_raw:
            like = like_raw.strip().strip("'\"")

        def _name_match(name):
            if like is None:
                return True
            pattern = '^' + like.replace('%', '.*').replace('_', '.') + '$'
            return bool(re.match(pattern, str(name)))

        rows = [
            (c['name'], c['type'], c['null'], c['key'],
             c['default'], c['extra'])
            for c in describe_columns(table)
            if _name_match(c['name'])
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
            table, decode_err = self._decode_client_utf8(
                body.split(b'\x00')[0], 'COM_FIELD_LIST')
            if decode_err is not None:
                self._send_packet(decode_err)
                return
        except Exception:
            table = ''

        # 权限：COM_FIELD_LIST 返回列定义，属于表元数据 → 需要 SELECT
        if table:
            access_err = self._access_error_packet('SELECT', table=table)
            if access_err is not None:
                self._send_packet(access_err)
                return

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

    # ==================== 预处理语句（COM_STMT_*） ====================
    #
    # 设计要点（阶段 1 协议补齐）：
    # * **连接级注册表**：statement_id 从 1 递增，CLOSE/断连即释放
    #   （MySQL 的 prepared statement 也是 per-connection 的）。
    # * PREPARE 只做**静态**分析：数 `?` 占位符（跳过字符串/反引号/注释），
    #   并对 SELECT 给出结果列元数据（表列类型来自 engine 的真实列定义）。
    #   绝不为了拿元数据执行 DML —— PREPARE 一个 INSERT 不会写入任何行。
    # * EXECUTE 把二进制参数按 MySQL 文本语义**安全内联**成 SQL 字面量，
    #   再走与文本协议**完全相同**的 parser/executor/事务/权限路径
    #   （执行器没有参数化接口，内联是唯一不改执行器的做法）。
    # * 返回**二进制结果集**（EXECUTE 协议要求）；DML/DDL 回 OK；错误回 ERR
    #   （沿用 `_send_execution_result` 的 errno/SQLSTATE 契约）。
    # * 未实现边界：COM_STMT_SEND_LONG_DATA 只做登记（该命令按协议无响应），
    #   随后 EXECUTE 显式报 1235；游标（CURSOR flag / COM_STMT_FETCH）不支持
    #   —— EXECUTE 直接回全量结果集，绝不伪装成"游标已打开"。

    @staticmethod
    def _iter_sql_chars(sql: str):
        """逐字符产出 `(下标, 字符, 是否在"可执行代码"区)`

        引号（`'` / `"` / 反引号）、`#` / `-- ` 行注释、`/* */` 块注释里的
        字符 `is_code=False`。字符串转义遵循**本引擎解析器**的语义：
        只有成对引号是转义（`''`），反斜杠按字面保留 —— 这样"引号外的
        `?`"与执行器真正的占位位置一致。
        """
        i, n = 0, len(sql)
        state = None            # None=代码区；否则是当前引号/注释状态
        while i < n:
            ch = sql[i]
            if state is not None:
                yield i, ch, False
                if state in ("'", '"', '`'):
                    if ch == state:
                        if i + 1 < n and sql[i + 1] == state:
                            # `''` / `""` / ``` `` ```：成对引号 = 一个字面引号
                            yield i + 1, sql[i + 1], False
                            i += 1
                        else:
                            state = None
                elif state == '/*':
                    if ch == '*' and sql.startswith('*/', i):
                        yield i + 1, '/', False
                        i += 1
                        state = None
                else:           # '#' / '-- ' 行注释：到行尾结束
                    if ch in '\r\n':
                        state = None
                i += 1
                continue

            # ---- 正常代码区 ----
            if ch in ("'", '"', '`'):
                state = ch
                yield i, ch, False
            elif ch == '#':
                state = '#'
                yield i, ch, False
            elif ch == '/' and sql.startswith('/*', i):
                state = '/*'
                yield i, ch, False
            elif (ch == '-' and sql.startswith('--', i)
                  and (i + 2 >= n or sql[i + 2] in ' \t\r\n')):
                state = '--'
                yield i, ch, False
            else:
                yield i, ch, True
            i += 1

    @classmethod
    def _count_placeholders(cls, sql: str) -> int:
        """统计 `?` 占位符个数（引号/注释里的 `?` 不算）"""
        return sum(1 for _i, ch, is_code in cls._iter_sql_chars(sql)
                   if is_code and ch == '?')

    @staticmethod
    def _quote_sql_string(text: str) -> str:
        """把 Python 字符串内联成 SQL 字面量（无注入面）

        优先选一种**值里不存在**的引号包裹；两种引号都有时用单引号并把
        `'` 写成 `''`（本引擎解析器的成对引号转义）。反斜杠不做转义
        （引擎按字面保留），因此值不会被改写。
        """
        if "'" not in text:
            return "'" + text + "'"
        if '"' not in text:
            return '"' + text + '"'
        return "'" + text.replace("'", "''") + "'"

    def _sql_literal(self, value):
        """把 EXECUTE 解出的一个参数值内联成 SQL 字面量

        支持 int / float / str / bytes(UTF-8) / None；其它（含 NaN/Inf 与
        非 UTF-8 二进制）显式报 1235，绝不静默转成"看起来像"的值。
        """
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, int):
            return str(int(value))
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise mp.StmtProtocolError(
                    1235, 'NaN/Infinity 不能作为 SQL 参数（MySQL 文本协议'
                          '没有对应字面量）')
            return repr(float(value))
        if isinstance(value, (bytes, bytearray)):
            try:
                text = bytes(value).decode('utf-8')
            except UnicodeDecodeError:
                raise mp.StmtProtocolError(
                    1235, 'BLOB 参数不是合法 UTF-8 文本：本引擎以文本字面量'
                          '内联参数，二进制内容无法无损表达（暂不支持）')
            return self._quote_sql_string(text)
        if isinstance(value, str):
            return self._quote_sql_string(value)
        raise mp.StmtProtocolError(
            1235, f'不支持的参数值类型: {type(value).__name__}')

    def _substitute_prepared_params(self, sql: str, values) -> str:
        """把 SQL 里引号/注释之外的 `?` 依次替换成参数的字面量"""
        out = []
        index = 0
        total = len(values)
        for _pos, ch, is_code in self._iter_sql_chars(sql):
            if is_code and ch == '?':
                if index >= total:
                    raise mp.StmtProtocolError(
                        1210, '参数个数与占位符不匹配'
                              '（SQL 里的 `?` 多于提供的参数）')
                out.append(self._sql_literal(values[index]))
                index += 1
            else:
                out.append(ch)
        if index != total:
            raise mp.StmtProtocolError(
                1210, '参数个数与占位符不匹配'
                      '（提供的参数多于 SQL 里的 `?`）')
        return ''.join(out)

    # ---------- PREPARE：静态元数据（绝不执行语句） ----------

    @classmethod
    def _find_top_level_keyword(cls, text: str, keyword: str):
        """在顶层（引号/括号之外）找关键字位置；找不到返回 None"""
        kw = keyword.upper()
        depth = 0
        n = len(text)
        for i, _ch, is_code in cls._iter_sql_chars(text):
            if not is_code:
                continue
            ch = text[i]
            if ch == '(':
                depth += 1
                continue
            if ch == ')':
                depth = max(0, depth - 1)
                continue
            if depth != 0 or ch.upper() != kw[0]:
                continue
            if text[i:i + len(kw)].upper() != kw:
                continue
            before = text[i - 1] if i > 0 else ' '
            after = text[i + len(kw)] if i + len(kw) < n else ' '
            if (not (before.isalnum() or before == '_')
                    and not (after.isalnum() or after == '_')):
                return i
        return None

    @staticmethod
    def _split_select_item(item: str):
        """把 SELECT 列表里的一个项拆成 `(表达式, 别名)`"""
        item = item.strip()
        if not item:
            return '', None
        m = re.match(
            r'(?is)^(.*?)\s+AS\s+[`"]?([\w$]+)[`"]?\s*$', item)
        if m:
            return m.group(1).strip(), m.group(2)
        # 无 AS 的尾部别名（`COUNT(*) cnt` / `id x`）：别名前必须有空白
        m = re.match(
            r'(?is)^(.*\S)\s+[`"]?([A-Za-z_$][\w$]*)[`"]?\s*$', item)
        if m:
            return m.group(1).strip(), m.group(2)
        return item, None

    @staticmethod
    def _column_display_name(expr: str) -> str:
        """结果列名：裸列取列名（去掉表限定/反引号），表达式取原文"""
        text = str(expr or '').strip()
        if re.fullmatch(r'[`"]?[\w$]+(?:\.[`"]?[\w$]+[`"]?)*', text):
            return text.split('.')[-1].strip('`"')
        return (text[:64] if text else '?')

    def _prepared_table_columns(self, table):
        """真实表 → `[(列名, 协议类型, 显示长度)]`；不是真实表返回 None"""
        name = str(table or '').strip().strip('`"')
        if '.' in name:
            name = name.split('.')[-1]
        table_obj = None
        try:
            table_obj = self.db.tables.get(name)
        except Exception:                     # pragma: no cover - 防御
            table_obj = None
        if table_obj is None:
            return None
        try:
            cols = describe_columns(table_obj)
        except Exception:                     # pragma: no cover - 防御
            return None
        return [(c['name'], self._mysql_type_for(c['type']),
                 self._mysql_len_for(c['type'])) for c in cols]

    def _prepared_result_metadata(self, sql: str):
        """静态提取 SELECT 的结果列元数据 → `(columns, table)`

        * 简单单表 SELECT：列类型来自表定义（不取数据、不执行语句）；
        * FROM 子句复杂（多表/JOIN/子查询）或 `SELECT *` 无法静态展开：
          返回 0 列 —— 协议允许 PREPARE 报 0 列，客户端以 EXECUTE 时的
          实际列定义为准；
        * 非 SELECT（INSERT/UPDATE/DDL…）：0 列、table=None。
        """
        m = re.match(r'(?is)^\s*SELECT\s+(.*)$', sql)
        if not m:
            return [], None
        rest = m.group(1)
        table = None
        from_idx = self._find_top_level_keyword(rest, 'FROM')
        if from_idx is None:
            select_part = rest
        else:
            select_part = rest[:from_idx]
            after = rest[from_idx + len('FROM'):].strip()
            if not after.startswith('('):
                tm = re.match(r'[`"]?([A-Za-z_][\w.$]*)[`"]?', after)
                table = tm.group(1) if tm else None
                # `FROM a, b` / `FROM a JOIN b`：不静态展开列（避免报错元数据）
                if (self._find_top_level_keyword(after, 'JOIN') is not None
                        or len(self._split_set_assignments(after)) > 1):
                    table = None
        table_cols = self._prepared_table_columns(table) if table else None
        by_name = ({c[0]: (c[1], c[2]) for c in table_cols}
                   if table_cols else {})
        select_part = select_part.strip()
        if not select_part:
            return [], None
        out = []
        for item in self._split_set_assignments(select_part):
            expr, alias = self._split_select_item(item)
            if not expr:
                continue
            if expr == '*' or expr.endswith('.*'):
                if table_cols is None:
                    return [], None       # 无法静态展开 → 回 0 列
                out.extend(table_cols)
                continue
            bare = None
            # 裸列名 / 限定列名（`t.id`）：取最后一节去匹配表定义（类型/长度
            # 与真实列一致，SQL 里写 `t.id` 不代表结果值变成字符串）
            bm = re.fullmatch(
                r'[`"]?([A-Za-z_][\w$]*)[`"]?'
                r'(?:\.[`"]?([A-Za-z_][\w$]*)[`"]?)?', expr)
            if bm:
                bare = bm.group(2) or bm.group(1)
            name = alias or (bare or self._column_display_name(expr))
            if bare and bare in by_name:
                ctype, length = by_name[bare]
            elif expr in by_name:
                ctype, length = by_name[expr]
            elif re.fullmatch(r'-?\d+', expr):
                ctype, length = mp.MYSQL_TYPE_LONGLONG, 20
            elif re.fullmatch(r'-?\d+\.\d+', expr):
                ctype, length = mp.MYSQL_TYPE_DOUBLE, 22
            else:
                ctype, length = mp.MYSQL_TYPE_VAR_STRING, 255
            out.append((name, ctype, length))
            if len(out) > 4096:           # 防滥用（协议列数上限 2^16）
                return [], None
        return out, table

    # ---------- COM_STMT_PREPARE / EXECUTE / CLOSE / RESET ----------

    def _handle_stmt_prepare(self, body: bytes):
        sql, decode_err = self._decode_client_utf8(body, 'COM_STMT_PREPARE')
        if decode_err is not None:
            self._send_packet(decode_err)
            return
        sql = sql.strip()
        while sql.endswith(';'):
            sql = sql[:-1].rstrip()
        if not sql:
            self._send_packet(mp.make_err_packet(
                1064, '42000', '预处理语句为空'))
            return

        num_params = self._count_placeholders(sql)
        columns, table = self._prepared_result_metadata(sql)

        # 连接级数量上限（防"只 PREPARE 不 CLOSE"把服务端内存吃光）
        if len(self.prepared_stmts) >= MAX_PREPARED_STMTS:
            self._send_packet(mp.make_err_packet(
                1461, '42000',
                f"Can't create more than max_prepared_stmt_count "
                f"statements ({MAX_PREPARED_STMTS})"))
            return

        # 权限：能静态识别出 SELECT 的表时，PREPARE 阶段就先做一次
        # SELECT 鉴权 —— 否则受限用户可以通过 PREPARE 拿到他没有权限的
        # 表的列名（元数据泄露），与文本协议 SHOW COLUMNS 的鉴权口径不符。
        if table:
            access_err = self._access_error_packet('SELECT', table=table)
            if access_err is not None:
                self._send_packet(access_err)
                return

        stmt_id = self._next_stmt_id
        self._next_stmt_id += 1
        self.prepared_stmts[stmt_id] = {
            'id': stmt_id,
            'sql': sql,
            'num_params': num_params,
            # 参数类型缓存：客户端第二次 EXECUTE 可以 new_params_bound_flag=0
            # 复用上一次的类型（MySQL 协议允许），RESET 会清掉。
            'param_types': None,
            # COM_STMT_SEND_LONG_DATA 是否收到过（未实现累积 → EXECUTE 报 1235）
            'long_data': False,
            'columns': columns,
            'num_columns': len(columns),
            'exec_count': 0,
        }

        self._send_packet(mp.make_prepare_ok_packet(
            stmt_id, len(columns), num_params))
        # 参数列定义（客户端执行前只知道个数；类型在执行时才绑定）
        if num_params > 0:
            for _ in range(num_params):
                self._send_packet(mp.make_column_definition(
                    '?', mp.MYSQL_TYPE_VAR_STRING, 255))
            self._send_packet(
                mp.make_eof_packet(status_flags=self._status_flags()))
        # 结果列定义（简单 SELECT 才静态给出；复杂查询为 0 列）
        if columns:
            for name, ctype, length in columns:
                self._send_packet(
                    mp.make_column_definition(name, ctype, length))
            self._send_packet(
                mp.make_eof_packet(status_flags=self._status_flags()))

    def _handle_stmt_execute(self, body: bytes):
        if len(body) < 4:
            self._send_packet(mp.make_err_packet(
                1210, 'HY000',
                'Incorrect arguments to mysqld_stmt_execute'
                '（EXECUTE 包体缺少 statement_id）'))
            return
        stmt_id = struct.unpack_from('<I', body, 0)[0]
        stmt = self.prepared_stmts.get(stmt_id)
        if stmt is None:
            self._send_packet(mp.make_err_packet(
                1243, 'HY000',
                f'Unknown prepared statement handler ({stmt_id}) '
                f'given to mysqld_stmt_execute'))
            return

        # 客户端用过 COM_STMT_SEND_LONG_DATA（长数据流式传参）：本引擎没有
        # 累积长数据，参数值会缺失 —— 显式报 1235，绝不拿半截数据执行。
        if stmt.get('long_data'):
            stmt['long_data'] = False       # 语义上本次长数据已被"消费"
            self._send_packet(mp.make_err_packet(
                1235, '42000',
                'COM_STMT_SEND_LONG_DATA（长数据参数）暂不支持：'
                '本引擎的预处理语句在 EXECUTE 包里一次性接收参数，'
                '不做流式累积；请改用普通文本参数发送'))
            return

        try:
            req = mp.StmtExecuteRequest(
                body, stmt['num_params'], stmt.get('param_types'))
        except mp.StmtProtocolError as e:
            self._send_packet(mp.make_err_packet(
                e.errno, e.sqlstate, e.message[:200]))
            return
        # 缓存本次绑定的参数类型（下次 new_params_bound_flag=0 时复用）
        stmt['param_types'] = req.param_types
        stmt['exec_count'] = int(stmt.get('exec_count') or 0) + 1

        try:
            sql = self._substitute_prepared_params(
                stmt['sql'], req.params)
        except mp.StmtProtocolError as e:
            self._send_packet(mp.make_err_packet(
                e.errno, e.sqlstate, e.message[:200]))
            return

        # 与文本协议一样登记"正在执行"状态（SHOW PROCESSLIST 的
        # Command/Info/Time 必须同口径真实）。Info 用**带占位符的原文**，
        # 不把展开后的参数值暴露给能看 PROCESSLIST 的其它会话。
        self._update_session(command='Query', info=stmt['sql'][:200],
                             stmt_started=time.time())
        try:
            self._execute_sql_text(sql, send_result=self._send_prepared_result)
        finally:
            self._update_session(command='Sleep', info='',
                                 stmt_started=None)

    def _handle_stmt_close(self, body: bytes):
        """COM_STMT_CLOSE：释放语句，**无响应包**（协议规定）"""
        if len(body) >= 4:
            stmt_id = struct.unpack_from('<I', body, 0)[0]
            self.prepared_stmts.pop(stmt_id, None)

    def _handle_stmt_send_long_data(self, body: bytes):
        """COM_STMT_SEND_LONG_DATA：未实现累积，只登记以便 EXECUTE 显式报错

        协议上该命令**没有响应包**（客户端发完不读），因此这里不发任何包。
        """
        if len(body) < 4:
            return
        stmt_id = struct.unpack_from('<I', body, 0)[0]
        stmt = self.prepared_stmts.get(stmt_id)
        if stmt is not None:
            stmt['long_data'] = True

    def _handle_stmt_reset(self, body: bytes):
        """COM_STMT_RESET：清掉绑定参数类型缓存（回到 PREPARE 后状态）"""
        if len(body) < 4:
            self._send_packet(mp.make_err_packet(
                1210, 'HY000',
                'Incorrect arguments to mysqld_stmt_reset'
                '（RESET 包体缺少 statement_id）'))
            return
        stmt_id = struct.unpack_from('<I', body, 0)[0]
        stmt = self.prepared_stmts.get(stmt_id)
        if stmt is None:
            self._send_packet(mp.make_err_packet(
                1243, 'HY000',
                f'Unknown prepared statement handler ({stmt_id}) '
                f'given to mysqld_stmt_reset'))
            return
        stmt['param_types'] = None
        stmt['long_data'] = False
        self._send_packet(self._ok_packet())

    # ---------- 预处理结果回包（EXECUTE 用二进制结果集） ----------

    def _send_prepared_result(self, result, parsed):
        """EXECUTE 回包：结果集用**二进制行格式**，DML/DDL 用 OK，错误用 ERR"""
        if isinstance(result, dict) and result.get('type') == 'resultset':
            cols, rows = self._extract_result(result, parsed)
            self._send_binary_resultset(cols, rows)
            return
        # 非结果集路径与文本协议完全一致（affected rows / OK / ERR + SQLSTATE）
        self._send_execution_result(result, parsed)

    def _binary_columns(self, columns, rows):
        """把结果列推断成**全行一致**的协议类型（二进制行按此编码）

        不能用 `_infer_columns` 的"采样首值"推断：若某列前 100 行是 int、
        后面混入 str，声明成 LONG 却按字符串编码会让客户端字节错位。
        这里对**所有**非 NULL 值做一致性判断，混类型一律降级 VAR_STRING。
        """
        out = []
        n = len(columns)
        for ci in range(n):
            values = [r[ci] for r in rows
                      if ci < len(r) and r[ci] is not None]
            name = columns[ci]
            if not values:
                out.append((name, mp.MYSQL_TYPE_VAR_STRING, 255))
                continue
            if all(isinstance(v, bool) for v in values):
                out.append((name, mp.MYSQL_TYPE_TINY, 1))
            elif all(isinstance(v, int) and not isinstance(v, bool)
                     for v in values):
                if all(-2147483648 <= v <= 2147483647 for v in values):
                    out.append((name, mp.MYSQL_TYPE_LONG, 11))
                else:
                    out.append((name, mp.MYSQL_TYPE_LONGLONG, 20))
            elif all(isinstance(v, float) for v in values):
                out.append((name, mp.MYSQL_TYPE_DOUBLE, 22))
            elif all(isinstance(v, (bytes, bytearray)) for v in values):
                out.append((name, mp.MYSQL_TYPE_BLOB, 65535))
            else:
                max_len = max((len(str(v)) for v in values), default=0)
                out.append((name, mp.MYSQL_TYPE_VAR_STRING,
                            min(max(max_len * 4, 64), 65535)))
        return out

    def _send_binary_resultset(self, columns, rows):
        """二进制结果集：列数 + 列定义 + EOF + 二进制行 + EOF"""
        enc = self._result_codec()
        meta = self._binary_columns(columns, rows)
        self._send_packet(mp.write_lenenc_int(len(meta)))
        for name, ctype, length in meta:
            self._send_packet(
                mp.make_column_definition(name, ctype, length,
                                          encoding=enc))
        self._send_packet(
            mp.make_eof_packet(status_flags=self._status_flags()))
        for row in rows:
            self._send_packet(
                mp.make_binary_row(list(row), meta, encoding=enc))
        self._send_packet(
            mp.make_eof_packet(status_flags=self._status_flags()))

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

        # ---------- TLS/SSL（可选；ssl_enabled=True 时失败即显式抛错） ----------
        self.ssl_enabled = False
        self.ssl_context = None
        self._init_ssl_context()

        # ★ task-70：真实运行期指标（连接/QPS/错误码/慢查询由执行器侧采集）
        self.runtime_metrics = ServerRuntimeMetrics()
        self.metrics_server = None
        self.metrics_url = None

        super().__init__(
            (config.host, config.port),
            MySQLHandler)

        # 内嵌 /metrics（LIGHTDB_METRICS_PORT>0 时启动；失败只告警不阻断 DB）
        try:
            self.metrics_server = maybe_start_metrics(self)
            if self.metrics_server is not None:
                self.metrics_url = self.metrics_server.url
        except Exception as e:                   # pragma: no cover - 防御
            logger.warn(f"指标端点初始化失败（数据库继续运行）: {e}")
            self.metrics_server = None
            self.metrics_url = None

        # ★ task-71：连接池（worker 线程复用）。
        #   默认启用；`LIGHTDB_POOL_ENABLED=0` 退回"每连接一线程"旧行为。
        #   池化的是**线程**：每个连接仍独占一个 handler/session（见 pool.py
        #   模块 docstring 的会话隔离/长事务说明）。
        self.pool = None
        try:
            self.pool = ConnectionPool(self)
            if not self.pool.enabled:
                logger.info("连接池已禁用（LIGHTDB_POOL_ENABLED=0）："
                            "每连接新建线程")
                self.pool = None
            else:
                snap = self.pool.snapshot()
                logger.info(
                    f"连接池已启用: max_workers={snap['max_workers']} "
                    f"min_idle={snap['min_idle']} "
                    f"idle_timeout={snap['idle_timeout']}s "
                    f"overflow={snap['overflow']}")
        except Exception as e:                   # pragma: no cover - 防御
            logger.warn(f"连接池初始化失败（回退每连接一线程）: {e}")
            self.pool = None

    # ==================== 连接池接入（task-71） ====================

    def process_request(self, request, client_address):
        """socketserver 钩子：把已 accept 的连接交给连接池

        池禁用时回退父类实现（每条连接新建线程，保持旧行为）。
        """
        pool = getattr(self, 'pool', None)
        if pool is None:
            return super().process_request(request, client_address)
        return pool.submit(request, client_address)

    def _serve_pooled_connection(self, request, client_address):
        """池化 worker 内处理一条完整连接（父类的 per-connection 流程）"""
        self.process_request_thread(request, client_address)

    def _pool_reject_connection(self, request, reason: str,
                                code: int = 1040):
        """连接池超限/排队超时：握手前显式回 ERR 并关闭

        注意：连接已通过 `verify_request`（准入计数 +1），这里必须走
        `shutdown_request` 把计数减回去，否则会被误算成"还在服务中"。
        """
        logger.warn(f"拒绝连接 {reason}")
        try:
            self._reject_connection(request, code, '08004', reason)
        except Exception:                         # pragma: no cover - 防御
            pass
        try:
            self.shutdown_request(request)
        except Exception:                         # pragma: no cover - 防御
            pass
        try:
            self.runtime_metrics.on_connection_rejected()
        except Exception:                         # pragma: no cover - 防御
            pass

    def shutdown(self):
        """先停连接池/指标 HTTP 线程，再走 socketserver 的正常关闭"""
        pool = getattr(self, 'pool', None)
        if pool is not None:
            try:
                pool.shutdown()
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"停止连接池失败: {e}")
            self.pool = None
        metrics = getattr(self, 'metrics_server', None)
        if metrics is not None:
            try:
                metrics.stop()
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"停止指标端点失败: {e}")
            self.metrics_server = None
        try:
            super().shutdown()
        except Exception:                        # pragma: no cover - 防御
            pass

    def _init_ssl_context(self):
        """按配置初始化服务端 TLS 上下文。

        - `ssl_cert`/`ssl_key` 显式指定 → 直接用；
        - 否则尝试 `<data_dir>/ssl/` 已有证书，或调用 SSLManager（openssl CLI）
          生成自签证书；
        - 都不行 → **抛 RuntimeError**（启动失败），绝不静默降级成明文。
        """
        if not bool(getattr(self.config, 'ssl_enabled', False)):
            return
        cert = str(getattr(self.config, 'ssl_cert', '') or '').strip()
        key = str(getattr(self.config, 'ssl_key', '') or '').strip()
        if not cert or not key:
            cert_dir = os.path.join(
                str(getattr(self.config, 'data_dir', '') or 'data'), 'ssl')
            try:
                from ..security.ssl_support import SSLManager
                mgr = SSLManager(cert_dir)
                if not mgr.ensure_certificates():
                    raise RuntimeError('证书生成失败')
                cert, key = str(mgr.cert_file), str(mgr.key_file)
            except Exception as e:            # noqa: BLE001
                raise RuntimeError(
                    'ssl_enabled=True 但未提供 ssl_cert/ssl_key，且无法自动生成'
                    f'证书（需要 openssl CLI 或显式证书路径）: {e}') from e
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
        except Exception as e:                # noqa: BLE001
            raise RuntimeError(
                f'ssl_enabled=True 但证书加载失败 (cert={cert!r}, '
                f'key={key!r}): {e}') from e
        self.ssl_enabled = True
        self.ssl_context = ctx
        logger.info(f"TLS 已启用（证书: {cert}）")

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
            try:
                self.runtime_metrics.on_connection_rejected()
            except Exception:                     # pragma: no cover - 防御
                pass
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
            try:
                self.runtime_metrics.on_connection_rejected()
            except Exception:                     # pragma: no cover - 防御
                pass
            self._reject_connection(
                request, 1040, "08004", reject_reason)
            return False
        try:
            self.runtime_metrics.on_connection_accepted()
        except Exception:                         # pragma: no cover - 防御
            pass
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
            stats = {
                'active_connections': self._active_connections,
                'active_by_ip': dict(self._active_by_ip),
                'max_connections': self.max_connections,
                'max_conn_per_ip': self.max_conn_per_ip,
            }
        # 累计计数（task-70 /metrics 使用；真实打点见 verify_request）
        try:
            snap = self.runtime_metrics.snapshot()
            stats.update({
                'connections_total': snap.get('connections_total', 0),
                'connections_rejected': snap.get(
                    'connections_rejected', 0),
                'auth_failures': snap.get('auth_failures', 0),
            })
        except Exception:                         # pragma: no cover - 防御
            pass
        return stats

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