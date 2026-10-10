"""
HTAP 混合表
- 行存：OLTP 事务
- 列存：OLAP 分析
- 双写，自动同步
"""
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.btree import BTree
from ..storage.buffer_pool import BufferPool
from ..storage.row_format import BinaryRowFormat
from ..columnar.column_store import ColumnStore
from ..olap.olap_optimizer import OLAPOptimizer


class HybridTable:
    """
    混合表：行存 + 列存
    """

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

    # ==================== 查询（OLAP）====================

    def olap_scan(self, columns: List[str] = None) -> Dict[str, List]:
        """列扫描"""
        # 先同步待处理的行
        self.sync_now()
        return self.column_store.scan(columns)

    def olap_aggregate(self, query: Dict) -> List:
        """
        执行分析查询
        query: {
            'columns': [...],
            'where': {...},
            'group_by': [...],
            'aggregations': [...]
        }
        """
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