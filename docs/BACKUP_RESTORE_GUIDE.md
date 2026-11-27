# 备份与恢复手册（用户版）

> 适用于单机部署。**备份不是可选项**：LightDB 免费且不提供数据恢复服务。

## 1. 备份前检查

1. 确认数据目录路径（启动参数 `--data-dir`，默认 `data`）。
2. **备份需要数据目录静默**：先停止对数据库的写入 / 停止服务。
   - 全量二进制快照：服务运行中会被拒绝（检测到监听端口即拒绝）。
   - 逻辑 dump：可在只读时执行，但建议同样停写以保证一致性。
3. 预留 ≥ 数据量 2 倍的空闲空间。

## 2. 全量备份（推荐）

```powershell
# 停止服务后执行
python scripts/snapshot_backup.py --data-dir D:\lightdb\data --out D:\backup\full-20260101
```

- 生成二进制快照目录（含 `manifest.json`、SHA256、每表行数）
- 校验：`python scripts/snapshot_restore.py --snapshot D:\backup\full-20260101 --target D:\verify\restore --verify-only`
- 建议频率：每日一次（低峰期）；保留最近 7–30 份

## 3. 增量备份链（可选，节省空间）

```powershell
# 初始化基线
python scripts/backup_chain.py init --data-dir D:\lightdb\data --out D:\backup\chain
# 之后每次增量（同样要求静默）
python scripts/backup_chain.py incr --chain-dir D:\backup\chain
# 保留最近 7 个增量（自动 rebase 基线）
python scripts/backup_chain.py prune --chain-dir D:\backup\chain --keep 7
```

## 4. 恢复流程（演练过的步骤）

1. **先恢复到 staging 目录验证**（绝不要直接覆盖生产目录）：
   ```powershell
   python scripts/snapshot_restore.py --snapshot D:\backup\full-20260101 --target D:\restore_staging
   # 或增量链：
   python scripts/backup_chain.py restore --chain-dir D:\backup\chain --latest --target D:\restore_staging
   ```
2. 检查输出中的**逐表行数 / 关键值 / SHOW CREATE / WAL=0** 校验结果。
3. 停止服务，把原数据目录改名保留（如 `data_old_20260101`），将 staging 目录设为新 `--data-dir`。
4. 启动服务，抽查数据后，旧目录保留至少 7 天再删除。

## 5. 恢复演练（建议每周一次）

```powershell
# 用最新备份恢复到临时目录并自动校验；失败应告警
python scripts/restore_drill.py --backup D:\backup\full-latest --target D:\restore_drill --json
```

- 演练失败 = 备份不可用，必须立即排查，而不是等到真出事
- 记录：演练时间、备份版本、校验结果、耗时（写入 `docs/` 下的运维日志）

## 6. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| “检测到运行中的服务，拒绝备份” | 服务在跑 | 停止服务后重试（或确认只读后使用逻辑 dump） |
| 恢复报版本不兼容 | 备份来自不同大版本 | 用对应版本恢复，或按迁移文档升级后再恢复 |
| 恢复报 SHA256 不符 | 备份文件损坏 | 换更早的备份；检查磁盘 |
| 恢复目标非空 | 目标目录已有数据 | 用新目录，或确认后 `--force`（谨慎） |

## 7. 必须知道的限制

- 没有自动备份调度服务：需要你自己用 Windows 计划任务调用上述命令
- RPO/RTO 由你的备份频率决定（建议每日全量 + 每小时增量，RPO ≤ 1 小时）
- **本软件不承担数据丢失责任**（见 `docs/POSITIONING.md` 免责声明）


## 8. 自动化（计划任务 + 每周演练）

手工流程可以完全自动化：每日备份+校验+保留、每周恢复演练。见
docs/BACKUP_AUTOMATION.md（scripts/backup_automation.py +
scripts/install_backup_task.ps1）。装任务前先按本文档做一次手工备份/恢复验证。
