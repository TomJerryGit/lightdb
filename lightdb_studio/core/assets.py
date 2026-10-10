"""
数据资产报表
- 资产统计
- 使用排行
- 闲置表识别
- 数据增长率
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class AssetStats:
    """资产统计"""
    total_tables: int = 0
    total_columns: int = 0
    total_rows: int = 0
    total_size_estimate: int = 0  # 字节估算
    by_domain: Dict = field(default_factory=dict)
    by_sensitivity: Dict = field(default_factory=dict)
    by_connection: Dict = field(default_factory=dict)
    generated_at: float = 0

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = time.time()


class AssetAnalyzer:
    """资产分析器"""

    def __init__(self, catalog=None):
        self.catalog = catalog

    def stats(self) -> AssetStats:
        """整体资产统计"""
        stats = AssetStats()
        if not self.catalog:
            return stats

        stats.total_tables = len(self.catalog.tables)
        by_domain = defaultdict(lambda: {
            'tables': 0, 'rows': 0, 'cols': 0})
        by_sens = defaultdict(int)
        by_conn = defaultdict(int)

        for key, meta in self.catalog.tables.items():
            stats.total_columns += len(meta.columns)
            stats.total_rows += meta.row_count

            # 大小估算（每行 ~200 字节）
            stats.total_size_estimate += meta.row_count * 200

            by_domain[meta.domain]['tables'] += 1
            by_domain[meta.domain]['rows'] += meta.row_count
            by_domain[meta.domain]['cols'] += len(meta.columns)

            by_sens[meta.sensitivity] += 1
            by_conn[meta.connection] += 1

        stats.by_domain = dict(by_domain)
        stats.by_sensitivity = dict(by_sens)
        stats.by_connection = dict(by_conn)

        return stats

    def top_tables(self, by: str = 'query_count',
                   top_n: int = 20) -> List[Dict]:
        """使用排行"""
        if not self.catalog:
            return []

        data = []
        for key, meta in self.catalog.tables.items():
            if by == 'query_count':
                value = meta.query_count
            elif by == 'rows':
                value = meta.row_count
            elif by == 'columns':
                value = len(meta.columns)
            else:
                value = 0

            data.append({
                'key': key,
                'table': meta.name,
                'database': meta.database,
                'domain': meta.domain,
                'value': value,
                'last_queried': meta.last_queried,
            })

        return sorted(data, key=lambda x: -x['value'])[:top_n]

    def idle_tables(self, days: int = 30) -> List[Dict]:
        """闲置表"""
        if not self.catalog:
            return []

        cutoff = time.time() - days * 86400
        result = []

        for key, meta in self.catalog.tables.items():
            if meta.query_count == 0:
                result.append({
                    'key': key,
                    'table': meta.name,
                    'domain': meta.domain,
                    'rows': meta.row_count,
                    'reason': '从未查询',
                    'days_idle': None,
                })
            elif meta.last_queried > 0 and meta.last_queried < cutoff:
                days_idle = int((time.time() - meta.last_queried) / 86400)
                result.append({
                    'key': key,
                    'table': meta.name,
                    'domain': meta.domain,
                    'rows': meta.row_count,
                    'reason': f'最近一次使用 {days_idle} 天前',
                    'days_idle': days_idle,
                })

        return sorted(result, key=lambda x: -(x['days_idle'] or 9999))

    def table_growth_trend(self) -> List[Dict]:
        """数据增长率（需要多次采样）"""
        # 简化：基于当前快照
        return []

    def asset_report(self) -> str:
        """生成 Markdown 资产报告"""
        stats = self.stats()

        lines = [
            "# 数据资产报告",
            f"生成时间: "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats.generated_at))}",
            "",
            "## 概览",
            "",
            f"- 表总数: **{stats.total_tables}**",
            f"- 列总数: **{stats.total_columns}**",
            f"- 总行数: **{stats.total_rows:,}**",
            f"- 估算存储: **{self._fmt_size(stats.total_size_estimate)}**",
            "",
            "## 按业务域",
            "",
            "| 业务域 | 表数 | 行数 | 列数 |",
            "|--------|------|------|------|",
        ]

        for d, info in sorted(stats.by_domain.items(),
                              key=lambda x: -x[1]['rows']):
            lines.append(
                f"| {d} | {info['tables']} | "
                f"{info['rows']:,} | {info['cols']} |")

        lines.append("")
        lines.append("## 按敏感度")
        lines.append("")
        for sens, cnt in sorted(stats.by_sensitivity.items(),
                                 key=lambda x: -x[1]):
            lines.append(f"- {sens}: {cnt} 表")

        lines.append("")
        lines.append("## Top 10 使用最多")
        lines.append("")
        lines.append("| 表 | 查询次数 | 行数 |")
        lines.append("|----|---------|------|")
        for t in self.top_tables('query_count', 10):
            lines.append(
                f"| `{t['key']}` | {t['value']} | - |")

        lines.append("")
        lines.append("## Top 10 数据量")
        lines.append("")
        lines.append("| 表 | 行数 |")
        lines.append("|----|------|")
        for t in self.top_tables('rows', 10):
            lines.append(f"| `{t['key']}` | {t['value']:,} |")

        lines.append("")
        lines.append("## 闲置表（30 天）")
        lines.append("")
        idle = self.idle_tables(30)
        if idle:
            lines.append("| 表 | 行数 | 原因 |")
            lines.append("|----|------|------|")
            for t in idle[:20]:
                lines.append(
                    f"| `{t['key']}` | {t['rows']:,} | {t['reason']} |")
        else:
            lines.append("✅ 无闲置表")

        return '\n'.join(lines)

    def _fmt_size(self, b: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"