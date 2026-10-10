"""
Cython 构建脚本
"""
import os
import sys
from pathlib import Path
from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False
    print("⚠️ Cython 未安装，将使用 .c 预编译文件")
    print("   安装: pip install cython")
    cythonize = None


HERE = Path(__file__).parent


# Cython 编译指令（性能优先）
COMPILER_DIRECTIVES = {
    'language_level': 3,
    'boundscheck': False,       # 关闭边界检查
    'wraparound': False,        # 关闭负索引
    'cdivision': True,          # C 除法
    'initializedcheck': False,  # 关闭初始化检查
    'nonecheck': False,         # 关闭 None 检查
    'embedsignature': True,     # 保留函数签名
    'profile': False,           # 关闭 profiling
}


# 编译参数
EXTRA_COMPILE_ARGS = []
EXTRA_LINK_ARGS = []

if sys.platform == 'win32':
    EXTRA_COMPILE_ARGS = ['/O2', '/arch:AVX2']
elif sys.platform == 'darwin':
    EXTRA_COMPILE_ARGS = ['-O3', '-march=native', '-flto']
else:  # Linux
    EXTRA_COMPILE_ARGS = [
        '-O3', '-march=native', '-flto',
        '-fno-semantic-interposition',
        '-fno-trapping-math', '-fno-math-errno',
    ]


# 定义扩展
extensions = [
    Extension(
        'lightdb.cython.btree_pyx',
        sources=['btree_pyx.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
        extra_link_args=EXTRA_LINK_ARGS,
    ),
    Extension(
        'lightdb.cython.row_codec',
        sources=['row_codec.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
    ),
    Extension(
        'lightdb.cython.page_pyx',
        sources=['page_pyx.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
    ),
    Extension(
        'lightdb.cython.predicate_pyx',
        sources=['predicate_pyx.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
    ),
    Extension(
        'lightdb.cython.buffer_pool_pyx',
        sources=['buffer_pool_pyx.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
    ),
    Extension(
        'lightdb.cython.hash_table_pyx',
        sources=['hash_table_pyx.pyx'],
        extra_compile_args=EXTRA_COMPILE_ARGS,
    ),
]


if __name__ == '__main__':
    if HAS_CYTHON:
        exts = cythonize(
            extensions,
            compiler_directives=COMPILER_DIRECTIVES,
            annotate=False,
            nthreads=4,
        )
    else:
        # 用已有的 .c 文件
        exts = []
        for ext in extensions:
            c_file = ext.sources[0].replace('.pyx', '.c')
            if os.path.exists(c_file):
                ext.sources = [c_file]
                exts.append(ext)

    setup(
        name='lightdb-cython',
        version='4.0.0',
        packages=['lightdb', 'lightdb.cython'],
        package_dir={
            'lightdb': '..',
            'lightdb.cython': '.',
        },
        ext_modules=exts,
        zip_safe=False,
    )