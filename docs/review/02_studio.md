# LightDB Studio（PySide6 图形端）功能与质量评估

> 评估对象：`lightdb_studio/**` —— 130 个 `.py` 文件、**37 280 行**（含空行，`(Get-Content f).Count` 实测合计）
> 环境：Windows / Python 3.11.9 / PySide6 6.6.0 / pymysql 已装 / **本机无 MySQL 服务、无任何可用库凭证**
> 标注约定：**【实测】**=本次真跑；**【阅读】**=代码静态阅读（附 `文件:行`）；**【推断】**=由前两者推出
> 本次未修改 `lightdb_studio/**` 源码或测试；临时探针脚本 `_studio_probe*.py`、`_studio_*.py` 用后已删。

## 1. 模块清单与规模

按功能分组（行数含空行，占比按 37 280 行）：

| 功能分组 | 文件 | 行数 | 占比 |
|---|---|---|---|
| 数据平台「愿景」模块（数据湖/网格/ETL/联邦/数字孪生/自治…） | 21 | 8 302 | 22.3% |
| 质量/治理/监控/性能 | 19 | 5 611 | 15.1% |
| AI（对话/Agent/SQL 生成/微调/ReAct/NL-BI） | 11 | 4 182 | 11.2% |
| 导入导出/报表/图表/仪表盘/调度 | 9 | 3 423 | 9.2% |
| 数据目录/血缘/数据地图/资产 | 10 | 3 121 | 8.4% |
| 协作/团队/云同步/审查 | 13 | 3 054 | 8.2% |
| 快照/迁移/插件/工作流/环境 | 12 | 2 570 | 6.9% |
| 主窗口外壳（主题/环境条/设置/历史） | 6 | 2 356 | 6.3% |
| 连接/对象浏览/表与数据编辑 | 7 | 2 074 | 5.6% |
| SQL 编辑器/执行/结果展示 | 13 | 1 792 | 4.8% |
| DB 访问层（`db/`） | 5 | 527 | **1.4%** |
| 入口/配置 | 3 | 235 | 0.6% |
| 未分类（`core/__init__.py`） | 1 | 33 | 0.1% |

主窗口 `ui/main_window.py` 单文件 1 256 行 / 68 个函数，是唯一的组装点；`core/` 63 文件 20 732 行（55.6%）。
全仓共 1 648 个函数、259 个类（AST 统计）。

**可达性【阅读+脚本】**：以 `lightdb_studio.app` 为根做 AST 导入图遍历，130 个模块中仅 **81 个可达（62.3%）**，22 283 行（59.8%）；
**49 个模块 / 14 997 行（40.2%）从入口不可达**，其中 20 个是 300~763 行的大型 `core/` 模块（`agent_react` 763、`etl` 656、`data_lake` 482、`sql_auto_optimizer` 479…）。
「可达」仅表示被 import；8 个 UI 模块（`dashboard` 425、`chart_view_enhanced` 551、`import_wizard` 469、`perf_panel` 260、`ai_panel` 197、`data_grid_enhanced` 157、`cell_viewer` 125、`multi_result` 79，共 2 263 行）虽被 import 但**从未被实例化**（见 §2 第 21/22 项）。

## 2. 功能盘点

判据：是否真的有实现 + 能否从 GUI 触达 + 是否真的读写数据库/落盘。**可用 8 / 部分可用 12 / 桩或死代码 7**（共列 27 项）。

| # | 功能 | 状态 | 证据 |
|---|---|---|---|
| 1 | 连接管理（新建/测试连接/持久化） | **可用** | `ui/connection_dialog.py:104-123` 真实 `pymysql.connect`；`config.py:89-106` 写 `~/.lightdb_studio/connections.json`（原子 replace）。**密码明文入库**（`config.py:19`、`connection_dialog.py:98`） |
| 2 | SQL 编辑器 + 执行 | **部分可用** | 真走 `DBConnection.query/execute`（`main_window.py:707-711`）；多语句切分（`main_window.py:740-776`，实测 5 组用例均正确，含字符串内分号/注释）；选中执行、EXPLAIN、F5/Ctrl+Enter 齐全。**但**：`autocommit=True`（`db/connection.py:40`）→ **无事务**（无 BEGIN/COMMIT 按钮，`commit/rollback` 仅出现在高亮词表）；**无结果分页**（`fetchall()` 全量入内存）；**查询在 GUI 线程同步执行**（§3-2）；**无自动补全**（`utils/sql_completer.py` 182 行无人调用） |
| 3 | 结果表格 | **可用** | `ui/result_table.py` 只读 QTableWidget，NULL/二进制/长文本降级显示真实现；>500 字符截断（:73-74）；全量内存 |
| 4 | 对象浏览器 | **部分可用** | `ui/object_tree.py` 惰性加载 + `db/metadata.py` 真实 `SHOW DATABASES/TABLES/COLUMNS`；右键 DROP/TRUNCATE 真执行（:364/:375）。**但** 存储过程/函数/分区用 `SHOW PROCEDURES`(:37)、`SHOW FUNCTIONS`(:44)、`SHOW PARTITIONS FROM`(:65)——MySQL 无这三条语句（本仓引擎 `lightdb/sql/executor.py:5545` 自述 `SHOW AUDIT LOG` 等是 LightDB 扩展）→ 连 MySQL 时这些目录恒为空且**异常被吞**（`object_tree.py:196-257` 共 5 处 `except: pass`）；`get_tables(db)` 的 `db` 参数被忽略（`db/connection.py:129`）；列信息树中不展示（只在双击时弹 SQL） |
| 5 | 表设计器（新建表） | **可用** | `ui/table_designer.py:206-229` `build_create_table` → 预览 → 真 `conn.execute` |
| 6 | 数据编辑器（网格增删改） | **可用【实测】** | 探针 3：假连接下 `reload` 真发 `SELECT`+`COUNT(*)`，页签 `1/2`→改每页 500→`1/1`，改值后 `modified={0:{'name':'changed'}}`，新增行计入 pending；无主键表拒绝提交（`data_editor.py:296-300`）；提交前弹 SQL 预览并逐条 `execute` |
| 7 | CSV 导入 / 导出 | **部分可用** | 导出真写 `utf-8-sig` CSV（`import_export.py:246-262`）。**导入结果统计是错的**：`import_export.py:183-186` 调 `execute_many` 后无视返回值并 `ok += len(batch)`，而 `execute_many` 从不抛异常、只返回 `(0, err)`（`db/connection.py:109-116`）→ 全表导入失败也会报「成功 N 行，失败 0 行」；且无事务 |
| 8 | 查询历史 / SQL 片段 | **可用** | `utils/history.py` 落盘 `~/.lightdb_studio/history.json`，`main_window.py:729/736` 每次执行都写全量文件；`core/snippets.py` 内置片段 + 持久化 |
| 9 | 报表（模板→执行→CSV/Excel/HTML） | **可用【实测】** | 模板落 `~/.lightdb_studio/reports/templates.json`（`core/reports.py:76-81`）；探针 5：CSV 19B / HTML 1 375B / XLSX 4 969B 全部真落盘；`ui/report_builder.py:271/297` 真跑 `conn.query(模板 SQL)`。**注意**：报表/仪表盘**不写**仓库根的 `dashboards/*.json`（那 4 个文件不来自 studio，studio 全文无 "dashboards" 字样） |
| 10 | 任务调度（定时执行+导出+重试） | **部分可用** | `core/scheduler.py` 后台线程每秒 tick，真跑 SQL/报表并导出（:219/:297），失败指数退避重试，历史落 `schedules.json`。**但** `scheduler_panel.py:419-421` 在**调度线程**里用 `QTimer.singleShot` 触发 UI 刷新——该线程无 Qt 事件循环，回调不会执行【推断】；面板只能手动刷新 |
| 11 | 数据库对比 / schema diff | **部分可用** | `db/schema_diff.py` 真读 `SHOW COLUMNS`；`ui/db_compare.py:311` 生成同步 SQL 需人工执行；本机无库，**未验证** |
| 12 | ER 图 | **部分可用** | `ui/er_diagram.py:142-146` 布局用 `random.randint` 随机抖动（不可复现）；关系靠 FK 元数据；**未验证** 渲染 |
| 13 | 图表 | **可用** | `ui/chart_view.py` 自绘 QPainter（柱/线/饼/散点），支持导出 PNG；`chart_view_enhanced.py` 另实现 7 种图但仅被死代码 `dashboard.py` 引用 |
| 14 | SQL 性能分析（EXPLAIN 解析+建议） | **可用** | `core/sql_profiler.py:142` 真跑 `EXPLAIN`，解析 type/rows/Extra 给建议；入口 `main_window.py:1060-1067` |
| 15 | 数据质量 1.0/2.0、健康度评分 | **部分可用** | `core/quality_v2.py`、`core/health_score.py` 均发真实 SQL（`SELECT COUNT(*)`、`SHOW INDEX FROM`、空值/重复/主键检查）；但评分权重与部分规则为本地常量推断，无基线校准；**未验证**（无库） |
| 16 | 脱敏规则 | **桩** | `core/masking.py` 只有规则 CRUD + `mask_value`；全仓仅 `ui/masking_dialog.py:223-225`（对话框内预览）与死代码 `shadow_table.py` 调用 `get_strategy` → **查询结果表格从不过滤敏感列**，规则对实际使用无影响 |
| 17 | 智能告警 | **桩** | `core/alerts.py` 引擎需外部 `metrics_provider`/`add_sample` 喂数据；`main_window.py:112` 构造时**没传 provider**，`start_monitoring()` 全仓无人调用（只有 closeEvent 里 `stop_monitoring`）→ 规则表可增删，但永不产生告警 |
| 18 | AI 对话（OpenAI/Claude/Ollama） | **部分可用** | `core/ai_integration.py:87-198` 三个 provider 的 HTTP 调用真实（含错误体截断）；未配 key 时 `ai_chat.py:168-172` 明确弹窗「AI 未配置」。【阅读】**回调在 worker 线程直接改控件**（§3-2）；流式 `on_chunk` 形参从未使用 |
| 19 | AI Agent 对话/自主诊断/索引推荐 | **部分可用→桩** | **【实测】性能诊断分支恒失效**：`ai_agent.py:103` 写的是 `from ..logger import logger`，而该模块**不存在**（实测 `ModuleNotFoundError: No module named 'lightdb_studio.logger'`），异常被 `ai_agent.py:143-144` 吞掉 → 探针 2 用假连接调 `diagnose('performance')`，**实际发起 SQL 数 = 0**、findings 为空、摘要「发现 0 个严重问题」；`SHOW STATUS`（:120）QPS/连接数检查永远不执行。索引推荐依赖 `catalog.tables[...].top_queries`（:378），而正常使用不会写入该字段 → 恒返回空 |
| 20 | ReAct Agent 2.0（9 工具、长期记忆） | **死代码** | `core/agent_react.py` 763 行，全仓无任何引用（`ReActAgent` 只出现在自身定义处）；GUI 的 Agent 页签用的是 `core/ai_agent.py`。相关测试 `tests/test_v2_batch6.py:276` 失败（`FakeConn` 无 `get_tables`） |
| 21 | 仪表盘 DashboardWidget | **死代码** | `main_window.py:80` 只 import、无菜单、无实例化；卡片逻辑本身真实（**【实测】**探针 4：`sql=SELECT COUNT(*)` → 卡片显示 `250`，真发 SQL）。配置只写 `~/.lightdb_studio/dashboard.json`（`dashboard.py:20`） |
| 22 | 导入向导/PerfPanel/MultiResult/独立 AI 面板/网格增强 | **死代码** | `ImportWizard`、`PerfPanel`、`MultiResultView`、`AIPanel`、`EnhancedDataGridMixin.install()` 均只在 `ui/__init__.py:73-138` 的懒加载表或类定义中出现；无任何调用点 → 结果表格**没有**复制/粘贴/大单元格查看（`data_grid_enhanced.py`+`cell_viewer.py` 共 282 行失效） |
| 23 | 插件系统 | **桩** | `core/plugin_manager.py` 动态加载是真的，但扫描目录是仓库根 `plugins/`（`main_window.py:129-131`）——**【实测】该目录为空**，`discover()` 返回 `[]`、`load_all()` 返回 `[]`；示例插件在 `lightdb_studio/plugins/example_plugin.py`（不在扫描路径）；且 `get_menu_items/get_toolbar_buttons/process_sql` **全仓无人调用** → 插件只能"被列出"，不能加菜单/工具栏/改写 SQL |
| 24 | 快照/克隆（备份导出） | **部分可用** | `core/snapshot.py:65-119` 真生成含 `SHOW CREATE TABLE` + `INSERT` 的 `.sql` 到 `~/.lightdb_studio/snapshots/`；**但** `SELECT *` 整表进内存（:106），且每表异常静默（:102/115）→ 可能"成功"导出却静默漏表；无恢复入口（只能手动灌 SQL） |
| 25 | 迁移向导（MySQL→SQLite/MySQL） | **部分可用** | `core/migration.py` 真建表+搬数据；**未验证**（无源库） |
| 26 | 协作/团队配置/云同步/审查流 | **桩** | 团队配置与云同步加密为**异或 + 自带注释「演示用」**（`core/team_config.py:30`、`core/cloud_sync.py:31`）；`net/collab_server.py` 需 `websockets` 包（`HAS_WS`）且无任何 UI 启动服务；SQL 审查流（`core/review_workflow.py` + `ui/review_panel.py`）可提交/执行，但提交人恒为 `'me'`（`main_window.py:1177`） |
| 27 | 审计日志 | **桩（对 MySQL）** | `ui/audit_viewer.py:126` 发 `SHOW AUDIT LOG`——MySQL 无此语句（本仓 `lightdb/sql/executor.py:5547` 自述为引擎扩展），连 MySQL 时必然报错 |

**最关键的 5 个功能状态**：①SQL 执行=部分可用（真跑，但无事务/无分页/阻塞 UI）；②连接管理=可用（密码明文）；③对象浏览器=部分可用（依赖 LightDB 私有 SHOW 语句）；④报表=可用、仪表盘=死代码；⑤AI Agent=部分可用（性能诊断实测恒失效、ReAct 组件死代码）。

## 3. 代码质量（决定性证据）

1. **吞错泛滥：149 处 `except …: pass`，分布在 67/130 个文件（51.5%）**【阅读+脚本】。密集点：`ui/main_window.py` 9 处、`ui/object_tree.py` 7 处、`core/ai_agent.py` 6 处、`core/quality_v2.py` 6 处。危害已实证：`ai_agent.py:143` 一处吞掉 `ImportError` 就让整个性能诊断永久失效（§2-19）；`snapshot.py:102/115` 吞错可静默漏表；`db/metadata.py` 10 个方法全部 `except: return []`，把「语句不被支持」与「库里没对象」变成同一种结果。
2. **线程模型不成立**【阅读+实测】：全仓 UI 层 **0 个 QThread/QRunnable**（grep 仅命中 QTimer）。查询、导入、ER、健康检查、质量检查都在 GUI 主线程同步执行 → 大表查询/大 CSV 导入期间窗口冻结（导入仅靠 `QProgressDialog.setValue` 顺带处理事件，`import_export.py:187`）。反向问题：AI 调用在 `threading.Thread` 里跑（`ai_integration.py:64-75`），`on_done` 回调**直接改 QWidget**（`agent_chat.py:150-157`、`ai_chat.py:190-202`、`agent_chat.py:164-175`）——跨线程操作控件，无 signal/slot 编组。
3. **缺模块导致的静默功能缺失**【实测】：`lightdb_studio/logger.py` 不存在（`Test-Path` 为 False），而 `core/ai_agent.py:103` 引用它。这是"能 import 但功能是假的"的典型：不报错、不提示，只是诊断结果永远为空。
4. **资源与内存**【阅读】：①关标签页不销毁控件——`_close_tab` 只 `removeTab`（`main_window.py:542-545`），Qt 语义下 widget 仍挂在 QTabWidget 下不释放，每开/关一个查询页泄漏一个编辑器+结果表；②同名连接重复连接不关闭旧连接（`object_tree.py:81-83` 直接覆盖字典，树里还会多出一个同名根节点）；③CSV 导入 `list(csv.reader(f))` 全量入内存（`import_export.py:90`）；④快照 `SELECT *` 全表入内存（`snapshot.py:106`）；⑤`QueryHistory.add` 每次执行 SQL 都全量重写 JSON（`history.py:31-54`）。
5. **设置项大面积"只写不读"**【阅读+脚本】：`config.py` 的 `page_size / auto_connect / restore_tabs / editor_tab_size / editor_auto_complete / editor_auto_pair / editor_highlight_line / log_level / query_timeout / max_result_rows / shortcuts` 只在 `config.py` 定义处和 `settings_dialog.py` 读写处出现，**main_window 只消费 theme / editor_font_* / language**（`main_window.py:1187-1206`）。具体后果：数据编辑器页大小硬编码 200（`data_editor.py:33`）无视设置；`_auto_restore()` 无条件自动连接（`main_window.py:615`）无视 `auto_connect`；自定义快捷键存了但 QAction 仍用硬编码 `QKeySequence`。
6. 其他：【阅读+脚本】AST 扫描出 232 处「导入但文件内未使用」的名字（含少量 `# noqa` 故意保留者，如 `__init__.py:41/50`；`main_window.py:25/36` 的 `MySQLMigrator/SQLiteMigrator/get_registry` 是真未使用）；PySide6 用法除线程问题外基本规范（`ExtraSelection` 已按 6.6 修好，`apply_theme` 三套主题完整）；`.query()` 里用 f-string 拼表名（`db/connection.py:134`）无转义，表名带反引号即注入。

**测试覆盖**【实测】：`tests/` 里 **0 个测试** import `lightdb_studio.ui`/`.app`，也无任何 `QT_QPA_PLATFORM`/offscreen 用法 → **GUI 层零测试**。仅 `core/`+`db/` 被测试：引用 studio 的 10 个测试文件 **12 失败 / 36 通过**（`pytest ... --tb=line` 实测）；全仓 `pytest tests -q` = **38 failed, 411 passed, 11 skipped（307s）**。其中可确认为**测试期望写错**的：`tests/test_batch3.py:233` 断言 `'age > 20' in SQL`，而生成器正确输出 `` `age` > 20 ``（带反引号）；`test_v2_batch1.py:42` 期望 `salary→confidential`，但 `catalog.py:37` 把 `salary` 明确列在 `restricted` 模式里（测试与代码自身表冲突）。可确认为**功能缺口**的：`test_v2_batch5.py` 推荐器找不出相关表、`test_v2_batch4.py::test_data_api` 调用计数 0≠2、`test_v2_batch6.py::test_federated_governance` 策略校验对 `DROP prod.users` 返回 `allowed=True`。可确认为**环境限制**的：`test_v3_batch1.py::test_full_pipeline_dry_run`、`test_v3_batch5.py::test_dashboard_generation` 因写 `C:\Users\Administrator\.lightdb_studio\reports\*.md` 被沙箱拒绝（`lightdb/benchmark/report.py:112` PermissionError）——与本 GUI 无关。

## 4. 可运行性实测

| 命令 | 结果 |
|---|---|
| `python lightdb_studio\app.py` | **失败**【实测】：`ImportError: attempted relative import with no known parent package`（`app.py:7`）。必须 `-m` 启动 |
| `python -m lightdb_studio.app`（`QT_QPA_PLATFORM=offscreen`） | **可启动**【实测】：无异常，进入事件循环持续运行（>60s，我手动 kill）；stderr 只有 `QFontDatabase: Cannot find font directory .../PySide6/lib/fonts` 警告（Qt 不再附带字体，与代码无关） |
| 离屏构造主窗口 | **通过**【实测】：`MainWindow(AppConfig())` 成功，`tabs=1, right_tabs=6, menus=6`；`SQLEditor`/`ResultTable`/`ObjectTree`/`apply_theme` 全部可实例化 |
| 主窗口组件级验证（写文件权限） | `~/.lightdb_studio` **在沙箱下不可写**【实测】：`Set-Content ...\.lightdb_studio\_dsh_probe.txt` → `UnauthorizedAccessException 访问被拒绝`。该目录**本次已存在**（历史运行创建），所以 `AppConfig()` 只读成功；若目录不存在，`config.py:71` 的 `mkdir` 会抛 `PermissionError` 让主窗口根本建不起来——**这是环境限制，不是代码缺陷**，但暴露了「配置目录初始化失败即整个应用不可用」的耦合 |
| 非窗口逻辑（探针 1/2，见 §2 各项【实测】） | 多语句切分 5/5 正确；`ReportEngine` 真实导出 CSV/HTML/XLSX；`DataEditor` 分页+脏标记+新增行全通；`MetricCard` 真跑 SQL 显示 250；`PluginManager` 扫描 `D:\lightdb_project\plugins` 发现 **0 个**插件；`AIAgent.diagnose('performance')` 发起 **0 条** SQL |

**未验证项（需什么条件）**：
- 真实数据库往返（建连、建表、CRUD、导入导出到库、快照/迁移/DB 对比/ER 图/质量与健康度得分）——**需要一台可连的 MySQL/LightDB 与凭证**；本机无服务、无凭证。
- 审计日志、`SHOW STATS`、`SHOW PARTITIONS`、`SHOW PROCEDURES/FUNCTIONS` 的真实返回——**需要对端是 LightDB 引擎**（MySQL 不支持，见 §2-4/27）。
- AI 对话/Agent 的真实模型输出与失败表现——**需要 API key 或本地 Ollama**（未配置时行为已实测：明确报「AI 未配置」）。
- 模态对话框内的交互路径（导入向导、迁移向导、调度面板执行、图表导出 PNG、协作服务端）——offscreen 下模态 `exec()` 会阻塞，**需要真实桌面会话人工点检**。
- 关标签页内存泄漏量级——**需要带内存采样的人工/自动化长跑**（本次仅代码判定）。

## 5. 定位判断

**结论：这是一个「以数据库管理为外壳、以数据治理概念为内容」的演示/课程型项目，不是可用的数据库管理工具。**

依据：
1. **完成度结构失衡**：37 280 行里 DB 访问层只占 1.4%（527 行），而「数据湖/数据网格/ETL/联邦治理/数字孪生/自主运维」等愿景模块占 22.3%（8 302 行）且**全部从入口不可达**；整体 40.2% 代码不可达，8 个 UI 面板（2 263 行）实现了却从未被实例化。
2. **关键功能有洞且不自知**：AI 诊断因引用一个不存在的模块而永久静默失效（实测 0 条 SQL）；告警引擎从不喂数据；脱敏规则不影响结果集；插件系统扫描一个空目录；CSV 导入的成功率统计在失败时仍报全成功。这些都不是"未实现"，而是"看起来实现了"。
3. **错误处理与测试不足以支撑工具定位**：51.5% 的文件存在吞错；GUI 零测试；studio 相关测试 12 失败/36 通过且多处失败是真功能缺口（策略校验放行 DROP、推荐器失效）。
4. **依赖与方言自缚**：元数据/审计/统计依赖 LightDB 私有 `SHOW` 语句（MySQL 下直接不可用），而 UI 文案把自己定位成「类似 Navicat」。工具型数据库客户端的必备能力全部缺失：事务控制、结果集分页/流式、多结果集、连接池、SSH 隧道、多驱动、备份恢复、自动补全、跨线程执行。
5. **安全与工程细节是演示级**：连接密码明文 JSON；云同步用 XOR 且自注「演示用」；`SHOW` 语句按 f-string 拼表名无转义。
6. **确实可用的部分（应当承认）**：连接管理、SQL 执行与 EXPLAIN、结果表格、表设计器、数据编辑网格、查询历史/片段、报表模板与 CSV/Excel/HTML 导出、自绘图表、EXPLAIN 性能分析——这 8 项在连上库后是真能干活的，可作为**「能跑 SQL 的课程 Demo / 内部试点原型」**。要变成工具，缺口清单即 §3 与 §2 中标「部分可用/桩」的条目。

## 6. 附：本次产出与证据脚本

- 交付文档：`docs/review/02_studio.md`（本文，≤250 行）
- 临时脚本（均已删除，未提交）：`_studio_probe.py`（离屏构造 + 多语句切分 + 报表导出）、`_studio_probe2.py`（假连接验证 DataEditor/MetricCard/AIAgent/插件扫描）、`_studio_scan.py`（AST 导入图可达性）、`_studio_unused.py`（未使用 import）、`_studio_stats.py`（吞错/规模统计）、`_studio_groups.py`（功能分组行数）
- 未改动：`lightdb_studio/**`、`tests/**`
