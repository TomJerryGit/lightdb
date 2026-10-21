"""LightDB Studio PyInstaller 入口（打包专用）。

用独立入口模块而不是直接打包 `lightdb_studio/app.py`，因为 app.py 使用包内
相对导入（`from .core.fonts import ...`），作为脚本入口会导致 ImportError。
"""
import multiprocessing
import sys

from lightdb_studio.app import run


if __name__ == '__main__':
    # Windows 下若未来使用 multiprocessing（打包后必须 freeze_support）
    multiprocessing.freeze_support()
    sys.exit(run())
