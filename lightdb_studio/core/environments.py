"""
多环境管理
- 定义 dev/staging/prod 等多套环境
- 一键切换（颜色标识）
- 生产环境保护：写操作二次确认
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Optional

ENV_FILE = Path.home() / '.lightdb_studio' / 'environments.json'


# 环境预设
ENV_PRESETS = {
    'dev': {
        'color': '#89d185',
        'icon': '🛠',
        'dangerous': False,
        'require_confirm': False,
    },
    'test': {
        'color': '#dcdcaa',
        'icon': '🧪',
        'dangerous': False,
        'require_confirm': False,
    },
    'staging': {
        'color': '#ce9178',
        'icon': '⚙️',
        'dangerous': True,
        'require_confirm': True,
    },
    'prod': {
        'color': '#f48771',
        'icon': '🚨',
        'dangerous': True,
        'require_confirm': True,
        'readonly_warn': True,
    },
    'other': {
        'color': '#569cd6',
        'icon': '📦',
        'dangerous': False,
        'require_confirm': False,
    },
}


@dataclass
class Environment:
    name: str = "dev"                    # 标识符 dev / test / staging / prod
    label: str = "开发环境"              # 显示名
    connection: str = ""                 # 关联的连接名
    database: str = ""                   # 默认库
    color: str = "#89d185"               # 主题色
    icon: str = "🛠"                     # 图标
    dangerous: bool = False              # 是否危险（写操作需二次确认）
    require_confirm: bool = False        # 是否需要二次确认
    readonly: bool = False               # 是否只读（禁止 DML/DDL）
    description: str = ""
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class EnvironmentManager:
    def __init__(self):
        self.file = ENV_FILE
        self.environments: List[Environment] = []
        self.current: Optional[str] = None   # 当前环境的 name
        self._listeners = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()
        self._ensure_defaults()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for e in data.get('environments', []):
                self.environments.append(Environment(**e))
            self.current = data.get('current')
        except Exception:
            pass

    def _ensure_defaults(self):
        """确保至少有几个默认环境"""
        if not self.environments:
            self.environments = [
                Environment(name='dev', label='开发环境',
                            color='#89d185', icon='🛠'),
                Environment(name='test', label='测试环境',
                            color='#dcdcaa', icon='🧪'),
                Environment(name='staging', label='预发布环境',
                            color='#ce9178', icon='⚙️',
                            dangerous=True, require_confirm=True),
                Environment(name='prod', label='生产环境',
                            color='#f48771', icon='🚨',
                            dangerous=True, require_confirm=True,
                            readonly=False),
            ]
            self.save()

    def save(self):
        data = {
            'environments': [asdict(e) for e in self.environments],
            'current': self.current,
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add(self, env: Environment):
        self.environments.append(env)
        self.save()

    def update(self, name: str, env: Environment):
        for i, e in enumerate(self.environments):
            if e.name == name:
                self.environments[i] = env
                self.save()
                self._notify()
                return

    def remove(self, name: str):
        self.environments = [e for e in self.environments if e.name != name]
        if self.current == name:
            self.current = None
        self.save()
        self._notify()

    def get(self, name: str) -> Optional[Environment]:
        for e in self.environments:
            if e.name == name:
                return e
        return None

    def set_current(self, name: str):
        if self.get(name) is None:
            return
        self.current = name
        self.save()
        self._notify()

    def get_current(self) -> Optional[Environment]:
        if self.current:
            return self.get(self.current)
        return None

    def apply_preset(self, env: Environment, preset: str):
        """应用预设配置到环境"""
        p = ENV_PRESETS.get(preset)
        if not p:
            return
        env.name = preset
        env.color = p.get('color', env.color)
        env.icon = p.get('icon', env.icon)
        env.dangerous = p.get('dangerous', False)
        env.require_confirm = p.get('require_confirm', False)
        env.readonly = p.get('readonly_warn', False) and False

    # ==================== 安全检查 ====================

    def check_sql_safety(self, sql: str) -> dict:
        """
        检查当前环境下 SQL 是否安全
        返回 {'safe': bool, 'reason': str, 'require_confirm': bool}
        """
        env = self.get_current()
        if env is None:
            return {'safe': True, 'reason': '', 'require_confirm': False}

        up = sql.strip().upper()

        # 危险操作
        dangerous = False
        if up.startswith(('DROP', 'TRUNCATE')):
            dangerous = True
        elif up.startswith('DELETE') and 'WHERE' not in up:
            dangerous = True
        elif up.startswith('UPDATE') and 'WHERE' not in up:
            dangerous = True
        elif up.startswith('ALTER'):
            dangerous = True

        # 只读保护
        if env.readonly and up.startswith(
                ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE',
                 'ALTER', 'TRUNCATE')):
            return {
                'safe': False,
                'reason': f"[{env.label}] 只读模式，禁止写操作",
                'require_confirm': False,
            }

        # 生产环境危险操作二次确认
        if env.require_confirm and (dangerous or up.startswith(('INSERT', 'UPDATE', 'DELETE'))):
            return {
                'safe': True,
                'reason': f"[{env.label}] 危险操作需要二次确认",
                'require_confirm': True,
            }

        return {'safe': True, 'reason': '', 'require_confirm': False}

    def on_change(self, callback):
        self._listeners.append(callback)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb(self.get_current())
            except Exception:
                pass