"""
智能告警引擎
- 阈值告警
- 趋势告警（突增/突降）
- 异常检测（Z-score / IQR）
- 告警抑制与合并
"""
import json
import math
import threading
import time
from collections import deque, defaultdict
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional


ALERTS_FILE = Path.home() / '.lightdb_studio' / 'alert_rules.json'


@dataclass
class AlertRule:
    name: str = ""
    metric: str = "qps"           # qps / connections / slow_queries / table_rows
    # 类型: threshold / trend / anomaly
    rule_type: str = "threshold"
    # 阈值
    operator: str = ">"           # > < >= <=
    threshold: float = 0
    # 趋势：变化率
    change_pct: float = 50        # 50%
    window: int = 5               # 观察窗口（样本数）
    # 异常检测
    method: str = "zscore"        # zscore / iqr
    sensitivity: float = 3.0      # 标准差倍数
    # 触发
    severity: str = "warning"
    enabled: bool = True
    cooldown: int = 300           # 冷却时间（秒）
    labels: Dict = field(default_factory=dict)
    # 通知
    notify_channels: List[str] = field(default_factory=list)
    # 状态
    last_fired: float = 0
    fire_count: int = 0


@dataclass
class Alert:
    rule_name: str = ""
    severity: str = "warning"
    metric: str = ""
    message: str = ""
    value: float = 0
    threshold: float = 0
    fired_at: float = 0
    resolved: bool = False
    resolved_at: float = 0
    extra: Dict = field(default_factory=dict)


class AlertEngine:
    """智能告警引擎"""

    def __init__(self, metrics_provider: Callable = None):
        """
        metrics_provider: () -> dict 返回当前指标快照
        """
        self.file = ALERTS_FILE
        self.metrics_provider = metrics_provider
        self.rules: List[AlertRule] = []
        self.active_alerts: List[Alert] = []
        self.history: deque = deque(maxlen=5000)

        # 历史样本
        self.samples: Dict[str, deque] = defaultdict(lambda: deque(maxlen=60))
        self._callbacks = []
        self._notification_mgr = None
        self._running = False
        self._thread = None

        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def set_notification_manager(self, mgr):
        self._notification_mgr = mgr

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for r in data.get('rules', []):
                self.rules.append(AlertRule(**r))
        except Exception:
            pass

    def save(self):
        data = {'rules': [asdict(r) for r in self.rules]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 规则 ====================

    def add_rule(self, rule: AlertRule):
        self.rules.append(rule)
        self.save()

    def remove_rule(self, name: str):
        self.rules = [r for r in self.rules if r.name != name]
        self.save()

    def add_default_rules(self):
        """添加默认规则"""
        defaults = [
            AlertRule(name='QPS 过高', metric='qps',
                      rule_type='threshold', operator='>',
                      threshold=5000, severity='warning'),
            AlertRule(name='连接数过高', metric='connections',
                      rule_type='threshold', operator='>',
                      threshold=80, severity='warning'),
            AlertRule(name='QPS 突降', metric='qps',
                      rule_type='trend', change_pct=-50,
                      window=5, severity='warning'),
            AlertRule(name='QPS 突增', metric='qps',
                      rule_type='trend', change_pct=100,
                      window=5, severity='warning'),
            AlertRule(name='QPS 异常', metric='qps',
                      rule_type='anomaly', method='zscore',
                      sensitivity=3.0, severity='warning'),
        ]
        for r in defaults:
            if not any(x.name == r.name for x in self.rules):
                self.add_rule(r)

    # ==================== 采集与评估 ====================

    def add_sample(self, metric: str, value: float):
        """添加样本"""
        self.samples[metric].append((time.time(), value))

    def evaluate(self, metric: str, value: float):
        """评估指标，触发告警"""
        now = time.time()
        self.add_sample(metric, value)

        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.metric != metric:
                continue

            # 冷却
            if now - rule.last_fired < rule.cooldown:
                continue

            triggered = False
            msg = ""
            threshold_val = 0

            if rule.rule_type == 'threshold':
                triggered = self._check_threshold(
                    rule, value)
                threshold_val = rule.threshold
                msg = (f"{metric}={value:.2f} {rule.operator} "
                       f"{rule.threshold}")

            elif rule.rule_type == 'trend':
                triggered, msg = self._check_trend(rule, metric, value)

            elif rule.rule_type == 'anomaly':
                triggered, msg = self._check_anomaly(
                    rule, metric, value)

            if triggered:
                self._fire(rule, metric, value, threshold_val, msg)

    def _check_threshold(self, rule: AlertRule, value: float) -> bool:
        op = rule.operator
        t = rule.threshold
        if op == '>': return value > t
        if op == '<': return value < t
        if op == '>=': return value >= t
        if op == '<=': return value <= t
        if op == '==': return value == t
        return False

    def _check_trend(self, rule: AlertRule, metric: str,
                     value: float) -> tuple:
        """趋势检查"""
        window = rule.window
        samples = list(self.samples[metric])[-window * 2:]
        if len(samples) < window + 1:
            return False, ""

        # 前 N 个样本的平均
        prev = samples[:-1]
        prev_avg = sum(v for _, v in prev) / len(prev) if prev else 0
        if prev_avg == 0:
            return False, ""

        change_pct = (value - prev_avg) / prev_avg * 100

        if rule.change_pct > 0 and change_pct >= rule.change_pct:
            return True, (f"{metric} 增长 {change_pct:.1f}% "
                          f"(平均 {prev_avg:.2f} → {value:.2f})")
        if rule.change_pct < 0 and change_pct <= rule.change_pct:
            return True, (f"{metric} 下降 {change_pct:.1f}% "
                          f"(平均 {prev_avg:.2f} → {value:.2f})")
        return False, ""

    def _check_anomaly(self, rule: AlertRule, metric: str,
                       value: float) -> tuple:
        """异常检测"""
        samples = list(self.samples[metric])
        if len(samples) < 10:
            return False, ""

        # 排除最新点
        vals = [v for _, v in samples[:-1]]
        if rule.method == 'zscore':
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = math.sqrt(var)
            if std == 0:
                return False, ""
            z = abs(value - mean) / std
            if z >= rule.sensitivity:
                return True, (f"{metric} 异常值 {value:.2f} "
                              f"(Z-score={z:.2f}, 均值 {mean:.2f})")

        elif rule.method == 'iqr':
            sorted_v = sorted(vals)
            n = len(sorted_v)
            q1 = sorted_v[n // 4]
            q3 = sorted_v[3 * n // 4]
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            if value < lower or value > upper:
                return True, (f"{metric} 超出 IQR 范围 "
                              f"[{lower:.2f}, {upper:.2f}]")

        return False, ""

    def _fire(self, rule: AlertRule, metric: str, value: float,
              threshold: float, msg: str):
        """触发告警"""
        now = time.time()
        rule.last_fired = now
        rule.fire_count += 1

        alert = Alert(
            rule_name=rule.name,
            severity=rule.severity,
            metric=metric,
            message=msg,
            value=value,
            threshold=threshold,
            fired_at=now,
        )
        self.active_alerts.append(alert)
        self.history.append(alert)
        self.save()

        # 通知回调
        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception:
                pass

        # 通知系统
        if self._notification_mgr:
            from .notifications import NotificationMessage
            notify_msg = NotificationMessage(
                title=f"[{rule.severity.upper()}] {rule.name}",
                content=msg,
                level=rule.severity,
                source='alert',
            )
            self._notification_mgr.notify(notify_msg)

    def resolve(self, alert_index: int):
        """标记告警已解决"""
        if 0 <= alert_index < len(self.active_alerts):
            self.active_alerts[alert_index].resolved = True
            self.active_alerts[alert_index].resolved_at = time.time()

    def clear_resolved(self):
        self.active_alerts = [a for a in self.active_alerts
                              if not a.resolved]

    def get_active(self) -> List[Alert]:
        return [a for a in self.active_alerts if not a.resolved]

    def get_history(self, limit: int = 100) -> List[Alert]:
        return list(self.history)[-limit:][::-1]

    def on_alert(self, callback):
        self._callbacks.append(callback)

    def start_monitoring(self, interval: float = 5.0):
        """启动监控循环"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, args=(interval,),
            daemon=True, name='AlertEngine')
        self._thread.start()

    def stop_monitoring(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _monitor_loop(self, interval):
        while self._running:
            try:
                if self.metrics_provider:
                    metrics = self.metrics_provider()
                    for k, v in metrics.items():
                        if isinstance(v, (int, float)):
                            self.evaluate(k, v)
            except Exception:
                pass
            time.sleep(interval)