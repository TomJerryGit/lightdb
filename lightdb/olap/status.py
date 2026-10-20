# lightdb/olap/status.py
"""OLAP / 列存层的**接线状态常量与显式检查点**（决策 2：4A.2 真接线后）。

历史（阶段 2 第 6 项，方案 A「摘牌 + 显式化」）：当时 OLAP/列存与 SQL 路径
零引用，本模块用 `WIRED_TO_SQL_PATH = False` 把「未接线」登记为事实，并让
实验性入口显式报错。

**现状（4A.2，2026-10）**：已**真接线**——`Executor` 的 SELECT 分支在
`config.olap_dispatch` 开启时会调用
`lightdb/olap/sql_dispatch.py::try_vectorized_select`，对白名单语法（单表
聚合/分组、简单 WHERE/ORDER BY/LIMIT）走向量化执行；其余语法一律返回
None 走行存。因此：

* `WIRED_TO_SQL_PATH = True`：接线是真实存在的（不是文档口径）；
* `DEFAULT_ENABLED = False`：**默认关闭**。不设 `LIGHTDB_OLAP_DISPATCH=1`
  或 `Config(olap_dispatch=True)` 时，SQL 执行与接线前完全一致（行存），
  且不会 import 任何 OLAP/列存引擎模块（延迟 import）；
* `COUNTS_FOR_PERFORMANCE_CLAIMS = True`（4A.5 起）：该层已有**可辩护的基准
  门禁**（`benchmarks/gate.py: olap_parallel_groupby_speedup` 并行加速比、
  `olap_dispatch_crosscheck_mismatches` 对拍不一致数=0），且实测数字写在
  `benchmarks/README.md`；对外只允许引用这些实测/门禁数字，不得引用白皮书
  设计目标。dispatch 本身当前无性能收益（默认关闭），不得据此宣称加速；
* `PARALLEL_AUTO_WORKERS`：`config.olap_parallel_workers = 0`（自动）时
  向量化聚合/分组实际使用的线程数。4A.3 实测 1M 行纯 SUM 加速不足 1.2x，
  自动档保持 1（默认关闭并行）；100k 行 GROUP BY w1→w2 实测 2.75x，
  显式设 2/4 才会强制并行（结果逐值一致）。

本模块只放常量与检查点函数，不做任何计算、不 import 引擎（避免
`import lightdb.olap` 时产生循环依赖）。
"""
from typing import NoReturn

#: OLAP / 列存层是否接入了 SQL 执行路径（`Executor` 的 SELECT 分派钩子）。
#: 4A.2 起为 True（真实接线；默认开关关闭，见 `DEFAULT_ENABLED`）。
WIRED_TO_SQL_PATH = True

#: 接线是否**默认开启**。False = 默认行存，必须显式打开
#: `config.olap_dispatch` / env `LIGHTDB_OLAP_DISPATCH=1` 才会派发。
DEFAULT_ENABLED = False

#: 该层是否参与对外性能声明。4A.5 起为 True：已存在可辩护的基准门禁
#: （并行 GROUP BY 加速比 ≥1.2x、dispatch 对拍不一致数 ==0），实测数字见
#: `benchmarks/README.md`。注意：dispatch 默认关闭且当前无性能收益，
#: 不得据此宣称"SQL 查询已获得向量化加速"。
COUNTS_FOR_PERFORMANCE_CLAIMS = True

#: `olap_parallel_workers = 0`（自动）时向量化并行档位。
#: 4A.3 实测 1M 行 SUM 的 1→4 线程加速比不足 1.2x（numpy 求和本身在
#: 单线程已很快，线程池调度/分片开销吃掉收益），因此自动档取 1（串行）；
#: 显式设 2/4 可强制并行（结果与串行逐值一致，见测试）。
PARALLEL_AUTO_WORKERS = 1

#: dispatch 实现模块（日志/错误报文/测试共用同一路径口径）。
DISPATCH_MODULE = 'lightdb/olap/sql_dispatch.py'

#: 该层的状态标签（文档与错误报文共用同一份口径）。
STATUS_LABEL = '已接线、默认关闭、有基准门禁（实测对照见 benchmarks/README.md）'

#: 实测对照表的位置（所有性能口径都指向它）。
BENCH_REFERENCE = 'benchmarks/README.md'


def ensure_not_requested_via_sql(what: str, hint: str = '') -> None:
    """接线状态检查点（4A.2 起语义变化）。

    旧语义：未接线时一旦有人从 SQL 路径调到这里就报错。
    新语义：**已接线**（`WIRED_TO_SQL_PATH=True`），本函数变成状态自检：
    调用方可以从 `lightdb.parallel.executor` 等非 SQL 路径调用它，用来
    声明「本路径不依赖 SQL 分派」。没有隐形改道时是空操作。

    真正的「禁止静默改道」由 `sql_dispatch.try_vectorized_select` 的白名单
    保证：未列入白名单的语法一律返回 None 走行存，且派发/回退都有计数与
    日志（`sql_dispatch.dispatch_stats()`）。
    """
    return None


def raise_experimental_unwired(what: str, hint: str = '') -> NoReturn:
    """实验性入口的统一报错（HybridTable.olap_scan 等无既有调用方的入口）。

    铁律：这些入口被触达时**显式报错**，绝不返回空结果、绝不静默改走别的
    路径。SQL 查询请走 `olap_dispatch`（默认关闭，见 `DEFAULT_ENABLED`）。
    """
    raise NotImplementedError(
        f'{what} 属于实验性接口（{DISPATCH_MODULE} 之外的历史入口）；'
        f'SQL 路径请使用 `olap_dispatch`（默认关闭，需显式开启），'
        f'本接口**不计入性能声明**（{BENCH_REFERENCE}）。'
        + (f' {hint}' if hint else ''))


__all__ = [
    'WIRED_TO_SQL_PATH',
    'DEFAULT_ENABLED',
    'COUNTS_FOR_PERFORMANCE_CLAIMS',
    'PARALLEL_AUTO_WORKERS',
    'DISPATCH_MODULE',
    'STATUS_LABEL',
    'BENCH_REFERENCE',
    'ensure_not_requested_via_sql',
    'raise_experimental_unwired',
]
