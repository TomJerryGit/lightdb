"""
团队配置
- 连接配置的导出/导入（去除密码）
- 加密保存密码（可选）
"""
import json
import base64
import hashlib
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List


@dataclass
class TeamConnection:
    name: str
    host: str
    port: int
    user: str
    database: str = ""
    charset: str = "utf8mb4"
    ssl: bool = False
    description: str = ""
    # 密码不明文存储，可选：
    password_encrypted: str = ""


def _xor_encrypt(text: str, key: str) -> str:
    """简单异或（演示用；生产应使用 AES）"""
    if not text:
        return ""
    k = key.encode()
    t = text.encode()
    return base64.b64encode(
        bytes(t[i] ^ k[i % len(k)] for i in range(len(t)))
    ).decode('ascii')


def _xor_decrypt(cipher: str, key: str) -> str:
    if not cipher:
        return ""
    try:
        raw = base64.b64decode(cipher)
        k = key.encode()
        return bytes(raw[i] ^ k[i % len(k)]
                     for i in range(len(raw))).decode()
    except Exception:
        return ""


class TeamConfigManager:
    def __init__(self, team_dir: str = None):
        if team_dir is None:
            team_dir = str(Path.home() / '.lightdb_studio' / 'team')
        self.dir = Path(team_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def export_connections(self, connections: list, filepath: str,
                           include_passwords: bool = False,
                           encrypt_key: str = ""):
        """导出连接配置"""
        data = {
            'version': 1,
            'exported_at': time.time(),
            'connections': [],
        }
        for c in connections:
            entry = {
                'name': c.name,
                'host': c.host,
                'port': c.port,
                'user': c.user,
                'database': c.database or '',
                'charset': c.charset or 'utf8mb4',
                'ssl': getattr(c, 'ssl', False),
                'description': '',
            }
            if include_passwords and getattr(c, 'password', ''):
                if encrypt_key:
                    entry['password_encrypted'] = _xor_encrypt(
                        c.password, encrypt_key)
                else:
                    entry['password_encrypted'] = _xor_encrypt(
                        c.password, 'lightdb_default')
            data['connections'].append(entry)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return len(data['connections'])

    def import_connections(self, filepath: str, encrypt_key: str = "") -> list:
        """导入连接配置"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        result = []
        for entry in data.get('connections', []):
            password = ''
            if 'password_encrypted' in entry:
                key = encrypt_key or 'lightdb_default'
                password = _xor_decrypt(entry['password_encrypted'], key)
            result.append({
                'name': entry.get('name', ''),
                'host': entry.get('host', '127.0.0.1'),
                'port': entry.get('port', 8888),
                'user': entry.get('user', 'root'),
                'password': password,
                'database': entry.get('database', ''),
                'charset': entry.get('charset', 'utf8mb4'),
                'ssl': entry.get('ssl', False),
            })
        return result

    def save_team_profile(self, name: str, connections: list,
                          encrypt_key: str = ""):
        """保存为团队配置（共享目录）"""
        fp = self.dir / f"{name}.json"
        self.export_connections(
            connections, str(fp),
            include_passwords=False,
            encrypt_key=encrypt_key)
        return str(fp)

    def list_profiles(self):
        return [p.stem for p in self.dir.glob('*.json')]

    def load_profile(self, name: str) -> list:
        fp = self.dir / f"{name}.json"
        if not fp.exists():
            return []
        return self.import_connections(str(fp))