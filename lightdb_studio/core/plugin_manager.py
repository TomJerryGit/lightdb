"""
插件系统
- 扫描 plugins/ 目录
- 动态加载 Python 模块
- 插件接口：菜单项、工具栏按钮、SQL 处理器
"""
import importlib.util
import inspect
import os
import sys
import traceback
from pathlib import Path


class PluginBase:
    """插件基类 - 所有插件应继承"""
    name = "Plugin"
    version = "0.1"
    description = ""
    author = ""

    def __init__(self, app_context):
        """
        app_context: {
            'main_window': MainWindow,
            'get_connection': func,
            'show_message': func,
        }
        """
        self.ctx = app_context

    def on_load(self):
        """加载时调用"""
        pass

    def on_unload(self):
        """卸载时调用"""
        pass

    def get_menu_items(self):
        """
        返回菜单项 [{'menu': '工具', 'text': '示例', 'callback': func}]
        """
        return []

    def get_toolbar_buttons(self):
        """
        返回工具栏按钮 [{'text': '🔧 示例', 'callback': func}]
        """
        return []

    def process_sql(self, sql: str) -> str:
        """处理 SQL（重写时可选）"""
        return sql


class PluginInfo:
    """插件运行时信息"""
    def __init__(self, module, path):
        self.module = module
        self.path = path
        self.name = getattr(module, 'name', Path(path).stem)
        self.version = getattr(module, 'version', '0.1')
        self.description = getattr(module, 'description', '')
        self.author = getattr(module, 'author', '')
        self.instance = None
        self.enabled = True
        self.error = None


class PluginManager:
    def __init__(self, plugins_dir: str, app_context: dict):
        self.dir = Path(plugins_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.app_context = app_context
        self.plugins = {}

    def discover(self):
        """扫描插件目录"""
        found = []
        for fp in sorted(self.dir.glob('*.py')):
            if fp.name.startswith('_'):
                continue
            found.append(fp)
        return found

    def load_all(self):
        """加载所有插件"""
        results = []
        for fp in self.discover():
            info = self.load_one(fp)
            results.append(info)
        return results

    def load_one(self, filepath) -> PluginInfo:
        """加载单个插件"""
        filepath = Path(filepath)
        module_name = f"lightdb_plugin_{filepath.stem}"

        try:
            spec = importlib.util.spec_from_file_location(
                module_name, filepath)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            info = PluginInfo(None, str(filepath))
            info.error = f"加载失败: {e}\n{traceback.format_exc()}"
            self.plugins[filepath.stem] = info
            return info

        info = PluginInfo(module, str(filepath))

        # 找插件类
        plugin_class = None
        for name, obj in inspect.getmembers(module):
            if (inspect.isclass(obj) and issubclass(obj, PluginBase)
                    and obj is not PluginBase
                    and obj.__module__ == module_name):
                plugin_class = obj
                break

        if plugin_class is None:
            info.error = "未找到 PluginBase 子类"
            self.plugins[filepath.stem] = info
            return info

        try:
            info.instance = plugin_class(self.app_context)
            info.instance.on_load()
        except Exception as e:
            info.error = f"实例化失败: {e}"
            self.plugins[filepath.stem] = info
            return info

        self.plugins[filepath.stem] = info
        return info

    def unload_all(self):
        for info in self.plugins.values():
            if info.instance:
                try:
                    info.instance.on_unload()
                except Exception:
                    pass

    def reload_all(self):
        self.unload_all()
        self.plugins.clear()
        return self.load_all()

    def get_all(self):
        return list(self.plugins.values())

    def get_menu_items(self):
        """收集所有插件的菜单项"""
        items = []
        for info in self.plugins.values():
            if info.error or not info.instance:
                continue
            try:
                for item in info.instance.get_menu_items():
                    item['plugin'] = info.name
                    items.append(item)
            except Exception:
                pass
        return items

    def get_toolbar_buttons(self):
        items = []
        for info in self.plugins.values():
            if info.error or not info.instance:
                continue
            try:
                for item in info.instance.get_toolbar_buttons():
                    item['plugin'] = info.name
                    items.append(item)
            except Exception:
                pass
        return items

    def process_sql(self, sql: str) -> str:
        """让所有插件处理 SQL"""
        for info in self.plugins.values():
            if info.error or not info.instance:
                continue
            try:
                sql = info.instance.process_sql(sql)
            except Exception:
                pass
        return sql