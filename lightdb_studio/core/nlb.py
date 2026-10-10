"""
自然语言 BI
- 问答式数据探索
- 自动生成图表
- 智能追问
- 数据故事
"""
import json
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


@dataclass
class BIQuestion:
    """BI 问题"""
    question: str
    context: Dict = field(default_factory=dict)


@dataclass
class BIAnswer:
    """BI 回答"""
    question: str
    sql: str = ""
    columns: List[str] = field(default_factory=list)
    rows: List[List] = field(default_factory=list)
    # 图表配置
    chart: Dict = field(default_factory=dict)
    # 洞察
    insight: str = ""
    # 追问建议
    follow_ups: List[str] = field(default_factory=list)
    # 耗时
    elapsed_ms: float = 0
    error: str = ""


class NaturalLanguageBI:
    """自然语言 BI 引擎"""

    # 问题模式 → 意图
    INTENT_PATTERNS = [
        ('count', r'(?:多少|数量|总数|有几|count)'),
        ('trend', r'(?:趋势|变化|增长|时间线|trend)'),
        ('compare', r'(?:对比|比较|差异|vs)'),
        ('top', r'(?:排名|前几|最多|最少|top)'),
        ('distribution', r'(?:分布|占比|比例|构成)'),
        ('aggregate', r'(?:平均|总和|合计|avg|sum|total)'),
        ('detail', r'(?:详细|明细|列出|list|show)'),
    ]

    # 维度识别关键词
    DIMENSION_KEYWORDS = {
        'date': ['日期', '时间', '天', '月', '年', '周', 'date', 'time'],
        'city': ['城市', '地区', '区域', 'city', 'region'],
        'category': ['分类', '类别', '类型', 'category', 'type'],
        'status': ['状态', 'status', 'state'],
        'user': ['用户', '客户', 'user', 'customer'],
        'product': ['产品', '商品', 'product', 'item'],
    }

    # 度量识别
    METRIC_KEYWORDS = {
        'amount': ['金额', '销售', '收入', '营收', 'amount', 'revenue', 'sales'],
        'count': ['数量', '订单数', '次数', 'count'],
        'price': ['价格', '单价', 'price'],
        'quantity': ['数量', '件数', 'quantity'],
    }

    def __init__(self, conn=None, catalog=None, ai_client=None):
        self.conn = conn
        self.catalog = catalog
        self.ai_client = ai_client

    def ask(self, question: str,
            auto_chart: bool = True) -> BIAnswer:
        """回答问题"""
        t0 = time.time()
        answer = BIAnswer(question=question)

        try:
            # 1. 识别意图
            intent = self._detect_intent(question)

            # 2. 识别维度、度量
            dimensions = self._detect_dimensions(question)
            metrics = self._detect_metrics(question)

            # 3. 找目标表
            table = self._find_table(question)
            if not table:
                answer.error = "未识别到数据表"
                return answer

            # 4. 生成 SQL
            sql = self._build_sql(table, intent, dimensions,
                                   metrics, question)
            answer.sql = sql

            # 5. 执行
            if self.conn:
                cols, rows = self.conn.query(sql)
                answer.columns = cols
                answer.rows = [list(r) for r in rows]

            # 6. 生成图表
            if auto_chart:
                answer.chart = self._suggest_chart(
                    intent, dimensions, metrics)

            # 7. 洞察
            answer.insight = self._generate_insight(
                question, intent, answer.rows)

            # 8. 追问建议
            answer.follow_ups = self._suggest_follow_ups(
                question, intent, dimensions, table)

        except Exception as e:
            answer.error = str(e)

        answer.elapsed_ms = (time.time() - t0) * 1000
        return answer

    def _detect_intent(self, question: str) -> str:
        for intent, pattern in self.INTENT_PATTERNS:
            if re.search(pattern, question, re.IGNORECASE):
                return intent
        return 'detail'

    def _detect_dimensions(self, question: str) -> List[str]:
        dims = []
        for dim, keywords in self.DIMENSION_KEYWORDS.items():
            if any(kw in question.lower() for kw in keywords):
                dims.append(dim)
        return dims

    def _detect_metrics(self, question: str) -> List[str]:
        metrics = []
        for m, keywords in self.METRIC_KEYWORDS.items():
            if any(kw in question.lower() for kw in keywords):
                metrics.append(m)
        return metrics

    def _find_table(self, question: str) -> Optional[str]:
        if not self.catalog:
            return None

        q = question.lower()
        candidates = []

        for key, meta in self.catalog.tables.items():
            # 表名匹配
            if meta.name.lower() in q:
                return meta.name
            if meta.business_name and meta.business_name in question:
                return meta.name

            # 业务域匹配
            score = 0
            if meta.domain and meta.domain.lower() in q:
                score += 3
            for tag in meta.tags:
                if tag.lower() in q:
                    score += 1
            if score > 0:
                candidates.append((score, meta.name))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

        return None

    def _build_sql(self, table: str, intent: str,
                   dimensions: List[str],
                   metrics: List[str],
                   question: str) -> str:
        """构建 SQL"""
        if intent == 'count':
            return f"SELECT COUNT(*) AS total FROM `{table}`"

        if intent == 'trend':
            # 需要日期列
            date_col = self._find_date_column(table)
            if date_col:
                return (f"SELECT DATE(`{date_col}`) AS day, "
                        f"COUNT(*) AS cnt "
                        f"FROM `{table}` "
                        f"GROUP BY DATE(`{date_col}`) "
                        f"ORDER BY day DESC LIMIT 30")

        if intent == 'top':
            return (f"SELECT * FROM `{table}` "
                    f"ORDER BY id DESC LIMIT 10")

        if intent == 'distribution':
            # 找分类列
            cat_col = self._find_category_column(table)
            if cat_col:
                return (f"SELECT `{cat_col}`, COUNT(*) AS cnt "
                        f"FROM `{table}` "
                        f"GROUP BY `{cat_col}` "
                        f"ORDER BY cnt DESC LIMIT 20")

        if intent == 'aggregate':
            # 找数值列
            num_col = self._find_numeric_column(table)
            if num_col:
                return (f"SELECT AVG(`{num_col}`) AS avg_val, "
                        f"SUM(`{num_col}`) AS sum_val, "
                        f"COUNT(*) AS cnt FROM `{table}`")

        # 默认
        return f"SELECT * FROM `{table}` LIMIT 100"

    def _find_date_column(self, table: str) -> Optional[str]:
        if not self.catalog:
            return None
        for key, meta in self.catalog.tables.items():
            if meta.name == table:
                for c in meta.columns:
                    ct = (c.type or '').upper()
                    if 'DATE' in ct or 'TIME' in ct or \
                       'date' in c.name.lower():
                        return c.name
        return None

    def _find_category_column(self, table: str) -> Optional[str]:
        if not self.catalog:
            return None
        for key, meta in self.catalog.tables.items():
            if meta.name == table:
                for c in meta.columns:
                    cn = c.name.lower()
                    if cn in ('status', 'type', 'category',
                               'state', 'class', 'kind'):
                        return c.name
        return None

    def _find_numeric_column(self, table: str) -> Optional[str]:
        if not self.catalog:
            return None
        for key, meta in self.catalog.tables.items():
            if meta.name == table:
                for c in meta.columns:
                    ct = (c.type or '').upper()
                    cn = c.name.lower()
                    if ('INT' in ct or 'DECIMAL' in ct or
                        'FLOAT' in ct or 'DOUBLE' in ct):
                        if cn not in ('id',):
                            return c.name
        return None

    def _suggest_chart(self, intent: str,
                       dimensions: List[str],
                       metrics: List[str]) -> Dict:
        """推荐图表"""
        if intent == 'trend':
            return {
                'type': 'line',
                'x_axis': '时间',
                'y_axis': '数量',
                'reason': '时间趋势用折线图',
            }
        if intent == 'distribution':
            return {
                'type': 'pie',
                'reason': '占比用饼图/环形图',
            }
        if intent == 'top':
            return {
                'type': 'bar',
                'reason': '排名用柱状图',
            }
        if intent == 'compare':
            return {
                'type': 'grouped_bar',
                'reason': '对比用分组柱状图',
            }
        return {
            'type': 'table',
            'reason': '明细用表格',
        }

    def _generate_insight(self, question: str, intent: str,
                           rows: List) -> str:
        """生成洞察文字"""
        if not rows:
            return "无数据"

        if intent == 'count':
            try:
                v = rows[0][0]
                return f"共有 **{v:,}** 条记录。"
            except (IndexError, TypeError):
                pass

        if intent == 'trend':
            try:
                values = [r[1] for r in rows if len(r) > 1]
                if len(values) >= 2:
                    latest, prev = values[0], values[1]
                    if prev:
                        delta = (latest - prev) / prev * 100
                        trend = '上升' if delta > 0 else '下降'
                        return (f"最近一天数量 {latest}，"
                                f"较上一期{trend} **{abs(delta):.1f}%**。")
            except (IndexError, TypeError):
                pass

        if intent == 'distribution':
            try:
                if len(rows) >= 2:
                    top = rows[0]
                    return (f"**{top[0]}** 占比最高，"
                            f"共 {top[1]} 条。")
            except (IndexError, TypeError):
                pass

        if intent == 'aggregate':
            try:
                if rows and len(rows[0]) >= 3:
                    return (f"平均值 {rows[0][0]:.2f}，"
                            f"总和 {rows[0][1]:,.2f}，"
                            f"共 {rows[0][2]:,} 条记录。")
            except (IndexError, TypeError):
                pass

        return f"查询返回 {len(rows)} 条结果。"

    def _suggest_follow_ups(self, question: str, intent: str,
                             dimensions: List[str],
                             table: str) -> List[str]:
        """建议追问"""
        suggestions = []

        if intent == 'count':
            suggestions.append(f"{table} 按状态分布")
            suggestions.append(f"{table} 的近期趋势")
        elif intent == 'trend':
            suggestions.append("最近一周的变化")
            suggestions.append("和上个月对比")
        elif intent == 'distribution':
            suggestions.append("每个分类的详细数据")
            suggestions.append("金额占比")
        elif intent == 'top':
            suggestions.append("完整榜单")
            suggestions.append("按金额排序")
        else:
            suggestions.append(f"{table} 有多少条")
            suggestions.append(f"{table} 分布情况")

        return suggestions[:3]

    def export_data_story(self, answers: List[BIAnswer]) -> str:
        """导出数据故事"""
        lines = ["# 数据故事", ""]
        for i, a in enumerate(answers, 1):
            lines.append(f"## {i}. {a.question}")
            lines.append("")
            if a.insight:
                lines.append(a.insight)
                lines.append("")
            lines.append(f"```sql\n{a.sql}\n```")
            lines.append("")
        return '\n'.join(lines)