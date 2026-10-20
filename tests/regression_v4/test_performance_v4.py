"""
v4.0 性能对比
"""
import os
import statistics
import sys
import time
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


PERF_TARGETS = {
    'single_thread_point_select': 12000,   # QPS（历史白皮书目标，不在本文件断言）
    'multi_process_point_select': 80000,   # QPS（历史白皮书目标，不在本文件断言）
    # 4A.5 门禁化：阈值 = 2026-10-20 本机实测基线 × 安全余量（不是设计目标）。
    # 实测：1M 行单聚合 SUM 中位 367ms = 2.72M rows/s（样本 2.40–2.78M）。
    # 取 2.0M rows/s（低于实测中位 ~26%）用于抓"数量级回退"，机器漂移见
    # benchmarks/gate.py 的 tolerance 口径。
    'olap_scan_rows_per_sec': 3000000,     # 1M 行列扫描实测 ~50M+ rows/s；门禁抓回退
    'olap_aggregate_rows_per_sec': 2000000,
    # 4A.3 实测 100k 行 GROUP BY：w1 中位 2.75s → w2 中位 1.00s = 2.75x；
    # 取 1.2x（低于实测 ~56%）只抓"并行完全没有收益/负加速"。
    'olap_parallel_speedup': 1.2,
}


def test_cython_row_codec():
    """测试：Cython 行编解码性能"""
    print("\n【Cython 行编解码】")
    # 没有编译好的扩展时 BinaryRowFormatV4 走纯 Python 回退，
    # 拿 Python 和 Python 比出来的 ratio 没有意义（原先 `_cy is None`
    # 让本用例成了假通过）。只有存在真正加速的编译版时断言才成立。
    from lightdb.cython import status as _cython_status
    if not _cython_status().get('any_accelerated'):
        print("  ⏭ 未编译 Cython 扩展（当前为纯 Python 回退），跳过加速断言")
        return
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
    """测试：OLAP 聚合性能（4A.5 门禁化：阈值来自实测基线，见 PERF_TARGETS）"""
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


@pytest.mark.skipif(
    (os.cpu_count() or 1) < 4,
    reason="并行加速断言需 ≥4 核验证（本机 8 核实测 GROUP BY w1→w2 = 2.75x；"
           "2 核 CI 环境跳过，结果仍由单线程一致性测试覆盖）")
def test_parallel_speedup():
    """测试：并行加速比（4A.3 分片线程池；100k 行 GROUP BY w1 vs w2）"""
    print("\n【并行加速比（向量化 GROUP BY 分片）】")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedGroupBy)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'city', 'type': 'VARCHAR'},
                {'name': 'amount', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)
        N = 100_000
        store.append_rows([[f'c{i % 100}', i] for i in range(N)])
        aggs = [{'func': 'SUM', 'column': 'amount', 'alias': 'total'},
                {'func': 'COUNT', 'column': '*', 'alias': 'cnt'}]

        def run(workers):
            gb = VectorizedGroupBy(
                VectorizedScan(store, ['city', 'amount']),
                ['city'], aggs, workers=workers)
            t0 = time.perf_counter()
            rows = gb.execute()
            return rows, time.perf_counter() - t0

        # 先逐值校验：并行不得改变结果（含顺序是否稳定由实现保证）
        r1, _ = run(1)
        r2, _ = run(2)
        assert r1 == r2, '单线程/2 线程结果必须逐值一致'

        serial_samples = []
        parallel_samples = []
        for _ in range(2):
            _, dt = run(1)
            serial_samples.append(dt)
            _, dt = run(2)
            parallel_samples.append(dt)
        serial = statistics.median(serial_samples)
        parallel = statistics.median(parallel_samples)
        speedup = serial / parallel if parallel > 0 else 0
        print(f"  → 串行: {serial*1000:.0f}ms")
        print(f"  → 并行(2 线程): {parallel*1000:.0f}ms")
        print(f"  → 加速: {speedup:.2f}x")
        assert speedup > PERF_TARGETS['olap_parallel_speedup'], (
            f"并行加速不足: {speedup:.2f}x <= "
            f"{PERF_TARGETS['olap_parallel_speedup']}x")
        print(f"  ✅ 并行加速达标（> {PERF_TARGETS['olap_parallel_speedup']}x，"
              f"基线 2.75x）")


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