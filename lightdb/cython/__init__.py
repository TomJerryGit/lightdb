"""
LightDB Cython 加速层加载器
===========================

真实状态（请勿误读）
--------------------
* 本目录下有 6 个 ``.pyx`` 源码，但**本机没有 Cython、没有任何 C 编译器**
  （``import Cython`` → ModuleNotFoundError；``gcc``/``cl``/``clang`` 均未找到），
  因此 **6 个模块从未编译过，本仓库不存在任何 .pyd/.so/.c 产物**，
  也**没有**任何实测加速比。
* 旧版本这里用 ``__import__`` 逐个尝试导入编译模块，失败就把模块名置为
  ``None``，包内**没有任何纯 Python 回退**。而 ``tests/test_v4_batch3.py``
  （约第 34 行）与 ``tests/regression_v4/test_cython_compat.py`` 用
  ``if mod is None: return`` 提前返回，于是"全绿"其实什么都没测 ——
  文档宣称的"Cython 模块可选（自动回退）"当时是**假的**。
* 现在：编译模块缺失时，模块名绑定到 ``lightdb/cython/fallback/`` 里的
  **纯 Python 等价实现**（薄包装既有 ``lightdb/storage/*``、``lightdb/sql/*``
  代码），因此 ``btree_pyx`` 等**永远不会是 None**，调用方会真正执行到实现。
  回退带来的是**正确性/可用性**，不是性能 —— 纯 Python 不会比既有 Python 快。

回退方案说明（为何用 ``fallback/`` 子包而不是 ``*_pyx.py``）
------------------------------------------------------------
编译产物落盘名是 ``btree_pyx.pyd`` 等，若回退文件取名 ``btree_pyx_py.py``
仍需一层映射；而 ``fallback/btree.py`` 与 .pyx 同名同构，导入路径一眼可对：
``lightdb.cython.btree_pyx``（.pyx 目标名）↔ ``lightdb.cython.fallback.btree``。
调用方**不需要**改代码，仍写 ``from lightdb.cython import btree_pyx``。

未在本机验证（需要什么条件才能验证）
------------------------------------
需要 ``pip install cython`` + 一个 C 编译器（Windows: MSVC Build Tools /
``cl.exe``；Linux: gcc），然后 ``python lightdb/cython/setup.py build_ext
--inplace`` 或 ``bash scripts/build_cython.sh``。在那之前，回退与 .pyx 的
等价性只是**按源码逐行静态比对**的结论，编译错误是否真的修完也未经编译器
确认。

用法::

    from lightdb.cython import status, print_status
    print_status()          # 人类可读
    status()                # 机器可读 dict

⚠️ 关于 ``use_cython`` 这个字段名
---------------------------------
``lightdb/storage/row_format_v4.py`` 与 ``lightdb/sql/predicate_v4.py`` 里有
``self.use_cython = _cy is not None``，而这些文件本轮**不允许改动**。
由于现在 ``_cy`` 一定不是 None，这两个类的 ``use_cython`` 会显示 ``True``，
**但这不代表跑的是编译产物** —— 本机跑的是 ``fallback/`` 里的纯 Python，
所以 ``tests/test_v4_batch3.py`` 里打印的 "使用 Cython: True" 应读作
"使用 lightdb.cython 提供的实现"。要判断是否真的有加速，请以本模块
``status()['any_accelerated']`` / ``status()['compiled']`` 为准。
"""

import importlib

# 逻辑名 → (编译模块名, 回退模块名)
_MODULES = {
    'btree': ('btree_pyx', 'btree'),
    'row_codec': ('row_codec', 'row_codec'),
    'page': ('page_pyx', 'page'),
    'predicate': ('predicate_pyx', 'predicate'),
    'buffer_pool': ('buffer_pool_pyx', 'buffer_pool'),
    'hash_table': ('hash_table_pyx', 'hash_table'),
}

SOURCES = ('compiled', 'python-fallback')

AVAILABLE = {}          # {逻辑名: bool}
MODULE_SOURCES = {}     # {逻辑名: dict}
_IMPORT_ERRORS = {}     # {编译模块名: 导入失败原因}
_FALLBACK_ERRORS = {}   # {逻辑名: 回退也失败的原因}


def _try_import(name):
    """尝试导入编译模块，失败返回 (None, 错误串)"""
    try:
        return importlib.import_module(f'lightdb.cython.{name}'), None
    except Exception as e:          # 缺模块 / 二进制不兼容（如 PYD 版本错）
        return None, f'{type(e).__name__}: {e}'


def _load(logical, compiled_name, fallback_name):
    """编译版优先；缺失则用纯 Python 回退；两者都失败才返回 None"""
    mod, err = _try_import(compiled_name)
    if mod is not None:
        return mod, {
            'source': 'compiled',
            'module': f'lightdb.cython.{compiled_name}',
            'accelerated': True,
        }

    _IMPORT_ERRORS[compiled_name] = err
    try:
        mod = importlib.import_module(f'lightdb.cython.fallback.{fallback_name}')
    except Exception as e:
        _FALLBACK_ERRORS[logical] = f'{type(e).__name__}: {e}'
        return None, {
            'source': 'unavailable',
            'module': None,
            'accelerated': False,
            'compile_error': err,
            'fallback_error': _FALLBACK_ERRORS[logical],
        }

    return mod, {
        'source': 'python-fallback',
        'module': f'lightdb.cython.fallback.{fallback_name}',
        'accelerated': False,        # 纯 Python，明确不是加速
        'compile_error': err,
    }


btree_pyx, _src_btree = _load('btree', *_MODULES['btree'])
row_codec, _src_row_codec = _load('row_codec', *_MODULES['row_codec'])
page_pyx, _src_page = _load('page', *_MODULES['page'])
predicate_pyx, _src_predicate = _load('predicate', *_MODULES['predicate'])
buffer_pool_pyx, _src_buffer_pool = _load('buffer_pool', *_MODULES['buffer_pool'])
hash_table_pyx, _src_hash_table = _load('hash_table', *_MODULES['hash_table'])

MODULE_SOURCES = {
    'btree': _src_btree,
    'row_codec': _src_row_codec,
    'page': _src_page,
    'predicate': _src_predicate,
    'buffer_pool': _src_buffer_pool,
    'hash_table': _src_hash_table,
}

# 逻辑名 → 模块对象（调用方可以 dep = MODULES['btree']）
MODULES = {
    'btree': btree_pyx,
    'row_codec': row_codec,
    'page': page_pyx,
    'predicate': predicate_pyx,
    'buffer_pool': buffer_pool_pyx,
    'hash_table': hash_table_pyx,
}

AVAILABLE = {name: mod is not None for name, mod in MODULES.items()}

CYTHON_INSTALLED = False
try:                                # 仅用于如实说明"为何没有编译产物"
    import Cython as _Cython        # noqa: F401
    CYTHON_INSTALLED = True
    CYTHON_VERSION = getattr(_Cython, '__version__', 'unknown')
except ImportError:
    CYTHON_VERSION = None


def status():
    """
    返回可用状态。

    ``available`` 的值是来源字符串（``'compiled'`` / ``'python-fallback'``，
    两者都真值），旧调用方 ``if st['available'][name]:`` 不受影响；
    需要精确判断时看 ``sources`` 或 ``accelerated``。
    """
    available = {name: MODULE_SOURCES[name]['source']
                 for name in _MODULES}
    return {
        'available': available,
        'sources': {name: dict(MODULE_SOURCES[name]) for name in _MODULES},
        'compiled': sorted(
            n for n in _MODULES if MODULE_SOURCES[n]['source'] == 'compiled'),
        'fallback': sorted(
            n for n in _MODULES
            if MODULE_SOURCES[n]['source'] == 'python-fallback'),
        'accelerated': {name: MODULE_SOURCES[name]['accelerated']
                        for name in _MODULES},
        'errors': dict(_IMPORT_ERRORS),       # 编译模块导入失败原因
        'fallback_errors': dict(_FALLBACK_ERRORS),
        'all_available': all(AVAILABLE.values()),
        'any_accelerated': any(MODULE_SOURCES[n]['accelerated']
                               for n in _MODULES),
        'cython_installed': CYTHON_INSTALLED,
        'cython_version': CYTHON_VERSION,
        'compiled_verified': False,   # 本机无条件编译，恒为 False
        'note': ('全部模块均可调用；未编译的走纯 Python 回退（无加速）。'
                 '编译未在本机验证。'),
    }


def print_status():
    """人类可读状态"""
    st = status()
    print("=" * 62)
    print("  Cython 加速模块状态")
    print("=" * 62)
    for name in _MODULES:
        src = st['available'][name]
        if src == 'compiled':
            icon = '✅'
        elif src == 'python-fallback':
            icon = '🐍'
        else:
            icon = '❌'
        print(f"  {icon} {name:<12} {src}")
    print(f"\n  Cython 已安装: {st['cython_installed']}"
          f"{' (' + str(st['cython_version']) + ')' if st['cython_version'] else ''}")
    print(f"  编译产物已在本机验证: {st['compiled_verified']}")
    if st['fallback']:
        print(f"\n  🐍 纯 Python 回退: {', '.join(st['fallback'])}")
        print("     （可调用、但**没有**加速；编译需 Cython + C 编译器）")
    if st['errors']:
        print("\n  编译模块导入失败原因:")
        for name, err in st['errors'].items():
            print(f"    {name}: {err}")
    if st['fallback_errors']:
        print("\n  回退模块失败:")
        for name, err in st['fallback_errors'].items():
            print(f"    {name}: {err}")
    if st['all_available']:
        print(f"\n  ✅ 全部 {len(_MODULES)} 个模块可用"
              f"（其中 {len(st['compiled'])} 个为编译版）")
    print("=" * 62)
