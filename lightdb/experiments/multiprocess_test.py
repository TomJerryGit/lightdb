"""
多进程技术验证
- 模拟多进程架构
- 测试 IPC 开销
- 对比单进程
"""
import multiprocessing as mp
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class MPTestResult:
    mode: str
    n_workers: int
    duration: float
    total_ops: int
    ops_per_sec: float
    cpu_percent: float


class MultiprocessExperiment:
    """多进程实验"""

    @staticmethod
    def _worker_cpu(intensity: int = 10000):
        """CPU 密集的工作单元"""
        x = 0
        for i in range(intensity):
            x += i * i
        return x

    # ==================== 单进程基线 ====================

    def single_process(self, duration: int = 5) -> MPTestResult:
        """单进程基线"""
        print("  [单进程] 测试中...")
        t0 = time.time()
        count = 0

        while time.time() - t0 < duration:
            self._worker_cpu()
            count += 1

        elapsed = time.time() - t0
        return MPTestResult(
            mode='single_process',
            n_workers=1,
            duration=elapsed,
            total_ops=count,
            ops_per_sec=count / elapsed,
            cpu_percent=100.0,
        )

    # ==================== 多线程 ====================

    def multi_thread(self, n_threads: int,
                     duration: int = 5) -> MPTestResult:
        print(f"  [{n_threads} 线程] 测试中...")
        stop = threading.Event()
        counters = [0] * n_threads

        def worker(idx):
            count = 0
            while not stop.is_set():
                self._worker_cpu()
                count += 1
            counters[idx] = count

        threads = [threading.Thread(target=worker, args=(i,))
                    for i in range(n_threads)]
        t0 = time.time()
        for t in threads:
            t.start()
        time.sleep(duration)
        stop.set()
        for t in threads:
            t.join()
        elapsed = time.time() - t0

        total = sum(counters)
        return MPTestResult(
            mode='multi_thread',
            n_workers=n_threads,
            duration=elapsed,
            total_ops=total,
            ops_per_sec=total / elapsed,
            cpu_percent=100.0 * (total / (total / n_threads)) if total else 0,
        )

    # ==================== 多进程（fork）====================

    @staticmethod
    def _proc_worker(duration: float, q: mp.Queue):
        t0 = time.time()
        count = 0
        while time.time() - t0 < duration:
            x = 0
            for i in range(10000):
                x += i * i
            count += 1
        q.put(count)

    def multi_process(self, n_procs: int,
                       duration: int = 5) -> MPTestResult:
        print(f"  [{n_procs} 进程] 测试中...")
        q = mp.Queue()
        procs = [mp.Process(target=self._proc_worker,
                              args=(duration, q))
                  for _ in range(n_procs)]

        t0 = time.time()
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        elapsed = time.time() - t0

        total = 0
        while not q.empty():
            total += q.get()

        return MPTestResult(
            mode='multi_process',
            n_workers=n_procs,
            duration=elapsed,
            total_ops=total,
            ops_per_sec=total / elapsed,
            cpu_percent=100.0,
        )

    # ==================== IPC 开销测试 ====================

    def test_ipc_latency(self, n_ops: int = 10000) -> dict:
        """
        IPC 延迟测试
        - pipe
        - queue
        - shared_memory
        - unix socket
        """
        print(f"\n【IPC 延迟测试 ({n_ops} 次)】")
        results = {}

        # 1. Pipe
        try:
            results['pipe'] = self._test_pipe(n_ops)
            print(f"  Pipe:         {results['pipe']['latency_us']:.2f} µs")
        except Exception as e:
            print(f"  Pipe: 失败 {e}")

        # 2. Queue
        try:
            results['queue'] = self._test_queue(n_ops // 10)
            print(f"  Queue:        {results['queue']['latency_us']:.2f} µs")
        except Exception as e:
            print(f"  Queue: 失败 {e}")

        # 3. Shared Memory
        try:
            results['shm'] = self._test_shm(n_ops)
            print(f"  SharedMem:    {results['shm']['latency_us']:.3f} µs")
        except Exception as e:
            print(f"  SharedMem: 失败 {e}")

        # 4. Unix Socket
        try:
            results['unix_socket'] = self._test_unix_socket(n_ops // 10)
            print(f"  UnixSocket:   {results['unix_socket']['latency_us']:.2f} µs")
        except Exception as e:
            print(f"  UnixSocket: 失败 {e}")

        return results

    def _test_pipe(self, n: int) -> dict:
        import os
        r, w = os.pipe()

        def reader():
            for _ in range(n):
                os.read(r, 4)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        data = b'\x01\x02\x03\x04'
        t0 = time.perf_counter()
        for _ in range(n):
            os.write(w, data)
        t1 = time.perf_counter()
        t.join(timeout=2)

        os.close(r)
        os.close(w)
        return {'latency_us': (t1 - t0) / n * 1e6}

    def _test_queue(self, n: int) -> dict:
        q = mp.Queue()
        stop = [False]

        def reader():
            while not stop[0]:
                try:
                    q.get(timeout=0.1)
                except queue.Empty:
                    pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        t0 = time.perf_counter()
        for i in range(n):
            q.put(i)
        t1 = time.perf_counter()
        stop[0] = True
        t.join(timeout=1)

        return {'latency_us': (t1 - t0) / n * 1e6}

    def _test_shm(self, n: int) -> dict:
        from multiprocessing import shared_memory
        try:
            shm = shared_memory.SharedMemory(
                create=True, size=1024)
            buf = shm.buf

            t0 = time.perf_counter()
            for i in range(n):
                buf[0] = i & 0xff
            t1 = time.perf_counter()

            shm.close()
            shm.unlink()
            return {'latency_us': (t1 - t0) / n * 1e6}
        except Exception:
            return {'latency_us': 0}

    def _test_unix_socket(self, n: int) -> dict:
        import tempfile
        path = tempfile.mktemp(suffix='.sock')

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def server_loop():
            conn, _ = server.accept()
            for _ in range(n):
                conn.recv(4)

        t = threading.Thread(target=server_loop, daemon=True)
        t.start()
        time.sleep(0.01)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)

        data = b'\x01\x02\x03\x04'
        t0 = time.perf_counter()
        for _ in range(n):
            client.send(data)
        t1 = time.perf_counter()

        client.close()
        server.close()
        os.unlink(path)
        t.join(timeout=1)

        return {'latency_us': (t1 - t0) / n * 1e6}

    # ==================== 综合实验 ====================

    def run_all(self) -> dict:
        """完整对比实验"""
        print("=" * 70)
        print("  多进程架构验证实验")
        print("=" * 70)

        n_cpu = mp.cpu_count()
        duration = 5

        result = {'cpu_count': n_cpu, 'runs': []}

        print(f"\n【并行度对比 (CPU 核数: {n_cpu})】\n")

        # 单进程
        r = self.single_process(duration)
        result['runs'].append(r.__dict__)
        base = r.ops_per_sec
        print(f"    单进程: {r.ops_per_sec:>10.0f} ops/s (基准)")

        # 多线程
        for n in [2, 4, min(8, n_cpu)]:
            r = self.multi_thread(n, duration)
            r_dict = r.__dict__
            r_dict['speedup'] = r.ops_per_sec / base
            r_dict['efficiency'] = r_dict['speedup'] / n
            result['runs'].append(r_dict)
            print(f"    {n} 线程: {r.ops_per_sec:>10.0f} ops/s "
                  f"({r_dict['speedup']:.2f}x, {r_dict['efficiency']*100:.0f}%)")

        # 多进程
        for n in [2, 4, min(8, n_cpu)]:
            r = self.multi_process(n, duration)
            r_dict = r.__dict__
            r_dict['speedup'] = r.ops_per_sec / base
            r_dict['efficiency'] = r_dict['speedup'] / n
            result['runs'].append(r_dict)
            print(f"    {n} 进程: {r.ops_per_sec:>10.0f} ops/s "
                  f"({r_dict['speedup']:.2f}x, {r_dict['efficiency']*100:.0f}%)")

        # IPC 测试
        result['ipc'] = self.test_ipc_latency(10000)

        # 结论
        best_thread = max(
            (r for r in result['runs'] if r['mode'] == 'multi_thread'),
            key=lambda x: x.get('speedup', 0),
            default={'speedup': 1}
        )
        best_process = max(
            (r for r in result['runs'] if r['mode'] == 'multi_process'),
            key=lambda x: x.get('speedup', 0),
            default={'speedup': 1}
        )

        result['conclusion'] = {
            'thread_speedup': best_thread.get('speedup', 1),
            'process_speedup': best_process.get('speedup', 1),
            'process_advantage': (best_process.get('speedup', 1) /
                                    max(best_thread.get('speedup', 1), 0.01)),
            'verdict': '多进程明显占优，推荐 v4.0 采用' if
                        best_process.get('speedup', 1) > 2 else
                        '需进一步验证',
        }

        print(f"\n【结论】")
        print(f"  最佳线程加速: {result['conclusion']['thread_speedup']:.2f}x")
        print(f"  最佳进程加速: {result['conclusion']['process_speedup']:.2f}x")
        print(f"  进程优势:     {result['conclusion']['process_advantage']:.2f}x")
        print(f"  判定:         {result['conclusion']['verdict']}")

        return result


if __name__ == '__main__':
    exp = MultiprocessExperiment()
    exp.run_all()