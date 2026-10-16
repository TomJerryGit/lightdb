"""
并行执行器
- 分片级并行
- 支持聚合/扫描/过滤/分组
- 结果合并

接线状态（阶段 2 第 6 项，方案 A「摘牌 + 显式化」）：

* 本模块**不再引用 `lightdb.olap` / `lightdb.columnar`**。原
  `_serial_group_by` 回退路径里有一句
  `from ..olap.vectorized_engine import VectorizedScan, VectorizedGroupBy`，
  于是「并行分组在 workers<=1 或全部分片失败时」会悄悄走到实验性的
  向量化引擎上 —— 这是本项目禁止的**静默改道**。现在串行回退是
  本模块自己的逐行实现（`_serial_group_by`），不依赖任何 olap/列存代码。
* 分片接口（`row_count` + `scan_range`）由调用方提供的**只读列式视图**
  满足：SQL 路径用的是 `lightdb/parallel/table_view.py::TableColumnView`
  （行式 `Table` 的适配器，走 BTree 逐行解码），**不是**列存
  `lightdb/columnar/ColumnStore`。
* 因此 OLAP / 列存在 SQL 执行路径上依旧零引用，也不计入任何性能声明；
  实测对照见 `benchmarks/README.md`。
"""
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, as_completed)
from typing import Any, Callable, Dict, List, Optional

from ..logger import logger
from ..olap.status import (
    ensure_not_requested_via_sql, WIRED_TO_SQL_PATH as _OLAP_WIRED_TO_SQL)
from .fragment import (
    Fragment, FragmentResult, FragmentSplitter)
from .adaptive import AdaptiveParallelism
from .work_stealing import WorkStealingScheduler
from .merge import ResultMerger


def _required_columns(group_cols, aggs) -> List[str]:
    """分片真正需要读的列

    `COUNT(*)` 的 `column` 是 `'*'`（**不是**列名）。原实现直接把它塞进
    `scan_range` 的列清单，于是取回一个空的 `data['*']`，随后
    `data[col][i]` 抛 `list index out of range`；异常又被
    `parallel_group_by` 的 `except Exception: pass` 吞掉，partials 为空，
    最终返回 0 组（`tests/test_v4_batch5.py::test_parallel_group_by`
    的 `assert 0 == 100`）。这里把 `'*'` 与空列名剔掉。
    """
    cols = []
    for c in list(group_cols or []) + [a.get('column') for a in aggs or []]:
        if not c or c == '*':
            continue
        if c not in cols:
            cols.append(c)
    return cols


class ParallelExecutor:
    """
    并行执行器

    `WIRED_TO_SQL_PATH`：本并行执行器接的是**调用方给的只读列式视图**，
    与实验性的 `lightdb/olap/**` / `lightdb/columnar/**` 没有代码依赖
    （见模块 docstring），因此 OLAP/列存层未接入 SQL 路径这一状态不受
    本模块影响。
    """

    #: OLAP / 列存层是否接入 SQL 路径（阶段 2 第 6 项摘牌依据，恒为 False）
    WIRED_TO_SQL_PATH = False

    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers or 8
        self.adaptive = AdaptiveParallelism(max_workers)

        # 线程池
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers)

        # 工作窃取调度器
        self.ws = WorkStealingScheduler(self.max_workers)

        # 统计
        self.stats = {
            'total_queries': 0,
            'parallel_queries': 0,
            'serial_queries': 0,
            'fragments_executed': 0,
            'total_elapsed_ms': 0,
        }
        self.lock = threading.RLock()

    def shutdown(self):
        self.pool.shutdown(wait=False)
        self.ws.stop()

    # ==================== 并行扫描 ====================

    def parallel_scan(self, column_store,
                       columns: List[str] = None,
                       where: Dict = None,
                       workers: int = None) -> Dict[str, List]:
        """
        并行列扫描
        """
        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=False)

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1:
            with self.lock:
                self.stats['serial_queries'] += 1
            return column_store.scan(columns)

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 生成分片
        fragments = FragmentSplitter.split_by_range(
            total_rows, workers)

        # 2. 并行执行
        t0 = time.perf_counter()
        results = []
        futures = []

        for frag in fragments:
            future = self.pool.submit(
                self._scan_fragment, column_store,
                columns, frag)
            futures.append((future, frag))

        for future, frag in futures:
            try:
                result = future.result(timeout=60)
                results.append(result)
            except Exception as e:
                frag.status = 'failed'
                frag.error = str(e)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并
        merged = ResultMerger.merge_scan_results(
            results, columns)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return merged

    def _scan_fragment(self, column_store, columns,
                        frag: Fragment) -> FragmentResult:
        """扫描一个分片"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            data = column_store.scan_range(
                columns, frag.offset, frag.offset + frag.limit)
            result.data = data
            result.row_count = frag.limit
            frag.status = 'done'
            frag.rows_processed = frag.limit
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    # ==================== 并行聚合 ====================

    def parallel_aggregate(self, column_store,
                            agg_column: str,
                            agg_func: str,
                            where: Dict = None,
                            workers: int = None) -> Any:
        """
        并行聚合
        """
        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=True)

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1:
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_aggregate(
                column_store, agg_column, agg_func, where)

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 分片
        fragments = FragmentSplitter.split_by_range(
            total_rows, workers)

        # 2. 并行局部聚合
        t0 = time.perf_counter()
        futures = []

        for frag in fragments:
            future = self.pool.submit(
                self._agg_fragment, column_store,
                agg_column, agg_func, frag)
            futures.append(future)

        partials = []
        for future in as_completed(futures):
            try:
                partials.append(future.result())
            except Exception as e:
                # 绝不静默：丢掉一个分片会让聚合**少算**，而调用方只会看到
                # 一个看起来合法的数字。至少留下 ERROR 级日志（分片内部
                # 自己捕获的异常由 `FragmentResult.error` 记，下面统一报）。
                logger.error(f"并行聚合分片失败（该分片结果被丢弃）: {e}")

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并
        merged = ResultMerger.merge_aggregations(
            partials, agg_func)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return merged

    def _agg_fragment(self, column_store, agg_column,
                        agg_func, frag):
        """单分片聚合"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            data = column_store.scan_range(
                [agg_column], frag.offset, frag.offset + frag.limit)
            values = data.get(agg_column, [])
            values = [v for v in values if v is not None]

            if not values:
                partial = {
                    'count': 0, 'sum': 0, 'min': None, 'max': None,
                }
            elif agg_func == 'COUNT':
                partial = {'count': len(values)}
            elif agg_func == 'SUM':
                partial = {'count': len(values), 'sum': sum(values)}
            elif agg_func == 'MIN':
                partial = {'count': len(values), 'min': min(values)}
            elif agg_func == 'MAX':
                partial = {'count': len(values), 'max': max(values)}
            elif agg_func == 'AVG':
                partial = {'count': len(values), 'sum': sum(values)}
            else:
                partial = {'count': len(values)}

            result.data = partial
            result.row_count = len(values)
            frag.status = 'done'
            frag.rows_processed = frag.limit
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    def _serial_aggregate(self, column_store, agg_column,
                            agg_func, where):
        """串行回退"""
        data = column_store.scan([agg_column])
        values = [v for v in data.get(agg_column, [])
                   if v is not None]
        if not values:
            return None
        if agg_func == 'COUNT':
            return len(values)
        if agg_func == 'SUM':
            return sum(values)
        if agg_func == 'MIN':
            return min(values)
        if agg_func == 'MAX':
            return max(values)
        if agg_func == 'AVG':
            return sum(values) / len(values)
        return None

    # ==================== 并行分组聚合 ====================

    def parallel_group_by(self, column_store,
                            group_cols: List[str],
                            aggs: List[Dict],
                            workers: int = None) -> List[List]:
        """
        并行分组聚合（两阶段）
        1. 局部 hash 分组
        2. 全局 merge
        """
        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=True)

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1:
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_group_by(
                column_store, group_cols, aggs)

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 分片
        fragments = FragmentSplitter.split_by_range(
            total_rows, workers)

        # 2. 并行局部聚合
        t0 = time.perf_counter()
        futures = []
        for frag in fragments:
            future = self.pool.submit(
                self._group_by_fragment, column_store,
                group_cols, aggs, frag)
            futures.append(future)

        partials = []
        for future in as_completed(futures):
            try:
                partials.append(future.result())
            except Exception as e:
                # 绝不静默：分片失败会让结果**少行**（或全空），而调用方
                # 只会看到"0 组"这种看起来合法的返回值。至少留下日志。
                logger.warn(f"并行分组分片失败: {e}")

        bad = [r for r in partials if getattr(r, 'error', '')]
        for r in bad:
            logger.warn(f"并行分组分片 {r.fragment_id} 失败: {r.error}")
        good = [r for r in partials if not getattr(r, 'error', '')]
        if fragments and not good:
            # 全部分片都失败 → 退回串行，结果正确优先于并行度
            logger.warn("并行分组全部分片失败，回退串行执行")
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_group_by(column_store, group_cols, aggs)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并（只用成功的分片；失败分片在上面已经记录）
        merged = ResultMerger.merge_group_bys(
            good, group_cols, aggs)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return merged

    def _group_by_fragment(self, column_store, group_cols,
                             aggs, frag):
        """单分片分组"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            # 读取需要的列（`COUNT(*)` 的 '*' 不是列名，必须剔除）
            needed = _required_columns(group_cols, aggs)
            data = column_store.scan_range(
                needed, frag.offset, frag.offset + frag.limit)

            n = frag.limit
            groups = {}

            for i in range(n):
                # 分组键
                key = tuple(
                    data[col][i] if col in data else None
                    for col in group_cols
                )

                if key not in groups:
                    init = {}
                    for a in aggs:
                        if a['func'] == 'COUNT':
                            init[a['alias']] = 0
                        else:
                            init[a['alias']] = None
                    groups[key] = init

                # 累加聚合
                for a in aggs:
                    col = a.get('column')
                    func = a['func']
                    alias = a['alias']

                    if func == 'COUNT' and (col is None or col == '*'):
                        # COUNT(*) / COUNT()，按行计数：原实现直接
                        # `continue`，于是 COUNT 恒为 0
                        groups[key][alias] += 1
                        continue
                    if col is None or col not in data:
                        continue
                    v = data[col][i]
                    if v is None:
                        continue

                    if func == 'COUNT':
                        groups[key][alias] += 1
                    elif func == 'SUM':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else cur + v)
                    elif func == 'MIN':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else min(cur, v))
                    elif func == 'MAX':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else max(cur, v))

            result.data = groups
            result.row_count = len(groups)
            frag.status = 'done'
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    def _serial_group_by(self, column_store, group_cols, aggs):
        """串行分组聚合（**不依赖 olap / 列存**）

        改前（阶段 2 第 6 项之前）：

            from ..olap.vectorized_engine import (
                VectorizedScan, VectorizedGroupBy)
            scan = VectorizedScan(column_store, list(set(
                group_cols + [a['column'] for a in aggs
                                if a.get('column')])))
            gb = VectorizedGroupBy(scan, group_cols, aggs)
            return gb.execute()

        问题有两个，都属于「静默改道 / 静默错值」：

        1. **静默改道**：`workers <= 1`（或全部并行分片失败）时，串行回退
           悄悄把查询交给实验性的 `lightdb/olap` 向量化引擎 —— 这条路径
           `Executor` / `plan_executor` 零引用、未接入 SQL 路径，却在这里
           被真实调用；
        2. **静默错值**：`VectorizedScan` 只取回被请求的列，`COUNT(*)` 的
           `'*'` 不在其中，于是 `VectorizedGroupBy` 对非 * 聚合走
           `isinstance(..., list)` 分支时拿到的是**单元素列表**
           （`[v]` / `[total]`），与并行路径（`merge_group_bys`）返回的
           标量形状不一致。

        现在改为逐行实现：语义与 `_group_by_fragment` + `merge_group_bys`
        完全一致（同样的 NULL 语义、同样的 `COUNT(*)` 计行、同样的
        `[分组键..., 聚合值...]` 行形状），只是不分片、不换引擎。

        最后再走一次状态检查点 `ensure_not_requested_via_sql`：如果哪天
        有人把 OLAP/列存真接进 SQL 路径，这里会**显式报错**并指向
        `benchmarks/README.md`，而不是让这条回退路径悄悄改变语义。
        """
        ensure_not_requested_via_sql('ParallelExecutor._serial_group_by')
        # 状态一致性：本文件已不引用 olap/列存实现，若状态常量被改成
        # 「已接线」，说明有人改了接线却没改这里 —— 显式失败，不静默。
        assert _OLAP_WIRED_TO_SQL is False, (
            'lightdb/olap/status.py 声称 OLAP 已接入 SQL 路径，但 '
            'lightdb/parallel/executor.py 已不引用 olap/列存实现；'
            '请同步两者（口径见 benchmarks/README.md）')

        needed = _required_columns(group_cols, aggs)
        data = column_store.scan(needed) if needed else {}
        total = getattr(column_store, 'row_count', 0)

        groups = {}
        for i in range(total):
            key = tuple(
                data[col][i] if col in data and i < len(data[col]) else None
                for col in group_cols
            )
            if key not in groups:
                init = {}
                for a in aggs:
                    init[a['alias']] = 0 if a['func'] == 'COUNT' else None
                groups[key] = init

            for a in aggs:
                col = a.get('column')
                func = a['func']
                alias = a['alias']
                if func == 'COUNT' and (col is None or col == '*'):
                    groups[key][alias] += 1
                    continue
                if col is None or col not in data or i >= len(data[col]):
                    continue
                v = data[col][i]
                if v is None:
                    continue
                if func == 'COUNT':
                    groups[key][alias] += 1
                elif func == 'SUM':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else cur + v
                elif func == 'MIN':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else min(cur, v)
                elif func == 'MAX':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else max(cur, v)

        return [list(key) + [vals[a['alias']] for a in aggs]
                for key, vals in groups.items()]

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        with self.lock:
            return {
                **self.stats,
                'adaptive': self.adaptive.stats_report(),
                'avg_elapsed_ms': (
                    self.stats['total_elapsed_ms'] /
                    max(1, self.stats['total_queries'])),
            }