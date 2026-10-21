"""数据库连接封装（基于 PyMySQL）"""
import threading
from contextlib import contextmanager
from typing import Optional, List, Tuple, Any

try:
    import pymysql
    import pymysql.cursors
    import pymysql.err
except ImportError:
    pymysql = None

# 让 DBQueryError 同时是 pymysql 的错误类型：上层若按 `pymysql.err.Error`
# / `MySQLError` 捕获，行为不变（只是多带了 errno/sqlstate/message 字段）。
_DBErrorBase = pymysql.err.MySQLError if pymysql is not None else Exception


# ==================== 错误信息提取（不许丢字段） ====================
# 背景：pymysql 把错误码放在 args[0]、报文放在 args[1]，只有部分异常带
# sqlstate 属性。以前这里统一 `str(e)`，于是 `errno` / `sqlstate` 这两个
# 诊断时最关键的字段在上层全部丢失（GUI 只能显示一句光秃秃的文本）。
# 下面两个函数把 (errno, sqlstate, message) 完整抠出来，供 DBQueryError
# 与 GUI 复用。

def error_sqlstate(exc: BaseException) -> Optional[str]:
    """取异常上的 SQLSTATE；取不到返回 None（不编造）。"""
    if exc is None:
        return None
    st = getattr(exc, 'sqlstate', None)
    if st:
        return str(st)
    return None


def error_code(exc: BaseException) -> Optional[int]:
    """取 MySQL 错误码（errno）。取不到返回 None。"""
    if exc is None:
        return None
    en = getattr(exc, 'errno', None)
    if isinstance(en, int):
        return en
    args = getattr(exc, 'args', None) or ()
    if args and isinstance(args[0], int):
        return args[0]
    return None


def error_message(exc: BaseException) -> str:
    """取完整错误报文（不截断、不丢后续参数）。"""
    if exc is None:
        return ''
    # 本模块的 DBQueryError 自带 message 字段；必须**优先**取它。
    # 否则 errno=None 时 args[0] 不是 int，会落到 `str(exc)` →
    # DBQueryError.__str__ → format_db_error → error_message → 无限递归
    # （2026-10-21 P1 真实服务端探测发现；此前只测了 errno 为 int 的路径）。
    raw = getattr(exc, 'message', None)
    if isinstance(raw, str) and raw:
        return raw
    args = getattr(exc, 'args', None) or ()
    if args and isinstance(args[0], int):
        rest = [str(a) for a in args[1:]]
        if rest:
            return ' | '.join(rest) if len(rest) > 1 else rest[0]
        # 只有错误码、没有报文：仍然把码报出来
        return f'(错误码 {args[0]}，服务端未提供报文)'
    return str(exc)


def format_db_error(exc: BaseException, sql: str = None) -> str:
    """把数据库错误格式化成可读、可复制的完整文本。

    形如::

        错误码  : 1064
        SQLSTATE: 42000
        报文    : You have an error in your SQL syntax ...
        SQL     : SELCT bad syntax

    字段缺失时写「(未提供)」，绝不静默省略，也绝不截断报文。
    """
    code = error_code(exc)
    state = error_sqlstate(exc)
    msg = error_message(exc)
    lines = [
        f"错误码  : {code if code is not None else '(未提供)'}",
        f"SQLSTATE: {state if state else '(未提供)'}",
        f"报文    : {msg}",
    ]
    if sql:
        lines.append(f"SQL     : {sql}")
    return '\n'.join(lines)


class DBQueryError(_DBErrorBase):
    """带完整诊断字段的数据库错误。

    属性：
        errno     MySQL 错误码（int 或 None）
        sqlstate  SQLSTATE（str 或 None）
        message   服务端完整报文（不截断）
        sql       触发错误的 SQL（可为 None）

    `str(exc)` 与 `format_db_error` 一致，因此任何只做 `str(e)` 的旧代码
    也至少能看到错误码 + SQLSTATE + 完整报文。

    `exc.args` 保持与 pymysql 原生异常同形 —— `(errno, message)`，
    因此历史上写 `code, msg = e.args` 或 `code = e.args[0]` 的代码继续可用。
    """

    def __init__(self, errno=None, sqlstate=None, message='', sql=None,
                 cause: BaseException = None):
        self.errno = errno
        self.sqlstate = sqlstate
        self.message = message or ''
        self.sql = sql
        self.cause = cause
        # 与 pymysql.err.MySQLError 同形：args = (errno, message)
        super().__init__(errno, self.message)

    def text(self) -> str:
        return format_db_error(self, sql=self.sql)

    def failure_record(self) -> dict:
        """失败清单里的一条记录（结构稳定，便于断言/展示）。"""
        return {
            'errno': self.errno,
            'sqlstate': self.sqlstate,
            'message': self.message,
            'sql': self.sql,
            'text': self.text(),
        }

    def __iter__(self):
        # 兼容 `code, msg = e` 写法
        return iter(self.args)

    def __str__(self):
        return self.text()


def _raise_db_error(exc: BaseException, sql: str = None):
    """把底层异常包装成 DBQueryError 上抛——绝不吞掉，也绝不丢字段。"""
    raise DBQueryError(
        errno=error_code(exc),
        sqlstate=error_sqlstate(exc),
        message=error_message(exc),
        sql=sql,
        cause=exc,
    ) from exc


class DBConnection:
    """单条数据库连接（会话）"""

    def __init__(self, config, read_timeout=None):
        """
        config: ConnectionInfo
        read_timeout: 可选 pymysql 读超时（秒）。None = 不限（沿用旧行为）。
                      主窗口的后台查询另有 QTimer→KILL 的超时兜底（P1-1）。
        """
        self.config = config
        self.conn: Optional['pymysql.connections.Connection'] = None
        self.lock = threading.RLock()
        self.server_version = ""
        try:
            self.read_timeout = int(read_timeout) if read_timeout else None
        except (TypeError, ValueError):
            self.read_timeout = None

    def connect(self):
        if pymysql is None:
            raise RuntimeError("请先安装 pymysql: pip install pymysql")

        ssl_param = None
        if getattr(self.config, 'ssl', False):
            ssl_param = {'check_hostname': False}

        self.conn = pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database or None,
            charset=self.config.charset or 'utf8mb4',
            autocommit=True,
            connect_timeout=10,
            read_timeout=self.read_timeout,
            ssl=ssl_param,
            cursorclass=pymysql.cursors.Cursor,
        )
        with self.conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            row = cur.fetchone()
            self.server_version = row[0] if row else "unknown"
        return self

    def is_connected(self) -> bool:
        if self.conn is None:
            return False
        try:
            self.conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    def close(self):
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None

    @contextmanager
    def cursor(self):
        if self.conn is None:
            self.connect()
        cur = self.conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # ============ 查询接口 ============

    def query(self, sql: str, params=None) -> Tuple[List[str], List[tuple]]:
        """执行查询，返回 (列名, 行列表)。

        失败时抛 `DBQueryError`（**不吞错**），异常上带有
        `errno` / `sqlstate` / `message` / `sql` 四个字段。
        """
        with self.lock:
            try:
                with self.cursor() as cur:
                    cur.execute(sql, params)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = cur.fetchall()
                        return cols, list(rows)
                    # 非查询语句
                    return [], []
            except Exception as e:
                _raise_db_error(e, sql)

    def execute(self, sql: str, params=None) -> Tuple[int, Optional[str]]:
        """
        执行非查询语句
        返回 (影响行数, 错误信息)

        错误信息是 `format_db_error` 的完整文本（错误码 + SQLSTATE +
        完整报文 + SQL），不再是一句 `str(e)`。成功时错误信息为 None。
        """
        try:
            with self.lock:
                with self.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.rowcount, None
        except Exception as e:
            return 0, format_db_error(e, sql=sql)

    def execute_many(self, sql: str, params_list) -> Tuple[int, Optional[str]]:
        try:
            with self.lock:
                with self.cursor() as cur:
                    cur.executemany(sql, params_list)
                    return cur.rowcount, None
        except Exception as e:
            # executemany 的 sql 是模板（含 %s 占位），照原样带出来
            return 0, format_db_error(e, sql=sql)

    def switch_database(self, db_name: str):
        with self.lock:
            if self.conn is None:
                self.connect()
            self.conn.select_db(db_name)
            self.config.database = db_name

    def get_databases(self) -> List[str]:
        cols, rows = self.query("SHOW DATABASES")
        return [r[0] for r in rows]

    def get_tables(self, db: Optional[str] = None) -> List[str]:
        cols, rows = self.query("SHOW TABLES")
        return [r[0] for r in rows]

    def get_columns(self, table: str) -> List[dict]:
        cols, rows = self.query(f"SHOW COLUMNS FROM `{table}`")
        result = []
        for r in rows:
            # Field, Type, Null, Key, Default, Extra
            result.append({
                'name': r[0],
                'type': r[1],
                'null': r[2] if len(r) > 2 else 'YES',
                'key': r[3] if len(r) > 3 else '',
                'default': r[4] if len(r) > 4 else None,
                'extra': r[5] if len(r) > 5 else '',
            })
        return result

    # ============ 后台查询支持（P1-1 / P1-2） ============

    def thread_id(self) -> Optional[int]:
        """当前 pymysql 连接的服务器会话 id（取消查询时 KILL 用）。"""
        if self.conn is None:
            return None
        try:
            return int(self.conn.thread_id())
        except Exception:
            return None

    def query_chunked(self, sql: str, params=None, max_rows=None,
                      chunk_size: int = 1000) -> dict:
        """分块读取查询结果（P1-2 有界结果），避免 `fetchall()` 一次性进内存。

        返回 ``{'columns', 'rows', 'truncated', 'fetched', 'affected'}``：

        - `max_rows` 为 None/0 → 读到结果结束（与 query() 等价，但分块读）；
        - `max_rows > 0` → 最多返回 `max_rows` 行；若还有更多行，
          `truncated=True`（多取 1 行判断"是否还有"，该行不返回给调用方）；
        - 查询失败抛 `DBQueryError`（与 query() 相同的完整诊断字段）。
        """
        try:
            max_rows = int(max_rows) if max_rows else None
        except (TypeError, ValueError):
            max_rows = None
        if max_rows is not None and max_rows <= 0:
            max_rows = None
        chunk_size = max(50, int(chunk_size or 1000))

        with self.lock:
            try:
                with self.cursor() as cur:
                    cur.execute(sql, params)
                    if not cur.description:
                        return {'columns': [], 'rows': [], 'truncated': False,
                                'fetched': 0,
                                'affected': int(cur.rowcount or 0)}
                    cols = [d[0] for d in cur.description]
                    rows = []
                    truncated = False
                    while True:
                        if max_rows is not None:
                            want = min(chunk_size, max_rows - len(rows))
                        else:
                            want = chunk_size
                        if want <= 0:
                            # 已达上限：多读 1 行判断"是否还有更多"（不返回该行）
                            truncated = bool(cur.fetchmany(1))
                            break
                        batch = cur.fetchmany(want)
                        if not batch:
                            break
                        rows.extend(batch)
                        if len(batch) < want:
                            break
                    return {'columns': cols, 'rows': rows,
                            'truncated': truncated, 'fetched': len(rows),
                            'affected': 0}
            except Exception as e:
                _raise_db_error(e, sql)

    def kill_connection(self, thread_id) -> Tuple[bool, Optional[str]]:
        """用**另一条独立连接**对 `thread_id` 执行 `KILL <id>`（取消后台查询）。

        本引擎不支持 `KILL QUERY`（lightdb/net/server.py 显式拒绝），`KILL`
        会终止**整个目标连接** → 目标连接随后不可用，调用方需重连。
        返回 `(ok, error_text)`；取消是尽力而为路径，不抛异常。
        """
        if pymysql is None:
            return False, 'pymysql 未安装，无法发送 KILL'
        if thread_id is None:
            return False, '缺少目标连接 thread_id'
        try:
            tid = int(thread_id)
        except (TypeError, ValueError):
            return False, f'thread_id 非法: {thread_id!r}'

        ssl_param = None
        if getattr(self.config, 'ssl', False):
            ssl_param = {'check_hostname': False}
        killer = None
        try:
            killer = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                charset=self.config.charset or 'utf8mb4',
                autocommit=True,
                connect_timeout=5,
                read_timeout=5,
                ssl=ssl_param,
            )
        except Exception as e:
            return False, f'取消连接建立失败: {e}'
        try:
            with killer.cursor() as cur:
                cur.execute(f"KILL {tid}")
                try:
                    cur.fetchall()
                except Exception:
                    pass
            return True, None
        except Exception as e:
            return False, f"KILL {tid} 失败: {e}"
        finally:
            try:
                killer.close()
            except Exception:
                pass