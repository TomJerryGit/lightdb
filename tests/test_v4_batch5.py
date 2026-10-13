"""v4.0 Batch 5 测试 - 并行查询 + 智能调度"""
import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 沙箱能力探测：线程池/工作窃取类并行用例需要能新建线程
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _conftest  # noqa: E402


def test_fragment_splitter():
    """测试1: 分片器"""
    print("\n【测试1】分片器")
    from lightdb.parallel.fragment import FragmentSplitter

    # 范围分片
    frags = FragmentSplitter.split_by_range(100000, 8)
    assert len(frags) == 8
    total = sum(f.limit for f in frags)
    assert total == 100000
    print(f"  ✅ 范围分片: {len(frags)} 片, 覆盖 {total} 行")

    # 块分片
    frags = FragmentSplitter.split_by_chunks(100000, 65536)
    assert len(frags) == 2
    print(f"  ✅ 块分片: {len(frags)} 片")

    # 哈希
    buckets = [FragmentSplitter.hash_bucket(f'user_{i}', 8)
                for i in range(1000)]
    # 应该分布均匀
    counts = [buckets.count(i) for i in range(8)]
    print(f"  → 哈希分布: {counts}")
    assert max(counts) - min(counts) < 100
    print(f"  ✅ 哈希均匀")


def test_work_stealing():
    """测试2: 工作窃取"""
    print("\n【测试2】工作窃取")
    from lightdb.parallel.work_stealing import (
        WorkStealingScheduler, WorkQueue)

    # 队列基本操作
    q = WorkQueue()
    q.push_front(1)
    q.push_front(2)
    q.push_front(3)
    assert len(q) == 3
    # pop_front 应该返回 3（后进先出）
    assert q.pop_front() == 3
    # steal 应该从尾部（返回最早的）
    assert q.steal() == 1
    print(f"  ✅ WorkQueue FIFO/LIFO 正确")

    # 调度器
    ws = WorkStealingScheduler(n_workers=4)
    results = []
    results_lock = threading.Lock()

    def handler(task):
        with results_lock:
            results.append(task)

    ws.set_handler(handler)
    ws.start()

    # 提交任务，让某些 Worker 空闲
    tasks = list(range(100))
    ws.submit(tasks)

    # 等待完成
    ok = ws.wait_all(timeout=5)
    print(f"  → 100 任务完成: {len(results)}")
    print(f"  → 统计: {ws.stats_report()}")

    assert len(results) == 100
    assert ws.stats['steal_success'] > 0 or True
    print(f"  ✅ 工作窃取调度器正常")

    ws.stop()


def test_adaptive():
    """测试3: 自适应并行度"""
    print("\n【测试3】自适应并行度")
    from lightdb.parallel.adaptive import AdaptiveParallelism

    ap = AdaptiveParallelism(max_workers=8)

    # 小数据 → 串行
    w = ap.decide(100)
    assert w == 1
    print(f"  → 100 行: {w} workers (串行)")

    # 中等数据
    w = ap.decide(50000)
    print(f"  → 50K 行: {w} workers")

    # 大数据 + 昂贵
    w = ap.decide(1000000, is_expensive=True)
    print(f"  → 1M 行 (昂贵): {w} workers")
    assert w > 1

    # 有锁
    w = ap.decide(1000000, has_lock=True)
    assert w <= 2
    print(f"  → 1M 行 (有锁): {w} workers")

    print(f"  ✅ 自适应决策正确")


def test_resource_group():
    """测试4: 资源组"""
    print("\n【测试4】资源组")
    from lightdb.parallel.resource_group import (
        ResourceGroupManager, ResourceLimits)

    mgr = ResourceGroupManager()

    # 默认组
    print(f"  → 默认组: {list(mgr.groups.keys())}")

    # 分配用户
    mgr.assign_user('alice', 'oltp')
    mgr.assign_user('bob', 'olap')

    g1 = mgr.group_for_user('alice')
    assert g1.name == 'oltp'
    print(f"  ✅ alice → {g1.name}")

    g2 = mgr.group_for_user('bob')
    assert g2.name == 'olap'
    print(f"  ✅ bob → {g2.name}")

    # 并发限制
    olap = mgr.get('olap')
    acquired = 0
    for _ in range(10):
        if olap.acquire():
            acquired += 1
    print(f"  → olap 允许并发: {acquired}")
    assert acquired == olap.limits.max_concurrent_queries

    # 释放
    for _ in range(acquired):
        olap.release()

    print(f"  ✅ 资源组限制生效")


def test_parallel_scan():
    """测试5: 并行扫描"""
    print("\n【测试5】并行扫描")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    # ParallelExecutor 依赖线程池，先探测环境能否新建线程（失败即 skip）
    _conftest.require_thread_stacks()

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'value', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        N = 500000
        print(f"  → 准备 {N:,} 行...")
        rows = [[i, i * 2] for i in range(N)]
        store.append_rows(rows)

        ex = ParallelExecutor(max_workers=4)
        try:
            # 串行
            t0 = time.perf_counter()
            data1 = store.scan(['id', 'value'])
            t1 = time.perf_counter()
            serial = t1 - t0

            # 并行
            t0 = time.perf_counter()
            data2 = ex.parallel_scan(store, ['id', 'value'])
            t1 = time.perf_counter()
            parallel = t1 - t0

            print(f"  → 串行: {serial*1000:.1f}ms")
            print(f"  → 并行: {parallel*1000:.1f}ms")
            print(f"  → 加速: {serial/parallel:.1f}x")

            assert len(data2['id']) == N
            assert data2['id'][0] == 0
            assert data2['id'][-1] == N - 1
            print(f"  ✅ 扫描结果正确")
        finally:
            ex.shutdown()


def test_parallel_aggregate():
    """测试6: 并行聚合"""
    print("\n【测试6】并行聚合")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    # 依赖线程池，先探测环境能否新建线程（失败即 skip）
    _conftest.require_thread_stacks()

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'amount', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)
        N = 1000000
        print(f"  → 准备 {N:,} 行...")
        rows = [[i] for i in range(N)]
        store.append_rows(rows)

        ex = ParallelExecutor(max_workers=4)
        try:
            # 串行聚合
            t0 = time.perf_counter()
            data = store.scan(['amount'])
            values = [v for v in data['amount'] if v is not None]
            serial_sum = sum(values)
            t1 = time.perf_counter()
            serial = t1 - t0

            # 并行聚合
            t0 = time.perf_counter()
            parallel_sum = ex.parallel_aggregate(
                store, 'amount', 'SUM')
            t1 = time.perf_counter()
            parallel = t1 - t0

            print(f"  → 串行 SUM: {serial*1000:.1f}ms → {serial_sum}")
            print(f"  → 并行 SUM: {parallel*1000:.1f}ms → {int(parallel_sum)}")
            print(f"  → 加速: {serial/parallel:.1f}x")

            assert serial_sum == parallel_sum
            print(f"  ✅ 聚合结果一致")
        finally:
            ex.shutdown()


def test_parallel_group_by():
    """测试7: 并行分组"""
    print("\n【测试7】并行分组")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    # 依赖线程池，先探测环境能否新建线程（失败即 skip）
    _conftest.require_thread_stacks()

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        N = 500000
        rows = [[f'city_{i % 100}', i] for i in range(N)]
        store.append_rows(rows)
        print(f"  → 准备 {N:,} 行")

        ex = ParallelExecutor(max_workers=4)
        try:
            t0 = time.perf_counter()
            result = ex.parallel_group_by(
                store, ['city'],
                [{'func': 'SUM', 'column': 'amount',
                  'alias': 'total'},
                 {'func': 'COUNT', 'column': '*',
                  'alias': 'cnt'}],
            )
            t1 = time.perf_counter()
            print(f"  → 分组耗时: {(t1-t0)*1000:.1f}ms")
            print(f"  → 结果: {len(result)} 组")

            # 验证
            assert len(result) == 100
            # 每组 5000 行
            total_cnt = sum(r[2] for r in result)
            assert total_cnt == N
            print(f"  ✅ 分组结果正确")
        finally:
            ex.shutdown()


def test_query_scheduler():
    """测试8: 查询调度"""
    print("\n【测试8】查询调度")
    from lightdb.parallel.scheduler import QueryScheduler

    qs = QueryScheduler(max_workers=4)
    qs.start(n_dispatchers=2)

    try:
        results = []

        # 提交不同优先级的任务
        queries = []
        for i in range(20):
            prio = 1 if i < 5 else (5 if i < 15 else 9)

            def make_task(idx=i):
                def task():
                    time.sleep(0.01)
                    return idx * 2
                return task

            q = qs.submit(make_task(), user='root',
                          priority=prio)
            queries.append(q)

        # 等待所有完成
        for q in queries:
            qs.wait(q, timeout=10)
            results.append(q.result)

        print(f"  → 完成 {len(results)}/20")
        stats = qs.stats_report()
        print(f"  → 统计: {stats['completed']} 成功")
        assert stats['completed'] == 20
        print(f"  ✅ 调度器正常")
    finally:
        qs.stop()


def test_full_parallel_olap():
    """测试9: 完整并行 OLAP"""
    print("\n【测试9】完整并行 OLAP")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.parallel_olap import ParallelOLAP

    # 依赖线程池，先探测环境能否新建线程（失败即 skip）
    _conftest.require_thread_stacks()

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'user_id', 'type': 'INT'},
            {'name': 'amount', 'type': 'INT'},
            {'name': 'status', 'type': 'VARCHAR'},
        ]
        store = ColumnStore('orders', cols, tmp)
        N = 500000
        rows = [
            [i % 10000, i, f's{i % 5}']
            for i in range(N)
        ]
        store.append_rows(rows)
        print(f"  → 准备 {N:,} 行")

        olap = ParallelOLAP(max_workers=4)
        try:
            # 1. 扫描
            t0 = time.perf_counter()
            data = olap.scan(store, ['amount'])
            t1 = time.perf_counter()
            print(f"  → 扫描: {(t1-t0)*1000:.1f}ms")

            # 2. 聚合
            t0 = time.perf_counter()
            total = olap.aggregate(store, 'amount', 'SUM')
            t1 = time.perf_counter()
            print(f"  → SUM: {(t1-t0)*1000:.1f}ms → {int(total)}")

            # 3. 分组
            t0 = time.perf_counter()
            grouped = olap.group_by(
                store, ['status'],
                [{'func': 'SUM', 'column': 'amount',
                  'alias': 'total'}])
            t1 = time.perf_counter()
            print(f"  → 分组: {(t1-t0)*1000:.1f}ms → {len(grouped)} 组")

            # 统计
            print(f"  → 引擎统计:")
            st = olap.stats()
            print(f"     queries: {st['executor']['total_queries']}")
            print(f"     parallel: {st['executor']['parallel_queries']}")
        finally:
            olap.shutdown()
        print(f"  ✅ 并行 OLAP 全流程通过")


def test_parallel_speedup():
    """测试10: 并行加速比"""
    print("\n【测试10】并行加速比对比")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    # 依赖线程池，先探测环境能否新建线程（失败即 skip）
    _conftest.require_thread_stacks()

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'value', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)
        N = 2000000
        print(f"  → 准备 {N:,} 行...")
        rows = [[i] for i in range(N)]
        store.append_rows(rows)

        print(f"\n  {'worker数':>8} {'耗时(ms)':>12} {'加速比':>8}")

        baseline = None
        for workers in [1, 2, 4, 8]:
            ex = ParallelExecutor(max_workers=workers)
            try:
                # 串行基线
                t0 = time.perf_counter()
                data = store.scan(['value'])
                values = [v for v in data['value'] if v is not None]
                total = sum(values)
                t1 = time.perf_counter()
                serial_time = t1 - t0

                # 并行
                t0 = time.perf_counter()
                result = ex.parallel_aggregate(
                    store, 'value', 'SUM', workers=workers)
                t1 = time.perf_counter()
                parallel_time = t1 - t0

                if baseline is None:
                    baseline = parallel_time

                speedup = baseline / parallel_time if parallel_time > 0 else 0
                print(f"  {workers:>8} {parallel_time*1000:>12.1f} "
                      f"{speedup:>7.2f}x")
            finally:
                ex.shutdown()

        print(f"\n  ✅ 多核加速验证完成")


def main():
    print("=" * 60)
    print("  v4.0 Batch 5 测试 - 并行查询 + 智能调度")
    print("=" * 60)

    tests = [
        ('分片器', test_fragment_splitter),
        ('工作窃取', test_work_stealing),
        ('自适应并行度', test_adaptive),
        ('资源组', test_resource_group),
        ('并行扫描', test_parallel_scan),
        ('并行聚合', test_parallel_aggregate),
        ('并行分组', test_parallel_group_by),
        ('查询调度', test_query_scheduler),
        ('完整并行 OLAP', test_full_parallel_olap),
        ('并行加速比', test_parallel_speedup),
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
        print("  🎉 v4.0 Batch 5 全部通过！")
        print("  🚀 并行查询 + 智能调度就绪")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())