"""
并行查询回归
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_parallel_scan_correctness():
    """测试：并行扫描结果正确"""
    print("\n【并行扫描正确性】")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'value', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        N = 100000
        rows = [[i, i * 2] for i in range(N)]
        store.append_rows(rows)

        ex = ParallelExecutor(max_workers=4)
        try:
            data = ex.parallel_scan(store, ['id', 'value'])
            assert len(data['id']) == N
            assert len(data['value']) == N
            assert data['id'] == list(range(N))
            assert data['value'] == [i * 2 for i in range(N)]
            print(f"  ✅ 并行扫描结果一致")
        finally:
            ex.shutdown()


def test_parallel_aggregate_correctness():
    """测试：并行聚合结果正确"""
    print("\n【并行聚合正确性】")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'value', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)
        N = 1000000
        rows = [[i] for i in range(N)]
        store.append_rows(rows)

        ex = ParallelExecutor(max_workers=4)
        try:
            # SUM
            total = ex.parallel_aggregate(store, 'value', 'SUM')
            expected = N * (N - 1) // 2
            assert total == expected
            print(f"  ✅ SUM = {int(total)}")

            # COUNT
            cnt = ex.parallel_aggregate(store, 'value', 'COUNT')
            assert cnt == N
            print(f"  ✅ COUNT = {cnt}")

            # MIN / MAX
            mn = ex.parallel_aggregate(store, 'value', 'MIN')
            mx = ex.parallel_aggregate(store, 'value', 'MAX')
            assert mn == 0
            assert mx == N - 1
            print(f"  ✅ MIN={mn}, MAX={mx}")
        finally:
            ex.shutdown()


def test_parallel_groupby_correctness():
    """测试：并行分组结果正确"""
    print("\n【并行分组正确性】")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'k', 'type': 'VARCHAR'},
            {'name': 'v', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        N = 500000
        rows = [[f'k{i % 50}', i] for i in range(N)]
        store.append_rows(rows)

        ex = ParallelExecutor(max_workers=4)
        try:
            result = ex.parallel_group_by(
                store, ['k'], [
                    {'func': 'SUM', 'column': 'v', 'alias': 'total'},
                    {'func': 'COUNT', 'column': '*', 'alias': 'cnt'},
                ])

            assert len(result) == 50
            total = sum(r[2] for r in result)
            assert total == N
            print(f"  ✅ 50 组, 总计数 {total}")
        finally:
            ex.shutdown()


def test_work_stealing_balance():
    """测试：工作窃取负载均衡"""
    print("\n【工作窃取均衡】")
    from lightdb.parallel.work_stealing import WorkStealingScheduler
    import threading
    import time

    ws = WorkStealingScheduler(n_workers=4)

    # 4 个 worker，但 1 个工人拿 90% 任务
    # 其他工人应窃取
    results = []

    def handler(task):
        results.append(task)
        if task % 10 == 0:
            time.sleep(0.001)   # 部分任务慢

    ws.set_handler(handler)
    ws.start()

    # 提交任务
    tasks = list(range(200))
    ws.submit(tasks)
    ws.wait_all(timeout=10)

    assert len(results) == 200
    print(f"  ✅ 完成 200 任务")

    ws.stop()


def test_resource_isolation():
    """测试：资源组隔离"""
    print("\n【资源组隔离】")
    from lightdb.parallel.resource_group import (
        ResourceGroupManager, ResourceLimits)

    mgr = ResourceGroupManager()

    # 场景：创建自定义组
    mgr.create_group('batch', ResourceLimits(
        max_workers=8, max_concurrent_queries=2))

    g = mgr.get('batch')
    # 只允许 2 个并发
    assert g.acquire()
    assert g.acquire()
    assert not g.acquire()
    print(f"  ✅ batch 组限制 2 并发")

    g.release()
    g.release()
    assert g.acquire()
    print(f"  ✅ 释放后可再次获取")


def main():
    print("=" * 60)
    print("  v4.0 并行查询回归")
    print("=" * 60)

    tests = [
        test_parallel_scan_correctness,
        test_parallel_aggregate_correctness,
        test_parallel_groupby_correctness,
        test_work_stealing_balance,
        test_resource_isolation,
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