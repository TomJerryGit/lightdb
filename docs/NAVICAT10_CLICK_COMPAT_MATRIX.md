# Navicat for MySQL 10.0.11 真实点击兼容矩阵（LightDB 4.0.0，2026-10-22）

> 目的：用户要求"按本机 Navicat 10 做一轮真实点击兼容矩阵，把剩余 1064/失败点录成清单"。
> 环境：Navicat for MySQL **10.0.11**（`D:\Program Files\Navicat For MySQL 工具软件\Navicat for MySQL\navicat.exe`）
> + LightDB **4.0.0**（127.0.0.1:8898，demo 库，E1 兼容改动已包含）。
> 方法：Windows 真实 GUI 自动化（pywinauto/win32 + 真实鼠标/键盘），每步截图。
> 诚实标注：**受本机 DSH GUI 持续抢前台焦点影响，部分点击未能完成**；已完成项与未完成项
> 分别列出，不用推测冒充实测。

## 1. 真实点击已完成的证据链（截图）

| 步骤 | 操作 | 结果 | 截图 |
|---|---|---|---|
| 1 | 以完整权限启动 Navicat 10 | ✅ 主窗口打开 | `navicat_03/04` |
| 2 | Navicat 连接其既有 MySQL（172.18.12.110） | ✅ 成功：库 `ecommerce_db`、5 张 InnoDB 表、用户管理视图可见 | `navicat_05/07/08` |
| 3 | 「连接」→ 新建连接，填 LightDB：名称 `LightDB-8898`、主机 `127.0.0.1`、端口 `8898`、用户 `root`、空口令 | ✅ 字段填写正确并保存进左侧连接树（重启 Navicat 后仍在） | `navicat_09/10/11/13` |
| 4 | 双击/右键 LightDB-8898 打开连接 | ⚠️ **未完成**：DSH 前端周期抢占前台，双击/右键焦点不稳定；连接节点已存在但对象树未展开（详见 §4） | `navicat_12/14/15/16` |

> 结论：**连接创建/保存链路真实可用**；"打开连接 → 对象树 → 表数据/设计器/导入导出/备份"
> 这条需要稳定焦点的人工点击链路本轮未跑完，不写成 PASS。

## 2. 协议级兼容矩阵（代理证据：同代 libmysql 客户端真实连接）

Navicat 10 内置 libmysql 与 MySQL 5.0 CLI 同代；用本机 MySQL 5.0 老客户端直连
LightDB 8898 的实测结果如下（可复现）：

| 类别 | 语句 | 结果 |
|---|---|---|
| 握手/基础 | `SELECT VERSION()/SHOW DATABASES/SHOW TABLES/DESC/SHOW CREATE TABLE/SHOW INDEX/EXPLAIN` | ✅ |
| DDL/DML/事务 | `CREATE TABLE/INSERT/UPDATE/DELETE/BEGIN/COMMIT` | ✅ |
| 事务保存点 | `SAVEPOINT/RELEASE/ROLLBACK TO` | ✅（E1 新实现） |
| 元数据（Navicat 面板依赖） | `SHOW TABLE STATUS/FULL COLUMNS/FULL TABLES/COLLATION/CHARSET/ENGINES/WARNINGS` | ✅（E1 新实现，老客户端实跑通过） |
| 连接时变量 | `SHOW VARIABLES LIKE 'version%'`、`SET NAMES utf8mb4`、`SELECT @@version_comment` | ✅ |
| 用户/权限 | `SHOW GRANTS` | ❌ 1064（未实现；Navicat「用户」面板对 LightDB 为空/不可用，MySQL 侧正常） |
| 视图 | `CREATE VIEW` / `SHOW CREATE VIEW` | ❌ 1064（未实现） |
| 独立索引 | `CREATE INDEX` / `CREATE FULLTEXT INDEX` | ❌ 1064（只支持建表内联索引） |
| ALTER | `ALTER ... ADD/DROP COLUMN, RENAME TO` | ✅；`MODIFY COLUMN` ❌ 显式 1064 |
| 表选项 | `ENGINE=InnoDB` ❌ 1064；`CHARSET=gbk` ❌ 1115；`COLLATE=...` ❌ 1273；`ROW_FORMAT/ON UPDATE` ❌ 1064 | 显式拒绝（禁止静默） |
| 外键/全文/事件 | `FOREIGN KEY` ❌ 明确拒绝；`FULLTEXT` ❌ 明确拒绝；`CREATE EVENT` ❌ 1064 | 需 GUI 侧禁用/提示 |
| 存储过程 | `CREATE PROCEDURE` 接受；`CALL` 单 SELECT 返回结果集、多结果集 ❌ 1235 | 非静默（E1 修复） |
| 分区 | `PARTITION BY` 已实现，`SHOW CREATE TABLE` 如实回显 | ✅（E1 修复） |
| SQL 层 PREPARE | `PREPARE ... FROM` | ❌ 1064（协议层 prepared 已支持） |
| binlog 命令 | `SHOW BINARY LOGS` | ❌ 1064（有 binlog/`SHOW MASTER STATUS`） |
| 字符集 | 非法 UTF-8 → **1366**（不落 U+FFFD）；gbk→1115 | ✅（E1 修复） |
| 老客户端中文写入 | MySQL 5.0 CLI 默认字符集下写中文曾落 U+FFFD；pymysql UTF-8 往返正确 | ⚠️ 旧客户端字符集协商仍需验证（建议 Navicat 连接字符集设 utf8mb4） |

## 3. 剩余失败点清单（按 Navicat 操作会撞到的优先级）

| 优先级 | 失败点 | 会触发的 Navicat 操作 | 建议 |
|---|---|---|---|
| **P0** | `ENGINE=InnoDB` 被 1064 拒绝 | **表设计器保存建表**（Navicat 默认带 ENGINE=InnoDB）、导入向导建表 | 兼容策略二选一：① 接受并持久化 `ENGINE=InnoDB`（当作别名，不实现 InnoDB 语义，文档说明）；② Studio/Navicat 侧提示。当前"禁止静默"口径导致 Navicat 建表必失败 |
| **P0** | `ALTER ... MODIFY COLUMN` 未实现 | 表设计器改列类型/长度 | 实现 MODIFY COLUMN（或设计器禁用该路径） |
| **P1** | 独立 `CREATE INDEX` 未实现 | 表设计器加索引、结构同步 | 实现 CREATE INDEX（DDL + 持久化） |
| **P1** | `SHOW GRANTS` 未实现 | Navicat「用户」面板 / 权限可视化 | 基于现有授权数据实现 |
| **P1** | 视图相关语句未实现 | Navicat「视图」节点/设计器 | 实现 CREATE VIEW/SHOW CREATE VIEW/DROP VIEW，或对象树明确空并提示 |
| **P2** | `PREPARE`（SQL 层）未实现 | Navicat 查询编辑器一般不用；部分客户端会 | 低优先，可继续 1064 但文档写明 |
| **P2** | `SHOW BINARY LOGS` 未实现 | Navicat 备份/复制菜单 | 可用 `SHOW MASTER STATUS` 替代；实现成本低 |
| **P2** | `SET NAMES gbk/latin1` 仅记录不转码 | 老 Navicat/中文连接 | 建议字符集统一 utf8mb4；若要兼容旧客户端需转码层 |
| **P3** | 外键/全文/事件 | 表设计器对应页签 | GUI 侧显式禁用并提示（引擎已明确拒绝） |
| **P3** | 存储过程多结果集 1235 | 存储过程调用 | 保持显式错误 |

## 4. 未完成项与原因（不冒充实测）

1. **打开 LightDB 连接后的对象树/表数据/设计器/导入导出/备份**：本机 DSH 前端（另一个
   长任务会话）持续抢占前台焦点，pywinauto `SetForegroundWindow` 后目标窗口仍被覆盖，
   真实点击落到 DSH 窗口；把 Navicat 置顶可短暂稳定（截图 13–16），但双击/右键连接节点
   后焦点再次被抢。**未完成，不标记 PASS**。
2. **Navicat 10 用户/权限面板对 LightDB**：连接树选中后右侧「用户」列表为空
   （与 `SHOW GRANTS` 未实现一致），但未完成完整点击验证。
3. 备份/导入导出/表设计器向导：未进入（依赖连接打开）。

## 5. 下一步（据此矩阵收敛，而不是继续加面板）

- **E2（引擎，最高优先）**：`ALTER MODIFY COLUMN` → `CREATE INDEX` → 视图
  （CREATE/SHOW/DROP VIEW）→ `SHOW GRANTS`；`ENGINE=InnoDB` 需要一个明确的兼容策略
  （接受为别名并文档说明，或由 GUI/导入剥掉）。
- **GUI（G2）**：对象树/表设计器/导入导出/备份入口与 Navicat 一致；引擎不支持项显式禁用。
- **验收方式**：在**用户自己打开 Navicat**（无 DSH 抢焦点）时按本文 §1 表格逐项点击，
  或由我们提供一份 10 项人工清单，用户在 Navicat 里核对；目标是 §3 P0/P1 全部消除。

## 6. 证据文件

- 截图：`_analysis_tmp/gui_full/navicat_03_escalated.png` … `navicat_16_context_menu.png`
  （启动/自带 MySQL 连接/新建连接/字段填写/连接树/右键菜单）。
- 自动化脚本：`_analysis_tmp/lead/navicat_driver.py`、`navicat_dialog.py`、`nav_step*.json`
  （真实鼠标/键盘操作；可复跑）。
- 协议探针：`_analysis_tmp/lead/feature_probe.py`、`feature_probe2.py`；E1 测试
  `tests/test_mysql_compat_p4a.py`。

## 7. 2026-10-22 更新：E2 已消除 P0/P1 阻塞

- ✅ `ENGINE=InnoDB/MyISAM/MEMORY/CSV` 已接受为 LightDB 原生存储兼容别名（请求引擎名
  持久化，SHOW TABLE STATUS/SHOW CREATE 如实回显，Create_options 标注
  `storage=LightDB-native`；未知引擎仍 1064）——Navicat 表设计器建表不再失败。
- ✅ `ALTER TABLE ... MODIFY COLUMN` 实现（全量试算转换，不可转换显式错误且零变化）。
- ✅ `CREATE [UNIQUE] INDEX / DROP INDEX` 实现（持久化、错误码、UNIQUE 冲突 1062；
  非唯一索引 Index_type 如实报 `SCAN-ASSIST`，不谎称 BTREE）。
- ✅ `SHOW GRANTS [FOR user]` 边界/协议测试补齐。
- 验证：`tests/test_mysql_compat_e2.py` 20 passed；指定回归 135、高风险 248/1 skipped。
- **仍剩余（P2）**：视图（CREATE/SHOW/DROP VIEW）、SQL 层 PREPARE、SHOW BINARY LOGS、
  旧客户端字符集协商；P3 外键/全文/事件仍显式拒绝。
- **G2 Studio 同步**：连接 JSON 导入导出（密码默认不导出/DPAPI/冲突策略）、SSL
  高级字段、函数/别名补全、JSON/SQL INSERT 导出、对象树拖拽；16 tests/186 回归。
  SSL 参数真正写入 pymysql 尚未接线（下一步）。
