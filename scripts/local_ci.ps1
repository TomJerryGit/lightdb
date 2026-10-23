#requires -Version 5.1
<#
.SYNOPSIS
    LightDB 本地 CI（GitHub Actions 不可用/额度耗尽时的等价验证）。
.DESCRIPTION
    依次执行：UTF-8 环境 → compileall → GUI smoke(offscreen) → 全量 pytest(JUnit)
    → benchmark quick gate → 打包检查（-SkipPackaging 可跳过）。
    任一步失败立即以非 0 退出；输出写入 _analysis_tmp/local_ci/。
.EXAMPLE
    pwsh -File scripts/local_ci.ps1
    pwsh -File scripts/local_ci.ps1 -SkipBenchmark
#>
param(
    [switch]$SkipBenchmark,
    [switch]$SkipPackaging
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
$env:LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN = '1'
$outDir = Join-Path $root '_analysis_tmp\local_ci'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$python = 'python'

function Step([string]$name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "OK: $name" -ForegroundColor Green
}

Step 'compileall' { & $python -m compileall -q lightdb lightdb_studio scripts benchmarks }
Step 'GUI smoke (offscreen)' {
    $env:USERPROFILE = Join-Path $root '_analysis_tmp\gui_full\home'
    $env:HOME = $env:USERPROFILE
    & $python -m lightdb_studio.app --smoke
}
Step 'full pytest (JUnit)' {
    $junit = Join-Path $outDir 'pytest-results.xml'
    $log = Join-Path $outDir 'pytest.log'
    & $python -m pytest tests -q -p no:cacheprovider "--junitxml=$junit" *> $log
    $code = $LASTEXITCODE
    & $python scripts/check_junit.py $junit
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($code -ne 0) { exit $code }
    exit 0
}
if (-not $SkipBenchmark) {
    Step 'benchmark quick gate' { & $python -m benchmarks.run --suite quick --gate }
}
if (-not $SkipPackaging) {
    Step 'packaging check' {
        & $python -m build --wheel --outdir (Join-Path $outDir 'dist') 2>&1 |
            Out-File (Join-Path $outDir 'build.log')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $wheel = Get-ChildItem (Join-Path $outDir 'dist') -Filter *.whl |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        & $python -c "import sys, zipfile; z=zipfile.ZipFile(sys.argv[1]); names=z.namelist(); assert any(n.startswith('lightdb/') for n in names), names; print('wheel ok:', sys.argv[1])" $wheel.FullName
    }
}
Write-Host "`nLOCAL CI PASS (logs: $outDir)" -ForegroundColor Green
exit 0
