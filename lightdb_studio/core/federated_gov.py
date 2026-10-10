"""
联邦治理
- 集中策略 + 分散执行
- 跨组织合规审计
- 策略继承与冲突解决
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Optional


GOV_FILE = Path.home() / '.lightdb_studio' / 'governance.json'


@dataclass
class Policy:
    """策略"""
    policy_id: str = ""
    name: str = ""
    description: str = ""
    scope: str = "global"        # global / domain / product / connection
    scope_ref: str = ""          # 域/产品/连接名
    policy_type: str = ""        # access / retention / classification / quality / cost
    rule: Dict = field(default_factory=dict)
    # 优先级：数字越大越优先
    priority: int = 0
    # 状态
    enabled: bool = True
    enforced: bool = True        # 违规时是否阻止
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.policy_id:
            self.policy_id = f"p_{int(now*1000)%1000000}"
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


@dataclass
class PolicyViolation:
    policy_id: str
    policy_name: str
    scope: str
    target: str
    severity: str = "warning"
    detail: str = ""
    detected_at: float = 0

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = time.time()


@dataclass
class AuditRecord:
    """审计记录"""
    record_id: str = ""
    timestamp: float = 0
    user: str = ""
    action: str = ""
    resource: str = ""
    result: str = ""            # allow / deny
    policy_applied: str = ""
    reason: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class FederatedGovernance:
    """联邦治理引擎"""

    def __init__(self):
        self.file = GOV_FILE
        self.policies: Dict[str, Policy] = {}
        self.violations: List[PolicyViolation] = []
        self.audit_log: List[AuditRecord] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for pid, pd in data.get('policies', {}).items():
                self.policies[pid] = Policy(**pd)
            for vd in data.get('violations', [])[-500:]:
                self.violations.append(PolicyViolation(**vd))
            for ad in data.get('audit_log', [])[-1000:]:
                self.audit_log.append(AuditRecord(**ad))
        except Exception:
            pass

    def save(self):
        data = {
            'policies': {p: asdict(po)
                          for p, po in self.policies.items()},
            'violations': [asdict(v) for v in self.violations[-500:]],
            'audit_log': [asdict(a) for a in self.audit_log[-1000:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 策略管理 ====================

    def add_policy(self, name: str, policy_type: str,
                   rule: Dict, scope: str = 'global',
                   scope_ref: str = '', priority: int = 0,
                   description: str = '',
                   enforced: bool = True) -> Policy:
        p = Policy(
            name=name,
            description=description,
            scope=scope,
            scope_ref=scope_ref,
            policy_type=policy_type,
            rule=rule,
            priority=priority,
            enforced=enforced,
        )
        self.policies[p.policy_id] = p
        self.save()
        return p

    def remove_policy(self, policy_id: str):
        if policy_id in self.policies:
            del self.policies[policy_id]
            self.save()

    def update_policy(self, policy_id: str, **kwargs):
        p = self.policies.get(policy_id)
        if not p:
            return False
        for k, v in kwargs.items():
            if hasattr(p, k):
                setattr(p, k, v)
        p.updated_at = time.time()
        self.save()
        return True

    # ==================== 策略评估 ====================

    def get_applicable_policies(self, context: Dict) -> List[Policy]:
        """
        获取适用于某上下文的策略
        context: {
            'user': 'alice',
            'action': 'SELECT',
            'resource': 'db.users',
            'domain': '用户中心',
            'connection': 'prod',
        }
        """
        result = []
        for p in self.policies.values():
            if not p.enabled:
                continue

            # 按 scope 过滤
            if p.scope == 'global':
                result.append(p)
            elif p.scope == 'domain' and context.get('domain') == p.scope_ref:
                result.append(p)
            elif p.scope == 'product' and context.get('product') == p.scope_ref:
                result.append(p)
            elif p.scope == 'connection' and context.get('connection') == p.scope_ref:
                result.append(p)

        # 按优先级排序
        return sorted(result, key=lambda x: -x.priority)

    def check_policy(self, context: Dict) -> Dict:
        """
        检查上下文是否违反策略
        返回 {allowed, violations, applied_policies}
        """
        applicable = self.get_applicable_policies(context)
        violations = []
        applied = []

        for policy in applicable:
            applied.append(policy.policy_id)
            violation = self._evaluate_policy(policy, context)
            if violation:
                violations.append(violation)

                # 记录
                self.violations.append(violation)

                # 审计
                self._audit(
                    user=context.get('user', ''),
                    action=context.get('action', ''),
                    resource=context.get('resource', ''),
                    result='deny' if policy.enforced else 'allow',
                    policy_id=policy.policy_id,
                    reason=violation.detail,
                )

        allowed = not any(
            self.policies[v.policy_id].enforced
            for v in violations if v.policy_id in self.policies)

        if not violations:
            self._audit(
                user=context.get('user', ''),
                action=context.get('action', ''),
                resource=context.get('resource', ''),
                result='allow',
                policy_id='',
                reason='',
            )

        if len(self.violations) > 1000:
            self.violations = self.violations[-500:]
        if len(self.audit_log) > 2000:
            self.audit_log = self.audit_log[-1000:]

        self.save()

        return {
            'allowed': allowed,
            'violations': [
                {
                    'policy': v.policy_name,
                    'severity': v.severity,
                    'detail': v.detail,
                }
                for v in violations
            ],
            'applied_policies': applied,
        }

    def _evaluate_policy(self, policy: Policy,
                         context: Dict) -> Optional[PolicyViolation]:
        """评估单个策略"""
        t = policy.policy_type
        rule = policy.rule

        if t == 'access':
            return self._check_access(policy, rule, context)
        elif t == 'retention':
            return None    # 需要数据才能检查
        elif t == 'classification':
            return self._check_classification(policy, rule, context)
        elif t == 'quality':
            return None
        elif t == 'cost':
            return None

        return None

    def _check_access(self, policy: Policy, rule: Dict,
                      context: Dict) -> Optional[PolicyViolation]:
        """访问控制策略"""
        # 规则: {'deny_actions': ['DROP', 'TRUNCATE'], 'resource_pattern': 'prod.*'}
        # 或 {'allow_users': ['alice'], 'resource_pattern': 'db.users'}
        # 或 {'require_conditions': [{'time_start': '09:00', 'time_end': '18:00'}]}

        resource = context.get('resource', '')
        action = context.get('action', '').upper()

        # 检查资源模式
        pattern = rule.get('resource_pattern', '')
        if pattern:
            rx = pattern.replace('*', '.*').replace('?', '.')
            if not re.match('^' + rx + '$', resource):
                return None

        # 拒绝动作
        deny_actions = [a.upper() for a in rule.get('deny_actions', [])]
        if action in deny_actions:
            return PolicyViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                scope=policy.scope,
                target=resource,
                severity='critical',
                detail=f"策略 {policy.name} 禁止 {action} 操作",
            )

        # 用户白名单
        allow_users = rule.get('allow_users', [])
        if allow_users and context.get('user') not in allow_users:
            return PolicyViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                scope=policy.scope,
                target=resource,
                severity='warning',
                detail=f"用户 {context.get('user')} 不在白名单",
            )

        # 用户黑名单
        deny_users = rule.get('deny_users', [])
        if context.get('user') in deny_users:
            return PolicyViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                scope=policy.scope,
                target=resource,
                severity='critical',
                detail=f"用户 {context.get('user')} 已被禁用",
            )

        # 时间窗口
        time_windows = rule.get('time_windows', [])
        if time_windows:
            now_h = time.localtime().tm_hour
            in_window = False
            for w in time_windows:
                start = w.get('start_hour', 0)
                end = w.get('end_hour', 24)
                if start <= now_h < end:
                    in_window = True
                    break
            if not in_window:
                return PolicyViolation(
                    policy_id=policy.policy_id,
                    policy_name=policy.name,
                    scope=policy.scope,
                    target=resource,
                    severity='warning',
                    detail=f"当前时间不在允许窗口",
                )

        # 需要审批的 SQL 类型
        require_approval = [a.upper() for a in
                             rule.get('require_approval', [])]
        if action in require_approval and not context.get('approved'):
            return PolicyViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                scope=policy.scope,
                target=resource,
                severity='warning',
                detail=f"{action} 需要事前审批",
            )

        return None

    def _check_classification(self, policy: Policy, rule: Dict,
                              context: Dict) -> Optional[PolicyViolation]:
        """数据分类策略"""
        # 规则: {'min_classification': 'restricted', 'require_masking': True}
        current = context.get('sensitivity', 'public')
        order = ['public', 'internal', 'confidential', 'restricted', 'pii']

        min_level = rule.get('min_classification', 'public')
        if order.index(current) < order.index(min_level):
            return None    # 未达到触发级别

        if rule.get('require_masking') and not context.get('masked'):
            return PolicyViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                scope=policy.scope,
                target=context.get('resource', ''),
                severity='critical',
                detail=f"敏感级 {current} 需要脱敏",
            )

        return None

    # ==================== 审计 ====================

    def _audit(self, user: str, action: str, resource: str,
               result: str, policy_id: str, reason: str):
        record = AuditRecord(
            user=user,
            action=action,
            resource=resource,
            result=result,
            policy_applied=policy_id,
            reason=reason,
        )
        self.audit_log.append(record)

    def get_audit_log(self, user: str = None, action: str = None,
                      result: str = None, limit: int = 100) -> List[Dict]:
        logs = self.audit_log
        if user:
            logs = [l for l in logs if l.user == user]
        if action:
            logs = [l for l in logs if l.action.upper() == action.upper()]
        if result:
            logs = [l for l in logs if l.result == result]
        return [asdict(l) for l in logs[-limit:][::-1]]

    # ==================== 策略模板 ====================

    def add_default_policies(self):
        """添加一组默认策略"""
        defaults = [
            ('禁止生产环境 DROP', 'access',
             {'resource_pattern': 'prod.*',
              'deny_actions': ['DROP', 'TRUNCATE']},
             'connection', 'prod', 100,
             '生产环境禁止 DROP/TRUNCATE'),
            ('生产写入需审批', 'access',
             {'resource_pattern': 'prod.*',
              'require_approval': ['INSERT', 'UPDATE', 'DELETE', 'ALTER']},
             'connection', 'prod', 90,
             '生产环境写操作需要审批'),
            ('PII 数据必须脱敏', 'classification',
             {'min_classification': 'pii', 'require_masking': True},
             'global', '', 80,
             'PII 级数据导出时必须脱敏'),
            ('工作时间访问控制', 'access',
             {'resource_pattern': 'prod.*',
              'time_windows': [{'start_hour': 8, 'end_hour': 22}]},
             'global', '', 50,
             '仅在工作时间访问生产'),
        ]

        n = 0
        existing_names = {p.name for p in self.policies.values()}
        for name, ptype, rule, scope, ref, prio, desc in defaults:
            if name in existing_names:
                continue
            self.add_policy(name, ptype, rule, scope, ref,
                             prio, desc)
            n += 1
        return n

    def stats(self) -> dict:
        by_type = {}
        for p in self.policies.values():
            by_type[p.policy_type] = by_type.get(p.policy_type, 0) + 1

        # 近期违约
        cutoff = time.time() - 86400
        recent_violations = [v for v in self.violations
                              if v.detected_at >= cutoff]

        return {
            'total_policies': len(self.policies),
            'enabled': sum(1 for p in self.policies.values() if p.enabled),
            'enforced': sum(1 for p in self.policies.values() if p.enforced),
            'by_type': by_type,
            'total_violations': len(self.violations),
            'recent_violations_24h': len(recent_violations),
            'audit_records': len(self.audit_log),
        }