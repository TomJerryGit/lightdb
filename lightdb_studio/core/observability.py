"""
数据可观测性
- 全链路追踪
- 数据质量事件
- 根因分析
"""
import json
import time
import threading
import uuid
from collections import deque, defaultdict
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


OBS_FILE = Path.home() / '.lightdb_studio' / 'observability.json'


@dataclass
class Trace:
    """追踪记录"""
    trace_id: str = ""
    name: str = ""
    # 类型: query / etl / api / agent / policy
    trace_type: str = "query"
    user: str = ""
    start_time: float = 0
    end_time: float = 0
    duration_ms: float = 0
    status: str = "ok"          # ok / error
    error: str = ""
    # 上下文
    attributes: Dict = field(default_factory=dict)
    # Span（子操作）
    spans: List[Dict] = field(default_factory=list)
    # 关联的资源
    resources: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex[:16]
        if not self.start_time:
            self.start_time = time.time()


@dataclass
class QualityEvent:
    """数据质量事件"""
    event_id: str = ""
    event_type: str = ""        # anomaly / breach / recovery
    severity: str = "warning"
    resource: str = ""          # 表或产品
    metric: str = ""
    value: float = 0
    threshold: float = 0
    detail: str = ""
    detected_at: float = 0
    resolved_at: float = 0
    # 关联
    related_traces: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:12]
        if not self.detected_at:
            self.detected_at = time.time()


class ObservabilityEngine:
    """数据可观测性引擎"""

    def __init__(self, max_traces: int = 5000):
        self.file = OBS_FILE
        self.traces: deque = deque(maxlen=max_traces)
        self.events: deque = deque(maxlen=2000)
        self._active_spans: Dict[str, Dict] = {}
        self._callbacks: List[Callable] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)

    # ==================== 追踪 ====================

    def start_trace(self, name: str, trace_type: str = 'query',
                    user: str = '', **attributes) -> str:
        """开始追踪"""
        t = Trace(
            name=name,
            trace_type=trace_type,
            user=user,
            attributes=attributes,
        )
        self._active_spans[t.trace_id] = {
            'trace': t,
            'spans': [],
            'start': time.time(),
        }
        return t.trace_id

    def end_trace(self, trace_id: str, status: str = 'ok',
                  error: str = '', resources: List[str] = None):
        """结束追踪"""
        if trace_id not in self._active_spans:
            return
        ctx = self._active_spans.pop(trace_id)
        t = ctx['trace']
        t.end_time = time.time()
        t.duration_ms = (t.end_time - t.start_time) * 1000
        t.status = status
        t.error = error
        t.resources = resources or []
        t.spans = ctx['spans']

        self.traces.append(t)

        for cb in self._callbacks:
            try:
                cb(t)
            except Exception:
                pass

    def add_span(self, trace_id: str, name: str,
                 duration_ms: float = 0, **attributes):
        """添加子操作"""
        if trace_id not in self._active_spans:
            return
        ctx = self._active_spans[trace_id]
        ctx['spans'].append({
            'name': name,
            'duration_ms': duration_ms,
            'attributes': attributes,
            'ts': time.time(),
        })

    def get_trace(self, trace_id: str) -> Optional[Trace]:
        for t in self.traces:
            if t.trace_id == trace_id:
                return t
        return None

    def recent_traces(self, trace_type: str = None,
                      user: str = None, limit: int = 50) -> List[Dict]:
        traces = list(self.traces)
        if trace_type:
            traces = [t for t in traces if t.trace_type == trace_type]
        if user:
            traces = [t for t in traces if t.user == user]
        return [asdict(t) for t in reversed(traces[-limit:])]

    # ==================== 质量事件 ====================

    def record_event(self, event_type: str, resource: str,
                     metric: str, value: float,
                     threshold: float = 0, severity: str = 'warning',
                     detail: str = '') -> QualityEvent:
        """记录质量事件"""
        event = QualityEvent(
            event_type=event_type,
            severity=severity,
            resource=resource,
            metric=metric,
            value=value,
            threshold=threshold,
            detail=detail,
        )
        self.events.append(event)
        return event

    def resolve_event(self, event_id: str):
        for e in self.events:
            if e.event_id == event_id:
                e.resolved_at = time.time()
                return

    def get_events(self, resource: str = None,
                    resolved: bool = None,
                    limit: int = 50) -> List[Dict]:
        events = list(self.events)
        if resource:
            events = [e for e in events if e.resource == resource]
        if resolved is not None:
            if resolved:
                events = [e for e in events if e.resolved_at > 0]
            else:
                events = [e for e in events if e.resolved_at == 0]
        return [asdict(e) for e in reversed(events[-limit:])]

    # ==================== 根因分析 ====================

    def root_cause_analysis(self, event_id: str) -> Dict:
        """针对某个质量事件进行根因分析"""
        event = None
        for e in self.events:
            if e.event_id == event_id:
                event = e
                break
        if not event:
            return {'error': '事件不存在'}

        result = {
            'event': asdict(event),
            'suspected_traces': [],
            'related_events': [],
            'timeline': [],
            'hypotheses': [],
        }

        # 1. 找时间窗口内的追踪
        window_start = event.detected_at - 600   # 前 10 分钟
        window_end = event.detected_at + 60

        for t in self.traces:
            if not (window_start <= t.start_time <= window_end):
                continue
            # 是否访问了相关资源
            if event.resource in t.resources or \
               any(event.resource in str(r) for r in t.resources):
                result['suspected_traces'].append({
                    'trace_id': t.trace_id,
                    'name': t.name,
                    'duration_ms': t.duration_ms,
                    'status': t.status,
                    'ts': t.start_time,
                })

        # 2. 相关事件（同资源）
        for e in self.events:
            if e.event_id == event.event_id:
                continue
            if e.resource == event.resource:
                result['related_events'].append(asdict(e))

        # 3. 时间线
        timeline = []
        for t in result['suspected_traces']:
            timeline.append({
                'ts': t['ts'],
                'type': 'trace',
                'desc': f"{t['name']} ({t['status']})",
            })
        for e in result['related_events']:
            timeline.append({
                'ts': e['detected_at'],
                'type': 'event',
                'desc': f"{e['event_type']}: {e['detail']}",
            })
        timeline.sort(key=lambda x: x['ts'])
        result['timeline'] = timeline

        # 4. 假设
        hypotheses = []
        if result['suspected_traces']:
            error_traces = [t for t in result['suspected_traces']
                             if t['status'] == 'error']
            if error_traces:
                hypotheses.append({
                    'probability': 0.8,
                    'hypothesis': f"疑似由 {len(error_traces)} 个失败"
                                  f"操作引发",
                    'evidence': [t['name'] for t in error_traces[:3]],
                })
            else:
                hypotheses.append({
                    'probability': 0.5,
                    'hypothesis': "可能由相关操作引起的数据变化",
                    'evidence': [t['name']
                                 for t in result['suspected_traces'][:3]],
                })
        else:
            hypotheses.append({
                'probability': 0.3,
                'hypothesis': "未发现相关操作，可能是数据源本身变化",
            })

        result['hypotheses'] = hypotheses
        return result

    # ==================== 统计 ====================

    def stats(self) -> dict:
        traces = list(self.traces)
        events = list(self.events)

        by_type = defaultdict(int)
        for t in traces:
            by_type[t.trace_type] += 1

        # 平均延迟（按类型）
        latency_by_type = defaultdict(list)
        for t in traces:
            latency_by_type[t.trace_type].append(t.duration_ms)

        avg_latency = {}
        for k, vals in latency_by_type.items():
            avg_latency[k] = sum(vals) / len(vals) if vals else 0

        # 错误率
        error_count = sum(1 for t in traces if t.status == 'error')
        error_rate = error_count / len(traces) if traces else 0

        # 未解决事件
        unresolved = sum(1 for e in events if e.resolved_at == 0)

        return {
            'total_traces': len(traces),
            'traces_by_type': dict(by_type),
            'avg_latency_ms': avg_latency,
            'error_rate': error_rate,
            'total_events': len(events),
            'unresolved_events': unresolved,
        }

    def on_trace(self, callback):
        self._callbacks.append(callback)