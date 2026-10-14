"""
联邦治理
- 集中策略 + 分散执行
- 跨组织合规审计
- 策略继承与冲突解决
"""
import json
import re
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Optional


GOV_FILE = Path.home() / '.lightdb_studio' / 'governance.json'


def new_policy_id() -> str:
    """生成策略 id

    **必须是不会碰撞的**：旧实现是 `f"p_{int(time.time()*1000) % 1000000}"`
    —— 毫秒时间戳取模，同一毫秒内的两次 `add_policy` 必然同 id，随后
    `self.policies[id] = p` 把前一条策略**静默覆盖**（实测：同毫秒连续
    添加"禁止生产 DROP" + "工作时间限制" → `policies=1`，
    `check_policy('DROP')` 返回 `allowed=True`，即安全策略凭空消失）。
    而且取模 1e6 让 id 每 1000 秒就整段重复一次。

    改用 uuid4（122 位随机）：与时钟刻度、进程重启、调用频率都无关。
    """
    return f"p_{uuid.uuid4().hex}"


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
            self.policy_id = new_policy_id()
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
        """读取策略库

        **失败必须显式报错，不能静默变成"没有策略"**：治理引擎的默认决策
        是"放行"，静默丢掉策略库 = 所有限制无声失效（`DROP prod.*` 立刻
        被允许）。旧实现 `except Exception: pass` 正是这样 fail-open 的。
        只有"文件不存在"（首次使用）才算空库。
        """
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            raise RuntimeError(
                f"策略库 {self.file} 无法读取（{type(e).__name__}: {e}）。"
                f"拒绝以降级为空策略的方式启动：那会让所有治理限制静默失效。"
                f"请修复或移走该文件后重试。")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"策略库 {self.file} 结构不合法（顶层应为对象，实际是 "
                f"{type(data).__name__}）：拒绝静默降级为空策略。")
        try:
            for pid, pd in (data.get('policies') or {}).items():
                self.policies[pid] = Policy(**pd)
            for vd in (data.get('violations') or [])[-500:]:
                self.violations.append(PolicyViolation(**vd))
            for ad in (data.get('audit_log') or [])[-1000:]:
                self.audit_log.append(AuditRecord(**ad))
        except Exception as e:
            raise RuntimeError(
                f"策略库 {self.file} 内容无法解析（{type(e).__name__}: {e}）；"
                f"已加载 {len(self.policies)} 条策略后放弃。"
                f"拒绝带着不完整的策略集继续运行（缺失的策略会被当成"
                f"'没有这条限制'）。")

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
        """新增一条策略

        **绝不覆盖既有策略**：这里用 `policies[id] = p` 落库，因此 id 一旦
        重复，前一条策略就被静默抹掉（旧实现的毫秒时间戳 id 正是如此，
        "禁止生产 DROP"被后来的"工作时间限制"顶掉）。除了把 id 生成换成
        uuid4，这里再加一道去重兜底：即使将来 id 生成方式被改回可能碰撞的
        实现，也只会重新生成 id，不会覆盖任何已有策略。

        对**同名**策略：本方法让两条同时存在（不做静默替换）。同名覆盖
        与 id 撞号是同一类"静默丢失"——审计上一条限制忽然变了内容却看不
        出痕迹。需要改既有策略请用 `update_policy(policy_id, **kwargs)`。
        """
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
        for _ in range(8):
            if p.policy_id not in self.policies:
                break
            p.policy_id = new_policy_id()
        else:
            raise RuntimeError(
                f"无法为策略 {name!r} 生成唯一 id（连续 8 次碰撞）；"
                f"拒绝覆盖既有策略 {p.policy_id!r}")
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

        # 违规的判定：只要有**任何一条强制执行的策略**被违反就不放行。
        # 策略记录查不到时按"强制执行"处理 —— 违规已经产生了，此时默认
        # 放行才是危险的（旧实现 `if v.policy_id in self.policies` 会把
        # 查不到记录的违规直接跳过，等于 fail-open）。
        denied = False
        for v in violations:
            pol = self.policies.get(v.policy_id)
            if pol is None or pol.enforced:
                denied = True
                break
        allowed = not denied

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