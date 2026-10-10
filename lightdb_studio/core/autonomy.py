"""
智能自治
- 自愈能力（自动修复）
- 自适应优化（基于指标调整）
- 主动推荐（预测问题）
"""
import json
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional


AUTO_FILE = __import__('pathlib').Path.home() / \
    '.lightdb_studio' / 'autonomy.json'


@dataclass
class HealingAction:
    """自愈动作"""
    action_id: str = ""
    trigger: str = ""            # 触发条件
    action_type: str = ""        # kill_query / drop_shadow / flush_cache / ...
    target: str = ""
    description: str = ""
    # 状态
    status: str = "pending"      # pending / running / success / failed / skipped
    result: str = ""
    executed_at: float = 0
    # 自动化
    auto_execute: bool = False
    requires_confirmation: bool = True


@dataclass
class AutoRule:
    """自治规则"""
    rule_id: str = ""
    name: str = ""
    # 条件
    metric: str = ""
    condition: str = ""           # '>' '<' '==' etc
    threshold: float = 0
    # 动作
    action_type: str = ""
    action_params: Dict = field(default_factory=dict)
    # 执行模式
    mode: str = "suggest"         # suggest / auto / confirm
    cooldown: int = 300
    # 状态
    enabled: bool = True
    last_triggered: float = 0
    trigger_count: int = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.rule_id:
            import uuid
            self.rule_id = f"r_{uuid.uuid4().hex[:6]}"
        if not self.created_at:
            self.created_at = time.time()


class AutonomyEngine:
    """智能自治引擎"""

    def __init__(self, conn_provider=None,
                 notification_mgr=None):
        self.file = AUTO_FILE
        self.conn_provider = conn_provider
        self.notification_mgr = notification_mgr
        self.rules: Dict[str, AutoRule] = {}
        self.actions_history: List[HealingAction] = []
        self.metrics_buffer: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100))

        self._callbacks: List[Callable] = []
        self._running = False
        self._thread = None

        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for rid, rd in data.get('rules', {}).items():
                self.rules[rid] = AutoRule(**rd)
            for ah in data.get('actions', [])[-500:]:
                self.actions_history.append(HealingAction(**ah))
        except Exception:
            pass

    def save(self):
        data = {
            'rules': {r: asdict(rl) for r, rl in self.rules.items()},
            'actions': [asdict(a)
                         for a in self.actions_history[-200:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 规则管理 ====================

    def add_rule(self, name: str, metric: str, condition: str,
                 threshold: float, action_type: str,
                 action_params: Dict = None,
                 mode: str = 'suggest',
                 cooldown: int = 300) -> AutoRule:
        r = AutoRule(
            name=name,
            metric=metric,
            condition=condition,
            threshold=threshold,
            action_type=action_type,
            action_params=action_params or {},
            mode=mode,
            cooldown=cooldown,
        )
        self.rules[r.rule_id] = r
        self.save()
        return r

    def remove_rule(self, rule_id: str):
        if rule_id in self.rules:
            del self.rules[rule_id]
            self.save()

    def update_rule(self, rule_id: str, **kwargs):
        r = self.rules.get(rule_id)
        if not r:
            return False
        for k, v in kwargs.items():
            if hasattr(r, k):
                setattr(r, k, v)
        self.save()
        return True

    def add_default_rules(self):
        """添加默认规则"""
        defaults = [
            ('长时间空转连接', 'idle_connections', '>', 50,
             'notify', {'message': '连接数过高'}, 'suggest', 600),
            ('慢查询过多', 'slow_queries', '>', 10,
             'kill_slow', {}, 'confirm', 300),
            ('CPU 高负载', 'cpu_usage', '>', 0.9,
             'scale_suggest', {}, 'suggest', 900),
            ('QPS 骤降', 'qps_drop_pct', '>', 50,
             'investigate', {}, 'suggest', 600),
            ('影子表过期', 'expired_shadows', '>', 0,
             'cleanup_shadows', {}, 'auto', 3600),
        ]
        n = 0
        existing_names = {r.name for r in self.rules.values()}
        for name, metric, cond, thr, action, params, mode, cool in defaults:
            if name in existing_names:
                continue
            self.add_rule(name, metric, cond, thr, action, params,
                          mode, cool)
            n += 1
        return n

    # ==================== 评估 ====================

    def evaluate(self, metrics: Dict) -> List[HealingAction]:
        """根据指标评估触发动作"""
        triggered = []
        now = time.time()

        # 记录历史
        for k, v in metrics.items():
            if isinstance(v, (int, float)):
                self.metrics_buffer[k].append((now, v))

        for rule in self.rules.values():
            if not rule.enabled:
                continue

            # 冷却检查
            if now - rule.last_triggered < rule.cooldown:
                continue

            value = metrics.get(rule.metric)
            if value is None:
                continue

            if self._check_condition(value, rule.condition,
                                       rule.threshold):
                rule.last_triggered = now
                rule.trigger_count += 1

                action = HealingAction(
                    action_id=f"a_{int(now*1000)%1000000}",
                    trigger=rule.name,
                    action_type=rule.action_type,
                    target=rule.metric,
                    description=f"{rule.name}: {rule.metric}="
                                f"{value} {rule.condition} "
                                f"{rule.threshold}",
                )
                triggered.append(action)

                # 根据 mode 自动执行
                if rule.mode == 'auto':
                    self._execute_action(action, rule)
                elif rule.mode == 'suggest':
                    action.status = 'pending'

                self.actions_history.append(action)

                # 通知
                for cb in self._callbacks:
                    try:
                        cb(action, rule)
                    except Exception:
                        pass

        if triggered:
            self.save()

        return triggered

    def _check_condition(self, value: float, cond: str,
                         threshold: float) -> bool:
        try:
            if cond == '>': return value > threshold
            if cond == '<': return value < threshold
            if cond == '>=': return value >= threshold
            if cond == '<=': return value <= threshold
            if cond == '==': return value == threshold
            if cond == '!=': return value != threshold
        except Exception:
            pass
        return False

    # ==================== 自愈动作 ====================

    def execute_action(self, action: HealingAction,
                       rule: AutoRule = None) -> Dict:
        """执行自愈动作"""
        action.status = 'running'
        action.executed_at = time.time()

        try:
            result = self._do_action(action, rule)
            action.status = 'success' if result.get('ok') else 'failed'
            action.result = json.dumps(result, ensure_ascii=False,
                                        default=str)
        except Exception as e:
            action.status = 'failed'
            action.result = str(e)

        self.save()
        return {'status': action.status, 'result': action.result}

    def _execute_action(self, action: HealingAction,
                        rule: AutoRule):
        return self.execute_action(action, rule)

    def _do_action(self, action: HealingAction,
                   rule: AutoRule) -> Dict:
        """实际执行"""
        t = action.action_type

        if t == 'notify':
            if self.notification_mgr:
                from .notifications import NotificationMessage
                msg = NotificationMessage(
                    title=f"自治告警: {action.trigger}",
                    content=action.description,
                    level='warning',
                    source='autonomy',
                )
                self.notification_mgr.notify(msg)
            return {'ok': True, 'message': '已发送通知'}

        elif t == 'kill_slow':
            conn = self.conn_provider('default') \
                if self.conn_provider else None
            if not conn:
                return {'ok': False, 'error': '无连接'}
            killed = 0
            try:
                cols, rows = conn.query("SHOW PROCESSLIST")
                # 找运行时间长的
                for r in rows:
                    # 假设最后几列包含 Time 和 Info
                    try:
                        # 简化：kill 所有超过 60 秒的
                        for i, c in enumerate(cols):
                            if c.lower() == 'time':
                                if int(r[i]) > 60:
                                    # 找 id 列
                                    for j, cj in enumerate(cols):
                                        if cj.lower() == 'id':
                                            conn.execute(
                                                f"KILL {r[j]}")
                                            killed += 1
                                            break
                                break
                    except (ValueError, IndexError):
                        pass
                return {'ok': True, 'killed': killed}
            except Exception as e:
                return {'ok': False, 'error': str(e)}

        elif t == 'cleanup_shadows':
            # 需要 ShadowTableManager
            return {'ok': True, 'message': '建议手动清理影子表'}

        elif t == 'scale_suggest':
            if self.notification_mgr:
                from .notifications import NotificationMessage
                msg = NotificationMessage(
                    title="扩缩容建议",
                    content="CPU 持续高负载，建议扩容",
                    level='info',
                    source='autonomy',
                )
                self.notification_mgr.notify(msg)
            return {'ok': True, 'message': '已生成扩容建议'}

        elif t == 'investigate':
            # 启动调查
            return {'ok': True, 'message': '已记录调查任务'}

        return {'ok': False, 'error': f'未知动作: {t}'}

    def approve_action(self, action_id: str) -> Dict:
        """人工批准动作"""
        for a in self.actions_history:
            if a.action_id == action_id:
                rule = next((r for r in self.rules.values()
                              if r.name == a.trigger), None)
                return self.execute_action(a, rule)
        return {'ok': False, 'error': 'action not found'}

    def skip_action(self, action_id: str):
        for a in self.actions_history:
            if a.action_id == action_id:
                a.status = 'skipped'
                self.save()
                return True
        return False

    # ==================== 自适应优化 ====================

    def analyze_trends(self) -> List[Dict]:
        """分析趋势，给出优化建议"""
        suggestions = []

        for metric, samples in self.metrics_buffer.items():
            if len(samples) < 10:
                continue

            values = [v for _, v in samples]
            n = len(values)
            first_half = sum(values[:n // 2]) / max(1, n // 2)
            second_half = sum(values[n // 2:]) / max(1, n - n // 2)

            if first_half > 0:
                change = (second_half - first_half) / first_half
                if change > 0.5:
                    suggestions.append({
                        'metric': metric,
                        'trend': 'increasing',
                        'change_pct': change * 100,
                        'suggestion': f"{metric} 持续上升，建议提前扩容"
                                       f"或优化",
                    })
                elif change < -0.3:
                    suggestions.append({
                        'metric': metric,
                        'trend': 'decreasing',
                        'change_pct': change * 100,
                        'suggestion': f"{metric} 持续下降，可能存在问题",
                    })

        return suggestions

    def predict_issues(self) -> List[Dict]:
        """预测潜在问题"""
        issues = []

        for metric, samples in self.metrics_buffer.items():
            if len(samples) < 20:
                continue
            values = [v for _, v in samples]

            # 简单线性外推
            n = len(values)
            xs = list(range(n))
            mean_x = sum(xs) / n
            mean_y = sum(values) / n
            num = sum((xs[i] - mean_x) * (values[i] - mean_y)
                      for i in range(n))
            den = sum((x - mean_x) ** 2 for x in xs)
            if den == 0:
                continue
            slope = num / den

            # 预测未来 10 期
            predicted = slope * (n + 10) + (mean_y - slope * mean_x)

            # 阈值检查
            if 'usage' in metric and predicted > 0.9:
                issues.append({
                    'metric': metric,
                    'current': values[-1],
                    'predicted': predicted,
                    'period': '10 期后',
                    'severity': 'warning',
                    'message': f"{metric} 预计将超过 90%",
                })

        return issues

    def on_action(self, callback):
        self._callbacks.append(callback)

    # ==================== 后台监控 ====================

    def start_monitoring(self, metrics_provider: Callable,
                          interval: float = 30):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            args=(metrics_provider, interval),
            daemon=True, name='AutonomyEngine')
        self._thread.start()

    def stop_monitoring(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _monitor_loop(self, provider, interval):
        while self._running:
            try:
                metrics = provider()
                if metrics:
                    self.evaluate(metrics)
            except Exception:
                pass
            time.sleep(interval)

    def stats(self) -> dict:
        by_status = defaultdict(int)
        for a in self.actions_history:
            by_status[a.status] += 1

        return {
            'total_rules': len(self.rules),
            'enabled_rules': sum(1 for r in self.rules.values()
                                  if r.enabled),
            'total_actions': len(self.actions_history),
            'actions_by_status': dict(by_status),
            'metrics_tracked': len(self.metrics_buffer),
        }