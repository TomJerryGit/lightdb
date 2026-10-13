"""v4.0 Batch 2 测试 - 多进程架构"""
import os
import sys
import time
import socket
import threading
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 沙箱能力探测：多进程原语（Queue/Pipe/Process/Pool）被禁用时 skip 而不是误报失败
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _conftest  # noqa: E402


def _cpu_work(n):
    """CPU 密集负载（必须模块级，spawn 下 mp.Pool.map 需要 pickle 它）"""
    x = 0
    for i in range(n):
        x += i * i
    return x


def _fake_worker_for_pool(worker_id, config, shared_state):
    """WorkerPool 的测试用 Worker

    必须是模块级函数：Windows 用 spawn 启动子进程，嵌套函数无法 pickle。
    """
    stats = shared_state.get('stats_hub')
    if stats:
        stats.report(worker_id, 'connections', 1)
    time.sleep(0.5)


def _shared_counter_child(counter_offset):
    """跨进程计数器测试的子进程入口（必须模块级，spawn 无法 pickle 嵌套函数）"""
    from lightdb.concurrency.shared_memory import (
        SharedMemoryManager, SharedCounter)
    mgr2 = SharedMemoryManager(namespace='test')
    shm2 = mgr2.open('test_shm', 1024)
    c = SharedCounter(shm2, offset=counter_offset)
    for _ in range(100):
        c.increment()


def test_has_reuseport():
    """测试1: SO_REUSEPORT 检测"""
    print("\n【测试1】SO_REUSEPORT 支持检测")
    from lightdb.net.listener import HAS_REUSEPORT, IS_LINUX, IS_MAC

    print(f"  平台: Linux={IS_LINUX}, macOS={IS_MAC}")
    print(f"  SO_REUSEPORT: {'✅ 支持' if HAS_REUSEPORT else '❌ 不支持'}")

    if HAS_REUSEPORT:
        print(f"  → 将使用内核级负载均衡")
    else:
        print(f"  → 将降级使用 Master 转发")


def test_reuseport_listener():
    """测试2: SO_REUSEPORT 监听器"""
    print("\n【测试2】SO_REUSEPORT 监听器")
    from lightdb.net.listener import HAS_REUSEPORT, ReusePortListener

    if not HAS_REUSEPORT:
        print("  ⚠️ 平台不支持，跳过")
        return

    # 同时 bind 3 个相同端口
    port = 18991
    listeners = []
    try:
        for i in range(3):
            l = ReusePortListener('127.0.0.1', port)
            l.bind()
            listeners.append(l)
        print(f"  ✅ 3 个监听器同时 bind {port} 成功")

        # 连接测试：内核会自动分配
        conns = []
        for _ in range(10):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            try:
                s.connect(('127.0.0.1', port))
                conns.append(s)
            except Exception:
                pass

        print(f"  → 建立 {len(conns)} 个连接")
        assert len(conns) >= 5
        for s in conns:
            s.close()
    finally:
        for l in listeners:
            l.close()
    print("  ✅ 内核负载均衡生效")


def test_shared_memory():
    """测试3: 共享内存"""
    print("\n【测试3】共享内存")
    from lightdb.concurrency.shared_memory import (
        SharedMemoryManager, SharedCounter, SharedLock)

    # 该用例跨进程 + 共享内存，先探测环境能力（探测失败即 skip）
    _conftest.require_mp_primitives()

    mgr = SharedMemoryManager(namespace='test')

    # 创建
    shm = mgr.create('test_shm', 1024)
    print(f"  ✅ 创建共享内存 1024B")

    # 写读
    shm.write(0, b'hello')
    data = shm.read(0, 5)
    assert data == b'hello'
    print(f"  ✅ 写读正确")

    # 计数器
    counter = SharedCounter(shm, offset=100)
    v1 = counter.increment()
    v2 = counter.increment()
    assert v1 == 1 and v2 == 2
    print(f"  ✅ 计数器: {v1}, {v2}")

    # 跨进程测试
    import multiprocessing as mp

    procs = [mp.Process(target=_shared_counter_child, args=(100,))
              for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    final = counter.get()
    print(f"  → 4 进程各加 100 后: {final}")
    assert final == 402, f"期望 402，实际 {final}"
    print(f"  ✅ 跨进程计数正确")

    mgr.destroy_all()


def test_worker_pool():
    """测试4: Worker 池"""
    print("\n【测试4】Worker 池")
    from lightdb.concurrency.multiprocess import WorkerPool

    # 该用例起真实子进程，先探测环境能力（探测失败即 skip）
    _conftest.require_mp_primitives()

    # 注意：Windows 用 spawn 启动子进程，target 必须是模块级函数，
    # 局部的嵌套函数无法被 pickle（见文件顶部 _fake_worker_for_pool）
    pool = WorkerPool(
        worker_func=_fake_worker_for_pool,
        size=2,
        config={},
        shared_state={},
    )
    pool.start()
    time.sleep(0.5)

    stats = pool.stats()
    print(f"  → 状态: size={stats['size']}, "
          f"alive={stats['alive']}, ready={stats['ready']}")

    assert stats['alive'] >= 1

    pool.stop()
    print(f"  ✅ Worker 池正常停止")


def test_shared_buffer_pool():
    """测试5: 共享 Buffer Pool"""
    print("\n【测试5】共享 Buffer Pool")
    from lightdb.cluster.shared_buffer import SharedBufferPool

    # 该用例建立在共享内存段 + 进程间原语之上，先探测环境能力
    _conftest.require_mp_primitives()

    pool = SharedBufferPool(
        name='test_bp', slot_count=100, create=True)

    # 写入页
    page_data = b'A' * 4096
    pool.put_page(1, page_data)
    pool.put_page(2, b'B' * 4096)

    # 读取
    got = pool.get_page(1)
    assert got == page_data
    print(f"  ✅ 页写入/读取正确")

    # 统计
    stats = pool.stats()
    print(f"  → {stats}")
    assert stats['used_slots'] == 2

    pool.close()
    print(f"  ✅ Buffer Pool 正常关闭")


def test_txn_coordinator():
    """测试6: 事务协调器"""
    print("\n【测试6】事务协调器")
    from lightdb.cluster.txn_coordinator import (
        TxnCoordinator, SharedTxnCoordinator)

    # 单进程版本
    coord = TxnCoordinator()
    t1 = coord.begin(1)
    t2 = coord.begin(2)
    assert t1 < t2
    print(f"  → 分配: {t1}, {t2}")

    coord.commit(t1)
    coord.rollback(t2)

    stats = coord.get_stats()
    print(f"  → 统计: {stats}")
    assert stats['total_begun'] == 2
    assert stats['total_committed'] == 1
    assert stats['total_rolled_back'] == 1
    print(f"  ✅ 单进程协调器正确")


def test_lock_coordinator():
    """测试7: 锁协调器"""
    print("\n【测试7】锁协调器")
    from lightdb.cluster.lock_coordinator import LockCoordinator

    lock = LockCoordinator(timeout=0.5)

    # txn 1 获取 X
    ok = lock.acquire(1, ('users', 100), 'X')
    assert ok
    print(f"  → Txn 1 获取 X 锁")

    # txn 2 尝试 X（应超时）
    t0 = time.time()
    ok = lock.acquire(2, ('users', 100), 'X')
    elapsed = time.time() - t0
    assert not ok
    print(f"  → Txn 2 等待超时 ({elapsed:.2f}s)")

    # txn 1 释放
    lock.release_all(1)
    print(f"  → Txn 1 释放")

    # txn 2 再获取
    ok = lock.acquire(2, ('users', 100), 'X')
    assert ok
    print(f"  → Txn 2 获取成功")

    stats = lock.stats_report()
    print(f"  → 统计: {stats}")


def test_stats_hub():
    """测试8: 统计中心"""
    print("\n【测试8】统计中心")
    from lightdb.cluster.stats_hub import StatsHub

    # StatsHub 用 SharedMemoryManager + 进程间原语，先探测环境能力
    _conftest.require_mp_primitives()

    hub = StatsHub(name='test_stats', worker_count=4,
                    create=True)

    # 模拟 4 个 Worker 上报
    for wid in range(4):
        for _ in range(10):
            hub.report(wid, 'queries')
        hub.report(wid, 'connections', 2)

    agg = hub.aggregate()
    print(f"  → 总计: {agg['totals']}")

    assert agg['totals']['queries'] == 40
    assert agg['totals']['connections'] == 8
    print(f"  ✅ 聚合正确")

    hub.close()


def test_multi_process_parallelism():
    """测试9: 多进程真并行验证"""
    print("\n【测试9】多进程真并行")
    import multiprocessing as mp
    import time

    # 该用例用 mp.Pool，先探测环境能力（探测失败即 skip）
    _conftest.require_mp_primitives()

    # 单进程跑 4 次
    t0 = time.perf_counter()
    for _ in range(4):
        _cpu_work(500000)
    single_time = time.perf_counter() - t0
    print(f"  → 单进程 4 次: {single_time*1000:.1f}ms")

    # 4 进程并行
    t0 = time.perf_counter()
    with mp.Pool(4) as pool:
        pool.map(_cpu_work, [500000] * 4)
    multi_time = time.perf_counter() - t0
    print(f"  → 4 进程并行: {multi_time*1000:.1f}ms")

    speedup = single_time / multi_time if multi_time > 0 else 0
    print(f"  → 加速: {speedup:.2f}x")

    if speedup >= 2.0:
        print(f"  ✅ 多进程有效利用多核")
    else:
        print(f"  ⚠️ 加速有限（可能核数不足）")


def test_config_v4():
    """测试10: v4 配置"""
    print("\n【测试10】v4 配置")

    config = {
        'host': '0.0.0.0',
        'port': 8888,
        'workers': 8,
        'buffer_pool_size': 1024,
        'data_dir': 'data',
    }

    # 保存
    import tempfile, json
    with tempfile.NamedTemporaryFile('w', suffix='.json',
                                      delete=False) as f:
        json.dump(config, f)
        path = f.name

    # 读取
    with open(path) as f:
        loaded = json.load(f)

    assert loaded['workers'] == 8
    print(f"  ✅ 配置: {loaded}")

    os.unlink(path)


def main():
    print("=" * 60)
    print("  v4.0 Batch 2 测试 - 多进程架构")
    print("=" * 60)

    tests = [
        ('SO_REUSEPORT 检测', test_has_reuseport),
        ('SO_REUSEPORT 监听器', test_reuseport_listener),
        ('共享内存', test_shared_memory),
        ('Worker 池', test_worker_pool),
        ('共享 Buffer Pool', test_shared_buffer_pool),
        ('事务协调器', test_txn_coordinator),
        ('锁协调器', test_lock_coordinator),
        ('统计中心', test_stats_hub),
        ('多进程真并行', test_multi_process_parallelism),
        ('v4 配置', test_config_v4),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 v4.0 Batch 2 全部通过！")
        print("  📊 多进程架构就绪")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())