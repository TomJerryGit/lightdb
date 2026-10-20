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
* 取不到的（GTID、server_uuid、事件时间戳换算的 `Seconds_Behind_Master`）
  一律 `None`，并在 `show_status()` 的注释里逐项说明原因，**绝不编造**。
* 决策记录（决策 D 阶段 2）：**GTID 不实现** —— 复制只按
  "binlog 文件 + 位点"进行；`Retrieved_Gtid_Set` / `Executed_Gtid_Set`
  保持 NULL（不冒充 MySQL 语义）。**半同步不实现**（死代码已删除）。
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

    取不到时返回 0 —— 但 `_record_sql_error` 不会再让"0 + 非空错误文本"
    并存：MySQL 里 0 表示"没有错误"，有错误文本却报 0 是自相矛盾的状态
    （见 `_infer_errno_from_text`）。
    """
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return int(args[0])
    return 0


def _infer_errno_from_text(text) -> int:
    """错误号取不到时按错误文本推断 MySQL 语义码（E2）

    为什么需要：SQL 线程重放的失败来自 `_apply_event` 抛出的内置异常
    （`KeyError` / `SyntaxError` / `ValueError` / `RuntimeError`），它们
    **没有 errno**，旧实现于是 `Last_SQL_Errno=0` 与 `Last_SQL_Error='...'`
    并存 —— 客户端看到"错误文本非空但错误号 0"，与 MySQL 的「0 = 没有
    错误」矛盾，也无法据此判断错误类别。

    映射（与执行器 `_storage_errno` 的口径一致，但这里只有文本可用）：
        缺表 / 表不存在 → 1146 (42S02)
        语法错误        → 1064 (42000)
        唯一/主键冲突   → 1062 (23000)
        其它            → 1105 (HY000，通用错误，**不再是 0**)
    空文本返回 0（确实没有错误）。
    """
    msg = str(text or '')
    if not msg.strip():
        return 0
    low = msg.lower()
    if ('不存在' in msg or "doesn't exist" in low or 'does not exist' in low
            or 'unknown table' in low or 'no such table' in low
            or 'not found' in low):
        return 1146
    if '语法' in msg or 'syntax' in low or 'parse error' in low \
            or '无法解析' in msg:
        return 1064
    if ('重复' in msg or '主键冲突' in msg or 'duplicate' in low
            or 'unique' in low or '唯一' in msg):
        return 1062
    return 1105


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

    def _ensure_open(self):
        """STOP SLAVE 关闭句柄后，START SLAVE 需要能继续追加（同一实例）"""
        if self.fh is None or getattr(self.fh, 'closed', True):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, 'a', encoding='utf-8')
            try:
                self.size = self.path.stat().st_size
            except OSError:
                self.size = 0

    def append(self, event_tuple) -> int:
        with self.lock:
            self._ensure_open()
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
    """从库侧

    ★ 持久化位点（决策 D 项）：构造时从 `<data_dir>/master.info` 读取上次
    的 read/exec 位点与主库配置，重启后**从持久位点续传**（不重复拉取，
    也不从头重放）。位点写回时机：io 线程每拉一批、sql 线程每应用一批、
    STOP SLAVE 时（原子替换写，见 `_save_master_info`）。
    """

    #: 持久化位点文件名（仿 MySQL 的 master.info；JSON 形态）
    INFO_FILE = 'master.info'

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
        self.last_pos = 0               # 已应用的数据事件逻辑序号（Exec_Master_Log_Pos）
        self.relay = RelayLog(str(db.data_dir))

        self.reconnect_delay = RECONNECT_DELAY

        # ★ 真实跟踪到的复制状态（全部由 io/sql 线程写入，show_status 只读快照）
        self._lock = threading.RLock()
        self.read_master_log_pos = 0    # io 线程已拉到的数据事件逻辑序号
        self.master_log_file = None     # 主库 SHOW MASTER STATUS 的 File（没读到就是 None）
        self.master_pos = None          # 主库最新**字节**位点（没读到就是 None）
        self.relay_master_log_file = None   # 已应用位点所属的主库日志文件
        # 真实字节位点（持久化续传用；逻辑序号用于 MySQL 兼容的状态列显示）
        self.read_byte_pos = 0
        self.exec_byte_pos = 0
        self.io_connected = False
        self.io_ever_connected = False
        #: io 线程是否已确认"拉到了主库当前末尾"（Seconds_Behind_Master 判定）
        self._caught_up = False
        self._info_loaded = False
        self.io_connects = 0
        self.last_io_errno = 0
        self.last_io_error = ''
        self.last_io_error_ts = 0
        self.last_sql_errno = 0
        self.last_sql_error = ''
        self.last_sql_error_ts = 0
        self._ddl_executor = None

        self.stats = {
            'io_events': 0,
            'sql_events': 0,
            'errors': 0,
            'last_error': None,     # 兼容旧调用方：最后一次(IO/SQL)错误文本
            'last_sync_ts': 0,
        }

        # ★ 重启续传：先读持久位点，再注册到 Database（否则 SHOW SLAVE
        #   STATUS 在启动瞬间会报 0）
        self._load_master_info()

        # ★ 把自己注册到 Database：执行器靠这条通道拿到真实状态
        attach = getattr(db, 'attach_replicator', None)
        if callable(attach):
            attach(self)

    # ==================== 持久化位点（master.info）====================
    def _info_path(self):
        from pathlib import Path
        return Path(self.db.data_dir) / self.INFO_FILE

    def _load_master_info(self):
        """读取上次的位点/配置（文件不存在或损坏 → 从头开始，不伪造位点）"""
        path = self._info_path()
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warn(f"master.info 读取失败（从头开始复制）: {e}")
            return False
        if not isinstance(raw, dict):
            return False
        try:
            self.read_master_log_pos = int(raw.get('read_pos') or 0)
            self.last_pos = int(raw.get('exec_pos') or 0)
            self.read_byte_pos = int(raw.get('read_byte_pos') or 0)
            self.exec_byte_pos = int(raw.get('exec_byte_pos') or 0)
            if raw.get('master_log_file'):
                self.master_log_file = str(raw['master_log_file'])
                self.relay_master_log_file = str(raw['master_log_file'])
            # 从库重启后：位点已知 → 状态列按"已连接过"如实报（而不是 NULL）
            if self.read_master_log_pos > 0:
                self.io_ever_connected = True
        except (TypeError, ValueError) as e:
            logger.warn(f"master.info 字段非法（忽略，从头开始）: {e}")
            return False
        self._info_loaded = True
        logger.info(
            f"从库复制从持久位点续传: read_pos={self.read_master_log_pos} "
            f"exec_pos={self.last_pos} file={self.master_log_file}")
        return True

    def _save_master_info(self):
        """原子写回位点（tmp + replace；失败只告警，不影响复制线程）"""
        from pathlib import Path
        path = self._info_path()
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    'master_host': self.master_host,
                    'master_port': self.master_port,
                    'master_user': self.user,
                    'master_log_file': self.master_log_file,
                    'read_pos': self.read_master_log_pos,
                    'exec_pos': self.last_pos,
                    'read_byte_pos': self.read_byte_pos,
                    'exec_byte_pos': self.exec_byte_pos,
                    'saved_at': time.time(),
                }
            tmp = Path(str(path) + '.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding='utf-8')
            tmp.replace(path)
        except Exception as e:                   # pragma: no cover - 磁盘故障
            logger.warn(f"master.info 写入失败（位点持久化降级）: {e}")

    # ============ 生命周期 ============
    def start(self):
        if self.running:
            return
        self.running = True
        self.started = True
        self.relay._ensure_open()
        self._save_master_info()
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
        self._save_master_info()
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
        """记录 SQL 线程重放失败（E2：保证「有错误文本 ⇒ Errno 非 0」）

        `_errno_of` 只认 pymysql 形态的异常（args[0] 是 int）；重放失败更
        常见的是内置异常（`KeyError` 缺表 / `SyntaxError` 语法 / `ValueError`
        唯一冲突），它们没有 errno。旧实现直接写 0，导致
        `Last_SQL_Errno=0` 与 `Last_SQL_Error` 非空并存（MySQL 0 = 无错误，
        客户端会误判状态自相矛盾且无法归类）。现在按文本推断语义码。
        """
        with self._lock:
            errno = _errno_of(exc)
            text = str(exc)
            if errno == 0 and text.strip():
                errno = _infer_errno_from_text(text)
            self.last_sql_errno = errno
            self.last_sql_error = text
            self.last_sql_error_ts = time.time()
            self.stats['errors'] += 1
            self.stats['last_error'] = text

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
                        # r = (pos=逻辑序号, lsn=真实字节偏移, txn_id,
                        #      event_type, table_name, data)
                        self.relay.append(r)
                        try:
                            pos = int(r[0])
                        except (TypeError, ValueError):
                            continue
                        try:
                            byte_pos = int(r[1])
                        except (TypeError, ValueError):
                            byte_pos = self.read_byte_pos
                        with self._lock:
                            if pos > self.read_master_log_pos:
                                self.read_master_log_pos = pos
                            if byte_pos > self.read_byte_pos:
                                self.read_byte_pos = byte_pos
                            self._caught_up = False
                            self.stats['io_events'] += 1

                    if rows:
                        # 位点持久化：每拉一批写一次 master.info（原子替换）
                        self._save_master_info()

                    if not rows:
                        # 空闲时顺便刷新主库位点（Seconds_Behind_Master 判定用）
                        self._refresh_master_status(cur)
                        with self._lock:
                            self._caught_up = True
                        self._save_master_info()
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

            applied = 0
            for ev in events:
                try:
                    pos = self._apply_event(ev)
                except Exception as e:
                    self._record_sql_error(e)
                    logger.error(f"从库重放失败 pos={ev[0] if ev else '?'}: {e}")
                    continue
                byte_pos = 0
                try:
                    byte_pos = int(ev[1])
                except (TypeError, ValueError, IndexError):
                    byte_pos = 0
                with self._lock:
                    if pos is not None and pos >= self.last_pos:
                        self.last_pos = pos
                    if byte_pos > self.exec_byte_pos:
                        self.exec_byte_pos = byte_pos
                    self.relay_master_log_file = self.master_log_file
                    self.stats['sql_events'] += 1
                    self.stats['last_sync_ts'] = time.time()
                applied += 1
            if applied:
                # 已应用位点持久化（重启续传的 exec_pos）
                self._save_master_info()

    def _get_ddl_executor(self):
        """DDL 重放用的执行器（复用同一个，避免每次建审计/策略对象）"""
        if self._ddl_executor is None:
            from ..sql.executor import Executor
            self._ddl_executor = Executor(self.db)
        return self._ddl_executor

    def _apply_event(self, ev):
        """重放一条从主库拉到的事件（`__BINLOG_FETCH__` 的 6 列），返回事件位点

        `__BINLOG_FETCH__` 的列序是
        `(pos=逻辑序号, lsn=真实字节偏移, txn_id, event_type, table_name, data)`
        —— 这里必须按 6 列解包（V4 旧实现按 5 列解包，任何事件都会
        `ValueError: too many values to unpack`）。同时兼容 5 列的旧形态。

        重放语义（决策 D 项）：
        * DML 直写存储层（行镜像来自主库 binlog，要求两边 schema 一致）；
        * DDL 交给执行器重放；**解析失败/执行报错会显式抛出**，由
          `_record_sql_error` 记成非 0 的 `Last_SQL_Errno`（沿用 E2 语义），
          绝不静默跳过；
        * 重放期间 `db.suspend_binlog()`：从库不把重放产生的事件再记一遍；
        * 未知事件类型 → 显式 RuntimeError（不静默）。
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

        if etype in ('FORMAT_DESCRIPTION', 'BEGIN', 'COMMIT'):
            # 事务边界/格式事件：不改变数据（`__BINLOG_FETCH__` 也默认
            # 不返回它们，这里只是防御性兼容）
            return pos

        if etype == 'ROLLBACK':
            # 主库已回滚的事务不会出现在 fetch 的数据事件里；若真收到，
            # 不做任何数据变更（回滚事件本身不携带行镜像）
            return pos

        if etype == 'INSERT':
            tbl = self.db.get_table(table)
            row = (data or {}).get('row', [])
            try:
                with self.db.suspend_binlog():
                    tbl.insert(row, txn_id=0)
            except ValueError:
                # 主键已存在（重复投递/从持久位点重放边界）——幂等跳过。
                # 注意：只有"主键重复"这类 ValueError 才跳过；其它异常
                # 由外层记录为 SQL 错误（不静默）。
                pass

        elif etype == 'UPDATE':
            tbl = self.db.get_table(table)
            cols = (data or {}).get('cols')
            pk = data['pk']
            new = data.get('new', [])
            set_pairs = {}
            for i, c in enumerate(tbl.cols):
                if i != tbl.pk_idx and i < len(new):
                    set_pairs[c['name']] = new[i]
            with self.db.suspend_binlog():
                tbl.update(set_pairs,
                           {'column': tbl.pk_col, 'op': '=', 'value': pk},
                           txn_id=0)

        elif etype == 'DELETE':
            tbl = self.db.get_table(table)
            pk = data['pk']
            with self.db.suspend_binlog():
                tbl.delete({'column': tbl.pk_col, 'op': '=', 'value': pk},
                           txn_id=0)

        elif etype == 'DDL':
            sql = str((data or {}).get('sql', '') or '').strip()
            if not sql:
                raise RuntimeError(
                    "DDL 事件缺少 sql 文本：无法重放（拒绝静默跳过）")
            from ..sql.parser import parser
            try:
                p = parser.parse(sql)
            except Exception as e:
                raise RuntimeError(
                    f"DDL 重放解析失败（拒绝静默跳过）: {sql[:200]}: {e}")
            if not p:
                raise RuntimeError(
                    f"DDL 重放无法解析（拒绝静默跳过）: {sql[:200]}")
            with self.db.suspend_binlog():
                result = self._get_ddl_executor().execute(p, {})
            if isinstance(result, dict) and result.get('type') == 'error':
                raise RuntimeError(
                    f"DDL 重放失败（拒绝静默跳过）: errno="
                    f"{result.get('code')} {result.get('message')}；"
                    f"sql={sql[:200]}")
        else:
            raise RuntimeError(f"未知 binlog 事件类型 {etype!r}（拒绝静默跳过）")

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
        "最后一个已应用事件的主库时间 - 现在"算延迟。决策 D 项后的判定：
        * SQL 线程没在跑 / io 没连上 / 中继队列还有待应用事件 -> None
          （确实无法断言"追平了"，不编造 0）；
        * io 线程已确认拉到主库当前末尾（`_caught_up`）且队列为空 -> 0；
        * 落后但事件时间戳口径不可靠 -> None（**不编造**秒数）。
        """
        if not self.running or self.sql_running() != 'Yes':
            return None
        with self._lock:
            if not self.io_connected:
                return None
            if self.relay.pending():
                return None
            return 0 if self._caught_up else None

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
                # 决策 D 项后本引擎**真的**有持久化位点文件了：如实报路径
                'Master_Info_File': str(self._info_path()),
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
