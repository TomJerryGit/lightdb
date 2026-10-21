# -*- coding: utf-8 -*-
"""P2-8 插件 API 产品化测试（offscreen + tmp_path）。

覆盖：
- 示例插件 plugins/example_plugin.py：加载/元数据/菜单/工具栏/process_sql；
- 坏插件（语法错/无类/模块异常）只失败自己，错误可见；
- 启用/禁用：on_unload 调用、active 列表、状态持久化 round-trip、enable 恢复；
- 重载：unload → 清 sys.modules → 重新 import → on_load，新实例可观测；
- 菜单/工具栏注入：build_* 返回 action 文本/属性，install_actions(QMainWindow) 真安装；
- 状态文件损坏：告警 + 回退默认、不崩溃；
- PluginPanel（offscreen）：列表/启停/重载/刷新/安装文件，结果显式。
"""
import json
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from lightdb_studio.core.plugin_manager import (
    PluginManager, PluginBase, MODULE_PREFIX)

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


class _MsgBox:
    def __init__(self):
        self.calls = []

    def _rec(self, kind):
        def fn(*args, **kwargs):
            self.calls.append({'kind': kind,
                               'text': ' | '.join(str(a) for a in args)})
            from PySide6.QtWidgets import QMessageBox
            return QMessageBox.Yes
        return fn

    def texts(self, kind=None):
        return [c['text'] for c in self.calls
                if kind is None or c['kind'] == kind]


@pytest.fixture
def msgbox(monkeypatch):
    box = _MsgBox()
    from PySide6.QtWidgets import QMessageBox
    for name in ('information', 'warning', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(box._rec(name)))
    return box


EXAMPLE = ROOT / 'plugins' / 'example_plugin.py'

MARKER_PLUGIN = '''# -*- coding: utf-8 -*-
from lightdb_studio.core.plugin_manager import PluginBase
from pathlib import Path

name = "标记插件"
version = "2.0.1"
description = "记录 on_load/on_unload 次数"
author = "test"
MARK = Path({marker!r})

def _mark(line):
    with open(MARK, 'a', encoding='utf-8') as f:
        f.write(line + "\\n")

class MarkerPlugin(PluginBase):
    name = name
    version = version

    def on_load(self):
        _mark("load")

    def on_unload(self):
        _mark("unload")

    def get_menu_items(self):
        return [{{'menu': '工具', 'text': '标记：加载',
                  'callback': self.on_load}}]

    def get_toolbar_buttons(self):
        return [{{'text': '标记按钮', 'callback': self.on_load}}]

    def process_sql(self, sql):
        return (sql or "").strip()
'''


def marker_content(marker_path):
    return MARKER_PLUGIN.format(marker=str(marker_path))


def make_mgr(tmp_path, extra=None, marker=True, state_path=None):
    pdir = tmp_path / 'plugins'
    pdir.mkdir(parents=True, exist_ok=True)
    if EXAMPLE.exists():
        shutil.copy2(EXAMPLE, pdir / 'example_plugin.py')
    if marker:
        (pdir / 'marker_plugin.py').write_text(
            marker_content(tmp_path / 'marker.log'), encoding='utf-8')
    for name, content in (extra or {}).items():
        (pdir / name).write_text(content, encoding='utf-8')
    calls = {'messages': []}
    ctx = {'show_message': lambda m: calls['messages'].append(m),
           'get_connection': lambda: None}
    mgr = PluginManager(str(pdir), ctx,
                        state_path=str(state_path or (tmp_path / 'state.json')))
    return mgr, pdir, calls


def read_marker(tmp_path):
    p = tmp_path / 'marker.log'
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding='utf-8').splitlines() if ln]


# ==================== 1. 示例插件 ====================

def test_example_plugin_loads_and_exposes_api(tmp_path):
    mgr, pdir, calls = make_mgr(tmp_path, marker=False)
    results = mgr.load_all()
    assert results
    info = mgr.get('example_plugin')
    assert info is not None and info.status == 'loaded', info
    assert info.name == '示例插件' and info.version == '1.0.0'
    assert '示例' in info.description
    menu = mgr.get_menu_items()
    assert any(i['text'] == '示例：插入 SELECT 1' for i in menu), menu
    toolbar = mgr.get_toolbar_buttons()
    assert any(i['text'] == '✨ 示例插件' for i in toolbar), toolbar
    assert mgr.process_sql('SELECT 1   \n') == 'SELECT 1'
    # 菜单回调安全：无主窗口时走 show_message
    menu[0]['callback']()
    assert any('SELECT 1' in m for m in calls['messages']), calls


def test_load_all_reports_status_per_plugin(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path)
    results = mgr.load_all()
    by_key = {info.key: info for info in results}
    assert by_key['example_plugin'].status == 'loaded'
    assert by_key['marker_plugin'].status == 'loaded'
    assert all(isinstance(i.to_dict()['status'], str) for i in results)


# ==================== 2. 坏插件隔离 ====================

BAD_PLUGINS = {
    'bad_syntax.py': 'def broken(:\n    pass\n',
    'bad_no_class.py': 'name = "无类插件"\nversion = "0.1"\nX = 1\n',
    'bad_raise.py': 'raise RuntimeError("插件模块导入即报错")\n',
    'bad_inherit.py': (
        'class NotAPlugin(object):\n    pass\nname = "继承错"\n'),
}


def test_bad_plugins_do_not_break_others(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path, extra=BAD_PLUGINS)
    mgr.load_all()
    assert mgr.get('example_plugin').status == 'loaded'
    assert mgr.get('marker_plugin').status == 'loaded'
    for stem in ('bad_syntax', 'bad_no_class', 'bad_raise', 'bad_inherit'):
        info = mgr.get(stem)
        assert info is not None, stem
        assert info.status == 'failed', (stem, info.status)
        assert info.error, stem
        assert '加载失败' in info.error or 'PluginBase' in info.error, \
            (stem, info.error)
    active = {i.key for i in mgr.get_active()}
    assert 'example_plugin' in active and 'marker_plugin' in active
    assert not (active & set(BAD_PLUGINS))
    print(f"  → 坏插件错误可见且未影响好插件: {[mgr.get(s).error.splitlines()[0] for s in ('bad_syntax', 'bad_no_class')]}")


def test_bad_plugin_reload_error_visible(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path, extra=BAD_PLUGINS)
    mgr.load_all()
    result = mgr.reload_one('bad_syntax')
    assert result['ok'] is False and result['error']
    assert '加载失败' in result['error']


# ==================== 3. 启用/禁用 + 持久化 ====================

def test_disable_calls_unload_and_persists_state(tmp_path):
    state = tmp_path / 'state.json'
    mgr, _pdir, _calls = make_mgr(tmp_path, state_path=state)
    mgr.load_all()
    assert read_marker(tmp_path) == ['load']
    result = mgr.disable('marker_plugin')
    assert result['ok'] is True, result
    info = mgr.get('marker_plugin')
    assert info.status == 'disabled' and info.enabled is False
    assert info.instance is None
    assert read_marker(tmp_path) == ['load', 'unload']
    assert 'marker_plugin' not in {i.key for i in mgr.get_active()}
    # 状态持久化 round-trip：新 manager 同目录/状态文件 → 禁用生效
    mgr2 = PluginManager(str(tmp_path / 'plugins'), {},
                         state_path=str(state))
    mgr2.load_all()
    info2 = mgr2.get('marker_plugin')
    assert info2.enabled is False and info2.status == 'disabled'
    assert read_marker(tmp_path) == ['load', 'unload'], '禁用插件不应 on_load'
    raw = json.loads(state.read_text(encoding='utf-8'))
    assert 'marker_plugin' in raw['disabled']
    # enable 恢复 + 持久化更新
    result = mgr2.enable('marker_plugin')
    assert result['ok'] is True, result
    assert mgr2.get('marker_plugin').status == 'loaded'
    assert read_marker(tmp_path) == ['load', 'unload', 'load']
    raw = json.loads(state.read_text(encoding='utf-8'))
    assert 'marker_plugin' not in raw['disabled']


def test_enable_disable_unknown_plugin_explicit(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path, marker=False)
    mgr.load_all()
    assert mgr.disable('nope')['ok'] is False
    assert mgr.enable('nope')['ok'] is False
    assert '不存在' in mgr.disable('nope')['error']


def test_on_unload_failure_is_visible(tmp_path):
    bad_unload = '''from lightdb_studio.core.plugin_manager import PluginBase
name = "卸载失败"
version = "1.0"
class P(PluginBase):
    def on_unload(self):
        raise RuntimeError("unload boom")
'''
    mgr, _pdir, _calls = make_mgr(tmp_path, extra={'bad_unload.py': bad_unload},
                                   marker=False)
    mgr.load_all()
    result = mgr.disable('bad_unload')
    assert result['ok'] is False, result
    assert 'on_unload' in result['error'] or 'boom' in result['error']
    info = mgr.get('bad_unload')
    assert info.status == 'disabled'
    assert any('on_unload' in w for w in mgr.warnings)


# ==================== 4. 重载（unload + sys.modules 清理） ====================

def test_reload_one_unloads_and_reimports(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path)
    mgr.load_all()
    old_module = sys.modules.get(MODULE_PREFIX + 'marker_plugin')
    assert old_module is not None
    old_instance = mgr.get('marker_plugin').instance
    result = mgr.reload_one('marker_plugin')
    assert result['ok'] is True, result
    assert read_marker(tmp_path) == ['load', 'unload', 'load']
    new_info = mgr.get('marker_plugin')
    assert new_info.instance is not old_instance, 'reload 后应换新实例'
    new_module = sys.modules.get(MODULE_PREFIX + 'marker_plugin')
    assert new_module is not None and new_module is not old_module, \
        '重载未清理旧 sys.modules 模块'
    assert new_info.status == 'loaded'
    # 监听器收到事件
    events = []
    mgr.add_listener(events.append)
    mgr.reload_one('marker_plugin')
    assert 'reload' in events


def test_reload_disabled_plugin_keeps_disabled(tmp_path):
    mgr, _pdir, _calls = make_mgr(tmp_path)
    mgr.load_all()
    mgr.disable('marker_plugin')
    lines_before = read_marker(tmp_path)
    result = mgr.reload_one('marker_plugin')
    assert result['ok'] is True, result
    info = mgr.get('marker_plugin')
    assert info.enabled is False and info.status == 'disabled'
    assert read_marker(tmp_path) == lines_before, '禁用插件重载不应 on_load'


# ==================== 5. 菜单/工具栏注入 ====================

def test_build_and_install_actions(tmp_path, msgbox):
    from PySide6.QtWidgets import QMainWindow, QToolBar
    qapp()
    mgr, _pdir, _calls = make_mgr(tmp_path)
    mgr.load_all()
    menu_actions = mgr.build_menu_actions()
    toolbar_actions = mgr.build_toolbar_actions()
    menu_texts = [a.text() for a in menu_actions]
    toolbar_texts = [a.text() for a in toolbar_actions]
    assert '示例：插入 SELECT 1' in menu_texts, menu_texts
    assert '标记：加载' in menu_texts, menu_texts
    assert '✨ 示例插件' in toolbar_texts, toolbar_texts
    assert all(a.property('plugin') for a in menu_actions)
    # 真安装到 QMainWindow（menuBar + toolbar）
    win = QMainWindow()
    win.addToolBar(QToolBar('main', win))
    result = mgr.install_actions(win)
    assert result['ok'] is True, result
    assert '示例：插入 SELECT 1' in result['menu_texts']
    assert '✨ 示例插件' in result['toolbar_texts']
    from PySide6.QtWidgets import QMenu
    titles = [m.title().replace('&', '')
              for m in win.menuBar().findChildren(QMenu)]
    assert '工具' in titles, titles
    toolbar = win.findChildren(QToolBar)[0]
    actions_text = [a.text() for a in toolbar.actions()]
    assert '✨ 示例插件' in actions_text, actions_text
    win.close()


# ==================== 6. 状态损坏 / process_sql 容错 ====================

def test_corrupt_state_warns_and_falls_back(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text('{this is not json', encoding='utf-8')
    mgr, _pdir, _calls = make_mgr(tmp_path, state_path=state)
    assert mgr.disabled == set()
    assert any('状态文件' in w for w in mgr.warnings), mgr.warnings
    mgr.load_all()
    assert mgr.get('example_plugin').status == 'loaded'


def test_process_sql_failure_isolated(tmp_path):
    bad_process = '''from lightdb_studio.core.plugin_manager import PluginBase
name = "坏SQL"
version = "1.0"
class P(PluginBase):
    def process_sql(self, sql):
        raise RuntimeError("process boom")
'''
    mgr, _pdir, _calls = make_mgr(tmp_path, extra={'bad_process.py': bad_process})
    mgr.load_all()
    out = mgr.process_sql('  SELECT 2  ')
    assert out == 'SELECT 2', out
    assert mgr.get('bad_process').last_error
    assert mgr.get('example_plugin').status == 'loaded'


# ==================== 7. PluginPanel（offscreen） ====================

def _make_panel(tmp_path, extra=None, msgbox=None):
    from lightdb_studio.ui.plugin_panel import PluginPanel
    qapp()
    mgr, pdir, _calls = make_mgr(tmp_path, extra=extra)
    mgr.load_all()
    panel = PluginPanel(mgr)
    panel.show()
    qapp().processEvents()
    return panel, mgr, pdir


def test_plugin_panel_lists_and_toggles(tmp_path, msgbox):
    panel, mgr, _pdir = _make_panel(tmp_path, extra=BAD_PLUGINS)
    assert panel.table.columnCount() == 5
    headers = [panel.table.horizontalHeaderItem(i).text()
               for i in range(5)]
    assert headers == ['插件名', '版本', '状态', '文件', '错误']
    assert panel.table.rowCount() == 6  # example + marker + 4 bad
    assert panel._select_key('marker_plugin')
    result = panel.set_enabled_selected(False)
    assert result['ok'] is True, result
    assert mgr.get('marker_plugin').status == 'disabled'
    assert panel._select_key('marker_plugin')
    result = panel.set_enabled_selected(True)
    assert result['ok'] is True
    assert mgr.get('marker_plugin').status == 'loaded'
    panel.close()


def test_plugin_panel_reload_install_and_errors(tmp_path, msgbox):
    panel, mgr, pdir = _make_panel(tmp_path)
    assert panel._select_key('marker_plugin')
    result = panel.reload_selected()
    assert result['ok'] is True
    # 安装新插件文件
    src = tmp_path / 'new_plugin.py'
    src.write_text(marker_content(tmp_path / 'installed.log').replace(
        'marker_plugin', 'new_plugin'), encoding='utf-8')
    result = panel.install_plugin_files([str(src)])
    assert result['ok'] is True and result['installed'] == 1, result
    assert (pdir / 'new_plugin.py').exists()
    assert mgr.get('new_plugin') is not None
    # 安装失败显式
    result_bad = panel.install_plugin_files([str(tmp_path / 'not_exists.py')])
    assert result_bad['ok'] is False and result_bad['errors']
    assert any('不存在' in e or 'not_exists' in e
               for e in result_bad['errors'])
    # 刷新 + 未选择操作显式失败
    panel.refresh_plugins()
    panel.table.clearSelection()
    panel.table.setCurrentCell(-1, -1)
    assert panel.reload_selected()['ok'] is False
    panel.close()


def test_plugin_panel_install_can_load_bad_plugin_visible(tmp_path, msgbox):
    panel, mgr, pdir = _make_panel(tmp_path)
    bad_src = tmp_path / 'broken_plugin.py'
    bad_src.write_text('def x(:\n', encoding='utf-8')
    result = panel.install_plugin_files([str(bad_src)])
    assert result['ok'] is True and result['installed'] == 1
    info = mgr.get('broken_plugin')
    assert info is not None and info.status == 'failed' and info.error
    panel.close()


# ==================== P2-8：主窗口启动时安装插件 action ====================

def test_main_window_installs_plugin_actions_on_startup():
    """`MainWindow._install_plugin_actions()` 必须调用 install_actions(self)。

    插件 action 只在启动时安装一次（面板启停后不自动重装，避免菜单/工具栏
    重复堆积）；安装错误写 stderr、不崩溃。
    """
    from lightdb_studio.ui.main_window import MainWindow

    called = []

    class _Mgr:
        def __init__(self, errors=None):
            self.errors = errors or []

        def install_actions(self, win):
            called.append(win)
            return {'ok': not self.errors, 'errors': list(self.errors)}

    class _Win:
        def __init__(self, mgr):
            self.plugin_mgr = mgr

    win = _Win(_Mgr())
    MainWindow._install_plugin_actions(win)
    assert called == [win]

    # errors 不为空：不抛异常（只写 stderr），调用仍发生
    win2 = _Win(_Mgr(errors=['bad plugin']))
    MainWindow._install_plugin_actions(win2)
    assert called[-1] is win2

    # manager 不支持 install_actions：直接返回
    class _NoInstall:
        pass

    MainWindow._install_plugin_actions(_Win(_NoInstall()))


# ==================== 现场 bug 回归：示例插件「插入 SELECT 1」 ====================

def test_sql_editor_append_sql_uses_scoped_enum():
    """GUI 现场点「示例插件」暴露：`cur.End` 在 PySide6 6.x 不存在。

    回归：`SQLEditor.append_sql()` 必须能把 SQL 追加到编辑器（空/非空两条路径），
    不能抛 `AttributeError: QTextCursor has no attribute End`。
    """
    qapp()
    from lightdb_studio.ui.sql_editor import SQLEditor
    ed = SQLEditor()
    ed.append_sql('SELECT 1')
    assert ed.toPlainText() == 'SELECT 1'
    ed.append_sql('SELECT 2')
    text = ed.toPlainText()
    assert 'SELECT 1' in text and 'SELECT 2' in text
    assert text.index('SELECT 1') < text.index('SELECT 2')
    ed.close()


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
