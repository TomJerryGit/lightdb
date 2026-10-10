"""
预写日志 (WAL) - 修复版

修复历史:
- 第二批: find_committed_transactions 按顺序扫描
- 第二批: read_all 加缓存
- 第二批: truncate 保持 LSN 单调
- 第三批: 增加 checkpoint / compact

checkpoint 机制说明:
- checkpoint() 记录一个安全 LSN
- compact_before(lsn) 删除该 LSN 之前的已提交记录
- 使用前提: 调用方需保证 lsn 之前的事务都已提交/回滚，且不再需要恢复
"""
import json
import os
import threading


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
        with self.lock:
            self.lsn += 1
            rec = WALRecord(self.lsn, txn_id, rtype, data)
            self.f.write(rec.to_json() + '\n')
            if sync:
                self._flush_locked()
            return self.lsn

    def commit(self, txn_id: int):
        self.log(txn_id, 'COMMIT', {}, sync=True)

    def abort(self, txn_id: int):
        self.log(txn_id, 'ABORT', {}, sync=True)

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
        """
        records = self.read_all()
        by_txn = {}
        for r in records:
            if r.txn_id == 0:
                continue
            by_txn.setdefault(r.txn_id, []).append(r)

        committed = []
        for tid, recs in by_txn.items():
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

            ops = [r for r in recs[:commit_idx]
                   if r.type in ('INSERT', 'UPDATE', 'DELETE')]
            committed.append((tid, ops))

        return committed

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

            # 更新 base_lsn
            if kept:
                self._base_lsn = kept[0].lsn - 1

            return removed

    # ==================== 维护 ====================

    def truncate(self):
        """
        清空 WAL。LSN 保持单调。
        """
        with self.lock:
            try:
                self.f.close()
            except Exception:
                pass

            self._base_lsn = self.lsn

            with open(self.path, 'w', encoding='utf-8'):
                pass

            self.f = open(self.path, 'a', encoding='utf-8')

            self._cached_records = []
            self._cached_offset = 0
            self._checkpoint_lsn = self.lsn

    def close(self):
        with self.lock:
            self._flush_locked()
            try:
                self.f.close()
            except Exception:
                pass