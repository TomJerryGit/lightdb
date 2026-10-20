# LightDB local CI - one command, same gates as .github/workflows/ci.yml
#
#   .\scripts\ci.ps1                 # install + compileall + full pytest + benchmark gate + packaging check
#   .\scripts\ci.ps1 -SkipInstall    # skip pip install (environment already set up)
#   .\scripts\ci.ps1 -SkipTests      # only compileall + packaging check
#
# Exit code: 0 = all passed; non-zero = a step failed.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI,
# so non-ASCII comments can break parsing on this machine.
param(
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host ''
    Write-Host ('=== {0} ===' -f $Name) -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw ('[FAIL] {0} (exit {1})' -f $Name, $LASTEXITCODE)
    }
    Write-Host ('[OK] {0} ({1:N1}s)' -f $Name, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

if (-not $SkipInstall) {
    Step 'pip install -e .[all,dev]' { python -m pip install -e '.[all,dev]' }
}

Step 'compileall' {
    # Redirect bytecode cache to a writable dir: in restricted sandboxes some
    # source dirs (e.g. web/) are read-only, and compileall treats unwritable
    # __pycache__ as an error. PYTHONPYCACHEPREFIX keeps the check meaningful.
    $env:PYTHONPYCACHEPREFIX = Join-Path (Get-Location) '_analysis_tmp\pycache'
    try {
        python -m compileall -q lightdb lightdb_studio web
    } finally {
        Remove-Item Env:\PYTHONPYCACHEPREFIX -ErrorAction SilentlyContinue
    }
}

if (-not $SkipTests) {
    Step 'pytest' { python -m pytest tests -q -p no:cacheprovider }
    Step 'benchmark gate' { python -m benchmarks.run --suite quick --gate }
}

Step 'packaging check' { python scripts/check_packaging.py }

Write-Host ''
Write-Host 'CI: ALL CHECKS PASSED' -ForegroundColor Green
