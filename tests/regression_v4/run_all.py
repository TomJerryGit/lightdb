"""
LightDB v4.0 全量回归测试
24 个套件 + 6 个 v4 专项
"""
import os
import sys
import json
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List


REGRESSION_DIR = Path(__file__).parent
TESTS_DIR = REGRESSION_DIR.parent


class V4RegressionRunner:
    """v4.0 全量回归"""

    SUITES = [
        # ============ v1-v3 回归（关键保留）============
        ('v1-B1 MySQL协议', 'test_batch1.py', 60, True),
        ('v1-B2 CRUD', 'test_batch2.py', 60, True),
        ('v1-B3 存储引擎', 'test_batch3.py', 120, True),
        ('v1-B4 事务', 'test_batch4.py', 120, True),
        ('v1-B5 优化器', 'test_batch5.py', 90, True),
        ('v1-B6 JOIN', 'test_batch6.py', 90, True),
        ('v1-B7 复杂查询', 'test_batch7.py', 90, True),
        ('v1-B8 复制', 'test_batch8.py', 180, False),
        ('v1-B9 安全', 'test_batch9.py', 120, True),
        ('v1-B10 性能', 'test_batch10.py', 120, False),
        ('v1-B11 窗口函数', 'test_batch11.py', 90, True),
        ('v1-B12 存储过程', 'test_batch12.py', 120, True),
        ('v1-B13 JSON/全文', 'test_batch13.py', 90, True),
        ('v1-B14 半同步/SSL', 'test_batch14.py', 120, False),
        ('v1-B15 生产就绪', 'test_batch15.py', 90, True),
        ('v3-B1 性能诊断', 'test_v3_batch1.py', 60, True),
        ('v3-B2 核心优化', 'test_v3_batch2.py', 90, True),
        ('v3-B3 稳定性', 'test_v3_batch3.py', 60, True),
        ('v3-B4 功能补齐', 'test_v3_batch4.py', 90, True),
        ('v3-B5 运维监控', 'test_v3_batch5.py', 60, True),
        ('v3-B6 发布', 'test_v3_batch6.py', 60, True),
        # ============ v4.0 专项 ============
        ('v4-B1 性能诊断', 'test_v4_batch1.py', 90, True),
        ('v4-B2 多进程', 'test_v4_batch2.py', 120, True),
        ('v4-B3 Cython', 'test_v4_batch3.py', 120, True),
        ('v4-B4 列式OLAP', 'test_v4_batch4.py', 120, True),
        ('v4-B5 并行', 'test_v4_batch5.py', 180, True),
        # ============ v4.0 兼容性回归 ============
        ('v4-Cython兼容', 'regression_v4/test_cython_compat.py',
          120, True),
        ('v4-多进程回归', 'regression_v4/test_multiprocess.py',
          180, True),
        ('v4-列式回归', 'regression_v4/test_columnar.py',
          120, True),
        ('v4-并行回归', 'regression_v4/test_parallel.py',
          180, True),
        ('v4-性能对比', 'regression_v4/test_performance_v4.py',
          240, True),
    ]

    def __init__(self):
        self.results = []
        self.start_time = 0
        self.end_time = 0

    def run(self) -> Dict:
        self.start_time = time.time()
        print("=" * 75)
        print("  LightDB v4.0 全量回归测试")
        print("=" * 75)
        print(f"  开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  套件: {len(self.SUITES)}")
        print("=" * 75)
        print()

        for i, (name, path, timeout, critical) in enumerate(
                self.SUITES, 1):
            print(f"[{i:2d}/{len(self.SUITES)}] {name}")
            r = self._run_suite(path, timeout)
            r.update({'name': name, 'critical': critical})
            self.results.append(r)

            status = '✅' if r['passed'] else ('❌' if critical else '⚠️')
            print(f"          {status} {r['elapsed']:.2f}s "
                  f"(P={r['passed_count']}, F={r['failed_count']})")

            if not r['passed'] and critical:
                print(f"          ❌ 关键测试失败，中止")
                break

        self.end_time = time.time()
        summary = self._summary()
        self._print_summary(summary)
        self._save(summary)
        return summary

    def _run_suite(self, path: str, timeout: int) -> Dict:
        full_path = TESTS_DIR / path
        if not full_path.exists():
            return {
                'passed': False, 'elapsed': 0,
                'passed_count': 0, 'failed_count': 0,
                'output': 'NOT_FOUND',
                'error': f'文件不存在: {path}',
            }

        t0 = time.time()
        try:
            result = subprocess.run(
                [sys.executable, str(full_path)],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(TESTS_DIR))
            elapsed = time.time() - t0
            passed = result.returncode == 0
            output = result.stdout
            return {
                'passed': passed,
                'elapsed': elapsed,
                'passed_count': output.count('✅'),
                'failed_count': output.count('❌'),
                'output': output[-500:],
                'error': result.stderr[-500:] if not passed else '',
            }
        except subprocess.TimeoutExpired:
            return {
                'passed': False, 'elapsed': timeout,
                'passed_count': 0, 'failed_count': 0,
                'output': '', 'error': f'超时({timeout}s)',
            }
        except Exception as e:
            return {
                'passed': False, 'elapsed': time.time() - t0,
                'passed_count': 0, 'failed_count': 0,
                'output': '', 'error': str(e),
            }

    def _summary(self) -> Dict:
        total = len(self.results)
        passed = sum(1 for r in self.results if r['passed'])
        critical_failed = sum(
            1 for r in self.results
            if not r['passed'] and r['critical'])

        return {
            'version': 'v4.0.0',
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_s': self.end_time - self.start_time,
            'total_suites': total,
            'passed_suites': passed,
            'failed_suites': total - passed,
            'critical_failed': critical_failed,
            'total_passed': sum(r['passed_count'] for r in self.results),
            'total_failed': sum(r['failed_count'] for r in self.results),
            'overall': 'PASS' if critical_failed == 0 else 'FAIL',
            'results': self.results,
        }

    def _print_summary(self, s: Dict):
        print()
        print("=" * 75)
        print("  v4.0 回归测试汇总")
        print("=" * 75)
        print(f"  时长:     {s['duration_s']:.1f}s")
        print(f"  套件:     {s['passed_suites']}/{s['total_suites']} 通过")
        print(f"  断言:     {s['total_passed']} passed, "
              f"{s['total_failed']} failed")

        if s['overall'] == 'PASS':
            print()
            print("  ╔═══════════════════════════════════════════════╗")
            print("  ║                                               ║")
            print("  ║       🎉 v4.0.0 全量回归通过！ 🎉             ║")
            print("  ║                                               ║")
            print("  ║       Production Ready — v4.0.0               ║")
            print("  ║                                               ║")
            print("  ╚═══════════════════════════════════════════════╝")
        else:
            print(f"\n  ❌ {s['critical_failed']} 个关键套件失败")
        print("=" * 75)

    def _save(self, s: Dict):
        out = REGRESSION_DIR / 'v4_regression_report.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2,
                      default=str)
        print(f"\n  报告: {out}")


def main():
    runner = V4RegressionRunner()
    summary = runner.run()
    sys.exit(0 if summary['overall'] == 'PASS' else 1)


if __name__ == '__main__':
    main()