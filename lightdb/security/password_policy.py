"""
密码策略
- 最小长度
- 大小写/数字/符号组合
- 常见弱密码黑名单
- 密码历史（可选）
"""
import hashlib
import re
import time


WEAK_PASSWORDS = {
    '123456', 'password', '12345678', 'qwerty', '123456789',
    '12345', '1234', '111111', '1234567', 'dragon',
    '123123', 'abc123', 'admin', 'root', 'letmein',
    'welcome', 'monkey', 'passw0rd', 'master', 'hello',
}


class PasswordPolicy:
    def __init__(self, min_length=6, require_upper=False,
                 require_lower=False, require_digit=False,
                 require_symbol=False, deny_weak=True,
                 max_age_days=0):
        self.min_length = min_length
        self.require_upper = require_upper
        self.require_lower = require_lower
        self.require_digit = require_digit
        self.require_symbol = require_symbol
        self.deny_weak = deny_weak
        self.max_age_days = max_age_days

    def validate(self, password: str) -> tuple:
        """返回 (是否合法, 错误消息)"""
        if not isinstance(password, str):
            return False, "密码必须是字符串"

        if len(password) < self.min_length:
            return False, f"密码长度至少 {self.min_length} 位"

        if self.deny_weak and password.lower() in WEAK_PASSWORDS:
            return False, "密码太弱，属于常见弱密码"

        if self.require_upper and not re.search(r'[A-Z]', password):
            return False, "密码必须包含大写字母"

        if self.require_lower and not re.search(r'[a-z]', password):
            return False, "密码必须包含小写字母"

        if self.require_digit and not re.search(r'\d', password):
            return False, "密码必须包含数字"

        if self.require_symbol and not re.search(r'[^A-Za-z0-9]', password):
            return False, "密码必须包含特殊字符"

        return True, None

    def strength(self, password: str) -> int:
        """0-100 强度评分"""
        if not password:
            return 0
        score = 0
        score += min(len(password) * 4, 40)
        if re.search(r'[a-z]', password):
            score += 10
        if re.search(r'[A-Z]', password):
            score += 15
        if re.search(r'\d', password):
            score += 10
        if re.search(r'[^A-Za-z0-9]', password):
            score += 15
        # 惩罚弱密码
        if password.lower() in WEAK_PASSWORDS:
            score = max(0, score - 50)
        # 惩罚重复
        if len(set(password)) < len(password) / 2:
            score = max(0, score - 10)
        return min(100, score)

    def to_dict(self):
        return {
            'min_length': self.min_length,
            'require_upper': self.require_upper,
            'require_lower': self.require_lower,
            'require_digit': self.require_digit,
            'require_symbol': self.require_symbol,
            'deny_weak': self.deny_weak,
            'max_age_days': self.max_age_days,
        }


class PasswordHistory:
    """记录用户密码变更历史"""
    def __init__(self, keep: int = 5):
        self.keep = keep
        self.records = {}  # user -> [hash, ...]

    def add(self, user: str, password: str):
        h = hashlib.sha256(password.encode()).hexdigest()
        lst = self.records.setdefault(user, [])
        lst.append(h)
        if len(lst) > self.keep:
            lst.pop(0)

    def is_reused(self, user: str, password: str) -> bool:
        h = hashlib.sha256(password.encode()).hexdigest()
        return h in self.records.get(user, [])