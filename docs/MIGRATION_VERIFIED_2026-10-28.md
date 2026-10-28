# BTree v2 迁移实测记录（2026-10-28）

背景：复合主键支持把 B+Tree 物理键从 uint32 扩展为 uint64，页格式升级为 v2；
旧 `.dat` 页条目布局不同，**不可直接读取**。

## 1. 旧格式检测（启动即显式失败，不挂起）

用真实旧演示库 `_analysis_tmp/gui_full/data`（13 表，BTree v1）启动服务端：

```
lightdb.db.errors.LegacyBTreeFormatError: 检测到旧版 B+Tree 页格式：
表 'customers' 的根页 1（...\customers.dat）缺少 v2 格式标记。
Task-72 将主键从 uint32 扩为 uint64 后页条目大小已变化，旧数据文件不能按新格式读取。
请先备份数据目录，然后运行：`python scripts/migrate_btree_format.py <旧数据目录> <新数据目录>`；
迁移会逐表重建为新格式并校验行数/主键/索引。详见 docs/MIGRATION_BTREE_V2.md。
```

进程退出、不监听端口（此前旧代码会在查询时挂起/读数错误）。

## 2. 迁移实测（真实旧演示库：13 表）

命令：
```powershell
python scripts\migrate_btree_format.py _analysis_tmp\gui_full\data _analysis_tmp\gui_full\data_v2
```

输出（节选）：
```
迁移完成：
  - orders: 1200 行, 1 索引, 校验=OK
  - products: 20 行, 1 索引, 校验=OK
  - events: 50 行, 1 索引, 校验=OK
  - vf_all_types: 2 行, 1 索引, 校验=OK
  - vf_unique: 4 行, 2 索引, 校验=OK
  - vf_types: 0 行, 1 索引, 校验=OK
  - vf_dept: 3 行, 1 索引, 校验=OK
  - vf_emp: 5 行, 1 索引, 校验=OK
  - vf_bulk: 500 行, 1 索引, 校验=OK
  - gui_import_target: 3 行, 1 索引, 校验=OK
  - parent_fk: 1 行, 1 索引, 校验=OK
  - customers: 200 行, 1 索引, 校验=OK
  - t: 1 行, 1 索引, 校验=OK
  - 系统元数据: 不迁移
  - 视图: 0 行
新数据目录: _analysis_tmp\gui_full\data_v2（旧目录只读不动）
```

## 3. 迁移后服务端实测（端口 8899，pymysql）

```
tables: 13
  customers 200 / orders 1200 / products 20 / events 50 / vf_bulk 500 / ...
customers rows: 200
sample: ((1, 'Customer 001'), (2, 'Customer 002'))
has PK: True
write/read ok: 2      # 新建表→双行插入→BETWEEN 查询→删除
PROBE OK
```

- 行数与迁移前一致；主键保留；读写/建表/删除正常；迁移生成随机 root 口令写入 `root.password`（本次为演示库）。
- 旧格式检测 + 迁移 + 迁移后读写均通过真实数据验证。

## 4. 二进制 PREPARE（同批）

- 协议层测试 `tests/test_binary_prepare.py` + 既有 prepared 套件：22 passed（2 skip：PyMySQL 2.2.8 无 `prepared=True` 游标、无 mysql-connector）。
- 覆盖 PREPARE OK 字段/参数类型、int/string/NULL/NEWDECIMAL/DATETIME 绑定、1243/1210/1064 错误码、RESET、连接隔离、3000 行大结果集、与文本 PREPARE 一致。
- 本批测试：`test_binary_prepare.py + test_btree_migration.py` = 10 passed / 1 skipped。

## 5. 边界

- 迁移要求旧库正常关闭/checkpoint；不迁移 binlog；旧格式只可能有单列整数主键。
- `COM_STMT_SEND_LONG_DATA` / 游标 FETCH 未实现（显式 1235）。
- 迁移后的目录可直接被 v2 服务端加载（本次已验证）。
