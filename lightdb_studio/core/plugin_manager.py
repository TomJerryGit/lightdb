"""
插件系统（P2-8 产品化）
- 扫描 plugins/ 目录，动态加载 Python 模块
- 插件接口：菜单项、工具栏按钮、SQL 处理器
- 支持启用/禁用（状态持久化，可注入 state_path）、单个/全部重载、
  on_unload 闭环、sys.modules 清理；坏插件失败只影响自己且错误可见
- 菜单/工具栏注入 API：build_menu_actions / build_toolbar_actions /
  install_actions(main_window)（返回已安装 action 与错误，不静默）

本模块**不强制依赖 Qt**：只有 build/install actions 时才延迟导入 PySide6。
"""
import importlib.util
import inspect
import json
import os
import sys
import time
import traceback
from pathlib import Path

DEFAULT_STATE_PATH = Path.home() / '.lightdb_studio' / 'plugins_state.json'
MODULE_PREFIX = 'lightdb_plugin_'


class PluginBase:
    """插件基类 - 所有插件应继承"""

    name = "Plugin"
    version = "0.1"
    description = ""
    author = ""

    def __init__(self, app_context):
        self.ctx = app_context or {}

    def on_load(self):
        """加载时调用"""
        pass

    def on_unload(self):
        """卸载时调用"""
        pass

    def get_menu_items(self):
        """返回 [{'menu': '工具', 'text': '...', 'callback': func}]"""
        return []

    def get_toolbar_buttons(self):
        """返回 [{'text': '...', 'callback': func}]"""
        return []

    def process_sql(self, sql: str) -> str:
        """处理 SQL（重写时可选）；不得改变 SQL 语义"""
        return sql


class PluginInfo:
    """插件运行时信息（面板/测试可直接读取）。"""

    def __init__(self, module=None, path=''):
        self.module = module
        self.path = str(path or '')
        self.key = Path(self.path).stem if self.path else ''
        self.name = getattr(module, 'name', Path(self.path).stem if self.path else 'Plugin')
        self.version = str(getattr(module, 'version', '0.1'))
        self.description = getattr(module, 'description', '')
        self.author = getattr(module, 'author', '')
        self.instance = None
        self.enabled = True
        self.loaded = False
        self.error = None
        self.last_error = ''

    @property
    def status(self) -> str:
        if self.error:
            return 'failed'
        if not self.enabled:
            return 'disabled'
        return 'loaded' if (self.instance is not None and self.loaded) else 'failed'

    def to_dict(self) -> dict:
        return {'key': self.key, 'name': self.name, 'version': self.version,
                'author': self.author, 'description': self.description,
                'path': self.path, 'status': self.status,
                'enabled': self.enabled, 'error': self.error or ''}

    def __repr__(self):
        return f"PluginInfo({self.key!r}, status={self.status!r})"


class PluginManager:
    def __init__(self, plugins_dir: str, app_context: dict = None,
                 state_path: str = None):
        self.dir = Path(plugins_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.app_context = dict(app_context or {})
        self.state_path = (Path(state_path) if state_path
                           else DEFAULT_STATE_PATH)
        self.plugins = {}
        self.disabled = set()
        self.warnings = []
        self.listeners = []
        self.load_state()

    # ==================== 状态持久化 ====================

    def load_state(self):
        """读取启停状态；文件损坏时显式告警并回退默认（不崩溃）。"""
        self.disabled = set()
        path = self.state_path
        if not path.exists():
            return self.disabled
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                disabled = data.get('disabled') or []
                if isinstance(disabled, (list, tuple)):
                    self.disabled = {str(x) for x in disabled if str(x)}
                else:
                    raise ValueError('disabled 字段不是数组')
            elif isinstance(data, list):
                self.disabled = {str(x) for x in data if str(x)}
            else:
                raise ValueError('状态文件根节点不是对象/数组')
        except Exception as exc:                      # noqa: BLE001
            msg = f'插件状态文件 {path} 读取失败，回退为全部启用: {exc}'
            self.warnings.append(msg)
            print(f'[plugins] WARN {msg}', file=sys.stderr)
            self.disabled = set()
        return self.disabled

    def save_state(self) -> bool:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {'disabled': sorted(self.disabled), 'updated_at': time.time()}
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding='utf-8')
            tmp.replace(self.state_path)
            return True
        except Exception as exc:                      # noqa: BLE001
            msg = f'插件状态写入失败 {self.state_path}: {exc}'
            self.warnings.append(msg)
            print(f'[plugins] WARN {msg}', file=sys.stderr)
            return False

    def is_enabled(self, key: str) -> bool:
        return key not in self.disabled

    # ==================== 加载 / 卸载 ====================

    def discover(self):
        return [fp for fp in sorted(self.dir.glob('*.py'))
                if not fp.name.startswith('_')]

    def _module_name(self, stem: str) -> str:
        return MODULE_PREFIX + stem

    def _cleanup_module(self, stem: str):
        """重载前清理旧模块，避免 import 缓存导致旧代码/旧实例残留。"""
        name = self._module_name(stem)
        for key in [k for k in list(sys.modules)
                    if k == name or k.startswith(name + '.')]:
            sys.modules.pop(key, None)
        try:
            importlib.invalidate_caches()
        except Exception:
            pass

    def load_one(self, filepath, respect_state: bool = True) -> PluginInfo:
        filepath = Path(filepath)
        stem = filepath.stem
        module_name = self._module_name(stem)
        enabled = (stem not in self.disabled) if respect_state else True
        self._cleanup_module(stem)

        info = PluginInfo(None, str(filepath))
        info.key = stem
        info.enabled = enabled
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, filepath)
            if spec is None or spec.loader is None:
                raise ImportError(f'无法为 {filepath} 创建模块加载器')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:                      # noqa: BLE001
            info.module = None
            info.error = f"加载失败: {exc}\n{traceback.format_exc()}"
            self._cleanup_module(stem)
            self.plugins[stem] = info
            return info

        info.module = module
        info.name = getattr(module, 'name', stem)
        info.version = str(getattr(module, 'version', '0.1'))
        info.description = getattr(module, 'description', '')
        info.author = getattr(module, 'author', '')

        plugin_class = None
        for _name, obj in inspect.getmembers(module):
            if (inspect.isclass(obj) and issubclass(obj, PluginBase)
                    and obj is not PluginBase
                    and obj.__module__ == module_name):
                plugin_class = obj
                break
        if plugin_class is None:
            info.error = '未找到 PluginBase 子类'
            self._cleanup_module(stem)
            self.plugins[stem] = info
            return info
        if not enabled:
            # 禁用状态：只解析元数据，不实例化 / 不 on_load
            self.plugins[stem] = info
            return info
        try:
            info.instance = plugin_class(self.app_context)
            info.instance.on_load()
            info.loaded = True
        except Exception as exc:                      # noqa: BLE001
            info.instance = None
            info.loaded = False
            info.error = f"实例化/on_load 失败: {exc}"
            self._cleanup_module(stem)
        self.plugins[stem] = info
        return info

    def load_all(self):
        """重新扫描加载所有插件；每个插件独立失败，返回 PluginInfo 列表。"""
        for info in list(self.plugins.values()):
            self._unload_instance(info)
        self.plugins = {}
        results = []
        for fp in self.discover():
            try:
                results.append(self.load_one(fp))
            except Exception as exc:                  # noqa: BLE001
                info = PluginInfo(None, str(fp))
                info.key = fp.stem
                info.error = f"加载失败: {exc}"
                self.plugins[fp.stem] = info
                results.append(info)
        self._notify('load_all')
        return results

    def _unload_instance(self, info: PluginInfo):
        if info is None or info.instance is None:
            return True
        try:
            info.instance.on_unload()
            return True
        except Exception as exc:                      # noqa: BLE001
            msg = f'插件 {getattr(info, "name", "?")} on_unload 失败: {exc}'
            info.last_error = msg
            self.warnings.append(msg)
            print(f'[plugins] WARN {msg}', file=sys.stderr)
            return False

    def unload_all(self):
        """卸载当前所有实例（窗口关闭时调用），不删除元数据。"""
        ok = True
        for info in list(self.plugins.values()):
            if not self._unload_instance(info):
                ok = False
            info.instance = None
            info.loaded = False
        self._notify('unload_all')
        return ok

    # ==================== 启用 / 禁用 / 重载 ====================

    def _result(self, ok: bool, **kwargs) -> dict:
        out = {'ok': bool(ok), 'error': '', 'info': None, 'warnings': []}
        out.update(kwargs)
        out['warnings'] = list(self.warnings[-5:])
        return out

    def get(self, key: str) -> PluginInfo:
        return self.plugins.get(key)

    def get_all(self):
        return [self.plugins[k] for k in sorted(self.plugins)]

    def get_active(self):
        return [info for info in self.get_all()
                if info.enabled and info.loaded and info.instance is not None
                and not info.error]

    def set_enabled(self, key: str, enabled: bool) -> dict:
        info = self.plugins.get(key)
        if info is None:
            return self._result(False, error=f'插件不存在: {key}')
        if enabled:
            if info.enabled and info.loaded and not info.error:
                return self._result(True, info=info)
            self.disabled.discard(key)
            saved = self.save_state()
            fresh = self.load_one(info.path, respect_state=False)
            self._notify('enable')
            if fresh.error:
                return self._result(False, error=fresh.error, info=fresh,
                                    state_saved=saved)
            return self._result(True, info=fresh, state_saved=saved)
        # disable
        if not info.enabled:
            return self._result(True, info=info)
        unload_ok = self._unload_instance(info)
        info.instance = None
        info.loaded = False
        info.enabled = False
        self.disabled.add(key)
        saved = self.save_state()
        self._notify('disable')
        if not saved:
            return self._result(False, info=info,
                                error='插件已禁用，但状态持久化写入失败',
                                state_saved=False)
        return self._result(unload_ok, info=info,
                            error='' if unload_ok else
                            f'{info.name} on_unload 失败（已禁用但需检查日志）',
                            state_saved=True)

    def enable(self, key: str) -> dict:
        return self.set_enabled(key, True)

    def disable(self, key: str) -> dict:
        return self.set_enabled(key, False)

    def reload_one(self, key: str) -> dict:
        """单个插件重载：unload → 清 sys.modules → 重新 import → on_load。"""
        info = self.plugins.get(key)
        if info is None:
            # 允许按文件名重载尚未加载的插件
            candidates = {fp.stem: fp for fp in self.discover()}
            if key not in candidates:
                return self._result(False, error=f'插件不存在: {key}')
            fresh = self.load_one(candidates[key])
            self._notify('reload')
            return self._result(not bool(fresh.error),
                                info=fresh, error=fresh.error or '')
        unload_ok = self._unload_instance(info)
        self._cleanup_module(key)
        path = info.path
        del self.plugins[key]
        # 尊重当前启停状态：disabled 插件重载后仍不实例化/不 on_load
        fresh = self.load_one(path)
        self._notify('reload')
        if fresh.error:
            return self._result(False, info=fresh,
                                error=fresh.error,
                                unload_ok=unload_ok)
        return self._result(True, info=fresh, unload_ok=unload_ok)

    def reload_all(self):
        results = self.load_all()
        self._notify('reload_all')
        return results

    # ==================== 菜单 / 工具栏注入 ====================

    def get_menu_items(self):
        items = []
        for info in self.get_active():
            try:
                plugin_items = info.instance.get_menu_items() or []
            except Exception as exc:                  # noqa: BLE001
                info.last_error = f'get_menu_items 失败: {exc}'
                self.warnings.append(f'[{info.name}] {info.last_error}')
                continue
            for item in plugin_items:
                if not isinstance(item, dict) or not callable(item.get('callback')):
                    continue
                item = dict(item)
                item['plugin'] = info.name
                item['plugin_key'] = info.key
                items.append(item)
        return items

    def get_toolbar_buttons(self):
        items = []
        for info in self.get_active():
            try:
                buttons = info.instance.get_toolbar_buttons() or []
            except Exception as exc:                  # noqa: BLE001
                info.last_error = f'get_toolbar_buttons 失败: {exc}'
                self.warnings.append(f'[{info.name}] {info.last_error}')
                continue
            for item in buttons:
                if not isinstance(item, dict) or not callable(item.get('callback')):
                    continue
                item = dict(item)
                item['plugin'] = info.name
                item['plugin_key'] = info.key
                items.append(item)
        return items

    def build_menu_actions(self, parent=None):
        """返回 QAction 列表（不依赖主窗口；测试可直接检查 text/plugin）。"""
        from PySide6.QtGui import QAction
        actions = []
        for item in self.get_menu_items():
            try:
                action = QAction(str(item.get('text') or '插件菜单'), parent)
                action.setProperty('plugin', item.get('plugin', ''))
                action.setProperty('plugin_key', item.get('plugin_key', ''))
                action.setProperty('menu', item.get('menu', '工具'))
                action.triggered.connect(item['callback'])
                actions.append(action)
            except Exception as exc:                  # noqa: BLE001
                self.warnings.append(f'构建菜单 action 失败: {exc}')
        return actions

    def build_toolbar_actions(self, parent=None):
        from PySide6.QtGui import QAction
        actions = []
        for item in self.get_toolbar_buttons():
            try:
                action = QAction(str(item.get('text') or '插件'), parent)
                action.setProperty('plugin', item.get('plugin', ''))
                action.setProperty('plugin_key', item.get('plugin_key', ''))
                action.triggered.connect(item['callback'])
                actions.append(action)
            except Exception as exc:                  # noqa: BLE001
                self.warnings.append(f'构建工具栏 action 失败: {exc}')
        return actions

    def install_actions(self, main_window=None) -> dict:
        """把插件菜单/工具栏 action 安装到主窗口（若有），返回安装结果。

        不修改 MainWindow 源码；由面板/调用方传入窗口即可。返回：
        {'ok','menu_actions','toolbar_actions','errors','menu_texts','toolbar_texts'}
        """
        result = {'ok': True, 'menu_actions': [], 'toolbar_actions': [],
                  'errors': [], 'menu_texts': [], 'toolbar_texts': []}
        try:
            result['menu_actions'] = self.build_menu_actions(main_window)
            result['toolbar_actions'] = self.build_toolbar_actions(main_window)
        except Exception as exc:                      # noqa: BLE001
            result['ok'] = False
            result['errors'].append(f'构建插件 action 失败: {exc}')
            return result
        result['menu_texts'] = [a.text() for a in result['menu_actions']]
        result['toolbar_texts'] = [a.text() for a in result['toolbar_actions']]
        if main_window is not None:
            try:
                menu_bar = main_window.menuBar()
                from PySide6.QtWidgets import QMenu
                for action in result['menu_actions']:
                    title = action.property('menu') or '工具'
                    target_menu = None
                    for menu in menu_bar.findChildren(QMenu):
                        if menu.title().replace('&', '') == title:
                            target_menu = menu
                            break
                    if target_menu is None:
                        target_menu = menu_bar.addMenu(title)
                    target_menu.addAction(action)
            except Exception as exc:                  # noqa: BLE001
                result['ok'] = False
                result['errors'].append(f'安装插件菜单失败: {exc}')
            try:
                from PySide6.QtWidgets import QToolBar
                toolbars = []
                try:
                    toolbars = main_window.findChildren(QToolBar)
                except Exception:
                    toolbars = []
                toolbar = toolbars[0] if toolbars else getattr(
                    main_window, 'toolbar', None)
                if toolbar is not None:
                    for action in result['toolbar_actions']:
                        toolbar.addAction(action)
            except Exception as exc:                  # noqa: BLE001
                result['ok'] = False
                result['errors'].append(f'安装插件工具栏失败: {exc}')
        return result

    # ==================== SQL 处理 / 监听 ====================

    def process_sql(self, sql: str) -> str:
        for info in self.get_active():
            try:
                sql = info.instance.process_sql(sql)
            except Exception as exc:                  # noqa: BLE001
                info.last_error = f'process_sql 失败: {exc}'
                self.warnings.append(f'[{info.name}] {info.last_error}')
                continue
        return sql

    def add_listener(self, callback):
        if callable(callback):
            self.listeners.append(callback)

    def remove_listener(self, callback):
        try:
            self.listeners.remove(callback)
        except ValueError:
            pass

    def _notify(self, event: str):
        for cb in list(self.listeners):
            try:
                cb(event)
            except Exception as exc:                  # noqa: BLE001
                self.warnings.append(f'插件监听器失败: {exc}')
