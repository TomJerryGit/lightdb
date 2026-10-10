"""
全栈可观测
- 血缘 + 追踪 + 日志关联
- 端到端 SLO
- 智能告警关联
"""
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional


@dataclass
class LinkedEvent:
    """关联事件"""
    event_id: str = ""
    event_type: str = ""    # trace / quality_event / slo_breach / alert
    timestamp: float = 0
    # 关联键
    trace_id: str = ""
    table: str = ""
    product_id: str = ""
    user: str = ""
    # 内容
    summary: str = ""
    details: Dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = f"e_{uuid.uuid4().hex[:8]}"
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class CorrelationGroup:
    """关联组"""
    group_id: str = ""
    root_cause_hint: str = ""
    events: List[LinkedEvent] = field(default_factory=list)
    # 影响范围
    affected_tables: List[str] = field(default_factory=list)
    affected_products: List[str] = field(default_factory=list)
    # 时间范围
    start_time: float = 0
    end_time: float = 0
    # 严重度
    severity: str = "warning"
    # 状态
    status: str = "open"        # open / investigating / resolved

    def __post_init__(self):
        if not self.group_id:
            self.group_id = f"g_{uuid.uuid4().hex[:6]}"


class FullStackObservability:
    """全栈可观测引擎"""

    def __init__(self, catalog=None, lineage=None,
                 obs_engine=None, closure=None):
        self.catalog = catalog
        self.lineage = lineage
        self.obs = obs_engine
        self.closure = closure

        # 关联窗口
        self.window_seconds = 600    # 10 分钟
        self.groups: List[CorrelationGroup] = []
        self._events: deque = deque(maxlen=10000)

    # ==================== 事件收集 ====================

    def add_trace_event(self, trace: Dict) -> LinkedEvent:
        """从追踪生成事件"""
        event = LinkedEvent(
            event_type='trace',
            timestamp=trace.get('start_time', time.time()),
            trace_id=trace.get('trace_id', ''),
            user=trace.get('user', ''),
            summary=(f"{trace.get('name', '')} "
                     f"({trace.get('status', '')})"),
            details=trace,
        )
        # 提取资源
        resources = trace.get('resources', [])
        if resources:
            event.table = resources[0] if resources else ''

        self._events.append(event)
        return event

    def add_quality_event(self, event: Dict) -> LinkedEvent:
        """从质量事件生成"""
        le = LinkedEvent(
            event_type='quality_event',
            timestamp=event.get('detected_at', time.time()),
            table=event.get('resource', ''),
            summary=(f"质量事件 {event.get('event_type', '')}: "
                     f"{event.get('detail', '')}"),
            details=event,
        )
        self._events.append(le)
        return le

    def add_slo_breach(self, breach: Dict) -> LinkedEvent:
        """SLO 违约事件"""
        le = LinkedEvent(
            event_type='slo_breach',
            timestamp=breach.get('detected_at', time.time()),
            summary=(f"SLO 违约: {breach.get('name', '')}"),
            details=breach,
        )
        self._events.append(le)
        return le

    def add_alert(self, alert: Dict) -> LinkedEvent:
        """告警事件"""
        le = LinkedEvent(
            event_type='alert',
            timestamp=alert.get('fired_at', time.time()),
            user=alert.get('user', ''),
            summary=(f"告警: {alert.get('rule_name', '')} - "
                     f"{alert.get('message', '')}"),
            details=alert,
        )
        self._events.append(le)
        return le

    # ==================== 关联分析 ====================

    def correlate(self, window_seconds: int = 600) -> List[CorrelationGroup]:
        """
        关联分析：找出时间窗口内互相相关的事件
        """
        if not self._events:
            return []

        now = time.time()
        cutoff = now - window_seconds

        # 过滤近期事件
        recent = [e for e in self._events if e.timestamp >= cutoff]
        recent.sort(key=lambda x: x.timestamp)

        # 分组：按 table / trace_id / product 关联
        groups = []
        used = set()

        for i, event in enumerate(recent):
            if event.event_id in used:
                continue

            group = CorrelationGroup()
            group.events.append(event)
            group.start_time = event.timestamp
            used.add(event.event_id)

            # 查找关联事件
            for j, other in enumerate(recent):
                if other.event_id in used:
                    continue

                # 时间接近
                if abs(other.timestamp - event.timestamp) > 120:
                    continue

                # 关联判定
                if self._is_related(event, other):
                    group.events.append(other)
                    used.add(other.event_id)

            if len(group.events) >= 2:
                group.end_time = max(e.timestamp
                                      for e in group.events)
                group.affected_tables = list({
                    e.table for e in group.events if e.table})
                group.affected_products = list({
                    e.product_id for e in group.events
                    if e.product_id})
                group.severity = self._compute_severity(group)
                group.root_cause_hint = self._infer_root_cause(group)
                groups.append(group)

        self.groups.extend(groups)
        return groups

    def _is_related(self, e1: LinkedEvent,
                    e2: LinkedEvent) -> bool:
        """判断两个事件是否相关"""
        # 相同表
        if e1.table and e1.table == e2.table:
            return True

        # 相同 trace
        if e1.trace_id and e1.trace_id == e2.trace_id:
            return True

        # 相同用户
        if e1.user and e1.user == e2.user:
            return True

        # 血缘相关
        if e1.table and e2.table and self.lineage:
            # e1 → e2 或 e2 → e1 有血缘
            for edge in self.lineage.edges:
                if (edge.from_table == e1.table and
                        edge.to_table == e2.table):
                    return True
                if (edge.to_table == e1.table and
                        edge.from_table == e2.table):
                    return True

        return False

    def _compute_severity(self, group: CorrelationGroup) -> str:
        """计算组严重度"""
        severities = []
        for e in group.events:
            if e.event_type == 'slo_breach':
                severities.append(3)
            elif e.event_type == 'quality_event':
                sev = e.details.get('severity', 'warning')
                severities.append({'critical': 3,
                                    'warning': 2,
                                    'info': 1}.get(sev, 1))
            elif e.event_type == 'alert':
                sev = e.details.get('severity', 'warning')
                severities.append({'critical': 3,
                                    'warning': 2,
                                    'info': 1}.get(sev, 1))
            elif e.event_type == 'trace':
                status = e.details.get('status', 'ok')
                severities.append(3 if status == 'error' else 0)

        if not severities:
            return 'info'

        max_sev = max(severities)
        # 事件数量加权
        if len(group.events) >= 5:
            max_sev = min(3, max_sev + 1)

        return {3: 'critical', 2: 'warning', 1: 'info'}[max_sev]

    def _infer_root_cause(self, group: CorrelationGroup) -> str:
        """推断根因"""
        # 找最早上游事件
        sorted_events = sorted(group.events, key=lambda x: x.timestamp)

        # 优先 SLO / 质量事件
        for e in sorted_events:
            if e.event_type == 'slo_breach':
                return f"疑似 SLO 违约引发后续问题"

        for e in sorted_events:
            if e.event_type == 'quality_event':
                return f"疑似数据质量事件引发：{e.summary}"

        # 失败 trace
        for e in sorted_events:
            if e.event_type == 'trace' and \
               e.details.get('status') == 'error':
                return f"疑似 {e.summary} 引发"

        return "无法确定根因"

    # ==================== 端到端 SLO ====================

    def compute_end_to_end_slo(self, product_id: str,
                                window_hours: int = 24) -> Dict:
        """
        计算端到端 SLO：从数据源到消费方的完整链路
        """
        cutoff = time.time() - window_hours * 3600

        # 找到产品的所有上游
        upstream_products = set()
        if hasattr(self, '_mesh'):
            mesh = self._mesh
            product = mesh.products.get(product_id)
            if product:
                for inp in product.inputs:
                    if inp.get('product_id'):
                        upstream_products.add(inp['product_id'])

        # 收集相关的事件
        related_events = [
            e for e in self._events
            if e.timestamp >= cutoff and (
                e.product_id == product_id or
                e.product_id in upstream_products
            )
        ]

        if not related_events:
            return {
                'product_id': product_id,
                'slo_achieved': True,
                'compliance': 1.0,
                'events': 0,
            }

        # 统计达标率
        breaches = sum(1 for e in related_events
                        if e.event_type in ('slo_breach', 'quality_event'))
        compliance = max(0, 1 - breaches / max(1, len(related_events)))

        return {
            'product_id': product_id,
            'slo_achieved': compliance >= 0.99,
            'compliance': compliance,
            'events': len(related_events),
            'breaches': breaches,
            'window_hours': window_hours,
        }

    # ==================== 查询 ====================

    def get_groups(self, status: str = None,
                    limit: int = 50) -> List[Dict]:
        groups = self.groups
        if status:
            groups = [g for g in groups if g.status == status]
        groups.sort(key=lambda x: -x.start_time)
        return [self._group_to_dict(g)
                 for g in groups[:limit]]

    def _group_to_dict(self, g: CorrelationGroup) -> Dict:
        return {
            'group_id': g.group_id,
            'severity': g.severity,
            'root_cause_hint': g.root_cause_hint,
            'status': g.status,
            'affected_tables': g.affected_tables,
            'affected_products': g.affected_products,
            'start_time': g.start_time,
            'end_time': g.end_time,
            'event_count': len(g.events),
            'events': [asdict(e) for e in g.events[:10]],
        }

    def resolve_group(self, group_id: str):
        for g in self.groups:
            if g.group_id == group_id:
                g.status = 'resolved'
                return True
        return False

    def stats(self) -> dict:
        by_severity = defaultdict(int)
        by_status = defaultdict(int)
        for g in self.groups:
            by_severity[g.severity] += 1
            by_status[g.status] += 1

        return {
            'total_events': len(self._events),
            'total_groups': len(self.groups),
            'groups_by_severity': dict(by_severity),
            'groups_by_status': dict(by_status),
            'open_groups': sum(1 for g in self.groups
                                if g.status == 'open'),
        }