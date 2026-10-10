"""慢查询日志分析"""
import re
from collections import Counter, defaultdict
from datetime import datetime


class SlowLogAnalyzer:
    def __init__(self, logger_obj):
        self.logger = logger_obj

    def analyze(self, top_n: int = 10) -> dict:
        entries = list(self.logger.slow_log)
        if not entries:
            return {'total': 0, 'top_slow': [], 'by_pattern': []}

        parsed = []
        for line in entries:
            m = re.search(r'([\d.]+)s:\s*(.+)$', line)
            if m:
                elapsed = float(m.group(1))
                sql = m.group(2).strip()
                parsed.append((elapsed, sql))

        # Top N 最慢
        parsed.sort(reverse=True)
        top_slow = parsed[:top_n]

        # 按 SQL 模式聚合（去数字）
        patterns = Counter()
        pattern_time = defaultdict(float)
        for elapsed, sql in parsed:
            pat = re.sub(r'\d+', '?', sql)
            patterns[pat] += 1
            pattern_time[pat] += elapsed

        by_pattern = [
            (pat, cnt, pattern_time[pat])
            for pat, cnt in patterns.most_common(top_n)
        ]

        return {
            'total': len(parsed),
            'top_slow': top_slow,
            'by_pattern': by_pattern,
            'avg_time': sum(e for e, _ in parsed) / len(parsed),
        }

    def report(self) -> str:
        st = self.analyze()
        lines = [f"=== Slow Query Report ===",
                 f"Total: {st['total']}",
                 f"Avg time: {st.get('avg_time', 0):.3f}s",
                 ""]
        lines.append("--- Top 10 Slowest ---")
        for elapsed, sql in st['top_slow']:
            lines.append(f"  {elapsed:.3f}s  {sql[:100]}")

        lines.append("")
        lines.append("--- Top 10 by Pattern ---")
        for pat, cnt, total in st['by_pattern']:
            lines.append(f"  x{cnt:3d}  {total:.3f}s  {pat[:80]}")

        return '\n'.join(lines)