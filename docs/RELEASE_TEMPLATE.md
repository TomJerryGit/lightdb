# LightDB Release Notes 模板

> 复制到 `docs/RELEASE_NOTES_<version>.md`，发布 GitHub Release 时同步粘贴。

## LightDB <version>（<date>）

**类型**：稳定版 / 修复版（二选一）
**下载**：`LightDB-Studio-<version>-Setup.exe`（安装包）、`LightDB-Studio-<version>-win64.zip`（便携）
**SHA256**：见 Release 附件或 `dist/release/release_manifest.json`

### 亮点

- （1–3 条，用户可感知的变化）

### 修复

- （从 CHANGELOG 提炼的缺陷修复；标注 issue 编号）

### 升级说明

- 升级前**必须备份数据目录**（备份/恢复手册见 `docs/BACKUP_RESTORE_GUIDE.md`）
- 从旧版本升级：卸载旧版 → 安装新版 → 指向原数据目录；
  跨大版本需先看 `docs/MIGRATION_BTREE_V2.md` 等格式迁移说明
- 回滚步骤见 `docs/UPGRADE_ROLLBACK_GUIDE.md`

### 已知问题 / 限制

- 见 `docs/LIMITS.md`（单机、≤20GB、≤10 并发写、无 HA、未签名等）
- 本版本新增的已知问题：（列出）

### 校验

```powershell
Get-FileHash .\LightDB-Studio-<version>-Setup.exe -Algorithm SHA256
Get-FileHash .\LightDB-Studio-<version>-win64.zip -Algorithm SHA256
```

### 免责声明

免费软件、按现状提供、不附带担保；**请勿用于无法承受数据丢失的场景**，并自行定期备份。
