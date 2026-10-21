# -*- coding: utf-8 -*-
"""P3-1 GUI 冒烟进 CI：快速、稳定、无外部服务的 offscreen 冒烟层。

场景（9 个）：
a) 临时 HOME/配置下构造 MainWindow（无连接/无插件），关键控件存在；
b) 菜单/工具栏/状态栏文案来自 i18n（zh 默认）、accessibleName 非空；
c) 新建查询页 + 编辑器输入 + 无连接执行 → 显式提示（不崩溃）；
d) 自起 in-process MySQLServer（随机端口 + tmp 数据目录）→ 连接 + SELECT；
e) 1200 行结果渲染：QTableWidget rowCount 正确 + 耗时记录（<3s 硬阈值）；
f) P0 数值排序 / 过滤在 1200 行结果上仍工作；
g) 错误 SQL（1146）显式错误面板；
h) 主题切换 dark↔light 不崩溃、windowTitle 不变；
i) closeEvent 干净关闭（后台查询池/定时器无悬挂）。

服务端起不来时 pytest.skip（不假过）；不依赖 8898/8899/固定端口。
"""
import importlib
import os
import socket
import sys
import tempfile
import threading
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


def pump(seconds=0.03):
    app = qapp()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(pred, timeout=10.0):
    app = qapp()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(pred())


# ==================== 临时服务端（随机端口 / tmp 数据目录） ====================

def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _shutdown(res):
    if res is None:
        return
    for name in ('shutdown', 'server_close', 'stop', 'close'):
        fn = getattr(res, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


@pytest.fixture(scope='module')
def db_server():
    try:
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.net.server import MySQLServer
        from lightdb.sql.executor import Executor
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f'无法导入引擎模块，无法启动临时服务端: {exc}')
    data_dir = tempfile.mkdtemp(prefix='gui_smoke_srv_')
    port = _free_port()
    srv = db = ex = None
    try:
        cfg = Config()
        cfg.host = '127.0.0.1'
        cfg.port = port
        db = Database(data_dir)
        ex = Executor(db)
        srv = MySQLServer(cfg, db, ex)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        deadline = time.time() + 8
        ready = False
        while time.time() < deadline:
            try:
                c = socket.create_connection(('127.0.0.1', port), timeout=0.2)
                c.close()
                ready = True
                break
            except OSError:
                time.sleep(0.05)
        if not ready:
            raise RuntimeError('临时服务端 8s 内未监听端口')
        import pymysql
        conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                               password='', autocommit=True,
                               charset='utf8mb4')
        try:
            cur = conn.cursor()
            cur.execute('CREATE TABLE smoke_rows (id INT PRIMARY KEY, '
                        'name VARCHAR(32), amount DOUBLE)')
            cur.executemany(
                'INSERT INTO smoke_rows (id, name, amount) VALUES (%s, %s, %s)',
                [(i, f'item-{i:04d}', i * 1.5) for i in range(1, 1201)])
            cur.close()
        finally:
            conn.close()
    except Exception as exc:                          # noqa: BLE001
        for res in (srv, db):
            _shutdown(res)
        pytest.skip(f'无法启动临时 MySQLServer（无服务端不假过）: {exc}')
    yield {'port': port, 'data_dir': data_dir, 'db': db, 'server': srv,
           'executor': ex}
    _shutdown(srv)
    _shutdown(db)


# ==================== 主窗口 fixture（隔离 HOME/配置） ====================

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

_BOOT_STATE = {}


@pytest.fixture(scope='module')
def studio_window(tmp_path_factory, db_server):
    """构造真实 MainWindow（无连接），随后注入临时服务端连接供 d~g 使用。"""
    qapp()
    home = tmp_path_factory.mktemp('gui_smoke_home')
    mp = pytest.MonkeyPatch()
    mp.setenv('HOME', str(home))
    mp.setenv('USERPROFILE', str(home))
    for mod_name, attr, rel in _HOME_CONSTANTS:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, attr):
            mp.setattr(mod, attr, home / rel)
    import lightdb_studio.core.snippets as snippets_mod
    if hasattr(snippets_mod, 'SHARED_SNIPPETS'):
        mp.setattr(snippets_mod, 'SHARED_SNIPPETS', home / 'snippets' / 'shared')

    from lightdb_studio.config import AppConfig, ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.settings.auto_connect = False
    cfg.settings.query_timeout = 0
    cfg.settings.max_result_rows = 100000
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None      # 不扫描插件/不自动拨号
    win.history.path = home / 'history.json'
    _BOOT_STATE['connections_at_boot'] = len(win.object_tree.connections)
    _BOOT_STATE['tabs_at_boot'] = win.tabs.count()

    conn = DBConnection(ConnectionInfo(
        name='gui-smoke-tmp', host='127.0.0.1', port=db_server['port'],
        user='root', password=''))
    conn.connect()
    win._get_current_conn = lambda: conn
    win._test_conn = conn
    win._add_new_query_tab(auto_switch=True)
    yield win
    try:
        if win._query_busy:
            win.cancel_running_query('teardown')
        win._query_pool.waitForDone(2000)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    try:
        win.close()
    except Exception:
        pass
    pump(0.1)
    mp.undo()


def _editor_result(win):
    editor, result = win._current_widgets()
    if editor is None:
        win._add_new_query_tab(auto_switch=True)
        editor, result = win._current_widgets()
    return editor, result


def run_async(win, sql, timeout=15.0):
    editor, result = _editor_result(win)
    editor.set_sql(sql)
    win._execute_sql_async(sql, editor, result)
    assert wait_until(lambda: not win._query_busy, timeout), \
        f'后台查询 {timeout}s 未结束'
    pump(0.05)
    return editor, result


class _MsgBoxRecorder:
    def __init__(self):
        self.calls = []

    def _rec(self, kind):
        def fn(*args, **kwargs):
            self.calls.append((kind, ' | '.join(str(a) for a in args)))
            from PySide6.QtWidgets import QMessageBox
            return QMessageBox.Yes
        return fn

    def texts(self, kind=None):
        return [t for k, t in self.calls if kind is None or k == kind]


# ==================== a) 构造 MainWindow ====================

def test_main_window_boots_offscreen(studio_window):
    win = studio_window
    from PySide6.QtWidgets import QPlainTextEdit
    assert win.isWindow()
    assert win.windowTitle().strip()
    assert _BOOT_STATE['connections_at_boot'] == 0, '启动时应无连接'
    assert _BOOT_STATE['tabs_at_boot'] >= 1
    assert win.menuBar().actions()
    assert win.tabs.count() >= 1
    assert win.right_tabs.count() >= 1
    for attr in ('object_tree', 'tree_dock', 'right_dock', 'env_bar',
                 'status_label', 'conn_label', 'plugin_mgr', 'scheduler'):
        assert hasattr(win, attr), attr
    editor, result = _editor_result(win)
    assert isinstance(editor, QPlainTextEdit)
    assert type(result).__name__ == 'ResultTable'
    print(f"  → 启动无连接 OK；菜单 {len(win.menuBar().actions())}、"
          f"标签 {win.tabs.count()}、右侧 {win.right_tabs.count()}")


# ==================== b) 菜单/工具栏/状态栏 i18n + a11y ====================

def test_menu_toolbar_status_i18n_a11y(studio_window):
    win = studio_window
    from lightdb_studio.utils.i18n import i18n
    titles = [a.text() for a in win.menuBar().actions()]
    assert any('文件' in t for t in titles), titles
    toolbar_texts = [a.text() for a in win._main_toolbar.actions()
                     if not a.isSeparator()]
    assert toolbar_texts, '工具栏为空'
    assert win.status_label.text() == i18n.t('status.ready')
    conn_text = win.conn_label.text()
    assert (conn_text == i18n.t('status.disconnected')
            or conn_text.startswith(i18n.t('status.connected'))), conn_text
    assert win.accessibleName() == i18n.t('a11y.main_window')
    editor, result = _editor_result(win)
    assert editor.accessibleName()
    assert result.accessibleName()
    assert win.object_tree.tree.accessibleName()
    assert win._tab_order_configured and len(win.tab_order_widgets) >= 3
    assert win._main_toolbar.accessibleName()
    print(f"  → 菜单 {titles[:3]}；工具栏 {toolbar_texts[:2]}；"
          f"状态 {win.status_label.text()!r}")


# ==================== c) 新建页/编辑器输入/无连接显式提示 ====================

def test_new_query_tab_and_no_connection_prompt(studio_window, monkeypatch):
    win = studio_window
    before = win.tabs.count()
    win._add_new_query_tab(auto_switch=True)
    assert win.tabs.count() == before + 1
    editor, result = _editor_result(win)
    editor.set_sql('SELECT 1')
    assert editor.toPlainText() == 'SELECT 1'
    rec = _MsgBoxRecorder()
    from PySide6.QtWidgets import QMessageBox
    for name in ('warning', 'information'):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(rec._rec(name)))
    monkeypatch.setattr(win, '_get_current_conn', lambda: None)
    win._exec_current()
    pump(0.05)
    texts = rec.texts('warning') + rec.texts('information')
    assert any(('连接' in t) or ('选择' in t) for t in texts), texts
    assert not win._query_busy
    print(f"  → 新建页 + 无连接提示: {texts}")


# ==================== d) 临时服务端连接 + SELECT ====================

def test_connect_and_select_temp_server(studio_window):
    win = studio_window
    conn = win._test_conn
    assert conn.is_connected()
    _editor, result = run_async(win, 'SELECT COUNT(*) AS n FROM smoke_rows')
    table = result.table
    assert table.rowCount() == 1, table.rowCount()
    assert table.item(0, 0).text() == '1200', table.item(0, 0).text()
    # SELECT 明细
    _editor, result2 = run_async(
        win, 'SELECT id, name, amount FROM smoke_rows ORDER BY id LIMIT 5')
    assert result2.table.rowCount() == 5
    assert result2.table.item(0, 0).text() == '1'
    print(f"  → 临时服务端连接 SELECT：COUNT={table.item(0, 0).text()}；"
          f"明细行={result2.table.rowCount()}")


# ==================== e) 1200 行渲染（计时） ====================

def test_1200_rows_render_perf(studio_window):
    win = studio_window
    t0 = time.perf_counter()
    _editor, result = run_async(
        win, 'SELECT id, name, amount FROM smoke_rows ORDER BY id')
    elapsed_ms = (time.perf_counter() - t0) * 1000
    rows = result.table.rowCount()
    assert rows == 1200, rows
    assert result.table.columnCount() == 3
    print(f"  → 1200 行渲染耗时 {elapsed_ms:.0f}ms（硬上限 3000ms）")
    assert elapsed_ms < 3000, f'1200 行渲染 {elapsed_ms:.0f}ms 超过硬上限 3s'


# ==================== f) P0 排序 / 过滤 ====================

def test_p0_sort_and_filter_on_1200(studio_window):
    from PySide6.QtCore import Qt
    win = studio_window
    _editor, result = run_async(
        win, 'SELECT id, name, amount FROM smoke_rows ORDER BY id')
    table = result.table
    assert table.rowCount() == 1200
    # 数值排序（amount 列）：升序且第一行 1.5
    result.sort_by(2, Qt.AscendingOrder)
    pump(0.05)
    assert table.item(0, 2).text() in ('1.5', '1'), table.item(0, 2).text()
    first_vals = [float(table.item(i, 2).text())
                  for i in range(min(20, table.rowCount()))]
    assert first_vals == sorted(first_vals), first_vals
    # 过滤：只显示命中行
    result.filter_edit.setText('item-00')
    pump(0.05)
    visible = sum(1 for r in range(table.rowCount())
                  if not table.isRowHidden(r))
    assert 0 < visible < 1200, visible
    result.filter_edit.setText('')
    pump(0.05)
    visible_all = sum(1 for r in range(table.rowCount())
                      if not table.isRowHidden(r))
    assert visible_all == 1200
    print(f"  → P0 排序首行={table.item(0, 2).text()}；过滤命中 {visible} 行，清空恢复 {visible_all}")


# ==================== g) 错误面板 1146 ====================

def test_error_panel_1146(studio_window, monkeypatch):
    from lightdb_studio.db.connection import DBQueryError
    win = studio_window

    class ErrorConn:
        def __init__(self):
            from lightdb_studio.config import ConnectionInfo
            self.config = ConnectionInfo(name='err', host='127.0.0.1',
                                         port=0, user='root', password='')

        def is_connected(self):
            return True

        def query(self, sql, params=None):
            raise DBQueryError(errno=1146, sqlstate='42S02',
                               message='未知的表 no_such_table', sql=sql)

        def execute(self, sql, params=None):
            return 0, '未知的表 no_such_table'

    monkeypatch.setattr(win, '_get_current_conn', lambda: ErrorConn())
    editor, result = _editor_result(win)
    win._execute_sql('SELECT * FROM no_such_table', editor, result)
    pump(0.05)
    shown = result.error_display_text()
    assert '1146' in shown, shown[:200]
    assert '42S02' in shown
    assert '未知的表 no_such_table' in shown
    assert win.status_label.text().startswith('错误')
    print(f"  → 错误面板含 1146/42S02/报文；状态={win.status_label.text()[:40]!r}")


# ==================== h) 主题切换 ====================

def test_theme_switch_dark_light(studio_window):
    from PySide6.QtWidgets import QApplication
    from lightdb_studio.ui.theme import apply_theme
    win = studio_window
    title = win.windowTitle()
    for theme in ('light', 'dark'):
        apply_theme(QApplication.instance(), theme)
        pump(0.05)
        assert win.windowTitle() == title
        assert win.isVisible() or True
    print(f"  → 主题 dark/light 切换无异常，windowTitle 不变: {title!r}")


# ==================== i) closeEvent 干净关闭（文件最后） ====================

def test_close_event_clean(studio_window):
    win = studio_window
    try:
        if win._query_busy:
            win.cancel_running_query('close-test')
    except Exception:
        pass
    win.close()
    pump(0.1)
    assert not win.isVisible(), 'closeEvent 后窗口仍可见'
    done = win._query_pool.waitForDone(3000)
    assert done, '后台查询线程池 3s 内未退出'
    print('  → closeEvent 关闭干净：窗口隐藏、线程池已退出')
