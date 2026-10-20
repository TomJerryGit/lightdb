"""用 Zig 自带的 C 编译器构建 LightDB 的 Cython 扩展（无需 MSVC/Visual Studio）。

背景与定位
==========
本机（以及很多精简 Windows 环境）没有 MSVC Build Tools，但有 Python + 网络时
可以用 ``pip install cython ziglang`` 得到：Cython（把 .pyx 转成 C）+ Zig
（自带 C 编译器，``zig cc``）。本脚本用它们构建 6 个扩展模块：

    btree_pyx / row_codec / page_pyx / predicate_pyx /
    buffer_pool_pyx / hash_table_pyx

产物是 ``lightdb/cython/*.pyd``（Windows）或 ``*.so``（Linux/macOS），
**不提交进仓库**（.gitignore 已忽略），只用于本机验证/加速。

⚠️ ABI 注意（必须知道）
---------------------
Windows 上 Python 官方发行版是 MSVC ABI，而 Zig 的 ``x86_64-windows-gnu``
目标是 MinGW ABI。链接 Python 的导入库通常能加载、功能测试也能通过，但这
**不是**官方支持的组合；本脚本构建产物只建议用于开发验证/基准，不建议发布。
生产/发布请用 MSVC Build Tools 走 ``lightdb/cython/setup.py build_ext``。

用法
====
    # 前置（本机已把工具装在 D 盘 venv，可任选）:
    #   python -m venv --system-site-packages D:\\lightdb_tools\\cython-venv
    #   D:\\lightdb_tools\\cython-venv\\Scripts\\python -m pip install cython ziglang
    #
    # 构建（用带 Cython 的解释器运行）：
    D:\\lightdb_tools\\cython-venv\\Scripts\\python scripts/build_cython_zig.py
    # 只检查前置条件：
    python scripts/build_cython_zig.py --check
    # 从零重新构建并清理中间 .c：
    python scripts/build_cython_zig.py --clean

退出码：0 = 成功（或 --check 前置齐备）；1 = 失败/前置缺失。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / 'lightdb' / 'cython'
DEFAULT_BUILD_DIR = ROOT / '_analysis_tmp' / 'cython_build'

#: (Extension 名, 源文件名, 输出模块名)
MODULES = [
    ('lightdb.cython.btree_pyx', 'btree_pyx.pyx', 'btree_pyx'),
    ('lightdb.cython.row_codec', 'row_codec.pyx', 'row_codec'),
    ('lightdb.cython.page_pyx', 'page_pyx.pyx', 'page_pyx'),
    ('lightdb.cython.predicate_pyx', 'predicate_pyx.pyx', 'predicate_pyx'),
    ('lightdb.cython.buffer_pool_pyx', 'buffer_pool_pyx.pyx',
     'buffer_pool_pyx'),
    ('lightdb.cython.hash_table_pyx', 'hash_table_pyx.pyx',
     'hash_table_pyx'),
]

COMPILER_DIRECTIVES_CLI = ['-3', '--directive', 'boundscheck=False',
                           '--directive', 'wraparound=False']


def find_zig(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    env = os.environ.get('ZIG_EXE')
    if env and Path(env).exists():
        return Path(env)
    try:                                     # pip install ziglang
        import ziglang                       # noqa: F401
        cand = Path(ziglang.__file__).parent / 'zig.exe'
        if cand.exists():
            return cand
        cand = Path(ziglang.__file__).parent / 'zig'
        if cand.exists():
            return cand
    except ImportError:
        pass
    which = shutil.which('zig')
    return Path(which) if which else None


def python_include_lib():
    """返回 (include_dir, libs_dir, python_lib_name)。"""
    base = Path(sys.base_prefix)
    include = Path(sysconfig.get_paths()['include'])
    if not include.exists():
        include = base / 'include'
    libs = base / 'libs'
    lib = 'python%d%d' % sys.version_info[:2]
    return include, libs, lib


def check_prereqs(zig: Path | None) -> list[str]:
    problems = []
    try:
        import Cython  # noqa: F401
        print(f'[OK] Cython {Cython.__version__} ({sys.executable})')
    except ImportError:
        problems.append("当前解释器没有 Cython：pip install 'cython>=3.0'"
                        "（或用带 Cython 的解释器运行本脚本）")
    if zig is None:
        problems.append('找不到 zig：pip install ziglang，或设置 ZIG_EXE')
    else:
        out = subprocess.run([str(zig), 'version'], capture_output=True,
                             text=True)
        if out.returncode != 0:
            problems.append(f'zig 不可用: {zig}')
        else:
            print(f'[OK] zig {out.stdout.strip()} ({zig})')
    include, libs, lib = python_include_lib()
    if not include.exists():
        problems.append(f'找不到 Python include 目录: {include}')
    if sys.platform == 'win32' and not libs.exists():
        problems.append(f'找不到 Python libs 目录: {libs}')
    print(f'[i] Python {sys.version.split()[0]} include={include} '
          f'libs={libs} lib={lib}')
    return problems


def zig_env(build_dir: Path) -> dict:
    env = dict(os.environ)
    env['ZIG_GLOBAL_CACHE_DIR'] = str(build_dir / 'zigcache' / 'global')
    env['ZIG_LOCAL_CACHE_DIR'] = str(build_dir / 'zigcache' / 'local')
    return env


def run(cmd, env=None, cwd=None):
    p = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True,
                       errors='replace')
    if p.returncode != 0:
        print('$ ' + ' '.join(str(c) for c in cmd))
        print(p.stdout[-4000:])
        print(p.stderr[-8000:])
        raise RuntimeError(f'命令失败（exit {p.returncode}）')
    return p


def build(build_dir: Path, out_dir: Path, zig: Path, clean: bool) -> int:
    build_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    include, libs, lib = python_include_lib()
    env = zig_env(build_dir)
    built = []
    for _fullname, pyx_name, out_name in MODULES:
        pyx = PKG_DIR / pyx_name
        c_file = build_dir / (pyx.stem + '.c')
        if clean and c_file.exists():
            c_file.unlink()
        print(f'[1/2] Cythonize {pyx_name} -> {c_file.name}')
        run([sys.executable, '-m', 'cython', str(pyx), '-o', str(c_file),
             *COMPILER_DIRECTIVES_CLI])
        if sys.platform == 'win32':
            out_file = out_dir / (out_name + '.pyd')
            cmd = [str(zig), 'cc', '-target', 'x86_64-windows-gnu',
                   '-D_WIN64', '-DMS_WIN64', '-shared', '-O2',
                   f'-I{include}', f'-L{libs}',
                   str(c_file), f'-l{lib}', '-o', str(out_file)]
        else:
            suffix = '.so' if sys.platform != 'darwin' else '.so'
            out_file = out_dir / (out_name + suffix)
            cmd = [str(zig), 'cc', '-shared', '-fPIC', '-O2',
                   f'-I{include}', str(c_file), '-o', str(out_file)]
        print(f'[2/2] Compile {out_name} <- zig cc')
        run(cmd, env=env)
        built.append(out_file)
        print(f'      -> {out_file} ({out_file.stat().st_size} bytes)')
    print(f'\n[OK] 构建完成 {len(built)} 个模块：{out_dir}')
    print('[i] 验证: python -c "from lightdb.cython import status,print_status;'
          ' print_status()"')
    print('[i] 注意: Windows 上 Zig/MinGW ABI 产物仅建议用于开发验证/基准；'
          '发布请用 MSVC。')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Build LightDB Cython extensions '
                                             'with Cython + Zig (no MSVC).')
    ap.add_argument('--zig', default=None, help='zig 可执行文件路径')
    ap.add_argument('--build-dir', default=str(DEFAULT_BUILD_DIR))
    ap.add_argument('--out-dir', default=str(PKG_DIR))
    ap.add_argument('--check', action='store_true', help='只检查前置条件')
    ap.add_argument('--clean', action='store_true', help='删除中间 .c 后重建')
    ap.add_argument('--jobs', type=int, default=4, help='保留参数（当前串行）')
    args = ap.parse_args(argv)

    zig = find_zig(args.zig)
    problems = check_prereqs(zig)
    if args.check:
        if problems:
            print('\n[FAIL] 前置条件不满足:')
            for p in problems:
                print('  -', p)
            return 1
        print('\n[PASS] 前置条件齐备（可运行本脚本构建）')
        return 0
    if problems:
        print('\n[FAIL] 前置条件不满足:')
        for p in problems:
            print('  -', p)
        return 1
    try:
        return build(Path(args.build_dir), Path(args.out_dir), zig, args.clean)
    except Exception as e:                   # noqa: BLE001
        print(f'\n[FAIL] 构建失败: {type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
