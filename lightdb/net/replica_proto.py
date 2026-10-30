"""最小 binlog 复制原型（task-71 生产化 B）

定位（**原型**，不是完整复制实现）
==================================

把主库 append-only binlog 里的**行事件**拉取到目标库（从库/备份节点）并按
原顺序 apply：DDL 交执行器、DML 走存储层行镜像。用于验证"位点复制"链路与
断点续传，**不**宣称生产级复制。

已实现
------

* 拉取入口：真实 MySQL 协议连接主库（pymysql）：
  - `SHOW BINARY LOGS`（server 内建，task-71）→ 文件清单与大小；
  - `SHOW MASTER STATUS` → 当前文件/位点；
  - `SELECT * FROM __BINLOG_FETCH__ WHERE lsn > N LIMIT k` → 机器可读的
    行事件（内部导出接口，6 列：pos/lsn/txn_id/event_type/table_name/data；
    `lsn` 是跨文件全局字节偏移，可跨 binlog 轮转单调续传）。
* apply 语义：
  - **DDL**：交目标库 `Executor` 执行（与从库 SQL 线程同一条路径）；
    CREATE TABLE 已存在 / DROP TABLE 不存在 / TRUNCATE / ALTER ADD 列已存在 /
    ALTER DROP 列已不存在 → 判为**幂等重放**（记录 `idempotent`），其余错误
    显式 `ReplicaApplyError`；
  - **DML**：按行镜像直写目标存储层（不触发触发器，与 `SlaveReplicator`
    的 SQL 线程一致）：INSERT 主键冲突 → 幂等；UPDATE/DELETE 按主键，
    affected=0 → 幂等；目标缺表/列不匹配 → 显式 `ReplicaApplyError`；
  - apply 期间 `suspend_binlog()`，目标库不把重放事件再记进自己的 binlog。
* 断点续传：每应用一条事件后把 `{master_file, lsn, seq, applied, idempotent,
  errors, last_event_type, last_apply_ts}` 原子写回状态文件（tmp+replace），
  进程重启/中断后从持久 lsn 继续（不会重复应用已成功的事件；重复到了也幂等）。
* 错误显式：拉取失败（主库不可用/binlog 损坏被服务端拒绝）、事件不是 6 列、
  payload 非法 JSON、未知 event_type、DDL/DML apply 失败 → 全部抛
  `ReplicaApplyError`（带上下文），绝不静默跳过。

增量模式（task-81）
-------------------

* `follow()`（CLI `--follow --interval N`）持续轮询增量：拉取→应用→等待；
* **失败保留断点、不跳过**：apply 失败时状态文件停在最后成功事件的 lsn，
  下一轮从同一位点重试（幂等重放）；连续失败按指数退避（可配上限/重试次数）
  并写日志；终态错误显式抛给调用方；
* **binlog 位点缺口**（主库轮转后把起始文件删了/重建，`MIN(lsn)` 已大于本地
  已应用 lsn）→ 抛 `ReplicaBinlogGap`（errno **1236** 风格），消息含可用
  文件清单与恢复策略（`--reset-position` 显式放弃缺失区间，不自动跳过）；
* 状态文件原子写 + `checksum` 校验（损坏/半截/被篡改 → 显式拒绝，不猜位点）；
* 指标：`db.replication_status()` 暴露 `Replica_*` 快照，`/metrics` 渲染为
  `lightdb_replica_*`；`SHOW SLAVE STATUS` 的延迟/位点列取同一份状态。

限制（原型边界，如实写进 docs/PRODUCTION_READINESS.md）
--------------------------------------------------------

* **单主**：一个原型实例只跟一个主库；没有拓扑管理；
* **无自动 failover**：主库挂了就停（重试到上限后抛 `ReplicaError`），
  不做选主/切主；
* **无多线程 apply**：严格按事件顺序串行应用（单线程）；
* 目标库 schema 必须与主库一致（DML 行镜像按列顺序/主键 apply；DDL 按
  binlog 里的原始 SQL 重放；不做 schema diff 修复）；
* 触发器/外键/存储过程副作用不在行镜像里（行级复制不触发目标侧触发器）；
* 事件时间戳不在导出接口里：`lag_seconds` 是**落后时距上次成功 apply 的
  秒数**（追平恒 0），不是 MySQL 按事件时间戳算的 `Seconds_Behind_Master`；
  `lag_lsn` 是主库最大 lsn 与已应用 lsn 的**真实字节差**；
* 不加密、不压缩、不做 binlog 位点校验和（CRC 已由主库读取端校验）。
"""
import hashlib
import json
import os
import socket
import struct
import threading
import time

from ..logger import logger
from . import mysql_protocol as mp

#: 原型支持的事件类型（其它一律显式错误）
KNOWN_EVENT_TYPES = frozenset({'INSERT', 'UPDATE', 'DELETE', 'DDL'})

#: 目标库 apply 时判定"幂等重放"的 DDL action
_IDEMPOTENT_DDL_ACTIONS = frozenset({
    'ADD COLUMN', 'DROP COLUMN', 'RENAME TO',
})

#: 位点缺口恢复说明（CLI / 异常消息 / 文档共用同一段文字）
GAP_RECOVERY_HINT = (
    '恢复策略：原型不会自动跳过缺失区间。请二选一：'
    '(a) 从主库重新做一次全量备份/快照并重建目标库（推荐，保证一致）；'
    '(b) 确认可接受丢事件后，用 CLI `--reset-position` 显式把位点推进到'
    '主库当前可用末尾（会跳过缺失事件，目标数据需自行校验/修复）。'
)


class ReplicaError(Exception):
    """复制链路错误（不可恢复/需人工处理）"""


class ReplicaRetryableError(ReplicaError):
    """可重试错误（主库连接/读取瞬时失败）；断点保留，退避后重试同一个 lsn"""


class ReplicaBinlogGap(ReplicaError):
    """起始位点所在 binlog 已被主库清理/重建（MySQL 1236 风格错误）

    * 显式拒绝继续：本地已应用 lsn 早于主库当前最早可用 lsn，缺失事件
      无法补齐；自动跳过会造成静默数据丢失，因此**必须人工决策**；
    * ``available_logs`` / ``earliest_lsn`` / ``local_lsn`` 供排障；
    * 恢复策略见 ``GAP_RECOVERY_HINT`` / docs。
    """

    errno = 1236
    sqlstate = 'HY000'

    def __init__(self, message: str, available_logs=None,
                 earliest_lsn=None, local_lsn=None):
        self.available_logs = list(available_logs or [])
        self.earliest_lsn = earliest_lsn
        self.local_lsn = local_lsn
        super().__init__(
            f'复制位点缺口（errno 1236）：{message}；'
            f'可用 binlog 文件={self.available_logs or "[]"}，'
            f'主库最早 lsn={earliest_lsn}，本地已应用 lsn={local_lsn}。'
            f'{GAP_RECOVERY_HINT}')


class ReplicaApplyError(ReplicaError):
    """事件应用失败（显式；带事件上下文）"""

    def __init__(self, message: str, event=None):
        self.event = event
        ctx = ''
        if event is not None:
            try:
                ctx = (f' [pos={event[0]!r} lsn={event[1]!r} '
                       f'txn={event[2]!r} type={event[3]!r} '
                       f'table={event[4]!r}]')
            except Exception:                    # pragma: no cover - 防御
                ctx = f' [{event!r}]'
        super().__init__(f'复制 apply 失败{ctx}: {message}')


#: binlog dump 流协议版本（响应为 marker+JSON，不是 MySQL binlog v4 字节）
BINLOG_DUMP_SCHEMA = mp.BINLOG_DUMP_SCHEMA


class BinlogDumpClient:
    """最小 MySQL 协议客户端：握手 + COM_BINLOG_DUMP 流式拉取

    只实现复制需要的两条链路：4.1 明文握手（mysql_native_password）与
    COM_BINLOG_DUMP（非 GTID）。响应体是**本引擎自定义的 JSON 流**：

    * ``0x00 + JSON``：``stream_start`` / ``event`` / ``eof`` 三类包；
    * ``0xff``：MySQL ERR 包（errno 1236/1381/1105/1044...）；
    * ``0xfe``：标准 EOF（流结束；本引擎"取完当前可用事件即 EOF"）。

    ``event`` JSON 与 ``__BINLOG_FETCH__`` 行同口径：
    pos(全局 seq)/lsn(全局字节偏移)/txn_id/event_type/table_name/data，
    另带 file/next_pos 用于断点续传。**不是 MySQL binlog v4 兼容格式**，
    差异见 docs/PRODUCTION_READINESS.md §10。
    """

    def __init__(self, host: str, port: int, user: str = 'root',
                 password: str = '', connect_timeout: float = 5.0,
                 read_timeout: float = 60.0):
        self.host = str(host)
        self.port = int(port)
        self.user = user
        self.password = password
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.sock = None
        self.server_id = 0
        self.server_version = ''

    # ---------- 包收发 ----------
    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            part = self.sock.recv(n - got)
            if not part:
                raise ConnectionError('主库连接在读取响应时被关闭')
            chunks.append(part)
            got += len(part)
        return b''.join(chunks)

    def _read_packet(self) -> bytes:
        header = self._recv_exact(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        if length <= 0:
            return b''
        return self._recv_exact(length)

    def _send_packet(self, payload: bytes, seq: int = 0):
        length = len(payload)
        header = bytes([length & 0xff, (length >> 8) & 0xff,
                        (length >> 16) & 0xff, seq & 0xff])
        self.sock.sendall(header + payload)

    # ---------- 连接/认证 ----------
    def connect(self):
        if self.sock is not None:
            return self
        try:
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout)
            self.sock.settimeout(self.read_timeout)
        except OSError as exc:
            self.close()
            raise ReplicaRetryableError(
                f'连接主库失败 {self.host}:{self.port}: {exc}') from exc
        try:
            handshake = self._read_packet()
            info = mp.parse_handshake_v10(handshake)
            self.server_id = int(info.get('connection_id') or 0)
            self.server_version = str(info.get('server_version') or '')
            response = mp.make_handshake_response(
                self.user, self.password, info['auth_seed'])
            self._send_packet(response, 1)
            auth = self._read_packet()
        except ReplicaError:
            self.close()
            raise
        except (OSError, ValueError, IndexError) as exc:
            self.close()
            raise ReplicaRetryableError(
                f'主库握手失败: {exc}') from exc
        if auth[:1] == b'\xff':
            err = mp.parse_err_packet(auth) or (1105, 'HY000', '认证失败')
            self.close()
            if int(err[0]) in (2003, 2006, 2013):
                raise ReplicaRetryableError(
                    f'主库认证时连接失败: {err[0]} {err[2]}')
            raise ReplicaError(
                f'主库认证失败（errno {err[0]}）: {err[2]}')
        if auth[:1] != b'\x00':
            self.close()
            raise ReplicaError('主库认证响应非法（不是 OK/ERR 包）')
        return self

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:                   # pragma: no cover - 防御
                pass

    # ---------- binlog dump ----------
    @staticmethod
    def _map_stream_error(errno, sqlstate, message):
        if int(errno) == 1236:
            return ReplicaBinlogGap(message)
        return ReplicaError(
            f'主库 binlog dump 错误（errno {errno}, {sqlstate}）: {message}')

    def dump(self, filename: str, binlog_pos: int, max_events: int = None,
             flags: int = mp.BINLOG_DUMP_NON_BLOCK,
             server_id: int = 1) -> dict:
        """执行一次 COM_BINLOG_DUMP；返回 events/eof/truncated

        ``max_events`` 只取前 N 条就主动断开（模拟"拉一半断线"：服务端
        后续发送会得到 BrokenPipe；客户端状态由最后一条事件的
        file/next_pos 决定，下一次从该位点续传）。
        """
        self.connect()
        request = (bytes([mp.COM_BINLOG_DUMP])
                   + mp.make_binlog_dump_request(
                       binlog_pos=binlog_pos, flags=flags,
                       server_id=server_id, filename=filename))
        try:
            self._send_packet(request, 0)
            first = self._read_packet()
        except OSError as exc:
            self.close()
            raise ReplicaRetryableError(
                f'COM_BINLOG_DUMP 发送/读取失败: {exc}') from exc
        if first[:1] == b'\xff':
            err = mp.parse_err_packet(first) or (1105, 'HY000', 'dump 失败')
            self.close()
            raise self._map_stream_error(*err)
        if first[:1] != bytes([mp.BINLOG_STREAM_MARKER]):
            self.close()
            raise ReplicaError(
                f'COM_BINLOG_DUMP 响应头非法（首字节 {first[:1]!r}）')
        try:
            header = json.loads(first[1:].decode('utf-8'))
        except Exception as exc:                 # noqa: BLE001
            self.close()
            raise ReplicaError(f'流头 JSON 解析失败: {exc}') from exc
        if header.get('schema') != BINLOG_DUMP_SCHEMA:
            self.close()
            raise ReplicaError(
                f"binlog dump 协议不匹配: {header.get('schema')!r} "
                f"（期望 {BINLOG_DUMP_SCHEMA!r}）")
        events = []
        eof = None
        truncated = False
        while True:
            try:
                packet = self._read_packet()
            except (OSError, ConnectionError) as exc:
                self.close()
                if events:
                    # 已拿到部分事件：交给上层 apply（由状态位点续传）
                    return {'header': header, 'events': events,
                            'eof': None, 'truncated': True,
                            'disconnect': str(exc)}
                raise ReplicaRetryableError(
                    f'binlog 流读取失败: {exc}') from exc
            marker = packet[:1]
            if marker == b'\xff':
                err = mp.parse_err_packet(packet) or (1105, 'HY000', '流错误')
                self.close()
                raise self._map_stream_error(*err)
            if marker == bytes([mp.BINLOG_STREAM_EOF]):
                self.close()
                return {'header': header, 'events': events,
                        'eof': eof, 'truncated': truncated}
            if marker != bytes([mp.BINLOG_STREAM_MARKER]):
                self.close()
                raise ReplicaError(f'binlog 流包标记非法: {marker!r}')
            try:
                obj = json.loads(packet[1:].decode('utf-8'))
            except Exception as exc:             # noqa: BLE001
                self.close()
                raise ReplicaError(
                    f'binlog 流 JSON 解析失败: {exc}') from exc
            kind = obj.get('type')
            if kind == 'event':
                events.append(obj)
                if max_events is not None and len(events) >= int(max_events):
                    # 主动断开（服务端后续写 BrokenPipe）→ 状态位点续传
                    self.close()
                    truncated = True
                    return {'header': header, 'events': events,
                            'eof': None, 'truncated': True}
            elif kind == 'eof':
                eof = obj
            else:
                self.close()
                raise ReplicaError(f'未知 binlog 流包类型: {kind!r}')


class ReplicaPrototype:
    """单主、单线程的 binlog → 目标库 apply 原型"""

    STATE_VERSION = 1

    def __init__(self, master_host: str, master_port: int, target_db,
                 user: str = 'root', password: str = '',
                 state_path: str = None, connect_timeout: float = 5.0,
                 read_timeout: float = 60.0, target_executor=None,
                 auto_register: bool = True, source: str = 'export'):
        self.master_host = str(master_host)
        self.master_port = int(master_port)
        self.user = user
        self.password = password
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.target_db = target_db
        self._executor = target_executor
        #: 拉取方式：``export``=内部导出（__BINLOG_FETCH__），
        #: ``protocol``=MySQL 协议 COM_BINLOG_DUMP（自定义 JSON 流，task-86）
        self.source = str(source or 'export').lower()
        if self.source not in ('export', 'protocol'):
            raise ReplicaError(
                f'未知复制拉取方式: {source!r}（仅 export/protocol）')
        #: RLock：apply_event/延迟快照/show_status 互斥，避免 metrics 读到
        #: "state 已前进但 master_lsn 还是上一批"的撕裂快照
        self._lock = threading.RLock()
        self.state_path = (
            state_path if state_path is not None
            else os.path.join(str(target_db.data_dir), 'replica_state.json'))
        self.state = self._load_state()
        self.started_at = time.time()
        self._last_error = None
        #: 最近一次成功 run 的运行时快照（不进状态文件，SHOW/metrics 用）
        self._following = False
        self._caught_up = True
        self._master_max_lsn = 0
        self._earliest_master_lsn = 0
        self._events_behind = 0
        self._lag_bytes = 0
        self._lag_seconds = 0.0
        self._last_batch_events = 0
        self._last_batch_ts = 0.0
        self._batches = 0
        self._retries = 0
        self._last_error_errno = None
        self._closed = False
        # 让目标库的 `SHOW SLAVE STATUS`（经 db.replication_status()）读到
        # 本原型的真实位点/延迟；这是既有注册接口，不需要改 db/executor。
        if auto_register:
            self._register_on_target()

    def _register_on_target(self):
        attach = getattr(self.target_db, 'attach_replicator', None)
        if callable(attach):
            try:
                attach(self)
            except Exception as e:                # pragma: no cover - 防御
                logger.warn(f"注册复制状态到目标库失败（不影响复制）: {e}")

    def close(self):
        """注销状态注册并落一次盘（幂等；不停止外部 follow 循环）"""
        if self._closed:
            return
        self._closed = True
        try:
            self.save_state(sync=True)
        except ReplicaError as e:                 # pragma: no cover - 防御
            logger.warn(f"复制器关闭时写状态失败: {e}")
        detach = getattr(self.target_db, 'detach_replicator', None)
        if callable(detach):
            try:
                detach(self)
            except Exception:                     # pragma: no cover - 防御
                pass

    # ==================== 状态持久化 ====================

    def _default_state(self) -> dict:
        return {
            'version': self.STATE_VERSION,
            'master_host': self.master_host,
            'master_port': self.master_port,
            'master_file': None,
            'lsn': 0,
            'seq': 0,
            'applied': 0,
            'idempotent': 0,
            'errors': 0,
            'last_event_type': None,
            'last_apply_ts': 0.0,
            # ★ task-86：协议模式的精确续传位点（file + 文件内 pos）
            'protocol_file': None,
            'protocol_pos': None,
        }

    #: 状态文件里参与 checksum 的字段（排除 checksum/saved_at 自身）
    _CHECKSUM_EXCLUDED = ('checksum', 'saved_at')

    @classmethod
    def _state_checksum(cls, state: dict) -> str:
        """状态内容 sha256（按 key 排序的规范 JSON；不含 checksum/saved_at）"""
        payload = {k: v for k, v in state.items()
                   if k not in cls._CHECKSUM_EXCLUDED}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def _validate_state(self, state: dict, path: str):
        """状态字段校验（类型/范围/版本）；非法 → 显式 ReplicaError"""
        if state.get('version') != self.STATE_VERSION:
            raise ReplicaError(
                f'复制状态文件版本不支持（期望 {self.STATE_VERSION}，'
                f'实际 {state.get("version")!r}）: {path}')
        for key in ('lsn', 'seq', 'applied', 'idempotent', 'errors'):
            value = state.get(key)
            if not isinstance(value, int) or isinstance(value, bool) \
                    or value < 0:
                raise ReplicaError(
                    f'复制状态文件字段 {key} 非法（需要非负整数）: '
                    f'{value!r} @ {path}')
        mf = state.get('master_file')
        if mf is not None and not isinstance(mf, str):
            raise ReplicaError(
                f'复制状态文件字段 master_file 非法: {mf!r} @ {path}')
        ts = state.get('last_apply_ts')
        if not isinstance(ts, (int, float)) or ts < 0:
            raise ReplicaError(
                f'复制状态文件字段 last_apply_ts 非法: {ts!r} @ {path}')
        pf = state.get('protocol_file')
        if pf is not None and not isinstance(pf, str):
            raise ReplicaError(
                f'复制状态文件字段 protocol_file 非法: {pf!r} @ {path}')
        pp = state.get('protocol_pos')
        if pp is not None and (not isinstance(pp, int)
                               or isinstance(pp, bool) or pp < 0):
            raise ReplicaError(
                f'复制状态文件字段 protocol_pos 非法: {pp!r} @ {path}')
        for key in ('master_host', 'master_port'):
            if state.get(key) is None:
                state[key] = getattr(self, key, None)

    def _load_state(self) -> dict:
        state = self._default_state()
        path = str(self.state_path)
        if not os.path.exists(path):
            return state
        try:
            raw = json.loads(open(path, encoding='utf-8').read())
        except Exception as e:
            raise ReplicaError(
                f'复制状态文件损坏，拒绝从不确定位点继续: {path}: {e}')
        if not isinstance(raw, dict):
            raise ReplicaError(f'复制状态文件格式非法: {path}')
        saved = raw.get('checksum')
        if saved is not None:
            actual = self._state_checksum(raw)
            if saved != actual:
                raise ReplicaError(
                    f'复制状态文件校验失败（checksum 不匹配，'
                    f'可能被截断/篡改）: {path}: '
                    f'expected={saved} actual={actual}')
        else:                                    # 兼容 task-71 旧状态文件
            logger.warn(f'复制状态文件无 checksum（旧格式），按内容校验: {path}')
        state.update({k: raw[k] for k in state if k in raw})
        self._validate_state(state, path)
        return state

    def save_state(self, sync: bool = False):
        """原子写回状态（tmp + fsync? + replace），并写入内容校验和

        * 默认 ``sync=False``：只 flush 到 OS（不 fsync）—— 每条事件都 fsync
          在 Windows 上实测约 0.2s/次，1000 事件会慢到不可用；
        * ``sync=True``：批末/错误路径做一次 fsync，保证断点续传位点尽量
          不丢（崩溃最多回放最后一批事件，重放按幂等处理）；
        * 状态里带 ``checksum``（sha256，排除 checksum/saved_at），
          `_load_state` 会校验；损坏/半截文件显式拒绝，不猜位点。
        * 失败抛 ReplicaError（不能假装已持久化）。
        """
        state = dict(self.state)
        state['saved_at'] = time.time()
        state['checksum'] = self._state_checksum(state)
        path = str(self.state_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                if sync:
                    os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            raise ReplicaError(f'复制状态持久化失败: {path}: {e}')

    # ==================== 主库访问 ====================

    #: 主库连接/读取的瞬时错误码（可退避重试；断点保留）
    _RETRYABLE_ERRNOS = frozenset({
        1040, 1053, 1152, 1158, 1159, 1160, 1161,      # 连接/过载类
        2002, 2003, 2006, 2013, 2014, 2038, 2055,      # socket/网络类
    })

    @classmethod
    def _classify_link_error(cls, exc: Exception) -> ReplicaError:
        """把 pymysql 异常分成"可重试"与"显式不可恢复"两类"""
        code = None
        try:
            code = int(getattr(exc, 'args', [None])[0])
        except Exception:                        # pragma: no cover - 防御
            code = None
        if code in cls._RETRYABLE_ERRNOS:
            return ReplicaRetryableError(
                f'主库链路瞬时失败（errno {code}）: {exc}')
        return ReplicaError(f'主库链路失败: {exc}')

    def _connect(self):
        import pymysql
        try:
            return pymysql.connect(
                host=self.master_host, port=self.master_port,
                user=self.user, password=self.password,
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout, autocommit=True)
        except pymysql.MySQLError as e:
            raise self._classify_link_error(e)
        except Exception as e:
            # 连接拒绝/DNS/超时等：transient，可退避重试
            raise ReplicaRetryableError(
                f'连接主库失败 {self.master_host}:{self.master_port}: {e}')

    def _query(self, conn, sql: str):
        import pymysql
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, rows
        except ReplicaError:
            raise
        except pymysql.MySQLError as e:
            raise self._classify_link_error(e)
        except Exception as e:
            raise ReplicaError(f'主库查询失败: {sql[:120]}: {e}')

    def _aggregate_lsn(self) -> dict:
        """主库当前可读事件的 lsn 范围（COUNT/MIN/MAX，真实查询）

        返回 ``{'count': n, 'min_lsn': m, 'max_lsn': M}``；无事件时 0。
        用于：真实 lag（主库最大 lsn - 已应用 lsn）、位点缺口检测。
        """
        conn = self._connect()
        try:
            cols, rows = self._query(
                conn,
                'SELECT COUNT(*), MIN(lsn), MAX(lsn) FROM __BINLOG_FETCH__')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        row = rows[0] if rows else (0, 0, 0)

        def _int(v):
            try:
                return int(v) if v is not None else 0
            except (TypeError, ValueError):
                return 0
        return {'count': _int(row[0]), 'min_lsn': _int(row[1]),
                'max_lsn': _int(row[2])}

    def _aggregate_max_pos(self) -> int:
        """主库当前最大事件序号（pos），用于 ``--reset-position``"""
        conn = self._connect()
        try:
            cols, rows = self._query(
                conn, 'SELECT MAX(pos) FROM __BINLOG_FETCH__')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            return int(rows[0][0]) if rows and rows[0][0] is not None else 0
        except (TypeError, ValueError):
            return 0

    def master_status(self) -> dict:
        """`SHOW MASTER STATUS` → {file, position}；binlog 关闭时显式报错"""
        conn = self._connect()
        try:
            cols, rows = self._query(conn, 'SHOW MASTER STATUS')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not rows:
            raise ReplicaError(
                '主库 SHOW MASTER STATUS 为空：binlog 未启用'
                '（LIGHTDB_BINLOG_ENABLED=0）或尚未产生写入，无法复制')
        row = rows[0]
        return {'file': row[0], 'position': int(row[1])}

    def binary_logs(self) -> list:
        """`SHOW BINARY LOGS` → [{name,size,encrypted}, ...]"""
        conn = self._connect()
        try:
            cols, rows = self._query(conn, 'SHOW BINARY LOGS')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        out = []
        for row in rows:
            out.append({'name': row[0], 'size': int(row[1]),
                        'encrypted': row[2] if len(row) > 2 else 'No'})
        return out

    def fetch_events(self, after_lsn: int = None, limit: int = 500) -> list:
        """拉取 `lsn > after_lsn` 的行事件（`__BINLOG_FETCH__` 6 列）"""
        after_lsn = int(self.state['lsn'] if after_lsn is None
                        else after_lsn)
        limit = max(1, int(limit))
        sql = ('SELECT * FROM __BINLOG_FETCH__ WHERE lsn > '
               f'{after_lsn} LIMIT {limit}')
        conn = self._connect()
        try:
            cols, rows = self._query(conn, sql)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        expect = ['pos', 'lsn', 'txn_id', 'event_type', 'table_name', 'data']
        if cols and cols != expect:
            raise ReplicaError(
                f'主库 __BINLOG_FETCH__ 列契约变化：期望 {expect}，实际 {cols}')
        return [tuple(r) for r in rows]

    # ==================== 协议拉取（COM_BINLOG_DUMP，task-86） ====================

    def _protocol_start_position(self) -> tuple:
        """解析协议模式的起始 (file, pos)

        * 状态里有 `protocol_file/pos` → 直接续传（断线/进程重启后）；
        * 全新状态（从未应用）→ 从主库**最早可用 binlog 文件头**开始，
          这样协议模式与内部导出模式一样从历史事件全量追平；
        * 已有 lsn 但没有协议位点（状态来自 export 模式）→ 显式报错，
          要求换新状态文件/显式重置，避免从错误位点静默复制。
        """
        from ..replication.append_only_binlog import FILE_HEADER_SIZE
        with self._lock:
            file = self.state.get('protocol_file')
            pos = self.state.get('protocol_pos')
            lsn = int(self.state.get('lsn') or 0)
            applied = int(self.state.get('applied') or 0)
        if file is not None and pos is not None:
            return str(file), int(pos)
        if lsn > 0 or applied > 0:
            raise ReplicaError(
                '状态文件来自内部导出租模式（有 lsn 但没有协议续传位点）：'
                '协议模式需要独立状态文件；请使用新的 --state-file，或人工'
                '确认后用 --reset-position 把协议位点重置到主库当前末尾')
        logs = self.binary_logs()
        if not logs:
            raise ReplicaError(
                '主库没有可用 binlog 文件（binlog 未启用或尚无写入）')
        first = sorted(logs, key=lambda x: str(x.get('name')))[0]
        return str(first['name']), int(FILE_HEADER_SIZE)

    def _fetch_protocol_events(self, limit: int) -> dict:
        """经 COM_BINLOG_DUMP 拉取一批事件（返回六列元组 + 续传位点）

        连接是**每次拉取一条短连接**：服务端在"取完当前可用事件"后发 EOF
        并结束流；断线/只取 N 条时状态里的 file/pos 保留在最后成功应用的
        事件之后，下次从该位点重连续传（服务端会校验 file/pos 边界）。
        """
        start_file, start_pos = self._protocol_start_position()
        client = BinlogDumpClient(
            self.master_host, self.master_port, user=self.user,
            password=self.password, connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout)
        try:
            batch = client.dump(start_file, start_pos,
                                max_events=max(1, int(limit)))
        finally:
            client.close()
        events = []
        for obj in batch.get('events') or []:
            try:
                data = obj.get('data')
                if isinstance(data, (dict, list)):
                    data_text = json.dumps(data, ensure_ascii=False,
                                           default=str)
                else:
                    data_text = data if isinstance(data, str) else '{}'
                events.append({
                    'event': (int(obj.get('pos') or 0),
                              int(obj.get('lsn') or 0),
                              obj.get('txn_id'),
                              str(obj.get('event_type') or ''),
                              obj.get('table_name'),
                              data_text),
                    'file': obj.get('file'),
                    'next_pos': obj.get('next_pos'),
                })
            except Exception as exc:             # noqa: BLE001
                raise ReplicaError(
                    f'协议事件字段非法（拒绝静默跳过）: {obj!r}: {exc}')
        return {'events': events, 'header': batch.get('header'),
                'eof': batch.get('eof'),
                'truncated': bool(batch.get('truncated')),
                'disconnect': batch.get('disconnect')}

    # ==================== apply ====================

    @property
    def executor(self):
        """目标库执行器（DDL 用；惰性创建并复用）"""
        if self._executor is None:
            from ..sql.executor import Executor
            self._executor = Executor(self.target_db)
        return self._executor

    def apply_event(self, event, persist: bool = True) -> dict:
        """应用一条事件；返回 {'result': 'applied'|'idempotent', 'type': ...}

        ``persist=False`` 用于批量 apply：由 `run_once` 按批/按间隔统一写
        状态文件，避免每条事件一次 JSON dump + rename 的开销（1000 条事件
        实测省 ~2/3 时间）；直接调用方默认每条都持久化。
        """
        event = self._validate_event(event)
        pos, lsn, txn_id, etype, table, payload = event
        data = payload
        if etype == 'DDL':
            result = self._apply_ddl(data.get('sql', ''), event)
        else:
            result = self._apply_dml(etype, table, data, event)
        with self._lock:
            self.state['lsn'] = max(int(self.state.get('lsn') or 0), int(lsn))
            self.state['seq'] = max(int(self.state.get('seq') or 0), int(pos))
            self.state['last_event_type'] = etype
            self.state['last_apply_ts'] = time.time()
            if result == 'applied':
                self.state['applied'] = int(self.state.get('applied') or 0) + 1
            else:
                self.state['idempotent'] = \
                    int(self.state.get('idempotent') or 0) + 1
        if persist:
            self.save_state()
        return {'result': result, 'type': etype, 'lsn': int(lsn),
                'table': table}

    def _validate_event(self, event):
        """校验并解码事件；损坏/未知类型 → ReplicaApplyError（显式）"""
        if not isinstance(event, (tuple, list)):
            raise ReplicaApplyError(f'事件不是元组/列表: {type(event)}')
        if len(event) != 6:
            raise ReplicaApplyError(
                f'事件列数非法: {len(event)}（期望 6）', event)
        pos, lsn, txn_id, etype, table, payload = event
        try:
            lsn_i = int(lsn)
            pos_i = int(pos) if pos is not None else 0
        except (TypeError, ValueError):
            raise ReplicaApplyError('事件位点不是整数', event)
        etype = str(etype or '').upper()
        if etype not in KNOWN_EVENT_TYPES:
            raise ReplicaApplyError(
                f'未知事件类型 {etype!r}（拒绝静默跳过）', event)
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', 'replace')
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except Exception as e:
                raise ReplicaApplyError(
                    f'事件 payload 不是合法 JSON（损坏事件）: {e}', event)
        elif isinstance(payload, dict):
            data = payload
        else:
            raise ReplicaApplyError(
                f'事件 payload 类型非法: {type(payload)}', event)
        if not isinstance(data, dict):
            raise ReplicaApplyError('事件 payload 不是 JSON 对象', event)
        return (pos_i, lsn_i, txn_id, etype, table, data)

    # ---------- DML ----------
    def _get_table(self, name, event):
        try:
            return self.target_db.get_table(name)
        except KeyError:
            raise ReplicaApplyError(
                f'目标库缺表 {name!r}：DDL 事件未先复制/顺序错误', event)

    def _apply_dml(self, etype: str, table: str, data: dict, event):
        tbl = self._get_table(table, event)
        cols = data.get('cols')
        if etype == 'INSERT':
            row = data.get('row')
            if not isinstance(row, (list, tuple)):
                raise ReplicaApplyError('INSERT 缺 row 镜像', event)
            try:
                with self.target_db.suspend_binlog():
                    tbl.insert(list(row), txn_id=0)
                return 'applied'
            except ValueError as e:
                msg = str(e)
                if any(k in msg for k in ('重复', 'duplicate', 'Duplicate',
                                          'unique', '唯一')):
                    return 'idempotent'      # 已应用过（重放边界）
                raise ReplicaApplyError(f'INSERT apply 失败: {msg}', event)
            except Exception as e:
                raise ReplicaApplyError(f'INSERT apply 失败: {e}', event)

        if etype == 'UPDATE':
            pk = data.get('pk')
            new = data.get('new')
            if pk is None or not isinstance(new, (list, tuple)):
                raise ReplicaApplyError('UPDATE 缺 pk/new 镜像', event)
            table_cols = [c['name'] for c in tbl.cols]
            set_pairs = {}
            for i, name in enumerate(table_cols):
                if name == str(tbl.pk_col):
                    continue
                if i < len(new):
                    set_pairs[name] = new[i]
            try:
                with self.target_db.suspend_binlog():
                    n = tbl.update(
                        set_pairs,
                        {'column': tbl.pk_col, 'op': '=', 'value': pk},
                        txn_id=0)
            except Exception as e:
                raise ReplicaApplyError(f'UPDATE apply 失败: {e}', event)
            return 'applied' if n else 'idempotent'

        if etype == 'DELETE':
            pk = data.get('pk')
            if pk is None:
                raise ReplicaApplyError('DELETE 缺 pk 镜像', event)
            try:
                with self.target_db.suspend_binlog():
                    n = tbl.delete(
                        {'column': tbl.pk_col, 'op': '=', 'value': pk},
                        txn_id=0)
            except Exception as e:
                raise ReplicaApplyError(f'DELETE apply 失败: {e}', event)
            return 'applied' if n else 'idempotent'
        raise ReplicaApplyError(f'未支持的 DML 类型 {etype!r}', event)

    # ---------- DDL ----------
    def _apply_ddl(self, sql: str, event):
        sql = str(sql or '').strip()
        if not sql:
            raise ReplicaApplyError('DDL 事件缺少 sql 文本', event)
        from ..sql.parser import parser
        try:
            parsed = parser.parse(sql)
        except Exception as e:
            raise ReplicaApplyError(
                f'DDL 解析失败（拒绝静默跳过）: {e}', event)
        if not isinstance(parsed, dict):
            raise ReplicaApplyError(f'DDL 无法解析: {sql[:200]}', event)
        cmd = str(parsed.get('command') or '')
        table = parsed.get('table')

        # 幂等预判（重放边界：上一条已应用但状态未落盘）
        if cmd == 'CREATE TABLE' and table in self.target_db.tables:
            return 'idempotent'
        if cmd == 'DROP TABLE' and table not in self.target_db.tables:
            return 'idempotent'
        if cmd == 'ALTER TABLE' and table not in self.target_db.tables:
            raise ReplicaApplyError(
                f'ALTER TABLE 目标表不存在 {table!r}（DDL 顺序错误）', event)

        try:
            target_ex = self.executor
            with self.target_db.suspend_binlog():
                result = target_ex.execute(parsed, {})
        except Exception as e:
            raise ReplicaApplyError(f'DDL 执行异常: {e}', event)
        if isinstance(result, dict) and result.get('type') == 'error':
            if self._ddl_error_is_idempotent(parsed, result, event):
                return 'idempotent'
            raise ReplicaApplyError(
                f"DDL apply 失败 errno={result.get('code')}: "
                f"{result.get('message')}；sql={sql[:200]}", event)
        return 'applied'

    def _ddl_error_is_idempotent(self, parsed, result, event) -> bool:
        """判断 DDL 报错是否"该变更已经生效过"（幂等重放）

        原型只对可**验证**的形态判幂等（ALTER ADD/DROP COLUMN）：不仅看
        错误码/报文，还核对目标表的真实结构；核对不上就按失败显式上抛。
        """
        cmd = str(parsed.get('command') or '')
        code = result.get('code')
        msg = str(result.get('message') or '')
        table = parsed.get('table')
        if cmd != 'ALTER TABLE':
            # CREATE FULLTEXT INDEX 重放：索引/表已存在时按幂等处理
            if cmd == 'CREATE FULLTEXT INDEX' and any(
                    k in msg for k in ('已存在', 'Duplicate', 'duplicate',
                                       'exists')):
                return True
            return False
        action = str(parsed.get('action') or '').upper()
        if action not in _IDEMPOTENT_DDL_ACTIONS:
            return False
        tbl = self.target_db.tables.get(table)
        if tbl is None:
            return False
        have = {str(c['name']) for c in tbl.cols}
        if action == 'ADD COLUMN':
            col = (parsed.get('column') or {}).get('name')
            return bool(col) and str(col) in have
        if action == 'DROP COLUMN':
            col = parsed.get('column')
            return bool(col) and str(col) not in have
        return False

    # ==================== 位点/延迟核算 ====================

    def _count_events_after(self, lsn: int) -> int:
        """主库 `lsn > N` 的事件条数（真实 COUNT，用于 events_behind）"""
        conn = self._connect()
        try:
            cols, rows = self._query(
                conn,
                f'SELECT COUNT(*) FROM __BINLOG_FETCH__ WHERE lsn > {int(lsn)}')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            return int(rows[0][0]) if rows and rows[0][0] is not None else 0
        except (TypeError, ValueError):
            return 0

    def check_binlog_gap(self, logs=None, agg=None):
        """检测"起始位点所在 binlog 已被清理/重建"（1236 风格，显式拒绝）

        判定（任一成立 → `ReplicaBinlogGap`）：
        1. 状态文件记录的 ``master_file`` 不在主库 `SHOW BINARY LOGS` 清单里
           （正常轮转会把旧文件保留，因此不会误报）；
        2. 本地已应用 ``lsn`` 早于主库当前**最早可读 lsn**（`MIN(lsn)`），
           中间事件已被删除 —— 无法补齐，自动跳过会静默丢数据。
        """
        if int(self.state.get('lsn') or 0) == 0:
            return None                          # 全新复制：从可用起点开始
        try:
            if logs is None:
                logs = self.binary_logs()
            if agg is None:
                agg = self._aggregate_lsn()
        except ReplicaError:
            raise                                # 链路错误由调用方按可重试处理
        names = [item.get('name') for item in (logs or [])]
        prev_file = self.state.get('master_file')
        earliest = int((agg or {}).get('min_lsn') or 0)
        local_lsn = int(self.state.get('lsn') or 0)
        missing_file = bool(prev_file and names and prev_file not in names)
        lsn_behind_earliest = bool(earliest > 0 and local_lsn < earliest)
        if missing_file or lsn_behind_earliest:
            reason = (f'状态文件记录的 binlog {prev_file!r} 已不在主库'
                      if missing_file else
                      '本地已应用位点早于主库最早可读事件')
            raise ReplicaBinlogGap(
                reason, available_logs=names, earliest_lsn=earliest,
                local_lsn=local_lsn)
        return None

    def reset_position(self) -> dict:
        """显式放弃缺失区间：把位点推进到主库当前可用末尾（CLI 手动动作）

        **会跳过主库已删除区间内的事件**，目标数据可能不一致；只在用户
        明确接受（CLI ``--reset-position``）或 1236 缺口后人工决策时调用。
        """
        status = self.master_status()
        agg = self._aggregate_lsn()
        max_pos = self._aggregate_max_pos()
        old_lsn = int(self.state.get('lsn') or 0)
        self.state['lsn'] = int(agg.get('max_lsn') or 0)
        self.state['seq'] = int(max_pos or 0)
        self.state['master_file'] = status['file']
        # ★ task-86：协议模式的续传位点也重置到主库当前文件/位点
        self.state['protocol_file'] = status['file']
        self.state['protocol_pos'] = int(status['position'])
        self.state['last_event_type'] = None
        self._last_error = None
        self._last_error_errno = None
        self._caught_up = True
        self._lag_bytes = 0
        self._lag_seconds = 0.0
        self._events_behind = 0
        self.save_state(sync=True)
        logger.warn(
            f'复制位点被显式重置：lsn {old_lsn} -> {self.state["lsn"]}'
            f'（跳过了主库已不可读区间，目标数据需自行校验）')
        return {'old_lsn': old_lsn, 'new_lsn': self.state['lsn'],
                'new_seq': self.state['seq'],
                'master_file': status['file'],
                'master_position': status['position'],
                'master_events': agg.get('count', 0)}

    def _update_lag_runtime(self, max_lsn: int, earliest_lsn: int,
                            events_behind: int):
        """更新运行时延迟快照（SHOW/metrics 用；不改持久位点）

        在锁内一次性更新 state 引用之外的运行时字段，`show_status()` 在
        同一把锁里复制快照 → 指标不会出现 "applied_lsn 已前进、
        master_lsn 还是上一批" 的撕裂值。
        """
        with self._lock:
            self._master_max_lsn = int(max_lsn or 0)
            self._earliest_master_lsn = int(earliest_lsn or 0)
            self._events_behind = max(0, int(events_behind or 0))
            local_lsn = int(self.state.get('lsn') or 0)
            self._lag_bytes = max(0, self._master_max_lsn - local_lsn)
            self._caught_up = (self._events_behind == 0)
            if self._caught_up:
                self._lag_seconds = 0.0
            else:
                last = float(self.state.get('last_apply_ts') or 0.0)
                self._lag_seconds = (round(time.time() - last, 3)
                                     if last > 0 else 0.0)

    # ==================== 批量运行 ====================

    def run_once(self, limit: int = 500, save_every: int = 50,
                 source: str = None) -> dict:
        """拉取一批并串行 apply；返回统计（events/applied/idempotent/...）

        ``source``：``export``=内部导出（默认，``__BINLOG_FETCH__``）；
        ``protocol``=任务 86 的 COM_BINLOG_DUMP 协议流。两种来源的事件
        六列语义一致（pos/lsn/txn_id/event_type/table_name/data），apply/
        断点/状态文件/指标逻辑完全复用；协议模式额外用
        ``protocol_file/protocol_pos`` 做精确的断线续传位点。

        * 先做位点缺口检测（1236 风格，显式拒绝而不是静默跳过）；
        * ``save_every``：每 N 条事件写一次状态（不 fsync）；批末统一
          flush+fsync 一次。崩溃最多回放最后一批事件（重放按幂等处理），
          换来 1000 事件 apply 的可用延迟（每条 fsync 在 Windows 上约 0.2s）；
        * **apply 失败不推进断点**：抛出前把 errors+1 写入状态文件（sync），
          下一轮从同一个 lsn 重试（幂等重放），绝不跳到失败事件之后；
        * 返回 ``caught_up/lag_lsn/lag_seconds/events_behind/master_lsn``
          等真实核算值（供指标/SHOW SLAVE STATUS/CLI 使用）。
        """
        started = time.time()
        source = str(source or self.source).lower()
        if source not in ('export', 'protocol'):
            raise ReplicaError(
                f'未知复制拉取方式: {source!r}（仅 export/protocol）')
        status = self.master_status()
        agg = self._aggregate_lsn()
        protocol_meta = None
        if source == 'protocol':
            fetched = self._fetch_protocol_events(limit)
            entries = fetched['events']
            protocol_meta = {k: fetched.get(k) for k in
                             ('header', 'eof', 'truncated', 'disconnect')}
        else:
            logs = self.binary_logs()
            self.check_binlog_gap(logs=logs, agg=agg)
            entries = [{'event': ev} for ev in
                       self.fetch_events(self.state['lsn'], limit)]
        events = [entry['event'] for entry in entries]
        stats = {'fetched': len(events), 'applied': 0, 'idempotent': 0,
                 'errors': 0, 'events': len(events), 'last_lsn': None,
                 'last_seq': None, 'master_file': status['file'],
                 'master_position': status['position'],
                 'master_lsn': int(agg.get('max_lsn') or 0),
                 'master_min_lsn': int(agg.get('min_lsn') or 0),
                 'source': source,
                 'elapsed_ms': 0.0, 'events_per_sec': 0.0}
        for idx, entry in enumerate(entries):
            ev = entry['event']
            try:
                res = self.apply_event(ev, persist=False)
            except ReplicaApplyError as e:
                with self._lock:
                    self.state['errors'] = int(
                        self.state.get('errors') or 0) + 1
                    self._last_error = str(e)
                    self._last_error_errno = getattr(e, 'errno', None)
                self.save_state(sync=True)       # 断点保留（lsn 未前进）
                stats['errors'] = 1
                stats['error'] = str(e)
                stats['elapsed_ms'] = round(
                    (time.time() - started) * 1000.0, 3)
                raise
            if res['result'] == 'applied':
                stats['applied'] += 1
            else:
                stats['idempotent'] += 1
            stats['last_lsn'] = res['lsn']
            stats['last_seq'] = int(self.state.get('seq') or 0)
            if source == 'protocol':
                # 断点续传位点：最后成功应用事件所在文件 + 事件结束偏移
                with self._lock:
                    if entry.get('file') is not None:
                        self.state['protocol_file'] = str(entry['file'])
                    if entry.get('next_pos') is not None:
                        self.state['protocol_pos'] = int(entry['next_pos'])
            if save_every and (idx + 1) % max(1, int(save_every)) == 0:
                self.save_state()
        # 协议流 EOF：把精确续传位点推进到 EOF 报告的位置（空批次也能前进）
        if protocol_meta and protocol_meta.get('eof'):
            eof = protocol_meta['eof'] or {}
            with self._lock:
                if eof.get('last_file'):
                    self.state['protocol_file'] = str(eof['last_file'])
                if eof.get('last_pos') is not None:
                    self.state['protocol_pos'] = int(eof['last_pos'])
        # 记录主库当前文件（用于清理检测；文件级，不含位点）
        self.state['master_file'] = status['file']
        # 批末 fsync 一次：断点续传位点的持久化点（每事件不 fsync）
        self.save_state(sync=True)

        # ===== 真实延迟核算（不含事件时间戳，口径见模块 docstring） =====
        local_lsn = int(self.state.get('lsn') or 0)
        # apply 期间主库可能继续写入：用**批末重新查询**的 lsn 范围核算，
        # 避免出现 "master_lsn < applied_lsn" 的陈旧值（如实采集）。
        try:
            agg_after = self._aggregate_lsn()
        except ReplicaError:
            agg_after = agg
        max_lsn = int(agg_after.get('max_lsn') or 0)
        earliest_lsn = int(agg_after.get('min_lsn') or 0)
        if max_lsn > local_lsn:
            try:
                events_behind = self._count_events_after(local_lsn)
            except ReplicaError:
                events_behind = len(events)
        else:
            events_behind = 0
        self._update_lag_runtime(max_lsn, earliest_lsn, events_behind)
        with self._lock:
            self._batches += 1
            self._last_batch_events = len(events)
            self._last_batch_ts = time.time()
            if events:
                self._last_error = None           # 成功应用后清空错误
                self._last_error_errno = None
        elapsed = time.time() - started
        stats['elapsed_ms'] = round(elapsed * 1000.0, 3)
        stats['events_per_sec'] = (
            round(len(events) / elapsed, 1) if elapsed > 0 else 0.0)
        stats['lag_ms'] = stats['elapsed_ms']
        stats['caught_up'] = self._caught_up
        stats['lag_lsn'] = self._lag_bytes
        stats['lag_seconds'] = self._lag_seconds
        stats['events_behind'] = self._events_behind
        stats['applied_lsn'] = local_lsn
        stats['applied_events'] = int(self.state.get('applied') or 0)
        stats['apply_errors'] = int(self.state.get('errors') or 0)
        stats['last_apply_ts'] = float(
            self.state.get('last_apply_ts') or 0.0)
        stats['protocol_file'] = self.state.get('protocol_file')
        stats['protocol_pos'] = self.state.get('protocol_pos')
        if protocol_meta is not None:
            stats['protocol_truncated'] = bool(
                protocol_meta.get('truncated'))
            stats['protocol_disconnect'] = protocol_meta.get('disconnect')
            stats['protocol_eof'] = bool(protocol_meta.get('eof'))
        stats['state'] = dict(self.state)
        return stats

    def run_until_caught_up(self, batch: int = 500,
                            max_batches: int = 1000,
                            source: str = None) -> dict:
        """连续应用直到没有新事件（用于测试/一次性同步）"""
        started = time.time()
        total = {'batches': 0, 'events': 0, 'applied': 0,
                 'idempotent': 0, 'elapsed_ms': 0.0, 'caught_up': False}
        for _ in range(max(1, int(max_batches))):
            stats = self.run_once(limit=batch, source=source)
            total['batches'] += 1
            total['events'] += stats['events']
            total['applied'] += stats['applied']
            total['idempotent'] += stats['idempotent']
            if stats['caught_up']:
                total['caught_up'] = True
                break
        total['elapsed_ms'] = round((time.time() - started) * 1000.0, 3)
        total['events_per_sec'] = (
            round(total['events'] / (total['elapsed_ms'] / 1000.0), 1)
            if total['elapsed_ms'] > 0 else 0.0)
        total['lag_seconds'] = self._lag_seconds
        total['lag_lsn'] = self._lag_bytes
        total['caught_up'] = self._caught_up
        total['state'] = dict(self.state)
        return total

    def follow(self, interval: float = 0.2, batch: int = 500,
               stop_event: threading.Event = None, max_events: int = None,
               retry_backoff: float = 0.5, max_backoff: float = 30.0,
               max_retries: int = 0, source: str = None) -> dict:
        """持续增量跟随（单线程轮询；失败保留断点、退避重试、不跳过）

        语义（task-81）：

        * 每一轮从**持久 lsn** 拉取增量并 apply；apply 失败 → 状态文件停在
          最后成功事件、错误数+1，按指数退避重试同一位点（幂等重放）；
        * ``max_retries=0`` 表示无限重试（生产默认）；>0 时连续失败超过上限
          抛 ``ReplicaError``（显式，不自动 failover）；
        * **binlog 位点缺口**（``ReplicaBinlogGap``，errno 1236）是终态错误：
          缺失事件无法补齐，立即抛给调用方并附恢复策略（不自动跳过）；
        * ``stop_event`` 置位后返回；``max_events`` 达到后返回（测试用）；
        * 每次重试/错误都写日志（含当前断点位点），可观测、可告警。
        """
        applied_total = 0
        events_total = 0
        retries = 0                 # 连续失败次数（退避用；成功后清零）
        total_retries = 0           # 累计重试次数（结果/指标用，不清零）
        failures = 0
        started = time.time()
        with self._lock:
            self._following = True
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return self._follow_result(
                        applied_total, events_total, total_retries, True,
                        started)
                try:
                    stats = self.run_once(limit=batch, source=source)
                    retries = 0
                except ReplicaBinlogGap:
                    # 位点缺口：终态错误，显式上抛（含可用文件/恢复策略）
                    raise
                except ReplicaError as e:
                    failures += 1
                    retries += 1
                    total_retries += 1
                    with self._lock:
                        self._retries += 1
                        self._last_error = str(e)
                        self._last_error_errno = getattr(e, 'errno', None)
                    delay = min(float(max_backoff),
                                float(retry_backoff)
                                * (2 ** min(retries - 1, 16)))
                    logger.warn(
                        f'增量复制失败（第 {retries} 次，累计 {failures} 次）'
                        f'，断点 lsn={self.state.get("lsn")} 保留不跳过；'
                        f'{delay:.2f}s 后重试: {e}')
                    if max_retries and retries > int(max_retries):
                        raise
                    if stop_event is not None:
                        # 可中断的退避等待
                        if stop_event.wait(delay):
                            return self._follow_result(
                                applied_total, events_total, total_retries,
                                True, started)
                    else:
                        time.sleep(delay)
                    continue
                applied_total += stats['events']
                events_total += stats['events']
                if max_events is not None and events_total >= int(max_events):
                    return self._follow_result(
                        applied_total, events_total, total_retries, True,
                        started)
                if stats['events'] == 0:
                    if stop_event is not None:
                        if stop_event.wait(float(interval)):
                            return self._follow_result(
                                applied_total, events_total, total_retries,
                                True, started)
                    else:
                        time.sleep(float(interval))
        finally:
            with self._lock:
                self._following = False
                self._last_batch_ts = time.time()

    def _follow_result(self, applied, events, retries, stopped,
                       started) -> dict:
        elapsed = time.time() - started
        with self._lock:
            caught_up = bool(self._caught_up)
            lag_seconds = float(self._lag_seconds)
            lag_lsn = int(self._lag_bytes)
            state = dict(self.state)
        return {
            'applied': int(applied), 'events': int(events),
            'retries': int(retries), 'stopped': bool(stopped),
            'elapsed_ms': round(elapsed * 1000.0, 3),
            'events_per_sec': (round(events / elapsed, 1)
                               if elapsed > 0 else 0.0),
            'caught_up': caught_up,
            'lag_seconds': lag_seconds,
            'lag_lsn': lag_lsn,
            'state': state,
        }

    # ==================== 状态展示（SHOW SLAVE STATUS / 指标） ====================

    def show_status(self) -> dict:
        """真实复制状态快照（供 `Database.replication_status()` 使用）

        返回两类键：
        * MySQL `SHOW SLAVE STATUS` 列名：真实主库连接、位点、延迟、错误
          （位点 = 状态文件里的**全局 lsn**；`Seconds_Behind_Master` 用
          `lag_seconds`：追上时 0，落后时=距上次成功 apply 的秒数）；
        * ``Replica_*`` 自定义键：给 `/metrics` 渲染用（executor 的
          `_slave_status_values` 只取已知列，未知键不影响 SHOW 输出）。
        无数据（全新/未运行）时数值列输出 0，不编造非零值。
        """
        # 同一把锁内复制状态+运行时字段，避免指标/SHOW 读到撕裂快照
        with self._lock:
            state = dict(self.state)
            following = self._following
            closed = self._closed
            last_error = self._last_error
            last_error_errno = self._last_error_errno
            master_max_lsn = int(self._master_max_lsn)
            master_min_lsn = int(self._earliest_master_lsn)
            lag_bytes = int(self._lag_bytes)
            lag_seconds = float(self._lag_seconds)
            events_behind = int(self._events_behind)
            caught_up = bool(self._caught_up)
            last_batch_events = int(self._last_batch_events)
            batches = int(self._batches)
            retries = int(self._retries)
        lsn = int(state.get('lsn') or 0)
        applied = int(state.get('applied') or 0)
        errors = int(state.get('errors') or 0)
        last_ts = float(state.get('last_apply_ts') or 0.0)
        running = following and not closed
        has_error = bool(last_error)
        if running and has_error:
            io_running = 'Connecting'
            st = f'Retrying: {str(last_error)[:200]}'
        elif running:
            io_running = 'Yes'
            st = 'Replica polling master for binlog events'
        elif has_error:
            io_running = 'No'
            st = f'Error: {str(last_error)[:200]}'
        elif applied or lsn:
            io_running = 'No'
            st = 'Replica idle (last run completed; not following)'
        else:
            io_running = 'No'
            st = 'Replica not started (prototype registered)'
        errno = last_error_errno or 0
        state_size = 0
        try:
            if os.path.exists(str(self.state_path)):
                state_size = os.path.getsize(str(self.state_path))
        except OSError:                          # pragma: no cover - 防御
            state_size = 0
        return {
            'Slave_IO_State': st,
            'Master_Host': self.master_host,
            'Master_User': self.user,
            'Master_Port': self.master_port,
            'Connect_Retry': 1,
            'Master_Log_File': state.get('master_file'),
            'Read_Master_Log_Pos': lsn,
            'Relay_Log_File': None,
            'Relay_Log_Pos': None,
            'Relay_Master_Log_File': state.get('master_file'),
            'Slave_IO_Running': io_running,
            'Slave_SQL_Running': 'Yes' if (running and not has_error)
            else 'No',
            'Last_Errno': errno,
            'Last_Error': str(self._last_error or ''),
            'Skip_Counter': 0,
            'Exec_Master_Log_Pos': lsn,
            'Relay_Log_Space': state_size,
            'Seconds_Behind_Master': (float(self._lag_seconds)
                                      if (running or applied or lsn)
                                      else 0.0),
            'Last_IO_Errno': errno,
            'Last_IO_Error': str(last_error or ''),
            'Last_SQL_Errno': errno,
            'Last_SQL_Error': str(last_error or ''),
            'Master_Info_File': str(self.state_path),
            'Slave_SQL_Running_State': st,
            # ---- 自定义快照（/metrics 用；SHOW 列清单会忽略这些键）----
            'Replica_Is_Prototype': True,
            'Replica_Node': 'apply',
            'Replica_Source': self.source,
            'Replica_Protocol_File': state.get('protocol_file'),
            'Replica_Protocol_Pos': state.get('protocol_pos'),
            'Replica_Master_Host': self.master_host,
            'Replica_Master_Port': self.master_port,
            'Replica_Following': 1 if running else 0,
            'Replica_Mode': 'follow' if running else 'once',
            'Replica_Applied_LSN': lsn,
            'Replica_Master_LSN': master_max_lsn,
            'Replica_Master_Min_LSN': master_min_lsn,
            'Replica_Lag_LSN': lag_bytes,
            'Replica_Lag_Seconds': lag_seconds,
            'Replica_Events_Behind': events_behind,
            'Replica_Applied_Events': applied,
            'Replica_Idempotent_Events': int(state.get('idempotent') or 0),
            'Replica_Apply_Errors': errors,
            'Replica_Last_Apply_Ts': last_ts,
            'Replica_Last_Batch_Events': last_batch_events,
            'Replica_Batches': batches,
            'Replica_Retries': retries,
            'Replica_Caught_Up': 1 if caught_up else 0,
            'Replica_Error_Errno': errno,
            'Replica_State_File': str(self.state_path),
            'Replica_State_Valid': 1,
        }

    def stats(self) -> dict:
        """当前状态快照（CLI/测试/排障用）"""
        with self._lock:
            state = dict(self.state)
            state.update({
                'state_path': str(self.state_path),
                'target_data_dir': str(self.target_db.data_dir),
                'source': self.source,
                'protocol_file': self.state.get('protocol_file'),
                'protocol_pos': self.state.get('protocol_pos'),
                'last_error': self._last_error,
                'last_error_errno': self._last_error_errno,
                'following': self._following,
                'caught_up': self._caught_up,
                'lag_lsn': self._lag_bytes,
                'lag_seconds': self._lag_seconds,
                'events_behind': self._events_behind,
                'master_lsn': self._master_max_lsn,
                'master_min_lsn': self._earliest_master_lsn,
                'batches': self._batches,
                'retries': self._retries,
                'process_uptime': round(time.time() - self.started_at, 3),
            })
        return state

