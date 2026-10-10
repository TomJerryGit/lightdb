"""
慢查询自动归因
- 分析慢查询模式
- 关联 EXPLAIN
- 生成优化建议
"""
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SlowQueryRecord:
    """慢查询记录"""
    sql: str
    duration_ms: float
    timestamp: float = 0
    user: str = ""
    db: str = ""
    rows_examined: int = 0
    rows_sent: int = 0
    # 归因
    category: str = ""           # 分类：full_scan / no_index / bad_join ...
    suspicion: str = ""          # 疑似原因
    suggestions: List[str] = field(default_factory=list)
    explain: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class SlowQueryStats:
    """慢查询统计"""
    total: int = 0
    by_pattern: Dict = field(default_factory=dict)
    by_category: Dict = field(default_factory=dict)
    by_hour: Dict = field(default_factory=dict)
    top_slowest: List[Dict] = field(default_factory=list)
    total_time_s: float = 0
    avg_time_ms: float = 0
    p50_ms: float = 0
    p95_ms: float = 0
    p99_ms: float = 0


class SlowQueryAnalyzer:
    """慢查询分析器"""

    def __init__(self, conn=None):
        self.conn = conn
        self.records: List[SlowQueryRecord] = []

    # ==================== 添加 ====================

    def add_from_log(self, log_entry: str):
        """从日志解析"""
        m = re.search(r'([\d.]+)s:\s*(.+)$', log_entry)
        if not m:
            return
        duration_ms = float(m.group(1)) * 1000
        sql = m.group(2).strip()

        record = SlowQueryRecord(
            sql=sql,
            duration_ms=duration_ms,
        )
        self._analyze_record(record)
        self.records.append(record)

    def add_record(self, sql: str, duration_ms: float,
                   **kwargs):
        record = SlowQueryRecord(
            sql=sql,
            duration_ms=duration_ms,
            **kwargs)
        self._analyze_record(record)
        self.records.append(record)
        return record

    # ==================== 归因 ====================

    def _analyze_record(self, r: SlowQueryRecord):
        """分析单条记录"""
        sql_up = r.sql.upper()

        # 分类
        categories = []
        suggestions = []

        # 1. 全表扫描
        if 'SELECT *' in sql_up and 'WHERE' not in sql_up:
            categories.append('full_scan')
            suggestions.append("避免 SELECT *，明确列出字段")
            suggestions.append("添加 WHERE 条件限制扫描范围")

        # 2. 前置通配
        if re.search(r"LIKE\s+'%", sql_up):
            categories.append('prefix_like')
            suggestions.append(
                "前置通配符 LIKE '%...' 无法使用索引，考虑全文索引")

        # 3. WHERE 函数
        if re.search(r'WHERE\s+\w+\s*\([^)]*\)\s*=', sql_up):
            categories.append('function_in_where')
            suggestions.append(
                "WHERE 中对列使用函数导致索引失效，改为对值使用函数")

        # 4. 隐式类型
        if re.search(r"=\s*'[0-9]+'", r.sql):
            categories.append('implicit_cast')
            suggestions.append("数字列避免用字符串比较，去掉引号")

        # 5. NOT IN
        if re.search(r'\bNOT\s+IN\b', sql_up):
            categories.append('not_in')
            suggestions.append(
                "NOT IN 性能差，改写为 NOT EXISTS 或 LEFT JOIN")

        # 6. OR
        or_count = len(re.findall(r'\bOR\b', sql_up))
        if or_count >= 2:
            categories.append('multi_or')
            suggestions.append(
                f"{or_count} 处 OR，改写为 UNION ALL 或 IN")

        # 7. 子查询
        subqueries = sql_up.count('SELECT') - 1
        if subqueries >= 2:
            categories.append('nested_subquery')
            suggestions.append(
                f"{subqueries} 层子查询，考虑用 JOIN 或 CTE")

        # 8. 无 LIMIT
        if sql_up.startswith('SELECT') and 'LIMIT' not in sql_up \
                and 'COUNT(' not in sql_up and 'GROUP BY' not in sql_up:
            categories.append('no_limit')
            suggestions.append("大表查询加 LIMIT")

        # 9. 无索引列 JOIN
        joins = re.findall(r'JOIN\s+\w+\s+ON\s+(\w+)\.(\w+)',
                            r.sql, re.IGNORECASE)
        if joins:
            categories.append('multi_join')

        # 10. 时间范围缺失
        if re.search(r'WHERE.*DATE', sql_up) and 'BETWEEN' not in sql_up \
                and '<' not in sql_up and '>' not in sql_up:
            suggestions.append("时间条件使用范围查询可走索引")

        r.category = categories[0] if categories else 'unknown'
        r.suspicion = '; '.join(categories)
        r.suggestions = suggestions

    # ==================== EXPLAIN 关联 ====================

    def explain_record(self, r: SlowQueryRecord) -> bool:
        """执行 EXPLAIN"""
        if not self.conn:
            return False
        if not r.sql.strip().upper().startswith(
                ('SELECT', 'UPDATE', 'DELETE')):
            return False

        try:
            cols, rows = self.conn.query("EXPLAIN " + r.sql)
            r.explain = [
                ' | '.join(str(v) for v in row) for row in rows]
            # 提取 rows_examined
            for i, c in enumerate(cols):
                if c.lower() == 'rows':
                    try:
                        r.rows_examined = int(rows[0][i] or 0)
                    except (ValueError, TypeError, IndexError):
                        pass
            return True
        except Exception:
            return False

    # ==================== 统计 ====================

    def analyze(self, top_n: int = 10) -> SlowQueryStats:
        """聚合分析"""
        stats = SlowQueryStats()
        if not self.records:
            return stats

        stats.total = len(self.records)
        stats.total_time_s = sum(r.duration_ms for r in self.records) / 1000
        stats.avg_time_ms = sum(
            r.duration_ms for r in self.records) / stats.total

        # 分位数
        sorted_dur = sorted(r.duration_ms for r in self.records)
        n = len(sorted_dur)
        stats.p50_ms = sorted_dur[int(n * 0.5)]
        stats.p95_ms = sorted_dur[int(n * 0.95)]
        stats.p99_ms = sorted_dur[min(int(n * 0.99), n - 1)]

        # 按模式聚合
        patterns = Counter()
        pattern_time = defaultdict(float)
        for r in self.records:
            # 归一化
            pat = re.sub(r'\d+', '?', r.sql)
            pat = re.sub(r"'[^']*'", "'?'", pat)
            pat = re.sub(r'\s+', ' ', pat).strip()
            patterns[pat] += 1
            pattern_time[pat] += r.duration_ms

        stats.by_pattern = {
            pat: {'count': cnt, 'total_ms': pattern_time[pat]}
            for pat, cnt in patterns.most_common(top_n)
        }

        # 按分类
        categories = Counter(r.category for r in self.records)
        stats.by_category = dict(categories)

        # 按小时
        hours = Counter()
        for r in self.records:
            h = time.localtime(r.timestamp).tm_hour
            hours[h] += 1
        stats.by_hour = dict(sorted(hours.items()))

        # Top 最慢
        top = sorted(self.records, key=lambda x: -x.duration_ms)[:top_n]
        stats.top_slowest = [
            {
                'sql': r.sql[:200],
                'duration_ms': r.duration_ms,
                'category': r.category,
                'suggestions': r.suggestions[:3],
            }
            for r in top
        ]

        return stats

    # ==================== 报告 ====================

    def generate_report(self, stats: SlowQueryStats = None) -> str:
        """生成 Markdown 报告"""
        if stats is None:
            stats = self.analyze()

        lines = [
            "# 慢查询分析报告",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 概览",
            f"- 慢查询总数: {stats.total}",
            f"- 总耗时: {stats.total_time_s:.2f}s",
            f"- 平均耗时: {stats.avg_time_ms:.2f}ms",
            f"- P50: {stats.p50_ms:.2f}ms",
            f"- P95: {stats.p95_ms:.2f}ms",
            f"- P99: {stats.p99_ms:.2f}ms",
            "",
            "## 分类分布",
            "",
            "| 分类 | 数量 |",
            "|------|------|",
        ]
        for cat, cnt in sorted(stats.by_category.items(),
                                key=lambda x: -x[1]):
            lines.append(f"| {cat} | {cnt} |")

        lines.append("")
        lines.append("## Top 10 最慢查询")
        lines.append("")
        for i, r in enumerate(stats.top_slowest, 1):
            lines.append(f"### {i}. {r['duration_ms']:.1f}ms "
                          f"[{r['category']}]")
            lines.append("")
            lines.append(f"```sql\n{r['sql']}\n```")
            if r['suggestions']:
                lines.append("**建议:**")
                for s in r['suggestions']:
                    lines.append(f"- {s}")
            lines.append("")

        lines.append("## 按 SQL 模式聚合")
        lines.append("")
        for pat, info in list(stats.by_pattern.items())[:10]:
            lines.append(
                f"- x{info['count']} ({info['total_ms']:.0f}ms) "
                f"`{pat[:80]}`")

        return '\n'.join(lines)

    def save(self, path: str):
        stats = self.analyze()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'stats': asdict(stats),
                'records': [asdict(r) for r in self.records[-500:]],
            }, f, ensure_ascii=False, indent=2)