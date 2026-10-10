"""
AI Agent - 智能运维助手
- 自主诊断（数据库体检 + 分析）
- 对话式运维（"为什么这个查询慢？"）
- 智能推荐（索引、优化、告警）
- 自动执行修复
"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Callable, Optional

from .ai_integration import AIClient, AIConfig
from .sql_profiler import SQLProfiler
from .health_score import HealthScorer
from .lineage_enhanced import EnhancedLineageStore


@dataclass
class DiagnosisReport:
    """诊断报告"""
    title: str = ""
    summary: str = ""
    findings: List[Dict] = field(default_factory=list)
    recommendations: List[Dict] = field(default_factory=list)
    severity: str = "info"       # info / warning / critical
    generated_at: float = 0
    ai_analysis: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = time.time()


class AIAgent:
    """AI Agent 智能运维"""

    def __init__(self, conn_getter, ai_config: AIConfig = None,
                 catalog=None, lineage=None):
        self.conn_getter = conn_getter
        self.ai_client = AIClient(ai_config) if ai_config else None
        self.catalog = catalog
        self.lineage = lineage
        self.conversation = []       # 对话历史

    def set_ai_config(self, config: AIConfig):
        self.ai_client = AIClient(config)

    # ==================== 自主诊断 ====================

    def diagnose(self, scope: str = 'full') -> DiagnosisReport:
        """
        自主诊断
        scope: full / performance / security / health
        """
        report = DiagnosisReport(title="数据库诊断报告")

        conn = self.conn_getter()
        if conn is None:
            report.summary = "无可用连接"
            report.severity = "critical"
            return report

        # 1. 性能诊断
        if scope in ('full', 'performance'):
            self._diagnose_performance(conn, report)

        # 2. 安全检查
        if scope in ('full', 'security'):
            self._diagnose_security(conn, report)

        # 3. 健康度
        if scope in ('full', 'health'):
            self._diagnose_health(conn, report)

        # 4. AI 综合分析
        if self.ai_client and self.ai_client.is_enabled():
            try:
                ai_text = self._ai_analyze(report)
                report.ai_analysis = ai_text
            except Exception as e:
                report.ai_analysis = f"AI 分析失败: {e}"

        # 汇总
        critical = sum(1 for f in report.findings if f['level'] == 'critical')
        warning = sum(1 for f in report.findings if f['level'] == 'warning')
        if critical > 0:
            report.severity = 'critical'
        elif warning > 0:
            report.severity = 'warning'

        report.summary = (
            f"发现 {critical} 个严重问题，{warning} 个警告，"
            f"{len(report.recommendations)} 条建议"
        )
        return report

    def _diagnose_performance(self, conn, report: DiagnosisReport):
        """性能诊断"""
        try:
            # 慢查询（如果有日志）
            from ..logger import logger
            slow_count = len(logger.slow_log)
            if slow_count > 0:
                report.findings.append({
                    'level': 'warning',
                    'category': 'performance',
                    'message': f'存在 {slow_count} 条慢查询',
                    'detail': '检查慢查询日志了解详情',
                })
                report.recommendations.append({
                    'action': 'show_slow_queries',
                    'title': '查看慢查询',
                    'description': '分析并优化慢 SQL',
                })

            # QPS
            try:
                cols, rows = conn.query("SHOW STATUS")
                status = dict(rows)
                qps = int(status.get('Questions', 0) or 0)
                connections = int(status.get('Threads_connected', 0) or 0)
                running = int(status.get('Threads_running', 0) or 0)

                if running > 10:
                    report.findings.append({
                        'level': 'warning',
                        'category': 'performance',
                        'message': f'活跃线程数过高 ({running})',
                        'detail': '可能存在锁等待或慢查询',
                    })
                if connections > 80:
                    report.findings.append({
                        'level': 'warning',
                        'category': 'performance',
                        'message': f'连接数偏高 ({connections})',
                        'detail': '考虑调整 max_connections 或使用连接池',
                    })
            except Exception:
                pass

        except Exception:
            pass

    def _diagnose_security(self, conn, report: DiagnosisReport):
        """安全诊断"""
        if not self.catalog:
            return

        # 敏感数据
        sensitive = self.catalog.scan_sensitive()
        pii_count = sum(1 for s in sensitive if s['sensitivity'] == 'pii')
        restricted = sum(1 for s in sensitive
                         if s['sensitivity'] == 'restricted')

        if pii_count > 0:
            report.findings.append({
                'level': 'warning',
                'category': 'security',
                'message': f'发现 {pii_count} 个 PII 字段未标记脱敏',
                'detail': '建议在脱敏规则中启用',
            })
            report.recommendations.append({
                'action': 'add_masking_rule',
                'title': '添加脱敏规则',
                'description': f'为 {pii_count} 个 PII 字段启用脱敏',
            })

        if restricted > 0:
            report.findings.append({
                'level': 'critical',
                'category': 'security',
                'message': f'发现 {restricted} 个敏感字段（密码/密钥）',
                'detail': '确认是否有加密存储',
            })

        # 无主键表
        try:
            tables = conn.get_tables()
            no_pk = []
            for t in tables[:50]:
                try:
                    cols = conn.get_columns(t)
                    if not any(c.get('key') == 'PRI' for c in cols):
                        no_pk.append(t)
                except Exception:
                    pass
            if no_pk:
                report.findings.append({
                    'level': 'warning',
                    'category': 'design',
                    'message': f'{len(no_pk)} 张表没有主键',
                    'detail': f"例如: {', '.join(no_pk[:5])}",
                })
        except Exception:
            pass

    def _diagnose_health(self, conn, report: DiagnosisReport):
        """健康度诊断"""
        try:
            scorer = HealthScorer(conn)
            health = scorer.analyze()
            if health.score < 60:
                report.findings.append({
                    'level': 'critical',
                    'category': 'health',
                    'message': f'数据库健康度偏低 ({health.score}/100)',
                    'detail': f"分类得分: {health.category_scores}",
                })
                report.recommendations.append({
                    'action': 'open_health_dashboard',
                    'title': '查看健康报告',
                    'description': '查看详细问题和修复建议',
                })
            elif health.score < 80:
                report.findings.append({
                    'level': 'warning',
                    'category': 'health',
                    'message': f'数据库健康度中等 ({health.score}/100)',
                })
        except Exception:
            pass

    # ==================== AI 分析 ====================

    def _ai_analyze(self, report: DiagnosisReport) -> str:
        """用 AI 分析诊断结果"""
        findings_text = '\n'.join(
            f"- [{f['level']}] {f['message']}: {f.get('detail', '')}"
            for f in report.findings)

        prompt = f"""你是数据库运维专家。请分析以下诊断结果，给出优先级最高的 3 个问题的详细分析和处理建议。

诊断发现：
{findings_text}

请用中文回答，格式：
1. **问题**: xxx
   **分析**: xxx
   **建议**: xxx
"""
        messages = [{'role': 'user', 'content': prompt}]

        # 同步调用
        import threading
        result = [None]
        error = [None]
        event = threading.Event()

        def on_done(r):
            result[0] = r
            event.set()

        def on_error(e):
            error[0] = e
            event.set()

        self.ai_client.chat(messages, None, on_done, on_error)
        event.wait(timeout=30)

        if error[0]:
            raise RuntimeError(error[0])
        return result[0] or ""

    # ==================== 对话式运维 ====================

    def ask(self, question: str,
            on_done: Callable = None,
            on_error: Callable = None):
        """
        对话式问答
        示例：
        - "为什么 users 表的查询这么慢？"
        - "如何优化 SELECT * FROM orders WHERE user_id=1？"
        - "orders 表可以加什么索引？"
        """
        if not self.ai_client or not self.ai_client.is_enabled():
            if on_error:
                on_error("AI 未配置，请先设置 API")
            return

        # 收集上下文
        context = self._build_context(question)

        # 组装消息
        sys_prompt = self._build_agent_system_prompt()
        user_content = f"问题: {question}\n\n上下文:\n{context}"

        messages = [
            {'role': 'system', 'content': sys_prompt},
            *self.conversation[-4:],  # 保留最近 4 轮
            {'role': 'user', 'content': user_content},
        ]

        def _on_done(response):
            self.conversation.append(
                {'role': 'user', 'content': question})
            self.conversation.append(
                {'role': 'assistant', 'content': response})
            if len(self.conversation) > 20:
                self.conversation = self.conversation[-20:]
            if on_done:
                on_done(response)

        self.ai_client.chat(messages, None, _on_done, on_error)

    def _build_agent_system_prompt(self) -> str:
        return """你是 LightDB Studio 的 AI 运维助手。
你可以：
1. 分析 SQL 性能问题，给出优化建议
2. 推荐索引、表设计优化
3. 解释执行计划
4. 排查慢查询
5. 给出运维建议

回答要求：
- 用中文
- 提供具体可执行的 SQL 语句
- 分步骤给出建议
- 遇到不确定的问题要明确说明"""

    def _build_context(self, question: str) -> str:
        """构建上下文"""
        lines = []

        # 提取表名
        tables = re.findall(r'\b(\w+)\b', question)
        if self.catalog:
            for t in tables:
                for key, meta in self.catalog.tables.items():
                    if meta.name == t:
                        lines.append(
                            f"表 {key}: {meta.row_count} 行, "
                            f"{len(meta.columns)} 列")
                        col_defs = ', '.join(
                            f"{c.name} {c.type}" for c in meta.columns[:15])
                        lines.append(f"  列: {col_defs}")
                        break

        # SQL 上下文
        sql_match = re.search(
            r'(SELECT|INSERT|UPDATE|DELETE|WITH).+?(?=\?|$)',
            question, re.IGNORECASE | re.DOTALL)
        if sql_match:
            sql = sql_match.group(0).strip()
            lines.append(f"\n待分析 SQL:\n{sql}")

            # 尝试执行 EXPLAIN
            conn = self.conn_getter()
            if conn:
                try:
                    profiler = SQLProfiler(conn)
                    result = profiler.profile(sql, execute=False)
                    lines.append(f"\n执行计划:")
                    for p in result.plan:
                        lines.append(f"  {p}")
                    if result.suggestions:
                        lines.append("\n已知建议:")
                        for s in result.suggestions[:3]:
                            lines.append(f"  - {s}")
                except Exception:
                    pass

        return '\n'.join(lines) if lines else "（无上下文）"

    # ==================== 智能推荐 ====================

    def recommend_indexes(self, table: str, db: str = None) -> List[Dict]:
        """推荐索引"""
        if not self.catalog:
            return []

        # 从查询历史中找
        records = []
        for key, meta in self.catalog.tables.items():
            if meta.name == table:
                records = meta.top_queries
                break

        if not records:
            return []

        # 分析 WHERE / ORDER BY / JOIN 条件
        col_usage = {}
        for sql in records:
            # WHERE
            m = re.search(
                r'\bWHERE\s+(.+?)(?=\s+(?:GROUP|ORDER|LIMIT)\b|$)',
                sql, re.IGNORECASE | re.DOTALL)
            if m:
                for cm in re.finditer(r'(\w+)\s*[=<>]', m.group(1)):
                    col = cm.group(1)
                    col_usage[col] = col_usage.get(col, 0) + 3

            # ORDER BY
            m = re.search(r'ORDER\s+BY\s+(\w+)', sql, re.IGNORECASE)
            if m:
                col = m.group(1)
                col_usage[col] = col_usage.get(col, 0) + 2

            # JOIN
            m = re.search(r'ON\s+(\w+)\.(\w+)\s*=', sql, re.IGNORECASE)
            if m:
                col = m.group(2)
                col_usage[col] = col_usage.get(col, 0) + 2

        # 生成推荐
        recommendations = []
        for col, score in sorted(col_usage.items(), key=lambda x: -x[1]):
            if score >= 3:
                recommendations.append({
                    'column': col,
                    'score': score,
                    'sql': f"CREATE INDEX idx_{table}_{col} ON {table}({col})",
                    'reason': f'该列在历史查询中出现 {score} 次',
                })

        return recommendations[:5]

    def recommend_partitioning(self, table: str) -> List[Dict]:
        """推荐分区"""
        if not self.catalog:
            return []

        meta = None
        for key, m in self.catalog.tables.items():
            if m.name == table:
                meta = m
                break

        if not meta or meta.row_count < 1000000:
            return []

        # 找时间列
        time_cols = [c for c in meta.columns
                     if 'date' in c.name.lower()
                     or 'time' in c.name.lower()
                     or c.type.upper() in ('DATE', 'DATETIME',
                                           'TIMESTAMP')]
        if time_cols:
            col = time_cols[0].name
            return [{
                'type': 'range',
                'column': col,
                'sql': f"""ALTER TABLE {table}
PARTITION BY RANGE (YEAR({col})) (
  PARTITION p2023 VALUES LESS THAN (2024),
  PARTITION p2024 VALUES LESS THAN (2025),
  PARTITION p2025 VALUES LESS THAN (2026),
  PARTITION pmax VALUES LESS THAN MAXVALUE
)""",
                'reason': f'表有 {meta.row_count:,} 行，建议按 {col} 分区',
            }]
        return []