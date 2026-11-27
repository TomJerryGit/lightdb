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
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "LOG=$backupAbs\logs\task-cmd.log"
"$PythonExe" "$script" run --data-dir "$dataAbs" --out "$backupAbs" --keep $Keep --drill >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $dailyCmd -Encoding ASCII
@"
@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "LOG=$backupAbs\logs\task-cmd.log"
"$PythonExe" "$script" drill --out "$backupAbs" >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $drillCmd -Encoding ASCII

# 先创建任务绑定当前用户交互令牌（/IT），无需保存密码、无需管理员
function New-LightDBTask([string]$name, [string[]]$params) {
    # PS 5.1 在 $ErrorActionPreference='Stop' 时会把原生 stderr 当终止错误；
    # 这里局部放宽，先拿到退出码与输出，再决定是"给出权限指引"还是抛错。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & schtasks @params 2>&1
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $out | Out-Host
    if ($rc -ne 0) {
        $text = ($out | Out-String)
        if ($text -match 'Access is denied|拒绝访问') {
            Write-Host ""
            Write-Host "!! 当前会话无权创建计划任务（被沙箱/低权限限制）。"
            Write-Host "!! 请在普通 PowerShell（开始菜单 → Windows PowerShell）中重新运行本脚本。"
            exit 3
        }
        throw "创建任务 $name 失败 rc=${rc}: $text"
    }
}

New-LightDBTask $dailyTask @('/Create', '/F', '/SC', 'DAILY', '/ST', $DailyAt,
                             '/TN', $dailyTask, '/TR', "`"$dailyCmd`"", '/IT')
New-LightDBTask $drillTask @('/Create', '/F', '/SC', 'WEEKLY', '/D', $DrillDay,
                             '/ST', $DrillAt, '/TN', $drillTask,
                             '/TR', "`"$drillCmd`"", '/IT')

Write-Host ''
Write-Host '已安装计划任务：'
& schtasks /Query /TN $dailyTask /FO LIST | Out-Host
& schtasks /Query /TN $drillTask /FO LIST | Out-Host
Write-Host ''
Write-Host "验证一次性运行（立即备份+校验）:"
Write-Host "  `"$PythonExe`" `"$script`" run --data-dir `"$dataAbs`" --out `"$backupAbs`" --keep $Keep --drill"
Write-Host "查看状态: `"$PythonExe`" `"$script`" status --out `"$backupAbs`""
Write-Host "卸载: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $repo 'scripts\install_backup_task.ps1')`" -Uninstall"
