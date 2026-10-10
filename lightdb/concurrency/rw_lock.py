"""
读写锁（Read-Write Lock）
- 多读互不阻塞
- 写独占
- 写优先，防止写饥饿
"""
import threading
import time
from contextlib import contextmanager


class RWLock:
    """读写锁（写优先）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._read_ready = threading.Condition(self._lock)
        self._readers = 0
        self._writers_waiting = 0
        self._writer_active = False

    def acquire_read(self):
        with self._read_ready:
            while self._writer_active or self._writers_waiting > 0:
                self._read_ready.wait()
            self._readers += 1

    def release_read(self):
        with self._read_ready:
            self._readers -= 1
            if self._readers == 0:
                self._read_ready.notify_all()

    def acquire_write(self):
        with self._read_ready:
            self._writers_waiting += 1
            while self._readers > 0 or self._writer_active:
                self._read_ready.wait()
            self._writers_waiting -= 1
            self._writer_active = True

    def release_write(self):
        with self._read_ready:
            self._writer_active = False
            self._read_ready.notify_all()

    @contextmanager
    def read(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()

    # 可重入版本
    def acquire_read_reentrant(self):
        tid = threading.get_ident()
        if not hasattr(self, '_owned'):
            self._owned = {}
        if self._owned.get(tid, (0, 0))[0] > 0:
            self._owned[tid] = (self._owned[tid][0] + 1, self._owned[tid][1])
        else:
            self.acquire_read()
            self._owned[tid] = (1, 0)

    def release_read_reentrant(self):
        tid = threading.get_ident()
        r, w = self._owned.get(tid, (0, 0))
        if r > 1:
            self._owned[tid] = (r - 1, w)
        elif r == 1:
            self._owned.pop(tid, None)
            self.release_read()


class TableRWLock:
    """表级读写锁（分表）"""

    def __init__(self):
        self._locks = {}
        self._global = threading.RLock()

    def _get(self, table: str) -> RWLock:
        with self._global:
            if table not in self._locks:
                self._locks[table] = RWLock()
            return self._locks[table]

    @contextmanager
    def read(self, table: str):
        lock = self._get(table)
        with lock.read():
            yield

    @contextmanager
    def write(self, table: str):
        lock = self._get(table)
        with lock.write():
            yield


# ==================== 性能对比 ====================

def benchmark_rw_vs_mutex():
    """对比读写锁 vs 普通互斥锁"""
    import time
    import threading

    N_THREADS = 10
    N_OPS = 100000

    # 场景：8 读 2 写

    def work_rw(lock, results, tid):
        for i in range(N_OPS):
            if tid < 8:
                with lock.read():
                    time.sleep(0)  # 模拟微小工作
            else:
                with lock.write():
                    time.sleep(0)

    def work_mutex(lock, results, tid):
        for i in range(N_OPS):
            with lock:
                time.sleep(0)

    # RW 锁
    rw = RWLock()
    threads = []
    t0 = time.perf_counter()
    for i in range(N_THREADS):
        t = threading.Thread(target=work_rw, args=(rw, None, i))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    t1 = time.perf_counter()
    rw_time = t1 - t0

    # 互斥锁
    mtx = threading.RLock()
    threads = []
    t0 = time.perf_counter()
    for i in range(N_THREADS):
        t = threading.Thread(target=work_mutex, args=(mtx, None, i))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    t1 = time.perf_counter()
    mtx_time = t1 - t0

    print(f"互斥锁:   {mtx_time:.3f}s")
    print(f"读写锁:   {rw_time:.3f}s")
    print(f"加速:     {mtx_time/rw_time:.2f}x")


if __name__ == '__main__':
    benchmark_rw_vs_mutex()