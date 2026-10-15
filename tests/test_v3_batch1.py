"""v3.0 Batch 1 测试 - 性能诊断与基准框架"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_workloads_registry():
    """测试1: 工作负载注册"""
    print("\n【测试1】工作负载注册")
    from lightdb.benchmark.workloads import (
        WORKLOADS, get_workload, list_workloads)

    expected = ['point_select', 'range_scan', 'insert',
                'update', 'delete', 'oltp_ro', 'oltp_rw', 'join']
    for name in expected:
        assert name in WORKLOADS, f"缺少工作负载: {name}"
    print(f"  ✅ 已注册 {len(WORKLOADS)} 个负载")

    wl = get_workload('point_select')
    assert wl is not None
    assert wl.name == 'point_select'
    print(f"  ✅ get_workload 正确")

    print("  → 负载列表:")
    list_workloads()


def test_workload_result_stats():
    """测试2: 结果统计计算"""
    print("\n【测试2】结果统计")
    from lightdb.benchmark.workloads import WorkloadResult

    r = WorkloadResult(name='test')
    r.latencies_ms = [float(i) for i in range(1, 1001)]
    r.queries = 1000
    r.compute_stats(10.0)

    print(f"  → QPS: {r.qps}")
    print(f"  → P50: {r.p50}")
    print(f"  → P95: {r.p95}")
    print(f"  → P99: {r.p99}")
    print(f"  → 平均: {r.avg_lat:.2f}")

    assert r.qps == 100.0
    assert r.min_lat == 1.0
    assert r.max_lat == 1000.0
    assert r.p50 == 500.0 or r.p50 == 501.0
    assert r.p99 >= 990
    print("  ✅ 统计正确")


def test_data_generator():
    """测试3: 数据生成"""
    print("\n【测试3】数据生成")
    from lightdb.benchmark.workloads import generate_rows

    rows = generate_rows(1, 100)
    assert len(rows) == 100
    assert rows[0][0] == 1
    assert rows[-1][0] == 100
    for r in rows:
        assert len(r) == 4
        assert isinstance(r[0], int)
        assert isinstance(r[1], int)
        assert len(r[2]) == 120
        assert len(r[3]) == 60
    print(f"  ✅ 生成 100 行，格式正确")


def test_baseline_store():
    """测试4: 基线存储与对比"""
    print("\n【测试4】基线存储与对比")
    import lightdb.benchmark.baseline as bl

    tmp = tempfile.mkdtemp()
    old_dir = bl.BASELINE_DIR
    bl.BASELINE_DIR = Path(tmp) / 'baselines'

    try:
        store = bl.BaselineStore()

        # 保存基线 A
        result_a = {
            'metadata': {'run_id': 'a'},
            'results': {
                'point_select': {'qps': 5000, 'latency_ms': {'p99': 5}},
                'insert': {'qps': 3000, 'latency_ms': {'p99': 10}},
            }
        }
        store.save('v3.0.base', result_a)
        print(f"  ✅ 保存基线 v3.0.base")

        # 保存基线 B
        result_b = {
            'metadata': {'run_id': 'b'},
            'results': {
                'point_select': {'qps': 5500, 'latency_ms': {'p99': 4}},
                'insert': {'qps': 2800, 'latency_ms': {'p99': 11}},
            }
        }
        store.save('latest', result_b)

        # 列表
        all_baselines = store.list_all()
        assert len(all_baselines) == 1
        print(f"  → 基线数: {len(all_baselines)}")

        # 对比
        cmp = store.compare('v3.0.base', 'latest')
        assert cmp is not None
        assert 'point_select' in cmp['workloads']

        w = cmp['workloads']['point_select']
        print(f"  → point_select:")
        print(f"    基线 QPS: {w['baseline_qps']}")
        print(f"    当前 QPS: {w['current_qps']}")
        print(f"    变化: {w['qps_delta_pct']:+.2f}%")
        assert w['qps_delta_pct'] == 10.0   # (5500-5000)/5000

        insert_w = cmp['workloads']['insert']
        print(f"  → insert 变化: {insert_w['qps_delta_pct']:+.2f}%")
        assert insert_w['qps_delta_pct'] < 0

        # 格式化
        text = store.format_compare(cmp)
        print(f"  → 对比文本 {len(text)} 字符")
        assert 'v3.0.base' in text

    finally:
        bl.BASELINE_DIR = old_dir


def test_report_generation():
    """测试5: 报告生成"""
    print("\n【测试5】报告生成")
    import lightdb.benchmark.report as rp

    tmp = tempfile.mkdtemp()
    old_dir = rp.REPORT_DIR
    rp.REPORT_DIR = Path(tmp)

    try:
        result = {
            'metadata': {
                'run_id': 'test',
                'host': '127.0.0.1',
                'port': 8888,
                'tables': 4,
                'rows': 100000,
                'threads': 8,
                'duration': 30,
                'warmup': 5,
                'total_elapsed_s': 180.5,
            },
            'results': {
                'point_select': {
                    'qps': 5500, 'queries': 165000, 'errors': 0,
                    'latency_ms': {
                        'avg': 1.5, 'p50': 1.2,
                        'p95': 3.0, 'p99': 4.5, 'p999': 8.0,
                    },
                },
                'oltp_rw': {
                    'qps': 3500, 'queries': 105000, 'errors': 0,
                    'latency_ms': {
                        'avg': 2.5, 'p50': 2.0,
                        'p95': 5.0, 'p99': 8.0, 'p999': 15.0,
                    },
                },
            },
        }

        path = rp.generate_report(result, 'test_run')
        assert Path(path).exists()
        print(f"  ✅ 报告生成: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'LightDB 性能基准报告' in content
        assert 'point_select' in content
        assert '5500' in content
        assert '█' in content   # ASCII 柱状图
        print(f"  ✅ 报告内容完整 ({len(content)} 字符)")

    finally:
        rp.REPORT_DIR = old_dir


def test_profiler_structure():
    """测试6: Profiler 结构"""
    print("\n【测试6】Profiler 结构")
    from lightdb.benchmark.profiler import CPUProfiler

    p = CPUProfiler(host='127.0.0.1', port=8888)
    assert p.host == '127.0.0.1'
    assert p.port == 8888
    print("  ✅ Profiler 初始化")

    # 测试格式化
    result = {
        'hotspots': [
            {'ncalls': '1000', 'tottime': 5.2,
             'cumtime': 10.5, 'function': 'btree.search'},
            {'ncalls': '500', 'tottime': 3.1,
             'cumtime': 6.0, 'function': 'page.read'},
        ]
    }
    text = p.format_hotspots(result)
    assert 'btree.search' in text
    assert '1000' in text
    print("  ✅ 热点格式化正确")


def test_memory_analyzer_structure():
    """测试7: 内存分析器结构"""
    print("\n【测试7】内存分析器")
    from lightdb.benchmark.memory_analyzer import MemoryAnalyzer

    m = MemoryAnalyzer()

    # 构造假样本
    samples = [
        {'elapsed_s': 0, 'rss_mb': 100.0, 'connections': 5,
         'queries': 0, 'timestamp': 0},
        {'elapsed_s': 30, 'rss_mb': 101.0, 'connections': 5,
         'queries': 1000, 'timestamp': 30},
        {'elapsed_s': 60, 'rss_mb': 101.5, 'connections': 5,
         'queries': 2000, 'timestamp': 60},
    ]
    result = m._analyze(samples, 60)
    print(f"  → 增长: {result['rss_growth_mb']:.1f} MB")
    print(f"  → 每分钟: {result['growth_per_min_mb']:.3f} MB/min")
    print(f"  → 状态: {result['status']}")
    # 修正（E 类 #27）：样本 100.0→101.0→101.5MB/60s，最小二乘斜率
    # = 1.5 MB/min，而引擎状态梯子（lightdb/benchmark/memory_analyzer.py:176-181）
    # 是 `>5 → leak_suspected`、`>1 → watch`、否则 `stable`。
    # 原断言 `'stable'` 与引擎既有（且设计合理的）判据矛盾：+1.5MB/min
    # ≈ 2.16GB/天，判 watch 正是该阈值的用途；错的是断言，不是引擎。
    # 新断言仍钉住数值本身，阈值被改动/回归会立即失败（未放水）。
    assert abs(result['growth_per_min_mb'] - 1.5) < 1e-6
    assert result['status'] == 'watch'

    # 平缓样本（+0.04MB/min）必须仍是 stable：保住梯子低端的覆盖
    samples_flat = [
        {'elapsed_s': 0, 'rss_mb': 100.0, 'connections': 5,
         'queries': 0, 'timestamp': 0},
        {'elapsed_s': 30, 'rss_mb': 100.02, 'connections': 5,
         'queries': 1000, 'timestamp': 30},
        {'elapsed_s': 60, 'rss_mb': 100.04, 'connections': 5,
         'queries': 2000, 'timestamp': 60},
    ]
    flat = m._analyze(samples_flat, 60)
    print(f"  → 平缓样本: {flat['growth_per_min_mb']:.3f} MB/min "
          f"→ {flat['status']}")
    assert flat['status'] == 'stable'

    # 构造疑似泄漏
    samples2 = [
        {'elapsed_s': i * 10, 'rss_mb': 100 + i * 10,
         'connections': 5, 'queries': 0, 'timestamp': i * 10}
        for i in range(7)
    ]
    result2 = m._analyze(samples2, 60)
    print(f"  → 泄漏样本: {result2['growth_per_min_mb']:.1f} MB/min")
    assert result2['status'] == 'leak_suspected'

    # 报告
    text = m.format_report(result)
    assert '内存分析报告' in text
    print("  ✅ 报告格式化正确")


def test_full_pipeline_dry_run():
    """测试8: 完整流程（模拟）"""
    print("\n【测试8】完整流程模拟")

    # 模拟一次运行结果
    from lightdb.benchmark.workloads import WorkloadResult
    from lightdb.benchmark.baseline import BaselineStore
    import lightdb.benchmark.baseline as bl
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    old = bl.BASELINE_DIR
    bl.BASELINE_DIR = Path(tmp) / 'baselines'

    try:
        # 构造运行结果
        results = {}
        for wname in ['point_select', 'oltp_rw']:
            r = WorkloadResult(name=wname)
            r.latencies_ms = [1.0] * 100
            r.queries = 100
            r.compute_stats(1.0)
            results[wname] = r.to_dict()

        full_result = {
            'metadata': {
                'run_id': 'dry_run',
                'host': '127.0.0.1',
                'port': 8888,
                'tables': 4, 'rows': 100000,
                'threads': 8, 'duration': 30, 'warmup': 5,
                'total_elapsed_s': 100,
            },
            'results': results,
        }

        # 保存
        store = BaselineStore()
        store.save('v3.0.dry_run', full_result)
        print(f"  ✅ 保存基线")

        # 生成报告
        from lightdb.benchmark.report import generate_report
        path = generate_report(full_result, 'dry_run')
        assert Path(path).exists()
        print(f"  ✅ 报告: {Path(path).name}")

        print("  ✅ 完整流程无异常")

    finally:
        bl.BASELINE_DIR = old


def main():
    print("=" * 60)
    print("  v3.0 Batch 1 测试 - 性能诊断与基准框架")
    print("=" * 60)

    tests = [
        ('工作负载注册', test_workloads_registry),
        ('结果统计', test_workload_result_stats),
        ('数据生成', test_data_generator),
        ('基线存储对比', test_baseline_store),
        ('报告生成', test_report_generation),
        ('Profiler', test_profiler_structure),
        ('内存分析', test_memory_analyzer_structure),
        ('完整流程', test_full_pipeline_dry_run),
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
        print("  🎉 v3.0 Batch 1 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())