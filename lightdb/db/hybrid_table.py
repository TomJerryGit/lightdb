"""
HTAP 混合表
- 行存：OLTP 事务
- 列存：OLAP 分析
- 双写，自动同步

接线状态（阶段 2 第 6 项，方案 A「摘牌 + 显式化」）——**必读**：

* 本模块是**实验性**的「行存 + 列存」混合表原型，`lightdb/olap/**`（向量化）
  与 `lightdb/columnar/**`（列存 `ColumnStore`）只在这里被直接构造 ——
  也就是说整个 OLAP / 列存层的**唯一 import 落点**就是本模块，而本模块
  在 SQL 执行路径（`Executor` / `plan_executor`）上**零引用**：SQL 的
  `CREATE TABLE` 只创建行式 `lightdb/db/table.py::Table`
  （见 `docs/DEAD_CODE.md` 把本模块判为不可达）。
* 因此 `olap_scan()` / `olap_aggregate()` **未接入 SQL 路径**、
  **不计入任何性能声明**（白皮书的 3.5M / 85M rows/s、列存 41x 与本模块
  无关；可复现实测见 `benchmarks/README.md`）。
* 本模块**保留**（删包会打破 `tests/test_vectorized_correctness.py` 一类
  既有测试），但两个对外分析入口在被触达时会走
  `lightdb/olap/status.py` 的显式报错检查点，绝不静默返回结果。
"""
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.btree import BTree
from ..storage.buffer_pool import BufferPool
from ..storage.row_format import BinaryRowFormat
from ..logger import logger
from ..columnar.column_store import ColumnStore
from ..olap.olap_optimizer import OLAPOptimizer
from ..olap.status import (
    raise_experimental_unwired, WIRED_TO_SQL_PATH, BENCH_REFERENCE)


def _entrypoint_status(what: str, hint: str = '') -> None:
    """对外分析入口的统一状态检查（4A.2 后：**始终显式报错**）。

    `WIRED_TO_SQL_PATH=True` 表示「SQL 路径已有显式 dispatch」，但
    `HybridTable` 的这两个历史入口**不是** SQL dispatch 路径：它们没有
    行存语义保证、也没有被 `Executor` 调用。因此这里保持 4A.1 的铁律：
    触达即显式报错，报文指向新的 SQL 入口 `olap_dispatch`（默认开启，
    可用 LIGHTDB_OLAP_DISPATCH=0 关闭）。
    """
    raise_experimental_unwired(
        f'HybridTable.{what}',
        hint=hint or ('SQL 路径请使用 `olap_dispatch`（默认开启，'
                      'LIGHTDB_OLAP_DISPATCH=0 可关闭）'))


def _entrypoint_notice(what: str) -> None:
    """已**有既有测试调用**的实验性入口：不抛异常，但每次都显式留痕。

    `HybridTable.olap_aggregate` 是唯一这种情况 ——
    `tests/test_v4_batch4.py::test_hybrid_table` 直接调它（既有断言不得
    改动）。因此这里按「显式化」的另一半处理：**每次调用都打一条带状态
    标签的告警日志**，且报文指向新的 SQL 入口 `olap_dispatch`（默认开启，
    可用 LIGHTDB_OLAP_DISPATCH=0 关闭）。它依旧不计入性能声明。
    """
    logger.warn(
        f'HybridTable.{what} 被调用：实验性入口（非 SQL dispatch 路径）；'
        f'SQL 路径请用 `olap_dispatch`（默认开启，LIGHTDB_OLAP_DISPATCH=0 可关闭）；'
        f'不计入性能声明（实测对照见 {BENCH_REFERENCE}）。')


class HybridTable:
    """
    混合表：行存 + 列存

    **实验性历史入口**：4A.2 起 SQL 路径的 OLAP 入口是
    `lightdb/olap/sql_dispatch.py`（`olap_dispatch`，默认开启，
    LIGHTDB_OLAP_DISPATCH=0 可关闭）；
    `olap_scan` 触达即**显式报错**，`olap_aggregate` 有既有测试调用
    故改为**每次调用显式留痕**（告警日志），两者都不静默，也都不计入
    性能声明（口径见 `lightdb/olap/status.py` 与 `benchmarks/README.md`）。
    """

    #: SQL 路径是否已有 OLAP 显式 dispatch（True；默认开启，见 status）
    WIRED_TO_SQL_PATH = WIRED_TO_SQL_PATH

    def __init__(self, name: str, cols: List[dict],
                 data_dir: str):
        self.name = name
        self.cols = cols
        self.col_names = [c['name'] for c in cols]
        self.data_dir = Path(data_dir)

        # 行存（OLTP）
        from ..storage.row_format_v4 import BinaryRowFormatV4
        self.row_fmt = BinaryRowFormatV4(cols)
        self.bp = BufferPool(
            str(self.data_dir / f'{name}.dat'), capacity=500)
        self.bt = BTree(self.bp)

        # 列存（OLAP）
        self.column_store = ColumnStore(
            name, cols, str(self.data_dir / 'columnar'))

        # 优化器
        self.olap_optimizer = OLAPOptimizer(self.column_store)

        # 待同步到列存的行（延迟同步）
        self._pending_rows: List[List] = []
        self._pending_lock = threading.RLock()

        # 自动同步策略
        self.sync_threshold = 1000   # 累积 1000 行同步一次
        self.sync_interval = 60      # 或每 60 秒同步

        self._last_sync = time.time()
        self.row_count = 0

    # ==================== 写入（OLTP）====================

    def insert(self, values: List):
        """插入一行（双写）"""
        # 1. 行存
        pk = values[0] if values else None
        rb = self.row_fmt.encode(values)
        # 简化：直接放到列存待同步
        with self._pending_lock:
            self._pending_rows.append(values)

        self.row_count += 1

        # 2. 触发同步检查
        self._maybe_sync()

    def insert_batch(self, rows: List[List]):
        """批量插入"""
        with self._pending_lock:
            self._pending_rows.extend(rows)
            self.row_count += len(rows)
        self._maybe_sync()

    def _maybe_sync(self):
        """检查是否需要同步到列存"""
        with self._pending_lock:
            if len(self._pending_rows) >= self.sync_threshold:
                self._sync_to_columnar()
                return

        # 时间触发
        if time.time() - self._last_sync > self.sync_interval:
            self.sync_now()

    def sync_now(self):
        """立即同步"""
        with self._pending_lock:
            self._sync_to_columnar()

    def _sync_to_columnar(self):
        """内部同步"""
        if not self._pending_rows:
            return
        rows = self._pending_rows
        self._pending_rows = []
        self.column_store.append_rows(rows)
        self._last_sync = time.time()

    # ==================== 查询（OLAP，实验性）====================

    def olap_scan(self, columns: List[str] = None) -> Dict[str, List]:
        """列扫描（**实验性入口，触达即显式报错**；SQL 请用 `olap_dispatch`）"""
        _entrypoint_status(
            'olap_scan',
            hint='SQL 路径请使用 `olap_dispatch`（默认开启，'
                 'LIGHTDB_OLAP_DISPATCH=0 可关闭）；'
                 '该历史入口不参与性能声明，也不保证 SQL 语义兼容。')
        # 先同步待处理的行
        self.sync_now()
        return self.column_store.scan(columns)

    def olap_aggregate(self, query: Dict) -> List:
        """
        执行分析查询（**实验性入口；每次调用显式留痕**）

        4A.2 后 SQL 路径的分析查询入口是 `lightdb/olap/sql_dispatch.py`
        的 `olap_dispatch`（默认开启，LIGHTDB_OLAP_DISPATCH=0 可关闭）；
        本方法是没有行存语义保证的历史
        实验入口，只保留给既有测试/演示使用（`tests/test_v4_batch4.py::
        test_hybrid_table` 直接调用，既有断言不得改动），**不计入任何
        性能声明**。

        query: {
            'columns': [...],
            'where': {...},
            'group_by': [...],
            'aggregations': [...]
        }
        """
        _entrypoint_notice('olap_aggregate')
        self.sync_now()

        # 优化器生成计划
        plan = self.olap_optimizer.optimize(query)

        # 执行
        if hasattr(plan, 'execute'):
            result = plan.execute()
            return result

        # scan 类型
        columns = query.get('columns', ['*'])
        if columns == ['*']:
            columns = self.col_names
        return self.column_store.scan(columns)

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        col_stats = self.column_store.stats_report()
        with self._pending_lock:
            pending = len(self._pending_rows)
        return {
            'name': self.name,
            'row_count': self.row_count,
            'pending_sync': pending,
            'column_store': col_stats,
        }

    def close(self):
        self.sync_now()
        self.column_store.flush()
        self.bp.close()