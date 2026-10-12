"""
LightDB Cython 构建脚本
=======================

真实状态
--------
本机（Windows, D:\\lightdb_project）**没有安装 Cython**，也**没有任何 C
编译器**（gcc / cl / clang 均未找到），因此本脚本在这里**无法编译出任何
产物**，请勿据此宣称"已加速"。

旧版本的两个问题（本轮已修）:
  1. 找不到 Cython 且找不到 .c 时，``exts = []`` 然后照常调用 ``setup()`` ——
     一声不吭地什么都不编译，使用者以为编译成功了。现在会**明确打印告警
     与原因**并（在 build_ext 请求时）**非零退出**。
  2. Windows 下硬编码 ``/arch:AVX2``，非 MSVC 工具链或非 AVX2 CPU 会直接
     编译失败。现在改为可配置：
       * 默认**不加** ``/arch:``（保持最大可移植性）；
       * 只有确认 MSVC（``cl.exe``）且显式允许时才加（环境变量
         ``LIGHTDB_CYTHON_AVX2=1``，或 ``--with-avx2``）。
     用法::

        python setup.py build_ext --inplace                 # 推荐，默认保守
        LIGHTDB_CYTHON_AVX2=1 python setup.py build_ext --inplace   # MSVC+AVX2
        LIGHTDB_CYTHON_AVX2=0 python setup.py build_ext --inplace   # 强制关闭
        LIGHTDB_CYTHON_NATIVE=1 python setup.py build_ext --inplace # -march=native

  另外支持 ``--check`` 只做环境体检（不编译），退出码即结论：
      0 = 具备编译条件；1 = 缺 Cython / 缺编译器。
"""
import os
import shutil
import sys
from pathlib import Path
from setuptools import setup, Extension

HERE = Path(__file__).parent

# ==================== 环境探测 ====================

try:
    from Cython.Build import cythonize
    HAS_CYTHON = True
    import Cython as _Cython
    CYTHON_VERSION = getattr(_Cython, '__version__', 'unknown')
except ImportError:
    HAS_CYTHON = False
    CYTHON_VERSION = None
    cythonize = None


def _find_compiler():
    """返回 (名字, 路径) 或 (None, None)"""
    candidates = ['cl', 'gcc', 'clang', 'cc']
    if sys.platform == 'win32':
        candidates = ['cl', 'gcc', 'clang']
    for name in candidates:
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def _is_msvc(compiler_name):
    """cl.exe 才是 MSVC；clang/gcc 不认 /arch: 语法"""
    return compiler_name == 'cl'


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


COMPILER_NAME, COMPILER_PATH = _find_compiler()
IS_MSVC = _is_msvc(COMPILER_NAME)

BUILD_READY = HAS_CYTHON and COMPILER_NAME is not None


def report_environment(stream=sys.stderr):
    """把"为什么没有编译产物"说清楚，而不是静默跳过"""
    print("=" * 66, file=stream)
    print("  LightDB Cython 构建环境体检", file=stream)
    print("=" * 66, file=stream)
    print(f"  Python        : {sys.version.split()[0]} ({sys.platform})", file=stream)
    if HAS_CYTHON:
        print(f"  Cython        : ✅ {CYTHON_VERSION}", file=stream)
    else:
        print("  Cython        : ❌ 未安装 → 无法把 .pyx 转成 C/C++", file=stream)
        print("                  修复: pip install 'cython>=3.0.0'", file=stream)
    if COMPILER_NAME:
        print(f"  C 编译器      : ✅ {COMPILER_NAME} ({COMPILER_PATH})", file=stream)
    else:
        print("  C 编译器      : ❌ 未找到（gcc / cl / clang / cc 均不可用）",
              file=stream)
        print("                  修复: Windows 装 MSVC Build Tools（cl.exe），"
              "Linux 装 build-essential", file=stream)
    print(f"  可编译        : {'✅ 是' if BUILD_READY else '❌ 否'}", file=stream)
    if not BUILD_READY:
        print("\n  ⚠️ 无法编译：本目录下的 6 个 .pyx 保持未编译状态。", file=stream)
        print("     这不是失败的回退 —— lightdb/cython/fallback/ 会提供纯 Python", file=stream)
        print("     等价实现（可调用，但**没有**加速）。", file=stream)
    print("=" * 66, file=stream)
    return BUILD_READY


# ==================== Cython 编译指令 ====================

COMPILER_DIRECTIVES = {
    'language_level': 3,
    'boundscheck': False,
    'wraparound': False,
    'cdivision': True,
    'initializedcheck': False,
    'nonecheck': False,
    'embedsignature': True,
    'profile': False,
}


# ==================== 编译参数（不再硬编码 /arch:AVX2）====================

EXTRA_COMPILE_ARGS = []
EXTRA_LINK_ARGS = []
ARCH_NOTES = []

WITH_AVX2 = _env_flag('LIGHTDB_CYTHON_AVX2', False)
WITH_NATIVE = _env_flag('LIGHTDB_CYTHON_NATIVE', False)

if sys.platform == 'win32':
    EXTRA_COMPILE_ARGS = ['/O2']
    if WITH_AVX2:
        if IS_MSVC:
            EXTRA_COMPILE_ARGS.append('/arch:AVX2')
            ARCH_NOTES.append('/arch:AVX2 已启用（MSVC + 显式允许）')
        else:
            ARCH_NOTES.append(
                f"请求了 AVX2 但编译器是 {COMPILER_NAME or '未知'}（非 MSVC），"
                "已忽略 /arch:AVX2 以免编译失败")
    else:
        ARCH_NOTES.append(
            "未加 /arch:AVX2（默认保守；需要时设 LIGHTDB_CYTHON_AVX2=1，"
            "且仅 MSVC 生效）")
elif sys.platform == 'darwin':
    EXTRA_COMPILE_ARGS = ['-O3']
    if WITH_NATIVE:
        EXTRA_COMPILE_ARGS.append('-march=native')
        ARCH_NOTES.append('-march=native 已启用')
    else:
        ARCH_NOTES.append('未加 -march=native（默认保守，可用 '
                          'LIGHTDB_CYTHON_NATIVE=1 启用）')
else:  # Linux
    EXTRA_COMPILE_ARGS = ['-O3', '-fno-semantic-interposition']
    if WITH_NATIVE:
        EXTRA_COMPILE_ARGS.append('-march=native')
        ARCH_NOTES.append('-march=native 已启用')
    else:
        ARCH_NOTES.append('未加 -march=native（默认保守，可用 '
                          'LIGHTDB_CYTHON_NATIVE=1 启用）')


# ==================== 扩展定义 ====================

_EXT_SPECS = [
    ('lightdb.cython.btree_pyx', 'btree_pyx.pyx'),
    ('lightdb.cython.row_codec', 'row_codec.pyx'),
    ('lightdb.cython.page_pyx', 'page_pyx.pyx'),
    ('lightdb.cython.predicate_pyx', 'predicate_pyx.pyx'),
    ('lightdb.cython.buffer_pool_pyx', 'buffer_pool_pyx.pyx'),
    ('lightdb.cython.hash_table_pyx', 'hash_table_pyx.pyx'),
]


def build_extensions():
    """
    返回 (extensions, 说明列表)。

    找不到 Cython 时**不再**静默给空列表：要么用同目录里已存在的预生成
    .c（说明来源），要么明确报告"缺源码"。
    """
    extensions = [
        Extension(
            name,
            sources=[src],
            extra_compile_args=list(EXTRA_COMPILE_ARGS),
            extra_link_args=list(EXTRA_LINK_ARGS),
        )
        for name, src in _EXT_SPECS
    ]

    notes = []

    if HAS_CYTHON:
        notes.append("用 Cython 从 .pyx 生成 C 源")
        return cythonize(
            extensions,
            compiler_directives=COMPILER_DIRECTIVES,
            annotate=False,
            nthreads=int(os.environ.get('LIGHTDB_CYTHON_THREADS', '4')),
        ), notes

    # 没有 Cython：退而求其次找预生成的 .c
    notes.append("⚠️ 未安装 Cython，改为寻找预生成的 .c 文件")
    exts, missing = [], []
    for ext in extensions:
        pyx = HERE / ext.sources[0]
        c_file = pyx.with_suffix('.c')
        if c_file.exists():
            ext.sources = [str(c_file)]
            exts.append(ext)
            notes.append(f"  命中: {c_file.name}")
        else:
            missing.append(pyx.name)

    if missing:
        notes.append(f"  缺失（既无 .pyx 编译能力，也无预生成 .c）: "
                     f"{', '.join(missing)}")
    if not exts:
        notes.append("❌ 没有任何可编译的源码 —— 不会产生任何编译产物")
    return exts, notes


if __name__ == '__main__':
    check_only = '--check' in sys.argv
    if check_only:
        sys.argv.remove('--check')

    report_environment()
    for note in ARCH_NOTES:
        print(f"  [arch] {note}", file=sys.stderr)

    exts, notes = build_extensions()
    for note in notes:
        print(f"  [src ] {note}", file=sys.stderr)

    if check_only:
        sys.exit(0 if BUILD_READY else 1)

    if not BUILD_READY:
        # 不再假装成功：明确告警 + 非零退出
        print("\n" + "!" * 66, file=sys.stderr)
        print("  构建中止：缺少编译条件，未生成任何 .pyd/.so。", file=sys.stderr)
        missing_what = []
        if not HAS_CYTHON:
            missing_what.append("Cython (pip install 'cython>=3.0.0')")
        if COMPILER_NAME is None:
            missing_what.append("C 编译器 (MSVC cl.exe / gcc / clang)")
        print(f"  缺: {'; '.join(missing_what)}", file=sys.stderr)
        print("  运行时不受影响：lightdb.cython 会自动使用纯 Python 回退。",
              file=sys.stderr)
        print("!" * 66, file=sys.stderr)
        sys.exit(1)

    if not exts:
        print("\n" + "!" * 66, file=sys.stderr)
        print("  构建中止：没有任何可编译的扩展源码（未生成任何产物）。",
              file=sys.stderr)
        print("!" * 66, file=sys.stderr)
        sys.exit(1)

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
