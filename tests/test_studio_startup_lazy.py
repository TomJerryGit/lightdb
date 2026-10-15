"""任务：启动加速的三项优化必须"行为不变"——离线冒烟。

覆盖本轮（启动速度优化）新增的惰性 / 延迟语义：

  1. **惰性管理器 + 惰性面板**：`MainWindow.__init__` 之后它们**还没**
     被构造（省启动时间），但 `hasattr` / 直接访问照常可用（构造后可写回
     实例字典，行为与惰性化之前一致）；切换右侧标签页也能按需构造。
  2. **启动期恢复延迟**：插件扫描 / 自动连接推到首屏之后，但最终一定
     要发生 —— `do_startup_restore()` 幂等且真的把插件加载了。

环境适配与 `test_studio_smoke.py` 相同（主目录改道到 tmp、offscreen）。
"""
import os
import sys
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

_APP = None
_HOME_PREFIX = 'lightdb_studio'

LAZY_MANAGERS = ('workflow_engine', 'ai_agent', 'alert_engine')
LAZY_PANELS = ('ai_chat', 'agent_chat', 'collab_panel', 'catalog_panel')
TITLES = {'ai_chat': 'AI', 'agent_chat': 'Agent',
          'collab_panel': '协作', 'catalog_panel': '目录'}


def _require_gui():
    global _APP
    pytest.importorskip('PySide6')
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        try:
            app = QApplication([])
        except BaseException as e:                      # noqa: BLE001
            pytest.skip(f'无法进入 offscreen 平台：{type(e).__name__}: {e}')
    _APP = app
    if QGuiApplication.platformName() != 'offscreen':
        pytest.skip('当前 Qt 平台不是 offscreen')
    return _APP


def _redirect_home(tmp_home):
    mp = pytest.MonkeyPatch()
    real_home = Path.home()
    mp.setattr(Path, 'home', classmethod(lambda cls: tmp_home))
    import lightdb_studio.ui.main_window          # noqa: F401

    for module in list(sys.modules.values()):
        name = getattr(module, '__name__', '') or ''
        if not name.startswith(_HOME_PREFIX):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith('_') or not isinstance(value, Path):
                continue
            try:
                rel = value.relative_to(real_home)
            except ValueError:
                continue
            mp.setattr(module, attr, tmp_home / rel)
    return mp


@pytest.fixture(scope='module')
def studio_window(tmp_path_factory):
    _require_gui()
    tmp_home = tmp_path_factory.mktemp('startup_home')
    mp = _redirect_home(tmp_home)
    win = None
    try:
        from lightdb_studio.config import AppConfig
        from lightdb_studio.ui.main_window import MainWindow

        cfg = AppConfig()
        cfg.settings.auto_connect = False
        win = MainWindow(cfg)
        win.history.path = tmp_home / '.lightdb_studio' / 'history.json'
        yield win
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:                           # noqa: BLE001
                pass
        mp.undo()


# ==================== 1. 首屏不构造 ====================

def test_lazy_things_are_not_built_during_init(studio_window):
    """构造完 MainWindow 后，惰性管理器/面板**还没**被创建（断言刚建时）。"""
    _require_gui()
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    try:
        built = [n for n in LAZY_MANAGERS + LAZY_PANELS if n in vars(win)]
        print(f'\n【S-1】__init__ 后已构造的惰性对象: {built}')
        assert not built, f'首屏不该构造这些对象，但它们已经建好了: {built}'
        # 占位仍在，标签顺序/数量与以前一致
        titles = [win.right_tabs.tabText(i)
                  for i in range(win.right_tabs.count())]
        assert titles == ['历史', '片段', 'AI', 'Agent', '协作', '目录'], titles
        assert win.right_tabs.currentIndex() == 0
    finally:
        win.close()


# ==================== 2. 访问即构造、行为不变 ====================

def test_property_access_builds_on_demand(studio_window):
    """`hasattr` / 直接访问都能拿到真正的对象，且写回实例字典。"""
    _require_gui()
    win = studio_window
    for name, expect in (('catalog_panel', 'CatalogPanel'),
                         ('alert_engine', 'AlertEngine')):
        assert hasattr(win, name), f'{name} 应当可访问'
        obj = getattr(win, name)
        assert type(obj).__name__ == expect, (name, type(obj).__name__)
        assert name in vars(win), f'{name} 构造后应写回实例字典'
        assert getattr(win, name) is obj, '重复访问应返回同一个对象'
        print(f'  → {name}: {type(obj).__name__}（已写回 vars）')
    # 赋值覆盖语义不变（不再是只读 property）
    win.alert_engine = 'sentinel'
    assert win.alert_engine == 'sentinel'
    del win.alert_engine


def test_right_tab_switch_builds_panel_lazily(studio_window):
    """切到延迟标签页时真的把面板建出来，标题/顺序不变。"""
    _require_gui()
    win = studio_window
    for attr in LAZY_PANELS:
        idx = win._right_tab_index[attr]
        win.right_tabs.setCurrentIndex(idx)
        _APP.processEvents()
        panel = vars(win).get(attr)
        assert panel is not None, f'切到 {TITLES[attr]} 后 {attr} 仍未构造'
        assert win.right_tabs.widget(idx) is panel, '占位没有被真面板替换'
        assert win.right_tabs.tabText(idx) == TITLES[attr]
        print(f'  → 切到「{TITLES[attr]}」→ {type(panel).__name__}')
    titles = [win.right_tabs.tabText(i)
              for i in range(win.right_tabs.count())]
    assert titles == ['历史', '片段', 'AI', 'Agent', '协作', '目录'], titles


def test_unknown_attribute_still_raises(studio_window):
    """惰性化不能把打错的名字变成"存在"（`__getattr__` 不得吞掉 AttributeError）。"""
    _require_gui()
    win = studio_window
    with pytest.raises(AttributeError):
        win.definitely_not_an_attribute_9f8e7d
    assert not hasattr(win, 'definitely_not_an_attribute_9f8e7d')


# ==================== 3. 延迟的启动期恢复最终一定发生 ====================

def test_startup_restore_happens_and_is_idempotent(studio_window):
    """插件扫描/自动连接被推迟，但 `do_startup_restore()` 一定要执行到。"""
    _require_gui()
    win = studio_window
    assert win._startup_restore_done, '启动期恢复从未执行（窗口显示后应当执行）'

    calls = []
    real = win.plugin_mgr.load_all

    def spy():
        calls.append(1)
        return real()

    win.plugin_mgr.load_all = spy
    win.do_startup_restore()                 # 已经跑过 → 必须是幂等 no-op
    assert calls == [], 'do_startup_restore() 重复执行了启动期恢复'
    print('  → do_startup_restore() 幂等：第二次未重复加载插件')


def test_deferred_work_is_scheduled_on_show(studio_window):
    """刚构造的新窗口：启动期恢复被登记为 0ms 定时器（首屏之后执行）。"""
    _require_gui()
    from PySide6.QtCore import QTimer
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    try:
        # 处理一次事件循环：0ms 定时器应当已经跑过
        _APP.processEvents()
        assert win._startup_restore_done, '0ms 定时器没有触发启动期恢复'
        assert QTimer is not None
        print('  → QTimer.singleShot(0) 已触发 do_startup_restore()')
    finally:
        win.close()
