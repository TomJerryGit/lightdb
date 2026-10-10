"""
性能报告生成器
- 从 cProfile / py-spy 生成 Markdown 报告
"""
import time
from pathlib import Path
from typing import Dict, List


class ProfilingReporter:

    def __init__(self, output_dir: str = None):
        self.output_dir = Path(output_dir or
                                Path.home() / '.lightdb_studio' / 'profiles')
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def report_cprofile(self, analyzer, label: str = '') -> str:
        """从 CProfileAnalyzer 生成报告"""
        lines = [
            f"# 性能分析报告 - {label}",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"总耗时: {analyzer.total_time:.2f}s",
            "",
            "## Top 20 热点函数",
            "",
            "| # | tottime | ncalls | function | file |",
            "|---|---------|--------|----------|------|",
        ]
        for i, f in enumerate(analyzer.top_by_tottime(20), 1):
            lines.append(
                f"| {i} | {f.tottime:.3f} | {f.ncalls} | "
                f"`{f.name}` | {f.file.split('/')[-1]} |")

        # 模块分布
        lines.append("")
        lines.append("## 按模块耗时")
        lines.append("")
        for mod, info in list(analyzer.by_module().items())[:15]:
            lines.append(
                f"- `{mod}`: {info['tottime']:.2f}s "
                f"({info['pct']:.1f}%)")

        path = self.output_dir / f"report_{label or 'latest'}.md"
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return str(path)

    def report_hotspots(self, hotspots: List, label: str = '') -> str:
        """从热点排行生成报告"""
        lines = [
            f"# 热点排行 - {label}",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "| # | 耗时 | 占比 | 优化方式 | 收益 | 函数 |",
            "|---|------|------|----------|------|------|",
        ]
        for h in hotspots:
            lines.append(
                f"| {h.rank} | {h.tottime:.3f} | {h.pct:.1f}% | "
                f"{h.optimize_via} | {h.est_gain_pct}% | "
                f"`{h.name[:40]}` |")

        path = self.output_dir / f"hotspots_{label or 'latest'}.md"
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return str(path)