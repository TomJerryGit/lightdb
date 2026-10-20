# lightdb/olap/__init__.py
"""OLAP（4A.2 起显式接入 SQL 路径，默认关闭；见 `olap.status`）

注意：本包的 `__init__` 会 import 向量化引擎与优化器；SQL 执行器**默认**
（`config.olap_dispatch=False`）不会 import 本包 —— 分派模块
`lightdb/olap/sql_dispatch.py` 只在开关打开时被延迟 import。
"""
from .vectorized_engine import (
    VectorBatch, VectorizedScan, VectorizedFilter,
    VectorizedAggregate, VectorizedAggregateSet, VectorizedGroupBy,
    VectorizedSort, VectorizedProject, VECTOR_SIZE,
    PARALLEL_CHUNK_ROWS, AUTO_PARALLEL_WORKERS, resolve_workers,
)
from .olap_optimizer import OLAPOptimizer
from .parallel_olap import ParallelOLAP

__all__ = [
    'VectorBatch', 'VectorizedScan', 'VectorizedFilter',
    'VectorizedAggregate', 'VectorizedAggregateSet', 'VectorizedGroupBy',
    'VectorizedSort', 'VectorizedProject', 'VECTOR_SIZE',
    'PARALLEL_CHUNK_ROWS', 'AUTO_PARALLEL_WORKERS', 'resolve_workers',
    'OLAPOptimizer',
    'ParallelOLAP',
]
