"""
结果合并（正确性止血版，2026-10）

修复：
- `merge_aggregations` / `merge_group_bys` 对**不支持的聚合函数**以前返回
  None / 静默填 None；现在显式 ValueError。
- 分组 AVG 以前是 `pass`（合并阶段直接丢掉），AVG 列恒为 None。现在用
  fragment 里保存的 `{'sum','count'}` 合并出真实平均值（全 NULL 分组
  仍为 None）。
- `merge_scan_results` 对失败分片（`error`）以前静默跳过 —— 结果行数
  变少但调用方看不出来。现在只要有一个失败分片就显式 RuntimeError；
  分片失败由 Executor 在调用本函数之前负责。
"""
from typing import Any, Dict, List

SUPPORTED_AGGS = ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX')


class ResultMerger:
    """结果合并器"""

    # ==================== 扫描结果合并 ====================

    @staticmethod
    def merge_scan_results(results: List,
                             columns: List[str]) -> Dict[str, List]:
        """合并扫描结果（按行顺序拼接）"""
        if not results:
            return {}

        # 失败分片必须显式失败：静默丢掉一整个范围会返回短结果集
        bad = [r for r in results if getattr(r, 'error', '')]
        if bad:
            raise RuntimeError(
                "并行扫描存在失败分片，结果不完整: "
                + "; ".join(
                    f"fragment {getattr(r, 'fragment_id', '?')}: {r.error}"
                    for r in bad))

        merged = {c: [] for c in (columns or [])}

        # 按 fragment_id 排序保证顺序
        sorted_results = sorted(results, key=lambda r: r.fragment_id)

        for r in sorted_results:
            if r.data is None:
                continue
            names = (columns or list(r.data.keys()))
            for col in names:
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
        func = str(agg_func).upper()
        if func not in SUPPORTED_AGGS:
            raise ValueError(
                f"ResultMerger.merge_aggregations: 不支持的聚合函数 "
                f"{agg_func!r}，仅支持 {list(SUPPORTED_AGGS)}")

        if not partials:
            # 空输入：COUNT=0，其余 None（SQL 语义）
            return 0 if func == 'COUNT' else None

        datas = [p.data for p in partials if p.data is not None]
        if not datas:
            return 0 if func == 'COUNT' else None

        if func == 'COUNT':
            return sum(d.get('count', 0) for d in datas)

        if func == 'SUM':
            return sum(d.get('sum', 0) for d in datas)

        if func == 'MIN':
            vals = [d.get('min') for d in datas
                     if d.get('min') is not None]
            return min(vals) if vals else None

        if func == 'MAX':
            vals = [d.get('max') for d in datas
                     if d.get('max') is not None]
            return max(vals) if vals else None

        if func == 'AVG':
            total_sum = sum(d.get('sum', 0) for d in datas)
            total_count = sum(d.get('count', 0) for d in datas)
            return (total_sum / total_count) if total_count else None

        raise ValueError(f"不支持的聚合函数 {func!r}")   # pragma: no cover

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

        for a in aggs:
            if str(a.get('func', '')).upper() not in SUPPORTED_AGGS:
                raise ValueError(
                    f"ResultMerger.merge_group_bys: 不支持的聚合函数 "
                    f"{a.get('func')!r}，仅支持 {list(SUPPORTED_AGGS)}")

        # 全局分组
        global_groups: Dict[tuple, Dict] = {}

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
                    func = str(a['func']).upper()
                    old = global_groups[key].get(alias)
                    new = vals.get(alias)

                    if func == 'COUNT':
                        global_groups[key][alias] = (
                            (old or 0) + (new or 0))
                    elif func == 'SUM':
                        # None 表示该分片该组没有非 NULL 值（贡献 0）
                        old_v = 0 if old is None else old
                        new_v = 0 if new is None else new
                        global_groups[key][alias] = old_v + new_v
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
                        old_d = old if isinstance(old, dict) else {
                            'sum': 0, 'count': 0}
                        new_d = new if isinstance(new, dict) else {
                            'sum': 0, 'count': 0}
                        global_groups[key][alias] = {
                            'sum': old_d.get('sum', 0) + new_d.get('sum', 0),
                            'count': old_d.get('count', 0)
                            + new_d.get('count', 0),
                        }

        # 转换为行
        result = []
        for key, vals in global_groups.items():
            row = list(key)
            for a in aggs:
                alias = a['alias']
                func = str(a['func']).upper()
                v = vals.get(alias)
                if func == 'AVG':
                    if isinstance(v, dict):
                        cnt = v.get('count', 0)
                        v = (v.get('sum', 0) / cnt) if cnt else None
                    else:
                        v = None
                row.append(v)
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
