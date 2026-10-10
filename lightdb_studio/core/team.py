"""
团队与角色管理
- 用户 CRUD
- 角色定义
- 权限矩阵
"""
import json
import hashlib
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict

TEAM_FILE = Path.home() / '.lightdb_studio' / 'team_members.json'


# 权限定义
PERMISSIONS = {
    'query': '执行查询',
    'write': '执行 DML（INSERT/UPDATE/DELETE）',
    'ddl': '执行 DDL（CREATE/ALTER/DROP）',
    'grant': '授权/创建用户',
    'admin': '系统管理',
    'review': '审批 SQL',
    'deploy_prod': '部署到生产环境',
    'view_audit': '查看审计日志',
    'manage_schedule': '管理调度任务',
}

ROLES = {
    'admin': {
        'label': '管理员',
        'permissions': list(PERMISSIONS.keys()),
    },
    'dba': {
        'label': 'DBA',
        'permissions': ['query', 'write', 'ddl', 'grant', 'review',
                        'view_audit', 'manage_schedule'],
    },
    'developer': {
        'label': '开发',
        'permissions': ['query', 'write', 'review'],
    },
    'analyst': {
        'label': '分析师',
        'permissions': ['query'],
    },
    'viewer': {
        'label': '只读',
        'permissions': ['query'],
    },
}


@dataclass
class TeamMember:
    username: str = ""
    display_name: str = ""
    email: str = ""
    role: str = "developer"
    extra_permissions: List[str] = field(default_factory=list)
    denied_permissions: List[str] = field(default_factory=list)
    enabled: bool = True
    password_hash: str = ""
    created_at: float = 0
    last_login: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    def has_permission(self, perm: str) -> bool:
        if not self.enabled:
            return False
        if perm in self.denied_permissions:
            return False
        if perm in self.extra_permissions:
            return True
        role = ROLES.get(self.role, ROLES['viewer'])
        return perm in role['permissions']

    def effective_permissions(self) -> List[str]:
        base = set(ROLES.get(self.role, ROLES['viewer'])['permissions'])
        base |= set(self.extra_permissions)
        base -= set(self.denied_permissions)
        return sorted(base)


class TeamManager:
    def __init__(self):
        self.file = TEAM_FILE
        self.members: List[TeamMember] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()
        self._ensure_defaults()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for m in data.get('members', []):
                self.members.append(TeamMember(**m))
        except Exception:
            pass

    def _ensure_defaults(self):
        if not self.members:
            self.members = [
                TeamMember(
                    username='admin',
                    display_name='管理员',
                    email='admin@local',
                    role='admin',
                    password_hash=self._hash('admin123'),
                ),
                TeamMember(
                    username='dev',
                    display_name='开发',
                    role='developer',
                    password_hash=self._hash('dev123'),
                ),
            ]
            self.save()

    def save(self):
        data = {'members': [asdict(m) for m in self.members]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def _hash(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    # ==================== CRUD ====================

    def add_member(self, username: str, password: str = '',
                   role: str = 'developer', email: str = '',
                   display_name: str = '') -> TeamMember:
        if self.get(username):
            raise ValueError(f"用户 {username} 已存在")
        m = TeamMember(
            username=username,
            display_name=display_name or username,
            email=email,
            role=role,
            password_hash=self._hash(password) if password else '',
        )
        self.members.append(m)
        self.save()
        return m

    def update_member(self, username: str, **kwargs):
        m = self.get(username)
        if not m:
            raise ValueError(f"用户 {username} 不存在")
        for k, v in kwargs.items():
            if k == 'password':
                m.password_hash = self._hash(v)
            elif hasattr(m, k):
                setattr(m, k, v)
        self.save()
        return m

    def remove_member(self, username: str):
        self.members = [m for m in self.members if m.username != username]
        self.save()

    def get(self, username: str) -> TeamMember:
        for m in self.members:
            if m.username == username:
                return m
        return None

    def authenticate(self, username: str, password: str) -> bool:
        m = self.get(username)
        if not m or not m.enabled:
            return False
        if m.password_hash != self._hash(password):
            return False
        m.last_login = time.time()
        self.save()
        return True

    def list_all(self) -> List[TeamMember]:
        return list(self.members)

    def permissions_matrix(self) -> Dict:
        """返回权限矩阵"""
        matrix = {}
        for perm in PERMISSIONS.keys():
            matrix[perm] = {
                'label': PERMISSIONS[perm],
                'roles': [r for r, info in ROLES.items()
                          if perm in info['permissions']]
            }
        return matrix

    def stats(self) -> dict:
        by_role = {}
        for m in self.members:
            by_role[m.role] = by_role.get(m.role, 0) + 1
        return {
            'total': len(self.members),
            'enabled': sum(1 for m in self.members if m.enabled),
            'by_role': by_role,
        }