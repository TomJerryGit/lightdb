"""
Cython 模块加载器
优先使用 Cython 加速版本，回退到 Python
"""
import os
import sys
import warnings


# 检测哪些 Cython 模块可用
AVAILABLE = {}
_IMPORT_ERRORS = {}


def _try_import(name):
    """尝试导入 Cython 模块"""
    try:
        mod = __import__(f'lightdb.cython.{name}', fromlist=[name])
        return mod
    except ImportError as e:
        _IMPORT_ERRORS[name] = str(e)
        return None


# 尝试导入
btree_pyx = _try_import('btree_pyx')
row_codec = _try_import('row_codec')
page_pyx = _try_import('page_pyx')
predicate_pyx = _try_import('predicate_pyx')
buffer_pool_pyx = _try_import('buffer_pool_pyx')
hash_table_pyx = _try_import('hash_table_pyx')


AVAILABLE = {
    'btree': btree_pyx is not None,
    'row_codec': row_codec is not None,
    'page': page_pyx is not None,
    'predicate': predicate_pyx is not None,
    'buffer_pool': buffer_pool_pyx is not None,
    'hash_table': hash_table_pyx is not None,
}


def status():
    """返回可用状态"""
    return {
        'available': dict(AVAILABLE),
        'errors': dict(_IMPORT_ERRORS),
        'all_available': all(AVAILABLE.values()),
    }


def print_status():
    print("=" * 60)
    print("  Cython 加速模块状态")
    print("=" * 60)
    for name, ok in AVAILABLE.items():
        icon = '✅' if ok else '❌'
        print(f"  {icon} {name}")
    if _IMPORT_ERRORS:
        print("\n  错误:")
        for name, err in _IMPORT_ERRORS.items():
            print(f"    {name}: {err}")
    if all(AVAILABLE.values()):
        print("\n  🚀 全部加速模块已启用")
    print("=" * 60)