# 本地全量 GUI + 数据库功能验证（2026-10-20）

> 目的：按用户要求，在**本机真实运行** LightDB 数据库服务 + LightDB Studio 桌面端，
> 对数据库功能与 UI 功能做一次端到端全量验证，并把发现的问题如实登记。
> 本文所有数字都来自本机实际运行（证据见文末路径）；未入库的截图/报告位于
> `_analysis_tmp/gui_full/`（工作区未跟踪目录）。

- 日期：2026-10-20
- 环境：Windows 10 x64 / Python 3.11.9 / 8 逻辑核
- 代码：`main` @ `16bcd1d`（含 `92b033c` Cython 4A.4、`16bcd1d` dispatch 默认开启
  + 本次验证修复）
- 数据库服务：`python -m lightdb.main --mode single --host 127.0.0.1 --port 8898
  --data-dir _analysis_tmp/gui_full/data --allow-empty-password`（本地 demo，
  `--allow-empty-password` 仅用于本机演示，禁止生产使用）
- 客户端：Studio（PySide6，offscreen 渲染产出截图；另用
  `QT_QPA_PLATFORM=windows` 验证真实 Win32 窗口可创建）

## 1. 结论

| 维度 | 结果 |
|---|---|
| 数据库功能矩阵（独立 pymysql 脚本） | **34 / 34 通过，0 失败**（3.68 s） |
| GUI 功能场景（Studio 真实 UI 驱动） | **45 / 45 通过，0 失败** |
| Studio 截图 | **77 张**（offscreen）+ 1 张 Windows 平台主窗口 |
| 弹窗/对话框捕获 | **25 个**对话框 + 3 个消息框 |
| 已知引擎限制 | 2 条（显式报错，不静默错答） |
| Studio UI 缺陷 | 3 条（已附证据，未在本轮修复） |

## 2. 数据库中验证内容（34 项）

覆盖并通过：

- **认证/权限**：root 空口令本地 demo 登录；`demo_user` 密码登录；错误口令/未知用户
  返回 1045；`demo_user` SELECT 允许（200 行）、UPDATE 拒绝 1142。
- **DDL/元数据**：多类型 CREATE TABLE、SHOW TABLES、DESCRIBE（7 列）。
- **CRUD**：INSERT/SELECT（含 NULL/中文/负数/DOUBLE）、UPDATE、DELETE、
  ORDER BY + LIMIT + OFFSET。
- **约束/类型错误**：主键重复 1062、NOT NULL 1048、UNIQUE 重复 1062、
  INT 越界 1264、VARCHAR 超长 1406、非法 DATETIME 1292。
- **事务**：显式事务 ROLLBACK 恢复被删行并撤销插入；COMMIT 持久化。
- **查询**：JOIN + GROUP BY + COUNT/SUM/AVG、HAVING、标量子查询（已知限制）、
  窗口函数 ROW_NUMBER、CTE、UNION/UNION ALL（含协议层 + union 级
  ORDER BY/LIMIT）。
- **触发器/复制状态**：SHOW TRIGGERS 可见；SHOW MASTER STATUS 返回 binlog 文件名/
  位点；SHOW SLAVE STATUS 如实返回未配置复制。
- **批量/延迟**：500 行批量 INSERT 190 ms、主键点查平均 1.4 ms。

### 2.1 已知引擎限制（显式报错，不静默错答）

1. **标量子查询作比较右值**：`WHERE col > (SELECT AVG(col) FROM t)` 返回 1105
   `'>' not supported between instances of 'int' and 'str'`；分析报告中已列为已知
   限制（显式失败，不做错答）。
2. **触发器体 NEW 列的数值表达式**：`SET NEW.amount = ABS(NEW.amount)` 中
   `NEW.amount` 以字符串参与求值 → 1105 `bad operand type for abs(): 'str'`。
   因此该触发器存在期间对 `orders` 的 INSERT（正数也会）失败；属引擎限制，
   不是 Studio UI 缺陷。

## 3. GUI 验证内容（45 场景 / 77 截图）

覆盖并通过（每项都有截图，见 `gui_report.json` 的 `scenarios`）：

- 主窗口状态（6 菜单 / 62 动作 / 2 dock / 6 右侧惰性 tab / 已连接状态）
- 对象树展开（default + information_schema、11 张表、5 个文件夹）、刷新
- 查询编辑器：SELECT orders 1200 行（列名/行数）、LIMIT/OFFSET 10 行
  （id 6–15）、ORDER BY DESC top5、GROUP BY + SUM/AVG、JOIN、子查询（116 行）
- DDL/CUD：CREATE/INSERT/UPDATE/DELETE + 查询验证、错误 surface（1146/42S02、
  1064/42000 完整错误码+SQLSTATE）、EXPLAIN
- 多查询 tab、结果表排序（QTest 点击表头）、DataEditor（200 行×6 列）
- 表设计器、CSV 导入（3 行）/导出（797 B，26 行）、图表、ER 图、快照、
  SQL 格式化、数据质量（0 issues）、健康度/告警/调度/资产/脱敏/团队/云同步/
  插件/审计/血缘/DB 对比/迁移/性能面板、SQL 审查、连接/设置/关于
- 报表管理：HTML/CSV/XLSX 三格式落盘（xlsx 6 行）
- 主题 dark/light 两套截图、工具栏动作冒烟、AI/Agent/协作/目录惰性面板构建
- UNION / UNION ALL 经 GUI 真实路径复核通过（对应协议层修复）
- `QT_QPA_PLATFORM=windows`：真实 Win32 窗口创建成功、可见、标题
  `LightDB Studio v1.0`、连接到 8898；截图 `windows_platform_main_window.png`

## 4. 本轮验证发现并修复的真实缺陷（提交 `16bcd1d`）

1. **UNION 经 MySQL 文本协议返回 OK/0 行**：`net/server.py` 的
   `_send_execution_result` 只登记 `cmd == 'SELECT'`，`UNION` 落到"其他"分支回
   OK 包；Executor 内部执行正常。修复：把 `UNION` 纳入结果集序列化分支。
2. **UNION 级 ORDER BY/LIMIT 被静默丢弃/只作用于最后一段**：parser 把末尾
   ORDER BY/LIMIT/OFFSET 提升到 union 层，executor `_cmd_union` 合并后统一用
   `_finish_select` 应用。
3. **并发 DDL + 提交/回滚时 `dictionary changed size during iteration`**：
   `Database.commit/commit_statement/abort` 等遍历 `self.tables` 改为
   `list(...)` 快照（13 处 values + 2 处 items）。

新增回归测试 `tests/test_wire_union.py`（2 例，真实 MySQL 协议/多列/全局
ORDER BY+LIMIT）。

## 5. Studio UI 缺陷（本轮只登记，未修）

1. **结果表数值列按字符串字典序排序**：ResultTable 用 `QTableWidgetItem` 文本
   排序，`88.5` 会排在 `270.5` 之前；排序动作本身可用（表头点击有效）。
2. **结果表无过滤 UI**：仅支持排序；原 `data_grid_enhanced.column_filter` 已随
   死代码删除。
3. **快照同名不去重**：`create_snapshot()` 允许同名重复，`list_all()` 出现 2 个
   同名条目；`remove()` 只删首个匹配（SnapshotManager 无唯一性校验），同名快照
   会累积重复项。

## 6. 复现命令

```powershell
# 1) 起本地 demo 数据库（数据目录可换；--allow-empty-password 仅本地 demo）
python -m lightdb.main --mode single --host 127.0.0.1 --port 8898 `
  --data-dir _analysis_tmp/gui_full/data --allow-empty-password

# 2) 灌入演示数据（customers 200 / orders 1200 / products 20 / events 50 + 触发器/用户）
python _analysis_tmp/gui_full/seed_demo.py

# 3) 数据库功能矩阵（34 项）
python _analysis_tmp/gui_full/db_verify.py

# 4) Studio 全量 GUI 驱动（offscreen；45 场景 + 截图）
python _analysis_tmp/gui_full/gui_driver.py

# 5) 真实 Win32 窗口探针（可选）
python _analysis_tmp/gui_full/windows_probe.py
```

## 7. 证据路径

- 汇总：`_analysis_tmp/gui_full/gui_summary.md`
- 结构化报告：`_analysis_tmp/gui_full/gui_report.json`、`db_report.json`
- 截图：`_analysis_tmp/gui_full/shots/`（77 张）、
  `_analysis_tmp/gui_full/windows_platform_main_window.png`
- 驱动日志：`_analysis_tmp/gui_full/driver.log`、`server.log`
- 脚本：`seed_demo.py`、`db_verify.py`、`gui_driver.py`、`windows_probe.py`

> 说明：以上证据目录为工作区未跟踪文件（不入库）；仓库内只提交本报告与回归测试。
