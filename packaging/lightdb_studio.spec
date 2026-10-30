# -*- mode: python ; coding: utf-8 -*-
"""LightDB Studio PyInstaller spec（onedir 默认，onefile 由环境变量切换）。

构建方式（不要直接 pyinstaller 本文件，用脚本统一参数/校验/冒烟）：

    python scripts/build_studio.py              # onedir -> dist/studio/LightDB-Studio/
    python scripts/build_studio.py --onefile    # 单文件 -> dist/studio-onefile/LightDB-Studio.exe

环境变量：
    LIGHTDB_STUDIO_ONEFILE=1   → 单文件模式（脚本会设置）

task-87：打包版内置 paramiko/cryptography/bcrypt/nacl（SSH 隧道可选依赖），
连接对话框的 SSH 状态在打包产物中应显示“paramiko 可用”，不再提示未安装。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent      # SPECPATH = packaging/ 目录
ONEFILE = os.environ.get('LIGHTDB_STUDIO_ONEFILE', '0').lower() in (
    '1', 'true', 'yes', 'on')
# 单文件模式的解压目录（默认系统 TEMP）。某些受限环境（沙箱/终端安全软件）会阻止
# 写入 TEMP，导致 bootloader 卡在解压阶段；此时用 LIGHTDB_STUDIO_ONEFILE_TMPDIR
# 指定一个可写目录（build_studio.py --onefile-tmpdir 会设置该变量）。
ONEFILE_TMPDIR = os.environ.get('LIGHTDB_STUDIO_ONEFILE_TMPDIR', '').strip() or None
NAME = 'LightDB-Studio'

# 运行期数据文件：主题/QSS/JSON（目前仓库中没有图标资源，保持为空也不报错）
datas = []
for pattern in ('*.qss', '*.json', '*.png', '*.ico', '*.svg'):
    for p in (ROOT / 'lightdb_studio').rglob(pattern):
        if '\\__pycache__\\' in str(p):
            continue
        datas.append((str(p), str(p.parent.relative_to(ROOT / 'lightdb_studio'))))

# 仓库根 plugins/（示例插件）打到 _MEIPASS/plugins，主窗口按 "包目录/../plugins" 能找到
plugins_dir = ROOT / 'plugins'
if plugins_dir.is_dir():
    datas.append((str(plugins_dir), 'plugins'))

hiddenimports = (
    collect_submodules('lightdb_studio')
    + collect_submodules('lightdb_studio.core')
    # task-87：打包版 SSH 隧道依赖（可选依赖 paramiko 及其密码学后端）
    + collect_submodules('paramiko')
    + collect_submodules('cryptography')
    + collect_submodules('bcrypt')
    + collect_submodules('nacl')
    + ['pymysql', 'PIL', 'PIL.Image', 'PIL.ImageQt',
       'paramiko', 'cryptography']
)

a = Analysis(
    [str(ROOT / 'packaging' / 'studio_entry.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tests', 'benchmarks', 'scripts', 'web', 'docs', 'pytest',
              'Cython'],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        runtime_tmpdir=ONEFILE_TMPDIR,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=NAME,
    )
