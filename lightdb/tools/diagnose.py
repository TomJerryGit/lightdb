"""
lightdb-diagnose - 一键性能诊断
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def find_lightdb_pid() -> int:
    """查找 LightDB 进程"""
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', 'lightdb.*main.py'],
            stderr=subprocess.DEVNULL)
        pids = out.decode().strip().split('\n')
        return int(pids[0]) if pids and pids[0] else 0
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(
        prog='lightdb-diagnose',
        description='LightDB 性能诊断工具')
    ap.add_argument('--mode', default='all',
                    choices=['all', 'hotspot', 'gil', 'route'])
    ap.add_argument('--duration', type=int, default=30)
    ap.add_argument('--output-dir', default=None)
    ap.add_argument('--skip-experiments', action='store_true',
                    help='跳过多进程/Cython 实验（耗时）')
    args = ap.parse_args()

    output_dir = Path(args.output_dir or
                       Path.home() / '.lightdb_studio' / 'diagnosis')
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  LightDB v4.0 性能诊断")
    print("=" * 70)
    print(f"  输出: {output_dir}")
    print()

    # 1. 环境检查
    print("【1/5】环境检查")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  CPU 核数: {os.cpu_count()}")

    has_pyspy = False
    try:
        subprocess.run(['py-spy', '--version'],
                        capture_output=True, timeout=2)
        has_pyspy = True
        print(f"  py-spy: [OK]")
    except Exception:
        print(f"  py-spy: [FAIL] (建议安装: pip install py-spy)")

    has_cython = False
    try:
        import Cython
        has_cython = True
        print(f"  Cython: [OK] {Cython.__version__}")
    except ImportError:
        print(f"  Cython: [FAIL] (建议安装: pip install cython)")

    # 2. 采集 CPU Profile
    print("\n【2/5】采集 CPU Profile")

    pid = find_lightdb_pid()
    if pid and has_pyspy:
        from lightdb.profiling.flamegraph import FlameGraphGenerator
        gen = FlameGraphGenerator(str(output_dir))
        svg = gen.record_pid(pid, duration=args.duration)
        if svg:
            print(f"  [OK] 火焰图: {svg}")
    else:
        print(f"  [WARN] 跳过（PID={pid}, py-spy={has_pyspy}）")

    # 3. 分析热点
    print("\n【3/5】热点分析")
    from lightdb.profiling.cprofile_analyzer import CProfileAnalyzer
    from lightdb.profiling.hotspot_ranker import HotspotRanker

    # 用本地 CPU 密集函数测试（实际使用应替换为 LightDB 负载）
    def dummy_workload(duration):
        from lightdb.storage.btree import BTree
        from lightdb.storage.buffer_pool import BufferPool
        import tempfile
        from pathlib import Path

        tmp = tempfile.mkdtemp()
        bp = BufferPool(str(Path(tmp) / 'test.db'), capacity=100)
        bt = BTree(bp)

        # 插入
        for i in range(1000):
            bt.insert(i, i % 100 + 1, i % 100)
        # 查询
        stop = time.time() + duration
        while time.time() < stop:
            for i in range(0, 1000, 10):
                bt.search(i)

    analyzer = CProfileAnalyzer()
    prof_file = str(output_dir / 'profile.prof')
    try:
        analyzer.profile_workload(
            dummy_workload, duration=args.duration,
            output=prof_file)
        print(f"  [OK] Profile: {prof_file}")

        # 热点排行
        ranker = HotspotRanker(
            analyzer.functions, analyzer.total_time)
        hotspot_report = ranker.format_report(top_n=30)
        print(hotspot_report[:3000])   # 前 3K 字符

        # 保存
        report_file = output_dir / 'hotspot_report.txt'
        report_file.write_text(hotspot_report, encoding='utf-8')
        print(f"\n  [OK] 报告: {report_file}")

    except Exception as e:
        print(f"  [WARN] 分析失败: {e}")

    # 4. GIL 分析
    if args.mode in ('all', 'gil'):
        print("\n【4/5】GIL 影响分析")
        from lightdb.profiling.gil_analyzer import GILAnalyzer

        analyzer = GILAnalyzer()
        try:
            gil_result = analyzer.measure_gil(
                'cpu', max_workers=min(8, os.cpu_count() or 4),
                duration=3)
            gil_file = output_dir / 'gil_analysis.json'
            gil_file.write_text(
                json.dumps(gil_result, indent=2, default=str),
                encoding='utf-8')
            print(f"\n  [OK] GIL 报告: {gil_file}")
        except Exception as e:
            print(f"  [WARN] 失败: {e}")

    # 5. 技术路线验证
    if not args.skip_experiments and args.mode in ('all', 'route'):
        print("\n【5/5】技术路线验证")
        from lightdb.experiments.comparison import RouteComparison

        cmp = RouteComparison()
        cmp.run_experiments()
        report = cmp.format_report()
        print(report)

        route_file = output_dir / 'route_comparison.txt'
        route_file.write_text(report, encoding='utf-8')
        print(f"\n  [OK] 路线报告: {route_file}")

        route_json = output_dir / 'route_comparison.json'
        route_json.write_text(
            json.dumps(cmp.to_dict(), indent=2, default=str),
            encoding='utf-8')

    print("\n" + "=" * 70)
    print("  [OK] 诊断完成")
    print(f"  📁 结果目录: {output_dir}")
    print("=" * 70)


if __name__ == '__main__':
    main()