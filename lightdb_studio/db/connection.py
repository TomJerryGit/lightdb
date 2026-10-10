"""数据库连接封装（基于 PyMySQL）"""
import threading
from contextlib import contextmanager
from typing import Optional, List, Tuple, Any

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None


class DBConnection:
    """单条数据库连接（会话）"""

    def __init__(self, config):
        """
        config: ConnectionInfo
        """
        self.config = config
        self.conn: Optional['pymysql.connections.Connection'] = None
        self.lock = threading.RLock()
        self.server_version = ""

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
        """执行查询，返回 (列名, 行列表)"""
        with self.lock:
            with self.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    return cols, list(rows)
                # 非查询语句
                return [], []

    def execute(self, sql: str, params=None) -> Tuple[int, Optional[str]]:
        """
        执行非查询语句
        返回 (影响行数, 错误信息)
        """
        try:
            with self.lock:
                with self.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.rowcount, None
        except Exception as e:
            return 0, str(e)

    def execute_many(self, sql: str, params_list) -> Tuple[int, Optional[str]]:
        try:
            with self.lock:
                with self.cursor() as cur:
                    cur.executemany(sql, params_list)
                    return cur.rowcount, None
        except Exception as e:
            return 0, str(e)

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