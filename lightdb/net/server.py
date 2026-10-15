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
import socket
import socketserver
import struct
import threading
import time
from datetime import datetime
from typing import Optional

from ..config import Config
from ..logger import logger
# `SHOW COLUMNS` 的列元数据来自与 information_schema / 进程内执行器**同一份**
# 实现（db/database.py），避免三条路径各编一套元数据。
from ..db.database import describe_columns
from . import mysql_protocol as mp


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
            except (socket.timeout, ConnectionResetError):
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

        # 用户校验
        user_info = self.db.users.get(self.user)
        if not user_info:
            self._send_packet(mp.make_err_packet(
                1045, "28000",
                f"Access denied for user '{self.user}'@"
                f"'{self.client_address[0]}'"))
            return False

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
            self._send_packet(mp.make_err_packet(
                1045, "28000",
                f"Access denied for user '{self.user}'@"
                f"'{self.client_address[0]}' "
                f"(using password: YES)"))
            return False

        if resp.database:
            # 登录时指定的 database 必须**校验**：不存在的库要显式报错，
            # 否则客户端以为连上了那个库（`SELECT DATABASE()` 又会报出
            # 一个不存在的名字，或旧实现那种错位值）。
            resolved, err = self._resolve_database(resp.database)
            if err is not None:
                self._send_packet(err)
                return False
            self.database = resolved
            # 会话表里的 db 列与真实会话状态保持一致
            self._update_session(db=resolved)

        self._send_packet(mp.make_ok_packet(0, 0))
        logger.info(
            f"用户 {self.user} 从 {self.client_address[0]} "
            f"登录成功 (sid={self.server_id})")
        return True

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
                BrokenPipeError):
            pass
        except Exception as e:
            logger.error(f"会话异常 (sid={self.server_id}): {e}")
        finally:
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
            self._send_packet(mp.make_ok_packet())

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
            self._update_session(db=resolved)
            self._send_packet(mp.make_ok_packet())

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
            self._send_packet(mp.make_ok_packet())
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
        # 标准 MySQL 客户端在登录成功后固定发送 SET NAMES <charset>（字符集协商）
        # 以及 SET AUTOCOMMIT/AUTOCOMMIT=1 等语句。本引擎不维护会话变量表，
        # 这里直接确认；否则 pymysql 等客户端会在连接阶段就报 1064 而无法使用。
        if upper == "SET" or upper.startswith("SET "):
            self._send_packet(mp.make_ok_packet())
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

        session = {
            'user': self.user or 'root',
            'database': self.database,
        }

        try:
            result = self.executor.execute(parsed, session)
        except Exception as e:
            logger.error(f"SQL执行错误: {e}")
            self._send_packet(mp.make_err_packet(
                1105, "HY000", str(e)[:200]))
            return

        # `USE <库>` 由执行器校验（不存在 → 1049）。成功时必须把会话状态
        # 同步回 handler：否则后续的 `SELECT DATABASE()` / `SHOW TABLES`
        # 仍按旧库回答 —— 与"切库成功"的回报直接矛盾。
        if parsed.get('command') == 'USE' and not (
                isinstance(result, dict)
                and result.get('type') == 'error'):
            self.database = session.get('database') or self.database
            self._update_session(db=self.database)

        self._send_execution_result(result, parsed)

    def _update_session(self, **kwargs):
        with _sessions_lock:
            s = _sessions.get(self.server_id)
            if s:
                s.update(kwargs)

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

        # EOF
        self._send_packet(mp.make_eof_packet())

        # 数据行
        for row in rows:
            self._send_packet(mp.make_text_row(list(row)))

        # 结束 EOF
        self._send_packet(mp.make_eof_packet())

    def _send_execution_result(self, result, parsed):
        cmd = parsed['command']

        # 执行器把异常转成错误字典返回而不抛出（见 sql/executor.py:107 _err），
        # 因此这里必须优先翻译成 ERR 包；否则所有失败都会以 OK 包回给客户端，
        # 客户端（以及测试）会把失败当成成功。
        if isinstance(result, dict) and result.get('type') == 'error':
            self._send_packet(mp.make_err_packet(
                int(result.get('code') or 1105),
                'HY000',
                str(result.get('message', 'execution error'))[:200]))
            return

        if cmd == 'INSERT':
            affected = self._extract_affected(result)
            self._send_packet(
                mp.make_ok_packet(affected_rows=affected))
            return

        if cmd in ('UPDATE', 'DELETE'):
            affected = self._extract_affected(result)
            self._send_packet(
                mp.make_ok_packet(affected_rows=affected))
            return

        if cmd in ('CREATE TABLE', 'DROP TABLE',
                   'CREATE INDEX', 'DROP INDEX',
                   'TRUNCATE', 'BEGIN', 'COMMIT',
                   'ROLLBACK', 'GRANT', 'REVOKE',
                   'SET PASSWORD', 'ALTER TABLE',
                   'USE', 'ANALYZE'):
            self._send_packet(mp.make_ok_packet())
            return

        if cmd == 'EXPLAIN':
            if result.get('type') == 'resultset':
                self._send_resultset(
                    self._infer_columns(
                        result['columns'], result['rows']),
                    result['rows'])
            else:
                self._send_packet(mp.make_ok_packet())
            return

        if cmd == 'SELECT':
            if result.get('type') != 'resultset':
                self._send_packet(mp.make_ok_packet())
                return

            cols, rows = self._extract_result(result, parsed)
            self._send_resultset(
                self._infer_columns(cols, rows), rows)
            return

        # 其他：显示消息
        self._send_packet(mp.make_ok_packet(
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
        variables = {
            'version': '5.7.30-LightDB',
            'version_comment': 'LightDB Enterprise Server',
            'character_set_client': 'utf8mb4',
            'character_set_connection': 'utf8mb4',
            'character_set_results': 'utf8mb4',
            'character_set_server': 'utf8mb4',
            'collation_connection': 'utf8mb4_general_ci',
            'collation_server': 'utf8mb4_general_ci',
            'autocommit': (
                '1' if self.db.autocommit else '0'),
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
        return variables.get(
            var.lower().lstrip('@'), '')

    def _show_variables(self, sql: str):
        import re
        variables = [
            ('auto_increment_increment', '1'),
            ('auto_increment_offset', '1'),
            ('autocommit',
             'ON' if self.db.autocommit else 'OFF'),
            ('character_set_client', 'utf8mb4'),
            ('character_set_connection', 'utf8mb4'),
            ('character_set_database', 'utf8mb4'),
            ('character_set_results', 'utf8mb4'),
            ('character_set_server', 'utf8mb4'),
            ('collation_connection', 'utf8mb4_general_ci'),
            ('collation_database', 'utf8mb4_general_ci'),
            ('collation_server', 'utf8mb4_general_ci'),
            ('have_innodb', 'YES'),
            ('innodb_buffer_pool_size', '134217728'),
            ('innodb_page_size', '16384'),
            ('max_allowed_packet', '67108864'),
            ('max_connections', '151'),
            ('port', str(self.server.config.port)),
            ('protocol_version', '10'),
            ('query_cache_size', '0'),
            ('query_cache_type', 'OFF'),
            ('sql_mode',
             'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES'),
            ('system_time_zone', 'CST'),
            ('time_zone', 'SYSTEM'),
            ('transaction_isolation', 'READ-COMMITTED'),
            ('version', '5.7.30-LightDB'),
            ('version_comment', 'LightDB Enterprise Server'),
        ]

        m = re.search(r"LIKE\s+'([^']+)'", sql,
                      re.IGNORECASE)
        if m:
            import fnmatch
            pattern = m.group(1).replace('%', '*')
            variables = [
                v for v in variables
                if fnmatch.fnmatch(v[0], pattern)
            ]

        self._send_resultset(
            [("Variable_name", mp.MYSQL_TYPE_VAR_STRING, 64),
             ("Value", mp.MYSQL_TYPE_VAR_STRING, 1024)],
            variables)

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
            self._send_packet(mp.make_ok_packet(
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
        self._send_packet(mp.make_ok_packet(
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
    """MySQL 兼容服务器"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, config: Config, db, executor):
        self.config = config
        self.db = db
        self.executor = executor
        self.start_time = time.time()
        super().__init__(
            (config.host, config.port),
            MySQLHandler)

    def start(self):
        logger.info(
            f"LightDB MySQL Server 监听 "
            f"{self.config.host}:{self.config.port}")
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务器正在关闭...")
            self.shutdown()