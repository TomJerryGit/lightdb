"""
性能基准引擎
- 主键点查
- 范围扫描
- 批量插入
- 并发读写
"""
import random
import threading
import time
from collections import defaultdict


class BenchmarkRunner:
    def __init__(self, db, executor):
        self.db = db
        self.executor = executor
        self.results = {}

    def run_all(self, table_name=None, n_rows: int = 5000) -> dict:
        results = {}
        table = table_name or '__bench_table__'

        try:
            self._setup(table, n_rows)
        except Exception:
            pass

        results['insert_batch'] = self.bench_batch_insert(table, n_rows)
        results['point_select'] = self.bench_point_select(table, n_rows)
        results['range_scan'] = self.bench_range_scan(table)
        results['full_count'] = self.bench_full_count(table)
        results['concurrent_select'] = self.bench_concurrent(table, n_rows)

        return results

    def _setup(self, table, n):
        try:
            self.db.drop_table(table)
        except Exception:
            pass
        self.db.create_table(table, [
            {'name': 'id', 'type': 'INT', 'primary': True,
             'notnull': True, 'unique': False},
            {'name': 'name', 'type': 'VARCHAR', 'primary': False,
             'notnull': False, 'unique': False},
            {'name': 'score', 'type': 'INT', 'primary': False,
             'notnull': False, 'unique': False},
        ])

    # ============ 基准 ============

    def bench_batch_insert(self, table, n):
        """批量插入"""
        from ..sql.parser import parser
        tbl = self.db.get_table(table)

        t0 = time.time()
        for i in range(n):
            tbl.insert([i + 1, f'user{i}', random.randint(0, 100)])
        t1 = time.time()
        elapsed = t1 - t0
        return {
            'name': 'batch_insert',
            'rows': n,
            'elapsed': elapsed,
            'rows_per_sec': n / elapsed if elapsed > 0 else 0,
        }

    def bench_point_select(self, table, n):
        """主键点查"""
        from ..sql.parser import parser
        ex = self.executor

        # 预热
        for _ in range(10):
            ex.execute(parser.parse(
                f"SELECT * FROM {table} WHERE id = {random.randint(1, n)}"), {})

        N = 1000
        t0 = time.time()
        for _ in range(N):
            pk = random.randint(1, n)
            ex.execute(parser.parse(
                f"SELECT * FROM {table} WHERE id = {pk}"), {})
        t1 = time.time()
        elapsed = t1 - t0
        return {
            'name': 'point_select',
            'queries': N,
            'elapsed': elapsed,
            'qps': N / elapsed if elapsed > 0 else 0,
            'avg_ms': elapsed / N * 1000,
        }

    def bench_range_scan(self, table):
        """范围扫描"""
        from ..sql.parser import parser
        ex = self.executor
        N = 100
        t0 = time.time()
        for _ in range(N):
            lo = random.randint(1, 1000)
            hi = lo + 500
            ex.execute(parser.parse(
                f"SELECT * FROM {table} WHERE id >= {lo} AND id <= {hi}"), {})
        t1 = time.time()
        elapsed = t1 - t0
        return {
            'name': 'range_scan',
            'queries': N,
            'elapsed': elapsed,
            'qps': N / elapsed if elapsed > 0 else 0,
        }

    def bench_full_count(self, table):
        """全表 COUNT"""
        from ..sql.parser import parser
        ex = self.executor
        N = 20
        t0 = time.time()
        for _ in range(N):
            ex.execute(parser.parse(f"SELECT COUNT(*) FROM {table}"), {})
        t1 = time.time()
        elapsed = t1 - t0
        return {
            'name': 'full_count',
            'queries': N,
            'elapsed': elapsed,
            'qps': N / elapsed if elapsed > 0 else 0,
            'avg_ms': elapsed / N * 1000,
        }

    def bench_concurrent(self, table, n, threads: int = 10, per_thread: int = 100):
        """并发查询"""
        from ..sql.parser import parser
        ex = self.executor
        errors = []
        latencies = []
        lock = threading.Lock()

        def worker(wid):
            try:
                for i in range(per_thread):
                    pk = (wid * per_thread + i) % n + 1
                    t0 = time.time()
                    ex.execute(parser.parse(
                        f"SELECT * FROM {table} WHERE id = {pk}"), {})
                    dt = time.time() - t0
                    with lock:
                        latencies.append(dt)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        t0 = time.time()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        elapsed = time.time() - t0

        total = len(latencies)
        avg = sum(latencies) / total if total else 0
        sorted_lat = sorted(latencies)
        p99 = sorted_lat[int(total * 0.99)] if total else 0

        return {
            'name': 'concurrent_select',
            'threads': threads,
            'total': total,
            'errors': len(errors),
            'elapsed': elapsed,
            'qps': total / elapsed if elapsed > 0 else 0,
            'avg_ms': avg * 1000,
            'p99_ms': p99 * 1000,
        }


def format_results(results: dict) -> str:
    lines = ["=" * 60, "  Benchmark Results", "=" * 60]
    for k, v in results.items():
        lines.append(f"\n[{k}]")
        for fk, fv in v.items():
            if fk == 'name':
                continue
            if isinstance(fv, float):
                lines.append(f"  {fk}: {fv:.2f}")
            else:
                lines.append(f"  {fk}: {fv}")
    lines.append("=" * 60)
    return '\n'.join(lines)