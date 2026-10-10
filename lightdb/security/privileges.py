"""
权限系统 - 支持用户、角色、细粒度授权
对标 MySQL 权限模型：
  - 全局权限: ALL / SUPER / PROCESS
  - 库级:  SCHEMA.SELECT
  - 表级:  db.table.SELECT
  - 列级:  暂不支持
"""
import hashlib
import json
import threading
from pathlib import Path


# 可用权限列表
PRIV_SELECT = 'SELECT'
PRIV_INSERT = 'INSERT'
PRIV_UPDATE = 'UPDATE'
PRIV_DELETE = 'DELETE'
PRIV_CREATE = 'CREATE'
PRIV_DROP = 'DROP'
PRIV_ALTER = 'ALTER'
PRIV_INDEX = 'INDEX'
PRIV_GRANT = 'GRANT'
PRIV_SUPER = 'SUPER'
PRIV_PROCESS = 'PROCESS'
PRIV_REPLICATION = 'REPLICATION'
PRIV_ALL = 'ALL'

ALL_PRIVS = {
    PRIV_SELECT, PRIV_INSERT, PRIV_UPDATE, PRIV_DELETE,
    PRIV_CREATE, PRIV_DROP, PRIV_ALTER, PRIV_INDEX,
    PRIV_GRANT, PRIV_SUPER, PRIV_PROCESS, PRIV_REPLICATION,
}


def hash_password(password: str) -> str:
    """MySQL native password 存储哈希"""
    if not password:
        return ''
    s1 = hashlib.sha1(password.encode()).digest()
    s2 = hashlib.sha1(s1).digest()
    return s2.hex()


class User:
    __slots__ = ('name', 'host', 'auth_hash', 'privs', 'is_locked')

    def __init__(self, name, auth_hash='', host='%'):
        self.name = name
        self.host = host
        self.auth_hash = auth_hash
        self.privs = set()      # 全局权限
        self.is_locked = False

    def to_dict(self):
        return {
            'name': self.name,
            'host': self.host,
            'auth_hash': self.auth_hash,
            'privs': sorted(self.privs),
            'is_locked': self.is_locked,
        }

    @classmethod
    def from_dict(cls, d):
        u = cls(d['name'], d.get('auth_hash', ''), d.get('host', '%'))
        u.privs = set(d.get('privs', []))
        u.is_locked = d.get('is_locked', False)
        return u


class PrivilegeManager:
    """权限管理器"""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / '_users.json'
        self.lock = threading.RLock()
        self.users = {}

        # 内置 root 用户
        self.users['root'] = User('root', hash_password(''), '%')
        self.users['root'].privs = {PRIV_ALL}

        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for name, d in raw.items():
                self.users[name] = User.from_dict(d)
        except Exception:
            pass

    def save(self):
        with self.lock:
            raw = {name: u.to_dict() for name, u in self.users.items()}
            tmp = self.path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    # ============ 用户管理 ============
    def create_user(self, name: str, password: str = '', host: str = '%',
                    if_not_exists: bool = False) -> bool:
        with self.lock:
            if name in self.users:
                if if_not_exists:
                    return False
                raise ValueError(f"用户 {name}@{host} 已存在")
            u = User(name, hash_password(password), host)
            self.users[name] = u
            self.save()
            return True

    def drop_user(self, name: str) -> bool:
        with self.lock:
            if name not in self.users:
                raise ValueError(f"用户 {name} 不存在")
            if name == 'root':
                raise ValueError("不能删除 root 用户")
            del self.users[name]
            self.save()
            return True

    def set_password(self, name: str, password: str):
        with self.lock:
            if name not in self.users:
                raise ValueError(f"用户 {name} 不存在")
            self.users[name].auth_hash = hash_password(password)
            self.save()

    def authenticate(self, name: str, password: str) -> bool:
        with self.lock:
            u = self.users.get(name)
            if u is None:
                return False
            if u.is_locked:
                return False
            return u.auth_hash == hash_password(password)

    # ============ 权限授予 ============
    def grant(self, user: str, privs: list, table: str = '*.*') -> int:
        with self.lock:
            u = self.users.get(user)
            if u is None:
                # 自动创建
                u = User(user, hash_password(''), '%')
                self.users[user] = u
            n = 0
            for p in privs:
                p = p.upper()
                if p == PRIV_ALL:
                    for ap in ALL_PRIVS:
                        u.privs.add(ap)
                    n = len(ALL_PRIVS)
                elif p in ALL_PRIVS:
                    u.privs.add(p)
                    n += 1
                else:
                    raise ValueError(f"未知权限: {p}")
            self.save()
            return n

    def revoke(self, user: str, privs: list) -> int:
        with self.lock:
            u = self.users.get(user)
            if u is None:
                raise ValueError(f"用户 {user} 不存在")
            n = 0
            for p in privs:
                p = p.upper()
                if p == PRIV_ALL:
                    u.privs.clear()
                    n = len(ALL_PRIVS)
                elif p in u.privs:
                    u.privs.discard(p)
                    n += 1
            self.save()
            return n

    # ============ 权限检查 ============
    def check(self, user: str, priv: str, table: str = None) -> bool:
        """检查用户是否有某权限"""
        u = self.users.get(user)
        if u is None:
            return False
        if PRIV_ALL in u.privs or PRIV_SUPER in u.privs:
            return True
        return priv.upper() in u.privs

    def list_users(self):
        return list(self.users.keys())

    def show_grants(self, user: str) -> list:
        u = self.users.get(user)
        if u is None:
            return []
        if PRIV_ALL in u.privs or len(u.privs) >= len(ALL_PRIVS) - 1:
            return [f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'"]
        grants = []
        for p in sorted(u.privs):
            grants.append(f"GRANT {p} ON *.* TO '{user}'@'%'")
        return grants