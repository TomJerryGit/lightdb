"""GUI P0-4/P0-5 测试：本地口令加密（DPAPI/Fernet）+ 旧明文迁移 + 密码显示切换。

覆盖：
1. `credentials` 后端可用性与 encrypt/decrypt 往返（含中文/空串/旧明文）；
2. `AppConfig.save()` 落盘不含明文口令，`AppConfig.load()` 能还原；
3. 旧版明文 connections.json 首次加载自动迁移为密文；
4. 密文损坏/换用户：保留连接、口令置空、显式告警（不静默丢连接/不静默当空）；
5. 空口令仍写空串、schema 仍是 8 键；
6. ConnectionDialog 密码显示/隐藏按钮（offscreen）。
"""
import json
import os
import sys
from dataclasses import asdict

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _redirect_config(tmp_path, monkeypatch):
    """把 lightdb_studio.config 的落盘路径改到 tmp_path（返回 config 模块）。"""
    import lightdb_studio.config as config_mod

    cfg_dir = tmp_path / '.lightdb_studio'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, 'CONFIG_DIR', cfg_dir)
    monkeypatch.setattr(config_mod, 'CONFIG_FILE', cfg_dir / 'connections.json')
    monkeypatch.setattr(config_mod, 'SETTINGS_FILE', cfg_dir / 'settings.json')
    return config_mod


# ==================== 包延迟导入回归 ====================

def test_package_lazy_import_fromlist_does_not_recurse():
    """`from lightdb_studio import config` 不得 RecursionError。

    包内 PEP 562 `__getattr__` 若用 `from . import config` 实现，fromlist
    处理会再次触发 `__getattr__` → 无限递归。用 importlib 直接导入修复。
    """
    import lightdb_studio
    from lightdb_studio import config as cfg
    assert cfg is not None
    assert cfg.CONFIG_FILE.name == 'connections.json'


# ==================== credentials 单元 ====================

def test_backend_and_roundtrip():
    from lightdb_studio.core import credentials
    backend = credentials.backend_name()
    assert backend in ('dpapi', 'fernet', 'plain-fallback')
    if backend == 'plain-fallback':
        pytest.skip('当前平台无 DPAPI/cryptography，跳过真实加密往返')
    for plain in ('s3cret-中文-!@#', 'a', 'x' * 300):
        stored = credentials.encrypt_password(plain)
        assert stored and stored != plain
        assert credentials.is_encrypted(stored)
        assert credentials.decrypt_password(stored) == plain
    assert credentials.encrypt_password('') == ''
    assert credentials.decrypt_password('') == ''
    # 旧版裸明文：原样返回（由 AppConfig 迁移）
    assert credentials.decrypt_password('legacy-plain') == 'legacy-plain'


def test_plain_prefix_roundtrip():
    from lightdb_studio.core import credentials
    assert credentials.decrypt_password('plain:abc') == 'abc'


# ==================== AppConfig 落盘加密 ====================

def test_save_hides_password_and_load_restores(tmp_path, monkeypatch):
    config_mod = _redirect_config(tmp_path, monkeypatch)
    from lightdb_studio.config import AppConfig, ConnectionInfo

    cfg = AppConfig()
    cfg.add_connection(ConnectionInfo(name='prod', host='127.0.0.1',
                                      port=8899, user='root',
                                      password='P@ss-中文-123'))
    raw = json.loads((tmp_path / '.lightdb_studio' / 'connections.json')
                     .read_text(encoding='utf-8'))
    stored = raw['connections'][0]['password']
    assert stored and 'P@ss-中文-123' not in stored
    assert set(raw['connections'][0]) == {
        'name', 'host', 'port', 'user', 'password', 'database',
        'charset', 'ssl'}, 'connections.json schema 必须保持 8 键'

    loaded = AppConfig()
    back = loaded.find('prod')
    assert back is not None
    assert back.password == 'P@ss-中文-123'
    assert asdict(back) == asdict(cfg.find('prod'))


def test_empty_password_stays_empty(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    from lightdb_studio.config import AppConfig, ConnectionInfo
    cfg = AppConfig()
    cfg.add_connection(ConnectionInfo(name='local', host='127.0.0.1',
                                      port=8899, user='root', password=''))
    raw = json.loads((tmp_path / '.lightdb_studio' / 'connections.json')
                     .read_text(encoding='utf-8'))
    assert raw['connections'][0]['password'] == ''
    assert AppConfig().find('local').password == ''


def test_legacy_plaintext_migrates_on_load(tmp_path, monkeypatch):
    _redirect_config(tmp_path, monkeypatch)
    from lightdb_studio import config as config_mod
    from lightdb_studio.config import AppConfig

    legacy = {
        'connections': [{
            'name': 'old', 'host': '127.0.0.1', 'port': 8899, 'user': 'root',
            'password': 'legacy-secret-中文', 'database': '',
            'charset': 'utf8mb4', 'ssl': False,
        }],
        'last_connection': 'old',
    }
    (config_mod.CONFIG_FILE).write_text(
        json.dumps(legacy, ensure_ascii=False), encoding='utf-8')

    cfg = AppConfig()                      # load 时应自动迁移
    assert cfg.find('old').password == 'legacy-secret-中文'
    raw_text = config_mod.CONFIG_FILE.read_text(encoding='utf-8')
    assert 'legacy-secret-中文' not in raw_text, '迁移后文件不得再有明文'
    stored = json.loads(raw_text)['connections'][0]['password']
    assert stored.startswith(('dpapi:v1:', 'fernet:v1:', 'plain:'))
    # 二次加载仍能还原
    assert AppConfig().find('old').password == 'legacy-secret-中文'


def test_corrupt_ciphertext_keeps_connection_with_warning(tmp_path, monkeypatch,
                                                          capsys):
    _redirect_config(tmp_path, monkeypatch)
    from lightdb_studio import config as config_mod
    from lightdb_studio.config import AppConfig

    broken = {
        'connections': [{
            'name': 'broken', 'host': '127.0.0.1', 'port': 8899,
            'user': 'root', 'password': 'dpapi:v1:AAAA',
            'database': '', 'charset': 'utf8mb4', 'ssl': False,
        }],
        'last_connection': 'broken',
    }
    config_mod.CONFIG_FILE.write_text(
        json.dumps(broken, ensure_ascii=False), encoding='utf-8')
    cfg = AppConfig()
    back = cfg.find('broken')
    assert back is not None, '单条口令损坏不得丢掉整条连接'
    assert back.password == '', '损坏口令应置空，而不是静默当空却仍显示旧值'
    err = capsys.readouterr().err
    assert '无法解密' in err


# ==================== ConnectionDialog 密码显示切换 ====================

def test_connection_dialog_password_toggle(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    _redirect_config(tmp_path, monkeypatch)
    from PySide6.QtWidgets import QApplication, QLineEdit
    from lightdb_studio.ui.connection_dialog import ConnectionDialog

    app = QApplication.instance() or QApplication([])
    dlg = ConnectionDialog()
    try:
        assert dlg.pwd_edit.echoMode() == QLineEdit.Password
        dlg.pwd_toggle_btn.setChecked(True)
        assert dlg.pwd_edit.echoMode() == QLineEdit.Normal
        assert dlg.pwd_toggle_btn.text() == '隐藏'
        dlg.pwd_toggle_btn.setChecked(False)
        assert dlg.pwd_edit.echoMode() == QLineEdit.Password
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()
