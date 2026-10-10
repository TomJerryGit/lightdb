"""
LightDB Studio 插件目录

放置 .py 文件即可被 PluginManager 自动加载。
每个插件必须:
1. 定义 name / version / description / author（模块级变量）
2. 定义一个继承自 PluginBase 的类
"""

from pathlib import Path

# 当前插件目录
PLUGIN_DIR = Path(__file__).parent


def list_plugins():
    """列出当前目录下所有 .py 插件文件（不含 __init__.py）"""
    return sorted([
        p for p in PLUGIN_DIR.glob('*.py')
        if p.name != '__init__.py' and not p.name.startswith('_')
    ])


def get_plugin_dir() -> Path:
    """返回插件目录路径（供 PluginManager 使用）"""
    return PLUGIN_DIR


__all__ = ['PLUGIN_DIR', 'list_plugins', 'get_plugin_dir']