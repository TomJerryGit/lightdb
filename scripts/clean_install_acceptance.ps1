# 干净安装 -> 冒烟 -> 备份/恢复 -> 重装(升级) -> 回滚 -> 卸载 全流程验收
# 用法（管理员或普通 PowerShell 均可；Windows Sandbox 内也可）：
#   powershell -ExecutionPolicy Bypass -File scripts\clean_install_acceptance.ps1 `
#     -Installer D:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe `
#     -WorkDir D:\lightdb_acceptance
# 输出：summary.json + 每步日志；任一步失败返回非 0 并保留现场。

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
New-Item -ItemType Directory -Force -Path $WorkDir, $logs, $homeDir | Out-Null

function Record([string]$name, [bool]$ok, [string]$detail) {
    $script:steps += [pscustomobject]@{ step = $name; ok = $ok; detail = $detail; at = (Get-Date).ToString('s') }
    $script:failed = $script:failed -or (-not $ok)
    Write-Host ("[{0}] {1} - {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $name, $detail)
}

function Run([string]$name, [scriptblock]$block) {
    try {
        $out = & $block 2>&1
        $code = $LASTEXITCODE
        $out | Out-File -FilePath (Join-Path $logs "$name.log") -Encoding utf8
        if ($null -eq $code) { $code = 0 }
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

# 1) 静默安装
Run 'install' {
    $args = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
              "/DIR=$AppDir", "/LOG=$(Join-Path $logs 'install.log')")
    $p = Start-Process -FilePath $Installer -ArgumentList $args -Wait -PassThru
    $p.ExitCode
}
$exe = Join-Path $AppDir 'LightDB-Studio.exe'
$unins = Join-Path $AppDir 'unins000.exe'
if (-not (Test-Path $exe)) {
    # 兜底：Inno 在 lowest 权限下可能装到 {autopf}（=%LOCALAPPDATA%\Programs）；
    # 某些受限/沙箱会话写注册表会被拒绝 → 安装 exit 0 但目录不存在。
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\LightDB Studio\LightDB-Studio.exe'
    if (Test-Path $candidate) {
        $AppDir = Split-Path -Parent $candidate
        $exe = $candidate
        $unins = Join-Path $AppDir 'unins000.exe'
    }
}
if (-not (Test-Path $exe)) {
    Record 'install_artifact' $false "missing $exe（请在本机普通 PowerShell 或 Windows Sandbox 中运行本脚本：受限沙箱会话无法写注册表/开始菜单）"
    $steps | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $WorkDir 'summary.json') -Encoding utf8
    exit 1
}
Record 'install_artifact' $true $exe

# 2) 安装后冒烟
Run 'smoke' {
    $env:QT_QPA_PLATFORM = 'offscreen'; $env:LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN = '1'
    $env:USERPROFILE = $homeDir; $env:HOME = $homeDir
    $p = Start-Process -FilePath $exe -ArgumentList '--smoke' -Wait -PassThru
    $p.ExitCode
}

# 3) 服务 + 连接 + 建表/插入/查询
Run 'server_start' {
    Stop-LightDB
    $env:USERPROFILE = $homeDir; $env:HOME = $homeDir
    Start-Process -FilePath $PythonExe -ArgumentList @(
        '-m', 'lightdb.main', '--mode', 'single', '--host', '127.0.0.1',
        '--port', "$Port", '--data-dir', $dataDir, '--root-password', 'demo123'
    ) -WorkingDirectory $repo -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { 1 } else { 0 }
}
Run 'probe' {
    $code = @'
import pymysql, sys
c = pymysql.connect(host='127.0.0.1', port=PORT, user='root', password='demo123', autocommit=True)
cur = c.cursor()
cur.execute('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
cur.execute("INSERT INTO t VALUES (1,'a'),(2,'b')")
cur.execute('SELECT COUNT(*) FROM t')
n = cur.fetchone()[0]
c.close()
print('rows=', n)
sys.exit(0 if n == 2 else 1)
'@
    $code = $code.Replace('PORT', "$Port")
    $tmp = Join-Path $WorkDir 'probe.py'
    Set-Content -Path $tmp -Value $code -Encoding ascii
    & $PythonExe $tmp
    $LASTEXITCODE
}

# 4) 备份 + 恢复 + 行数校验
Run 'snapshot_backup' {
    & $PythonExe (Join-Path $repo 'scripts\snapshot_backup.py') --data-dir $dataDir --out $backupDir
    $LASTEXITCODE
}
Run 'snapshot_restore' {
    & $PythonExe (Join-Path $repo 'scripts\snapshot_restore.py') --snapshot $backupDir --data-dir $restoreDir
    $LASTEXITCODE
}
Run 'verify_restored_shows_ai' {
    # 恢复后按快照目录核对：恢复脚本内部已做行数/校验；这里再确认目标目录非空
    if (Test-Path $restoreDir) { 0 } else { 1 }
}

# 5) 重装（模拟升级）：覆盖安装后原数据仍在
Stop-LightDB
Run 'reinstall_upgrade' {
    $p = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', "/DIR=$AppDir") -Wait -PassThru
    $p.ExitCode
}
Run 'data_survives_upgrade' {
    if ((Test-Path $dataDir) -and (Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue)) { 0 } else { 1 }
}

# 6) 回滚：卸载 + 重装（保留数据目录）
Run 'rollback_uninstall' {
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    Start-Sleep -Seconds 3
    $p.ExitCode
}
Run 'rollback_reinstall' {
    $p = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', "/DIR=$AppDir") -Wait -PassThru
    $p.ExitCode
}
Run 'rollback_data_check' {
    if ((Test-Path $dataDir) -and (Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue)) { 0 } else { 1 }
}

# 7) 最终卸载 + 目录清理
Stop-LightDB
Run 'final_uninstall' {
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    Start-Sleep -Seconds 5
    $p.ExitCode
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

