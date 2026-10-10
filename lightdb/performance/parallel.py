"""
并行查询执行
- 表扫描分片（按 B+Tree 页范围）
- 并行 COUNT / SUM / MIN / MAX
- 线程池
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


class ParallelExecutor:
    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self.pool = ThreadPoolExecutor(max_workers=max_workers)
        self.stats = {
            'parallel_queries': 0,
            'total_workers_used': 0,
        }

    def shutdown(self):
        self.pool.shutdown(wait=False)

    def parallel_scan(self, tbl, read_view, filter_fn=None):
        """
        并行扫描表 → 返回所有行
        - 每个 worker 处理一个 pk 范围
        """
        # 1. 先收集所有 pk（快速）
        all_pks = [k for k, _, _ in tbl.bt.scan()]
        if not all_pks:
            return []

        n = len(all_pks)
        # 2. 决定分片数
        if n < 1000:
            # 小表串行
            return self._scan_serial(tbl, all_pks, read_view, filter_fn)

        workers = min(self.max_workers, (n + 999) // 1000)
        chunk = (n + workers - 1) // workers
        self.stats['parallel_queries'] += 1
        self.stats['total_workers_used'] += workers

        # 3. 分片执行
        futures = []
        for i in range(workers):
            start = i * chunk
            end = min(start + chunk, n)
            if start >= end:
                break
            pks = all_pks[start:end]
            futures.append(
                self.pool.submit(self._scan_chunk, tbl, pks, read_view, filter_fn)
            )

        # 4. 收集
        rows = []
        for f in as_completed(futures):
            try:
                rows.extend(f.result())
            except Exception:
                pass
        return rows

    def _scan_serial(self, tbl, pks, read_view, filter_fn):
        rows = []
        for pk in pks:
            loc = tbl.bt.search(pk)
            if loc is None:
                continue
            page = tbl.bp.get_page(loc[0])
            rb = page.get_row(loc[1])
            r = tbl._decode_row_full(rb)
            if r is None:
                continue
            row, xmin, xmax = r
            if read_view is not None and not read_view.is_visible(xmin, xmax):
                continue
            if filter_fn and not filter_fn(row):
                continue
            rows.append(row)
        return rows

    def _scan_chunk(self, tbl, pks, read_view, filter_fn):
        return self._scan_serial(tbl, pks, read_view, filter_fn)

    def parallel_aggregate(self, tbl, read_view, agg_fn, filter_fn=None):
        """
        并行聚合：每个 worker 计算局部聚合，最后 merge
        agg_fn: 'COUNT' / 'SUM' / 'AVG' / 'MIN' / 'MAX'
        """
        all_pks = [k for k, _, _ in tbl.bt.scan()]
        if len(all_pks) < 1000:
            rows = self._scan_serial(tbl, all_pks, read_view, filter_fn)
            return self._merge_agg([self._local_agg(rows, agg_fn)], agg_fn)

        n = len(all_pks)
        workers = min(self.max_workers, (n + 999) // 1000)
        chunk = (n + workers - 1) // workers
        self.stats['parallel_queries'] += 1
        self.stats['total_workers_used'] += workers

        futures = []
        for i in range(workers):
            start = i * chunk
            end = min(start + chunk, n)
            if start >= end:
                break
            pks = all_pks[start:end]
            futures.append(
                self.pool.submit(self._agg_chunk, tbl, pks, read_view, agg_fn, filter_fn)
            )

        partials = []
        for f in as_completed(futures):
            try:
                partials.append(f.result())
            except Exception:
                pass

        return self._merge_agg(partials, agg_fn)

    def _agg_chunk(self, tbl, pks, read_view, agg_fn, filter_fn):
        rows = self._scan_serial(tbl, pks, read_view, filter_fn)
        return self._local_agg(rows, agg_fn)

    def _local_agg(self, rows, agg_fn):
        """局部聚合"""
        if agg_fn == 'COUNT':
            return {'count': len(rows)}
        if not rows:
            return {'count': 0, 'sum': 0, 'min': None, 'max': None, 'vals': []}
        # 单列：取第一列
        vals = [r[0] for r in rows if r and r[0] is not None]
        if agg_fn in ('SUM', 'AVG'):
            return {'count': len(vals), 'sum': sum(vals)}
        if agg_fn == 'MIN':
            return {'count': len(vals), 'min': min(vals) if vals else None}
        if agg_fn == 'MAX':
            return {'count': len(vals), 'max': max(vals) if vals else None}
        return {'count': len(vals)}

    def _merge_agg(self, partials, agg_fn):
        if agg_fn == 'COUNT':
            return sum(p.get('count', 0) for p in partials)
        if agg_fn == 'SUM':
            return sum(p.get('sum', 0) for p in partials)
        if agg_fn == 'AVG':
            total = sum(p.get('sum', 0) for p in partials)
            n = sum(p.get('count', 0) for p in partials)
            return total / n if n else None
        if agg_fn == 'MIN':
            vals = [p.get('min') for p in partials if p.get('min') is not None]
            return min(vals) if vals else None
        if agg_fn == 'MAX':
            vals = [p.get('max') for p in partials if p.get('max') is not None]
            return max(vals) if vals else None
        return None