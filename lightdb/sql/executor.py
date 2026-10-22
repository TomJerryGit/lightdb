"""
SQL 执行器 - 结构化结果（修复版）

修复:
- __init__ 初始化 self.optimizer（原 bug: AttributeError）
- 加 start_event_scheduler / stop_event_scheduler
- 加 stats_manager
"""
import atexit
import contextlib
import json
import os
import re
import threading
import time
import weakref
from collections import deque
from typing import Dict, List, Optional

from ..logger import logger
from ..db.information_schema import InformationSchema
# `SHOW COLUMNS` 的列元数据构造（与 information_schema 共用一份实现，
# 见 db/database.py 的说明）
from ..db.database import describe_columns
from ..db.errors import DdlError
from ..db.table import Table as _StorageTable
# CHECK 约束违规（MySQL ERROR 3819 / SQLSTATE HY000）：`Table.insert` /
# `Table.update` 抛它，`execute` 在这里把它渲染成错误结果而不是 1062。
from .checks import CheckConstraintError
# 标量表达式（单行函数 + 算术）：解析 / 渲染 / 求值都在 parser 里，
# 执行器与计划执行器共用同一份实现与同一套报错文案。
# parser 不反向依赖 executor，因此这里可以模块级导入（见 sql/__init__ 的顺序）。
from .parser import (
    parse_scalar_expr as _parse_scalar_expr,
    render_scalar_expr as _render_scalar_expr,
    eval_expr_node as _eval_expr_node,
    eval_expr_predicate as _eval_expr_predicate,
    expr_has_agg as _expr_has_agg,
    expr_has_between as _expr_has_between,
    expr_has_func as _expr_has_func,
    expr_columns_outside_agg as _expr_columns_outside_agg,
    expr_agg_texts as _expr_agg_texts,
    expr_pred_agg_texts as _expr_pred_agg_texts,
    unsupported_function_message,
    normalize_order_by,
    AGGREGATE_FUNCTION_NAMES as _AGGREGATE_FUNCTION_NAMES,
    SCALAR_FUNCTION_NAMES as _SCALAR_FUNCTION_NAMES,
)

# PL 解释器的常量（迭代上限默认值 / 循环嵌套上限）。放在这里而不是
# 写死两份，是为了让"绝不无限循环"的保护只有一处定义。
from .pl import (
    DEFAULT_MAX_ITERATIONS as PL_DEFAULT_MAX_ITERATIONS,
    _MAX_LOOP_LABEL_DEPTH as PL_MAX_LOOP_DEPTH,
)


# 哨兵：区分"调用方没传 where"与"where 被改写成恒真(None)"
_UNSET = object()

# 哨兵：作用域里解析不到某个列引用
_MISSING = object()


class _CTERelation:
    """WITH 子句物化出来的内存关系

    只实现 Table._match / Table._aggregate 需要的接口（cols / _col_idx），
    因此 WHERE 与聚合可以直接复用存储层的实现，而不必创建真实表文件
    （真实表还会因为主键唯一性要求而无法承载有重复值的 CTE 结果）。
    """

    def __init__(self, name, columns, rows):
        self.name = name
        self.col_names = [str(c) for c in (columns or [])]
        self.rows = [list(r) for r in (rows or [])]
        self.cols = [
            {'name': n, 'type': self._guess_type(i)}
            for i, n in enumerate(self.col_names)
        ]

    def _guess_type(self, i):
        vals = [r[i] for r in self.rows
                if i < len(r) and r[i] is not None]
        if vals and all(isinstance(v, bool) for v in vals):
            return 'INT'
        if vals and all(isinstance(v, int) for v in vals):
            return 'INT'
        if vals and all(isinstance(v, (int, float)) for v in vals):
            return 'DOUBLE'
        return 'VARCHAR'

    def _col_idx(self, name: str) -> int:
        """列名 -> 下标

        精确匹配失败后再按「列名末段」唯一匹配一次：JOIN 结果物化成的内存
        关系列名带限定前缀（`u.id` / `o.amt`），而存储层的 `_aggregate` /
        `_match` 只认裸列名（`amt`）。末段歧义时仍然报错，绝不任取一列。
        """
        for i, c in enumerate(self.cols):
            if c['name'] == name:
                return i
        want = str(name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        hits = [i for i, c in enumerate(self.cols)
                if str(c['name']).strip().strip('`"')
                .rsplit('.', 1)[-1].lower() == want]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise KeyError(f"列 {name} 歧义，请加表名前缀")
        raise KeyError(f"列 {name} 不存在")


# 存活的审计器（弱引用）：解释器正常退出时统一把待落盘批刷掉。
# 用 WeakSet 而不是 atexit 里逐个强引用，是为了不让审计器（及其
# 2000 条环形缓冲）因为"注册了 atexit"而永远无法回收。
_LIVE_AUDIT_SINKS = weakref.WeakSet()


def _flush_all_audit_sinks():
    """atexit 钩子：进程正常退出时刷掉所有审计器的待落盘批"""
    for sink in list(_LIVE_AUDIT_SINKS):
        try:
            sink.close()
        except Exception:                        # pragma: no cover - 防御
            pass


atexit.register(_flush_all_audit_sinks)


class _AuditSink:
    """语句级审计（`SHOW AUDIT LOG` 的**唯一**数据源）

    真实记录每条被执行的语句：时间戳 / 用户 / 命令 / 成功与否 / 耗时 / SQL 原文
    （SQL 原文来自 `parser` 附在 AST 上的 `source_sql`，见 `Parser.parse`）。

    级别语义与 `lightdb.security.audit.AuditLogger` 完全一致
    （NONE < DCL < DDL < DML < ALL，记录 `语句级别 <= 配置级别`），分类也直接
    复用 `AuditLogger._classify`，两边不会漂移。

    两处有意的差别（都在这里注释说明，绝不静默）：
    * 落盘用「批量攒 + 一次性 append + close」，**不持有常驻文件句柄**：
      审计器比 `Database` 活得久，常驻句柄会让 Windows 上的数据目录删除失败
      （PermissionError winerror=32，见 docs/FAILURE_TRIAGE.md §8 的级联簇）；
    * 级别默认取环境变量 `LIGHTDB_AUDIT_LEVEL`，未设置时用 `DML`。
      `net/server.py` 不会把 `MySQLServer(cfg, ...)` 里的 `cfg.audit_level`
      传给执行器（net/** 不在本次可改范围），因此用例里
      `start_server(port, audit_level='DML')` 传进来的级别到不了这里；
      默认 DML 保证 DCL / DDL / DML 语句都被审计到。

    ★ 阶段 2 第 2 项：**批量 flush**。
    原实现每条语句 `open(path,'a') + write + close`（3 次系统调用 × 每条
    语句）。逐语句路径（executemany / 逐行 INSERT）下这是纯粹的开销，
    而且批量语句省下的解析收益会被审计里"每条语句一次 open/close"吃掉。
    现在改为：记录先进内存 (`recent` 环形缓冲 + `_pending` 待落盘批)，
    在下面任一条件成立时**一次性** append 落盘：
      1) 攒够 `AUDIT_BATCH`(32) 条；
      2) 距本批第一条已经超过 `AUDIT_FLUSH_INTERVAL`(0.5s)（在 record()
         里机会式检查，不需要后台线程）；
      3) **失败语句立即落盘**（success=0 是审计里最要紧的记录，
         不参与延迟 —— 报错路径本来就稀少，不构成性能负担）；
      4) 显式 `flush()` / `close()`：`Database.shutdown()` 会关闭挂在
         自己身上的审计器，`net/server.py` 关服务时会走到；
         另外模块级 `atexit` 钩子在**解释器正常退出**时统一关闭所有
         存活审计器（WeakSet，不会把审计器钉在内存里）。
    不丢记录的保证：
      * 落盘失败（磁盘满/权限）时 **`_pending` 原样保留**并告警，
        下次 flush 重试 —— 不会"失败了还把记录当已落盘丢掉"；
      * `SHOW AUDIT LOG` 读的是内存环形缓冲，与是否已落盘无关，
        因此"落盘延迟"不会让审计查询看不到记录；
      * 未落盘记录的上界是显式的（`len(_pending) <= AUDIT_PENDING_MAX`），
        超过上限时丢弃**最旧**的记录并告警（防磁盘永久故障导致内存
        无限增长），丢弃这件事本身有日志，不是静默丢失。
      残余风险（如实说明）：进程被 `os._exit()`/SIGKILL 硬杀时，内存里
      尚未攒到落盘条件的那 ≤31 条记录会丢 —— 这是"批量"与"每进程崩溃
      零丢失"之间的取舍；既有实现（每条 close）在硬杀下确实一条不丢。
      正常退出、`Database.shutdown()`、显式 `close()` 三条路径都保证落盘。
    """

    #: 攒够多少条审计记录就落盘（一次 append 写多条）
    AUDIT_BATCH = 32
    #: 攒了多久（秒）就机会式落盘；不引入后台线程
    AUDIT_FLUSH_INTERVAL = 0.5
    #: `_pending` 的内存上限：磁盘长期故障时丢弃最旧记录并告警
    AUDIT_PENDING_MAX = 4096

    def __init__(self, data_dir: str, level: str = None):
        self.path = os.path.join(str(data_dir), 'audit.log')
        self.recent = deque(maxlen=2000)
        self._lock = threading.RLock()
        # 待落盘批（内存）——不是常驻文件句柄，Windows 删除数据目录不受影响
        self._pending = []
        self._pending_since = None
        self._closed = False
        self.flushed_records = 0
        self.dropped_records = 0
        try:
            from ..security.audit import AuditLogger
            self._levels = dict(AuditLogger.LEVELS)
            self._classify = AuditLogger._classify
            _AuditLevels.sync()
            self._classifier = _AuditLevels
            self._AuditLogger = AuditLogger
        except Exception:                        # pragma: no cover
            self._levels = {'NONE': 0, 'DCL': 1, 'DDL': 2, 'DML': 3, 'ALL': 4}
            self._classify = None
            self._classifier = None
            self._AuditLogger = None
        env = os.environ.get('LIGHTDB_AUDIT_LEVEL')
        name = str(level or env or 'DML').upper()
        self.level_name = name if name in self._levels else 'DML'
        self.level = self._levels.get(self.level_name, 3)
        self._load()
        _LIVE_AUDIT_SINKS.add(self)

    def _load(self):
        """启动时把已落盘的审计记录载入内存环形缓冲（重启后仍可查询）"""
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.recent.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:                   # pragma: no cover
            logger.warn(f"审计日志载入失败: {e}")

    def classify(self, cmd: str) -> int:
        if self._classify is not None:
            return self._classify(self._classifier, cmd)
        up = str(cmd or '').upper()
        if up in ('GRANT', 'REVOKE', 'CREATE USER', 'DROP USER',
                  'ALTER USER', 'SET PASSWORD'):
            return self._levels['DCL']
        if up.startswith(('CREATE', 'DROP', 'ALTER')) or up == 'TRUNCATE':
            return self._levels['DDL']
        if up in ('INSERT', 'UPDATE', 'DELETE'):
            return self._levels['DML']
        return self._levels['ALL']

    def record(self, user: str, cmd: str, sql: str, success: bool,
               error: str = '', elapsed: float = 0.0):
        if self.classify(cmd) > self.level:
            return
        entry = {
            'ts': time.time(),
            'user': user,
            'cmd': cmd,
            'sql': str(sql or '')[:500],
            'success': bool(success),
            'error': str(error or '')[:200],
            'elapsed_ms': round(elapsed * 1000, 3),
        }
        with self._lock:
            self.recent.append(entry)
            self._pending.append(entry)
            if self._pending_since is None:
                self._pending_since = time.time()
            # 触发条件见类注释：失败记录 / 攒够一批 / 攒够一段时间
            if (not success
                    or len(self._pending) >= self.AUDIT_BATCH
                    or (time.time() - self._pending_since)
                    >= self.AUDIT_FLUSH_INTERVAL):
                self._flush_locked()

    def flush(self):
        """把待落盘的审计记录立刻写盘（显式关闭/停机路径调用）"""
        with self._lock:
            self._flush_locked()

    def close(self):
        """关闭审计器：**先把待落盘批刷掉**再标记关闭（幂等）

        与旧实现一样不持有常驻句柄，因此这里的"关闭"只是落盘 + 标记；
        这样 Windows 上删除数据目录不会遇到句柄占用。
        """
        with self._lock:
            self._closed = True
            self._flush_locked()
        try:
            _LIVE_AUDIT_SINKS.discard(self)
        except Exception:                        # pragma: no cover
            pass

    @property
    def pending(self) -> int:
        """当前待落盘（仍在内存里）的记录条数"""
        with self._lock:
            return len(self._pending)

    def _flush_locked(self):
        """一次 append 把所有待落盘记录写出去；**失败不丢记录**

        注意：这里不 fsync。旧实现（每条 open/write/close）也只把数据交给
        OS（close 不做 fsync），因此持久性语义不变；批量只是把 N 次
        open/close 合并成一次。
        """
        n = len(self._pending)
        if not n:
            self._pending_since = None
            return
        try:
            data = ''.join(
                json.dumps(e, ensure_ascii=False) + '\n'
                for e in self._pending)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(data)
                f.flush()
        except Exception as e:                   # pragma: no cover - 磁盘故障
            logger.warn(f"审计记录落盘失败（保留待落盘批，下次重试）: {e}")
            # 保留 _pending：绝不能把"没写成功"的记录当成已落盘丢掉
            if len(self._pending) > self.AUDIT_PENDING_MAX * 4:
                dropped = len(self._pending) - self.AUDIT_PENDING_MAX
                del self._pending[:dropped]
                self.dropped_records += dropped
                logger.error(
                    f"审计待落盘积压超过上限，丢弃最旧的 {dropped} 条记录")
            return
        self._pending = []
        self._pending_since = None
        self.flushed_records += n

    def rows(self, limit: int = None) -> list:
        """按时间顺序返回 (ts, user, cmd, success, elapsed_ms, sql)"""
        with self._lock:
            recs = list(self.recent)
        if limit is not None:
            recs = recs[-int(limit):]
        out = []
        for r in recs:
            ts = r.get('ts')
            try:
                tstr = time.strftime('%Y-%m-%d %H:%M:%S',
                                     time.localtime(float(ts)))
            except (TypeError, ValueError):
                tstr = str(ts)
            out.append((tstr, r.get('user', ''), r.get('cmd', ''),
                        1 if r.get('success') else 0,
                        r.get('elapsed_ms', 0.0), r.get('sql', '')))
        return out


class _AuditLevels:
    """给 `AuditLogger._classify` 当 `self` 用的最小载体（只需 LEVELS）"""

    LEVELS = {'NONE': 0, 'DCL': 1, 'DDL': 2, 'DML': 3, 'ALL': 4}

    @classmethod
    def sync(cls):
        try:
            from ..security.audit import AuditLogger
            cls.LEVELS = dict(AuditLogger.LEVELS)
        except Exception:                        # pragma: no cover
            pass


class Executor:

    def __init__(self, db):
        self.db = db
        self.info_schema = InformationSchema(db)
        # CTE 注册表：名字 -> _CTERelation（仅在一次语句执行期间有效）
        self._cte_relations = {}
        self.stats = {
            'queries': 0,
            'rows': 0,
            'commits': 0,
        }

        # ★ 修复：初始化 optimizer 和 stats_manager
        try:
            from .optimizer import Optimizer
            from .stats import StatsManager
            self.stats_manager = StatsManager(
                str(db.data_dir))
            self.optimizer = Optimizer(
                db, self.stats_manager)
        except Exception as e:
            logger.warn(f"初始化 optimizer 失败: {e}")
            self.optimizer = None
            self.stats_manager = None

        # 兼容别名：测试与部分调用方用的是 stats_mgr
        self.stats_mgr = self.stats_manager

        # ★ 语句审计（`SHOW AUDIT LOG` 的数据源）
        try:
            self.audit = _AuditSink(str(db.data_dir))
            # 让 `Database.shutdown()` 成为审计待落盘批的确定性落盘点
            attach = getattr(db, 'attach_audit_sink', None)
            if callable(attach):
                attach(self.audit)
        except Exception as e:                   # pragma: no cover
            logger.warn(f"初始化审计日志失败: {e}")
            self.audit = None

        # 密码策略（`CREATE USER` 强口令校验用；`SET PASSWORD POLICY` 可改）
        try:
            from ..security.password_policy import PasswordPolicy
            from ..config import Config
            cfg = Config()
            self.password_policy = PasswordPolicy(
                min_length=int(cfg.password_min_length or 6),
                require_digit=bool(cfg.password_require_digit),
                deny_weak=bool(cfg.password_deny_weak))
        except Exception as e:                   # pragma: no cover
            logger.warn(f"初始化密码策略失败: {e}")
            self.password_policy = None

        # IP 白名单（`ADD IP WHITELIST` / `SHOW IP WHITELIST`）
        try:
            from ..security.ip_whitelist import IPWhitelist
            from ..config import Config
            cfg = Config()
            self.ip_whitelist = IPWhitelist(
                enabled=bool(cfg.ip_whitelist_enabled),
                entries=list(cfg.ip_whitelist or []))
        except Exception as e:                   # pragma: no cover
            logger.warn(f"初始化 IP 白名单失败: {e}")
            self.ip_whitelist = None

        # 存储过程 / 触发器 / 存储函数（SQL 层 DDL 的落地位置）
        try:
            from .stored import StoredManager
            self._stored = StoredManager(str(db.data_dir))
        except Exception as e:                   # pragma: no cover
            logger.warn(f"初始化存储过程管理器失败: {e}")
            self._stored = None
        # 公开别名：`tests/test_batch12.py::test_function` 直接读
        # `ex.stored_mgr.functions`（存储函数与过程共用同一个管理器，
        # 定义一起落在 `<data_dir>/_stored.json`）。
        self.stored_mgr = self._stored
        # ★ JOIN 查询由 `plan_executor.PlanExecutor` 执行，它的静态函数名
        # 校验（`PlanExecutor._check_expr_functions`）看不到本执行器的
        # StoredManager（那个文件不在本次可改范围内）。这里把「名字是不是
        # 存储函数」的查询按数据目录登记到 parser 的注册表，两边用同一份
        # 答案：JOIN 的投影/WHERE 里调用存储函数不会被误判成"不支持的函数"。
        try:
            from .parser import register_stored_function_lookup
            register_stored_function_lookup(
                str(db.data_dir), self._has_stored_function,
                self._call_stored_function)
        except Exception as e:                   # pragma: no cover
            logger.warn(f"登记存储函数查询钩子失败: {e}")
        try:
            from .trigger import TriggerManager
            self._trigger_mgr = TriggerManager(str(db.data_dir))
        except Exception as e:                   # pragma: no cover
            logger.warn(f"初始化触发器管理器失败: {e}")
            self._trigger_mgr = None

        # PL 循环的迭代上限（默认 100 万；测试/运维可调小）
        # 也可以在会话里写 `SET SESSION pl_max_iterations = N`（优先）。
        self._pl_max_iterations = PL_DEFAULT_MAX_ITERATIONS

        # ★ 事件调度器（可选）
        self._event_scheduler = None

        # ★ 并行执行器（`tests/test_batch14.py::test_parallel_count` 读
        # `Executor.parallel`，原实现没有该属性 → AttributeError）。
        # 它接的是**真的** `lightdb.parallel.executor.ParallelExecutor`：
        # 列式分片聚合/扫描（柱存 `ColumnStore` 与 `TableColumnView`
        # 适配的存储表都能跑）。当前被真正使用的入口见
        # `_is_plain_count` / `_parallel_count`（单表裸 COUNT 走并行分片，
        # 行数 ≥ `parallel_min_rows` 才启用）；其余 SQL 仍走串行路径，
        # 因为本引擎的查询路径（JOIN/子查询/表达式投影/WHERE 下推）没有
        # 做成可交换的分片算子，硬接会改变结果语义。
        #
        # `parallel` 是**惰性属性**（`_parallel` 为 None 时才建）：
        # 普通串行查询不应该平白多出一个线程池（全量测试里每个用例都建
        # Executor，提前建池会放大资源压力、让计时类断言更容易抖动）。
        # 构造 `ThreadPoolExecutor` 本身不建线程，线程在第一次 submit 才起。
        # 受限环境（沙箱禁命名管道等）里构造失败 → 置 None，并行入口自动
        # 退回串行。
        self.parallel_enabled = True
        self._parallel = None

        # ★ 阶段 2：UPDATE/DELETE 的 binlog 镜像改为从 WAL 增量取（少一次
        #   SELECT）。autocommit 路径需要"表级语句锁"把一段 WAL 记录圈定为
        #   本语句所有（见 `_binlog_table_guard`）；锁按表惰性创建。
        self._binlog_table_locks = {}
        self._binlog_locks_guard = threading.Lock()

        # ★ 决策 2 / 4A.2：OLAP 向量化**显式分派**开关（默认开启，
        #   2026-10-20 用户要求；LIGHTDB_OLAP_DISPATCH=0 可关闭）。
        #   仅在开关打开时才延迟 import `lightdb/olap/sql_dispatch.py`；
        #   关闭时 SQL 路径不加载任何 OLAP/列存引擎模块（测试用 import 钩子锁住）。
        #   取值来源：环境变量（LIGHTDB_OLAP_*）→ Config 默认值；测试可直接
        #   改实例属性（如 `ex.olap_dispatch = False`）。
        try:
            from ..config import Config
            _olap_cfg = Config.from_env()
            self.olap_dispatch = bool(
                getattr(_olap_cfg, 'olap_dispatch', True))
            self.olap_dispatch_min_rows = int(
                getattr(_olap_cfg, 'olap_dispatch_min_rows', 1000))
            self.olap_crosscheck = bool(
                getattr(_olap_cfg, 'olap_crosscheck', False))
            self.olap_parallel_workers = int(
                getattr(_olap_cfg, 'olap_parallel_workers', 0))
        except Exception:                     # pragma: no cover - 防御
            self.olap_dispatch = True
            self.olap_dispatch_min_rows = 1000
            self.olap_crosscheck = False
            self.olap_parallel_workers = 0
        #: crosscheck 参考执行期间置 True，防止行存参考路径再次分派（递归）
        self._olap_bypass = False

    # ==================== 主入口 ====================

    # ★ GROUP BY 严格模式开关（逃生口，与 MySQL 的 ONLY_FULL_GROUP_BY 等价）
    #   None（默认）-> 由环境变量 LIGHTDB_STRICT_GROUP_BY 决定，未设置即**严格**
    #   False       -> 关闭严格模式，恢复旧行为（非分组键裸列取「组内第一行」）
    #   True        -> 强制严格
    # 用法（类属性，对所有实例生效）：
    #   from lightdb.sql.executor import Executor
    #   Executor.strict_group_by = False        # 旧行为
    # 或进程级：set LIGHTDB_STRICT_GROUP_BY=0
    strict_group_by = None

    def _check_strict_group_by(self, p):
        """GROUP BY 严格校验（静态，见 `parser.validate_group_by`）

        校验只吃解析后的 AST，不需要任何行数据，因此挂在 `_cmd_select`
        的最前面：单表 / JOIN / CTE / FROM 子查询 / UNION 各分支都在那里
        汇合，报错必然发生在真正取数之前（不会先算一半再失败）。

        统一路径（`_group_rows`）与计划路径（`plan_executor._exec_GroupBy`）
        还会各自再兜一次底，兜底用的是同一份实现，报错信息完全一致。
        """
        from .parser import validate_group_by, resolve_strict_group_by
        validate_group_by(
            p, strict=resolve_strict_group_by(self.strict_group_by))

    def execute(self, parsed: Dict, session: Dict) -> Dict:
        start = time.time()
        # 记录"当前会话"：投影列表里的标量子查询需要把折叠后的子查询
        # 重新交给 self.execute 执行（复用事务读视图 / CTE 关系）。
        # 有些内部路径（FROM 子查询物化、JOIN 计划执行）拿不到 session，
        # 由这里兜底，避免子查询在无事务上下文里重新执行。
        prev_session = getattr(self, '_active_session', None)
        self._active_session = session
        result = None
        try:
            # ★ P1 阶段 1.1：执行期鉴权（在**分发之前**做，拒绝的语句不会
            #   产生任何副作用）。网络路径的 `session['user']` 来自握手
            #   （net/server.py），进程内/内部路径没有该键时视为可信内部
            #   调用（不是数据库账户，详见 `_require_privilege`）。
            denied = self._require_privilege(parsed, session)
            if denied is not None:
                result = denied
            elif isinstance(parsed, dict) and parsed.get('ctes'):
                result = self._execute_with_ctes(parsed, session)
            else:
                result = self._dispatch(parsed, session)
            self.stats['queries'] += 1
            elapsed = time.time() - start
            self._record_audit(parsed, session, result, elapsed)
            if elapsed > 0.5:
                logger.warn(
                    f"慢查询 {elapsed:.3f}s: "
                    f"{parsed.get('command')}")
                try:
                    logger.slow(
                        sql=str(parsed)[:500],
                        elapsed=elapsed,
                        user=session.get('user', ''),
                        threshold=0.5)
                except Exception:
                    pass
            return result
        except KeyError as e:
            # 失败的语句同样进审计（success=0 + 错误原文），
            # 否则"语句失败"这件事在审计里就是空白。
            #
            # 语义码修正：此前一律报 1146「表不存在」，于是**未知列**也
            # 被说成"表不存在"，排障会被误导。MySQL 把两者分开：
            #   1146 = 表不存在；1054 = 未知的列。
            # 这里用执行器已有的信息判别：KeyError 的键名与语句主表同名
            # 才当"表不存在"，否则当"未知列"。
            _missing = str(e).strip("'\"")
            if '表' in _missing and '不存在' in _missing:
                result = self._err(1146, _missing)
            elif '列' in _missing:
                result = self._err(1054, _missing)
            else:
                _tbl = parsed.get('table')
                if _tbl and _missing == _tbl:
                    result = self._err(1146, f"表不存在: {_missing}")
                else:
                    result = self._err(1054, f"未知的列: {_missing}")
            self._record_audit(parsed, session, result,
                               time.time() - start)
            return result
        except CheckConstraintError as e:
            # CHECK 约束违规 / 定义错误：**必须**排在 ValueError 之前
            # （CheckConstraintError 是它的子类），否则会被下面统一成
            # 误导性的 1062「主键重复」。errno 取异常自带的
            # 3819（违规）/ 3813（定义引用了不存在的列），
            # SQLSTATE 按 MySQL 是 HY000。
            result = self._err(
                getattr(e, 'errno', 3819), str(e),
                sqlstate=getattr(e, 'sqlstate', 'HY000'))
            self._record_audit(parsed, session, result,
                               time.time() - start)
            return result
        except DdlError as e:
            # DDL 的显式报错（见 `db/errors.py`）：错误码是 db 层给的真实
            # 语义码（1235 未支持 / 1054 未知列 / 1060 列重复 / 1091 不能删
            # / 1138 NOT NULL 无默认值 …），这里**原样**带给客户端 ——
            # 绝不能落进下面的 ValueError 分支被压成 1105/1062。
            result = self._err(e.errno, str(e), sqlstate=e.sqlstate)
            self._record_audit(parsed, session, result,
                               time.time() - start)
            return result
        except ValueError as e:
            # 修复 C：存储层现在抛**带 errno/sqlstate 的专用异常**
            # （`db/typecheck.py::TypeCheckError`：1264 越界 / 1406 超长 /
            # 1292 非法日期 / 1366 类型错 / 1048 空值）。这里优先采用
            # 异常自带的语义码，`_storage_errno` 只作为旧异常（没带码）的
            # 兼容兜底 —— 不再按报文猜，避免措辞变化让错误码漂移。
            result = self._err(
                getattr(e, 'errno', None) or self._storage_errno(e),
                str(e),
                sqlstate=getattr(e, 'sqlstate', None))
            self._record_audit(parsed, session, result,
                               time.time() - start)
            return result
        except Exception as e:
            logger.error(f"执行错误: {e}")
            # 语义码修正：此前一律 1105。存储层有些错误抛的是 TypeError
            # 或自定义异常（例如 "无法把 'abc' 转成 INT"），落到这里就被
            # 说成"通用错误"。同样优先取异常自带的 errno/sqlstate
            # （修复 C 的 TypeCheckError 走 ValueError 分支，这里兜的是
            # 其它自定义异常），没有才按报文归类，**不再一律 1105**。
            result = self._err(
                getattr(e, 'errno', None) or self._storage_errno(e),
                str(e),
                sqlstate=getattr(e, 'sqlstate', None))
            self._record_audit(parsed, session, result,
                               time.time() - start)
            return result
        finally:
            # ★ 真 binlog 收尾：autocommit 组提交/丢弃、DDL 事件。
            #   放在 finally 里保证错误返回/异常分支同样执行（失败不落盘）。
            try:
                self._binlog_finish(session, parsed, result)
            except Exception as e:              # pragma: no cover - 防御
                logger.error(f"binlog 收尾失败: {e}")
            self._active_session = prev_session

    def _use_session(self, session):
        """取本次执行使用的会话（显式传参优先，其次 execute() 记录的会话）"""
        if isinstance(session, dict):
            return session
        active = getattr(self, '_active_session', None)
        return active if isinstance(active, dict) else {}

    @staticmethod
    def _storage_errno(e) -> int:
        """把存储层异常归类成 MySQL 语义码

        语义码修正（本会话）：此前 `ValueError` 一律 1062（主键/唯一冲突
        专用码）、其它一律 1105，于是 NOT NULL 违反与类型转换失败都被
        说成"主键重复"，排障方向完全错。现在按 MySQL 语义区分：
            1048 = Column cannot be null
            1062 = Duplicate entry（主键/唯一冲突）
            1366 = Incorrect value（类型/取值不合法）
            1105 = 其它（通用），**不再硬套 1062**

        局限（已记录）：存储层目前只用内置异常与少数自定义异常，没有可
        依赖的类型层级，因此这里靠报文判别。P1 应改为在存储层抛专用异常
        （NotNullViolation / DuplicateKey / IncorrectValue），届时删掉本方法。
        """
        msg = str(e)
        low = msg.lower()
        if ('不允许为空' in msg or 'cannot be null' in low
                or 'not null' in low):
            return 1048
        if ('重复' in msg or 'duplicate' in low
                or '唯一' in msg or 'unique' in low):
            return 1062
        if ('转成' in msg or 'invalid literal' in low
                or 'incorrect' in low or '取值范围' in msg):
            return 1366
        return 1105

    def _record_audit(self, parsed, session, result, elapsed):
        """把本条语句写入审计（`SHOW AUDIT LOG` 的数据源）

        审计本身出错绝不影响语句执行结果（记录失败只告警），
        但审计记录的内容是**真实执行结果**：SQL 原文取自 AST 的
        `source_sql`，成功/失败与错误文本取自执行器返回值。
        """
        if self.audit is None or not isinstance(parsed, dict):
            return
        try:
            ok = not (isinstance(result, dict)
                      and result.get('type') == 'error')
            err = ''
            if not ok:
                err = str(result.get('message', ''))
            self.audit.record(
                str((session or {}).get('user') or ''),
                str(parsed.get('command') or ''),
                str(parsed.get('source_sql')
                    or parsed.get('command') or ''),
                ok, err, elapsed)
        except Exception as e:                   # pragma: no cover
            logger.warn(f"审计记录失败: {e}")

    def execute_sql(self, sql: str, session: Dict = None) -> Dict:
        """便捷方法：直接执行 SQL 字符串"""
        from .parser import parser
        try:
            parsed = parser.parse(sql)
        except (SyntaxError, ValueError) as e:
            # **定义期拒绝**的语句（例如 `AUTO_INCREMENT` / 不支持的
            # `DEFAULT` 形态，见 `parser._column_def`）必须变成"显式错误
            # 结果"，而不是把解析异常抛给调用方：抛出去的话（a）调用方
            # 拿不到 errno/报文，（b）会绕过 `_record_audit` 的失败记录。
            # 服务端那条路径（`net/server.py`）本来就是这么处理的，这里
            # 让进程内路径与它行为一致：要么真实，要么显式报错。
            return self._err(1064, f"SQL 解析错误: {str(e)[:200]}")
        if parsed is None:
            head = (str(sql).strip().split(None, 1) or [''])[0].upper()
            if head == 'ALTER':
                # 兜底：解析器认不出的 ALTER 形态。已实现的变体见
                # `parser._alter_table`（ADD COLUMN / DROP COLUMN / RENAME TO），
                # 其余变体会走 `_cmd_alter_table` 的 UNSUPPORTED 分支回 1235。
                return self._err(
                    1235,
                    f"ALTER 语句无法解析或形态不支持：{sql[:120]}"
                    f"（本引擎只支持 ALTER TABLE 的 ADD COLUMN / "
                    f"DROP COLUMN / RENAME TO）")
            return self._err(1064, f"无法解析 SQL: {sql[:80]}")
        # 注意：不能用 `session or {}` —— 空 dict 是 falsy，会新建一个字典，
        # 调用方传进来的会话就丢了（BEGIN 写的 s['txn'] 传不回去，
        # 导致 BEGIN/ROLLBACK 各自变成 autocommit，回滚静默失效）。
        return self.execute(
            parsed, session if session is not None else {})

    def _dispatch(self, p: Dict, s: Dict) -> Dict:
        cmd = p['command']
        handler = getattr(
            self,
            f'_cmd_{cmd.replace(" ", "_").lower()}',
            None)
        if handler:
            return handler(p, s)
        return self._err(1064, f"不支持的语句: {cmd}")

    # ==================== 执行期鉴权（P1 阶段 1.1） ====================
    #
    # 事实来源是 `Database.users[user]['privs']`（与握手共用同一份用户表），
    # 权限模型见 `security/privileges.py`。矩阵：
    #
    #   命令                      所需权限           拒绝码
    #   SELECT / SHOW / DESC      SELECT             1142（表级）
    #   INSERT/UPDATE/DELETE      对应 DML           1142
    #   CREATE/ALTER/DROP/...     对应 DDL           1142（表级）/1044（库级）
    #   TRUNCATE                  DROP               1142
    #   INDEX（全文索引）           INDEX              1142
    #   USE / SHOW TABLES         库上任意权限/ SELECT  1044
    #   DCL / 管理语句            全局 ALL/root       1227
    #   未知命令                   管理员             1227（fail-closed）
    #
    # 错误报文照 MySQL 语义：表级 1142「<CMD> command denied to user
    # 'u'@'host' for table 't'」、库级 1044「Access denied for user ... to
    # database 'db'」、管理语句 1227「Access denied; you need ...」。
    # SQLSTATE 统一 42000（访问拒绝类）。

    #: 需要「表级」权限的命令 → 所需权限
    _PRIV_TABLE_COMMANDS = {
        'SHOW COLUMNS': 'SELECT',   # SHOW COLUMNS / DESC / DESCRIBE
        'INSERT': 'INSERT',
        'UPDATE': 'UPDATE',
        'DELETE': 'DELETE',
        'CREATE TABLE': 'CREATE',
        'DROP TABLE': 'DROP',
        'ALTER TABLE': 'ALTER',
        'ALTER UNSUPPORTED': 'ALTER',
        'TRUNCATE': 'DROP',
        'CREATE FULLTEXT INDEX': 'INDEX',
        'CREATE INDEX': 'INDEX',
        'DROP INDEX': 'INDEX',
        'CREATE TRIGGER': 'CREATE',
        'ANALYZE': 'SELECT',
    }
    #: 需要「库级」权限的命令 → 所需权限（None = 该库上任意权限即可）
    _PRIV_DB_COMMANDS = {
        'USE': None,
        'SHOW TABLES': 'SELECT',
        'CREATE PROCEDURE': 'CREATE',
        'CREATE FUNCTION': 'CREATE',
        'DROP FUNCTION': 'DROP',
        'DROP TRIGGER': 'DROP',
    }
    #: 需要管理员（全局 ALL / root）的命令
    _ADMIN_COMMANDS = frozenset({
        'GRANT', 'REVOKE', 'CREATE USER', 'DROP USER',
        'SET PASSWORD POLICY', 'ADD IP WHITELIST',
        'REMOVE IP WHITELIST', 'DELETE IP WHITELIST',
        'START SLAVE', 'STOP SLAVE', 'CHANGE MASTER', 'SHOW STATS',
    })
    #: 以结果集回传的 SHOW 标签 → 需要管理员的那些
    _ADMIN_SHOW_TAGS = frozenset({
        'security', 'audit_log', 'ip_whitelist', 'master_status',
        'slave_status', 'binlog_events', 'vacuum', 'optimize_table',
        'check_table', 'engine',
    })
    #: SHOW 标签 → 需要 SELECT（有表名按表级 1142，否则库级 1044）
    _SELECT_SHOW_TAGS = frozenset({
        'create_table', 'index', 'partitions', 'triggers', 'functions',
    })
    #: 任何已登录账户都可以执行的命令（无数据访问 / 会话控制）
    _PUBLIC_COMMANDS = frozenset({
        'BEGIN', 'COMMIT', 'ROLLBACK', 'SELECT VARIABLES',
        'CALL', 'EXPLAIN', 'SHOW DATABASES',
    })

    #: 引擎内部的**服务身份**（不是数据库账户）。事件调度器等内部组件用
    #: 它们执行语句（见 sql/event.py）；只有当同名数据库账户**不存在**时
    #: 才按内部身份处理 —— 这样真实账户 'system' 不会被误当服务身份。
    _INTERNAL_SERVICE_USERS = frozenset({'system'})

    @staticmethod
    def _session_user(session):
        return (session.get('user')
                if isinstance(session, dict) else None)

    def _is_internal_session(self, session) -> bool:
        """无 user 键 = 进程内/内部调用；或未注册的内部服务身份。

        网络路径在握手成功时一定会写入 `session['user']` 且该用户必然
        存在于 `db.users`（否则 1045），因此这里只给测试/内部组件放行，
        **不会**给任何网络请求放行。
        """
        user = self._session_user(session)
        if not user:
            return True
        if user in self._INTERNAL_SERVICE_USERS:
            users = getattr(self.db, 'users', None) or {}
            return not isinstance(users.get(user), dict)
        return False

    def _session_host(self, session, record=None) -> str:
        host = (session.get('host')
                if isinstance(session, dict) else None)
        if not host and isinstance(record, dict):
            host = record.get('host')
        return str(host or '%')

    def _target_database(self, session) -> str:
        """当前会话库（未选库 = 引擎真实单库名）"""
        db = (session.get('database')
              if isinstance(session, dict) else None)
        return str(db or self.db.name)

    def _deny_table(self, cmd, table, user, host) -> Dict:
        display = str(cmd or 'ACCESS').split()[0].upper()
        return self._err(
            1142,
            f"{display} command denied to user '{user}'@'{host}' "
            f"for table '{table}'",
            sqlstate='42000')

    def _deny_db(self, user, host, database) -> Dict:
        return self._err(
            1044,
            f"Access denied for user '{user}'@'{host}' "
            f"to database '{database}'",
            sqlstate='42000')

    def _deny_admin(self, cmd, user, host) -> Dict:
        needed = str(cmd or 'ADMIN').split()[0].upper()
        return self._err(
            1227,
            f"Access denied; you need (at least one of) the {needed} "
            f"privilege(s) for this operation",
            sqlstate='42000')

    def _is_internal_session(self, session) -> bool:
        """无 user 键 = 进程内/内部调用；或未注册的内部服务身份。

        网络路径在握手成功时一定会写入 `session['user']` 且该用户必然
        存在于 `db.users`（否则 1045），因此这里只给测试/内部组件放行，
        **不会**给任何网络请求放行。
        """
        user = self._session_user(session)
        if not user:
            return True
        if user in self._INTERNAL_SERVICE_USERS:
            users = getattr(self.db, 'users', None) or {}
            return not isinstance(users.get(user), dict)
        return False

    def _require_privilege(self, parsed, session) -> Optional[Dict]:
        """执行期鉴权：返回 None = 放行，否则返回错误结果字典

        在 `execute()` 分发**之前**调用，拒绝的语句不会执行、不会改数据。
        """
        if not isinstance(parsed, dict):
            return None
        if self._is_internal_session(session):
            return None
        from ..security.privileges import (
            has_any_privilege_on_db, has_privilege, is_admin)
        user = self._session_user(session)
        users = getattr(self.db, 'users', None) or {}
        record = users.get(user) if isinstance(user, str) else None
        host = self._session_host(session, record)
        cmd = str(parsed.get('command') or '')

        # ---- 未知用户：按未授权处理（绝不静默放行） ----
        if not isinstance(record, dict):
            return self._deny_unknown(cmd, parsed, user, host)

        if is_admin(users, user):
            return None

        # ---- SHOW 系列 ----
        show_tag = str(parsed.get('show_stmt') or '').lower()
        if show_tag:
            return self._check_show_privilege(
                show_tag, parsed, user, host, session)

        # ---- UNION：逐段检查（每段都是独立的 SELECT） ----
        if cmd == 'UNION':
            for seg in parsed.get('segments') or []:
                err = self._require_privilege(seg, session)
                if err is not None:
                    return err
            return None

        # ---- 单表 SELECT（含 JOIN / 子查询 / CTE / 系统表） ----
        if cmd == 'SELECT':
            return self._check_select_privilege(parsed, session, user, host)

        # ---- 表级权限命令 ----
        priv = self._PRIV_TABLE_COMMANDS.get(cmd)
        if priv:
            table = parsed.get('table')
            if not table:
                return self._deny_db(
                    user, host, self._target_database(session))
            if has_privilege(users, user, priv, self.db.name):
                return None
            return self._deny_table(cmd, table, user, host)

        # ---- 库级权限命令 ----
        if cmd in self._PRIV_DB_COMMANDS:
            need = self._PRIV_DB_COMMANDS[cmd]
            database = (parsed.get('database')
                        if cmd == 'USE'
                        else self._target_database(session))
            database = database or self.db.name
            if (str(database).lower() == 'information_schema'
                    and self.db.is_known_database(database)):
                # 系统库保持"最小可用"：任何已登录账户可查看元数据
                return None
            if need is None:
                allowed = has_any_privilege_on_db(
                    users, user, database)
            else:
                allowed = has_privilege(users, user, need, database)
            if allowed:
                return None
            return self._deny_db(user, host, database)

        # ---- 管理员命令 ----
        if cmd in self._ADMIN_COMMANDS:
            return self._deny_admin(cmd, user, host)

        # ---- 公共命令（EXPLAIN 还要检查内层语句） ----
        if cmd in self._PUBLIC_COMMANDS:
            if cmd == 'EXPLAIN':
                inner = parsed.get('inner')
                if isinstance(inner, dict):
                    return self._require_privilege(inner, session)
            return None

        # ---- 未知命令：fail-closed（只有管理员能过） ----
        return self._deny_admin(cmd, user, host)

    def _deny_unknown(self, cmd, parsed, user, host) -> Dict:
        """未知（或已删除）用户：按命令目标返回对应的 MySQL 拒绝码"""
        if cmd == 'SELECT' or cmd == 'UNION' or cmd in self._PRIV_TABLE_COMMANDS:
            table = parsed.get('table') or ''
            if not table:
                return self._deny_db(user, host, self._target_database({}))
            return self._deny_table(cmd or 'SELECT', table, user, host)
        if cmd in self._PRIV_DB_COMMANDS:
            database = parsed.get('database') or self._target_database({})
            return self._deny_db(user, host, database)
        if cmd in self._PUBLIC_COMMANDS:
            return self._deny_admin(cmd, user, host)
        return self._deny_admin(cmd, user, host)

    def _check_show_privilege(self, show_tag, parsed, user, host,
                              session) -> Optional[Dict]:
        """SHOW 类语句的鉴权（结果集以 command='SELECT' + show_stmt 回传）"""
        from ..security.privileges import has_privilege
        users = getattr(self.db, 'users', None) or {}
        if show_tag in self._ADMIN_SHOW_TAGS:
            return self._deny_admin('SHOW ' + show_tag, user, host)
        if show_tag == 'grants':
            # 非管理员只能看自己的授权（FOR 别人 → 拒绝）
            target = parsed.get('grant_user')
            actor = self._session_user(session)
            if target and str(target) != str(actor):
                return self._deny_table(
                    'SHOW GRANTS', 'user', user, host)
            return None
        if show_tag not in self._SELECT_SHOW_TAGS:
            # 未登记的 SHOW：fail-closed
            return self._deny_admin('SHOW ' + show_tag, user, host)
        table = parsed.get('table')
        database = self._target_database(session)
        if table:
            if has_privilege(users, user, 'SELECT', self.db.name):
                return None
            return self._deny_table('SHOW', table, user, host)
        if has_privilege(users, user, 'SELECT', database):
            return None
        return self._deny_db(user, host, database)

    def _check_select_privilege(self, parsed, session, user,
                                host) -> Optional[Dict]:
        """SELECT 的鉴权：普通表 1142；information_schema 最小可用"""
        from ..security.privileges import has_privilege
        users = getattr(self.db, 'users', None) or {}
        table = parsed.get('table')
        if not table:
            # FROM-less（SELECT 1）不需要数据权限；FROM 子查询按内层检查
            sub = parsed.get('subquery')
            if isinstance(sub, dict) and sub.get('sql'):
                try:
                    from .parser import parser
                    inner = parser.parse(sub['sql'])
                except Exception:                    # pragma: no cover - 防御
                    inner = None
                if isinstance(inner, dict):
                    return self._require_privilege(inner, session)
            return None
        tname = str(table)
        if tname.upper() == '__BINLOG_FETCH__':
            # 内部复制接口（binlog 拉取）只给管理员
            return self._deny_admin('SELECT', user, host)
        if tname.lower().startswith('information_schema.'):
            return None
        if (tname.upper() in ('TABLES', 'COLUMNS', 'SCHEMATA')
                and tname not in self.db.tables):
            return None
        # CTE 名：物化时内层查询会各自鉴权，这里放行
        cte_names = {
            str(c.get('name') or '').lower()
            for c in (parsed.get('ctes') or [])
            if isinstance(c, dict)
        }
        if tname.lower() in cte_names:
            return None
        if not has_privilege(users, user, 'SELECT', self.db.name):
            return self._deny_table('SELECT', tname, user, host)
        # JOIN 的其它表：同一库内，权限相同；内部虚拟表额外要求管理员
        for join in (parsed.get('joins') or []):
            jt = (join or {}).get('table') if isinstance(join, dict) else None
            if not jt:
                continue
            if str(jt).upper() == '__BINLOG_FETCH__':
                return self._deny_admin('SELECT', user, host)
        return None

    def is_admin_session(self, session) -> bool:
        """当前会话是否管理员（net 层 SHOW/KILL 等内置分支复用）"""
        if self._is_internal_session(session):
            return True          # 内部调用 / 内部服务身份
        user = self._session_user(session)
        from ..security.privileges import is_admin
        return is_admin(getattr(self.db, 'users', None) or {}, user)

    def check_access(self, session, priv, database=None,
                     table=None) -> Optional[Dict]:
        """net 层复用的准入检查：None = 允许，否则返回 `_err` 结构错误

        `table` 非空 → 表级 1142；否则库级 1044；`priv=None` = 该库上任意
        权限即可。未知用户按未授权处理。
        """
        if self._is_internal_session(session):
            return None          # 内部调用 / 内部服务身份
        user = self._session_user(session)
        users = getattr(self.db, 'users', None) or {}
        record = users.get(user)
        host = self._session_host(session, record)
        if not isinstance(record, dict):
            if table:
                return self._deny_table(priv or 'ACCESS', table, user, host)
            return self._deny_db(user, host,
                                 database or self._target_database(session))
        from ..security.privileges import (
            has_any_privilege_on_db, has_privilege, is_admin)
        if is_admin(users, user):
            return None
        if (database is not None
                and str(database).lower() == 'information_schema'):
            return None
        if table:
            if has_privilege(users, user, priv or 'SELECT', self.db.name):
                return None
            return self._deny_table(priv or 'ACCESS', table, user, host)
        if priv is None:
            allowed = has_any_privilege_on_db(users, user, database)
        else:
            allowed = has_privilege(users, user, priv, database)
        if allowed:
            return None
        return self._deny_db(user, host,
                             database or self._target_database(session))

    def visible_databases(self, session) -> List[str]:
        """当前会话可见的库（非管理员只看到有权限的库 + information_schema）"""
        names = list(self.db.database_names())
        if self._is_internal_session(session):
            return names
        user = self._session_user(session)
        from ..security.privileges import (
            has_any_privilege_on_db, is_admin)
        users = getattr(self.db, 'users', None) or {}
        if is_admin(users, user):
            return names
        if not isinstance(users.get(user), dict):
            return []
        visible = []
        for name in names:
            if str(name).lower() == 'information_schema':
                visible.append(name)     # 系统库保持最小可用
            elif has_any_privilege_on_db(users, user, name):
                visible.append(name)
        return visible


    # ==================== 结果构造 ====================

    def _ok(self, affected: int = 0,
            message: str = "") -> Dict:
        return {
            'type': 'ok',
            'affected': affected,
            'message': message,
        }

    def _rs(self, cols, rows) -> Dict:
        return {
            'type': 'resultset',
            'columns': [str(c) for c in cols],
            'rows': [tuple(r) for r in rows],
        }

    def _err(self, code: int, msg: str, sqlstate: str = None) -> Dict:
        out = {
            'type': 'error',
            'code': code,
            'message': msg,
        }
        if sqlstate:
            # 只在调用方明确给出时带上（CHECK 约束是 3819 / HY000）；
            # 其余路径的返回结构保持不变。
            out['sqlstate'] = sqlstate
        return out

    # ==================== 真 · append-only binlog（决策 D 项） ====================
    #
    # 记录时机（与 MySQL 语义对齐：binlog 记的是"已提交的变更"）：
    #   * DML：执行器把行镜像（INSERT 的 row；UPDATE 的 old/pk/new；DELETE 的
    #     old/pk）缓冲到当前组（显式事务用事务号，autocommit 语句用一次性
    #     组 id），**提交时**由 `Database.commit()` / `commit_statement()`
    #     成组写成 `BEGIN → rows… → COMMIT`；
    #   * DDL：执行器成功返回后写一组 `BEGIN → DDL → COMMIT`
    #     （DDL 自带隐式提交；分组只为统一读取端边界语义）；
    #   * 回滚：`Database.abort()` 写 `BEGIN → ROLLBACK`（行事件从未落盘，
    #     只记 discarded 条数，不伪造变更）。
    #
    # 已知边界（TODO 阶段 2）：直接写存储层的调用不经过执行器，因此不产生
    # binlog 事件。

    _BINLOG_DML_COMMANDS = frozenset({'INSERT', 'UPDATE', 'DELETE'})
    _BINLOG_DDL_COMMANDS = frozenset({
        'CREATE TABLE', 'DROP TABLE', 'ALTER TABLE', 'TRUNCATE',
        'CREATE FULLTEXT INDEX'})

    def _binlog_writer(self):
        """当前库的真 binlog 写器；未启用/不可用时 None（不抛异常）"""
        try:
            return self.db.binlog_log
        except Exception:                       # pragma: no cover - 防御
            return None

    def _binlog_record_enabled(self) -> bool:
        if getattr(self.db, 'binlog_suspended', False):
            return False
        return self._binlog_writer() is not None

    @staticmethod
    def _binlog_wal_images_enabled() -> bool:
        """UPDATE/DELETE 镜像是否走 WAL 增量（阶段 2 优化，默认开）

        ``LIGHTDB_BINLOG_MIRROR=legacy`` 可退回"语句前 SELECT 取旧行"的
        阶段 1 行为——仅用于 A/B 计时对照与排障；生产默认走 WAL（少一次
        全表扫描）。两种模式产出的 binlog 镜像内容一致（同一批存储层行）。
        """
        raw = str(os.environ.get('LIGHTDB_BINLOG_MIRROR', '') or '').strip()
        return raw.lower() != 'legacy'

    def _binlog_group(self, s):
        """返回本次语句的行事件组 id

        * 显式事务 / 语句级内部事务：`('txn', txn_id)`，由 Database 的
          提交/回滚钩子负责组提交；
        * autocommit 单条语句：会话上挂一个一次性组 id，语句成功返回时
          由 `execute()` 的 finally 统一组提交（失败则丢弃，不落盘）。
        """
        rec = self._binlog_writer()
        if rec is None:
            return None
        txn = s.get('txn') if isinstance(s, dict) else None
        if txn:
            return rec.txn_group_id(txn['txn_id'])
        if isinstance(s, dict):
            gid = s.get('_binlog_gid')
            if gid is None:
                gid = rec.new_group_id()
                s['_binlog_gid'] = gid
            return gid
        return rec.new_group_id()

    def _binlog_record(self, s, event_type: str, table, data: dict):
        """缓冲一条行变更事件（提交时才落盘）"""
        if not self._binlog_record_enabled():
            return
        try:
            gid = self._binlog_group(s)
            rec = self._binlog_writer()
            txn = s.get('txn') if isinstance(s, dict) else None
            txn_id = txn['txn_id'] if txn else 0
            rec.record(gid, event_type, table, data, txn_id=txn_id)
        except Exception as e:
            self.db._binlog_errors = getattr(self.db, '_binlog_errors', 0) + 1
            logger.error(f"binlog 行事件缓冲失败（{event_type} {table}）: {e}")

    def _wal_dml_images(self, tbl, lsn_start, lsn_end, kind: str,
                        txn_id: int = None):
        """从本表 WAL 增量里取本次 UPDATE/DELETE 产生的行镜像

        阶段 2 优化（少一次 SELECT）：`Table.update/delete` 在写路径上已经
        把每个受影响行的 `old`/`new`/`pk` 镜像写进本表 WAL（崩溃恢复与
        undo 都依赖它）。执行器记录 binlog 行镜像时直接读这段**增量**
        （`WAL.read_from_lsn` 走文件增量缓存，不重扫数据页），因此语句
        执行前不再需要一次 `SELECT` 取旧行 —— 大表 UPDATE/DELETE 少一整
        趟全表扫描。

        正确性边界：
        * 显式事务：只取 `txn_id` 匹配的记录（同一事务内顺序执行，不会
          混入别的语句/会话）；
        * autocommit：`txn_id=None`，由调用方持有**表级语句锁**
          （`_binlog_table_guard`）保证这一段 WAL 只属于本语句，再按
          `[lsn_start+1, lsn_end]` + 表名 + 记录类型过滤；
        * `lsn_start` 为空（读不到）时返回空，调用方回退到"预先 SELECT"
          的旧路径（安全降级）。

        返回 ``[(record_type, data_dict), ...]``，data 里含 pk/old/new，
        与手工 SELECT 得到的是同一批行（同一存储层快照）。语句结束后
        未提交的 WAL 记录即本语句的变更（提交语义由 Database 负责）。
        """
        if lsn_start is None:
            return []
        try:
            recs = tbl.wal.read_from_lsn(int(lsn_start) + 1)
        except Exception as e:                   # pragma: no cover - 防御
            logger.warn(f"读取 WAL 增量失败（binlog 镜像回退到 SELECT）: {e}")
            return []
        want = str(kind or '').upper()
        out = []
        for rec in recs:
            try:
                if lsn_end is not None and int(getattr(rec, 'lsn', 0)) > int(lsn_end):
                    break
            except (TypeError, ValueError):
                pass
            if txn_id is not None:
                try:
                    if int(getattr(rec, 'txn_id', -1) or -1) != int(txn_id):
                        continue
                except (TypeError, ValueError):
                    continue
            rtype = str(getattr(rec, 'type', '') or '').upper()
            if rtype != want:
                continue
            data = dict(getattr(rec, 'data', None) or {})
            if data.get('table') not in (None, tbl.name):
                continue
            out.append((rtype, data))
        return out

    @contextlib.contextmanager
    def _binlog_table_guard(self, table_name: str):
        """语句级表锁（只在 autocommit + WAL 镜像路径使用）

        作用域只覆盖"读 WAL 增量取镜像"这条路径：保证从 `lsn_start` 到
        `lsn_end` 之间该表的 WAL 记录**只属于本语句**（不会被另一个连接
        的 UPDATE/DELETE 插进来）。显式事务不需要它（按 txn_id 过滤）。

        已知边界：绕过执行器直接写存储层（`Table.insert` 等）仍可能插入
        记录；这是本引擎既有的"binlog 只记执行器路径"边界（见模块说明）。
        """
        with self._binlog_locks_guard:
            lock = self._binlog_table_locks.get(table_name)
            if lock is None:
                lock = threading.RLock()
                self._binlog_table_locks[table_name] = lock
        with lock:
            yield

    @staticmethod
    def _binlog_coerce_row(tbl, row):
        """按列声明把行值转成真实类型（binlog 存**实际写入的值**）

        解析器的字面量是原文（`'1'` 而不是 `1`）；行镜像若原样存字符串，
        从库虽能再 coerce 一次，但"binlog 内容 = 实际落盘值"更忠实
        （对照 MySQL 行格式复制存的是列值本身）。转不了的值保留原样
        （例如函数默认值），不因 binlog 记录失败而中断业务语句。
        """
        from ..db.table import coerce
        out = []
        for i, v in enumerate(row):
            try:
                ctype = str(tbl.cols[i]['type'])
            except (IndexError, KeyError, TypeError):
                out.append(v)
                continue
            try:
                out.append(coerce(v, ctype))
            except Exception:
                out.append(v)
        return out

    def _binlog_finish(self, s, parsed, result):
        """语句收尾：autocommit 组提交/丢弃 + DDL 事件写入

        由 `execute()` 的 finally 统一调用（任何返回路径都会经过这里，
        包括错误字典返回与异常分支），因此"失败语句不落 binlog"有保证。
        """
        rec = self._binlog_writer()
        if rec is None or not isinstance(s, dict):
            return
        cmd = str(parsed.get('command') or '') if isinstance(parsed, dict) else ''
        ok = not (isinstance(result, dict) and result.get('type') == 'error')
        # 只有 DML/DDL 语句才"收尾"自己的组。SELECT / SHOW 等（包括触发器里
        # 嵌套执行的 SELECT）绝不能 pop 掉外层 DML 正在缓冲的组 —— 否则
        # 先扣住的行事件会被静默丢弃（触发器体里 SELECT 在 DML 之间的场景）。
        is_dml = cmd in self._BINLOG_DML_COMMANDS
        is_ddl = cmd in self._BINLOG_DDL_COMMANDS
        if not (is_dml or is_ddl):
            return
        gid = s.pop('_binlog_gid', None)
        try:
            if gid is not None and rec is not None:
                if ok and is_dml:
                    rec.commit_group(gid)
                else:
                    rec.discard_group(gid)
        except Exception as e:
            # 注意（阶段 2 残余边界，如实说明）：单行 autocommit 的数据持久
            # 点在 Table 内部（合成事务），这里 binlog 写失败时数据已提交，
            # 无法再回滚 —— 只告警并计数。strict 的"写失败拒绝提交"覆盖的是
            # Database.commit / commit_statement（显式事务、多值语句）。
            self.db._binlog_errors = getattr(self.db, '_binlog_errors', 0) + 1
            logger.error(
                f"binlog 语句组提交失败（{cmd}，autocommit 单语句："
                f"数据已提交，复制日志缺少该事件）: {e}")
        # DDL：成功的 DDL 才记录（失败/不支持的分支不回 OK，因此不落）
        if ok and is_ddl:
            try:
                sql = parsed.get('source_sql') or ''
                rec.write_ddl(sql, table=parsed.get('table'))
            except Exception as e:
                self.db._binlog_errors = getattr(self.db, '_binlog_errors', 0) + 1
                logger.error(f"binlog DDL 事件写入失败（{cmd}）: {e}")

    # ==================== DDL ====================

    #: E1：本引擎真实支持的字符集（存储/协议均为 UTF-8；不做 gbk/latin1
    #: 转码，因此这些字符集**显式拒绝**而不是"按 utf8 收下"）。
    #: `latin1` 是历史客户端兼容名（既有协议测试锁定可记录），但同样
    #: 不做转码 —— 服务端按 UTF-8 解码，非法字节显式 1366。
    OLAP_E1_CHARSETS = ('utf8', 'utf8mb4', 'utf8mb3', 'latin1')
    #: CREATE TABLE 的 CHARSET 选项：latin1 不做转码，表选项层**不接受**
    #: （避免"声明 latin1 实际按 UTF-8 存"的静默语义）。
    OLAP_E1_TABLE_CHARSETS = ('utf8', 'utf8mb4', 'utf8mb3')
    #: E1：可接受的连接排序规则（bin = 真实比较语义；`_general_ci` 仅作
    #: 连接元数据记录，不改变 Python 码点比较语义）。
    OLAP_E1_COLLATIONS = ('utf8mb4_bin', 'utf8_bin',
                          'utf8mb4_general_ci', 'utf8_general_ci')
    #: CREATE TABLE 的 COLLATE 选项：只有 `*_bin` 与真实比较语义一致；
    #: `_ci` 不接受（表级排序规则会影响比较，不能只记录不生效）。
    OLAP_E1_TABLE_COLLATIONS = ('utf8mb4_bin', 'utf8_bin')

    #: E2：CREATE/ALTER 里可接受的存储引擎名 → 展示名。它们全部映射到
    #: **LightDB 原生存储**（没有任何对应语义：InnoDB 的 MVCC/隔离级别、
    #: MyISAM 表锁、MEMORY 临时存储、CSV 文件格式都**未实现**），只作为
    #: Navicat 表设计器/导入向导的兼容别名持久化并如实回显。
    ENGINE_ALIASES = {
        'LIGHTDB': 'LightDB',
        'INNODB': 'InnoDB',
        'MYISAM': 'MyISAM',
        'MEMORY': 'MEMORY',
        'CSV': 'CSV',
        'HEAP': 'MEMORY',          # MySQL 的 HEAP 是 MEMORY 的旧名
    }

    @classmethod
    def _normalize_engine_name(cls, raw):
        """引擎名归一化；未知引擎抛 KeyError（调用方回显式 1064）"""
        key = str(raw or '').strip().upper()
        if not key:
            return 'LightDB'
        if key not in cls.ENGINE_ALIASES:
            raise KeyError(key)
        return cls.ENGINE_ALIASES[key]

    def _validate_table_options(self, options: dict):
        """校验 CREATE TABLE 表选项 → `(normalized, error_result|None)`

        E1 静默语义审计：旧实现把 ENGINE/CHARSET/COLLATE/AUTO_INCREMENT/
        COMMENT 整段丢掉（SHOW CREATE 里看不到）。这里逐项判定：
        支持的**真实生效**（持久化 + SHOW CREATE/STATUS 如实回显），
        不支持的一律显式错误（绝不接受后静默丢弃）。
        """
        import re as _re
        opts = dict(options or {})
        out = {'engine': 'LightDB', 'charset': 'utf8mb4',
               'collate': 'utf8mb4_bin', 'comment': None}
        engine = (opts.get('engine') or '').upper()
        if engine:
            try:
                out['engine'] = self._normalize_engine_name(engine)
            except KeyError:
                return None, self._err(
                    1064,
                    f"Unknown storage engine '{opts.get('engine')}': "
                    f"本引擎真实存储是 LightDB；兼容别名只接受 "
                    f"InnoDB / MyISAM / MEMORY / CSV / LightDB"
                    f"（别名不实现对应引擎语义，SHOW TABLE STATUS 的 "
                    f"Create_options 会标明 native storage）")
        charset = (opts.get('charset') or '').lower()
        if charset:
            if charset not in self.OLAP_E1_TABLE_CHARSETS:
                return None, self._err(
                    1115, f"Unknown character set: '{charset}'",
                    sqlstate='42000')
            out['charset'] = 'utf8' if charset in ('utf8', 'utf8mb3') \
                else 'utf8mb4'
            out['collate'] = ('utf8_bin' if out['charset'] == 'utf8'
                              else 'utf8mb4_bin')
        collate = (opts.get('collate') or '').lower()
        if collate:
            if collate not in self.OLAP_E1_TABLE_COLLATIONS:
                return None, self._err(
                    1273, f"Unknown collation: '{collate}'",
                    sqlstate='HY000')
            coll_charset = 'utf8' if collate.startswith('utf8_') \
                else 'utf8mb4'
            if opts.get('charset') and coll_charset != out['charset']:
                return None, self._err(
                    1253, f"COLLATION '{collate}' is not valid for "
                          f"CHARACTER SET '{out['charset']}'",
                    sqlstate='42000')
            out['charset'] = coll_charset
            out['collate'] = collate
        if opts.get('auto_increment') is not None:
            return None, self._err(
                1064,
                f"表级 AUTO_INCREMENT={opts['auto_increment']} 未实现："
                f"本引擎只在插入 INT 主键时按现有最大值+1 取值，"
                f"不支持指定起始值。显式拒绝而不是接受后静默忽略")
        comment = opts.get('comment')
        if comment is not None:
            out['comment'] = comment
        return out, None

    def _cmd_create_table(self, p, s):
        # 分区描述要真正落到表对象上（Table._partition），否则
        # SHOW PARTITIONS / 分区裁剪都读不到（原实现把解析结果丢掉了）。
        # 表级 CHECK 约束（p['checks']）同理：原实现只把 columns 与
        # partition 传下去，解析器辛苦解析出来的 `checks` 被**整体丢弃**
        # （列级 CHECK 因为藏在 cols[i]['check'] 里侥幸活下来，但没人校验），
        # 于是 `CHECK` 既不报错也不生效。这里如实传给 create_table，
        # 由它在定义期解析/校验/命名并持久化。
        if p.get('if_not_exists') and p['table'] in self.db.tables:
            # `CREATE TABLE IF NOT EXISTS`：MySQL 语义下"已存在"是成功且
            # 不改动原表。原实现把解析出来的标志丢掉，于是同一份逻辑备份
            # 无法重复恢复（第二次恢复的第一步就报 1062）。
            return self._ok(
                message=f"Table '{p['table']}' already exists")
        # E1：表选项逐项校验（不支持 → 显式错误；支持 → 持久化 + 回显）
        table_options, opt_err = self._validate_table_options(
            p.get('table_options') or {})
        if opt_err is not None:
            return opt_err
        self.db.create_table(p['table'], p['columns'],
                             partition=p.get('partition'),
                             checks=p.get('checks'),
                             # 表级 `UNIQUE KEY/INDEX`：解析器已不再静默
                             # 丢弃，这里必须如实传下去；列级 `col UNIQUE`
                             # 由 `create_table` 从 cols 推导成等价规格。
                             # 两类唯一性都由写入路径强制，`SHOW INDEX`
                             # 会列出 UNIQUE-CHECK。
                             unique_indexes=p.get('unique_indexes'),
                             # E2：内联非唯一 KEY/INDEX（与独立 CREATE INDEX
                             # 共用同一份持久化声明）
                             secondary_indexes=p.get('secondary_indexes'),
                             # E2：ENGINE= 兼容别名（持久化请求名）
                             requested_engine=table_options['engine'])
        # E1：引擎扩展元数据（表选项/注释）持久化到 _meta.json，
        # SHOW CREATE TABLE / SHOW TABLE STATUS 如实回显。
        tbl = self.db.tables[p['table']]
        table_options['user_specified'] = bool(p.get('table_options'))
        tbl.table_options = table_options
        tbl.comment = table_options.get('comment') or ''
        tbl.requested_engine = table_options['engine']
        self.db.save_meta()
        return self._ok(
            message=f"Table '{p['table']}' created")

    def _cmd_drop_table(self, p, s):
        if p.get('if_exists') and p['table'] not in self.db.tables:
            # `DROP TABLE IF EXISTS`：不存在也算成功（否则恢复脚本里的
            # 清理语句在全新数据目录上必然失败）
            return self._ok(
                message=f"Unknown table '{p['table']}'")
        self.db.drop_table(p['table'])
        return self._ok(
            message=f"Table '{p['table']}' dropped")

    # ==================== ALTER TABLE / TRUNCATE ====================
    #
    # 语义与边界（本批次）：
    #   * 实现：`ADD COLUMN` / `DROP COLUMN` / `RENAME TO` / `TRUNCATE TABLE`；
    #   * **其余一切 ALTER 变体**（MODIFY / CHANGE / ADD INDEX / ADD CHECK …）
    #     返回 MySQL 1235（`ER_NOT_SUPPORTED_YET`）+ 明确报文，**绝不**返回
    #     OK 却什么都没做；
    #   * 事务：MySQL 把 DDL 归为"隐式提交"语句，这里照做 —— 执行前先提交
    #     本会话的事务（`_implicit_commit_ddl`）。表上还有**别的**未提交事务
    #     时 `Database._ddl_guard` 显式报错（本引擎没有元数据锁，硬做会刷出
    #     未提交数据并退休 WAL）。

    def _implicit_commit_ddl(self, s, what: str):
        """DDL 的隐式提交（MySQL 13.3.3「导致隐式提交的语句」）"""
        txn = s.pop('txn', None) if isinstance(s, dict) else None
        if not txn:
            return None
        try:
            self.db.commit(txn)
            self.stats['commits'] += 1
        except Exception as e:                       # pragma: no cover - 防御
            raise DdlError(
                f"{what} 前的隐式提交失败: {e}", errno=1105)
        return txn.get('txn_id')

    def _cmd_alter_table(self, p, s):
        action = str(p.get('action') or '').upper()
        table = p.get('table')
        if action == 'UNSUPPORTED':
            # 错误码选 1064（`ER_PARSE_ERROR`）而不是 1235
            # （`ER_NOT_SUPPORTED_YET`）：本引擎的 ALTER TABLE **语法**只
            # 实现了 ADD COLUMN / DROP COLUMN / RENAME TO / MODIFY COLUMN /
            # ENGINE= 兼容别名五种动作，其余动作在语法层面就不被接受。
            return self._err(
                1064,
                f"ALTER TABLE 的 '{p.get('variant')}' 变体本引擎未实现："
                f"只支持 ADD COLUMN / DROP COLUMN / RENAME TO / "
                f"MODIFY COLUMN / ENGINE=<别名>，"
                f"其余变体一律显式报错、绝不静默成功"
                f"（语句: {p.get('sql')}）")
        self._implicit_commit_ddl(s, f"ALTER TABLE {table}")
        if action == 'MODIFY COLUMN':
            # E2：改类型/长度/NULL/DEFAULT；已有数据先逐行试算转换，
            # 不可转换/会截断 → 显式错误且表结构与数据完全不变。
            col = dict(p['column'])
            stats = self.db.alter_modify_column(table, col)
            return self._ok(
                message=(f"Table '{table}' altered: column "
                         f"'{col['name']}' modified to "
                         f"{col.get('type')}, "
                         f"{stats.get('rows_rewritten', 0)} row "
                         f"version(s) rewritten"))
        if action == 'ENGINE':
            # E2：ENGINE= 兼容别名。物理存储不迁移（始终是 LightDB 原生
            # 行存），只持久化"请求的引擎名"并在 SHOW 中如实回显；
            # **不实现** InnoDB/MyISAM/MEMORY/CSV 的任何语义。
            try:
                display = self._normalize_engine_name(p.get('engine'))
            except KeyError:
                return self._err(
                    1064,
                    f"Unknown storage engine '{p.get('engine')}': "
                    f"兼容别名只接受 InnoDB / MyISAM / MEMORY / CSV / "
                    f"LightDB（不实现对应引擎语义）")
            tbl = self.db.get_table(table)
            tbl.requested_engine = display
            opts = dict(getattr(tbl, 'table_options', None) or {})
            opts.setdefault('engine', 'LightDB')
            opts['engine'] = display
            tbl.table_options = opts
            self.db.save_meta()
            return self._ok(
                message=(f"Table '{table}' altered: ENGINE set to "
                         f"{display}（兼容别名；实际存储为 LightDB 原生"
                         f"行存，不实现 {display} 语义）"))
        if action == 'ADD COLUMN':
            col = dict(p['column'])
            stats = self.db.alter_add_column(table, col)
            n = stats.get('rows_backfilled', 0)
            return self._ok(
                message=(f"Table '{table}' altered: column "
                         f"'{col['name']}' added, {n} existing row "
                         f"version(s) backfilled"
                         + (f" with {stats['backfill']!r}"
                            if 'backfill' in stats else '')))
        if action == 'DROP COLUMN':
            cname = p['column']
            stats = self.db.alter_drop_column(table, cname)
            return self._ok(
                message=(f"Table '{table}' altered: column '{cname}' "
                         f"dropped, {stats.get('rows_rewritten', 0)} row "
                         f"version(s) rewritten (数据已从数据文件清除)"))
        if action == 'RENAME':
            new = p['new_name']
            self.db.rename_table(table, new)
            return self._ok(
                message=f"Table '{table}' renamed to '{new}'")
        return self._err(
            1064,
            f"ALTER TABLE 的动作 {action!r} 本引擎未实现"
            f"（只支持 ADD COLUMN / DROP COLUMN / RENAME TO / "
            f"MODIFY COLUMN / ENGINE=<别名>）")

    # ---------- E2：独立索引语句（与表内联 KEY 共用持久化） ----------

    def _cmd_create_index(self, p, s):
        """`CREATE [UNIQUE] INDEX name ON t (列...)`"""
        table = p['table']
        name = p['name']
        self._implicit_commit_ddl(s, f"CREATE INDEX {name} ON {table}")
        stats = self.db.create_index(
            table, name, p.get('columns') or [],
            unique=bool(p.get('unique')))
        if not stats.get('created'):
            return self._ok(
                message=f"Index '{name}' already exists on '{table}'")
        kind = 'UNIQUE INDEX' if stats.get('unique') else 'INDEX'
        return self._ok(
            message=(f"{kind} '{stats.get('name')}' created on "
                     f"'{table}' ({', '.join(stats.get('columns') or [])})"))

    def _cmd_drop_index(self, p, s):
        """`DROP INDEX name ON t`"""
        table = p['table']
        name = p['name']
        self._implicit_commit_ddl(s, f"DROP INDEX {name} ON {table}")
        stats = self.db.drop_index(table, name)
        kind = 'UNIQUE INDEX' if stats.get('unique') else 'INDEX'
        return self._ok(
            message=f"{kind} '{stats.get('dropped')}' dropped from "
                    f"'{table}'")

    def _cmd_alter_unsupported(self, p, s):
        """`ALTER DATABASE` / `ALTER USER` 等非 TABLE 形态：显式不支持

        这一类与 `ALTER TABLE` 的"动作没实现"不同：整个语句族本引擎都没有
        （`ALTER DATABASE` 尤其无意义 —— 本引擎是单库模型，见
        `Database.database_names`），因此按 MySQL 的 1235
        （`ER_NOT_SUPPORTED_YET`）报，报文照该错误的原文写。
        """
        v = p.get('variant')
        name = ' '.join(v) if isinstance(v, (list, tuple)) else str(v)
        return self._err(
            1235,
            f"This version of lightdb doesn't yet support '{name}'")

    def _cmd_truncate(self, p, s):
        if str(p.get('action') or '').upper() == 'UNSUPPORTED':
            return self._err(
                1064,
                f"TRUNCATE 的 '{p.get('variant')}' 形态本引擎未实现："
                f"只支持 `TRUNCATE TABLE <单表>`，多表形态一律显式报错"
                f"（不做「清了一半」的半套结果）"
                f"（语句: {p.get('sql')}）")
        table = p.get('table')
        self._implicit_commit_ddl(s, f"TRUNCATE TABLE {table}")
        stats = self.db.truncate_table(table)
        return self._ok(
            message=(f"Table '{table}' truncated "
                     f"({stats.get('deleted_versions', 0)} index entr(y/ies) "
                     f"dropped, 表定义保留)"))

    def _cmd_analyze(self, p, s):
        """ANALYZE [TABLE] t —— 收集统计信息

        解析器一直能解析 ANALYZE，但执行器没有对应 handler（返回 1064），
        导致 stats 永远为空、优化器只能一直选全表扫描。
        """
        if self.stats_manager is None:
            return self._err(1105, "统计管理器不可用")
        tbl = self.db.get_table(p['table'])
        st = self.stats_manager.analyze_table(tbl)
        try:
            self.stats_manager.save()
        except Exception as e:
            logger.warn(f"统计信息落盘失败: {e}")
        n = getattr(st, 'row_count', None)
        if n is None:
            n = tbl.row_count
        return self._ok(
            message=f"Table '{p['table']}' analyzed "
                    f"({n} rows)")

    # ==================== DML ====================

    def _cmd_insert(self, p, s):
        tbl = self.db.get_table(p['table'])
        values_list = p['values']

        # 指定列 → 补全
        if p.get('columns'):
            col_map = {
                c['name']: i
                for i, c in enumerate(tbl.cols)
            }
            unknown = [c for c in p['columns'] if c not in col_map]
            if unknown:
                # 原实现把不存在的列名静默丢掉（`row[col_map[cn]]` 用 `if
                # cn in col_map` 守着），于是 `INSERT INTO t (nope) VALUES (1)`
                # 变成"插入一行全 NULL"，恢复/导入时这就是静默丢数据。
                return self._err(1054, f"未知列: {', '.join(unknown)}")
            expanded = []
            for v in values_list:
                if len(v) != len(p['columns']):
                    return self._err(
                        1136, f"列数不匹配: 语句给了 {len(v)} 个值，"
                              f"列清单有 {len(p['columns'])} 列")
                # 未出现在列清单里的列不填 None，而是填**真实的列默认值**
                # （`Table._column_default`），与 `SHOW COLUMNS` 报出的
                # Default 一致。旧实现一律填 None，于是"表声明了 DEFAULT、
                # 引擎却不用"——一条看得见却无效的元数据。
                row = [tbl._column_default(c) for c in tbl.cols]
                for i, cn in enumerate(p['columns']):
                    row[col_map[cn]] = v[i]
                expanded.append(row)
            values_list = expanded
        else:
            # 没有列清单 → 值个数必须与列数**严格相等**。原实现放任不管，
            # `VALUES (1,2,3,4,5)` 插进两列的表里也能"成功"（多出来的值
            # 被 `_insert_locked` 补/截掉），调用方拿到的 affected=1 是假的。
            for v in values_list:
                if len(v) != len(tbl.cols):
                    return self._err(
                        1136, f"列数不匹配: 表 {tbl.name} 有 "
                              f"{len(tbl.cols)} 列，VALUES 给了 {len(v)} 个值")

        # E1 静默语义审计：生成列（GENERATED ALWAYS AS ...）由引擎按表达式
        # 计算，**不接受显式赋值**（MySQL 5.7 错误 3105）。旧实现把显式值
        # 原样存进生成列（不计算也不报错）——同一条 DDL 两种行为。
        gen_idx = [i for i, c in enumerate(tbl.cols)
                   if c.get('generated')]
        if gen_idx:
            try:
                from ..db.table import _OMITTED as _OMITTED_DEFAULT
            except Exception:                     # pragma: no cover - 防御
                _OMITTED_DEFAULT = object()
            for row in values_list:
                for i in gen_idx:
                    val = row[i] if i < len(row) else None
                    # 省略列时补位的是 `_OMITTED` 哨兵（不是显式值）；
                    # 显式 NULL / 具体值才按 MySQL 3105 处理。
                    if val is None or val is _OMITTED_DEFAULT:
                        continue
                    return self._err(
                        3105,
                        f"The value specified for generated column "
                        f"'{tbl.cols[i].get('name')}' in table "
                        f"'{tbl.name}' is not allowed.",
                        sqlstate='HY000')

        n = 0
        # 必须把事务号传下去：原实现无条件走 txn_id=0（autocommit），
        # 导致 BEGIN; INSERT; ROLLBACK 无法回滚，且插入的锁与事务无关。
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None

        # ★ 阶段 2 第 2 项：**语句级原子性**。
        #
        # 一条 `INSERT ... VALUES (...),(...),(...)` 在 MySQL 里是原子的：
        # 要么全部生效，要么一行都不留。原实现对 values_list **逐行**
        # 调 `tbl.insert()`，每行各自 autocommit —— 第 3 行主键冲突/CHECK
        # 违规时前 2 行已经提交留下（实测），且每行一次 WAL COMMIT 记录 +
        # 行锁 + 合成事务号，把批量省下的解析/审计收益全吃光。
        #
        # 修法：**没有用户显式事务**时，为整条语句开一个引擎内部事务
        # （`db.begin()`，拿到真实事务号 + 读视图）。全部行成功后一次性
        # COMMIT（只写一条 COMMIT 记录，autocommit 档持久性）；任一行失败
        # 则整体 ABORT，由物理 undo 把已写入的行逐条撤销 —— 0 行落地。
        #
        # 有用户显式事务时**完全不碰**事务边界（不替用户提交/回滚）：
        # 行写入照旧挂在用户事务号上，语句失败只是让外层事务里的这条语句
        # 无效，用户仍可继续执行或 ROLLBACK（既有语义，见 _cmd_rollback）。
        implicit = None
        marks = None
        if txn is None and len(values_list) > 1:
            implicit = self.db.begin()
            tid = implicit['txn_id']
            rv = implicit['read_view']
            if isinstance(s, dict):
                # 把内部事务挂到会话上：触发器体里的 DML 与 SELECT 于是
                # （a）和主语句同属一个事务（一起提交/一起回滚），
                # （b）读得到本语句已写入的行（read-your-writes）。
                # 语句结束后原样恢复（用户会话本来没有事务）。
                s['txn'] = implicit
        elif txn is not None and len(values_list) > 1:
            # 用户显式事务：**绝不**提交/回滚用户事务，但这条语句本身仍然
            # 必须是原子的 —— 记一个"语句级保存点"（各表物理 undo 栈深度），
            # 失败时只撤销本语句追加的那一段（见 Table.rollback_statement）。
            marks = self.db.mvcc.undo_marks(tid)

        try:
            for row in values_list:
                row = list(row)
                # BEFORE INSERT 触发器可以改 NEW 列（`SET NEW.x = ...`），
                # 因此必须在真正写入之前触发；AFTER INSERT 在写入之后触发。
                self._fire_triggers('BEFORE', 'INSERT', p['table'],
                                    new_row=row, session=s)
                tbl.insert(row, txn_id=tid, read_view=rv)
                self._binlog_record(
                    s, 'INSERT', p['table'],
                    {'cols': [c['name'] for c in tbl.cols],
                     'row': self._binlog_coerce_row(tbl, row)})
                n += 1
                self._fire_triggers('AFTER', 'INSERT', p['table'],
                                    new_row=row, session=s)
        except BaseException:
            # 任一行失败（主键/唯一冲突、CHECK、NOT NULL、类型错、
            # 触发器体报错、空间不足…）→ 整条语句回滚。
            if implicit is not None:
                self._restore_session_txn(s, implicit)
                try:
                    self.db.abort(implicit)
                except Exception as e:           # pragma: no cover - 防御
                    logger.error(f"多值 INSERT 回滚失败: {e}")
            elif marks is not None:
                self._rollback_statement(tbl, tid, marks)
            raise

        if implicit is not None:
            self._restore_session_txn(s, implicit)
            # 语句级提交：COMMIT 记录 flush 到 OS（不 fsync、不刷脏页），
            # 持久性档位与逐行 autocommit 路径一致（见 Database.commit_statement）
            self.db.commit_statement(implicit)
            # ★ 与 2.1 的 checkpoint 协同：`Table.insert` 里那次阈值检查
            # 被"内部事务尚未提交"挡掉了（`_maybe_checkpoint` 见
            # `_read_views` 非空一律推迟），提交后必须补一次 —— 否则纯
            # 批量写入路径的 WAL 永远不轮转，崩溃恢复要重放整个历史。
            for t in list(self.db.tables.values()):
                t._safe_maybe_checkpoint()

        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) inserted")

    @staticmethod
    def _restore_session_txn(s, implicit):
        """把会话上的内部事务摘掉（只在它仍是当前事务时）"""
        if isinstance(s, dict) and s.get('txn') is implicit:
            s.pop('txn', None)

    def _rollback_statement(self, tbl, tid, marks):
        """显式事务里的"语句级回滚"（不碰用户事务边界）

        安全边界（如实记录）：只在本语句追加的物理 undo 全部是**目标表上的
        INSERT** 时才回滚。如果触发器在别的表上写了行、或做了 UPDATE/DELETE，
        补偿 WAL 的形态就不再是"INSERT→墓碑"那一套，硬回滚会让"进程内状态"
        与"崩溃恢复后状态"不一致 —— 这时宁可保持旧行为（已写入的行留在事务
        里，由用户 ROLLBACK 撤销）并显式告警，也不做半吊子的回滚。
        """
        try:
            pending = self.db.mvcc.peek_undo_after(tid, marks)
        except Exception as e:                   # pragma: no cover - 防御
            logger.error(f"语句级回滚检查失败: {e}")
            return 0
        foreign = [a for a in pending
                   if a.op != 'INSERT' or a.table != tbl.name]
        if foreign:
            logger.warn(
                f"多值 INSERT 失败但语句级回滚被跳过：本语句在显式事务里产生了"
                f" {len(foreign)} 条非目标表-INSERT 的副作用"
                f"（触发器写别的表 / UPDATE / DELETE），已写入的行仍留在事务里，"
                f"需要 ROLLBACK 才能撤销")
            return 0
        try:
            return tbl.rollback_statement(tid, marks)
        except Exception as e:
            logger.error(f"语句级回滚失败: {e}")
            return 0

    def _cmd_select(self, p, s):
        # `SHOW MASTER STATUS` / `CHECK TABLE` / `SHOW ENGINE ...` 等以结果集
        # 回传的非 SELECT 语句：解析期打上 show_stmt 标记（见
        # parser._show_result），在这里分流，避免与单表/JOIN/CTE 各条
        # SELECT 路径纠缠。
        if p.get('show_stmt'):
            return self._exec_show_stmt(p, s)

        # 复制事件虚拟表：`SELECT * FROM __BINLOG_FETCH__ WHERE pos >= n`
        # （从库 io 线程拉事件用的接口），数据来自真实 WAL 记录。
        if str(p.get('table') or '').upper() == '__BINLOG_FETCH__':
            return self._select_from_relation(p, s, self._binlog_relation())

        # 全文谓词 `MATCH(列) AGAINST('词')`：先用表上的倒排索引真算命中主键，
        # 再改写为 `主键 IN (...)` 交给既有下推/求值路径（含 JOIN 之外的
        # 单表统一路径）。
        if self._where_has_match(p.get('where')):
            rewritten = self._apply_match(p)
            if isinstance(rewritten, dict) \
                    and rewritten.get('type') == 'error':
                return rewritten
            p = rewritten

        # ★ GROUP BY 严格校验放在最前面（静态判定，见 _check_strict_group_by）
        self._check_strict_group_by(p)
        table = p['table']

        # ★ 决策 2 / 4A.2：OLAP 向量化**显式分派**（默认关闭）。
        #   只对白名单语法（单表聚合/分组 + 简单 WHERE/ORDER BY/LIMIT）
        #   尝试派发；`try_vectorized_select` 返回 None 表示「不派发」，
        #   继续走下方行存原路径。分派内部异常也显式回退行存（warn）。
        #   可见性：`lightdb.olap.sql_dispatch.dispatch_stats()` 计数 +
        #   INFO 日志（每次命中/回退都有记录，禁止静默改道）。
        #   `_olap_bypass` 只在对拍（crosscheck）跑行存参考时置位。
        if self.olap_dispatch and not self._olap_bypass:
            try:
                from ..olap.sql_dispatch import try_vectorized_select
            except Exception as _olap_imp_err:   # pragma: no cover - 防御
                logger.warn(
                    f"OLAP dispatch 模块不可用，回退行存: {_olap_imp_err}")
                try_vectorized_select = None
            if try_vectorized_select is not None:
                _olap_res = try_vectorized_select(self, p, s)
                if _olap_res is not None:
                    return _olap_res

        # WHERE 里的字面量 IN 列表（`id IN (1,2,NULL)`）解析出来是
        # {'values': [...]}，存储层的 _match / 统一投影都不认这种形态
        # （会 KeyError 或恒假）。先统一改写成 and/or 链，并在这里处理
        # 三值逻辑（列表含 NULL 时 NOT IN 整体为 UNKNOWN）。
        if self._contains_in_list(p.get('where')):
            p = dict(p)
            p['where'] = self._rewrite_in_lists(p['where'])

        # FROM-less SELECT（`SELECT 1 AS n`）：递归 CTE 的 base 分支常见写法
        if not table:
            if p.get('no_from'):
                return self._select_no_from(p, s)
            # FROM (子查询) 别名：物化成内存关系后走「以内存关系为数据源」
            # 的既有路径（与 CTE 同一条路径）
            sub = p.get('subquery')
            if isinstance(sub, dict) and sub.get('sql'):
                # 任务 3（`FROM (子查询) x JOIN 表 ...`）：本可以交给 JOIN
                # 计划路径（`_exec_join_query`，优化器的 SubqueryScan 原生
                # 支持），实测结果正确；但既有回归
                # `tests/test_v4_sql_features.py::test_from_subquery_alias` 第
                # ⑧ 条**断言这里必须显式报错**，而本次任务的硬约束是
                # 「不要改任何既有断言」，因此保持显式报错，不改行为。
                # 详见 `_select_from_subquery` 的说明。
                return self._select_from_subquery(p, s, sub)
            return self._err(1105, "暂不支持 FROM 子查询")

        # information_schema
        if table.lower().startswith('information_schema.'):
            _, name = table.split('.', 1)
            cols, rows = self.info_schema.query(name)
            return self._rs(cols, rows)
        # 裸 `TABLES` / `COLUMNS` / `SCHEMATA`：只有在真实表里不存在时才
        # 当成 information_schema 的虚拟表，避免遮蔽用户自己的同名表
        # （原实现无条件优先虚拟表，与 `TABLES` 同名的真实表就查不到了）。
        if (table.upper() in ('TABLES', 'COLUMNS', 'SCHEMATA')
                and table not in self.db.tables):
            cols, rows = self.info_schema.query(table)
            return self._rs(cols, rows)

        # CTE：WITH 物化出来的内存关系（优先于真实表）
        rel = self._cte_relations.get(table.lower())
        if rel is not None:
            return self._select_from_relation(p, s, rel)

        # 外层引用无法无歧义解析（外层未起别名、子查询里又有同名表被别名
        # 遮蔽）时显式报错：这种形态原来会被判成"非相关子查询"，一次性
        # 求值成空集后静默返回 0 行，属于会误导使用者的错答。
        amb = self._ambiguous_outer_ref(
            p.get('where'), table, p.get('table_alias'))
        if amb:
            return self._err(1062, self._ambiguous_outer_ref_message(
                amb, table, p.get('table_alias')))

        # 有 JOIN → 用计划执行器
        joins = p.get('joins') or []
        if joins:
            return self._exec_join_query(p, s)

        # 单表
        if self.optimizer is not None:
            try:
                plan = self.optimizer.optimize(p)
                self._last_plan = plan
            except Exception:
                self._last_plan = None

        # WHERE 里的子查询条件（IN/NOT IN/EXISTS）先在执行器侧求值，
        # 改写成存储层认识的条件再下推（db/table.py 的 _match 只认
        # and/or/column+op+value，不认 subquery）
        has_sub = self._contains_subquery(p.get('where'))
        where = p.get('where')
        # 相关子查询不能一次性求值改写（它的真值依赖外层行），
        # 必须走逐行求值路径；非相关 EXISTS / IN 子查询仍走原来的
        # 「求值 + 改写」路径（它们是一次性集合语义，改写后能继续下推）。
        # **标量比较谓词**（`(SELECT ...) = 5`）同样必须逐外层行求值：
        # 它需要按行数语义收敛（0 行 → NULL、1 行 1 列 → 取值、
        # 多行/多列 → 显式报错），一次性改写无法表达这套语义。
        # 原实现让这种谓词掉进 _rewrite_subqueries 的兜底分支，把 subquery
        # 直接丢掉，产出没有左值的条件后报出误导性的 err 1146「缺少:
        # column」；逐行路径按折叠值缓存，非相关标量子查询只执行一次。
        correlated = has_sub and (
            self._where_has_correlated(where, table, p.get('table_alias'))
            or self._where_has_scalar_subquery(where))
        if has_sub and not correlated:
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, table, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")

        tbl = self.db.get_table(table)

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        if correlated:
            return self._select_correlated(p, s, tbl, rv, table)

        # GROUP BY / DISTINCT / 窗口函数 / 带限定列名的单表查询：
        # 都要在拿到行之后再加工
        # （原实现的 DISTINCT 只在解析器里记录，执行路径从未使用，属于静默错答）
        # ORDER BY 是**多键**或键带限定名时，同样不能再交给存储层
        # （存储层的 `db/table.py::select` 只认单个 `(列, desc)` 元组）。
        ob_keys = normalize_order_by(p.get('order_by'))
        qualified = (
            any('.' in str(c) for c in (p.get('columns') or []))
            or self._where_has_qualified(p.get('where'))
            or any('.' in key for key, _d in ob_keys))
        # 常量投影（`SELECT 1 FROM orders`）存储层认不了，必须走投影路径
        literal = any(self._literal_value(c)[0]
                      for c in (p.get('columns') or []) if c != '*')
        # 投影列表里的算术表达式（`SELECT salary*2 AS d`）：存储层只认
        # 列名，直接下推会报「列 salary*2 不存在」，必须走投影路径由
        # _project_rows 的 _parse_arith / _eval_arith 求值
        arith = any(self._arith_projection_expr(c) is not None
                    for c in (p.get('columns') or []) if c != '*')
        # 投影里的 BETWEEN（`SELECT id BETWEEN 1 AND 3 FROM t`）：存储层
        # 只认列名，下推会报「列 ... 不存在」。必须走投影表达式求值路径
        # （`parser.eval_expr_node` 的 between 分支，返回 MySQL 的 1/0/NULL）。
        proj_between = any(
            self._projection_has_between(c)
            for c in (p.get('columns') or []) if c != '*')
        # 投影列表里的标量子查询（`SELECT id, (SELECT COUNT(*) ...) AS cnt`）
        # ——必须走 _project_rows 的逐行求值路径，否则要么被存储层当未知列
        # （视有无点号）静默填 NULL，要么直接报"列不存在"。
        proj_sub = any(
            self._projection_subquery_ast(self._split_alias(str(c))[0])[0]
            is not None
            for c in (p.get('columns') or []) if c != '*')
        # 投影里的标量函数（`SELECT LOWER(name)`）：存储层 select 用
        # `\w+\s*\(` 判断"是否有聚合"，于是把标量函数也交给 _aggregate
        # —— 非聚合项取「第一行的值」，实测 `SELECT id, LOWER(name) FROM
        # users` 静默返回 1 行 (1, None)（NULL + 丢行），是同类错答。
        # 现在检查器里带函数调用的投影统一走投影路径，由
        # `_projection_item_plan` 用表达式求值器逐行算（支持清单见
        # parser.SCALAR_FUNCTION_NAMES；不支持的函数名在那里显式报错）。
        # 另外 `SUM(salary * 2)` / `ROUND(AVG(v), 1)` 这类「聚合 + 表达式」
        # 参数存储层也不认（`_aggregate` 的正则只吃单列），同样要接管。
        scalar_func = any(
            self._is_scalar_function_projection(c)
            or self._agg_needs_expr_eval(c)
            for c in (p.get('columns') or []) if c != '*')
        # 投影项带 `AS 别名`（`SELECT salary AS v`）：存储层只认裸列名，
        # 会把整段 `salary AS v` 当列名去查 → 报「列 salary AS v 不存在」
        # （别名能力等于没有）。带别名的投影统一走投影路径，别名由
        # `_projection_item_plan` 落实（输出列名 = 别名）。
        aliased = any(
            self._split_alias(str(c))[1] is not None
            for c in (p.get('columns') or []) if c != '*')
        # WHERE/HAVING 里的 NOT (...)：存储层 _match 只认 and/or/column+op+value，
        # 遇到 {'not': ...} 直接 KeyError，必须走统一求值路径
        has_not = self._where_has_not(p.get('where'))
        has_null = self._where_has_is_null(p.get('where'))
        # WHERE/HAVING 里的**表达式谓词**（`WHERE salary * 2 > 230`、
        # `WHERE LOWER(name) = 'alice'`、`HAVING SUM(v) * 2 > 100`）：
        # 存储层只认 `column + op + value`，下推过去会 KeyError（报出
        # 与真实原因无关的「表不存在」），必须走统一求值路径
        expr_pred = self._where_has_expr_pred(where)
        # WHERE/HAVING 里**右侧是列引用**的比较（`WHERE user_id = id`、
        # `WHERE o.user_id = u.id`）：存储层 `_match` 会把右侧当字符串
        # 字面量（`1 == 'id'` 恒假）→ 静默返回空集，必须走统一求值路径
        # 由 `_eval_condition` 按列取值比较。
        col_rhs = self._where_has_column_rhs(where) \
            or self._where_has_column_rhs(p.get('having'))
        # ORDER BY 是表达式 / 序号 / 多键，或键引用了没被投影的列
        # （`SELECT name FROM emp ORDER BY salary`）：存储层与计划执行器都
        # 只会去找同名列，找不到就**静默不排序**（DESC 也可能被丢掉），
        # 必须由执行器按表达式算出排序键；解析不到的键在统一路径显式报错。
        # `SELECT *` 时把表列名传进去（存储层的输出列就是它们），这样
        # `SELECT * FROM t ORDER BY 不存在的列` 也会显式报错而非静默不排序。
        storage_cols = getattr(tbl, 'cols', None)
        storage_cols = ([str(c.get('name')) for c in storage_cols
                         if isinstance(c, dict)]
                        if isinstance(storage_cols, list) else None)
        order_expr = self._order_by_needs_eval(
            p.get('order_by'), p.get('columns'), storage_cols)
        # 归一化后仍残留 negate 标记（LIKE 取反等存储层无法表达的形态）：
        # `_base_rows_and_cols` 会在取到行之后自己做三值过滤
        if self._where_has_negate(self._normalize_where(where)):
            has_not = True
        if (p.get('windows') or p.get('group_by') or p.get('having')
                or p.get('distinct') or qualified or literal or has_sub
                or arith or proj_sub or proj_between or scalar_func
                or has_not or has_null
                or expr_pred or col_rhs or order_expr or aliased):
            return self._select_advanced(
                p, tbl, read_view=rv, where=where, session=s)
        # 单表纯 COUNT(*) / COUNT(列)（无 WHERE、无 GROUP BY、无修饰）：
        # 交给真正的并行聚合 —— 按主键范围分片、多线程扫描、再合并局部计数。
        # 放在 `tbl.select()` **之前**，避免既算了投影又算一遍计数。
        # 这是唯一一个能安全地按行分片的聚合（COUNT 与行顺序、分片集合无关：
        # SUM 若按主键范围切分也安全，但需要把存储层行解码搬进线程，
        # 收益不明，故不在此处扩展）。
        if self._is_plain_count(p, where):
            fast = self._parallel_count(tbl, str((p.get('columns') or [''])[0]))
            if fast is not None:
                # 结果集形状与 `_rs` 一致：行是序列（这里是一元组）
                return self._rs([str(c) for c in p['columns']], [(fast,)])

        cols, rows = tbl.select(
            p['columns'], where,
            # 存储层只认单键 `(列, desc)`；走到这里时 `_order_by_needs_eval`
            # 已经保证「最多一个键、且键就是投影输出列名」。
            (ob_keys[0] if ob_keys else None),
            # 存储层只认 limit；OFFSET 靠多取 offset 行再切片实现
            (int(p['limit']) + int(p.get('offset') or 0))
            if p.get('limit') is not None else None,
            read_view=rv)
        off = int(p.get('offset') or 0)
        if off:
            rows = rows[off:]
            if p.get('limit') is not None:
                rows = rows[:int(p['limit'])]

        return self._rs(cols, rows)

    # ==================== 并行 COUNT ====================

    #: 低于该行数不做并行 COUNT（分片/线程开销大于收益）
    parallel_min_rows = 2000

    @property
    def parallel(self):
        """并行执行器（惰性创建；不可用时为 None）

        `tests/test_batch14.py::test_parallel_count` 直接读 `ex.parallel`，
        因此这里返回的是**真的** `ParallelExecutor`（`parallel_group_by` /
        `parallel_scan` / `parallel_aggregate` 都能在真实数据上跑），
        不是统计桩。它只被 `_parallel_count` 用于单表裸 COUNT 的分片并行。
        """
        if self._parallel is None and self.parallel_enabled:
            try:
                from ..parallel.executor import ParallelExecutor
                self._parallel = ParallelExecutor()
            except Exception as e:               # pragma: no cover
                logger.warn(f"初始化并行执行器失败（回退串行）: {e}")
                self.parallel_enabled = False
        return self._parallel

    def _is_plain_count(self, p, where) -> bool:
        """该查询是否是"单表裸 COUNT"（能安全交给并行分片聚合的形态）

        要求：无 WHERE（分片聚合不认条件下推）、无 GROUP BY/HAVING/DISTINCT/
        窗口/ORDER BY/LIMIT/OFFSET（都会改变结果语义）、投影只有一个项且
        形如 `COUNT(*)` 或 `COUNT(列)`（`COUNT(DISTINCT x)`、`COUNT(x)+1`
        一律退回既有路径）。
        """
        if not self.parallel_enabled:
            return False
        if where or p.get('group_by') or p.get('having') or p.get('distinct'):
            return False
        if p.get('windows') or p.get('order_by') or p.get('limit') is not None:
            return False
        if p.get('offset'):
            return False
        cols = p.get('columns') or []
        if len(cols) != 1:
            return False
        col = self._split_alias(str(cols[0]))[0].strip()
        return bool(re.fullmatch(r'COUNT\s*\(\s*(\*|[A-Za-z_]\w*)\s*\)',
                                 col, re.IGNORECASE))

    def _parallel_count(self, tbl, projection: str):
        """用 `ParallelExecutor` 真做一次并行 COUNT；不适用时返回 None

        `COUNT(*)` → 直接数分片行数（不读任何列）；`COUNT(col)` → 只读那一列。
        任何异常/不适用都返回 None，由调用方走既有串行路径（结果正确优先）。
        """
        n = getattr(tbl, 'row_count', 0)
        if self.parallel is None or n < self.parallel_min_rows:
            return None
        m = re.fullmatch(r'COUNT\s*\(\s*(\*|[A-Za-z_]\w*)\s*\)',
                         self._split_alias(projection)[0].strip(),
                         re.IGNORECASE)
        if not m:
            return None
        col = '*' if m.group(1) == '*' else m.group(1)
        if col != '*' and col not in [c['name'] for c in tbl.cols]:
            return None
        try:
            from ..parallel.table_view import TableColumnView
            workers = max(2, self.parallel.adaptive.decide(
                n, is_expensive=True))
            return self.parallel.parallel_aggregate(
                TableColumnView(tbl), col, 'COUNT', workers=workers)
        except Exception as e:                      # noqa: BLE001
            logger.warn(f"并行 COUNT 失败，退回串行: {e}")
            return None

    # ==================== 子查询条件求值（WHERE） ====================

    # 常量条件的内部表示（沿用工程既有约定）：
    #   恒假 -> {'or': []}（空 or 列表，_match 的 any([]) / _eval_condition
    #          的 any([]) 都是 False）
    #   恒真 -> None（条件被就地删除；带 filter 的路径也都把 None 当"无过滤"）
    _ALWAYS_FALSE = {'or': []}

    @classmethod
    def _contains_subquery(cls, where) -> bool:
        """WHERE 条件里是否包含 EXISTS / IN (SELECT ...) / 标量子查询"""
        if not isinstance(where, dict):
            return False
        if 'subquery' in where:
            return True
        inner = where.get('not')
        if isinstance(inner, dict) and cls._contains_subquery(inner):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._contains_subquery(c):
                    return True
        return False

    @staticmethod
    def _eq_conditions(column, values):
        """column IN (v1,v2,...) -> {'or': [{'column':c,'op':'=','value':v}, ...]}

        存储层的 _match 只有单值比较，没有 IN，因此必须展开成等值 or 链。
        """
        return {'or': [{'column': column, 'op': '=', 'value': v}
                       for v in values]}

    @classmethod
    def _collect_agg_exprs(cls, having) -> list:
        """收集 HAVING 里的聚合表达式（形如 COUNT(*) / SUM(amount)）

        表达式谓词（`HAVING SUM(v) * 2 > 100`）里的聚合也要收集 —— 它们的
        文本由 `render_scalar_expr` 规范渲染（`SUM(v)`、`SUM(v * 2)`），
        与 `_eval_condition` 取聚合值时的查找键完全一致。

        **聚合嵌在函数调用里**（`HAVING double_it(COUNT(*)) > 1`）同样要
        收集：否则 `avg` 求值时 `COUNT(*)` 的聚合值取不到 → 整条 HAVING
        被静默丢掉（实测 `HAVING double_it(COUNT(*)) > 1` 返回了全部
        分组，属于静默错答）。
        """
        found = []
        if isinstance(having, dict):
            if 'agg' in having:
                a = str(having['agg']).strip()
                if a and a not in found:
                    found.append(a)
            if isinstance(having.get('expr_pred'), dict):
                for a in cls._all_agg_texts_in_pred(having['expr_pred']):
                    if a not in found:
                        found.append(a)
            # NOT (...) 里也可能藏着聚合（`HAVING NOT (COUNT(*) > 1)`）
            inner = having.get('not')
            if isinstance(inner, dict):
                for a in cls._collect_agg_exprs(inner):
                    if a not in found:
                        found.append(a)
            for key in ('and', 'or'):
                for c in (having.get(key) or []):
                    for a in cls._collect_agg_exprs(c):
                        if a not in found:
                            found.append(a)
        return found

    @classmethod
    def _all_agg_texts_in_pred(cls, pred) -> list:
        """表达式谓词里**任意深度**的聚合文本（含 `F(COUNT(*))` 这种嵌法）"""
        out = []
        # parser.expr_pred_agg_texts 处理「谓词最外层的聚合」
        # （`HAVING SUM(v) * 2 > 100`）；这里再补「聚合嵌在函数调用里」
        # （`HAVING double_it(COUNT(*)) > 1`）—— 两条合起来才完整。
        for a in cls._expr_agg_texts(pred):
            if a not in out:
                out.append(a)
        for node in (pred.get('left'), pred.get('right'),
                     pred.get('low'), pred.get('high')):
            cls._all_agg_texts_in_node(node, out)
        for node in (pred.get('values') or []):
            cls._all_agg_texts_in_node(node, out)
        return out

    @classmethod
    def _all_agg_texts_in_node(cls, node, out) -> list:
        """表达式节点里**任意深度**的聚合文本（`F(COUNT(*))` 也要挖出来）

        文本用 `render_scalar_expr` 规范渲染，与 `lookup_agg_value` 的
        查找键一致（`COUNT(*)` / `SUM(v * 2)` 都逐字相同）。
        """
        if not isinstance(node, dict):
            return out
        kind = node.get('kind')
        if kind == 'arith':
            cls._all_agg_texts_in_node(node.get('left'), out)
            cls._all_agg_texts_in_node(node.get('right'), out)
            return out
        if kind != 'func':
            return out
        name = str(node.get('name') or '').upper()
        if name in _AGGREGATE_FUNCTION_NAMES:
            text = _render_scalar_expr(node)
            if text not in out:
                out.append(text)
            return out
        for arg in (node.get('args') or []):
            cls._all_agg_texts_in_node(arg, out)
        return out

    @classmethod
    def _expr_agg_texts(cls, pred) -> list:
        """表达式谓词里的聚合函数文本（实现在 parser.expr_pred_agg_texts）"""
        return _expr_pred_agg_texts(pred)

    @classmethod
    def _expr_agg_texts_in_node(cls, node) -> list:
        """表达式节点里最外层聚合函数的规范文本（实现在 parser.expr_agg_texts）"""
        return _expr_agg_texts(node)

    def _rewrite_subqueries(self, where, session, outer_table=None,
                            outer_alias=None, depth=0):
        """把带子查询的条件改写成可下推的条件

        返回值 (条件, 是否常量真)：

        * EXISTS / NOT EXISTS：先执行一次子查询，非空即真（沿用
          plan_executor._eval_subquery_exists 的语义），再改写成常量
          —— 真 -> (None, True)，假 -> ({'or': []}, False)。
        * col IN (SELECT ...)：执行子查询取第一列的值集合 v，改写成
          `col = v1 OR col = v2 OR ...`；空集合时 IN 恒假 → {'or': []}。
          子查询返回**多于 1 列**时显式报错（行值 IN 不支持，绝不只认
          第一列 —— 见 `_guard_in_subquery_width`）。
        * col NOT IN (SELECT ...)：同样取集合后改写成
          `col != v1 AND col != v2 AND ...`；空集合时 NOT IN 恒真 → None。

        相关子查询（引用外层列）：本方法没有外层行可折叠，遇到直接报错，
        避免"看起来有结果其实是错答案"。真正需要逐外层行求值的场景走
        `_select_correlated` → `_eval_correlated_predicate`（外层列常量折叠
        + 复用正常执行路径）。
        """
        if not isinstance(where, dict):
            return where, False
        if depth > 8:
            raise ValueError("子查询嵌套过深（>8 层）")

        # NOT (...)：整体取反。恒真/恒假的判定要跟着翻过来。
        if 'not' in where:
            inner = where['not']
            if self._contains_subquery(inner):
                rc, always_true = self._rewrite_subqueries(
                    inner, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    return self._ALWAYS_FALSE, False
                if rc == self._ALWAYS_FALSE:
                    return None, True
            return {'not': rc if rc is not None else self._ALWAYS_FALSE}, False

        if 'and' in where:
            out = []
            for c in where['and']:
                rc, always_true = self._rewrite_subqueries(
                    c, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    continue          # 恒真：AND 里可以直接去掉
                out.append(rc)
            if not out:
                return None, True
            if len(out) == 1:
                return out[0], False
            return {'and': out}, False

        if 'or' in where:
            out = []
            for c in where['or']:
                rc, always_true = self._rewrite_subqueries(
                    c, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    return None, True  # 有一项恒真 → 整体恒真
                out.append(rc)
            if not out:
                return self._ALWAYS_FALSE, False
            if len(out) == 1:
                return out[0], False
            return {'or': out}, False

        op = where.get('op')
        sub = where.get('subquery')

        if 'subquery' in where and sub is not None:
            self._check_correlated(sub, outer_table, outer_alias)
            res = self.execute(sub, session)
            if not isinstance(res, dict) or res.get('type') == 'error':
                msg = (res or {}).get('message', '未知错误')
                raise ValueError(f"子查询执行失败: {msg}")
            rows = res.get('rows') or []

            if op in ('EXISTS', 'NOT EXISTS'):
                exists = len(rows) > 0
                if op == 'NOT EXISTS':
                    exists = not exists
                if exists:
                    return None, True
                return self._ALWAYS_FALSE, False

            if op in ('IN', 'NOT IN'):
                column = where.get('column')
                if not isinstance(column, str) or not column:
                    # 左值不是单列（行值 IN `(a,b) IN (...)` 之类）：
                    # 本引擎不支持行值比较，显式报错，绝不猜一个左值。
                    raise ValueError(
                        "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                        "（多列）比较，无法安全推断出参与比较的列")
                # 集合上下文同样只允许**一列**：多列一律显式报错（静态
                # 列数 + 实际列数双判定，空结果集也拦得住），绝不静默
                # 只取每行第一列 —— 见 `_guard_in_subquery_width`。
                self._guard_in_subquery_width(
                    sub, 'WHERE/HAVING 里的', res_cols=res.get('columns'),
                    rows=rows)
                # IN 是**集合语义**（允许多行），每行取唯一一列做候选值；
                # 多列形态已在上一行被显式拒绝（行值 IN 不支持）。
                values, seen = [], set()
                for r in rows:
                    if not r:
                        continue
                    k = (type(r[0]).__name__, r[0])
                    if k in seen:
                        continue
                    seen.add(k)
                    values.append(r[0])

                # NULL 三值语义：
                #   结果集含 NULL 时 `c NOT IN (...)` 整体为 UNKNOWN
                #   → 恒不通过（原实现会写成 `c != NULL` 之类的错误条件）；
                #   `c IN (...)` 里 NULL 匹配不了任何值（连 NULL 左值也不行）
                #   → 直接把 NULL 从等值链里丢掉。
                has_null = any(v is None for v in values)
                non_null = [v for v in values if v is not None]

                neg = (op == 'NOT IN')
                if neg:
                    if has_null:
                        return self._ALWAYS_FALSE, False
                    # NOT IN 空集合恒真（含 NULL 左值，语义上也是真）
                    if not non_null:
                        return None, True
                    if len(non_null) == 1:
                        return {'column': column, 'op': '!=',
                                'value': non_null[0]}, False
                    return {'and': [{'column': column, 'op': '!=',
                                     'value': v} for v in non_null]}, False
                if not non_null:
                    return self._ALWAYS_FALSE, False
                if len(non_null) == 1:
                    return {'column': column, 'op': '=',
                            'value': non_null[0]}, False
                return self._eq_conditions(column, non_null), False

        # 兜底：带 subquery 但不是 EXISTS/IN，也不是标量比较（正常情况
        # 下列子查询已在 _cmd_select / _select_from_relation 里被路由到
        # 逐行求值路径，走不到这里）。原实现把 subquery 直接丢掉，产出
        # `{'op': '=', 'value': '10'}` 这种没有左值的条件，下游报
        # 「缺少: 'column'」（误导性错误），还可能把条件静默放过。
        # 显式报错，绝不静默丢条件。
        if isinstance(sub, dict):
            raise ValueError(
                f"无法求值的子查询谓词（op={op}）：该形态没有一次性求值的语义")
        # 结构上不像已知形态，原样返回（保持旧行为）
        rw = dict(where)
        rw.pop('subquery', None)
        return rw, False

    @staticmethod
    def _referenced_columns(where, out=None):
        """递归收集条件里引用到的列名（含有子查询）

        比较条件的右侧也可能是列（`orders.user_id = users.id`），
        带点号又没被引号包起来的一律按限定列名收集，供相关子查询检测用。
        """
        if out is None:
            out = []
        if not isinstance(where, dict):
            return out
        col = where.get('column')
        if isinstance(col, str) and col:
            out.append(col)
        val = where.get('value')
        if (isinstance(val, str) and '.' in val
                and not val.startswith(("'", '"'))
                and re.match(r'^[\w.]+$', val)):
            out.append(val)
        # 表达式谓词（`LOWER(u.name) = 'x'`）：列引用藏在表达式节点里，
        # 不收集的话 `_is_correlated` 会漏判 → 相关子查询被当成非相关
        # 一次性求值（静默错答）。聚合参数内部的引用不算（见
        # `expr_columns_outside_agg`：由残留检查兜底显式报错）。
        pred = where.get('expr_pred')
        if isinstance(pred, dict):
            for node in (pred.get('left'), pred.get('right'),
                         pred.get('low'), pred.get('high')):
                out.extend(_expr_columns_outside_agg(node))
            for node in (pred.get('values') or []):
                out.extend(_expr_columns_outside_agg(node))
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                Executor._referenced_columns(c, out)
        if isinstance(where.get('subquery'), dict):
            Executor._referenced_columns(where['subquery'], out)
        return out

    def _subquery_ast_columns(self, sub, root=True) -> list:
        """取子查询 AST 里出现的所有列引用（SELECT/WHERE/HAVING/JOIN ON）"""
        if not isinstance(sub, dict):
            return []
        cmds = sub.get('segments')
        if cmds:
            out = []
            for c in cmds:
                out.extend(self._subquery_ast_columns(c, root=False))
            return out

        cols = []
        for c in (sub.get('columns') or []):
            for part in str(c).split(','):
                part = part.strip()
                if not part or part == '*' or '(' in part:
                    continue
                cols.append(part)
        cols.extend(self._referenced_columns(sub.get('where')))
        cols.extend(self._referenced_columns(sub.get('having')))
        for j in (sub.get('joins') or []):
            for k in (j.get('on') or {}).values():
                if isinstance(k, str):
                    cols.append(k)
        return cols

    @staticmethod
    def _qualified_owner(text) -> str:
        """`users.id` -> 'users'；裸列名 -> ''"""
        t = str(text or '').strip().strip('`"')
        if '.' in t and re.match(r'^[\w.]+$', t):
            return t.rsplit('.', 1)[0].lower()
        return ''

    def _check_correlated(self, sub, outer_table, outer_alias=None):
        """相关子查询检测：子查询引用了外层表的列就直接报错

        判据：
        1. 列名限定符等于外层表名、且不是子查询自己的表/别名 —— 外层引用；
        2. WHERE 里的裸列名在子查询表里不存在，却在外层表里存在 —— 外层引用。
        （`SELECT 1 FROM orders`、`SELECT user_id FROM orders WHERE id > 0`
        这类自洽子查询一律放行。）
        """
        if not outer_table:
            return
        outers = {str(outer_table).lower()}
        if outer_alias:
            outers.add(str(outer_alias).lower())

        # 子查询自身可用的限定符（表名 + 别名 + JOIN 进来的表）
        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())

        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            # 限定符是外层表名/别名，而子查询自己并没有同名表/别名 → 外层引用
            if owner and owner in outers and owner not in inner_names:
                raise ValueError(
                    f"不支持相关子查询（引用了外层列 {c}）")

        inner = sub.get('table')
        inner_cols = set()
        if inner:
            try:
                inner_tbl = self.db.get_table(inner)
                inner_cols = {col['name'].lower() for col in inner_tbl.cols}
            except KeyError:
                pass

        outer_cols = set()
        try:
            outer_tbl = self.db.get_table(outer_table)
            outer_cols = {col['name'].lower() for col in outer_tbl.cols}
        except KeyError:
            pass

        if not inner_cols or not outer_cols:
            return

        # 裸列名（不带限定符）只在子查询表里找不到、只在外层表里存在时，
        # 才可以断定是外层引用 —— 避免把 `SELECT id FROM orders` 这种
        # 同名自洽子查询误判成相关子查询。只看 WHERE：
        # SELECT 列表里的裸列名属于投影，不是条件。
        for c in self._referenced_columns(sub.get('where')):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                raise ValueError(
                    f"不支持相关子查询（列 {c} 只在子查询外的表里存在）")

    # ==================== CTE / 窗口函数 ====================

    def _execute_with_ctes(self, parsed, session):
        """物化 WITH 子句，然后执行主体语句

        CTE 存为内存关系并逐个求值，因此后面的 CTE 可以引用前面的 CTE。
        递归 CTE（`WITH RECURSIVE t AS (base UNION [ALL] recursive)`）
        走迭代定点路径，见 _run_recursive_cte。
        """
        saved = dict(self._cte_relations)
        try:
            for cte in parsed.get('ctes') or []:
                sub = cte.get('query')
                if sub is None:
                    return self._err(
                        1064, f"CTE {cte['name']} 无法解析")
                if cte.get('recursive') and self._is_recursive_body(sub):
                    res = self._run_recursive_cte(cte, session)
                else:
                    res = self.execute(sub, session)
                if res.get('type') == 'error':
                    return res
                self._cte_relations[cte['name'].lower()] = _CTERelation(
                    cte['name'],
                    res.get('columns') or [],
                    res.get('rows') or [])
            body = {k: v for k, v in parsed.items() if k != 'ctes'}
            return self.execute(body, session)
        finally:
            self._cte_relations = saved

    # 递归 CTE 的最大迭代轮数（防止 `UNION ALL` 写错导致死循环）
    MAX_CTE_ITERATIONS = 1000

    @staticmethod
    def _is_recursive_body(query) -> bool:
        """递归体的形态：`base UNION [ALL] recursive`（两个分支）"""
        return (isinstance(query, dict)
                and query.get('command') == 'UNION'
                and len(query.get('segments') or []) == 2)

    def _run_recursive_cte(self, cte, session):
        """迭代求解递归 CTE

        第 0 轮先算 base 分支；之后每一轮把 CTE 名绑定为「上一轮新增的行」
        再求值递归分支并追加，直到某轮没有新增行为止。
        `UNION` 需要跨全局去重，`UNION ALL` 原样追加（只按递归分支返回空
        来终止，因此必须依赖迭代上限兜底）。
        """
        name = cte['name']
        query = cte['query']
        segments = query['segments']
        flags = query.get('unions_all') or []
        union_all = bool(flags[0]) if flags else False

        # 第 0 轮：base 分支（此时 CTE 还不能引用自己）
        res = self.execute(segments[0], session)
        if not isinstance(res, dict) or res.get('type') == 'error':
            return self._err(
                1105, f"递归 CTE {name} 的 base 分支失败: "
                      f"{(res or {}).get('message', '未知错误')}")
        cols = res.get('columns') or []
        rows = [list(r) for r in (res.get('rows') or [])]
        seen = set(self._row_key(r) for r in rows)

        prev = rows
        iterations = 0
        while prev:
            if iterations >= self.MAX_CTE_ITERATIONS:
                return self._err(
                    1105, f"递归 CTE {name} 超过最大迭代次数 "
                          f"({self.MAX_CTE_ITERATIONS})，可能存在无限递归")
            iterations += 1

            # 关键：绑定「上一轮新增的行」，而不是全部累积结果
            self._cte_relations[name.lower()] = _CTERelation(
                name, cols, prev)
            rres = self.execute(segments[1], session)
            if not isinstance(rres, dict) or rres.get('type') == 'error':
                return self._err(
                    1105, f"递归 CTE {name} 的递归分支失败: "
                          f"{(rres or {}).get('message', '未知错误')}")
            produced = [list(r) for r in (rres.get('rows') or [])]
            if not produced:
                break

            if union_all:
                new = produced
            else:
                new = []
                for r in produced:
                    k = self._row_key(r)
                    if k not in seen:
                        seen.add(k)
                        new.append(r)
                if not new:
                    break

            rows.extend(new)
            prev = new

        return self._rs(cols, rows)

    @staticmethod
    def _row_key(row):
        """行去重用的键（list/dict 不可哈希，先转成可哈希形态）"""
        return tuple(
            v if isinstance(v, (int, float, str, bytes, type(None), bool))
            else str(v)
            for v in row)

    @staticmethod
    def _split_alias(text: str):
        """把 `expr AS alias` / `expr alias` 拆成 (expr, alias)"""
        m = re.match(r'^(.+?)\s+AS\s+[`"]?(\w+)[`"]?$',
                     text, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2)
        return text.strip(), None

    @staticmethod
    def _col_index_of(cols, name: str) -> int:
        """在列名列表里定位，支持 `t.col` 与 `col AS x` 形式

        JOIN 路径产出的列名带限定前缀（`u.id` / `o.amt`），因此精确匹配全部
        失败后，再按「列名末段」唯一匹配一次（`amt` → `o.amt`）。末段歧义
        （`u.id` 与 `o.id` 并存时的裸 `id`）仍返回 -1，由调用方显式报错，
        绝不随便挑一列 —— 与计划执行器 `_resolve_col` 的语义保持一致，
        这样 JOIN 的后置处理路径与纯计划路径对裸列名的解析结果相同。
        """
        if name == '*':
            return -1
        expr, _ = Executor._split_alias(name)
        candidates = [name, expr]
        if '.' in expr:
            candidates.append(expr.rsplit('.', 1)[1])
        for c in candidates:
            if c in cols:
                return cols.index(c)
        want = str(expr or name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        if not want:
            return -1
        hits = [i for i, c in enumerate(cols)
                if isinstance(c, str)
                and str(c).strip().strip('`"').rsplit('.', 1)[-1].lower()
                == want]
        if len(hits) == 1:
            return hits[0]
        return -1

    def _base_rows_and_cols(self, src, where, read_view=None):
        """取「全部列 + 过滤后的行」；src 可以是 Table 或 _CTERelation

        条件里若残留 `{'negate': ...}`（NOT 作用在 LIKE 这类存储层没有
        取反运算符的形态上），先取全部行再用执行器自己的统一求值器过滤
        —— 三值语义（NOT UNKNOWN 仍是 UNKNOWN）也由它负责，
        不会因为 `NOT (c LIKE ...)` 把 NULL 行错误地放行。
        """
        if isinstance(where, dict) and 'negate' in where \
                and isinstance(where['negate'], dict):
            target = where['negate']
            if isinstance(src, _CTERelation):
                cols = list(src.col_names)
                rows = [list(r) for r in src.rows]
            else:
                cols, rows = src.select(['*'], None, None, None, read_view)
                cols, rows = list(cols), [list(r) for r in rows]
            kept = []
            for r in rows:
                # 三值逻辑：NOT 只在「内层确定为假」时为真；
                # 内层为 None（UNKNOWN，例如 `NULL LIKE 'A%'`）时不通过，
                # 与 `is False` 的判定天然一致
                if self._eval_condition(r, cols, target) is False:
                    kept.append(r)
            return cols, kept
        # 表达式谓词（函数 / 算术，`WHERE LOWER(name) = 'alice'`、
        # `WHERE salary * 2 > 230`）：存储层 `_match` 认不出这种节点
        # （KeyError('column')），因此取全部行后用统一求值器逐行过滤。
        # 三值逻辑：UNKNOWN（None）按「不通过」处理，与 SQL 的 WHERE 一致。
        # 「右侧是列引用」的比较（`WHERE user_id = id`）同样：存储层会把
        # 右侧当字符串字面量（恒假 → 静默空集），必须在这里按列取值求值。
        if isinstance(where, dict) and (
                self._where_has_expr_pred(where)
                or self._where_has_column_rhs(where)):
            if isinstance(src, _CTERelation):
                cols = list(src.col_names)
                rows = [list(r) for r in src.rows]
            else:
                cols, rows = src.select(['*'], None, None, None, read_view)
                cols, rows = list(cols), [list(r) for r in rows]
            return cols, [r for r in rows
                          if self._eval_condition(r, cols, where)]
        if isinstance(src, _CTERelation):
            rows = [list(r) for r in src.rows]
            if where:
                rows = [r for r in rows
                        if _StorageTable._match(src, r, where)]
            return list(src.col_names), rows
        cols, rows = src.select(['*'], where, None, None, read_view)
        return list(cols), [list(r) for r in rows]

    def _select_advanced(self, p, src, read_view=None, where=_UNSET,
                         session=None):
        """GROUP BY / 窗口函数 / 普通投影的统一执行路径

        分组与窗口都必须先拿到「全部列的行」，因此流程固定为：
        取基础行 → （可选）分组聚合 → （可选）计算窗口 → 投影 → DISTINCT/ORDER BY/LIMIT。

        where: WHERE 子查询求值改写后的条件。默认 _UNSET 表示"调用方没改写"，
        此时用 p['where']；显式传 None 表示"条件已被改写成恒真、不再需要过滤"。
        session: 投影里的标量子查询要重新执行（折叠后）子查询，需要会话
        （事务读视图）；拿不到时 `_project_rows` 会退回 `_active_session`。
        """
        src_where = p.get('where') if where is _UNSET else where
        base_cols, rows = self._base_rows_and_cols(
            src, self._normalize_where(src_where), read_view)
        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(
                p, src, base_cols, rows, session=session,
                read_view=read_view)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows, session=session,
                                       read_view=read_view)
        return self._project_rows(p, base_cols, rows, session=session,
                                  read_view=read_view,
                                  outer_name=p.get('table'),
                                  outer_alias=p.get('table_alias'))

    # 存储层 Table._aggregate 支持的聚合函数（用于识别「无 GROUP BY 聚合」）
    _AGG_FUNCS = ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX')

    @classmethod
    def _has_bare_aggregate(cls, p) -> bool:
        """SELECT 列表里有聚合函数、且没写 GROUP BY（整表聚成一组）

        内存关系（CTE / FROM 子查询）路径原先直接按行投影，`SELECT
        COUNT(*) FROM (SELECT ...) x` 会输出「每行一个 NULL」——静默错答。
        这里识别出来交给 `_group_rows` 的「零分组键」形态：key=() 只产生
        一个分组，再算聚合值。

        判定分两层：
        * `名字(` 开头且名字是存储层支持的 5 个聚合（`COUNT(*)` / `SUM(x)`）；
        * **表达式内部含聚合**（`ABS(SUM(v))`、`ROUND(AVG(v), 1)`）——
          这类形态原先既不进分组路径、也不是标量函数，最后报「列不存在」。
        标量函数（`SELECT LOWER(name) FROM cte`）不含聚合，仍然按行投影。
        """
        if p.get('group_by') or p.get('windows'):
            return False
        for c in (p.get('columns') or []):
            s = str(c).strip()
            if s == '*':
                continue
            expr = cls._split_alias(s)[0].strip()
            m = re.match(r'^(\w+)\s*\(', expr)
            if m and m.group(1).upper() in cls._AGG_FUNCS:
                return True
            node = _parse_scalar_expr(expr)
            if node is not None and _expr_has_agg(node):
                return True
        return False

    @staticmethod
    def _strip_qualifier(text: str) -> str:
        """去掉列名上的表限定符：`o.amount` -> `amount`

        存储层的 _col_idx / _aggregate 只认裸列名，因此带表别名的
        `SUM(o.amount)`、`GROUP BY u.city` 必须归一化后才能复用它们。
        """
        return re.sub(r'\b\w+\.(\w+)', r'\1', str(text))

    def _normalize_where(self, where, strip=True):
        """把 WHERE 条件里的限定列名归一化（`u.city` -> `city`）

        存储层的 Table._match 只认裸列名；只改 column 字段，
        value 原样保留（字符串常量里也可能含点）。

        `NOT (...)` 存储层完全不认识（`_match` 只认 and/or/column+op+value，
        遇到 {'not':...} 会 KeyError），因此这里把它改写成等价的德摩根形式：
            NOT (A AND B)  ->  OR(NOT A, NOT B)
            NOT (A OR  B)  ->  AND(NOT A, NOT B)
            NOT (col = v)  ->  col != v
        叶子比较一律**先取反再交给存储层**（not op），
        这样 `_base_rows_and_cols` 传给存储层的条件永远是它能认的形态，
        既避免了 KeyError，也让 NOT 条件能享受存储层的索引/下推，
        不必把整表读进内存再过滤。

        strip=False：**JOIN 后置处理路径**里保留限定列名不动。连接结果的
        列名本身带前缀（`u.id` / `o.amt`），一旦归一化成裸名就会歧义
        （`id` 同时属于两张表）甚至解析失败；该路径不经过存储层
        `_match`（行已经在内存里），因此不需要归一化。
        """
        if not isinstance(where, dict):
            return where
        if isinstance(where.get('not'), dict):
            return self._negate_condition(
                self._normalize_where(where['not'], strip))
        if 'and' in where:
            return {'and': [self._normalize_where(c, strip)
                            for c in where['and']]}
        if 'or' in where:
            return {'or': [self._normalize_where(c, strip)
                           for c in where['or']]}
        out = dict(where)
        if strip and isinstance(out.get('column'), str):
            out['column'] = self._strip_qualifier(out['column'])
        return out

    # 比较运算符的取反表（>= / <= 的取反要保留等号）
    _NOT_OP = {
        '=': '!=', '!=': '=', '<': '>=', '>': '<=',
        '<=': '>', '>=': '<',
    }

    @classmethod
    def _negate_condition(cls, where):
        """对**已归一化**的条件取反（德摩根律），产出存储层认识的条件"""
        if not isinstance(where, dict):
            return where
        if 'and' in where:
            return {'or': [cls._negate_condition(c) for c in where['and']]}
        if 'or' in where:
            return {'and': [cls._negate_condition(c) for c in where['or']]}
        if isinstance(where.get('not'), dict):
            return cls._negate_condition(where['not'])
        op = where.get('op')
        val = where.get('value')
        if 'column' in where and op in cls._NOT_OP:
            # `NOT (c IS NULL)` 即 `c IS NOT NULL`：value=None 保持不变，
            # 只翻转运算符（=' -> '!=、'!=' -> '='），NULL 语义自动对齐
            return {'column': where.get('column'),
                    'op': cls._NOT_OP[op],
                    'value': val,
                    'value_column': where.get('value_column', False)}
        if 'agg' in where and op in cls._NOT_OP:
            return {'agg': where.get('agg'), 'op': cls._NOT_OP[op],
                    'value': val}
        if op in ('IN', 'NOT IN'):
            # IN 列表 / IN (子查询) 直接翻转 IN ↔ NOT IN：三值语义由
            # _in_predicate / _rewrite_in_lists 负责，含 NULL 时同样正确
            return {'column': where.get('column'),
                    'op': 'NOT IN' if op == 'IN' else 'IN',
                    'values': where.get('values'),
                    'subquery': where.get('subquery')}
        # 其它形态（LIKE 等）存储层没有取反运算符：打上 negate 标记，
        # 由 `_base_rows_and_cols` 取全部行后用统一求值器做三值过滤
        return {'negate': dict(where)}

    def _where_has_qualified(self, where) -> bool:
        """WHERE 里是否出现 `u.city` 这类限定列名"""
        if not isinstance(where, dict):
            return False
        if 'not' in where:
            return self._where_has_qualified(where['not'])
        if 'and' in where:
            return any(self._where_has_qualified(c)
                       for c in where['and'])
        if 'or' in where:
            return any(self._where_has_qualified(c)
                       for c in where['or'])
        col = where.get('column')
        return isinstance(col, str) and '.' in col

    @classmethod
    def _where_has_not(cls, where) -> bool:
        """WHERE 里是否出现 `NOT (...)` 节点

        存储层 `Table._match` 只认 and/or/column+op+value，遇到
        `{'not': ...}` 会直接 KeyError（外层看起来像"表不存在"）。
        只要出现 NOT 就让查询走统一求值路径 `_eval_condition`。
        """
        if not isinstance(where, dict):
            return False
        if isinstance(where.get('not'), dict):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_not(c):
                    return True
        return False

    @classmethod
    def _where_has_negate(cls, where) -> bool:
        """条件里是否残留 negate 标记（存储层无法表达的 NOT 取反）

        例如 `NOT (name LIKE 'A%')`：LIKE 没有取反运算符，_negate_condition
        只能标成 {'negate': {...}}，由 _base_rows_and_cols 用执行器自己的
        三值求值器过滤。
        """
        if not isinstance(where, dict):
            return False
        if isinstance(where.get('negate'), dict):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_negate(c):
                    return True
        return False

    @classmethod
    def _where_has_is_null(cls, where) -> bool:
        """WHERE 里是否出现字面量 NULL 比较（`col IS NULL` / `IS NOT NULL`）

        解析器把 IS [NOT] NULL 编码成 value=None 的 =/!= 条件。存储层
        `Table._match` 虽然能求值，但 `_find_pks` 会用
        `coerce(None, col_type)` 走索引/主键快路径，结果把 NULL 当成
        默认值（`amount IS NULL` 返回 0 行、`IS NOT NULL` 返回全部行）。
        因此含字面量 NULL 的条件统一走执行器的统一求值路径。
        """
        if not isinstance(where, dict):
            return False
        if 'not' in where:
            return cls._where_has_is_null(where['not'])
        if 'value' in where and where.get('value') is None \
                and 'column' in where:
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_is_null(c):
                    return True
        return False

    @classmethod
    def _where_has_expr_pred(cls, where) -> bool:
        """条件里是否含**表达式谓词**（`{'expr_pred': {...}}`）

        表达式谓词 = 标量函数 / 算术参与比较（`WHERE salary * 2 > 230`、
        `WHERE LOWER(name) = 'alice'`、`HAVING SUM(v) * 2 > 100`）。
        存储层 `Table._match` 只认 `column + op + value`，遇到这种节点会
        `KeyError('column')`（外层报成"表不存在"，与真实原因无关），
        因此只要出现就必须走执行器的统一求值路径。
        """
        if not isinstance(where, dict):
            return False
        if isinstance(where.get('expr_pred'), dict):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_expr_pred(c):
                    return True
        for key in ('not', 'negate'):
            inner = where.get(key)
            if isinstance(inner, dict) and cls._where_has_expr_pred(inner):
                return True
        return False

    @classmethod
    def _where_has_column_rhs(cls, where) -> bool:
        """条件里是否有**右侧是列引用**的比较（`{'value_column': True}`）

        这种节点存储层 `Table._match` 只当字面量比较（`a == 'b'` 恒假），
        必须走执行器的统一求值路径由 `_eval_condition` 按列取值比较，
        否则单表 `WHERE user_id = id` 会静默返回空集。
        """
        if not isinstance(where, dict):
            return False
        if where.get('value_column'):
            return True
        if where.get('op') in ('IN', 'NOT IN') \
                and isinstance(where.get('values'), list):
            # `IN (t.col)`：带限定名的候选项是列引用，存储层会当字面量
            if any(isinstance(v, str) and '.' in v.strip().strip('`"')
                   for v in where['values']):
                return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_column_rhs(c):
                    return True
        for key in ('not', 'negate'):
            inner = where.get(key)
            if isinstance(inner, dict) and cls._where_has_column_rhs(inner):
                return True
        return False

    @classmethod
    def _projection_output_names(cls, columns):
        """投影列表对应的**输出列名**；无法确定（`*` / 空）时返回 None

        `SELECT name, salary AS v` → `['name', 'v']`（别名优先，其次去限定符
        的表达式文本）。`*` 展开后列名取决于表结构，这里返回 None 表示
        「任何普通列名都可能存在」，由调用方决定是否保守地走存储层。
        """
        cols = [str(c) for c in (columns or [])]
        if not cols or any(c.strip() == '*' or c.strip().endswith('.*')
                           for c in cols):
            return None
        names = []
        for c in cols:
            expr, alias = cls._split_alias(c)
            names.append(alias or cls._strip_qualifier(expr).strip())
        return names

    @classmethod
    def _is_ordinal_order_key(cls, text) -> bool:
        """`ORDER BY 2` 的序号形态（1 基，指向输出列）"""
        return bool(re.match(r'^\d+$', str(text).strip()))

    @staticmethod
    def _order_key_error_message(pos, text, reason='') -> str:
        """排序键无法解析时的统一报错文案（带**第几个键**与原因）"""
        base = f"ORDER BY 第 {pos + 1} 个排序键 '{text}' 无法解析"
        if reason:
            return f"{base}：{reason}（ORDER BY 的键绝不会被静默忽略）"
        return (f"{base}：它既不是输出列 / 投影别名 / 列序号，也不是可求值的"
                f"标量表达式（ORDER BY 的键绝不会被静默忽略）")

    @classmethod
    def _order_by_needs_eval(cls, order_by, columns=None,
                             table_cols=None) -> bool:
        """ORDER BY 是否必须走执行器的**统一排序路径**（`_finish_select`）

        下列任一成立就返回 True；否则可以继续交给存储层 `db/table.py::select`
        按单键排序（那条路径只认 `(输出列名, desc)`）：

        * **键数 > 1**（多键排序存储层不支持，`ORDER BY a, b` 原来只按 a 排）；
        * 键是**序号**（`ORDER BY 2`，存储层不认识）；
        * 键是需要求值的**表达式**（`LENGTH(name)` / `salary * 2` /
          `SUM(salary)`），存储层找不到同名列会 `except: pass` 静默不排序；
        * 键**不是投影输出列名**（`SELECT name FROM emp ORDER BY salary`：
          存储层 out_cols 里没有 salary，同样静默不排序）；
        * 键解析不出任何形态 —— 交给统一路径**显式报错**，绝不落存储层丢弃。

        columns：投影列表（`SELECT *` 时无法从它推出输出列名）；
        table_cols：`SELECT *` 时存储层实际会输出的列名（表列），由调用方
        传入 —— 有了它，`SELECT * FROM t ORDER BY 不存在的列` 也能走统一
        路径显式报错，而不是落进存储层的 `except: pass` 静默不排序。
        """
        keys = normalize_order_by(order_by)
        if not keys:
            return False
        if len(keys) > 1:
            return True
        out_names = (cls._projection_output_names(columns)
                     if columns is not None else None)
        if out_names is None:
            # `SELECT *`：存储层的输出列就是表列
            out_names = ([str(c) for c in table_cols]
                         if table_cols is not None else None)
        for text, _desc in keys:
            t = str(text).strip()
            if not t:
                continue
            if cls._is_ordinal_order_key(t):
                return True
            if re.match(r'^[`"]?[\w.]+[`"]?$', t):
                # 纯列名 / 限定列名：投影里能直接找到才交给存储层
                if out_names is None:
                    return False
                if t in out_names or t.strip('`"') in out_names:
                    return False
                return True
            # 非纯列名：表达式（或认不出的形态）一律走统一路径
            return True
        return False

    @classmethod
    def _agg_needs_expr_eval(cls, text) -> bool:
        """投影项是否是「存储层聚合算不了、要执行器自己算」的形态

        存储层 `Table._aggregate` 只认 `FN(单列)` / `FN(*)`，且只支持
        COUNT/SUM/AVG/MIN/MAX；下列形态必须交给表达式聚合求值器
        （`_eval_agg_expr_over_rows`）：

        * 参数是表达式：`SUM(salary * 2)`、`COUNT(ABS(v))`；
        * 函数名不在那 5 个之内：`ROUND(AVG(v), 1)`、`STDDEV(v)`、
          `GROUP_CONCAT(name)`（存储层对未知聚合名**静默返回 NULL**，
          这也是本次顺带修掉的静默错答）。
        """
        expr, _ = cls._split_alias(str(text))
        e = expr.strip()
        m = re.match(r'^(\w+)\s*\((.*)\)\s*$', e, re.DOTALL)
        if not m:
            return False
        fn, arg = m.group(1).upper(), m.group(2).strip()
        if fn in cls._AGG_FUNCS:
            return not re.match(r'^(\*|[\w.]+)$', arg)
        node = _parse_scalar_expr(e)
        return bool(node is not None and _expr_has_agg(node))

    @classmethod
    def _projection_has_between(cls, text) -> bool:
        """投影项是否是含 BETWEEN 的表达式（`id BETWEEN 1 AND 3`）"""
        expr, _ = cls._split_alias(str(text))
        node = _parse_scalar_expr(expr)
        return bool(node is not None and _expr_has_between(node))

    @classmethod
    def _arith_projection_expr(cls, text):
        """投影项是否是算术表达式（可带 AS 别名），是则返回表达式文本

        `salary*2 AS d` → 'salary*2'；`id` → None；`2*salary` → '2*salary'。
        要求至少一侧是列名，纯常量算式（`SELECT 1+2`）不在此列（走常量投影）。
        """
        expr, _ = cls._split_alias(str(text))
        spec = cls._parse_arith(expr)
        if spec is None:
            return None
        left, _, right = spec
        if not (cls._is_plain_literal(left) and cls._is_plain_literal(right)):
            return expr.strip()
        return None

    @staticmethod
    def _is_plain_literal(tok) -> bool:
        """操作数是否是数字字面量（`2` / `-1.5`）"""
        return bool(re.match(r'^-?\d+(?:\.\d+)?$', str(tok)))

    def _group_rows(self, p, src, base_cols, rows, session=None,
                    read_view=None, extra_names=None):
        """按 GROUP BY 分组并计算聚合列，返回 (分组后的列名, 分组后的行)

        原实现把 group_by 完全忽略，`SELECT dept, COUNT(*) ... GROUP BY dept`
        会退化成「全表一行」，属于静默错答。

        session / read_view：HAVING 里的子查询需要（重新）执行子查询，
        必须沿用同一个事务读视图；拿不到时由 `_use_session` 兜底。
        extra_names：JOIN 后置处理路径传入的连接表名/别名，供 HAVING 里的
        子查询折叠使用（见 `_eval_having_with_subquery`）。
        """
        # ★ 严格模式运行期兜底：非分组键裸列在下面「分组后只剩 keys+聚合值」
        # 的形态里会被静默丢掉（随后 `_project_rows` 报「列不存在」，完全
        # 看不出是 GROUP BY 的问题）。这里先按同一份静态校验显式报错，
        # 保证两条执行路径的报错信息逐字一致；逃生口见 strict_group_by。
        from .parser import validate_group_by, resolve_strict_group_by
        validate_group_by(
            p, strict=resolve_strict_group_by(self.strict_group_by))

        keys = [str(k).strip() for k in (p.get('group_by') or [])]
        # 分组键可以是**表达式**（`GROUP BY UPPER(dept)`、`GROUP BY LENGTH(name)`）：
        # 纯列名走原来的下标取法，表达式则逐行求值。两者都取不到时仍显式报错。
        key_getters = []
        for k in keys:
            i = self._col_index_of(base_cols, k)
            if i >= 0:
                key_getters.append(('idx', i))
                continue
            node = _parse_scalar_expr(self._split_alias(k)[0])
            if node is None or _expr_has_agg(node):
                raise KeyError(f"GROUP BY 列不存在: {keys}")
            self._check_expr_functions(node, 'GROUP BY')
            key_getters.append(('expr', node))

        agg_cols = [str(c).strip() for c in (p.get('columns') or [])
                    if re.match(r'\w+\s*\(', str(c).strip())]
        # 计算用归一化后的表达式，输出列名保留用户原样（含表别名）
        agg_eval = [self._strip_qualifier(c) for c in agg_cols]

        having = p.get('having')
        # HAVING 里的聚合同样要归一化限定符：`HAVING SUM(o.amount) > 25`
        # 里的 `SUM(o.amount)` 交给聚合求值器前必须变成 `SUM(amount)`，
        # 否则报「列 SUM(o.amount) 不存在」。
        # 归一化前后用同一套键查 agg_vals：eval_cols 存归一化名，
        # having_aggs 保留原样，下面按归一化名建 extra 映射。
        having_aggs = self._collect_agg_exprs(having)
        having_aggs_eval = [self._strip_qualifier(a) for a in having_aggs]
        eval_cols = list(agg_eval)
        for a in having_aggs_eval:
            if a not in eval_cols:
                eval_cols.append(a)
        # HAVING 里有子查询 → 每个分组行单独求值（分组键作为外层行折叠），
        # 结果按折叠值缓存；没有子查询时仍走原来的 _eval_condition 快路径
        having_has_sub = self._contains_subquery(having)
        having_cache = {}

        # ORDER BY 表达式里用到的聚合（`ORDER BY ROUND(AVG(salary), 1)`）：
        # 分组输出列里可能根本没有这一列。这里把它们当**隐藏列**补进
        # 分组结果（只供 `_order_by_sort_values` 取聚合值，不会出现在
        # SELECT 的输出列里，因为输出列由投影列表决定）。
        # 多键 ORDER BY：**每个键**都要收集（原来只看第一个键，
        # `ORDER BY dept, SUM(salary)` 里的聚合取不到值）。
        order_aggs = []
        for ob_text, _ob_desc in normalize_order_by(p.get('order_by')):
            node_ob = _parse_scalar_expr(str(ob_text).strip())
            if node_ob is not None and _expr_has_agg(node_ob):
                for t in self._expr_agg_texts_in_node(node_ob):
                    if t not in order_aggs and t not in eval_cols:
                        order_aggs.append(t)

        groups, order = {}, []
        for r in rows:
            parts = []
            for kind, spec in key_getters:
                if kind == 'idx':
                    parts.append(r[spec] if 0 <= spec < len(r) else None)
                else:
                    parts.append(self._eval_expr_in_row(
                        spec, base_cols, r, None, 'GROUP BY'))
            key = tuple(parts)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)

        out_rows = []
        for key in order:
            vals = list(key)
            agg_vals = {}
            if eval_cols or order_aggs:
                # 聚合求值：优先用表达式聚合求值器（支持 `SUM(salary * 2)`、
                # `ROUND(AVG(v), 1)`、`STDDEV`、`GROUP_CONCAT` 等存储层算不了
                # 的形态），存储层能算的 `FN(单列)` 也走同一条路（结果逐字
                # 相同，见 `_eval_agg_expr_over_rows`）；真正解析不出聚合
                # 形态的（`COUNT(DISTINCT x)`）才回落到存储层 `_aggregate`
                # —— 那里会显式报错，绝不静默给 NULL。
                agg_vals = self._compute_agg_values(
                    eval_cols + order_aggs, src, base_cols, groups[key])
                vals += list(agg_vals[a] for a in agg_eval)
                vals += list(agg_vals[t] for t in order_aggs)
            if having:
                # HAVING 的聚合值单独备好：可能不在 SELECT 列表里
                # （`SELECT city FROM users GROUP BY city HAVING COUNT(*)>5`），
                # 此时 SELECT 输出里没有这一列，必须由 agg_vals 提供。
                # 键用用户原样的表达式（_eval_condition 按原样查表）
                extra = {a: agg_vals.get(e)
                         for a, e in zip(having_aggs, having_aggs_eval)}
                hcols = list(keys) + list(eval_cols)
                if having_has_sub:
                    # GROUP BY 之后的行只剩分组键 + 聚合值：子查询里的
                    # 外层引用只能折叠成「分组键」的取值（引用别的列要
                    # 显式报错，见 _eval_having_with_subquery）
                    if not self._eval_having_with_subquery(
                            vals, hcols, having, extra, p, session,
                            read_view, having_cache, extra_names):
                        continue
                elif not self._eval_condition(vals, hcols, having, extra):
                    continue
            out_rows.append(vals)
        return keys + agg_cols + order_aggs, out_rows

    # ==================== 聚合表达式求值（分组 / HAVING） ====================

    def _compute_agg_values(self, exprs, src, base_cols, rows) -> dict:
        """算出一组聚合表达式在**同一分组**上的取值 {表达式文本: 值}

        表达式文本按 `_strip_qualifier` 归一化过（`SUM(o.amount)` →
        `SUM(amount)`），输出键与调用方的 eval_cols 一致。

        两趟：
        1. **纯聚合**（`COUNT(*)` / `SUM(v)`）先算出来，放进 `agg_ctx`；
        2. 其余表达式（`SUM(v * 2)`、`ROUND(AVG(v), 1)`、
           `f(COUNT(*))`）求值时，内层聚合直接从 `agg_ctx` 取值。

        分两趟是必须的：`f(COUNT(*))` 这种「聚合嵌在函数调用里」的形态，
        `eval_agg_expr_over_rows` 会把 `COUNT(*)` 当**逐行**表达式再求一遍
        （`COUNT` 是聚合名，逐行求值取不到值 → 显式报错）。有了 agg_ctx，
        内层聚合按第一趟的真实值代入，HAVING 才真的起到过滤作用。
        """
        out = {}
        agg_ctx = {}
        for e in exprs:
            node = _parse_scalar_expr(self._split_alias(str(e))[0])
            if node is not None and _expr_has_agg(node):
                agg_ctx[e] = self._eval_agg_expr_over_rows(
                    node, base_cols, rows, 'GROUP BY/HAVING')
        fallback = []
        for e in exprs:
            if e in agg_ctx:
                out[e] = agg_ctx[e]
                continue
            node = _parse_scalar_expr(self._split_alias(str(e))[0])
            if node is not None:
                out[e] = self._eval_agg_expr_over_rows(
                    node, base_cols, rows, 'GROUP BY/HAVING', agg_ctx)
                continue
            fallback.append(e)
        if fallback:
            # 存储层兜底：只认 `FN(单列)` / `FN(*)`，认不出会显式报错
            vals = list(_StorageTable._aggregate(src, rows, fallback))
            out.update(dict(zip(fallback, vals)))
        return out

    def _eval_agg_expr_over_rows(self, node, cols, rows, position='',
                                 agg_ctx=None):
        """在**分组行集合**上求值「含聚合函数的表达式」

        实现放在 `parser.eval_agg_expr_over_rows`（与计划执行器共用同一份：
        聚合语义、NULL 传播、除零 NULL、不支持函数报错、非分组键裸列报错
        都只有一处实现）。这里只负责注入「按列名取当前行取值」的回调、
        本实例的 GROUP BY 严格模式开关，以及 `agg_ctx`（已经按分组算好的
        聚合值 + `F(COUNT(*))` 这类嵌套聚合的求值方式）。
        """
        from .parser import eval_agg_expr_over_rows, resolve_strict_group_by
        strict = resolve_strict_group_by(self.strict_group_by)
        return eval_agg_expr_over_rows(
            node, rows,
            lambda r, name: self._expr_row_value(cols, r, name, position),
            position, strict, self._stored_function_resolver(position),
            agg_ctx or {},
            lambda n: self._eval_agg_ctx_node(n, agg_ctx or {}, cols, rows,
                                              position))

    def _eval_agg_ctx_node(self, node, agg_ctx, cols, rows, position=''):
        """求值一个**内层聚合**节点：先查 agg_ctx，取不到就现算

        `F(COUNT(*))` 里的 `COUNT(*)` 走这里：查第一趟算好的值；查不到
        （例如聚合只出现在 HAVING 且没被收集到）就地在同一个分组行集合上
        真算一次 —— 绝不静默返回 NULL 或让外层拿到一个"没过滤"的条件。
        """
        text = _render_scalar_expr(node)
        if text in agg_ctx:
            return agg_ctx[text]
        stripped = self._strip_qualifier(text)
        if stripped in agg_ctx:
            return agg_ctx[stripped]
        return self._eval_agg_expr_over_rows(
            node, cols, rows, position or 'GROUP BY/HAVING', agg_ctx)

    def _expr_row_value(self, cols, row, name, position=''):
        """分组求值时的列取值（列必须存在于 base_cols 且在当前行范围内）"""
        i = self._col_index_of(cols, name)
        if i < 0 or i >= len(row):
            raise ValueError(
                f"{position} 里的列 '{name}' 不存在（分组表达式里的列引用）")
        return row[i]

    def _eval_having_with_subquery(self, row, cols, where, agg_values, p,
                                   session, read_view, cache,
                                   extra_names=None):
        """HAVING 里带子查询的条件求值（逐分组行）

        HAVING 在 GROUP BY 之后求值，此时「外层行」是分组行：列只有
        分组键 + 聚合值。因此：

        * `{'and'/'or'/'not'}` 递归处理；
        * 不含子查询的叶子节点交给 `_eval_condition`（聚合谓词靠
          agg_values 取聚合值）；
        * 含子查询的叶子节点：
          - 相关（引用外层/分组键）→ `_eval_correlated_predicate`，
            把分组键取值折叠成字面量后执行子查询；
          - 非相关 → `_eval_outer_where` 的非相关分支（EXISTS/IN 一次性
            求值；标量比较走 `_eval_scalar_subquery`，整体只算一次）；
        * 引用不到分组键的列（例如 `HAVING (SELECT ... WHERE o.uid = u.id)`
          而 GROUP BY 只有 city）→ 显式报错。原实现直接 `return True`，
          把 HAVING 条件静默丢掉（实测所有分组都保留）。

        缓存：按折叠用到的分组键取值缓存子查询结果（同值分组共享一次
        执行），因此分组很多时也不会重复跑子查询。

        extra_names：JOIN 后置处理路径传入的连接表名/别名（外层行由多表
        连接而来），相关判定与折叠都要把它们算作外层限定符。
        """
        if not isinstance(where, dict):
            return True
        # NOT (...)：三值逻辑下 NOT UNKNOWN 仍是 UNKNOWN → 该分组不通过
        if 'not' in where and isinstance(where['not'], dict):
            inner = self._eval_having_with_subquery(
                row, cols, where['not'], agg_values, p, session,
                read_view, cache, extra_names)
            if inner is None:
                return None
            return not inner
        if 'and' in where:
            return all(self._eval_having_with_subquery(
                row, cols, c, agg_values, p, session, read_view, cache,
                extra_names)
                for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_having_with_subquery(
                row, cols, c, agg_values, p, session, read_view, cache,
                extra_names)
                for c in parts)

        sub = where.get('subquery')
        if not isinstance(sub, dict):
            # 普通列比较 / 聚合谓词：沿用统一求值器
            return self._eval_condition(row, cols, where, agg_values)

        outer_name = p.get('table')
        outer_alias = p.get('table_alias')
        session = self._use_session(session)
        correlated = (
            self._is_correlated(sub, outer_name, outer_alias, extra_names)
            or self._sub_owner_is_outer(sub, outer_name, outer_alias,
                                        extra_names))
        if correlated:
            # 先做「外层引用能否在分组行里取到值」的前置检查：取不到时
            # 给出比折叠失败更明确的信息（GROUP BY 之后只保留分组键）
            names = self._fold_outer_names(sub, outer_name, outer_alias,
                                           extra_names)
            missing = [
                c for c in self._outer_ref_columns(sub, names)
                if self._outer_column_value(cols, row, c) is _MISSING]
            if missing:
                raise ValueError(
                    f"HAVING 里的相关子查询引用了 '{missing[0]}'，"
                    f"但 GROUP BY 之后只保留分组键与聚合值，取不到该列："
                    f"请把该列加入 GROUP BY，或改成引用分组键")
            return self._eval_correlated_predicate(
                row, cols, where, session, outer_name, outer_alias,
                cache, read_view, extra_names)
        # 非相关子查询：EXISTS / IN 走集合语义，标量比较按行数语义收敛，
        # 两者都按子查询缓存（整体只求值一次）。`_eval_outer_where` 里还会
        # 做一次「外层引用藏在表达式里」的残留检查（命中即显式报错）。
        return self._eval_outer_where(
            row, cols, where, session, outer_name, outer_alias,
            cache, read_view, extra_names)

    # ==================== 条件求值（统一路径） ====================

    def _eval_condition(self, row, cols, where, agg_values=None):
        """统一路径上的 WHERE/HAVING 条件求值

        只处理分组后还要再过滤的场景（HAVING），因此实现得比
        plan_executor._eval_condition 窄：and/or/agg/普通比较。
        """
        if where is None:
            return True
        if not isinstance(where, dict):
            return True

        # NOT (...)：整体取反（可嵌套，可与 AND/OR 组合）
        if 'not' in where and isinstance(where['not'], dict):
            return not self._eval_condition(
                row, cols, where['not'], agg_values)

        # `{'negate': ...}`：`_normalize_where` 对「存储层没有取反运算符」的
        # 形态（例如 `NOT (name LIKE 'A%')`）打的标记。**必须在这里就地展开**：
        # 只被顶层 `_base_rows_and_cols` 特殊处理是不够的 —— 嵌在 AND/OR 里的
        # negate 走到这里会被当成「认不出的节点 → return True」而**静默丢掉**
        # 整个 NOT 条件（例如 `WHERE NOT (name LIKE 'A%') AND LOWER(dept)='eng'`）。
        # 三值逻辑：NOT UNKNOWN 仍是 UNKNOWN（None）。
        if isinstance(where.get('negate'), dict):
            inner = self._eval_condition(row, cols, where['negate'], agg_values)
            if inner is None:
                return None
            return not inner

        if 'and' in where:
            return all(self._eval_condition(row, cols, c, agg_values)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_condition(row, cols, c, agg_values)
                       for c in parts)

        # ---------- 表达式谓词（标量函数 / 算术）----------
        # `WHERE LOWER(name) = 'alice'`、`WHERE salary * 2 + 10 > 50`、
        # `HAVING SUM(v) * 2 > 100`。左右两侧都交给共用求值器
        # `parser.eval_expr_predicate`；列解析走 `_col_index_of`
        # （含 JOIN 结果的 `u.id` 限定名与末段唯一匹配），聚合值从
        # agg_values（HAVING 语境）里按规范文本取。
        # 任一侧是 NULL → UNKNOWN(None) → 行过滤时按「不通过」处理。
        pred = where.get('expr_pred')
        if isinstance(pred, dict):
            pos = 'HAVING' if agg_values is not None else 'WHERE'
            resolve = self._expr_resolver(cols, row, agg_values, pos)
            return _eval_expr_predicate(pred, resolve, agg_values, pos,
                                        self._stored_function_resolver(pos))

        op = where.get('op')
        if 'agg' in where:
            key = str(where.get('agg', '')).strip()
            # 分组时已经算好（normalize 过、位于 cols 里的聚合列也能按名取到）
            l = None
            if agg_values is not None and key in agg_values:
                l = agg_values[key]
            else:
                i = self._col_index_of(cols, key)
                if 0 <= i < len(row):
                    l = row[i]
            r = where.get('value')
            if l is None:
                return op == '=' and r is None
            if r is None:
                return op == '!='
            try:
                if isinstance(l, (int, float)) and isinstance(r, str):
                    r = float(r) if '.' in r else int(r)
            except (TypeError, ValueError):
                pass
            if op == '=':  return l == r
            if op == '!=': return l != r
            if op == '<':  return l < r
            if op == '>':  return l > r
            if op == '<=': return l <= r
            if op == '>=': return l >= r
            return False

        # ---------- IN / NOT IN（集合上下文）----------
        # 字面量列表 `c IN (1,2,NULL)`：按三值语义求值。
        # **原实现没有这个分支**，条件会掉进下面「r = where.get('value')
        # → None → return op == '!='」的普通比较逻辑；op 是 'IN' 于是恒返回
        # False —— 实测 `... GROUP BY uid HAVING uid IN (1,2)` 静默返回 0 个
        # 分组（WHERE 里的字面量列表在 `_cmd_select` / `_select_from_relation`
        # 已被 `_rewrite_in_lists` 改写成 and/or 链，因此只有 HAVING 位置
        # 暴露了这个静默错答）。
        # 子查询形态（`{'subquery': ...}`）不该走到这里：WHERE 由
        # `_rewrite_subqueries` / `_eval_outer_where` 处理，HAVING 由
        # `_eval_having_with_subquery` 处理。真走到这里说明路由有缺口，
        # 显式报错，绝不静默判假（否则会变成「查不到行」这种错误答案）。
        if op in ('IN', 'NOT IN'):
            if isinstance(where.get('subquery'), dict):
                raise ValueError(
                    "IN / NOT IN 的子查询谓词没有走子查询求值路径："
                    "该条件在此处无法安全求值，绝不静默判假")
            if 'values' not in where:
                raise ValueError(
                    "IN / NOT IN 谓词缺少值列表（也没带子查询）：无法求值")
            if not isinstance(where.get('column'), str) \
                    or not where.get('column'):
                raise ValueError(
                    "IN / NOT IN 的左值必须是单列：本引擎不支持行值"
                    "（多列）比较，无法安全推断出参与比较的列")
            ci = self._col_index_of(cols, where['column'])
            if ci < 0:
                # 末段歧义（JOIN 结果里 `u.id` / `o.id` 并存）→ 显式报错
                self._raise_if_ambiguous_col(cols, where['column'])
                if agg_values is not None:
                    raise ValueError(
                        f"HAVING 里的列 '{where['column']}' 在分组结果里"
                        f"取不到：GROUP BY 之后只保留分组键与聚合值，"
                        f"请把该列加入 GROUP BY / SELECT，或改用聚合表达式")
                return False
            lv = row[ci] if ci < len(row) else None
            # `IN (u.id)` 这类「右侧是列引用」的形态：带限定名的项按列取值
            # （原来是字面量集合 → 恒不命中 → 静默空集）；字面量 NULL 同样
            # 走三值语义（在 `_resolve_in_values` 里统一处理）。
            vals = self._resolve_in_values(
                where.get('values') or [], row, cols)
            return bool(self._in_predicate(lv, vals, neg=(op == 'NOT IN')))

        col = where.get('column')
        if not isinstance(col, str):
            return True
        i = self._col_index_of(cols, col)
        if i < 0:
            # 末段歧义（JOIN 结果里 `u.id` 与 `o.id` 并存时的裸 `id`）必须
            # 显式报错：静默返回"条件为假"会把整个结果集变成空集，用户看不出
            # 是「没有匹配行」还是「列名有歧义」。
            self._raise_if_ambiguous_col(cols, col)
            # HAVING 语境（agg_values is not None）取不到列时显式报错：
            # GROUP BY 之后行里只有分组键 + 聚合值，`HAVING <裸列> ...`
            # （例如 `SELECT id, COUNT(*) ... GROUP BY id HAVING city IN ('BJ')`）
            # 原来静默判假、整个分组被丢掉；正确做法是要求用户把该列放进
            # GROUP BY/SELECT 或改用聚合表达式。
            if agg_values is not None:
                raise ValueError(
                    f"HAVING 里的列 '{col}' 在分组结果里取不到："
                    f"GROUP BY 之后只保留分组键与聚合值，"
                    f"请把该列加入 GROUP BY / SELECT，或改用聚合表达式")
            return False
        l = row[i] if i < len(row) else None
        r = where.get('value')
        # ★ 右侧是**列引用**（`WHERE o.user_id = u.id`）：按列取值比较。
        # 原来把 `u.id` 当字符串字面量 → `1 == 'u.id'` 恒假 → 静默空集
        # （跨表 WHERE / JOIN ON 回退路径的主症状）。
        if where.get('value_column') and isinstance(r, str):
            j = self._col_index_of(cols, r)
            if j < 0:
                # 带限定符却解析不到 = 列名写错：显式报错，绝不静默判假
                if '.' in r.strip().strip('`"'):
                    self._raise_if_ambiguous_col(cols, r)
                    raise ValueError(f"列 '{r}' 不存在（WHERE 里的列引用）")
            else:
                r = row[j] if j < len(row) else None
        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        try:
            if isinstance(l, (int, float)) and isinstance(r, str):
                r = float(r) if '.' in r else int(r)
        except (TypeError, ValueError):
            pass
        if op == '=':  return l == r
        if op == '!=': return l != r
        if op == '<':  return l < r
        if op == '>':  return l > r
        if op == '<=': return l <= r
        if op == '>=': return l >= r
        if op == 'LIKE':
            pat = str(r).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(l)))
        return False

    @staticmethod
    def _raise_if_ambiguous_col(cols, name):
        """列名末段歧义时显式报错（`u.id` / `o.id` 并存时的裸 `id`）

        与计划执行器 `_resolve_col` 的「列 'x' 歧义，请加表名前缀」保持一致：
        多表连接的结果行必须用限定名引用同名列，绝不任取一侧。
        """
        want = str(name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        hits = [c for c in cols
                if isinstance(c, str)
                and str(c).strip().strip('`"').rsplit('.', 1)[-1].lower()
                == want]
        if len(hits) > 1:
            raise ValueError(f"列 '{name}' 歧义，请加表名前缀")

    # ==================== 标量表达式求值（统一路径） ====================

    def _expr_resolver(self, cols, row, agg_values=None, position=''):
        """构造表达式求值用的列解析回调 `resolve(name) -> 值`

        解析不到列时**显式报错**（绝不静默给 NULL）：

        * JOIN 结果的末段歧义沿用 `_raise_if_ambiguous_col` 的文案；
        * HAVING 语境（agg_values 不为 None，即分组之后）说明
          「GROUP BY 之后只保留分组键与聚合值」，避免报出令人费解的
          「列不存在」—— 那正是本次要消除的误导性错误信息。
        """
        def resolve(name):
            i = self._col_index_of(cols, name)
            if i < 0:
                self._raise_if_ambiguous_col(cols, name)
                if agg_values is not None:
                    raise ValueError(
                        f"{position} 里的列 '{name}' 在分组结果里取不到："
                        f"GROUP BY 之后只保留分组键与聚合值，请把该列加入"
                        f"GROUP BY / SELECT，或改用聚合表达式")
                raise ValueError(f"{position} 里的列 '{name}' 不存在")
            if i >= len(row):
                raise ValueError(
                    f"{position} 里的列 '{name}' 在当前行里取不到值")
            return row[i]
        return resolve

    def _eval_expr_in_row(self, node, cols, row, agg_values=None,
                          position=''):
        """在当前行上求值一个标量表达式节点（投影 / 排序键 / 分组键）"""
        resolve = self._expr_resolver(cols, row, agg_values, position)
        return _eval_expr_node(node, resolve, agg_values, position,
                               funcs=self._stored_function_resolver(position))

    # ==================== 存储函数调用（表达式里） ====================

    #: 存储函数最大嵌套调用深度（含间接递归）——超过即显式报错，
    #: 不会让进程栈溢出成 RecursionError。
    _FUNCTION_MAX_DEPTH = 32

    def _stored_function_resolver(self, position=''):
        """给 `parser.eval_expr_node` / `pl.eval_expr` 用的存储函数解析回调

        契约（三条求值路径共用）：

        * `resolver.has_function(名字) -> bool` —— 是不是已注册的存储函数；
        * `resolver(名字, [实参值]) -> 返回值` —— 真正调用；出错（参数个数
          不对、函数体出错、递归过深）**直接抛异常**。

        绝不返回任何"看起来像值"的替代对象：那种对象会被当成表达式结果
        写进结果集（实测就是「函数对象被静默当成返回值」这种静默错答）。
        """
        def resolve(fname, args):
            return self._call_stored_function(fname, args, position)
        resolve.has_function = self._has_stored_function
        return resolve

    def _has_stored_function(self, name):
        return self._lookup_stored_function(name) is not None

    def _lookup_stored_function(self, name):
        mgr = self._stored
        if mgr is None:
            return None
        try:
            return mgr.get_function(name)
        except Exception:                        # pragma: no cover
            return None

    def _call_stored_function(self, name, args, position='', depth=1):
        """在表达式里调用已注册的存储函数（返回值）

        嵌套调用（函数体里调用另一个函数）走同一条路径，共享一个深度计数
        `_FUNCTION_ACTIVE`：超过 `_FUNCTION_MAX_DEPTH` 显式报错。
        """
        fn = self._lookup_stored_function(name)
        if fn is None:
            raise RuntimeError(f"存储函数 '{name}' 不存在")
        params = fn.params or []
        n_args = len(args)
        n_par = len(params)
        if n_args != n_par:
            raise RuntimeError(
                f"函数 {name} 的参数个数不对：需要 {n_par} 个"
                f"（{', '.join(p.get('name', '') for p in params)}），"
                f"实际给了 {n_args} 个")
        active = getattr(self, '_FUNCTION_ACTIVE', None)
        if active is None:
            active = self._FUNCTION_ACTIVE = []
        # depth 参数只用于日志/兼容；真正的深度来自当前线程的调用栈
        # （active 列表）：嵌套调用（含间接递归）每次都重新走
        # `_stored_function_resolver`，靠参数层层传会丢失深度信息。
        if len(active) + 1 > self._FUNCTION_MAX_DEPTH:
            chain = ' -> '.join(active + [str(name)])
            raise RuntimeError(
                f"存储函数递归过深（超过 {self._FUNCTION_MAX_DEPTH} 层）："
                f"{chain}。请检查函数是否直接或间接调用了自己")
        bind_sql, bind_val = {}, {}
        for i, prm in enumerate(params):
            v = args[i]
            bind_val[str(prm['name'])] = v
            bind_sql[str(prm['name'])] = self._sql_literal(v)
        active.append(str(name))
        session = self._active_session if isinstance(
            getattr(self, '_active_session', None), dict) else {}
        try:
            return self._exec_function_body(fn, bind_sql, bind_val, session,
                                            depth)
        finally:
            active.pop()

    def _exec_function_body(self, fn, bind_sql, bind_val, session, depth):
        """执行函数体并返回 RETURN 的值

        与过程体**完全共用** `_pl_run_block`（同一个 PL 语句执行器）：因此
        函数体里 IF / WHILE / LOOP / REPEAT / LEAVE / ITERATE / 游标 /
        `SELECT ... INTO` / SIGNAL 的行为与过程体逐字一致 —— 这也是 MySQL
        的语义（13.1.12：存储函数只禁止改数据的语句，不禁止循环与游标）。
        区别只有两处：
        * 函数体的 RETURN 必须有值（无 RETURN 显式报错，不静默返回 NULL）；
        * 不允许改数据的 SQL（创建期已校验，执行期再兜一道，
          见 `_pl_sql_run(funcs_depth=…)`）。
        """
        body = self._substitute_words(fn.body, bind_sql)
        from .pl import PLParser, PLContext, ExprParser, eval_expr
        ctx = PLContext(self, session, self.db)
        ctx.function_name = getattr(fn, 'name', '?')
        for k, v in (bind_val or {}).items():
            ctx.variables[k] = v
            ctx.declared_vars.add(k)
        text = self._strip_begin_end(body)
        try:
            stmts = PLParser(text).parse_program()
        except SyntaxError as e:
            raise RuntimeError(f"函数体语法错误: {e}")
        if not stmts:
            raise RuntimeError("函数体为空（本引擎要求 RETURN <表达式>）")
        returned = self._pl_run_block(
            stmts, ctx, ExprParser, eval_expr, funcs_depth=depth + 1)
        if self._pl_signal_set(ctx):
            raise RuntimeError(
                f"函数体 SIGNAL SQLSTATE {ctx.signal_state}: "
                f"{ctx.signal_message}")
        if not ctx.return_hit:
            raise RuntimeError(
                "函数体执行结束都没有遇到 RETURN：存储函数必须有返回值，"
                "本引擎不会静默返回 NULL")
        return returned

    def _check_expr_functions(self, node, position=''):
        """静态校验表达式里的函数名（不支持的**显式报错并回列支持清单**）

        这是「不支持的函数不得静默返回 NULL / 不得被当成列名」的落点：
        `REVERSE(name)`、`NOW()` 这类名字在这里抛 ValueError，
        文案由 `parser.unsupported_function_message` 统一给出。

        **已注册的存储函数名**（`CREATE FUNCTION f(...)`）同样合法：名字
        对得上就放行，由 `_stored_function_resolver` 在求值时真正调用；
        注册表里没有的名字仍然按「不支持的函数」报错。
        """
        if not isinstance(node, dict):
            return
        kind = node.get('kind')
        if kind == 'func':
            name = str(node.get('name') or '').upper()
            if name not in _AGGREGATE_FUNCTION_NAMES \
                    and name not in _SCALAR_FUNCTION_NAMES \
                    and self._lookup_stored_function(node.get('name')) is None:
                raise ValueError(unsupported_function_message(name))
            for a in (node.get('args') or []):
                self._check_expr_functions(a, position)
        elif kind == 'arith':
            self._check_expr_functions(node.get('left'), position)
            self._check_expr_functions(node.get('right'), position)

    def _project_rows(self, p, base_cols, rows, session=None, read_view=None,
                      outer_name=None, outer_alias=None, extra_names=None):
        """把行按 SELECT 列表投影成输出列

        投影项支持：`*` / 普通列（含表限定名）/ 常量 / 二元算术表达式 /
        **标量子查询 `(SELECT ...)`**。

        标量子查询实现方式（与 WHERE 里的相关谓词共用同一套机制）：
        逐个外层行把子查询 AST 里的「外层限定名.列」折叠成该行的字面量
        （`_fold_outer_literals`），折叠后的子查询已不相关，交给正常执行
        路径求值（因此内层 JOIN / GROUP BY / HAVING / 嵌套子查询都能用）；
        结果按「折叠用到的外层列取值」缓存，相同外层行只算一次。非相关的
        标量子查询整体只求值一次，所有行共用结果。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名 —— 子查询里引用连接表的列也能被折叠。

        语义边界（全部显式处理，绝不静默填 NULL）：
        * 子查询 0 行 → 聚合补空集值（COUNT→0，SUM/AVG/MIN/MAX→NULL），
          非聚合 → NULL（SQL 标准语义）；
        * 子查询 >1 行 → 抛 ValueError（标量子查询返回了多行）；
        * 子查询 >1 列 → 抛 ValueError（标量子查询返回了多列）；
        * 未知列 / 无法求值的投影表达式 → 抛 ValueError，不再降级成 NULL
          （原实现用 `('col', -1)` 的哨兵把所有认不出来的投影项静默输出
          NULL，`SELECT u.uid FROM users u` 会返回一堆 None 而不是报错）。
        """
        projection = p.get('projection') or [
            ('col', c) for c in (p.get('columns') or ['*'])]
        displays = self._projection_displays(p, projection)

        if any(k == 'col' and str(t).strip() == '*'
               for k, t in projection):
            expanded = []
            exp_displays = []
            for i, (k, t) in enumerate(projection):
                if k == 'col' and str(t).strip() == '*':
                    expanded.extend(('col', c) for c in base_cols)
                    exp_displays.extend([None] * len(base_cols))
                else:
                    expanded.append((k, t))
                    exp_displays.append(displays[i])
            projection = expanded
            displays = exp_displays

        session = self._use_session(session)
        if outer_name is None:
            outer_name = p.get('table')
        if outer_alias is None:
            outer_alias = p.get('table_alias')
        sub_cache = {}

        out_cols, plan_items = [], []
        for i, (kind, text) in enumerate(projection):
            if kind == 'win':
                continue
            item = self._projection_item_plan(
                str(text), base_cols, displays[i])
            out_cols.append(item[2])
            plan_items.append(item)

        out_rows = []
        for r in rows:
            out = []
            for item in plan_items:
                out.append(self._eval_projection_item(
                    item, r, base_cols, session, read_view,
                    outer_name, outer_alias, sub_cache, extra_names))
            out_rows.append(out)
        return self._finish_select(
            p, out_cols, out_rows,
            sort_values=self._order_by_sort_values(
                p, out_cols, base_cols, rows))

    @staticmethod
    def _projection_displays(p, projection):
        """投影项的「MySQL 输出列名覆盖」表：`data->'$.k'` 显示**原文**

        解析期把 `data->'$.k'` 改写成等价的 `JSON_EXTRACT(data, '$.k')`
        （内部求值走这条路径），但 MySQL 的输出列名是用户写的原文。
        `p['json_display']`（parser `_select` 产出）是**与投影列表逐项对齐**
        的原文表，无覆盖处为 None；长度不一致时整体不覆盖（宁可沿用既有
        命名，也不按错位的位置改名）。带 `AS 别名` 的项在解析期就记为 None
        （列名是别名，与 MySQL 一致）。

        `SELECT *` 展开时由调用方同步展开本表（见 `_project_rows`）。
        """
        disp = list((p or {}).get('json_display') or [])
        if len(disp) != len(projection):
            return [None] * len(projection)
        return disp

    def _projection_item_plan(self, text, base_cols, display=None):
        """单个投影项 -> (kind, spec, 输出列名)

        display：MySQL 输出列名（仅 `data->'$.k'` 这类原文覆盖用，见
        `_projection_displays`）。无别名时优先用它，其次才是解析后的表达式文本。

        kind ∈ {'const','col','arith','expr','sub'}。认不出来的形态抛
        ValueError：投影是用户直接看得到的结果，宁可变报错也不能安静地
        输出 NULL。

        'expr' 是本次新增的**标量表达式**（函数调用 / 含函数的算术）：
        逐行用 `parser.eval_expr_node` 求值。函数名在规划期就校验，
        不支持的函数（`REVERSE(name)`）在这里显式报错并回列支持清单。
        """
        expr, alias = self._split_alias(str(text))
        sub, bad = self._projection_subquery_ast(expr)
        if bad:
            raise ValueError(f"投影表达式无法求值: {expr}（{bad}）")
        if sub is not None:
            # 输出列名保留子查询原文（不要做限定符归一化，否则列名会被改写）
            return ('sub', sub, alias or expr.strip())
        label = alias or display or self._strip_qualifier(expr)
        is_lit, val = self._literal_value(expr)
        if is_lit:
            # 字面量列（`SELECT 1 FROM orders`）只出现在常量投影里
            return ('const', val, label)
        i = self._col_index_of(base_cols, str(text))
        if i >= 0:
            return ('col', i, label)
        arith = self._parse_arith(expr)
        if arith is not None:
            # 简单算术表达式（`SELECT n + 1`）：递归 CTE 的迭代步长
            # 常见写法，原实现会当成未知列静默投影成 NULL
            for tok in (arith[0], arith[2]):
                if self._is_plain_literal(tok):
                    continue
                if self._col_index_of(base_cols, tok) < 0:
                    # 操作数解析不到时 `_eval_arith` 会返回 NULL：静默填 NULL，
                    # 显式报错更安全
                    raise ValueError(
                        f"列 '{tok}' 不存在（算术表达式 {expr} 的列操作数）")
            return ('arith', arith, label)
        # 标量函数 / 含函数的嵌套算术（`LOWER(name)`、`ROUND(AVG(v), 1)`
        # 作为**列**已经在上面命中，这里只处理真正要逐行求值的形态）：
        # 放在最后，保证既有形态（列 / 常量 / 二元算术 / 标量子查询）的
        # 解析结果逐字不变。
        node = _parse_scalar_expr(expr)
        if node is not None and _expr_has_func(node):
            self._check_expr_functions(node, '投影')
            if _expr_has_agg(node):
                raise ValueError(
                    f"聚合表达式 {expr} 在投影里没有可用的分组上下文："
                    f"请配合 GROUP BY 使用，或确认聚合列已被 SELECT 列表"
                    f"显式列出（本项目 GROUP BY 语义按 ONLY_FULL_GROUP_BY 收紧）")
            return ('expr', node, label)
        if node is not None:
            # 纯算术但没被 `_parse_arith` 认出来（三元以上嵌套，如
            # `salary * 2 + 10`）：同样交给表达式求值器
            return ('expr', node, label)
        raise ValueError(f"列 '{expr}' 不存在")

    def _eval_projection_item(self, item, row, base_cols, session, read_view,
                              outer_name, outer_alias, sub_cache,
                              extra_names=None):
        """求值一个投影项在当前行上的取值"""
        kind, spec, label = item
        if kind == 'const':
            return spec
        if kind == 'col':
            if 0 <= spec < len(row):
                return row[spec]
            raise ValueError(f"投影列 '{label}' 在当前行里取不到值")
        if kind == 'arith':
            return self._eval_arith(spec, base_cols, row)
        if kind == 'expr':
            return self._eval_expr_in_row(spec, base_cols, row, None, '投影')
        return self._eval_projection_scalar(
            spec, row, base_cols, session, read_view,
            outer_name, outer_alias, sub_cache, extra_names)

    # ---- 投影列表里的标量子查询 ----

    # 投影项以 `(SELECT` 开头即视为标量子查询（是否括号正好包住整段再校验）
    _SCALAR_SUB_LEAD = re.compile(r'^\(\s*SELECT\b', re.IGNORECASE)

    @classmethod
    def _projection_subquery_ast(cls, expr):
        """把投影项 `(SELECT ...)` 解析成内层 AST

        返回 (AST 或 None, 不支持的原因或 None)：
        * 不是以 `(SELECT` 开头 → (None, None)；
        * 括号正好包住整段且能解析成 SELECT → (AST, None)；
        * 以 `(SELECT` 开头但括号不匹配整段（`(SELECT 1) + 1`）或解析失败
          → (None, 原因文本)，由调用方显式报错。
        """
        t = str(expr or '').strip()
        if not cls._SCALAR_SUB_LEAD.match(t):
            return None, None
        end = cls._scan_matching_paren(t, 0)
        if end is None or t[end + 1:].strip():
            return None, "标量子查询必须独占一个投影项（不能参与运算）"
        try:
            from .parser import parser as _parser
            ast = _parser.parse(t[1:end].strip())
        except SyntaxError as exc:
            return None, f"标量子查询语法错误: {exc}"
        except Exception as exc:
            return None, f"标量子查询无法解析: {exc}"
        if not isinstance(ast, dict) or ast.get('command') != 'SELECT':
            return None, "标量子查询无法解析成 SELECT"
        return ast, None

    @staticmethod
    def _scan_matching_paren(text: str, start: int):
        """返回 text[start] == '(' 对应右括号的下标（跳过字符串字面量）"""
        depth = 0
        i = start
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "'":
                i += 1
                while i < n:
                    if text[i] == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            i += 2
                            continue
                        break
                    i += 1
            elif ch == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 1
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    @staticmethod
    def _scalar_static_width(sub):
        """标量子查询的静态列数（无法判断时返回 None，例如 `SELECT *`）"""
        proj = sub.get('projection')
        if not isinstance(proj, list) or not proj:
            proj = [('col', c) for c in (sub.get('columns') or [])]
        items = [t for k, t in proj if k != 'win']
        if any(str(t).strip() == '*' for t in items):
            return None
        return len(items)

    @staticmethod
    def _in_subquery_arity_error(pos_text, n):
        """IN / NOT IN 子查询多列时的报错文本（静态/动态判定共用同一份措辞）"""
        pos = str(pos_text or '').strip()
        head = f"{pos} " if pos else ""
        return (
            f"{head}IN / NOT IN 的子查询只能返回一列，"
            f"当前返回了 {n} 列："
            f"多列子查询的 IN 在 SQL 标准里是**行值（tuple）比较**，"
            f"本引擎不支持行值比较，因此显式报错"
            f"（绝不只取第一列参与比较，那会给出看似正确、实际错误的行）")

    @staticmethod
    def _guard_in_subquery_width(sub, pos_text="", res_cols=None, rows=None):
        """IN / NOT IN（集合上下文）的子查询列数守卫：多于 1 列一律报错

        背景：`c IN (SELECT a, b FROM t)` 在 SQL 标准里等价于行值比较
        `c IN ((a1,b1),(a2,b2),...)`，左值必须是行值；左值是单列时它本身
        就是语义错误。原实现在集合上下文里对每行 `r[0]` 取值，把
        `id IN (SELECT uid, amt FROM orders)` 静默算成
        `id IN (SELECT uid FROM orders)` —— 返回的是**看起来对**的行集，
        属于最典型的静默错答。标量上下文（`_scalar_from_rows`）上一轮已经
        收紧为「多列报错」，这里把集合上下文对齐到同一纪律。

        静态 + 动态双保险（缺一不可）：
        * **静态**：解析期就能数出投影项数（`_scalar_static_width`），
          因此**结果集为空**也拦得住（`IN (SELECT a, b ... WHERE ...)`
          命中 0 行时不能让多列悄悄通过）；
        * **动态**：拿到结果后再按实际列数（结果列名个数，其次首行长度）
          判一次，兜住静态判不出的形态（`SELECT *`）。

        pos_text：出错位置前缀（如 'WHERE 里的'），拼进报错信息。
        res_cols：结果集列名列表（`res['columns']`），可为 None。
        rows：结果行列表，可为 None。
        """
        static_w = Executor._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                Executor._in_subquery_arity_error(pos_text, static_w))
        if res_cols is not None:
            n = len(res_cols)
        elif rows:
            n = len(rows[0]) if rows[0] else 0
        else:
            n = 0
        if n > 1:
            raise ValueError(
                Executor._in_subquery_arity_error(pos_text, n))

    def _eval_projection_scalar(self, sub, row, cols, session, read_view,
                                outer_name, outer_alias, cache,
                                extra_names=None):
        """求值投影里的标量子查询（对当前外层行）

        相关子查询 → 折叠外层列后执行并按折叠值缓存；
        非相关子查询 → 整体执行一次并缓存。
        多行/多列/无法求值一律抛 ValueError（显式报错，不静默填 NULL）。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，
        连接进来的表名/别名同样可以折叠。
        """
        # 外层限定名被内层同名表遮蔽时无法无歧义解析：显式报错
        amb = self._ambiguous_outer_ref(
            {'subquery': sub}, outer_name, outer_alias)
        if amb:
            raise ValueError(self._ambiguous_outer_ref_message(
                amb, outer_name, outer_alias))

        static_w = self._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                f"标量子查询返回了多列（{static_w} 列）："
                f"投影里的标量子查询只能返回一列")

        correlated = bool(outer_name) and (
            self._is_correlated(sub, outer_name, outer_alias, extra_names)
            or self._sub_owner_is_outer(sub, outer_name, outer_alias,
                                        extra_names))

        if correlated:
            names = self._fold_outer_names(sub, outer_name, outer_alias,
                                           extra_names)
            if not names:
                raise ValueError(
                    "投影里的相关标量子查询无法求值：外层引用无法安全折叠"
                    "（请检查外层限定名是否被子查询里的同名表遮蔽）")
            key = ('psub', id(sub), self._fold_cache_key(
                sub, row, cols, outer_name, outer_alias, extra_names))
            if key not in cache:
                # `_fold_and_run` 现在返回 (列名, 行)；投影是标量上下文，
                # 列数语义仍统一由 `_scalar_from_rows` 收敛
                _res_cols, folded_rows = self._fold_and_run(
                    sub, row, cols, session, outer_name, outer_alias,
                    read_view, extra_names)
                cache[key] = folded_rows
            rows = cache[key]
        else:
            # 疑似非相关：确认里面没有「藏在表达式里的外层引用」，
            # 否则会被当成非相关子查询一次性求值后静默给出错值
            self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                           extra_names)
            key = ('psub-nc', id(sub))
            if key not in cache:
                res = self.execute(sub, session)
                if not isinstance(res, dict) or res.get('type') == 'error':
                    raise ValueError(
                        "标量子查询执行失败: "
                        f"{(res or {}).get('message', '未知错误')}")
                cache[key] = [tuple(r) for r in (res.get('rows') or [])]
            rows = cache[key]

        # 行数/列数语义统一交给 _scalar_from_rows（与 WHERE/HAVING 同一份
        # 实现）：0 行 → 空集聚合值/NULL、1 行 1 列 → 取值、
        # 多行/多列 → 显式报错。绝不再取第一行/第一列。
        return self._scalar_from_rows(sub, rows, '投影里的')

    def _scalar_from_rows(self, sub, rows, where_text=""):
        """把子查询的结果行按**标量语义**收敛成一个值（统一实现）

        WHERE / HAVING / 投影三个位置的标量上下文共用这一份语义：

        * 多于 1 行 → 抛 ValueError（显式报错，绝不取第一行）；
        * 0 行 → 空集聚合值（COUNT→0，SUM/AVG/MIN/MAX→NULL），
          非聚合投影 → NULL（SQL 标准语义，这是正确的 NULL）；
        * 1 行但多于 1 列 → 抛 ValueError（绝不取第一列）；
        * 1 行 1 列 → 返回该值。

        静态列数可判（`SELECT a, b` / `SELECT *` 之外的形态）时先报多列，
        这样即使某个外层行恰好匹配 0 行也能显式报出多列子查询。
        where_text 只用于把出错位置写进信息（例如 'WHERE/HAVING 里的'）。
        """
        pos = str(where_text or '')
        static_w = self._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多列（{static_w} 列）："
                f"标量上下文只允许返回 1 列")
        if len(rows) > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多行（{len(rows)} 行）："
                f"标量上下文最多只允许返回 1 行")
        if not rows or not rows[0]:
            return self._empty_agg_scalar(sub)
        if len(rows[0]) > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多列（{len(rows[0])} 列）："
                f"标量上下文只允许返回 1 列")
        return rows[0][0]

    # 简单的二元算术表达式：`col + 1` / `salary * 2` / `a - b` / `a / b`
    _ARITH_RE = re.compile(
        r'^\s*([\w.]+|-?\d+(?:\.\d+)?)\s*([+\-*/])\s*'
        r'([\w.]+|-?\d+(?:\.\d+)?)\s*$')

    @classmethod
    def _parse_arith(cls, text):
        """识别二元算术表达式，返回 (左操作数, 运算符, 右操作数)"""
        m = cls._ARITH_RE.match(str(text or ''))
        if not m:
            return None
        return (m.group(1), m.group(2), m.group(3))

    def _eval_arith(self, spec, cols, row):
        """求值二元算术表达式；操作数取不到或为 NULL 时返回 NULL"""
        left, op, right = spec

        def operand(tok):
            if re.match(r'^-?\d+(?:\.\d+)?$', str(tok)):
                return float(tok) if '.' in str(tok) else int(tok)
            i = self._col_index_of(cols, tok)
            if i < 0 or i >= len(row):
                return _UNSET
            return row[i]

        l, r = operand(left), operand(right)
        if l is _UNSET or r is _UNSET or l is None or r is None:
            return None
        try:
            if op == '+': return l + r
            if op == '-': return l - r
            if op == '*': return l * r
            if op == '/': return l / r if r != 0 else None
        except TypeError:
            return None
        return None

    @classmethod
    def _is_scalar_function_projection(cls, text) -> bool:
        """投影项是否是「标量函数调用」（形如 LOWER(name) / NOW()）

        只认函数名不是存储层支持的 5 个聚合函数的 `名字(` 开头形态；
        子查询项（`(SELECT ...)`）与 `COUNT(*) OVER ()` 都不会命中。
        """
        expr, _ = cls._split_alias(str(text))
        m = re.match(r'^(\w+)\s*\(', expr.strip())
        return bool(m) and m.group(1).upper() not in cls._AGG_FUNCS

    @staticmethod
    def _literal_value(text):
        """识别 SQL 字面量，返回 (是否字面量, 值)

        子查询里常见的 `SELECT 1 FROM orders` / `SELECT 'x' FROM t`
        原来会被当成列名，报 `列 1 不存在`，导致 EXISTS 子查询直接失败
        （错误信息是 err(1146)，外面看起来还像"表不存在"）。
        """
        t = str(text or '').strip()
        if not t:
            return False, None
        if re.match(r'^-?\d+$', t):
            return True, int(t)
        if re.match(r'^-?\d+\.\d+$', t):
            return True, float(t)
        if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
            return True, t[1:-1].replace("''", "'")
        if t.upper() in ('NULL',):
            return True, None
        return False, None

    def _select_from_relation(self, p, s, rel):
        """从 CTE 内存关系里查询"""
        where = p.get('where')
        if self._contains_in_list(where):
            p = dict(p)
            p['where'] = self._rewrite_in_lists(p['where'])
            where = p['where']
        if self._contains_subquery(where):
            # 外层限定名被内层同名表遮蔽 → 显式报错（原来静默返回空集）
            amb = self._ambiguous_outer_ref(
                where, rel.name, p.get('table_alias'))
            if amb:
                return self._err(1062, self._ambiguous_outer_ref_message(
                    amb, rel.name, p.get('table_alias')))
            if self._where_has_correlated(where, rel.name,
                                          p.get('table_alias')) \
                    or self._where_has_scalar_subquery(where):
                return self._select_correlated(
                    p, s, rel, self._rv_of(s), rel.name)
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, rel.name, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")
            return self._select_advanced(p, rel, where=where, session=s)
        return self._select_advanced(p, rel, session=s)

    def _select_no_from(self, p, s):
        """FROM-less SELECT（`SELECT 1 AS n`）

        递归 CTE 的 base 分支常写成 `SELECT 1 AS n`，解析器现在允许没有
        FROM；执行时用「一行的空关系」承接，常量投影直接算出结果行。
        """
        return self._select_advanced(p, _CTERelation('', [], [[]]), session=s)

    def _select_from_subquery(self, p, s, sub):
        """`FROM (子查询) 别名`：把子查询物化成内存关系再执行外层查询

        实现方式（方案 A，复用 CTE 机制）：

        1. 解析并执行内层子查询，拿到 (columns, rows)。内层可以含
           GROUP BY / 聚合 / 窗口函数 / 多层嵌套子查询（嵌套时内层又会
           递归走一遍本方法）；
        2. 用别名注册一个临时 `_CTERelation`（与 WITH 物化同一条路径，
           不落盘、不建真实表，因此子查询结果允许有重复值）；
        3. 外层查询的 table 指向该别名，交给 `_select_from_relation`
           —— WHERE / GROUP BY / HAVING / ORDER BY / LIMIT+OFFSET /
           限定列名（`e.salary`）都由这条已有路径处理，
           限定符会由 `_normalize_where` / `_strip_qualifier` 归一化；
        4. finally 里注销临时关系（若同名 CTE 已存在则还原），
           避免泄漏到下一次执行。

        已知限制：
        * `FROM (子查询) x JOIN t ON ...` 暂不支持（显式报错，不静默错答）；
        * 子查询里的 `ORDER BY` 只影响物化顺序，外层不写 ORDER BY 时
          依赖该顺序（本实现保持物化顺序，与 SQLite 行为一致）。
        """
        from .parser import parser as _parser

        if p.get('joins'):
            return self._err(1105, "FROM 子查询暂不支持再 JOIN")
        alias = str(sub.get('alias') or 'sub')
        try:
            inner = _parser.parse(str(sub.get('sql') or ''))
        except SyntaxError as e:
            return self._err(1064, f"FROM 子查询语法错误: {e}")
        if not isinstance(inner, dict):
            return self._err(
                1064, f"FROM 子查询无法解析: {str(sub.get('sql'))[:80]}")

        res = self.execute(inner, s)
        if not isinstance(res, dict) or res.get('type') == 'error':
            return res if isinstance(res, dict) else self._err(
                1105, "FROM 子查询执行失败")

        key = alias.lower()
        saved = self._cte_relations.get(key, _UNSET)
        self._cte_relations[key] = _CTERelation(
            alias, res.get('columns') or [], res.get('rows') or [])
        try:
            outer = dict(p)
            outer['table'] = alias
            outer['subquery'] = None
            return self._select_from_relation(
                outer, s, self._cte_relations[key])
        finally:
            if saved is _UNSET:
                self._cte_relations.pop(key, None)
            else:
                self._cte_relations[key] = saved

    def _rv_of(self, s):
        """取当前会话的读视图（没有事务就临时建一个）"""
        txn = s.get('txn')
        if txn:
            return txn['read_view']
        tid = self.db.mvcc.next_txn_id()
        return self.db.mvcc.create_readview(tid)

    # ==================== 字面量 IN 列表（三值逻辑） ====================

    @classmethod
    def _contains_in_list(cls, where) -> bool:
        """条件里是否含字面量 IN 列表（{'op':'IN','values':[...]}）"""
        if not isinstance(where, dict):
            return False
        if 'values' in where and where.get('op') in ('IN', 'NOT IN'):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._contains_in_list(c):
                    return True
        return False

    @classmethod
    def _rewrite_in_lists(cls, where):
        """把字面量 IN 列表改写成 and/or 比较链（带上 NULL 三值语义）

        * `c IN (v1, v2, NULL)`：NULL 匹配不了任何值（连 NULL 左值也不行，
          SQL 里 `NULL IN (...)` 是 UNKNOWN），所以直接丢掉 NULL 项；
        * `c NOT IN (..., NULL)`：SQL 标准下整体为 UNKNOWN → 恒不通过；
        * 空列表：IN 恒假、NOT IN 恒真（保持既有行为）；
        * `c IN (u.id)`（候选项是**列引用**，解析期记在 `value_columns` 里）：
          展开出的比较必须带上 `value_column` 标记，否则会退化成与字符串
          `'u.id'` 比较 → 恒不命中 → 静默空集。
        """
        if not isinstance(where, dict):
            return where
        if 'and' in where:
            return {'and': [cls._rewrite_in_lists(c) for c in where['and']]}
        if 'or' in where:
            return {'or': [cls._rewrite_in_lists(c) for c in where['or']]}
        if 'values' not in where or where.get('op') not in ('IN', 'NOT IN'):
            return where

        col = where.get('column')
        neg = where.get('op') == 'NOT IN'
        raw = list(where.get('values') or [])
        has_null = any(v is None or str(v).upper() == 'NULL' for v in raw)
        # (原始下标, 去掉引号的文本)；NULL 项在 SQL 语义下不参与匹配
        kept = [(idx, v) for idx, v in enumerate(raw)
                if not (v is None or str(v).upper() == 'NULL')]
        # ★ 解析期标出的「列引用项」（`IN (u.id)`）：展开成比较时必须把
        # `value_column` 标记带过去，否则展开后的 `= 'u.id'` 会被当字符串
        # 字面量比较 → 恒不命中 → 静默空集（本次修复的同一类吞错）。
        col_refs = where.get('value_columns') or {}

        def mk(idx, v, op):
            node = {'column': col, 'op': op, 'value': v}
            if idx in col_refs:
                node['value'] = col_refs[idx]
                node['value_column'] = True
            return node

        if neg:
            if has_null:
                return cls._ALWAYS_FALSE
            if not kept:
                return None
            if len(kept) == 1:
                return mk(kept[0][0], kept[0][1], '!=')
            return {'and': [mk(i, v, '!=') for i, v in kept]}
        if not kept:
            return cls._ALWAYS_FALSE
        if len(kept) == 1:
            return mk(kept[0][0], kept[0][1], '=')
        return {'or': [mk(i, v, '=') for i, v in kept]}

    # ==================== 相关子查询（逐外层行求值） ====================

    def _where_has_correlated(self, where, outer_table, outer_alias) -> bool:
        """WHERE 里是否存在「引用了外层列」的子查询条件"""
        if not isinstance(where, dict):
            return False
        sub = where.get('subquery')
        if isinstance(sub, dict) and self._is_correlated(
                sub, outer_table, outer_alias):
            return True
        inner = where.get('not')
        if isinstance(inner, dict) and self._where_has_correlated(
                inner, outer_table, outer_alias):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if self._where_has_correlated(c, outer_table, outer_alias):
                    return True
        return False

    @classmethod
    def _where_has_scalar_subquery(cls, where) -> bool:
        """条件里是否存在「标量子查询比较」节点（`(SELECT ...) <op> v`）

        与 EXISTS / IN 不同，这类谓词没有一次性求值的语义：每个外层行
        都要把子查询结果按行数/列数收敛（0 行 → NULL、1 行 1 列 → 取值、
        多行或多列 → 显式报错），因此必须走 `_select_correlated` 的
        逐行求值路径（`_eval_outer_where` → `_eval_correlated_predicate`
        / `_eval_scalar_subquery`）。漏判会让条件走一次性改写路径，
        原实现会把 subquery 静默丢掉。
        """
        if not isinstance(where, dict):
            return False
        if 'subquery' in where and where.get('op') not in (
                'EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
            return True
        inner = where.get('not')
        if isinstance(inner, dict) and cls._where_has_scalar_subquery(inner):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_scalar_subquery(c):
                    return True
        return False

    def _is_correlated(self, sub, outer_table, outer_alias=None,
                       extra_names=None) -> bool:
        """判断子查询是否相关（引用了外层表的列）

        判据：
        1. 列名限定符等于外层表名/别名，且不是子查询自己的表/别名；
        2. WHERE 里的裸列名在子查询表里不存在、却在外层表里存在。
        `SELECT 1 FROM orders` 这类自洽子查询一律放行。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名同样是外层限定符 —— 漏掉会让「引用连接表」的子查询被误判成
        非相关、一次性求值（实测会静默返回空集/0 行）。
        """
        if not outer_table or not isinstance(sub, dict):
            return False
        outers = {str(outer_table).lower()}
        if outer_alias:
            outers.add(str(outer_alias).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())

        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())

        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            if owner and owner in outers and owner not in inner_names:
                return True

        # 本层 WHERE 里的**嵌套子查询**也可能引用外层列。`inner_names`
        # 已经包含本层与所有嵌套层自己的表/别名（见方法开头的收集），
        # 因此这里只判"限定符等于外层表名/别名、又不在这个集合里"。
        inner_nested = self._nested_subqueries(sub.get('where'))
        scope = set(inner_names)
        for nested in inner_nested:
            for key in ('table', 'table_alias'):
                if nested.get(key):
                    scope.add(str(nested[key]).lower())
            for j in (nested.get('joins') or []):
                for key in ('table', 'alias'):
                    if j.get(key):
                        scope.add(str(j[key]).lower())
        for nested in inner_nested:
            for c in self._nested_direct_columns(nested):
                owner = self._qualified_owner(c)
                if owner and owner in outers and owner not in scope:
                    return True

        inner = sub.get('table')
        inner_cols = set()
        if inner:
            try:
                inner_tbl = self.db.get_table(inner)
                inner_cols = {col['name'].lower() for col in inner_tbl.cols}
            except KeyError:
                rel = self._cte_relations.get(str(inner).lower())
                if rel is not None:
                    inner_cols = {c.lower() for c in rel.col_names}
        outer_cols = set()
        try:
            outer_tbl = self.db.get_table(outer_table)
            outer_cols = {col['name'].lower() for col in outer_tbl.cols}
        except KeyError:
            rel = self._cte_relations.get(str(outer_table).lower())
            if rel is not None:
                outer_cols = {c.lower() for c in rel.col_names}

        if not inner_cols or not outer_cols:
            return False
        for c in self._referenced_columns(sub.get('where')):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                return True

        for c in self._nested_direct_columns(sub, local_only=True):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                return True
        return False

    def _nested_direct_columns(self, sub, local_only=False) -> list:
        """收集 `sub` 本层 WHERE 直接引用的列 + 其嵌套子查询里的列引用

        只用于相关子查询判定：递归进嵌套子查询是必要的（嵌套层也可能
        引用最外层列），但判定时要配合 *_scope() 排除嵌套层自己的表/别名。
        local_only=True 时只看本层 WHERE 直接引用的列，不再往嵌套层递归
        —— 裸列名判据必须这样做：嵌套层的裸列属于嵌套层自己的表，
        拿它去比对本层表的列名会产生假相关（实测
        `... FROM orders WHERE user_id IN (SELECT id FROM users ...)` 里的
        `id` 会被误判成外层 users.id）。
        """
        out = list(self._referenced_columns(sub.get('where')))
        if local_only:
            return out
        for nested in self._nested_subqueries(sub.get('where')):
            out.extend(self._subquery_ast_columns(nested))
        return out

    def _nested_subqueries(self, where, out=None) -> list:
        """收集条件树里出现的所有嵌套子查询 AST（不含自身）"""
        if out is None:
            out = []
        if not isinstance(where, dict):
            return out
        sub = where.get('subquery')
        if isinstance(sub, dict):
            out.append(sub)
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                self._nested_subqueries(c, out)
        inner = where.get('not')
        if isinstance(inner, dict):
            self._nested_subqueries(inner, out)
        return out

    def _select_correlated(self, p, s, src, read_view, outer_name,
                           extra_names=None):
        """相关子查询的执行路径

        不能像非相关子查询那样「一次求值 + 改写条件」，因为每个外层行的
        真值都不同。这里先一次性取出「全部列的行」（只扫一遍表），再逐行
        用作用域求值器判断条件；子查询的内层行也按子查询缓存，因此单表
        相关子查询不会每行重新打开表。

        extra_names：外层行由多表连接而来时（JOIN 后置处理路径）传入的连接
        表名/别名列表；非 None 同时表示「多表行」，此时普通比较的列名保留
        限定前缀、按连接结果的列名精确解析。
        """
        outer_alias = p.get('table_alias')
        base_cols, rows = self._base_rows_and_cols(src, None, read_view)
        cache = {}
        kept = []
        for r in rows:
            if self._eval_outer_where(r, base_cols, p.get('where'), s,
                                      outer_name, outer_alias, cache,
                                      read_view, extra_names):
                kept.append(r)
        rows = kept

        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(
                p, src, base_cols, rows, session=s, read_view=read_view,
                extra_names=extra_names)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows, session=s,
                                       read_view=read_view,
                                       extra_names=extra_names)
        return self._project_rows(p, base_cols, rows, session=s,
                                  read_view=read_view,
                                  outer_name=outer_name,
                                  outer_alias=outer_alias,
                                  extra_names=extra_names)

    def _eval_outer_where(self, row, cols, where, s, outer_name,
                          outer_alias, cache, read_view, extra_names=None):
        """外层行上的条件求值（含相关/非相关子查询谓词）

        返回值：True / False / **None（三值逻辑的 UNKNOWN）**。
        UNKNOWN 在调用方（`_select_correlated` 的 `if`、and/or 的 all/any）
        里按「不通过」处理，与 SQL 的行过滤语义一致；只有 `NOT` 需要显式
        区分：`NOT UNKNOWN` 仍是 UNKNOWN，**不能**变成真。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名列表 —— 相关子查询要能折叠这些表的列。该参数同时充当
        「多表行」标志：普通比较的列名不做限定符归一化（见 `_normalize_where`）。
        """
        if where is None or not isinstance(where, dict):
            return True
        # NOT (...)：整体取反（可与 AND/OR 组合、可嵌套）。
        # 三值逻辑：NOT UNKNOWN 仍是 UNKNOWN（该行不通过），不能取反成真。
        if 'not' in where and isinstance(where['not'], dict):
            inner = self._eval_outer_where(
                row, cols, where['not'], s, outer_name, outer_alias,
                cache, read_view, extra_names)
            if inner is None:
                return None
            return not inner
        if 'and' in where:
            return all(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view,
                extra_names)
                for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view,
                extra_names)
                for c in parts)

        sub = where.get('subquery')
        op = where.get('op')
        if isinstance(sub, dict):
            # 相关性判定要"宽进"：`_is_correlated` 覆盖裸列/限定列两种判据，
            # `_sub_owner_is_outer` 兜底"子查询里带 JOIN"等漏判形态。
            # 任一命中就走逐外层行求值（外层列常量折叠）路径；漏判会走去
            # 重/标量分支 → 静默给出错误结果（实测 EXISTS 恒为空）。
            correlated = (self._is_correlated(sub, outer_name, outer_alias,
                                              extra_names)
                          or self._sub_owner_is_outer(
                              sub, outer_name, outer_alias, extra_names))
            if correlated:
                return self._eval_correlated_predicate(
                    row, cols, where, s, outer_name, outer_alias,
                    cache, read_view, extra_names)
            # 疑似非相关：再确认一次里面没有「藏在表达式里的外层引用」
            # （`x.uid = MIN(u.id)` 这类识别不到的相关引用），命中即报错，
            # 绝不当成非相关子查询一次性求值后静默返回 0 行。
            self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                           extra_names)
            if op not in ('EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
                # 非相关标量子查询：可以一次性求值（结果按子查询缓存）
                sv = self._eval_scalar_subquery(sub, s, cache)
                rv = where.get('value')
                if self._is_column_ref(rv):
                    rv = self._scoped_value(rv, row, cols, None)
                    if rv is _MISSING:
                        return False
                if sv is None and rv is not None:
                    # 标量是 NULL（0 行 → NULL）→ 比较结果是 UNKNOWN，
                    # 不是 False：AND/OR 里表现相同，但 NOT 下必须保持
                    # UNKNOWN（不能变成真）。见 `_eval_outer_where` 的
                    # NOT 分支。
                    return None
                return self._compare_scoped(sv, op, rv)
            return self._eval_plain_subquery_predicate(
                row, cols, where, s, cache, outer_name, outer_alias,
                extra_names)
        # 普通比较 / LIKE / IS NULL（编码成 value=None 的 =/!=）/ 聚合谓词
        return self._eval_plain_condition(
            row, cols, self._normalize_where(where, strip=not extra_names))

    def _eval_plain_condition(self, row, cols, where):
        """不含子查询的普通条件求值（三值语义，含 `negate` 标记）

        `_normalize_where` 对 LIKE 这类存储层没有取反运算符的形态会打上
        `{'negate': ...}` 标记。`_eval_condition` 不认识它，遇到会顺着
        「column 不是字符串 → return True」的分支把条件**静默丢掉**
        （实测 `NOT (name LIKE 'A%')` 被判成恒真）。这里把 negate 标记按
        三值语义就地展开后再交给 `_eval_condition`。
        """
        if isinstance(where, dict):
            if isinstance(where.get('negate'), dict):
                inner = self._eval_plain_condition(row, cols, where['negate'])
                return None if inner is None else (not inner)
            if 'and' in where:
                return all(self._eval_plain_condition(row, cols, c)
                           for c in where['and'])
            if 'or' in where:
                parts = where['or']
                if not parts:
                    return False
                return any(self._eval_plain_condition(row, cols, c)
                           for c in parts)
        return self._eval_condition(row, cols, where)

    def _eval_plain_subquery_predicate(self, row, cols, where, s, cache,
                                       outer_name=None, outer_alias=None,
                                       extra_names=None):
        """相关 WHERE 里混进来的非相关子查询（结果按子查询缓存）"""
        sub = where['subquery']
        op = where['op']
        self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                       extra_names)
        key = ('sub', id(sub))
        if key not in cache:
            res = self.execute(sub, s)
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise ValueError(
                    f"子查询执行失败: {(res or {}).get('message', '')}")
            cache[key] = (list(res.get('columns') or []),
                          [tuple(r) for r in (res.get('rows') or [])])
        res_cols, rows = cache[key]

        if op == 'EXISTS':
            return len(rows) > 0
        if op == 'NOT EXISTS':
            return len(rows) == 0
        # IN / NOT IN 是**集合语义**（允许多行），但只允许**一列**：
        # 多列子查询在 SQL 标准里是行值（tuple）比较，本引擎不支持，
        # 因此显式报错 —— 原实现对每行取 r[0]，把
        # `id IN (SELECT uid, amt ...)` 静默算成 `id IN (SELECT uid ...)`，
        # 返回看似正确、实际错误的行集（本会话消灭的静默错答）。
        # 静态列数（解析期）+ 实际列数（结果列名/首行长度）双判定，
        # 空结果集也拦得住，见 `_guard_in_subquery_width`。
        self._guard_in_subquery_width(
            sub, 'WHERE/HAVING 里的', res_cols=res_cols, rows=rows)
        if not isinstance(where.get('column'), str) or not where.get('column'):
            raise ValueError(
                "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                "（多列）比较，无法安全推断出参与比较的列")
        vals = [r[0] for r in rows if r]
        lv = self._scoped_value(where.get('column'), row, cols, None)
        return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

    def _eval_correlated_predicate(self, row, cols, where, s, outer_name,
                                   outer_alias, cache, read_view,
                                   extra_names=None):
        """相关子查询谓词求值：外层列常量折叠 + 复用正常执行路径

        实现方式（通用做法，能直接复用 JOIN / GROUP BY / HAVING / 窗口 /
        嵌套子查询等全部既有能力）：

        1. 把子查询 AST 里出现的「外层别名.列」全部替换成该外层行的
           字面量（WHERE / HAVING / SELECT 列表 / JOIN ON 都要处理）；
        2. 折叠后的子查询已经**不相关**，交给 `self.execute` 正常执行；
        3. 按折叠后的结果判定谓词真值：
           * EXISTS  → 行数 > 0
           * NOT EXISTS → 行数 == 0
           * IN / NOT IN → 取投影第一列做三值语义判定
           * 标量比较（`(SELECT COUNT(*)...) > 1`）→ 取第一行第一列比较

        缓存：同一子查询对象按「折叠用的外层列取值元组」缓存结果，
        因此相同外层行只算一次、取值相同的外层行共享结果，
        20 行规模也是瞬时完成。

        无法安全折叠的形态（子查询 JOIN ON 里引用外层列、折叠后仍残留
        外层引用、FROM 子查询）显式报错，不静默错答。
        """
        sub = where['subquery']
        op = where['op']
        cache_key = ('corr', id(sub), self._fold_cache_key(
            sub, row, cols, outer_name, outer_alias, extra_names))
        if cache_key in cache:
            res_cols, rows = cache[cache_key]
        else:
            res_cols, rows = self._fold_and_run(
                sub, row, cols, s, outer_name, outer_alias, read_view,
                extra_names)
            cache[cache_key] = (res_cols, rows)

        if op == 'EXISTS':
            return len(rows) > 0
        if op == 'NOT EXISTS':
            return len(rows) == 0

        if op in ('IN', 'NOT IN'):
            # IN / NOT IN（子查询形态）：集合语义，允许多行，但只允许
            # **一列**。多列在 SQL 标准里是行值比较，本引擎不支持 →
            # 显式报错。原实现取 r[0]，把多列子查询静默截断成第一列
            # （`id IN (SELECT uid, amt ...)` 算成 `id IN (SELECT uid ...)`），
            # 是典型静默错答，这里按 `_guard_in_subquery_width` 收紧
            # （静态列数 + 实际列数双判定，空结果集同样报错）。
            self._guard_in_subquery_width(
                sub, 'WHERE/HAVING 里的', res_cols=res_cols, rows=rows)
            if not isinstance(where.get('column'), str) \
                    or not where.get('column'):
                raise ValueError(
                    "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                    "（多列）比较，无法安全推断出参与比较的列")
            vals = [r[0] for r in rows if r]
            lv = self._scoped_value(where.get('column'), row, cols, None)
            return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

        # 标量比较：按标量行数/列数语义收敛后再比较（统一走
        # _scalar_from_rows）。注意空结果：SQL 里 `(SELECT COUNT(*) ...)`
        # 即使没有匹配行也返回 0（聚合作用于空集），而 SUM/AVG/MIN/MAX
        # 返回 NULL，因此空集要按投影的聚合函数名补出正确的空集值，
        # 否则 `(SELECT COUNT(*) ...) = 0` 会把 0 误判成 NULL 而漏掉整行。
        # 原实现无条件取 `rows[0][0]`：多行静默取第一行、多列静默取第一列，
        # 属于会给出错误结果的静默降级（WHERE 位置的主验收点）。
        sv = self._scalar_from_rows(sub, rows, 'WHERE/HAVING 里的')
        rv = where.get('value')
        if self._is_column_ref(rv):
            rv = self._scoped_value(rv, row, cols, None)
            if rv is _MISSING:
                return False
        elif sv is not None and not isinstance(rv, type(sv)):
            try:
                if isinstance(sv, (int, float)) and isinstance(rv, str):
                    rv = float(rv) if '.' in rv else int(rv)
            except (TypeError, ValueError):
                pass
        if sv is None and rv is not None:
            # 标量是 NULL（0 行 → NULL，或空集上的 SUM/AVG/MIN/MAX）
            # → 比较结果是三值逻辑的 UNKNOWN（该行不通过），但**不是 False**：
            # NOT 下必须保持 UNKNOWN（见 `_eval_outer_where` 的 NOT 分支）。
            return None
        return self._compare_scoped(sv, op, rv)

    @staticmethod
    def _empty_agg_scalar(sub):
        """空结果集上的标量子查询取值（聚合作用于空集的 SQL 语义）

        COUNT → 0，其它聚合（SUM/AVG/MIN/MAX）→ NULL；
        投影不是聚合函数时也返回 NULL。

        已知偏差（本次刻意不改，仅显式记录）：本引擎对空集不产出任何行，
        因此「0 行」有两种来源——① 无 GROUP BY 的裸聚合作用于空集
        （SQL 定义为 COUNT=0，这个补值是正确的）；② 带 GROUP BY/HAVING
        且没有任何分组存活（SQL 标准下整个标量子查询是 NULL，即使投影是
        COUNT）。这里对两种情况一律按「COUNT → 0」补值，第二种情况下
        `(SELECT COUNT(*) ... GROUP BY g HAVING ...) = 0` 会命中、
        而 PostgreSQL/SQLite/MySQL 下应为 NULL（UNKNOWN）不命中。
        收紧这一点会同时改变**投影位置**已锁定的语义
        （tests/test_v4_sql_features.py 中 `... GROUP BY o.user_id
        HAVING COUNT(*) >= 1) AS g` 对无订单用户的期望值 0），
        属于上一轮已决策的范围，因此留待统一决策；WHERE/HAVING 位置
        与投影位置保持一致，不单独偏离。
        """
        cols = sub.get('columns') or []
        expr = str(cols[0]).strip() if cols else ''
        m = re.match(r'^(\w+)\s*\(', expr)
        if m and m.group(1).upper() == 'COUNT':
            return 0
        return None

    def _eval_scalar_subquery(self, sub, s, cache):
        """非相关标量子查询求值（结果按子查询缓存）

        与相关路径共用 `_scalar_from_rows` 的行数/列数语义：
        0 行 → 空集聚合值（COUNT→0，其余→NULL）、1 行 1 列 → 取值、
        多行/多列 → 显式报错。原实现是
        `cache[key] = rows[0][0] if (rows and rows[0]) else None`
        ——多行静默取第一行、多列静默取第一列、空集上的 COUNT 还会
        错成 NULL，三重静默错答。
        """
        key = ('scalar', id(sub))
        if key not in cache:
            res = self.execute(sub, s)
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise ValueError(
                    f"子查询执行失败: {(res or {}).get('message', '')}")
            rows = [tuple(r) for r in (res.get('rows') or [])]
            cache[key] = self._scalar_from_rows(sub, rows, 'WHERE/HAVING 里的')
        return cache[key]

    # ---- 外层列常量折叠 ----

    @staticmethod
    def _is_column_ref(text) -> bool:
        """文本是否可能是列引用（`u.id` / `id`；排除数字与字符串字面量）"""
        t = str(text or '').strip().strip('`"')
        if not t or not re.match(r'^[\w.]+$', t):
            return False
        if re.match(r'^-?\d+(?:\.\d+)?$', t):
            return False
        return True

    @classmethod
    def _render_literal(cls, value) -> str:
        """把 Python 值渲染成 SQL 字面量文本（供折叠后的 SQL 文本重新解析）"""
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(value)
        return "'" + str(value).replace("'", "''") + "'"

    @classmethod
    def _ast_literal(cls, value):
        """把 Python 值折叠进 AST 的 value 字段（不是 SQL 文本！）

        解析器存进 AST 的比较右侧是**去掉引号**的原始值
        （`name = 'BJ'` → value='BJ'、`n = 1` → value='1'），折叠必须与之一致。
        原来这里一律用 `_render_literal`（SQL 文本），字符串会被带上引号，
        折叠后的条件变成 `u2.city = "'BJ'"`（value 里含引号）→ 永远匹配不上，
        实测 `WHERE u2.city = u.city` 静默返回 0 行。数值本来就是文本形态，
        保持 repr()，NULL 保持 'NULL'（与解析器/求值器的 NULL 编码一致）。
        """
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(value)
        return value

    def _outer_column_value(self, cols, row, name):
        """在外层行里取 `name` 的值；取不到返回 _MISSING

        先按精确名 / 去限定符名找列；都找不到时再按「列名末段」匹配，
        因为外层列名本身可能带限定符（`GROUP BY u.city` 之后外层列名是
        `u.city`，而子查询里引用的是 `u.city` 或裸 `city` 两种写法）。
        匹配不上仍返回 _MISSING，由调用方显式报错（不静默当 NULL）。
        """
        if not isinstance(name, str) or not name.strip():
            return _MISSING
        t = name.strip().strip('`"')
        cands = [t]
        if '.' in t:
            cands.append(t.rsplit('.', 1)[1])
        for cand in cands:
            i = self._col_index_of(cols, cand)
            if 0 <= i < len(row):
                return row[i]
        want = t.rsplit('.', 1)[-1].lower()
        for i, c in enumerate(cols):
            if not isinstance(c, str):
                continue
            if c.strip().strip('`"').rsplit('.', 1)[-1].lower() == want:
                if 0 <= i < len(row):
                    return row[i]
        return _MISSING

    def _sub_owner_is_outer(self, sub, outer_name, outer_alias,
                            extra_names=None) -> bool:
        """粗判：子查询 AST 里是否有「外层限定名」引用

        比 `_is_correlated` 宽松（不做裸列名/表列比对），用于兜底识别
        `_is_correlated` 可能漏掉的相关引用（例如子查询里带了 JOIN）。
        必须排除**子查询及其嵌套子查询自己的表/别名**：自洽子查询里出现
        与外层同名的表（`... IN (SELECT ... FROM users WHERE city='BJ')`
        而外层也叫 users）否则会被误判成相关引用。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，
        连接进来的表名/别名同样是可引用的外层限定符。
        """
        outers = set()
        for n in (outer_name, outer_alias):
            if n:
                outers.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())
        if not outers:
            return False
        inner = self._sub_scope_names(sub)
        if inner & outers:
            outers -= inner
            if not outers:
                return False
        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            if owner and owner in outers:
                return True
        return False

    def _sub_scope_names(self, sub) -> set:
        """子查询（含其 WHERE 里的嵌套子查询）自己声明的表名/别名集合"""
        names = set()
        if not isinstance(sub, dict):
            return names
        for key in ('table', 'table_alias'):
            if sub.get(key):
                names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    names.add(str(j[key]).lower())
        for nested in self._nested_subqueries(sub.get('where')):
            for key in ('table', 'table_alias'):
                if nested.get(key):
                    names.add(str(nested[key]).lower())
            for j in (nested.get('joins') or []):
                for key in ('table', 'alias'):
                    if j.get(key):
                        names.add(str(j[key]).lower())
        return names

    # ---- 外层引用被内层同名表遮蔽（无法无歧义解析） ----

    @staticmethod
    def _sub_binding_names(sub):
        """子查询里的 FROM 项 -> (可见引用名集合, 表名集合)

        * 可见名：写了别名就用别名（SQL 里别名会遮蔽表名），否则用表名；
        * 表名集合：FROM/JOIN 项实际指向的表名（FROM 子查询记其别名）。

        两者的差集就是「被别名遮蔽掉的表名」：这种名字在子查询里
        既可能是外层引用、又对应一张内层表，引擎无法无歧义解析。
        """
        visible, tables = set(), set()
        if not isinstance(sub, dict):
            return visible, tables
        items = [(sub.get('table'), sub.get('table_alias'))]
        for j in (sub.get('joins') or []):
            items.append((j.get('table'), j.get('alias')))
        for t, a in items:
            if t:
                tables.add(str(t).lower())
            if a:
                visible.add(str(a).lower())
            elif t:
                visible.add(str(t).lower())
        return visible, tables

    def _all_nested_subqueries(self, where, out=None) -> list:
        """条件树里所有（含嵌套层）的子查询 AST（按出现顺序去重）"""
        if out is None:
            out = []
        for sub in self._nested_subqueries(where):
            if any(s is sub for s in out):
                continue
            out.append(sub)
            for key in ('where', 'having'):
                if isinstance(sub.get(key), dict):
                    self._all_nested_subqueries(sub[key], out)
        return out

    def _ambiguous_outer_ref(self, where, outer_table, outer_alias=None):
        """找出「外层限定名被内层同名表遮蔽」的引用，返回限定符；无则 None

        典型形态（实测原来静默返回空集）：

            SELECT id FROM users WHERE EXISTS (
              SELECT 1 FROM orders o JOIN users u2 ON o.user_id = u2.id
              WHERE o.user_id = users.id)

        外层没起别名时，`users.id` 既可能是外层 users、又对应子查询里
        被别名 u2 遮蔽的那张 users——引擎的作用域判定（`_is_correlated` /
        `_sub_scope_names`）会把它算成**内层**引用，于是整个子查询被当成
        非相关、一次性求值成空集，静默返回 0 行。这里显式报错，要求给
        外层表起别名（起别名后名字不再冲突，正常用法不受影响）。
        """
        outers = {str(n).lower() for n in (outer_table, outer_alias) if n}
        if not isinstance(where, dict) or not outers:
            return None
        for sub in self._all_nested_subqueries(where):
            if not isinstance(sub, dict):
                continue
            visible, tables = self._sub_binding_names(sub)
            hidden = tables - visible          # 被别名遮蔽掉的表名
            conflict = outers & hidden
            if not conflict:
                continue
            for c in self._subquery_ast_columns(sub):
                owner = self._qualified_owner(c)
                if owner and owner in conflict:
                    return owner
        return None

    @staticmethod
    def _ambiguous_outer_ref_message(owner, outer_table, outer_alias=None):
        """外层引用无法无歧义解析时的报错信息"""
        return (
            f"子查询里的 '{owner}.' 无法无歧义解析：外层表 "
            f"'{outer_table}' 与子查询里被别名遮蔽的同名表 '{owner}' 冲突。"
            f"请给外层表起别名（例如 FROM {outer_table} {owner[:1]}x），"
            f"并在子查询里用该别名引用外层列")

    def _fold_outer_literals(self, sub, row, cols, outer_names,
                             depth=0):
        """把子查询 AST 里的外层列引用替换成外层行的字面量

        outer_names：可以安全折叠的限定符集合（来自 _fold_outer_names）。
        返回折叠后的新 AST（不改原对象，避免污染缓存）。已知限制：
        * 子查询 JOIN ON 里引用外层列 → 抛 ValueError（连接键折叠不安全）；
        * 嵌套子查询里的外层引用 → 抛 ValueError（本实现只折叠一层）。
        """
        if depth > 6:
            raise ValueError("相关子查询嵌套过深（>6 层）")
        if not isinstance(sub, dict):
            return sub

        def fold_value(v):
            """比较右侧 / 聚合右值：是外层列引用就换字面量

            注意用 `_ast_literal`（AST 值）而不是 `_render_literal`（SQL 文本）：
            折叠结果是直接喂给执行器的 AST，字符串不能带引号。
            """
            if (isinstance(v, str) and self._is_column_ref(v)
                    and self._qualified_owner(v) in outer_names):
                got = self._outer_column_value(cols, row, v)
                if got is _MISSING:
                    raise ValueError(
                        f"相关子查询折叠失败：外层列 {v} 解析不到")
                return self._ast_literal(got)
            return v

        def fold_expr(node):
            """把表达式节点里的外层列引用换成**字面量节点**

            表达式谓词（`{'expr_pred': ...}`）里的列引用不在 column / value
            字段上，`fold_node` 的既有逻辑看不到它们；漏折叠的话折叠后的
            子查询仍然引用外层列 → 内层解析不到该列，要么报「列不存在」，
            要么被当成字符串比较静默返回 0 行。
            """
            if not isinstance(node, dict):
                return node
            if node.get('kind') == 'col':
                name = str(node.get('name'))
                if self._qualified_owner(name) in outer_names:
                    got = self._outer_column_value(cols, row, name)
                    if got is _MISSING:
                        raise ValueError(
                            f"相关子查询折叠失败：外层列 {name} 解析不到")
                    return {'kind': 'lit', 'value': got}
                return node
            out_n = dict(node)
            if out_n.get('kind') == 'arith':
                out_n['left'] = fold_expr(out_n.get('left'))
                out_n['right'] = fold_expr(out_n.get('right'))
            elif out_n.get('kind') == 'func':
                out_n['args'] = [fold_expr(a)
                                 for a in (out_n.get('args') or [])]
            return out_n

        def fold_node(node, d):
            if not isinstance(node, dict):
                return node
            out = dict(node)
            for key in ('and', 'or'):
                if isinstance(out.get(key), list):
                    out[key] = [fold_node(c, d) for c in out[key]]
            if 'not' in out:
                out['not'] = fold_node(out['not'], d)
            pred = out.get('expr_pred')
            if isinstance(pred, dict):
                new_pred = dict(pred)
                new_pred['left'] = fold_expr(pred.get('left'))
                new_pred['right'] = fold_expr(pred.get('right'))
                out['expr_pred'] = new_pred
            # JOIN ON 的条件树（`on_filter`）：ON 里的外层列引用同样要折叠，
            # 否则折叠后的子查询仍引用外层列 → 内层解析不到 → 静默错答
            if isinstance(out.get('filter'), dict):
                out['filter'] = fold_node(out['filter'], d)
            inner_sub = out.get('subquery')
            if isinstance(inner_sub, dict):
                # 嵌套子查询：它引用的外层列同样折叠成字面量，
                # 然后整个嵌套子查询走正常执行路径（可继续嵌套）
                out['subquery'] = self._fold_outer_literals(
                    inner_sub, row, cols, outer_names, d + 1)
            col = out.get('column')
            if isinstance(col, str) and '.' in col and \
                    self._qualified_owner(col) in outer_names:
                got = self._outer_column_value(cols, row, col)
                if got is _MISSING:
                    raise ValueError(
                        f"相关子查询折叠失败：外层列 {col} 解析不到")
                # 折叠后没有任何比较能留在这一层，直接给常量真
                return None
            if 'value' in out:
                folded_v = fold_value(out['value'])
                if folded_v is not out['value']:
                    # 已折叠成字面量：右侧不再是列引用，标记必须清掉
                    # （否则求值器会拿字面量去当列名解析）
                    out.pop('value_column', None)
                out['value'] = folded_v
            return out

        out = dict(sub)
        # SELECT 列表：`SELECT u.x + 1 AS y` → 常量表达式文本
        # （折叠后用正常路径重新解析，因此标量聚合子查询也能算对）
        cols_out = []
        for c in (sub.get('columns') or []):
            text = str(c)
            for owner in sorted(outer_names, key=len, reverse=True):
                pattern = r'\b' + re.escape(owner) + r'\.(\w+)'

                def _sub(m, _o=owner):
                    got = self._outer_column_value(
                        cols, row, f'{_o}.{m.group(1)}')
                    return (m.group(0) if got is _MISSING
                            else self._render_literal(got))
                text = re.sub(pattern, _sub, text, flags=re.IGNORECASE)
            cols_out.append(text)
        out['columns'] = cols_out
        # 投影顺序：窗口函数项按原样保留（内层窗口不引用外层列）
        if isinstance(sub.get('projection'), list):
            out['projection'] = [
                (k, t) if k != 'col' else (k, str(t))
                for k, t in sub['projection']]

        for key in ('where', 'having'):
            if isinstance(out.get(key), dict):
                folded = fold_node(out[key], 0)
                out[key] = None if folded is None else folded

        new_joins = []
        for j in (sub.get('joins') or []):
            nj = dict(j)
            on = nj.get('on')
            if isinstance(on, dict):
                # JOIN ON 里的外层列引用同样折叠成字面量，交给 JOIN 路径
                # 正常执行（例如 `JOIN users u2 ON u2.id = o.user_id AND
                # u2.id = u.id` → `... AND u2.id = 1`）
                nj['on'] = fold_node(on, 0)
            new_joins.append(nj)
        if new_joins:
            out['joins'] = new_joins
        return out

    def _fold_outer_names(self, sub, outer_name, outer_alias,
                          extra_names=None):
        """收集「可以安全折叠」的外层限定符集合

        只认限定符等于外层表名/别名、且子查询自己没有同名表/别名的引用，
        避免把子查询内部的表别名（例如自连接的 u2）误当外层引用。

        extra_names：外层行由**多张表连接**而来时（JOIN 后置处理路径），
        除 `outer_name` / `outer_alias` 之外还可以折叠的连接表名/别名。
        """
        outers = set()
        for n in (outer_name, outer_alias):
            if n:
                outers.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())
        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())
        return {o for o in outers if o not in inner_names}

    def _outer_ref_columns(self, sub, names, out=None) -> list:
        """收集子查询（含嵌套子查询）里引用外层限定符的列名

        只看 `where` 会漏掉 `value` 位置的引用（`WHERE o.uid = u.id` 里的
        `u.id` 是 value），也漏掉嵌套子查询里的引用。漏掉会让
        `_fold_cache_key` 返回空键 → 所有外层行共用第一行的结果，
        实测表现为"最后一行的答案串到所有行"这类静默错答。
        """
        if out is None:
            out = []
        if not isinstance(sub, dict):
            return out

        def consider(text):
            if (isinstance(text, str) and self._is_column_ref(text)
                    and self._qualified_owner(text) in names):
                key = str(text).strip().strip('`"').lower()
                if key not in out:
                    out.append(key)

        consider(sub.get('column'))
        consider(sub.get('value'))
        # 表达式谓词里的外层列引用（`WHERE LOWER(u.name) = 'x'`）同样要计入：
        # 漏掉会让 `_fold_cache_key` 少一个维度 → 不同外层行共用缓存（错答）
        pred = sub.get('expr_pred')
        if isinstance(pred, dict):
            for node in (pred.get('left'), pred.get('right')):
                for c in _expr_columns_outside_agg(node):
                    consider(c)
        for key in ('and', 'or'):
            for c in (sub.get(key) or []):
                self._outer_ref_columns(c, names, out)
        if isinstance(sub.get('not'), dict):
            self._outer_ref_columns(sub['not'], names, out)
        # 本节点可能是一个条件节点（带 subquery 键指向子查询 AST），
        # 也可能本身就是子查询 AST。两种都要继续往下走。
        nested = sub.get('subquery')
        if isinstance(nested, dict):
            self._outer_ref_columns(nested, names, out)
        for key in ('where', 'having'):
            if isinstance(sub.get(key), dict):
                self._outer_ref_columns(sub[key], names, out)
        for j in (sub.get('joins') or []):
            on = j.get('on')
            if isinstance(on, dict):
                self._outer_ref_columns(on, names, out)
        return out

    def _fold_cache_key(self, sub, row, cols, outer_name, outer_alias,
                        extra_names=None):
        """按「折叠用到的外层列取值」构造缓存键（同值外层行共享结果）

        必须覆盖子查询（含嵌套层）里出现的**所有**外层列引用：
        漏一个就会让取值不同的外层行命中同一条缓存，产生静默错答。
        """
        names = self._fold_outer_names(sub, outer_name, outer_alias,
                                       extra_names)
        items = []
        for c in self._outer_ref_columns(sub, names):
            val = self._outer_column_value(cols, row, c)
            items.append((c, self._fold_key_value(val)))
        items.sort(key=lambda kv: kv[0])
        return tuple(items)

    @classmethod
    def _fold_key_value(cls, value):
        """缓存键里的单值归一化（list/dict 等不可比较的值转成字符串）"""
        if value is _MISSING:
            return ('__missing__',)
        if isinstance(value, (int, float, str, bytes, type(None), bool)):
            return (type(value).__name__, value)
        return ('repr', repr(value))

    def _fold_and_run(self, sub, row, cols, s, outer_name, outer_alias,
                      read_view, extra_names=None):
        """折叠外层列 → 正常执行 → 返回 (结果列名, 结果行列表)

        结果列名一并返回，是为了让 IN / NOT IN 的集合上下文能按**实际
        列数**判定多列子查询（空结果集也要拦得住：`SELECT *` 这类静态
        判不出列数的形态只能靠结果列名）。
        """
        names = self._fold_outer_names(sub, outer_name, outer_alias,
                                       extra_names)
        if not names:
            raise ValueError(
                "相关子查询的外层引用无法安全折叠（找不到外层限定符）")
        folded = self._fold_outer_literals(
            sub, row, cols, names)
        # 折叠后必须不再相关：否则说明还有本实现没覆盖到的外层引用
        if self._is_correlated(folded, outer_name, outer_alias,
                               extra_names) or \
                self._sub_owner_is_outer(folded, outer_name, outer_alias,
                                         extra_names):
            raise ValueError(
                "相关子查询的外层引用无法安全折叠（折叠后仍残留外层引用）")
        leftover = self._leftover_outer_pattern(folded, names)
        if leftover:
            # 例如 `WHERE o.uid = u.id, o.amount > 5`（逗号当 AND 用）：
            # 解析器把整段塞进了 value，折叠后既不是字面量、又含外层引用，
            # 直接执行会静默给出 0 行。这种形态没有安全语义 → 显式报错。
            raise ValueError(
                f"相关子查询无法安全折叠：条件里还残留外层引用 '{leftover}'"
                f"（该形态无法求值，请检查 WHERE 条件是否缺少 AND/OR）")
        res = self.execute(folded, s)
        if not isinstance(res, dict) or res.get('type') == 'error':
            raise ValueError(
                f"相关子查询执行失败: {(res or {}).get('message', '未知错误')}")
        return (list(res.get('columns') or []),
                [tuple(r) for r in (res.get('rows') or [])])

    def _leftover_outer_pattern(self, node, outer_names):
        """折叠后的 AST 里是否还残留 `外层名.列` 文本（返回该文本或 None）"""
        if not isinstance(node, dict):
            return None
        for key in ('column', 'value'):
            v = node.get(key)
            if isinstance(v, str):
                for owner in outer_names:
                    if re.search(r'\b' + re.escape(owner) + r'\.\w+',
                                 v, re.IGNORECASE):
                        return v
        # 表达式谓词：列引用在表达式节点里，按渲染文本做同样的残留检查
        pred = node.get('expr_pred')
        if isinstance(pred, dict):
            text = (_render_scalar_expr(pred.get('left')) + ' '
                    + _render_scalar_expr(pred.get('right')))
            for owner in outer_names:
                if re.search(r'\b' + re.escape(owner) + r'\.\w+',
                             text, re.IGNORECASE):
                    return text
        for key in ('and', 'or'):
            for c in (node.get(key) or []):
                got = self._leftover_outer_pattern(c, outer_names)
                if got:
                    return got
        if isinstance(node.get('not'), dict):
            got = self._leftover_outer_pattern(node['not'], outer_names)
            if got:
                return got
        if isinstance(node.get('subquery'), dict):
            got = self._leftover_outer_pattern(node['subquery'], outer_names)
            if got:
                return got
        for key in ('where', 'having'):
            if isinstance(node.get(key), dict):
                got = self._leftover_outer_pattern(node[key], outer_names)
                if got:
                    return got
        for j in (node.get('joins') or []):
            if isinstance(j.get('on'), dict):
                got = self._leftover_outer_pattern(j['on'], outer_names)
                if got:
                    return got
        return None

    def _residual_outer_ref(self, sub, outer_name, outer_alias,
                            extra_names=None):
        """「疑似非相关」的子查询里是否还藏有外层引用（返回该文本或 None）

        `_is_correlated` / `_sub_owner_is_outer` 的判据只认 `别名.列` 这种
        **纯列引用**（正则 `[\\w.]+`）。真值藏在表达式里的外层引用
        （`WHERE x.uid = MIN(u.id)`、`WHERE x.amt = u.amt + 1`）识别不到，
        会被当成非相关子查询一次性执行：内层解析不到该列 → 要么报「列不存在」，
        要么被存储层当成字符串比较后**静默返回 0 行**。这里做一次文本级残留
        检查，命中就显式报错（见 `_guard_residual_outer_ref`）。

        计算外层限定符时要扣掉**子查询及其嵌套子查询**自己声明的表名/别名，
        否则 `... FROM orders WHERE uid IN (SELECT id FROM users WHERE
        users.city='BJ')` 这类自洽子查询会被误判（users 只是嵌套层自己的表）。
        """
        names = set()
        for n in (outer_name, outer_alias):
            if n:
                names.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                names.add(str(n).lower())
        names -= self._sub_scope_names(sub)
        if not names:
            return None
        return self._leftover_outer_pattern(sub, names)

    def _guard_residual_outer_ref(self, sub, outer_name, outer_alias,
                                  extra_names=None):
        """非相关子查询里仍残留外层引用 → 显式报错（绝不静默给错结果）"""
        got = self._residual_outer_ref(sub, outer_name, outer_alias,
                                       extra_names)
        if got:
            raise ValueError(
                f"子查询里的 '{got}' 无法安全折叠：本引擎只支持把"
                f"「别名.列」形式的纯列引用折叠成外层行的取值，"
                f"不支持在外层引用上套聚合/函数（该形态无法求值）")

    def _eval_scoped(self, row, cols, where, outer):
        """内层行上的条件求值（列名可用外层行解析）

        支持 and/or 与普通比较（比较右侧也可以引用外层列，例如
        `WHERE o.user_id = u.id`），以及字面量 IN 列表；遇到聚合谓词或
        嵌套子查询时明确报错，避免静默给出错误真值。
        """
        if where is None or not isinstance(where, dict):
            return True
        if 'and' in where:
            return all(self._eval_scoped(row, cols, c, outer)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            return any(self._eval_scoped(row, cols, c, outer)
                       for c in parts) if parts else False
        if 'agg' in where:
            raise ValueError("不支持相关子查询里的聚合谓词")
        if isinstance(where.get('subquery'), dict):
            raise ValueError("不支持相关子查询里再嵌套子查询")

        op = where.get('op')
        if op in ('IN', 'NOT IN') and 'values' in where:
            lv = self._scoped_value(where.get('column'), row, cols, outer)
            vals = [None if str(v).upper() == 'NULL' else v
                    for v in (where.get('values') or [])]
            return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))
        if op in ('EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
            raise ValueError("不支持相关子查询里再嵌套子查询")

        lv = self._scoped_value(where.get('column'), row, cols, outer)
        rv = where.get('value')
        # 右侧可能是外层列引用（`o.user_id = u.id`）：能解析成列就当列用，
        # 否则保持原来的字面量语义
        if isinstance(rv, str) and '.' in rv:
            ov = self._scoped_value(rv, row, cols, outer)
            if ov is not _MISSING:
                rv = ov
        if lv is _MISSING:
            return False
        return self._compare_scoped(lv, op, rv)

    @staticmethod
    def _compare_scoped(l, op, r):
        """作用域比较（与 _eval_condition 的语义保持一致）"""
        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        try:
            if isinstance(l, (int, float)) and isinstance(r, str):
                r = float(r) if '.' in r else int(r)
        except (TypeError, ValueError):
            pass
        try:
            if op == '=':  return l == r
            if op == '!=': return l != r
            if op == '<':  return l < r
            if op == '>':  return l > r
            if op == '<=': return l <= r
            if op == '>=': return l >= r
            if op == 'LIKE':
                pat = str(r).replace('%', '.*').replace('_', '.')
                return bool(re.match('^' + pat + '$', str(l)))
        except TypeError:
            return False
        return False

    def _scoped_value(self, name, row, cols, outer):
        """按作用域解析列引用

        解析顺序：外层限定名 → 内层列（裸名或内层限定名）→ 外层裸列。
        外层限定名优先是必须的：内外层都有 `id` 时（users/orders），
        `u.id` 不能解析成内层的 `id`。解析不到返回 _MISSING。
        """
        t = str(name or '').strip().strip('`"')
        if not t:
            return _MISSING
        qual = ''
        if '.' in t and re.match(r'^[\w.]+$', t):
            qual = t.rsplit('.', 1)[0].lower()

        # 1) 外层限定名（`u.id`）
        if outer is not None and qual and qual in outer['names']:
            j = self._col_index_of(outer['cols'], t.rsplit('.', 1)[1])
            if 0 <= j < len(outer['row']):
                return outer['row'][j]
            return _MISSING

        # 2) 内层列（裸名或内层限定名）
        i = self._col_index_of(cols, t)
        if 0 <= i < len(row):
            return row[i]

        # 3) 外层裸列 / 其它限定名
        if outer is not None:
            j = self._col_index_of(outer['cols'],
                                   t.rsplit('.', 1)[1] if qual else t)
            if 0 <= j < len(outer['row']):
                return outer['row'][j]
        return _MISSING

    def _resolve_in_values(self, values, row, cols):
        """IN 候选列表求值：带限定名的项是**列引用**，取本行对应列的值

        `WHERE o.user_id IN (u.id)` 原来被当成字面量集合 {'u.id'} → 恒不命中
        → 静默空集（与 `a = b` 同一类吞错）。带点的项只可能是列引用：
        解析不到就显式报错；裸标识符 / 字面量保持原样，字面量 NULL 归一到
        None（三值语义由 `_in_predicate` 负责）。
        """
        out = []
        for v in (values or []):
            if isinstance(v, str) and '.' in v.strip().strip('`"'):
                j = self._col_index_of(cols, v)
                if j < 0:
                    # 末段歧义（JOIN 结果里 `u.id` / `o.id` 并存）显式报错
                    self._raise_if_ambiguous_col(cols, v)
                    raise ValueError(
                        f"IN 列表里的列 '{v}' 解析不到（列不存在）")
                out.append(row[j] if j < len(row) else None)
                continue
            out.append(None if (v is None or str(v).upper() == 'NULL') else v)
        return out

    @staticmethod
    def _in_predicate(value, values, neg=False):
        """IN / NOT IN 的三值语义（简化为真/假）

        * 左值为 NULL / 解析不到 → UNKNOWN → 不通过；
        * NOT IN 且候选里含 NULL → UNKNOWN → 不通过；
        * 否则按等值匹配。
        """
        if value is _MISSING or value is None:
            return False
        has_null = any(v is None for v in values)
        if neg and has_null:
            return False
        hit = any(v is not None and Executor._value_eq(value, v)
                  for v in values)
        return (not hit) if neg else hit

    @staticmethod
    def _value_eq(a, b):
        """类型感知等值比较（数字与数字字符串视为相等）"""
        if a is None or b is None:
            return a is None and b is None
        if type(a) is type(b):
            return a == b
        try:
            if isinstance(a, (int, float)) and isinstance(b, str):
                b = float(b) if '.' in b else int(b)
            elif isinstance(b, (int, float)) and isinstance(a, str):
                a = float(a) if '.' in a else int(a)
        except (TypeError, ValueError):
            return False
        return a == b

    def _apply_windows(self, p, base_cols, rows, session=None,
                       read_view=None, extra_names=None):
        """计算窗口列并按投影顺序拼装结果行

        非窗口投影项复用 `_projection_item_plan` / `_eval_projection_item`：
        原来这里用 `_col_index_of` 取不到列就静默输出 None，同样属于
        「静默填 NULL」，现在认不出来的投影项会显式报错。

        extra_names：JOIN 后置处理路径传入的连接表名/别名（见 `_project_rows`）。
        """
        windows = p.get('windows') or []
        projection = p.get('projection') or [
            ('col', c) for c in (p.get('columns') or ['*'])]
        displays = self._projection_displays(p, projection)

        # 展开 SELECT *
        if any(k == 'col' and str(t).strip() == '*'
               for k, t in projection):
            expanded = []
            exp_displays = []
            for i, (k, t) in enumerate(projection):
                if k == 'col' and str(t).strip() == '*':
                    expanded.extend(('col', c) for c in base_cols)
                    exp_displays.extend([None] * len(base_cols))
                else:
                    expanded.append((k, t))
                    exp_displays.append(displays[i])
            projection = expanded
            displays = exp_displays

        computed = {}
        for w in windows:
            computed[id(w)] = self._compute_window(w, base_cols, rows)

        session = self._use_session(session)
        outer_name = p.get('table')
        outer_alias = p.get('table_alias')
        sub_cache = {}

        out_cols, plan_items = [], []
        for i, (kind, text) in enumerate(projection):
            if kind == 'win':
                out_cols.append(text['alias'])
                plan_items.append(('win', id(text), text['alias']))
                continue
            item = self._projection_item_plan(
                str(text), base_cols, displays[i])
            out_cols.append(item[2])
            plan_items.append(item)

        out_rows = []
        for ri, r in enumerate(rows):
            out = []
            for item in plan_items:
                if item[0] == 'win':
                    out.append(computed[item[1]][ri])
                    continue
                out.append(self._eval_projection_item(
                    item, r, base_cols, session, read_view,
                    outer_name, outer_alias, sub_cache, extra_names))
            out_rows.append(out)
        return self._finish_select(
            p, out_cols, out_rows,
            sort_values=self._order_by_sort_values(
                p, out_cols, base_cols, rows))

    # ---- ORDER BY 表达式排序键 ----

    @staticmethod
    def _row_agg_values(base_cols, row):
        """分组结果行上可用的聚合值映射（供 ORDER BY 表达式取聚合值）

        实现在 `parser.row_agg_values_map`（计划执行器共用同一份）。
        """
        from .parser import row_agg_values_map
        return row_agg_values_map(base_cols, row)

    def _order_by_sort_values(self, p, out_cols, base_cols, rows):
        """按 ORDER BY 的**每一个键**预先算好排序值

        返回值：与 `p['order_by']` 等长的列表（没有 ORDER BY 时为 None），
        每个元素为：

        * None —— 该键是**输出列**（普通列 / 限定列 / 投影别名）或**序号**，
          由 `_finish_select` 直接从输出行取值；
        * 值列表（与 rows 一一对应）—— 该键是需要按源行求值的表达式
          （`LENGTH(name)`、`salary * 2`、`SUM(salary)`、`ROUND(AVG(v),1)`）。

        为什么需要这条路径：这些键在输出列里根本不存在，存储层与计划执行器的
        `except: pass` 会**静默不排序**（而且解析器原来还会把 DESC 一起丢掉），
        返回一份顺序错误的结果 —— 典型的静默错答。

        **绝不静默丢弃**：认不出的键在这里显式报错，报出「第几个键 + 键文本」。
        """
        keys = normalize_order_by(p.get('order_by'))
        if not keys:
            return None
        out = []
        for pos, (text, _desc) in enumerate(keys):
            col = str(text).strip()
            if self._is_ordinal_order_key(col):
                out.append(None)
                continue
            if col in out_cols:
                out.append(None)
                continue
            stripped = self._strip_qualifier(col)
            if stripped in out_cols:
                if out_cols.count(stripped) == 1:
                    out.append(None)
                    continue
                # ★ 去限定符后**重名**（`SELECT u.id, o.id` → ['id','id']）：
                # `_finish_select` 从输出行取值时只认裸名，两个键都会落到
                # 第一列上（第二键静默算错）。这时改按**基础行**的限定名
                # 求值（`u.id` / `o.id` 在连接结果的列名里是精确唯一的）。
                if self._col_index_of(base_cols, col) < 0:
                    out.append(None)
                    continue
            node = _parse_scalar_expr(col)
            if node is None:
                raise ValueError(self._order_key_error_message(pos, col))
            self._check_expr_functions(node, 'ORDER BY')
            if node.get('kind') == 'col':
                # 纯列名形态：列必须在基础行里存在，否则显式报错
                # （原来会静默不排序，或者取到 None 当成 NULL 值排序）
                name = str(node.get('name') or col)
                if self._col_index_of(base_cols, name) < 0:
                    raise ValueError(self._order_key_error_message(
                        pos, col,
                        f"列 '{name}' 既不在输出列里，也不在可用列里"))
            values = [
                self._eval_expr_in_row(
                    node, base_cols, r, self._row_agg_values(base_cols, r),
                    'ORDER BY')
                for r in rows
            ]
            out.append(values)
        return out

    @classmethod
    def _order_output_values(cls, col, pos, out_cols, out_rows):
        """从**输出行**里取出第 pos 个 ORDER BY 键的取值

        解析顺序：列序号（`ORDER BY 2` → 2 号输出列）→ 精确列名 →
        去限定符的列名；都取不到时**显式报错**（绝不静默不排序）。
        """
        n = len(out_cols)
        if cls._is_ordinal_order_key(col):
            k = int(col)
            if k < 1 or k > n:
                raise ValueError(
                    f"ORDER BY 第 {pos + 1} 个排序键是列序号 {k}，"
                    f"超出输出列范围（1..{n}）")
            i = k - 1
        else:
            i = None
            if col in out_cols:
                i = out_cols.index(col)
            elif cls._strip_qualifier(col) in out_cols:
                i = out_cols.index(cls._strip_qualifier(col))
            if i is None:
                raise ValueError(cls._order_key_error_message(pos, col))
        return [r[i] if i < len(r) else None for r in out_rows]

    def _finish_select(self, p, out_cols, out_rows, sort_values=None):
        """DISTINCT / ORDER BY / LIMIT（窗口计算之后才生效）

        ORDER BY（本次改造为**真正的多键排序**）：

        * 任意个键，**逐键独立** ASC / DESC；
        * 实现方式：先按最后一个键、再倒数第二个 …… 最后第一个键，逐个做
          **稳定排序**（Python 的 `list.sort` 是稳定排序）。后一个键相等的行
          在前一个键的排序里保持原有相对顺序，因此整体等价于字典序多键排序，
          且方向可以逐键不同；
        * NULL 定义：**NULL 视为最大值**（与 PostgreSQL 默认一致）—— 升序时
          排在最后、降序时排在最前，**逐键一致、可预测**。实现上用
          `(值 is None, 值)` 作为排序键：NULL 的元组首元素为 True，比任何
          非 NULL 的都大；`reverse=True`（DESC）时它排到最前。

        sort_values：`_order_by_sort_values` 按 ORDER BY **逐键**预先算好的
        排序值（与 out_rows 一一对应；元素为 None 表示该键从输出列取）。
        """
        order_by = p.get('order_by')
        keys = normalize_order_by(order_by)

        if p.get('distinct'):
            seen, deduped = set(), []
            # 排序键值必须与行**一起**去重：DISTINCT 之后行数变了，原来按
            # 「源行下标」对齐的排序值列表会整体错位（长度不等时旧实现直接
            # 回落到按列名查找，查不到就静默不排序）。
            # 注意 `sort_values` 的外层是**键**、内层是行：这里要按「每个键
            # 各自保留被去重保留下来的那些行的取值」重建，不能按行拼接。
            kept_values = ([[] if sv is not None else None
                            for sv in sort_values] if sort_values else None)
            for i, r in enumerate(out_rows):
                key = tuple(r)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(r)
                if kept_values is not None:
                    for kpos, sv in enumerate(sort_values):
                        if sv is not None:
                            kept_values[kpos].append(
                                sv[i] if i < len(sv) else None)
            out_rows = deduped
            if kept_values is not None:
                sort_values = kept_values

        if keys:
            # 逐键取排序值（None → 从输出行取；否则用预先算好的表达式值）
            key_values = []
            for pos, (text, desc) in enumerate(keys):
                col = str(text).strip()
                sv = (sort_values[pos]
                      if (sort_values and pos < len(sort_values)) else None)
                if sv is not None and len(sv) == len(out_rows):
                    key_values.append((list(sv), bool(desc)))
                else:
                    key_values.append((
                        self._order_output_values(
                            col, pos, out_cols, out_rows),
                        bool(desc)))
            # 从最后一个键往前做稳定排序 → 字典序多键 + 逐键方向
            idx = list(range(len(out_rows)))
            for vals, desc in reversed(key_values):
                idx.sort(
                    key=lambda i, v=vals: (v[i] is None, v[i]),
                    reverse=desc)
            out_rows = [out_rows[i] for i in idx]

        # LIMIT / OFFSET：都在 ORDER BY 之后、对最终结果裁剪
        offset = int(p.get('offset') or 0)
        limit = p.get('limit')
        if limit is not None:
            out_rows = out_rows[offset:offset + int(limit)]
        elif offset:
            out_rows = out_rows[offset:]
        return self._rs(out_cols, out_rows)

    def _compute_window(self, spec, base_cols, rows):
        """计算一个窗口函数在每行上的取值

        支持 ROW_NUMBER / RANK / DENSE_RANK / LAG / LEAD /
        SUM / AVG / COUNT / MIN / MAX；有 ORDER BY 时聚合为累计值，
        无 ORDER BY 时为整个分区的值。
        """
        func = spec.get('func', '').upper()
        args = spec.get('args') or []
        pidx = [self._col_index_of(base_cols, c)
                for c in (spec.get('partition_by') or [])]
        oidx = [(self._col_index_of(base_cols, c), d)
                for c, d in (spec.get('order_by') or [])]
        arg_idx = (self._col_index_of(base_cols, args[0])
                   if args else -1)
        offset = 1
        if len(args) > 1:
            try:
                offset = int(args[1])
            except (TypeError, ValueError):
                offset = 1

        total = len(rows)
        out = [None] * total

        groups = {}
        for i in range(total):
            key = tuple(rows[i][j] for j in pidx
                        if 0 <= j < len(rows[i]))
            groups.setdefault(key, []).append(i)

        for idxs in groups.values():
            # 稳定排序：从最后一个排序键往前排，可支持多列升降序混排
            for j, desc in reversed(oidx):
                idxs.sort(
                    key=lambda i, j=j: (rows[i][j] is None, rows[i][j]),
                    reverse=bool(desc))
            n = len(idxs)
            vals = [rows[i][arg_idx]
                    if 0 <= arg_idx < len(rows[i]) else None
                    for i in idxs]

            if func == 'ROW_NUMBER':
                for pos, i in enumerate(idxs):
                    out[i] = pos + 1
            elif func in ('RANK', 'DENSE_RANK'):
                prev = object()
                rank = dense = 0
                for pos, i in enumerate(idxs):
                    cur = tuple(rows[i][j] for j, _ in oidx
                                if 0 <= j < len(rows[i]))
                    if pos == 0 or cur != prev:
                        rank = pos + 1
                        dense += 1
                        prev = cur
                    out[i] = rank if func == 'RANK' else dense
            elif func in ('LAG', 'LEAD'):
                for pos, i in enumerate(idxs):
                    q = pos - offset if func == 'LAG' else pos + offset
                    out[i] = vals[q] if 0 <= q < n else None
            elif func in ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX'):
                frame = spec.get('frame')
                if frame is not None:
                    # ★ 显式帧：ROWS 按物理行取帧；RANGE 只支持默认帧
                    # （UNBOUNDED PRECEDING → CURRENT ROW，CURRENT ROW
                    # 按 peer 组解释）。帧只在聚合类窗口函数上生效；
                    # ROW_NUMBER/RANK/DENSE_RANK/LAG/LEAD 按标准忽略帧。
                    peers = (self._peer_ends(idxs, rows, oidx)
                             if str(frame.get('type') or 'ROWS').upper()
                             == 'RANGE' else None)
                    for pos, i in enumerate(idxs):
                        lo, hi = self._frame_bounds(frame, pos, n, peers)
                        seg = vals[lo:hi + 1] if hi >= lo else []
                        out[i] = self._agg_over_frame(seg, func, arg_idx)
                    continue
                acc, seen_vals = None, []
                for pos, i in enumerate(idxs):
                    v = vals[pos]
                    if func == 'COUNT':
                        if arg_idx < 0 or v is not None:
                            acc = (acc or 0) + 1
                        out[i] = acc or 0
                        continue
                    if v is not None:
                        if func == 'SUM':
                            acc = v if acc is None else acc + v
                        elif func == 'AVG':
                            seen_vals.append(v)
                            acc = sum(seen_vals) / len(seen_vals)
                        elif func == 'MIN':
                            acc = v if acc is None else min(acc, v)
                        elif func == 'MAX':
                            acc = v if acc is None else max(acc, v)
                    out[i] = acc
                if not oidx:
                    # 无 ORDER BY：整个分区同一个值
                    final = out[idxs[-1]] if idxs else None
                    for i in idxs:
                        out[i] = final
            else:
                logger.warn(f"未实现的窗口函数: {func}")
        return out

    # ==================== 窗口帧（ROWS / RANGE 语义） ====================

    @staticmethod
    def _peer_ends(idxs, rows, oidx):
        """RANGE 帧用的 peer 边界表：每个位置所属「对等行组」的末位下标

        peer = 分区内 ORDER BY 值完全相同的行（SQL 的 RANGE 按值域取帧，
        CURRENT ROW 作为结束边界表示当前对等组的最后一行）。无 ORDER BY
        时整个分区互为 peer，因此末位恒为分区最后一行。
        返回值的下标是「分区内排序位置」，与 `_frame_bounds` 的 pos 对齐。
        """
        n = len(idxs)
        if not oidx:
            return [n - 1] * n
        keys = [tuple(rows[i][j] for j, _ in oidx
                      if 0 <= j < len(rows[i])) for i in idxs]
        out = [0] * n
        last = n - 1
        for pos in range(n - 1, -1, -1):
            if pos < n - 1 and keys[pos] != keys[pos + 1]:
                last = pos
            out[pos] = last
        return out

    @staticmethod
    def _frame_bounds(frame, pos, n, peers=None):
        """把窗口帧换算成当前分区内的下标闭区间 [lo, hi]

        ROWS：严格按「分区内排序位置」取行，不做值域合并。
        RANGE（仅 UNBOUNDED PRECEDING → CURRENT ROW）：CURRENT ROW 取
        当前 peer 组的末位（peers[pos]），即对等值行整组一起进帧。
        区间不裁剪到合法范围之外，因此越界时会返回 lo > hi（空帧），
        而不是错误地退化成边界那一行。

        边界形式：UNBOUNDED PRECEDING / <n> PRECEDING / CURRENT ROW /
        <n> FOLLOWING / UNBOUNDED FOLLOWING（已在解析器里校验过组合）。
        """
        ftype = str(frame.get('type') or 'ROWS').upper()

        def to_pos(bound):
            kind, k = bound
            if kind == 'UNBOUNDED PRECEDING':
                return 0
            if kind == 'UNBOUNDED FOLLOWING':
                return n - 1
            if kind == 'CURRENT ROW':
                if ftype == 'RANGE':
                    return peers[pos] if peers else pos
                return pos
            if ftype == 'RANGE':
                # 解析器已挡掉；这里是防御性检查，避免静默按 ROWS 取帧
                raise ValueError("RANGE 帧不支持数值偏移，请显式写成 ROWS")
            if kind == 'PRECEDING':
                return pos - k
            return pos + k          # FOLLOWING

        lo = max(0, to_pos(frame['start']))
        hi = min(n - 1, to_pos(frame['end']))
        return lo, hi

    @staticmethod
    def _agg_over_frame(seg, func, arg_idx):
        """在帧内的行上求聚合值（NULL 一律跳过，符合 SQL 语义）"""
        if func == 'COUNT':
            if arg_idx < 0:          # COUNT(*)：帧内所有行
                return len(seg)
            return sum(1 for v in seg if v is not None)
        nums = [v for v in seg if v is not None]
        if not nums:
            return None
        try:
            if func == 'SUM':
                return sum(nums)
            if func == 'AVG':
                return sum(nums) / len(nums)
            if func == 'MIN':
                return min(nums)
            if func == 'MAX':
                return max(nums)
        except TypeError:
            return None
        return None

    # ==================== UNION ====================

    def _cmd_union(self, p, s):
        """UNION / UNION ALL

        按左结合逐段合并：UNION ALL 直接追加；UNION 对「已累积的整份结果」
        去重（因此同一分支内部的重复行也会被去掉），保留首次出现的顺序。
        """
        segments = p.get('segments') or []
        unions_all = p.get('unions_all') or []
        out_cols = None
        result = []

        for i, seg in enumerate(segments):
            res = self.execute(seg, s)
            if isinstance(res, dict) and res.get('type') == 'error':
                return res

            cols = res.get('columns') or []
            if out_cols is None:
                out_cols = cols
            elif cols and len(cols) != len(out_cols):
                return self._err(1222, "UNION 各分支的列数不一致")

            rows = [tuple(r) for r in (res.get('rows') or [])]
            is_all = bool(unions_all[i - 1]) if (
                i > 0 and i - 1 < len(unions_all)) else False

            if i == 0 or is_all:
                result.extend(rows)
            else:
                merged = result + rows
                seen, deduped = set(), []
                for row in merged:
                    if row not in seen:
                        seen.add(row)
                        deduped.append(row)
                result = deduped

        # 整份 UNION 结果再应用 ORDER BY / LIMIT / OFFSET（SQL 语义：
        # 它们作用在合并后的结果上，而不是最后一个分支）。parser 已把
        # 末尾的 order_by/limit/offset 提升到 union 层。
        return self._finish_select(p, out_cols or [], result)

    def _exec_join_query(self, p, s):
        """多表 JOIN

        两条路径：

        1. **纯计划路径**（既有行为，零改动）：查询里没有需要逐连接行求值的
           子查询时，整条查询交给优化器 + 计划执行器（Filter/Project/
           GroupBy/Sort/Limit 都在计划里）。
        2. **后置处理路径**：WHERE 里有标量子查询比较 / 引用连接表的子查询、
           HAVING 里有子查询、投影列表里有标量子查询，**或 ORDER BY 的键
           是计划路径解析不了的形态**（引用了没被投影的列、是表达式 / 序号 /
           聚合 / 非投影别名，见 `_join_order_needs_materialize`）时，计划
           执行器只负责「FROM + JOIN（含 ON）」，产出带限定列名的连接结果；
           随后由执行器逐连接行把子查询折叠求值（`_fold_and_run` +
           `_scalar_from_rows`，与单表相关子查询完全同一套机制与缓存），
          排序则由统一排序路径 `_finish_select` 完成（多键 / 逐键方向 /
           别名 / 序号 / 表达式 / 聚合都支持）。

        为什么不让计划执行器只处理一半条件：`A OR (SELECT ...) > 1` 这种形态
        只摘掉子查询那一半会让真值表变错（要么全放行、要么丢太多行），
        因此只要 WHERE 里出现必须在执行器里求值的子查询，整个 WHERE 都改由
        执行器逐行求值（`_eval_plain_condition` 保留限定列名精确解析）。
        """
        from .plan_executor import PlanExecutor

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        if self.optimizer is None:
            return self._err(1105, "优化器未初始化")

        join_names = self._join_qualifiers(p)
        if self._join_needs_row_eval(p, join_names) \
                or self._join_order_needs_materialize(p):
            return self._exec_join_query_with_subqueries(
                p, s, rv, join_names)

        plan = self.optimizer.optimize(p)
        self._last_plan = plan
        # ★ 计划构建失败（optimize 返回 None）必须显式报错：
        # `PlanExecutor.execute(None)` 会返回 `[], []`，JOIN 查询会**静默**
        # 变成一个 0 列 0 行的结果集（用户以为「没有匹配行」）。
        if plan is None:
            return self._err(
                1105, "JOIN 查询无法生成执行计划（FROM/JOIN 形态不支持）"
                      "：绝不静默返回空集")

        executor = PlanExecutor(self.db, read_view=rv)
        # ★ 逃生口要能传到计划路径：本实例的严格模式开关原样传给计划执行器，
        # 否则 `Executor.strict_group_by = False` 在 JOIN 查询上不生效。
        # 只在显式表态（非 None）时传递，避免用 None 覆盖掉
        # `PlanExecutor.strict_group_by` 的类属性设置。
        if self.strict_group_by is not None:
            executor.strict_group_by = self.strict_group_by
        cols, rows = executor.execute(plan)
        return self._rs(self._plan_display_cols(p, cols), rows)

    def _plan_display_cols(self, p, cols):
        """把计划路径的输出列名换成 MySQL 的输出列名（`data->'$.k'` 显示原文）

        计划执行器 `_exec_Project` 的列名来自 `p['columns']`（已在解析期被
        改写成 `JSON_EXTRACT(...)`），且会按「最后一个点号」切短名 —— 对
        `JSON_EXTRACT(d.data, '$.k')` 会切出 `data, '$.k')` 这种无意义的列名。
        MySQL 在这里显示的是用户写的原文，因此按位置还原：用
        `p['json_display']`（与投影列表逐项对齐，无改写处为 None）覆盖；
        项数不一致（`SELECT *` 展开 / 投影里有窗口函数）时原样返回。
        """
        disp = list((p or {}).get('json_display') or [])
        cols = [str(c) for c in cols]
        if not any(d for d in disp) or len(disp) != len(cols):
            return cols
        return [disp[i] or cols[i] for i in range(len(cols))]

    def _join_qualifiers(self, p) -> list:
        """JOIN 查询里所有可作外层限定符的名字（别名优先，其次表名）

        计划执行器的 SeqScan 用别名（没有别名则用表名）作列前缀，因此连接
        结果的列名是 `u.id` / `o.uid`；这份名单同时用于「相关判定」和
        「外层列折叠」，漏一个就会把引用连接表的子查询误判成非相关。
        """
        names = []
        items = [(p.get('table'), p.get('table_alias'))]
        for j in (p.get('joins') or []):
            items.append((j.get('table'), j.get('alias')))
        for t, a in items:
            for n in (a, t):
                if n and str(n).lower() not in [x.lower() for x in names]:
                    names.append(str(n))
        return names

    def _join_needs_row_eval(self, p, join_names) -> bool:
        """JOIN 查询里是否存在「必须逐连接行求值」的子查询

        * WHERE 里的标量比较（`(SELECT ...) > 1`）：行数语义要逐行收敛；
        * WHERE 里引用任一连接表的相关子查询（EXISTS / IN / 标量）：
          计划执行器没有逐行折叠外层列的能力（原来会静默返回空集/0 行）；
        * HAVING 里的任何子查询（分组键折叠必须逐分组做）；
        * 投影列表里的标量子查询。
        """
        where = p.get('where')
        if self._where_has_scalar_subquery(where):
            return True
        for sub in self._all_nested_subqueries(where):
            if self._is_correlated(sub, p.get('table'),
                                   p.get('table_alias'), join_names) or \
                    self._sub_owner_is_outer(
                        sub, p.get('table'), p.get('table_alias'),
                        join_names):
                return True
        if self._contains_subquery(p.get('having')):
            return True
        return any(
            self._projection_subquery_ast(self._split_alias(str(c))[0])[0]
            is not None
            for c in (p.get('columns') or []) if c != '*')

    @staticmethod
    def _proj_name_matches(name, key) -> bool:
        """计划路径的输出列名 `name` 能否被排序键 `key` 命中

        与 `plan_executor._resolve_loose` 同一套判定：按「列名末段」或
        `AS 别名` 匹配（大小写不敏感）。
        """
        want = str(key).strip().strip('`"').rsplit('.', 1)[-1].lower()
        if not want:
            return False
        text = str(name).strip().strip('`"')
        m = re.search(r'\s+AS\s+[`"]?(\w+)[`"]?\s*$', text, re.IGNORECASE)
        last = (m.group(1) if m else text.rsplit('.', 1)[-1]).lower()
        return last == want

    def _join_order_needs_materialize(self, p) -> bool:
        """JOIN 计划路径**解析不了** ORDER BY 键时，改走物化连接路径

        计划路径的 Sort 位于 Project / GroupBy 之上，只能看到**投影之后**
        的列名（`u.city` 压成 `city`、聚合表达式压成 `COUNT(*)` / `AS 别名`）。
        排序键引用了没被投影的列（`SELECT u.id ... ORDER BY o.amt DESC`）或
        是表达式时，计划路径算不出排序键 —— 原实现静默不排序（顺序错误）。

        这里判定为 True 时改走 `_exec_join_query_with_subqueries`：先把
        `FROM ... JOIN ... ON ...` 物化成带限定列名的内存关系，再交给执行器
        的统一排序路径（`_finish_select`），因此「非投影列 / 别名 / 序号 /
        表达式 / 聚合 / 多键」全部由同一份实现处理，结果正确。
        仍无法解析的键由统一路径**显式报错**（绝不静默丢弃）。
        """
        keys = normalize_order_by(p.get('order_by'))
        if not keys:
            return False
        columns = [str(c) for c in (p.get('columns') or ['*'])]
        if columns == ['*']:
            return False        # 全部列都在，计划路径能解析任何列名
        has_agg = any(re.match(r'^\w+\s*\(', c.strip()) for c in columns)
        if p.get('group_by') or has_agg:
            # GroupBy 的输出列名 = SELECT 列的**原文**（含 `AS 别名`）。
            # 曾把原文转成大写（`u.city` -> `U.CITY`、`cnt` -> `CNT`），
            # 这不符合 MySQL：列标签应保留书写形式。
            out_names = [c.strip() for c in columns]
        else:
            out_names = []
            for c in columns:
                expr, alias = self._split_alias(c)
                out_names.append(
                    alias or self._strip_qualifier(expr).strip())
        for text, _desc in keys:
            t = str(text).strip()
            if not t:
                continue
            if self._is_ordinal_order_key(t):
                k = int(t)
                if k < 1 or k > len(columns):
                    return True     # 序号越界：交给统一路径显式报错
                continue
            if re.match(r'^[`"]?[\w.]+[`"]?$', t):
                hits = sum(1 for n in out_names
                           if self._proj_name_matches(n, t))
                if hits == 1:
                    continue
                # hits == 0：非投影列；hits > 1：去限定符后重名
                # （`SELECT u.id, o.id ... ORDER BY u.id` → 计划 Sort 只看得到
                # 投影后的裸列名 'id'，两个键都落第一列 → 静默算错第二键）。
                # 两种情况都改走物化连接路径，由统一排序路径按限定名求值。
                return True
            # 表达式：引用的列未必都在投影里，交给统一路径按基础行求值
            return True
        return False

    def _exec_join_query_with_subqueries(self, p, s, rv, join_names):
        """JOIN 查询的后置处理路径：先算连接，再逐连接行处理子查询

        1. 计划执行器只算 `FROM ... JOIN ... ON ...`（投影成 `*`，列名保留
           限定前缀 `u.id` / `o.uid`）；
        2. 结果物化成内存关系，交给 `_select_correlated` 的逐行路径：
           WHERE（含标量/相关子查询）→ GROUP BY/HAVING（含标量子查询）→
           投影（含标量子查询）→ DISTINCT/ORDER BY/LIMIT+OFFSET。
        """
        from .plan_executor import PlanExecutor

        join_p = dict(p)
        join_p.update({
            'columns': ['*'], 'projection': None, 'where': None,
            'having': None, 'group_by': None, 'order_by': None,
            'limit': None, 'offset': None, 'distinct': False,
            'windows': [],
        })
        plan = self.optimizer.optimize(join_p)
        self._last_plan = plan
        if plan is None:
            return self._err(
                1105, "JOIN 查询无法生成执行计划（FROM/JOIN 形态不支持）"
                      "：绝不静默返回空集")
        executor = PlanExecutor(self.db, read_view=rv)
        # ★ 同 _exec_join_query：严格模式开关随实例传递（逃生口）
        if self.strict_group_by is not None:
            executor.strict_group_by = self.strict_group_by
        cols, rows = executor.execute(plan)
        rel = _CTERelation('join', cols, rows)
        return self._select_correlated(
            p, s, rel, rv, p.get('table'), extra_names=join_names)

    def _where_to_pk_equalities(self, tbl, where, read_view):
        """把「存储层认不出的条件」改写成按主键等值的 or 链（UPDATE/DELETE 用）

        表达式谓词（标量函数 / 算术）存储层 `Table._match` 认不出，会
        `KeyError('column')` —— 外层报成"表不存在"，与真实原因无关，
        甚至可能被当成"条件恒真"而**误改全表**。这里先用执行器自己的
        统一求值器扫一遍表算出命中行的主键，再把条件改写成
        `pk = k1 OR pk = k2 ...` 交给存储层，语义与逐行判断完全一致。
        """
        cols, rows = tbl.select(['*'], None, None, None, read_view)
        cols = [str(c) for c in cols]
        pk_name = str(tbl.pk_col)
        if pk_name not in cols:
            raise ValueError(
                f"UPDATE/DELETE 的函数/算术条件需要主键列，表里找不到 "
                f"'{pk_name}'")
        pi = cols.index(pk_name)
        norm = self._normalize_where(where)
        pks = [r[pi] for r in rows
               if self._eval_condition(list(r), cols, norm)]
        return {'or': [{'column': pk_name, 'op': '=', 'value': k}
                       for k in pks]}

    def _cmd_update(self, p, s):
        tbl = self.db.get_table(p['table'])
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None
        where = p.get('where')
        if self._where_has_expr_pred(where) \
                or self._where_has_column_rhs(where):
            where = self._where_to_pk_equalities(tbl, where, rv)

        before = self._triggers_for(p['table'], 'BEFORE', 'UPDATE')
        after = self._triggers_for(p['table'], 'AFTER', 'UPDATE')
        has_triggers = bool(before or after)
        need_binlog = self._binlog_record_enabled()

        # 两套镜像来源（阶段 2）：
        #  * **WAL 增量**（无触发器 且 where 非空）：语句前不 SELECT，写完
        #    后从本表 WAL 增量读 old/new/pk（见 `_wal_dml_images`）。
        #    显式事务按 txn_id 过滤；autocommit 用表级语句锁圈定 WAL 区间。
        #  * **预先 SELECT**（有触发器，或 where 为空 = 可能整表更新，或
        #    `LIGHTDB_BINLOG_MIRROR=legacy` 排障模式）：本来就要扫全表/
        #    触发器需要 OLD 行，旧行直接复用（不再多扫一趟）。
        wal_mode = self._binlog_wal_images_enabled()
        use_wal = bool(need_binlog and not has_triggers
                       and where is not None and wal_mode)
        prefetch = bool(has_triggers or
                        (need_binlog and (where is None or not wal_mode)))
        old_cols, old_rows = (None, [])
        if prefetch:
            old_cols, old_rows = tbl.select(['*'], where, read_view=rv)
            if has_triggers:
                for r in old_rows:
                    row = [self._set_value(p['set'], old_cols, r, c)
                           for c in old_cols]
                    self._fire_triggers('BEFORE', 'UPDATE', p['table'],
                                        old_row=list(r), new_row=row,
                                        session=s)
                    for i, c in enumerate(old_cols):
                        if row[i] != r[i]:
                            # BEFORE UPDATE 里 `SET NEW.c = ...` 的改动合并进
                            # 真正的 SET 列表（否则触发器算了不用 = 静默丢弃）
                            p['set'][c] = row[i]

        lsn_start = getattr(tbl.wal, 'lsn', None) if use_wal else None
        guard = (self._binlog_table_guard(tbl.name)
                 if (use_wal and txn is None) else contextlib.nullcontext())
        with guard:
            n = tbl.update(p['set'], where, txn_id=tid, read_view=rv)
            if use_wal:
                lsn_end = getattr(tbl.wal, 'lsn', None)
                images = self._wal_dml_images(
                    tbl, lsn_start, lsn_end, 'UPDATE',
                    txn_id=(tid if txn is not None else None))
                self._record_update_images(s, tbl, images, n)

        # 预先 SELECT 已拿到旧行：镜像直接复用（有触发器 / where 为空的路径）
        if need_binlog and prefetch and old_rows:
            bl_cols = [str(c) for c in old_cols]
            pk_name = str(tbl.pk_col)
            pk_idx = (bl_cols.index(pk_name)
                      if pk_name in bl_cols else int(tbl.pk_idx))
            for r in old_rows:
                new_row = [self._set_value(p['set'], bl_cols, r, c)
                           for c in bl_cols]
                new_row = self._binlog_coerce_row(tbl, new_row)
                self._binlog_record(
                    s, 'UPDATE', p['table'],
                    {'cols': bl_cols, 'pk': r[pk_idx],
                     'old': list(r), 'new': new_row})

        if after:
            for r in old_rows:
                new_row = [self._set_value(p['set'], old_cols, r, c)
                           for c in old_cols]
                self._fire_triggers('AFTER', 'UPDATE', p['table'],
                                    old_row=list(r), new_row=new_row,
                                    session=s)
        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) updated")

    def _record_update_images(self, s, tbl, images, affected):
        """把 WAL 增量里的 UPDATE 镜像写进 binlog 缓冲（内容与手工 SELECT 一致）

        WAL 里的 `old` 是行前像（已 coerce），`new` 是 SET 之后的意图值
        （可能仍是解析器原文，如 `'999'`）；这里按列类型 coerce 一次，
        保证 binlog 镜像与"手工 SELECT 出来的值"逐列相等（从库重放也用
        实际落盘值）。
        """
        cols = [c['name'] for c in tbl.cols]
        for _rtype, d in images:
            old = self._binlog_coerce_row(tbl, list(d.get('old') or []))
            new = self._binlog_coerce_row(tbl, list(d.get('new') or []))
            self._binlog_record(
                s, 'UPDATE', tbl.name,
                {'cols': cols, 'pk': d.get('pk'),
                 'old': old, 'new': new})
        if affected and len(images) != affected:
            # 如实告警：镜像条数与 affected 不一致（正常不应发生；不阻塞
            # 语句，但把差异暴露出来，避免静默的记录缺失）
            logger.warn(
                f"binlog UPDATE 镜像条数 {len(images)} != affected "
                f"{affected}（表 {tbl.name}）—— 请检查 WAL 增量读取")

    @staticmethod
    def _set_value(set_pairs, cols, row, col):
        """取 SET 列表里某列的目标值（列不在 SET 里就沿用旧值）"""
        if col in set_pairs:
            return set_pairs[col]
        try:
            return row[cols.index(col)]
        except (ValueError, TypeError):
            return None

    def _triggers_for(self, table, timing, event):
        mgr = self._trigger_mgr
        if mgr is None:
            return []
        return [t for t in (getattr(mgr, 'triggers', {}).get(table) or [])
                if t.timing == timing and t.event == event]

    def _cmd_delete(self, p, s):
        tbl = self.db.get_table(p['table'])
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None
        where = p.get('where')
        if self._where_has_expr_pred(where) \
                or self._where_has_column_rhs(where):
            where = self._where_to_pk_equalities(tbl, where, rv)

        has_triggers = bool(
            self._triggers_for(p['table'], 'BEFORE', 'DELETE')
            or self._triggers_for(p['table'], 'AFTER', 'DELETE'))
        need_binlog = self._binlog_record_enabled()

        # 阶段 2：镜像来源与 UPDATE 同理 —— 无触发器且 where 非空时用 WAL
        # 增量（语句前不 SELECT）；有触发器/where 为空/legacy 对照时预先
        # SELECT 并复用。
        wal_mode = self._binlog_wal_images_enabled()
        use_wal = bool(need_binlog and not has_triggers
                       and where is not None and wal_mode)
        prefetch = bool(has_triggers or
                        (need_binlog and (where is None or not wal_mode)))
        old_rows = []
        if prefetch:
            _, old_rows = tbl.select(['*'], where, read_view=rv)
            if has_triggers:
                for r in old_rows:
                    self._fire_triggers('BEFORE', 'DELETE', p['table'],
                                        old_row=list(r), session=s)

        lsn_start = getattr(tbl.wal, 'lsn', None) if use_wal else None
        guard = (self._binlog_table_guard(tbl.name)
                 if (use_wal and txn is None) else contextlib.nullcontext())
        with guard:
            n = tbl.delete(where, txn_id=tid, read_view=rv)
            if use_wal:
                lsn_end = getattr(tbl.wal, 'lsn', None)
                images = self._wal_dml_images(
                    tbl, lsn_start, lsn_end, 'DELETE',
                    txn_id=(tid if txn is not None else None))
                col_names = [c['name'] for c in tbl.cols]
                for _rtype, d in images:
                    old = self._binlog_coerce_row(
                        tbl, list(d.get('old') or []))
                    self._binlog_record(
                        s, 'DELETE', p['table'],
                        {'cols': col_names, 'pk': d.get('pk'),
                         'old': old})
                if n and len(images) != n:
                    logger.warn(
                        f"binlog DELETE 镜像条数 {len(images)} != affected "
                        f"{n}（表 {tbl.name}）—— 请检查 WAL 增量读取")

        if need_binlog and prefetch and old_rows:
            bl_cols = [c['name'] for c in tbl.cols]
            pk_name = str(tbl.pk_col)
            pk_idx = (bl_cols.index(pk_name)
                      if pk_name in bl_cols else int(tbl.pk_idx))
            for r in old_rows:
                self._binlog_record(
                    s, 'DELETE', p['table'],
                    {'cols': bl_cols, 'pk': r[pk_idx], 'old': list(r)})

        if has_triggers:
            for r in old_rows:
                self._fire_triggers('AFTER', 'DELETE', p['table'],
                                    old_row=list(r), session=s)
        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) deleted")

    # ============ MySQL 兼容：复制 / 运维 / 安全 / 存储程序 ============

    # ---------- 复制位点：真 · append-only binlog（决策 D 项） ----------

    def _binlog_reader(self):
        """本库的 append-only binlog 读写器（`Database.binlog_log`）

        未启用（`LIGHTDB_BINLOG_ENABLED=0`）或打开失败时为 None，调用方按
        "本实例没有 binlog"如实处理（空结果集），**不编造位点**。
        """
        return self._binlog_writer()

    def _binlog_relation(self):
        """`__BINLOG_FETCH__` 虚拟表：直接读 append-only binlog 文件

        列保持从库接口的 6 列兼容形态：

        * ``pos``        = 数据事件逻辑序号（1 起；沿用旧从库 watermark 语义）
        * ``lsn``        = 该事件在 binlog 文件里的**真实字节偏移**
        * ``txn_id``     = 事务号（autocommit 语句为 0）
        * ``event_type`` = INSERT / UPDATE / DELETE / DDL
        * ``table_name`` = 表名（DDL 未解析出表名时为 NULL）
        * ``data``       = 事件 payload 的 data 字段（JSON 文本）

        文件损坏/截断时**显式报错**（`BinlogCorrupted` / `BinlogTruncated`
        会向上抛，最终由 `execute()` 统一成错误结果），绝不静默返回空流。
        """
        cols = ['pos', 'lsn', 'txn_id', 'event_type', 'table_name', 'data']
        rec = self._binlog_reader()
        if rec is None:
            return _CTERelation('__BINLOG_FETCH__', cols, [])
        rows = rec.fetch_rows(0)
        return _CTERelation('__BINLOG_FETCH__', cols, rows)

    # ---------- SHOW 系列（结果集回传） ----------

    def _exec_show_stmt(self, p, s):
        """show_stmt 标记 → 具体 handler（见 parser._show_result）"""
        kind = str(p.get('show_stmt') or '').lower()
        handler = getattr(self, f'_show_{kind}', None)
        if handler is None:
            return self._err(1064, f"不支持的 SHOW 语句: {p.get('show_stmt')}")
        return handler(p, s)

    def _show_master_status(self, p, s):
        """SHOW MASTER STATUS —— 来自真 append-only binlog 文件

        * `File`     = **当前可写** binlog 文件名（轮转后为 `binlog.00000N`；
          旧文件只读保留，可用 `SHOW BINLOG EVENTS IN '...'` 读取）
        * `Position` = **该文件内**的真实字节位点（文件末尾，即下一条记录
          的起始偏移；MySQL 语义。单调递增，不随任何表 WAL 的
          checkpoint/ALTER 回退；跨文件的全局位点在 `__BINLOG_FETCH__.lsn`）
        * `Binlog_Do_DB` / `Binlog_Ignore_DB` = ''（未配置过滤，MySQL 同）
        * `Executed_Gtid_Set` = NULL —— **决策：不实现 GTID**，复制按
          "文件 + 位点"进行（不冒充 MySQL 的 GTID 语义）

        binlog 未启用（`LIGHTDB_BINLOG_ENABLED=0`）时返回**空结果集**
        （MySQL `log_bin=OFF` 时 SHOW MASTER STATUS 也是空结果集）。
        """
        cols = ['File', 'Position', 'Binlog_Do_DB', 'Binlog_Ignore_DB',
                'Executed_Gtid_Set']
        rec = self._binlog_reader()
        if rec is None:
            return self._rs(cols, [])
        try:
            return self._rs(cols, [(rec.current_file, rec.current_pos,
                                    '', '', None)])
        except Exception as e:
            return self._err(1105, f"读取 binlog 位点失败: {e}")

    #: MySQL `SHOW SLAVE STATUS` 的列清单（**严格按文档顺序**）
    #
    #: 来源：MySQL 5.7 Reference Manual 13.7.5.34「SHOW SLAVE STATUS
    #: Statement」里 `\G` 的完整输出（57 列，从 Slave_IO_State 到
    #: Master_TLS_Version；Master_TLS_Version 自 5.7.10 起）。MySQL 8.0
    #: 只在其后追加了 `Master_public_key_path` / `Get_master_public_key`
    #: （8.0.19）与 `Network_Namespace`（8.0.22），前 57 列的名称与顺序
    #: 与 5.7 完全相同，因此这里以 5.7 的实证清单为准（8.0 追加的三列本
    #: 引擎不输出，已在交付说明里登记）。
    #:
    #: ★ 取值来源（语义为真）：本进程登记了从库复制器时，动态列全部取
    #: `replication/slave.py::SlaveReplicator` 自报的**真实**状态与位点
    #: —— 复制器在构造时把自己注册到 `Database`（`db/database.py::
    #: attach_replicator`），执行器经 `Database.replication_status()` 拿到
    #: 快照（见 `_slave_status_values`）。没有登记复制器 = 本实例没有配置
    #: 复制，此时如实报告 `'No'` / `'No'` / `'No replication configured'` +
    #: 连接与位点类列 NULL —— 这是当时的真实状态，不是"退化降级"。
    #: 两个运行标志按 MySQL 文本语义 `'Yes'` / `'No'` / `'Connecting'`。
    _SLAVE_STATUS_COLUMNS = (
        'Slave_IO_State', 'Master_Host', 'Master_User', 'Master_Port',
        'Connect_Retry', 'Master_Log_File', 'Read_Master_Log_Pos',
        'Relay_Log_File', 'Relay_Log_Pos', 'Relay_Master_Log_File',
        'Slave_IO_Running', 'Slave_SQL_Running',
        'Replicate_Do_DB', 'Replicate_Ignore_DB',
        'Replicate_Do_Table', 'Replicate_Ignore_Table',
        'Replicate_Wild_Do_Table', 'Replicate_Wild_Ignore_Table',
        'Last_Errno', 'Last_Error', 'Skip_Counter', 'Exec_Master_Log_Pos',
        'Relay_Log_Space', 'Until_Condition', 'Until_Log_File',
        'Until_Log_Pos', 'Master_SSL_Allowed', 'Master_SSL_CA_File',
        'Master_SSL_CA_Path', 'Master_SSL_Cert', 'Master_SSL_Cipher',
        'Master_SSL_Key', 'Seconds_Behind_Master',
        'Master_SSL_Verify_Server_Cert', 'Last_IO_Errno', 'Last_IO_Error',
        'Last_SQL_Errno', 'Last_SQL_Error', 'Replicate_Ignore_Server_Ids',
        'Master_Server_Id', 'Master_UUID', 'Master_Info_File', 'SQL_Delay',
        'SQL_Remaining_Delay', 'Slave_SQL_Running_State',
        'Master_Retry_Count', 'Master_Bind', 'Last_IO_Error_Timestamp',
        'Last_SQL_Error_Timestamp', 'Master_SSL_Crl', 'Master_SSL_Crlpath',
        'Retrieved_Gtid_Set', 'Executed_Gtid_Set', 'Auto_Position',
        'Replicate_Rewrite_DB', 'Channel_name', 'Master_TLS_Version',
    )

    def _slave_status_values(self):
        """逐列取值（键 = 列名）。没有登记复制器时，下面这份字面量就是
        **真实的"未配置复制"取值**；登记了复制器时由它的真实快照覆盖。

        未配置复制（`Database.replication_status()` 返回 None）时的依据：

        * Slave_IO_State → 'No replication configured'：一句真实的说明
          文本（本实例没有主库连接，也就没有 MySQL 那些 IO 线程状态）；
        * Slave_IO_Running / Slave_SQL_Running → 'No'：真实的运行标志
          （没有复制线程在跑；MySQL 在对应线程未运行时同样不是 Yes）；
        * 连接 / 位点 / 中继日志 / 状态文本类列（Master_Host、Master_User、
          Master_Port、Connect_Retry、Master_Log_File、Read_Master_Log_Pos、
          Relay_Log_File、Relay_Log_Pos、Relay_Master_Log_File、
          Exec_Master_Log_Pos、Relay_Log_Space、Master_Server_Id、
          Master_Info_File、Master_Retry_Count、Slave_SQL_Running_State、
          Seconds_Behind_Master）→ NULL：本实例没有配置复制，无从得知
          对端信息与位点（MySQL 在这些值未知时同样是 NULL）；
        * Master_UUID → NULL：本引擎没有 server_uuid 概念，不编造。
        * 复制过滤列表（Replicate_Do_DB / ... / Replicate_Rewrite_DB）、
          Master_Bind、Master_SSL_* 文件路径 → ''：
          MySQL 未配置这些选项时就是空串。
        * Last_Errno / Last_IO_Errno / Last_SQL_Errno → 0，Last_Error /
          Last_IO_Error / Last_SQL_Error / 两个 *_Error_Timestamp → ''：
          文档明确「错误号 0 + 空串 = 没有错误」，与「无复制线程」一致。
        * Until_Condition → 'None'、Until_Log_File → ''、Until_Log_Pos → 0：
          没有 START SLAVE UNTIL；'None' 是文档给出的取值。
        * Master_SSL_Allowed / Master_SSL_Verify_Server_Cert → 'No'：
          文档取值，且本引擎的复制实现没有到主库的 TLS（No = 不允许）。
        * SQL_Delay → 0（没有 MASTER_DELAY）、SQL_Remaining_Delay → NULL
          （文档：只在等待延迟时才有值）、Auto_Position → 0（没有 GTID
          自动定位）。
        * Retrieved_Gtid_Set / Executed_Gtid_Set → NULL：本引擎没有 GTID
          概念（与 `SHOW MASTER STATUS` 的 Executed_Gtid_Set = NULL 一致）。
        * Channel_name → ''：MySQL 默认复制通道名为空串。

        登记了复制器时，**动态列**用 `SlaveReplicator.show_status()` 的真实
        快照整体覆盖（运行标志 'Yes'/'No'/'Connecting'、主库连接、真实
        位点、真实错误、状态文本、Seconds_Behind_Master），逐列来源见该
        方法自己的注释；确实取不到的列（GTID / server_uuid / master.info /
        事件时间戳）依旧是 None 并写明原因。
        """
        values = {
            'Slave_IO_State': 'No replication configured',
            'Master_Host': None,
            'Master_User': None,
            'Master_Port': None,
            'Connect_Retry': None,
            'Master_Log_File': None,
            'Read_Master_Log_Pos': None,
            'Relay_Log_File': None,
            'Relay_Log_Pos': None,
            'Relay_Master_Log_File': None,
            'Slave_IO_Running': 'No',   # 未配置复制：IO 线程没在跑
            'Slave_SQL_Running': 'No',  # 未配置复制：SQL 线程没在跑
            'Replicate_Do_DB': '',
            'Replicate_Ignore_DB': '',
            'Replicate_Do_Table': '',
            'Replicate_Ignore_Table': '',
            'Replicate_Wild_Do_Table': '',
            'Replicate_Wild_Ignore_Table': '',
            'Last_Errno': 0,            # Last_SQL_Errno 的别名（无错误）
            'Last_Error': '',
            'Skip_Counter': 0,
            'Exec_Master_Log_Pos': None,
            'Relay_Log_Space': None,
            'Until_Condition': 'None',  # 文档取值：没有 UNTIL 子句
            'Until_Log_File': '',
            'Until_Log_Pos': 0,
            'Master_SSL_Allowed': 'No',
            'Master_SSL_CA_File': '',
            'Master_SSL_CA_Path': '',
            'Master_SSL_Cert': '',
            'Master_SSL_Cipher': '',
            'Master_SSL_Key': '',
            'Seconds_Behind_Master': None,   # SQL 线程未运行 -> NULL
            'Master_SSL_Verify_Server_Cert': 'No',
            'Last_IO_Errno': 0,
            'Last_IO_Error': '',
            'Last_SQL_Errno': 0,
            'Last_SQL_Error': '',
            'Replicate_Ignore_Server_Ids': '',
            'Master_Server_Id': None,
            'Master_UUID': None,        # 本引擎没有 server_uuid 概念
            'Master_Info_File': None,
            'SQL_Delay': 0,
            'SQL_Remaining_Delay': None,
            'Slave_SQL_Running_State': None,
            'Master_Retry_Count': None,
            'Master_Bind': '',
            'Last_IO_Error_Timestamp': '',
            'Last_SQL_Error_Timestamp': '',
            'Master_SSL_Crl': '',
            'Master_SSL_Crlpath': '',
            'Retrieved_Gtid_Set': None,     # 没有 GTID 概念
            'Executed_Gtid_Set': None,
            'Auto_Position': 0,
            'Replicate_Rewrite_DB': '',
            'Channel_name': '',
            'Master_TLS_Version': '',
        }

        # ★ 动态列覆盖：本实例登记了从库复制器就用它自报的真实状态
        #   （`SlaveReplicator.show_status()`，逐列来源见其注释）；
        #   没有登记复制器时，上面那份字面量就是真实的"未配置复制"取值
        #   （'No' / 'No' / 'No replication configured' + 连接与位点类 NULL）。
        status = None
        getter = getattr(self.db, 'replication_status', None)
        if callable(getter):
            status = getter()
        if status is not None:
            values.update(status)
        return values

    def _show_slave_status(self, p, s):
        """SHOW SLAVE STATUS / SHOW REPLICA STATUS —— 严格按 MySQL 列顺序

        语义为真：本实例登记了从库复制器就报它的真实运行状态 / 主库连接 /
        真实位点 / 最后一次真实错误（见 `SlaveReplicator.show_status()` 的
        逐列来源）；没有登记复制器就报真实的"未配置复制"（两个运行标志
        'No'、`Slave_IO_State='No replication configured'`、连接与位点类列
        NULL）。列清单与逐列依据见 `_SLAVE_STATUS_COLUMNS` /
        `_slave_status_values`。
        """
        values = self._slave_status_values()
        row = tuple(values[c] for c in self._SLAVE_STATUS_COLUMNS)
        return self._rs(list(self._SLAVE_STATUS_COLUMNS), [row])

    def _show_binlog_events(self, p, s):
        """SHOW BINLOG EVENTS —— 逐条真实记录（含事务边界/格式事件）

        列保持 MySQL 兼容形态：`Log_name / Pos / Event_type / Server_id /
        End_log_pos / Info`。`Pos`/`End_log_pos` 是**该文件内**的真实字节偏移
        （MySQL 语义）；`Info` 是一行真实摘要（表名/行镜像/SQL/事务号），
        不是占位文本。

        支持 MySQL 的可选子句（parser 解析进 AST）：
        ``SHOW BINLOG EVENTS [IN 'file'] [FROM pos] [LIMIT n]``：

        * 不写 `IN` → 读**当前可写文件**；
        * `IN 'binlog.00000N'` → 读指定历史文件（轮转后旧文件只读保留，
          位点 `FROM pos` 是该文件内的偏移）；
        * 文件不存在 → 1105 显式报错（不悄悄换成别的文件）。

        文件损坏/截断 → 1105 显式报错（拒绝静默跳过）。
        """
        cols = ['Log_name', 'Pos', 'Event_type', 'Server_id',
                'End_log_pos', 'Info']
        rec = self._binlog_reader()
        if rec is None:
            return self._rs(cols, [])
        requested_file = p.get('binlog_file') or rec.current_file
        if requested_file not in rec.file_names():
            return self._err(
                1105, f"SHOW BINLOG EVENTS IN '{requested_file}' 失败："
                      f"binlog 文件不存在（现有: "
                      f"{', '.join(rec.file_names())}）")
        start = p.get('from_pos')
        try:
            start = int(start) if start is not None else 16
        except (TypeError, ValueError):
            return self._err(1064, f"SHOW BINLOG EVENTS 的 FROM 位点非法: "
                                   f"{p.get('from_pos')!r}")
        limit = p.get('limit')
        try:
            limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit = None
        try:
            records = rec.read_file(requested_file, max(16, start), limit)
        except Exception as e:
            return self._err(
                1105, f"读取 binlog 失败（拒绝静默跳过损坏记录）: {e}")
        rows = [
            (requested_file, r.pos, r.type_name, r.server_id,
             r.end_pos, r.summary())
            for r in records
        ]
        return self._rs(cols, rows)

    def _show_engine(self, p, s):
        """SHOW ENGINE <name> [STATUS|MUTEX]

        只支持本引擎真实拥有的内建存储引擎（INNODB 是引擎名的兼容叫法）。
        其它引擎名、以及 MUTEX（InnoDB 互斥量/信号量列表）在本引擎没有对应
        概念 → 显式报错，绝不返回空结果集糊弄。
        """
        engine = str(p.get('engine') or 'INNODB').upper()
        kind = str(p.get('engine_kind') or 'STATUS').upper()
        if engine not in ('INNODB', 'DEFAULT'):
            return self._err(
                1105, f"SHOW ENGINE {engine} 不支持：本引擎只有内建存储引擎"
                      f"（无 MyISAM/NDB 等可插拔引擎）")
        if kind != 'STATUS':
            return self._err(
                1105, f"SHOW ENGINE INNODB {kind} 不支持：本引擎没有 "
                      f"InnoDB 互斥量 / 信号量概念")
        return self._rs(['Type', 'Name', 'Status'],
                        [('InnoDB', '', self._innodb_status_text())])

    def _innodb_status_text(self):
        """真实存储统计拼成的引擎状态文本（不是固定字符串）"""
        lines = [
            '=====================================',
            f'{time.strftime("%Y-%m-%d %H:%M:%S")} INNODB MONITOR OUTPUT',
            '=====================================',
            f'Data directory: {self.db.data_dir}',
            f'Tables: {len(self.db.tables)}',
            '--- BUFFER POOL / TABLE STATS ---',
        ]
        for name in sorted(self.db.tables):
            tbl = self.db.tables[name]
            try:
                st = tbl.stats()
                pages = st.get('pages')
                rows = st.get('rows')
                height = st.get('btree_height')
            except Exception as e:
                lines.append(f'TABLE {name}: stats 读取失败: {e}')
                continue
            try:
                wal_records = len(tbl.wal.read_all())
            except Exception:
                wal_records = None
            lines.append(
                f'TABLE {name}: rows={rows} heap_pages={pages} '
                f'btree_height={height} wal_records={wal_records} '
                f'pk={tbl.pk_col} cols={len(tbl.cols)}')
        brec = self._binlog_reader()
        lines.append('--- LOG ---')
        if brec is None:
            lines.append('binlog: 未启用（LIGHTDB_BINLOG_ENABLED=0）')
        else:
            try:
                # 保持旧的 Info 行名（Log records (data events)），但数值
                # 现在来自**真 append-only binlog** 而不是各表 WAL 扫描
                lines.append(
                    f'Log records (data events): {brec.data_event_count}')
                lines.append(f'binlog file: {brec.current_file}')
                lines.append(
                    f'binlog position (bytes): {brec.current_pos}')
            except Exception as e:
                lines.append(f'binlog: 读取失败 ({e})')
        lines.append('--- TRANSACTIONS / LOCKS ---')
        try:
            lines.append(f'MVCC: {self.db.mvcc.stats()}')
        except Exception as e:
            lines.append(f'MVCC: 不可用 ({e})')
        try:
            lines.append(f'LOCK: {self.db.lock_manager.stats()}')
        except Exception as e:
            lines.append(f'LOCK: 不可用 ({e})')
        lines.append('--- SERVER ---')
        lines.append(f'Queries (this executor): {self.stats.get("queries")}')
        lines.append(f'Rows written (this executor): {self.stats.get("rows")}')
        return '\n'.join(lines)

    def _show_audit_log(self, p, s):
        """SHOW AUDIT LOG —— 真实语句审计（见 _AuditSink）

        这是本引擎的扩展语句（MySQL 本身没有 SHOW AUDIT LOG；MySQL 企业版
        审计要查 performance_schema.audit_log）。列形状按本仓库用例的契约：
        (ts, user, cmd, success, elapsed_ms, sql) —— tests/test_batch14.py::
        test_audit 按 r[2]=cmd、r[5]=sql 取值。
        """
        if self.audit is None:
            return self._err(1105, "审计日志不可用")
        rows = self.audit.rows(p.get('limit'))
        return self._rs(
            ['ts', 'user', 'cmd', 'success', 'elapsed_ms', 'sql'], rows)

    # ---------- CHECK / OPTIMIZE TABLE（真去校验存储） ----------

    def _check_one_table(self, name):
        """真正校验表结构与索引：逐个主键定位堆行并解码

        **列顺序 = MySQL 原生顺序 (Table, Op, Msg_type, Msg_text)**：
        `Op` 是 'check'，`Msg_type` 是 'status' / 'info' / 'error'，
        `Msg_text` 是人类可读文本（见 `_CHECK_COLUMNS`）。
        """
        if name not in self.db.tables:
            return (name, 'check', 'error', f"Table '{name}' doesn't exist")
        tbl = self.db.tables[name]
        try:
            if not tbl.cols:
                raise RuntimeError('表没有列定义')
            if not any(c.get('primary') for c in tbl.cols):
                raise RuntimeError('表没有主键（索引无法校验）')
            n_idx = 0
            seen = set()
            for key, page_id, slot_id in tbl.bt.scan():
                n_idx += 1
                if key in seen:
                    raise RuntimeError(f'索引里主键 {key} 重复')
                seen.add(key)
                page = tbl.bp.get_page(page_id)
                rb = page.get_row(slot_id) if page is not None else None
                if rb is None:
                    raise RuntimeError(
                        f'主键 {key} 的索引项指向空槽位 '
                        f'(page={page_id}, slot={slot_id})')
                if tbl._decode_row_ver(rb) is None:
                    raise RuntimeError(
                        f'主键 {key} 的行字节无法解码 '
                        f'(page={page_id}, slot={slot_id})')
            note = (f'{n_idx} 个索引项全部可定位并解码，'
                    f'可见行数 {tbl.row_count}')
        except Exception as e:
            return (name, 'check', 'error', f'校验失败: {e}')
        return [(name, 'check', 'status', 'OK'),
                (name, 'check', 'info', note)]

    #: CHECK TABLE / OPTIMIZE TABLE 的列清单（**MySQL 原生顺序**）
    #:
    #: 来源：MySQL 5.7/8.0 Reference Manual「CHECK TABLE Statement」
    #: （13.7.2.2）与「OPTIMIZE TABLE Statement」（13.7.2.4）：两者输出同样的
    #: 4 列 `Table / Op / Msg_type / Msg_text`，CHECK 的 `Op` 固定 'check'、
    #: OPTIMIZE 的 `Op` 固定 'optimize'，`Msg_type` 取 'status'（结果状态
    #: 文本 'OK'/'error'）、'info'（说明）、'error'（失败原因）。
    _CHECK_COLUMNS = ['Table', 'Op', 'Msg_type', 'Msg_text']

    def _show_check_table(self, p, s):
        rows = []
        for name in p.get('tables') or []:
            got = self._check_one_table(name)
            if isinstance(got, list):
                rows.extend(got)
            else:
                rows.append(got)
        return self._rs(list(self._CHECK_COLUMNS), rows)

    #: `VACUUM` 结果集列（真实数字，不编造）
    _VACUUM_COLUMNS = ['Table', 'Reclaimed_versions', 'Relocated_rows',
                       'Pages_before', 'Pages_after', 'Trimmed_pages',
                       'Free_pages', 'Free_bytes', 'Dat_before', 'Dat_after',
                       'Shrink_ratio', 'Msg_type', 'Msg_text']

    def _show_vacuum(self, p, s):
        """
        `VACUUM [TABLE t1, t2]` —— 版本回收 + **页级回收**（真正的磁盘空间回收）。

        与 `OPTIMIZE TABLE` 的区别（两条语句都保留，职责不重叠）:
          * `OPTIMIZE TABLE`: 回收历史版本 + 页内紧凑化 + 刷盘 + 刷新统计；
          * `VACUUM`:        **额外**做"前向搬页 + 重建 B+Tree + 截断 `.dat`"，
            并把空闲页登记表落盘 —— 会改变页号（索引与 prev 链同步改写），
            是本引擎最重的空间回收动作。

        安全门: 若表上存在**未提交事务**（`Table._read_views` 非空），
        一律拒绝（返回 error 行），因为搬页会改变行位置，而未提交事务的
        物理 undo 镜像记的是"原页原槽位" —— 搬完就回滚不回去了。
        这正是 `Table.vacuum` 的 horizon 判定所保护的同一件事，
        这里额外显式挡一道并如实报告。
        """
        rows = []
        names = p.get('tables') or sorted(self.db.tables.keys())
        for name in names:
            if name not in self.db.tables:
                rows.append((name, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', 'error',
                             f"Table '{name}' doesn't exist"))
                continue
            tbl = self.db.tables[name]
            if getattr(tbl, '_read_views', None):
                rows.append((name, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', 'error',
                             '存在未提交事务，拒绝回收（先 COMMIT/ROLLBACK）'))
                continue
            before = tbl.bp.file_size()
            try:
                reclaimed = tbl.vacuum()
                tbl.flush()
            except Exception as e:
                rows.append((name, 0, 0, 0, 0, 0, 0, 0, before, before, '',
                             'error', f'回收失败: {e}'))
                continue
            st = tbl.vacuum_stats()
            fs = tbl.free_page_stats()
            after = st.get('bytes_after', tbl.bp.file_size())
            ratio = (1 - after / before) if before else 0.0
            rows.append((
                name, reclaimed, st.get('relocated_rows', 0),
                st.get('pages_before', 0), st.get('pages_after', 0),
                st.get('trimmed_pages', 0), fs['registered_pages'],
                fs['free_bytes'], before, after, f'{ratio * 100:.1f}%',
                'status',
                f"回收 {reclaimed} 个版本，页内释放 "
                f"{st.get('compacted_bytes', 0)} 字节，"
                f"索引页 {st.get('btree_rebuild', {}).get('old_index_pages', 0)}"
                f" → {st.get('btree_rebuild', {}).get('new_index_pages', 0)}，"
                f"空闲页 {fs['registered_pages']} 个"))
        return self._rs(list(self._VACUUM_COLUMNS), rows)

    def _show_optimize_table(self, p, s):
        """OPTIMIZE TABLE —— 真做回收 + 刷盘 + 刷新统计（列序同 CHECK TABLE）"""
        rows = []
        horizon = None
        try:
            horizon = self.db.mvcc.oldest_active_txn()
        except Exception:
            horizon = None
        for name in p.get('tables') or []:
            if name not in self.db.tables:
                rows.append(
                    (name, 'optimize', 'error',
                     f"Table '{name}' doesn't exist"))
                continue
            tbl = self.db.tables[name]
            try:
                reclaimed = tbl.vacuum(horizon)
                tbl.flush()
                freed = getattr(tbl, 'last_vacuum_freed_bytes', 0)
                analyzed = ''
                if self.stats_manager is not None:
                    try:
                        self.stats_manager.analyze_table(tbl)
                        self.stats_manager.save()
                        analyzed = '，统计已刷新'
                    except Exception as e:
                        analyzed = f'，统计刷新失败: {e}'
            except Exception as e:
                rows.append((name, 'optimize', 'error', f'优化失败: {e}'))
                continue
            rows.append((name, 'optimize', 'status', 'OK'))
            rows.append((name, 'optimize', 'info',
                         f'回收 {reclaimed} 个历史版本，页内释放 {freed} '
                         f'字节可复用空间，脏页已刷盘{analyzed}'))
        return self._rs(list(self._CHECK_COLUMNS), rows)

    def _show_create_table(self, p, s):
        """`SHOW CREATE TABLE t` —— 真实 DDL，**含 CHECK 约束**

        两列（`Table` / `Create Table`），列名与 MySQL 一致；DDL 文本不是
        在这里拼的，而是走 `sql/logical_dump.py::check_ddl` —— 那是
        `TableSchema.canonical_ddl` 的**同一份实现**，逻辑 dump / 备份 /
        恢复校验都从它出发。原因就是本批要修的那类缺陷：DDL 生成一旦有
        两条路径，`SHOW CREATE TABLE` 就会输出一份"看起来对、但缺约束"
        的表结构，而备份路径另写一份（CHECK 丢失就是这么发生的）。

        表不存在 → 1146（`Unknown table`），与 MySQL 一致，不返回空结果集。
        """
        from .logical_dump import BackupError, check_ddl
        name = p.get('table')
        try:
            ddl = check_ddl(self.db, name)
        except BackupError as e:
            return self._err(1146, f"Table '{name}' doesn't exist: {e}")
        except CheckConstraintError as e:               # 约束定义坏了
            return self._err(getattr(e, 'errno', 3819), str(e),
                             getattr(e, 'sqlstate', None))
        ddl = self._enhance_show_create_ddl(self.db.get_table(name), ddl)
        return self._rs(['Table', 'Create Table'], [(name, ddl)])

    @staticmethod
    def _render_partition_spec(spec) -> str:
        """把持久化的分区描述渲染成 SHOW CREATE TABLE 的 PARTITION 子句"""
        if not isinstance(spec, dict):
            return ''
        ptype = str(spec.get('type') or '').upper()
        col = spec.get('col') or ''
        if ptype == 'HASH':
            return (f" PARTITION BY HASH({col}) "
                    f"PARTITIONS {int(spec.get('num') or 4)}")
        if ptype == 'RANGE':
            parts = []
            for r in (spec.get('ranges') or []):
                hi = 'MAXVALUE' if r.get('hi') is None else r.get('hi')
                parts.append(f"PARTITION {r.get('name')} VALUES LESS THAN "
                             f"({hi})")
            if parts:
                return (f" PARTITION BY RANGE({col}) ("
                        + ', '.join(parts) + ')')
        if ptype == 'LIST':
            parts = []
            for part in (spec.get('lists') or []):
                vals = ', '.join(str(v) for v in (part.get('values') or []))
                parts.append(f"PARTITION {part.get('name')} VALUES IN "
                             f"({vals})")
            if parts:
                return (f" PARTITION BY LIST({col}) ("
                        + ', '.join(parts) + ')')
        return ''

    def _enhance_show_create_ddl(self, tbl, ddl: str) -> str:
        """E1：SHOW CREATE TABLE 补齐**真实存在但旧渲染丢失**的语义

        `check_ddl` 是逻辑备份/恢复共用的一份 DDL（保证备份往返），但它
        不导出生成列表达式、列注释、表选项、分区子句 —— 于是
        `SHOW CREATE TABLE` 看起来"接受过这些语法却没生效"。
        这里在不改变 check_ddl 备份路径的前提下，把**真实存在于 Table
        元数据里的语义**补回展示文本：
          * 生成列 `GENERATED ALWAYS AS (expr) STORED|VIRTUAL`；
          * 列注释 `COMMENT '...'`；
          * 表选项 `ENGINE=LightDB DEFAULT CHARSET=... COLLATE=...` +
            表注释 + `PARTITION BY ...`。
        这些值都来自实际生效/持久化的元数据，不是编造的。
        """
        import re as _re
        if not isinstance(ddl, str) or not ddl:
            return ddl
        cols = getattr(tbl, 'cols', []) or []
        lines = ddl.splitlines()
        gen_map = {c.get('name'): c.get('generated') for c in cols
                   if isinstance(c, dict) and c.get('generated')}
        comment_map = {c.get('name'): c.get('comment') for c in cols
                       if isinstance(c, dict) and c.get('comment')}
        opts = getattr(tbl, 'table_options', None) or {}
        tcomment = getattr(tbl, 'comment', '') or ''
        partition_spec = getattr(tbl, '_partition_spec', None)
        engine = (getattr(tbl, 'requested_engine', None)
                  or opts.get('engine') or 'LightDB')
        secondary = (getattr(tbl, 'secondary_indexes', lambda: [])() or [])
        # 没有任何"check_ddl 不导出"的真实语义时，保持与 check_ddl
        # **逐字节一致**（既有契约：SHOW CREATE 与逻辑 dump 同一份文本）。
        has_extras = bool(gen_map or comment_map or tcomment
                          or partition_spec
                          or secondary
                          or engine != 'LightDB'
                          or opts.get('comment')
                          or opts.get('user_specified'))
        if not has_extras:
            return ddl
        for i, line in enumerate(lines):
            m = _re.match(r'^(\s*)([`"]?\w+[`"]?)\s', line)
            if not m:
                continue
            cname = m.group(2).strip('`"')
            if cname not in gen_map and cname not in comment_map:
                continue
            body = line.rstrip()
            has_comma = body.endswith(',')
            if has_comma:
                body = body[:-1].rstrip()
            gen = gen_map.get(cname)
            if gen:
                body += (f" GENERATED ALWAYS AS ({gen.get('expr')}) "
                         f"{'STORED' if gen.get('stored') else 'VIRTUAL'}")
            cmt = comment_map.get(cname)
            if cmt:
                body += " COMMENT '" + str(cmt).replace("'", "''") + "'"
            lines[i] = body + (',' if has_comma else '')
        ddl = '\n'.join(lines)

        # E2：非唯一索引声明（`CREATE INDEX` / 内联 KEY）写进 SHOW CREATE，
        # 文本形态可被本引擎重新解析（CREATE TABLE ... KEY name (列)）。
        if secondary:
            key_lines = [
                "  KEY " + str(s.get('name')) + " ("
                + ', '.join(str(c) for c in (s.get('columns') or []))
                + ")"
                for s in secondary]
            close = ddl.rfind(')')
            if close >= 0:
                head = ddl[:close].rstrip()
                if not head.endswith(','):
                    head += ','
                ddl = head + '\n' + ',\n'.join(key_lines) + '\n' + ddl[close:]

        opts = getattr(tbl, 'table_options', None) or {}
        suffix = (f" ENGINE={engine}"
                  f" DEFAULT CHARSET={opts.get('charset') or 'utf8mb4'}"
                  f" COLLATE={opts.get('collate') or 'utf8mb4_bin'}")
        tcomment = getattr(tbl, 'comment', '') or ''
        if tcomment:
            suffix += " COMMENT='" + str(tcomment).replace("'", "''") + "'"
        suffix += self._render_partition_spec(
            getattr(tbl, '_partition_spec', None))
        idx = ddl.rfind(')')
        if idx >= 0:
            ddl = ddl[:idx + 1] + suffix + ddl[idx + 1:]
        return ddl

    #: MySQL 8.0 `SHOW INDEX` 的列清单（顺序照官方文档 13.7.7.22）
    _SHOW_INDEX_COLUMNS = (
        'Table', 'Non_unique', 'Key_name', 'Seq_in_index', 'Column_name',
        'Collation', 'Cardinality', 'Sub_part', 'Packed', 'Null',
        'Index_type', 'Comment', 'Index_comment', 'Visible', 'Expression',
    )

    def _show_index(self, p, s):
        """`SHOW INDEX FROM t` —— 列形状与 MySQL 一致，取值来自真实索引

        数据源是 `Table.index_specs()`，**只报真的存在 / 真的在强制的
        索引**：

        * 主键 B+Tree → `Index_type='BTREE'`；
        * 表级 `UNIQUE KEY/INDEX`（写入路径唯一性检查强制）→
          `Non_unique=0` 是真的，但 `Index_type='UNIQUE-CHECK'`，**不谎称
          BTREE**；`Cardinality` 是真实的不同非 NULL 键值个数；
        * 列级 `col ... UNIQUE`：等价于自动建一个名为列名的唯一索引
          （MySQL 惯例），由写入路径同样强制 → 同样报 `Non_unique=0`，
          `Index_type='UNIQUE-CHECK'`，**不**谎称 BTREE。

        列形状（15 列、顺序、`Key_name`/`Column_name`/`Seq_in_index` 名字）
        保持不变：`lightdb_studio` 的 `health_score._check_indexes` 与
        `sql_auto_optimizer`、`lightdb/monitor/index_advisor` 都按列名读。
        一个索引多列 → 多行（`Seq_in_index` 从 1 递增），与 MySQL 一致；
        表不存在 → 1146（不返回空结果集，免得客户端把"表没了"当成"没索引"）。
        """
        name = p.get('table')
        if name not in self.db.tables:
            return self._err(1146, f"Table '{name}' doesn't exist")
        tbl = self.db.tables[name]
        # `Null` 列按 MySQL 语义报**该行对应列**能否为 NULL（主键隐式
        # NOT NULL）—— 不是按"索引是否唯一"编造。
        null_by_col = {
            c.get('name'): ('NO' if (c.get('notnull') or c.get('primary'))
                            else 'YES')
            for c in (getattr(tbl, 'cols', None) or [])
        }
        rows = []
        for spec in tbl.index_specs():
            for seq, cname in enumerate(spec.get('columns') or [], start=1):
                index_type = spec.get('index_type') or 'BTREE'
                rows.append((
                    name,
                    0 if spec.get('unique') else 1,
                    spec.get('name') or '',
                    seq,
                    cname,
                    'A',                                 # Collation（升序）
                    spec.get('cardinality'),
                    None,                                # Sub_part
                    None,                                # Packed
                    '' if null_by_col.get(cname) == 'NO' else 'YES',
                    index_type,                          # 真实实现形态
                    '',                                  # Comment
                    '',                                  # Index_comment
                    'YES',                               # Visible
                    None,                                # Expression
                ))
        return self._rs(list(self._SHOW_INDEX_COLUMNS), rows)

    def _show_partitions(self, p, s):
        """SHOW PARTITIONS FROM t —— 读真实的 Table._partition 元数据

        未分区表返回空集（与 MariaDB 的 SHOW PARTITIONS 一致）。列名沿用
        MariaDB：Database / Table / Partition / Method / Expression /
        Partition_description。
        """
        name = p.get('table')
        tbl = self.db.get_table(name)
        pm = getattr(tbl, '_partition', None)
        cols = ['Database', 'Table', 'Partition', 'Method', 'Expression',
                'Partition_description']
        if pm is None or not getattr(pm, 'partitions', None):
            return self._rs(cols, [])
        rows = []
        for part in pm.partitions:
            if part.ptype == 'RANGE':
                lo, hi = part.value
                desc = (f'LESS THAN '
                        f'({"MAXVALUE" if hi is None else hi})')
            elif part.ptype == 'HASH':
                desc = f'HASH({pm.pk_col}) % {part.value} = {part._hash_index}'
            else:
                desc = f'VALUES IN ({part.value})'
            rows.append((self.db.name, name, part.name, part.ptype,
                         pm.pk_col, desc))
        return self._rs(cols, rows)

    # ---------- 安全面：白名单 / 口令策略 / 用户 ----------

    def _show_ip_whitelist(self, p, s):
        """SHOW IP WHITELIST（本引擎扩展语句；MySQL 无此语句）"""
        entries = []
        if self.ip_whitelist is not None:
            entries = self.ip_whitelist.list_all()
        return self._rs(['Entry'], [(e,) for e in entries])

    def _show_grants(self, p, s):
        """`SHOW GRANTS [FOR '<user>'[@'host']]` —— 真实的授权账户记录

        输出列名与 MySQL 一致：`Grants for <user>@<host>`。非管理员只能查
        自己（FOR 别人在执行期鉴权里已被 1142 拒绝）；root/管理员可查任意
        账户（包括自己的 ALL）。
        """
        from ..security.privileges import show_grants
        users = getattr(self.db, 'users', None) or {}
        actor = self._session_user(s)
        target = p.get('grant_user') or actor or 'root'
        record = users.get(target)
        if not isinstance(record, dict):
            return self._err(
                1141,
                f"There is no such grant defined for user '{target}' "
                f"on host '%'",
                sqlstate='42000')
        host = str(p.get('grant_host')
                   or record.get('host') or '%')
        rows = [(line,) for line in show_grants(users, target)]
        return self._rs([f"Grants for {target}@{host}"], rows)

    def _show_security(self, p, s):
        """SHOW SECURITY —— 真实安全配置快照 (Name, Value)"""
        pp = self.password_policy
        wl = self.ip_whitelist
        rows = []

        def _val(v):
            # 统一转成文本：Value 列混排 int/bool 时，服务端按第一行推断类型
            # （int）会把 'False' 当数字解析，pymysql 直接报
            # ValueError: invalid literal for int()。MySQL 的 SHOW 系列
            # 本来也是文本值。
            if isinstance(v, bool):
                return 'ON' if v else 'OFF'
            return '' if v is None else str(v)

        if pp is not None:
            for k, v in pp.to_dict().items():
                rows.append((f'password_policy.{k}', _val(v)))
        else:
            rows.append(('password_policy', ''))
        rows.append(('authentication_plugin', 'mysql_native_password'))
        rows.append(('password_storage', 'sha1(sha1(password)) 十六进制，不落明文'))
        rows.append(('ip_whitelist.enabled',
                     _val(bool(wl is not None and wl.enabled))))
        rows.append(('ip_whitelist.entries',
                     ','.join(wl.list_all()) if wl is not None else ''))
        rows.append(('audit.level',
                     self.audit.level_name if self.audit is not None else ''))
        rows.append(('audit.records',
                     _val(len(self.audit.recent)
                          if self.audit is not None else 0)))
        rows.append(('users', _val(len(self.db.users))))
        rows.append(('user_list', ','.join(sorted(self.db.users))))
        return self._rs(['Name', 'Value'], rows)

    def _check_password_policy(self, user, password):
        """弱口令校验 → (是否通过, 错误说明)

        使用与 `SHOW SECURITY` 同一份策略对象（`SET PASSWORD POLICY` 改的就是
        它），空密码与 `IDENTIFIED BY NULL`（= 无密码账户）不参与强度校验 ——
        MySQL 的 validate_password 同样只对「提供了口令」的账号做校验。
        """
        pp = self.password_policy
        if pp is None:
            return True, None
        if password is None:
            return True, None
        ok, err = pp.validate(password)
        return ok, err

    def _cmd_grant(self, p, s):
        """`GRANT <privs> ON <*.*|db.*> TO '<user>'[@'host']`（DCL）

        只有管理员能到这里（`_require_privilege` 已按 ALL/root 拦截）。
        权限写入 `db.users[user]['privs']` 并随 `_meta.json` 持久化 ——
        与握手共用同一份用户表，重开 Database 后仍然生效。
        """
        from ..security.privileges import grant_privileges
        database = p.get('database') or '*'
        if database != '*':
            if not self.db.is_known_database(database):
                return self._err(
                    1049, f"Unknown database '{database}'",
                    sqlstate='42000')
        updated = 0
        for acct in p.get('accounts') or []:
            user = acct.get('user')
            host = acct.get('host') or '%'
            record = self.db.users.get(user)
            if not isinstance(record, dict):
                # MySQL 8：GRANT 不会隐式建号
                return self._err(
                    1410,
                    f"You are not allowed to create a user with GRANT"
                    f"（用户 '{user}'@'{host}' 不存在，请先 CREATE USER）",
                    sqlstate='42000')
            try:
                updated += grant_privileges(
                    self.db.users, user, p.get('privs') or [], database)
            except ValueError as e:
                return self._err(1064, str(e))
            if not record.get('host'):
                record['host'] = host
        try:
            self.db.save_meta()
        except Exception as e:
            return self._err(1105, f"保存权限失败: {e}")
        return self._ok(
            message=f"Query OK, 0 rows affected "
                    f"（{updated} privilege(s) granted）")

    def _cmd_revoke(self, p, s):
        """`REVOKE <privs> ON <*.*|db.*> FROM '<user>'[@'host']`（DCL）"""
        from ..security.privileges import revoke_privileges
        database = p.get('database') or '*'
        if database != '*':
            if not self.db.is_known_database(database):
                return self._err(
                    1049, f"Unknown database '{database}'",
                    sqlstate='42000')
        privs = p.get('privs') or []
        removed = 0
        for acct in p.get('accounts') or []:
            user = acct.get('user')
            host = acct.get('host') or '%'
            record = self.db.users.get(user)
            if not isinstance(record, dict):
                return self._err(
                    1141,
                    f"There is no such grant defined for user '{user}' "
                    f"on host '{host}'",
                    sqlstate='42000')
            if (str(user) == 'root' and database == '*'
                    and 'ALL' in privs):
                # 防止把管理员自己锁死（root 是最后的救援入口）
                return self._err(
                    1227,
                    "不允许回收 root 的全局 ALL 权限"
                    "（root 是引擎的救援账户；如需降权请直接修改用户表）",
                    sqlstate='42000')
            try:
                removed += revoke_privileges(
                    self.db.users, user, privs, database)
            except ValueError as e:
                return self._err(1064, str(e))
        try:
            self.db.save_meta()
        except Exception as e:
            return self._err(1105, f"保存权限失败: {e}")
        return self._ok(
            message=f"Query OK, 0 rows affected "
                    f"（{removed} privilege(s) revoked）")

    def _cmd_create_user(self, p, s):
        """CREATE USER ... IDENTIFIED BY '...'（DCL）

        口令策略**真实生效**：不满足 `self.password_policy` 直接返回
        MySQL 的 ER_NOT_VALID_PASSWORD(1819) 错误（见交付说明的错误原文），
        绝不静默降级成"创建成功"。密码只存
        `sha1(sha1(password))` 的十六进制（`db.hash_native_password`，
        与 `net/mysql_protocol.hash_password` 同一算法），登录校验因此可用。
        """
        from ..db.database import hash_native_password
        created, existed = [], []
        for acct in p.get('accounts') or []:
            user = acct.get('user')
            host = acct.get('host') or '%'
            pwd = acct.get('password')
            if user in self.db.users:
                if p.get('if_not_exists'):
                    existed.append(user)
                    continue
                return self._err(
                    1396, f"Operation CREATE USER failed for "
                          f"'{user}'@{host}：用户已存在")
            if acct.get('has_ident'):
                ok, err = self._check_password_policy(user, pwd)
                if not ok:
                    return self._err(
                        1819, f"Your password does not satisfy the current "
                              f"policy requirements（用户 '{user}'@{host}："
                              f"{err}）")
            self.db.users[user] = {
                'auth_hash': hash_native_password(pwd) if pwd else '',
                'host': host,
                'privs': [],
            }
            created.append(f"'{user}'@{host}")
        try:
            self.db.save_meta()
        except Exception as e:
            return self._err(1105, f"保存用户失败: {e}")
        if existed:
            return self._ok(
                message=f"User(s) already exist: {', '.join(existed)}")
        return self._ok(message=f"User(s) created: {', '.join(created)}")

    def _cmd_drop_user(self, p, s):
        gone, missing = [], []
        for acct in p.get('users') or []:
            user = acct.get('user')
            if user not in self.db.users:
                missing.append(user)
                continue
            if user == 'root':
                return self._err(1396, "Operation DROP USER failed：不允许删除 root")
            self.db.users.pop(user, None)
            gone.append(user)
        if missing:
            return self._err(
                1396, f"Operation DROP USER failed：用户不存在 "
                      f"{', '.join(missing)}")
        try:
            self.db.save_meta()
        except Exception as e:
            return self._err(1105, f"保存用户失败: {e}")
        return self._ok(message=f"User(s) dropped: {', '.join(gone)}")

    def _cmd_set_password_policy(self, p, s):
        """SET PASSWORD POLICY k = v [, ...]（本引擎扩展语句）

        注意：经 pymysql 连接时 `net/server.py` 会把任何 `SET ...` 直接回
        OK 包（字符集/autocommit 协商需要），因此这条语句只有走
        `Executor.execute_sql` 才会到达这里；级别也可用环境变量
        `LIGHTDB_PASSWORD_MIN_LENGTH` 等在启动时配置。这一点在交付说明里
        已登记，不是静默忽略（执行器收到就一定生效）。
        """
        pp = self.password_policy
        if pp is None:
            return self._err(1105, "密码策略不可用")
        bools = ('require_upper', 'require_lower', 'require_digit',
                 'require_symbol', 'deny_weak')
        ints = ('min_length', 'max_age_days')
        applied = []
        for key, raw in (p.get('options') or {}).items():
            k = str(key).lower()
            if k in bools:
                val = str(raw).strip().upper() in ('1', 'ON', 'TRUE', 'YES')
            elif k in ints:
                if not re.match(r'^\d+$', str(raw).strip()):
                    return self._err(
                        1064, f"SET PASSWORD POLICY {k} 需要整数: {raw}")
                val = int(raw)
            else:
                return self._err(
                    1064, f"SET PASSWORD POLICY 不支持的选项: {k}"
                          f"（支持 {', '.join(ints + bools)}）")
            setattr(pp, k, val)
            applied.append(f'{k}={val}')
        return self._ok(message='Password policy: ' + ', '.join(applied))

    def _cmd_add_ip_whitelist(self, p, s):
        wl = self.ip_whitelist
        if wl is None:
            return self._err(1105, "IP 白名单不可用")
        entry = p.get('entry')
        if not wl.add(entry):
            return self._err(1064, f"IP 白名单条目不合法: {entry}")
        return self._ok(message=f"IP whitelist added: {entry}")

    def _cmd_remove_ip_whitelist(self, p, s):
        wl = self.ip_whitelist
        if wl is None:
            return self._err(1105, "IP 白名单不可用")
        entry = p.get('entry')
        if not wl.remove(entry):
            return self._err(1064, f"IP 白名单条目不合法: {entry}")
        return self._ok(message=f"IP whitelist removed: {entry}")

    # ---------- 全文检索（MATCH ... AGAINST） ----------

    @classmethod
    def _where_has_match(cls, where) -> bool:
        if isinstance(where, list):
            return any(cls._where_has_match(w) for w in where)
        if not isinstance(where, dict):
            return False
        if where.get('op') == 'MATCH':
            return True
        for key in ('and', 'or'):
            if cls._where_has_match(where.get(key)):
                return True
        return cls._where_has_match(where.get('not'))

    def _apply_match(self, p):
        """把 WHERE 里的 `MATCH(列) AGAINST('词')` 换成主键 IN (命中集)

        命中集来自表上的倒排索引（`sql/fulltext.py`），不是 LIKE 近似：
        索引在 CREATE FULLTEXT INDEX 时按现有行建立、之后每次写入 / 删除
        都会维护（见 db/table.py）。
        """
        table = p.get('table')
        if not table or p.get('joins') or p.get('subquery'):
            return self._err(
                1105, "MATCH ... AGAINST 只支持单表查询："
                      "JOIN / FROM 子查询里的全文谓词本引擎未实现")
        try:
            tbl = self.db.get_table(table)
        except KeyError as e:
            return self._err(1146, f"表不存在: {e}")
        try:
            q = dict(p)
            q['where'] = self._rewrite_match_node(p.get('where'), tbl)
        except Exception as e:
            return self._err(1105, f"全文检索失败: {e}")
        return q

    def _rewrite_match_node(self, where, tbl):
        if isinstance(where, list):
            return [self._rewrite_match_node(w, tbl) for w in where]
        if not isinstance(where, dict):
            return where
        if where.get('op') == 'MATCH':
            return self._match_to_pk_in(where, tbl)
        out = dict(where)
        for key in ('and', 'or'):
            if isinstance(out.get(key), list):
                out[key] = [self._rewrite_match_node(w, tbl)
                            for w in out[key]]
        if 'not' in out:
            out['not'] = self._rewrite_match_node(out['not'], tbl)
        return out

    def _match_to_pk_in(self, node, tbl):
        if str(node.get('mode') or '').upper() == 'BOOLEAN':
            raise ValueError(
                "MATCH ... AGAINST(... IN BOOLEAN MODE) 未实现："
                "本引擎只实现自然语言模式（默认），布尔模式的 +/-/通配语义"
                "无法安全近似")
        names = [c['name'] for c in tbl.cols]
        scores = {}
        for col in node.get('columns') or []:
            if col not in names:
                raise ValueError(f"MATCH 里的列 {col} 在表 {tbl.name} 上不存在")
            if not tbl.has_fulltext_index(col):
                raise ValueError(
                    f"列 {col} 上没有全文索引，请先 "
                    f"CREATE FULLTEXT INDEX ... ({col})")
            for pk, sc in tbl.fulltext_search(col, node.get('against')).items():
                scores[pk] = max(scores.get(pk, 0.0), sc)
        return {
            'column': tbl.pk_col,
            'op': 'IN',
            'values': [str(pk) for pk in scores],
        }

    # ---------- 存储过程 / 触发器 ----------

    def _cmd_create_procedure(self, p, s):
        """`CREATE PROCEDURE ...` —— 解析 → 校验 → 落 `_stored.json`

        本轮补上**创建期语法/语义校验**（与 `CREATE FUNCTION` 对齐）：
        过程体先交给 `PLParser` 解析一遍，`SIGNAL SQLSTATE '450'`（不是
        5 个字符）、`END LOOP` 收尾不匹配、尾部多余记号这类问题在
        **创建时**就报 1064，而不是等 CALL 的时候才失败 —— 后者会让
        "过程已创建成功"变成一个假象。
        """
        if self._stored is None:
            return self._err(1105, "存储过程管理器不可用")
        from .stored import parse_params
        params = parse_params(', '.join(p.get('params') or []))
        try:
            self._validate_procedure_body(p['name'], p['body'])
        except SyntaxError as e:
            return self._err(
                1064, f"CREATE PROCEDURE {p['name']} 过程体语法错误: {e}")
        except ValueError as e:
            return self._err(1105, str(e))
        self._stored.create_procedure(p['name'], params, p['body'])
        return self._ok(message=f"Procedure '{p['name']}' created")

    def _validate_procedure_body(self, name, body):
        """解析过程体（不执行）：语法/结构错误在创建期显式报错"""
        from .pl import PLParser
        text = self._strip_begin_end(str(body or ''))
        if not text:
            raise ValueError(f"CREATE PROCEDURE {name}: 过程体为空")
        try:
            stmts = PLParser(text).parse_program()
        except SyntaxError as e:
            raise SyntaxError(str(e))
        if not stmts:
            raise ValueError(f"CREATE PROCEDURE {name}: 过程体为空")
        return stmts

    # ---------- 存储函数 ----------

    _FUNCTION_BODY_HINT = (
        "函数体只支持 `RETURN <表达式>` 或 "
        "`BEGIN [DECLARE ...;] [SET ...;] [IF ... THEN ... END IF;] "
        "RETURN <表达式>; END`")

    def _cmd_create_function(self, p, s):
        """`CREATE FUNCTION ...` —— 解析 → 校验 → 落 `_stored.json`

        三条**创建期**校验（都在真正保存之前）：
        1. 参数只支持 IN（MySQL 13.1.12：存储函数的参数只能是 IN，
           OUT/INOUT 在 MySQL 里本身就非法，报 1064）；
        2. `RETURNS 类型` 必须在本引擎的类型清单里（CURSOR/ROW/TABLE 这类
           没有标量值语义的类型显式报错）；
        3. 函数体交给 `PLParser` 解析并逐个语句校验（**循环 / 游标 /
           SELECT ... INTO / SIGNAL 合法**；handler / 事务 / 改数据的 SQL
           显式报错 —— 与 MySQL 只禁"改数据"的边界一致）。
        """
        if self._stored is None:
            return self._err(1105, "存储函数管理器不可用")
        name = p['name']
        if p.get('if_not_exists') and self._lookup_stored_function(name) is not None:
            return self._ok(message=f"Function '{name}' already exists")

        from .stored import parse_params
        params = parse_params(', '.join(p.get('params') or []))
        bad_mode = [prm['name'] for prm in params
                    if str(prm.get('mode', 'IN')).upper() in ('OUT', 'INOUT')]
        if bad_mode:
            return self._err(
                1064, f"CREATE FUNCTION {name}: 参数 "
                      f"{', '.join(bad_mode)} 用了 OUT/INOUT —— MySQL 语义"
                      f"（13.1.12「CREATE FUNCTION Statement」）规定存储函数"
                      f"的参数只能是 IN，OUT/INOUT 在函数签名里本身就非法")

        ret_type = str(p.get('return_type') or '').strip()
        try:
            ret_upper = self._check_function_return_type(name, ret_type)
        except ValueError as e:
            return self._err(1235, str(e))

        try:
            self._validate_function_body(name, p['body'], params)
        except SyntaxError as e:
            return self._err(1064, f"CREATE FUNCTION {name} 函数体语法错误: {e}")
        except ValueError as e:
            return self._err(1105, str(e))

        self._stored.create_function(name, params, ret_upper, p['body'])
        return self._ok(message=f"Function '{name}' created")

    #: 本引擎支持的 `RETURNS` 类型（规范化形式）。没有标量值语义的类型
    #: （CURSOR / ROW / TABLE / SET / ENUM）一律显式报错。
    _FUNCTION_RETURN_TYPES = (
        'TINYINT', 'SMALLINT', 'MEDIUMINT', 'INT', 'INTEGER', 'BIGINT',
        'DECIMAL', 'NUMERIC', 'FLOAT', 'DOUBLE', 'REAL',
        'CHAR', 'VARCHAR', 'TEXT', 'TINYTEXT', 'MEDIUMTEXT', 'LONGTEXT',
        'BOOL', 'BOOLEAN', 'BIT',
        'DATE', 'DATETIME', 'TIMESTAMP', 'TIME', 'YEAR', 'JSON', 'BLOB',
    )

    def _check_function_return_type(self, name, ret_type):
        """校验 `RETURNS 类型`；返回规范化（大写、去长度）的类型名

        `VARCHAR(20)` → `VARCHAR`；`INT` → `INT`；`CURSOR` 等显式报错。
        """
        t = str(ret_type or '').strip()
        if not t:
            raise ValueError(f"CREATE FUNCTION {name}: RETURNS 缺少返回类型")
        if not re.match(r'^[A-Za-z_]\w*(\s*\(\s*[\w, ]+\s*\))?$', t):
            raise ValueError(
                f"CREATE FUNCTION {name}: 无法识别的 RETURNS 类型 {t!r}")
        base = re.match(r'^([A-Za-z_]\w*)', t).group(1).upper()
        if base not in self._FUNCTION_RETURN_TYPES:
            raise ValueError(
                f"CREATE FUNCTION {name}: 不支持的 RETURNS 类型 '{t}' —— "
                f"本引擎的标量函数只能返回单个标量值，支持的类型是 "
                f"{' / '.join(self._FUNCTION_RETURN_TYPES)}；"
                f"CURSOR / ROW / TABLE / SET / ENUM 这类没有标量值语义的"
                f"类型不会静默降级成 INT 或 NULL")
        return base

    def _validate_function_body(self, name, body, params):
        """解析并静态校验函数体（不执行）

        复用的是过程体那套 `PLParser`（同一个词法/语法实现），只在**语句
        种类**上收紧：函数体里出现游标 / 循环 / handler / 事务 / 改数据的
        SQL 都显式报错（MySQL 的存储函数同样禁止改数据的语句）。
        """
        from .pl import PLParser
        text = self._strip_begin_end(str(body or ''))
        if not text:
            raise ValueError(
                f"CREATE FUNCTION {name} 函数体为空（{self._FUNCTION_BODY_HINT}）")
        try:
            stmts = PLParser(text).parse_program()
        except SyntaxError as e:
            raise SyntaxError(f"{e}（{self._FUNCTION_BODY_HINT}）")
        if not stmts:
            raise ValueError(
                f"CREATE FUNCTION {name} 函数体为空（{self._FUNCTION_BODY_HINT}）")
        self._check_function_stmts(name, stmts, bool(params))

    #: 函数体允许的 PL 语句种类（其余显式报错）
    #:
    #: **MySQL 依据**：存储函数禁的是"改数据 / 事务 / 锁 / 临时表"这类
    #: 语句（MySQL 13.1.12「CREATE FUNCTION Statement」：`INSERT`、
    #: `UPDATE`、`DELETE`、`COMMIT`、`ROLLBACK`、`LOCK TABLES`、
    #: 改非确定性数据等会显式报错 1422），而 WHILE / LOOP / REPEAT /
    #: 游标 / `SELECT ... INTO` / SIGNAL 在函数体里**都是合法的**（同一
    #: 手册章节的示例里就有循环与游标）。因此这里放行这些控制流语句；
    #: 改数据的 SQL 与 CALL 仍然显式拒绝。
    _FUNCTION_ALLOWED_STMTS = (
        'DECLARE', 'DECLARE_CURSOR', 'SET', 'IF', 'RETURN',
        'WHILE', 'LOOP', 'REPEAT', 'LEAVE', 'ITERATE',
        'SELECT_INTO', 'OPEN', 'FETCH', 'CLOSE', 'SIGNAL',
    )

    def _check_function_stmts(self, name, stmts, has_params):
        """递归校验函数体里的每条语句（越界一律显式报错）

        放行的控制流（WHILE / LOOP / REPEAT / 游标 / SELECT ... INTO /
        SIGNAL）会**递归进语句块**继续校验，因此 `WHILE ... DO INSERT ...
        END WHILE` 里的 INSERT 同样在这里被拦下。
        """
        for stmt in stmts:
            if not isinstance(stmt, tuple) or not stmt:
                raise ValueError(
                    f"CREATE FUNCTION {name}: 函数体里有无法识别的语句 "
                    f"{stmt!r}")
            op = stmt[0]
            if op == 'SQL':
                raise ValueError(
                    f"CREATE FUNCTION {name}: 函数体里的 SQL 语句不受支持: "
                    f"{stmt[1][:80]} —— MySQL 语义禁止存储函数修改数据"
                    f"（13.1.12：INSERT / UPDATE / DELETE / CALL / 事务与锁"
                    f"语句都会报 ERROR 1422）。允许的是 DECLARE / SET / IF / "
                    f"WHILE / LOOP / REPEAT / LEAVE / ITERATE / 游标 / "
                    f"SELECT ... INTO / SIGNAL / RETURN。"
                    f"{self._FUNCTION_BODY_HINT}")
            if op not in self._FUNCTION_ALLOWED_STMTS:
                raise ValueError(
                    f"CREATE FUNCTION {name}: 函数体里暂不支持的语句类型 "
                    f"{op}（只支持 {' / '.join(self._FUNCTION_ALLOWED_STMTS)}"
                    f"）。{self._FUNCTION_BODY_HINT}")
            if op == 'IF':
                _, branches, else_body = stmt
                for _cond, block in branches:
                    self._check_function_stmts(name, block, has_params)
                if else_body:
                    self._check_function_stmts(name, else_body, has_params)
            elif op in ('WHILE', 'REPEAT', 'LOOP'):
                body = stmt[3] if len(stmt) == 4 else stmt[-1]
                if op == 'LOOP' and len(stmt) == 3:
                    body = stmt[2]
                self._check_function_stmts(name, body, has_params)

    def _cmd_drop_function(self, p, s):
        if self._stored is None:
            return self._err(1105, "存储函数管理器不可用")
        name = p['name']
        if self._lookup_stored_function(name) is None:
            if p.get('if_exists'):
                return self._ok(message=f"Unknown function '{name}'")
            return self._err(1305, f"FUNCTION {name} does not exist")
        self._stored.drop_function(name)
        return self._ok(message=f"Function '{name}' dropped")

    def _show_functions(self, p, s):
        """`SHOW FUNCTIONS` / `SHOW FUNCTION STATUS` —— 真实注册表快照

        列名沿用 MySQL `SHOW FUNCTION STATUS`（首列是 Name，这样
        `lightdb_studio` 的 `[r[0] for r in rows]` 拿到的是函数名）。
        """
        mgr = self._stored
        funcs = (getattr(mgr, 'functions', {}) if mgr is not None else {})
        cols = ['Name', 'Db', 'Type', 'Return_type', 'Parameters',
                'Definer', 'Modified', 'Created', 'Security_type', 'Comment',
                'character_set_client', 'collation_connection',
                'Database Collation']
        rows = []
        for fname in sorted(funcs):
            fn = funcs[fname]
            params = ', '.join(
                f"{prm.get('name')} {prm.get('type')}"
                for prm in (fn.params or []))
            created = time.strftime(
                '%Y-%m-%d %H:%M:%S',
                time.localtime(getattr(fn, 'created_at', time.time())))
            rows.append((fname, 'default', 'FUNCTION', fn.return_type,
                         params, 'root@localhost', created, created,
                         'DEFINER', '', 'utf8mb4', 'utf8mb4_general_ci',
                         'utf8mb4_general_ci'))
        return self._rs(cols, rows)

    #: `SHOW TRIGGERS` 的列清单（MySQL 5.7/8.0 文档顺序）
    _SHOW_TRIGGERS_COLUMNS = (
        'Trigger', 'Event', 'Table', 'Statement', 'Timing', 'Created',
        'sql_mode', 'Definer', 'character_set_client',
        'collation_connection', 'Database Collation',
    )

    def _show_triggers(self, p, s):
        """`SHOW TRIGGERS`（F2）—— 触发器的**真实**定义快照

        旧实现完全没有这条 SHOW（1064），坏触发器建成功之后连"它长什么样"
        都查不到。这里按 MySQL 列清单输出 `TriggerManager` 里真实持久化的
        定义（名字/事件/表/体/时机/创建时间），GUI 的对象树
        （`lightdb_studio/db/metadata.py`）也靠它列触发器。
        本引擎没有独立 schema/definer 概念，Definer 如实写 `root@localhost`，
        sql_mode 如实写引擎的默认严格模式。
        """
        mgr = self._trigger_mgr
        rows = []
        if mgr is not None:
            for trig in sorted(mgr.list_all(), key=lambda t: t.name):
                created = time.strftime(
                    '%Y-%m-%d %H:%M:%S',
                    time.localtime(getattr(trig, 'created_at', time.time())))
                rows.append((
                    trig.name, trig.event, trig.table, trig.body, trig.timing,
                    created, 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES',
                    'root@localhost', 'utf8mb4', 'utf8mb4_general_ci',
                    'utf8mb4_general_ci'))
        return self._rs(list(self._SHOW_TRIGGERS_COLUMNS), rows)

    def _cmd_create_trigger(self, p, s):
        """`CREATE TRIGGER ...`（F2：坏体在定义期显式失败，不落库）

        旧实现只把 body 存进 `_triggers.json`，坏体建触发器返回 OK；之后该表
        任何写入都在触发路径解析失败 → 永久 1105，且没有 DROP TRIGGER 语法
        可以自救。现在 `TriggerManager.create()` 在落库前校验（PL 结构 +
        语句形态 + NEW/OLD 列引用）；这里把异常自带语义码透传给客户端：

        * 触发器体语法/结构错误 → `TriggerBodyError`（1064 / 42000）；
        * 引用了不存在的列 → `TriggerColumnError`（1054 / 42S22）；
        * 同名触发器等旧错误 → 保持 MySQL 1359。
        """
        if self._trigger_mgr is None:
            return self._err(1105, "触发器管理器不可用")
        if p.get('table') not in self.db.tables:
            return self._err(1146, f"表不存在: '{p.get('table')}'")
        columns = [c['name'] for c in self.db.tables[p['table']].cols]
        try:
            self._trigger_mgr.create(
                p['name'], p['table'], p['timing'], p['event'], p['body'],
                columns=columns)
        except ValueError as e:
            return self._err(
                getattr(e, 'errno', None) or 1359, str(e),
                sqlstate=getattr(e, 'sqlstate', None))
        return self._ok(message=f"Trigger '{p['name']}' created")

    def _cmd_drop_trigger(self, p, s):
        """`DROP TRIGGER [IF EXISTS] name`（F2）

        MySQL 语义：不存在时 1360（`ER_TRG_DOES_NOT_EXIST`）；`IF EXISTS`
        时把"不存在"当成功（恢复脚本/重复执行幂等）。
        """
        if self._trigger_mgr is None:
            return self._err(1105, "触发器管理器不可用")
        name = p.get('name')
        if self._trigger_mgr.drop(name):
            return self._ok(message=f"Trigger '{name}' dropped")
        if p.get('if_exists'):
            return self._ok(message=f"Unknown trigger '{name}'")
        return self._err(1360, f"Trigger '{name}' does not exist",
                         sqlstate='HY000')

    def _cmd_create_fulltext_index(self, p, s):
        try:
            tbl = self.db.get_table(p['table'])
        except KeyError as e:
            return self._err(1146, f"表不存在: {e}")
        names = [c['name'] for c in tbl.cols]
        for col in p.get('columns') or []:
            if col not in names:
                return self._err(
                    1072, f"Key column '{col}' doesn't exist in table")
        try:
            tbl.create_fulltext_index(p['columns'],
                                      name=p.get('name'),
                                      if_not_exists=p.get('if_not_exists'))
        except Exception as e:
            return self._err(1061, f"创建全文索引失败: {e}")
        return self._ok(
            message=f"Fulltext index '{p.get('name')}' created on "
                    f"{p['table']}({', '.join(p.get('columns') or [])})")

    def _pl_arg_value(self, raw):
        """CALL 实参 → Python 值（字面量；`@name` 取用户变量）

        `@name` 的读取顺序与 `_write_back_out_params` 的写入位置一致：
        ① 当前会话的 `user_vars`；② 全局 `variables.get_vm()` 的用户变量表
        （`SELECT @x` 走的是后者，因此两条路径都要写，见
        `_write_back_out_params`）。取不到时返回 None —— MySQL 里读未赋值
        的用户变量本来就是 NULL。
        """
        if raw is None:
            return None
        t = str(raw).strip()
        if t.startswith('@'):
            name = t.lstrip('@')
            sess = getattr(self, '_active_session', None)
            store = self._pl_user_var_store(sess)
            if name in store:
                return store[name]
            try:
                from .variables import get_vm
                sid = (sess or {}).get('session_id') \
                    if isinstance(sess, dict) else None
                if sid is not None:
                    return get_vm().get_session(int(sid)).get_user(name)
            except Exception:                      # pragma: no cover
                pass
            return None
        if t.upper() == 'NULL':
            return None
        if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
            return t[1:-1].replace("''", "'")
        if re.match(r'^-?\d+$', t):
            return int(t)
        if re.match(r'^-?\d+\.\d+$', t):
            return float(t)
        return t

    @staticmethod
    def _sql_literal(v):
        if v is None:
            return 'NULL'
        if isinstance(v, bool):
            return '1' if v else '0'
        if isinstance(v, (int, float)):
            return repr(v)
        return "'" + str(v).replace("'", "''") + "'"

    @staticmethod
    def _substitute_words(text, mapping):
        """把 SQL 文本里**引号外**的整词替换成 mapping 的值

        mapping 的键按大写匹配（SQL 标识符大小写不敏感）。
        引号内（字符串字面量 / 反引号标识符）一律不动 —— 否则
        `INSERT INTO t VALUES (1, 'x marks')` 里的 `x` 会被参数值替换成
        静默错误值，这正是要避免的。

        **函数调用名不替换**（存储函数那一轮补的边界）：标识符后面紧跟
        `(` 时它是函数名，不是变量 —— 否则 `CREATE FUNCTION quad(x INT)
        RETURNS INT RETURN double_it(double_it(x))` 里的函数名会被参数
        值顶掉，变成 `2(2)` 这种无法解析的文本，或者更糟：静默算成别的
        东西。带点的限定名（`t.a` / `NEW.age`）同样整段匹配，只有整段
        命中 mapping 才替换（因此 `t.a` 不会因为变量 `a` 而被改写）。
        """
        if not mapping:
            return text
        upper = {str(k).upper(): v for k, v in mapping.items()}
        s = str(text)
        out = []
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch in ("'", '"', '`'):
                j = i + 1
                while j < n:
                    if s[j] == ch:
                        # SQL 的 '' 是转义的单引号，继续留在字符串里
                        if ch == "'" and j + 1 < n and s[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                out.append(s[i:j + 1])
                i = j + 1
                continue
            if ch.isalpha() or ch == '_':
                j = i
                while j < n and (s[j].isalnum() or s[j] == '_'):
                    j += 1
                k = j
                while k < n and s[k] == '.':
                    k += 1
                    if k < n and (s[k].isalpha() or s[k] == '_'):
                        while k < n and (s[k].isalnum() or s[k] == '_'):
                            k += 1
                    else:
                        k = j
                        break
                # 后面紧跟 `(` → 函数调用名，不替换
                if k < n and s[k] == '(':
                    out.append(s[i:k])
                    i = k
                    continue
                word = s[i:k]
                out.append(upper.get(word.upper(), word))
                i = k
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    @staticmethod
    def _split_statements(text):
        """按顶层分号切分语句块（引号 / 括号内不算）"""
        out, cur, in_str, depth = [], '', None, 0
        for ch in str(text or ''):
            if in_str:
                cur += ch
                if ch == in_str:
                    in_str = None
                continue
            if ch in ("'", '"', '`'):
                in_str = ch
                cur += ch
            elif ch == '(':
                depth += 1
                cur += ch
            elif ch == ')':
                depth -= 1
                cur += ch
            elif ch == ';' and depth == 0:
                out.append(cur)
                cur = ''
            else:
                cur += ch
        if cur.strip():
            out.append(cur)
        return [x.strip() for x in out if x.strip()]

    @staticmethod
    def _strip_begin_end(text):
        t = str(text or '').strip()
        while t.endswith(';'):
            t = t[:-1].strip()
        up = t.upper()
        if up.startswith('BEGIN') and up.endswith('END'):
            return t[len('BEGIN'):-len('END')].strip()
        return t

    @staticmethod
    def _has_pl_control_flow(body) -> bool:
        """体里是否有需要 PL 解释器的构造（否则走"直接逐条执行"的快路）

        `REPEAT` / `LOOP` / 游标 / `SIGNAL` / `SET` / `SELECT ... INTO`
        都必须在这里被认出来 —— 漏掉任何一个，那条过程体就会被当成
        "一堆普通 SQL"按分号切开执行（`SET s = 42` 这类 PL 赋值语句不是
        SQL 的 SET，解析必然失败）。
        """
        return bool(re.search(
            r'(^|;|\s)(IF|WHILE|LOOP|REPEAT|DECLARE|CURSOR|RETURN|'
            r'LEAVE|ITERATE|OPEN|FETCH|CLOSE|SIGNAL|SET)\b'
            r'|INTO\s+\w',
            str(body or ''), re.IGNORECASE))

    # ==================== PL 语句执行器（过程体 / 函数体共用） ====================
    #
    # 设计要点（这一轮的重点）：
    # * **只有一份实现**：过程体（`_exec_pl_body`）与函数体
    #   （`_exec_function_body`）都走 `_pl_run_block`。`sql/pl.py` 的
    #   `PLInterpreter` 只剩转发壳，避免"两个解释器语义不一致"。
    # * SQL 语句里的变量名在**每次执行前**按当前值做一次引号感知的
    #   字面量替换（`_substitute_words`），所以循环体里变量的逐轮变化
    #   是真的生效的（旧 `pl.py::PLInterpreter._substitute_vars` 是空
    #   实现，会把字符串 `'i'` 写进库 —— 静默错值）。
    # * `SELECT ... INTO` 的**目标变量名**在替换时被保护（占位符），
    #   否则参数 `n` 会被替换成 `105`，语句变成 `SELECT ... INTO 105`，
    #   解析失败后静默什么都不做（`test_procedure_out_param` 的形态）。
    # * 迭代上限：每执行一条语句、每求值一次循环条件都消耗 1 次配额，
    #   超过 `ctx.max_iterations`（默认 100 万）显式报错 —— 绝不挂死。

    #: `SELECT ... INTO` 目标变量名在字面量替换期间的占位前缀
    _PL_INTO_PLACEHOLDER = '__lightdb_pl_into_'

    def _pl_iteration_budget(self, ctx):
        """取本次执行的迭代上限（会话变量 / 执行器属性可覆盖默认值）"""
        try:
            val = int(getattr(ctx, 'max_iterations', None)
                      or PL_DEFAULT_MAX_ITERATIONS)
        except (TypeError, ValueError):
            val = PL_DEFAULT_MAX_ITERATIONS
        return max(1, val)

    def _pl_tick(self, ctx):
        """消耗一次迭代配额；超限显式报错并终止（绝不无限循环）"""
        ctx.iterations += 1
        limit = self._pl_iteration_budget(ctx)
        if ctx.iterations > limit:
            where = ''
            if getattr(ctx, 'function_name', None):
                where = f"（函数 {ctx.function_name}）"
            raise RuntimeError(
                f"PL 执行超过迭代上限 {limit} 次{where}：循环没有收敛"
                f"（可能条件恒真 / ITERATE 没有推进循环变量）。"
                f"本引擎不会无限循环，已显式中止；"
                f"上限可用环境变量 LIGHTDB_PL_MAX_ITERATIONS 或 "
                f"`ctx.max_iterations` 调整")

    def _pl_save_into_slots(self, names, ctx):
        """`SELECT ... INTO` 目标变量 → 占位符（返回 `[(占位, 名字)]`）

        占位符在 SQL 里代替变量名出现，事后由 `_pl_assign_into` 按列写回，
        因此过程中 `ctx.variables` 的变化不会影响目标名。
        """
        return [(f'{self._PL_INTO_PLACEHOLDER}{i}', name)
                for i, name in enumerate(names)]

    def _pl_assign_into(self, ctx, slots, values):
        """把 SELECT 结果的第 i 列写进第 i 个目标变量"""
        for i, (_ph, name) in enumerate(slots):
            ctx.variables[name] = values[i]

    def _pl_bind_into_sql(self, sql, ctx, names):
        """替换 SQL 里的**变量值**，并返回 `SELECT ... INTO` 的槽位

        返回 `(SQL, 槽位列表)`。这是 `test_procedure_out_param` 那个既有
        缺陷的落点：旧实现把 `INTO n` 里的 `n` 也当成变量值替换成
        `105`，语句变成 `... INTO 105 ...`，`parser.parse` 返回 None，
        然后 `if p:` 静默什么都不做 —— OUT 变量永远是 NULL。
        现在的做法是：过程体在**上游**就先保护了目标位
        （`_pl_protect_assign_targets`），这里只需要把剩下的变量做值
        替换；槽位只用于"结果列 → 目标变量"的写回。
        """
        slots = self._pl_save_into_slots(names, ctx)
        lowered = {str(n).lower() for n in names}
        value_map = {k: self._sql_literal(v)
                     for k, v in ctx.variables.items()
                     if str(k).lower() not in lowered}
        return self._substitute_words(sql, value_map), slots

    def _pl_run_block(self, stmts, ctx, ExprParser, eval_expr,
                      cond_tokens=None, funcs_depth=None):
        """逐条执行 PL 语句（过程体与函数体**共用这一份实现**）

        funcs_depth 不为 None 时进入**函数体模式**：表达式求值带上存储
        函数解析回调（函数体里可以调用别的函数，共享同一个递归深度
        计数）。函数体与过程体的**语句能力现在完全一致**（循环 / 游标 /
        SELECT ... INTO / SIGNAL 都能用）—— 这正是 MySQL 的语义：
        MySQL 的存储函数同样允许 WHILE / LOOP / REPEAT / 游标 / SELECT
        ... INTO，只禁止改数据的语句（见 `_check_function_stmts`）。
        """
        if cond_tokens is not None:
            self._pl_tick(ctx)
            if not eval_expr(ExprParser(cond_tokens).parse(), ctx):
                return None

        if not isinstance(getattr(ctx, 'loop_stack', None), list):
            ctx.loop_stack = []

        def _funcs():
            """表达式求值用的存储函数回调（**过程体与函数体都要**）

            过程体里 `SET s = double_it(i)` 必须真的调用已注册的存储函数；
            函数体里还要靠它传递递归深度计数。两边都用同一个 resolver，
            因此行为一致（此前过程体没接，被当成"未知函数"）。
            """
            return self._stored_function_resolver(
                '函数体' if funcs_depth is not None else '过程体')

        if funcs_depth is not None:
            self._check_function_stmts(
                getattr(ctx, 'function_name', '?'), stmts, True)

        for stmt in stmts:
            self._pl_tick(ctx)
            op = stmt[0]

            if op == 'DECLARE':
                _, name, _types, default_toks = stmt
                value = None
                if default_toks:
                    value = eval_expr(ExprParser(default_toks).parse(), ctx,
                                      funcs=_funcs())
                if name in getattr(ctx, 'declared_vars', ()) or \
                        name in getattr(ctx, 'cursors', {}):
                    raise RuntimeError(
                        f"PL: 变量 {name} 重复声明（MySQL 语义：同一块内"
                        f"不能重复 DECLARE 同名变量）")
                ctx.variables[name] = value
                ctx.declared_vars.add(name)

            elif op == 'DECLARE_CURSOR':
                self._pl_declare_cursor(stmt, ctx)

            elif op == 'SET':
                _, name, expr_toks = stmt
                ctx.variables[name] = eval_expr(
                    ExprParser(expr_toks).parse(), ctx, funcs=_funcs())

            elif op == 'IF':
                _, branches, else_body = stmt
                taken = False
                for cond_toks, block in branches:
                    self._pl_tick(ctx)
                    if eval_expr(ExprParser(cond_toks).parse(), ctx,
                                 funcs=_funcs()):
                        self._pl_run_block(block, ctx, ExprParser, eval_expr,
                                           funcs_depth=funcs_depth)
                        taken = True
                        break
                if not taken and else_body:
                    self._pl_run_block(else_body, ctx, ExprParser, eval_expr,
                                       funcs_depth=funcs_depth)
                if self._pl_signal_set(ctx):
                    return self._pl_signal_value(ctx)

            elif op in ('WHILE', 'LOOP', 'REPEAT'):
                self._pl_loop(stmt, ctx, ExprParser, eval_expr,
                              funcs_depth=funcs_depth)
                if self._pl_signal_set(ctx):
                    return self._pl_signal_value(ctx)

            elif op == 'SELECT_INTO':
                self._pl_select_into(stmt, ctx, funcs_depth=funcs_depth)

            elif op == 'RETURN':
                ctx.return_hit = True
                ctx.return_value = None
                if stmt[1]:
                    ctx.return_value = eval_expr(
                        ExprParser(stmt[1]).parse(), ctx, funcs=_funcs())
                return ctx.return_value

            elif op == 'LEAVE':
                self._pl_leave(stmt, ctx)
                return None

            elif op == 'ITERATE':
                self._pl_iterate(stmt, ctx)
                return None

            elif op == 'OPEN':
                self._pl_open_cursor(stmt, ctx)

            elif op == 'FETCH':
                self._pl_fetch_cursor(stmt, ctx)

            elif op == 'CLOSE':
                self._pl_close_cursor(stmt, ctx)

            elif op == 'SIGNAL':
                self._pl_signal(stmt, ctx)
                return self._pl_signal_value(ctx)

            elif op == 'SQL':
                if self._pl_signal_set(ctx):
                    return self._pl_signal_value(ctx)
                self._pl_sql_run(stmt[1], ctx, funcs_depth=funcs_depth)

            else:
                raise RuntimeError(f"PL 里暂不支持的语句类型: {op}")

            # ---- 非局部退出的传播（这一条是 LEAVE 正确性的关键） ----
            # `LEAVE` / `ITERATE` 设置的标记必须在**每条语句之后**检查并
            # 立即结束当前语句块，否则循环体里 LEAVE 之后的语句还会继续
            # 执行（实测：游标到底后 `LEAVE read_loop` 之后紧跟的
            # `SET acc = acc + v` 仍然执行，结果 15 变 20 —— 静默错值）。
            if ctx.return_hit or self._pl_signal_set(ctx):
                return ctx.return_value
            if ctx.leave_flag is not None or ctx.iterate_flag is not None:
                return None
        return None

    # ---------- 循环 ----------

    def _pl_find_loop(self, ctx, label):
        """按**动态作用域**从内到外找循环（MySQL 的 LEAVE 语义）

        返回 `(层级, {'label','kind'})`；label 为空串表示"最内层循环"。
        找不到就抛错（显式失败，不静默跳到某处）。
        """
        stack = getattr(ctx, 'loop_stack', None) or []
        if not label:
            if not stack:
                return None, None
            return len(stack) - 1, stack[-1]
        want = str(label).lower()
        for i in range(len(stack) - 1, -1, -1):
            if str(stack[i].get('label') or '').lower() == want:
                return i, stack[i]
        return None, None

    def _pl_leave(self, stmt, ctx):
        """`LEAVE [label]` —— 把控制流交给目标循环（离开它）"""
        label = stmt[1] if len(stmt) > 1 else ''
        idx, target = self._pl_find_loop(ctx, label)
        if target is None:
            shown = label or '(无标签)'
            raise RuntimeError(
                f"LEAVE {shown}: 没有对应的循环标签"
                f"（当前循环栈：{self._pl_loop_stack_text(ctx)}）")
        ctx.leave_flag = label or ''

    def _pl_iterate(self, stmt, ctx):
        """`ITERATE [label]` —— 进入目标循环的下一轮（离开当前这一轮体）"""
        label = stmt[1] if len(stmt) > 1 else ''
        idx, target = self._pl_find_loop(ctx, label)
        if target is None:
            shown = label or '(无标签)'
            raise RuntimeError(
                f"ITERATE {shown}: 没有对应的循环标签"
                f"（当前循环栈：{self._pl_loop_stack_text(ctx)}）")
        ctx.iterate_flag = label or ''

    @staticmethod
    def _pl_loop_stack_text(ctx):
        stack = getattr(ctx, 'loop_stack', None) or []
        if not stack:
            return '(空)'
        return ' > '.join(str(x.get('label') or '(无标签)') for x in stack)

    def _pl_loop(self, stmt, ctx, ExprParser, eval_expr, funcs_depth=None):
        """执行 `WHILE` / `LOOP` / `REPEAT`（含 LEAVE / ITERATE 处理）

        三元组统一解析：
        * `('WHILE', label, cond_toks, body)`   —— 每轮先判条件；
        * `('LOOP',  label, None, body)`        —— 无条件，靠 LEAVE 退出；
        * `('REPEAT', label, cond_toks, body)`  —— 先执行体，再判 UNTIL。
        函数体与过程体走**同一条**路径（MySQL 同样允许函数体里有循环）。
        """
        op = stmt[0]
        # 结构（标签先被前置到元组里，因此长度会 +1）：
        #   ('LOOP',   body)                 / ('LOOP',   label, body)
        #   ('WHILE',  label, cond, body)    （无标签时 label 是空串）
        #   ('REPEAT', label, body, cond)
        # 注意 WHILE/REPEAT 的 **条件与体的次序不同**（REPEAT 的 cond 在
        # 最后），早期版本按同一次序取，REPEAT 拿到的是 token 列表当体，
        # 直接报"'Token' object is not subscriptable"。
        if op == 'REPEAT':
            label = stmt[1] if len(stmt) == 4 else ''
            body = stmt[2] if len(stmt) == 4 else stmt[1]
            cond_toks = stmt[3] if len(stmt) == 4 else stmt[2]
        elif op == 'LOOP':
            label = stmt[1] if len(stmt) == 3 else ''
            body = stmt[2] if len(stmt) == 3 else stmt[1]
            cond_toks = None
        else:                               # WHILE
            label = stmt[1] if len(stmt) == 4 else ''
            cond_toks = stmt[2] if len(stmt) == 4 else stmt[1]
            body = stmt[3] if len(stmt) == 4 else stmt[2]

        stack = ctx.loop_stack
        if len(stack) >= PL_MAX_LOOP_DEPTH:
            raise RuntimeError(
                f"PL 循环嵌套超过 {PL_MAX_LOOP_DEPTH} 层（标签栈上限）")
        stack.append({'label': label or '', 'kind': op})
        try:
            guard = 0
            while True:
                guard += 1
                if guard > 10 * self._pl_iteration_budget(ctx):
                    raise RuntimeError(
                        "PL 循环调度次数异常（内部保护），已中止")
                if op in ('WHILE', 'LOOP'):
                    if cond_toks is not None:
                        self._pl_tick(ctx)
                        if not eval_expr(ExprParser(cond_toks).parse(), ctx):
                            break
                self._pl_run_block(body, ctx, ExprParser, eval_expr,
                                   funcs_depth=funcs_depth)
                if self._pl_signal_set(ctx):
                    # SIGNAL 是"非局部退出"：一路往上抛
                    return
                if ctx.return_hit:
                    return
                if ctx.leave_flag is not None:
                    if self._pl_leave_targets(ctx, stack, ctx.leave_flag):
                        ctx.leave_flag = None
                        return
                    return                       # 交给外层同名循环
                if ctx.iterate_flag is not None:
                    if self._pl_leave_targets(ctx, stack, ctx.iterate_flag):
                        ctx.iterate_flag = None
                        if op == 'REPEAT':
                            pass                 # REPEAT 的"下一轮"= 重跑体
                        continue
                    return
                if op == 'REPEAT':
                    self._pl_tick(ctx)
                    if eval_expr(ExprParser(cond_toks).parse(), ctx):
                        break
        finally:
            if stack and stack[-1].get('kind') == op \
                    and str(stack[-1].get('label') or '') == str(label or ''):
                stack.pop()
            else:
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i].get('kind') == op and \
                            str(stack[i].get('label') or '') == str(label or ''):
                        del stack[i]
                        break

    @staticmethod
    def _pl_leave_targets(ctx, stack, label):
        """当前循环是否就是 LEAVE / ITERATE 的目标（区分同名嵌套）"""
        if not stack:
            return True
        cur = stack[-1].get('label') or ''
        if not label:
            return True
        return str(cur).lower() == str(label).lower()

    # ---------- 游标 ----------

    def _pl_cursor(self, ctx, name, create=False, select_toks=None,
                   declared_at=0):
        cursors = ctx.cursors
        key = str(name or '')
        cur = cursors.get(key) or cursors.get(key.lower())
        if cur is None and create:
            from .pl import PLCursor
            cur = PLCursor(key, select_toks, declared_at)
            cursors[key] = cur
            ctx.cursor_order.append(key)
        return cur

    def _pl_declare_cursor(self, stmt, ctx):
        """`DECLARE name CURSOR FOR <select>`（游标是只读的，无参数）"""
        _, name, select_toks, declared_at = stmt
        if self._pl_cursor(ctx, name) is not None:
            raise RuntimeError(f"PL: 游标 {name} 重复声明")
        if name in ctx.variables:
            raise RuntimeError(
                f"PL: {name} 已作为变量声明，不能再用作游标名（MySQL 语义："
                f"同一块里的变量名与游标名不能重名）")
        self._pl_cursor(ctx, name, create=True, select_toks=select_toks,
                        declared_at=declared_at)

    def _pl_open_cursor(self, stmt, ctx):
        """`OPEN cur` —— 物化游标查询的全部行（OPEN 时报错不静默）"""
        _, name = stmt
        cur = self._pl_cursor(ctx, name)
        if cur is None:
            raise RuntimeError(
                f"OPEN {name}: 游标未声明（MySQL 语义：必须先 "
                f"DECLARE ... CURSOR FOR）")
        if cur.is_open:
            raise RuntimeError(
                f"OPEN {name}: 游标已经打开（MySQL 语义：游标不可重复 "
                f"OPEN，需先 CLOSE）")
        from .pl import PLParser
        sql = PLParser('').tokens_to_sql(cur.select_toks)
        sql = self._substitute_words(
            sql, {k: self._sql_literal(v) for k, v in ctx.variables.items()})
        parsed = self._parse_pl_sql(sql, f"OPEN {name}")
        res = self.execute(parsed, ctx.session)
        if isinstance(res, dict) and res.get('type') == 'error':
            raise RuntimeError(
                f"OPEN {name} 失败: {res.get('message')}")
        if not (isinstance(res, dict) and res.get('type') == 'resultset'):
            raise RuntimeError(f"OPEN {name}: 游标查询没有结果集: {sql[:120]}")
        cur.columns = list(res.get('columns') or [])
        cur.rows = [tuple(r) for r in (res.get('rows') or [])]
        cur.pos = -1
        cur.is_open = True
        ctx.not_found = False

    def _pl_fetch_cursor(self, stmt, ctx):
        """`FETCH cur INTO v1[, v2]` —— 到底时置 NOT FOUND（SQLSTATE 02000）"""
        _, name, names = stmt
        cur = self._pl_cursor(ctx, name)
        if cur is None:
            raise RuntimeError(f"FETCH {name}: 游标未声明")
        if not cur.is_open:
            raise RuntimeError(
                f"FETCH {name}: 游标未打开（MySQL 语义：必须先 OPEN）")
        if len(names) != len(cur.columns):
            raise RuntimeError(
                f"FETCH {name}: 目标变量个数 {len(names)} 与游标查询的列数 "
                f"{len(cur.columns)} 不一致（MySQL 语义：必须一一对应）")
        cur.pos += 1
        if cur.pos >= len(cur.rows):
            # MySQL：FETCH 没有更多行时置 SQLSTATE 02000（NOT FOUND），
            # **变量保持原值**（与 SELECT ... INTO 的 0 行语义一致）。
            ctx.not_found = True
            return
        ctx.not_found = False
        row = cur.rows[cur.pos]
        for i, v in enumerate(row):
            if i < len(names):
                ctx.variables[names[i]] = v

    def _pl_close_cursor(self, stmt, ctx):
        _, name = stmt
        cur = self._pl_cursor(ctx, name)
        if cur is None:
            raise RuntimeError(f"CLOSE {name}: 游标未声明")
        if not cur.is_open:
            raise RuntimeError(f"CLOSE {name}: 游标未打开")
        cur.is_open = False
        cur.rows = []
        cur.pos = -1

    # ---------- SIGNAL ----------

    def _pl_signal(self, stmt, ctx):
        """`SIGNAL SQLSTATE 'xxxxx' [SET MESSAGE_TEXT = '...']`

        实现方式：在 `ctx` 上登记"当前正在抛出的 SIGNAL"，由各层的
        语句块在每次执行后检查（`_pl_signal_set`）并一路向上返回，
        最终由 `_cmd_call` / `_exec_function_body` 转成显式错误。
        **不用 Python 异常**是刻意的：异常会被 `execute()` 的兜底
        `except Exception` 吞成 error dict，反而丢掉"哪一层抛的"信息。

        `HANDLER` 仍未实现（见 `CREATE ... HANDLER` 的显式报错）。
        """
        _, state, msg, items, _pos = stmt
        ctx.signal_state = state
        ctx.signal_message = msg if msg is not None else \
            (f"unhandled user-defined exception" if state == '45000'
             else f"SQLSTATE {state}")
        ctx.signal_items = dict(items or {})

    @staticmethod
    def _pl_signal_set(ctx):
        return getattr(ctx, 'signal_state', None) is not None

    @staticmethod
    def _pl_signal_value(ctx):
        return getattr(ctx, 'signal_message', None)

    # ---------- SELECT ... INTO ----------

    def _pl_select_into(self, stmt, ctx, funcs_depth=None):
        """`SELECT ... INTO v1[, v2] [FROM ...]`

        采用 MySQL 的语义（MySQL 13.2.10.1「SELECT ... INTO Statement」+
        13.6.7.1「SELECT ... INTO 在存储程序里」）：
        * **0 行**：变量**保持原值**并给出警告（ERROR 1329 是"无数据"，
          MySQL 文档原文："If the query returns no rows, a warning with
          error code 1329 occurs (No data), and the variable values remain
          unchanged."）。本引擎没有 warning 通道，因此不报错、不改动。
        * **多行**：显式报错（MySQL 就是 ERROR 1172 "Result consisted of
          more than one row"）—— 绝不静默只取第一行。
        * **列数与变量数不一致**：显式报错（MySQL 是 ERROR 1222
          "The used SELECT statements have a different number of columns"）。
        """
        from .pl import PLParser
        _, toks, into_pos = stmt
        rest = toks[into_pos + 1:]
        from_pos = next(
            (i for i, t in enumerate(rest)
             if t.type == 'KW' and t.value.upper() == 'FROM'), -1)
        var_toks = rest[:from_pos] if from_pos >= 0 else rest
        names = PLParser._target_names(var_toks)
        if not names:
            raise RuntimeError(
                "SELECT ... INTO 后面缺少目标变量名（MySQL 语法："
                "SELECT 列 INTO 变量[, 变量] FROM ...）")
        col_sql = PLParser('').tokens_to_sql(toks[1:into_pos])
        if from_pos < 0:
            # `SELECT <表达式> INTO var`（无 FROM）：单值赋值；0 行语义
            # 不适用（没有数据源），表达式出错会显式抛。
            if len(names) != 1:
                raise RuntimeError(
                    f"SELECT ... INTO: 目标变量有 {len(names)} 个"
                    f"（{'、'.join(names)}），但只有 1 个表达式 —— MySQL "
                    f"语义要求列数与变量数一致（ERROR 1222）")
            ep = ExprParser(toks[1:into_pos])
            value = eval_expr(ep.parse(), ctx,
                              funcs=self._stored_function_resolver('过程体'))
            ctx.variables[names[0]] = value
            return
        tail = PLParser('').tokens_to_sql(rest[from_pos:])
        # 注意：**不追加 LIMIT 1**（旧实现追加了，于是"多行"永远被截成
        # 一行，ERROR 1172 的语义被静默吞掉）。用户自己写 LIMIT 时按用户
        # 的来（`SELECT ... INTO ... LIMIT 1` 是合法且常见的写法）。
        sql = f"SELECT {col_sql} {tail}"
        sql, slots = self._pl_bind_into_sql(sql, ctx, names)
        parsed = self._parse_pl_sql(sql, "SELECT ... INTO")
        res = self.execute(parsed, ctx.session)
        if isinstance(res, dict) and res.get('type') == 'error':
            raise RuntimeError(
                f"SELECT ... INTO 执行失败: {res.get('message')}")
        if not (isinstance(res, dict) and res.get('type') == 'resultset'):
            raise RuntimeError(
                f"SELECT ... INTO 没有返回结果集: {sql[:120]}")
        rows = list(res.get('rows') or [])
        ncol = len(res.get('columns') or [])
        if len(names) != ncol:
            raise RuntimeError(
                f"SELECT ... INTO: 列数 {ncol} 与目标变量个数 {len(names)} "
                f"不一致（MySQL 语义 ERROR 1222：The used SELECT statements "
                f"have a different number of columns）—— 目标变量："
                f"{'、'.join(names)}；语句：{sql[:120]}")
        if not rows:
            # MySQL：0 行 → 变量保持原值（见方法 docstring）
            return
        if len(rows) > 1:
            raise RuntimeError(
                f"SELECT ... INTO 返回了 {len(rows)} 行，但目标变量只有 "
                f"{len(names)} 个 —— MySQL 语义 ERROR 1172（Result consisted "
                f"of more than one row）；请加 WHERE 或 LIMIT 1")
        self._pl_assign_into(ctx, slots, list(rows[0]))

    # ---------- 普通 SQL ----------

    def _parse_pl_sql(self, sql, label):
        """解析 PL 里拼出来的 SQL；**解析不出来就显式报错**（不静默跳过）"""
        from .parser import parser as _p
        parsed = _p.parse(sql)
        if parsed is None:
            raise RuntimeError(f"{label} 语句无法解析: {sql[:160]}")
        return parsed

    def _pl_run_call(self, sql, ctx):
        """在 PL 体内执行 `CALL proc(args)`（嵌套过程调用）

        与顶层 `CALL` 走**同一个** `_cmd_call`（参数校验 / OUT 回填 /
        错误处理都一致），区别是**实参解析与回填的落点**：
        * 实参是 `@user` → 用户变量（`_cmd_call` 自己处理）；
        * 实参是 PL 局部变量（`CALL inner(n, tmp)`，MySQL 存储程序里合法）
          → 入参取外层上下文的变量值，出参回填外层上下文的变量。
        缺少这一层时，`CALL inner(n, tmp)` 里 `tmp` 永远保持旧值
        （实测 `outer_proc(4)` 返回 0 而不是 50）—— 静默错值。
        """
        from .parser import parser as _p
        parsed = _p.parse(str(sql))
        if not parsed or parsed.get('command') != 'CALL':
            raise RuntimeError(f"PL 的 CALL 语句无法解析: {str(sql)[:120]}")
        session = ctx.session if isinstance(ctx.session, dict) else {}
        store = self._pl_user_var_store(session)
        store.update(ctx.variables)            # 局部变量对 _pl_arg_value 可见
        res = self._cmd_call(parsed, session)
        if isinstance(res, dict) and res.get('type') == 'error':
            raise RuntimeError(
                f"CALL {parsed.get('name')} 失败: {res.get('message')}")
        # 局部变量实参 → 把形参的最终值写回外层上下文
        proc = self._stored.get_procedure(parsed.get('name')) \
            if self._stored is not None else None
        args = list(parsed.get('args') or [])
        for i, prm in enumerate(proc.params if proc else []):
            mode = str(prm.get('mode', 'IN')).upper()
            if mode not in ('OUT', 'INOUT') or i >= len(args):
                continue
            raw = str(args[i]).strip()
            if not raw or raw.startswith('@'):
                continue                       # 用户变量：_cmd_call 已回填
            if raw in store:
                ctx.variables[raw] = store[raw]

    def _pl_sql_run(self, sql, ctx, funcs_depth=None):
        """执行 PL 里的普通 SQL 语句（变量按当前值做字面量替换）"""
        up = str(sql).strip().upper()
        if up.startswith('CALL ') or up.startswith('CALL('):
            self._pl_run_call(sql, ctx)
            return
        if funcs_depth is not None:
            first = up.split(None, 1)[0] if up.split() else ''
            if first in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'LOAD',
                         'TRUNCATE', 'CREATE', 'DROP', 'ALTER'):
                raise RuntimeError(
                    f"函数体里不允许执行改数据的语句: {str(sql)[:80]}"
                    f"（MySQL 语义：存储函数不能修改数据）")
        replaced = self._substitute_words(
            sql, {k: self._sql_literal(v) for k, v in ctx.variables.items()})
        parsed = self._parse_pl_sql(replaced, 'PL')
        res = self.execute(parsed, ctx.session)
        if isinstance(res, dict) and res.get('type') == 'error':
            raise RuntimeError(
                f"PL 语句执行失败: {res.get('message')}（语句：{replaced[:120]}）")

    def _exec_pl_body(self, body, session, variables=None):
        """执行过程体的控制流（IF/WHILE/LOOP/REPEAT/SELECT ... INTO/游标）

        为什么不用 `sql/pl.py::PLInterpreter`：它的 `_substitute_vars` 是空
        实现 —— SQL 语句里的变量名不会被替换，`INSERT INTO t VALUES (i)` 会把
        字符串 `'i'` 写进库（静默错值），循环变量因此永远不起作用。
        这里复用 pl.py 的 `PLParser`（切语句结构）与 `ExprParser`/`eval_expr`
        （表达式求值），但**每次执行 SQL 前都按当前变量值做一次引号感知的
        字面量替换**，因此循环体里的变量是逐轮变化的真实值。
        """
        from .pl import PLParser, PLContext, ExprParser, eval_expr
        ctx = PLContext(self, session, self.db)
        for k, v in (variables or {}).items():
            ctx.variables[k] = v
        stmts = PLParser(body).parse_program()
        # 过程体的表达式同样接上**存储函数解析回调**（`_pl_run_block` 里的
        # `_funcs()` 统一给）：过程里 `SET s = double_it(i)` 必须真的调到
        # `CREATE FUNCTION` 定义的函数，而不是走 `call_builtin` 报
        # "未知函数"。此前只有函数体模式接了回调，过程体漏了。
        self._pl_run_block(stmts, ctx, ExprParser, eval_expr)
        return ctx

    # ---------- 各类"PC 赋值目标名"的保护（占位符） ----------
    #
    # 统一的思路：过程体在交给 PL 解析器之前会做一次**参数文本替换**
    # （`_cmd_call` 的 `_substitute_words(proc.body, bind_sql)`），
    # 于是 `SET s = acc` / `SELECT ... INTO n` 里的目标名（OUT 形参）会
    # 被换成 `NULL` 或 `105`，语法直接崩。做法是在替换前先把这些"赋值
    # 目标位"换成占位符，替换完再还原成变量名。
    _PL_PLACEHOLDER = '__lightdb_pl_target_'

    def _pl_protect_assign_targets(self, body, names):
        """把 `SET <名字> =` / `INTO <名字>[, <名字>]` 的目标位换成占位符

        返回 `(保护后的文本, {占位符: 名字})`。只动**赋值目标位**，
        表达式里的同名变量照旧（`SET s = s + 1` 的右侧仍是真变量）。
        实现走 `PLParser` 的 token 流（而不是正则）：token 自带原文位置
        （`Token.pos`），按位置切片重写文本才能保证"只改目标位、其它
        字节不动、引号内绝不误伤"。
        """
        text = str(body or '')
        names_up = {str(n).upper(): str(n) for n in (names or [])}
        if not text.strip() or not names_up:
            return text, {}
        from .pl import PLParser as _PLP
        try:
            toks = _PLP(text).tokens
        except Exception:                              # pragma: no cover
            return text, {}
        spans = []            # [(start, end, 占位符)]
        mapping = {}
        counter = 0

        def _slot(name):
            nonlocal counter
            ph = f'{self._PL_PLACEHOLDER}{counter}'
            counter += 1
            mapping[ph] = name
            return ph

        i = 0
        while i < len(toks):
            t = toks[i]
            up = t.value.upper() if t.type in ('KW', 'IDENT') else ''
            if up == 'SET' and i + 1 < len(toks) \
                    and toks[i + 1].type == 'IDENT' \
                    and toks[i + 1].value.upper() in names_up:
                tgt = toks[i + 1]
                spans.append((tgt.pos, tgt.pos + len(str(tgt.value)),
                              _slot(names_up[tgt.value.upper()])))
                i += 2
                continue
            if up == 'INTO':
                j = i + 1
                while j < len(toks):
                    if toks[j].type == 'IDENT' \
                            and toks[j].value.upper() in names_up:
                        tgt = toks[j]
                        spans.append((tgt.pos, tgt.pos + len(str(tgt.value)),
                                      _slot(names_up[tgt.value.upper()])))
                        j += 1
                        if j < len(toks) and toks[j].type == 'PUNC' \
                                and toks[j].value == ',':
                            j += 1
                            continue
                    break
                i = j
                continue
            i += 1
        if not spans:
            return text, {}
        out, last = [], 0
        for start, end, ph in spans:
            if start < last:
                continue
            out.append(text[last:start])
            out.append(ph)
            last = end
        out.append(text[last:])
        return ''.join(out), mapping

    def _pl_restore_targets(self, text, mapping):
        """占位符 → 原始变量名（纯文本替换，不引入引号）"""
        if not mapping:
            return text
        for ph in sorted(mapping, key=len, reverse=True):
            text = str(text).replace(ph, str(mapping[ph]))
        return text

    #: 兼容旧入口（`_run_pl_statements` 是 `_pl_run_block` 的历史名字）
    _run_pl_statements = _pl_run_block

    def _run_statement_list(self, body, session, label):
        """顺序执行语句块：解析 + 执行，失败一定显式抛出（不静默）"""
        from .parser import parser as _p
        for stmt in self._split_statements(self._strip_begin_end(body)):
            parsed = _p.parse(stmt)
            if parsed is None:
                raise RuntimeError(
                    f"{label} 里的语句无法解析: {stmt[:120]}")
            res = self.execute(parsed, session)
            if isinstance(res, dict) and res.get('type') == 'error':
                raise RuntimeError(
                    f"{label} 执行失败: {res.get('message')}")
        return None

    def _cmd_call(self, p, s):
        """CALL 存储过程

        过程体按参数**文本替换**后执行（引号内不替换），控制流过程体交给
        `_exec_pl_body`（PL 解释器）。
        已知限制：OUT / INOUT 参数回填未实现 → 显式报错（不静默丢弃回填）。
        """
        mgr = self._stored
        if mgr is None:
            return self._err(1105, "存储过程管理器不可用")
        proc = mgr.get_procedure(p['name'])
        if proc is None:
            return self._err(1305, f"PROCEDURE {p['name']} does not exist")
        params = proc.params or []
        args = list(p.get('args') or [])
        if len(args) > len(params):
            return self._err(
                1318, f"Incorrect number of arguments for PROCEDURE "
                      f"{proc.name}; expected {len(params)}, got {len(args)}")
        # OUT / INOUT 参数：MySQL 语义要求对应实参是**用户变量**（`@x`），
        # 过程里对形参的赋值在返回时写回 `@x`。非用户变量的实参在 MySQL
        # 里也是合法的（值被丢弃），这里照做。
        bind_sql, bind_val = {}, {}
        out_slots = []
        for i, prm in enumerate(params):
            mode = str(prm.get('mode', 'IN')).upper()
            raw = args[i] if i < len(args) else None
            value = self._pl_arg_value(raw) if mode != 'OUT' else None
            bind_val[prm['name']] = value
            bind_sql[prm['name']] = self._sql_literal(value)
            if mode in ('OUT', 'INOUT'):
                out_slots.append((prm['name'], raw))
        try:
            # 先把"赋值目标位"（`SET 形参 =` / `INTO 形参`）换成占位符，
            # 再做参数值替换 —— 否则 `SET s = acc` 会变成 `SET NULL = 0`，
            # 过程体的 PL 解析直接失败（这是 test_procedure_out_param
            # 那条路径上的既有缺陷）。
            protected, targets = self._pl_protect_assign_targets(
                proc.body, [prm['name'] for prm in params])
            body = self._substitute_words(protected, bind_sql)
            body = self._pl_restore_targets(body, targets)
            plain = self._strip_begin_end(body)
            # E1 静默语义审计：过程体里的结果集（`SELECT ...`）旧实现被
            # **静默丢弃**（CALL 只回 OK，客户端看不到任何结果却以为成功）。
            # 现在的口径：
            #   * 单条无 INTO 的 SELECT → 按 MySQL 行为返回该结果集；
            #   * 多条 / 混合结果集 → 显式 1235（不实现多结果集，但
            #     绝不假装成功）；`SELECT ... INTO var` 不是结果集，放行。
            sel_stmts = self._procedure_result_set_statements(plain)
            if sel_stmts:
                all_stmts = [x.strip() for x in plain.split(';') if x.strip()]
                if (len(sel_stmts) == 1 and len(all_stmts) == 1
                        and not out_slots):
                    return self.execute_sql(sel_stmts[0], s)
                return self._err(
                    1235,
                    f"CALL {proc.name}: 过程体包含返回结果集的 SELECT，"
                    f"本引擎 CALL 不返回多结果集（不静默丢弃结果）。"
                    f"请改用普通 SELECT，或在过程里用 SELECT ... INTO")
            # **统一走 PL 解释器**（过程体与函数体共用一份实现）：
            # 不再区分"有没有控制流"。旧实现把纯 SQL 过程体交给
            # `_run_statement_list`（主 SQL 解析器），于是 `SET s = 42`
            # 这种 PL 赋值语句解析失败、OUT 参数也永远回填不了。
            ctx = self._exec_pl_body(plain, s, bind_val)
            if self._pl_signal_set(ctx):
                return self._err(
                    1644, f"CALL {proc.name}: SIGNAL SQLSTATE "
                          f"{ctx.signal_state}: {ctx.signal_message}")
            self._write_back_out_params(ctx, out_slots, s)
        except Exception as e:
            return self._err(1105, f"CALL {proc.name} 失败: {e}")
        return self._ok(message=f"Procedure '{proc.name}' called")

    @staticmethod
    def _procedure_result_set_statements(body: str):
        """过程体里**会返回结果集**的 SELECT 语句（`SELECT ... INTO` 不算）

        用于 E1 静默语义审计：旧实现把这些 SELECT 静默丢弃。这里只做
        词法级判定（过程体是用户写的 SQL/PL 文本，不是 AST）：
        顶层以 SELECT 开头且不含 INTO 的语句即视为返回结果集。
        """
        out = []
        for stmt in str(body or '').split(';'):
            s = stmt.strip()
            if not re.match(r'(?is)^SELECT\b', s):
                continue
            if re.search(r'(?is)\bINTO\b', s):
                continue               # SELECT ... INTO var：赋值语句
            out.append(s)
        return out

    #: 过程调用的用户变量存放位置（会话 dict 里的键名）
    _PL_USER_VARS_KEY = 'user_vars'

    def _pl_user_var_store(self, session):
        """取"用户变量表"（`@name` 的唯一权威存储）

        三个写入/读取点必须共用同一份表，否则 OUT 参数回填就成了
        "写在一处、读在另一处"的静默 NULL：
        * 会话 dict 里的 `user_vars`（同一会话内的后续语句）；
        * 本执行器自持的 `_pl_user_vars`（`execute_sql(sql, {})` 每次
          都新建会话 dict，只写会话里下一次语句就读不到）；
        * `variables.get_vm()` 里按 session_id 的会话（`SELECT @x`
          在没有会话 dict 时按 session_id 查它）。
        本方法返回前两者共用的同一份 dict（挂到执行器上），第三份由
        调用方按 session_id 补写。
        """
        store = getattr(self, '_pl_user_vars', None)
        if not isinstance(store, dict):
            store = self._pl_user_vars = {}
        if isinstance(session, dict):
            sess_store = session.get(self._PL_USER_VARS_KEY)
            if not isinstance(sess_store, dict):
                session[self._PL_USER_VARS_KEY] = store
            else:
                store = sess_store
                self._pl_user_vars = store
        return store

    def _write_back_out_params(self, ctx, out_slots, session):
        """把 OUT / INOUT 形参的最终值写回实参的 `@用户变量`

        为什么必须写回：不写回的话调用方拿到的永远是 NULL —— 那是静默
        错值（`CALL p(@c)` 之后 `SELECT @c` 是 0/空），比显式报错更糟。
        实参不是用户变量时按 MySQL 语义丢弃（不报错）。
        """
        if not out_slots:
            return
        store = self._pl_user_var_store(session)
        # 全局 `variables` 模块的会话（按 session_id）也写一份：
        # `SELECT @x` 在没有会话 dict 时查的就是它。
        sess_obj = None
        sid = session.get('session_id') or session.get('id') \
            if isinstance(session, dict) else None
        if sid is not None:
            try:
                from .variables import get_vm
                sess_obj = get_vm().get_session(int(sid))
            except Exception:                          # pragma: no cover
                sess_obj = None
        for pname, raw in out_slots:
            name = str(raw or '').strip()
            if not name:
                continue
            # 实参是 PL 局部变量（`CALL inner(n, tmp)`）时也要回填 ——
            # 只认 `@user` 会让嵌套调用里的局部变量永远保持旧值
            # （实测 `outer_proc(4)` 返回 0 而不是 50，静默错值）。
            # 非用户变量的实参在 MySQL 顶层 CALL 里是"值被丢弃"，但对
            # 存储程序内部的 CALL 必须是可写的局部变量引用。
            key = name[1:] if name.startswith('@') else name
            value = ctx.variables.get(pname)
            store[key] = value
            if sess_obj is not None and name.startswith('@'):
                try:
                    sess_obj.set_user(name, value)
                except Exception:                      # pragma: no cover
                    pass

    def _fire_triggers(self, timing, event, table, old_row=None,
                       new_row=None, session=None):
        """执行表上的 BEFORE/AFTER 触发器（真的执行触发器体）

        * 体内 `NEW.列` / `OLD.列` 会被替换成真实行的字面量（引号内不替换）；
        * BEFORE 触发器里的 `SET NEW.列 = 表达式` 会真改写入的行值（本方法
          直接改 `new_row` 列表，调用方随后写入的就是改后的值）；
        * 触发器体执行失败会向上抛出 → 语句以显式错误结束（不静默）。
        """
        mgr = self._trigger_mgr
        if mgr is None:
            return
        tlist = list(getattr(mgr, 'triggers', {}).get(table) or [])
        if not tlist:
            return
        tbl = self.db.get_table(table)
        cols = [c['name'] for c in tbl.cols]
        env = {
            'NEW': dict(zip(cols, new_row)) if new_row is not None else None,
            'OLD': dict(zip(cols, old_row)) if old_row is not None else None,
        }
        for trig in tlist:
            if trig.timing != timing or trig.event != event:
                continue
            self._run_trigger_body(trig, tbl, cols, env, new_row, old_row,
                                   session)

    def _run_trigger_body(self, trig, tbl, cols, env, new_row, old_row,
                          session):
        refs = {}
        if env.get('NEW') is not None:
            for c in cols:
                refs[f'NEW.{c}'] = self._sql_literal(env['NEW'].get(c))
        if env.get('OLD') is not None:
            for c in cols:
                refs[f'OLD.{c}'] = self._sql_literal(env['OLD'].get(c))
        stmts = self._split_statements(self._strip_begin_end(trig.body))
        self._run_trigger_stmts(stmts, trig, tbl, cols, env, new_row,
                                old_row, session, refs)

    def _run_trigger_stmts(self, stmts, trig, tbl, cols, env, new_row,
                           old_row, session, refs):
        """执行触发器语句序列，识别 `IF ... THEN ... [ELSE ...] END IF`

        自实现的小解释器（触发器体的形态有限：SET NEW/OLD 赋值 + 普通 SQL +
        IF 判断）。IF 块用语句级配对（嵌套时按深度配对），条件里的
        `NEW.列` / `OLD.列` / 字面量由 `_eval_trigger_expr` 求值。
        `ELSEIF` 未实现 → 显式报错，不静默跳过分支。
        """
        i = 0
        while i < len(stmts):
            s = stmts[i].strip()
            im = re.match(r'^IF\s+(.+?)\s+THEN\s*(.*)$', s,
                          re.IGNORECASE | re.DOTALL)
            if not im:
                self._run_trigger_stmt(s, trig, tbl, cols, env, new_row,
                                       old_row, session, refs)
                i += 1
                continue
            cond = im.group(1)
            then_body = [im.group(2)] if im.group(2).strip() else []
            else_body = []
            target = then_body
            depth = 0
            i += 1
            while i < len(stmts):
                cur = stmts[i].strip()
                if re.match(r'^ELSEIF\b', cur, re.IGNORECASE):
                    raise RuntimeError(
                        f"触发器 {trig.name}: 触发器体里的 ELSEIF 暂不支持"
                        f"（请改用嵌套 IF）")
                if re.match(r'^IF\b', cur, re.IGNORECASE):
                    depth += 1
                elif re.match(r'^END\s+IF$', cur, re.IGNORECASE):
                    if depth == 0:
                        i += 1
                        break
                    depth -= 1
                em = re.match(r'^ELSE\s*(.*)$', cur, re.IGNORECASE | re.DOTALL)
                if em is not None and depth == 0:
                    target = else_body
                    if em.group(1).strip():
                        target.append(em.group(1))
                    i += 1
                    continue
                target.append(cur)
                i += 1
            if self._eval_trigger_condition(cond, env, trig.name):
                self._run_trigger_stmts(then_body, trig, tbl, cols, env,
                                        new_row, old_row, session, refs)
            elif else_body:
                self._run_trigger_stmts(else_body, trig, tbl, cols, env,
                                        new_row, old_row, session, refs)

    def _run_trigger_stmt(self, stmt, trig, tbl, cols, env, new_row, old_row,
                          session, refs):
        setm = re.match(
            r'SET\s+(NEW|OLD)\.(\w+)\s*=\s*(.+)$', stmt,
            re.IGNORECASE | re.DOTALL)
        if setm:
            which = setm.group(1).upper()
            col = setm.group(2)
            if col not in cols:
                raise RuntimeError(
                    f"触发器 {trig.name}: 列 {col} 在表 {tbl.name} 上不存在")
            if trig.timing != 'BEFORE':
                raise RuntimeError(
                    f"触发器 {trig.name}: 只有 BEFORE 触发器可以给 "
                    f"{which}.{col} 赋值")
            if env.get(which) is None:
                raise RuntimeError(
                    f"触发器 {trig.name}: 该事件没有 {which} 行")
            value = self._eval_trigger_expr(setm.group(3), env, trig.name)
            env[which][col] = value
            target = new_row if which == 'NEW' else old_row
            if target is not None:
                target[cols.index(col)] = value
            return
        sql = self._substitute_words(stmt, refs)
        from .parser import parser as _p
        parsed = _p.parse(sql)
        if parsed is None:
            raise RuntimeError(
                f"触发器 {trig.name} 里的语句无法解析: {stmt[:120]}")
        res = self.execute(parsed, session)
        if isinstance(res, dict) and res.get('type') == 'error':
            raise RuntimeError(
                f"触发器 {trig.name} 执行失败: {res.get('message')}")

    def _eval_trigger_condition(self, cond, env, trig_name):
        """触发器 IF 条件求值（NULL 语义按 SQL 三值：UNKNOWN -> 不成立）"""
        from .parser import parser as _p
        c = str(cond).strip()
        for kw in ('OR', 'AND'):
            parts = _p._split_cond_top_level(c, kw)
            if len(parts) > 1:
                vals = [self._eval_trigger_condition(x, env, trig_name)
                        for x in parts]
                return any(vals) if kw == 'OR' else all(vals)
        nm = re.match(r'^NOT\s+(.+)$', c, re.IGNORECASE | re.DOTALL)
        if nm:
            return not self._eval_trigger_condition(
                nm.group(1), env, trig_name)
        zm = re.match(r'^(.*?)\s+IS\s+(NOT\s+)?NULL$', c,
                      re.IGNORECASE | re.DOTALL)
        if zm:
            v = self._eval_trigger_expr(zm.group(1), env, trig_name)
            return v is not None if zm.group(2) else v is None
        cm = re.match(r'^(.*?)\s*(<=|>=|<>|!=|=|<|>)\s*(.*)$', c,
                      re.IGNORECASE | re.DOTALL)
        if cm:
            left = self._eval_trigger_expr(cm.group(1), env, trig_name)
            right = self._eval_trigger_expr(cm.group(3), env, trig_name)
            if left is None or right is None:
                return False             # SQL 里与 NULL 比较是 UNKNOWN
            op = cm.group(2)
            if op == '=':
                return left == right
            if op in ('!=', '<>'):
                return left != right
            if op == '<':
                return left < right
            if op == '>':
                return left > right
            if op == '<=':
                return left <= right
            return left >= right
        return bool(self._eval_trigger_expr(c, env, trig_name))

    def _eval_trigger_expr(self, expr, env, trig_name):
        node = _parse_scalar_expr(expr)
        if node is None:
            raise RuntimeError(
                f"触发器 {trig_name}: 表达式无法解析: {str(expr)[:80]}")

        def resolve(name):
            n = str(name)
            if '.' in n:
                which, col = n.split('.', 1)
                row = env.get(which.upper())
                if isinstance(row, dict) and col in row:
                    return row[col]
            raise ValueError(f"列 {n} 在触发器上下文里解析不到")
        return _eval_expr_node(node, resolve, position='触发器')

    # ==================== SHOW ====================

    def _cmd_show_tables(self, p, s):
        db_name = s.get('database') or self.db.name
        if not self.db.is_known_database(db_name):
            # 不存在的库上 `SHOW TABLES` 必须报错（MySQL 1049），
            # 而不是把别的库的表列出来冒充。
            return self._err(
                1049, f"Unknown database '{db_name}'", sqlstate='42000')
        cols = [f"Tables_in_{db_name}"]
        rows = [(name,) for name in self.db.visible_table_names(db_name)]
        return self._rs(cols, rows)

    def _cmd_show_databases(self, p, s):
        """`SHOW DATABASES` —— 引擎**真实**支持、且当前账户**可见**的库

        依据见 `Database.database_names()`：一个真实的单库 + 真实可查询的
        `information_schema` 虚拟库。非管理员只看到自己有权限的库
        （`information_schema` 作为系统库保持最小可用，所有账户可见）；
        未知用户看不到任何库。
        """
        return self._rs(
            ['Database'],
            [(name,) for name in self.visible_databases(s)])

    def _cmd_show_columns(self, p, s):
        """`SHOW COLUMNS` / `DESC` / `DESCRIBE` —— 来自真实列定义

        Type（保留长度/精度）、Default（真实默认值或 NULL）、Extra（没有
        对应概念就留空，**不**由主键推断）三者与 `information_schema`
        共用 `db/database.py::describe_columns`，不会再各写一份。
        """
        tbl = self.db.get_table(p['table'])
        cols = ['Field', 'Type', 'Null', 'Key',
                'Default', 'Extra']
        rows = [
            (c['name'], c['type'], c['null'], c['key'],
             c['default'], c['extra'])
            for c in describe_columns(tbl)
        ]
        return self._rs(cols, rows)

    def _cmd_show_stats(self, p, s):
        """SHOW STATS - 表统计"""
        cols = ['table', 'rows', 'pages', 'btree_height']
        rows = []
        for name, tbl in self.db.tables.items():
            st = tbl.stats()
            rows.append((
                name, st['rows'], st['pages'],
                st['btree_height']))
        return self._rs(cols, rows)

    # ==================== E1：第三方客户端元数据 SHOW ====================
    #
    # 这批语句原先全部 1064（Navicat/mysql CLI 的元数据面板依赖它们）。
    # 取值原则与项目一贯口径一致：**真实信息**，不可用的字段填 NULL
    # 并注释原因；不硬编码 MySQL 全量假数据。
    # 派发入口：parser._show_metadata_stmt → show_stmt 标记 →
    # `_exec_show_stmt` 按 `_show_<kind>` 反射。

    #: 字符串列在本引擎里的**真实**排序规则：Python 码点比较（大小写敏感）。
    #: 报 `utf8mb4_bin` 而不是 MySQL 默认的 `_general_ci`，避免客户端据此
    #: 以为大小写不敏感的比较会生效。
    _REAL_COLLATION = 'utf8mb4_bin'

    @staticmethod
    def _like_match(value: str, pattern) -> bool:
        """SQL LIKE 语义（% / _）到正则的转换；pattern 为 None 表示不过滤"""
        if pattern is None:
            return True
        import re as _re
        rx = ('^' + _re.escape(str(pattern))
              .replace(r'\%', '.*').replace(r'\_', '.') + '$')
        return bool(_re.match(rx, str(value), _re.IGNORECASE | _re.DOTALL))

    def _show_table_status(self, p, s):
        """`SHOW TABLE STATUS [FROM db] [LIKE ...]`（MySQL 5.7 列形状）"""
        db_name = p.get('database') or s.get('database') or self.db.name
        if not self.db.is_known_database(db_name):
            return self._err(1049, f"Unknown database '{db_name}'",
                             sqlstate='42000')
        cols = ['Name', 'Engine', 'Version', 'Row_format', 'Rows',
                'Avg_row_length', 'Data_length', 'Max_data_length',
                'Index_length', 'Data_free', 'Auto_increment', 'Create_time',
                'Update_time', 'Check_time', 'Collation', 'Checksum',
                'Create_options', 'Comment']
        rows = []
        for name, tbl in list(self.db.tables.items()):
            if not self._like_match(name, p.get('like')):
                continue
            try:
                n_rows = len(tbl._visible_pks(None))
            except Exception:
                n_rows = int(getattr(tbl, 'row_count', 0) or 0)
            dat_path = self.db.data_dir / f'{name}.dat'
            try:
                data_len = os.path.getsize(dat_path) if os.path.exists(
                    dat_path) else None
            except OSError:
                data_len = None
            try:
                from datetime import datetime
                ctime = datetime.fromtimestamp(
                    os.path.getmtime(dat_path)).strftime(
                        '%Y-%m-%d %H:%M:%S')
            except OSError:
                ctime = None
            # Index_length 不可用：BTree 页与堆页在同一个 .dat 里，
            # 拆不出独立索引长度 → NULL（不编造）。
            # Data_free：已登记的空闲页 × 4096（真实可复用字节）。
            try:
                data_free = len(getattr(tbl, 'freelist').pages) * 4096
            except Exception:
                data_free = None
            auto_inc = None
            try:
                if (tbl.cols and tbl.cols[tbl.pk_idx].get('type', '').upper()
                        in ('INT', 'BIGINT')):
                    auto_inc = int(getattr(tbl, '_next_pk', 0) or 0)
            except Exception:
                auto_inc = None
            opts = getattr(tbl, 'table_options', {}) or {}
            # E2：Engine 列如实显示"请求的引擎名"（兼容别名）；真实存储
            # 始终是 LightDB 原生行存，因此 Create_options 一并标明
            # `storage=LightDB-native`，避免客户端以为 InnoDB 语义生效。
            engine = (getattr(tbl, 'requested_engine', None)
                      or opts.get('engine') or 'LightDB')
            create_options = ('' if engine == 'LightDB'
                              else 'storage=LightDB-native')
            rows.append((
                name, engine, 10, 'Dynamic', n_rows,
                (data_len // n_rows) if (data_len and n_rows) else 0,
                data_len, None, None, data_free, auto_inc,
                ctime, None, None,
                opts.get('collate') or self._REAL_COLLATION,
                None, create_options, getattr(tbl, 'comment', '') or '',
            ))
        return self._rs(cols, rows)

    def _show_full_tables(self, p, s):
        """`SHOW FULL TABLES [FROM db] [LIKE ...]`：Tables_in_x + Table_type"""
        db_name = p.get('database') or s.get('database') or self.db.name
        if not self.db.is_known_database(db_name):
            return self._err(1049, f"Unknown database '{db_name}'",
                             sqlstate='42000')
        col = f'Tables_in_{db_name}'
        rows = [(name, 'BASE TABLE')
                for name in self.db.visible_table_names(db_name)
                if self._like_match(name, p.get('like'))]
        return self._rs([col, 'Table_type'], rows)

    def _show_full_columns(self, p, s):
        """`SHOW FULL COLUMNS FROM t [FROM db] [LIKE ...]`（MySQL 9 列）"""
        tbl = self.db.get_table(p.get('table'))
        cols = ['Field', 'Type', 'Collation', 'Null', 'Key', 'Default',
                'Extra', 'Privileges', 'Comment']
        str_types = ('VARCHAR', 'TEXT', 'CHAR', 'MEDIUMTEXT', 'LONGTEXT',
                     'TINYTEXT', 'JSON')
        rows = []
        for c in describe_columns(tbl):
            if not self._like_match(c['name'], p.get('like')):
                continue
            base_type = str(c['type']).split('(')[0].upper()
            coll = self._REAL_COLLATION if base_type in str_types else None
            raw = next((x for x in (getattr(tbl, 'cols', []) or [])
                        if x.get('name') == c['name']), {})
            gen = raw.get('generated') or None
            extra = c.get('extra', '')
            if gen:
                extra = 'STORED GENERATED' if gen.get('stored') \
                    else 'VIRTUAL GENERATED'
            rows.append((c['name'], c['type'], coll, c['null'], c['key'],
                         c['default'], extra,
                         'select,insert,update,references',
                         raw.get('comment') or ''))
        return self._rs(cols, rows)

    def _show_collation(self, p, s):
        """`SHOW COLLATION [LIKE ...]`：只返回本引擎**真实支持**的排序规则"""
        cols = ['Collation', 'Charset', 'Id', 'Default', 'Compiled',
                'Sortlen']
        rows = []
        for cid, (coll, charset, default) in enumerate((
                ('utf8mb4_bin', 'utf8mb4', 'Yes'),
                ('utf8_bin', 'utf8', 'Yes')), start=1):
            if not self._like_match(coll, p.get('like')):
                continue
            rows.append((coll, charset, cid, default, 'Yes', 1))
        return self._rs(cols, rows)

    def _show_charset(self, p, s):
        """`SHOW CHARSET` / `SHOW CHARACTER SET [LIKE ...]`（真实支持集合）"""
        cols = ['Charset', 'Description', 'Default collation', 'Maxlen']
        rows = []
        for charset, desc, coll, maxlen in (
                ('utf8mb4', 'UTF-8 Unicode', 'utf8mb4_bin', 4),
                ('utf8', 'UTF-8 Unicode', 'utf8_bin', 3)):
            if not self._like_match(charset, p.get('like')):
                continue
            rows.append((charset, desc, coll, maxlen))
        return self._rs(cols, rows)

    def _show_engines(self, p, s):
        """`SHOW ENGINES`：只有真实存在的 LightDB 引擎（不列 MySQL 假引擎）"""
        cols = ['Engine', 'Support', 'Comment', 'Transactions', 'XA',
                'Savepoints']
        rows = [('LightDB', 'DEFAULT',
                 'LightDB pure-Python row-store engine'
                 '（OLAP 向量化 dispatch 可选、默认关闭）',
                 'YES', 'NO', 'YES')]
        return self._rs(cols, rows)

    def _show_warnings(self, p, s):
        """`SHOW WARNINGS` / `SHOW ERRORS`：列名与 MySQL 对齐

        本引擎**没有 warning 机制**（错误一律显式返回，不降级成 warning），
        因此这里是**空结果集**。若执行器将来记录会话级 warning，可把
        `s['warnings']` 填进来；当前如实为空，不伪造任何条目。
        """
        cols = ['Level', 'Code', 'Message']
        rows = list((s or {}).get('warnings') or [])
        return self._rs(cols, rows)

    def _show_metadata_noop(self, p, s):        # pragma: no cover - 防御
        return self._err(1064, "不支持的 SHOW 语句")

    # ==================== 事务 ====================

    def _cmd_begin(self, p, s):
        txn = self.db.begin()
        s['txn'] = txn
        return self._ok(
            message=f"Transaction started (id={txn['txn_id']})")

    def _cmd_commit(self, p, s):
        txn = s.pop('txn', None)
        if txn:
            self.db.commit(txn)
            self.stats['commits'] += 1
            return self._ok(
                message=f"Committed (id={txn['txn_id']})")
        return self._ok(message="Committed")

    def _cmd_rollback(self, p, s):
        txn = s.pop('txn', None)
        if txn:
            self.db.abort(txn)
            return self._ok(
                message=f"Rolled back (id={txn['txn_id']})")
        return self._ok(message="Rolled back")

    # ---------- E1：SAVEPOINT / RELEASE / ROLLBACK TO ----------
    #
    # 语义依据（MySQL 5.7 Reference Manual 13.3.4「SAVEPOINT,
    # ROLLBACK TO SAVEPOINT, and RELEASE SAVEPOINT」）：
    #   * 同名 SAVEPOINT 会删除旧保存点并设置新保存点（可重名嵌套）；
    #   * RELEASE SAVEPOINT 删除该名字**以及其后**的所有保存点；
    #   * ROLLBACK TO SAVEPOINT 撤销该保存点之后的数据修改，**保留**
    #     该保存点本身、删除其后保存点；
    #   * 不存在的保存点（RELEASE/ROLLBACK TO）→ MySQL 1305
    #     `SAVEPOINT xxx does not exist`；SAVEPOINT 在无活动事务时是
    #     空操作（autocommit 下不报错）。
    # 实现：复用语句级保存点的物理 undo 栈（mvcc.undo_marks）；
    # 回滚只撤销进程内数据（与 ROLLBACK 同一条 undo 路径）。
    # 已知限制（如实记录，测试注释同口径）：UPDATE/DELETE 的 WAL 记录
    # 没有为"保存点回滚后再 COMMIT"写补偿记录，因此"回滚到保存点 →
    # COMMIT → 进程崩溃重启"后这些 UPDATE/DELETE 会被 redo 重放。
    # 单进程内语义（可见性）完全正确；崩溃一致性需后续补补偿 WAL。

    _ER_SP_DOES_NOT_EXIST = 1305

    @staticmethod
    def _savepoint_missing(name: str):
        return {'type': 'error', 'code': 1305,
                'sqlstate': '42000',
                'message': f'SAVEPOINT {name} does not exist'}

    def _cmd_savepoint(self, p, s):
        name = p['name']
        txn = s.get('txn') if isinstance(s, dict) else None
        if not txn:
            # MySQL：无活动事务时 SAVEPOINT 为空操作（返回 OK）
            return self._ok(
                message=f"Savepoint '{name}' set "
                        f"(no active transaction: no-op)")
        marks = self.db.mvcc.undo_marks(txn['txn_id'])
        sps = txn.setdefault('savepoints', [])
        # 同名旧保存点先删除（MySQL 语义），再压入新保存点
        sps[:] = [x for x in sps if x[0] != name]
        sps.append((name, marks))
        return self._ok(message=f"Savepoint '{name}' set")

    def _cmd_release_savepoint(self, p, s):
        name = p['name']
        txn = s.get('txn') if isinstance(s, dict) else None
        sps = (txn or {}).get('savepoints') or []
        idx = next((i for i, (n, _m) in enumerate(sps) if n == name), -1)
        if not txn or idx < 0:
            return self._savepoint_missing(name)
        del sps[idx:]                 # 删除该保存点及其后所有保存点
        return self._ok(message=f"Savepoint '{name}' released")

    def _cmd_rollback_to_savepoint(self, p, s):
        name = p['name']
        txn = s.get('txn') if isinstance(s, dict) else None
        sps = (txn or {}).get('savepoints') or []
        idx = next((i for i, (n, _m) in enumerate(sps) if n == name), -1)
        if not txn or idx < 0:
            return self._savepoint_missing(name)
        marks = sps[idx][1]
        tid = txn['txn_id']
        restored = 0
        for tbl in list(self.db.tables.values()):
            try:
                restored += tbl.rollback_statement(tid, marks)
            except Exception as e:               # pragma: no cover - 防御
                logger.warn(f"ROLLBACK TO SAVEPOINT {name} 回滚表 "
                            f"{tbl.name} 失败: {e}")
        # 保留命名保存点，删除其后保存点（MySQL 语义）
        del sps[idx + 1:]
        return self._ok(
            message=f"Rolled back to savepoint '{name}' "
                    f"({restored} row version(s) reverted)")

    # ---------- E1：SET NAMES（进程内执行器路径）----------
    #
    # 网络路径由 net/server.py::_handle_set 处理（协议会话层）；进程内
    # `Executor.execute_sql` 也必须有同名命令，否则两套入口语义不一致。
    # 只接受真实支持的字符集（UTF-8 存储/收发）；gbk/latin1 不做转码 →
    # 显式 1115，绝不"按 utf8 收下"。
    def _cmd_set_names(self, p, s):
        charset = 'utf8mb4' if p.get('default') else (p.get('charset') or '')
        charset = charset or 'utf8mb4'
        if charset not in self.OLAP_E1_CHARSETS:
            return self._err(
                1115, f"Unknown character set: '{charset}'",
                sqlstate='42000')
        collation = p.get('collation') or ''
        if collation and collation not in self.OLAP_E1_COLLATIONS:
            return self._err(
                1273, f"Unknown collation: '{collation}'", sqlstate='HY000')
        if isinstance(s, dict):
            s.setdefault('variables', {}).update({
                'character_set_client': charset,
                'character_set_connection': charset,
                'character_set_results': charset,
                'collation_connection':
                    collation or ('utf8_bin' if charset == 'utf8'
                                  else 'utf8mb4_bin'),
            })
        return self._ok(message="Query OK")

    # ==================== 复制控制语句（最小实现） ====================

    #: MySQL `ER_SLAVE_NOT_CONFIGURED`：STOP/START SLAVE 时没有配置主库
    _ER_SLAVE_NOT_CONFIGURED = 1190

    def _replication_not_configured(self, action: str):
        return self._err(
            self._ER_SLAVE_NOT_CONFIGURED,
            f"Slave is not configured or failed to initialize properly. "
            f"{action} requires CHANGE MASTER TO first "
            f"（本引擎：先执行 CHANGE MASTER TO MASTER_HOST='..', "
            f"MASTER_PORT=..，再 START SLAVE）")

    def _cmd_start_slave(self, p, s):
        """`START SLAVE` / `START REPLICA`：启动已配置的从库复制器

        未配置（`Database.replicator` 为 None）→ 显式 1190，不伪装成功。
        """
        rep = getattr(self.db, 'replicator', None)
        if rep is None:
            return self._replication_not_configured('START SLAVE')
        try:
            rep.start()
        except Exception as e:
            return self._err(1105, f"START SLAVE 失败: {e}")
        return self._ok(message='Slave started')

    def _cmd_stop_slave(self, p, s):
        """`STOP SLAVE` / `STOP REPLICA`：停止复制线程，保留配置与位点"""
        rep = getattr(self.db, 'replicator', None)
        if rep is None:
            return self._replication_not_configured('STOP SLAVE')
        try:
            rep.stop()
        except Exception as e:
            return self._err(1105, f"STOP SLAVE 失败: {e}")
        return self._ok(message='Slave stopped')

    def _cmd_change_master(self, p, s):
        """`CHANGE MASTER TO MASTER_HOST=.., MASTER_PORT=.., MASTER_USER=..,
        MASTER_PASSWORD=..`：登记/替换主库配置（构造从库复制器，不自动启动）

        语义照 MySQL：CHANGE MASTER 只改配置；要开始复制需再 `START SLAVE`。
        参数缺失/非法 → 1064 显式报错（不静默用默认值蒙混）。
        """
        host = p.get('master_host')
        port = p.get('master_port')
        if not host or str(host).strip() == '':
            return self._err(
                1064, "CHANGE MASTER TO 缺少 MASTER_HOST='主机'")
        try:
            port = int(port if port is not None else 3306)
        except (TypeError, ValueError):
            return self._err(
                1064, f"CHANGE MASTER TO 的 MASTER_PORT 非法: {port!r}")
        if not (0 < port < 65536):
            return self._err(
                1064, f"CHANGE MASTER TO 的 MASTER_PORT 越界: {port}")
        user = p.get('master_user')
        password = p.get('master_password')
        if user is None:
            # MySQL：未指定 MASTER_USER 时使用当前连接用户
            user = (s.get('user') if isinstance(s, dict) else None) or 'root'
        try:
            from ..replication.slave import SlaveReplicator
            old = getattr(self.db, 'replicator', None)
            if old is not None:
                try:
                    old.stop()
                except Exception as e:         # pragma: no cover - 防御
                    logger.warn(f"替换主库配置时停止旧复制器失败: {e}")
            SlaveReplicator(self.db, str(host), int(port),
                            user=str(user), password=str(password or ''))
        except Exception as e:
            return self._err(1105, f"CHANGE MASTER TO 失败: {e}")
        return self._ok(
            message=f"Master changed to {host}:{port} (user={user}); "
                    f"run START SLAVE to begin replication")

    def _cmd_use(self, p, s):
        """`USE <库>` —— **校验**后切换（不存在 → 1049）

        旧实现无条件 `s['database'] = p['database']` 然后回 OK：切到
        `no_such_db` 也"成功"，而 `SHOW TABLES` 又完全不看库名，于是
        使用者得到一个"切库成功了"的假象 + 一份与所选库无关的表清单。
        本引擎是单库模型，因此这里只接受**真实存在**的库
        （`Database.database_names()`：真实单库 + `information_schema`），
        其余一律显式报错——不静默忽略。
        """
        name = p['database']
        if not self.db.is_known_database(name):
            return self._err(
                1049, f"Unknown database '{name}'", sqlstate='42000')
        s['database'] = name
        return self._ok(
            message=f"Database changed to {name}")

    def _cmd_select_variables(self, p, s):
        """`SELECT @var[, @var]` —— 读**用户变量**（MySQL 13.7.6.2）

        `CALL p(@out)` 之后必须有办法把 `@out` 读出来验证回填，否则
        OUT/INOUT 的正确性无法从外部观测（旧实现直接报"列 '@s' 不存在"）。
        取值顺序与本执行器写回 OUT 参数的位置一致：
        会话 `user_vars` → `variables` 模块的用户变量表（按 session_id）。
        未赋值的用户变量是 NULL（MySQL 语义），不是错误。
        """
        store = self._pl_user_var_store(s)
        sess_obj = None
        try:
            from .variables import get_vm
            sid = s.get('session_id') if isinstance(s, dict) else None
            if sid is not None:
                sess_obj = get_vm().get_session(int(sid))
        except Exception:                              # pragma: no cover
            sess_obj = None
        cols, values = [], []
        for v in p.get('variables') or []:
            cols.append(v)
            name = str(v).lstrip('@$')
            value = None
            if isinstance(store, dict) and name in store:
                value = store[name]
            elif sess_obj is not None:
                value = sess_obj.get_user(name)
            values.append(value)
        return self._rs(cols, [tuple(values)])

    def _cmd_explain(self, p, s):
        """EXPLAIN / EXPLAIN ANALYZE

        `EXPLAIN ANALYZE` 会**真的执行**内层语句并报告实测耗时与行数
        （MySQL 8 的 EXPLAIN ANALYZE 语义），不是只打印计划。
        """
        inner = p.get('inner')
        if not inner or self.optimizer is None:
            return self._err(1105, "EXPLAIN 无法解析")

        # ★ 与 _cmd_select 同一份静态校验：非法 SQL 在 EXPLAIN 阶段就报错，
        # 不要等真正执行时才发现（MySQL 的 ONLY_FULL_GROUP_BY 同样如此）
        self._check_strict_group_by(inner)
        plan = self.optimizer.optimize(inner)
        try:
            cols, rows, plan_text = self.optimizer.explain(plan)
        except Exception as e:
            return self._err(1105, f"EXPLAIN 失败: {e}")

        if not p.get('analyze'):
            return self._rs(cols, rows)

        # ---- EXPLAIN ANALYZE：真实执行 + 实测统计 ----
        # MySQL 8 的 EXPLAIN ANALYZE 输出单列 'EXPLAIN'（计划树 + actual
        # time/rows 标注），这里同样给：真实计划树 + 实测耗时/行数。
        t0 = time.time()
        res = self.execute(inner, s)
        elapsed = time.time() - t0
        if isinstance(res, dict) and res.get('type') == 'error':
            return self._err(
                int(res.get('code') or 1105),
                f"EXPLAIN ANALYZE 执行失败: {res.get('message')}")
        n_rows = len(res.get('rows') or []) if isinstance(res, dict) else 0
        text = plan_text if isinstance(plan_text, str) else '\n'.join(
            str(x) for x in (plan_text or []))
        out = [[line] for line in text.splitlines() if line.strip()]
        out.append([f"实际耗时: {elapsed * 1000:.3f} ms (rows={n_rows})"])
        return self._rs(['EXPLAIN'], out)

    # ==================== 事件调度器 ====================

    def start_event_scheduler(self):
        """启动事件调度器（可选）"""
        if self._event_scheduler is not None:
            return
        try:
            from ..sql.event import EventScheduler
            self._event_scheduler = EventScheduler(
                str(self.db.data_dir), self)
            self._event_scheduler.start()
            logger.info("事件调度器已启动")
        except ImportError:
            logger.debug("EventScheduler 未提供，跳过")
        except Exception as e:
            logger.warn(f"事件调度器启动失败: {e}")

    def stop_event_scheduler(self):
        """停止事件调度器（以及并行执行器的线程池）

        `tests/conftest.py` 的 `shutdown_quietly` 对 Executor 调用的就是这个
        方法，因此并行线程池也必须在**失败路径**上被关掉，否则工作线程会
        活到会话结束、持有 BufferPool 句柄，下一个用例的 `reset()` 就撞
        winerror=32（docs/FAILURE_TRIAGE.md 第 4 节记录的级联形态）。
        """
        if self._event_scheduler is not None:
            try:
                self._event_scheduler.stop()
            except Exception:
                pass
            self._event_scheduler = None
        if self._parallel is not None:
            try:
                self._parallel.shutdown()
            except Exception:
                pass
            self._parallel = None

    # ==================== 统计 ====================

    def get_stats(self) -> dict:
        return dict(self.stats)

    def stats_report(self) -> dict:
        return {
            **self.stats,
            'tables': len(self.db.tables),
            'has_optimizer': self.optimizer is not None,
        }