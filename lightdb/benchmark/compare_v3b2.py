"""
v3.0 Batch 2 性能对比
- 对比优化前后
- 生成改进报告
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from lightdb.benchmark.runner import BenchmarkRunner
from lightdb.benchmark.baseline import BaselineStore
from lightdb.benchmark.report import generate_report


def main():
    print("=" * 70)
    print("  v3.0 Batch 2 性能对比")
    print("=" * 70)

    # 1. 跑当前版本
    print("\n[1/3] 运行优化后基准...")
    runner = BenchmarkRunner()
    result = runner.run(
        workload='all',
        tables=4, rows=100000,
        threads=8, duration=30, warmup=5)

    # 2. 保存
    store = BaselineStore()
    store.save('v3.0.batch2', result)
    print(f"✅ 已保存基线: v3.0.batch2")

    # 3. 与 v3.0.batch1 对比
    print("\n[2/3] 对比 batch1...")
    cmp = store.compare('v3.0.baseline', 'v3.0.batch2')
    if cmp:
        print(store.format_compare(cmp))
        report = store.format_compare(cmp)
    else:
        print("⚠️ 未找到 batch1 基线")

    # 4. 生成报告
    print("\n[3/3] 生成报告...")
    path = generate_report(result, 'v3.0.batch2')
    print(f"📄 报告: {path}")


if __name__ == '__main__':
    main()