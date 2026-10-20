"""任务 B：LightDB Studio GUI 离屏冒烟测试（无桌面 / 无数据库也能跑）。

背景：`lightdb_studio` 有 130 个文件 / 约 3.7 万行，此前**零测试**；GUI 的
回归只能靠人肉点。本文件只做最小冒烟，不追求覆盖率：

  1. `QT_QPA_PLATFORM=offscreen` 下真的能构造 `MainWindow`，关键控件都在；
  2. 错误呈现：错误码 / SQLSTATE / 完整报文（含 >80 字符超长文本）都看得到，
     且不是只剩「错误」两个字；
  3. 结果表格：列数 / 行数 / 单元格数据与表头正确；
  4. SQL 编辑器：可写入、可读回（含 MainWindow 的插入接口）。

环境适配（受限沙箱里 `~/.lightdb_studio` 可能不可写，**本文件完全不依赖
真实主目录**）：
  * 导入 PySide6 之前强制 `QT_QPA_PLATFORM=offscreen`；
  * 用 `MonkeyPatch` 把 `Path.home()` 以及 `lightdb_studio` 各模块里所有
    「主目录下的 Path 常量」（HISTORY_FILE / SNAPSHOT_DIR / REPORT_DIR …）
    整体改道到 pytest 的临时目录；
  * 数据库一律用假连接（不联网、不起服务、不写真实主目录）。

没装 PySide6 或进不去 offscreen 时**明确 skip 并写明原因**，绝不伪造通过。
"""
import os
import sys
from pathlib import Path

# 必须在导入 PySide6 之前设置：offscreen 平台让 GUI 在没有桌面会话的
# CI / 沙箱里也能真实构造 QWidget（不是 mock，是 Qt 自己的离屏后端）。
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

# 必须保持强引用：QApplication 被 GC 掉之后再建 QWidget 会直接崩溃。
_APP = None

# 需要改道主目录的模块前缀
_HOME_PREFIX = 'lightdb_studio'

LONG_TAIL = 'TAIL_MARKER_绝不截断_9f8e7d6c5b4a'


# ==================== 环境闸门 ====================

def _require_gui():
    """用例入口：PySide6 可用 + 真的进了 offscreen；否则 skip 并写明原因。"""
    global _APP
    pytest.importorskip('PySide6')

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        plugins = (Path(__import__('PySide6').__file__).parent
                   / 'plugins' / 'platforms')
        has_offscreen = (
            plugins.is_dir()
            and any(p.name.startswith('qoffscreen') for p in plugins.iterdir()))
        if not has_offscreen:
            pytest.skip(f'缺少 offscreen 平台插件（{plugins}），无桌面环境跑不了 GUI')
        try:
            app = QApplication([])
        except BaseException as e:                      # noqa: BLE001
            pytest.skip(
                f'无法进入 offscreen 平台（QT_QPA_PLATFORM='
                f'{os.environ.get("QT_QPA_PLATFORM")!r}）：'
                f'{type(e).__name__}: {e}')
    _APP = app

    platform_name = QGuiApplication.platformName()
    if platform_name != 'offscreen':
        pytest.skip(f'当前 Qt 平台是 {platform_name!r} 而不是 offscreen'
                    f'（本进程已有其它模块用该平台建过 QApplication），'
                    f'无法验证离屏路径')
    return _APP


def _is_under(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _redirect_home(tmp_home):
    """把「主目录下的落盘路径」整体改道到 tmp_home，返回 MonkeyPatch。

    顺序很重要，两步都要做：
      1) 先改 `Path.home()` —— 之后才 import 的模块（以及像
         `AIConfigStore.__init__` 那样在运行时才算路径的代码）直接落在 tmp；
      2) 再扫一遍**已经 import 过**的模块 —— 它们的 `HISTORY_FILE` /
         `SNAPSHOT_DIR` 等常量是在 import 那一刻按真实主目录算好的，只改
         `Path.home()` 对它们无效，必须逐个改道。
    """
    mp = pytest.MonkeyPatch()
    real_home = Path.home()
    mp.setattr(Path, 'home', classmethod(lambda cls: tmp_home))

    # 触发 lightdb_studio 全部依赖的导入（MainWindow 的依赖面）
    import lightdb_studio.ui.main_window          # noqa: F401

    moved = 0
    for module in list(sys.modules.values()):
        name = getattr(module, '__name__', '') or ''
        if not name.startswith(_HOME_PREFIX):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith('_') or not isinstance(value, Path):
                continue
            if not _is_under(value, real_home):
                continue
            mp.setattr(module, attr, tmp_home / value.relative_to(real_home))
            moved += 1
    print(f'  → 主目录落盘路径改道 {moved} 处 → {tmp_home}')
    return mp


# ==================== 主窗口脚手架 ====================

@pytest.fixture(scope='module')
def studio_window(tmp_path_factory):
    """构造**真实** MainWindow（不 stub 产品逻辑），用完关闭。

    module 作用域：MainWindow 构造要 5~8s（30+ 个管理器 + 插件扫描），
    每个用例各建一个只是浪费时间；各用例自己把状态设成自己要的样子即可。
    """
    _require_gui()
    tmp_home = tmp_path_factory.mktemp('studio_home')
    mp = _redirect_home(tmp_home)
    win = None
    try:
        from lightdb_studio.config import AppConfig
        from lightdb_studio.ui.main_window import MainWindow

        cfg = AppConfig()                    # CONFIG_DIR 已改道到 tmp
        cfg.settings.auto_connect = False    # 不要在冒烟里自动拨号
        win = MainWindow(cfg)
        win.history.path = tmp_home / '.lightdb_studio' / 'history.json'
        yield win
    finally:
        if win is not None:
            try:
                win.close()                  # closeEvent: 关连接/停调度/卸插件
            except Exception:                # noqa: BLE001
                pass
        mp.undo()


def _editor(win):
    """当前查询标签页里的 SQL 编辑器。"""
    return win.tabs.widget(0).widget(0)


def _result(win):
    """当前查询标签页里的结果区。"""
    return win.tabs.widget(0).widget(1)


class FakeConn:
    """假连接：断言的是 GUI 的错误呈现通路，不能依赖真实数据库。

    `query()` 抛出与 `DBConnection.query` 同形的 `DBQueryError`
    （带 errno / sqlstate / message / sql），因此 `_execute_sql` 走的是
    真实代码路径，只是数据源是假的。
    """

    def __init__(self, errno=1146, sqlstate='42S02',
                 message='未知的表 no_such_table'):
        from lightdb_studio.config import ConnectionInfo

        self.config = ConnectionInfo(name='fake_conn', host='127.0.0.1',
                                     port=0, user='root', password='')
        self.errno = errno
        self.sqlstate = sqlstate
        self.message = message

    def _make_error(self, sql):
        """与 `DBConnection.query` 一样带上 SQL 原文（_raise_db_error 的行为）。"""
        from lightdb_studio.db.connection import DBQueryError

        return DBQueryError(errno=self.errno, sqlstate=self.sqlstate,
                            message=self.message, sql=sql)

    def is_connected(self):
        return True

    def connect(self):
        return None

    def close(self):
        return None

    def query(self, sql, params=None):
        raise self._make_error(sql)

    def execute(self, sql, params=None):
        return 0, str(self._make_error(sql))

    def get_databases(self):
        return ['fakedb']

    def get_tables(self, db=None):
        return ['users']

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT'}]


# ==================== 1. 能起来、关键控件在 ====================

def test_main_window_boots_offscreen(studio_window):
    """离屏下 MainWindow 真的构造出来了，且关键控件都在。"""
    _require_gui()
    win = studio_window
    from PySide6.QtWidgets import QPlainTextEdit

    menus = win.menuBar().actions()
    print(f"\n【B-1】主窗口：{win.windowTitle()!r}  菜单 {len(menus)} 个  "
          f"中央标签 {win.tabs.count()} 个  右侧工具标签 "
          f"{win.right_tabs.count()} 个")

    assert win.isWindow(), 'MainWindow 不是顶层窗口'
    assert win.windowTitle().strip(), '窗口标题为空'
    assert len(menus) > 0, '一个菜单都没有'
    assert win.tabs.count() > 0, '中央标签页为 0（连查询标签都没建起来）'
    assert win.right_tabs.count() > 0, '右侧工具面板标签为 0'

    keep = ['object_tree', 'tree_dock', 'right_dock', 'env_bar', 'status_label',
            'conn_label', 'history_panel', 'snippet_panel', 'ai_chat',
            'catalog_panel', 'collab_panel', 'scheduler', 'workflow_engine',
            'catalog', 'alert_engine', 'plugin_mgr', 'history']
    missing = [a for a in keep if not hasattr(win, a)]
    assert not missing, f'MainWindow 缺少关键属性: {missing}'

    editor, result = _editor(win), _result(win)
    assert isinstance(editor, QPlainTextEdit), f'编辑器类型异常: {type(editor)}'
    assert type(result).__name__ == 'ResultTable', f'结果区类型异常: {type(result)}'
    assert hasattr(result, 'table') and hasattr(result, 'error_text')

    menu_titles = [a.text() for a in menus]
    print(f"  → 菜单: {menu_titles}")
    print(f"  → 编辑器={type(editor).__name__}  结果区={type(result).__name__}")


# ==================== 2. 错误呈现 ====================

def test_show_error_keeps_errno_message_and_sql(studio_window):
    """`show_error` 必须用入参：错误码 + 完整报文 + SQL 原文都在界面上。"""
    _require_gui()
    result = _result(studio_window)

    result.show_error('未知的表 no_such_table', errno=1146,
                      sqlstate='42S02',
                      sql='SELECT * FROM no_such_table')
    shown = result.error_display_text()
    print('\n【B-2】错误呈现 可见文本:')
    for line in shown.splitlines():
        print(f'      {line}')

    assert '1146' in shown, '界面上看不到错误码 1146'
    assert '42S02' in shown, '界面上看不到 SQLSTATE 42S02'
    assert '未知的表 no_such_table' in shown, '完整报文丢失'
    assert 'SELECT * FROM no_such_table' in shown, 'SQL 原文丢失'
    assert shown.strip() != '错误', '又退化成只剩「错误」两个字'
    assert result.error_display_is_selectable(), '错误文本必须可选中复制'
    assert result.error_label.text().strip(), '错误标题栏为空'


def test_error_over_80_chars_is_not_truncated(studio_window):
    """>80 字符的错误必须**一字不少**地显示（历史缺陷是截到 80 字符）。"""
    _require_gui()
    result = _result(studio_window)

    message = '未知的表 no_such_table：' + 'x' * 120 + ' ' + LONG_TAIL
    assert len(message) > 80
    result.show_error(message, errno=1146, sqlstate='42S02',
                      sql='SELECT * FROM no_such_table')
    shown = result.error_display_text()
    print(f'\n【B-3】超长错误：入参 {len(message)} 字符 → 可见 {len(shown)} 字符')
    print(f'  → 末尾 80 字符: ...{shown[-80:]}')

    assert LONG_TAIL in shown, '超长报文被截断（尾部标记丢失）'
    assert message in shown, '可见文本没有完整包含入参报文'
    assert len(shown) > 80
    assert not shown.rstrip().endswith('...'), '出现截断省略号'


def test_execute_sql_error_path_with_fake_connection(studio_window, monkeypatch):
    """走真实 `_execute_sql`：失败时结果区 + 状态栏都要有完整诊断字段。

    SQL 故意写长（>80 字符：40 个列名）——报文尾部与 SQL 尾部都在，才能
    证明状态栏没有沿历史实现截到 80 字符。
    """
    _require_gui()
    win = studio_window
    fake = FakeConn()
    monkeypatch.setattr(win, '_get_current_conn', lambda: fake)

    sql = 'SELECT ' + ', '.join(f'col{i}' for i in range(40)) \
        + ' FROM no_such_table'
    assert len(sql) > 80
    result = _result(win)
    win._execute_sql(sql, _editor(win), result)

    shown = result.error_display_text()
    status = win.status_label.text()
    print('\n【B-4】_execute_sql 失败路径 结果区可见文本:')
    for line in shown.splitlines():
        print(f'      {line}')
    print(f'  → 状态栏 {len(status)} 字符，尾部: ...{status[-60:]}')

    assert '1146' in shown
    assert '42S02' in shown
    assert '未知的表 no_such_table' in shown
    assert '1146' in status, '状态栏没带错误码'
    assert '未知的表 no_such_table' in status, '状态栏没带完整报文'
    assert len(status) > 80, '状态栏文本疑似被截断'
    assert status.rstrip().endswith(sql), '状态栏丢了 SQL 尾部（被截断）'
    assert shown.rstrip().endswith(sql), '结果区丢了 SQL 尾部（被截断）'
    assert not shown.rstrip().endswith('...')
    assert not result.error_view.isHidden(), '结果区的错误视图没有显示'
    assert shown.strip() != '错误'


def test_show_message_hides_stale_error(studio_window):
    """P0-6：`show_message` 必须收起上一次错误的错误视图。

    `ResultTable.show_message()`（改完 INSERT/UPDATE 之类无结果集的语句
    走这条）此前漏调 `_hide_error_view()`，上一次错误的错误视图会一直留在
    结果区（旧 xfail 记录）。P0 已修复：错误视图收起、表格恢复显示。
    """
    _require_gui()
    result = _result(studio_window)

    result.show_error('未知的表 no_such_table', errno=1146, sqlstate='42S02')
    assert not result.error_view.isHidden()
    result.show_message('影响 1 行')

    assert '影响 1 行' in result.info_label.text()
    # 正确行为：错误视图应当收起、表格应当恢复显示
    assert result.error_view.isHidden(), '错误视图没有收起（陈旧错误仍在显示）'
    assert not result.table.isHidden(), '表格没有恢复显示'


# ==================== 3. 结果表格 ====================

def test_result_table_rows_and_columns(studio_window):
    """喂 3 列 × 5 行：行数 / 列数 / 表头 / 单元格内容都要对。"""
    _require_gui()
    result = _result(studio_window)

    columns = ['id', 'name', 'score']
    rows = [
        [1, 'alice', 95.5],
        [2, 'bob', None],
        [3, 'carol', 88.0],
        [4, 'dave', 0.0],
        [5, 'eve', 77.7],
    ]
    result.show_result(columns, rows, elapsed_ms=1.5)
    table = result.table
    print(f'\n【B-5】结果表格：{table.rowCount()} 行 × {table.columnCount()} 列')

    assert table.columnCount() == 3 == len(columns)
    assert table.rowCount() == 5 == len(rows)
    headers = [table.horizontalHeaderItem(c).text() for c in range(3)]
    assert headers == columns, f'表头不对: {headers}'

    # 逐行核对（不依赖显示顺序：表格开了排序）
    got = {table.item(r, 0).text(): table.item(r, 1).text()
           for r in range(table.rowCount())}
    assert got == {'1': 'alice', '2': 'bob', '3': 'carol',
                   '4': 'dave', '5': 'eve'}, f'单元格内容/错位: {got}'
    assert table.item(0, 2).text() == '95.5'

    # None → 灰斜体 NULL（只有 bob 的 score 为空）
    null_cells = [(r, c) for r in range(5) for c in range(3)
                  if table.item(r, c) is not None
                  and table.item(r, c).text() == 'NULL']
    assert len(null_cells) == 1, f'NULL 单元格数量异常: {null_cells}'
    nr, _nc = null_cells[0]
    assert table.item(nr, 0).text() == '2', 'NULL 那一行不是 bob'

    assert '5' in result.info_label.text(), '状态栏没写返回行数'
    assert result.error_view.isHidden(), '新结果没有清掉错误视图'
    print(f"  → 表头 {headers}；状态栏 {result.info_label.text()!r}")


# ==================== 4. SQL 编辑器 ====================

def test_sql_editor_write_and_read_back(studio_window):
    """编辑器可写可读回，且 MainWindow 的插入接口指向同一个编辑器。"""
    _require_gui()
    win = studio_window
    editor = _editor(win)

    sql = 'SELECT * FROM users WHERE id = 1;'
    editor.set_sql(sql)
    assert editor.toPlainText().strip() == sql, 'set_sql 后读回不一致'
    print(f'\n【B-6】编辑器读回: {editor.toPlainText()!r}')

    editor.setPlainText('UPDATE t SET a = 1')
    assert editor.toPlainText() == 'UPDATE t SET a = 1', '直接写读回不一致'

    editor.set_sql('')
    assert editor.toPlainText() == ''

    # 历史 / 片段 / AI 面板共用的插入通路
    win._insert_sql_to_editor('SELECT 2')
    assert _editor(win).toPlainText().strip() == 'SELECT 2', \
        'MainWindow._insert_sql_to_editor 没写进当前编辑器'

    editor.selectAll()
    assert editor.toPlainText().strip() == 'SELECT 2'
    print('  → set_sql / setPlainText / _insert_sql_to_editor 均可读回')
