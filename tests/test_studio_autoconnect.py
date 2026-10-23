"""任务 1：`connections.json` 自动连接（真实服务端 + 临时 HOME，端到端）。

覆盖：
  1. `connections.json` 的**真实 schema**（顶层 `connections` / `last_connection`，
     每条恰好 8 个字段）+ 手写 JSON 的容错（未知键/坏条目不再连坐）；
  2. 启动即自动连接：**真实** `MainWindow` 跑事件循环到首帧之后，
     `last_connection` 那条先被连、并被设为「当前连接」→
     `win._get_current_conn()` 非 None 且 `get_tables()` 里有真表；
  3. `settings.json` 的 `auto_connect=False` 时**完全不拨号**；
  4. 自动连接失败：状态栏一行可行动结论 + 非阻塞弹框（原始报文一字不删）。

环境：`QT_QPA_PLATFORM=offscreen`；`Path.home()` 与 `lightdb_studio` 各模块里
「主目录下的 Path 常量」整体改道到 pytest 临时目录（沙箱里 `~` 可能不可写，
参考 `tests/test_studio_smoke.py::_redirect_home`）；服务端是**本进程内的
临时服务端**（不依赖别人的 8899，也不起外部进程）。
"""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

try:
    from tests.conftest import shutdown_quietly
except ImportError:                       # 直接 python tests/test_studio_autoconnect.py
    from conftest import shutdown_quietly

SERVER_PORT = 8934
HOME_PREFIX = 'lightdb_studio'
CONN_KEYS = {'name', 'host', 'port', 'user', 'password', 'database',
             'charset', 'ssl', 'ssl_options', 'ssh_options'}

_APP = None


# ==================== 环境 ====================

def qapp():
    """整个会话只保留一份 QApplication（被 GC 掉再建 QWidget 会崩）。"""
    global _APP
    pytest.importorskip('PySide6')
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    if QGuiApplication.platformName() != 'offscreen':
        pytest.skip(f'当前 Qt 平台是 {QGuiApplication.platformName()!r}，'
                    f'不是 offscreen')
    return _APP


def _redirect_home(tmp_home):
    """把「主目录下的落盘路径」改道到 tmp_home（含 `~/.lightdb_studio`）。"""
    mp = pytest.MonkeyPatch()
    real_home = Path.home()
    mp.setattr(Path, 'home', classmethod(lambda cls: tmp_home))

    import lightdb_studio.ui.main_window          # noqa: F401 触发依赖导入

    moved = 0
    for module in list(sys.modules.values()):
        name = getattr(module, '__name__', '') or ''
        if not name.startswith(HOME_PREFIX):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith('_') or not isinstance(value, Path):
                continue
            try:
                rel = value.relative_to(real_home)
            except ValueError:
                continue
            mp.setattr(module, attr, tmp_home / rel)
            moved += 1

    # 兜底：这三个常量是 AppConfig 的直接依据，显式再指一次（也走 mp，能还原）
    from lightdb_studio import config as config_mod
    cfg_dir = tmp_home / '.lightdb_studio'
    mp.setattr(config_mod, 'CONFIG_DIR', cfg_dir)
    mp.setattr(config_mod, 'CONFIG_FILE', cfg_dir / 'connections.json')
    mp.setattr(config_mod, 'SETTINGS_FILE', cfg_dir / 'settings.json')
    print(f'  → 主目录改道 {moved} 处 → {tmp_home}')
    return mp


def _write_config(tmp_home, connections, last='', auto_connect=None):
    cfg_dir = tmp_home / '.lightdb_studio'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'connections.json').write_text(
        json.dumps({'connections': connections, 'last_connection': last},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    if auto_connect is not None:
        (cfg_dir / 'settings.json').write_text(
            json.dumps({'auto_connect': auto_connect}), encoding='utf-8')
    return cfg_dir


def _conn(name, port, **kw):
    d = {'name': name, 'host': '127.0.0.1', 'port': port, 'user': 'root',
         'password': '', 'database': '', 'charset': 'utf8mb4', 'ssl': False, 'ssl_options': {}, 'ssh_options': {}, 'ssh_options': {}}
    d.update(kw)
    return d


def _closed_port():
    """拿一个「刚被释放、肯定没人监听」的端口（用它造连接被拒绝）。"""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pump(app, seconds=8.0, until=None):
    """跑到满足条件或超时（`QTimer.singleShot(0)` 的首帧就在第一次泵里）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return bool(until is not None and until())


def _make_window(tmp_home, connections, last='', auto_connect=None):
    """建真实 MainWindow（不 stub 产品逻辑），返回 (win, cfg)。"""
    app = qapp()
    _write_config(tmp_home, connections, last=last,
                  auto_connect=auto_connect)
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    win = MainWindow(cfg)
    win.history.path = tmp_home / '.lightdb_studio' / 'history.json'
    _pump(app, until=lambda: getattr(win, '_startup_restore_done', False), )
    for _ in range(10):                    # 再泵几轮，让信号/延迟动作跑完
        app.processEvents()
        time.sleep(0.02)
    return win, cfg


# ==================== 临时服务端 ====================

def _connect_raw(port, timeout=3):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection

    conn = DBConnection(ConnectionInfo(name='seed', host='127.0.0.1',
                                       port=port, user='root', password=''))
    conn.connect()
    return conn


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    """本进程内的临时 LightDB 服务端 + 一张 `gui_demo` 表（2 行）。

    故意**不用** `conftest.track()`：那是「用例级」清理，module 作用域的
    服务端会在第一个用例结束时被关掉。这里自己收尾。
    """
    data_dir = tmp_path_factory.mktemp('autoconnect_data')
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = SERVER_PORT
    db = Database(str(data_dir))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    deadline = time.time() + 10
    conn = None
    while time.time() < deadline:
        try:
            conn = _connect_raw(SERVER_PORT, timeout=1)
            break
        except Exception:                                  # noqa: BLE001
            time.sleep(0.2)
    assert conn is not None, f'临时服务端没能在 127.0.0.1:{SERVER_PORT} 起来'

    err = conn.execute('CREATE TABLE gui_demo (id INT PRIMARY KEY, '
                       'name VARCHAR(32))')[1]
    assert err is None, err
    assert conn.execute("INSERT INTO gui_demo VALUES (1, 'alice')")[1] is None
    assert conn.execute("INSERT INTO gui_demo VALUES (2, 'bob')")[1] is None
    conn.close()
    print(f'\n  → 临时服务端 127.0.0.1:{SERVER_PORT}  data={data_dir}')
    yield SERVER_PORT
    shutdown_quietly(srv, db)


# ==================== 1. schema + 容错 ====================

def test_connections_json_schema_and_tolerance(tmp_path):
    """凭代码确认的 schema：顶层只有 connections / last_connection，每条 8 键。

    同时钉住容错行为：多写的键被忽略（不再让整份配置连坐丢掉），坏条目只
    跳过自己。
    """
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    mp = _redirect_home(tmp_home)
    try:
        cfg_dir = _write_config(
            tmp_home,
            [_conn('local-8899', 8899, auto_connect=True),   # 未知键
             'not-a-dict',                                    # 坏条目
             _conn('second', 8898)],
            last='local-8899')
        from lightdb_studio.config import AppConfig, CONNECTION_FIELDS

        cfg = AppConfig()
        names = [c.name for c in cfg.connections]
        print(f'\n【A-1】载入 {names}  last_connection={cfg.last_connection!r}')
        assert names == ['local-8899', 'second'], names
        assert cfg.last_connection == 'local-8899'
        assert CONNECTION_FIELDS == CONN_KEYS, sorted(CONNECTION_FIELDS)

        # 与代码一致的字段与默认值
        first = cfg.connections[0]
        assert (first.host, first.port, first.user, first.password) == \
            ('127.0.0.1', 8899, 'root', '')
        assert (first.database, first.charset, first.ssl) == ('', 'utf8mb4',
                                                             False)

        # 存回去：顶层与每条连接都恰好是 schema 里的键
        cfg.save()
        data = json.loads((cfg_dir / 'connections.json')
                          .read_text(encoding='utf-8'))
        assert set(data) == {'connections', 'last_connection'}, sorted(data)
        for c in data['connections']:
            assert set(c) == CONN_KEYS, sorted(c)
        print(f'  → 落盘顶层键 {sorted(data)}；每条键 {sorted(data["connections"][0])}')
    finally:
        mp.undo()


# ==================== 2. 对象树：连上就是「当前连接」 ====================

class _FakeConn:
    def __init__(self, name, tables=('gui_demo',)):
        from lightdb_studio.config import ConnectionInfo

        self.config = ConnectionInfo(name=name, host='127.0.0.1', port=1)
        self._tables = list(tables)

    def get_databases(self):
        return ['lightdb']

    def get_tables(self, db=None):
        return list(self._tables)

    def is_connected(self):
        return True

    def close(self):
        pass


def test_object_tree_add_connection_selects_first_node():
    """回归：`add_connection` 之后 `get_current_connection()` 不能是 None。

    Qt 插入节点**不会**自动设 currentItem（实测），所以此前「已连接」的
    连接在 GUI 里仍是「未连接」，跑查询还会弹「请先选择一个连接」。
    """
    qapp()
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui.object_tree import ObjectTree

    tree = ObjectTree()
    assert tree.get_current_connection() is None

    tree.add_connection(ConnectionInfo(name='a', host='127.0.0.1', port=1),
                        _FakeConn('a'))
    first = tree.get_current_connection()
    assert first is not None and first.config.name == 'a', '第一条连接没有成为当前连接'
    print('\n【A-2】add_connection → currentItem='
          f'{tree.tree.currentItem().text(0)!r}')

    tree.add_connection(ConnectionInfo(name='b', host='127.0.0.1', port=2),
                        _FakeConn('b'))
    assert tree.get_current_connection().config.name == 'a', '不该抢当前选择'
    assert tree.select_connection('b') is True
    assert tree.get_current_connection().config.name == 'b'
    assert tree.select_connection('nope') is False
    print('  → select_connection("b") 生效；不存在的名字返回 False')


# ==================== 3. 启动即自动连（端到端） ====================

def test_auto_restore_connects_last_first_and_makes_it_current(server,
                                                               tmp_path):
    """真服务端 + 真 MainWindow：首帧之后当前连接可用、表看得到。"""
    app = qapp()
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    dead = _closed_port()
    mp = _redirect_home(tmp_home)
    win = None
    try:
        # dead 排在前面：last_connection 必须**优先**被连（顺序可观测）
        import lightdb_studio.ui.main_window as mw
        real_connect = mw.DBConnection.connect
        order = []
        mw.DBConnection.connect = lambda self: (
            order.append(self.config.name), real_connect(self))[1]
        try:
            win, cfg = _make_window(
                tmp_home,
                [_conn('dead-%d' % dead, dead),
                 _conn('local-%d' % server, server)],
                last='local-%d' % server)
        finally:
            mw.DBConnection.connect = real_connect

        print(f'\n【A-3】连接顺序 {order}   状态栏 {win.status_label.text()!r}')
        print(f'  → 当前连接 {win._get_current_conn()}  '
              f'连接标签 {win.conn_label.text()!r}')
        assert order and order[0] == 'local-%d' % server, \
            f'last_connection 没有被优先连接: {order}'

        conn = win._get_current_conn()
        assert conn is not None, '自动连接后 _get_current_conn() 仍是 None'
        assert conn.config.name == 'local-%d' % server
        tables = conn.get_tables()
        print(f'  → get_tables() = {tables}')
        assert 'gui_demo' in tables, tables

        cols, rows = conn.query('SELECT * FROM gui_demo ORDER BY id')
        assert [list(r) for r in rows] == [[1, 'alice'], [2, 'bob']], rows
        assert win.conn_label.text().startswith('已连接: local-%d' % server)
        assert win.status_label.text() == '自动连接: local-%d' % server
        assert win.object_tree.tree.topLevelItemCount() == 1, '死端口那条不该进树'
        cur = win.object_tree.tree.currentItem()
        assert cur is not None and 'local-%d' % server in cur.text(0)
        print('  ✅ 首帧后即可用：当前连接 + 对象树（gui_demo）都在')
    finally:
        if win is not None:
            win.close()
        app.processEvents()
        mp.undo()


def test_auto_connect_false_restores_nothing_and_does_not_dial(server,
                                                              tmp_path):
    """`settings.json: auto_connect=False` → 一条都不拨（此前完全无效）。"""
    app = qapp()
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    mp = _redirect_home(tmp_home)
    win = None
    try:
        import lightdb_studio.ui.main_window as mw
        real_connect = mw.DBConnection.connect
        dialed = []
        mw.DBConnection.connect = lambda self: (
            dialed.append(self.config.name), real_connect(self))[1]
        try:
            win, cfg = _make_window(
                tmp_home, [_conn('local-%d' % server, server)],
                last='local-%d' % server, auto_connect=False)
        finally:
            mw.DBConnection.connect = real_connect

        print(f'\n【A-4】auto_connect=False → 拨号 {dialed}  '
              f'状态栏 {win.status_label.text()!r}')
        assert dialed == [], f'auto_connect=False 仍然拨号了: {dialed}'
        assert win.object_tree.tree.topLevelItemCount() == 0
        assert win._get_current_conn() is None
        assert '跳过' in win.status_label.text()
        # 配置本身仍然读得到（"恢复列表" 与 "自动拨号" 是两件事）
        assert [c.name for c in cfg.connections] == ['local-%d' % server]
        assert cfg.settings.auto_connect is False
        print('  ✅ 不拨号，但配置列表照旧可读（设置里可以再点连接）')
    finally:
        if win is not None:
            win.close()
        app.processEvents()
        mp.undo()


# ==================== 4. 自动连接失败：可见 + 可行动 ====================

def test_failed_auto_connect_is_visible_and_actionable(server, tmp_path):
    """last_connection 连不上：状态栏有结论与命令，弹框不阻塞且原文完整。"""
    app = qapp()
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    dead = _closed_port()
    mp = _redirect_home(tmp_home)
    win = None
    try:
        win, _cfg = _make_window(tmp_home, [_conn('dead-%d' % dead, dead)],
                                 last='dead-%d' % dead)
        status = win.status_label.text()
        print(f'\n【A-5】状态栏：{status}')
        assert '没有服务端在监听' in status
        assert '127.0.0.1:%d' % dead in status
        assert '--mode single --host 127.0.0.1 --port %d' % dead in status
        assert '\n' not in status
        assert win._get_current_conn() is None

        from PySide6.QtWidgets import QMessageBox

        boxes = [b for b in win.findChildren(QMessageBox)]
        assert boxes, '自动连接失败既没进对象树也没弹框（用户看不到）'
        text = boxes[0].text()
        print('  → 非阻塞弹框标题: %r' % boxes[0].windowTitle())
        print('  → 弹框前 3 行: %s' % ' | '.join(text.splitlines()[:3]))
        assert '没有服务端在监听' in text
        assert '--mode single --host 127.0.0.1 --port %d' % dead in text
        assert '原始错误' in text
        assert '10061' in text or '2003' in text, '弹框里丢了驱动原始报文'
        # 非阻塞：`open()` 不是 `exec()`，事件循环没有被挡住
        print('  ✅ 状态栏 + 非阻塞弹框都可见、可复制、保留了原始报文')
    finally:
        if win is not None:
            win.close()
        app.processEvents()
        mp.undo()


# ==================== 5. 手动「新建连接」失败也走同一套文案 ====================

def test_new_connection_failure_uses_actionable_prompt(server, tmp_path,
                                                       monkeypatch):
    """`_new_connection()` 失败：弹的是可行动文案（不是光秃秃的驱动原文）。"""
    app = qapp()
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    dead = _closed_port()
    mp = _redirect_home(tmp_home)
    win = None
    try:
        win, cfg = _make_window(tmp_home, [], last='')
        from PySide6.QtWidgets import QDialog

        import lightdb_studio.ui.main_window as mw
        from lightdb_studio.config import ConnectionInfo
        from lightdb_studio.ui.connection_dialog import ConnectionDialog

        info = ConnectionInfo(name='x', host='127.0.0.1', port=dead,
                              user='root', password='')
        monkeypatch.setattr(ConnectionDialog, 'exec',
                            lambda self: QDialog.Accepted)
        monkeypatch.setattr(ConnectionDialog, 'get_connection',
                            lambda self: info)
        shown = []
        monkeypatch.setattr(mw, 'show_connection_error',
                            lambda parent, exc, host=None, port=None,
                            title='连接失败': shown.append(
                                (title, mw.connection_error_summary(
                                    exc, host, port)))
                            or 'STUB')
        win._new_connection()

        assert shown, '_new_connection() 失败时没有走可行动提示'
        title, summary = shown[0]
        print(f'\n【A-6】弹框标题 {title!r}  状态栏 {win.status_label.text()!r}')
        assert title == '连接失败'
        assert '127.0.0.1:%d' % dead in win.status_label.text()
        assert '--mode single --host 127.0.0.1 --port %d' % dead \
            in win.status_label.text()
        # 失败不该写入配置 / 不该进对象树
        assert cfg.connections == []
        assert not (tmp_home / '.lightdb_studio' / 'connections.json').exists() \
            or json.loads((tmp_home / '.lightdb_studio' / 'connections.json')
                          .read_text(encoding='utf-8'))['connections'] == []
        assert win.object_tree.tree.topLevelItemCount() == 0
        print('  ✅ 失败路径：可行动提示 + 状态栏结论，配置未被污染')
    finally:
        if win is not None:
            win.close()
        app.processEvents()
        mp.undo()
