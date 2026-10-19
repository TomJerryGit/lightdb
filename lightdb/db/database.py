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
import os
import re
import threading
from pathlib import Path
from typing import Dict, List

from ..logger import logger
from ..storage.txn import TxnManager
from ..storage.lock_manager import LockManager
from ..storage.mvcc import MVCCManager
from .errors import DdlError
from .table import Table, coerce


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
    """`SHOW COLUMNS` / `information_schema` 的 Key 列（真实约束，不推断）

    只读**列自身**的声明（列级 PRIMARY KEY / 列级 UNIQUE）。表级
    `UNIQUE KEY (列)` 覆盖到的列由 `describe_columns` 补 `UNI`
    （那需要表的上下文，单列字典看不到）。
    """
    if col.get('primary'):
        return 'PRI'
    if col.get('unique'):
        return 'UNI'
    return ''


#: 本引擎能写进 `SHOW CREATE TABLE` / 逻辑 dump 并原样读回的索引名
_UNIQUE_NAME_RE = re.compile(r'^[A-Za-z_]\w*$')


def resolve_unique_indexes(table: str, cols: List[dict], specs) -> List[dict]:
    """校验并规范化 `CREATE TABLE` 的唯一性声明（表级 + 列级）

    `specs` 同时接收表级 `UNIQUE KEY/INDEX` 与列级 `col ... UNIQUE`
    推导出的规格（后者带 `origin='column'`、名字默认为列名）。**定义期**
    就拒绝坏定义（未知列 / 重名 / 非法名字 / 空列清单），绝不把一条
    "永远不会生效"的唯一约束写进元数据：
    * 未知列 → 1072（`Key column 'x' doesn't exist in table`）；
    * 索引重名 / 占用 PRIMARY 名字 → 1061（`Duplicate key name`）；
    * 名字含不支持字符 → 1064（名字要能写进 DDL 并原样读回，
      否则 `SHOW CREATE TABLE` → 重建 的往返会失败）；
    * 同一索引里同一列出现两次 → 1060（MySQL 同样拒绝）。
    """
    names = [c.get('name') for c in cols]
    out, seen = [], {}
    for spec in (specs or []):
        if not isinstance(spec, dict):
            raise DdlError(
                f"表 {table} 的 UNIQUE 声明格式错误: {spec!r}", errno=1064)
        columns = [str(c) for c in (spec.get('columns') or [])]
        if not columns:
            raise DdlError(
                f"表 {table} 的 UNIQUE 声明没有列（无法强制任何唯一性）",
                errno=1064)
        if len(set(columns)) != len(columns):
            raise DdlError(
                f"表 {table} 的 UNIQUE 声明列清单里有重复列: {columns}",
                errno=1060)
        for cname in columns:
            if cname not in names:
                raise DdlError(
                    f"Key column '{cname}' doesn't exist in table "
                    f"'{table}'", errno=1072, sqlstate='42022')
        name = str(spec.get('name') or columns[0]).strip()
        if not _UNIQUE_NAME_RE.match(name):
            raise DdlError(
                f"表 {table} 的唯一索引名 {name!r} 含不支持的字符"
                f"（只支持 [A-Za-z_]\\w*；否则 SHOW CREATE TABLE / 逻辑"
                f"备份无法往返）", errno=1064)
        low = name.lower()
        if low == 'primary' or low in seen:
            raise DdlError(f"Duplicate key name '{name}'", errno=1061,
                           sqlstate='42000')
        seen[low] = True
        item = {'name': name, 'columns': columns}
        if spec.get('origin') == 'column':
            # 列级 `col ... UNIQUE` 自动推导出的规格（索引名 = 列名）。
            # 标记来源，避免 `SHOW CREATE TABLE` 既写内联 UNIQUE 又写
            # 一条重复的 `UNIQUE KEY 列名 (列)`。
            item['origin'] = 'column'
        out.append(item)
    return out


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

    `Key` 列：列级 `PRIMARY KEY`/`UNIQUE` 来自列自身声明；表级
    `UNIQUE KEY (列)` 覆盖的列同样报 `UNI`（MySQL 里唯一索引的列就是
    `UNI`，这是真实约束，不是推断）。
    """
    # 唯一约束覆盖的列（表级 UNIQUE + 列级 UNIQUE 推导；`_unique_indexes`
    # 只有真 Table 才有，RemoteTable 之类的最小投影没有就按空集处理）
    unique_cols = set()
    for spec in (getattr(table, '_unique_indexes', None) or []):
        unique_cols.update(spec.get('columns') or [])
    out = []
    for c in getattr(table, 'cols', None) or []:
        key = column_key(c)
        if not key and c.get('name') in unique_cols:
            key = 'UNI'
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
            'key': key,
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

        # 挂在库上的审计器（`sql/executor.py::_AuditSink`）。审计批量落盘后，
        # `shutdown()` 是"正常停机 → 待落盘批一定写出去"的确定性落盘点：
        # 执行器与库同生共死，但库不知道执行器，所以由执行器反向注册进来。
        self._audit_sinks = []

        #: **进行中的结构变更**（ALTER / TRUNCATE / RENAME）。
        #:
        #: 为什么必须持久化：改行载荷的列数（ADD/DROP COLUMN）要遍历
        #: 所有堆页，中途崩溃必然可能只改了一部分行。恢复时需要一个
        #: "这次变更到底算不算数"的权威来源 —— 就是这里。它随 `_meta.json`
        #: 原子落盘，因此"结构变更生效"这件事有唯一的原子提交点：
        #: `save_meta()`。见 `_recover_pending_ddl`。
        self._pending_ddl = None

        self._load()
        self._advance_mvcc_epoch()
        self._recover()
        # 恢复"未完成的结构变更"必须排在 WAL 重放之后：WAL 里可能还有
        # 旧列数的行记录，先让它重放完，再按 pending 记录把列数补到位。
        self._recover_pending_ddl()

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
                    # 唯一性约束必须跨重启保留：丢了它，重启后唯一性就
                    # 悄悄失效（声明还在列里、约束却没了）。列级 UNIQUE
                    # 由 `Table.__init__` 从 `cols[i]['unique']` 推导，
                    # 因此老 `_meta.json`（没有 unique_indexes 条目）也
                    # 会在打开时补上（见 `_sync_column_unique_indexes`）。
                    unique_indexes=tinfo.get('unique_indexes'),
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
            self._pending_ddl = meta.get('pending_ddl')
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
                    # 唯一性约束（表级 UNIQUE KEY + 列级 UNIQUE 推导）
                    # 随元数据持久化：它们由写入路径真实强制，重启后必须
                    # 继续强制。列级条目带 origin='column'。
                    'unique_indexes': t.unique_indexes(),
                    # CHECK 约束随元数据持久化（列级约束本来就在 cols 里，
                    # 表级约束在这里）。没有它，重启后表级 CHECK 就消失，
                    # 违规数据会被静默写进来。
                    'checks': getattr(t, '_checks', None)}
                for n, t in self.tables.items()
            },
            'users': self.users,
            # ★ 进行中的结构变更（崩溃恢复的唯一依据）。它有值 = "结构变更
            #   还没提交"：重启后按它把剩余的行补完（见 `_recover_pending_ddl`）。
            #   写在**同一份** `_meta.json` 里，是为了让"新列定义"与
            #   "pending 标记清除"成为同一个原子替换 —— 两者分开写就会出现
            #   "结构已换、标记还在"（重启后重复改一次）或"标记没了、结构
            #   还是旧的"（重启后表结构永远停在半套）这两种半套状态。
            'pending_ddl': self._pending_ddl,
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
                     checks: List[dict] = None,
                     unique_indexes: List[dict] = None) -> Table:
        """建表

        `checks` 是 `CREATE TABLE` 里表级 CHECK 约束（`sql/parser.py` 的
        `checks`，含 `CONSTRAINT 名字 CHECK (...)`）。**定义期**就解析并
        校验（列引用 / 函数名 / 不支持的形态）：不合法直接抛
        `CheckConstraintError`，绝不把一条"永远不会生效"的约束写进元数据。
        校验通过后按 MySQL 规则补全自动名，随 `_meta.json` 一起持久化。

        `unique_indexes` 是表级 `UNIQUE KEY/INDEX 名字 (列...)`（解析器
        不再静默丢弃它们）。这里在定义期校验列引用/重名/名字形态，然后
        交给 `Table` —— 由写入路径**真正强制**唯一性，并随 `_meta.json`
        持久化（重启后仍强制，`SHOW INDEX` 如实列出）。

        列级 `col ... UNIQUE`（`cols[i]['unique']`）在这里等价成"索引名 =
        列名"的单列唯一规格，与表级声明一起校验、一起强制。因此
        `CREATE TABLE t (a INT UNIQUE)` 与
        `CREATE TABLE t (a INT, UNIQUE KEY a (a))` 在本引擎里是同一件事。
        """
        if name in self.tables:
            raise ValueError(f"表 {name} 已存在")
        from ..sql.checks import (
            resolve_check_names, validate_check_definitions)
        checks = resolve_check_names(name, cols, checks)
        validate_check_definitions(name, cols, checks)
        # 表级显式声明 + 列级 UNIQUE 推导（列名 = 自动索引名）。列级规格
        # 由调用方传进来的旧元数据里可能已经存在（带 origin），这里以
        # `cols` 为准重新推导，避免重复添加同名规格。
        merged_specs = [
            dict(s) for s in (unique_indexes or [])
            if isinstance(s, dict) and s.get('origin') != 'column']
        for c in cols:
            if c.get('unique'):
                cname = str(c.get('name') or '')
                if cname:
                    merged_specs.append({'name': cname,
                                         'columns': [cname],
                                         'origin': 'column'})
        unique_specs = resolve_unique_indexes(name, cols, merged_specs)
        tbl = Table(
            name, cols, str(self.data_dir),
            self.lock_manager, self.mvcc,
            unique_indexes=unique_specs)
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
        for suffix in ('.dat', '.btroot', '.wal', '.free'):
            p = self.data_dir / f"{name}{suffix}"
            if p.exists():
                p.unlink()
        self.save_meta()

    def get_table(self, name: str) -> Table:
        if name not in self.tables:
            raise KeyError(f"表 {name} 不存在")
        return self.tables[name]

    # ==================== DDL：结构变更（ALTER / TRUNCATE） ====================
    #
    # 设计要点（每一条都对应一种"看起来成功、其实没生效"）:
    #
    # 1) 行载荷是**按列位置**编码的 JSON 数组，所以 ADD/DROP COLUMN 必须
    #    真的改写每一个行版本，而不是只改 `cols` 元数据；
    # 2) `_meta.json` 用 tmp + `os.replace` 原子替换，"结构变更生效"因此
    #    有唯一的原子提交点；进行中的变更写进同一份文件（`pending_ddl`），
    #    崩溃后据此把剩余的行补完 —— 结果只会是"完整生效"或"完全没生效"，
    #    不会停在半套；
    # 3) 每次结构变更的执行序列固定为
    #        guard（表上无未提交事务）
    #     →  退休 WAL（清掉旧列数的行记录）
    #     →  写 pending 意图（原子）
    #     →  改写所有行版本 + 换列定义
    #     →  checkpoint 落盘 + 清 pending + 原子换元数据（提交点）
    #    整段持有**表级锁**（`Table._lock`），等价于 MySQL 的元数据锁：
    #    并发写入不会插进"行已改写、列定义还没换"的窗口里；
    # 4) 事务/并发：MySQL 的 DDL 隐式提交，本引擎同样在 ALTER/TRUNCATE 前
    #    隐式提交本会话的事务（见 `Executor._implicit_commit_ddl`）；表上
    #    还有**别的**未提交事务时**显式报错**（`_ddl_guard`），不做
    #    "尽力而为"（本引擎没有元数据锁，硬做会刷出未提交数据并退休 WAL）。

    def _ddl_guard(self, tbl, what: str):
        """结构变更前置检查：表上存在未提交事务 → 显式报错（不硬做）"""
        if getattr(tbl, '_read_views', None):
            raise DdlError(
                f"表 {tbl.name} 上存在未提交事务，无法执行 {what}：请先 "
                f"COMMIT / ROLLBACK（本引擎没有元数据锁，DDL 会在未提交"
                f"数据上刷页并退休 WAL，因此显式拒绝而不是尽力而为）",
                errno=1105)

    def _ddl_begin(self, entry: dict):
        """登记"进行中的结构变更"并原子落盘（崩溃恢复的意图记录）"""
        self._pending_ddl = entry
        self.save_meta()

    def _ddl_retire_wal(self, tbl, what: str):
        """结构变更前先退休（截断）WAL —— 失败即**放弃本次变更**

        为什么必须排在 `_ddl_begin` 之前：这一步失败时我们要能"干净地什么都
        不做"（客户端看到报错、盘上什么都没变）。若先把 pending 记录写下去
        再失败，恢复端会照着 pending 把这条**已经报错**的变更补完 ——
        那就成了"报错却生效"。

        为什么必须成功：WAL 里可能还有**旧列数**的行记录，列定义换掉之后
        一次崩溃重放就会插回一行旧结构（与 `Table.rewrite_row_payloads`
        的幂等判据冲突）。`Table.checkpoint` 在本表存在未提交事务时返回
        False（拒绝在未提交数据上刷页 + 退休日志），这里把它变成显式错误。
        """
        try:
            if not tbl.checkpoint():
                raise DdlError(
                    f"表 {tbl.name} 上出现了未提交事务，{what} 中止"
                    f"（先 COMMIT / ROLLBACK 重试）", errno=1105)
        except DdlError:
            raise
        except Exception as e:
            raise DdlError(
                f"{what} 前退休 WAL 失败（{tbl.name}）: {e}", errno=1105)

    def _ddl_commit(self, tbl):
        """结构变更的**提交点**：落盘 + 退休 WAL → 清标记并原子换元数据

        顺序即正确性：先 `checkpoint()`（脏页 fsync + 根指针 fsync +
        截断 WAL），再原子替换 `_meta.json`（新列定义 + `pending_ddl=None`
        在同一次写入里）。崩溃发生在两者之间时，盘上是"旧元数据 + 新行"，
        重启后 `_recover_pending_ddl` 按 pending 记录把结构补到位。
        """
        try:
            tbl.checkpoint(force=True)
        except Exception as e:                       # pragma: no cover - 防御
            logger.warn(f"结构变更落盘失败 {tbl.name}: {e}")
        self._pending_ddl = None
        self.save_meta()

    def _get_table_for_ddl(self, name) -> Table:
        if name not in self.tables:
            raise DdlError(f"Table '{name}' doesn't exist", errno=1146)
        return self.tables[name]

    def alter_add_column(self, name: str, col: dict) -> dict:
        """`ALTER TABLE t ADD COLUMN c <type> [DEFAULT ..] [NOT NULL]`

        对**已有行**按 MySQL 语义回填：

        * 声明了 `DEFAULT` → 用该默认值（写入前按列类型转换一次，
          之后所有老行拿到的是同一个具体值，而不是"每次读的时候再求值"）；
        * 没声明 `DEFAULT` 且允许 NULL → NULL；
        * `NOT NULL` 且没有 `DEFAULT` 且表非空 → **显式报错 1138**
          （MySQL 8.0 严格模式的 `Invalid use of NULL value`），
          **绝不**静默填 0 / '' / NULL —— 那是"看起来加上了、数据却是编的"。
        """
        tbl = self._get_table_for_ddl(name)
        col = dict(col or {})
        cname = str(col.get('name') or '')
        if not cname:
            raise DdlError("ADD COLUMN 缺少列名", errno=1064)
        if any(c.get('name') == cname for c in tbl.cols):
            raise DdlError(f"Duplicate column name '{cname}'", errno=1060)
        if col.get('primary'):
            # MySQL 1068：一张表只能有一个主键。`ADD COLUMN c INT PRIMARY KEY`
            # 在 MySQL 里就是这条错误（不是静默忽略 PRIMARY KEY 子句）。
            raise DdlError("Multiple primary key defined", errno=1068)

        notnull = bool(col.get('notnull'))
        if col.get('has_default'):
            fill = tbl._column_default(col)
            if fill is None and notnull:
                raise DdlError(
                    f"列 '{cname}' 声明了 NOT NULL 但 DEFAULT 是 NULL："
                    f"定义本身矛盾", errno=1138, sqlstate='22004')
        elif notnull:
            if tbl.row_count:
                raise DdlError(
                    f"列 '{cname}' 声明了 NOT NULL 却没有 DEFAULT，表 "
                    f"{name} 中已有 {tbl.row_count} 行：无法确定这些行该"
                    f"填什么（MySQL 严格模式同样拒绝），本引擎不会静默填 "
                    f"0/''/NULL。请给出 DEFAULT 或允许 NULL 后重试",
                    errno=1138, sqlstate='22004')
            fill = None
        else:
            fill = None
        if fill is not None:
            try:
                fill = coerce(fill, col.get('type'))
            except (ValueError, TypeError) as e:
                raise DdlError(
                    f"DEFAULT 取值 {fill!r} 无法转换为 {col.get('type')}: {e}",
                    errno=1366, sqlstate='22007')

        # `ADD COLUMN email ... UNIQUE DEFAULT 'x'`：已有行会全部回填成
        # 同一个非 NULL 值，唯一性从第一天起就不成立。定义期直接报 1062，
        # 而不是先把列加上、等到下一次写入才发现"声明没生效"。
        if col.get('unique') and fill is not None:
            live = len(tbl._live_rows())
            if live > 1:
                raise DdlError(
                    f"Duplicate entry '{fill}' for key '{cname}'"
                    f"（ADD COLUMN ... UNIQUE 会把 {live} 行已有数据回填成"
                    f"同一个非 NULL 值，唯一性无法成立；请改用允许 NULL 的"
                    f"默认值或先清洗数据）",
                    errno=1062, sqlstate='23000')

        old_cols = [dict(c) for c in tbl.cols]
        new_cols = old_cols + [col]
        entry = {'op': 'ADD COLUMN', 'table': name,
                 'old_cols': old_cols, 'new_cols': new_cols,
                 'fill': fill}
        # ★ 全程持有**表级锁**：`insert/update/delete` 都在这个锁里写行，
        #   因此"检查未提交事务 → 退休 WAL → 改写所有行 → 换列定义"这一段
        #   不会被并发的写入插进来（插进来就会写下一行**旧列数**的数据，
        #   与刚换上的新列定义错位）。语义上等价于 MySQL 的元数据锁把 DDL
        #   挡在后面；代价是 DDL 期间该表的写入被阻塞。
        with tbl._lock:
            self._ddl_guard(tbl, 'ALTER TABLE ... ADD COLUMN')
            # 先把 WAL 退休（里头的行记录是**旧列数**的，不能留到列定义换掉
            # 之后；否则一次崩溃重放就会插回一行旧结构）。这一步必须在
            # `_ddl_begin` **之前**：checkpoint 失败时我们要能"干净地什么都不
            # 做"，一旦先写了 pending 记录，恢复端就会把这条被报错的 DDL 补完。
            self._ddl_retire_wal(tbl, 'ADD COLUMN')
            self._ddl_begin(entry)
            stats = tbl.rewrite_row_payloads(
                lambda values: list(values) + [fill],
                len(old_cols), len(new_cols))
            tbl.cols = new_cols
            tbl.refresh_schema_meta()
            self._ddl_commit(tbl)
        return {'rows_backfilled': stats['rows'], 'backfill': fill, **stats}

    def alter_drop_column(self, name: str, colname: str) -> dict:
        """`ALTER TABLE t DROP COLUMN c`

        * 被删列的数据必须**真的不可再读到**：所有行版本（含墓碑与老版本）
          的载荷都去掉该字段，被腾空的槽位与行尾残字节一律**清零**
          （直接读 `.dat` 也读不回来）；
        * 主键 / `UNIQUE` 列、被 CHECK 引用的列、分区列、全文索引列
          **一律显式报错**（MySQL 会连带删索引，本引擎不做连带操作，
          因此拒绝而不是静默破坏索引）。
        """
        tbl = self._get_table_for_ddl(name)
        names = [c.get('name') for c in tbl.cols]
        if colname not in names:
            raise DdlError(f"Unknown column '{colname}' in '{name}'",
                           errno=1054, sqlstate='42S22')
        if len(tbl.cols) <= 1:
            raise DdlError(
                f"Can't remove all the columns from table '{name}'",
                errno=1090)
        idx = names.index(colname)
        col = tbl.cols[idx]
        if col.get('primary'):
            raise DdlError(
                f"Can't DROP '{colname}'; 该列是表 {name} 的主键，"
                f"删除它会破坏主键索引（本引擎不支持连带删除主键）",
                errno=1091, sqlstate='42000')
        if col.get('unique'):
            raise DdlError(
                f"Can't DROP '{colname}'; 该列声明了 UNIQUE，"
                f"删除它会破坏唯一性约束（本引擎不支持连带删除约束）",
                errno=1091, sqlstate='42000')
        for uspec in tbl.unique_indexes():
            if colname in uspec['columns']:
                raise DdlError(
                    f"Can't DROP '{colname}'; 该列属于 UNIQUE 约束 "
                    f"'{uspec['name']}'，删除它会破坏唯一性"
                    f"（本引擎不支持连带删除约束）",
                    errno=1091, sqlstate='42000')
        # 被 CHECK 引用的列（MySQL 的 3959 就是这个场景）
        from ..sql.checks import check_columns, parse_check_predicate
        for spec in tbl.check_constraints():
            try:
                used = check_columns(parse_check_predicate(spec['expr']))
            except Exception:                        # pragma: no cover
                used = []
            if colname in used:
                raise DdlError(
                    f"Check constraint '{spec.get('name') or ''}' uses "
                    f"column '{colname}', hence column cannot be dropped "
                    f"or renamed.", errno=3959, sqlstate='HY000')
        spec = getattr(tbl, '_partition_spec', None) or {}
        if spec.get('col') == colname:
            raise DdlError(
                f"Can't DROP '{colname}'; 该列是表 {name} 的分区列"
                f"（分区定义会因此失效）", errno=1091, sqlstate='42000')
        if tbl.has_fulltext_index(colname):
            raise DdlError(
                f"Can't DROP '{colname}'; 该列上有全文索引，删除列会让"
                f"倒排索引引用一个不存在的列（请先重建全文索引）",
                errno=1091, sqlstate='42000')

        old_cols = [dict(c) for c in tbl.cols]
        new_cols = [dict(c) for c in tbl.cols if c.get('name') != colname]
        entry = {'op': 'DROP COLUMN', 'table': name, 'at': idx,
                 'old_cols': old_cols, 'new_cols': new_cols}
        with tbl._lock:                      # 与 ADD COLUMN 同一套并发边界
            self._ddl_guard(tbl, 'ALTER TABLE ... DROP COLUMN')
            self._ddl_retire_wal(tbl, 'DROP COLUMN')
            self._ddl_begin(entry)
            stats = tbl.rewrite_row_payloads(
                lambda values: list(values)[:idx] + list(values)[idx + 1:],
                len(old_cols), len(new_cols), zero_removed=True)
            tbl.cols = new_cols
            tbl.refresh_schema_meta()
            self._ddl_commit(tbl)
        return {'column': colname, 'rows_rewritten': stats['rows'], **stats}

    def truncate_table(self, name: str) -> dict:
        """`TRUNCATE TABLE t` —— 清空所有行，**保留表定义**

        与 `DELETE FROM t` 的区别（MySQL 语义）：TRUNCATE 是 DDL
        （隐式提交、不可回滚、重置自增、清空全部行而不逐行写 undo），
        DELETE 是 DML（可回滚、逐行删除、不重置自增）。本引擎的
        `Table.truncate_all` 真的把每个行槽位清零、丢弃索引项并复位
        `_next_pk`，不是"标个标记"。
        """
        tbl = self._get_table_for_ddl(name)
        cols = [dict(c) for c in tbl.cols]
        entry = {'op': 'TRUNCATE', 'table': name,
                 'old_cols': cols, 'new_cols': cols}
        with tbl._lock:
            self._ddl_guard(tbl, 'TRUNCATE TABLE')
            self._ddl_retire_wal(tbl, 'TRUNCATE')
            self._ddl_begin(entry)
            stats = tbl.truncate_all()
            self._ddl_commit(tbl)
        return stats

    def rename_table(self, old: str, new: str) -> dict:
        """`ALTER TABLE t RENAME TO t2`

        只改数据文件名与 `_meta.json` 里的键，**不动**任何行数据：
        `.dat` / `.btroot` / `.wal` / `.free` 四个文件一起 `os.replace`
        （同目录内改名是原子的），元数据最后原子替换。中途崩溃时
        `pending_ddl` 里记着这次改名，重启后把还没改的文件补改完。
        """
        if old not in self.tables:
            raise DdlError(f"Table '{old}' doesn't exist", errno=1146)
        if new in self.tables:
            raise DdlError(f"Table '{new}' already exists", errno=1050)
        if not re.match(r'^[A-Za-z_]\w*$', str(new or '')):
            raise DdlError(
                f"Incorrect table name '{new}'（只支持 [A-Za-z_]\\w*）",
                errno=1103, sqlstate='42000')
        tbl = self.tables[old]
        self._ddl_guard(tbl, 'ALTER TABLE ... RENAME')
        cols = [dict(c) for c in tbl.cols]
        entry = {'op': 'RENAME', 'table': old, 'new_name': new,
                 'old_cols': cols, 'new_cols': cols,
                 'partition': getattr(tbl, '_partition_spec', None),
                 # 表级 UNIQUE 约束跟着改名走：漏了它，改名后唯一性
                 # 就悄悄失效（声明还在，强制没了）。
                 'unique_indexes': tbl.unique_indexes(),
                 'checks': [dict(c) for c in getattr(tbl, '_checks', [])]}
        self._ddl_begin(entry)
        self._finish_rename(entry)
        return {'from': old, 'to': new}

    def _finish_rename(self, entry: dict):
        """完成（或补完）一次改名；幂等，崩溃恢复直接复用这一份实现"""
        old = entry.get('table')
        new = entry.get('new_name')
        if not old or not new:
            self._pending_ddl = None
            self.save_meta()
            return
        if new in self.tables or old not in self.tables:
            # 已经把元数据换过去了（或表已不存在）→ 只清标记
            self._pending_ddl = None
            self.save_meta()
            return
        tbl = self.tables.pop(old)
        try:
            tbl.close()
        except Exception as e:                       # pragma: no cover
            logger.warn(f"改名时关闭表 {old} 失败: {e}")
        for suffix in ('.dat', '.btroot', '.wal', '.free'):
            src = self.data_dir / f"{old}{suffix}"
            dst = self.data_dir / f"{new}{suffix}"
            try:
                if src.exists():
                    os.replace(str(src), str(dst))
            except OSError as e:
                logger.warn(f"改名 {src.name} -> {dst.name} 失败: {e}")
                raise DdlError(
                    f"表 {old} 改名为 {new} 时文件改名失败: {e}", errno=1105)
        newtbl = Table(new, entry.get('new_cols') or tbl.cols,
                       str(self.data_dir), self.lock_manager, self.mvcc,
                       unique_indexes=(entry.get('unique_indexes')
                                       if entry.get('unique_indexes')
                                       is not None
                                       else tbl.unique_indexes()))
        if entry.get('partition'):
            newtbl.set_partition(entry['partition'])
        if entry.get('checks'):
            newtbl.set_checks(entry['checks'])
        self.tables[new] = newtbl
        self._pending_ddl = None
        self.save_meta()

    def _recover_pending_ddl(self):
        """补完崩溃前**未完成**的结构变更（幂等；见 `__init__` 的说明）

        判据只有一条：`_meta.json` 里还有 `pending_ddl`。有它就说明上次的
        结构变更没有走到原子提交点，因此这里把它**做完**（roll forward）：

        * 行载荷的改写按"长度"幂等 —— 已经是新列数的行原样保留，
          还是旧列数的行补改，所以重复执行/执行到一半再崩都安全；
        * 完成后同样走 `_ddl_commit`（落盘 + 清标记 + 原子换元数据），
          于是重启后的状态与"执行成功"逐字一致。

        任何异常都**不**吞掉：只记 error 并保留 pending 记录（下次启动
        再试），绝不把"没补完"当成"补完了"。
        """
        entry = self._pending_ddl
        if not entry:
            return
        op = str(entry.get('op') or '')
        tname = entry.get('table')
        try:
            if op == 'RENAME':
                logger.warn(f"崩溃恢复：补完未完成的 RENAME {tname} -> "
                            f"{entry.get('new_name')}")
                self._finish_rename(entry)
                return
            if tname not in self.tables:
                logger.error(
                    f"pending DDL 指向不存在的表 {tname}（op={op}）—— "
                    f"丢弃该记录（表本身已不存在，没有可补的结构）")
                self._pending_ddl = None
                self.save_meta()
                return
            tbl = self.tables[tname]
            old_cols = entry.get('old_cols') or []
            new_cols = entry.get('new_cols') or []
            if op == 'ADD COLUMN':
                fill = entry.get('fill')
                tbl.rewrite_row_payloads(
                    lambda values: list(values) + [fill],
                    len(old_cols), len(new_cols))
            elif op == 'DROP COLUMN':
                at = int(entry.get('at'))
                tbl.rewrite_row_payloads(
                    lambda values: list(values)[:at] + list(values)[at + 1:],
                    len(old_cols), len(new_cols), zero_removed=True)
            elif op == 'TRUNCATE':
                tbl.truncate_all()
            else:
                logger.error(f"无法识别的 pending DDL {op!r} —— 丢弃该记录")
                self._pending_ddl = None
                self.save_meta()
                return
            if new_cols:
                tbl.cols = new_cols
            tbl.refresh_schema_meta()
            logger.warn(f"崩溃恢复：补完未完成的结构变更 {op} on {tname}")
            self._ddl_commit(tbl)
        except Exception as e:
            logger.error(
                f"补完未完成的结构变更失败（保留 pending 记录，"
                f"下次启动重试）: {op} on {tname}: {e}")

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

    def commit_statement(self, txn):
        """**语句级内部事务**的提交（autocommit 档持久性）

        给 `executor._cmd_insert` 的多值 INSERT 用：没有用户显式事务时，
        整条语句跑在一个引擎内部事务里，成功才走到这里。与
        `commit(txn)` 的差别，两处，都是有意的：

        1) 只写 COMMIT 记录并 flush 到 OS（`wal.commit_autocommit`），
           **不 fsync**。理由：逐行 autocommit 路径的持久性档位就是
           "flush 到 OS、不 fsync"（`Table._cleanup_stmt` →
           `wal.commit_async`，见 Stage 2.1 的说明：每条 autocommit
           INSERT 都 fsync 一次，3000 行要 200s+）。语句级提交若改 fsync，
           等于**单方面加强**多值 INSERT 的持久性、同时把批量收益吃掉：
           实测 `Database.commit` 占多值 INSERT 总耗时的 31%（500 行/语句，
           `Table.flush` 0.25s + `WAL.commit` fsync 0.28s / 1.78s）。
        2) **不刷脏页**（不调 `tbl.flush()`）：页落盘交给阈值 checkpoint /
           `close()` / 显式 COMMIT 这三条既有路径。WAL-before-data 不受影响
           —— 每条行记录在改页之前就已经 `flush_to_os`，崩溃恢复能按 WAL
           重放或撤销。
        """
        tid = txn['txn_id']
        for tbl in self.tables.values():
            try:
                tbl.wal.commit_autocommit(tid)
            except Exception as e:
                logger.warn(f"语句级 COMMIT 写入失败 {tbl.name}: {e}")
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
        版本回收入口：删除"对所有活跃快照都不可见"的旧版本/墓碑，
        ★ 并做**页级回收**（阶段 2 第 3 项）：截断 `.dat` 尾部完全空置的页、
        把剩余空闲页登记到**持久化**的 `<name>.free`（重启后仍复用）。

        horizon 取"所有表的活跃快照边界"的下界
        （`Table.vacuum_horizon()`：最小活跃事务号 ∩ 各活跃读视图 max_id）：
        对任何活跃读视图都成立的判据只有"xmax 严格小于它的 max_id"，
        所以这里把**跨表**的最小值传下去 —— 单表各自算的话，
        后面那张表的 vacuum 会把还在被前面表快照依赖的版本判成可回收。

        注意: 只回收已提交淘汰的版本；未提交事务写的版本 xmax 是
        活跃事务号（>= horizon），不会被误删（判定是严格小于）。
        返回回收的版本数。页级统计见 `vacuum_stats()`。

        ★ autocommit 的墓碑（xmax 是合成高位号）不受 `horizon` 约束
        （合成号与真实事务号不可比），但**同样**必须过一个不可被调用方
        覆盖的"活跃读视图还看得见吗"的上界 —— 否则 `max_id <= xmax` 的
        老快照会从"读到被删的旧值"变成"行不存在"（实测: 插 200 行 →
        老快照 → autocommit 全删 → vacuum → 老快照读到 0/200）。
        详见 `Table.vacuum` 的判定、`Table._vacuum_bounds()` 与
        `Table._min_active_view_max_id()`。

        ★★ 阶段 2.3 第①步: 页级回收只保留**确定性收益**
        （版本回收 + 槽位置零 + 尾部纯空段截断 + 空闲页持久化）；
        `ENABLE_PAGE_RELOCATION` / `ENABLE_BTREE_REBUILD` 仍为 False，
        本步**不宣称** `.dat` 缩小（`compacted_bytes` 不是 `bytes_freed`）。
        """
        if horizon is None:
            horizon = self.mvcc.oldest_active_txn()
            for tbl in self.tables.values():
                try:
                    h = tbl._vacuum_horizon()
                    if h < horizon:
                        horizon = h
                except Exception:
                    pass
        total = 0
        for tbl in self.tables.values():
            try:
                total += tbl.vacuum(horizon)
            except Exception as e:
                logger.warn(f"vacuum {tbl.name} 失败: {e}")
        return total

    def vacuum_stats(self) -> dict:
        """所有表最近一次 vacuum 的页级统计汇总（供 `VACUUM` 结果集/巡检）"""
        out = {}
        for name, tbl in self.tables.items():
            try:
                out[name] = tbl.vacuum_stats()
            except Exception:
                out[name] = {}
        return out

    # ==================== checkpoint（WAL 轮转）====================

    def checkpoint(self, force: bool = False) -> int:
        """
        显式 checkpoint：把所有表的脏页安全落盘后退休 WAL 段落。

        每张表独立处理（`Table.checkpoint`）：
        - 无未提交事务 ⇒ 脏页 fsync → BTree 根 fsync → WAL 截断；
        - 该表上有未提交的显式事务 ⇒ **跳过**（返回不计入），
          因为它的脏页不能刷、它的 WAL 旧行镜像（undo 用）不能丢。

        返回值 = 真正完成 checkpoint 的表数。幂等：重复调用不会报错，
        也不会丢数据（WAL 已空时只是再刷一次页）。
        """
        n = 0
        for tbl in self.tables.values():
            try:
                if tbl.checkpoint(force=force):
                    n += 1
            except Exception as e:
                logger.warn(f"checkpoint {tbl.name} 失败: {e}")
        return n

    def shutdown(self):
        # 审计器的待落盘批：正常停机路径上**先刷盘**（审计批量 flush 的
        # 确定性落盘点），失败只告警，绝不影响停机的其它动作。
        for sink in list(getattr(self, '_audit_sinks', ()) or ()):
            try:
                sink.close()
            except Exception as e:                # pragma: no cover - 防御
                logger.warn(f"停机时审计落盘失败: {e}")
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

    def attach_audit_sink(self, sink):
        """登记一个审计器：`shutdown()` 时保证它的待落盘批被写出去"""
        if sink is None:
            return sink
        if not hasattr(self, '_audit_sinks') or self._audit_sinks is None:
            self._audit_sinks = []
        if sink not in self._audit_sinks:
            self._audit_sinks.append(sink)
        return sink

    def stats(self) -> dict:
        return {
            'tables': {
                n: t.stats() for n, t in self.tables.items()
            },
            'locks': self.lock_manager.stats(),
            'mvcc': self.mvcc.stats(),
        }