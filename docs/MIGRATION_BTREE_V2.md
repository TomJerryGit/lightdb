# 迁移：旧 B+Tree 页格式 → v2（uint32 键 → uint64 键）

**背景**：task-72 为支持复合主键，把主键 B+Tree 的键宽度从 `uint32` 扩到
`uint64`，叶子/内部页条目从 **12/8 字节** 变为 **16/12 字节**。因此
task-72 之前创建的 `.dat` 数据文件**不能**被新版本直接读取：新版本会在
启动时检测 `.btroot` 指向的 B+Tree 根页，发现缺少 v2 格式标记后**显式
拒绝启动**（不会按新格式误读旧页、静默返回错行/损坏索引）。

错误示例：

```
LegacyBTreeFormatError: 检测到旧版 B+Tree 页格式：表 'users' 的根页 2
（.../users.dat）缺少 v2 格式标记。... 请先备份数据目录，然后运行：
`python scripts/migrate_btree_format.py <旧数据目录> <新数据目录>` ...
```

## 迁移步骤（务必先备份）

1. **停止服务/关闭数据库进程**：迁移要求旧库是**正常关闭/checkpoint**
   后的状态。若上次是崩溃退出（`_meta.json` 有 `pending_ddl` 或仍有未重放
   WAL），请先从备份恢复旧目录，再重新正常启动一次旧版本完成 checkpoint，
   或直接用完整备份做逻辑恢复。
2. **备份旧数据目录**（复制整个目录，不要只复制 `.dat`）：
   ```powershell
   Copy-Item -Recurse data data_backup_pre_v2
   ```
3. **运行迁移脚本（旧目录只读，必须写到新目录）**：
   ```powershell
   python scripts\migrate_btree_format.py data data_v2
   # 目标目录已存在且非空时，确认备份后加 --force
   python scripts\migrate_btree_format.py data data_v2 --force
   ```
   脚本逐表：
   - 扫描旧堆页读取 `xmax=0` 的可见行（正常关闭后的当前数据）；
   - 用当前 v2 格式重建表：列定义、PRIMARY/UNIQUE/二级索引、CHECK、
     分区、表选项、AUTO_INCREMENT 计数；
   - 逐行插入并校验行数与主键集合；
   - 复制 `_meta.json` 中的用户/视图，以及 `_stored.json` / `_triggers.json`
     / `_events.json` / `root.password`（若存在）。
4. **用新目录启动服务**（配置里的数据目录指向 `data_v2`），抽查：
   - `SELECT COUNT(*)` 行数、主键点查/范围查询；
   - `SHOW CREATE TABLE` / `SHOW INDEX` / `SHOW COLUMNS`；
   - 写一条再删一条、重启后复验。
5. 确认无误后保留旧目录作为回滚备份；新版本**只**使用新目录。

## 迁移限制（脚本会显式报错，不会静默丢数据）

- 旧格式只可能有**单列整数主键**；元数据若出现多列主键说明被手工改过，
  脚本拒绝迁移。
- 不迁移未完成/半提交的 WAL 重放状态；请先正常关闭旧库。
- 不迁移 binlog 文件（复制位点请重新初始化从库）；不迁移 `.free` 空闲页
  登记表（新库会重新生成）。
- 旧表 `.dat` 不存在（空表元数据）时按空表迁移；行载荷列数与元数据不一致
  会显式报错。
- 迁移是"读到新目录"的一次性操作，旧目录不被修改。

## 验证旧格式检测（无需迁移即可确认）

直接启动新版本指向旧数据目录，会抛出
`lightdb.db.errors.LegacyBTreeFormatError`（含表名/根页/迁移脚本路径）。
新库（v2）的根页页头 `reserved[21]` = 2；旧库为 0。
