# -*- coding: utf-8 -*-
"""N1：Navicat 主题（tokens/QSS/持久化）+ Ribbon 工具栏 + 图标 fallback。"""
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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


# ==================== 1. 主题 tokens / QSS ====================

def test_design_tokens_and_theme_contrast(home):
    from lightdb_studio.ui.theme import (
        DESIGN_TOKENS, THEMES, theme_qss, theme_tokens)
    assert DESIGN_TOKENS['spacing']['md'] == 8
    assert DESIGN_TOKENS['radius']['md'] > 0
    assert DESIGN_TOKENS['icon_size']['ribbon'] >= 24
    dark = theme_tokens('dark')
    light = theme_tokens('light')
    assert dark['theme'] == 'dark' and light['theme'] == 'light'
    assert dark['colors']['bg'] != light['colors']['bg']
    assert dark['colors']['fg'] != light['colors']['fg']
    qss_dark = theme_qss('dark')
    qss_light = theme_qss('light')
    assert qss_dark != qss_light
    for marker in ('QToolBar#RibbonBar', 'ribbon="true"',
                   'QPushButton:focus', 'border: 2px solid',
                   'alternate-background-color', 'selection-background-color'):
        assert marker in qss_dark, marker
    assert 'QToolButton[ribbon="true"]:disabled' in qss_dark
    assert theme_tokens('missing')['theme'] == 'dark'


def test_apply_theme_runtime_switch(home):
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.theme import apply_theme, theme_qss
    app = QApplication.instance()
    apply_theme(app, 'light')
    assert app.styleSheet() == theme_qss('light')
    apply_theme(app, 'dark')
    assert app.styleSheet() == theme_qss('dark')
    assert getattr(app, '_lightdb_theme_name') == 'dark'


def test_theme_persisted_via_settings_dialog(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import Settings
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.settings_dialog import SettingsDialog
    from lightdb_studio.ui.theme import theme_qss
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 'settings.json')
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path)
    settings = Settings()
    dlg = SettingsDialog(settings)
    try:
        dlg.theme_combo.setCurrentIndex(1)  # light
        assert dlg.theme_combo.accessibleName()
        assert QApplication.instance().styleSheet() == theme_qss('light')
        result = dlg.apply_settings()
        assert result['ok'] is True
        assert settings.theme == 'light'
        assert Settings.load().theme == 'light'
        dlg.theme_combo.setCurrentIndex(0)
        dlg.theme_combo.setCurrentIndex(1)  # keep persisted light expected
        dlg.apply_settings()
    finally:
        dlg.close()


# ==================== 2. Ribbon 工具栏 / 图标 ====================

def test_icon_backend_explicit_fallback(home):
    from lightdb_studio.ui import toolbar
    backend = toolbar.icon_backend()
    assert backend in ('qtawesome', 'fallback')
    icon = toolbar.make_icon('mdi.table', 'table')
    assert not icon.isNull()
    if backend == 'fallback':
        assert toolbar.fallback_events(), 'fallback 必须显式记录，不假成功'
        assert any('qtawesome' in str(reason).lower()
                   for _name, reason in toolbar.fallback_events())
        print('  → qtawesome 未安装，显式 fallback 到 Qt 标准图标')


def test_ribbon_groups_actions_and_disable(home):
    from lightdb_studio.ui.toolbar import RibbonActionSpec, RibbonBar
    calls = []
    specs = [
        RibbonActionSpec('new_conn', '新建连接...', '新建连接',
                         lambda: calls.append('conn'), 'mdi.plug', 'connect',
                         '连接'),
        RibbonActionSpec('run', '执行', '执行',
                         lambda: calls.append('run'), 'mdi.play', 'run',
                         '查询', needs_connection=True),
        RibbonActionSpec('ai', 'AI 助手', 'AI 助手',
                         lambda: calls.append('ai'), 'mdi.robot', 'ai', '工具'),
    ]
    rb = RibbonBar(specs)
    try:
        assert rb.objectName() == 'RibbonBar'
        assert rb.groups == ['连接', '查询', '工具']
        assert rb.action_ids() == ['new_conn', 'run', 'ai']
        assert rb.button('new_conn').text() == '新建连接'
        assert rb.button('run').isEnabled() is False
        assert rb.button('ai').isEnabled() is True
        rb.update_availability(True)
        assert rb.button('run').isEnabled() is True
        rb.action('new_conn').trigger()
        assert calls == ['conn']
        # 保留旧接口：actions() 非空（i18n/可访问性兼容）
        assert rb.actions()
        assert rb.accessibleName()
    finally:
        rb.deleteLater()


def test_main_window_ribbon_and_seven_menus(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
    from lightdb_studio.ui.toolbar import RibbonBar
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE',
                        tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE',
                        tmp_path / 'cfg' / 'settings.json')
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None
    try:
        assert isinstance(win._main_toolbar, RibbonBar)
        assert win._main_toolbar is win.ribbon
        assert win._main_toolbar.accessibleName()
        for group in ('连接', '表', '视图', '查询', '工具'):
            assert group in win.ribbon.groups, win.ribbon.groups
        for action_id in ('new_connection', 'new_table', 'design_table',
                          'new_query', 'execute', 'explain', 'import_data',
                          'export_data', 'backup', 'ai_assistant'):
            assert action_id in win.ribbon.action_ids(), action_id
        # 无连接时依赖动作禁用
        assert win.ribbon.button('execute').isEnabled() is False
        assert win.ribbon.button('new_connection').isEnabled() is True
        # 菜单结构（现有动作不丢）
        titles = [a.text().replace('&', '') for a in win.menuBar().actions()]
        for needed in ('文件', '编辑', '视图', '查询', '工具', '窗口', '帮助'):
            assert any(needed in t for t in titles), (needed, titles)
        from PySide6.QtGui import QAction
        all_texts = {a.text() for a in win.findChildren(QAction)}
        for needed in ('新建连接...', '新建查询', '执行', 'EXPLAIN',
                       '保存 SQL...', '新建窗口', '设置...'):
            assert any(needed in t for t in all_texts), (needed, all_texts)
        # 运行时主题切换 + 持久化
        assert win._set_theme_from_menu('light') is True
        assert win.settings.theme == 'light'
        assert cfg.settings.theme == 'light'
        assert win._set_theme_from_menu('dark') is True
        assert cfg.settings.theme == 'dark'
    finally:
        win.close()
        qapp().processEvents()
