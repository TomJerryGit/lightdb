# 备份自动化（计划任务 + 每周恢复演练）

> 目标：让"每日备份 + 定期恢复演练"不依赖人记得——用 Windows 计划任务
> 调用 `scripts/backup_automation.py`，并把结果写入 `status.json` 与日志。

## 1. 一次手工验证（装任务前必须做）

```powershell
# 先停掉 LightDB 服务（备份要求数据目录静默）
python scripts\backup_automation.py run `
  --data-dir D:\lightdb\data --out D:\lightdb_backup --keep 14 --drill
# 期望：退出码 0，D:\lightdb_backup\status.json 中 ok=true、verify.ok=true
python scripts\backup_automation.py status --out D:\lightdb_backup
```

- 退出码 `2` = 检测到运行中的服务，备份拒绝执行（先停服）
- 退出码 `1` = 备份/校验失败，看 `<out>\logs\auto-YYYYMMDD.log`
- 每次备份目录：`full-YYYYmmdd-HHMMSS`（含 `manifest.json`）
- `--drill` 会立刻恢复到临时目录并做行数/关键值/WAL=0 自检
- 保留策略：只保留最新 `--keep` 份（`full-*` 且带 manifest），其余删除

## 2. 安装计划任务（当前用户，无需管理员）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1 `
  -DataDir D:\lightdb\data -BackupDir D:\lightdb_backup -Keep 14 `
  -DailyAt 02:00 -DrillDay Sunday -DrillAt 03:00
```

会创建两个任务（当前用户交互令牌 `/IT`，无需保存密码/管理员）：

| 任务 | 触发 | 动作 |
|---|---|---|
| `LightDBBackup-Daily` | 每天 `-DailyAt` | `backup_automation.py run --drill --keep N`（备份+校验+保留） |
| `LightDBBackup-Drill` | 每周 `-DrillDay -DrillAt` | `backup_automation.py drill`（对最新备份恢复演练） |

任务实际调用 `<BackupDir>\run_backup.cmd` / `run_drill.cmd`，输出追加到
`<BackupDir>\logs\task-YYYYMMDD.log`。

查看/卸载：

```powershell
schtasks /Query /TN LightDBBackup-Daily /FO LIST
schtasks /Query /TN LightDBBackup-Drill /FO LIST
powershell -ExecutionPolicy Bypass -File scripts\install_backup_task.ps1 -Uninstall
```

## 3. 状态与告警（零依赖）

- `status.json`（每次运行覆盖）：`ok` / `backup` / `verify` / `kept` / `deleted`
  / `error` / `at`
- 日志：`<out>\logs\auto-YYYYMMDD.log` 与 `task-YYYYMMDD.log`
- 告警：计划任务退出码非 0（可在"任务计划程序 → 历史记录"看到，或用一个
  包装脚本把日志转发到邮件/webhook——本项目不内置外部通知，保持零依赖）

## 4. 已知限制（如实）

- **备份需要数据目录静默**：任务运行时会话内若数据库在跑，`run` 返回 2 并
  不产生备份。建议把每日任务安排在维护窗口，或先停止服务；不要用"在线热备"
  描述本功能。
- 计划任务用 `/IT`（当前用户登录时运行）；机器重启后需用户登录才会触发。
  若需"开机即跑"，请改用服务账户并自行评估权限（未内置）。
- 没有自动清理旧备份之外的归档策略（如异地拷贝/对象存储），需要用户自行
  在 `BackupDir` 之上做同步。
- `drill` 验证的是"备份可恢复且逐表校验通过"，不验证业务层数据语义。

## 5. 相关文档

- 手工备份/恢复细节：`docs/BACKUP_RESTORE_GUIDE.md`
- 持久性档位：`docs/DURABILITY_MATRIX.md`
- 发布前检查：`docs/DURABILITY_MATRIX.md` §6.2

> 实测补充：计划任务实际执行的包装命令（
un_backup.cmd / 
un_drill.cmd）已用
> D:\lightdb\data（2 表 / 250 行）实测：备份 1.2s、恢复校验 0.9s、演练 PASS。
> **创建计划任务需要在普通（非 DSH 沙箱）PowerShell 中运行**：沙箱会话对 Task
> Scheduler 无权限（schtasks 报 Access is denied），脚本会给出提示并以 exit 3 结束。
