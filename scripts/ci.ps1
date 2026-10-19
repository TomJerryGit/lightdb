# LightDB 本地 CI（与 .github/workflows/ci.yml 等价的"一条命令"）
#
#   .\scripts\ci.ps1                 # 完整：安装 + compileall + 全量 pytest + 基准门禁 + 打包检查
#   .\scripts\ci.ps1 -SkipInstall    # 环境已装好时跳过 pip install
#   .\scripts\ci.ps1 -SkipTests      # 只跑编译/打包检查
#
# 退出码：0 = 全过；非 0 = 有失败步骤。
param(
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host ""
    Write-Host ("=== {0} ===" -f $Name) -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw ("[FAIL] {0} (exit {1})" -f $Name, $LASTEXITCODE)
    }
    Write-Host ("[OK] {0} ({1:N1}s)" -f $Name, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

if (-not $SkipInstall) {
    Step 'pip install -e .[all,dev]' { python -m pip install -e '.[all,dev]' }
}

Step 'compileall' { python -m compileall -q lightdb lightdb_studio web }

if (-not $SkipTests) {
    Step 'pytest' { python -m pytest tests -q -p no:cacheprovider }
    Step 'benchmark gate' { python -m benchmarks.run --suite quick --gate }
}

Step 'packaging check' { python scripts/check_packaging.py }

Write-Host ""
Write-Host 'CI: ALL CHECKS PASSED' -ForegroundColor Green
