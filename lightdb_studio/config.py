"""连接配置 + 应用设置"""
import json
import sys
from pathlib import Path
from dataclasses import dataclass, asdict, field, fields
from typing import List


CONFIG_DIR = Path.home() / '.lightdb_studio'
CONFIG_FILE = CONFIG_DIR / 'connections.json'
SETTINGS_FILE = CONFIG_DIR / 'settings.json'


@dataclass
class ConnectionInfo:
    name: str = ""
    host: str = "127.0.0.1"
    port: int = 8888
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    ssl: bool = False


# `connections.json` 里一条连接**恰好**是这些字段（`AppConfig.save()` 用
# `asdict` 写出来，键名就是这里的字段名）。手写 JSON 时多写一个键以前会让
# `ConnectionInfo(**c)` 抛 TypeError，而那个异常被 `load()` 的大 try 吞掉 →
# **所有**连接一起消失，只在 stdout 留一行 print。现在按字段过滤 + 逐条兜底。
CONNECTION_FIELDS = frozenset(f.name for f in fields(ConnectionInfo))


def _connection_from_dict(d: dict) -> ConnectionInfo:
    """把 json 里的一条连接转成 `ConnectionInfo`。

    未知键**忽略并告警**（不再让 TypeError 把整份配置连坐丢掉）；缺的键用
    `ConnectionInfo` 的默认值。注意「上次连接」标记不在每条连接里，而是
    `connections.json` 的顶层 `last_connection`（见 `AppConfig.load`）。
    """
    if not isinstance(d, dict):
        raise TypeError(f'连接配置必须是对象，收到 {type(d).__name__}')
    known = {k: v for k, v in d.items() if k in CONNECTION_FIELDS}
    unknown = sorted(set(d) - CONNECTION_FIELDS)
    if unknown:
        print(f"连接 {known.get('name')!r} 里有未知字段，已忽略: {unknown}"
              f"（合法字段: {sorted(CONNECTION_FIELDS)}）", file=sys.stderr)
    return ConnectionInfo(**known)


@dataclass
class Settings:
    theme: str = 'dark'
    language: str = 'zh_CN'
    page_size: int = 200
    auto_connect: bool = True
    restore_tabs: bool = False

    editor_font_family: str = 'Consolas'
    editor_font_size: int = 11
    editor_tab_size: int = 4
    editor_auto_complete: bool = True
    editor_auto_pair: bool = True
    editor_highlight_line: bool = True

    log_level: str = 'INFO'
    query_timeout: int = 0
    max_result_rows: int = 100000

    shortcuts: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> 'Settings':
        if not SETTINGS_FILE.exists():
            return cls()
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            s = cls()
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            return cls()

    def save(self):
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        tmp.replace(SETTINGS_FILE)


class AppConfig:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.connections: List[ConnectionInfo] = []
        self.last_connection: str = ""
        self.settings = Settings.load()
        self.load()

    def load(self):
        if not CONFIG_FILE.exists():
            return
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for c in data.get('connections', []):
                try:
                    self.connections.append(_connection_from_dict(c))
                except Exception as e:            # noqa: BLE001
                    # 单条写坏不该拖垮其余连接（以前是整份配置全丢）
                    print(f"跳过无法解析的连接配置 {c!r}: {e}", file=sys.stderr)
            self.last_connection = data.get('last_connection', '')
        except Exception as e:
            print(f"加载配置失败: {e}", file=sys.stderr)

    def save(self):
        data = {
            'connections': [asdict(c) for c in self.connections],
            'last_connection': self.last_connection,
        }
        tmp = CONFIG_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_FILE)

    def add_connection(self, conn: ConnectionInfo):
        for i, c in enumerate(self.connections):
            if c.name == conn.name:
                self.connections[i] = conn
                self.save()
                return
        self.connections.append(conn)
        self.save()

    def remove_connection(self, name: str):
        self.connections = [c for c in self.connections if c.name != name]
        self.save()

    def find(self, name: str):
        for c in self.connections:
            if c.name == name:
                return c
        return None