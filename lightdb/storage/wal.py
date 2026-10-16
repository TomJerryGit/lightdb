"""
预写日志 (WAL) - 修复版

修复历史:
- 第二批: find_committed_transactions 按顺序扫描
- 第二批: read_all 加缓存
- 第二批: truncate 保持 LSN 单调
- 第三批: 增加 checkpoint / compact
- 事务原子性批次:
  * `log()` 之后由调用方调用 `flush_to_os()`，落实 **log-before-data**
    （WAL 记录先于页修改进入 OS，进程被硬杀也不会出现"页改了、日志没了"）。
  * **log() 现在默认就把记录 flush 到 OS**（不做 fsync，成本约 0.05ms），
    这样所有调用方天然满足 log-before-data，不需要各自记得调用
    flush_to_os()；`sync=True`（COMMIT/ABORT）再补 fsync，是掉电级持久化点。
  * 新增 `analyze()`：一次扫描给出 [已提交操作] 与 [loser 事务操作]，
    供崩溃恢复做 redo + undo（loser undo 依赖 UPDATE/DELETE 记录里的旧行镜像）。
  * **autocommit 记录视为隐式提交**：`txn_id == 0`（executor 没有显式
    事务时传 0）以及 Table 为 autocommit 语句分配的合成高位事务号
    （`>= SYNTHETIC_TXN_FLOOR`，区间与 mvcc.ReadView 的判定一致）都没有
    COMMIT 记录，必须按已提交重放。旧实现把 0 号直接 continue 丢掉，
    合成号又会被判成 loser，结果是**autocommit 写入一重启就整表消失**。

checkpoint 机制说明:
- checkpoint() 记录一个安全 LSN
- compact_before(lsn) 删除该 LSN 之前的已提交记录
- 使用前提: 调用方需保证 lsn 之前的事务都已提交/回滚，且不再需要恢复

WAL 有界（轮转）批次:
- 新增 **体积/条数计量** (`size_bytes()` / `record_count()` /
  `needs_checkpoint()`)，供 Table 侧按阈值触发 checkpoint。
  计量不依赖每次 `os.path.getsize()`（每条语句一次 syscall），
  而是在 `log()` 里累加写入字节数；`__init__` 扫已有文件时一并初始化，
  `truncate()` / `compact_before()` 重算。
- `truncate()` 现在**先把重写落到 OS 并 fsync**，再重开追加句柄：
  checkpoint 的语义是"数据页已经 fsync，日志可以退休"，
  截断动作本身必须真的落盘，否则掉电后可能读回半截日志。
  LSN 仍然单调（`_base_lsn = self.lsn`），恢复侧不会因为轮转而
  把新记录当成旧记录。

已知限制:
- 只保证"进程崩溃不丢"（flush 到 OS）与"COMMIT 后掉电不丢"（fsync）。
  未提交事务的页若被提前刷盘，靠恢复阶段的 loser-undo 撤销，
  但该 undo 需要 WAL 记录里的旧行镜像（UPDATE/DELETE 已带 old/old_loc）。
- 无 checkpoint/LSN 页头联动，恢复是"日志全量重放 + 幂等覆盖"。
"""
import json
import os
import struct
import threading

# 合成 autocommit 事务号下界（必须与 storage/mvcc.py 的 ReadView 判定一致）。
# Table 给 autocommit 语句分配的事务号从 0xFFFFFFFF 往下取（行头 xmin/xmax
# 是 uint32，不能为负）；这类事务号没有 COMMIT 记录，恢复时必须按
# "隐式提交" 处理，否则 autocommit 写入会在重启时被当成 loser 撤销掉。
try:
    from .mvcc import SYNTHETIC_TXN_FLOOR
except Exception:  # pragma: no cover - 防御性回退
    SYNTHETIC_TXN_FLOOR = 0xF0000000


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
    提交、本来必须保留的旧行在恢复后彻底读不出来**
    （实测: 未提交事务 update/delete 过的行在重启后整行消失，
     连 select 都返回空表）。

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


class WAL:

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.lsn = 0
        self._base_lsn = 0

        # checkpoint 状态
        self._checkpoint_lsn = 0

        # 体积/条数计量（避免每条语句都 getsize；见文件头说明）
        self._size = 0
        self._records = 0

        # 扫描已有 WAL
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = WALRecord.from_json(line)
                        if rec.lsn > self.lsn:
                            self.lsn = rec.lsn
                        self._records += 1
                        self._size += len(line) + 1
                    except Exception:
                        pass

        self._base_lsn = self.lsn
        self.f = open(path, 'a', encoding='utf-8')

        # 增量读取缓存
        self._cached_records = []
        self._cached_offset = 0

    # ==================== 写 ====================

    def log(self, txn_id: int, rtype: str, data: dict,
            sync: bool = False) -> int:
        """
        追加一条 WAL 记录（写入 Python 缓冲，不做 syscall）。

        持久化点由调用方把控：
        - `commit()` / `abort()` → fsync（显式事务的持久化点）
        - `commit_async()`       → flush 到 OS（autocommit 语句）
        - `flush_to_os()`        → 交调用方在"改页之前"显式调用

        注意 sync=True 仍然会立即 flush+fsync，保留给需要单条
        持久化的调用方。
        """
        with self.lock:
            self.lsn += 1
            rec = WALRecord(self.lsn, txn_id, rtype, data)
            line = rec.to_json()
            self.f.write(line + '\n')
            self._size += len(line) + 1
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

        为什么可以：这条 COMMIT 的作用是让崩溃恢复的 analyze() 认出
        "这是一个已提交的隐式事务"；flush 到 OS 后进程被硬杀照样
        读得回来。真正的 fsync 留给显式 COMMIT、abort() 和
        Table.flush()/close()。

        为什么必须分开：每条 autocommit INSERT 都 fsync 一次，
        3000 行插入在 Windows 上实测要 200s+（14 rows/s）。
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
        """语句级 autocommit 提交：数据 + COMMIT 记录都交给 OS（**不 fsync**）

        与 `commit_async` 的差别只有一处：COMMIT 记录本身也 flush 到 OS。
        `commit_async` 把 COMMIT 留在 Python 缓冲里（靠下一条语句的
        `flush_to_os` 顺带带出去），对"逐行 autocommit"没有影响；但语句级
        COMMIT 是**整条语句**的判定点 —— 一条 500 行的多值 INSERT 若被硬杀
        时 COMMIT 还在 Python 缓冲里，恢复会把这几百行已提交的行当 loser
        全部撤销。因此这里在写 COMMIT 之后再 flush 一次。

        持久性档位与逐行 autocommit 完全一致（Stage 2.1 的既有设计）:
        进程被硬杀不丢（数据在 OS 缓冲里），掉电可能丢最后几条；
        真正的掉电级持久化点仍然只有显式 COMMIT（`commit` / fsync）、
        checkpoint 与 close。
        """
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
        """读取所有记录（带缓存）"""
        with self.lock:
            if not os.path.exists(self.path):
                return list(self._cached_records)

            try:
                cur_size = os.path.getsize(self.path)
            except OSError:
                return list(self._cached_records)

            if cur_size < self._cached_offset:
                # 文件被截断/重写
                self._cached_records = []
                self._cached_offset = 0

            if cur_size == self._cached_offset:
                return list(self._cached_records)

            with open(self.path, 'r', encoding='utf-8') as f:
                f.seek(self._cached_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = WALRecord.from_json(line)
                        self._cached_records.append(rec)
                    except Exception:
                        pass
                self._cached_offset = f.tell()

            return list(self._cached_records)

    def read_from_lsn(self, lsn: int, limit: int = None):
        """
        读取 lsn >= 指定值的记录（增量读，用于 PITR）
        """
        records = self.read_all()
        result = []
        for r in records:
            if r.lsn >= lsn:
                result.append(r)
                if limit and len(result) >= limit:
                    break
        return result

    def find_committed_transactions(self):
        """
        返回 [(txn_id, [ops]), ...]
        ops 是 COMMIT 之前的 INSERT/UPDATE/DELETE 记录。

        修复: txn_id == 0 与合成 autocommit 事务号不再被丢弃。
        它们是 autocommit（executor 的 INSERT/UPDATE/DELETE 在没有
        显式事务时传 txn_id=0；Table 则分配合成高位号），不会写
        COMMIT 记录，因此按 "隐式提交" 处理；否则 autocommit 写入
        崩溃后永远无法重放。
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
                # autocommit: 每条记录自身即已提交
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

        - committed_ops: 需要 **redo** 的操作，元素 (lsn, txn_id, type, data)，
          按 LSN 升序。包含 COMMIT 之前的记录，以及 autocommit 记录
          （txn_id == 0 或合成高位事务号 → 隐式提交）。
        - loser_ops: 需要 **undo** 的操作（有数据记录但没有 COMMIT，
          或 ABORT 在 COMMIT 之前），按 LSN 升序返回；
          调用方必须**倒序**应用撤销，才能正确回退同一行的多次修改。

        已知限制: 本方法只做"日志级"分类，不区分页是否已经落盘；
        redo/undo 由 Table 侧的幂等实现保证可重复执行。
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
                # loser: 未提交（或已 ABORT）。撤销是幂等的，
                # 即使进程内已经回滚过、这里重复撤销也不会出错。
                # 交给恢复层之前先修好旧行镜像（见 fix_loser_undo_image）。
                for r in ops:
                    if r.type in ('UPDATE', 'DELETE'):
                        fix_loser_undo_image(r.data)
                    loser_ops.append((r.lsn, tid, r.type, r.data))

        committed_ops.sort(key=lambda x: x[0])
        loser_ops.sort(key=lambda x: x[0])
        return committed_ops, loser_ops

    # ==================== checkpoint / compact ====================

    def checkpoint(self) -> int:
        """
        记录当前 LSN 作为安全点，返回该 LSN。
        调用方需保证：此 LSN 之前的事务都已提交/回滚。
        """
        with self.lock:
            self._checkpoint_lsn = self.lsn
            return self._checkpoint_lsn

    def compact_before(self, lsn: int) -> int:
        """
        删除 lsn 之前的记录。
        - 只保留 lsn >= 目标值的记录
        - 返回被删除的记录数
        - 注意：调用方需确保这些记录不再需要恢复
        """
        with self.lock:
            # 关闭当前文件句柄
            try:
                self.f.flush()
                self.f.close()
            except Exception:
                pass

            # 读全部记录
            records = []
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(WALRecord.from_json(line))
                        except Exception:
                            pass

            # 过滤
            kept = [r for r in records if r.lsn >= lsn]
            removed = len(records) - len(kept)

            # 重写文件
            with open(self.path, 'w', encoding='utf-8') as f:
                for r in kept:
                    f.write(r.to_json() + '\n')

            # 重开
            self.f = open(self.path, 'a', encoding='utf-8')

            # 重置缓存
            self._cached_records = list(kept)
            self._cached_offset = os.path.getsize(self.path)
            self._records = len(kept)
            self._size = self._cached_offset

            # 更新 base_lsn
            if kept:
                self._base_lsn = kept[0].lsn - 1

            return removed

    # ==================== 维护 ====================

    def size_bytes(self) -> int:
        """当前 WAL 段落的字节数（估算，用于阈值触发；见文件头说明）"""
        with self.lock:
            return self._size

    def record_count(self) -> int:
        """当前 WAL 段落的记录条数"""
        with self.lock:
            return self._records

    def needs_checkpoint(self, max_bytes: int = 0,
                         max_records: int = 0) -> bool:
        """
        是否达到 checkpoint 阈值（0 表示该维度不限制）。

        只做判断，不做任何刷盘/截断动作——真正的 checkpoint 语义
        （"页先 fsync、日志后退休"）由 Table.checkpoint() 负责，
        它必须持有表锁并确认没有未提交事务。
        """
        with self.lock:
            if max_bytes > 0 and self._size >= max_bytes:
                return True
            if max_records > 0 and self._records >= max_records:
                return True
            return False

    def truncate(self, sync: bool = True):
        """
        清空 WAL（checkpoint 的第二步）。LSN 保持单调。

        ★ 调用前提（由 Table.checkpoint 保证）: WAL 里所有记录对应的
        数据页**已经 fsync 落盘**，且不存在未提交事务。否则截断就是
        "日志先于数据退休"，崩溃后无法 redo/undo。

        `sync=True`（周期 checkpoint）: 把"段落已退休"这件事本身落盘，
        保证 WAL 的有界性在掉电后依然成立。
        `sync=False`（进程收尾）: 数据页已经 fsync，截断丢失只会让下次
        启动多幂等重放一批记录，不会丢已提交数据；省下一次 fsync。
        """
        with self.lock:
            self._base_lsn = self.lsn
            try:
                self.f.flush()          # 缓冲交给 OS 后 close（不需要 fsync：
                self.f.close()          # 这些记录马上要被截断掉）
            except Exception:
                pass

            with open(self.path, 'w', encoding='utf-8') as f:
                f.flush()
                if sync:
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass

            self.f = open(self.path, 'a', encoding='utf-8')

            self._cached_records = []
            self._cached_offset = 0
            self._size = 0
            self._records = 0
            self._checkpoint_lsn = self.lsn

    def close(self):
        with self.lock:
            self._flush_locked()
            try:
                self.f.close()
            except Exception:
                pass