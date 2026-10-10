"""
数据质量 2.0
- 正则规则引擎
- 数据分布直方图
- 跨表一致性检查
- 自动修复建议
"""
import json
import math
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Callable, Optional


QUALITY_RULES_FILE = Path.home() / '.lightdb_studio' / 'quality_rules.json'


# 内置规则模板
BUILTIN_PATTERNS = [
    {'name': 'email', 'regex': r'^[^@\s]+@[^@\s]+\.[^@\s]+$',
     'desc': '邮箱格式'},
    {'name': 'phone_cn', 'regex': r'^1[3-9]\d{9}$',
     'desc': '中国大陆手机号'},
    {'name': 'id_card_cn', 'regex': r'^\d{17}[\dXx]$',
     'desc': '身份证号'},
    {'name': 'url', 'regex': r'^https?://',
     'desc': 'URL 地址'},
    {'name': 'ipv4', 'regex': r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
     'desc': 'IPv4 地址'},
    {'name': 'hex_color', 'regex': r'^#[0-9a-fA-F]{6}$',
     'desc': '十六进制颜色'},
    {'name': 'postal_cn', 'regex': r'^\d{6}$',
     'desc': '中国邮编'},
    {'name': 'chinese_only', 'regex': r'^[\u4e00-\u9fa5]+$',
     'desc': '纯中文'},
    {'name': 'alphanumeric', 'regex': r'^[A-Za-z0-9]+$',
     'desc': '字母数字'},
]


@dataclass
class PatternRule:
    name: str = ""
    column_pattern: str = ""       # 列名通配
    regex: str = ""
    description: str = ""
    enabled: bool = True
    severity: str = "warning"      # info / warning / error

    def matches_column(self, col: str) -> bool:
        if not self.column_pattern:
            return False
        p = self.column_pattern
        if '*' in p or '?' in p:
            rx = p.replace('*', '.*').replace('?', '.')
            return bool(re.match('^' + rx + '$', col, re.IGNORECASE))
        return col.lower() == p.lower()


@dataclass
class QualityIssue:
    level: str = "info"
    category: str = ""
    table: str = ""
    column: str = ""
    message: str = ""
    sample_values: List[str] = field(default_factory=list)
    count: int = 0
    suggestion: str = ""
    fix_sql: str = ""


@dataclass
class Distribution:
    """列分布"""
    column: str = ""
    total: int = 0
    null_count: int = 0
    distinct_count: int = 0
    min_value: Any = None
    max_value: Any = None
    avg_value: Optional[float] = None
    # 直方图：bin_label -> count
    histogram: Dict[str, int] = field(default_factory=dict)
    # Top 值
    top_values: List[tuple] = field(default_factory=list)


class QualityEngineV2:
    """数据质量 2.0"""

    def __init__(self, conn=None):
        self.conn = conn
        self.file = QUALITY_RULES_FILE
        self.rules: List[PatternRule] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def set_connection(self, conn):
        self.conn = conn

    # ==================== 规则管理 ====================

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for r in data.get('rules', []):
                self.rules.append(PatternRule(**r))
        except Exception:
            pass

    def save(self):
        data = {'rules': [asdict(r) for r in self.rules]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add_rule(self, rule: PatternRule):
        self.rules.append(rule)
        self.save()

    def remove_rule(self, index: int):
        if 0 <= index < len(self.rules):
            del self.rules[index]
            self.save()

    def add_builtin_rule(self, name: str, column_pattern: str):
        """根据内置模板添加规则"""
        for p in BUILTIN_PATTERNS:
            if p['name'] == name:
                rule = PatternRule(
                    name=p['desc'],
                    column_pattern=column_pattern,
                    regex=p['regex'],
                    description=p['desc'],
                )
                self.add_rule(rule)
                return rule
        return None

    def auto_setup_rules(self) -> int:
        """根据内置模板自动配置规则"""
        auto_map = {
            'email': '*email*',
            'phone_cn': '*phone*',
            'id_card_cn': '*id_card*',
            'url': '*url*',
        }
        n = 0
        for name, pat in auto_map.items():
            # 去重
            if any(r.column_pattern == pat for r in self.rules):
                continue
            self.add_builtin_rule(name, pat)
            n += 1
        return n

    # ==================== 检查 ====================

    def check_table(self, table: str,
                    progress_cb: Callable = None) -> List[QualityIssue]:
        """检查单表"""
        if self.conn is None:
            return []

        issues = []
        try:
            cols = self.conn.get_columns(table)
            _, r = self.conn.query(f"SELECT COUNT(*) FROM `{table}`")
            total = r[0][0] if r else 0
        except Exception as e:
            return [QualityIssue(level='error', category='schema',
                                 table=table,
                                 message=f"无法读取: {e}")]

        if total == 0:
            issues.append(QualityIssue(
                level='info', category='empty',
                table=table, message='空表'))
            return issues

        # 1. 正则规则
        for rule in self.rules:
            if not rule.enabled:
                continue
            for c in cols:
                if not rule.matches_column(c['name']):
                    continue
                issue = self._check_pattern(table, c['name'], rule, total)
                if issue:
                    issues.append(issue)

        # 2. 逐列基础检查
        for i, c in enumerate(cols):
            if progress_cb:
                progress_cb(i, len(cols), c['name'])

            # 空值
            issue = self._check_null(table, c['name'], total)
            if issue:
                issues.append(issue)

            # 唯一
            if c.get('key') == 'UNI':
                issue = self._check_unique(table, c['name'])
                if issue:
                    issues.append(issue)

        # 3. 跨表一致性（外键推断）
        consistency_issues = self._check_consistency(table, cols)
        issues.extend(consistency_issues)

        return issues

    def _check_pattern(self, table: str, column: str,
                       rule: PatternRule, total: int) -> Optional[QualityIssue]:
        """检查正则规则"""
        try:
            # 抽样检测
            sample_size = min(1000, total)
            _, r = self.conn.query(
                f"SELECT `{column}` FROM `{table}` "
                f"WHERE `{column}` IS NOT NULL LIMIT {sample_size}")

            if not r:
                return None

            bad = []
            for row in r:
                v = str(row[0]) if row[0] is not None else ''
                if not re.match(rule.regex, v):
                    bad.append(v)
                    if len(bad) >= 5:
                        break

            if bad:
                # 估算总量
                est_bad = int(len(bad) / len(r) * total) if r else len(bad)
                return QualityIssue(
                    level=rule.severity,
                    category='pattern',
                    table=table,
                    column=column,
                    message=f"不满足 [{rule.description}] 格式，抽样 {len(bad)}/{len(r)} 异常",
                    sample_values=[str(b)[:50] for b in bad],
                    count=est_bad,
                    suggestion=f"检查数据源或修正正则",
                )
        except Exception:
            pass
        return None

    def _check_null(self, table: str, column: str,
                    total: int) -> Optional[QualityIssue]:
        """空值检查"""
        try:
            _, r = self.conn.query(
                f"SELECT COUNT(*) FROM `{table}` "
                f"WHERE `{column}` IS NULL")
            null_count = r[0][0] if r else 0
            if null_count == 0:
                return None

            ratio = null_count / total
            if ratio > 0.5:
                level = 'error'
            elif ratio > 0.1:
                level = 'warning'
            else:
                level = 'info'

            return QualityIssue(
                level=level, category='null_values',
                table=table, column=column,
                message=f'空值 {null_count} ({ratio*100:.1f}%)',
                count=null_count,
                suggestion='检查业务逻辑或添加 NOT NULL 约束'
                if ratio > 0.1 else '')
        except Exception:
            pass
        return None

    def _check_unique(self, table: str, column: str) -> Optional[QualityIssue]:
        """唯一性检查"""
        try:
            _, r = self.conn.query(
                f"SELECT `{column}`, COUNT(*) AS c FROM `{table}` "
                f"GROUP BY `{column}` HAVING c > 1 LIMIT 5")
            if r:
                return QualityIssue(
                    level='error', category='duplicate',
                    table=table, column=column,
                    message=f'存在重复值 ({len(r)} 组)',
                    sample_values=[f"{row[0]}x{row[1]}" for row in r[:5]],
                    count=len(r),
                    suggestion='添加唯一约束或清理数据')
        except Exception:
            pass
        return None

    def _check_consistency(self, table: str,
                           cols: List[Dict]) -> List[QualityIssue]:
        """跨表一致性：基于 _id 命名推断"""
        issues = []
        try:
            all_tables = self.conn.get_tables()
        except Exception:
            return issues

        for c in cols:
            cname = c['name'].lower()
            # 匹配 xxx_id
            m = re.match(r'^(\w+)_id$', cname)
            if not m:
                continue
            parent = m.group(1)
            if parent not in [t.lower() for t in all_tables]:
                continue

            # 找父表主键
            parent_table = next(t for t in all_tables
                                if t.lower() == parent)
            try:
                pcols = self.conn.get_columns(parent_table)
                pk = next((pc['name'] for pc in pcols
                           if pc.get('key') == 'PRI'), None)
                if not pk:
                    continue

                # 检查孤儿记录
                _, r = self.conn.query(
                    f"SELECT COUNT(*) FROM `{table}` t "
                    f"LEFT JOIN `{parent_table}` p "
                    f"ON t.`{c['name']}` = p.`{pk}` "
                    f"WHERE t.`{c['name']}` IS NOT NULL "
                    f"AND p.`{pk}` IS NULL")
                orphans = r[0][0] if r else 0
                if orphans > 0:
                    issues.append(QualityIssue(
                        level='warning', category='consistency',
                        table=table, column=c['name'],
                        message=f'{orphans} 条记录引用了不存在的 {parent}.{pk}',
                        count=orphans,
                        suggestion=f'清理孤儿记录或添加外键约束',
                        fix_sql=f"DELETE FROM `{table}` WHERE `{c['name']}` "
                                f"NOT IN (SELECT `{pk}` FROM `{parent_table}`)",
                    ))
            except Exception:
                pass
        return issues

    # ==================== 分布分析 ====================

    def distribution(self, table: str, column: str,
                     bins: int = 20) -> Optional[Distribution]:
        """分析列分布"""
        if self.conn is None:
            return None

        dist = Distribution(column=column)
        try:
            # 基础统计
            _, r = self.conn.query(
                f"SELECT COUNT(*), COUNT(`{column}`), "
                f"COUNT(DISTINCT `{column}`) FROM `{table}`")
            if r:
                row = r[0]
                dist.total = row[0] or 0
                dist.null_count = (row[0] or 0) - (row[1] or 0)
                dist.distinct_count = row[2] or 0

            # 是否为数值列
            cols = self.conn.get_columns(table)
            col_type = ''
            for c in cols:
                if c['name'] == column:
                    col_type = (c['type'] or '').upper()
                    break

            is_numeric = any(t in col_type for t in
                             ('INT', 'DECIMAL', 'FLOAT', 'DOUBLE', 'BIGINT'))

            if is_numeric:
                # 数值统计
                _, r = self.conn.query(
                    f"SELECT MIN(`{column}`), MAX(`{column}`), AVG(`{column}`) "
                    f"FROM `{table}` WHERE `{column}` IS NOT NULL")
                if r and r[0][0] is not None:
                    dist.min_value = r[0][0]
                    dist.max_value = r[0][1]
                    dist.avg_value = float(r[0][2]) if r[0][2] is not None else None

                    # 直方图
                    try:
                        mn, mx = float(r[0][0]), float(r[0][1])
                        if mx > mn:
                            step = (mx - mn) / bins
                            for i in range(bins):
                                lo = mn + i * step
                                hi = lo + step
                                _, r2 = self.conn.query(
                                    f"SELECT COUNT(*) FROM `{table}` "
                                    f"WHERE `{column}` >= {lo} AND `{column}` < {hi}")
                                cnt = r2[0][0] if r2 else 0
                                label = f"[{lo:.2f}, {hi:.2f})"
                                dist.histogram[label] = cnt
                    except Exception:
                        pass
            else:
                # 分类统计
                _, r = self.conn.query(
                    f"SELECT `{column}`, COUNT(*) AS c FROM `{table}` "
                    f"WHERE `{column}` IS NOT NULL "
                    f"GROUP BY `{column}` ORDER BY c DESC LIMIT 20")
                if r:
                    dist.top_values = [
                        (row[0], row[1]) for row in r]
                    total_non_null = sum(row[1] for row in r)
                    dist.histogram = {
                        str(row[0])[:30]: row[1] for row in r
                    }
                    if not dist.min_value and r:
                        dist.min_value = r[0][0]
                    if r:
                        dist.max_value = r[0][0]

        except Exception as e:
            return None

        return dist

    def check_database(self, progress_cb: Callable = None) -> Dict:
        """整库检查"""
        if self.conn is None:
            return {'error': '无连接'}

        try:
            tables = self.conn.get_tables()
        except Exception as e:
            return {'error': str(e)}

        all_issues = []
        for i, t in enumerate(tables):
            if progress_cb:
                progress_cb(i, len(tables), t)
            issues = self.check_table(t)
            all_issues.extend(issues)

        return {
            'total_tables': len(tables),
            'total_issues': len(all_issues),
            'errors': sum(1 for i in all_issues if i.level == 'error'),
            'warnings': sum(1 for i in all_issues if i.level == 'warning'),
            'infos': sum(1 for i in all_issues if i.level == 'info'),
            'issues': all_issues,
        }

    def generate_report(self, summary: Dict) -> str:
        """生成 Markdown 报告"""
        lines = [
            "# 数据质量报告 v2",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 概览",
            f"- 检查表: {summary.get('total_tables', 0)}",
            f"- 问题总数: {summary.get('total_issues', 0)}",
            f"  - 🔴 错误: {summary.get('errors', 0)}",
            f"  - 🟡 警告: {summary.get('warnings', 0)}",
            f"  - 🔵 提示: {summary.get('infos', 0)}",
            "",
        ]

        by_cat = {}
        for i in summary.get('issues', []):
            by_cat.setdefault(i.category, []).append(i)

        for cat, issues in by_cat.items():
            lines.append(f"## {cat} ({len(issues)})")
            lines.append("")
            for issue in issues:
                icon = {'error': '🔴', 'warning': '🟡',
                        'info': '🔵'}.get(issue.level, '⚪')
                col = f"`{issue.column}`" if issue.column else "(表级)"
                lines.append(f"- {icon} `{issue.table}` {col}: {issue.message}")
                if issue.sample_values:
                    lines.append(
                        f"  - 样例: `{', '.join(issue.sample_values[:3])}`")
                if issue.suggestion:
                    lines.append(f"  - 💡 {issue.suggestion}")
                if issue.fix_sql:
                    lines.append(f"  - 修复: `{issue.fix_sql}`")
            lines.append("")

        return '\n'.join(lines)