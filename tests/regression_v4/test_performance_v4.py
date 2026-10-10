"""
v4.0 性能对比
"""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


PERF_TARGETS = {
    'single_thread_point_select': 12000,   # QPS
    'multi_process_point_select': 80000,   # QPS
    'olap_scan_rows_per_sec': 3000000,     # rows/s
    'olap_aggregate_rows_per_sec': 20000000,
}


def test_cython_row_codec():
    """测试：Cython 行编解码性能"""
    print("\n【Cython 行编解码】")
    from lightdb.storage.row_format import BinaryRowFormat
    from lightdb.storage.row_format_v4 import BinaryRowFormatV4

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'value', 'type': 'INT'},
    ]

    py_fmt = BinaryRowFormat(cols)
    cy_fmt = BinaryRowFormatV4(cols)

    rows = [[i, f'user_{i}', i % 100] for i in range(10000)]

    # Python
    t0 = time.perf_counter()
    for r in rows:
        py_fmt.encode(r)
    t1 = time.perf_counter()
    py_time = t1 - t0

    # Cython
    t0 = time.perf_counter()
    for r in rows:
        cy_fmt.encode(r)
    t1 = time.perf_counter()
    cy_time = t1 - t0

    speedup = py_time / cy_time if cy_time > 0 else 0
    print(f"  → Python: {py_time*1000:.1f}ms")
    print(f"  → Cython: {cy_time*1000:.1f}ms")
    print(f"  → 加速: {speedup:.1f}x")

    assert speedup > 1.5, f"Cython 加速不足: {speedup:.1f}x"
    print(f"  ✅ Cython 加速达标")


def test_olap_scan_performance():
    """测试：OLAP 扫描性能"""
    print("\n【OLAP 扫描性能】")
    from lightdb.columnar.column_store import ColumnStore

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'value', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)

        N = 1000000
        print(f"  → 准备 {N:,} 行...")
        batch = [[i] for i in range(N)]
        store.append_rows(batch)

        # 扫描
        t0 = time.perf_counter()
        data = store.scan(['value'])
        t1 = time.perf_counter()

        elapsed = t1 - t0
        rows_per_sec = N / elapsed if elapsed > 0 else 0
        print(f"  → 扫描: {elapsed*1000:.1f}ms "
              f"({rows_per_sec:,.0f} rows/s)")

        assert rows_per_sec > PERF_TARGETS['olap_scan_rows_per_sec']
        print(f"  ✅ 达到目标 ({PERF_TARGETS['olap_scan_rows_per_sec']:,})")


def test_olap_aggregate_performance():
    """测试：OLAP 聚合性能"""
    print("\n【OLAP 聚合性能】")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedAggregate)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'value', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)

        N = 1000000
        batch = [[i] for i in range(N)]
        store.append_rows(batch)

        # 向量化 SUM
        scan = VectorizedScan(store, ['value'])
        agg = VectorizedAggregate(scan, 'value', 'SUM')

        t0 = time.perf_counter()
        total = agg.execute()
        t1 = time.perf_counter()

        elapsed = t1 - t0
        rows_per_sec = N / elapsed if elapsed > 0 else 0
        print(f"  → SUM: {elapsed*1000:.1f}ms "
              f"({rows_per_sec:,.0f} rows/s)")

        assert rows_per_sec > PERF_TARGETS['olap_aggregate_rows_per_sec']
        print(f"  ✅ 达到目标 "
              f"({PERF_TARGETS['olap_aggregate_rows_per_sec']:,})")


def test_parallel_speedup():
    """测试：并行加速比"""
    print("\n【并行加速比】")
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
            # 串行
            t0 = time.perf_counter()
            data = store.scan(['value'])
            values = [v for v in data['value'] if v is not None]
            _ = sum(values)
            t1 = time.perf_counter()
            serial = t1 - t0

            # 并行
            t0 = time.perf_counter()
            _ = ex.parallel_aggregate(store, 'value', 'SUM')
            t1 = time.perf_counter()
            parallel = t1 - t0

            speedup = serial / parallel if parallel > 0 else 0
            print(f"  → 串行: {serial*1000:.0f}ms")
            print(f"  → 并行: {parallel*1000:.0f}ms")
            print(f"  → 加速: {speedup:.2f}x")

            assert speedup > 1.5, f"并行加速不足: {speedup:.2f}x"
            print(f"  ✅ 并行加速达标")
        finally:
            ex.shutdown()


def test_compression_ratio():
    """测试：压缩比"""
    print("\n【列压缩比】")
    from lightdb.columnar.column_store import ColumnStore

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'status', 'type': 'VARCHAR'},
        ]
        store = ColumnStore('t', cols, tmp)

        N = 100000
        cities = ['Beijing', 'Shanghai', 'Shenzhen']
        statuses = ['active', 'inactive', 'pending']

        rows = [
            [cities[i % 3], statuses[i % 3]]
            for i in range(N)
        ]
        store.append_rows(rows)

        report = store.stats_report()
        ratio = report['compression_ratio']
        print(f"  → 压缩比: {ratio:.2f}x")
        assert ratio > 3
        print(f"  ✅ 压缩比达标")


def main():
    print("=" * 60)
    print("  v4.0 性能对比")
    print("=" * 60)

    tests = [
        test_cython_row_codec,
        test_olap_scan_performance,
        test_olap_aggregate_performance,
        test_parallel_speedup,
        test_compression_ratio,
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