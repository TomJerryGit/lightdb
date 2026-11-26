# 干净安装验收记录（2026-11-26，宿主隔离目录）

> 验证级别：**宿主隔离目录**（独立安装目录 + 独立数据目录 + 独立 HOME），
> 由管理员 PowerShell 执行。下一步真机干净环境可用 Windows Sandbox/Hyper-V
> （见 `docs/CLEAN_VM_TEST.md`），届时把本文件标记为"VM 验证"。

## 产物

| 项 | 值 |
|---|---|
| 安装包 | `dist/installer/LightDB-Studio-Setup-4.0.0.exe` |
| 大小 | 63,095,905 B |
| SHA256 | `973E0B5B4D7AD7394190DD49F240F2B6E68D58A99483A94CF5E71E73624F98D0` |
| 执行会话 | Administrator，elevated=True |
| 工作目录 | `D:\lightdb_acceptance` |

## 结果：**PASS（15/15 步）**

| 步骤 | 结果 | 证据 |
|---|---|---|
| precondition（文件/哈希） | ✅ | sha256 与构建记录一致 |
| install（静默安装） | ✅ | installer exit=0；Inno 日志 `Installation process succeeded` |
| install_artifact | ✅ | `App\LightDB-Studio.exe` 存在 |
| smoke（安装后 `--smoke`） | ✅ | exit=0 |
| server_start（单机服务） | ✅ | 端口 8901 监听（≤40s 轮询） |
| probe（建表/插入/查询） | ✅ | `rows=2` |
| snapshot_backup（全量快照） | ✅ | 1 表 / 2 行 / 5 文件 / 13.4 KB / 1.30s |
| snapshot_restore（恢复+自检） | ✅ | 1 表 / 2 行 / 5 文件 / 1.01s；`1 表行数/关键值/SHOW CREATE/自增/WAL=0 一致` |
| verify_restored_meta | ✅ | `_meta.json` + 数据文件存在 |
| reinstall_upgrade（覆盖安装） | ✅ | exit=0 |
| data_survives_upgrade | ✅ | 数据目录完整 |
| rollback_uninstall（回滚卸载） | ✅ | exit=0 |
| rollback_reinstall（回滚重装） | ✅ | exit=0 |
| rollback_data_check | ✅ | 数据目录完整可读 |
| final_uninstall + appdir_removed | ✅ | exit=0，安装目录已消失 |

汇总文件：`D:\lightdb_acceptance\summary.json`（schema `lightdb-clean-acceptance/1`，`ok: true`）。

## 本轮修复的脚本问题（beta.157/158）

1. `Run` 用陈旧 `$LASTEXITCODE` → 安装失败被误报为 `exit=0`（改为真实 `ExitCode` + 失败提前退出）
2. 上一轮 `backup/restore/data` 残留导致快照失败 → 每轮开始自动清理
3. 固定 `Start-Sleep 8` 不够/误判 → 端口轮询 40s + 2s 稳定期
4. 探针/备份/恢复输出走 stderr 被 `$ErrorActionPreference='Stop'` 当异常 → 输出重定向到日志文件
5. 恢复校验从“目录存在”改为 `_meta.json` + 数据文件实物检查

## 使用建议

- **安装/卸载请用管理员 PowerShell**（本验收在 elevated 会话完成；普通用户会话未做系统验证）
- 卸载时会提示删除用户配置（`~/.lightdb_studio`）的选项；测试环境可忽略
- 真机干净环境（无其它软件干扰）请按 `docs/CLEAN_VM_TEST.md` 的 Windows Sandbox 方案复跑，
  通过后把本文件"验证级别"改为 `Windows Sandbox` 并附截图
