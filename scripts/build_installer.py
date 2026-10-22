"""构建 LightDB Studio Windows 安装包（Inno Setup 优先，WiX v3 备选）。

用法::

    # 自动检测工具并构建（输入 = scripts/build_studio.py 的 onedir 产物）
    python scripts/build_installer.py

    # 指定版本 / 输入目录 / 输出目录 / 工具
    python scripts/build_installer.py --version 4.0.0 \
        --source dist/studio/LightDB-Studio --out dist/installer --tool inno

    # 构建 + 静默安装/卸载冒烟（本机无安装器工具时显式 skip）
    python scripts/build_installer.py --smoke

设计原则（P6）：
* **优先检测 Inno Setup (`ISCC.exe`)**；没有则检测 **WiX v3**
  (`candle.exe` + `light.exe`)；都没有 → 状态 `skipped`（退出码 3），
  显式打印检测结果与安装指引，并生成**可复现脚本**（`build_installer.ps1`
  + `BUILD_INSTALLER.md`），**绝不伪造安装包产物**。
* 输入必须是 `scripts/build_studio.py` 产出的 onedir
  （默认 `dist/studio/LightDB-Studio/`，内含 `LightDB-Studio.exe`）；
  缺失时显式失败（或 `--build-studio` 先构建）。
* 输出默认 `dist/installer/LightDB-Studio-Setup-<version>.exe`（Inno）/
  `.msi`（WiX），并写 `dist/installer/build_report.json`（版本/工具/命令/
  SHA256/签名状态）。
* 签名不在这里做：无证书时报告 `未签名（SmartScreen 风险）`；有证书用
  `scripts/sign_windows.ps1` 单独签名（本脚本不做假签名）。

退出码：
    0 = 构建成功（或 dry-run 打印计划）
    1 = 构建/冒烟失败
    2 = 参数/版本错误
    3 = 跳过：本机没有可用的安装器构建工具
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                   # 允许直接 `python scripts/xxx.py`
    sys.path.insert(0, str(ROOT))
PACKAGING = ROOT / 'packaging'
INSTALLER_ISS = PACKAGING / 'installer.iss'
INSTALLER_WXS = PACKAGING / 'installer.wxs'
DEFAULT_SOURCE = ROOT / 'dist' / 'studio' / 'LightDB-Studio'
DEFAULT_OUT = ROOT / 'dist' / 'installer'
APP_NAME = 'LightDB Studio'
EXE_NAME = 'LightDB-Studio.exe'
APP_ID = '{8E0D2B6E-6E42-4A7B-9F5B-1B0B4C9B0A61}'
REPORT_SCHEMA = 'lightdb-installer-build/1'

#: Inno Setup / WiX 的常见安装位置（除 PATH 之外再找这些）
INNO_CANDIDATES = (
    r'C:\Program Files (x86)\Inno Setup*\ISCC.exe',
    r'C:\Program Files\Inno Setup*\ISCC.exe',
    r'%LOCALAPPDATA%\Programs\Inno Setup*\ISCC.exe',
)
WIX_CANDIDATES = (
    r'C:\Program Files (x86)\WiX Toolset*\bin\candle.exe',
    r'C:\Program Files\WiX Toolset*\bin\candle.exe',
)
SIGNING_NOTE = ('未签名（无代码签名证书；可运行 scripts/sign_windows.ps1，'
                'SmartScreen 可能告警）')


# ==================== 版本 ====================

def get_version(explicit: str = '') -> str:
    """取安装包版本：`--version` 优先，否则用 lightdb.__version__。

    用产品的 `updater.parse_version` 校验，非法版本抛 ValueError。
    """
    if str(explicit or '').strip():
        version = str(explicit).strip()
    else:
        import lightdb
        version = str(lightdb.__version__).strip()
    try:
        sys.path.insert(0, str(ROOT))
        from lightdb_studio.utils.updater import parse_version
        parse_version(version)                      # 非法会抛 UpdateError
    except Exception as exc:                        # noqa: BLE001
        raise ValueError(f'非法版本号 {version!r}: {exc}') from exc
    return version


# ==================== 工具检测 ====================

def _path_candidates(patterns) -> list:
    found = []
    for pat in patterns:
        found.extend(glob.glob(os.path.expandvars(pat)))
    return sorted(set(found))


def detect_installer_tools(which=None, candidates=None) -> dict:
    """检测 Inno Setup / WiX。返回可 JSON 序列化的检测报告。

    * `which` / `candidates` 可注入（测试用 fake 工具路径），默认用
      `shutil.which` 与常见安装目录 glob。
    * WiX 只支持 **v3**（candle + light）；只检测到 v4 的 `wix.exe` 时
      记为 `available=True, variant='v4', supported=False`，由调用方显式
      说明“工具存在但本脚本按 v3 编写”，不伪造产物。
    """
    which = which or shutil.which
    if candidates is None:
        candidates = {'inno': _path_candidates(INNO_CANDIDATES),
                      'wix': _path_candidates(WIX_CANDIDATES)}
    out = {'inno': {'available': False, 'command': None,
                    'searched': ['ISCC.exe', 'iscc.exe'] + list(candidates.get('inno') or [])},
           'wix': {'available': False, 'variant': None,
                   'candle': None, 'light': None, 'wix': None,
                   'supported': False,
                   'searched': ['candle.exe', 'light.exe', 'wix.exe']
                               + list(candidates.get('wix') or [])}}

    # --- Inno Setup ---
    iscc = which('ISCC.exe') or which('iscc')
    if not iscc:
        for path in candidates.get('inno') or []:
            if Path(path).is_file():
                iscc = path
                break
    if iscc:
        out['inno']['available'] = True
        out['inno']['command'] = str(iscc)

    # --- WiX ---
    candle = which('candle.exe') or which('candle')
    light = which('light.exe') or which('light')
    wix4 = which('wix.exe') or which('wix')
    if not candle:
        for path in candidates.get('wix') or []:
            if Path(path).is_file():
                candle = path
                light = str(Path(path).with_name('light.exe'))
                break
    if candle and light and Path(str(light)).is_file():
        out['wix'].update({'available': True, 'variant': 'v3',
                           'candle': str(candle), 'light': str(light),
                           'supported': True})
    elif wix4:
        out['wix'].update({'available': True, 'variant': 'v4',
                           'wix': str(wix4), 'supported': False})
    return out


def choose_tool(detection: dict, requested: str = 'auto') -> tuple:
    """返回 (tool, reason)：tool ∈ {'inno','wix',None}。"""
    inno_ok = bool(detection.get('inno', {}).get('available'))
    wix = detection.get('wix', {})
    wix_ok = bool(wix.get('available') and wix.get('supported'))
    if requested == 'inno':
        return ('inno', '') if inno_ok else (None, '指定 --tool inno，但未检测到 ISCC.exe')
    if requested == 'wix':
        if wix_ok:
            return 'wix', ''
        if wix.get('available') and not wix.get('supported'):
            return None, ('检测到 WiX v4（wix.exe），但 packaging/installer.wxs 按 '
                          'WiX v3 编写；请安装 WiX v3（candle+light）或改用 Inno Setup')
        return None, '指定 --tool wix，但未检测到 WiX v3（candle.exe + light.exe）'
    # auto：Inno 优先
    if inno_ok:
        return 'inno', ''
    if wix_ok:
        return 'wix', ''
    if wix.get('available') and not wix.get('supported'):
        return None, ('检测到 WiX v4（wix.exe），但 packaging/installer.wxs 按 WiX v3 '
                      '编写；请安装 WiX v3 或 Inno Setup')
    return None, '未检测到 Inno Setup（ISCC.exe）或 WiX v3（candle.exe+light.exe）'


# ==================== 可复现脚本（工具缺失时生成） ====================

def _inno_command(version: str, source: Path, out: Path) -> str:
    return ('ISCC.exe /DAppVersion=%s /DSourceDir="%s" /DOutputDir="%s" '
            '/DOutputBaseFilename="LightDB-Studio-Setup-%s" packaging\\installer.iss'
            % (version, source, out, version))


def _wix_commands(version: str, source: Path, out: Path) -> list:
    obj = out / 'obj' / 'installer.wixobj'
    msi = out / ('LightDB-Studio-Setup-%s.msi' % version)
    return [
        'candle.exe -dAppVersion=%s -dSourceDir="%s" -o "%s" packaging\\installer.wxs'
        % (version, source, obj),
        'light.exe -o "%s" "%s"' % (msi, obj),
    ]


def write_repro_scripts(tool, version: str, source: Path, out: Path,
                        reason: str) -> list:
    """在输出目录生成可复现构建脚本 + 说明（工具缺失时调用）。

    返回生成的文件路径列表。脚本/说明均为“装了工具后照做即可复现”，
    **不包含任何伪造产物**。
    """
    out.mkdir(parents=True, exist_ok=True)
    ps1 = out / 'build_installer.ps1'
    md = out / 'BUILD_INSTALLER.md'
    inno_cmd = _inno_command(version, source, out)
    wix_cmds = _wix_commands(version, source, out)
    expected = out / ('LightDB-Studio-Setup-%s.exe' % version)
    if tool == 'wix':
        expected = out / ('LightDB-Studio-Setup-%s.msi' % version)

    ps1_text = """# Reproducible LightDB Studio installer build (generated by scripts/build_installer.py)
# Reason this file was generated: {reason}
# Install a tool first, then run this script from anywhere (PowerShell).
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\\..')).Path
Set-Location $Repo

# --- Preferred: Inno Setup 6 (winget install --id JRSoftware.InnoSetup -e) ---
# {inno_cmd}

# --- Alternative: WiX Toolset v3 (candle.exe + light.exe) ---
# {wix_cmd1}
# {wix_cmd2}

# --- Or simply let the builder redo detection/build/smoke ---
python "$Repo\\scripts\\build_installer.py" --version '{version}' --smoke
""".format(reason=reason.replace('\n', ' '), inno_cmd=inno_cmd,
           wix_cmd1=wix_cmds[0], wix_cmd2=wix_cmds[1], version=version)
    ps1.write_text(ps1_text, encoding='ascii', errors='replace')

    md_text = """# LightDB Studio 安装包构建说明（自动生成）

- 生成时间：{ts}
- 期望版本：`{version}`
- 输入（onedir 产物）：`{source}`
- 输出目录：`{out}`
- 期望产物：`{expected}`

## 为什么没有安装包？

{reason}

**本机未生成任何安装包文件**（不伪造产物）。要得到安装包，先安装工具再执行
本目录下自动生成的 `build_installer.ps1`，或手动执行：

```
# 方案 A：Inno Setup 6（推荐）
winget install --id JRSoftware.InnoSetup -e
{inno_cmd}

# 方案 B：WiX Toolset v3
{wix_cmd1}
{wix_cmd2}

# 方案 C：交给 builder 重新检测/构建/冒烟
python scripts\\build_installer.py --version {version} --smoke
```

## 冒烟（构建成功后）

```
LightDB-Studio-Setup-{version}.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\\tmp\\LightDBStudio
"C:\\tmp\\LightDBStudio\\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

签名是可选的：无证书时产物为**未签名**（SmartScreen 风险），可用
`scripts/sign_windows.ps1` 签名。
""".format(ts=time.strftime('%Y-%m-%d %H:%M:%S'), version=version,
           source=source, out=out, expected=expected,
           reason=reason, inno_cmd=inno_cmd, wix_cmd1=wix_cmds[0],
           wix_cmd2=wix_cmds[1])
    md.write_text(md_text, encoding='utf-8')
    return [ps1, md]


# ==================== 执行与构建 ====================

def run_command(cmd, cwd=None, timeout=900) -> tuple:
    """执行一条命令，返回 (returncode, 合并后的输出尾部)。"""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd or ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return proc.returncode, (proc.stdout or '')[-8000:]
    except subprocess.TimeoutExpired:
        return 124, '命令超时（>%ss）: %s' % (timeout, cmd)
    except Exception as exc:                        # noqa: BLE001
        return 127, '命令启动失败: %s: %s' % (exc, cmd)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _build_commands(tool: str, version: str, source: Path, out: Path,
                    detection: dict) -> tuple:
    """返回 (commands, expected_artifact)。"""
    if tool == 'inno':
        iscc = detection['inno']['command']
        expected = out / ('LightDB-Studio-Setup-%s.exe' % version)
        cmd = [iscc,
               '/DAppVersion=%s' % version,
               '/DSourceDir=%s' % source,
               '/DOutputDir=%s' % out,
               '/DOutputBaseFilename=%s' % expected.stem,
               str(INSTALLER_ISS)]
        return [cmd], expected
    if tool == 'wix':
        candle = detection['wix']['candle']
        light = detection['wix']['light']
        obj_dir = out / 'obj'
        obj_dir.mkdir(parents=True, exist_ok=True)
        wixobj = obj_dir / 'installer.wixobj'
        expected = out / ('LightDB-Studio-Setup-%s.msi' % version)
        return [
            [candle, '-dAppVersion=%s' % version, '-dSourceDir=%s' % source,
             '-o', str(wixobj), str(INSTALLER_WXS)],
            [light, '-o', str(expected), str(wixobj)],
        ], expected
    raise ValueError('未知 tool: %r' % (tool,))


def _planned_commands(tool: str, version: str, source: Path, out: Path) -> list:
    """dry-run / 报告用的可读命令文本。"""
    if tool == 'inno':
        return [_inno_command(version, source, out)]
    if tool == 'wix':
        return _wix_commands(version, source, out)
    return []


def build_installer(version: str = '', source=None, out=None, tool: str = 'auto',
                    dry_run: bool = False, runner=None, detect=None,
                    build_studio: bool = False) -> dict:
    """构建安装包；返回构建报告 dict（JSON 可序列化）。

    `runner` / `detect` 可注入（单测用 fake 工具/命令）；默认调用真实
    subprocess 与 `detect_installer_tools`。
    """
    report = {
        'schema': REPORT_SCHEMA,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'failed',
        'exit_code': 1,
        'version': None,
        'tool': None,
        'source': None,
        'out_dir': None,
        'artifact': None,
        'artifact_size': 0,
        'artifact_sha256': None,
        'commands_preview': [],
        'detection': None,
        'reason': '',
        'signing': {'status': 'not_signed', 'note': SIGNING_NOTE},
        'repro_scripts': [],
        'smoke': None,
    }
    try:
        version = get_version(version)
    except ValueError as exc:
        report.update({'reason': str(exc), 'exit_code': 2})
        return report
    report['version'] = version

    source = Path(source or DEFAULT_SOURCE)
    out = Path(out or DEFAULT_OUT)
    report['source'] = str(source)
    report['out_dir'] = str(out)

    if not source.is_dir():
        if build_studio:
            sys.path.insert(0, str(ROOT / 'scripts'))
            try:
                import build_studio as bs
                res = bs.build(onefile=False, smoke=False)
            except Exception as exc:                # noqa: BLE001
                report['reason'] = f'onedir 构建失败: {exc}'
                return report
            if not res.get('ok'):
                report['reason'] = ('onedir 构建失败（scripts/build_studio.py）: '
                                    + str(res.get('log_tail', ''))[-2000:])
                return report
            source = Path(res['artifact']).parent
            report['source'] = str(source)
        else:
            report['reason'] = (
                'onedir 产物不存在: %s（先运行 python scripts/build_studio.py，'
                '或加 --build-studio）' % source)
            return report

    out.mkdir(parents=True, exist_ok=True)
    detection = detect() if detect else detect_installer_tools()
    report['detection'] = detection
    chosen, why = choose_tool(detection, tool)
    if chosen is None:
        repro = write_repro_scripts(tool if tool in ('inno', 'wix') else 'auto',
                                    version, source, out, why)
        report.update({'status': 'skipped', 'exit_code': 3, 'reason': why,
                       'repro_scripts': [str(p) for p in repro],
                       'commands_preview': _planned_commands('inno' if
                                                            'Inno' in why else 'wix',
                                                            version, source, out)})
        (out / 'build_report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        return report
    report['tool'] = chosen
    commands, expected = _build_commands(chosen, version, source, out, detection)
    report['commands_preview'] = [' '.join(str(c) for c in cmd) for cmd in commands]
    report['expected_artifact'] = str(expected)

    if dry_run:
        report.update({'status': 'planned', 'exit_code': 0,
                       'reason': '--dry-run：只打印构建命令，未执行'})
        return report

    run = runner or run_command
    outputs = []
    for cmd in commands:
        rc, output = run(cmd, cwd=ROOT)
        outputs.append({'command': ' '.join(str(c) for c in cmd), 'rc': rc,
                        'output_tail': output[-4000:]})
        report['command_outputs'] = outputs
        if rc != 0:
            report.update({'status': 'failed', 'exit_code': 1,
                           'reason': '构建命令失败（rc=%s）' % rc})
            _write_report(out, report)
            return report

    if not expected.exists() or expected.stat().st_size <= 0:
        report.update({'status': 'failed', 'exit_code': 1,
                       'reason': '命令成功但未找到安装包产物: %s（不伪造产物）'
                                 % expected})
        _write_report(out, report)
        return report

    report.update({
        'status': 'ok', 'exit_code': 0,
        'artifact': str(expected),
        'artifact_size': expected.stat().st_size,
        'artifact_sha256': _sha256(expected),
        'reason': '构建成功',
    })
    _write_report(out, report)
    return report


def _write_report(out: Path, report: dict):
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'build_report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
    except OSError as exc:                          # pragma: no cover
        print('[build-installer] 写报告失败: %s' % exc, file=sys.stderr)


# ==================== 静默安装 / 卸载冒烟 ====================

def _default_registry_lookup(install_dir: Path):
    """在 HKCU/HKLM 的 Uninstall 项里找 LightDB Studio 的卸载项。"""
    try:
        import winreg
    except Exception:                                # pragma: no cover
        return None
    uninstall_key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    roots = [(winreg.HKEY_CURRENT_USER, 0),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)]
    hits = []
    for hive, flag in roots:
        try:
            with winreg.OpenKey(hive, uninstall_key, 0,
                                winreg.KEY_READ | flag) as root:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as sub:
                            try:
                                display = winreg.QueryValueEx(
                                    sub, 'DisplayName')[0]
                            except OSError:
                                continue
                            if 'LightDB Studio' in str(display):
                                info = {'key': name, 'display_name': display}
                                for value in ('InstallLocation',
                                              'UninstallString',
                                              'DisplayVersion'):
                                    try:
                                        info[value] = winreg.QueryValueEx(
                                            sub, value)[0]
                                    except OSError:
                                        info[value] = None
                                hits.append(info)
                    except OSError:
                        continue
        except OSError:
            continue
    return hits or None


def _start_menu_shortcut_path() -> Path:
    appdata = os.environ.get('APPDATA') or str(
        Path.home() / 'AppData' / 'Roaming')
    return (Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' /
            'Programs' / APP_NAME / (APP_NAME + '.lnk'))


def smoke_install(installer, tool: str = '', workdir=None, timeout: int = 300,
                  runner=None, registry_lookup=None, check_shortcuts: bool = True,
                  no_icons: bool = False, cleanup: bool = False) -> dict:
    """静默安装到临时目录 → 检查 exe/快捷方式/卸载项 → exe --smoke →
    静默卸载 → 确认清理。返回分步报告（JSON 可序列化）。

    本机没有安装器工具时通常不会调用到这里；单测用 `runner` 注入 fake
    安装/卸载逻辑覆盖各分支。真实环境（非受限沙箱）下才会生成开始菜单
    快捷方式；沙箱里可用 `no_icons=True`（传 `/NOICONS`）并把快捷方式项记
    为 SKIP。
    """
    run = runner or run_command
    lookup = registry_lookup or _default_registry_lookup
    installer = Path(installer)
    result = {'status': 'skipped', 'exit_code': 3, 'steps': [],
              'artifact': str(installer), 'tool': tool or None,
              'install_dir': None, 'reason': ''}

    if not installer.is_file():
        result['reason'] = '安装包不存在: %s' % installer
        return result
    tool = tool or ('inno' if installer.suffix.lower() == '.exe' else 'wix')
    result['tool'] = tool
    workdir = Path(workdir or tempfile.mkdtemp(prefix='lightdb_installer_smoke_'))
    workdir.mkdir(parents=True, exist_ok=True)
    install_dir = workdir / 'installed'
    result['install_dir'] = str(install_dir)

    def step(name, status, detail=''):
        result['steps'].append({'name': name, 'status': status, 'detail': detail})

    if tool == 'inno':
        install_cmd = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                       '/NORESTART', '/SP-', '/DIR=%s' % install_dir]
        if no_icons:
            install_cmd.append('/NOICONS')
        uninstall_exe = install_dir / 'unins000.exe'
    else:
        install_cmd = ['msiexec', '/i', str(installer), '/qn', '/norestart',
                       'INSTALLDIR=%s' % install_dir]
        uninstall_exe = None

    rc, output = run(install_cmd, cwd=workdir, timeout=timeout)
    result['install_rc'] = rc
    if rc != 0:
        step('silent_install', 'FAIL', '安装退出码 %s: %s' % (rc, output[-1000:]))
        result.update({'status': 'failed', 'exit_code': 1,
                       'reason': '静默安装失败'})
        return result
    step('silent_install', 'PASS', '退出码 0')

    exe = install_dir / EXE_NAME
    if exe.is_file():
        step('installed_exe', 'PASS', str(exe))
    else:
        step('installed_exe', 'FAIL', '安装目录缺少 %s: %s' % (EXE_NAME, install_dir))
        result.update({'status': 'failed', 'exit_code': 1,
                       'reason': '安装后找不到主程序'})
        return result

    uninstaller = None
    if tool == 'inno':
        uninstaller = install_dir / 'unins000.exe'
    if uninstaller is not None and uninstaller.is_file():
        step('uninstaller', 'PASS', str(uninstaller))
    elif tool == 'inno':
        step('uninstaller', 'FAIL', '安装目录缺少 unins000.exe')
    else:
        step('uninstaller', 'SKIP', 'MSI 由 Windows Installer 数据库管理卸载')

    # 开始菜单快捷方式（/NOICONS 或测试注入时 SKIP）
    shortcut = _start_menu_shortcut_path()
    result['shortcut_expected'] = str(shortcut)
    if no_icons:
        step('start_menu_shortcut', 'SKIP', '--no-icons（/NOICONS）：本次未创建快捷方式')
    elif not check_shortcuts:
        step('start_menu_shortcut', 'SKIP', 'check_shortcuts=False')
    elif shortcut.is_file():
        step('start_menu_shortcut', 'PASS', str(shortcut))
    else:
        step('start_menu_shortcut', 'FAIL',
             '未找到开始菜单快捷方式（受限沙箱可能禁止写用户目录）: %s' % shortcut)

    # 卸载注册项
    try:
        entries = lookup(install_dir)
    except Exception as exc:                        # noqa: BLE001
        entries = None
        step('uninstall_registry', 'SKIP', '注册表查询失败: %s' % exc)
    else:
        if entries:
            step('uninstall_registry', 'PASS', '找到 %d 条卸载项' % len(entries))
            result['registry_entries'] = entries
        else:
            step('uninstall_registry', 'FAIL', 'HKCU/HKLM Uninstall 下未找到 LightDB Studio')

    # exe --smoke（offscreen + 抑制首启向导 + 临时 HOME）
    home = workdir / 'home'
    home.mkdir(exist_ok=True)
    env_home = dict(os.environ)
    env_home['QT_QPA_PLATFORM'] = 'offscreen'
    env_home['LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN'] = '1'
    env_home['HOME'] = str(home)
    env_home['USERPROFILE'] = str(home)
    try:
        old = {}
        for key in ('QT_QPA_PLATFORM', 'LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN',
                    'HOME', 'USERPROFILE'):
            old[key] = os.environ.get(key)
            os.environ[key] = env_home[key]
        try:
            smoke_rc, smoke_out = run([str(exe), '--smoke'], cwd=install_dir,
                                      timeout=180)
        finally:
            for key, val in old.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        result['smoke_rc'] = smoke_rc
        if smoke_rc == 0:
            step('exe_smoke', 'PASS', '--smoke 退出码 0')
        else:
            step('exe_smoke', 'FAIL', '--smoke 退出码 %s: %s'
                 % (smoke_rc, smoke_out[-800:]))
    except Exception as exc:                        # noqa: BLE001
        step('exe_smoke', 'FAIL', '--smoke 执行异常: %s' % exc)

    # 静默卸载
    if tool == 'inno' and uninstaller is not None and uninstaller.exists():
        u_cmd = [str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                 '/NORESTART']
    else:
        u_cmd = ['msiexec', '/x', str(installer), '/qn', '/norestart']
    urc, uout = run(u_cmd, cwd=workdir, timeout=timeout)
    result['uninstall_rc'] = urc
    if urc == 0:
        step('silent_uninstall', 'PASS', '退出码 0')
    else:
        step('silent_uninstall', 'FAIL', '卸载退出码 %s: %s' % (urc, uout[-800:]))

    # 等待安装目录清理（卸载器异步）
    deadline = time.time() + 60
    while time.time() < deadline and exe.exists():
        time.sleep(0.2)
    if exe.exists():
        step('cleanup_install_dir', 'FAIL', '卸载后主程序仍存在: %s' % exe)
    else:
        step('cleanup_install_dir', 'PASS', '主程序已删除')
    try:
        entries = lookup(install_dir)
    except Exception:
        entries = None
    if entries:
        step('cleanup_registry', 'FAIL', '卸载后仍有 %d 条注册项' % len(entries))
    else:
        step('cleanup_registry', 'PASS', '卸载项已清理')

    failed = [s for s in result['steps'] if s['status'] == 'FAIL']
    result['status'] = 'failed' if failed else 'ok'
    result['exit_code'] = 1 if failed else 0
    if failed:
        result['reason'] = '冒烟失败: ' + '; '.join(s['name'] for s in failed)
    else:
        result['reason'] = '安装/卸载冒烟通过'
    if cleanup:
        shutil.rmtree(workdir, ignore_errors=True)
    return result


# ==================== CLI ====================

def _print_summary(report: dict):
    print('[build-installer] 版本: %s' % report.get('version'))
    print('[build-installer] 工具: %s' % (report.get('tool') or '（无可用工具）'))
    print('[build-installer] 状态: %s（exit=%s）'
          % (report.get('status'), report.get('exit_code')))
    if report.get('artifact'):
        print('[build-installer] 产物: %s  (%s bytes, sha256=%s…)'
              % (report['artifact'], report.get('artifact_size'),
                 (report.get('artifact_sha256') or '')[:16]))
    if report.get('reason'):
        print('[build-installer] 说明: %s' % report['reason'])
    if report.get('repro_scripts'):
        for p in report['repro_scripts']:
            print('[build-installer] 复现脚本: %s' % p)
    if report.get('commands_preview'):
        print('[build-installer] 构建命令（可复现）:')
        for c in report['commands_preview']:
            print('    ' + str(c))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='构建 LightDB Studio Windows 安装包（Inno Setup / WiX v3）')
    ap.add_argument('--version', default='',
                    help='安装包版本（默认取 lightdb.__version__）')
    ap.add_argument('--source', default='',
                    help='onedir 产物目录（默认 dist/studio/LightDB-Studio）')
    ap.add_argument('--out', default='',
                    help='输出目录（默认 dist/installer）')
    ap.add_argument('--tool', default='auto', choices=['auto', 'inno', 'wix'])
    ap.add_argument('--build-studio', action='store_true',
                    help='onedir 不存在时先运行 scripts/build_studio.py')
    ap.add_argument('--dry-run', action='store_true', help='只打印构建命令')
    ap.add_argument('--smoke', action='store_true',
                    help='构建成功后静默安装/卸载冒烟')
    ap.add_argument('--no-icons', action='store_true',
                    help='冒烟时传 /NOICONS（受限沙箱无法写用户开始菜单）')
    ap.add_argument('--json', default='',
                    help='把最终报告写到该路径（默认 build_report.json 已在 out 目录）')
    args = ap.parse_args(argv)

    report = build_installer(
        version=args.version, source=args.source or None,
        out=args.out or None, tool=args.tool, dry_run=args.dry_run,
        build_studio=args.build_studio)
    _print_summary(report)

    rc = int(report.get('exit_code', 1))
    if rc == 0 and args.smoke and report.get('status') == 'ok':
        smoke = smoke_install(report['artifact'], no_icons=args.no_icons,
                              cleanup=True)
        report['smoke'] = smoke
        for s in smoke.get('steps', []):
            print('[smoke] %-22s %-4s %s'
                  % (s['name'], s['status'], s['detail']))
        print('[build-installer] 冒烟: %s' % smoke.get('status'))
        rc = int(smoke.get('exit_code', 1))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        print('[build-installer] JSON 报告: %s' % args.json)
    return rc


if __name__ == '__main__':
    sys.exit(main())
