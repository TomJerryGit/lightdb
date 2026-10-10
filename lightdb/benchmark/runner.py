"""
统一基准测试运行器
"""
import threading
import time
import uuid
from collections import defaultdict
from typing import Dict, List

from .workloads import get_workload, WorkloadResult


class BenchmarkRunner:
    """多线程基准测试"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    # ==================== 连接管理 ====================

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.Cursor,
        )

    def check_connection(self) -> bool:
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return False

    # ==================== 主入口 ====================

    def run(self, workload='all', tables=4, rows=100000,
            threads=8, duration=30, warmup=5) -> Dict:
        """运行基准测试"""
        if not self.check_connection():
            raise RuntimeError("无法连接数据库")

        run_id = uuid.uuid4().hex[:8]
        t0 = time.time()

        print(f"\n{'='*60}")
        print(f"  LightDB Benchmark")
        print(f"{'='*60}")
        print(f"  目标: {self.host}:{self.port}")
        print(f"  工作负载: {workload}")
        print(f"  表数: {tables}  行数: {rows}  线程: {threads}")
        print(f"  时长: {duration}s  预热: {warmup}s")
        print(f"  Run ID: {run_id}")
        print(f"{'='*60}\n")

        # 确定要跑的工作负载列表
        if workload == 'all':
            workloads = ['point_select', 'range_scan',
                          'insert', 'update', 'oltp_ro',
                          'oltp_rw']
        else:
            workloads = [workload]

        results = {}
        metadata = {
            'run_id': run_id,
            'host': self.host,
            'port': self.port,
            'tables': tables,
            'rows': rows,
            'threads': threads,
            'duration': duration,
            'warmup': warmup,
            'timestamp': t0,
        }

        # 逐负载测试
        for wname in workloads:
            print(f"\n▶ {wname}")
            try:
                result = self._run_one(
                    wname, tables, rows, threads, duration, warmup)
                results[wname] = result.to_dict()
                self._print_result(result)
            except Exception as e:
                print(f"  ❌ 失败: {e}")
                results[wname] = {'error': str(e)}

        metadata['total_elapsed_s'] = time.time() - t0

        return {
            'metadata': metadata,
            'results': results,
        }

    # ==================== 单个负载 ====================

    def _run_one(self, workload_name: str, tables: int,
                 rows: int, threads: int,
                 duration: int, warmup: int) -> WorkloadResult:
        """执行单负载测试"""
        cls = get_workload(workload_name)
        if not cls:
            raise ValueError(f"未知工作负载: {workload_name}")

        # 准备阶段：建表、插数据
        print(f"  准备数据（{rows} 行 × {tables} 表）...")
        prep_t0 = time.time()
        main_conn = self._connect()
        wl = cls(main_conn, table='sbtest1')
        wl.setup(rows)
        prep_elapsed = time.time() - prep_t0
        main_conn.close()
        print(f"  ✓ 准备完成，耗时 {prep_elapsed:.1f}s")

        # 预热
        if warmup > 0:
            print(f"  预热 {warmup}s ...")
            self._run_threads(workload_name, threads, warmup)
            print(f"  ✓ 预热完成")

        # 正式测试
        print(f"  正式测试 {duration}s ...")
        result = self._run_threads(
            workload_name, threads, duration)
        return result

    def _run_threads(self, workload_name: str,
                     threads: int,
                     duration: int) -> WorkloadResult:
        """启动 N 个线程执行"""
        cls = get_workload(workload_name)
        result = WorkloadResult(name=workload_name)

        # 共享计数
        latencies = []
        errors = []
        lock = threading.Lock()
        stop_flag = threading.Event()

        def worker():
            try:
                conn = self._connect()
                wl = cls(conn, table='sbtest1')
                # 从主连接获取 _max_id
                wl._max_id = self._get_max_id(conn)
            except Exception as e:
                with lock:
                    errors.append(str(e))
                return

            local_lat = []
            local_count = 0

            while not stop_flag.is_set():
                t0 = time.perf_counter()
                try:
                    wl.execute_once()
                    local_count += 1
                except Exception as e:
                    with lock:
                        errors.append(str(e))
                t1 = time.perf_counter()
                local_lat.append((t1 - t0) * 1000)

            with lock:
                latencies.extend(local_lat)

            try:
                conn.close()
            except Exception:
                pass

        # 启动
        thread_list = []
        for _ in range(threads):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            thread_list.append(t)

        # 等待
        time.sleep(duration)
        stop_flag.set()

        for t in thread_list:
            t.join(timeout=10)

        # 汇总
        result.queries = len(latencies)
        result.errors = len(errors)
        result.latencies_ms = latencies
        result.total_ms = duration * 1000

        # 特殊：insert/delete 的 TPS
        if workload_name in ('insert', 'delete', 'oltp_rw'):
            result.tps = result.queries / duration

        result.compute_stats(duration)
        return result

    def _get_max_id(self, conn) -> int:
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM sbtest1")
            row = cur.fetchone()
            return row[0] if row and row[0] else 100000
        except Exception:
            return 100000

    # ==================== 输出 ====================

    def _print_result(self, result: WorkloadResult):
        print(f"  QPS:     {result.qps:>10.1f}")
        if result.tps:
            print(f"  TPS:     {result.tps:>10.1f}")
        print(f"  平均延迟: {result.avg_lat:>10.3f} ms")
        print(f"  P50:     {result.p50:>10.3f} ms")
        print(f"  P95:     {result.p95:>10.3f} ms")
        print(f"  P99:     {result.p99:>10.3f} ms")
        print(f"  P999:    {result.p999:>10.3f} ms")
        print(f"  最大:    {result.max_lat:>10.3f} ms")
        if result.errors:
            print(f"  ⚠️ 错误: {result.errors}")