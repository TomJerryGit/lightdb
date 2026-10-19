"""真 · append-only binlog（决策 D 项：独立于各表 WAL 的持久复制日志）

为什么要有这个文件
==================

旧实现（`lightdb/replication/binlog.py` 的 JSONL + `Executor._binlog_events()`
的"查询时重扫各表 WAL"）不是可复制的日志：

* 位点来自**当前** WAL 事件的序号：WAL 被 checkpoint 退休后事件消失，
  `SHOW MASTER STATUS` 的 Position 会**回退**（实测 ALTER/checkpoint 后
  14 → 12），从库的高水位会永久跳过新事件；
* 事件按「表名排序 + 摊平」重新编号，前序表新增事件会把后面表的事件位点
  推后，从库按 watermark 拉取时看不到（实测新 DML 不同步）；
* 没有 CRC、没有事务边界，损坏无从发现。

本模块提供一个**独立的 append-only 文件**：`<data_dir>/binlog/binlog.000001`。
写入只追加、位点（字节偏移）单调递增、不随任何表 WAL 的 retire/checkpoint
而改变。读取校验文件头 magic/version 与每条记录的 CRC32，损坏/截断**显式
报错**（绝不静默跳过）——这与 WAL 的"尽力恢复"语义不同，是复制日志的
正确取向。

磁盘格式
========

文件头（固定 16 字节）::

    magic      8 字节  b'LDBBLOG1'
    version    u32     FORMAT_VERSION
    header_crc u32     CRC32(magic + version)

记录（固定 17 字节记录头 + JSON payload + CRC32）::

    type       u8      记录类型（见 TYPE_*）
    pos        u64     本记录起始的**绝对字节偏移**（含文件头；单调）
    length     u32     payload 长度（字节）
    crc32      u32     CRC32(type 字节 + pos + length + payload)
    payload    length  UTF-8 JSON（对象；含公共字段 pos/txn_id/ts/server_id）

记录类型：``FORMAT_DESCRIPTION``（文件首条，描述格式）、``BEGIN`` /
``COMMIT`` / ``ROLLBACK``（事务边界）、``INSERT`` / ``UPDATE`` / ``DELETE``
（行变更，payload 含表名 + 列清单 + 行镜像）、``DDL``（结构变更 SQL）。

写入语义
========

* **组提交**：DML 先进内存（按组 id 缓冲），事务提交时一次 ``write`` 把
  ``BEGIN → 行事件… → COMMIT`` 整组写出去（autocommit 语句也是独立一组）；
  `ROLLBACK` 只写 ``BEGIN → ROLLBACK``（行事件从未持久化，如实记"被丢弃
  的条数"，不伪造数据变更）；
* DDL 执行成功后写独立一组 ``BEGIN → DDL → COMMIT``（DDL 自带隐式提交，
  分组只是为了让读取端的边界语义统一）；
* `fsync` 默认关闭（与 WAL 的 autocommit 持久性档位一致），
  ``LIGHTDB_BINLOG_FSYNC=1`` 可开启；
* 写端启动时只允许修复"末尾半条记录"（崩溃时写了一半）：扫描到最后一个
  完整记录后截断文件并告警。**中间**记录的 CRC/位点不一致一律拒绝打开
  （抛 ``BinlogCorrupted``），不静默丢弃。

读取接口
========

``AppendOnlyBinlog`` 同时是写端与读端（同一实例内含跨进程文件锁语义的
线程锁；多进程读同理安全，因为文件只追加）：

* ``read_from(pos, limit=None)``：读取起始偏移 ``>= pos`` 的记录；
* ``read_all()`` / ``iter_records()``：全量按顺序读（校验 CRC）；
* ``data_events()``：只取行变更/DDL 事件，并给每条分配 **1 起的逻辑序号**
  （`seq`，即 ``__BINLOG_FETCH__`` 的 pos 列；字节偏移在 ``lsn`` 列），
  保持与旧复制接口的 6 列兼容：``(pos, lsn, txn_id, event_type,
  table_name, data)``；
* ``current_file`` / ``current_pos``：真实文件名与**真实字节位点**。

已知边界（本轮未做，留给后续迭代，代码里以 TODO 标注）
=====================================================

* 单文件、无轮转（``max_bytes`` 仅做告警，不切换文件）；
* 无索引文件 / 无 GTID / 无半同步；
* 直接写存储层（``Table.insert`` 等）的路径不经过执行器，因此不产生
  binlog 事件 —— binlog 记录的是"经执行器执行并提交的语句"；
* 从库重放 DDL 走执行器，DML 走存储层（与 MySQL 行格式复制同构，但要
  求两边 schema 一致；不做 DDL 自动修复）。
"""
import json
import os
import struct
import threading
import time
import zlib
from pathlib import Path

from ..logger import logger

# ==================== 格式常量 ====================

MAGIC = b'LDBBLOG1'
FORMAT_VERSION = 1
FILE_HEADER_SIZE = 16
RECORD_HEADER_SIZE = 17

#: 单条 payload 上限（防御性：读到荒谬长度视为文件损坏而不是申请内存）
MAX_PAYLOAD = 64 * 1024 * 1024

#: binlog 文件所在子目录（相对 data_dir）
BINLOG_SUBDIR = 'binlog'
#: 当前日志文件名（无轮转；位置只增不减，所以文件名固定）
DEFAULT_FILE = 'binlog.000001'

# 记录类型
TYPE_FORMAT_DESCRIPTION = 1
TYPE_BEGIN = 2
TYPE_COMMIT = 3
TYPE_ROLLBACK = 4
TYPE_INSERT = 5
TYPE_UPDATE = 6
TYPE_DELETE = 7
TYPE_DDL = 8

TYPE_NAMES = {
    TYPE_FORMAT_DESCRIPTION: 'FORMAT_DESCRIPTION',
    TYPE_BEGIN: 'BEGIN',
    TYPE_COMMIT: 'COMMIT',
    TYPE_ROLLBACK: 'ROLLBACK',
    TYPE_INSERT: 'INSERT',
    TYPE_UPDATE: 'UPDATE',
    TYPE_DELETE: 'DELETE',
    TYPE_DDL: 'DDL',
}
NAME_TYPES = {v: k for k, v in TYPE_NAMES.items()}

#: 行变更事件（从库拉取 / 复制的数据事件）
DATA_TYPES = (TYPE_INSERT, TYPE_UPDATE, TYPE_DELETE, TYPE_DDL)
#: 事务边界事件
CONTROL_TYPES = (TYPE_BEGIN, TYPE_COMMIT, TYPE_ROLLBACK)

_HEADER_STRUCT = struct.Struct('<8sII')
_RECORD_PREFIX = struct.Struct('<BQI')      # type / pos / length
_RECORD_FULL = struct.Struct('<BQII')       # type / pos / length / crc32


# ==================== 环境开关 ====================

def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'off', 'no', '')


def binlog_enabled() -> bool:
    """是否启用真 binlog（默认启用；``LIGHTDB_BINLOG_ENABLED=0`` 关闭）"""
    return env_flag('LIGHTDB_BINLOG_ENABLED', True)


def binlog_fsync() -> bool:
    """组提交是否 fsync（默认关闭，与引擎 autocommit 持久性档位一致）"""
    return env_flag('LIGHTDB_BINLOG_FSYNC', False)


def binlog_max_bytes() -> int:
    raw = os.environ.get('LIGHTDB_BINLOG_MAX_BYTES')
    try:
        val = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        val = 0
    return val if val > 0 else 64 * 1024 * 1024


# ==================== 异常 ====================

class BinlogError(Exception):
    """binlog 读写错误的基类"""


class BinlogCorrupted(BinlogError):
    """文件结构/CRC 损坏：**必须**中断读取，绝不静默跳过。"""

    def __init__(self, message: str, file: str = None, pos: int = None):
        self.file = file
        self.pos = pos
        where = ''
        if file is not None:
            where = f' [{file}@{pos}]'
        super().__init__(f'binlog 损坏{where}: {message}')


class BinlogTruncated(BinlogError):
    """文件在记录中间截断（末尾半条 / 读越界）"""

    def __init__(self, message: str, file: str = None, pos: int = None):
        self.file = file
        self.pos = pos
        where = ''
        if file is not None:
            where = f' [{file}@{pos}]'
        super().__init__(f'binlog 截断{where}: {message}')


# ==================== 记录对象 ====================

class BinlogRecord:
    """一条已解码并校验通过的事件记录"""

    __slots__ = ('type', 'type_name', 'pos', 'end_pos', 'payload',
                 'size', 'seq')

    def __init__(self, rtype: int, pos: int, payload: dict, size: int,
                 seq: int = None):
        self.type = rtype
        self.type_name = TYPE_NAMES.get(rtype, f'UNKNOWN_{rtype}')
        self.pos = pos
        self.end_pos = pos + size
        self.payload = payload or {}
        self.size = size
        self.seq = seq

    # ---- 常用字段便捷访问（payload 里缺失就如实返回 None） ----
    @property
    def txn_id(self):
        return self.payload.get('txn_id')

    @property
    def ts(self):
        return self.payload.get('ts')

    @property
    def server_id(self):
        return self.payload.get('server_id')

    @property
    def table(self):
        return self.payload.get('table')

    @property
    def data(self):
        return self.payload.get('data')

    def summary(self) -> str:
        """`SHOW BINLOG EVENTS.Info` 用的一行摘要（不编造 content）"""
        p = self.payload
        if self.type == TYPE_FORMAT_DESCRIPTION:
            return (f"format_version={p.get('format_version')} "
                    f"server_version={p.get('server_version')} "
                    f"engine={p.get('engine')}")
        if self.type in (TYPE_BEGIN, TYPE_COMMIT, TYPE_ROLLBACK):
            extra = ''
            if self.type == TYPE_ROLLBACK:
                extra = f" discarded={p.get('discarded', 0)}"
            return f"txn_id={p.get('txn_id')}{extra}"
        if self.type == TYPE_DDL:
            sql = p.get('sql')
            if not sql:
                sql = (p.get('data') or {}).get('sql', '')
            return f"sql={str(sql)[:300]}"
        data = p.get('data') or {}
        if self.type == TYPE_INSERT:
            return f"table={p.get('table')} row={json.dumps(data.get('row'), ensure_ascii=False, default=str)[:200]}"
        if self.type == TYPE_UPDATE:
            return (f"table={p.get('table')} pk={data.get('pk')!r} "
                    f"new={json.dumps(data.get('new'), ensure_ascii=False, default=str)[:200]}")
        if self.type == TYPE_DELETE:
            return f"table={p.get('table')} pk={data.get('pk')!r}"
        return json.dumps(p, ensure_ascii=False, default=str)[:300]

    def to_fetch_row(self):
        """`__BINLOG_FETCH__` 的一行（6 列兼容形态）

        pos   = 数据事件逻辑序号（1 起，单调；兼容旧复制接口）
        lsn   = 真实字节偏移（payload.pos）
        """
        return (self.seq, self.pos, self.txn_id, self.type_name,
                self.table, json.dumps(self.data, ensure_ascii=False,
                                       default=str))

    def __repr__(self):
        return (f'<BinlogRecord {self.type_name} pos={self.pos} '
                f'end={self.end_pos} txn={self.txn_id}>')


# ==================== 写 + 读 ====================

def _new_server_id() -> int:
    """进程内唯一 server_id（**不冒充** MySQL 的 server_uuid/GTID）

    取值：pid 与启动时间混合，32 位非零整数。用于 `SHOW BINLOG EVENTS.
    Server_id` 与 payload.server_id；同一进程内稳定，跨进程不同。
    """
    mixed = (os.getpid() * 2654435761 + int(time.time() * 1000)) & 0x7FFFFFFF
    return mixed or 1


# ==================== 字节流解码（严格校验）====================

def iter_records_bytes(raw: bytes, file_name: str = DEFAULT_FILE,
                       start: int = FILE_HEADER_SIZE):
    """从文件字节里逐条解码记录（严格校验 pos 连续 + CRC32）

    独立函数而不是实例方法：只读校验/测试可以在**不打开写端**的情况下
    复用它（写端打开时会修复"末尾半条记录"，那样就测不到严格读取路径了）。
    """
    offset = start
    n = len(raw)
    while offset < n:
        if offset + RECORD_HEADER_SIZE > n:
            raise BinlogTruncated(
                f'记录头不完整（offset={offset}, '
                f'剩余 {n - offset} 字节 < {RECORD_HEADER_SIZE}）',
                file_name, offset)
        rtype, pos, length, crc = _RECORD_FULL.unpack_from(raw, offset)
        end = offset + RECORD_HEADER_SIZE + length
        if length > MAX_PAYLOAD:
            raise BinlogCorrupted(
                f'payload 长度异常（{length} > {MAX_PAYLOAD}）',
                file_name, offset)
        if end > n:
            raise BinlogTruncated(
                f'记录 payload 不完整（需要 {end} 字节，文件只有 {n}）',
                file_name, offset)
        if pos != offset:
            raise BinlogCorrupted(
                f'记录位点不连续（记录头 pos={pos}，实际 offset={offset}）'
                f' —— 可能是插入/删除/改写，拒绝静默跳过',
                file_name, offset)
        body = raw[offset + RECORD_HEADER_SIZE:end]
        prefix = _RECORD_PREFIX.pack(rtype, pos, length)
        want = zlib.crc32(prefix + body) & 0xFFFFFFFF
        if crc != want:
            raise BinlogCorrupted(
                f'记录 CRC32 校验失败（存={crc:#010x}，算={want:#010x}）',
                file_name, offset)
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            raise BinlogCorrupted(
                f'payload 不是合法 JSON: {e}', file_name, offset)
        if not isinstance(payload, dict):
            raise BinlogCorrupted('payload 不是 JSON 对象',
                                  file_name, offset)
        yield BinlogRecord(rtype, pos, payload, end - offset)
        offset = end


class AppendOnlyBinlog:
    """append-only binlog 的写端 + 读端（线程安全）"""

    def __init__(self, data_dir: str, file_name: str = DEFAULT_FILE,
                 fsync: bool = None, max_bytes: int = None):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / BINLOG_SUBDIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file_name = file_name
        self.path = self.dir / file_name
        self.fsync = binlog_fsync() if fsync is None else bool(fsync)
        self.max_bytes = binlog_max_bytes() if max_bytes is None else int(max_bytes)
        self.server_id = _new_server_id()

        self._lock = threading.RLock()
        #: 组 id -> [{'type': int, 'table': str|None, 'data': dict}]
        self._groups = {}
        #: 组 id -> txn_id
        self._group_txn = {}
        self._auto_group_seq = 0
        self._data_seq = 0            # 已写数据事件序号（fetch 的 pos 列）
        self._pos = 0                 # 下一个可写字节偏移（= 文件大小）
        self._size_warned = False
        self._fh = None
        self._open()

    # ==================== 打开 / 恢复 ====================
    def _open(self):
        exists = self.path.exists()
        if not exists or self.path.stat().st_size == 0:
            self._fh = open(self.path, 'wb')
            self._write_header_and_format()
        else:
            self._validate_and_recover()
            self._fh = open(self.path, 'ab')
        self._pos = self.path.stat().st_size

    def _write_header_and_format(self):
        header = _HEADER_STRUCT.pack(
            MAGIC, FORMAT_VERSION,
            zlib.crc32(MAGIC + struct.pack('<I', FORMAT_VERSION)) & 0xFFFFFFFF)
        self._fh.write(header)
        self._fh.flush()
        payload = {
            'type': 'FORMAT_DESCRIPTION',
            'format_version': FORMAT_VERSION,
            'server_version': _engine_version(),
            'engine': 'LightDB',
            'server_id': self.server_id,
            'created_at': time.time(),
        }
        blob = self._encode_record(TYPE_FORMAT_DESCRIPTION,
                                   FILE_HEADER_SIZE, payload)
        self._fh.write(blob)
        self._fh.flush()
        if self.fsync:
            self._sync()
        self._pos = FILE_HEADER_SIZE + len(blob)

    def _validate_and_recover(self):
        """校验文件头 + 全量记录；只允许修复**末尾**截断"""
        with open(self.path, 'rb') as f:
            raw = f.read()
        if len(raw) < FILE_HEADER_SIZE:
            raise BinlogCorrupted(
                f'文件短于文件头（{len(raw)} < {FILE_HEADER_SIZE}）',
                self.file_name, 0)
        magic, version, header_crc = _HEADER_STRUCT.unpack(raw[:FILE_HEADER_SIZE])
        want = zlib.crc32(magic + struct.pack('<I', version)) & 0xFFFFFFFF
        if magic != MAGIC:
            raise BinlogCorrupted(
                f'文件头 magic 不匹配（{magic!r} != {MAGIC!r}）',
                self.file_name, 0)
        if header_crc != want:
            raise BinlogCorrupted('文件头 header_crc 校验失败',
                                  self.file_name, 8)
        if version != FORMAT_VERSION:
            raise BinlogCorrupted(
                f'不支持的格式版本 {version}（本实现支持 {FORMAT_VERSION}）',
                self.file_name, 8)

        valid_end = FILE_HEADER_SIZE
        seq = 0
        try:
            for rec in self._iter_bytes(raw):
                valid_end = rec.end_pos
                if rec.type in DATA_TYPES:
                    seq += 1
        except BinlogTruncated as e:
            # 只允许"末尾半条记录"（进程崩溃时写了一半）：截断到最后一条
            # 完整记录之后，并明确告警 —— 中间的 CRC/位点错误不会被吞。
            logger.warn(
                f'binlog {self.file_name} 末尾存在未写完的记录'
                f'（offset={valid_end}），已截断修复：{e}')
            with open(self.path, 'r+b') as f:
                f.truncate(valid_end)
                f.flush()
                os.fsync(f.fileno())
        except BinlogCorrupted:
            raise
        self._data_seq = seq
        # 文件大小变了（截断）后重新取
        self._pos = self.path.stat().st_size
        if self._pos != valid_end:            # pragma: no cover - 防御
            self._pos = valid_end

    # ==================== 编码 ====================
    def _encode_record(self, rtype: int, pos: int,
                       payload: dict) -> bytes:
        payload = dict(payload)
        payload.setdefault('pos', pos)
        payload.setdefault('server_id', self.server_id)
        payload.setdefault('ts', time.time())
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode('utf-8')
        if len(body) > MAX_PAYLOAD:
            raise BinlogError(
                f'单条事件 payload 过大（{len(body)} > {MAX_PAYLOAD}）')
        prefix = _RECORD_PREFIX.pack(rtype, pos, len(body))
        crc = zlib.crc32(prefix + body) & 0xFFFFFFFF
        return _RECORD_FULL.pack(rtype, pos, len(body), crc) + body

    # ==================== 写：组提交 ====================
    def new_group_id(self):
        """分配一个新的 **autocommit 语句组** id（唯一、进程内）"""
        with self._lock:
            self._auto_group_seq += 1
            return ('auto', self._auto_group_seq)

    @staticmethod
    def txn_group_id(txn_id: int):
        """把真实事务号映射成组 id（Database 提交/回滚钩子用）"""
        return ('txn', int(txn_id))

    # -- 兼容旧 `MasterReplicator` 的简单追加接口（不属于组提交） --
    def append(self, txn_id: int, etype: str, table, data) -> int:
        """单条事件的便捷追加（**不**在事务组里；DDL/兼容调用用）

        返回该事件的字节偏移。DML 请走 `record()` + `commit_group()`，
        以保证 `BEGIN → 行事件 → COMMIT` 的组边界。
        """
        rtype = NAME_TYPES.get(str(etype or '').upper())
        if rtype is None or rtype == TYPE_FORMAT_DESCRIPTION:
            raise BinlogError(f'不支持的 binlog 事件类型: {etype!r}')
        gid = self.new_group_id()
        if rtype == TYPE_DDL:
            return self.write_ddl((data or {}).get('sql', ''),
                                  table=table, txn_id=txn_id)
        self.record(gid, TYPE_NAMES[rtype], table,
                    data or {}, txn_id=txn_id or 0)
        return self.commit_group(gid)

    def record(self, gid, event_type: str, table, data: dict,
               txn_id: int = 0):
        """把一条数据事件放进事务组的内存缓冲（提交时才落盘）"""
        rtype = NAME_TYPES.get(str(event_type or '').upper())
        if rtype not in DATA_TYPES:
            raise BinlogError(f'record() 只接受行变更/DDL 事件: {event_type!r}')
        with self._lock:
            self._groups.setdefault(gid, []).append({
                'type': rtype, 'table': table, 'data': data or {},
                'txn_id': int(txn_id or 0),
            })
            self._group_txn.setdefault(gid, int(txn_id or 0))

    def begin_group(self, gid, txn_id: int = 0):
        """显式声明组（可省略：record() 会懒创建）"""
        with self._lock:
            self._groups.setdefault(gid, [])
            self._group_txn.setdefault(gid, int(txn_id or 0))

    def discard_group(self, gid):
        """丢弃内存里的组（语句失败/回滚；不写任何字节）"""
        with self._lock:
            self._groups.pop(gid, None)
            self._group_txn.pop(gid, None)

    def commit_group(self, gid) -> int:
        """提交一个组：一次 write 写出 BEGIN → 事件… → COMMIT

        返回组结束后的字节位点（= 下一条记录的起始偏移）。
        """
        with self._lock:
            rows = self._groups.pop(gid, [])
            txn_id = self._group_txn.pop(gid, 0)
            if not rows:
                return self._pos
            return self._append_group(rows, txn_id)

    def commit_txn(self, txn_id: int) -> int:
        """Database.commit()/commit_statement() 钩子：提交真实事务的组"""
        return self.commit_group(self.txn_group_id(txn_id))

    def rollback_txn(self, txn_id: int) -> int:
        """Database.abort() 钩子：写 `BEGIN → ROLLBACK`（不伪造行事件）"""
        return self.rollback_group(self.txn_group_id(txn_id))

    def rollback_group(self, gid) -> int:
        with self._lock:
            rows = self._groups.pop(gid, None)
            txn_id = self._group_txn.pop(gid, 0)
            if rows is None:
                return self._pos
            payload = {'type': 'BEGIN', 'txn_id': txn_id,
                       'rolled_back': True}
            begin = self._encode_record(TYPE_BEGIN, self._pos, payload)
            start_rollback = self._pos + len(begin)
            rb = self._encode_record(
                TYPE_ROLLBACK, start_rollback,
                {'type': 'ROLLBACK', 'txn_id': txn_id,
                 'discarded': len(rows)})
            blob = begin + rb
            self._write_blob(blob)
            return self._pos

    def write_ddl(self, sql: str, table: str = None,
                  txn_id: int = 0) -> int:
        """DDL 成功后写一组 `BEGIN → DDL → COMMIT`，返回结束字节位点"""
        with self._lock:
            return self._append_group(
                [{'type': TYPE_DDL, 'table': table,
                  'data': {'sql': str(sql or '')}, 'txn_id': txn_id}],
                txn_id)

    def _append_group(self, rows, txn_id: int) -> int:
        # BEGIN
        cur = self._pos
        begin = self._encode_record(
            TYPE_BEGIN, cur,
            {'type': 'BEGIN', 'txn_id': txn_id})
        blobs = [begin]
        cur += len(begin)
        for row in rows:
            rtype = row['type']
            if rtype in DATA_TYPES:
                self._data_seq += 1
                seq = self._data_seq
            else:                              # pragma: no cover - 防御
                seq = None
            payload = {
                'type': TYPE_NAMES.get(rtype),
                'txn_id': row.get('txn_id', txn_id),
                'table': row.get('table'),
                'data': row.get('data') or {},
            }
            if seq is not None:
                payload['seq'] = seq
            rec = self._encode_record(rtype, cur, payload)
            blobs.append(rec)
            cur += len(rec)
        commit = self._encode_record(
            TYPE_COMMIT, cur,
            {'type': 'COMMIT', 'txn_id': txn_id,
             'events': len(rows)})
        blobs.append(commit)
        self._write_blob(b''.join(blobs))
        return self._pos

    def _write_blob(self, blob: bytes):
        if self._fh is None:
            raise BinlogError('binlog 已关闭，无法写入')
        try:
            self._fh.write(blob)
            self._fh.flush()
            if self.fsync:
                self._sync()
        except Exception:
            # 写失败：回退内存序号，避免与磁盘位点漂移（组边界原子写，
            # 失败时整组不落地；已写入的部分由启动时"末尾截断修复"处理）
            self._pos = self.path.stat().st_size
            raise
        self._pos += len(blob)
        if self._pos > self.max_bytes and not self._size_warned:
            self._size_warned = True
            logger.warn(
                f'binlog {self.file_name} 已达 {self._pos} 字节'
                f'（> LIGHTDB_BINLOG_MAX_BYTES={self.max_bytes}）：'
                f'当前实现**不做轮转**（TODO 阶段 2），位点继续单调增长')

    def _sync(self):
        try:
            os.fsync(self._fh.fileno())
        except OSError as e:               # pragma: no cover - 平台防御
            logger.warn(f'binlog fsync 失败: {e}')

    def flush(self):
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                if self.fsync:
                    self._sync()

    def close(self):
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    # ==================== 属性 ====================
    @property
    def current_file(self) -> str:
        return self.file_name

    @property
    def current_pos(self) -> int:
        """文件当前末尾的**真实字节偏移**（下一条记录将写在这里）"""
        with self._lock:
            try:
                return self.path.stat().st_size
            except OSError:                # pragma: no cover - 防御
                return self._pos

    @property
    def data_event_count(self) -> int:
        """已写入的数据事件（INSERT/UPDATE/DELETE/DDL）条数"""
        with self._lock:
            return self._data_seq

    # ==================== 读 ====================
    def _iter_bytes(self, raw: bytes, start: int = FILE_HEADER_SIZE):
        """从内存字节里逐条解码记录（严格校验 pos 连续 + CRC）"""
        yield from iter_records_bytes(raw, self.file_name, start)

    def iter_records(self, start_pos: int = FILE_HEADER_SIZE):
        """按顺序读记录（校验 CRC/位点；损坏显式抛错）"""
        with self._lock:
            try:
                with open(self.path, 'rb') as f:
                    raw = f.read()
            except FileNotFoundError:
                return
        for rec in self._iter_bytes(raw, start_pos):
            yield rec

    def read_from(self, pos: int = FILE_HEADER_SIZE, limit: int = None,
                  include_control: bool = True):
        """读取起始偏移 ``>= pos`` 的记录（limit=None 表示全部）"""
        out = []
        for rec in self.iter_records():
            if rec.pos < pos:
                continue
            if not include_control and rec.type in CONTROL_TYPES:
                continue
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
        return out

    def read_all(self, include_control: bool = True):
        return self.read_from(FILE_HEADER_SIZE, None, include_control)

    def data_events(self):
        """按顺序返回数据事件（带 1 起逻辑序号 seq）；CRC 损坏时抛错"""
        out = []
        for rec in self.iter_records():
            if rec.type not in DATA_TYPES:
                continue
            seq = rec.payload.get('seq')
            if not isinstance(seq, int) or seq <= 0:
                seq = len(out) + 1
            rec.seq = seq
            out.append(rec)
        return out

    def fetch_rows(self, pos_after: int = 0, limit: int = None):
        """`__BINLOG_FETCH__` 的 6 列行（pos=逻辑序号, lsn=真实字节偏移）"""
        rows = []
        for rec in self.data_events():
            if rec.seq is None or rec.seq <= pos_after:
                continue
            rows.append(rec.to_fetch_row())
            if limit is not None and len(rows) >= limit:
                break
        return rows


def _engine_version() -> str:
    try:
        from .. import __version__
        return str(__version__)
    except Exception:                      # pragma: no cover - 防御
        return 'unknown'


# ==================== 模块级便捷读取 ====================

def open_binlog(data_dir: str, **kwargs) -> AppendOnlyBinlog:
    """打开（必要时创建）某数据目录的 binlog 读写器"""
    return AppendOnlyBinlog(str(data_dir), **kwargs)


class BinlogReader:
    """只读视图（便捷封装；底层复用 AppendOnlyBinlog 的校验逻辑）"""

    def __init__(self, data_dir: str, file_name: str = DEFAULT_FILE):
        self.path = Path(data_dir) / BINLOG_SUBDIR / file_name
        self.file_name = file_name
        self._impl = None

    def _open(self):
        if self._impl is None:
            # 只读场景：若文件不存在 → 显式报错（不静默给空流）
            if not self.path.exists():
                raise BinlogError(f'binlog 文件不存在: {self.path}')
            self._impl = AppendOnlyBinlog(
                str(self.path.parent.parent), file_name=self.file_name)
        return self._impl

    def read_from(self, pos: int = FILE_HEADER_SIZE, limit: int = None,
                  include_control: bool = True):
        return self._open().read_from(pos, limit, include_control)

    def read_all(self, include_control: bool = True):
        return self._open().read_all(include_control)

    def data_events(self):
        return self._open().data_events()

    @property
    def current_pos(self) -> int:
        return self._open().current_pos

    def close(self):
        if self._impl is not None:
            self._impl.close()
            self._impl = None
