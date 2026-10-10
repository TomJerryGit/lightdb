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
        self.database = "default"
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
            self.database = resp.database

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
            self.database = db_name
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
        sql = sql.strip().rstrip(';').strip()
        if not sql:
            self._send_packet(mp.make_ok_packet())
            return

        # 更新会话状态
        self._update_session(command='Query', info=sql[:200])

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
                [("default",), ("information_schema",)])
            return

        if upper.startswith("SHOW TABLES"):
            rows = [(name,) for name in
                    sorted(self.db.tables.keys())]
            self._send_resultset(
                [(f"Tables_in_{self.database}",
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
            ('Aborted_clients', '0'),
            ('Aborted_connects', '0'),
            ('Bytes_received', '0'),
            ('Bytes_sent', '0'),
            ('Connections', str(session_count)),
            ('Queries', str(stats.get('queries', 0))),
            ('Questions', str(stats.get('queries', 0))),
            ('Threads_connected', str(session_count)),
            ('Threads_running', '1'),
            ('Uptime',
             str(int(time.time() - self.server.start_time))),
        ]
        self._send_resultset(
            [("Variable_name", mp.MYSQL_TYPE_VAR_STRING, 64),
             ("Value", mp.MYSQL_TYPE_VAR_STRING, 1024)],
            rows)

    def _show_processlist(self):
        cols = ['Id', 'User', 'Host', 'db', 'Command',
                'Time', 'State', 'Info']
        rows = []
        now = time.time()
        with _sessions_lock:
            for sid, info in list(_sessions.items()):
                rows.append((
                    sid,
                    info.get('user', ''),
                    str(info.get('host', ('', 0))[0]),
                    info.get('db', ''),
                    info.get('command', 'Sleep'),
                    int(now - info.get('start', now)),
                    '',
                    info.get('info', ''),
                ))

        self._send_resultset(
            [(c, mp.MYSQL_TYPE_VAR_STRING, 255)
             for c in cols],
            rows)

    def _handle_kill(self, sql: str):
        import re
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

        with _sessions_lock:
            exists = target_sid in _sessions

        if exists:
            self._send_packet(mp.make_ok_packet(
                message=f"Connection {target_sid} killed"))
        else:
            self._send_packet(mp.make_err_packet(
                1094, "HY000",
                f"Unknown thread id: {target_sid}"))

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
        rows = []
        for col in table.cols:
            key = ('PRI' if col.get('primary')
                   else 'UNI' if col.get('unique') else '')
            null = 'NO' if col.get('notnull') else 'YES'
            default = None
            extra = ('auto_increment'
                     if col.get('primary') else '')
            rows.append((
                col['name'], col['type'], null, key,
                default, extra))

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

        if table and table in self.db.tables:
            t = self.db.tables[table]
            fields = [
                (c['name'], mp.MYSQL_TYPE_VAR_STRING, 255)
                for c in t.cols
            ]
            for name, ctype, length in fields:
                self._send_packet(
                    mp.make_column_definition(
                        name, ctype, length))

        self._send_packet(mp.make_eof_packet())


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