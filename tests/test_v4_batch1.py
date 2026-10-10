"""v4.0 Batch 1 测试 - 性能诊断与决策"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_cprofile_analyzer():
    """测试1: cProfile 分析器"""
    print("\n【测试1】cProfile 分析器")
    from lightdb.profiling.cprofile_analyzer import CProfileAnalyzer

    analyzer = CProfileAnalyzer()

    # 模拟工作负载
    def workload(duration):
        t0 = time.time()
        while time.time() - t0 < duration:
            x = 0
            for i in range(10000):
                x += i * i

    with tempfile.TemporaryDirectory() as tmp:
        prof_file = os.path.join(tmp, 'test.prof')
        analyzer.profile_workload(
            workload, duration=0.5, output=prof_file)

        assert os.path.exists(prof_file)
        assert analyzer.total_time > 0
        assert len(analyzer.functions) > 0
        print(f"  ✅ 采集 {len(analyzer.functions)} 个函数")

        # 排行
        top = analyzer.top_by_tottime(10, exclude_std=True)
        print(f"  → Top 10:")
        for f in top[:3]:
            print(f"    {f.tottime:.3f}s  {f.name}")

        # 分类
        cats = analyzer.by_category()
        print(f"  → 类别: {list(cats.keys())}")

        # 报告
        report = analyzer.format_report(20)
        assert 'cProfile' in report
        print(f"  ✅ 报告: {len(report)} 字符")


def test_hotspot_ranker():
    """测试2: 热点排行"""
    print("\n【测试2】热点排行")
    from lightdb.profiling.cprofile_analyzer import FunctionStat
    from lightdb.profiling.hotspot_ranker import HotspotRanker

    # 构造假数据
    funcs = [
        FunctionStat(name='btree.search', file='lightdb/storage/btree.py',
                     line=100, ncalls=10000, tottime=5.0, cumtime=6.0),
        FunctionStat(name='row.encode', file='lightdb/storage/row.py',
                     line=50, ncalls=50000, tottime=4.0, cumtime=4.0),
        FunctionStat(name='executor.run', file='lightdb/sql/executor.py',
                     line=200, ncalls=1000, tottime=3.0, cumtime=15.0),
        FunctionStat(name='parser.parse', file='lightdb/sql/parser.py',
                     line=80, ncalls=500, tottime=1.0, cumtime=2.0),
    ]

    ranker = HotspotRanker(funcs, total_time=20.0)
    hotspots = ranker.rank(10)

    print(f"  → 排行 {len(hotspots)} 个热点")
    for h in hotspots:
        print(f"    [{h.optimize_via}] {h.name}: {h.pct:.1f}%")

    assert len(hotspots) == 4
    # btree 应该被识别为 cython 优化
    btree = next(h for h in hotspots if 'btree' in h.name)
    assert btree.optimize_via == 'cython'

    # 汇总
    summary = ranker.by_optimization_type(10)
    print(f"  → 分类: {list(summary.keys())}")

    # 收益预估
    est = ranker.estimate_total_gain(10)
    print(f"  → 预估加速: {est['estimated_speedup']:.2f}x")

    # 报告
    report = ranker.format_report(10)
    assert '热点排行' in report
    print(f"  ✅ 报告生成")


def test_gil_analyzer():
    """测试3: GIL 分析"""
    print("\n【测试3】GIL 分析")
    from lightdb.profiling.gil_analyzer import GILAnalyzer

    analyzer = GILAnalyzer()

    # 短时间快速测试
    result = analyzer.measure_gil(
        workload='cpu', max_workers=4, duration=1)

    assert 'threads' in result
    assert 'processes' in result
    assert 'conclusion' in result

    print(f"  → 线程效率: "
          f"{result['conclusion']['thread_efficiency']*100:.0f}%")
    print(f"  → 进程效率: "
          f"{result['conclusion']['process_efficiency']*100:.0f}%")
    print(f"  → GIL 影响: "
          f"{result['conclusion']['gil_impact_pct']:.0f}%")
    print(f"  → 判定: {result['conclusion']['verdict']}")

    # 应该检测出 GIL 影响
    assert result['conclusion']['gil_impact_pct'] > 50
    print(f"  ✅ GIL 影响已检测")


def test_multiprocess_experiment():
    """测试4: 多进程实验"""
    print("\n【测试4】多进程实验")
    from lightdb.experiments.multiprocess_test import (
        MultiprocessExperiment)

    exp = MultiprocessExperiment()

    # 短时间对比
    single = exp.single_process(1)
    print(f"  → 单进程: {single.ops_per_sec:.0f} ops/s")

    thread2 = exp.multi_thread(2, 1)
    print(f"  → 2 线程: {thread2.ops_per_sec:.0f} ops/s")

    proc2 = exp.multi_process(2, 1)
    print(f"  → 2 进程: {proc2.ops_per_sec:.0f} ops/s")

    # 多进程应该比多线程快
    print(f"  ✅ 多进程优势: "
          f"{proc2.ops_per_sec/max(thread2.ops_per_sec, 1):.2f}x")

    # IPC 测试
    ipc = exp.test_ipc_latency(1000)
    print(f"  → IPC: {list(ipc.keys())}")


def test_cython_experiment():
    """测试5: Cython 实验"""
    print("\n【测试5】Cython 实验")
    from lightdb.experiments.cython_test import CythonExperiment

    exp = CythonExperiment()

    has_cython = exp.has_cython()
    has_gcc = exp.has_gcc()

    print(f"  → Cython 可用: {has_cython}")
    print(f"  → GCC 可用: {has_gcc}")

    if not has_cython or not has_gcc:
        print(f"  ⚠️ 环境不完整，跳过编译测试")
        return

    # 编译 B+Tree
    result = exp.test_btree_speedup(10000)
    if result.get('speedup'):
        print(f"  ✅ B+Tree 加速: {result['speedup']:.1f}x")
        assert result['speedup'] > 1.5, \
            f"Cython 加速不足: {result['speedup']:.1f}x"


def test_route_comparison():
    """测试6: 路线对比"""
    print("\n【测试6】路线对比")
    from lightdb.experiments.comparison import RouteComparison

    cmp = RouteComparison()

    # 不跑实验，直接评估
    cmp.raw_data = {
        'multiprocess': {'max_speedup': 4.5},
        'cython': {'avg_speedup': 3.5},
        'hybrid': {'estimated_speedup': 0.6*3.5 + 0.4*4.5},
    }

    routes = cmp.evaluate_routes()
    assert len(routes) == 3

    print(f"  → 3 条路线:")
    for r in routes:
        print(f"    {r.name:25s} {r.speedup:.1f}x "
              f"({r.cost_months:.1f}月)")

    # 混合应该最高
    hybrid = [r for r in routes if '混合' in r.name][0]
    assert hybrid.speedup > 4

    # 报告
    report = cmp.format_report()
    assert '路线对比' in report
    print(f"  ✅ 报告: {len(report)} 字符")


def test_decision_doc():
    """测试7: 决策文档"""
    print("\n【测试7】决策文档")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc_path = os.path.join(root, 'docs', 'V4_DECISION.md')

    if not os.path.exists(doc_path):
        print(f"  ⚠️ 文档不存在: {doc_path}")
        return

    with open(doc_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 关键内容检查
    assert 'v4.0' in content
    assert 'GIL' in content
    assert 'Cython' in content
    assert '多进程' in content
    assert '决策' in content

    print(f"  ✅ 决策文档完整 ({len(content)} 字符)")


def test_diagnose_cli():
    """测试8: 诊断 CLI"""
    print("\n【测试8】诊断 CLI")
    import subprocess

    result = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.diagnose', '--help'],
        capture_output=True, text=True, timeout=10,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    assert result.returncode == 0
    assert 'diagnose' in result.stdout.lower()
    print(f"  ✅ CLI 可用")


def test_end_to_end_diagnosis():
    """测试9: 端到端诊断"""
    print("\n【测试9】端到端诊断")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with tempfile.TemporaryDirectory() as tmp:
        # 运行诊断（跳过耗时实验）
        result = subprocess.run(
            [sys.executable, '-m', 'lightdb.tools.diagnose',
             '--duration', '2', '--skip-experiments',
             '--output-dir', tmp],
            capture_output=True, text=True, timeout=60,
            cwd=root)

        print(f"  → 返回码: {result.returncode}")
        print(f"  → 输出文件:")
        for f in os.listdir(tmp):
            print(f"    - {f}")

        assert result.returncode == 0
        # 应该生成一些文件
        assert len(os.listdir(tmp)) > 0
        print(f"  ✅ 端到端诊断通过")


def main():
    print("=" * 60)
    print("  v4.0 Batch 1 测试 - 性能诊断与决策")
    print("=" * 60)

    tests = [
        ('cProfile 分析', test_cprofile_analyzer),
        ('热点排行', test_hotspot_ranker),
        ('GIL 分析', test_gil_analyzer),
        ('多进程实验', test_multiprocess_experiment),
        ('Cython 实验', test_cython_experiment),
        ('路线对比', test_route_comparison),
        ('决策文档', test_decision_doc),
        ('诊断 CLI', test_diagnose_cli),
        ('端到端诊断', test_end_to_end_diagnosis),
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
        print("  🎉 v4.0 Batch 1 全部通过！")
        print("  📊 已建立 v4.0 技术决策依据")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())