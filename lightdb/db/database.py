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
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
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


#: 自动生成的 root 明文口令文件名（位于数据目录内，权限尽量 0600）。
#: 只在"未显式提供口令"且确实生成了随机口令时创建；启动日志只打印路径。
ROOT_PASSWORD_FILE = 'root.password'


def _env_flag(name: str) -> bool:
    """解析布尔型环境变量（1/true/yes/on 视为真）"""
    return os.environ.get(name, '').strip().lower() in (
        '1', 'true', 'yes', 'on')


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


def resolve_secondary_indexes(table: str, cols: List[dict], specs,
                              reserved_names=()) -> List[dict]:
    """校验并规范化 **E2 非唯一索引**声明（`CREATE INDEX` / 内联 `KEY`）

    本引擎没有二级索引结构，但这不代表可以"声明了然后丢掉"：索引声明
    会持久化，`SHOW INDEX` 报 `Index_type='SCAN-ASSIST'`（不谎称 BTREE），
    `SHOW CREATE TABLE` 会写出 `KEY 名字 (列...)`（可被本引擎重新解析）。
    定义期拒绝坏声明：未知列 1072 / 重名 1061 / 同索引重复列 1060 /
    非法名字 1064（名字要能写进 DDL 原样读回）。
    """
    names = [c.get('name') for c in cols]
    out = []
    seen = {str(n).lower() for n in (reserved_names or ()) if n}
    for spec in (specs or []):
        if not isinstance(spec, dict):
            raise DdlError(
                f"表 {table} 的索引声明格式错误: {spec!r}", errno=1064)
        columns = [str(c) for c in (spec.get('columns') or [])]
        if not columns:
            raise DdlError(
                f"表 {table} 的索引声明没有列清单", errno=1064)
        if len(set(columns)) != len(columns):
            raise DdlError(
                f"表 {table} 的索引声明列清单里有重复列: {columns}",
                errno=1060)
        for cname in columns:
            if cname not in names:
                raise DdlError(
                    f"Key column '{cname}' doesn't exist in table "
                    f"'{table}'", errno=1072, sqlstate='42022')
        name = str(spec.get('name') or columns[0]).strip()
        if not _UNIQUE_NAME_RE.match(name):
            raise DdlError(
                f"表 {table} 的索引名 {name!r} 含不支持的字符"
                f"（只支持 [A-Za-z_]\\w*；否则 SHOW CREATE TABLE 无法往返）",
                errno=1064)
        low = name.lower()
        if low == 'primary' or low in seen:
            raise DdlError(f"Duplicate key name '{name}'", errno=1061,
                           sqlstate='42000')
        seen.add(low)
        out.append({'name': name, 'columns': columns, 'unique': False})
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
    """Extra 列：`auto_increment` 是 P6 起**真实实现**的自增列。

    只有列上真的声明了 `AUTO_INCREMENT`（且建表期通过了"整数 + 键"校验）
    才报 `auto_increment`；主键本身**不会**被推断成自增（那才是伪造元数据：
    普通 INT 主键的"省略主键取值"用的是内部行键兜底，不是自增语义）。
    """
    if col.get('auto_increment'):
        return 'auto_increment'
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
    def __init__(self, data_dir: str, name: str = DEFAULT_DATABASE,
                 root_password: str = None,
                 allow_empty_password: bool = None):
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
        #: E3：视图定义（`CREATE VIEW`）。只存定义（名字 / SELECT 文本 /
        #: 可选列别名），不落盘数据；随 `_meta.json` 持久化，重启后仍在。
        #: SELECT 展开在执行器（`Executor._select_from_view`），等价于把
        #: 视图查询物化后做外层 WHERE/聚合/排序。
        self.views: Dict[str, dict] = {}
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
        #
        # 注意：这是**旧的演示实现**（JSONL、无 CRC、无事务边界）。真复制
        # 日志是下面的 `binlog_log`（append-only + CRC + 组提交），
        # `SHOW MASTER STATUS` / `SHOW BINLOG EVENTS` / `__BINLOG_FETCH__`
        # 与从库复制器都只读那份；`self.binlog` 仅为兼容旧调用方保留。
        self._binlog = None

        # ★ 真 · append-only binlog（决策 D 项）：
        #   - 惰性创建：只有真正发生写/查复制位点的 Database 才会打开文件；
        #   - 追加写、位点=真实字节偏移、只增不回退（不依赖表 WAL 轮转）；
        #   - 提交钩子见 `commit()` / `commit_statement()` / `abort()`：
        #     DML 行事件由执行器按事务/语句组缓冲，**提交时才**成组落盘。
        self._binlog_log = None
        self._binlog_log_lock = threading.RLock()
        #: 重放从库事件时暂停本机 binlog 记录（避免从库把重放再记一遍）
        self._binlog_suspend_depth = 0
        #: binlog 记录失败计数（不静默：失败会告警并在 stats 里可见）
        self._binlog_errors = 0

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

        # P5：COMPACT 在 `_load()` 之前先把"崩溃中断的多文件替换"补完，
        # 否则随后 `_load`/`_recover` 会看到新旧文件混在一起的状态。
        self._pre_recover_compact()
        self._load()
        # 认证引导必须紧跟在用户表加载之后：新库要生成随机口令，旧数据目录
        # （root 仍为空口令）要在启动时安全迁移 —— 否则"升级后默认不再无认证"
        # 只是一句空话。配置来源：main.py/main_v4 传参 > 环境变量 > 安全默认。
        self._ensure_root_credentials(root_password, allow_empty_password)
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
        for tbl in list(self.tables.values()):
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

    # ==================== 认证引导（安全默认） ====================

    def root_password_path(self) -> Path:
        """自动生成的 root 明文口令文件路径（仅"随机生成"模式下存在）"""
        return self.data_dir / ROOT_PASSWORD_FILE

    def _ensure_root_credentials(self, root_password=None,
                                 allow_empty_password=None):
        """保证 root 不会因为"默认值"而成为无口令账户。

        历史行为（已修复）：`users` 的初始 root `auth_hash` 是
        `hash_native_password('')`（空串），于是**默认部署 = 无认证**，
        任何能连到端口的人都能 `root/''` 登录。现在的优先级（高 → 低）：

        1. **显式口令**：参数 `root_password` / 环境变量
           `LIGHTDB_ROOT_PASSWORD` / CLI `--root-password`。已存在的库同样
           以显式值为准（容器化部署常把环境变量当唯一事实来源）。
        2. 库里已有**非空** root 口令：保持不动。绝不悄悄轮换用户自己
           设置过的口令。
        3. **显式允许空口令**：参数 `allow_empty_password` / 环境变量
           `LIGHTDB_ALLOW_EMPTY_PASSWORD=1` / CLI `--allow-empty-password`
           → 保持空口令并打 WARN（仅限本地开发/演示的降级开关）。
        4. **安全默认**：生成 `secrets.token_urlsafe(16)` 随机口令，
           更新 `_meta.json` 的 users，并把明文写入
           `<data_dir>/root.password`（权限尽量 0600）。启动日志**只打印
           文件路径**，绝不打印明文。

        第 4 条同时覆盖**旧数据目录迁移**：`_meta.json` 里 root 的
        `auth_hash` 为空、又没显式允许空口令时，启动即换成随机口令并落盘，
        保证"升级后默认不再无认证"是真实生效的。
        """
        #: 是否由调用方（CLI/服务端配置）显式传入。env 是测试/运维的隐式降级
        #: 通道：只允许空口令、但**不打印 WARN**——大量既有用例的子进程会把
        #: stdout 当协议数据解析（如 `rows_written=2`、JSON），日志行会污染它们。
        #: 显式传入时（main.py 的 `--allow-empty-password`/配置）仍然 WARN。
        explicit_allow_empty = allow_empty_password is not None
        if allow_empty_password is None:
            allow_empty_password = _env_flag('LIGHTDB_ALLOW_EMPTY_PASSWORD')
        if root_password is None:
            root_password = os.environ.get('LIGHTDB_ROOT_PASSWORD') or ''
        explicit = str(root_password) if root_password else ''

        root = self.users.get('root')
        if not isinstance(root, dict):
            root = {}
            self.users['root'] = root
        root.setdefault('privs', ['ALL'])

        current_hash = str(root.get('auth_hash') or '')
        # 空串（旧默认值）与历史遗留的全零 20 字节都视为"无口令"。
        is_empty = current_hash in ('', '00' * 20)

        # 1) 显式口令优先（新库设置 / 已有库以显式值为准）
        if explicit:
            wanted = hash_native_password(explicit)
            if wanted != current_hash:
                root['auth_hash'] = wanted
                self.save_meta()
                logger.warn(
                    "root 口令已由 LIGHTDB_ROOT_PASSWORD / --root-password "
                    "设置（启动日志不打印明文）")
            return

        # 2) 用户自己设置过非空口令：保持不动
        if not is_empty:
            return

        # 3) 显式允许空口令：旧的不安全行为，仅本地开发/演示
        if allow_empty_password:
            root['auth_hash'] = ''
            if explicit_allow_empty:
                # 只有「调用方显式选择」才打 WARN；env-only 降级（测试环境）
                # 保持静默，避免污染子进程 stdout（见上方 explicit_allow_empty 注释）。
                logger.warn(
                    "已显式允许 root 空口令（LIGHTDB_ALLOW_EMPTY_PASSWORD=1 / "
                    "--allow-empty-password）：仅限本地开发/演示，"
                    "生产环境禁止使用")
            return

        # 4) 安全默认：生成随机口令并落盘（新库与旧空口令库迁移共用）
        generated = secrets.token_urlsafe(16)
        root['auth_hash'] = hash_native_password(generated)
        self.save_meta()
        path = self._write_root_password_file(generated)
        logger.warn(
            f"root 账户未配置口令：已生成随机口令并写入 {path}"
            f"（权限尽量 0600；日志不打印明文）。"
            f"首次登录请读取该文件；也可用 LIGHTDB_ROOT_PASSWORD / "
            f"--root-password 显式指定")

    def _write_root_password_file(self, password: str) -> Path:
        """把自动生成的 root 明文口令写入数据目录（权限尽量 0600）"""
        path = self.root_password_path()
        path.write_text(password + '\n', encoding='utf-8')
        try:
            os.chmod(path, 0o600)   # Windows 上基本是 no-op，失败忽略
        except OSError:
            pass
        return path

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
                    # E2：非唯一索引声明 + 请求的引擎名（兼容别名）
                    # 必须跨重启保留：SHOW INDEX/SHOW CREATE/TABLE STATUS
                    # 否则会在重启后"变回"没建过索引/LightDB 引擎。
                    secondary_indexes=tinfo.get('indexes'),
                    requested_engine=(tinfo.get('requested_engine')
                                      or 'LightDB'),
                    # P6：AUTO_INCREMENT 下一号随元数据持久化；重启后
                    # 接着分配（不因删行/回滚而重用旧号）。
                    auto_increment_next=tinfo.get('auto_increment_next'),
                )
                if tinfo.get('partition'):
                    # 分区描述随表元数据一起持久化（SHOW PARTITIONS /
                    # 分区裁剪都读 Table._partition）
                    self.tables[tname].set_partition(tinfo['partition'])
                if tinfo.get('checks'):
                    # 表级 CHECK 约束随元数据回来（重启后仍生效）
                    self.tables[tname].set_checks(tinfo['checks'])
                # E1：表选项/表注释（ENGINE/CHARSET/COLLATE/COMMENT）随
                # 元数据持久化，SHOW CREATE TABLE / SHOW TABLE STATUS 在
                # 重启后仍如实回显；旧 _meta.json 没有该键 → 用默认值。
                self.tables[tname].table_options = tinfo.get(
                    'table_options') or {
                        'engine': 'LightDB', 'charset': 'utf8mb4',
                        'collate': 'utf8mb4_bin', 'comment': None}
                self.tables[tname].comment = (
                    tinfo.get('comment')
                    or self.tables[tname].table_options.get('comment')
                    or '')
            if 'users' in meta:
                self.users = meta['users']
            # E3：视图定义随元数据恢复（重启后 SHOW FULL TABLES/CREATE/
            # SELECT 仍可用；损坏条目丢弃并告警，绝不让半个视图进入目录）。
            loaded_views = meta.get('views') or {}
            if isinstance(loaded_views, dict):
                for vname, vinfo in loaded_views.items():
                    if not isinstance(vinfo, dict):
                        continue
                    sql = str(vinfo.get('sql') or '').strip()
                    if not sql:
                        continue
                    self.views[str(vname)] = {
                        'sql': sql,
                        'columns': [str(c) for c in
                                    (vinfo.get('columns') or [])],
                    }
            self._pending_ddl = meta.get('pending_ddl')
        except Exception as e:
            logger.error(f"加载元数据失败: {e}")

    def _recover(self):
        total = 0
        for tbl in list(self.tables.values()):
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
                    'checks': getattr(t, '_checks', None),
                    # E2：非唯一索引声明 + 请求的引擎名（兼容别名）。
                    # 都是真实生效/回显的元数据，必须随元数据持久化。
                    'indexes': t.secondary_indexes()
                    if hasattr(t, 'secondary_indexes') else [],
                    'requested_engine': getattr(t, 'requested_engine',
                                                'LightDB'),
                    # E1：表选项/注释（CREATE TABLE 的 ENGINE/CHARSET/
                    # COLLATE/COMMENT 真实生效并回显，重启后保持）。
                    'table_options': getattr(t, 'table_options', None),
                    'comment': getattr(t, 'comment', ''),
                    # P6：AUTO_INCREMENT 每列"下一个待分配号"（真实计数，
                    # 重启后保持；回滚/删除不回收）
                    'auto_increment_next': (
                        t.auto_increment_next_values()
                        if hasattr(t, 'auto_increment_next_values') else {})}
                for n, t in list(self.tables.items())
            },
            'users': self.users,
            # E3：视图定义（名字 → {'sql','columns'}）。不导出数据，
            # 只持久化定义，SELECT 时展开到底层查询。
            'views': self.views,
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
                     unique_indexes: List[dict] = None,
                     secondary_indexes: List[dict] = None,
                     requested_engine: str = None,
                     auto_increment_start: int = None) -> Table:
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
        # P6：AUTO_INCREMENT 定义期校验（绝不静默降级）
        #   * 只允许一列自增（MySQL 1075）；
        #   * 必须是整数类型（1063）；
        #   * 必须是**显式声明的键**（主键 / 列级 UNIQUE / 表级唯一约束覆盖），
        #     parser 给"第一列"打的隐式内部行键标记不算声明（1075）。
        auto_cols = [c for c in cols if c.get('auto_increment')]
        if len(auto_cols) > 1:
            raise DdlError(
                "Incorrect table definition; there can be only one auto "
                "column and it must be defined as a key", errno=1075,
                sqlstate='42000')
        if auto_cols:
            ac = auto_cols[0]
            ac_name = str(ac.get('name') or '')
            if not Table.is_auto_increment_integer_type(ac.get('type')):
                raise DdlError(
                    f"Incorrect column specifier for column '{ac_name}'"
                    f"（AUTO_INCREMENT 只支持整数类型 INT/BIGINT 等，"
                    f"声明为 {ac.get('type')!r} 不会生效，因此显式拒绝）",
                    errno=1063, sqlstate='42000')
            unique_covered = set()
            for spec in unique_specs:
                unique_covered.update(spec.get('columns') or [])
            explicit_key = (
                (bool(ac.get('primary')) and not ac.get('implicit_pk'))
                or bool(ac.get('unique')) or ac_name in unique_covered)
            if not explicit_key:
                raise DdlError(
                    "Incorrect table definition; there can be only one auto "
                    "column and it must be defined as a key",
                    errno=1075, sqlstate='42000')
            if ac.get('generated'):
                raise DdlError(
                    f"AUTO_INCREMENT 不能与生成列同时声明（列 '{ac_name}'）",
                    errno=1063, sqlstate='42000')
            # MySQL：AUTO_INCREMENT 列隐式 NOT NULL（插入时由引擎填号，
            # 不存在"自增但允许 NULL"的语义）。
            ac['notnull'] = True
        # E2：非唯一 KEY/INDEX 声明（CREATE TABLE 内联）与独立 CREATE INDEX
        # 共用同一份持久化结构；名字不得与 PRIMARY/UNIQUE 冲突。
        reserved = [s['name'] for s in unique_specs]
        secondary_specs = resolve_secondary_indexes(
            name, cols, secondary_indexes, reserved_names=reserved)
        tbl = Table(
            name, cols, str(self.data_dir),
            self.lock_manager, self.mvcc,
            unique_indexes=unique_specs,
            secondary_indexes=secondary_specs,
            requested_engine=requested_engine or 'LightDB',
            auto_increment_start=auto_increment_start)
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
        for suffix in ('.dat', '.btroot', '.wal', '.free', '.autoinc'):
            p = self.data_dir / f"{name}{suffix}"
            if p.exists():
                p.unlink()
        self.save_meta()

    def get_table(self, name: str) -> Table:
        if name not in self.tables:
            raise KeyError(f"表 {name} 不存在")
        return self.tables[name]

    # ==================== E3：视图（定义持久化 + 名字空间） ====================
    #
    # 视图只存定义（SELECT 文本 + 可选列别名），不存数据；`SELECT` 时由
    # 执行器物化定义查询再做外层过滤/聚合。名字空间与表共用（MySQL 里
    # 表与视图不能同名）：CREATE VIEW 遇到同名表显式 1050。

    def get_view(self, name):
        """按名字取视图定义（大小写不敏感）；不存在返回 None"""
        want = str(name or '').strip().lower()
        if not want:
            return None
        for vname, vinfo in list(self.views.items()):
            if vname.lower() == want:
                return {'name': vname, **dict(vinfo)}
        return None

    def list_views(self) -> List[str]:
        """全部视图名（排序稳定，供 SHOW 输出）"""
        return sorted(self.views.keys())

    def create_view(self, name: str, sql: str, or_replace: bool = False,
                    columns=None) -> dict:
        """登记视图定义；名字与表/视图冲突按 MySQL 语义显式报错"""
        name = str(name or '').strip()
        if not re.match(r'^[A-Za-z_]\w*$', name):
            raise DdlError(
                f"Incorrect view name '{name}'（只支持 [A-Za-z_]\\w*）",
                errno=1103, sqlstate='42000')
        if any(t.lower() == name.lower() for t in self.tables):
            raise DdlError(f"Table '{name}' already exists", errno=1050,
                           sqlstate='42S01')
        existing = self.get_view(name)
        if existing is not None and not or_replace:
            raise DdlError(f"Table '{name}' already exists", errno=1050,
                           sqlstate='42S01')
        real_name = existing['name'] if existing is not None else name
        self.views[real_name] = {
            'sql': str(sql or '').strip(),
            'columns': [str(c) for c in (columns or [])],
        }
        self.save_meta()
        return {'name': real_name,
                'sql': self.views[real_name]['sql'],
                'replaced': bool(existing)}

    def drop_view(self, name: str, if_exists: bool = False) -> dict:
        """删除视图；不存在时 IF EXISTS 成功、否则显式 1051"""
        view = self.get_view(name)
        if view is None:
            if if_exists:
                return {'dropped': False, 'name': str(name)}
            raise DdlError(
                f"Unknown table '{self.name}.{name}'", errno=1051,
                sqlstate='42S02')
        self.views.pop(view['name'], None)
        self.save_meta()
        return {'dropped': True, 'name': view['name']}

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
        if col.get('auto_increment'):
            # 不给已有行编造自增值：显式拒绝，绝不"加一列但已有行没有号"。
            raise DdlError(
                "ALTER TABLE ADD COLUMN ... AUTO_INCREMENT 暂不支持"
                "（已有行的回填语义：MySQL 会按顺序补号，本引擎不做半套）",
                errno=1235, sqlstate='42000')
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
        # E2：被非唯一索引引用的列同样不能直接删（MySQL 会连带删索引，
        # 本引擎不做连带操作 → 显式拒绝而不是留下悬空索引声明）。
        for ispec in (getattr(tbl, 'secondary_indexes', lambda: [])() or []):
            if colname in ispec.get('columns', []):
                raise DdlError(
                    f"Can't DROP '{colname}'; 该列属于索引 "
                    f"'{ispec['name']}'，删除它会留下悬空索"
                    f"引声明（请先 DROP INDEX）", errno=1091,
                    sqlstate='42000')
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

    # ==================== E2：ALTER MODIFY COLUMN ====================

    #: `MODIFY COLUMN` 支持的目标类型（与解析器归一化后的类型基名一致）
    _MODIFY_TARGET_TYPES = frozenset((
        'INT', 'BIGINT', 'TINYINT', 'SMALLINT', 'MEDIUMINT', 'INTEGER',
        'VARCHAR', 'CHAR', 'TEXT', 'MEDIUMTEXT', 'LONGTEXT', 'TINYTEXT',
        'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC', 'BOOLEAN', 'BOOL',
        'DATETIME', 'DATE', 'TIME', 'TIMESTAMP', 'JSON',
    ))
    #: 主键列允许的目标类型（主键 B+Tree 键按 uint32 存储）
    _PK_COMPATIBLE_TYPES = frozenset((
        'INT', 'BIGINT', 'TINYINT', 'SMALLINT', 'MEDIUMINT', 'INTEGER',
    ))

    @staticmethod
    def _base_type(col_type) -> str:
        m = re.match(r'^([A-Z_][A-Z0-9_]*)',
                     str(col_type or '').strip().upper())
        return m.group(1) if m else ''

    def _convert_existing_value(self, value, cname, old_type, new_type):
        """把已有值转换到新类型；**不可转换/会静默截断** → 显式 DdlError

        刻意比 MySQL 的非严格模式更严：`DOUBLE 1.5 → INT`、`VARCHAR(10)
        '12345' → VARCHAR(3)`、`'abc' → INT` 这类会改变数据的转换一律显式
        失败（1265/1406/1366），绝不静默截断 —— 本引擎没有 warning
        通道，静默改值等于数据损坏。
        """
        if value is None:
            return None
        try:
            conv = coerce(value, new_type)
        except Exception as e:
            raise DdlError(
                f"列 '{cname}' 的已有值 {value!r} 无法转换为 {new_type}"
                f"（不静默截断/丢数据）: {e}",
                errno=getattr(e, 'errno', 1366),
                sqlstate=getattr(e, 'sqlstate', 'HY000'))
        base = self._base_type(new_type)
        if base in ('VARCHAR', 'CHAR', 'TEXT', 'MEDIUMTEXT', 'LONGTEXT',
                    'TINYTEXT'):
            if isinstance(value, str) and conv != value:
                raise DdlError(
                    f"Data truncated for column '{cname}': {value!r} 无法"
                    f"无损转换为 {new_type}", errno=1265, sqlstate='01000')
        elif base in ('INT', 'BIGINT', 'TINYINT', 'SMALLINT', 'MEDIUMINT',
                      'INTEGER'):
            if isinstance(value, float) and conv != value:
                raise DdlError(
                    f"Data truncated for column '{cname}': {value!r} 无法"
                    f"无损转换为 {new_type}（拒绝静默取整）",
                    errno=1265, sqlstate='01000')
            if isinstance(value, str):
                try:
                    if float(value) != float(conv):
                        raise DdlError(
                            f"Data truncated for column '{cname}': "
                            f"{value!r} 无法无损转换为 {new_type}",
                            errno=1265, sqlstate='01000')
                except ValueError:
                    pass
        elif base in ('BOOLEAN', 'BOOL'):
            if value not in (0, 1, True, False, '0', '1',
                             'true', 'false', 'TRUE', 'FALSE'):
                raise DdlError(
                    f"Data truncated for column '{cname}': {value!r} 无法"
                    f"无损转换为 BOOLEAN", errno=1265, sqlstate='01000')
        return conv

    def _modify_transform(self, entry: dict):
        """`MODIFY COLUMN` 的行转换函数（执行路径与崩溃恢复共用同一份）"""
        idx = int(entry.get('at'))
        old_cols = entry.get('old_cols') or []
        new_cols = entry.get('new_cols') or []
        cname = str(new_cols[idx].get('name') or '')
        old_type = old_cols[idx].get('type')
        new_type = new_cols[idx].get('type')
        notnull = bool(new_cols[idx].get('notnull'))

        def transform(values):
            out = list(values)
            out[idx] = self._convert_existing_value(
                out[idx], cname, old_type, new_type)
            if notnull and out[idx] is None:
                raise DdlError(
                    f"Invalid use of NULL value（列 '{cname}' 声明 NOT NULL，"
                    f"但已有行是 NULL）", errno=1138, sqlstate='22004')
            return out
        return transform

    def _prevalidate_modify(self, tbl, entry: dict):
        """`MODIFY COLUMN` 落盘前的语义校验（转换/NOT NULL/CHECK/UNIQUE）

        在改写任何页之前跑：转换不可行、NOT NULL 冲突、转换后违反 CHECK、
        或转换后唯一性被破坏 → 显式错误且表结构/数据完全不变。
        """
        transform = self._modify_transform(entry)
        new_rows = []
        for pk, values in tbl._live_rows():
            conv = transform(values)          # 不可转换/NOT NULL → DdlError
            try:
                tbl._validate_checks(conv)
            except Exception as e:
                raise DdlError(
                    f"已有行（主键 {pk!r}）在 MODIFY 转换后违反 CHECK 约束: "
                    f"{e}", errno=getattr(e, 'errno', 3819),
                    sqlstate=getattr(e, 'sqlstate', 'HY000'))
            new_rows.append((pk, conv))
        for spec in tbl.unique_indexes():
            idxs = [tbl._col_idx(c) for c in spec['columns']]
            seen = set()
            for _pk, conv in new_rows:
                tup = tuple(conv[i] for i in idxs)
                if any(v is None for v in tup):
                    continue
                if tup in seen:
                    shown = '-'.join('NULL' if v is None else str(v)
                                     for v in tup)
                    raise DdlError(
                        f"Duplicate entry '{shown}' for key "
                        f"'{spec['name']}'（MODIFY 转换后唯一性被破坏）",
                        errno=1062, sqlstate='23000')
                seen.add(tup)

    def alter_modify_column(self, name: str, col: dict) -> dict:
        """`ALTER TABLE t MODIFY [COLUMN] c <type> [NULL|NOT NULL] [DEFAULT]`

        语义与边界：
        * 类型/长度、NOT NULL、DEFAULT 真实生效（`SHOW CREATE`/`SHOW COLUMNS`
          立即反映新定义）；
        * 已有全部行版本在**动手改写之前**先做一次转换试算
          （`rewrite_row_payloads` 的 planning 阶段），任何一行不可转换/
          会截断 → 显式错误且表结构与数据完全不变；
        * 主键/唯一/CHECK/注释等既有语义不会被"没写全的列定义"静默丢掉
          （MODIFY 只要求写新类型；主键、UNIQUE、生成列表达式、列注释在
          新定义缺省时保留）；
        * 与 ADD/DROP COLUMN 共用同一套 DDL 原子提交/崩溃恢复流程。
        """
        tbl = self._get_table_for_ddl(name)
        col = dict(col or {})
        cname = str(col.get('name') or '')
        names = [c.get('name') for c in tbl.cols]
        if cname not in names:
            raise DdlError(f"Unknown column '{cname}' in '{name}'",
                           errno=1054, sqlstate='42S22')
        idx = names.index(cname)
        old = tbl.cols[idx]
        new_type = str(col.get('type') or '')
        base = self._base_type(new_type)
        if base not in self._MODIFY_TARGET_TYPES:
            raise DdlError(
                f"MODIFY COLUMN 不支持把列 '{cname}' 改成 {new_type!r}"
                f"（支持 INT/BIGINT/VARCHAR/DOUBLE/DECIMAL/BOOLEAN/"
                f"DATETIME 等）", errno=1064)
        new = dict(old)
        new['type'] = new_type
        new['notnull'] = bool(col.get('notnull')) or bool(old.get('primary'))
        new['primary'] = bool(old.get('primary') or col.get('primary'))
        new['unique'] = bool(old.get('unique') or col.get('unique'))
        new['has_default'] = bool(col.get('has_default'))
        new['default'] = col.get('default')
        for key in ('generated', 'check', 'comment'):
            if col.get(key) is not None:
                new[key] = col.get(key)
        # P6.3：MODIFY 省略 AUTO_INCREMENT = **真正移除自增**（MySQL 语义：
        # MODIFY 给的是完整的新列定义，没写 AUTO_INCREMENT 就是没有）。
        # 旧实现用 `old or new` 静默保留，导致"去自增"永不生效 —— 正是本
        # 任务要修掉的静默语义。计数条目不清除：之后重新加回自增时从原
        # 计数继续（不复用旧号，见 `Table._sync_auto_columns`）。
        auto_removed = bool(old.get('auto_increment')
                            and not col.get('auto_increment'))
        auto_added = bool(col.get('auto_increment')
                          and not old.get('auto_increment'))
        new['auto_increment'] = bool(col.get('auto_increment'))
        if new['auto_increment']:
            if not Table.is_auto_increment_integer_type(new_type):
                raise DdlError(
                    f"AUTO_INCREMENT 列 '{cname}' 必须是整数类型，"
                    f"不能改成 {new_type}", errno=1063, sqlstate='42000')
            if new.get('generated'):
                raise DdlError(
                    f"AUTO_INCREMENT 不能与生成列同时声明（列 '{cname}'）",
                    errno=1063, sqlstate='42000')
            others = [c for j, c in enumerate(tbl.cols)
                      if j != idx and c.get('auto_increment')]
            if others:
                raise DdlError(
                    "Incorrect table definition; there can be only one auto "
                    "column and it must be defined as a key", errno=1075,
                    sqlstate='42000')
            # 必须是显式声明的键（主键 / 列级 UNIQUE / 表级唯一约束覆盖）
            unique_covered = set()
            for spec in tbl.unique_indexes():
                unique_covered.update(spec.get('columns') or [])
            explicit_key = (
                (bool(new.get('primary')) and not new.get('implicit_pk'))
                or bool(new.get('unique')) or cname in unique_covered)
            if not explicit_key:
                raise DdlError(
                    "Incorrect table definition; there can be only one auto "
                    "column and it must be defined as a key",
                    errno=1075, sqlstate='42000')
            # MySQL：AUTO_INCREMENT 列隐式 NOT NULL
            new['notnull'] = True
        if new.get('primary') and base not in self._PK_COMPATIBLE_TYPES:
            raise DdlError(
                f"主键列 '{cname}' 只能改成整数类型（主键 B+Tree 的键按 "
                f"uint32 存储），拒绝改成 {new_type}", errno=1064)
        if new.get('has_default') and new.get('default') is not None:
            # 默认值本身也必须能转成新类型（否则定义就是自相矛盾的）
            self._convert_existing_value(
                new['default'], cname, old.get('type'), new_type)
        old_cols = [dict(c) for c in tbl.cols]
        new_cols = [dict(c) for c in tbl.cols]
        new_cols[idx] = new
        entry = {'op': 'MODIFY COLUMN', 'table': name, 'at': idx,
                 'old_cols': old_cols, 'new_cols': new_cols}
        transform = self._modify_transform(entry)
        with tbl._lock:
            self._ddl_guard(tbl, 'ALTER TABLE ... MODIFY COLUMN')
            # 先做语义预校验（全部行转换试算 + CHECK/UNIQUE），失败时页上
            # 一个字节都没动；通过后才退休 WAL / 写 pending / 真正改写。
            self._prevalidate_modify(tbl, entry)
            self._ddl_retire_wal(tbl, 'MODIFY COLUMN')
            self._ddl_begin(entry)
            # always_transform=True：列数不变，必须逐行做类型转换。
            # planning 阶段会先把全部行试算一遍（失败时页上一个字节都没动），
            # 因此"不可转换 → 显式错误 + 表不变"是真实成立的。
            stats = tbl.rewrite_row_payloads(
                transform, len(old_cols), len(new_cols),
                always_transform=True)
            tbl.cols = new_cols
            tbl.refresh_schema_meta()
            if auto_removed or auto_added:
                # 重新加回自增时把计数/预留文件按当前状态落盘，确保
                # SHOW 状态与重启后一致（不复用移除前的旧号）。
                try:
                    tbl._persist_auto_reservation(exact=True)
                except Exception as e:            # pragma: no cover - 防御
                    logger.warn(f"MODIFY 自增状态落盘失败 {name}: {e}")
            self._ddl_commit(tbl)
        return {'column': cname, 'new_type': new_type,
                'rows_rewritten': stats['rows'],
                'auto_increment_removed': auto_removed,
                'auto_increment_added': auto_added, **stats}

    # ==================== E2：CREATE / DROP INDEX ====================

    def _ensure_unique_data(self, tbl, spec):
        """建 UNIQUE 索引前检查已有数据（冲突 → 1062，绝不先建后炸）"""
        columns = list(spec.get('columns') or [])
        idxs = [tbl._col_idx(c) for c in columns]
        seen = {}
        for _pk, values in tbl._live_rows():
            tup = tuple(values[i] for i in idxs)
            if any(v is None for v in tup):
                continue
            if tup in seen:
                shown = '-'.join(str(v) for v in tup)
                raise DdlError(
                    f"Duplicate entry '{shown}' for key '{spec.get('name')}'",
                    errno=1062, sqlstate='23000')
            seen[tup] = True

    def create_index(self, table: str, name: str, columns: List[str],
                     unique: bool = False,
                     if_not_exists: bool = False) -> dict:
        """`CREATE [UNIQUE] INDEX name ON t (列...)`

        * 声明持久化到 `_meta.json`（重启后仍在）；
        * UNIQUE → 与表级 UNIQUE 共用同一套**真实强制**的写入期唯一性
          检查（含已有数据冲突检查：1062）；
        * 非唯一 → `SCAN-ASSIST` 声明元数据（本引擎没有二级索引结构，
          查询仍全表扫描；`SHOW INDEX` 的 Index_type 如实报出，不谎称
          BTREE），`SHOW CREATE TABLE` 可往返；
        * 重名 1061 / 未知列 1072 / 同索引重复列 1060 / 未知表 1146。
        """
        tbl = self._get_table_for_ddl(table)
        cols = [str(c) for c in (columns or [])]
        low_name = str(name or '').strip().lower()
        existing = (tbl.unique_indexes() + tbl.secondary_indexes())
        for spec in existing:
            if str(spec['name']).lower() == low_name:
                if if_not_exists:
                    return {'created': False, 'existed': True,
                            'name': name, 'unique': bool(
                                spec.get('origin') != 'column')}
                raise DdlError(f"Duplicate key name '{name}'", errno=1061,
                               sqlstate='42000')
        if low_name == 'primary':
            raise DdlError(f"Duplicate key name '{name}'", errno=1061,
                           sqlstate='42000')
        if unique:
            merged = [dict(s) for s in tbl.unique_indexes()
                      if s.get('origin') != 'column']
            merged.append({'name': name, 'columns': cols})
            resolved = resolve_unique_indexes(table, tbl.cols, merged)
            new_spec = {'name': name, 'columns': cols}
            self._ensure_unique_data(tbl, new_spec)
            with tbl._lock:
                tbl.set_unique_indexes(resolved)
            self.save_meta()
            return {'created': True, 'existed': False, 'name': name,
                    'columns': cols, 'unique': True}
        specs = tbl.secondary_indexes() + [
            {'name': name, 'columns': cols, 'unique': False}]
        reserved = [s['name'] for s in tbl.unique_indexes()]
        normalized = resolve_secondary_indexes(
            table, tbl.cols, specs, reserved_names=reserved)
        with tbl._lock:
            tbl.set_secondary_indexes(normalized)
        self.save_meta()
        return {'created': True, 'existed': False, 'name': name,
                'columns': cols, 'unique': False}

    def drop_index(self, table: str, name: str) -> dict:
        """`DROP INDEX name ON t` —— 删除真实存在的声明（不存在显式 1091）"""
        tbl = self._get_table_for_ddl(table)
        low = str(name or '').strip().lower()
        if low == 'primary':
            raise DdlError(
                "Can't DROP 'PRIMARY'; 主键索引不能通过 DROP INDEX 删除",
                errno=1091, sqlstate='42000')
        for spec in tbl.unique_indexes():
            if str(spec['name']).lower() != low:
                continue
            if spec.get('origin') == 'column':
                # 列级 UNIQUE 的自动索引：同时清掉列上的 UNIQUE 标志，
                # 否则 `_sync_column_unique_indexes` 会立刻把它推导回来。
                new_cols = [dict(c) for c in tbl.cols]
                for c in new_cols:
                    if c.get('name') in spec['columns'] and c.get('unique'):
                        c['unique'] = False
                with tbl._lock:
                    tbl.cols = new_cols
                    tbl.set_unique_indexes(
                        [dict(s) for s in tbl.unique_indexes()
                         if str(s['name']).lower() != low])
                    tbl.refresh_schema_meta()
            else:
                with tbl._lock:
                    tbl.set_unique_indexes(
                        [dict(s) for s in tbl.unique_indexes()
                         if str(s['name']).lower() != low])
            self.save_meta()
            return {'dropped': spec['name'], 'unique': True}
        for spec in tbl.secondary_indexes():
            if str(spec['name']).lower() == low:
                with tbl._lock:
                    tbl.set_secondary_indexes(
                        [x for x in tbl.secondary_indexes()
                         if str(x['name']).lower() != low])
                self.save_meta()
                return {'dropped': spec['name'], 'unique': False}
        raise DdlError(
            f"Can't DROP '{name}'; check that column/key exists",
            errno=1091, sqlstate='42000')

    def set_auto_increment(self, name: str, value: int) -> int:
        """`ALTER TABLE t AUTO_INCREMENT = n`：设置下一号并持久化

        返回**实际生效**的下一号（低于"现存最大号+1"时按 MySQL 语义抬到
        该下限；不会假装设成了更小的数）。
        """
        tbl = self._get_table_for_ddl(name)
        with tbl._lock:
            actual = tbl.set_auto_increment(int(value))
            self.save_meta()
        return actual

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
                 # E2：非唯一索引与请求的引擎名跟着改名走
                 'indexes': tbl.secondary_indexes(),
                 'requested_engine': getattr(tbl, 'requested_engine',
                                             'LightDB'),
                 # P6：自增计数跟着改名走（否则 RENAME 后计数回退/重用）
                 'auto_increment_next': (
                     tbl.auto_increment_next_values()
                     if hasattr(tbl, 'auto_increment_next_values') else {}),
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
        for suffix in ('.dat', '.btroot', '.wal', '.free', '.autoinc'):
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
                                       else tbl.unique_indexes()),
                       secondary_indexes=entry.get(
                           'indexes', tbl.secondary_indexes()),
                       requested_engine=entry.get(
                           'requested_engine',
                           getattr(tbl, 'requested_engine', 'LightDB')),
                       auto_increment_next=entry.get(
                           'auto_increment_next',
                           (tbl.auto_increment_next_values()
                            if hasattr(tbl, 'auto_increment_next_values')
                            else None)))
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
            elif op == 'MODIFY COLUMN':
                # 列数不变的类型变更：所有行都重新走一次幂等转换
                tbl.rewrite_row_payloads(
                    self._modify_transform(entry),
                    len(old_cols), len(new_cols), always_transform=True)
            elif op == 'TRUNCATE':
                tbl.truncate_all()
            elif op == 'COMPACT':
                # 文件替换已在 `_pre_recover_compact()`（`_load` 之前）补完；
                # 表已按新文件打开，这里只清 pending 标记并记日志。
                logger.warn(f"崩溃恢复：补完未完成的 COMPACT TABLE {tname}")
                self._pending_ddl = None
                self.save_meta()
                return
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
        """提交事务（COMMIT 落盘是持久化点，必须先于脏页刷盘）

        阶段 2（决策 D）：`binlog_strict`（默认 True）下**先**提交 binlog
        组（`BEGIN → rows… → COMMIT`），失败则物理回滚本事务并把
        `BinlogWriteError`（1180/HY000）抛给上层 → 客户端看到显式错误、
        数据不可见（可重试）。`LIGHTDB_BINLOG_STRICT=0` 时退化为旧行为：
        WAL 先提交，binlog 失败只告警（排障用）。
        """
        tid = txn['txn_id']
        strict = self._binlog_strict_active()
        if strict:
            try:
                self._binlog_commit_txn_strict(tid)
            except Exception as e:
                self._binlog_errors += 1
                logger.error(
                    f"binlog 组提交失败，拒绝提交事务 {tid}: {e}")
                # 回滚：物理撤销 + 释放 MVCC/锁/读视图，保证"未提交"状态
                try:
                    self.abort(txn)
                except Exception as e2:          # pragma: no cover - 防御
                    logger.error(
                        f"binlog 提交失败后回滚事务 {tid} 失败: {e2}")
                raise
        for tbl in list(self.tables.values()):
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
        for tbl in list(self.tables.values()):
            tbl._read_views.pop(tid, None)
        if not strict:
            # 非 strict（排障模式）：WAL 已提交后再补 binlog，失败只告警
            self._binlog_commit_txn(tid)

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
        strict = self._binlog_strict_active()
        if strict:
            try:
                self._binlog_commit_txn_strict(tid)
            except Exception as e:
                self._binlog_errors += 1
                logger.error(
                    f"binlog 组提交失败，拒绝语句级提交（txn {tid}）: {e}")
                try:
                    self.abort(txn)
                except Exception as e2:          # pragma: no cover - 防御
                    logger.error(
                        f"binlog 提交失败后回滚语句事务 {tid} 失败: {e2}")
                raise
        for tbl in list(self.tables.values()):
            try:
                tbl.wal.commit_autocommit(tid)
            except Exception as e:
                logger.warn(f"语句级 COMMIT 写入失败 {tbl.name}: {e}")
        self.mvcc.release(tid, committed=True)
        self.lock_manager.release(tid)
        for tbl in list(self.tables.values()):
            tbl._read_views.pop(tid, None)
        if not strict:
            # 语句级内部事务也是"已提交"：非 strict 模式补写 binlog
            self._binlog_commit_txn(tid)

    def abort(self, txn):
        """回滚事务：真正把数据改回去，再写 ABORT、放锁"""
        tid = txn['txn_id']
        for tbl in list(self.tables.values()):
            try:
                # ★ 物理回滚：倒序把旧行写回原页原槽位
                tbl.abort_txn(tid)
            except Exception as e:
                logger.error(f"回滚 {tbl.name} 失败: {e}")
        for tbl in list(self.tables.values()):
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
        for tbl in list(self.tables.values()):
            tbl._read_views.pop(tid, None)
        # ★ 真 binlog：回滚的事务写 `BEGIN → ROLLBACK`（行事件从未落盘，
        #   只记录被丢弃的条数，不伪造数据变更）
        self._binlog_rollback_txn(tid)

    # ==================== 真 · append-only binlog（决策 D 项）====================

    @property
    def binlog_log(self):
        """本实例的 append-only binlog 读写器（`replication/append_only_binlog.py`）

        * 惰性创建：第一次需要记录/查询时打开 `<data_dir>/binlog/binlog.000001`；
        * 环境变量 `LIGHTDB_BINLOG_ENABLED=0` 时返回 None（调用方按
          "本实例没有 binlog"处理：`SHOW MASTER STATUS` 返回空结果集）；
        * 线程安全：同进程多连接共享同一个写器（内部 RLock 串行化组提交）。
        """
        if self._binlog_log is not None:
            return self._binlog_log
        with self._binlog_log_lock:
            if self._binlog_log is not None:
                return self._binlog_log
            try:
                from ..replication.append_only_binlog import (
                    AppendOnlyBinlog, binlog_enabled)
            except Exception as e:            # pragma: no cover - 导入防御
                logger.error(f"加载 append-only binlog 失败: {e}")
                self._binlog_errors += 1
                return None
            if not binlog_enabled():
                return None
            try:
                self._binlog_log = AppendOnlyBinlog(str(self.data_dir))
            except Exception as e:
                logger.error(f"打开 binlog 文件失败: {e}")
                self._binlog_errors += 1
                return None
            return self._binlog_log

    @property
    def binlog_suspended(self) -> bool:
        """当前是否处于"暂停记录"区间（从库重放期间为 True）"""
        return self._binlog_suspend_depth > 0

    @contextlib.contextmanager
    def suspend_binlog(self):
        """在 with 块内暂停 binlog 记录（从库重放 DDL/DML 用）

        从库把重放产生的事件再记进自己的 binlog 会造成回环/双写
        （MySQL 默认 `log_slave_updates` 的行为由主库配置决定；本引擎
        当前语义是"从库不记录重放事件"，如实写在这里）。
        """
        with self._binlog_log_lock:
            self._binlog_suspend_depth += 1
        try:
            yield
        finally:
            with self._binlog_log_lock:
                self._binlog_suspend_depth -= 1

    def _binlog_strict_active(self) -> bool:
        """当前事务提交是否受"binlog 写失败即拒绝提交"约束

        * 从库重放期间（`suspend_binlog`）不写 binlog，自然不约束；
        * `LIGHTDB_BINLOG_STRICT=0` 时退化为旧告警行为（排障用）。
        """
        if self.binlog_suspended:
            return False
        try:
            from ..replication.append_only_binlog import binlog_strict
            return binlog_strict()
        except Exception:                        # pragma: no cover - 防御
            return False

    def _binlog_commit_txn_strict(self, txn_id: int):
        """严格组提交：写失败抛 `BinlogWriteError`（1180/HY000）

        binlog 被显式关闭（`LIGHTDB_BINLOG_ENABLED=0`）时不约束提交；
        启用但打开失败 → 视为写路径不可用，同样拒绝提交（不静默降级）。
        """
        from ..replication.append_only_binlog import (
            BinlogWriteError, binlog_enabled)
        if not binlog_enabled():
            return
        rec = self.binlog_log
        if rec is None:
            raise BinlogWriteError(
                'binlog 不可用（打开失败），按 strict 语义拒绝提交')
        rec.commit_txn(txn_id)

    def _binlog_commit_txn(self, txn_id: int):
        if self.binlog_suspended:
            return
        try:
            rec = self.binlog_log
            if rec is None:
                return
            rec.commit_txn(txn_id)
        except Exception as e:
            self._binlog_errors += 1
            logger.error(
                f"binlog 组提交失败（事务 {txn_id}）：{e} —— "
                f"WAL 已提交，但复制日志缺少本事务事件")

    def _binlog_rollback_txn(self, txn_id: int):
        if self.binlog_suspended:
            return
        try:
            rec = self.binlog_log
            if rec is None:
                return
            rec.rollback_txn(txn_id)
        except Exception as e:
            self._binlog_errors += 1
            logger.error(f"binlog 回滚记录失败（事务 {txn_id}）：{e}")

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
            for tbl in list(self.tables.values()):
                try:
                    h = tbl._vacuum_horizon()
                    if h < horizon:
                        horizon = h
                except Exception:
                    pass
        total = 0
        for tbl in list(self.tables.values()):
            try:
                total += tbl.vacuum(horizon)
            except Exception as e:
                logger.warn(f"vacuum {tbl.name} 失败: {e}")
        return total

    def vacuum_stats(self) -> dict:
        """所有表最近一次 vacuum 的页级统计汇总（供 `VACUUM` 结果集/巡检）"""
        out = {}
        for name, tbl in list(self.tables.items()):
            try:
                out[name] = tbl.vacuum_stats()
            except Exception:
                out[name] = {}
        return out

    # ==================== P5：WAL 状态 / 数据压实（COMPACT）====================

    def wal_status(self) -> dict:
        """P5：每张表的 WAL/checkpoint 真实状态（`SHOW WAL STATUS` 数据源）"""
        out = {}
        for name, tbl in list(self.tables.items()):
            try:
                out[name] = tbl.wal_status()
            except Exception as e:               # pragma: no cover - 防御
                out[name] = {'table': name, 'error': str(e)}
        return out

    def _pre_recover_compact(self):
        """P5：进入 `_load()` 之前补完崩溃中断的 COMPACT **文件替换**（幂等）

        COMPACT 的提交点是"多文件替换 + `_pending_ddl` 清空"。进程可能在
        只替换了一部分文件时被硬杀；此时磁盘上可能是"新 .dat + 旧 .btroot"
        这类混搭。必须在打开数据文件（`_load`）之前把临时目录里剩下的
        文件 `os.replace` 到位，随后 `_load`/`_recover` 才能看到完整的新表。

        另外清理"临时表已建好但 pending 标记还没写就崩了"留下的孤儿目录
        （此时数据文件仍是旧的，临时目录直接丢弃即可）。
        """
        p = self._meta_path()
        if not p.exists():
            return
        try:
            meta = json.loads(p.read_text(encoding='utf-8'))
        except Exception:                        # pragma: no cover - 防御
            meta = {}
        entry = meta.get('pending_ddl') or {}
        name = str(entry.get('table') or '') if \
            entry.get('op') == 'COMPACT' else ''
        tmp = None
        if name:
            tmp = self.data_dir / str(entry.get('tmp_dir') or '')
            try:
                if tmp.exists():
                    for suffix in ('.dat', '.btroot', '.free', '.autoinc'):
                        src = tmp / f'{name}{suffix}'
                        if not src.exists():
                            continue
                        try:
                            os.replace(str(src),
                                       str(self.data_dir / f'{name}{suffix}'))
                        except OSError as e:
                            logger.error(
                                f"补完 COMPACT {name} 时替换 {suffix} 失败: {e}")
                # 旧 WAL 已在写 pending 前退休；新表 WAL 必须为空
                wal_path = self.data_dir / f'{name}.wal'
                try:
                    with open(wal_path, 'w', encoding='utf-8'):
                        pass
                except OSError as e:             # pragma: no cover - 防御
                    logger.warn(f"重置 COMPACT {name} 的 WAL 失败: {e}")
                logger.warn(
                    f"崩溃恢复：补完未完成的 COMPACT TABLE {name}"
                    f"（临时文件已切换到目标位置）")
            except Exception as e:               # pragma: no cover - 防御
                logger.error(f"补完 COMPACT {name} 失败（保留 pending 记录）: {e}")
            finally:
                if tmp is not None and tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
            return
        # 没有 COMPACT pending：清理孤儿临时目录（数据文件仍是旧的）
        try:
            for d in self.data_dir.glob('_compact_*'):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:                        # pragma: no cover - 防御
            pass

    def _compact_table(self, name: str) -> dict:
        """P5：**离线** COMPACT（整表重写为紧凑文件，`.dat` 真实下降）

        做法（崩溃安全）:
          1) 安全门：表上不能有未提交事务（`_ddl_guard`）；
          2) `checkpoint(force=True)` —— 脏页 fsync、WAL 截断（之后不需要
             redo 任何旧记录）；
          3) 在临时目录构建新表：只插入**当前存活行**（顺序无关，主键
             与数据逐行校验一致），索引/约束/分区/全文索引按原定义重建；
             新表 flush + close 后大小若没有变小 → 放弃（不缩反大）；
          4) 写 `pending_ddl`（COMPACT 意图，原子落盘）→ 关闭旧表句柄 →
             `os.replace` 逐个切换 `.dat/.btroot/.free` → 清空 `.wal`；
             崩溃在任意一步都由 `_pre_recover_compact` 在下次启动补完；
          5) 从磁盘重新打开新表，替换内存对象并清 pending。

        这是一个**显式、离线**入口（无后台线程/无并发查询时调用）；返回
        压实前后字节数、行数与索引一致性指标。
        """
        tbl = self._get_table_for_ddl(name)
        if getattr(tbl, '_read_views', None):
            raise DdlError(
                f"表 {name} 上存在未提交事务，无法 COMPACT：请先 "
                f"COMMIT / ROLLBACK（压实会重写整个数据文件）", errno=1105)
        with tbl._lock:
            self._ddl_guard(tbl, 'COMPACT TABLE')
            # 1) 数据全部落盘 + 退休 WAL（此后不需要任何旧 WAL 重放）
            if not tbl.checkpoint(force=True, reason='manual'):
                raise DdlError(
                    f"表 {name} 的 checkpoint 未能完成，放弃 COMPACT",
                    errno=1105)
            old_cols = [dict(c) for c in tbl.cols]
            unique = tbl.unique_indexes()
            secondary = tbl.secondary_indexes()
            checks = [dict(c) for c in getattr(tbl, '_checks', [])]
            partition = getattr(tbl, '_partition_spec', None)
            options = dict(getattr(tbl, 'table_options', None) or {})
            comment = getattr(tbl, 'comment', '') or ''
            engine = getattr(tbl, 'requested_engine', 'LightDB')
            auto_state = (tbl.auto_increment_next_values()
                          if hasattr(tbl, 'auto_increment_next_values')
                          else None)
            fulltext_cols = sorted(
                getattr(getattr(tbl, '_fulltext', None), 'indexes', {}) or {})
            before_bytes = tbl.bp.file_size()
            before_pages = tbl.bp.total_pages()
            rows_before = tbl.row_count
            old_int = tbl.integrity_scan(deep=True)
            old_live = sorted((pk, tuple(values))
                              for pk, values in tbl._live_rows())
            # 2) 临时目录构建紧凑新表
            tmp = self.data_dir / f'_compact_{name}'
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                new = Table(
                    name, [dict(c) for c in old_cols], str(tmp),
                    LockManager(), MVCCManager(),
                    unique_indexes=unique,
                    secondary_indexes=secondary,
                    requested_engine=engine,
                    auto_increment_next=auto_state)
                if partition:
                    new.set_partition(partition)
                new.set_checks(checks)
                for col in fulltext_cols:
                    new.create_fulltext_index([col])
                for _pk, values in tbl._live_rows():
                    new.insert(list(values))
                new_int = new.integrity_scan(deep=True)
                new_live = sorted((pk, tuple(values))
                                  for pk, values in new._live_rows())
                if new_live != old_live:
                    raise DdlError(
                        f"COMPACT {name} 自检失败：新表内容与旧表不一致"
                        f"（{len(old_live)} vs {len(new_live)} 行），已放弃",
                        errno=1105)
                if new_int.get('index_entries') != rows_before or \
                        new_int.get('live_rows') != rows_before:
                    raise DdlError(
                        f"COMPACT {name} 自检失败：新表索引/行数不一致 "
                        f"{new_int}", errno=1105)
                new.bp.flush()
                new._save_btree_root(new.bt.root_page_id, sync=True)
                new.close()
                new_bytes = os.path.getsize(tmp / f'{name}.dat')
                new_pages = max(1, -(-new_bytes // 4096))
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            if new_bytes >= before_bytes:
                shutil.rmtree(tmp, ignore_errors=True)
                return {'table': name, 'skipped': True,
                        'reason': 'no_gain',
                        'rows': rows_before,
                        'bytes_before': before_bytes,
                        'bytes_after': before_bytes}
            entry = {
                'op': 'COMPACT', 'table': name,
                'tmp_dir': tmp.name,
                'before_bytes': before_bytes, 'after_bytes': new_bytes,
                'new_cols': old_cols,
                'unique_indexes': unique, 'indexes': secondary,
                'checks': checks, 'partition': partition,
                'table_options': options, 'comment': comment,
                'requested_engine': engine,
                'fulltext_columns': fulltext_cols,
                'auto_increment_next': auto_state,
            }
            # 3) 写 pending 意图（原子）—— 之后发生的崩溃由启动恢复补完
            self._ddl_begin(entry)
            # 4) 关闭旧句柄（Windows 上不关闭无法 os.replace）并切换文件
            try:
                tbl.bp.close()
                tbl.wal.close()
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"COMPACT 关闭旧表 {name} 句柄失败: {e}")
        for suffix in ('.dat', '.btroot', '.free', '.autoinc'):
            src = tmp / f'{name}{suffix}'
            dst = self.data_dir / f'{name}{suffix}'
            try:
                if src.exists():
                    os.replace(str(src), str(dst))
                elif suffix in ('.free', '.autoinc') and dst.exists():
                    dst.unlink()
            except OSError as e:
                logger.error(f"COMPACT {name}: 替换 {suffix} 失败: {e}")
        with open(self.data_dir / f'{name}.wal', 'w', encoding='utf-8'):
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        # 5) 从磁盘打开新表并替换内存对象
        new_tbl = Table(
            name, old_cols, str(self.data_dir),
            self.lock_manager, self.mvcc,
            unique_indexes=unique, secondary_indexes=secondary,
            requested_engine=engine,
            auto_increment_next=auto_state)
        if partition:
            new_tbl.set_partition(partition)
        new_tbl.set_checks(checks)
        new_tbl.table_options = options
        new_tbl.comment = comment
        for col in fulltext_cols:
            new_tbl.create_fulltext_index([col])
        if fulltext_cols:
            for pk, values in new_tbl._live_rows():
                new_tbl._index_fulltext(pk, values)
        # P6.2：压实会重建数据文件；自增预留文件按 entry 里的精确计数收敛，
        # 保证压实前后 `SHOW TABLE STATUS.Auto_increment` 一致（不跳号）。
        try:
            if auto_state:
                new_tbl._auto_next = {k: int(v) for k, v in
                                      auto_state.items()}
                new_tbl._auto_reserved = dict(new_tbl._auto_next)
            new_tbl._persist_auto_reservation(exact=True)
        except Exception as e:                   # pragma: no cover - 防御
            logger.warn(f"COMPACT 后写自增预留失败 {name}: {e}")
        self.tables[name] = new_tbl
        self._pending_ddl = None
        self.save_meta()
        after_bytes = os.path.getsize(self.data_dir / f'{name}.dat')
        after_pages = new_tbl.bp.total_pages()
        after_int = new_tbl.integrity_scan(deep=True)
        stats = {
            'table': name, 'skipped': False,
            'rows': rows_before,
            'bytes_before': before_bytes,
            'bytes_after': after_bytes,
            'bytes_freed': max(0, before_bytes - after_bytes),
            'pages_before': before_pages, 'pages_after': after_pages,
            'reclaimed_versions': old_int.get('chain_versions', 0)
            - after_int.get('live_rows', 0),
            'index_entries_before': old_int.get('index_entries', 0),
            'index_entries_after': after_int.get('index_entries', 0),
            'live_rows_after': after_int.get('live_rows', 0),
            'integrity_ok': (after_int.get('index_entries') == rows_before
                             and after_int.get('live_rows') == rows_before),
        }
        # 状态观测主入口是 `SHOW WAL STATUS`/`Database.wal_status()`；
        # 日志保持 ASCII + DEBUG 级（既有 subprocess 捕获 stdout 的测试
        # 要求子进程 stdout 仍是纯 JSON）。
        logger.debug(
            f"COMPACT {name}: rows={rows_before} "
            f"dat_bytes={before_bytes}->{after_bytes} "
            f"freed={stats['bytes_freed']} "
            f"pages={before_pages}->{after_pages} "
            f"index_entries={stats['index_entries_before']}->"
            f"{stats['index_entries_after']}")
        return stats

    def compact(self, tables=None) -> dict:
        """P5：对指定表（默认全部）执行离线 COMPACT，返回每表量化结果"""
        if tables:
            names = [str(n) for n in tables]
        else:
            names = sorted(self.tables.keys())
        out = {}
        for name in names:
            if name not in self.tables:
                out[name] = {'table': name, 'skipped': True,
                             'reason': 'no such table'}
                continue
            try:
                out[name] = self._compact_table(name)
            except Exception as e:
                out[name] = {'table': name, 'skipped': True,
                             'reason': str(e)}
                logger.warn(f"COMPACT {name} 跳过: {e}")
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
        for tbl in list(self.tables.values()):
            try:
                if tbl.checkpoint(force=force):
                    n += 1
            except Exception as e:
                logger.warn(f"checkpoint {tbl.name} 失败: {e}")
        # P6：自增计数是**已分配号**的不变量（回滚/删除不回收），checkpoint
        # 是确定性落盘点：把计数写进 `_meta.json`，重启后不会因"最大行被删"
        # 而重用旧号。
        try:
            self.save_meta()
        except Exception as e:                    # pragma: no cover - 防御
            logger.warn(f"checkpoint 后写元数据失败（忽略）: {e}")
        return n

    def shutdown(self):
        # 审计器的待落盘批：正常停机路径上**先刷盘**（审计批量 flush 的
        # 确定性落盘点），失败只告警，绝不影响停机的其它动作。
        for sink in list(getattr(self, '_audit_sinks', ()) or ()):
            try:
                sink.close()
            except Exception as e:                # pragma: no cover - 防御
                logger.warn(f"停机时审计落盘失败: {e}")
        for tbl in list(self.tables.values()):
            try:
                tbl.close()
            except Exception:
                pass
        # P6：正常停机也落一次元数据（自增计数 = 已分配号，回滚/删除不回收；
        # 不写盘的话，重启后按现存最大行推断会重用被删/回滚掉的号）。
        try:
            self.save_meta()
        except Exception as e:                    # pragma: no cover - 防御
            logger.warn(f"停机时写元数据失败（忽略）: {e}")
        if self._binlog is not None:
            try:
                self._binlog.close()
            except Exception:
                pass
        # 真 binlog：停机是"正常落盘"的确定性时点（未提交的组不落盘）
        if self._binlog_log is not None:
            try:
                self._binlog_log.close()
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
                n: t.stats() for n, t in list(self.tables.items())
            },
            'locks': self.lock_manager.stats(),
            'mvcc': self.mvcc.stats(),
        }