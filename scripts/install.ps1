# ============================================================
# LightDB v3.0 安装脚本 (Windows PowerShell)
#
# 用法:
#   .\install.ps1                        # 默认安装
#   .\install.ps1 -Prefix "D:\lightdb"   # 指定目录
#   .\install.ps1 -Mode venv             # venv 模式
#
# 注意: 需要 PowerShell 5.1+ 或 PowerShell 7+
# ============================================================

[CmdletBinding()]
param(
    [string]$Prefix  = "C:\LightDB",
    [string]$Version = "3.0.0",
    [string]$Python  = "python",
    [ValidateSet("pip", "venv", "docker")]
    [string]$Mode    = "venv",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# ---------- 颜色 ----------
function Write-Info  { param($msg) Write-Host "[INFO] $msg"  -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Write-Err   { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }
function Write-Step  { param($msg) Write-Host "`n━━━ $msg ━━━" -ForegroundColor Cyan }

# ---------- 环境检查 ----------
function Test-Environment {
    Write-Step "1/7 检查环境"

    # OS
    $os = [System.Environment]::OSVersion.Platform
    Write-Info "操作系统: $os"

    # Python
    try {
        $pyVersion = & $Python --version 2>&1
        Write-Info "$pyVersion"
    } catch {
        Write-Err "未找到 Python，请先安装 Python 3.8+"
        Write-Host "下载: https://www.python.org/downloads/"
        exit 1
    }

    # Python 版本检查
    $verOk = & $Python -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Python 版本过低，需要 3.8+"
        exit 1
    }

    # pip
    try {
        $pipVersion = & $Python -m pip --version 2>&1
        Write-Info $pipVersion
    } catch {
        Write-Err "pip 未安装"
        exit 1
    }

    # 磁盘空间
    try {
        $drive = (Split-Path -Qualifier $Prefix)
        $freeGB = [math]::Round((Get-PSDrive $drive.TrimEnd(':')).Free / 1GB, 2)
        Write-Info "可用空间 ($drive): $freeGB GB"
        if ($freeGB -lt 2) {
            Write-Warn "可用空间少于 2GB"
        }
    } catch {
        Write-Warn "无法检测磁盘空间"
    }

    Write-Info "✓ 环境检查通过"
}

# ---------- 创建目录 ----------
function New-Directories {
    Write-Step "2/7 创建目录"

    $dirs = @(
        $Prefix,
        "$Prefix\data",
        "$Prefix\logs",
        "$Prefix\config",
        "$Prefix\backups",
        "$Prefix\bin"
    )

    foreach ($d in $dirs) {
        if (-not (Test-Path $d)) {
            New-Item -ItemType Directory -Path $d -Force | Out-Null
            Write-Info "创建 $d"
        }
    }

    Write-Info "✓ 目录已就绪"
}

# ---------- 安装依赖 ----------
function Install-Dependencies {
    Write-Step "3/7 安装依赖"

    Write-Info "升级 pip..."
    & $Python -m pip install --upgrade pip -q

    Write-Info "安装核心依赖..."
    & $Python -m pip install -q `
        "pymysql>=1.0.0" `
        "psutil>=5.9.0"

    Write-Info "✓ 依赖安装完成"
}

# ---------- 安装 LightDB ----------
function Install-LightDB {
    Write-Step "4/7 安装 LightDB $Version"

    switch ($Mode) {
        "pip" {
            Write-Info "从 PyPI 安装 lightdb==$Version..."
            & $Python -m pip install -q "lightdb==$Version"
        }
        "venv" {
            $venvPath = "$Prefix\venv"
            Write-Info "创建虚拟环境: $venvPath"
            & $Python -m venv $venvPath

            $venvPy = "$venvPath\Scripts\python.exe"
            & $venvPy -m pip install --upgrade pip -q
            & $venvPy -m pip install -q `
                "pymysql>=1.0.0" `
                "psutil>=5.9.0"

            # 从源码安装
            $srcDir = "$Prefix\src"
            if (-not (Test-Path $srcDir)) {
                Write-Info "克隆源码..."
                git clone -q "https://github.com/lightdb/lightdb.git" $srcDir
            }
            Push-Location $srcDir
            & $venvPy -m pip install -q -e .
            Pop-Location
        }
        "docker" {
            Write-Info "Docker 模式：请使用 docker compose 部署"
            return
        }
    }

    Write-Info "✓ LightDB 安装完成"
}

# ---------- 生成配置 ----------
function New-Config {
    Write-Step "5/7 生成配置文件"

    $confFile = "$Prefix\config\config.json"
    if (Test-Path $confFile) {
        Write-Info "配置文件已存在，跳过"
        return
    }

    $dataDir = "$Prefix\data" -replace '\\', '/'
    $backupDir = "$Prefix\backups" -replace '\\', '/'

    $config = @{
        host = "0.0.0.0"
        port = 8888
        max_connections = 100
        buffer_pool_size = 1000
        page_size = 4096
        data_dir = $dataDir
        backup_dir = $backupDir
        workers = 1
        parallel_workers = 2
        slow_query_threshold = 0.5
        max_allowed_packet = 16777216
        log_level = "INFO"
    } | ConvertTo-Json

    $config | Out-File -FilePath $confFile -Encoding UTF8
    Write-Info "已生成: $confFile"
    Write-Info "✓ 配置就绪"
}

# ---------- 安装 CLI 工具 ----------
function Install-CLI {
    Write-Step "6/7 安装命令行工具"

    $binDir = "$Prefix\bin"
    $venvPy = if ($Mode -eq "venv") {
        "$Prefix\venv\Scripts\python.exe"
    } else {
        $Python
    }

    $tools = @{
        "lightdb-server.bat" = "-m lightdb.main --config `"$Prefix\config\config.json`" %*"
        "lightdb-cli.bat"    = "-m lightdb.tools.cli %*"
        "lightdb-check.bat"  = "-m lightdb.tools.check %*"
        "lightdb-bench.bat"  = "-m lightdb.benchmark.cli %*"
        "lightdb-tune.bat"   = "-m lightdb.tools.tune %*"
        "lightdb-dump.bat"   = "-m lightdb.tools.dump %*"
    }

    foreach ($name in $tools.Keys) {
        $content = "@echo off`r`n`"$venvPy`" $($tools[$name])"
        $content | Out-File -FilePath "$binDir\$name" -Encoding ASCII
        Write-Info "创建 $name"
    }

    # 加入 PATH
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath -notlike "*$binDir*") {
        Write-Warn "请手动将以下路径加入 PATH:"
        Write-Host "  $binDir"
        Write-Host ""
        Write-Host "临时加入（当前会话）:"
        Write-Host "  `$env:Path += `";$binDir`""
        Write-Host ""
        Write-Host "永久加入:"
        Write-Host "  [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$binDir`", 'User')"
    }

    Write-Info "✓ CLI 就绪"
}

# ---------- 生成服务 ----------
function New-Service {
    Write-Step "7/7 生成服务文件"

    $scriptDir = "$Prefix\bin"
    $taskName = "LightDB"

    # 提供注册计划任务的脚本
    $registerScript = @"
# 以管理员权限运行，注册 LightDB 为 Windows 服务

`$action = New-ScheduledTaskAction ``
    -Execute `"$Prefix\bin\lightdb-server.bat`" ``
    -WorkingDirectory `"$Prefix`"

`$trigger = New-ScheduledTaskTrigger -AtStartup

`$principal = New-ScheduledTaskPrincipal ``
    -UserId `"`$env:USERNAME`" ``
    -LogonType S4U ``
    -RunLevel Highest

`$settings = New-ScheduledTaskSettingsSet ``
    -AllowStartIfOnBatteries ``
    -DontStopIfGoingOnBatteries ``
    -RestartCount 3 ``
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask ``
    -TaskName `"$taskName`" ``
    -Action `$action ``
    -Trigger `$trigger ``
    -Principal `$principal ``
    -Settings `$settings ``
    -Force

Write-Host "服务已注册: $taskName"
Write-Host "启动: Start-ScheduledTask -TaskName $taskName"
"@

    $registerScript | Out-File -FilePath "$scriptDir\register-service.ps1" -Encoding UTF8
    Write-Info "已生成: $scriptDir\register-service.ps1"
    Write-Info "运行方式（管理员 PowerShell）:"
    Write-Host "  $scriptDir\register-service.ps1"

    Write-Info "✓ 服务脚本就绪"
}

# ---------- 打印总结 ----------
function Show-Summary {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  LightDB 安装完成！" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  安装目录: $Prefix"
    Write-Host "  数据目录: $Prefix\data"
    Write-Host "  配置目录: $Prefix\config"
    Write-Host "  日志目录: $Prefix\logs"
    Write-Host "  可执行:   $Prefix\bin"
    Write-Host ""
    Write-Host "  启动服务:"
    Write-Host "    $Prefix\bin\lightdb-server.bat"
    Write-Host ""
    Write-Host "  连接测试:"
    Write-Host "    $Prefix\bin\lightdb-cli.bat -h 127.0.0.1 -P 8888 -u root"
    Write-Host ""
    Write-Host "  性能测试:"
    Write-Host "    $Prefix\bin\lightdb-bench.bat run --workload all"
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
}

# ---------- 主流程 ----------
function Main {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Blue
    Write-Host "  LightDB v$Version 安装程序 (Windows)" -ForegroundColor Blue
    Write-Host "============================================================" -ForegroundColor Blue
    Write-Host "  安装目录: $Prefix"
    Write-Host "  安装模式: $Mode"
    Write-Host ""

    # 检查是否管理员
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Write-Warn "当前不是管理员模式，部分功能（服务注册）可能受限"
    }

    Test-Environment
    New-Directories
    Install-Dependencies
    Install-LightDB
    New-Config
    Install-CLI
    New-Service
    Show-Summary
}

Main