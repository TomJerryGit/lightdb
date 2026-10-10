"""
基准测试报告生成
"""
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict


REPORT_DIR = Path.home() / '.lightdb_studio' / 'reports'
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def generate_report(result: Dict, label: str) -> str:
    """生成 Markdown 报告"""
    meta = result.get('metadata', {})
    results = result.get('results', {})

    lines = [
        f"# LightDB 性能基准报告",
        f"",
        f"**报告标识**: `{label}`",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**目标主机**: {meta.get('host')}:{meta.get('port')}",
        f"",
        f"---",
        f"",
        f"## 测试参数",
        f"",
        f"| 参数 | 值 |",
        f"|------|----|",
        f"| 表数量 | {meta.get('tables')} |",
        f"| 每表行数 | {meta.get('rows'):,} |",
        f"| 并发线程 | {meta.get('threads')} |",
        f"| 测试时长 | {meta.get('duration')}s |",
        f"| 预热时长 | {meta.get('warmup')}s |",
        f"",
        f"---",
        f"",
        f"## 性能结果",
        f"",
        f"| 工作负载 | QPS | 平均延迟 | P50 | P95 | P99 | P999 |",
        f"|---------|-----|---------|-----|-----|-----|------|",
    ]

    for wname, w in sorted(results.items()):
        if 'error' in w:
            lines.append(f"| {wname} | ❌ 失败 | - | - | - | - | - |")
            continue
        lat = w.get('latency_ms', {})
        lines.append(
            f"| {wname} | "
            f"{w.get('qps', 0):.1f} | "
            f"{lat.get('avg', 0):.3f}ms | "
            f"{lat.get('p50', 0):.3f}ms | "
            f"{lat.get('p95', 0):.3f}ms | "
            f"{lat.get('p99', 0):.3f}ms | "
            f"{lat.get('p999', 0):.3f}ms |")

    lines.extend([
        "",
        "---",
        "",
        "## QPS 可视化",
        "",
        "```",
    ])

    # ASCII 柱状图
    qps_list = [(k, v.get('qps', 0))
                for k, v in results.items() if 'error' not in v]
    if qps_list:
        max_qps = max(v for _, v in qps_list) if qps_list else 1
        for name, qps in qps_list:
            bar_len = int(qps / max_qps * 40) if max_qps > 0 else 0
            bar = '█' * bar_len
            lines.append(f"{name:20s} {bar} {qps:>10.1f}")

    lines.extend([
        "```",
        "",
        "---",
        "",
        "## 详细数据",
        "",
    ])

    for wname, w in sorted(results.items()):
        if 'error' in w:
            continue
        lines.append(f"### {wname}")
        lines.append(f"")
        lines.append(f"- 总查询数: {w.get('queries', 0):,}")
        lines.append(f"- 错误数: {w.get('errors', 0)}")
        lines.append(f"- 吞吐: **{w.get('qps', 0):.1f} QPS**")
        if w.get('tps'):
            lines.append(f"- TPS: {w['tps']:.1f}")
        lines.append("")

    lines.extend([
        "---",
        "",
        f"**总耗时**: {meta.get('total_elapsed_s', 0):.1f}s",
        "",
        "**报告结束**",
    ])

    # 写文件
    filename = f"benchmark_{label}_{int(time.time())}.md"
    path = REPORT_DIR / filename
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return str(path)