"""
从库复制管理器
- IO 线程：从主库拉取 binlog → 中继日志
- SQL 线程：从中继日志重放到本地

状态语义（`SHOW SLAVE STATUS` 的数据源）
----------------------------------------
本类在构造时把自己**注册到 `Database`**（`db.attach_replicator(self)`），
执行器渲染 `SHOW SLAVE STATUS` 时只读这里记录的**真实**运行状态与本进程
真实跟踪到的位点，不再"恒报未运行"：

* 两个运行标志按 MySQL 文本语义：`'Yes'` / `'No'` / `'Connecting'`
  （`'Connecting'` 只用于 IO 线程活着但尚未连上主库时）。
* 位点全部来自真实数据流：
  - `Read_Master_Log_Pos`  = io 线程从主库 `__BINLOG_FETCH__` 拉到的事件序号；
  - `Exec_Master_Log_Pos`  = sql 线程真正重放完的事件序号；
  - `Relay_Log_Pos` / `Relay_Log_Space` = 中继日志文件真实字节数；
  - `Master_Log_File`      = 从主库 `SHOW MASTER STATUS` 读到的文件名。
* 取不到的（GTID、server_uuid、master.info 路径、事件时间戳换算的
  `Seconds_Behind_Master`）一律 `None`，并在 `show_status()` 的注释里
  逐项说明原因，**绝不编造**。
"""
import json
import threading
import time
from collections import deque

from ..logger import logger

#: io 线程重连间隔（秒，整数）—— `SHOW SLAVE STATUS.Connect_Retry` 的真实来源
RECONNECT_DELAY = 1

#: `Slave_IO_State`：没有登记复制器时的真实说明文本
IO_STATE_UNCONFIGURED = 'No replication configured'
#: `Slave_IO_State`：已配置但从未启动
IO_STATE_NOT_STARTED = 'Replication configured but not started'
#: `Slave_IO_State`：启动过、现在停着
IO_STATE_STOPPED = 'Slave IO thread has been stopped'
#: `Slave_IO_State`：正在连主库
IO_STATE_CONNECTING = 'Connecting to master'
#: `Slave_IO_State`：连上主库、暂时没有新事件
IO_STATE_WAITING = 'Waiting for master to send event'
#: `Slave_IO_State`：连上主库、正在往中继日志里搬事件
IO_STATE_QUEUEING = 'Queueing master event to the relay log'
#: `Slave_IO_State`：掉线后重连
IO_STATE_RECONNECTING = 'Reconnecting after a failed master event read'

#: `Slave_SQL_Running_State`
SQL_STATE_APPLYING = 'Reading event from the relay log'
SQL_STATE_CAUGHT_UP = 'Slave has read all relay log; waiting for more updates'


def _errno_of(exc) -> int:
    """从异常里取真实的错误号（pymysql 异常的第一个参数就是 errno）

    取不到时返回 0 —— MySQL 的 `Last_IO_Errno` / `Last_SQL_Errno` 用 0
    表示"没有错误"，比编一个数字诚实。
    """
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return int(args[0])
    return 0


def _fmt_ts(ts) -> str:
    """MySQL 风格的错误时间戳 `YYMMDD HH:MM:SS`；没有错误时为 ''"""
    if not ts:
        return ''
    try:
        return time.strftime('%y%m%d %H:%M:%S', time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ''


class RelayLog:
    """中继日志（内存 + 磁盘）

    位点用**真实字节数**：每追加一条事件就把该行字节数累加进去，
    `Relay_Log_Pos` / `Relay_Log_Space` 直接取这个值（不是估算）。
    """

    def __init__(self, data_dir: str):
        from pathlib import Path
        self.path = Path(data_dir) / 'relay.log'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue = deque()
        self.lock = threading.RLock()
        self.fh = open(self.path, 'a', encoding='utf-8')
        # 已有内容（上次运行的残留）也算进真实位点
        try:
            self.size = self.path.stat().st_size
        except OSError:
            self.size = 0

    @property
    def file_name(self) -> str:
        return self.path.name

    def append(self, event_tuple) -> int:
        with self.lock:
            line = json.dumps(event_tuple, ensure_ascii=False, default=str)
            self.queue.append(event_tuple)
            self.fh.write(line + '\n')
            self.fh.flush()
            self.size += len(line.encode('utf-8')) + 1
            return self.size

    def pending(self) -> int:
        """还没被 SQL 线程取走的事件条数（真实队列长度）"""
        with self.lock:
            return len(self.queue)

    def pop_all(self):
        with self.lock:
            events = list(self.queue)
            self.queue.clear()
            return events

    def close(self):
        with self.lock:
            try:
                self.fh.close()
            except Exception:
                pass


class SlaveReplicator:
    """从库侧"""

    def __init__(self, db, master_host: str, master_port: int,
                 user: str = 'root', password: str = ''):
        self.db = db
        self.master_host = master_host
        self.master_port = master_port
        self.user = user
        self.password = password

        self.running = False
        self.started = False            # 是否启动过（区分"没启动"与"停了"）
        self.io_thread = None
        self.sql_thread = None
        self.last_pos = 0               # 已应用的最大事件序号（Exec_Master_Log_Pos）
        self.relay = RelayLog(str(db.data_dir))

        self.reconnect_delay = RECONNECT_DELAY

        # ★ 真实跟踪到的复制状态（全部由 io/sql 线程写入，show_status 只读快照）
        self._lock = threading.RLock()
        self.read_master_log_pos = 0    # io 线程已拉到的事件序号
        self.master_log_file = None     # 主库 SHOW MASTER STATUS 的 File（没读到就是 None）
        self.master_pos = None          # 主库最新位点（没读到就是 None）
        self.relay_master_log_file = None   # 已应用位点所属的主库日志文件
        self.io_connected = False
        self.io_ever_connected = False
        self.io_connects = 0
        self.last_io_errno = 0
        self.last_io_error = ''
        self.last_io_error_ts = 0
        self.last_sql_errno = 0
        self.last_sql_error = ''
        self.last_sql_error_ts = 0

        self.stats = {
            'io_events': 0,
            'sql_events': 0,
            'errors': 0,
            'last_error': None,     # 兼容旧调用方：最后一次(IO/SQL)错误文本
            'last_sync_ts': 0,
        }

        # ★ 把自己注册到 Database：执行器靠这条通道拿到真实状态
        attach = getattr(db, 'attach_replicator', None)
        if callable(attach):
            attach(self)

    # ============ 生命周期 ============
    def start(self):
        if self.running:
            return
        self.running = True
        self.started = True
        self.io_thread = threading.Thread(target=self._io_loop, daemon=True,
                                          name='SlaveIO')
        self.sql_thread = threading.Thread(target=self._sql_loop, daemon=True,
                                           name='SlaveSQL')
        self.io_thread.start()
        self.sql_thread.start()
        logger.info(f"从库复制已启动 (主库 {self.master_host}:{self.master_port})")

    def stop(self):
        self.running = False
        if self.io_thread:
            self.io_thread.join(timeout=3)
        if self.sql_thread:
            self.sql_thread.join(timeout=3)
        with self._lock:
            self.io_connected = False
        self.relay.close()
        logger.info("从库复制已停止")

    # ============ 错误记录 ============
    def _record_io_error(self, exc):
        with self._lock:
            self.last_io_errno = _errno_of(exc)
            self.last_io_error = str(exc)
            self.last_io_error_ts = time.time()
            self.io_connected = False
            self.stats['errors'] += 1
            self.stats['last_error'] = str(exc)

    def _record_sql_error(self, exc):
        with self._lock:
            self.last_sql_errno = _errno_of(exc)
            self.last_sql_error = str(exc)
            self.last_sql_error_ts = time.time()
            self.stats['errors'] += 1
            self.stats['last_error'] = str(exc)

    def _refresh_master_status(self, cur):
        """读主库真实位点（`SHOW MASTER STATUS`）—— `Master_Log_File` 的来源

        读不到（主库不支持 / 连接已断）就保持上一次的值，绝不用常量顶上。
        """
        try:
            cur.execute('SHOW MASTER STATUS')
            row = cur.fetchone()
        except Exception as e:
            logger.warn(f"读取主库位点失败: {e}")
            return
        if not row:
            return
        with self._lock:
            self.master_log_file = row[0]
            try:
                self.master_pos = int(row[1])
            except (TypeError, ValueError):
                self.master_pos = None

    # ============ IO 线程 ============
    def _io_loop(self):
        import pymysql
        while self.running:
            conn = None
            try:
                conn = pymysql.connect(
                    host=self.master_host,
                    port=self.master_port,
                    user=self.user,
                    password=self.password,
                    autocommit=True,
                    connect_timeout=5,
                )
                cur = conn.cursor()
                with self._lock:
                    self.io_connected = True
                    self.io_ever_connected = True
                    self.io_connects += 1
                # 连上就先读一次主库位点（即使没有新事件，位点也是真实的）
                self._refresh_master_status(cur)

                while self.running:
                    sql = (f"SELECT * FROM __BINLOG_FETCH__ "
                           f"WHERE pos > {int(self.read_master_log_pos)} "
                           f"LIMIT 500")
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                    except Exception as e:
                        self._record_io_error(e)
                        break

                    for r in rows:
                        # r = (pos, lsn, txn_id, event_type, table_name, data)
                        self.relay.append(r)
                        try:
                            pos = int(r[0])
                        except (TypeError, ValueError):
                            continue
                        with self._lock:
                            if pos > self.read_master_log_pos:
                                self.read_master_log_pos = pos
                            self.stats['io_events'] += 1

                    if not rows:
                        # 空闲时顺便刷新主库位点（Seconds_Behind_Master 判定用）
                        self._refresh_master_status(cur)
                        time.sleep(0.1)

            except Exception as e:
                self._record_io_error(e)
                logger.warn(f"从库IO线程异常: {e}")
            finally:
                with self._lock:
                    self.io_connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if self.running:
                time.sleep(self.reconnect_delay)

    # ============ SQL 线程 ============
    def _sql_loop(self):
        while self.running:
            events = self.relay.pop_all()
            if not events:
                time.sleep(0.05)
                continue

            for ev in events:
                try:
                    pos = self._apply_event(ev)
                except Exception as e:
                    self._record_sql_error(e)
                    logger.error(f"从库重放失败 pos={ev[0] if ev else '?'}: {e}")
                    continue
                with self._lock:
                    if pos is not None and pos >= self.last_pos:
                        self.last_pos = pos
                    self.relay_master_log_file = self.master_log_file
                    self.stats['sql_events'] += 1
                    self.stats['last_sync_ts'] = time.time()

    def _apply_event(self, ev):
        """重放一条从主库拉到的事件（`__BINLOG_FETCH__` 的 6 列），返回事件位点

        `__BINLOG_FETCH__` 的列序是
        `(pos, lsn, txn_id, event_type, table_name, data)`
        —— 这里必须按 6 列解包（旧实现按 5 列解包，任何事件都会
        `ValueError: too many values to unpack`，复制一行都同步不了）。
        同时兼容 5 列的旧形态 `(pos, txn_id, event_type, table_name, data)`。
        """
        if isinstance(ev, dict):
            pos = ev.get('pos')
            etype = ev.get('event_type') or ev.get('type')
            table = ev.get('table_name') or ev.get('table')
            payload = ev.get('data')
        elif len(ev) == 6:
            pos, _lsn, _txn_id, etype, table, payload = ev
        elif len(ev) == 5:
            pos, _txn_id, etype, table, payload = ev
        else:
            raise ValueError(
                f"__BINLOG_FETCH__ 事件列数非法: {len(ev)} 列（期望 6 列）")

        data = json.loads(payload) if isinstance(payload, str) else payload
        etype = str(etype or '').upper()

        if etype == 'INSERT':
            tbl = self.db.get_table(table)
            row = (data or {}).get('row', [])
            try:
                tbl.insert(row, txn_id=0)
            except ValueError:
                # 主键已存在（重复投递）——幂等跳过
                pass

        elif etype == 'UPDATE':
            tbl = self.db.get_table(table)
            pk = data['pk']
            new = data.get('new', [])
            # 生成 set_pairs（除主键外的所有列）
            set_pairs = {}
            for i, c in enumerate(tbl.cols):
                if i != tbl.pk_idx and i < len(new):
                    set_pairs[c['name']] = new[i]
            tbl.update(set_pairs,
                       {'column': tbl.pk_col, 'op': '=', 'value': pk},
                       txn_id=0)

        elif etype == 'DELETE':
            tbl = self.db.get_table(table)
            pk = data['pk']
            tbl.delete({'column': tbl.pk_col, 'op': '=', 'value': pk},
                       txn_id=0)

        elif etype == 'DDL':
            sql = (data or {}).get('sql', '')
            from ..sql.parser import parser
            from ..sql.executor import Executor
            try:
                p = parser.parse(sql)
                if p:
                    ex = Executor(self.db)
                    ex.execute(p, {})
            except Exception as e:
                logger.warn(f"DDL 重放失败: {e}")

        elif etype in ('COMMIT', 'BEGIN'):
            pass

        return pos

    # ============ 状态 ============
    def io_state(self) -> str:
        """`Slave_IO_State` 的真实文案"""
        io_alive = self._thread_alive(self.io_thread)
        if not self.running or not io_alive:
            if not self.started:
                return IO_STATE_NOT_STARTED
            return IO_STATE_STOPPED
        with self._lock:
            connected = self.io_connected
            ever = self.io_ever_connected
            backlog = self.relay.pending()
        if not connected:
            return IO_STATE_RECONNECTING if ever else IO_STATE_CONNECTING
        return IO_STATE_QUEUEING if backlog else IO_STATE_WAITING

    def sql_state(self):
        """`Slave_SQL_Running_State`；SQL 线程没在跑时 None（MySQL 同）

        注意 `sql_running()` 返回的是 MySQL 文本 `'Yes'`/`'No'`，两个都是
        非空字符串（恒真），所以这里必须**比较字符串**，不能写
        `not self.sql_running()` —— 那会让"已停止"也报出运行态文案。
        """
        if self.sql_running() != 'Yes':
            return None
        return (SQL_STATE_APPLYING if self.relay.pending()
                else SQL_STATE_CAUGHT_UP)

    @staticmethod
    def _thread_alive(th) -> bool:
        return th is not None and th.is_alive()

    def io_running(self) -> str:
        """`Slave_IO_Running`：'Yes' / 'Connecting' / 'No'"""
        if not self.running or not self._thread_alive(self.io_thread):
            return 'No'
        with self._lock:
            return 'Yes' if self.io_connected else 'Connecting'

    def sql_running(self) -> str:
        """`Slave_SQL_Running`：'Yes' / 'No'"""
        if not self.running or not self._thread_alive(self.sql_thread):
            return 'No'
        return 'Yes'

    def seconds_behind_master(self):
        """`Seconds_Behind_Master`：能算就返回秒数，算不出返回 None

        本引擎的 WAL 事件**没有时间戳**，因此无法像 MySQL 那样用
        "最后一个已应用事件的主库时间 - 现在"算延迟。这里只用**真实位点**
        做判定：
        * SQL 线程没在跑 / 主库位点未知 -> None（MySQL 同样给 NULL）；
        * 已应用位点 >= 主库最新位点 -> 0（真的追平了）；
        * 落后但事件无时间戳 -> None（**不编造**一个秒数）；
        """
        if not self.running or self.sql_running() != 'Yes':
            return None
        with self._lock:
            master_pos = self.master_pos
            exec_pos = self.last_pos
        if master_pos is None:
            return None
        if exec_pos >= master_pos:
            return 0
        return None

    def _exec_master_log_pos(self):
        """已应用位点；还没应用过任何事件时 None（不是 0 的"假位点"）"""
        with self._lock:
            return self.last_pos if self.stats['sql_events'] else None

    def _read_master_log_pos(self):
        """已拉取位点；从没连上主库时 None"""
        with self._lock:
            if not self.io_ever_connected:
                return None
            return self.read_master_log_pos

    def show_status(self) -> dict:
        """`SHOW SLAVE STATUS` 需要的**动态**列（其余静态列见执行器）

        逐列来源：
        * `Slave_IO_State`  <- `io_state()`（真实线程状态 + 连接状态文案）
        * `Master_Host` / `Master_User` / `Master_Port` <- 构造时的真实配置
        * `Connect_Retry`   <- `reconnect_delay`（io 线程真实重连间隔）
        * `Master_Log_File` <- 主库 `SHOW MASTER STATUS` 的 File
        * `Read_Master_Log_Pos` <- io 线程拉到的最大事件序号
        * `Relay_Log_File` / `Relay_Log_Pos` / `Relay_Log_Space`
                            <- 中继日志文件真实名字与字节数
        * `Relay_Master_Log_File` / `Exec_Master_Log_Pos` <- 已应用位点
        * `Slave_IO_Running` / `Slave_SQL_Running` <- 线程真实存活 + 连接状态
        * `Last_IO_Errno/Error[_Timestamp]`、`Last_SQL_Errno/Error[_Timestamp]`
                            <- 真实记录的最近一次错误（无错误 = 0 / ''）
        * `Slave_SQL_Running_State` <- SQL 线程真实状态文案
        * `Seconds_Behind_Master`   <- `seconds_behind_master()`（见其说明）
        * `Master_Server_Id` / `Master_UUID` / `Master_Info_File` /
          `Master_Retry_Count` / `Retrieved_Gtid_Set` / `Executed_Gtid_Set`
          -> None：本引擎没有 server_id / server_uuid / master.info /
          GTID 概念，**不编造**（握手包里的 server_id 是会话线程号，
          语义与 MySQL 的 `Master_Server_Id` 不同，不能拿来冒充）。
        """
        with self._lock:
            return {
                'Slave_IO_State': self.io_state(),
                'Master_Host': self.master_host,
                'Master_User': self.user,
                'Master_Port': self.master_port,
                'Connect_Retry': self.reconnect_delay,
                'Master_Log_File': self.master_log_file,
                'Read_Master_Log_Pos': self._read_master_log_pos(),
                'Relay_Log_File': self.relay.file_name,
                'Relay_Log_Pos': self.relay.size,
                'Relay_Master_Log_File': self.relay_master_log_file,
                'Slave_IO_Running': self.io_running(),
                'Slave_SQL_Running': self.sql_running(),
                'Exec_Master_Log_Pos': self._exec_master_log_pos(),
                'Relay_Log_Space': self.relay.size,
                'Seconds_Behind_Master': self.seconds_behind_master(),
                'Last_Errno': self.last_sql_errno,
                'Last_Error': self.last_sql_error,
                'Last_IO_Errno': self.last_io_errno,
                'Last_IO_Error': self.last_io_error,
                'Last_SQL_Errno': self.last_sql_errno,
                'Last_SQL_Error': self.last_sql_error,
                'Last_IO_Error_Timestamp': _fmt_ts(self.last_io_error_ts),
                'Last_SQL_Error_Timestamp': _fmt_ts(self.last_sql_error_ts),
                'Slave_SQL_Running_State': self.sql_state(),
                'Master_Server_Id': None,
                'Master_UUID': None,
                'Master_Info_File': None,
                'Master_Retry_Count': None,
                'Retrieved_Gtid_Set': None,
                'Executed_Gtid_Set': None,
            }

    def status(self) -> dict:
        with self._lock:
            snapshot = {
                'running': self.running,
                'started': self.started,
                'master_host': self.master_host,
                'master_port': self.master_port,
                'master_user': self.user,
                'master_log_file': self.master_log_file,
                'master_pos': self.master_pos,
                'read_master_log_pos': self.read_master_log_pos,
                'exec_master_log_pos': self.last_pos,
                'relay_log_pos': self.relay.size,
                'last_pos': self.last_pos,
                **self.stats,
            }
        snapshot.update({
            'io_running': self.io_running(),
            'sql_running': self.sql_running(),
            'io_state': self.io_state(),
            'seconds_behind_master': self.seconds_behind_master(),
        })
        return snapshot
