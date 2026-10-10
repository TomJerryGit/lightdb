"""
结果合并
"""
from typing import Any, Dict, List


class ResultMerger:
    """结果合并器"""

    # ==================== 扫描结果合并 ====================

    @staticmethod
    def merge_scan_results(results: List,
                             columns: List[str]) -> Dict[str, List]:
        """合并扫描结果（按行顺序拼接）"""
        if not results:
            return {}

        merged = {c: [] for c in columns} if columns else {}

        # 按 fragment_id 排序保证顺序
        sorted_results = sorted(
            results, key=lambda r: r.fragment_id)

        for r in sorted_results:
            if r.error or r.data is None:
                continue
            for col in (columns or r.data.keys()):
                if col in r.data:
                    merged.setdefault(col, []).extend(r.data[col])

        return merged

    # ==================== 聚合结果合并 ====================

    @staticmethod
    def merge_aggregations(partials: List,
                             agg_func: str) -> Any:
        """
        合并聚合结果
        partials: [FragmentResult, ...]
        """
        if not partials:
            return None

        # 提取 data
        datas = [p.data for p in partials
                  if p.data is not None]
        if not datas:
            return None

        if agg_func == 'COUNT':
            return sum(d.get('count', 0) for d in datas)

        if agg_func == 'SUM':
            return sum(d.get('sum', 0) for d in datas)

        if agg_func == 'MIN':
            vals = [d.get('min') for d in datas
                     if d.get('min') is not None]
            return min(vals) if vals else None

        if agg_func == 'MAX':
            vals = [d.get('max') for d in datas
                     if d.get('max') is not None]
            return max(vals) if vals else None

        if agg_func == 'AVG':
            total_sum = sum(d.get('sum', 0) for d in datas)
            total_count = sum(d.get('count', 0) for d in datas)
            return total_sum / total_count if total_count else None

        return None

    # ==================== 分组结果合并 ====================

    @staticmethod
    def merge_group_bys(partials: List,
                          group_cols: List[str],
                          aggs: List[Dict]) -> List[List]:
        """
        合并分组结果
        partials: [FragmentResult, ...]
        """
        if not partials:
            return []

        # 全局分组
        global_groups = {}

        for p in partials:
            if p.data is None:
                continue

            local_groups = p.data
            for key, vals in local_groups.items():
                if key not in global_groups:
                    global_groups[key] = dict(vals)
                    continue

                # 合并聚合
                for a in aggs:
                    alias = a['alias']
                    func = a['func']
                    old = global_groups[key].get(alias)
                    new = vals.get(alias)

                    if func == 'COUNT':
                        global_groups[key][alias] = (
                            (old or 0) + (new or 0))
                    elif func == 'SUM':
                        global_groups[key][alias] = (
                            (old or 0) + (new or 0))
                    elif func == 'MIN':
                        if new is None:
                            continue
                        global_groups[key][alias] = (
                            new if old is None else min(old, new))
                    elif func == 'MAX':
                        if new is None:
                            continue
                        global_groups[key][alias] = (
                            new if old is None else max(old, new))
                    elif func == 'AVG':
                        # 简化：使用 sum/count 聚合
                        # 需保存 sum 和 count
                        pass

        # 转换为行
        result = []
        for key, vals in global_groups.items():
            row = list(key)
            for a in aggs:
                row.append(vals.get(a['alias']))
            result.append(row)

        return result

    # ==================== 排序（并行归并）====================

    @staticmethod
    def merge_sorted(partials: List[List],
                      column: int = 0,
                      desc: bool = False) -> List:
        """
        归并多个已排序列表
        """
        import heapq

        # 排序键
        key_func = lambda x: (
            x[column] is None, x[column])

        # 反向
        if desc:
            for p in partials:
                p.sort(key=key_func, reverse=True)
        else:
            for p in partials:
                p.sort(key=key_func)

        # 归并
        merged = []
        if desc:
            # 从大开始，需自定义
            # 简化：全部合并后排序
            for p in partials:
                merged.extend(p)
            merged.sort(key=key_func, reverse=True)
        else:
            # 小顶堆
            heap = []
            for i, p in enumerate(partials):
                if p:
                    heapq.heappush(heap, (key_func(p[0]), i, 0))
            while heap:
                _, pi, idx = heapq.heappop(heap)
                merged.append(partials[pi][idx])
                if idx + 1 < len(partials[pi]):
                    heapq.heappush(
                        heap,
                        (key_func(partials[pi][idx + 1]), pi, idx + 1))

        return merged