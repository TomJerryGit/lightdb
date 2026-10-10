"""
v3.0 全量回归测试
一键运行所有测试套件
"""
import os
import sys
import time
import json
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


REGRESSION_DIR = Path(__file__).parent
TESTS_DIR = REGRESSION_DIR.parent


class RegressionRunner:
    """回归测试运行器"""

    # 测试套件清单（按阶段）
    SUITES = [
        # (名称, 路径, 超时秒, 关键)
        ('Batch 1 - MySQL 协议', 'test_batch1.py', 60, True),
        ('Batch 2 - CRUD', 'test_batch2.py', 60, True),
        ('Batch 3 - 存储引擎', 'test_batch3.py', 120, True),
        ('Batch 4 - 事务', 'test_batch4.py', 120, True),
        ('Batch 5 - 优化器', 'test_batch5.py', 90, True),
        ('Batch 6 - JOIN', 'test_batch6.py', 90, True),
        ('Batch 7 - 复杂查询', 'test_batch7.py', 90, True),
        ('Batch 8 - 复制', 'test_batch8.py', 180, False),
        ('Batch 9 - 安全', 'test_batch9.py', 120, True),
        ('Batch 10 - 性能', 'test_batch10.py', 120, False),
        ('Batch 11 - 窗口函数', 'test_batch11.py', 90, True),
        ('Batch 12 - 存储过程', 'test_batch12.py', 120, True),
        ('Batch 13 - JSON/全文', 'test_batch13.py', 90, True),
        ('Batch 14 - 半同步/SSL', 'test_batch14.py', 120, False),
        ('Batch 15 - 生产就绪', 'test_batch15.py', 90, True),
        ('v3 Batch 1 - 性能诊断', 'test_v3_batch1.py', 60, True),
        ('v3 Batch 2 - 核心优化', 'test_v3_batch2.py', 90, True),
        ('v3 Batch 3 - 稳定性', 'test_v3_batch3.py', 60, True),
        ('v3 Batch 4 - 功能补齐', 'test_v3_batch4.py', 90, True),
        ('v3 Batch 5 - 运维监控', 'test_v3_batch5.py', 60, True),
        # 回归专项
        ('回归 - SQL 兼容', 'regression/test_sql_compat.py', 120, True),
        ('回归 - 事务', 'regression/test_transaction.py', 120, True),
        ('回归 - 复制', 'regression/test_replication.py', 240, False),
        ('回归 - 性能', 'regression/test_performance.py', 180, True),
    ]

    def __init__(self):
        self.results: List[Dict] = []
        self.start_time = 0
        self.end_time = 0

    def run_all(self, quick: bool = False) -> Dict:
        """运行全部测试"""
        self.start_time = time.time()

        print("=" * 70)
        print("  LightDB v3.0 全量回归测试")
        print("=" * 70)
        print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  测试套件: {len(self.SUITES)}")
        print("=" * 70)
        print()

        for i, (name, path, timeout, critical) in enumerate(
                self.SUITES, 1):
            print(f"[{i:2d}/{len(self.SUITES)}] {name}")
            r = self._run_suite(path, timeout)
            r['name'] = name
            r['critical'] = critical
            self.results.append(r)

            status = '✅' if r['passed'] else ('❌' if critical else '⚠️')
            print(f"          {status} {r['elapsed']:.2f}s "
                  f"(passed={r['passed_count']}, "
                  f"failed={r['failed_count']})")

            if not r['passed'] and critical:
                print(f"          ❌ 关键测试失败，中止")
                break

        self.end_time = time.time()
        summary = self._generate_summary()
        self._print_summary(summary)
        self._save_report(summary)

        return summary

    def _run_suite(self, path: str, timeout: int) -> Dict:
        """运行单个套件"""
        full_path = TESTS_DIR / path
        if not full_path.exists():
            return {
                'passed': False, 'elapsed': 0,
                'passed_count': 0, 'failed_count': 0,
                'output': 'NOT_FOUND',
                'error': f"文件不存在: {path}",
            }

        t0 = time.time()
        try:
            result = subprocess.run(
                [sys.executable, str(full_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(TESTS_DIR))
            elapsed = time.time() - t0

            passed = result.returncode == 0
            output = result.stdout

            # 解析 pass/fail 计数
            passed_count = output.count('✅')
            failed_count = output.count('❌')

            return {
                'passed': passed,
                'elapsed': elapsed,
                'passed_count': passed_count,
                'failed_count': failed_count,
                'output': output[-500:],   # 保留尾部
                'error': result.stderr[-500:] if not passed else '',
            }
        except subprocess.TimeoutExpired:
            return {
                'passed': False,
                'elapsed': timeout,
                'passed_count': 0, 'failed_count': 0,
                'output': '',
                'error': f'超时 ({timeout}s)',
            }
        except Exception as e:
            return {
                'passed': False,
                'elapsed': time.time() - t0,
                'passed_count': 0, 'failed_count': 0,
                'output': '',
                'error': str(e),
            }

    def _generate_summary(self) -> Dict:
        """生成汇总"""
        total = len(self.results)
        passed = sum(1 for r in self.results if r['passed'])
        failed = total - passed
        critical_failed = sum(
            1 for r in self.results
            if not r['passed'] and r['critical'])

        return {
            'version': 'v3.0.0',
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_s': self.end_time - self.start_time,
            'total_suites': total,
            'passed_suites': passed,
            'failed_suites': failed,
            'critical_failed': critical_failed,
            'total_passed': sum(r['passed_count'] for r in self.results),
            'total_failed': sum(r['failed_count'] for r in self.results),
            'overall': 'PASS' if critical_failed == 0 else 'FAIL',
            'results': self.results,
        }

    def _print_summary(self, summary: Dict):
        """打印汇总"""
        print()
        print("=" * 70)
        print("  回归测试汇总")
        print("=" * 70)
        print(f"  时长:     {summary['duration_s']:.1f}s")
        print(f"  套件:     {summary['passed_suites']}/"
              f"{summary['total_suites']} 通过")
        print(f"  断言:     {summary['total_passed']} passed, "
              f"{summary['total_failed']} failed")
        print()

        if summary['overall'] == 'PASS':
            print(f"  ✅ 全量回归通过！")
        else:
            print(f"  ❌ 回归失败，{summary['critical_failed']} "
                  f"个关键套件未通过")

        print("=" * 70)

    def _save_report(self, summary: Dict):
        """保存 JSON 报告"""
        out = REGRESSION_DIR / 'regression_report.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2,
                      default=str)
        print(f"\n  报告: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true',
                    help='快速模式（跳过非关键）')
    args = ap.parse_args()

    runner = RegressionRunner()
    summary = runner.run_all(quick=args.quick)

    sys.exit(0 if summary['overall'] == 'PASS' else 1)


if __name__ == '__main__':
    main()