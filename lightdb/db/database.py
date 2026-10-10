"""
数据库对象 - 集成 MVCC + 锁（修复版）

修复:
- abort() 调用 mvcc.rollback_undo(tid)
- commit() 中调用 wal.commit
- _recover 清理 aborted 事务

事务原子性 / 崩溃恢复批次:
- begin() 额外分配 **表级事务号**（一个真实事务对每张表是同一个号，
  该号同时用于行头 xmin/xmax、锁归属、WAL 记录），并登记到
  Table.register_read_view，使读路径能按快照过滤并发未提交版本。
- commit(): 先给每张表写 COMMIT 记录并 fsync（**持久化点**），
  再刷脏页 + 保存 BTree 根；然后释放 MVCC 与锁。
  顺序不能反：先落页后写 COMMIT 会让"已提交"在崩溃后丢失。
- abort(): 对每张表调用 Table.abort_txn() 做**真正的物理回滚**
  （倒序回放 undo 镜像把旧行写回原页原槽位），再写 ABORT 记录、
  释放 MVCC 与锁。旧实现只丢版本链节点，页上数据不会回来。
"""
import hashlib
import json
from pathlib import Path
from typing import Dict, List

from ..logger import logger
from ..storage.txn import TxnManager
from ..storage.lock_manager import LockManager
from ..storage.mvcc import MVCCManager
from .table import Table


def hash_native_password(password: str) -> str:
    """MySQL native password 哈希"""
    if not password:
        return ''
    s1 = hashlib.sha1(password.encode()).digest()
    s2 = hashlib.sha1(s1).digest()
    return s2.hex()


class Database:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.txn_manager = TxnManager()
        self.lock_manager = LockManager()
        self.mvcc = MVCCManager()

        self.tables: Dict[str, Table] = {}
        self.users: Dict[str, dict] = {
            'root': {
                'auth_hash': hash_native_password(''),
                'privs': ['ALL'],
            }
        }
        self.autocommit = True

        self._load()
        self._advance_mvcc_epoch()
        self._recover()

    # ==================== 事务号单调性 ====================

    def _advance_mvcc_epoch(self):
        """
        启动时恢复事务号单调性（修复"重启后显式事务看不到旧行"）。

        事务号只在内存里递增，重开数据库又从 1 开始，而页上行头里
        持久化的 xmin/xmax 是重启前的事务号。ReadView 的
        `xmin >= max_id → 不可见` 会把它们判成"未来事务写的行"，
        结果是重启后 `BEGIN; UPDATE ...` 匹配不到任何行
        （实测 affected=0），相应的 WAL 重放也被跳过。
        这里扫描所有堆页行头与 WAL 里的最大事务号，把 mvcc 的
        计数器推进到它之后，保证新事务号严格大于所有已持久化的号。

        只统计真实事务号（< SYNTHETIC_TXN_FLOOR），autocommit 的
        合成高位号不参与，避免把计数器顶到 uint32 上界。
        """
        import struct

        from ..storage.page import PAGE_TYPE_HEAP
        try:
            from ..storage.mvcc import SYNTHETIC_TXN_FLOOR
        except Exception:                    # pragma: no cover
            SYNTHETIC_TXN_FLOOR = 0xF0000000

        max_tid = 0
        for tbl in self.tables.values():
            try:
                for pid in range(1, tbl.bp.total_pages()):
                    page = tbl.bp.get_page(pid)
                    if page is None or page.page_type != PAGE_TYPE_HEAP:
                        continue
                    for _sid, rb in page.iter_rows():
                        if len(rb) < 8:
                            continue
                        xmin, xmax = struct.unpack_from('<II', rb, 0)
                        for t in (xmin, xmax):
                            if t < SYNTHETIC_TXN_FLOOR and t > max_tid:
                                max_tid = t
                for rec in tbl.wal.read_all():
                    t = rec.txn_id
                    if t < SYNTHETIC_TXN_FLOOR and t > max_tid:
                        max_tid = t
            except Exception as e:
                logger.warn(f"恢复事务号单调性失败 {tbl.name}: {e}")

        if max_tid:
            self.mvcc.advance_next_txn_id(max_tid + 1)
            logger.info(
                f"事务号计数器推进到 {max_tid + 1}（已持久化的最大号 {max_tid}）")

    # ==================== 元数据 ====================

    def _meta_path(self):
        return self.data_dir / "_meta.json"

    def _load(self):
        p = self._meta_path()
        if not p.exists():
            return
        try:
            with open(p, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            for tname, tinfo in meta.get('tables', {}).items():
                self.tables[tname] = Table(
                    tname, tinfo['cols'], str(self.data_dir),
                    self.lock_manager, self.mvcc,
                )
            if 'users' in meta:
                self.users = meta['users']
        except Exception as e:
            logger.error(f"加载元数据失败: {e}")

    def _recover(self):
        total = 0
        for tbl in self.tables.values():
            try:
                total += tbl.recover_from_wal()
            except Exception as e:
                logger.warn(f"恢复 {tbl.name} 失败: {e}")
        if total:
            logger.info(f"崩溃恢复完成，共恢复 {total} 条记录")

    def save_meta(self):
        meta = {
            'tables': {
                n: {'cols': t.cols}
                for n, t in self.tables.items()
            },
            'users': self.users,
        }
        p = self._meta_path()
        tmp = p.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        tmp.replace(p)

    # ==================== 表管理 ====================

    def create_table(self, name: str,
                     cols: List[dict]) -> Table:
        if name in self.tables:
            raise ValueError(f"表 {name} 已存在")
        tbl = Table(
            name, cols, str(self.data_dir),
            self.lock_manager, self.mvcc)
        self.tables[name] = tbl
        self.save_meta()
        return tbl

    def drop_table(self, name: str):
        if name not in self.tables:
            raise ValueError(f"表 {name} 不存在")
        tbl = self.tables.pop(name)
        tbl.close()
        for suffix in ('.dat', '.btroot', '.wal'):
            p = self.data_dir / f"{name}{suffix}"
            if p.exists():
                p.unlink()
        self.save_meta()

    def get_table(self, name: str) -> Table:
        if name not in self.tables:
            raise KeyError(f"表 {name} 不存在")
        return self.tables[name]

    # ==================== 事务 ====================

    def begin(self, isolation: str = "READ COMMITTED"):
        """开启事务"""
        tid = self.mvcc.next_txn_id()
        rv = self.mvcc.create_readview(tid)
        txn = {
            'txn_id': tid,
            'isolation': isolation,
            'read_view': rv,
        }
        return txn

    def commit(self, txn):
        """提交事务（COMMIT 落盘是持久化点，必须先于脏页刷盘）"""
        tid = txn['txn_id']
        for tbl in self.tables.values():
            try:
                # 1) 写 COMMIT 并 fsync —— 掉电后靠它判定事务已提交
                tbl.wal.commit(tid)
                # 2) 再把脏页 / BTree 根落盘
                tbl.flush()
                # 3) 丢弃物理 undo 镜像
                self.mvcc.purge_undo(tid)
            except Exception as e:
                logger.error(f"提交 {tbl.name} 失败: {e}")
        self.mvcc.release(tid, committed=True)
        self.lock_manager.release(tid)
        for tbl in self.tables.values():
            tbl._read_views.pop(tid, None)

    def abort(self, txn):
        """回滚事务：真正把数据改回去，再写 ABORT、放锁"""
        tid = txn['txn_id']
        for tbl in self.tables.values():
            try:
                # ★ 物理回滚：倒序把旧行写回原页原槽位
                tbl.abort_txn(tid)
            except Exception as e:
                logger.error(f"回滚 {tbl.name} 失败: {e}")
        for tbl in self.tables.values():
            try:
                tbl.wal.abort(tid)
                tbl.flush()
            except Exception:
                pass
        # ★ 撤销 MVCC 版本链里本事务制造的节点
        try:
            self.mvcc.rollback_undo(tid)
        except Exception as e:
            logger.error(f"rollback_undo 失败: {e}")
        # 释放 MVCC + 锁
        self.mvcc.release(tid, committed=False)
        self.lock_manager.release(tid)
        for tbl in self.tables.values():
            tbl._read_views.pop(tid, None)

    # ==================== 生命周期 ====================

    def shutdown(self):
        for tbl in self.tables.values():
            try:
                tbl.close()
            except Exception:
                pass

    def stats(self) -> dict:
        return {
            'tables': {
                n: t.stats() for n, t in self.tables.items()
            },
            'locks': self.lock_manager.stats(),
            'mvcc': self.mvcc.stats(),
        }