# 升级与回滚手册（用户版）

> 原则：**先备份，再升级；任何升级都必须能在 30 分钟内回滚。**

## 1. 升级前检查清单

- [ ] 已做全量备份并**演练过恢复**（`docs/BACKUP_RESTORE_GUIDE.md`）
- [ ] 记录当前版本：`LightDB-Studio.exe --version`（或设置页/日志首行）
- [ ] 记录数据目录路径与配置文件位置（`%USERPROFILE%\.lightdb_studio`）
- [ ] 阅读新版本 Release Notes 的“升级说明/已知问题”
- [ ] 若跨大版本：确认数据格式迁移要求（如 BTree v2/v3 迁移）

## 2. 升级步骤（Windows 安装包）

1. 停止 LightDB 服务；确认无写入（Studio 断开所有连接）。
2. 备份数据目录（见手册）。
3. 运行新版安装包：`LightDB-Studio-<new>-Setup.exe`；同一安装目录会覆盖安装。
4. 首次启动后确认：
   - 版本号正确；
   - 数据库可连接、表数量/行数抽查一致；
   - Studio 可打开表数据/设计器；
   - 备份中心能列出旧备份。
5. 观察 24 小时：无异常后再删除旧安装包。

## 3. 回滚步骤（30 分钟内）

1. 停止服务。
2. 卸载新版（控制面板/`unins000.exe /VERYSILENT`）。
3. 安装旧版安装包（保留旧版安装包！发布时同时保留上一版）。
4. 若新版升级过数据格式（跨大版本）：
   - 用升级前的备份恢复数据目录；
   - 或使用对应迁移脚本回退（仅当文档明确支持时）。
5. 启动旧版，抽查数据。

## 4. 版本策略

- 安装包只保留最近 **2 个**版本（当前 + 上一版），GitHub Release 永久保留
- 升级失败时优先“回滚 + 保留现场日志”，不要在故障现场反复升级
- 跨大版本升级前，先用**副本数据目录**演练一次

## 5. 自动化辅助脚本

```powershell
# 干净安装 → 冒烟 → 重装（模拟升级）→ 备份/恢复 → 卸载 全流程验收
powershell -ExecutionPolicy Bypass -File scripts\clean_install_acceptance.ps1 `
  -Installer D:\lightdb_project\dist\installer\LightDB-Studio-Setup-4.0.0.exe `
  -WorkDir D:\lightdb_acceptance
```

脚本输出每一步的 exit code 与日志路径；任一失败即停止并保留现场，便于回滚。

## 6. 已知升级风险

- 未签名安装包在 SmartScreen 下需“更多信息 → 仍要运行”
- 新版可能改变数据页格式（见 `docs/MIGRATION_BTREE_V2.md`）：跨大版本需迁移
- 配置（`connections.json`/`settings.json`）向后兼容，但降级时新字段会被忽略
