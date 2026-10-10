"""
多进程架构回归
"""
import multiprocessing as mp
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_worker_pool_restart():
    """测试：Worker 崩溃自动重启"""
    print("\n【Worker 池重启】")
    from lightdb.concurrency.multiprocess import WorkerPool

    def fake_worker(worker_id, config, shared_state):
        time.sleep(10)

    pool = WorkerPool(
        worker_func=fake_worker,
        size=2,
        config={},
        shared_state={})
    pool.start()
    time.sleep(1)

    stats1 = pool.stats()
    assert stats1['alive'] == 2
    print(f"  ✅ 2 Worker 启动")

    # 强杀第一个
    p = list(pool.workers.values())[0]
    if p.process:
        p.process.kill()

    # 等待重启
    time.sleep(3)

    stats2 = pool.stats()
    print(f"  → 重启次数: {stats2['total_restarts']}")
    assert stats2['total_restarts'] >= 1

    pool.stop()
    print(f"  ✅ 自动重启生效")


def test_shared_counter():
    """测试：共享计数器并发安全"""
    print("\n【共享计数器】")
    from lightdb.concurrency.shared_memory import (
        SharedMemoryManager, SharedCounter)

    mgr = SharedMemoryManager(namespace='regtest')
    shm = mgr.create('counter', 1024)
    counter = SharedCounter(shm, 0)

    N_WORKERS = 4
    N_ITER = 1000

    def worker():
        from lightdb.concurrency.shared_memory import (
            SharedMemoryManager, SharedCounter)
        mgr2 = SharedMemoryManager(namespace='regtest')
        shm2 = mgr2.open('counter', 1024)
        c = SharedCounter(shm2, 0)
        for _ in range(N_ITER):
            c.increment()

    procs = [mp.Process(target=worker) for _ in range(N_WORKERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    final = counter.get()
    expected = N_WORKERS * N_ITER
    print(f"  → 期望 {expected}, 实际 {final}")
    assert final == expected
    print(f"  ✅ 并发安全")

    mgr.destroy_all()


def test_ipc_latency():
    """测试：IPC 延迟"""
    print("\n【IPC 延迟】")
    import socket
    import threading

    # 用 socketpair 测试
    a, b = socket.socketpair()
    t = threading.Thread(
        target=lambda: [b.recv(4) for _ in range(10000)],
        daemon=True)
    t.start()

    data = b'\x01\x02\x03\x04'
    t0 = time.perf_counter()
    for _ in range(10000):
        a.send(data)
    t1 = time.perf_counter()
    a.close()
    b.close()
    t.join(timeout=1)

    latency_us = (t1 - t0) / 10000 * 1e6
    print(f"  → socketpair 延迟: {latency_us:.2f} µs")
    assert latency_us < 100
    print(f"  ✅ IPC 延迟正常")


def test_reuseport_multiple():
    """测试：多 Worker SO_REUSEPORT"""
    print("\n【SO_REUSEPORT 多 Worker】")
    from lightdb.net.listener import HAS_REUSEPORT, ReusePortListener

    if not HAS_REUSEPORT:
        print("  ⚠️ 平台不支持，跳过")
        return

    port = 28991
    listeners = []
    try:
        for _ in range(4):
            l = ReusePortListener('127.0.0.1', port)
            l.bind()
            listeners.append(l)
        print(f"  ✅ 4 个监听器共端口 {port}")
    finally:
        for l in listeners:
            l.close()


def test_multiprocess_parallelism():
    """测试：多进程并行效率"""
    print("\n【多进程并行】")

    def cpu_work(n):
        x = 0
        for i in range(n):
            x += i * i
        return x

    # 4 进程 vs 串行
    t0 = time.perf_counter()
    for _ in range(4):
        cpu_work(200000)
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    with mp.Pool(4) as p:
        p.map(cpu_work, [200000] * 4)
    parallel = time.perf_counter() - t0

    speedup = serial / parallel if parallel > 0 else 0
    print(f"  → 串行: {serial*1000:.0f}ms")
    print(f"  → 并行: {parallel*1000:.0f}ms")
    print(f"  → 加速: {speedup:.2f}x")

    if speedup >= 2.0:
        print(f"  ✅ 多进程有效利用多核")
    else:
        print(f"  ⚠️ 加速有限")


def main():
    print("=" * 60)
    print("  v4.0 多进程回归")
    print("=" * 60)

    tests = [
        test_worker_pool_restart,
        test_shared_counter,
        test_ipc_latency,
        test_reuseport_multiple,
        test_multiprocess_parallelism,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n通过: {passed}, 失败: {failed}")
    return failed


if __name__ == '__main__':
    sys.exit(main())