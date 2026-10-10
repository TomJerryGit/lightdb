"""
函数注册表（统一入口）
"""
from typing import Any, Optional, Callable, Dict
from .string_funcs import STRING_FUNCTIONS
from .datetime_funcs import DATETIME_FUNCTIONS
from .math_funcs import MATH_FUNCTIONS
from .control_funcs import CONTROL_FUNCTIONS


class FunctionRegistry:
    """函数注册表"""

    def __init__(self):
        self.functions: Dict[str, Callable] = {}
        self._register_builtin()
        # 缓存
        self._cache: Dict[str, Any] = {}

    def _register_builtin(self):
        self.functions.update(STRING_FUNCTIONS)
        self.functions.update(DATETIME_FUNCTIONS)
        self.functions.update(MATH_FUNCTIONS)
        self.functions.update(CONTROL_FUNCTIONS)

    def register(self, name: str, fn: Callable):
        self.functions[name.upper()] = fn

    def get(self, name: str) -> Optional[Callable]:
        return self.functions.get(name.upper())

    def call(self, name: str, *args) -> Any:
        fn = self.get(name)
        if fn is None:
            raise ValueError(f"未知函数: {name}")
        return fn(*args)

    def has(self, name: str) -> bool:
        return name.upper() in self.functions

    def list_all(self) -> list:
        return sorted(self.functions.keys())

    def count(self) -> int:
        return len(self.functions)


# 全局注册表
_registry = FunctionRegistry()


def get_registry() -> FunctionRegistry:
    return _registry


def call_function(name: str, *args):
    return _registry.call(name, *args)


def has_function(name: str) -> bool:
    return _registry.has(name)