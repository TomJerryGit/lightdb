"""
数据质量检查
- 空值检测
- 唯一性检查
- 分布统计
- 异常值
"""
import re
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class QualityIssue:
    level: str = 'info'          # info / warning / error
    category: str = ''
    table: str = ''
    column: str = ''
    message: str = ''
    count: int = 0
    suggestion: str = ''


class DataQualityChecker:
    def __init__(self, conn):
        self.conn = conn

    def check_table(self, table: str,
                    progress_cb=None) -> List[QualityIssue]:
        """检查单表"""
        issues = []
        try:
            cols = self.conn.get_columns(table)
        except Exception as e:
            return [QualityIssue(
                level='error', category='schema', table=table,
                message=f"无法获取表结构: {e}")]

        try:
            c, r = self.conn.query(
                f"SELECT COUNT(*) FROM `{table}`")
            total_rows = r[0][0] if r else 0
        except Exception as e:
            return [QualityIssue(
                level='error', category='query', table=table,
                message=f"无法统计行数: {e}")]

        if total_rows == 0:
            issues.append(QualityIssue(
                level='warning', category='empty_table',
                table=table,
                message='表为空',
                suggestion='确认是否正常'))
            return issues

        # 1. 主键检查
        pks = [c['name'] for c in cols if c.get('key') == 'PRI']
        if not pks:
            issues.append(QualityIssue(
                level='warning', category='no_primary_key',
                table=table,
                message='表没有主键',
                suggestion='建议添加主键以提升性能和保证唯一性'))

        # 2. 逐列检查
        for i, col in enumerate(cols):
            if progress_cb:
                progress_cb(i, len(cols), col['name'])

            cname = col['name']
            ctype = col['type'].upper()

            # 空值率
            try:
                _, r = self.conn.query(
                    f"SELECT COUNT(*) FROM `{table}` "
                    f"WHERE `{cname}` IS NULL")
                null_count = r[0][0] if r else 0
            except Exception:
                null_count = 0

            if null_count > 0:
                ratio = null_count / total_rows
                level = 'error' if ratio > 0.5 else \
                        'warning' if ratio > 0.1 else 'info'
                issues.append(QualityIssue(
                    level=level, category='null_values',
                    table=table, column=cname,
                    message=f'有 {null_count} 个空值 ({ratio*100:.1f}%)',
                    count=null_count,
                    suggestion='检查业务逻辑或添加 NOT NULL 约束'
                    if ratio > 0.1 else ''))

            # 唯一性检查（仅对可能是唯一键的列）
            if col.get('key') == 'UNI' or cname.lower() in (
                    'id', 'uuid', 'code', 'no', 'number'):
                try:
                    _, r = self.conn.query(
                        f"SELECT COUNT(*) FROM ("
                        f"SELECT `{cname}` FROM `{table}` "
                        f"GROUP BY `{cname}` HAVING COUNT(*) > 1) t")
                    dup = r[0][0] if r else 0
                    if dup > 0:
                        issues.append(QualityIssue(
                            level='error',
                            category='duplicate_values',
                            table=table, column=cname,
                            message=f'有 {dup} 个重复值',
                            count=dup,
                            suggestion='添加唯一约束或清理数据'))
                except Exception:
                    pass

            # 字符串长度检查
            m = re.match(r'VARCHAR\((\d+)\)', ctype)
            if m:
                max_len = int(m.group(1))
                try:
                    _, r = self.conn.query(
                        f"SELECT MAX(LENGTH(`{cname}`)) FROM `{table}`")
                    actual = r[0][0] if r and r[0][0] else 0
                    if actual > max_len * 0.9:
                        issues.append(QualityIssue(
                            level='warning',
                            category='length_usage',
                            table=table, column=cname,
                            message=f'最长值 {actual} 接近上限 {max_len}',
                            suggestion='考虑增大 VARCHAR 长度'))
                except Exception:
                    pass

            # 数值范围检查（仅 INT/DECIMAL）
            if 'INT' in ctype or 'DECIMAL' in ctype or 'FLOAT' in ctype:
                try:
                    _, r = self.conn.query(
                        f"SELECT MIN(`{cname}`), MAX(`{cname}`) "
                        f"FROM `{table}` WHERE `{cname}` IS NOT NULL")
                    if r and r[0][0] is not None:
                        min_v, max_v = r[0][0], r[0][1]
                        # 负数检查
                        if min_v < 0 and col.get('key') == 'PRI':
                            issues.append(QualityIssue(
                                level='warning',
                                category='negative_pk',
                                table=table, column=cname,
                                message=f'主键有负值: min={min_v}',
                                suggestion='主键通常应为正数'))
                except Exception:
                    pass

        return issues

    def check_database(self, progress_cb=None) -> Dict:
        """检查整个数据库"""
        try:
            tables = self.conn.get_tables()
        except Exception as e:
            return {'error': str(e), 'issues': []}

        all_issues = []
        for i, t in enumerate(tables):
            if progress_cb:
                progress_cb(i, len(tables), t)
            issues = self.check_table(t)
            all_issues.extend(issues)

        summary = {
            'total_tables': len(tables),
            'total_issues': len(all_issues),
            'errors': sum(1 for i in all_issues if i.level == 'error'),
            'warnings': sum(1 for i in all_issues if i.level == 'warning'),
            'infos': sum(1 for i in all_issues if i.level == 'info'),
            'issues': all_issues,
        }
        return summary

    def generate_report(self, summary: Dict) -> str:
        """生成 Markdown 报告"""
        lines = [
            "# 数据质量报告",
            f"生成时间: {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 概览",
            f"- 检查表数: {summary.get('total_tables', 0)}",
            f"- 问题总数: {summary.get('total_issues', 0)}",
            f"  - 🔴 错误: {summary.get('errors', 0)}",
            f"  - 🟡 警告: {summary.get('warnings', 0)}",
            f"  - 🔵 提示: {summary.get('infos', 0)}",
            "",
            "## 详细问题",
            "",
        ]

        by_table = {}
        for i in summary.get('issues', []):
            by_table.setdefault(i.table, []).append(i)

        for table, issues in by_table.items():
            lines.append(f"### {table} ({len(issues)} 个问题)")
            lines.append("")
            for issue in issues:
                icon = {'error': '🔴', 'warning': '🟡', 'info': '🔵'}.get(
                    issue.level, '⚪')
                col = f"**{issue.column}**" if issue.column else "(表级)"
                lines.append(f"- {icon} {col}: {issue.message}")
                if issue.suggestion:
                    lines.append(f"  - 💡 {issue.suggestion}")
            lines.append("")

        return '\n'.join(lines)