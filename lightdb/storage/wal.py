"""
预写日志 (WAL) - 命名多段轮转版

段文件布局（每张表一组）：
    <table>.wal                 ← **活动段**（当前追加写；兼容旧路径/测试）
    <table>-wal-000001.log      ← 已轮转的归档段（按序号递增）

* 活动段超过 `segment_bytes`（默认 256 KB，可用
  `LIGHTDB_WAL_SEGMENT_BYTES` 或 `Table.wal_segment_bytes` 配置）后，
  自动把活动段改名为 `-wal-NNNNNN.log` 归档，并开新活动段继续写；
  LSN 跨段单调。
* checkpoint（`Table._retire_wal` → `WAL.truncate`）先把活动段轮转为归档，
  再只保留最近 K 段（`LIGHTDB_WAL_KEEP_SEGMENTS` / `Table.wal_keep_segments`，
  默认 0 = 兼容旧的"checkpoint 后 WAL 清空"语义），更旧的段安全删除。
  之后活动段为空，归档总量 <= K * (segment_bytes + 单条记录上限)。
* 启动时按归档段序号 + 活动段的顺序读取全部记录再做 redo/undo，
  因此**跨段崩溃恢复**成立；恢复成功后才轮转/退休 WAL（失败保留原样重试）。
* `size_bytes()`/`record_count()` 返回**全部段**的真实合计（活动段 + 归档），
  这样 Table 的 WAL 阈值/有界性判定覆盖整个 WAL，而不会在轮转后重置为 0；
  需要单看活动段时用 `active_size_bytes()`。

写路径持久性语义与旧版一致：
- `log()` 写入 Python 缓冲；`flush_to_os()` 交给 OS（进程硬杀不丢）；
- `commit()`/`abort()` fsync（掉电级持久化点）；autocommit 语句用
  `commit_async()` / `commit_autocommit()`（flush 到 OS，不 fsync）；
- 恢复是"日志全量重放 + 幂等覆盖"，loser 用 UPDATE/DELETE 的旧行镜像 undo。
"""
import glob
import json
import os
import re
import struct
import threading

# 合成 autocommit 事务号下界（必须与 storage/mvcc.py 的 ReadView 判定一致）。
try:
    from .mvcc import SYNTHETIC_TXN_FLOOR
except Exception:  # pragma: no cover - 防御性回退
    SYNTHETIC_TXN_FLOOR = 0xF0000000

#: 默认段大小（字节）：可用环境变量/Table 类属性覆盖
WAL_SEGMENT_BYTES_DEFAULT = 256 * 1024
WAL_SEGMENT_BYTES_ENV = 'LIGHTDB_WAL_SEGMENT_BYTES'
#: checkpoint 后默认保留的最近归档段数（0 = 兼容旧语义：全部退休）
WAL_KEEP_SEGMENTS_DEFAULT = 0
WAL_KEEP_SEGMENTS_ENV = 'LIGHTDB_WAL_KEEP_SEGMENTS'


def is_implicit_commit_tid(txn_id: int) -> bool:
    """
    该事务号是否代表"没有 COMMIT 记录但已经提交"的 autocommit 写操作。
    - 0: executor 在没有显式事务时传的 txn_id
    - >= SYNTHETIC_TXN_FLOOR: Table 给 autocommit 语句分配的合成事务号
    """
    return txn_id == 0 or txn_id >= SYNTHETIC_TXN_FLOOR


ROW_HEADER = 21  # xmin(4)+xmax(4)+flags(1)+len(4)+prev_page(4)+prev_slot(4)
ROW_HEADER_LEGACY13 = 13


def fix_loser_undo_image(data: dict) -> bool:
    """
    兼容性补丁（自动失效）：修正 loser 撤销用的旧行镜像。

    Table 写 WAL 时把 "旧行镜像" 存成了 **带删除标记的版本**
    （old_b = encode(old, xmin, xmax=txn_id)）。恢复阶段把这份字节
    原样写回页，行头就永久带着 xmax=txn_id —— 于是**事务前就已经
    提交、本来必须保留的旧行在恢复后彻底读不出来**。

    正确的前像应当是 xmax = 0（事务前该行是活的）。这里按 old 值 +
    原 xmin 重建镜像；如果 Table 侧哪天改对了（xmax 本来就是 0），
    重建结果字节完全相同，本补丁自动变成空操作。

    ★ 行头已扩展为 21 字节（多出 prev_page/prev_slot 版本链指针）：
    重建时必须把原镜像里的 prev 指针原样搬过来，否则版本链会断，
    重启后老快照回溯不到更早的版本。

    返回是否真的改写了镜像。
    """
    if not isinstance(data, dict):
        return False
    old = data.get('old')
    old_b = data.get('old_b')
    if not old or not old_b:
        return False
    try:
        rb = bytes.fromhex(old_b)
        xmin = struct.unpack_from('<I', rb, 0)[0]
        xmax = struct.unpack_from('<I', rb, 4)[0]
    except Exception:
        return False
    if xmax == 0:
        return False                     # 已经是正确的前像
    try:
        jd = json.dumps(old, ensure_ascii=False, default=str).encode('utf-8')
    except Exception:
        return False
    prev = b'\x00' * 8
    try:
        dlen = struct.unpack_from('<I', rb, 9)[0]
    except Exception:
        dlen = -1
    if len(rb) >= ROW_HEADER and len(rb) == ROW_HEADER + dlen:
        prev = rb[13:21]          # 新布局（21 字节行头）才有 prev 指针
    data['old_b'] = (
        struct.pack('<IIBI', xmin, 0, 0, len(jd)) + prev + jd).hex()
    data['old_xmax'] = 0
    return True


class WALRecord:
    __slots__ = ('lsn', 'txn_id', 'type', 'data')

    def __init__(self, lsn, txn_id, rtype, data):
        self.lsn = lsn
        self.txn_id = txn_id
        self.type = rtype
        self.data = data

    def to_json(self):
        return json.dumps({
            'lsn': self.lsn,
            'txn_id': self.txn_id,
            'type': self.type,
            'data': self.data,
        }, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, line):
        o = json.loads(line)
        return cls(o['lsn'], o['txn_id'], o['type'], o['data'])


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


class WAL:

    def __init__(self, path: str, segment_bytes: int = None,
                 keep_segments: int = None):
        self.path = str(path)
        self.lock = threading.RLock()
        self.lsn = 0
        self._base_lsn = 0

        # checkpoint 状态
        self._checkpoint_lsn = 0

        # 段配置：显式参数 > 环境变量 > 默认值
        self.segment_bytes = int(
            segment_bytes if segment_bytes is not None
            else _env_int(WAL_SEGMENT_BYTES_ENV, WAL_SEGMENT_BYTES_DEFAULT))
        self.keep_segments = int(
            keep_segments if keep_segments is not None
            else _env_int(WAL_KEEP_SEGMENTS_ENV, WAL_KEEP_SEGMENTS_DEFAULT))
        if self.segment_bytes <= 0:
            self.segment_bytes = 0       # 0 = 不轮转
        if self.keep_segments < 0:
            self.keep_segments = 0

        # 归档段（按序号升序）：[(seq, path)]
        self._segments = []
        base = self.path[:-4] if self.path.lower().endswith('.wal') \
            else self.path
        self._prefix = base
        self._seq = 0

        # 体积/条数计量（**全部段合计**；见文件头说明）
        self._size = 0
        self._records = 0
        self._active_size = 0

        self._load_existing_segments()
        self.f = open(self.path, 'a', encoding='utf-8', newline='')

    # ==================== 段管理 ====================

    def _archive_pattern(self) -> str:
        return f"{self._prefix}-wal-*.log"

    def _archive_path(self, seq: int) -> str:
        return f"{self._prefix}-wal-{seq:06d}.log"

    def _discover_segments(self):
        """扫描磁盘上的归档段，返回 [(seq, path)]（按 seq 升序）"""
        found = []
        for p in glob.glob(self._archive_pattern()):
            m = re.search(r'-wal-(\d+)\.log$', p)
            if not m:
                continue
            try:
                found.append((int(m.group(1)), p))
            except (TypeError, ValueError):
                continue
        found.sort(key=lambda x: x[0])
        # 同一序号出现多个（异常手工拷贝）：保留序号最大的，忽略重复
        dedup = {}
        for seq, p in found:
            dedup[seq] = p
        return sorted(dedup.items())

    @staticmethod
    def _read_file_records(path: str):
        out = []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(WALRecord.from_json(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            return out
        return out

    def _load_existing_segments(self):
        """启动扫描：归档段（序号序）+ 活动段，LSN 取全部最大值

        恢复侧稍后调用 `read_all()`/`analyze()` 时再完整读一遍内容；
        这里只统计段大小/条数以保持检查点阈值语义（含跨段合计）。
        """
        self._segments = self._discover_segments()
        if self._segments:
            self._seq = max(s for s, _p in self._segments)
        self._size = 0
        self._records = 0
        for _seq, p in self._segments:
            try:
                self._size += os.path.getsize(p)
            except OSError:
                pass
        for p in [p for _s, p in self._segments] + [self.path]:
            for rec in self._read_file_records(p):
                self._records += 1
                if rec.lsn > self.lsn:
                    self.lsn = rec.lsn
        self._active_size = self._file_size(self.path)
        self._base_lsn = self.lsn

    @staticmethod
    def _file_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _rotate_locked(self):
        """把当前活动段改名为归档段，并开新的空活动段（调用方持锁）"""
        active_size = self._file_size(self.path)
        if active_size <= 0:
            # 空活动段：无需归档
            self._active_size = 0
            return 0
        try:
            self.f.flush()
            self.f.close()
        except Exception:
            pass
        self._seq += 1
        dst = self._archive_path(self._seq)
        try:
            os.replace(self.path, dst)
        except OSError:
            # 改不了名（被占用等）：保持原文件继续写，避免丢日志
            self.f = open(self.path, 'a', encoding='utf-8', newline='')
            self._active_size = active_size
            return 0
        self._segments.append((self._seq, dst))
        self.f = open(self.path, 'w', encoding='utf-8', newline='')
        self._size += 0                    # 总量不变（改名不改变字节数）
        self._active_size = 0
        return self._seq

    def _prune_segments_locked(self):
        """只保留最近 `keep_segments` 个归档段，删除更旧的（调用方持锁）"""
        if self.keep_segments < 0:
            self.keep_segments = 0
        keep = int(self.keep_segments)
        if len(self._segments) <= keep:
            return 0
        drop = self._segments[:len(self._segments) - keep] \
            if keep > 0 else list(self._segments)
        removed = 0
        for seq, p in drop:
            try:
                sz = self._file_size(p)
                os.unlink(p)
                self._size = max(0, self._size - sz)
                removed += 1
            except OSError:
                pass
        self._segments = self._segments[len(drop):]
        return removed

    def _scan_all_records(self):
        """按段序读取全部记录（归档段升序 + 活动段）"""
        records = []
        for _seq, p in list(self._segments):
            records.extend(self._read_file_records(p))
        records.extend(self._read_file_records(self.path))
        return records

    # ==================== 写 ====================

    def log(self, txn_id: int, rtype: str, data: dict,
            sync: bool = False) -> int:
        """
        追加一条 WAL 记录（写入 Python 缓冲，不做 syscall）。

        活动段超过配置大小后自动轮转（改名归档 + 新活动段），LSN 继续单调。
        持久化点由调用方把控：
        - `commit()` / `abort()` → fsync（显式事务的持久化点）
        - `commit_async()`       → flush 到 OS（autocommit 语句）
        - `flush_to_os()`        → 交调用方在"改页之前"显式调用
        """
        with self.lock:
            if (self.segment_bytes and self._active_size
                    and self._active_size >= self.segment_bytes):
                self._rotate_locked()
            self.lsn += 1
            rec = WALRecord(self.lsn, txn_id, rtype, data)
            line = rec.to_json()
            self.f.write(line + '\n')
            n = len(line) + 1
            self._active_size += n
            self._size += n
            self._records += 1
            if sync:
                self._flush_locked()
            return self.lsn

    def commit(self, txn_id: int):
        """显式 COMMIT：fsync，掉电也不丢（真正的持久化点）"""
        self.log(txn_id, 'COMMIT', {}, sync=True)

    def commit_async(self, txn_id: int):
        """
        autocommit 语句的隐式提交记录：先把数据记录交给 OS，再追加
        COMMIT，全程不 fsync。
        """
        with self.lock:
            try:
                self.f.flush()
            except Exception:
                pass
        self.log(txn_id, 'COMMIT', {}, sync=False)

    def abort(self, txn_id: int):
        self.log(txn_id, 'ABORT', {}, sync=True)

    def commit_autocommit(self, txn_id: int):
        """语句级 autocommit 提交：数据 + COMMIT 都 flush 到 OS（不 fsync）"""
        with self.lock:
            try:
                self.f.flush()
            except Exception:
                pass
        self.log(txn_id, 'COMMIT', {}, sync=False)
        with self.lock:
            try:
                self.f.flush()
            except Exception:
                pass

    def flush_to_os(self):
        """
        log-before-data 用：把缓冲交给 OS（不做 fsync）。
        - 进程被硬杀（含 kill -9 / 丢弃对象）：OS 缓冲仍在，WAL 可读回
        - 掉电：可能丢，只有 COMMIT 的 fsync 才是真正的持久化点
        """
        with self.lock:
            try:
                self.f.flush()
            except Exception:
                pass

    def _flush_locked(self):
        try:
            self.f.flush()
            os.fsync(self.f.fileno())
        except Exception:
            pass

    def flush(self):
        with self.lock:
            self._flush_locked()

    # ==================== 读 ====================

    def read_all(self):
        """读取全部记录：归档段（序号序）+ 活动段"""
        with self.lock:
            return self._scan_all_records()

    def read_from_lsn(self, lsn: int, limit: int = None):
        """读取 lsn >= 指定值的记录（增量读，用于 PITR；跨段同样适用）"""
        result = []
        for r in self.read_all():
            if r.lsn >= lsn:
                result.append(r)
                if limit and len(result) >= limit:
                    break
        return result

    def find_committed_transactions(self):
        """
        返回 [(txn_id, [ops]), ...]；跨段合并后按 LSN 分类。

        `txn_id == 0` 与合成 autocommit 事务号按"隐式提交"处理。
        """
        records = self.read_all()
        by_txn = {}
        for r in records:
            by_txn.setdefault(r.txn_id, []).append(r)

        committed = []
        for tid, recs in by_txn.items():
            ops = [r for r in recs
                   if r.type in ('INSERT', 'UPDATE', 'DELETE')]
            if not ops:
                continue
            if is_implicit_commit_tid(tid):
                committed.append((tid, ops))
                continue

            commit_idx = None
            abort_idx = None
            for i, r in enumerate(recs):
                if r.type == 'COMMIT':
                    commit_idx = i
                    break
                if r.type == 'ABORT':
                    abort_idx = i
                    break

            if commit_idx is None:
                continue
            if abort_idx is not None and abort_idx < commit_idx:
                continue

            committed.append(
                (tid, [r for r in recs[:commit_idx]
                       if r.type in ('INSERT', 'UPDATE', 'DELETE')]))

        return committed

    def analyze(self):
        """
        崩溃恢复用的一次性分析，返回 (committed_ops, loser_ops)。
        跨全部段收集记录后按事务分类，再按 LSN 排序。
        """
        records = self.read_all()
        by_txn = {}
        for r in records:
            by_txn.setdefault(r.txn_id, []).append(r)

        committed_ops = []
        loser_ops = []
        for tid, recs in by_txn.items():
            ops = [r for r in recs
                   if r.type in ('INSERT', 'UPDATE', 'DELETE')]
            if not ops:
                continue
            if is_implicit_commit_tid(tid):
                committed_ops.extend(
                    (r.lsn, tid, r.type, r.data) for r in ops)
                continue

            commit_idx = None
            abort_idx = None
            for i, r in enumerate(recs):
                if r.type == 'COMMIT' and commit_idx is None:
                    commit_idx = i
                if r.type == 'ABORT' and abort_idx is None:
                    abort_idx = i

            if commit_idx is not None and (
                    abort_idx is None or commit_idx < abort_idx):
                committed_ops.extend(
                    (r.lsn, tid, r.type, r.data)
                    for r in recs[:commit_idx]
                    if r.type in ('INSERT', 'UPDATE', 'DELETE'))
            else:
                for r in ops:
                    if r.type in ('UPDATE', 'DELETE'):
                        fix_loser_undo_image(r.data)
                    loser_ops.append((r.lsn, tid, r.type, r.data))

        committed_ops.sort(key=lambda x: x[0])
        loser_ops.sort(key=lambda x: x[0])
        return committed_ops, loser_ops

    # ==================== checkpoint / compact ====================

    def checkpoint(self) -> int:
        """记录当前 LSN 作为安全点，返回该 LSN"""
        with self.lock:
            self._checkpoint_lsn = self.lsn
            return self._checkpoint_lsn

    def compact_before(self, lsn: int) -> int:
        """删除 lsn 之前的记录（跨段合并后重写活动段，归档段删除）"""
        with self.lock:
            records = self._scan_all_records()
            kept = [r for r in records if r.lsn >= lsn]
            removed = len(records) - len(kept)
            try:
                self.f.flush()
                self.f.close()
            except Exception:
                pass
            for _seq, p in list(self._segments):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self._segments = []
            with open(self.path, 'w', encoding='utf-8', newline='') as f:
                for r in kept:
                    f.write(r.to_json() + '\n')
                f.flush()
            self.f = open(self.path, 'a', encoding='utf-8', newline='')
            self._active_size = self._file_size(self.path)
            self._size = self._active_size
            self._records = len(kept)
            if kept:
                self._base_lsn = kept[0].lsn - 1
            return removed

    # ==================== 维护 ====================

    def size_bytes(self) -> int:
        """**全部段**的字节数合计（活动段 + 归档段；用于 WAL 有界性判定）"""
        with self.lock:
            return self._size

    def record_count(self) -> int:
        """**全部段**的记录条数合计"""
        with self.lock:
            return self._records

    def active_size_bytes(self) -> int:
        """当前活动段的字节数（兼容旧观测；不含归档段）"""
        with self.lock:
            return self._active_size

    def segment_files(self):
        """当前 WAL 段文件列表（归档段升序 + 活动段）"""
        with self.lock:
            return [p for _s, p in self._segments] + [self.path]

    def segment_count(self) -> int:
        with self.lock:
            return len(self._segments) + (1 if self._file_size(self.path)
                                          else 0)

    def needs_checkpoint(self, max_bytes: int = 0,
                         max_records: int = 0) -> bool:
        """是否达到 checkpoint 阈值（0 表示该维度不限制）"""
        with self.lock:
            if max_bytes > 0 and self._size >= max_bytes:
                return True
            if max_records > 0 and self._records >= max_records:
                return True
            return False

    def truncate(self, sync: bool = True):
        """
        退休 WAL（checkpoint 的第二步）：活动段轮转为归档 → 只保留最近 K 段。

        调用前提（由 Table.checkpoint 保证）：WAL 里所有记录对应的数据页
        已经 fsync 落盘、且不存在未提交事务。LSN 保持单调。

        `keep_segments`（K）配置见文件头；K=0 时行为与旧版"截断单文件"完全
        一致（checkpoint 后 WAL 为空），但归档删除更旧段前先改名，保证
        删除动作本身是"整段删除"而不是截断到一半。
        """
        with self.lock:
            self._base_lsn = self.lsn
            try:
                self.f.flush()
                self.f.close()
            except Exception:
                pass
            # 1) 先把当前活动段落到归档（若有数据）
            if self._file_size(self.path) > 0:
                self._seq += 1
                dst = self._archive_path(self._seq)
                try:
                    os.replace(self.path, dst)
                    self._segments.append((self._seq, dst))
                except OSError:
                    # 改不了名：退化为旧行为，直接清空活动段
                    open(self.path, 'w', encoding='utf-8',
                         newline='').close()
            else:
                with open(self.path, 'w', encoding='utf-8', newline=''):
                    pass
            # 2) 只保留最近 K 段
            self._prune_segments_locked()
            self.f = open(self.path, 'a', encoding='utf-8', newline='')
            self._active_size = 0
            self._size = sum(self._file_size(p) for _s, p in self._segments)
            self._records = sum(len(self._read_file_records(p))
                                for _s, p in self._segments)
            self._checkpoint_lsn = self.lsn
            if sync:
                try:
                    self._flush_locked()
                except Exception:
                    pass

    def close(self):
        with self.lock:
            self._flush_locked()
            try:
                self.f.close()
            except Exception:
                pass

    def stats(self) -> dict:
        """观测用：段数量/活动段/归档段/总量（不编造，直接来自文件）"""
        with self.lock:
            return {
                'active': self.path,
                'active_bytes': self._active_size,
                'archives': [p for _s, p in self._segments],
                'archive_count': len(self._segments),
                'total_bytes': self._size,
                'records': self._records,
                'segment_bytes': self.segment_bytes,
                'keep_segments': self.keep_segments,
                'lsn': self.lsn,
            }
