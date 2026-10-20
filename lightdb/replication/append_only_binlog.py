"""真 · append-only binlog（决策 D 项：独立于各表 WAL 的持久复制日志）

本文件是**阶段 2 收尾版**：在阶段 1（独立文件、CRC32、组提交、位点只增
不回退）之上补齐了**日志轮转**与**写失败可拒绝提交**所需的异常语义。

目录与文件命名规范
==================

* 目录：``<data_dir>/binlog/``（启动/首次使用时自动创建）；
* 文件：``binlog.NNNNNN``（6 位零填充十进制序号，从 ``binlog.000001``
  开始，**严格递增**）。轮转时新建下一个序号的文件，旧文件**只读保留**
  供复制/PITR 读取，**绝不回写**；
* 当前可写文件 = 序号最大的那个；写端启动时扫描该目录、校验全量记录、
  只对**最新文件**允许"末尾半条记录"的崩溃修复（截断到最后一条完整记录），
  旧文件出现截断/CRC 损坏一律显式报错（只读文件不允许修改）。

磁盘格式
========

每个文件各自带文件头，记录里的 ``pos`` 是**文件内**字节偏移（与 MySQL
的 binlog file position 语义一致）：

文件头（固定 16 字节）::

    magic      8 字节  b'LDBBLOG1'
    version    u32     FORMAT_VERSION
    header_crc u32     CRC32(magic + version)

记录（固定 17 字节记录头 + JSON payload + CRC32）::

    type       u8      记录类型（见 TYPE_*）
    pos        u64     本记录起始的**文件内**字节偏移（含文件头；单调）
    length     u32     payload 长度（字节）
    crc32      u32     CRC32(type 字节 + pos + length + payload)
    payload    length  UTF-8 JSON（对象；含公共字段 pos/txn_id/ts/server_id）

记录类型：``FORMAT_DESCRIPTION``（每个文件首条）、``BEGIN`` / ``COMMIT`` /
``ROLLBACK``（事务边界）、``INSERT`` / ``UPDATE`` / ``DELETE``（行变更，
payload 含表名 + 列清单 + 行镜像）、``DDL``（结构变更 SQL）。

**跨文件全局字节偏移（global lsn）**
------------------------------------

复制位点必须能在轮转后继续单调（跨文件）。定义::

    global_lsn(record) = file_base(file) + record.pos
    file_base(binlog.000001) = 0
    file_base(binlog.N+1)    = file_base(binlog.N) + file_size(binlog.N)

即"把全部 binlog 文件按序号首尾相接"后的字节偏移。``__BINLOG_FETCH__``
的 ``lsn`` 列暴露它（单调、跨轮转连续），``pos`` 列仍是**全局数据事件
逻辑序号**（1 起，跨文件连续），从库按 pos 水位续传、按 lsn 持久化字节
位点，两者在轮转前后都单调。

写入语义
========

* **组提交**：DML 先进内存（按组 id 缓冲），事务提交时一次 ``write`` 把
  ``BEGIN → 行事件… → COMMIT`` 整组写出去（autocommit 语句也是独立一组）；
  `ROLLBACK` 只写 ``BEGIN → ROLLBACK``（行事件从未持久化，如实记"被丢弃
  的条数"，不伪造数据变更）；
* **轮转**：一组完整写完后若当前文件大小 ``>= max_bytes`` 立即切换到下一
  个序号文件（组不会跨文件拆分；单组本身大于 max_bytes 时允许超限一组）。
  默认 ``max_bytes = 32 MB``（``LIGHTDB_BINLOG_MAX_BYTES`` 可配）；
* DDL 执行成功后写独立一组 ``BEGIN → DDL → COMMIT``；
* **写失败可拒绝提交**：写/flush/fsync 失败抛 ``BinlogWriteError``
  （errno=1180 / SQLSTATE HY000）。``Database.commit`` /
  ``commit_statement`` 在 ``binlog_strict``（默认 True）下先组提交 binlog、
  失败则回滚事务并把错误返回客户端（``LIGHTDB_BINLOG_STRICT=0`` 退化为
  告警）；
* ``fsync`` 默认关闭（与 WAL 的 autocommit 持久性档位一致），
  ``LIGHTDB_BINLOG_FSYNC=1`` 可开启（strict 模式下 fsync 失败同样拒绝提交）。

读取接口
========

``AppendOnlyBinlog`` 同时是写端与读端（线程锁保护；文件只追加，多进程
读安全）：

* ``read_file(name, from_pos, limit)``：读指定文件（``Pos`` 为文件内偏移）；
* ``read_from(pos, limit)`` / ``read_all()``：当前文件；
* ``read_global_from(global_lsn, limit)``：按全局字节偏移跨文件顺序读；
* ``data_events()``：全部文件的 行变更/DDL 事件，带全局 seq 与 global lsn；
* ``fetch_rows(pos_after, limit)``：``__BINLOG_FETCH__`` 的 6 列行
  ``(pos=全局逻辑序号, lsn=全局字节偏移, txn_id, event_type, table_name,
  data)``；
* ``current_file`` / ``current_pos``（文件内位点）/ ``current_global_pos``；
* ``file_names()`` / ``file_size(name)`` / ``file_base(name)``。

已知边界（留给后续迭代，代码里 TODO 标注）
==========================================

* **无 GTID**（决策：不实现）——``SHOW MASTER STATUS.Executed_Gtid_Set``
  恒为 NULL，复制按"文件 + 位点"进行；
* **无半同步**（决策：不实现；阶段 2 已删除死代码）；
* 无索引文件：启动时扫描全部 binlog 文件计算 base/seq（当前规模可接受；
  TODO：``binlog.index`` 元数据文件，O(1) 启动）；
* binlog 与 WAL **非 2PC**：strict 模式保证"binlog 写失败 → 不提交"（覆盖
  `Database.commit` / `commit_statement`：显式事务与多值语句）；WAL 提交
  失败而 binlog 已写成功时会出现"多一条 binlog 事件"，由从库主键冲突幂等
  跳过（残余风险，如实记录）。**单行 autocommit** 的持久点在各表内部
  （`Table.insert/update/delete` 合成事务），该路径的 binlog 写失败目前
  只告警（数据已提交）——阶段 3 可统一改为语句级内部事务后同样纳入 strict；
* 直接写存储层（``Table.insert`` 等）的路径不经过执行器，因此不产生
  binlog 事件 —— binlog 记录的是"经执行器执行并提交的语句"；
* 从库重放 DDL 走执行器，DML 走存储层（与 MySQL 行格式复制同构，要求
  两边 schema 一致；不做 DDL 自动修复）。
"""
import json
import os
import re
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
#: 第一个日志文件名（后续为 binlog.000002、binlog.000003 …）
DEFAULT_FILE = 'binlog.000001'
#: 日志文件名模式（严格 6 位以上数字，避免误认备份文件）
FILE_RE = re.compile(r'^binlog\.(\d{6,})$')
#: 默认单文件大小阈值：32 MB（超过即轮转）
MAX_BYTES_DEFAULT = 32 * 1024 * 1024

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


# ==================== 环境开关 / 文件命名 ====================

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


def binlog_strict() -> bool:
    """binlog 写失败是否**拒绝提交**（决策 D 阶段 2：默认 True）

    ``LIGHTDB_BINLOG_STRICT=0`` 退化为"只告警"的旧行为（仅用于排障：
    排障期间允许 binlog 落后于数据，但复制位点会缺事件）。
    """
    return env_flag('LIGHTDB_BINLOG_STRICT', True)


def binlog_max_bytes() -> int:
    """单文件轮转阈值（默认 32MB；``LIGHTDB_BINLOG_MAX_BYTES`` 可配）"""
    raw = os.environ.get('LIGHTDB_BINLOG_MAX_BYTES')
    try:
        val = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        val = 0
    return val if val > 0 else MAX_BYTES_DEFAULT


def file_name_for(index: int) -> str:
    """序号 → binlog 文件名（``1 -> binlog.000001``）"""
    return f'binlog.{int(index):06d}'


def file_index(name: str):
    """binlog 文件名 → 序号；不是 binlog 文件名时返回 None"""
    m = FILE_RE.match(str(name or ''))
    return int(m.group(1)) if m else None


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


class BinlogWriteError(BinlogError):
    """binlog **写**失败（write/flush/fsync/轮转建文件）

    带 MySQL 语义码：``errno=1180``（ER_ERROR_DURING_COMMIT：
    "Got error during COMMIT"）+ ``SQLSTATE=HY000``。执行器在通用异常
    分支里会读取这两个属性，客户端因此拿到显式错误而不是"提交成功"。
    """

    errno = 1180
    sqlstate = 'HY000'

    def __init__(self, message: str, file: str = None, pos: int = None,
                 cause: Exception = None):
        self.file = file
        self.pos = pos
        self.cause = cause
        where = ''
        if file is not None:
            where = f' [{file}@{pos}]'
        detail = f'（原因: {cause}）' if cause is not None else ''
        super().__init__(f'binlog 写失败{where}: {message}{detail}')


# ==================== 记录对象 ====================

class BinlogRecord:
    """一条已解码并校验通过的事件记录"""

    __slots__ = ('type', 'type_name', 'pos', 'end_pos', 'payload',
                 'size', 'seq', 'file_name', 'global_pos')

    def __init__(self, rtype: int, pos: int, payload: dict, size: int,
                 seq: int = None, file_name: str = None,
                 global_pos: int = None):
        self.type = rtype
        self.type_name = TYPE_NAMES.get(rtype, f'UNKNOWN_{rtype}')
        self.pos = pos                 # 文件内字节偏移
        self.end_pos = pos + size      # 文件内结束偏移
        self.payload = payload or {}
        self.size = size
        self.seq = seq
        self.file_name = file_name
        self.global_pos = global_pos   # 跨文件全局字节偏移（file_base + pos）

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
            return (f"table={p.get('table')} "
                    f"row={json.dumps(data.get('row'), ensure_ascii=False, default=str)[:200]}")
        if self.type == TYPE_UPDATE:
            return (f"table={p.get('table')} pk={data.get('pk')!r} "
                    f"new={json.dumps(data.get('new'), ensure_ascii=False, default=str)[:200]}")
        if self.type == TYPE_DELETE:
            return f"table={p.get('table')} pk={data.get('pk')!r}"
        return json.dumps(p, ensure_ascii=False, default=str)[:300]

    def to_fetch_row(self):
        """`__BINLOG_FETCH__` 的一行（6 列兼容形态）

        pos = 数据事件**全局逻辑序号**（1 起，跨文件连续；兼容旧复制接口）
        lsn = **跨文件全局字节偏移**（file_base + 文件内 pos；单调）
        """
        lsn = self.global_pos if self.global_pos is not None else self.pos
        return (self.seq, lsn, self.txn_id, self.type_name,
                self.table, json.dumps(self.data, ensure_ascii=False,
                                       default=str))

    def __repr__(self):
        return (f'<BinlogRecord {self.type_name} '
                f'{self.file_name}@{self.pos} seq={self.seq} '
                f'txn={self.txn_id}>')


# ==================== 工具 ====================

def _new_server_id() -> int:
    """进程内唯一 server_id（**不冒充** MySQL 的 server_uuid/GTID）

    取值：pid 与启动时间混合，32 位非零整数。用于 `SHOW BINLOG EVENTS.
    Server_id` 与 payload.server_id；同一进程内稳定，跨进程不同。
    """
    mixed = (os.getpid() * 2654435761 + int(time.time() * 1000)) & 0x7FFFFFFF
    return mixed or 1


def _engine_version() -> str:
    try:
        from .. import __version__
        return str(__version__)
    except Exception:                      # pragma: no cover - 防御
        return 'unknown'


# ==================== 字节流解码（严格校验）====================

def iter_records_bytes(raw: bytes, file_name: str = DEFAULT_FILE,
                       start: int = FILE_HEADER_SIZE):
    """从文件字节里逐条解码记录（严格校验 pos 连续 + CRC32）

    独立函数而不是实例方法：只读校验/测试可以在**不打开写端**的情况下
    复用它（写端打开时会修复"末尾半条记录"，那样就测不到严格读取路径了）。
    产出的记录 ``pos``/``end_pos`` 是**文件内**偏移；``global_pos`` 由
    调用方（知道该文件 base）补上。
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
        yield BinlogRecord(rtype, pos, payload, end - offset,
                           file_name=file_name)
        offset = end


def _read_header(raw: bytes, file_name: str):
    """校验并返回文件头（magic/version/header_crc）"""
    if len(raw) < FILE_HEADER_SIZE:
        raise BinlogCorrupted(
            f'文件短于文件头（{len(raw)} < {FILE_HEADER_SIZE}）',
            file_name, 0)
    magic, version, header_crc = _HEADER_STRUCT.unpack(raw[:FILE_HEADER_SIZE])
    want = zlib.crc32(magic + struct.pack('<I', version)) & 0xFFFFFFFF
    if magic != MAGIC:
        raise BinlogCorrupted(
            f'文件头 magic 不匹配（{magic!r} != {MAGIC!r}）',
            file_name, 0)
    if header_crc != want:
        raise BinlogCorrupted('文件头 header_crc 校验失败', file_name, 8)
    if version != FORMAT_VERSION:
        raise BinlogCorrupted(
            f'不支持的格式版本 {version}（本实现支持 {FORMAT_VERSION}）',
            file_name, 8)
    return magic, version


def _header_bytes() -> bytes:
    return _HEADER_STRUCT.pack(
        MAGIC, FORMAT_VERSION,
        zlib.crc32(MAGIC + struct.pack('<I', FORMAT_VERSION)) & 0xFFFFFFFF)


# ==================== 写 + 读 ====================

class AppendOnlyBinlog:
    """append-only binlog 的写端 + 读端（线程安全，支持轮转）"""

    def __init__(self, data_dir: str, file_name: str = None,
                 fsync: bool = None, max_bytes: int = None):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / BINLOG_SUBDIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fsync = binlog_fsync() if fsync is None else bool(fsync)
        self.max_bytes = (binlog_max_bytes() if max_bytes is None
                          else int(max_bytes))
        self.server_id = _new_server_id()
        #: 显式请求读取的文件（BinlogReader 用；默认 None = 当前可写文件）
        self.requested_file = file_name

        self._lock = threading.RLock()
        #: 组 id -> [{'type': int, 'table': str|None, 'data': dict}]
        self._groups = {}
        #: 组 id -> txn_id
        self._group_txn = {}
        self._auto_group_seq = 0
        self._data_seq = 0            # 已写数据事件全局序号（fetch 的 pos 列）
        #: 文件元数据（按序号升序）：[{'name','path','size','base'}]
        self._files = []
        self._index = 0               # 当前可写文件序号
        self._path = None
        self._base_offset = 0         # 当前文件的全局 base（前面所有文件大小之和）
        self._local_pos = 0           # 当前文件内下一个可写偏移
        self._fh = None
        self._open()

    # ==================== 打开 / 恢复 ====================
    def _discover(self):
        """发现目录下全部 binlog 文件（按序号升序）"""
        found = []
        try:
            for p in self.dir.iterdir():
                if not p.is_file():
                    continue
                idx = file_index(p.name)
                if idx is not None:
                    found.append((idx, p))
        except FileNotFoundError:
            pass
        found.sort(key=lambda x: x[0])
        return found

    def _open(self):
        found = self._discover()
        if not found:
            # 全新目录：创建 binlog.000001（写文件头 + FORMAT_DESCRIPTION）
            size = self._create_file(self.dir / DEFAULT_FILE)
            found = [(1, self.dir / DEFAULT_FILE)]
            self._files = [{'name': DEFAULT_FILE,
                            'path': found[0][1], 'size': size, 'base': 0}]
            self._index = 1
            self._path = found[0][1]
            self._base_offset = 0
            self._local_pos = size
            self._data_seq = 0
            self._fh = open(self._path, 'ab')
            return

        # 已有文件：逐个校验（旧文件只读、不允许修复；仅最新文件可修复末尾）
        self._files = []
        base = 0
        total_data = 0
        last_seq = 0
        for i, (idx, path) in enumerate(found):
            is_last = (i == len(found) - 1)
            valid_end, count, last = self._scan_file(path, repair_tail=is_last)
            size = os.path.getsize(path)
            if valid_end != size:            # pragma: no cover - 防御
                size = valid_end
            self._files.append({'name': path.name, 'path': path,
                                'size': size, 'base': base})
            base += size
            total_data += count
            if isinstance(last, int) and last > last_seq:
                last_seq = last
        self._index = found[-1][0]
        self._path = found[-1][1]
        self._base_offset = base - self._files[-1]['size']
        self._local_pos = self._files[-1]['size']
        self._data_seq = max(total_data, last_seq)
        self._fh = open(self._path, 'ab')

    def _create_file(self, path: Path) -> int:
        """创建一个新 binlog 文件（文件头 + FORMAT_DESCRIPTION），返回大小"""
        if path.exists() and path.stat().st_size > 0:
            raise BinlogWriteError(
                f'轮转目标文件已存在且非空: {path.name}', path.name)
        try:
            with open(path, 'wb') as f:
                f.write(_header_bytes())
                f.flush()
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
                f.write(blob)
                f.flush()
                if self.fsync:
                    os.fsync(f.fileno())
                return FILE_HEADER_SIZE + len(blob)
        except OSError as e:
            raise BinlogWriteError(f'创建 binlog 文件失败: {path.name}',
                                   path.name, 0, e) from e

    def _scan_file(self, path: Path, repair_tail: bool):
        """校验一个文件；返回 (valid_end, data_count, last_seq)

        * ``repair_tail=True``（仅最新文件）：允许截断掉"末尾半条记录"并告警；
        * 旧文件只读：截断/CRC 损坏一律显式抛错。
        """
        name = path.name
        with open(path, 'rb') as f:
            raw = f.read()
        _read_header(raw, name)
        valid_end = FILE_HEADER_SIZE
        count = 0
        last_seq = 0
        try:
            for rec in iter_records_bytes(raw, name):
                valid_end = rec.end_pos
                if rec.type in DATA_TYPES:
                    count += 1
                    seq = rec.payload.get('seq')
                    if isinstance(seq, int) and seq > last_seq:
                        last_seq = seq
        except BinlogTruncated as e:
            if not repair_tail:
                raise BinlogCorrupted(
                    f'只读的旧 binlog 文件被截断（不允许修复）: {e}',
                    name, valid_end)
            logger.warn(
                f'binlog {name} 末尾存在未写完的记录'
                f'（offset={valid_end}），已截断修复：{e}')
            with open(path, 'r+b') as f:
                f.truncate(valid_end)
                f.flush()
                os.fsync(f.fileno())
        except BinlogCorrupted:
            raise
        return valid_end, count, last_seq

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
            raise BinlogWriteError(
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

        返回该事件的**文件内**字节偏移。DML 请走 `record()` +
        `commit_group()`，以保证 `BEGIN → 行事件 → COMMIT` 的组边界。
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

        返回组结束后的**文件内**字节位点（= 下一条记录的起始偏移）。
        写失败抛 ``BinlogWriteError``（strict 提交路径据此回滚事务）。
        """
        with self._lock:
            rows = self._groups.pop(gid, [])
            txn_id = self._group_txn.pop(gid, 0)
            if not rows:
                return self._local_pos
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
                return self._local_pos
            self._maybe_rotate_locked()
            begin = self._encode_record(
                TYPE_BEGIN, self._local_pos,
                {'type': 'BEGIN', 'txn_id': txn_id, 'rolled_back': True})
            rb = self._encode_record(
                TYPE_ROLLBACK, self._local_pos + len(begin),
                {'type': 'ROLLBACK', 'txn_id': txn_id,
                 'discarded': len(rows)})
            self._write_blob(begin + rb)
            return self._local_pos

    def write_ddl(self, sql: str, table: str = None,
                  txn_id: int = 0) -> int:
        """DDL 成功后写一组 `BEGIN → DDL → COMMIT`，返回结束文件内位点"""
        with self._lock:
            return self._append_group(
                [{'type': TYPE_DDL, 'table': table,
                  'data': {'sql': str(sql or '')}, 'txn_id': txn_id}],
                txn_id)

    def _append_group(self, rows, txn_id: int) -> int:
        # 写前轮转：**先**确保当前文件还有余量，再写整组。这样"轮转建新文件
        # 失败"发生在写任何字节之前 → strict 提交可以干净地拒绝（不会留下
        # 半份 binlog 组）；单组本身超过阈值时允许该文件超限一组（与 MySQL
        # 单事务可跨文件/超限的行为同向，但本实现不做组内拆分）。
        self._maybe_rotate_locked()
        cur = self._local_pos
        begin = self._encode_record(
            TYPE_BEGIN, cur, {'type': 'BEGIN', 'txn_id': txn_id})
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
            {'type': 'COMMIT', 'txn_id': txn_id, 'events': len(rows)})
        blobs.append(commit)
        self._write_blob(b''.join(blobs))
        return self._local_pos

    def _write_blob(self, blob: bytes):
        """追加一组字节；失败抛 ``BinlogWriteError``；写满阈值后轮转"""
        if self._fh is None:
            raise BinlogWriteError('binlog 已关闭，无法写入',
                                   self._path.name if self._path else None)
        try:
            self._fh.write(blob)
            self._fh.flush()
            if self.fsync:
                self._sync()
        except BinlogWriteError:
            raise
        except Exception as e:
            # 写失败：回退内存位点（整组未落地；已写入的半截由启动时
            # "最新文件末尾截断修复"处理），并把异常升级为可拒绝提交的错误
            try:
                self._local_pos = os.path.getsize(self._path)
            except OSError:
                pass
            raise BinlogWriteError(
                '组提交写盘失败', self._path.name, self._local_pos,
                e) from e
        self._local_pos += len(blob)

    def _maybe_rotate_locked(self):
        """写前轮转检查（调用方持有锁）：达到阈值就切下一个序号文件

        * 在**写整组之前**调用：轮转失败（建不了新文件）时还没有写任何
          字节，strict 提交可以干净拒绝；
        * 旧文件关闭后只读保留；新文件自带文件头 + FORMAT_DESCRIPTION，
          其全局 base = 前面所有文件大小之和（见模块 docstring）。
        """
        if self._local_pos >= self.max_bytes:
            self._rotate_locked()

    def _rotate_locked(self):
        """轮转：关闭当前文件 → 打开下一个序号的新文件（旧文件只读保留）"""
        old = self._path
        old_size = self._local_pos
        new_index = self._index + 1
        new_path = self.dir / file_name_for(new_index)
        if new_path.exists():                 # pragma: no cover - 防御
            raise BinlogWriteError(
                f'轮转目标文件已存在: {new_path.name}', new_path.name)
        try:
            self._fh.close()
        except Exception as e:                 # pragma: no cover - 防御
            raise BinlogWriteError(
                f'关闭 binlog 文件失败: {old.name}', old.name, old_size,
                e) from e
        try:
            size = self._create_file(new_path)
        except BinlogWriteError:
            # 新文件建不出来：保留旧文件句柄继续写（不丢事件），明确报错
            self._fh = open(old, 'ab')
            raise
        self._files.append({'name': new_path.name, 'path': new_path,
                            'size': size, 'base': self._base_offset + old_size})
        self._base_offset += old_size
        self._index = new_index
        self._path = new_path
        self._local_pos = size
        self._fh = open(new_path, 'ab')
        logger.info(
            f"binlog 已轮转: {old.name}({old_size} B) -> {new_path.name}")

    def _sync(self):
        """fsync 当前文件；失败抛 BinlogWriteError（strict 模式拒绝提交）"""
        try:
            os.fsync(self._fh.fileno())
        except OSError as e:
            raise BinlogWriteError('fsync 失败',
                                   self._path.name if self._path else None,
                                   self._local_pos, e) from e

    def flush(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    if self.fsync:
                        self._sync()
                except BinlogWriteError:
                    raise
                except Exception as e:         # pragma: no cover - 防御
                    raise BinlogWriteError(
                        'flush 失败', self._path.name, self._local_pos,
                        e) from e

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
        return self._path.name if self._path is not None else DEFAULT_FILE

    @property
    def current_pos(self) -> int:
        """当前文件内末尾的**真实字节偏移**（MySQL SHOW MASTER STATUS 语义）"""
        with self._lock:
            try:
                return os.path.getsize(self._path)
            except (OSError, TypeError):       # pragma: no cover - 防御
                return self._local_pos

    @property
    def current_global_pos(self) -> int:
        """跨文件全局字节偏移（= 本文件 base + 文件内位点）"""
        with self._lock:
            return self._base_offset + self.current_pos

    @property
    def data_event_count(self) -> int:
        """已写入的数据事件（INSERT/UPDATE/DELETE/DDL）全局条数"""
        with self._lock:
            return self._data_seq

    def file_names(self):
        """全部 binlog 文件名（按序号升序）"""
        with self._lock:
            return [f['name'] for f in self._files]

    def _file_meta(self, name: str = None):
        with self._lock:
            if name is None:
                name = self.current_file
            for f in self._files:
                if f['name'] == name:
                    return dict(f)
        raise BinlogError(f'binlog 文件不存在: {name}')

    def file_size(self, name: str = None) -> int:
        return self._file_meta(name)['size']

    def file_base(self, name: str = None) -> int:
        """该文件的跨文件全局 base 偏移"""
        return self._file_meta(name)['base']

    # ==================== 读 ====================
    def _read_file_bytes(self, name: str):
        meta = self._file_meta(name)
        with open(meta['path'], 'rb') as f:
            return f.read(), meta

    def read_file(self, name: str = None, from_pos: int = FILE_HEADER_SIZE,
                  limit: int = None, include_control: bool = True):
        """读**指定文件**里的记录：文件内偏移 >= from_pos

        ``Pos``/``End_log_pos`` 是文件内偏移；记录的 ``global_pos`` 已补上
        该文件的全局 base（跨轮转位点用）。文件不存在/损坏 → 显式报错。
        """
        raw, meta = self._read_file_bytes(name or self.current_file)
        _read_header(raw, meta['name'])
        out = []
        for rec in iter_records_bytes(raw, meta['name'], max(FILE_HEADER_SIZE,
                                                             from_pos)):
            rec.global_pos = meta['base'] + rec.pos
            if rec.pos < from_pos:
                continue
            if not include_control and rec.type in CONTROL_TYPES:
                continue
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
        return out

    def iter_records(self, start_pos: int = FILE_HEADER_SIZE):
        """按顺序读**当前文件**记录（兼容阶段 1 接口）"""
        yield from self.read_file(self.current_file, start_pos)

    def read_from(self, pos: int = FILE_HEADER_SIZE, limit: int = None,
                  include_control: bool = True):
        """读当前文件内偏移 >= pos 的记录（兼容阶段 1 接口）"""
        return self.read_file(self.current_file, pos, limit, include_control)

    def read_all(self, include_control: bool = True):
        """读当前文件全部记录（兼容阶段 1 接口）"""
        return self.read_file(self.current_file, FILE_HEADER_SIZE, None,
                              include_control)

    def read_global_from(self, global_pos: int = 0, limit: int = None,
                         include_control: bool = True):
        """按**跨文件全局字节偏移**顺序读记录（轮转后仍连续）"""
        out = []
        with self._lock:
            metas = [dict(f) for f in self._files]
        for meta in metas:
            start = meta['base']
            end = start + meta['size']
            if end <= global_pos:
                continue
            if global_pos <= start:
                local_start = FILE_HEADER_SIZE
            else:
                local_start = max(FILE_HEADER_SIZE, global_pos - start)
            for rec in self.read_file(meta['name'], local_start,
                                      include_control=include_control):
                if rec.global_pos < global_pos:
                    continue
                out.append(rec)
                if limit is not None and len(out) >= limit:
                    return out
        return out

    def iter_all_records(self):
        """按顺序读**全部文件**的记录（跨文件）"""
        with self._lock:
            names = [f['name'] for f in self._files]
        for name in names:
            yield from self.read_file(name)

    def data_events(self):
        """按顺序返回全部文件的数据事件（全局 seq，跨文件 lsn）"""
        out = []
        for rec in self.iter_all_records():
            if rec.type not in DATA_TYPES:
                continue
            seq = rec.payload.get('seq')
            if not isinstance(seq, int) or seq <= 0:
                seq = len(out) + 1
            rec.seq = seq
            out.append(rec)
        return out

    def fetch_rows(self, pos_after: int = 0, limit: int = None):
        """`__BINLOG_FETCH__` 的 6 列行（pos=全局逻辑序号, lsn=全局字节偏移）"""
        rows = []
        for rec in self.data_events():
            if rec.seq is None or rec.seq <= pos_after:
                continue
            rows.append(rec.to_fetch_row())
            if limit is not None and len(rows) >= limit:
                break
        return rows


# ==================== 模块级便捷读取 ====================

def open_binlog(data_dir: str, **kwargs) -> AppendOnlyBinlog:
    """打开（必要时创建）某数据目录的 binlog 读写器"""
    return AppendOnlyBinlog(str(data_dir), **kwargs)


class BinlogReader:
    """只读视图（便捷封装；底层复用 AppendOnlyBinlog 的校验逻辑）

    默认指向**最新文件**；``read_file(name)`` 可读任意历史文件（轮转后
    旧文件只读保留）。文件不存在 → 显式报错（不静默给空流）。
    """

    def __init__(self, data_dir: str, file_name: str = None):
        self.data_dir = str(data_dir)
        self.file_name = file_name
        self._impl = None

    def _open(self):
        if self._impl is None:
            if self.file_name is not None:
                path = Path(self.data_dir) / BINLOG_SUBDIR / self.file_name
                if not path.exists():
                    raise BinlogError(f'binlog 文件不存在: {path}')
            self._impl = AppendOnlyBinlog(self.data_dir)
        return self._impl

    def read_file(self, name: str = None, from_pos: int = FILE_HEADER_SIZE,
                  limit: int = None, include_control: bool = True):
        return self._open().read_file(name, from_pos, limit, include_control)

    def read_from(self, pos: int = FILE_HEADER_SIZE, limit: int = None,
                  include_control: bool = True):
        return self._open().read_from(pos, limit, include_control)

    def read_all(self, include_control: bool = True):
        return self._open().read_all(include_control)

    def read_global_from(self, global_pos: int = 0, limit: int = None,
                         include_control: bool = True):
        return self._open().read_global_from(global_pos, limit,
                                             include_control)

    def data_events(self):
        return self._open().data_events()

    def file_names(self):
        return self._open().file_names()

    @property
    def current_file(self) -> str:
        return self._open().current_file

    @property
    def current_pos(self) -> int:
        return self._open().current_pos

    def close(self):
        if self._impl is not None:
            self._impl.close()
            self._impl = None
