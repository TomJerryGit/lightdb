"""
内存泄漏深度检测
- 长时监控
- 分代分析
- 对象追踪
"""
import gc
import json
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class LeakReport:
    duration_s: float
    samples: int
    # 内存
    initial_mb: float = 0
    final_mb: float = 0
    peak_mb: float = 0
    growth_mb: float = 0
    growth_per_hour: float = 0
    # 判定
    status: str = "unknown"
    suspected_objects: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class MemoryLeakDetector:
    """内存泄漏检测器"""

    def __init__(self):
        self.samples: List[Dict] = []
        self._stop = threading.Event()

    # ==================== 采样 ====================

    def sample_now(self) -> Dict:
        """采样当前进程内存"""
        result = {
            'ts': time.time(),
            'rss_mb': 0,
            'obj_counts': {},
            'gc_stats': {},
        }

        # RSS
        try:
            import psutil
            import os
            proc = psutil.Process(os.getpid())
            result['rss_mb'] = proc.memory_info().rss / 1024 / 1024
        except Exception:
            pass

        # 对象统计（Top 30）
        try:
            gc.collect()
            type_counts = Counter()
            for obj in gc.get_objects():
                try:
                    t = type(obj).__name__
                    type_counts[t] += 1
                except Exception:
                    pass
            result['obj_counts'] = dict(type_counts.most_common(30))
        except Exception:
            pass

        # GC 统计
        try:
            stats = gc.get_stats()
            result['gc_stats'] = [
                {'collections': s.get('collections', 0),
                 'collected': s.get('collected', 0),
                 'uncollectable': s.get('uncollectable', 0)}
                for s in stats
            ]
        except Exception:
            pass

        return result

    # ==================== 监控 ====================

    def monitor(self, duration_s: int = 3600,
                interval_s: int = 30,
                on_sample: callable = None):
        """持续监控"""
        print(f"🔍 内存监控 {duration_s}s，采样间隔 {interval_s}s")

        start = time.time()
        last_log = 0

        try:
            while time.time() - start < duration_s:
                s = self.sample_now()
                self.samples.append(s)

                if on_sample:
                    on_sample(s)

                # 每 5 分钟打印
                if time.time() - last_log >= 300:
                    self._log_status(s)
                    last_log = time.time()

                time.sleep(interval_s)
        except KeyboardInterrupt:
            print("\n⚠️ 中断")

    def _log_status(self, s: Dict):
        elapsed = s['ts'] - self.samples[0]['ts']
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        print(f"  [{h:02d}h{m:02d}m] RSS={s['rss_mb']:7.1f}MB")

    # ==================== 分析 ====================

    def analyze(self) -> LeakReport:
        """分析结果"""
        if len(self.samples) < 2:
            return LeakReport(
                duration_s=0, samples=len(self.samples),
                status='insufficient_data')

        first = self.samples[0]
        last = self.samples[-1]
        duration = last['ts'] - first['ts']

        report = LeakReport(
            duration_s=duration,
            samples=len(self.samples),
            initial_mb=first['rss_mb'],
            final_mb=last['rss_mb'],
            peak_mb=max(s['rss_mb'] for s in self.samples),
        )
        report.growth_mb = report.final_mb - report.initial_mb
        hours = duration / 3600
        report.growth_per_hour = (
            report.growth_mb / hours if hours > 0 else 0)

        # 判定
        if report.growth_per_hour > 10:
            report.status = 'leak'
            report.notes.append(
                f"内存增长 {report.growth_per_hour:.2f} MB/h，疑似泄漏")
        elif report.growth_per_hour > 3:
            report.status = 'watch'
            report.notes.append(
                f"内存增长 {report.growth_per_hour:.2f} MB/h，需观察")
        else:
            report.status = 'stable'
            report.notes.append("内存稳定")

        # 对象增长分析
        if len(self.samples) >= 2:
            first_objs = first.get('obj_counts', {})
            last_objs = last.get('obj_counts', {})
            growth = []
            for name, count in last_objs.items():
                base = first_objs.get(name, 0)
                if base > 0:
                    delta = count - base
                    if delta > 1000:
                        growth.append({
                            'type': name,
                            'delta': delta,
                            'initial': base,
                            'current': count,
                        })
            growth.sort(key=lambda x: -x['delta'])
            report.suspected_objects = growth[:10]

            if growth and report.status == 'leak':
                report.notes.append(
                    f"可疑对象: "
                    f"{', '.join(g['type'] for g in growth[:5])}")

        return report

    # ==================== 报告 ====================

    def format_report(self, report: LeakReport) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("  内存泄漏检测报告")
        lines.append("=" * 60)
        lines.append(f"  时长: {report.duration_s/3600:.2f}h")
        lines.append(f"  采样: {report.samples}")
        lines.append(f"  初始: {report.initial_mb:.1f} MB")
        lines.append(f"  最终: {report.final_mb:.1f} MB")
        lines.append(f"  峰值: {report.peak_mb:.1f} MB")
        lines.append(f"  增长: {report.growth_mb:+.1f} MB")
        lines.append(f"  速率: {report.growth_per_hour:+.3f} MB/h")
        lines.append("")

        icon = {'stable': '✅', 'watch': '⚠️',
                'leak': '🔴'}.get(report.status, '❓')
        lines.append(f"  结论: {icon} {report.status}")
        for n in report.notes:
            lines.append(f"  - {n}")

        if report.suspected_objects:
            lines.append("")
            lines.append("  可疑对象类型:")
            for o in report.suspected_objects[:10]:
                lines.append(
                    f"    {o['type']:30s} "
                    f"{o['initial']:>10} → {o['current']:>10} "
                    f"({o['delta']:+,})")

        lines.append("=" * 60)
        return '\n'.join(lines)

    def save(self, path: str):
        report = self.analyze()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'report': asdict(report),
                'samples': self.samples,
            }, f, ensure_ascii=False, indent=2, default=str)