"""
数据脱敏
- 按列名规则自动识别敏感列
- 多种脱敏策略：全遮蔽 / 部分遮蔽 / 哈希 / 替换 / 泛化
- 导出时自动应用
"""
import hashlib
import re
import json
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict


MASK_RULES_FILE = Path.home() / '.lightdb_studio' / 'masking.json'


# 常见敏感列名模式
SENSITIVE_PATTERNS = {
    'password': 'full',
    'passwd': 'full',
    'pwd': 'full',
    'token': 'full',
    'secret': 'full',
    'api_key': 'full',
    'private_key': 'full',
    'phone': 'phone',
    'mobile': 'phone',
    'tel': 'phone',
    'email': 'email',
    'mail': 'email',
    'id_card': 'id_card',
    'idcard': 'id_card',
    'identity': 'id_card',
    'ssn': 'id_card',
    'bank_card': 'partial',
    'card_no': 'partial',
    'credit_card': 'partial',
    'address': 'partial',
    'addr': 'partial',
    'salary': 'generalize',
    'income': 'generalize',
    'birthday': 'date',
    'birth_date': 'date',
    'name': 'partial',   # 谨慎，可能误伤
}


@dataclass
class MaskingRule:
    column_pattern: str = ""       # 列名（支持 * 通配）
    strategy: str = "full"         # full/partial/phone/email/id_card/hash/replace/generalize/none
    replacement: str = "***"
    keep_prefix: int = 0           # partial 时保留前 N 位
    keep_suffix: int = 0
    enabled: bool = True

    def matches(self, column: str) -> bool:
        if not self.enabled:
            return False
        pattern = self.column_pattern
        if '*' in pattern or '?' in pattern:
            regex = pattern.replace('*', '.*').replace('?', '.')
            return bool(re.match('^' + regex + '$', column, re.IGNORECASE))
        return column.lower() == pattern.lower()


class MaskingEngine:
    def __init__(self):
        self.rules: List[MaskingRule] = []
        self.load()

    def load(self):
        if MASK_RULES_FILE.exists():
            try:
                with open(MASK_RULES_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.rules = [MaskingRule(**r) for r in data]
            except Exception:
                self.rules = []

    def save(self):
        MASK_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(r) for r in self.rules]
        tmp = MASK_RULES_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(MASK_RULES_FILE)

    def add_rule(self, rule: MaskingRule):
        self.rules.append(rule)
        self.save()

    def remove_rule(self, index):
        if 0 <= index < len(self.rules):
            del self.rules[index]
            self.save()

    def clear(self):
        self.rules.clear()
        self.save()

    def auto_detect_rules(self):
        """根据内置模式生成规则"""
        for pattern, strategy in SENSITIVE_PATTERNS.items():
            self.rules.append(MaskingRule(
                column_pattern=f"*{pattern}*",
                strategy=strategy,
                keep_prefix=2, keep_suffix=2,
            ))
        self.save()
        return len(self.rules)

    def get_strategy(self, column: str):
        for r in self.rules:
            if r.matches(column):
                return r
        return None

    def mask_value(self, value, rule: MaskingRule):
        """对单个值脱敏"""
        if value is None or rule is None:
            return value

        s = str(value)
        if not s:
            return s

        strategy = rule.strategy
        if strategy == 'none':
            return value

        if strategy == 'full':
            return rule.replacement or '***'

        if strategy == 'partial':
            kp = rule.keep_prefix or 0
            ks = rule.keep_suffix or 0
            if kp + ks >= len(s):
                return rule.replacement or '***'
            return s[:kp] + '*' * (len(s) - kp - ks) + (s[-ks:] if ks else '')

        if strategy == 'phone':
            # 138****8888
            if len(s) >= 11:
                return s[:3] + '****' + s[-4:]
            return '***'

        if strategy == 'email':
            # a***@domain.com
            if '@' in s:
                local, domain = s.split('@', 1)
                if len(local) <= 2:
                    masked = '*' * len(local)
                else:
                    masked = local[0] + '*' * (len(local) - 2) + local[-1]
                return f"{masked}@{domain}"
            return '***'

        if strategy == 'id_card':
            if len(s) >= 8:
                return s[:6] + '********' + s[-4:]
            return '***'

        if strategy == 'hash':
            return hashlib.sha256(s.encode()).hexdigest()[:16]

        if strategy == 'replace':
            return rule.replacement or 'REDACTED'

        if strategy == 'generalize':
            # 数值泛化：保留数量级
            try:
                n = float(s)
                if n == 0:
                    return 0
                import math
                mag = 10 ** int(math.log10(abs(n)))
                return int(round(n / mag) * mag)
            except (ValueError, TypeError):
                return '***'

        if strategy == 'date':
            # 只保留年月
            m = re.match(r'(\d{4})-(\d{2})', s)
            if m:
                return f"{m.group(1)}-{m.group(2)}"
            return '****-**'

        return rule.replacement or '***'

    def mask_row(self, columns: List[str], row: List) -> List:
        """对整行脱敏"""
        result = list(row)
        for i, col in enumerate(columns):
            if i >= len(result):
                break
            rule = self.get_strategy(col)
            if rule:
                result[i] = self.mask_value(result[i], rule)
        return result

    def mask_rows(self, columns: List[str], rows: List) -> List:
        return [self.mask_row(columns, r) for r in rows]

    def summary(self):
        return {
            'total_rules': len(self.rules),
            'enabled': sum(1 for r in self.rules if r.enabled),
            'strategies': list({r.strategy for r in self.rules}),
        }