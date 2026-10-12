"""
LightDB Cython 加速层 —— 纯 Python 回退实现
================================================

真实状态（请勿误读）
--------------------
本包**不是**编译器产物，也**没有**任何性能加速。它是在编译模块
（``btree_pyx`` / ``row_codec`` / ``page_pyx`` / ``predicate_pyx`` /
``buffer_pool_pyx`` / ``hash_table_pyx`` 的 .pyd/.so）缺失时，为
``lightdb.cython`` 提供**可调用**的等价 API 的兜底实现。

之所以需要它：``lightdb/cython/__init__.py`` 旧版本在导入失败时把模块名
置为 ``None``，而 ``tests/test_v4_batch3.py``、``tests/regression_v4/
test_cython_compat.py`` 用 ``if mod is None: return`` 提前返回，于是
"全绿"其实什么都没测。现在这些模块名一定会绑定到可调用对象，
调用方 ``if btree_pyx is not None`` 的分支会真正执行。

设计原则：**薄包装既有纯 Python 实现**，不重写算法
---------------------------------------------------
+-------------------+--------------------------------------------------+
| 本回退模块        | 复用的既有实现                                    |
+===================+==================================================+
| ``btree``         | ``lightdb/storage/btree.py`` 的页布局常量 +        |
|                   | ``bisect`` 维护有序键（RawBTreeIndex）             |
| ``page``          | ``lightdb/storage/page.py`` 的 ``Page``            |
| ``row_codec``     | ``lightdb/storage/row_format.py`` 的              |
|                   | ``BinaryRowFormat``                                |
| ``predicate``     | ``lightdb/sql/predicate_v4.py`` 的运算符求值       |
| ``buffer_pool``   | ``collections.OrderedDict``（与                   |
|                   | ``lightdb/storage/buffer_pool.py`` 同一手法）      |
| ``hash_table``    | 本文件下方 ``_ProbeTable``（无既有对应实现）       |
+-------------------+--------------------------------------------------+

字节序一致性：``.pyx`` 里的 ``memcpy`` 走本机字节序（Windows/x86 为小端），
``lightdb/storage/page.py`` 与 ``btree.py`` 也用 ``<`` 小端 struct，
因此回退读到的是同一份页字节布局，可与磁盘页互通。

已知与 .pyx 的语义差异（均为边界情形，已在代码处标注）
------------------------------------------------------
1. ``row_codec``：``.pyx`` 遇到 >65535 字节的字符串时会写出 2 字节的
   65535 标记后**直接截断**（剩余列丢失、长度字段不自洽）；回退走
   ``BinaryRowFormat`` 的 4 字节长度扩展，能正确往返。
2. ``row_codec`` 的 ``T_FLOAT`` 列：``.pyx`` 把值算成 C double 后
   ``memcpy`` 只拷 4 字节，且偏移与 ``T_INT`` 混用同一 4 字节语义；
   回退按 IEEE-754 单精度正常处理。
3. ``row_codec`` 的 ``T_INT`` 列：``.pyx`` 存 4 字节有符号整数，回退
   同样用 ``<i``（与 ``BinaryRowFormat`` 一致，可互通）。
4. ``predicate``：``.pyx`` 的字符串比较走 C ``strcmp``（按 UTF-8 字节序），
   回退按 Python 码点序；两者仅对非 ASCII 字符串的 ``<``/``>`` 有差异。
5. ``predicate``：``.pyx`` 对非空值遇到 ``IS NULL`` 会落到末尾返回 False，
   回退保持一致（即 ``NULL`` 比较不匹配非空行）。

这些都是"未在本机验证编译"的推断结论 —— 本机没有 Cython 也没有 C
编译器，无法用真实编译产物做对照，只能按源码逐行静态比对。

为何这里**故意不**在包级 ``import`` 六个子模块
------------------------------------------------
``lightdb.cython.__init__`` 在自身初始化过程中才会去加载回退模块，而
``fallback.predicate`` → ``lightdb.sql.predicate_v4`` →
``from ..cython import predicate_pyx`` 会绕回尚未绑定 ``predicate_pyx``
的半初始化包，触发 ``ImportError: cannot import name 'predicate_pyx'
from partially initialized module 'lightdb.cython' (most likely due to a
circular import)``。因此包级保持惰性：子模块由
``lightdb.cython.__init__`` 用 ``importlib`` 逐个加载，或按需通过下方
``__getattr__`` 访问（``from lightdb.cython.fallback import btree`` 依然可用）。
"""

import importlib

__all__ = [
    'btree',
    'buffer_pool',
    'hash_table',
    'page',
    'predicate',
    'row_codec',
]


def __getattr__(name):
    """惰性暴露六个子模块（PEP 562）"""
    if name in __all__:
        mod = importlib.import_module(f'lightdb.cython.fallback.{name}')
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
