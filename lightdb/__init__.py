# lightdb/__init__.py
"""LightDB - Python 实现的 MySQL 兼容数据库"""

__version__ = "4.0.0"
__author__ = "LightDB Team"
__license__ = "MIT"

from .logger import logger
from .config import Config

__all__ = ['logger', 'Config', '__version__']