# LightDB 备份计划任务安装/卸载（Windows，当前用户，无需管理员）
#
# 安装（普通 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1 `
#     -DataDir D:\lightdb\data -BackupDir D:\lightdb_backup -Keep 14 `
#     -DailyAt 02:00 -DrillDay Sunday -DrillAt 03:00
#
# 卸载：
#   powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1 -Uninstall
#
# 任务：
#   <Prefix>-Daily  每天 DailyAt 执行：备份 + 恢复校验 + 保留策略
#   <Prefix>-Drill  每周 DrillDay DrillAt 执行：对最新备份做恢复演练
# 日志：<BackupDir>\logs\task-YYYYMMDD.log（stdout+stderr 追加）

param(
    [string]$DataDir = '',
    [string]$BackupDir = '',
    [int]$Keep = 14,
    [string]$DailyAt = '02:00',
    [string]$DrillDay = 'Sunday',
    [string]$DrillAt = '03:00',
    [string]$Prefix = 'LightDBBackup',
    [string]$PythonExe = 'python',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $repo 'data' }
if (-not $BackupDir) { $BackupDir = Join-Path $repo 'backups_auto' }

$dailyTask = "$Prefix-Daily"
$drillTask = "$Prefix-Drill"

if ($Uninstall) {
    foreach ($t in @($dailyTask, $drillTask)) {
        & schtasks /Delete /TN $t /F 2>$null | Out-Null
        Write-Host "deleted task: $t (如果不存在则忽略)"
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $BackupDir, (Join-Path $BackupDir 'logs') | Out-Null
$dataAbs = (Resolve-Path -LiteralPath $DataDir -ErrorAction Stop).Path
$backupAbs = (Resolve-Path -LiteralPath $BackupDir).Path
$script = Join-Path $repo 'scripts\backup_automation.py'

# 计划任务用 .cmd 包装（避免 schtasks /TR 引号地狱），并重定向日志
$dailyCmd = Join-Path $BackupDir 'run_backup.cmd'
$drillCmd = Join-Path $BackupDir 'run_drill.cmd'
@"
@echo off
chcp 65001 >nul
set LOG=%BackupDir%\logs\task-%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.log
"$PythonExe" "$script" run --data-dir "$dataAbs" --out "$backupAbs" --keep $Keep --drill >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $dailyCmd -Encoding ASCII
@"
@echo off
chcp 65001 >nul
set LOG=%BackupDir%\logs\task-%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.log
"$PythonExe" "$script" drill --out "$backupAbs" >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $drillCmd -Encoding ASCII

# 先创建任务绑定当前用户交互令牌（/IT），无需保存密码、无需管理员
& schtasks /Create /F /SC DAILY /ST $DailyAt /TN $dailyTask /TR "`"$dailyCmd`"" /IT | Out-Host
if ($LASTEXITCODE -ne 0) { throw "创建每日任务失败 rc=$LASTEXITCODE" }
& schtasks /Create /F /SC WEEKLY /D $DrillDay /ST $DrillAt /TN $drillTask /TR "`"$drillCmd`"" /IT | Out-Host
if ($LASTEXITCODE -ne 0) { throw "创建每周演练任务失败 rc=$LASTEXITCODE" }

Write-Host ''
Write-Host '已安装计划任务：'
& schtasks /Query /TN $dailyTask /FO LIST | Out-Host
& schtasks /Query /TN $drillTask /FO LIST | Out-Host
Write-Host ''
Write-Host "验证一次性运行（立即备份+校验）:"
Write-Host "  `"$PythonExe`" `"$script`" run --data-dir `"$dataAbs`" --out `"$backupAbs`" --keep $Keep --drill"
Write-Host "查看状态: `"$PythonExe`" `"$script`" status --out `"$backupAbs`""
Write-Host "卸载: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $repo 'scripts\install_backup_task.ps1')`" -Uninstall"
