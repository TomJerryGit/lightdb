# -*- coding: utf-8 -*-
"""task-88：主题 token 一致性 / 暗色细节 / 高 DPI / 分栏持久化。"""
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

_APP = None


def qapp():
    from PySide6.QtWidgets import QApplication
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.setenv('LIGHTDB_STUDIO_LAYOUT_DIR',
                       str(tmp_path / 'layout'))
    qapp()
    return tmp_path


def test_design_tokens_are_complete_and_typed():
    from lightdb_studio.ui.theme import DESIGN_TOKENS, token
    for group in ('spacing', 'radius', 'font_size', 'icon_size', 'divider',
                  'dpi', 'icon', 'ribbon_button'):
        assert group in DESIGN_TOKENS, group
        assert isinstance(DESIGN_TOKENS[group], dict)
    assert all(isinstance(v, int) and v > 0
               for v in DESIGN_TOKENS['spacing'].values())
    assert all(isinstance(v, int) and v > 0
               for v in DESIGN_TOKENS['radius'].values())
    assert token('spacing', 'md') == 8
    assert token('missing', 'x', 42) == 42


def test_theme_tokens_light_dark_and_qss():
    from lightdb_studio.ui.theme import (
        DESIGN_TOKENS, theme_tokens, theme_qss, available_themes)
    required = {'bg', 'fg', 'panel', 'border', 'hover', 'selection',
                'selection_fg', 'alt_bg'}
    d = theme_tokens('dark')
    l = theme_tokens('light')
    assert d['theme'] == 'dark' and l['theme'] == 'light'
    assert required <= set(d['colors'])
    assert required <= set(l['colors'])
    assert d['colors']['bg'] != l['colors']['bg']
    assert d['spacing']['md'] == l['spacing']['md'] == \
        DESIGN_TOKENS['spacing']['md']
    dark_qss, light_qss = theme_qss('dark'), theme_qss('light')
    assert dark_qss != light_qss
    # 暗色细节：表格交替行/选中/工具条/输入框都由主题色生成
    for needle in ('alternate-background-color', 'selection-background-color',
                   'QToolBar', 'QLineEdit', 'QTabBar'):
        assert needle in dark_qss, needle
    assert d['colors']['selection'] in dark_qss
    assert l['colors']['selection'] in light_qss
    assert 'highcontrast' in available_themes()


def test_apply_theme_observable_and_idempotent(home):
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.theme import apply_theme, theme_qss
    app = QApplication.instance()
    dark = apply_theme(app, 'dark')
    assert dark.name == 'dark'
    assert app.styleSheet() == theme_qss('dark')
    light = apply_theme(app, 'light')
    assert light.name == 'light'
    assert app.styleSheet() == theme_qss('light')
    apply_theme(app, 'dark')


def test_high_dpi_125_150_scaling(home, monkeypatch):
    from lightdb_studio.ui.theme import (
        high_dpi_info, scale_token, DESIGN_TOKENS)
    info = high_dpi_info()
    for key in ('ratio', 'percent', 'dpi', 'scale_percent', 'spacing_md',
                'icon_toolbar', 'rounding_policy'):
        assert key in info
    assert info['rounding_policy'] == 'PassThrough'
    assert scale_token(DESIGN_TOKENS['spacing']['md'], 1.25) == 10
    assert scale_token(DESIGN_TOKENS['spacing']['md'], 1.5) == 12
    assert scale_token(DESIGN_TOKENS['icon_size']['toolbar'], 1.25) >= 22
    assert 1.0 <= info['ratio'] <= 4.0


def test_splitter_ratio_persists(home, tmp_path):
    from PySide6.QtWidgets import QSplitter, QWidget
    from lightdb_studio.ui.layout_store import (
        layout_path, save_splitter_ratios, restore_splitter_ratios,
        connect_splitter_persistence)
    sp = QSplitter()
    sp.setOrientation(__import__('PySide6.QtCore',
                                fromlist=['Qt']).Qt.Vertical)
    sp.addWidget(QWidget())
    sp.addWidget(QWidget())
    sp.resize(400, 600)
    sp.setSizes([200, 400])
    qapp().processEvents()
    assert save_splitter_ratios('query', sp) is True
    assert layout_path().exists()
    sp2 = QSplitter()
    sp2.setOrientation(sp.orientation())
    sp2.addWidget(QWidget())
    sp2.addWidget(QWidget())
    sp2.resize(400, 600)
    restore_splitter_ratios('query', sp2)
    a, b = sp2.sizes()
    assert a > 0 and b > 0
    assert abs(a / (a + b) - 1 / 3) < 0.08
    # 拖动保存回调可调用
    cb = connect_splitter_persistence('query2', sp2)
    assert callable(cb)
    assert cb() is True
