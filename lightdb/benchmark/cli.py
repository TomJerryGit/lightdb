"""
lightdb-bench - 性能基准测试工具
用法:
  python -m lightdb.benchmark.cli run --workload point_select
  python -m lightdb.benchmark.cli compare --baseline v3.0.base
  python -m lightdb.benchmark.cli profile --workload oltp_rw
  python -m lightdb.benchmark.cli memory --duration 60
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def main():
    ap = argparse.ArgumentParser(
        prog='lightdb-bench',
        description='LightDB 性能基准测试')
    sub = ap.add_subparsers(dest='cmd', required=True)

    # run
    p = sub.add_parser('run', help='运行基准测试')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8888)
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='')
    p.add_argument('--workload', default='all',
                   choices=['all', 'point_select', 'range_scan',
                            'insert', 'update', 'delete',
                            'oltp_rw', 'oltp_ro', 'join'])
    p.add_argument('--tables', type=int, default=4)
    p.add_argument('--rows', type=int, default=100000)
    p.add_argument('--threads', type=int, default=8)
    p.add_argument('--duration', type=int, default=30,
                   help='测试时长（秒）')
    p.add_argument('--warmup', type=int, default=5)
    p.add_argument('--label', default='', help='打标签，如 v3.0.run1')

    # compare
    p = sub.add_parser('compare', help='与基线对比')
    p.add_argument('--baseline', required=True)
    p.add_argument('--current', default='latest')

    # profile
    p = sub.add_parser('profile', help='CPU Profiling')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8888)
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='')
    p.add_argument('--workload', default='oltp_rw')
    p.add_argument('--duration', type=int, default=10)
    p.add_argument('--output', default='profile.out')
    p.add_argument('--format', default='pstats',
                   choices=['pstats', 'flamegraph'])

    # memory
    p = sub.add_parser('memory', help='内存分析')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8888)
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='')
    p.add_argument('--duration', type=int, default=60)

    # list
    sub.add_parser('list', help='列出可用工作负载')

    # baseline 管理
    p = sub.add_parser('baselines', help='列出所有基线')
    p = sub.add_parser('baseline-save', help='保存当前基线')
    p.add_argument('--name', required=True)

    args = ap.parse_args()

    from .runner import BenchmarkRunner
    from .baseline import BaselineStore

    if args.cmd == 'run':
        runner = BenchmarkRunner(
            host=args.host, port=args.port,
            user=args.user, password=args.password)
        result = runner.run(
            workload=args.workload,
            tables=args.tables,
            rows=args.rows,
            threads=args.threads,
            duration=args.duration,
            warmup=args.warmup,
        )
        store = BaselineStore()
        name = args.label or f"run_{result['run_id']}"
        store.save(name, result)
        print(f"\n✅ 结果已保存为基线: {name}")

        from .report import generate_report
        report_path = generate_report(result, name)
        print(f"📄 报告: {report_path}")

    elif args.cmd == 'compare':
        store = BaselineStore()
        cmp = store.compare(args.baseline, args.current)
        if cmp:
            print(store.format_compare(cmp))

    elif args.cmd == 'profile':
        from .profiler import CPUProfiler
        profiler = CPUProfiler(
            host=args.host, port=args.port,
            user=args.user, password=args.password)
        result = profiler.profile(
            workload=args.workload,
            duration=args.duration,
            output=args.output,
            format=args.format)
        print(profiler.format_hotspots(result))

    elif args.cmd == 'memory':
        from .memory_analyzer import MemoryAnalyzer
        analyzer = MemoryAnalyzer(
            host=args.host, port=args.port,
            user=args.user, password=args.password)
        result = analyzer.analyze(duration=args.duration)
        print(analyzer.format_report(result))

    elif args.cmd == 'list':
        from .workloads import list_workloads
        list_workloads()

    elif args.cmd == 'baselines':
        store = BaselineStore()
        for b in store.list_all():
            print(f"  {b['name']:30s} {b['created_at_str']:20s} "
                  f"workload={b['workload']}")

    elif args.cmd == 'baseline-save':
        store = BaselineStore()
        latest = store.get_latest()
        if latest:
            store.save(args.name, latest['data'])
            print(f"✅ 已保存: {args.name}")


if __name__ == '__main__':
    main()