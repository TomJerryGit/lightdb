# -*- coding: utf-8 -*-
"""P3 首启引导 / --smoke 测试（offscreen，不依赖数据库）。

覆盖：
- 默认未完成 → `maybe_show_first_run(interactive=False)` 返回 True；
- `LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN=1` 跳过；
- FirstRunDialog 三步内容（run_studio.ps1 / 8899 / root 空口令仅本地 / 强口令），
  可复制命令文本、accessibleName；
- 完成 / 跳过 / 关闭都持久化 `first_run_done=True`（Settings round-trip）；
- 旧 settings.json（无字段）兼容默认 False；
- `python -m lightdb_studio.app --smoke`（offscreen，temp HOME）exit 0 且 <30s。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """settings.json 改道到 tmp，并清空 SUPPRESS 环境变量。"""
    import importlib
    cfg_mod = importlib.import_module('lightdb_studio.config')
    cfg_dir = tmp_path / 'cfg'
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', cfg_dir)
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE', cfg_dir / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', cfg_dir / 'settings.json')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.delenv('LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN', raising=False)
    qapp()
    from lightdb_studio.config import Settings
    return Settings, tmp_path


# ==================== 1. 展示条件 / 抑制 ====================

def test_first_run_pending_by_default(isolated_settings):
    Settings, _tmp = isolated_settings
    from lightdb_studio.ui.first_run_wizard import maybe_show_first_run
    settings = Settings()
    assert settings.first_run_done is False
    assert maybe_show_first_run(None, settings, interactive=False) is True
    # 已完成 → 不再展示
    settings.first_run_done = True
    assert maybe_show_first_run(None, settings, interactive=False) is False


def test_suppress_env_skips_first_run(isolated_settings, monkeypatch):
    Settings, _tmp = isolated_settings
    from lightdb_studio.ui.first_run_wizard import maybe_show_first_run
    monkeypatch.setenv('LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN', '1')
    settings = Settings()
    assert maybe_show_first_run(None, settings, interactive=False) is False
    assert settings.first_run_done is False, '抑制不应改持久化状态'
    # 0/false 不抑制
    monkeypatch.setenv('LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN', '0')
    assert maybe_show_first_run(None, settings, interactive=False) is True


# ==================== 2. 对话框内容 / 可复制命令 ====================

def test_first_run_dialog_contents(isolated_settings):
    Settings, _tmp = isolated_settings
    from lightdb_studio.ui.first_run_wizard import (
        DEMO_COMMAND, DEMO_PORT, FirstRunDialog)
    dlg = FirstRunDialog(Settings())
    dlg.show()
    qapp().processEvents()
    assert dlg.stack.count() == 3
    assert dlg.accessibleName()
    assert dlg.stack.accessibleName()
    assert dlg.skip_button.accessibleName()
    # demo 页文字：一键脚本 / 端口 / root 空口令仅本地 / 生产强口令
    demo = dlg.demo_text.text()
    for expected in ('run_studio.ps1', str(DEMO_PORT), 'root', '空口令',
                     '强口令'):
        assert expected in demo, (expected, demo)
    assert f'{DEMO_PORT}' in dlg.done_text.text()
    assert 'run_studio.ps1' in DEMO_COMMAND
    assert dlg.demo_command_edit.text() == DEMO_COMMAND
    assert dlg.demo_command_edit.isReadOnly()
    # 复制命令
    assert dlg.copy_demo_command() is True
    from PySide6.QtWidgets import QApplication
    assert 'run_studio.ps1' in QApplication.clipboard().text()
    dlg.close()


# ==================== 3. 完成 / 跳过持久化 round-trip ====================

def _settings_file(_tmp):
    from lightdb_studio.config import SETTINGS_FILE
    return SETTINGS_FILE


def test_finish_persists_first_run_done(isolated_settings):
    Settings, tmp = isolated_settings
    from lightdb_studio.config import SETTINGS_FILE
    from lightdb_studio.ui.first_run_wizard import FirstRunDialog, \
        maybe_show_first_run
    settings = Settings()
    dlg = FirstRunDialog(settings)
    dlg.stack.setCurrentIndex(2)
    dlg.finish()
    assert settings.first_run_done is True
    assert SETTINGS_FILE.exists(), SETTINGS_FILE
    raw = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    assert raw['first_run_done'] is True
    assert Settings.load().first_run_done is True
    assert maybe_show_first_run(None, Settings.load(), interactive=False) is False
    dlg.close()


def test_skip_and_close_persist_first_run_done(isolated_settings):
    Settings, _tmp = isolated_settings
    from lightdb_studio.ui.first_run_wizard import FirstRunDialog
    s1 = Settings()
    dlg = FirstRunDialog(s1)
    dlg.skip()
    assert s1.first_run_done is True

    s2 = Settings()
    assert s2.first_run_done is False
    dlg2 = FirstRunDialog(s2)
    dlg2.reject()
    assert s2.first_run_done is True
    assert Settings.load().first_run_done is True


# ==================== 4. 旧 settings 兼容 ====================

def test_legacy_settings_without_first_run_done(isolated_settings):
    Settings, _tmp = isolated_settings
    from lightdb_studio.config import SETTINGS_FILE
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({
        'theme': 'light',
        'language': 'en_US',
        'page_size': 500,
        'editor_font_size': 13,
    }, ensure_ascii=False), encoding='utf-8')
    loaded = Settings.load()
    assert loaded.first_run_done is False, '旧 JSON 缺字段应默认 False'
    assert loaded.theme == 'light' and loaded.page_size == 500
    assert loaded.editor_font_size == 13


# ==================== 5. app --smoke 子进程 ====================

def test_app_smoke_subprocess_exit_zero(tmp_path):
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['HOME'] = str(tmp_path / 'home')
    env['USERPROFILE'] = str(tmp_path / 'home')
    env['LIGHTDB_STUDIO_LOG_DIR'] = str(tmp_path / 'logs')
    env['PYTHONPYCACHEPREFIX'] = str(tmp_path / 'pycache')
    env.pop('LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN', None)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, '-m', 'lightdb_studio.app', '--smoke'],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=60)
    elapsed = time.perf_counter() - t0
    assert proc.returncode == 0, (
        f'--smoke exit={proc.returncode}\nstderr:\n{proc.stderr[-2000:]}')
    assert elapsed < 30, f'--smoke 耗时 {elapsed:.1f}s（应 <30s）'
    # 崩溃日志已尽早安装
    assert (tmp_path / 'logs' / 'studio.log').exists()
    print(f"  → --smoke exit=0，耗时 {elapsed:.1f}s；日志已创建")
