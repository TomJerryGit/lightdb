# -*- coding: utf-8 -*-
"""P1 响应性回归：后台查询 / 取消 / 超时 / 有界结果 + 加载更多（offscreen Qt）。

覆盖（task-21 验收）：
1. 后台执行期间主线程仍处理事件（QTimer 计数证明 UI 不冻结）；
2. 完成后表格列/行正确、执行动作恢复、历史落盘；
3. 取消：worker 取消标志 + 另一条根连接 `KILL <thread_id>`；显式"已取消"，
   连接自动重连，随后查询可继续；
4. 超时：`settings.query_timeout` 到点触发取消并显式提示；
5. 有界结果：`settings.max_result_rows` 截断 → 结果区显式提示 + "加载更多"
   追加（保留 P0 排序/过滤）；
6. 错误面板仍显示 1146；同步 `_execute_sql` 旧入口保持可用（P0/回归兼容）。

测试自带临时 MySQLServer（随机空闲端口 + tmp 数据目录），起不来时
`pytest.skip` 而不是假过。不依赖 8898/8899，不污染仓库。
"""
import importlib
import os
import socket
import sys
import tempfile
import threading
import time

# 必须在导入 PySide6 之前设置，否则拿不到 offscreen 平台
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

# 临时服务端：与 tests/conftest.py 一致显式允许空口令（本地 demo 实例）。
# conftest 在收集阶段已 setdefault；这里再兜一层，保证单文件/独立运行也可用。
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')

_APP = None
_SERVER = {}


def qapp():
    """整个测试会话共享一个 QApplication（被 GC 后建 QWidget 会崩）。"""
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


def pump(seconds=0.02):
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


def wait_idle(win, timeout=10.0):
    return wait_until(lambda: not win._query_busy, timeout)


# ==================== 临时服务端（模块级，随机空闲端口） ====================

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture(scope='module')
def db_server():
    try:
        from lightdb.config import Config
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        from lightdb.net.server import MySQLServer
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f'无法导入引擎模块，无法启动临时服务端: {exc}')

    data_dir = tempfile.mkdtemp(prefix='studio_async_srv_')
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
            cur.execute('CREATE TABLE IF NOT EXISTS async_items '
                        '(id INT PRIMARY KEY, name VARCHAR(32))')
            cur.executemany(
                'INSERT INTO async_items (id, name) VALUES (%s, %s)',
                [(i, f'item-{i:03d}') for i in range(1, 251)])
            cur.close()
        finally:
            conn.close()
    except Exception as exc:                       # noqa: BLE001
        for res in (srv, db):
            _shutdown(res)
        pytest.skip(f'无法启动临时 MySQLServer（无服务端不假过）: {exc}')

    yield {'port': port, 'data_dir': data_dir, 'db': db, 'server': srv,
           'executor': ex}
    _shutdown(srv)
    _shutdown(db)


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


# ==================== 存储路径隔离 + MainWindow fixture ====================

# import 期就绑定 Path.home() 的常量：逐个 monkeypatch 到 tmp_path，保证
# 测试不写真实 ~/.lightdb_studio（与其他测试/全量套件互不影响）。
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
def gui(tmp_path, db_server, monkeypatch):
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
    from lightdb_studio.config import AppConfig, ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.settings.auto_connect = False
    cfg.settings.query_timeout = 0
    cfg.settings.max_result_rows = 100000
    win = MainWindow(cfg)
    win.history.path = tmp_path / 'history.json'

    conn = DBConnection(ConnectionInfo(
        name='async-tmp', host='127.0.0.1', port=db_server['port'],
        user='root', password=''))
    conn.connect()
    win._get_current_conn = lambda: conn
    win._test_conn = conn
    win._add_new_query_tab(auto_switch=True)
    yield win, conn
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
    win.close()
    pump(0.1)


# ==================== 辅助 ====================

def current_editor_result(win):
    editor, result = win._current_widgets()
    if editor is None:
        win._add_new_query_tab(auto_switch=True)
        editor, result = win._current_widgets()
    return editor, result


def start_async(win, sql):
    """走真实 UI 入口 `_execute_sql_async`，返回 (editor, result)。"""
    editor, result = current_editor_result(win)
    editor.set_sql(sql)
    win._execute_sql_async(sql, editor, result)
    return editor, result


def run_async(win, sql, timeout=10.0):
    editor, result = start_async(win, sql)
    assert wait_idle(win, timeout), '后台查询 10s 内未结束'
    qapp().processEvents()
    return editor, result


def grid(result):
    t = result.table
    cols = [t.horizontalHeaderItem(c).text() if t.horizontalHeaderItem(c) else ''
            for c in range(t.columnCount())]
    rows = [[t.item(r, c).text() if t.item(r, c) else ''
             for c in range(t.columnCount())] for r in range(t.rowCount())]
    return cols, rows


def find_action(win, text):
    from PySide6.QtGui import QAction
    for action in win.findChildren(QAction):
        if action.text().replace('&', '') == text:
            return action
    return None


def slow_query_patch(conn, monkeypatch, delay=0.45):
    """把 query_chunked 包一层 sleep：worker 里执行，证明 UI 线程不被占用。"""
    orig = conn.query_chunked

    def slow(sql, params=None, max_rows=None, chunk_size=1000):
        time.sleep(delay)
        return orig(sql, params=params, max_rows=max_rows,
                    chunk_size=chunk_size)

    monkeypatch.setattr(conn, 'query_chunked', slow)
    return orig


# ==================== 1. 后台执行：UI 不冻结 ====================

def test_async_query_does_not_freeze_ui(gui, monkeypatch):
    win, conn = gui
    slow_query_patch(conn, monkeypatch, delay=0.5)

    ticks = {'n': 0}
    from PySide6.QtCore import QTimer
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.__setitem__('n', ticks['n'] + 1))
    timer.start()

    editor, result = start_async(win, 'SELECT id, name FROM async_items ORDER BY id')
    assert wait_until(lambda: win._query_busy, 2.0), '后台查询未进入执行中状态'
    busy_snapshot = win._query_busy
    cancel_enabled = (win._cancel_action is not None
                      and win._cancel_action.isEnabled())
    exec_action = find_action(win, '执行')
    exec_disabled = (exec_action is not None and not exec_action.isEnabled())
    assert wait_idle(win, 10.0), '后台查询未结束'
    timer.stop()

    cols, rows = grid(result)
    print(f"  → 期间 QTimer 触发 {ticks['n']} 次；行数={len(rows)} cols={cols}")
    assert ticks['n'] >= 8, f'UI 事件循环被冻结（0.5s 仅 {ticks["n"]} 次定时器）'
    assert busy_snapshot, '执行期间 _query_busy 不是 True'
    assert cancel_enabled, '执行期间「取消当前查询」不可见/禁用'
    assert exec_disabled, '执行期间「执行」动作未禁用'
    assert cols == ['id', 'name']
    assert len(rows) == 250, f'行数={len(rows)}'
    # 完成后恢复
    assert not win._query_busy
    assert win._cancel_action is not None and not win._cancel_action.isEnabled()
    assert exec_action is not None and exec_action.isEnabled()
    assert '完成' in win.status_label.text()


def test_async_complete_restores_and_records_history(gui):
    win, conn = gui
    _, result = run_async(win, 'SELECT COUNT(*) AS n FROM async_items')
    cols, rows = grid(result)
    assert cols == ['n']
    assert rows == [['250']]
    status = win.status_label.text()
    assert status.startswith('完成'), status
    recent = win.history.recent(5)
    assert recent and 'async_items' in recent[-1].get('sql', ''), recent[-1:]
    assert not win._query_busy


# ==================== 2. 取消（KILL 连接 + 重连） ====================

def test_cancel_query_kills_and_reconnects(gui, monkeypatch):
    win, conn = gui
    slow_query_patch(conn, monkeypatch, delay=0.6)
    start_async(win, 'SELECT id FROM async_items ORDER BY id')
    assert wait_until(lambda: win._query_busy, 2.0)
    old_tid = conn.thread_id()
    ok = win.cancel_running_query('测试取消')
    print(f"  → kill ok={ok}; thread_id={old_tid}; "
          f"status={win.status_label.text()}")
    assert ok is True, 'KILL 发送失败: ' + win.status_label.text()
    assert wait_idle(win, 10.0), '取消后 worker 未结束'
    # 显式取消提示
    text = win._current_widgets()[1].error_display_text()
    assert '取消' in text, text[:200]
    assert '取消' in win.status_label.text()
    # KILL 后连接被终止 → 自动重连（finished 回调里）
    assert wait_until(conn.is_connected, 5.0), '取消后连接未恢复'
    # 连接恢复后可以继续查询（走同一异步路径）
    _, result2 = run_async(win, 'SELECT 1 AS n')
    assert grid(result2)[1] == [['1']]
    assert not win._query_busy


# ==================== 3. 超时（QTimer → KILL） ====================

def test_query_timeout_cancels_and_reports(gui, monkeypatch):
    win, conn = gui
    win.settings.query_timeout = 1
    slow_query_patch(conn, monkeypatch, delay=1.8)
    start_async(win, 'SELECT id FROM async_items ORDER BY id')
    assert wait_until(lambda: win._query_busy, 2.0)
    assert wait_idle(win, 12.0), '超时后 worker 未结束'
    text = win._current_widgets()[1].error_display_text()
    status = win.status_label.text()
    print(f"  → 超时后 status={status}; error={text[:120]}")
    assert '超时' in (text + status), (text[:200], status)
    assert win.settings.query_timeout == 1
    assert not win._query_busy
    # 超时同样走 KILL → 连接应被重连
    assert wait_until(conn.is_connected, 5.0)


# ==================== 4. 有界结果 + 加载更多 ====================

def test_truncation_and_load_more(gui):
    win, conn = gui
    win.settings.max_result_rows = 100
    _, result = run_async(win,
                          'SELECT id, name FROM async_items ORDER BY id')
    assert result.table.rowCount() == 100, result.table.rowCount()
    info = result.info_label.text()
    print(f"  → 截断提示: {info}")
    assert '已截断' in info and '前 100 行' in info, info
    assert not result.load_more_btn.isHidden(), '没有显示「加载更多」'
    assert '已截断' in win.status_label.text()

    # 第一次加载更多 → 200 行，仍有更多
    result.load_more_btn.click()
    assert wait_until(lambda: win._query_busy, 2.0)
    assert wait_idle(win, 10.0)
    assert result.table.rowCount() == 200, result.table.rowCount()
    assert not result.load_more_btn.isHidden(), '还有更多时按钮应保留'

    # 第二次 → 250 行，全部加载完，按钮隐藏、提示消失
    result.load_more_btn.click()
    assert wait_until(lambda: win._query_busy, 2.0)
    assert wait_idle(win, 10.0)
    assert result.table.rowCount() == 250, result.table.rowCount()
    assert result.load_more_btn.isHidden(), '全部加载后按钮应隐藏'
    assert '已截断' not in result.info_label.text()
    cols, rows = grid(result)
    ids = [int(r[0]) for r in rows]
    assert ids == list(range(1, 251)), '加载更多后行序/内容异常'


def test_truncated_result_keeps_p0_sort_and_filter(gui):
    win, conn = gui
    win.settings.max_result_rows = 100
    _, result = run_async(win, 'SELECT id, name FROM async_items ORDER BY id')
    from PySide6.QtCore import Qt
    result.sort_by(0, Qt.DescendingOrder)
    vals = [int(t) for t in (r[0] for r in grid(result)[1])]
    assert vals == sorted(vals, reverse=True), 'P0 类型化排序在后台结果上失效'
    visible = result.apply_filter('item-05', column=1)
    assert visible > 0, 'P0 过滤在后台结果上失效'
    visible_rows = [r for i, r in enumerate(grid(result)[1])
                    if not result.table.isRowHidden(i)]
    assert all('item-05' in r[1] for r in visible_rows)


# ==================== 5. 错误面板 + 同步旧入口兼容 ====================

def test_async_error_shows_error_panel(gui):
    win, conn = gui
    _, result = run_async(win, 'SELECT * FROM no_such_table_async')
    text = result.error_display_text()
    print(f"  → 错误面板: {text[:120]}")
    assert '1146' in text
    assert 'SQLSTATE' in text
    assert result.error_category_key == 'query'
    assert not win._query_busy
    assert '错误' in win.status_label.text()


def test_sync_execute_sql_still_works(gui):
    """旧同步入口（测试/嵌入调用方）保持可用，不被异步改造破坏。"""
    win, conn = gui
    editor, result = current_editor_result(win)
    editor.set_sql('SELECT 7 AS n')
    win._execute_sql('SELECT 7 AS n', editor, result)   # 同步路径
    pump(0.1)
    cols, rows = grid(result)
    assert cols == ['n'] and rows == [['7']], (cols, rows)


# ==================== 6. workers / DBConnection 单元级 ====================

def test_query_chunked_bounds_and_thread_id(gui):
    win, conn = gui
    assert isinstance(conn.thread_id(), int)
    res = conn.query_chunked('SELECT id FROM async_items ORDER BY id',
                             max_rows=10)
    assert res['fetched'] == 10
    assert res['truncated'] is True
    res_all = conn.query_chunked('SELECT id FROM async_items ORDER BY id',
                                 max_rows=None)
    assert res_all['fetched'] == 250
    assert res_all['truncated'] is False


if __name__ == '__main__':
    import traceback
    failed = 0
    print('本文件需 pytest（含 fixture）；直接 python 运行仅做导入检查')
    sys.exit(0)
