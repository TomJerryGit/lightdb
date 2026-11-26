# 干净环境验证（单机无第二台电脑的做法）

> 目标：验证 **干净安装 → 冒烟 → 升级/重装 → 备份/恢复 → 回滚 → 卸载** 全流程。
> 单机、无预算时按下面优先级选择方案。

## 方案 A（推荐）：Windows Sandbox（一次性、最接近干净机）

要求：Windows 10 Pro/Enterprise（1903+）、CPU 虚拟化开启、管理员权限一次性启用、重启一次。

```powershell
# 管理员 PowerShell（只需执行一次）
Enable-WindowsOptionalFeature -Online -FeatureName "Containers-DisposableClientVM" -All
# 重启电脑
```

创建 `lightdb_acceptance.wsb`（放在项目根，双击启动）：

```xml
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>D:\lightdb_project</HostFolder>
      <SandboxFolder>C:\lightdb_project</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>D:\lightdb_acceptance</HostFolder>
      <SandboxFolder>C:\lightdb_acceptance</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell -ExecutionPolicy Bypass -File C:\lightdb_project\scripts\clean_install_acceptance.ps1 -Installer C:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe -WorkDir C:\lightdb_acceptance</Command>
  </LogonCommand>
</Configuration>
```

- Sandbox 关闭后一切消失，天然满足"干净机 + 可重复"
- 结果（`summary.json`/日志）写入映射目录 `D:\lightdb_acceptance`，关沙箱后仍可查看

## 方案 B：Hyper-V 虚拟机（需要 Windows ISO）

要求：Win10 Pro/Enterprise、管理员、Hyper-V 启用、**需一个 Windows ISO**（可用微软官方
免费评估版）。没有 ISO 时不要走这条。

```powershell
# 管理员 PowerShell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All
# 重启后：
# Hyper-V 管理器 → 新建虚拟机 → 安装 Windows → 把项目目录/安装包拷进 VM
# 在 VM 内运行 scripts\clean_install_acceptance.ps1
```

## 方案 C（无虚拟化/无管理员时的降级验证，不等于干净机）

在当前机器上用**独立安装目录 + 独立数据目录 + 独立 HOME** 执行同一脚本，
可以得到"流程正确性"验证，但不能发现"其他软件影响/系统组件缺失"类问题。
脚本运行成功后在验收记录中标注：`验证级别：宿主隔离目录（非 VM）`。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\clean_install_acceptance.ps1 `
  -Installer D:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe `
  -WorkDir D:\lightdb_acceptance
```

## 验收判定（全部必须通过）

| 步骤 | 通过条件 |
|---|---|
| 静默安装 | exit 0，安装目录存在 exe |
| 安装后冒烟 | `LightDB-Studio.exe --smoke` exit 0 |
| 服务+连接 | 启动单机服务、pymysql 连接成功、建表/插入/查询正确 |
| 全量备份 + 恢复 | 备份成功；恢复到 staging 后行数/校验通过 |
| 重装（升级模拟） | 覆盖安装 exit 0，原数据目录仍可读、行数一致 |
| 回滚（卸载+重装） | 卸载 exit 0；重装后数据可读 |
| 卸载 | exit 0，安装目录消失、无残留服务/进程 |

失败即停：脚本保留 `WorkDir` 现场并返回非 0；**先回滚、后排查**。

## 实测记录（2026-11-26）

- **DSH 受限会话内**运行本脚本：安装返回 exit 0 但安装目录不存在（受限会话无法写
  注册表/开始菜单，即使带 `/NOICONS`）；后续冒烟/备份/恢复步骤因此失败。
  这是**运行环境限制**，不是安装包缺陷。
- **管理员会话**下同一安装包此前已验证：install/smoke/uninstall = 0/0/0（见
  `docs/RELEASE_2026-11-29_WAVE3.md` 或最新发布记录）。
- 结论：本脚本请**在普通 PowerShell（非 DSH 受限会话）或 Windows Sandbox/VM 内运行**。

## 验收结果（2026-11-26，宿主隔离目录）

**PASS（15/15）**：安装→冒烟→服务/探针→备份→恢复→重装→回滚→卸载全流程通过，
安装包 SHA256 973E0B5B...F98D0。详见 docs/CLEAN_INSTALL_ACCEPTANCE_2026-11-26.md。
真机干净环境（Windows Sandbox/VM）待启用虚拟化后复跑。
