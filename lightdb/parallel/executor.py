"""
并行执行器
- 分片级并行
- 支持聚合/扫描/过滤/分组
- 结果合并
"""
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, as_completed)
from typing import Any, Callable, Dict, List, Optional

from .fragment import (
    Fragment, FragmentResult, FragmentSplitter)
from .adaptive import AdaptiveParallelism
from .work_stealing import WorkStealingScheduler
from .merge import ResultMerger


class ParallelExecutor:
    """
    并行执行器
    """

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
            except Exception:
                pass

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
            except Exception:
                pass

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并
        merged = ResultMerger.merge_group_bys(
            partials, group_cols, aggs)

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
            # 读取需要的列
            needed = list(set(group_cols + [
                a['column'] for a in aggs if a.get('column')]))
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
                    if col is None or col not in data:
                        continue
                    v = data[col][i]
                    if v is None:
                        continue

                    alias = a['alias']
                    func = a['func']

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
        """串行回退"""
        from ..olap.vectorized_engine import (
            VectorizedScan, VectorizedGroupBy)
        scan = VectorizedScan(column_store, list(set(
            group_cols + [a['column'] for a in aggs
                            if a.get('column')])))
        gb = VectorizedGroupBy(scan, group_cols, aggs)
        return gb.execute()

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