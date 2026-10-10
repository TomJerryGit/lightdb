"""
GIL 影响量化
- 多线程 vs 多进程对比
- 计算 GIL 影响曲线
- IO/CPU 边界识别
"""
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Callable, Dict, List


@dataclass
class GILTestResult:
    """GIL 测试结果"""
    workload: str
    threads: int
    duration_s: float
    total_ops: int = 0
    ops_per_sec: float = 0
    cpu_percent: float = 0
    vs_single_thread: float = 1.0   # 相对单线程的加速比
    efficiency: float = 0           # 并行效率 = 加速比 / 线程数


class GILAnalyzer:
    """GIL 影响分析器"""

    def __init__(self, conn_provider=None):
        self.conn_provider = conn_provider

    # ==================== 测试基础操作 ====================

    def _cpu_bound_work(self, n: int = 100000):
        """CPU 密集任务"""
        x = 0
        for i in range(n):
            x += i * i
        return x

    def _io_bound_work(self, n: int = 100):
        """IO 密集任务（模拟网络）"""
        for _ in range(n):
            time.sleep(0.001)

    def _db_query_work(self, n: int = 1000):
        """数据库查询任务"""
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return
        cur = conn.cursor()
        for _ in range(n):
            cur.execute("SELECT 1")
            cur.fetchone()

    # ==================== 测试运行器 ====================

    def test_multithread(self, work_func: Callable,
                          threads: int, duration: float) -> int:
        """多线程测试"""
        stop = threading.Event()
        total = [0]   # 共享计数器
        lock = threading.Lock()

        def worker():
            local_count = 0
            while not stop.is_set():
                work_func()
                local_count += 1
            with lock:
                total[0] += local_count

        thread_list = [
            threading.Thread(target=worker, daemon=True)
            for _ in range(threads)
        ]
        for t in thread_list:
            t.start()

        time.sleep(duration)
        stop.set()

        for t in thread_list:
            t.join(timeout=2)

        return total[0]

    def test_multiprocess(self, work_func: Callable,
                           processes: int, duration: float) -> int:
        """多进程测试"""
        # 子进程的工作函数
        def proc_worker(func, duration, counter):
            import time
            stop_time = time.time() + duration
            count = 0
            while time.time() < stop_time:
                func()
                count += 1
            counter.value = count

        counter = multiprocessing.Value('i', 0)
        procs = [
            multiprocessing.Process(
                target=proc_worker,
                args=(work_func, duration, counter))
            for _ in range(processes)
        ]

        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=duration + 5)

        return counter.value

    # ==================== 主测试 ====================

    def measure_gil(self, workload: str = 'cpu',
                     max_workers: int = 8,
                     duration: int = 5) -> Dict:
        """
        测量 GIL 影响
        workload: cpu / io / db
        """
        print(f"\n{'='*70}")
        print(f"  GIL 影响量化 - {workload}")
        print(f"{'='*70}\n")

        # 选择工作函数
        if workload == 'cpu':
            work_func = self._cpu_bound_work
        elif workload == 'io':
            work_func = self._io_bound_work
        elif workload == 'db':
            work_func = self._db_query_work
        else:
            raise ValueError(f"未知负载: {workload}")

        # 测试不同并行度
        results = {
            'workload': workload,
            'duration': duration,
            'threads': [],
            'processes': [],
        }

        # 单线程基线
        print("【多线程测试】")
        for n in [1, 2, 4, max_workers]:
            if n > max_workers:
                continue
            ops = self.test_multithread(work_func, n, duration)
            r = {
                'threads': n,
                'ops': ops,
                'ops_per_sec': ops / duration,
            }
            if results['threads']:
                base_ops = results['threads'][0]['ops_per_sec']
                r['speedup'] = r['ops_per_sec'] / base_ops if base_ops else 0
                r['efficiency'] = r['speedup'] / n
            else:
                r['speedup'] = 1.0
                r['efficiency'] = 1.0
            results['threads'].append(r)
            print(f"  {n} 线程: {r['ops_per_sec']:>10.0f} ops/s "
                  f"(加速 {r['speedup']:.2f}x, 效率 {r['efficiency']*100:.0f}%)")

        print("\n【多进程测试】")
        for n in [1, 2, 4, max_workers]:
            if n > max_workers:
                continue
            try:
                ops = self.test_multiprocess(work_func, n, duration)
            except Exception as e:
                print(f"  {n} 进程: 失败 {e}")
                continue
            r = {
                'processes': n,
                'ops': ops,
                'ops_per_sec': ops / duration,
            }
            if results['processes']:
                base_ops = results['processes'][0]['ops_per_sec']
                r['speedup'] = r['ops_per_sec'] / base_ops if base_ops else 0
                r['efficiency'] = r['speedup'] / n
            else:
                r['speedup'] = 1.0
                r['efficiency'] = 1.0
            results['processes'].append(r)
            print(f"  {n} 进程: {r['ops_per_sec']:>10.0f} ops/s "
                  f"(加速 {r['speedup']:.2f}x, 效率 {r['efficiency']*100:.0f}%)")

        # GIL 影响结论
        max_t = results['threads'][-1]
        max_p = results['processes'][-1]

        results['conclusion'] = {
            'thread_efficiency': max_t['efficiency'],
            'process_efficiency': max_p['efficiency'],
            'gil_impact_pct': (1 - max_t['efficiency']) * 100,
            'verdict': self._verdict(max_t['efficiency']),
        }

        print(f"\n【结论】")
        print(f"  线程并行效率: {max_t['efficiency']*100:.0f}%")
        print(f"  进程并行效率: {max_p['efficiency']*100:.0f}%")
        print(f"  GIL 影响: {results['conclusion']['gil_impact_pct']:.0f}%")
        print(f"  判定: {results['conclusion']['verdict']}")

        return results

    def _verdict(self, thread_efficiency: float) -> str:
        if thread_efficiency > 0.8:
            return "无需优化"
        elif thread_efficiency > 0.5:
            return "轻度 GIL 影响，优先其他优化"
        elif thread_efficiency > 0.3:
            return "中度 GIL 影响，建议多进程"
        else:
            return "严重 GIL 影响，必须多进程"

    # ==================== IO/CPU 边界判定 ====================

    def analyze_bound_type(self) -> Dict:
        """分析 LightDB 是 CPU 边界还是 IO 边界"""
        print("\n【边界类型分析】")

        # 分别测 CPU 和 IO 负载
        cpu_r = self.measure_gil('cpu', max_workers=4, duration=3)
        io_r = self.measure_gil('io', max_workers=4, duration=3)

        cpu_eff = cpu_r['threads'][-1]['efficiency']
        io_eff = io_r['threads'][-1]['efficiency']

        print(f"\n  CPU 负载线程效率: {cpu_eff*100:.0f}%")
        print(f"  IO 负载线程效率:  {io_eff*100:.0f}%")

        if cpu_eff < io_eff:
            verdict = 'cpu_bound'
            detail = "CPU 边界，多进程收益大"
        else:
            verdict = 'io_bound'
            detail = "IO 边界，多线程已够用"

        return {
            'cpu_efficiency': cpu_eff,
            'io_efficiency': io_eff,
            'verdict': verdict,
            'detail': detail,
        }


# ==================== 命令行 ====================

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--workload', default='cpu',
                    choices=['cpu', 'io', 'db'])
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--duration', type=int, default=5)
    args = ap.parse_args()

    analyzer = GILAnalyzer()
    result = analyzer.measure_gil(
        args.workload, args.workers, args.duration)

    # JSON 输出
    import json
    print("\n【JSON 结果】")
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()