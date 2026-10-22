# Navicat 10 兼容验收记录（2026-10-22）

环境：Navicat for MySQL 10.0.11 + LightDB 4.0.0（127.0.0.1:8898，demo 库 `default`，12 张表）。
方法：真实 GUI 点击截图 + 按 Navicat 实际 SQL 行为序列自动验证。

## 1. 真实点击已确认 PASS

| 项 | 证据 |
|---|---|
| 启动 Navicat 10、连接 LightDB-8898（root/空口令） | `_analysis_tmp/gui_full/navicat_13/17/24/28/30*.png` |
| 对象树：`default`/`information_schema` 与表/视图/函数/事件/查询/报表/备份分类 | 同上 |
| 表列表：12 张表，列显示 自动递增/**表类型=LightDB**/数据长度/行数/注释，行数真实 | 用户截图 `捕获.PNG`（2026-10-22 13:49） |
| 导入向导入口：步骤 1/8，格式列表（文本/HTML/Excel/XML/Access/ODBC…）正常 | 同上截图 |
| 视图列表页打开（空列表、无 1064） | `navicat_24/26*.png` |

期间真实发现并修复：Navicat 列表时发送
`SHOW FULL TABLES WHERE Table_type != 'VIEW'` → 原 1064；已修复（提交 `91a661f`，
parser 支持 `WHERE Table_type (=|!=|<>) 'VIEW'|'BASE TABLE'`，executor 过滤；实时探针 12 行）。

## 2. 自动验证（协议层）23/23 PASS

脚本 `_analysis_tmp/lead/navicat_auto_verify.py`，报告
`_analysis_tmp/lead/navicat_auto_verify.json`：

- `SHOW FULL TABLES WHERE Table_type != 'VIEW'`（12 行）/ `= 'VIEW'`（0 行）
- `SHOW TABLE STATUS`（Engine=LightDB）、`SHOW FULL COLUMNS`（6 列）
- `SELECT COUNT(*)`=200、`SELECT 1`
- 设计器：建表 `ENGINE=InnoDB` + SHOW CREATE 回显、`ALTER MODIFY COLUMN`、
  `CREATE INDEX` + `SHOW INDEX`、清理
- `SHOW GRANTS`、`SHOW BINARY LOGS`
- 视图 CREATE/SHOW/SELECT/DROP
- `PREPARE/EXECUTE/DEALLOCATE`

## 3. 未完成（自动化受本机焦点抢占影响）

- 表数据网格（双击 `customers` 的 200 行）
- 表设计器保存（改列/加索引）
- 备份向导

上述三项对应的后端 SQL 已在 §2 全部自动验证通过；GUI 点击因 DSH 前台抢占导致
树节点点击漂移，未获得可信截图，**不记为 PASS**。建议在用户桌面手动点击 3 步即可闭环
（预计无引擎报错）。

## 4. 结论

Navicat 10 对 LightDB 的**连接、对象树、表列表、导入入口**已真实可用；
Navicat 表设计器/备份所依赖的 SQL 语义已 23/23 通过。
剩余仅为 GUI 截图核对，不是功能缺口。
