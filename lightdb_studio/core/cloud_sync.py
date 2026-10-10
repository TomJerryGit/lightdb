"""
云同步（本地文件方案）
- 导出所有配置（连接、片段、设置）到一个加密包
- 从加密包导入
- 支持自定义同步目录（如 OneDrive/Dropbox/坚果云）
"""
import json
import os
import shutil
import hashlib
import base64
import time
import zipfile
import tempfile
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import List


SYNC_DIR = Path.home() / '.lightdb_studio' / 'sync'
CONFIG_ROOT = Path.home() / '.lightdb_studio'


def _simple_key(password: str, salt: bytes) -> bytes:
    """简单派生密钥（演示用；生产用 PBKDF2）"""
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt,
                               100000, dklen=32)


def _encrypt(data: bytes, password: str) -> bytes:
    """简单流式加密（XOR + 打乱；演示用）"""
    import random
    salt = os.urandom(16)
    key = _simple_key(password, salt)
    out = bytearray()
    for i, b in enumerate(data):
        out.append(b ^ key[i % len(key)])
    return salt + bytes(out)


def _decrypt(data: bytes, password: str) -> bytes:
    if len(data) < 16:
        raise ValueError("密文损坏")
    salt = data[:16]
    body = data[16:]
    key = _simple_key(password, salt)
    out = bytearray()
    for i, b in enumerate(body):
        out.append(b ^ key[i % len(key)])
    return bytes(out)


class CloudSync:
    def __init__(self, sync_dir: str = None):
        self.sync_dir = Path(sync_dir) if sync_dir else SYNC_DIR
        self.sync_dir.mkdir(parents=True, exist_ok=True)

    def export_all(self, output_file: str, password: str,
                   include_passwords: bool = False) -> dict:
        """
        导出所有配置到加密 zip
        - connections.json（连接）
        - settings.json（设置）
        - snippets.json（片段）
        - masking.json（脱敏规则）
        """
        tmp = tempfile.mkdtemp()
        try:
            # 收集文件
            files = {}

            conn_file = CONFIG_ROOT / 'connections.json'
            if conn_file.exists():
                with open(conn_file, 'r', encoding='utf-8') as f:
                    connections = json.load(f)
                if not include_passwords:
                    for c in connections.get('connections', []):
                        c['password'] = ''
                files['connections.json'] = json.dumps(
                    connections, ensure_ascii=False)

            settings_file = CONFIG_ROOT / 'settings.json'
            if settings_file.exists():
                with open(settings_file, 'r', encoding='utf-8') as f:
                    files['settings.json'] = f.read()

            snip_file = CONFIG_ROOT / 'snippets' / 'snippets.json'
            if snip_file.exists():
                with open(snip_file, 'r', encoding='utf-8') as f:
                    files['snippets.json'] = f.read()

            mask_file = CONFIG_ROOT / 'masking.json'
            if mask_file.exists():
                with open(mask_file, 'r', encoding='utf-8') as f:
                    files['masking.json'] = f.read()

            # 打 zip
            zip_path = os.path.join(tmp, 'config.zip')
            with zipfile.ZipFile(zip_path, 'w',
                                 zipfile.ZIP_DEFLATED) as zf:
                for name, content in files.items():
                    zf.writestr(name, content)

            with open(zip_path, 'rb') as f:
                zip_data = f.read()

            # 加密
            encrypted = _encrypt(zip_data, password)

            with open(output_file, 'wb') as f:
                f.write(encrypted)

            return {
                'ok': True,
                'files': list(files.keys()),
                'size': len(encrypted),
                'output': output_file,
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def import_all(self, input_file: str, password: str,
                   overwrite: bool = False) -> dict:
        """从加密包恢复配置"""
        with open(input_file, 'rb') as f:
            encrypted = f.read()

        try:
            zip_data = _decrypt(encrypted, password)
        except Exception as e:
            return {'ok': False, 'error': f"解密失败: {e}"}

        # 保存备份
        backup_dir = CONFIG_ROOT / f'backup_{int(time.time())}'
        backup_dir.mkdir(parents=True, exist_ok=True)

        restored = []

        try:
            with zipfile.ZipFile(__import__('io').BytesIO(zip_data)) as zf:
                for name in zf.namelist():
                    content = zf.read(name)

                    # 备份现有
                    target = None
                    if name == 'connections.json':
                        target = CONFIG_ROOT / name
                    elif name == 'settings.json':
                        target = CONFIG_ROOT / name
                    elif name == 'snippets.json':
                        target = CONFIG_ROOT / 'snippets' / name
                    elif name == 'masking.json':
                        target = CONFIG_ROOT / name

                    if target is None:
                        continue

                    if target.exists() and not overwrite:
                        # 备份
                        bak = backup_dir / name
                        bak.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, bak)

                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, 'wb') as f:
                        f.write(content)
                    restored.append(name)

            return {
                'ok': True,
                'restored': restored,
                'backup_dir': str(backup_dir),
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def list_backups(self):
        backups = []
        for d in CONFIG_ROOT.glob('backup_*'):
            if d.is_dir():
                try:
                    ts = int(d.name.split('_', 1)[1])
                    backups.append({
                        'path': str(d),
                        'time': ts,
                        'files': [f.name for f in d.iterdir()]
                    })
                except Exception:
                    pass
        return sorted(backups, key=lambda x: x['time'], reverse=True)

    def restore_backup(self, backup_dir: str) -> dict:
        """从备份目录恢复"""
        bd = Path(backup_dir)
        if not bd.exists():
            return {'ok': False, 'error': '备份不存在'}

        restored = []
        for f in bd.iterdir():
            if f.name == 'connections.json':
                target = CONFIG_ROOT / f.name
            elif f.name == 'settings.json':
                target = CONFIG_ROOT / f.name
            elif f.name == 'snippets.json':
                target = CONFIG_ROOT / 'snippets' / f.name
            elif f.name == 'masking.json':
                target = CONFIG_ROOT / f.name
            else:
                continue
            shutil.copy2(f, target)
            restored.append(f.name)
        return {'ok': True, 'restored': restored}