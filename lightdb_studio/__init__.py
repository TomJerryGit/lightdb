"""
LightDB Studio - 图形化数据库管理工具

支持:
- 桌面版 (PySide6)
- Web 版 (FastAPI)
- CLI 工具

主要入口:
    lightdb-studio          # 启动桌面版
    lightdb-studio-web      # 启动 Web 版
    lightdb-studio-cli      # CLI 工具
"""

__version__ = "1.0.0"
__author__ = "LightDB Team"
__license__ = "MIT"
__url__ = "https://github.com/lightdb/studio"

# 包元信息
__all__ = [
    '__version__',
    '__author__',
    '__license__',
    'get_version',
    'is_gui_available',
]


def get_version() -> str:
    """返回版本号"""
    return __version__


def is_gui_available() -> bool:
    """
    检查桌面版依赖是否就绪。
    用于 CLI 环境避免误引入 PySide6。
    """
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        return False


def is_web_available() -> bool:
    """检查 Web 版依赖是否就绪"""
    try:
        import fastapi  # noqa: F401
        return True
    except ImportError:
        return False


# ---- 延迟导入：只有真正访问时才加载 UI / 核心模块 ----

def __getattr__(name):
    """
    PEP 562 延迟导入。
    这样 `import lightdb_studio` 不会拉起 PySide6 / FastAPI。

    注意：**不能**写 `from . import config`。在包内有 `__getattr__` 时，
    外层 `from lightdb_studio import config` 会先触发本函数，而函数内部的
    `from . import config` 又走一次 fromlist 处理 → 相互递归
    （RecursionError）。用 `importlib.import_module` 直接导入子模块，
    导入完成后 Python 会自动把它 setattr 到包上。
    """
    import importlib
    if name in ('app', 'config', 'core', 'ui', 'db', 'net', 'utils'):
        return importlib.import_module(f'.{name}', __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")