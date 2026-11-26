# LightDB clean acceptance: install -> smoke -> server/probe -> backup -> restore
# -> reinstall(upgrade) -> rollback -> uninstall
# Run in an elevated PowerShell (recommended) or Windows Sandbox/VM:
#   powershell -ExecutionPolicy Bypass -File scripts\clean_install_acceptance.ps1 `
#     -Installer D:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe `
#     -WorkDir D:\lightdb_acceptance

param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$WorkDir = (Join-Path $env:TEMP 'lightdb_acceptance'),
    [string]$AppDir = '',
    [string]$PythonExe = 'python',
    [int]$Port = 8901
)

$ErrorActionPreference = 'Stop'
$steps = @()
$failed = $false
if (-not $AppDir) { $AppDir = Join-Path $WorkDir 'App' }
$dataDir = Join-Path $WorkDir 'data'
$backupDir = Join-Path $WorkDir 'backup'
$restoreDir = Join-Path $WorkDir 'restore'
$homeDir = Join-Path $WorkDir 'home'
$logs = Join-Path $WorkDir 'logs'
$repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = $repo
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force -Path $WorkDir, $logs, $homeDir | Out-Null
# 清理上一轮残留（否则快照/恢复会因目标目录非空失败）
foreach ($d in @($dataDir, $backupDir, $restoreDir)) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
}

function Record([string]$name, [bool]$ok, [string]$detail) {
    $script:steps += [pscustomobject]@{ step = $name; ok = $ok; detail = $detail; at = (Get-Date).ToString('s') }
    $script:failed = $script:failed -or (-not $ok)
    Write-Host ("[{0}] {1} - {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name, $detail)
}

function Run([string]$name, [scriptblock]$block) {
    try {
        $out = @(& $block 2>&1)
        $code = 0
        if ($out.Count -gt 0) {
            $last = $out[-1]
            if ($last -is [int]) { $code = [int]$last }
            elseif ("$last" -match '^-?\d+$') { $code = [int]"$last" }
        }
        $out | Out-File -FilePath (Join-Path $logs "$name.log") -Encoding utf8
        Record $name ($code -eq 0) "exit=$code"
        return $code
    } catch {
        Record $name $false $_.Exception.Message
        return 1
    }
}

function Stop-LightDB {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*lightdb.main*' -and $_.CommandLine -like "*$WorkDir*" } |
        ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null }
    Start-Sleep -Seconds 1
}

function Wait-Port([int]$p, [int]$timeoutSec = 40) {
    for ($i = 0; $i -lt $timeoutSec; $i++) {
        if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

Write-Host "== LightDB clean acceptance =="
Write-Host "installer: $Installer"
Write-Host "workdir:   $WorkDir"
if (-not (Test-Path $Installer)) {
    Record 'precondition' $false "installer not found: $Installer"
    $steps | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $WorkDir 'summary.json') -Encoding utf8
    exit 1
}
$sha = (Get-FileHash $Installer -Algorithm SHA256).Hash
Record 'precondition' $true "sha256=$sha"
Record 'session' $true "user=$env:USERNAME elevated=$(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

# 1) silent install
$innoLog = Join-Path $WorkDir 'install_inno.log'
$installArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                 '/NOICONS', "/DIR=$AppDir", "/LOG=$innoLog")
Run 'install' {
    $p = Start-Process -FilePath $Installer -ArgumentList $installArgs -Wait -PassThru
    Write-Host "installer exit=$($p.ExitCode)"
    if (Test-Path $innoLog) {
        Write-Host '--- installer log (tail) ---'
        Get-Content $innoLog -Tail 20 | ForEach-Object { Write-Host $_ }
    }
    return [int]$p.ExitCode
}
$exe = Join-Path $AppDir 'LightDB-Studio.exe'
$unins = Join-Path $AppDir 'unins000.exe'
if (-not (Test-Path $exe)) {
    Record 'install_artifact' $false "missing $exe; see $innoLog"
    $steps | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $WorkDir 'summary.json') -Encoding utf8
    exit 1
}
Record 'install_artifact' $true $exe

# 2) smoke
Run 'smoke' {
    $env:QT_QPA_PLATFORM = 'offscreen'; $env:LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN = '1'
    $env:USERPROFILE = $homeDir; $env:HOME = $homeDir
    $p = Start-Process -FilePath $exe -ArgumentList '--smoke' -Wait -PassThru
    return [int]$p.ExitCode
}

# 3) server + probe
Run 'server_start' {
    Stop-LightDB
    $env:USERPROFILE = $homeDir; $env:HOME = $homeDir
    Start-Process -FilePath $PythonExe -ArgumentList @(
        '-m', 'lightdb.main', '--mode', 'single', '--host', '127.0.0.1',
        '--port', "$Port", '--data-dir', $dataDir, '--root-password', 'demo123'
    ) -WorkingDirectory $repo -WindowStyle Hidden | Out-Null
    if (Wait-Port $Port 40) { Start-Sleep -Seconds 2; return 0 } else { return 1 }
}
$probeFile = Join-Path $WorkDir 'probe.py'
$probeSrc = @'
import pymysql, sys
try:
    c = pymysql.connect(host='127.0.0.1', port=__PORT__, user='root', password='demo123', autocommit=True)
    cur = c.cursor()
    cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    cur.execute("INSERT INTO t VALUES (1,'a'),(2,'b')")
    cur.execute('SELECT COUNT(*) FROM t')
    n = cur.fetchone()[0]
    c.close()
    print('rows=', n)
    sys.exit(0 if n == 2 else 1)
except Exception as exc:
    print('PROBE ERROR:', type(exc).__name__, exc)
    sys.exit(1)
'@
$probeSrc = $probeSrc.Replace('__PORT__', "$Port")
Set-Content -Path $probeFile -Value $probeSrc -Encoding ascii
Run 'probe' {
    & $PythonExe $probeFile *> (Join-Path $logs 'probe_stdout.log')
    $code = $LASTEXITCODE
    Get-Content (Join-Path $logs 'probe_stdout.log') -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host $_ }
    return [int]$code
}

# 4) backup + restore
Run 'snapshot_backup' {
    & $PythonExe (Join-Path $repo 'scripts\snapshot_backup.py') --data-dir $dataDir --out $backupDir *> (Join-Path $logs 'backup_stdout.log')
    $code = $LASTEXITCODE
    Get-Content (Join-Path $logs 'backup_stdout.log') -ErrorAction SilentlyContinue |
        Select-Object -Last 5 | ForEach-Object { Write-Host $_ }
    return [int]$code
}
Run 'snapshot_restore' {
    & $PythonExe (Join-Path $repo 'scripts\snapshot_restore.py') --snapshot $backupDir --data-dir $restoreDir *> (Join-Path $logs 'restore_stdout.log')
    $code = $LASTEXITCODE
    Get-Content (Join-Path $logs 'restore_stdout.log') -ErrorAction SilentlyContinue |
        Select-Object -Last 5 | ForEach-Object { Write-Host $_ }
    return [int]$code
}
Run 'verify_restored_meta' {
    if ((Test-Path (Join-Path $restoreDir '_meta.json')) -and
        (Get-ChildItem $restoreDir -Filter '*.dat' -ErrorAction SilentlyContinue)) { return 0 } else { return 1 }
}

# 5) reinstall (upgrade simulation), data must survive
Stop-LightDB
Run 'reinstall_upgrade' {
    $p = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', "/DIR=$AppDir") -Wait -PassThru
    return [int]$p.ExitCode
}
Run 'data_survives_upgrade' {
    if ((Test-Path $dataDir) -and (Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue)) { return 0 } else { return 1 }
}

# 6) rollback: uninstall + reinstall (data dir untouched)
Run 'rollback_uninstall' {
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    Start-Sleep -Seconds 3
    return [int]$p.ExitCode
}
Run 'rollback_reinstall' {
    $p = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', "/DIR=$AppDir") -Wait -PassThru
    return [int]$p.ExitCode
}
Run 'rollback_data_check' {
    if ((Test-Path $dataDir) -and (Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue)) { return 0 } else { return 1 }
}

# 7) final uninstall + cleanup
Stop-LightDB
Run 'final_uninstall' {
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    Start-Sleep -Seconds 5
    return [int]$p.ExitCode
}
Record 'appdir_removed' (-not (Test-Path $AppDir)) "appdir_exists=$(Test-Path $AppDir)"

$summary = [pscustomobject]@{
    schema = 'lightdb-clean-acceptance/1'
    at = (Get-Date).ToString('s')
    installer = $Installer
    sha256 = $sha
    workdir = $WorkDir
    ok = (-not $failed)
    steps = $steps
}
$summary | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $WorkDir 'summary.json') -Encoding utf8
Write-Host "summary: $(Join-Path $WorkDir 'summary.json')"
Write-Host "result: $(if ($failed) { 'FAIL' } else { 'PASS' })"
if ($failed) { exit 1 } else { exit 0 }
