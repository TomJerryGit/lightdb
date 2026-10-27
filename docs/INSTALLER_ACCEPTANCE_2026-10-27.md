# Windows 安装包验收（2026-10-27）

产物：`dist/installer/LightDB-Studio-Setup-4.0.0.exe`（57,061,774 B，Inno Setup 6，
sha256 `c7a9b8d59bc40657…`），输入为 onedir `dist/studio/LightDB-Studio`。

## 验收命令与结果（管理员 PowerShell，-Wait 确保 GUI 程序阻塞）

```powershell
$p = Start-Process 'D:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe' `
  -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/DIR=D:\LightDBStudio','/LOG=D:\ld_install.log' -Wait -PassThru
"install_exit=$($p.ExitCode)"

$env:QT_QPA_PLATFORM='offscreen'; $env:LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN='1'
$s = Start-Process 'D:\LightDBStudio\LightDB-Studio.exe' -ArgumentList '--smoke' -Wait -PassThru
"smoke_exit=$($s.ExitCode)"

$u = Start-Process 'D:\LightDBStudio\unins000.exe' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru
"uninstall_exit=$($u.ExitCode)"
Start-Sleep 5; "dir_gone=$(-not (Test-Path 'D:\LightDBStudio'))"
```

结果：

| 步骤 | 退出码 | 说明 |
|---|---|---|
| 静默安装 | **0** | 安装到 `D:\LightDBStudio`；出现 `LightDB-Studio.exe`、`_internal`、`unins000.exe` |
| 安装后 `--smoke` | **0** | offscreen + suppress-first-run，程序可启动并正常退出 |
| 静默卸载 | **0** | `unins000.exe` 完成 |
| 安装目录清理 | **True** | `D:\LightDBStudio` 已删除 |

注：安装器脚本 `PrivilegesRequired=lowest`，普通用户可安装到自定义目录；本机 DSH
会话为 Low 完整性级别（无法写开始菜单/注册表），因此冒烟在管理员/正常会话执行。

## 发布校验（同一产物）

`python scripts/verify_release.py --release-dir dist/release` →

```
PASS=12 FAIL=0 WARN=2 SKIP=2 -> PASS
WARN: 安装包未签名（SmartScreen 风险）；无证书，按需求忽略签名
```

- manifest 4.0.0 / zip SHA256 / 大小 / updater 解析 / check_for_update /
  installer artifact 哈希 / installer 版本 全部 PASS
- SHA256SUMS 未生成（SKIP，manifest 内已含 sha256，可选 `--write-sums`）

## 结论

**Windows 安装/安装后启动/卸载 全链路 PASS**（退出码 0/0/0，目录清理确认）。
唯一未做项为代码签名（无证书，按用户要求忽略；SmartScreen 可能提示未签名）。
