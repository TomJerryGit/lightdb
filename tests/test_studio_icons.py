# -*- coding: utf-8 -*-
"""task-52：内置 SVG 图标集（本地优先加载、来源记录、fallback 显式）。"""
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None

REQUIRED_ICONS = [
    'connect', 'new_connection', 'edit_connection', 'refresh',
    'new_table', 'design_table', 'import', 'export', 'backup',
    'new_query', 'execute', 'explain', 'ai', 'settings', 'panel',
]


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


# ==================== 1. SVG 文件齐全 / 内容 ====================

def test_svg_files_complete_and_non_placeholder(home):
    from lightdb_studio.ui.toolbar import ASSET_ICON_DIR
    assert ASSET_ICON_DIR.is_dir()
    for name in REQUIRED_ICONS:
        path = ASSET_ICON_DIR / f'{name}.svg'
        assert path.is_file(), f'缺少图标: {path}'
        text = path.read_text(encoding='utf-8')
        assert '<svg' in text and '</svg>' in text
        assert 'viewBox="0 0 24 24"' in text, name
        assert len(text) > 100, f'{name} 不能是空/占位 SVG'
        assert 'placeholder' not in text.lower()
        # 单色：不得出现多个不同 hex 颜色（允许 #111111 一种）
        import re
        colors = set(re.findall(r'#[0-9a-fA-F]{6}', text))
        assert len(colors) <= 1, (name, colors)


def test_theme_icon_token_and_adapt_color(home):
    from lightdb_studio.ui.theme import DESIGN_TOKENS, icon_color, theme_tokens
    assert DESIGN_TOKENS['icon']['asset_dir'] == 'assets/icons'
    assert DESIGN_TOKENS['icon']['theme_adaptive'] is True
    dark = icon_color('dark')
    light = icon_color('light')
    assert dark and light and dark != light
    assert theme_tokens('dark')['icon']['size'] > 0
    # 高对比度也可用
    assert icon_color('highcontrast')


# ==================== 2. 加载器来源记录 ====================

def test_local_icons_all_constructible_and_source_local(home):
    from lightdb_studio.ui import toolbar
    for name in REQUIRED_ICONS:
        icon = toolbar.make_icon('', 'new', local_name=name)
        assert not icon.isNull(), name
        assert toolbar.icon_source(name) == 'local', (
            name, toolbar.icon_sources())
        assert toolbar.local_icon_path(name).is_file()
    sources = toolbar.icon_sources()
    assert any(s['source'] == 'local' for s in sources)
    assert 'local' in toolbar.icon_source_counts()


def test_missing_local_svg_fallback_explicit(home):
    from lightdb_studio.ui import toolbar
    before_misses = len(toolbar.local_misses())
    icon = toolbar.make_icon('mdi.help', 'new', local_name='__missing_icon__')
    assert not icon.isNull()
    source = toolbar.icon_source('__missing_icon__')
    assert source in ('qtawesome', 'qt-standard'), source
    assert len(toolbar.local_misses()) > before_misses
    assert any('__missing_icon__' in str(m[0]) for m in toolbar.local_misses())
    assert any('本地 SVG 缺失' in str(reason)
               for _n, reason in toolbar.fallback_events())
    # N1 旧接口：不传 local_name 时仍显式记录 fallback（不假成功）
    toolbar.make_icon('mdi.table', 'table')
    assert toolbar.fallback_events()
    assert toolbar.icon_backend() in ('qtawesome', 'fallback')


def test_icon_theme_color_render(home):
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.theme import apply_theme, icon_color
    from lightdb_studio.ui.toolbar import local_icon
    app = QApplication.instance()
    apply_theme(app, 'dark')
    dark_icon = local_icon('execute', color=icon_color('dark'))
    assert not dark_icon.isNull()
    apply_theme(app, 'light')
    light_icon = local_icon('execute', color=icon_color('light'))
    assert not light_icon.isNull()
    # 两种主题的像素颜色不同（同一 SVG 按 icon token 着色）
    d = dark_icon.pixmap(30, 30).toImage()
    l = light_icon.pixmap(30, 30).toImage()
    assert d != l
    apply_theme(app, 'dark')


# ==================== 3. Ribbon / 菜单使用新图标 ====================

def test_ribbon_uses_local_svg_icons(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
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
        # Ribbon 主要动作全部命中本地 SVG
        assert win.ribbon.local_icon_count() >= 15, win.ribbon.icon_source()
        for action_id in ('new_connection', 'new_table', 'design_table',
                          'import_data', 'export_data', 'backup',
                          'new_query', 'execute', 'explain',
                          'ai_assistant', 'settings'):
            assert win.ribbon.icon_source(action_id) == 'local', (
                action_id, win.ribbon.icon_source(action_id))
            assert not win.ribbon.button(action_id).icon().isNull()
        # 主题切换后重新着色不崩、来源仍 local
        assert win._set_theme_from_menu('light') is True
        assert win.ribbon.local_icon_count() >= 15
        assert win._set_theme_from_menu('dark') is True
        assert win.ribbon.local_icon_count() >= 15
        # 菜单主要动作也有图标（含子菜单）
        from PySide6.QtWidgets import QMenu
        menu_actions = []
        for menu in [win.menuBar()] + win.menuBar().findChildren(QMenu):
            menu_actions.extend(a for a in menu.actions()
                                if not a.isSeparator())
        assert any(not a.icon().isNull() for a in menu_actions)
    finally:
        win.close()
        qapp().processEvents()
