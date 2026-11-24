# 一次性初始化自托管 runner 的共享系统 Python 依赖
# 用法（在 runner 所在机器的普通 PowerShell 中）：
#   powershell -ExecutionPolicy Bypass -File scripts/bootstrap_ci_env.ps1
#
# 说明：
# - CI 每次运行只 `pip install -e . --no-deps`（见 .github/workflows/ci.yml）；
#   运行时依赖由本脚本一次性安装，避免每次联网解析依赖。
# - Qt 四个包必须同版本（PySide6/Essentials/Addons/shiboken6 == 6.6.0），
#   否则 QtCore DLL 加载失败、GUI 测试在 pytest_configure 崩溃。

$ErrorActionPreference = 'Stop'
$py = 'python'
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "[bootstrap] python: $(& $py --version 2>&1)"
Write-Host "[bootstrap] 清理被中断安装的 ~* 残留"
& $py -c "import glob, os, shutil, site; sp = site.getsitepackages()[0]; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob(os.path.join(sp, '~*'))]"

Write-Host "[bootstrap] 安装项目与全部 extras/dev 依赖"
& $py -m pip install -e "$repo[all,dev]" --retries 3 --timeout 30
if ($LASTEXITCODE -ne 0) { throw "pip install project failed" }

Write-Host "[bootstrap] 强制 Qt 四包 6.6.0 同版本"
& $py -m pip install --disable-pip-version-check --force-reinstall --no-deps --retries 3 --timeout 30 `
  "PySide6==6.6.0" "PySide6-Essentials==6.6.0" "PySide6-Addons==6.6.0" "shiboken6==6.6.0"
if ($LASTEXITCODE -ne 0) { throw "Qt pinned install failed" }

Write-Host "[bootstrap] 自检"
& $py -c "import PySide6.QtCore, shiboken6, pytest, fastapi, numpy, paramiko, psutil; print('bootstrap ok:', PySide6.QtCore.qVersion(), shiboken6.__version__)"
if ($LASTEXITCODE -ne 0) { throw "bootstrap import check failed" }
Write-Host "[bootstrap] done"
