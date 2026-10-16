# LightDB Studio one-click launcher
#
#   Usage (in the project directory):
#       .\run_studio.cmd            <- recommended (double-clickable)
#       .\run_studio.ps1            <- may be blocked by execution policy
#
# It does two things:
#   [1/2] If nothing listens on 127.0.0.1:8899, start a single-process server
#         in the BACKGROUND (multi-process mode cannot start in the sandbox
#         because named pipes are unavailable). If 8899 already answers, skip.
#   [2/2] Start the GUI in the FOREGROUND. The GUI auto-connects using
#         %USERPROFILE%\.lightdb_studio\connections.json ("last_connection")
#         and the object browser lists the tables.
#
# Closing: close the GUI window. The background server keeps running; the
# stop command is printed at the end.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads a BOM-less
# .ps1 as ANSI (GBK here), so non-ASCII text gets mangled in the console.
param(
    [int]$Port = 8899,
    [string]$DataDir = 'data_demo'
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Test-LightDbPort([int]$p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $r = $c.BeginConnect('127.0.0.1', $p, $null, $null)
        $ok = $r.AsyncWaitHandle.WaitOne(300) -and $c.Connected
        $c.Close()
        return $ok
    } catch {
        return $false
    }
}

Write-Host ''
Write-Host '  LightDB Studio launcher'
Write-Host '  ======================' -ForegroundColor DarkGray

# ---------- [1/2] server ----------
if (Test-LightDbPort $Port) {
    Write-Host ("  [1/2] server already listening on 127.0.0.1:{0} - skipped" -f $Port) -ForegroundColor Green
} else {
    Write-Host ("  [1/2] starting server on 127.0.0.1:{0} ..." -f $Port) -ForegroundColor Cyan
    $srv = Start-Process python -ArgumentList @(
        '-m', 'lightdb.main', '--mode', 'single',
        '--host', '127.0.0.1', '--port', "$Port",
        '--data-dir', $DataDir
    ) -PassThru -WindowStyle Hidden

    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 40 -and -not (Test-LightDbPort $Port)) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-LightDbPort $Port) {
        Write-Host ("        ready (PID {0}, data dir {1}, {2:N2}s)" -f `
            $srv.Id, $DataDir, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
        Write-Host ("        stop it with: Stop-Process -Id {0}" -f $srv.Id) -ForegroundColor DarkGray
    } else {
        Write-Host '        server did not come up within 40s; run it manually:' -ForegroundColor Red
        Write-Host ("        python -m lightdb.main --mode single --host 127.0.0.1 --port {0} --data-dir {1}" -f $Port, $DataDir) -ForegroundColor Yellow
        exit 1
    }
}

# ---------- [2/2] GUI ----------
Write-Host '  [2/2] starting GUI (closing the window ends this script) ...' -ForegroundColor Cyan
Write-Host ''
python -m lightdb_studio.app

Write-Host ''
Write-Host '  GUI exited.' -ForegroundColor DarkGray
Write-Host '  To stop the background server:' -ForegroundColor DarkGray
Write-Host '    Get-CimInstance Win32_Process -Filter "Name=''python.exe''" |' -ForegroundColor DarkGray
Write-Host '      Where-Object { $_.CommandLine -match "lightdb.main" } |' -ForegroundColor DarkGray
Write-Host '      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }' -ForegroundColor DarkGray
