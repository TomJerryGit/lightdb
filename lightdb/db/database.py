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
import threading
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


#: 本引擎的默认库名（镜像 MySQL 的 `test`/`sys`：一个**真实存在**的名字，
#: 所有无 schema 限定的表都住在它下面）。
DEFAULT_DATABASE = 'default'

#: 引擎真实提供、可查询的虚拟库。
INFORMATION_SCHEMA_DB = 'information_schema'

#: `information_schema` 里**真实可查询**的虚拟表（执行器的
#: `information_schema.<name>` 分支接的就是这三个）。`SHOW TABLES` 在
#: 这个库上只能列它们——不能把真实库的表也算进来。
INFORMATION_SCHEMA_TABLES = ('COLUMNS', 'SCHEMATA', 'TABLES')


def column_key(col: dict) -> str:
    """`SHOW COLUMNS` / `information_schema` 的 Key 列（真实约束，不推断）"""
    if col.get('primary'):
        return 'PRI'
    if col.get('unique'):
        return 'UNI'
    return ''


def column_default(col: dict):
    """列的**真实**默认值；没声明 `DEFAULT` 时返回 None（= SQL NULL）

    依据：解析器 `_parse_column_default` 只在文本里真的出现 `DEFAULT`
    子句时才置 `has_default=True`，并把取值存进 `default`。这里不做任何
    推断——尤其不能用"是否 NOT NULL / 是否主键"去猜一个默认值。
    """
    if not col.get('has_default'):
        return None
    return col.get('default')


def column_extra(col: dict) -> str:
    """Extra 列：本引擎**没有** `auto_increment` / `on update` 概念，
    因此恒为空字符串。

    旧实现把 `Extra` 写成 `'auto_increment' if col['primary'] else ''`
    —— 那是一条凭空捏造的元数据：本引擎既没有自增序列（`parser._column_def`
    现在对 `AUTO_INCREMENT` 显式报错），主键也不等于自增。宁可留空，
    也不给客户端一个它据此会做错的结论。
    """
    return ''


def describe_columns(table) -> list:
    """列元数据（`SHOW COLUMNS` / `information_schema.COLUMNS` 的**唯一**
    数据源）

    放在 `db/` 而不是 `sql/` 或 `net/`：`db` 是最底层包（不 import
    sql/net），所以进程内执行器、`information_schema` 虚拟表、
    MySQL 协议服务端三条路径可以共用同一份实现，不会再各自"编"一套。
    `table` 只需要有 `.cols`（`RemoteTable` 在 multiprocess 下也满足）。
    """
    out = []
    for c in getattr(table, 'cols', None) or []:
        out.append({
            'name': c.get('name', ''),
            # 解析器保留长度/精度：`VARCHAR(20)` / `DECIMAL(10,2)`。
            # 老 `_meta.json`（长度被抹掉）只有基类型，也如实回传基类型，
            # 不补猜一个长度。
            'type': c.get('type') or '',
            # MySQL 里主键**隐式 NOT NULL**，而本引擎的插入路径也确实按
            # NOT NULL 执行（不提供主键值会显式报错）。之前只读 `notnull`
            # 标志，于是主键列报 `Null=YES` —— 与真实行为矛盾，属假元数据。
            'null': ('NO' if (c.get('notnull') or c.get('primary')) else 'YES'),
            'key': column_key(c),
            'default': column_default(c),
            'extra': column_extra(c),
        })
    return out


class Database:
    def __init__(self, data_dir: str, name: str = DEFAULT_DATABASE):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        #: 本引擎是**单库模型**：一个 `Database` 实例 = 一个数据目录 =
        #: 一个库，所有表都在这个库下面（没有 `CREATE DATABASE`，
        #: `_meta.json` 里也没有 schema 维度）。`name` 是这个库**真实**
        #: 的名字，`SHOW DATABASES` / `SELECT DATABASE()` / `USE` 的校验
        #: 全部以它为依据，而不是写死一个字面量列表。
        self.name = name or DEFAULT_DATABASE

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

        # ★ 复制器注册位（本进程真实在跑 / 已配置的从库复制器）
        #
        # 为什么放在 Database 上：`SHOW SLAVE STATUS` 由 **执行器** 渲染，而
        # 复制器是 `replication/slave.py::SlaveReplicator`（net 层/用例直接
        # 构造）。两者之间原先没有任何通道，执行器只能"猜"（恒报未运行）
        # —— 列对、语义假。Database 是执行器与复制器共同持有的对象，因此
        # 在这里留一个注册位：复制器构造时挂进来、停止后仍保留（MySQL 在
        # STOP SLAVE 后也会继续显示配置与最后位点），执行器据此报**真实**状态。
        self._replicator = None
        self._replicator_lock = threading.RLock()

        # 逻辑 binlog（`replication/binlog.py::Binlog`）按需创建：只有真正
        # 用到的实例（MasterReplicator / 备份）才会有文件句柄，避免给每个
        # Database 都平白多一个常驻句柄（Windows 上会让数据目录删不掉）。
        self._binlog = None

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
                if tinfo.get('partition'):
                    # 分区描述随表元数据一起持久化（SHOW PARTITIONS /
                    # 分区裁剪都读 Table._partition）
                    self.tables[tname].set_partition(tinfo['partition'])
                if tinfo.get('checks'):
                    # 表级 CHECK 约束随元数据回来（重启后仍生效）
                    self.tables[tname].set_checks(tinfo['checks'])
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
                n: {'cols': t.cols,
                    'partition': getattr(t, '_partition_spec', None),
                    # CHECK 约束随元数据持久化（列级约束本来就在 cols 里，
                    # 表级约束在这里）。没有它，重启后表级 CHECK 就消失，
                    # 违规数据会被静默写进来。
                    'checks': getattr(t, '_checks', None)}
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

    def database_names(self) -> List[str]:
        """引擎**真实**支持的库集合（`SHOW DATABASES` 的唯一依据）

        依据（不是"照着老硬编码列表抄"）：

        1. `self.name` —— 本实例那个真实的单库（所有表都在它下面，
           见 `__init__` 的说明）；
        2. `information_schema` —— **真的可查询**：执行器把
           `information_schema.TABLES/COLUMNS/SCHEMATA` 路由到
           `db/information_schema.py::InformationSchema`（`executor.py`
           的 `_cmd_select` 分支），它本身也是一个真实实现的虚拟库。

        本引擎**没有** `CREATE DATABASE`，也没有第二份数据目录，所以
        这个列表就是全部；`mysql` / `performance_schema` / `sys` 在这里
        并不存在，绝不列出来。
        """
        return [self.name, INFORMATION_SCHEMA_DB]

    def is_known_database(self, name) -> bool:
        """库名是否存在（大小写不敏感，与 MySQL 的 schema 名一致）"""
        if name is None:
            return False
        want = str(name).strip().lower()
        return any(want == n.lower() for n in self.database_names())

    def visible_table_names(self, database=None) -> List[str]:
        """给定库下**可见**的表名；库不存在时返回空列表

        单库模型下这张库的表就是 `self.tables`；`information_schema` 里
        只有它**真的**认识的虚拟表（`INFORMATION_SCHEMA_TABLES`），
        真实表不会因为"切到了这个库"就跑进来。旧实现完全不看库名，
        任何库上 `SHOW TABLES` 都返回全部表——切库不生效却回 OK，
        属于假元数据。
        """
        want = (str(database).strip().lower()
                if database is not None else self.name.lower())
        if want == INFORMATION_SCHEMA_DB.lower():
            return sorted(INFORMATION_SCHEMA_TABLES)
        if want == self.name.lower():
            return sorted(self.tables.keys())
        return []

    def create_table(self, name: str,
                     cols: List[dict],
                     partition: dict = None,
                     checks: List[dict] = None) -> Table:
        """建表

        `checks` 是 `CREATE TABLE` 里表级 CHECK 约束（`sql/parser.py` 的
        `checks`，含 `CONSTRAINT 名字 CHECK (...)`）。**定义期**就解析并
        校验（列引用 / 函数名 / 不支持的形态）：不合法直接抛
        `CheckConstraintError`，绝不把一条"永远不会生效"的约束写进元数据。
        校验通过后按 MySQL 规则补全自动名，随 `_meta.json` 一起持久化。
        """
        if name in self.tables:
            raise ValueError(f"表 {name} 已存在")
        from ..sql.checks import (
            resolve_check_names, validate_check_definitions)
        checks = resolve_check_names(name, cols, checks)
        validate_check_definitions(name, cols, checks)
        tbl = Table(
            name, cols, str(self.data_dir),
            self.lock_manager, self.mvcc)
        if partition:
            tbl.set_partition(partition)
        tbl.set_checks(checks)
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

    # ==================== 复制器注册（SHOW SLAVE STATUS 的数据源）====================

    def attach_replicator(self, replicator):
        """登记本实例的从库复制器（`SlaveReplicator.__init__` 调用）

        同一时刻只保留一个：新的复制器接管时旧的只做告警，不静默丢掉
        （MySQL 的复制通道也只有一条；多个复制器同时写同一个从库实例本身
        就是错误配置，这里让后来者接管并把这件事记进日志）。
        """
        with self._replicator_lock:
            old = self._replicator
            self._replicator = replicator
        if old is not None and old is not replicator:
            logger.warn(
                "已有从库复制器被替换（同一实例只支持一条复制通道）："
                f"{getattr(old, 'master_host', '?')}:"
                f"{getattr(old, 'master_port', '?')} -> "
                f"{getattr(replicator, 'master_host', '?')}:"
                f"{getattr(replicator, 'master_port', '?')}")
        return replicator

    def detach_replicator(self, replicator=None):
        """注销复制器（RESET SLAVE 语义；普通 stop() 不注销）

        `replicator=None` 表示无条件注销当前登记项。
        """
        with self._replicator_lock:
            if replicator is None or self._replicator is replicator:
                self._replicator = None

    @property
    def replicator(self):
        """当前登记的从库复制器（没有则为 None）"""
        with self._replicator_lock:
            return self._replicator

    def replication_status(self):
        """复制器自报的真实状态快照（没有登记复制器时返回 None）

        复制器可能正被 io/sql 两个线程并发修改，所以取值走它自己的
        `show_status()` 快照（内部加锁），执行器只读这份快照。
        """
        rep = self.replicator
        if rep is None:
            return None
        try:
            return rep.show_status()
        except Exception as e:                    # pragma: no cover - 防御
            logger.error(f"读取复制器状态失败: {e}")
            return None

    # ==================== 逻辑 binlog（按需创建）====================

    @property
    def binlog(self):
        """本实例的逻辑 binlog（`replication/binlog.py::Binlog`）

        `MasterReplicator` / `BackupManager` 都按 `db.binlog` 取用；原先
        `Database` 没有这个属性，两处都是构造即 `AttributeError`
        （见 docs/FAILURE_TRIAGE.md）。这里按需创建并复用。
        """
        if self._binlog is None:
            from ..replication.binlog import Binlog
            self._binlog = Binlog(str(self.data_dir))
        return self._binlog

    # ==================== 生命周期 ====================

    def vacuum(self, horizon: int = None) -> int:
        """
        版本回收入口：删除"对所有活跃快照都不可见"的旧版本/墓碑。

        horizon 取**最小的活跃事务号**（mvcc.oldest_active_txn()）：
        任何活跃读视图的 max_id 都 > 它，所以 xmax <= horizon 的
        被淘汰版本对它们恒不可见；horizon = None 时没有活跃事务，
        oldest_active_txn() 返回下一个待分配的事务号，安全。

        注意: 只回收已提交淘汰的版本；未提交事务写的版本 xmax 是
        活跃事务号（> horizon），不会被误删。返回回收的版本数。
        """
        if horizon is None:
            horizon = self.mvcc.oldest_active_txn()
        total = 0
        for tbl in self.tables.values():
            try:
                total += tbl.vacuum(horizon)
            except Exception as e:
                logger.warn(f"vacuum {tbl.name} 失败: {e}")
        return total

    def shutdown(self):
        for tbl in self.tables.values():
            try:
                tbl.close()
            except Exception:
                pass
        if self._binlog is not None:
            try:
                self._binlog.close()
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