# lightdb/olap/status.py
"""OLAP / 列存层的**接线状态常量与显式检查点**（阶段 2 第 6 项，方案 A）。

背景：`lightdb/olap/**`（向量化引擎）与 `lightdb/columnar/**`（列存
`ColumnStore`）在 SQL 执行路径（`Executor` / `plan_executor`）上**零引用**，
但白皮书把它们当成 OLAP 3.5M rows/s、大表聚合 85M rows/s、列存 41x 的
性能来源。本阶段的处理是**摘牌 + 显式化**（不删实现，见任务约束）：

* **摘牌**：删掉生产代码里一切「可能悄悄走到 olap/列存」的分派
  （原 `lightdb/parallel/executor.py::_serial_group_by` 里的
  `from ..olap.vectorized_engine import ...`）；
* **显式化**：仍然保留的实验性入口在被触达时**显式报错**，报文说明
  「实验性、未接入 SQL 路径、不计入性能声明」，绝不返回空结果或
  静默改走别的路径；
* **口径统一**：对外文档一律标注「实验性 / 未接入 SQL 路径 /
  不计入性能声明」，可复现实测见 `benchmarks/README.md`。

本模块只放常量与一个检查点函数，不做任何计算、不 import 引擎（避免
`import lightdb.olap` 时产生循环依赖）。
"""
from typing import NoReturn

#: OLAP / 列存层是否接入了 SQL 执行路径（`Executor` / `plan_executor`）。
#: 事实是**没有**：这是本阶段摘牌的依据，也是测试 `tests/test_olap_not_wired.py`
#: 直接断言的对象。
WIRED_TO_SQL_PATH = False

#: 该层是否参与对外性能声明。未接入 => 不计入，任何把 3.5M / 85M rows/s、
#: 41x 之类数字当成实测性能的做法都是错的。
COUNTS_FOR_PERFORMANCE_CLAIMS = False

#: 该层的状态标签（文档与错误报文共用同一份口径）。
STATUS_LABEL = '实验性、未接入 SQL 路径、不计入性能声明'

#: 实测对照表的位置（所有性能口径都指向它）。
BENCH_REFERENCE = 'benchmarks/README.md'


def ensure_not_requested_via_sql(what: str, hint: str = '') -> None:
    """显式检查点：一旦有人把 OLAP/列存挂回 SQL 路径，就在**入口处**报错。

    现在是**空操作**（因为确实没有接线，没有任何 SQL 路径会调用它）；
    它的作用是「防回归的路标」：真有人接线时，第一个动作会在这里炸出
    一段说明，而不是悄悄多出一条未声明的执行路径。

    :param what: 被触达的能力名（如 ``'HybridTable.olap_aggregate'``）
    :param hint: 可选的补充提示（如替代做法）
    """
    if WIRED_TO_SQL_PATH:                       # pragma: no cover - 未接线
        raise NotImplementedError(
            f'{what} 被登记为「已接入 SQL 路径」，但 OLAP/列存层尚未完成接线'
            f'验收（{STATUS_LABEL}）。请先更新 {BENCH_REFERENCE} 的实测对照'
            f'表与 lightdb/olap/status.py 的状态常量。' + (f' {hint}' if hint else ''))


def raise_experimental_unwired(what: str, hint: str = '') -> NoReturn:
    """实验性入口的统一报错（报文含「实验性 / 未接入 / 不计入性能声明」）。

    铁律：实验性入口被触达时**显式报错**，绝不返回空结果、绝不静默
    改走别的路径（本项目一贯原则：不静默假成功）。
    """
    raise NotImplementedError(
        f'{what} 属于实验性接口，**未接入 SQL 路径**（`Executor` / '
        f'`plan_executor` 零引用），**不计入性能声明**；因此本接口不做'
        f'SQL 语义兼容承诺，请勿在生产查询路径使用。'
        f'可复现实测见 {BENCH_REFERENCE}。' + (f' {hint}' if hint else ''))


__all__ = [
    'WIRED_TO_SQL_PATH',
    'COUNTS_FOR_PERFORMANCE_CLAIMS',
    'STATUS_LABEL',
    'BENCH_REFERENCE',
    'ensure_not_requested_via_sql',
    'raise_experimental_unwired',
]
