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


# ==================== 引擎运行期权限模型（P1 阶段 1.1） ====================
#
# 上面 `PrivilegeManager` 是**旧的独立实现**（自己的 `_users.json`），与
# 登录/握手用的 `Database.users` 不是同一份数据，因此它从来没有真正影响
# 过引擎行为（登录即全权）。下面这一套是**运行期权限模型**：
#
# * 事实来源：`Database.users[user]['privs']`（与握手共用同一份用户表，
#   随 `_meta.json` 持久化）；
# * 存储格式（列表元素均为字符串，兼容既有 `['ALL']` 形态）：
#       'ALL'             全局所有权限（root 的内置形态，同时视为管理员）
#       'SELECT'          全局 SELECT（等价 GRANT SELECT ON *.*）
#       'SELECT@default'  库级 SELECT（等价 GRANT SELECT ON default.*）
#   单库模型下库名只接受 `Database.database_names()` 里的真实库；
# * 支持权限：ALL / SELECT / INSERT / UPDATE / DELETE / CREATE / DROP /
#   ALTER / INDEX；列级权限**不支持**（解析期显式 1064，绝不静默忽略）。

#: 本模型支持的权限名（ALL 是聚合名，不是可单独授予的"粒度"）
SUPPORTED_PRIVS = (
    'ALL', 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
    'CREATE', 'DROP', 'ALTER', 'INDEX',
)
#: 可以出现在 `GRANT <priv> ON <scope>` 里的权限名别名
_PRIV_ALIASES = {
    'ALL': 'ALL',
    'ALL PRIVILEGES': 'ALL',
    'SELECT': 'SELECT',
    'INSERT': 'INSERT',
    'UPDATE': 'UPDATE',
    'DELETE': 'DELETE',
    'CREATE': 'CREATE',
    'DROP': 'DROP',
    'ALTER': 'ALTER',
    'INDEX': 'INDEX',
}
#: 可授予到 **库级** 的权限（ALL 会展开成这些）
GRANTABLE_PRIVS = tuple(p for p in SUPPORTED_PRIVS if p != 'ALL')
#: 全局作用域标记（`*.*`）
GLOBAL_SCOPE = '*'


def normalize_priv(token) -> str:
    """权限名规范化：`all privileges` / `All` → `ALL`；不认识的返回原大写。"""
    raw = str(token or '').strip()
    key = raw.upper().replace('_', ' ')
    while '  ' in key:
        key = key.replace('  ', ' ')
    return _PRIV_ALIASES.get(key, key)


def normalize_scope(database) -> str:
    """作用域规范化：None/空/`*`/`*.*` → '*'（全局），否则库名小写"""
    db = str(database if database is not None else '').strip().strip('`"')
    if db in ('', '*', '*.*'):
        return GLOBAL_SCOPE
    return db.lower()


def split_priv_entry(entry):
    """存储项 → `(权限, 作用域)`；`'SELECT@default'` → `('SELECT','default')`"""
    s = str(entry or '').strip()
    if '@' in s:
        priv, db = s.split('@', 1)
        return normalize_priv(priv), normalize_scope(db)
    return normalize_priv(s), GLOBAL_SCOPE


def join_priv_entry(priv, database) -> str:
    """`(权限, 作用域)` → 存储项字符串"""
    p = normalize_priv(priv)
    db = normalize_scope(database)
    return p if db == GLOBAL_SCOPE else f'{p}@{db}'


def privilege_entries(record) -> list:
    """用户记录 → 规范化后的权限项列表（去重、保序）"""
    if not isinstance(record, dict):
        return []
    out, seen = [], set()
    for entry in (record.get('privs') or []):
        normalized = join_priv_entry(*split_priv_entry(entry))
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _split_entries(record):
    """→ `(全局权限集合, {库名: 权限集合})`"""
    globals_, scoped = set(), {}
    for entry in privilege_entries(record):
        priv, db = split_priv_entry(entry)
        if db == GLOBAL_SCOPE:
            globals_.add(priv)
        else:
            scoped.setdefault(db, set()).add(priv)
    return globals_, scoped


def is_admin(users, user) -> bool:
    """管理员判定：root，或持有**全局** ALL。

    库级 `GRANT ALL ON db.*` 只在那个库内全权，**不会**变成管理员
    （否则一条库级授权就能改用户/回收权限）。
    """
    if not user:
        return False
    if str(user) == 'root':
        return True
    globals_, _ = _split_entries((users or {}).get(user))
    return 'ALL' in globals_


def has_privilege(users, user, priv, database=None) -> bool:
    """用户是否持有某权限（全局优先，其次是 `database` 上的库级授权）"""
    if not user:
        return False
    record = (users or {}).get(user)
    if not isinstance(record, dict):
        return False
    globals_, scoped = _split_entries(record)
    if 'ALL' in globals_:
        return True
    priv = normalize_priv(priv)
    if priv in globals_:
        return True
    if database is not None:
        db_privs = scoped.get(normalize_scope(database))
        if db_privs and ('ALL' in db_privs or priv in db_privs):
            return True
    return False


def has_any_privilege_on_db(users, user, database) -> bool:
    """用户对某库是否有**任意**权限（`USE db` / `SHOW TABLES` 的准入依据）"""
    if not user:
        return False
    record = (users or {}).get(user)
    if not isinstance(record, dict):
        return False
    globals_, scoped = _split_entries(record)
    if globals_:
        return True          # 任一全局权限都覆盖所有库
    return bool(scoped.get(normalize_scope(database)))


def grant_privileges(users, user, privs, database=None) -> int:
    """把 `privs` 授予用户（就地修改用户记录，返回新增项数）。

    `database=None/'*'` = 全局（`*.*`）；库级 ALL 会展开成
    `GRANTABLE_PRIVS` 的库级项，不会写成全局 ALL（权限提升风险）。
    用户不存在时抛 `KeyError`（由调用方转成 MySQL 1410）。
    """
    record = (users or {}).get(user)
    if not isinstance(record, dict):
        raise KeyError(user)
    db = normalize_scope(database)
    current = privilege_entries(record)
    current_set = set(current)
    requested = [normalize_priv(p) for p in privs]
    if db == GLOBAL_SCOPE and 'ALL' in requested:
        new_entries = ['ALL']
    else:
        names = []
        for p in requested:
            if p == 'ALL':
                names.extend(GRANTABLE_PRIVS)
            elif p in GRANTABLE_PRIVS:
                names.append(p)
            else:
                raise ValueError(f"未知权限: {p}")
        new_entries = [join_priv_entry(p, db) for p in names]
    added = 0
    for entry in new_entries:
        if entry not in current_set:
            current.append(entry)
            current_set.add(entry)
            added += 1
    record['privs'] = current
    return added


def revoke_privileges(users, user, privs, database=None) -> int:
    """回收权限（就地修改用户记录，返回实际移除项数）。

    * `REVOKE ALL ON *.*`：移除**全部全局**权限（库级授权不受影响，
      与 MySQL 一致）；
    * `REVOKE ALL ON db.*`：移除该库的全部库级权限；
    * 具体权限：移除对应存储项（没授予过则不报错，MySQL 同为 warning）。
    """
    record = (users or {}).get(user)
    if not isinstance(record, dict):
        raise KeyError(user)
    db = normalize_scope(database)
    requested = [normalize_priv(p) for p in privs]
    removed = 0
    kept = []
    for entry in privilege_entries(record):
        priv, entry_db = split_priv_entry(entry)
        drop = False
        if db == GLOBAL_SCOPE:
            if 'ALL' in requested and entry_db == GLOBAL_SCOPE:
                drop = True
            elif entry_db == GLOBAL_SCOPE and priv in requested:
                drop = True
        else:
            if entry_db == db and ('ALL' in requested or priv in requested):
                drop = True
        if drop:
            removed += 1
        else:
            kept.append(entry)
    record['privs'] = kept
    return removed


def show_grants(users, user) -> list:
    """`SHOW GRANTS` 的行（MySQL 文本形态，按作用域合并成行）"""
    record = (users or {}).get(user)
    if not isinstance(record, dict):
        return []
    host = str(record.get('host') or '%')
    globals_, scoped = _split_entries(record)
    if is_admin(users, user):
        return [f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'{host}'"]
    lines = []
    if globals_:
        lines.append(
            f"GRANT {', '.join(sorted(globals_))} ON *.* "
            f"TO '{user}'@'{host}'")
    else:
        # MySQL 对"无任何权限"的账户同样显示一行 USAGE（= 无权限）
        lines.append(f"GRANT USAGE ON *.* TO '{user}'@'{host}'")
    for db in sorted(scoped):
        privs = scoped[db]
        if 'ALL' in privs or set(GRANTABLE_PRIVS) <= privs:
            body = 'ALL PRIVILEGES'
        else:
            body = ', '.join(sorted(privs))
        lines.append(
            f"GRANT {body} ON `{db}`.* TO '{user}'@'{host}'")
    return lines
