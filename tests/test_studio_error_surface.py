"""任务 B：GUI 必须显示完整错误（错误码 + SQLSTATE + 完整报文，且不截断）。

背景（docs/review + 任务书）：
* `lightdb_studio/ui/result_table.py:102-110` 的 `show_error` **完全不使用入参**，
  界面只剩「错误」二字；
* `lightdb_studio/ui/main_window.py:726-728` 状态栏把错误截断到 80 字符。

本文件在 `QT_QPA_PLATFORM=offscreen` 下对着**真实引擎**跑三类错误
（1146 / 1062 / 1064）+ 一条 >80 字符超长错误，断言界面上**实际可见**的
文本包含完整诊断信息。

`~/.lightdb_studio` 在受限沙箱里可能不可写：本文件把
`lightdb_studio.core.snapshot.SNAPSHOT_DIR` 指到 pytest 的临时目录，
并且不依赖任何需要写 `~/.lightdb_studio` 的功能。
"""
import os
import sys
import threading
import time

# 必须在导入 PySide6 之前设置，否则拿不到 offscreen 平台
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_studio_error_surface.py
    from conftest import safe_rmtree, track

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_err_surface')
PORT = 8932
LONG_MARKER = 'TAIL_MARKER_绝不截断_9f8e7d6c5b4a'


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = track(Database(DATA_DIR))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    return srv, db, ex


def connect(port, name='err_surface'):
    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.config import ConnectionInfo

    info = ConnectionInfo(name=name, host='127.0.0.1', port=port, user='root',
                          password='')
    conn = DBConnection(info)
    conn.connect()
    return track(conn)


def seed_users(conn):
    err = conn.execute("CREATE TABLE users (id INT PRIMARY KEY, "
                       "name VARCHAR(50))")[1]
    assert err is None, err
    conn.execute("INSERT INTO users VALUES (1, 'alice')")


# ==================== 事件循环 / 主窗口脚手架 ====================

# 必须保持强引用：QApplication 被 GC 掉之后再建 QWidget 会直接崩溃。
# （pytest 没有 Qt 插件，这里自己保证整个会话只有一份 QApplication。）
_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


def make_main_window(tmp_path):
    """构造真实 MainWindow（不 stub 产品逻辑）。

    只做两处环境适配（因为沙箱里 `~/.lightdb_studio` 可能不可写）：
      * 快照目录 → tmp_path；
      * 查询历史文件 → tmp_path（`_execute_sql` 每次都会写历史）。
    并把默认连接的自动登录关掉（免得多开一条连接影响断言）。
    """
    import lightdb_studio.core.snapshot as snap_mod
    from lightdb_studio.utils import history as history_mod

    snap_mod.SNAPSHOT_DIR = tmp_path / 'snapshots'
    history_mod.HISTORY_FILE = tmp_path / 'history.json'
    qapp()                     # 建任何 QWidget 之前先有 QApplication
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    # 实例上已有 path，直接改到 tmp（避免写 ~/.lightdb_studio）
    win.history.path = tmp_path / 'history.json'
    track(win)
    return win


def run_query(win, conn, sql):
    """直接走真实的 _execute_sql 路径（编辑器/标签页无关）。"""
    from lightdb_studio.ui.result_table import ResultTable

    win._get_current_conn = lambda: conn
    result = ResultTable()
    win.result_tables = [result]
    win._execute_sql(sql, None, result)
    qapp().processEvents()
    return result


def visible(result):
    """结果区**实际可见**的文本（QPlainTextEdit 内容）。"""
    return result.error_display_text()


# ==================== 三类真实错误 + 超长错误（走完整 _execute_sql） ====================

def test_missing_table_error_shows_1146(tmp_path):
    print("\n【B-1】不存在的表 → 界面含 1146")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    seed_users(conn)
    win = make_main_window(tmp_path)

    result = run_query(win, conn, "SELECT * FROM no_such_table")
    text = visible(result)
    status = win.status_label.text()
    print("  → 结果区可见文本:")
    for line in text.splitlines():
        print(f"      {line}")
    print(f"  → 状态栏: {status}")

    assert '1146' in text
    assert 'no_such_table' in text
    assert 'SQLSTATE' in text
    assert text != '错误', "结果区仍然只显示「错误」两个字"
    assert '1146' in status
    assert result.error_display_is_selectable(), "错误文本必须可选中复制"
    print("  ✅ 结果区/状态栏均含 1146 + 完整报文")


def test_duplicate_key_error_shows_1062(tmp_path):
    print("\n【B-2】主键重复 → 界面含 1062")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    seed_users(conn)
    win = make_main_window(tmp_path)

    result = run_query(win, conn, "INSERT INTO users VALUES (1, 'alice')")
    text = visible(result)
    status = win.status_label.text()
    print("  → 结果区可见文本:")
    for line in text.splitlines():
        print(f"      {line}")
    print(f"  → 状态栏: {status}")

    assert '1062' in text
    assert result._raw_error_text.strip() != ''
    assert '1062' in status or '1062' in result.info_label.text()
    assert result.error_display_is_selectable()
    print("  ✅ 结果区含 1062")


def test_syntax_error_shows_1064(tmp_path):
    print("\n【B-3】语法错误 → 界面含 1064")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    seed_users(conn)
    win = make_main_window(tmp_path)

    result = run_query(win, conn, "SELCT bad syntax here")
    text = visible(result)
    status = win.status_label.text()
    print("  → 结果区可见文本:")
    for line in text.splitlines():
        print(f"      {line}")
    print(f"  → 状态栏: {status}")

    assert '1064' in text
    assert '42000' in text
    assert 'SQL syntax' in text
    assert 'SELCT bad syntax here' in text, "SQL 原文也应可见"
    assert '1064' in status
    print("  ✅ 结果区含 1064 + SQLSTATE 42000 + 报文 + SQL 原文")


def test_long_error_is_not_truncated(tmp_path):
    """超长错误（>80 字符）：结果区与状态栏都必须是**全文**。"""
    print("\n【B-4】超长错误不截断")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    seed_users(conn)
    win = make_main_window(tmp_path)

    cols = ', '.join(f'col{i}' for i in range(40))
    sql = f"SELECT {cols} FROM no_such_table_at_all"
    result = run_query(win, conn, sql)
    text = visible(result)
    status = win.status_label.text()
    print(f"  → 结果区可见长度={len(text)}  状态栏长度={len(status)}")
    print(f"  → 结果区末尾 120 字符: ...{text[-120:]}")
    print(f"  → 状态栏末尾 120 字符: ...{status[-120:]}")

    assert len(text) > 80
    # 报文尾部与 SQL 尾部都在（截断会丢掉它们）
    assert text.rstrip().endswith(sql), "结果区少了 SQL 尾部（被截断）"
    assert status.rstrip().endswith(sql), "状态栏少了 SQL 尾部（被截断）"
    assert len(status) > 80
    # 80 字符截断的典型症状：结尾是「...」或长度恰为前缀
    assert not status.endswith('...')
    print("  ✅ 结果区与状态栏均为全文，未截断")


def test_copy_button_gives_full_error_text(tmp_path):
    """可复制：复制出来的就是完整错误文本（结果区 → 剪贴板）。"""
    print("\n【B-5】复制错误")
    reset()
    start_server(PORT)
    conn = connect(PORT)
    seed_users(conn)
    win = make_main_window(tmp_path)

    result = run_query(win, conn, "SELECT * FROM no_such_table")
    copied = result.copy_error()
    clip = qapp().clipboard().text()
    print(f"  → copy_error() 长度={len(copied)} 剪贴板长度={len(clip)}")
    assert '1146' in copied
    assert copied == result.error_display_text()
    assert clip == copied
    print("  ✅ 复制到剪贴板的是完整错误文本")


# ==================== 纯控件层：无数据库也能验证「不截断 / 可选中」 ====================

def test_show_error_uses_argument_not_placeholder():
    """回归：show_error 必须真正使用入参（以前只显示「错误」）。"""
    from lightdb_studio.ui.result_table import ResultTable

    qapp()
    r = ResultTable()
    r.show_error('错误码 1146 ... 这是完整报文')
    assert '这是完整报文' in r.error_display_text()
    print(f"  → 可见: {r.error_display_text()!r}")
    assert r.error_display_text() != '错误'


def test_show_error_never_truncates_long_text():
    """纯控件层：>80 字符的错误文本一字不少地显示出来。"""
    from lightdb_studio.ui.result_table import ResultTable

    qapp()
    r = ResultTable()
    msg = 'A' * 120 + ' ' + LONG_MARKER + ' ' + 'B' * 60
    assert len(msg) > 80
    r.show_error(msg, errno=1064, sqlstate='42000')
    shown = r.error_display_text()
    print(f"  → 输入长度={len(msg)} 可见长度={len(shown)}")
    assert LONG_MARKER in shown, "超长文本被截断（尾部标记丢失）"
    assert shown.endswith(msg), f"可见文本没有完整包含入参: ...{shown[-80:]!r}"
    assert len(shown) > len(msg)
    print("  ✅ 超长错误完整可见")


def test_show_error_does_not_swallow_missing_fields():
    """缺少 errno/sqlstate 时显示「(未提供)」，而不是静默丢掉整行。"""
    from lightdb_studio.ui.result_table import ResultTable

    qapp()
    r = ResultTable()
    r.show_error('没有诊断字段的错误')
    shown = r.error_display_text()
    assert '(未提供)' in shown
    assert '没有诊断字段的错误' in shown
    print(f"  → 可见: {shown.splitlines()[0]}")


def test_main_window_source_has_no_80_char_truncation():
    """回归：源码里不允许再出现把错误截到 80 字符的写法。"""
    import inspect
    from lightdb_studio.ui.main_window import MainWindow

    src = inspect.getsource(MainWindow._execute_sql)
    print(f"  → _execute_sql 里 [:80] 出现次数: {src.count('[:80]')}")
    assert '[:80]' not in src, "状态栏仍在静默截断错误"
    assert '_set_status' in src


if __name__ == '__main__':
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        test_missing_table_error_shows_1146(base / 'a')
        test_duplicate_key_error_shows_1062(base / 'b')
        test_syntax_error_shows_1064(base / 'c')
        test_long_error_is_not_truncated(base / 'd')
        test_copy_button_gives_full_error_text(base / 'e')
        test_show_error_uses_argument_not_placeholder()
        test_show_error_never_truncates_long_text()
        test_show_error_does_not_swallow_missing_fields()
        test_main_window_source_has_no_80_char_truncation()
    print('\nALL OK')
