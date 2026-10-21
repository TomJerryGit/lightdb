# -*- coding: utf-8 -*-
"""P2 i18n 覆盖率 + 可访问性测试（offscreen，无真实数据库）。

覆盖：
- zh_CN / en_US key 集合一致（check_coverage）、常用 key 与 {n} 格式化、
  未知 key 回退 key 且记录 missing、t_strict 缺失抛 KeyError；
- MainWindow 关键控件 accessibleName/工具提示、tab order 设置存在且无重复；
- 语言热切换：i18n.set_lang 后菜单/工具栏/状态栏/标签页/编辑器空态立即英文，
  切回中文恢复；英文模式下已翻译菜单/工具栏不含 CJK；
- 设置对话框：语言选择 + Apply → settings.language 持久化、i18n 立即切换、
  主窗口热重译；高 DPI 项明确显示“自动/由系统控制”（不静默无效）。
"""
import importlib
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None

CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture(autouse=True)
def _reset_i18n():
    from lightdb_studio.utils.i18n import i18n
    i18n.set_lang('zh_CN')
    i18n.reset_missing()
    yield
    i18n.set_lang('zh_CN')
    i18n.reset_missing()


_HOME_CONSTANTS = [
    ('lightdb_studio.config', 'CONFIG_DIR', 'cfg'),
    ('lightdb_studio.config', 'CONFIG_FILE', 'cfg/connections.json'),
    ('lightdb_studio.config', 'SETTINGS_FILE', 'cfg/settings.json'),
    ('lightdb_studio.core.alerts', 'ALERTS_FILE', 'alerts.json'),
    ('lightdb_studio.core.catalog', 'CATALOG_FILE', 'catalog.json'),
    ('lightdb_studio.core.cloud_sync', 'SYNC_DIR', 'sync'),
    ('lightdb_studio.core.cloud_sync', 'CONFIG_ROOT', 'cfg'),
    ('lightdb_studio.core.collaboration', 'COLLAB_FILE', 'collaboration.json'),
    ('lightdb_studio.core.environments', 'ENV_FILE', 'environments.json'),
    ('lightdb_studio.core.lineage', 'LINEAGE_FILE', 'lineage.json'),
    ('lightdb_studio.core.lineage_enhanced', 'ENHANCED_LINEAGE_FILE',
     'lineage_v2.json'),
    ('lightdb_studio.core.masking', 'MASK_RULES_FILE', 'masking.json'),
    ('lightdb_studio.core.notifications', 'NOTIFY_FILE', 'notifications.json'),
    ('lightdb_studio.core.quality_v2', 'QUALITY_RULES_FILE',
     'quality_rules.json'),
    ('lightdb_studio.core.reports', 'REPORT_DIR', 'reports'),
    ('lightdb_studio.core.review_workflow', 'REVIEW_FILE', 'reviews.json'),
    ('lightdb_studio.core.scheduler', 'SCHEDULE_FILE', 'schedules.json'),
    ('lightdb_studio.core.snapshot', 'SNAPSHOT_DIR', 'snapshots'),
    ('lightdb_studio.core.snippets', 'SNIPPET_DIR', 'snippets'),
    ('lightdb_studio.core.workflow', 'WORKFLOW_FILE', 'workflows.json'),
    ('lightdb_studio.utils.history', 'HISTORY_FILE', 'history.json'),
]


@pytest.fixture
def main_window(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    for mod_name, attr, rel in _HOME_CONSTANTS:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, tmp_path / rel)
    import lightdb_studio.core.snippets as snippets_mod
    if hasattr(snippets_mod, 'SHARED_SNIPPETS'):
        monkeypatch.setattr(snippets_mod, 'SHARED_SNIPPETS',
                            tmp_path / 'snippets' / 'shared')

    qapp()
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
    from lightdb_studio.utils.i18n import i18n
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    cfg.settings.language = 'zh_CN'
    i18n.set_lang('zh_CN')
    win = MainWindow(cfg)
    # 避免事件循环里延迟启动（插件注入会计入菜单文本，不影响本测试目标）
    win.do_startup_restore = lambda: None
    win.history.path = tmp_path / 'history.json'
    yield win
    try:
        win._query_pool.waitForDone(2000)
    except Exception:
        pass
    try:
        win.close()
    except Exception:
        pass
    i18n.set_lang('zh_CN')
    qapp().processEvents()


# ==================== 1. i18n 字典 / 格式化 / missing ====================

REQUIRED_KEYS = [
    'app.title', 'menu.file', 'menu.query', 'menu.tools', 'action.execute',
    'action.settings', 'toolbar.execute', 'status.ready', 'status.connected',
    'status.disconnected', 'editor.empty_hint', 'result.empty',
    'settings.title', 'settings.language', 'a11y.main_window',
]


def test_language_key_sets_identical():
    from lightdb_studio.utils.i18n import i18n, STRINGS
    report = i18n.check_coverage()
    assert report['ok'] is True, report
    assert len(i18n.key_set('zh_CN')) == len(i18n.key_set('en_US'))
    assert i18n.key_set('zh_CN') == i18n.key_set('en_US')
    assert len(i18n.key_set('zh_CN')) >= 120
    for key in REQUIRED_KEYS:
        assert key in STRINGS['zh_CN'], key
        assert key in STRINGS['en_US'], key
        assert STRINGS['zh_CN'][key].strip()
        assert STRINGS['en_US'][key].strip()


def test_translation_formatting_and_unknown_key():
    from lightdb_studio.utils.i18n import i18n
    i18n.set_lang('zh_CN')
    assert i18n.t('status.ready') == '就绪'
    assert i18n.t('result.rows', n=3) == '返回 3 行'
    i18n.set_lang('en_US')
    assert i18n.t('status.ready') == 'Ready'
    assert i18n.t('result.rows', n=3) == '3 row(s) returned'
    # 未知 key：原样返回 + missing 记录，不崩溃
    i18n.reset_missing()
    assert i18n.t('no.such.key') == 'no.such.key'
    assert 'no.such.key' in i18n.missing_report()['missing']
    assert i18n.missing('no.such.key') is True
    # 严格查询：缺失抛 KeyError
    with pytest.raises(KeyError):
        i18n.t_strict('no.such.key')
    with pytest.raises(KeyError):
        i18n.t_strict('another.missing.key')
    assert 'another.missing.key' in i18n.missing_report()['missing']
    # 未知语言不切换
    assert i18n.set_lang('xx_YY') is False
    assert i18n.lang == 'en_US'
    assert any('未知语言' in w for w in i18n.missing_report()['lang_warnings'])


# ==================== 2. MainWindow 可访问性 / tab order ====================

def test_main_window_accessible_names(main_window):
    from lightdb_studio.utils.i18n import i18n
    win = main_window
    assert win.accessibleName() == i18n.t('a11y.main_window')
    assert win.object_tree.accessibleName() == i18n.t('a11y.object_tree')
    assert win.object_tree.tree.accessibleName() == i18n.t('a11y.object_tree')
    assert win.tabs.accessibleName() == i18n.t('a11y.tabs')
    assert win.status_label.accessibleName() == i18n.t('a11y.status')
    editor, result = win._current_widgets()
    assert editor.accessibleName() == i18n.t('a11y.sql_editor')
    assert editor.toolTip() == i18n.t('a11y.sql_editor')
    assert result.accessibleName() == i18n.t('a11y.result_table')
    assert win._main_toolbar.accessibleName() == i18n.t('a11y.toolbar')
    # 执行/取消动作：文本/提示非空
    assert win._cancel_action.text()
    assert win._cancel_action.toolTip()
    # 菜单项文本非空（QAction 文本即 accessible 文本）
    menu_actions = [a for m in win.menuBar().actions() if m.menu()
                    for a in m.menu().actions() if not a.isSeparator()]
    assert menu_actions
    assert all(a.text() for a in menu_actions)
    print(f"  → a11y: menu={win.menuBar().accessibleName()!r} "
          f"editor={editor.accessibleName()!r} tab_order={len(win.tab_order_widgets)}")


def test_main_window_tab_order_configured(main_window):
    win = main_window
    assert win._tab_order_configured is True
    widgets = win.tab_order_widgets
    assert len(widgets) >= 3, widgets
    assert len({id(w) for w in widgets}) == len(widgets), 'tab order 有重复控件'
    # 编辑器必须在结果表之前，对象树在最前
    editor, result = win._current_widgets()
    names = [getattr(w, 'objectName', lambda: '')() for w in widgets]
    idx_editor = widgets.index(editor)
    idx_result = widgets.index(result)
    assert widgets.index(win.object_tree.tree) == 0
    assert idx_editor < idx_result, (idx_editor, idx_result)
    assert all(w is not None for w in widgets)
    print(f"  → tab order: {[type(w).__name__ for w in widgets]}")


# ==================== 3. 语言热切换 ====================

def _menu_titles(win):
    return [a.text() for a in win.menuBar().actions()]


def test_hot_switch_retranslates_ui(main_window):
    from lightdb_studio.utils.i18n import i18n
    win = main_window
    qapp().processEvents()
    zh_titles = _menu_titles(win)
    assert any('文件' in t for t in zh_titles), zh_titles
    assert win.status_label.text() == '就绪'
    assert win.conn_label.text() == '未连接'
    assert win.tabs.tabText(0) == '查询'
    editor, result = win._current_widgets()
    assert 'Ctrl+Enter' in editor.placeholderText()

    # 切英文（设置里 Apply 走的就是这个 API）
    assert i18n.set_lang('en_US') is True
    qapp().processEvents()
    en_titles = _menu_titles(win)
    assert any(t.replace('&', '') == 'File' for t in en_titles), en_titles
    assert win.status_label.text() == 'Ready'
    assert win.conn_label.text() == 'Disconnected'
    assert win.tabs.tabText(0) == 'Query'
    from lightdb_studio import __version__ as studio_version
    assert win.windowTitle() == f"{i18n.t('app.title')} v{studio_version}"
    assert 'Ctrl+Enter' in editor.placeholderText()
    assert 'Type SQL' in editor.placeholderText()
    # 工具栏动作文本英文
    toolbar_texts = [a.text() for a in win._main_toolbar.actions()
                     if not a.isSeparator()]
    assert any('Execute' in t for t in toolbar_texts), toolbar_texts
    # 已翻译的菜单/工具栏项不含 CJK
    for key in win._i18n_menus:
        assert not CJK_RE.search(i18n.t(key)), key
    for key in win._i18n_actions:
        assert not CJK_RE.search(i18n.t(key)), key
    for key in win._i18n_toolbar:
        assert not CJK_RE.search(i18n.t(key)), key
    assert win.accessibleName() == i18n.t('a11y.main_window')
    assert editor.accessibleName() == i18n.t('a11y.sql_editor')

    # 切回中文立即恢复
    assert i18n.set_lang('zh_CN') is True
    qapp().processEvents()
    assert any('文件' in t for t in _menu_titles(win))
    assert win.status_label.text() == '就绪'
    assert win.conn_label.text() == '未连接'
    assert win.tabs.tabText(0) == '查询'
    assert '输入 SQL' in editor.placeholderText()
    print(f"  → 热切换: zh {zh_titles[:2]} → en {en_titles[:2]} → zh 恢复")


# ==================== 4. 设置对话框：语言持久化 + 高 DPI 明确 ====================

def test_settings_language_apply_persists_and_hot_switches(main_window, tmp_path):
    from lightdb_studio.ui.settings_dialog import SettingsDialog
    from lightdb_studio.utils.i18n import i18n
    from lightdb_studio.config import Settings, SETTINGS_FILE
    win = main_window
    dlg = SettingsDialog(win.settings, win)
    assert dlg.lang_combo.count() == 2
    assert dlg.lang_combo.accessibleName()
    # 高 DPI：明确显示“自动/由系统控制”，不是静默无效设置
    assert dlg.high_dpi_note.text().strip()
    assert ('Qt6' in dlg.high_dpi_note.text()
            or '系统' in dlg.high_dpi_note.text())
    assert '自动' in dlg.dpi_label.text() or 'Auto' in dlg.dpi_label.text()
    # 切英文并 Apply
    dlg.lang_combo.setCurrentIndex(1)      # en_US
    result = dlg.apply_settings()
    assert result['ok'] is True
    assert win.settings.language == 'en_US'
    assert i18n.lang == 'en_US'
    # 主窗口立即热重译（无需重启）
    qapp().processEvents()
    assert any(t.replace('&', '') == 'File' for t in _menu_titles(win))
    assert win.status_label.text() == 'Ready'
    # settings.json 持久化 round-trip
    assert SETTINGS_FILE.exists(), SETTINGS_FILE
    saved = Settings.load()
    assert saved.language == 'en_US'
    raw = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    assert raw['language'] == 'en_US'
    # 切回中文
    dlg.lang_combo.setCurrentIndex(0)
    dlg.apply_settings()
    assert Settings.load().language == 'zh_CN'
    assert i18n.lang == 'zh_CN'
    qapp().processEvents()
    assert any('文件' in t for t in _menu_titles(win))
    dlg.close()


def test_settings_dialog_controls_a11y(main_window):
    from lightdb_studio.ui.settings_dialog import SettingsDialog
    win = main_window
    dlg = SettingsDialog(win.settings, win)
    for attr in ('lang_combo', 'theme_combo', 'page_size_spin',
                 'auto_connect_check', 'font_family_combo',
                 'font_size_spin', 'tab_size_spin', 'auto_complete_check',
                 'query_timeout_spin', 'max_rows_spin', 'clear_cache_btn',
                 'reset_btn', 'high_dpi_note'):
        widget = getattr(dlg, attr)
        assert widget.accessibleName(), attr
    assert dlg.accessibleName() == dlg.windowTitle() or dlg.accessibleName()
    assert dlg.tabs.accessibleName()
    dlg.close()


# ==================== 5. 默认语言来自 settings ====================

def test_new_window_uses_persisted_language(tmp_path, monkeypatch):
    home = tmp_path / 'home2'
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    for mod_name, attr, rel in _HOME_CONSTANTS:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, tmp_path / rel)
    qapp()
    from lightdb_studio.config import AppConfig, Settings
    from lightdb_studio.ui.main_window import MainWindow
    from lightdb_studio.utils.i18n import i18n
    s = Settings(language='en_US', auto_connect=False)
    s.save()
    i18n.set_lang('zh_CN')
    win = MainWindow(AppConfig())
    win.do_startup_restore = lambda: None
    try:
        qapp().processEvents()
        assert win.settings.language == 'en_US'
        assert i18n.lang == 'en_US'
        assert any(t.replace('&', '') == 'File' for t in _menu_titles(win))
        assert win.status_label.text() == 'Ready'
    finally:
        win.close()
        i18n.set_lang('zh_CN')


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
