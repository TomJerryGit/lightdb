"""
OLAP 优化器
- 聚合下推
- 列裁剪
- 谓词下推
"""
from typing import Any, Dict, List

from .vectorized_engine import (
    VectorizedScan, VectorizedFilter, VectorizedAggregate,
    VectorizedGroupBy, VectorizedProject, VectorizedSort)


class OLAPOptimizer:
    """
    分析型查询优化器
    """

    def __init__(self, column_store):
        self.store = column_store

    def optimize(self, query: Dict):
        """
        生成向量化执行计划
        query: {
            'columns': ['*'],  # 投影
            'where': {...},    # 谓词
            'group_by': [...], # 分组
            'aggregations': [...],  # 聚合
            'order_by': (...),
        }
        """
        columns = query.get('columns', ['*'])
        where = query.get('where')
        group_by = query.get('group_by', [])
        aggregations = query.get('aggregations', [])
        order_by = query.get('order_by')

        # 1. 列裁剪：只扫描需要的列
        needed_cols = set()

        if columns == ['*']:
            needed_cols.update(self.store.col_names)
        else:
            for c in columns:
                needed_cols.add(c.split('.')[-1])

        if where and 'column' in where:
            needed_cols.add(where['column'].split('.')[-1])

        if group_by:
            for g in group_by:
                needed_cols.add(g.split('.')[-1])

        for agg in aggregations:
            if agg.get('column') and agg['column'] != '*':
                needed_cols.add(agg['column'].split('.')[-1])

        if order_by:
            needed_cols.add(order_by[0].split('.')[-1])

        # 2. 构建扫描
        scan = VectorizedScan(self.store, list(needed_cols))

        # 3. 谓词下推
        if where:
            scan = self._push_predicate(scan, where)

        # 4. 分组聚合
        if group_by or aggregations:
            scan = VectorizedGroupBy(scan, group_by, aggregations)

        # 5. 排序
        if order_by:
            scan = VectorizedSort(scan, order_by[0], order_by[1])

        # 6. 投影（如果需要）
        # 简化：暂不实现

        return scan

    def _push_predicate(self, scan, where):
        """谓词下推"""
        conds = []
        if 'and' in where:
            conds = where['and']
        elif 'column' in where:
            conds = [where]

        for c in conds:
            if 'column' not in c:
                continue
            col = c['column'].split('.')[-1]
            op = c.get('op', '=')
            val = c.get('value')
            scan = VectorizedFilter(scan, col, op, val)

        return scan

    # ==================== 统计信息驱动的优化 ====================

    def can_skip_scan(self, where: Dict) -> bool:
        """
        基于列统计，判断是否可以跳过扫描
        例：WHERE age > 100 但 age 列 max = 50，则直接返回空
        """
        if not where:
            return False

        if 'column' not in where:
            return False

        col = where['column'].split('.')[-1]
        stats = self.store.get_column_stats(col)
        if not stats:
            return False

        op = where.get('op', '=')
        val = where.get('value')

        try:
            if op == '>' and stats['max'] is not None:
                if float(val) >= float(stats['max']):
                    return True
            if op == '<' and stats['min'] is not None:
                if float(val) <= float(stats['min']):
                    return True
            if op == '=' and stats['min'] is not None:
                if float(val) < float(stats['min']):
                    return True
                if float(val) > float(stats['max']):
                    return True
        except (ValueError, TypeError):
            pass

        return False