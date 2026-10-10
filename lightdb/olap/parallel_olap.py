"""
并行 OLAP 门面
- 自动判断是否并行
- 统一接口
"""
from typing import Any, Dict, List, Optional

from ..parallel.executor import ParallelExecutor
from ..parallel.adaptive import AdaptiveParallelism


class ParallelOLAP:
    """
    并行 OLAP 引擎
    """

    def __init__(self, max_workers: int = None):
        self.executor = ParallelExecutor(max_workers)
        self.adaptive = AdaptiveParallelism(max_workers)

    def scan(self, column_store,
              columns: List[str] = None,
              where: Dict = None) -> Dict[str, List]:
        """扫描（自动并行）"""
        return self.executor.parallel_scan(
            column_store, columns, where)

    def aggregate(self, column_store,
                   agg_column: str,
                   agg_func: str,
                   where: Dict = None) -> Any:
        """聚合"""
        return self.executor.parallel_aggregate(
            column_store, agg_column, agg_func, where)

    def group_by(self, column_store,
                  group_cols: List[str],
                  aggs: List[Dict]) -> List[List]:
        """分组"""
        return self.executor.parallel_group_by(
            column_store, group_cols, aggs)

    def stats(self) -> dict:
        return {
            'executor': self.executor.stats_report(),
            'adaptive': self.adaptive.stats_report(),
        }

    def shutdown(self):
        self.executor.shutdown()