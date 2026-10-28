# -*- coding: utf-8 -*-
"""task-75：Studio 多窗口生命周期 + 快捷键冲突/作用域/持久化（offscreen）。"""
import gc
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    qapp()
    return tmp_path


@pytest.fixture
def config(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE',
                        tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE',
                        tmp_path / 'cfg' / 'settings.json')
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    return cfg


def make_window(cfg):
    from lightdb_studio.ui.main_window import MainWindow
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None
    return win


# ==================== 1. 快捷键默认/冲突/作用域 ====================

def test_all_default_shortcuts_valid_and_cover_editor_actions(home):
    from lightdb_studio.ui.shortcuts import (
        DEFAULT_SHORTCUTS, default_shortcuts, validate_shortcuts)
    mapping = default_shortcuts()
    report = validate_shortcuts(mapping, allow_empty=False)
    assert report['ok'], report
    assert not report['conflicts'] and not report['errors']
    # 之前隐藏的编辑器 F5/Ctrl+R 已纳入默认表（全部编辑器动作可检测）
    assert 'run_f5' in mapping and mapping['run_f5'] == 'F5'
    assert 'replace_alt' in mapping and mapping['replace_alt'] == 'Ctrl+R'
    editor_ids = {k for k, v in DEFAULT_SHORTCUTS.items() if v.get('editor')}
    assert {'run', 'run_selection', 'find', 'replace', 'run_f5',
            'replace_alt'} <= editor_ids
    # 冲突覆盖：默认键与编辑器动作都能被检测
    bad = dict(mapping)
    bad['find'] = mapping['run_f5']
    conflict = validate_shortcuts(bad)
    assert conflict['ok'] is False and conflict['conflicts']
    assert any('find' in c['ids'] for c in conflict['conflicts'])
    # 恢复默认值
    from lightdb_studio.ui.shortcuts import default_shortcuts as defaults
    assert defaults() == mapping


def test_editor_shortcut_scope_and_custom_apply(home):
    from PySide6.QtCore import Qt
    from lightdb_studio.ui.sql_editor import SQLEditor
    from lightdb_studio.ui.shortcuts import (
        DEFAULT_SHORTCUTS, apply_to_editor, default_shortcuts)
    editor = SQLEditor()
    try:
        # 所有编辑器动作都有 QShortcut 引用，且作用域是 WidgetWithChildren
        for action_id, meta in DEFAULT_SHORTCUTS.items():
            if not meta.get('editor'):
                continue
            assert action_id in editor._shortcut_refs, action_id
            ref = editor._shortcut_refs[action_id]
            assert ref.context() == Qt.WidgetWithChildrenShortcut, action_id
        applied = apply_to_editor(editor, {'find': 'Ctrl+Shift+F',
                                           'run_f5': 'F7'})
        assert applied >= 2
        assert editor._shortcut_refs['find'].key().toString() == 'Ctrl+Shift+F'
        assert editor._shortcut_refs['run_f5'].key().toString() == 'F7'
        # 恢复默认
        apply_to_editor(editor, default_shortcuts())
        assert editor._shortcut_refs['find'].key().toString() == 'Ctrl+F'
        assert editor._shortcut_refs['run_f5'].key().toString() == 'F5'
    finally:
        editor.close()


def test_shortcut_persistence_and_restart(config):
    from lightdb_studio.config import Settings
    from lightdb_studio.ui.shortcuts import apply_to_window, load_shortcuts
    config.settings.shortcuts = {'find': 'Ctrl+Shift+F',
                                 'run': 'Ctrl+Alt+Return'}
    config.settings.save()
    loaded = Settings.load()
    assert loaded.shortcuts['find'] == 'Ctrl+Shift+F'
    assert load_shortcuts(loaded.shortcuts)['find'] == 'Ctrl+Shift+F'
    win = make_window(config)
    try:
        result = win._apply_shortcut_settings()
        assert result['ok'] is True
        from PySide6.QtGui import QAction
        acts = {a.text().replace('&', '').strip(): a
                for a in win.findChildren(QAction)}
        editor, _ = win._current_widgets()
        assert editor._shortcut_refs['find'].key().toString() == 'Ctrl+Shift+F'
        assert editor._shortcut_refs['run'].key().toString() == 'Ctrl+Alt+Return'
        # 菜单编辑动作不被编辑器快捷键串扰：新建查询仍是其默认键
        assert acts['新建查询'].shortcut().toString() == 'Ctrl+N'
    finally:
        win.close()


# ==================== 2. 多窗口生命周期/设置共享/关闭清理 ====================

def test_multiwindow_shared_settings_theme_and_lifecycle(config):
    from PySide6.QtWidgets import QApplication
    win = make_window(config)
    children = []
    try:
        child = win._new_window()
        assert child is not None
        children.append(child)
        assert child.settings is win.settings
        assert child.settings.theme == win.settings.theme
        assert child._shortcut_apply_result.get('ok') is True
        # 父窗口切换主题：共享 settings，子窗口读到的主题一致
        assert win._set_theme_from_menu('light') is True
        QApplication.instance().processEvents()
        assert child.settings.theme == 'light'
        assert win.settings.theme == 'light'
        win._set_theme_from_menu('dark')
        # 关闭子窗口 → 父窗口列表清理（WA_DeleteOnClose + destroyed）
        child.close()
        QApplication.instance().processEvents()
        gc.collect()
        QApplication.instance().processEvents()
        assert child not in win._extra_windows
        # 再开一个并关闭主窗口：子窗口被主窗口 closeEvent 一并关闭
        child2 = win._new_window()
        assert child2 is not None
        children.append(child2)
        assert len(win._extra_windows) >= 1
        win.close()
        QApplication.instance().processEvents()
        gc.collect()
        QApplication.instance().processEvents()
        assert win._extra_windows == []
        # 子窗口 C++ 对象已随主窗口关闭而销毁（不再可交互）
        from shiboken6 import isValid
        assert isValid(child2) is False
    finally:
        try:
            win.close()
        except Exception:
            pass
        for c in children:
            try:
                c.close()
            except Exception:
                pass


def test_multiwindow_shortcut_scope_consistent(config):
    from PySide6.QtCore import Qt
    win = make_window(config)
    child = None
    try:
        child = win._new_window()
        assert child is not None
        for w in (win, child):
            editor, _ = w._current_widgets()
            for aid in ('find', 'run_f5', 'replace_alt'):
                ref = editor._shortcut_refs[aid]
                assert ref.context() == Qt.WidgetWithChildrenShortcut
        # 父窗口更新快捷键，子窗口同步应用
        win.settings.shortcuts = {'find': 'Ctrl+Shift+F'}
        result = win._apply_shortcut_settings()
        assert result['ok'] is True
        assert child._shortcut_apply_result['ok'] is True
        c_editor, _ = child._current_widgets()
        assert c_editor._shortcut_refs['find'].key().toString() == 'Ctrl+Shift+F'
    finally:
        if child is not None:
            try:
                child.close()
            except Exception:
                pass
        win.close()


def test_settings_reset_shortcuts_restores_defaults(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import Settings
    from lightdb_studio.ui.settings_dialog import SettingsDialog
    from lightdb_studio.ui.shortcuts import default_shortcuts
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 's.json')
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path)
    settings = Settings()
    dlg = SettingsDialog(settings)
    try:
        idx = dlg._shortcut_ids.index('find')
        dlg.shortcut_table.item(idx, 1).setText('Ctrl+Shift+F')
        dlg._reset_shortcuts()
        assert dlg.shortcut_table.item(idx, 1).text() == 'Ctrl+F'
        dlg.apply_settings()
        assert settings.shortcuts.get('find', 'Ctrl+F') == 'Ctrl+F'
        assert default_shortcuts()['find'] == 'Ctrl+F'
    finally:
        dlg.close()
