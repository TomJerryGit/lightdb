# -*- coding: utf-8 -*-
"""P1-3 编辑器增强（自动补全 / SQL 错误位置高亮）+ P1-6 对象树懒加载/右键菜单。

offescreeen Qt，无真实服务端；对象树用记录调用次数的假连接。
覆盖：
1. 补全器按假 schema 返回表/列/关键字候选；上下文 `表.` 只给该表列；
   `editor_auto_complete=False` 时不弹且不读 schema；Ctrl+Return 执行不变。
2. 错误 token 定位（near 'token' / at line N / Unknown column）→ 选中标红；
   找不到时不崩溃、不高亮；成功执行/新 SQL 清除高亮。
3. 对象树：未展开表不查列；展开只查一次；重复展开命中缓存；刷新此表强制重查；
   100 张表展开表文件夹不查列。右键菜单：复制表名（剪贴板）、生成
   SELECT LIMIT 200（新查询页信号）、查看建表语句（query_requested）、
   表文件夹刷新/复制库名。
4. MainWindow 接线：new_query_requested → 新查询页填入 SQL（不自动执行）；
   同步查询错误 → 编辑器定位高亮；成功 → 清除。
"""
import importlib
import os
import sys
import time

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


# ==================== 假连接（对象树测试） ====================

class FakeConn:
    def __init__(self, n_tables=3, fail_columns=False):
        from types import SimpleNamespace
        self.config = SimpleNamespace(
            name='fake-conn', host='127.0.0.1', port=9999,
            user='root', database='fake_db')
        self.tables = [f't{i:03d}' for i in range(n_tables)]
        self.fail_columns = fail_columns
        self.calls = {'get_databases': 0, 'get_tables': 0, 'get_columns': 0,
                      'switch_database': 0, 'execute': 0, 'query': []}

    def is_connected(self):
        return True

    def get_databases(self):
        self.calls['get_databases'] += 1
        return ['fake_db']

    def get_tables(self):
        self.calls['get_tables'] += 1
        return list(self.tables)

    def get_columns(self, table):
        self.calls['get_columns'] += 1
        if self.fail_columns:
            raise RuntimeError('columns failed')
        return [
            {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
             'default': None, 'extra': ''},
            {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES', 'key': '',
             'default': None, 'extra': ''},
        ]

    def query(self, sql, params=None):
        self.calls['query'].append(sql)
        up = sql.upper()
        if up.startswith('SHOW INDEX'):
            return (['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                     'Column_name'],
                    [(t, 0, 'PRIMARY', 1, 'id') for t in self.tables[:1]])
        if up.startswith('SHOW TRIGGERS'):
            return (['Trigger', 'Event', 'Table', 'Statement', 'Timing'],
                    [('trg_x', 'INSERT', self.tables[0], 'SET @x=1', 'BEFORE')])
        if up.startswith('SHOW CREATE TABLE'):
            return ['Table', 'Create Table'], [
                (self.tables[0], 'CREATE TABLE `t000` (`id` INT)')]
        if up.startswith('SELECT COUNT'):
            return ['COUNT(*)'], [(0,)]
        return [], []

    def execute(self, sql, params=None):
        self.calls['execute'] += 1
        return 1, None

    def switch_database(self, db):
        self.calls['switch_database'] += 1

    def close(self):
        pass


def make_tree(n_tables=3, fail_columns=False):
    from lightdb_studio.ui.object_tree import ObjectTree
    qapp()
    tree = ObjectTree()
    conn = FakeConn(n_tables=n_tables, fail_columns=fail_columns)
    from types import SimpleNamespace
    info = SimpleNamespace(name='fake-conn', host='127.0.0.1', port=9999,
                           user='root')
    tree.add_connection(info, conn)
    tree.show()
    qapp().processEvents()
    root = tree.tree.topLevelItem(0)
    db_item = root.child(0)
    return tree, conn, root, db_item


def expand_db(db_item):
    """展开数据库节点（触发懒加载），返回 '表' 文件夹。"""
    from PySide6.QtCore import Qt
    db_item.setExpanded(True)
    qapp().processEvents()
    for i in range(db_item.childCount()):
        child = db_item.child(i)
        data = child.data(0, Qt.UserRole)
        if data and data[0] == 'tables':
            child.setExpanded(True)
            qapp().processEvents()
            return child
    raise AssertionError('没有表文件夹')


def table_item(tables_folder, name):
    from PySide6.QtCore import Qt
    for i in range(tables_folder.childCount()):
        child = tables_folder.child(i)
        data = child.data(0, Qt.UserRole)
        if data and data[0] == 'table' and data[1] == name:
            return child
    raise AssertionError(f'没有表节点 {name}')


def find_menu_action(menu, text):
    for action in menu.actions():
        if action.text() == text:
            return action
    raise LookupError(f'{text!r}; 现有: {[a.text() for a in menu.actions()]}')


# ==================== 1. 自动补全 ====================

def test_completion_candidates_from_fake_schema():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.set_schema_provider(lambda: (
        ['users', 'orders'],
        {'users': ['id', 'name', 'email'], 'orders': ['id', 'amount']}))
    all_cands = editor.completion_candidates('')
    for expected in ('SELECT', 'users', 'orders', 'email', 'amount'):
        assert expected in all_cands, (expected, all_cands[:20])
    us = editor.completion_candidates('us')
    assert us and all('us' in c.lower() for c in us), us
    # 上下文 `users.` → 只给 users 的列（+ 关键字前缀过滤后）
    ctx = editor.completion_candidates('', context_table='users')
    assert 'email' in ctx and 'name' in ctx
    assert 'amount' not in ctx, '上下文表 users 不应出现 orders 的列'
    print(f"  → 全部候选 {len(all_cands)} 个；'us' → {us[:5]}")


def test_show_completion_and_respect_auto_complete_flag():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    calls = {'n': 0}

    def provider():
        calls['n'] += 1
        return ['users'], {'users': ['id']}

    editor.set_schema_provider(provider)
    editor.show()
    assert editor.auto_complete_enabled() is True
    assert editor.show_completion(force=True) is True
    assert editor.visible_completion_count() > 0

    editor.set_auto_complete(False)
    calls['n'] = 0
    assert editor.auto_complete_enabled() is False
    assert editor.show_completion(force=False) is False, '关闭后不应弹补全'
    assert editor._maybe_show_completion() is False
    assert calls['n'] == 0, '关闭后不应再读 schema'
    try:
        editor.completer.popup().hide()
    except Exception:
        pass
    editor.close()
    print(f"  → 开启时候选 {editor.visible_completion_count()} 个；关闭后不弹、不读 schema")


def test_insert_completion_replaces_current_word():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.set_schema_provider(lambda: (['users'], {'users': ['id']}))
    editor.setPlainText('SEL')
    from PySide6.QtGui import QTextCursor
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    editor.setTextCursor(cursor)
    editor._insert_completion('SELECT')
    assert editor.toPlainText() == 'SELECT', editor.toPlainText()


def test_ctrl_return_still_executes():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.setPlainText('SELECT 1')
    editor.show()
    editor.setFocus()
    QTest.qWait(20)
    captured = []
    editor.execute_requested.connect(captured.append)
    QTest.keyClick(editor, Qt.Key_Return, Qt.ControlModifier)
    QApplication.processEvents()
    assert captured and captured[-1] == 'SELECT 1', captured
    print(f"  → Ctrl+Return 触发 {len(captured)} 次")
    editor.close()


# ==================== 2. 错误位置高亮 ====================

def test_error_token_near_is_highlighted_and_selected():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.show()
    editor.setPlainText('SELECT * FROM users\nSELCT 1')
    info = editor.highlight_error_token(
        1064, "错误码  : 1064\n报文    : You have an error in your SQL "
              "syntax near 'SELCT 1'")
    assert info is not None, 'near token 未定位'
    assert editor.has_error_highlight()
    assert editor.textCursor().selectedText() == 'SELCT 1'
    assert info['line'] == 2, info
    assert len(editor.extraSelections()) >= 2, '错误高亮未加到 ExtraSelection'
    print(f"  → 定位 {info}")


def test_error_named_column_highlighted():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.setPlainText('SELECT bad_col FROM users')
    info = editor.highlight_error_token(
        1054, "Unknown column 'bad_col' in 'field list'")
    assert info is not None and info['token'] == 'bad_col'
    assert 'bad_col' in editor.textCursor().selectedText()


def test_error_line_number_highlighted():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.setPlainText('SELECT 1\nSELECT 2\nSELECT 3')
    info = editor.highlight_error_token(1064, '语法错误 at line 2')
    assert info is not None and info['line'] == 2, info
    assert editor.has_error_highlight()
    assert 'SELECT 2' in editor.textCursor().selectedText()


def test_error_without_locatable_token_does_not_fake_position():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.setPlainText('SELECT 1')
    info = editor.highlight_error_token(1146, "Table 'nope' doesn't exist")
    assert info is None, info
    assert not editor.has_error_highlight(), '定位不到时不应伪造高亮'
    # 不崩溃、不选中
    assert editor.textCursor().selectedText() == ''


def test_clear_highlight_on_success_and_set_sql():
    from lightdb_studio.ui.sql_editor import SQLEditor
    qapp()
    editor = SQLEditor()
    editor.setPlainText('SELCT 1')
    assert editor.highlight_error_token(
        1064, "near 'SELCT 1'") is not None
    editor.clear_error_highlight()
    assert not editor.has_error_highlight()
    assert not editor.extraSelections() or len(editor.extraSelections()) == 1
    # 新 SQL 自动清空
    assert editor.highlight_error_token(
        1064, "near 'SELCT 1'") is not None
    editor.set_sql('SELECT 2')
    assert not editor.has_error_highlight()


# ==================== 3. 对象树懒加载 ====================

def test_table_children_not_loaded_before_expand():
    tree, conn, root, db_item = make_tree(3)
    assert conn.calls['get_columns'] == 0, '未展开就查了列'
    assert conn.calls['get_tables'] == 0, '未展开数据库就查表'
    folder = expand_db(db_item)
    assert conn.calls['get_tables'] == 1
    assert conn.calls['get_columns'] == 0, '展开表文件夹不应查每张表的列'
    assert folder.childCount() == 3
    assert tree.table_cache_size() == 0


def test_expand_table_loads_once_then_cache():
    tree, conn, root, db_item = make_tree(3)
    folder = expand_db(db_item)
    item = table_item(folder, 't000')
    item.setExpanded(True)                       # 触发 _on_expand → 懒加载
    qapp().processEvents()
    assert conn.calls['get_columns'] == 1, conn.calls
    assert tree.table_cache_size() == 1
    n_children = item.childCount()
    assert n_children >= 2, '表节点应加载列/索引/触发器子项'
    # 列节点存在
    from PySide6.QtCore import Qt
    types = [((item.child(i).data(0, Qt.UserRole) or (None,))[0])
             for i in range(n_children)]
    assert 'column' in types, types
    # 折叠再展开 → 不重复查询
    item.setExpanded(False)
    item.setExpanded(True)
    qapp().processEvents()
    assert conn.calls['get_columns'] == 1, '重复展开又查了一次列'
    # 强制刷新（右键「刷新此表」）→ 重查一次
    tree._reload_table(item)
    assert conn.calls['get_columns'] == 2
    print(f"  → 首展 get_columns=1；复展仍 1；强制刷新=2；子项 {n_children} 个")


def test_expand_failed_columns_is_safe():
    tree, conn, root, db_item = make_tree(1, fail_columns=True)
    folder = expand_db(db_item)
    item = table_item(folder, 't000')
    item.setExpanded(True)
    qapp().processEvents()
    assert conn.calls['get_columns'] == 1
    assert item.childCount() >= 1, '失败也要有占位/索引节点，不能崩'


def test_hundred_tables_expand_is_fast_and_lazy():
    tree, conn, root, db_item = make_tree(100)
    t0 = time.time()
    folder = expand_db(db_item)
    elapsed = time.time() - t0
    assert folder.childCount() == 100
    assert conn.calls['get_columns'] == 0
    assert elapsed < 2.0, f'100 张表展开花了 {elapsed:.2f}s'
    print(f"  → 100 张表展开 {elapsed * 1000:.1f}ms，get_columns=0")


# ==================== 4. 对象树右键菜单 ====================

def test_table_context_menu_actions_and_effects():
    from PySide6.QtWidgets import QApplication
    tree, conn, root, db_item = make_tree(3)
    folder = expand_db(db_item)
    item = table_item(folder, 't001')

    new_query_sql = []
    query_sql = []
    tree.new_query_requested.connect(new_query_sql.append)
    tree.query_requested.connect(query_sql.append)

    menu, handlers = tree.build_context_menu(item)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    for required in ('复制表名', '生成 SELECT LIMIT 200（新查询页）',
                     '查看建表语句', '查看列信息', '刷新此表'):
        assert required in texts, (required, texts)

    handlers[find_menu_action(menu, '复制表名')]()
    assert QApplication.clipboard().text() == 't001'

    handlers[find_menu_action(menu, '生成 SELECT LIMIT 200（新查询页）')]()
    assert new_query_sql == ['SELECT * FROM `t001` LIMIT 200'], new_query_sql

    handlers[find_menu_action(menu, '查看建表语句')]()
    assert query_sql[-1] == 'SHOW CREATE TABLE `t001`'
    print(f"  → 菜单项 {len(texts)} 个；复制/SELECT/DDL 均生效")


def test_table_folder_context_menu_copy_db():
    from PySide6.QtWidgets import QApplication
    tree, conn, root, db_item = make_tree(2)
    folder = expand_db(db_item)
    menu, handlers = tree.build_context_menu(folder)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert '刷新' in texts and '复制库名' in texts, texts
    handlers[find_menu_action(menu, '复制库名')]()
    assert QApplication.clipboard().text() == 'fake_db'


def test_db_context_menu_has_copy_name():
    tree, conn, root, db_item = make_tree(2)
    menu, _ = tree.build_context_menu(db_item)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert '复制库名' in texts and '刷新' in texts, texts


# ==================== 5. MainWindow 接线（无服务端） ====================

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


class _ErrorConn:
    """同步 `_execute_sql` 用：is_connected True，查询抛带 near token 的错误。"""

    def __init__(self):
        from types import SimpleNamespace
        self.config = SimpleNamespace(name='err-conn', host='127.0.0.1',
                                      port=1, user='root')
        self.fail = False

    def is_connected(self):
        return True

    def query(self, sql, params=None):
        if self.fail:
            raise RuntimeError(
                "错误码: 1064\n报文: You have an error in your SQL syntax "
                "near 'SELCT 1'")
        return ['n'], [(1,)]

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError(
                "错误码: 1064\n报文: You have an error in your SQL syntax "
                "near 'SELCT 1'")
        return 1, None


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
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    win.history.path = tmp_path / 'history.json'
    yield win
    try:
        win._query_pool.waitForDone(2000)
    except Exception:
        pass
    win.close()
    qapp().processEvents()


def test_main_window_new_query_signal_fills_new_tab(main_window):
    win = main_window
    before = win.tabs.count()
    win.object_tree.new_query_requested.emit('SELECT * FROM `t` LIMIT 200')
    qapp().processEvents()
    assert win.tabs.count() == before + 1, '没有新建查询页'
    editor, _ = win._current_widgets()
    assert editor.toPlainText() == 'SELECT * FROM `t` LIMIT 200'
    assert not win._query_busy, '生成 SELECT 不应自动执行'
    print("  → 新查询页已填入 SELECT，且未自动执行")


def test_main_window_error_highlight_and_clear(main_window):
    win = main_window
    conn = _ErrorConn()
    win._get_current_conn = lambda: conn
    editor, result = win._current_widgets()
    editor.set_sql('SELCT 1')
    conn.fail = True
    win._execute_sql('SELCT 1', editor, result)     # 同步路径（保持兼容）
    qapp().processEvents()
    assert win.status_label.text().startswith('错误')
    assert editor.has_error_highlight(), '查询错误未在编辑器定位'
    assert 'SELCT' in editor.textCursor().selectedText()
    # 成功执行 → 清除高亮
    conn.fail = False
    win._execute_sql('SELECT 1', editor, result)
    qapp().processEvents()
    assert not editor.has_error_highlight(), '成功执行后错误高亮未清除'


def test_main_window_auto_complete_setting_propagates(main_window):
    win = main_window
    editor, _ = win._current_widgets()
    win.settings.editor_auto_complete = False
    win._apply_settings()
    assert editor.auto_complete_enabled() is False
    win.settings.editor_auto_complete = True
    win._apply_settings()
    assert editor.auto_complete_enabled() is True


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
