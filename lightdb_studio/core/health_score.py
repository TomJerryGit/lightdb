"""
数据库健康度评分
- 表设计评分
- 索引效率
- 数据一致性
- 综合健康分
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Callable


@dataclass
class HealthIssue:
    category: str = ""
    severity: str = "info"          # info / warning / critical
    table: str = ""
    message: str = ""
    impact: int = 0                 # 扣分
    suggestion: str = ""


@dataclass
class HealthReport:
    score: int = 100
    category_scores: Dict[str, int] = field(default_factory=dict)
    issues: List[HealthIssue] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)
    generated_at: float = 0

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = time.time()


class HealthScorer:
    """数据库健康评分器"""

    def __init__(self, conn):
        self.conn = conn

    def analyze(self, progress_cb: Callable = None) -> HealthReport:
        report = HealthReport()

        try:
            tables = self.conn.get_tables()
        except Exception as e:
            report.issues.append(HealthIssue(
                category='connection', severity='critical',
                message=f"无法连接: {e}", impact=100))
            report.score = 0
            return report

        report.stats['total_tables'] = len(tables)

        # 分类评分
        design_score = self._check_design(tables, report)
        index_score = self._check_indexes(tables, report)
        data_score = self._check_data(tables, report)
        size_score = self._check_sizes(tables, report)

        report.category_scores = {
            'design': design_score,
            'index': index_score,
            'data': data_score,
            'size': size_score,
        }

        # 加权总分
        weights = {'design': 0.3, 'index': 0.3,
                   'data': 0.2, 'size': 0.2}
        total = sum(report.category_scores[k] * weights[k]
                    for k in weights)
        report.score = int(total)

        return report

    def _check_design(self, tables, report) -> int:
        """表设计：主键、命名、字段类型"""
        score = 100
        for t in tables:
            try:
                cols = self.conn.get_columns(t)
            except Exception:
                continue

            # 主键
            pks = [c['name'] for c in cols if c.get('key') == 'PRI']
            if not pks:
                score -= 10
                report.issues.append(HealthIssue(
                    category='design', severity='warning',
                    table=t,
                    message='表没有主键',
                    impact=10,
                    suggestion='添加自增主键'))

            # 字段数量
            if len(cols) > 50:
                score -= 3
                report.issues.append(HealthIssue(
                    category='design', severity='info',
                    table=t,
                    message=f'字段过多 ({len(cols)} 个)',
                    impact=3,
                    suggestion='考虑拆分表或使用 JSON'))

            # 类型检查
            for c in cols:
                ctype = (c['type'] or '').upper()
                # VARCHAR 过长
                m = re.match(r'VARCHAR\((\d+)\)', ctype)
                if m and int(m.group(1)) > 2000:
                    score -= 2
                    report.issues.append(HealthIssue(
                        category='design', severity='info',
                        table=t,
                        message=f'列 {c["name"]} VARCHAR 过长 ({m.group(1)})',
                        impact=2,
                        suggestion='改用 TEXT 类型'))

                # FLOAT 用于金额
                if 'FLOAT' in ctype and any(
                        k in c['name'].lower()
                        for k in ('amount', 'price', 'money', 'cost')):
                    score -= 3
                    report.issues.append(HealthIssue(
                        category='design', severity='warning',
                        table=t,
                        message=f'列 {c["name"]} 用 FLOAT 存金额，精度可能丢失',
                        impact=3,
                        suggestion='改用 DECIMAL'))

        return max(0, score)

    def _check_indexes(self, tables, report) -> int:
        """索引效率"""
        score = 100
        for t in tables:
            try:
                cols, rows = self.conn.query(f"SHOW INDEX FROM `{t}`")
            except Exception:
                continue

            # 收集索引
            indexes = {}
            for r in rows:
                # 列名定位
                # Table, Non_unique, Key_name, Seq_in_index, Column_name, ...
                idx_name = None
                col_name = None
                for i, c in enumerate(cols):
                    cl = c.lower() if isinstance(c, str) else str(c).lower()
                    if cl == 'key_name':
                        idx_name = r[i]
                    elif cl == 'column_name':
                        col_name = r[i]
                if idx_name:
                    indexes.setdefault(idx_name, []).append(col_name)

            # 无索引警告
            if not indexes:
                score -= 15
                report.issues.append(HealthIssue(
                    category='index', severity='warning',
                    table=t,
                    message='表无任何索引',
                    impact=15,
                    suggestion='为常用查询列添加索引'))

            # 冗余索引（某索引是另一个的前缀）
            names = list(indexes.keys())
            for i, n1 in enumerate(names):
                for n2 in names:
                    if n1 == n2:
                        continue
                    c1 = indexes[n1]
                    c2 = indexes[n2]
                    if len(c1) >= len(c2) and c2 == c1[:len(c2)]:
                        score -= 2
                        report.issues.append(HealthIssue(
                            category='index', severity='info',
                            table=t,
                            message=f'索引 {n2} 是 {n1} 的前缀，冗余',
                            impact=2,
                            suggestion=f'删除索引 {n2}'))

        return max(0, score)

    def _check_data(self, tables, report) -> int:
        """数据质量"""
        score = 100
        for t in tables:
            try:
                cols, rows = self.conn.query(
                    f"SELECT COUNT(*) FROM `{t}`")
                n = rows[0][0] if rows else 0
            except Exception:
                continue

            if n == 0:
                report.issues.append(HealthIssue(
                    category='data', severity='info',
                    table=t, message='空表', impact=1))

            # 空值率（只查前几列）
            try:
                col_infos = self.conn.get_columns(t)[:5]
                for c in col_infos:
                    if c.get('key') == 'PRI':
                        continue
                    if n > 0:
                        try:
                            _, r = self.conn.query(
                                f"SELECT COUNT(*) FROM `{t}` "
                                f"WHERE `{c['name']}` IS NULL")
                            null_count = r[0][0] if r else 0
                            ratio = null_count / n
                            if ratio > 0.5:
                                score -= 5
                                report.issues.append(HealthIssue(
                                    category='data', severity='warning',
                                    table=t,
                                    message=f'{c["name"]} 空值率 {ratio*100:.0f}%',
                                    impact=5,
                                    suggestion='检查业务逻辑'))
                        except Exception:
                            pass
            except Exception:
                pass

        return max(0, score)

    def _check_sizes(self, tables, report) -> int:
        """表规模分布"""
        score = 100
        counts = []
        for t in tables:
            try:
                _, rows = self.conn.query(
                    f"SELECT COUNT(*) FROM `{t}`")
                counts.append((t, rows[0][0] if rows else 0))
            except Exception:
                continue

        if not counts:
            return score

        total = sum(c for _, c in counts)
        report.stats['total_rows'] = total

        # 大表占比
        for t, c in counts:
            if total > 0 and c / total > 0.8 and total > 1000000:
                score -= 5
                report.issues.append(HealthIssue(
                    category='size', severity='info',
                    table=t,
                    message=f'该表占全库 {c/total*100:.0f}% 数据',
                    impact=5,
                    suggestion='考虑归档或分区'))

        return max(0, score)

    def generate_report(self, report: HealthReport) -> str:
        lines = [
            "# 数据库健康报告",
            f"生成时间: "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.generated_at))}",
            "",
            f"## 综合得分: **{report.score}/100**",
            "",
            "## 分类得分",
            "",
        ]
        for k, v in report.category_scores.items():
            icon = '🟢' if v >= 80 else '🟡' if v >= 60 else '🔴'
            label = {
                'design': '表设计', 'index': '索引效率',
                'data': '数据质量', 'size': '规模分布',
            }.get(k, k)
            lines.append(f"- {icon} **{label}**: {v}/100")

        lines.append("")
        lines.append("## 问题清单")
        lines.append("")

        # 按严重程度分组
        by_sev = {'critical': [], 'warning': [], 'info': []}
        for issue in report.issues:
            by_sev[issue.severity].append(issue)

        for sev, label, icon in [
                ('critical', '严重', '🔴'),
                ('warning', '警告', '🟡'),
                ('info', '提示', '🔵')]:
            if not by_sev[sev]:
                continue
            lines.append(f"### {icon} {label} ({len(by_sev[sev])})")
            lines.append("")
            for issue in by_sev[sev]:
                tbl = f"`{issue.table}` " if issue.table else ""
                lines.append(f"- {tbl}{issue.message}")
                if issue.suggestion:
                    lines.append(f"  - 💡 {issue.suggestion}")
            lines.append("")

        return '\n'.join(lines)