# LightDB Studio 桌面端 + Web 版分析（2026-10-19）

> **范围**：`lightdb_studio/**`（134 个 .py / 39,262 物理行）、`web/**`（6 个 .py 710 行 + 3 个静态文件）、`lightdb_import/`、`dashboards/`、`plugins/`。
> **方法**：AST 导入图 + 静态阅读 + 离屏运行（`QT_QPA_PLATFORM=offscreen`，QTimer 退出）+ 真实 LightDB 服务端端到端（临时端口 18899，测后杀进程）+ FastAPI TestClient。
> **标注**：【实测】=本次真跑；【源码】=静态代码证据；【文档】=仅见于文档/声明；【推断】=由前两者推出。
> **未改动任何源码**；未运行全量 pytest（其他成员独占）；临时脚本在 `docs/analysis_2026-10-19/tmp/studio-web/`，运行产物在 `_studio_web_tmp/`（本沙箱的 shell 子进程被拒写 `docs/`，故日志落工作区根目录，见附录 C）。

---

## 〇、结论摘要（先看这里）

1. **桌面端确实能启动**：离屏实测 `MainWindow` 构造成功——6 个菜单、42 个菜单项、15 项工具栏、2 个 Dock、6 个右侧标签；并且**对真实 LightDB 服务端的查询/写入全链路可用**【实测】。README 里"桌面端在 Python ≤3.11 无法启动"的旧结论**已修复、已过时**。
2. **但 33.5k 行 Studio 里 63.5% 可执行、36.5%（42 模块 / 14,336 行）从入口不可达**；其中 33 个是 `core/` 的"数据平台愿景"模块（data_mesh / federated_gov / digital_twin / etl / autonomy…），且多个 README 宣称的功能（仪表盘、Hex 查看器、Excel 兼容复制、工作流入口、生产保护）**只有死代码支撑**【源码+AST】。
3. **Web 版是"能跑但不可用"**：17 条 OpenAPI 路径、JWT 校验真实有效，但**没有任何"新建连接"端点**，`lightdb-studio-web` 以 `create_app()`（无连接）启动 → `/api/v1/query` 恒返回 404"连接 'None' 不存在"【实测】；宣称的 Prometheus `/metrics` 未挂载，实测 404。
4. **安全 P0**：`web/auth.py:11` 硬编码 JWT 密钥 + 自研 HMAC，**本次用该密钥伪造 admin token 实测通过**；默认口令 `admin/admin123` 等硬编码且启动时明文打印；默认绑定 `0.0.0.0`；另有已认证即可利用的 **SQL 注入**（table/db/order_by/列名 f-string 拼接）。Studio 侧连接密码与 AI api_key **均明文落盘**。
5. **代码质量**：429 个 except 处理器中 **172 个（40%）是 pass/continue 吞异常**；**全仓 0 个 QThread**，SQL 在 GUI 线程同步执行 + `fetchall()` 全量 + 无读超时 → 大结果/慢查询必然冻结界面；真正 DB 访问层仅 **670 行（1.7%）**，与 21.7k 行 `core/` 形成强烈反差。

---

## 一、产品形态与架构

### 1.1 形态

| 组件 | 形态 | 入口 | 规模 |
|---|---|---|---|
| `lightdb_studio` | PySide6 桌面 GUI（单进程） | `lightdb_studio/app.py:59 run()` / `python -m lightdb_studio.app` | 134 .py / 39,262 行（非空行 33,671） |
| `web` | FastAPI + 静态前端（单页） | `web/start.py:9 main()` / `python -m web.start` | 6 .py / 710 行；静态 3 文件（HTML 88 行、JS 309 行） |
| `lightdb_import` | **与 Studio 无关**的"资料知识库"建库/灌数脚本 | `执行.txt` 里的 `mysql` CLI 顺序导入 | 10 SQL + 2 py（219 行） |
| `dashboards` | **与 Studio/Web 无关**的 4 个 Grafana JSON 模板 | 无入口（仅 `lightdb/tools/dashboard.py` 引用） | 4 JSON（70 KB） |
| `plugins` | **空目录**（0 个文件） | `PluginManager` 扫描 | — |

- `lightdb_import`：`01_schema.sql` 开头写"LightDB 资料知识库"，表为 versions/modules/features/benchmarks/issues/api/docs/tags/files，配合 `generate_full_import.py` + `import_docs.py` 生成 SQL，`执行.txt` 用 `mysql -h 127.0.0.1 -P 8888` 灌库 → 属于**网站文档库的一次性导入工具**，不是 Studio 功能【源码】。
- `dashboards`：4 个文件全部是 Grafana dashboard JSON（含 `"datasource": {"type": "prometheus"}`），只有引擎侧 `lightdb/tools/dashboard.py` 引用；Studio 全文无 `dashboards` 字样【源码+全仓 grep】。README 的"Prometheus 指标"在 Studio/Web 侧没有落点（Web 的 metrics 路由未挂载，见 §6.4）。
- `plugins/`：仓库根目录为空 → 桌面端插件扫描结果 **0 个**【实测 `PLUGINS loaded: 0`】；`lightdb_studio/plugins/example_plugin.py`（121 行）不在扫描目录且从入口不可达，是死代码【AST】。

### 1.2 架构

```
app.py (QApplication + AppConfig + apply_theme)
└─ ui/main_window.py  MainWindow（唯一装配点，1340 行；本文件 42 个菜单项/31 处 dlg.exec()）
   ├─ 构造期立即创建 ~20 个 manager：EnvironmentManager/LineageStore/SnapshotManager/
   │   SnippetManager/MaskingEngine/TeamConfigManager/CloudSync/ReportEngine/
   │   CollaborationStore/AIConfigStore/CatalogEngine/Scheduler/PluginManager…
   ├─ 惰性属性（首次访问才建）：workflow_engine / ai_agent / alert_engine / ai_chat /
   │   agent_chat / collab_panel / catalog_panel（main_window.py:212-238, 346-410）
   ├─ 启动期恢复（QTimer.singleShot(0)）：plugin_mgr.load_all() + _auto_restore()
   │   （逐条 DBConnection.connect()，main_window.py:244-265, 860-898）
   ├─ db/connection.py  DBConnection（PyMySQL，289 行）+ db/metadata.py、schema_diff.py 等（db/ 共 670 行）
   └─ core/ 66 模块 21,743 行（55.4%）+ ui/ 51 模块 15,390 行（39.2%）
web/server.py create_app() → api.py(16 个 /api/v1 路由) + static/（index.html + app.js + style.css）
```

- **模块依赖方向**：`ui → core/db/utils`，`core` 内部有少量互相引用；`app.py` 不再被反向导入（`core/fonts.py` 已中立化，app.py:40-47 注释说明）；`core/__init__.py:1-33` 只 re-export 15 个模块（非全部 66 个）【源码】。
- **入口可启动**【实测】：离屏 `MainWindow` 构造 + `do_startup_restore()` + 500ms 事件循环全部正常，6 菜单/42 项/15 工具栏/2 Dock/6 右标签；`scheduler.start()` 后台线程启动（main_window.py:191）。
- **`ui/__init__.py` 是 PEP 562 延迟导入表**（51 条 `_LAZY`，`__getattr__` at :141-155），但 `main_window.py:48-88` 又**直接 import 了 33 个面板**——两套机制并存，前者对可达性分析是干扰项（本报告按真实 import 图计算，不把 `_LAZY` 当可达）【源码】。

### 1.3 规模分层（物理行数，含空行）

| 层 | 文件 | 行数 | 占比 |
|---|---:|---:|---:|
| `lightdb_studio/core/` | 66 | 21,743 | 55.4% |
| `lightdb_studio/ui/` | 51 | 15,390 | 39.2% |
| `lightdb_studio/db/`（**真正 DB 访问层**） | 5 | **670** | **1.7%** |
| `lightdb_studio/net/` | 2 | 431 | 1.1% |
| `lightdb_studio/utils/` | 5 | 548 | 1.4% |
| `lightdb_studio/plugins/` | 2 | 150 | 0.4% |
| 入口/配置/包初始化 | 3 | 329 | 0.8% |
| **合计** | **134** | **39,262** | 100% |

> 结论：这是一个"UI + 概念引擎"为主、DB 驱动层极薄的工具。`core/` 里 21.7k 行多数是内存数据结构/规则引擎，不访问数据库（§3.3）。

---

## 二、功能真实性矩阵（README 宣称逐项判定）

判定口径：**真可用**（离屏/端到端实测或源码证明 GUI 可达且真读写 DB）、**部分可用**（有实现但缺关键环节/宣称过度）、**桩**（接口存在、行为是假/空）、**死代码**（从入口不可达或从未实例化）、**入口不可达**。

| # | README 宣称（README.md 行） | 判定 | 证据 |
|---|---|---|---|
| 1 | 数据网格编辑（内联修改、批量提交）(180) | 部分可用 | `ui/data_editor.py` 有 reload/pending/提交前 SQL 预览；本机无库未复测；旧审计实测可用【文档+源码】 |
| 2 | 表设计器（可视化建表/改表）(181) | 部分可用 | `ui/table_designer.py` `build_create_table`→`conn.execute`；需连接【源码】 |
| 3 | CSV/Excel/JSON 导入导出 (182) | **部分可用（宣称过度）** | `ui/import_export.py` 全文只 import `csv`（:2），只有 `ImportCSVDialog`/`ExportCSVDialog`；**无 Excel/JSON 实现**【源码】 |
| 4 | 大文本/图片/Hex 查看器 (183) | **死代码** | `ui/cell_viewer.py`（125 行）从入口不可达、`CellViewerDialog` 实例化次数 0；`result_table.py`/`data_editor.py` 无引用【AST 实测】 |
| 5 | 复制粘贴（Excel 格式兼容）(184) | **死代码** | 唯一实现是 `ui/data_grid_enhanced.py:86-148` 的 `EnhancedDataGridMixin`，**从未被 mixin 进任何控件**（模块从入口不可达，`install()` 无调用）【AST+源码】 |
| 6 | SQL 编辑器语法高亮 (187) | 可用 | `ui/sql_editor.py:11,37` 挂 `utils.sql_highlighter.SQLHighlighter`（该模块可达）【源码】 |
| 7 | 自动补全 (187) | **死代码** | `utils/sql_completer.py`（210 行）import 数 0、从入口不可达；sql_editor 未引用【AST】 |
| 8 | 多标签查询 (188) | 可用 | `main_window.py:718,740` `tabs.addTab`；离屏实测 `tabs: ['查询','查询']`（新查询页自增）【实测+源码】 |
| 9 | 查询历史（搜索/收藏）(189) | 部分可用 | `utils/history.py` 落盘 `~/.lightdb_studio/history.json`，`main_window.py:1018,1025` 每次执行写入；"收藏"未见实现【源码】 |
| 10 | SQL 片段库（18+ 内置模板）(190) | 可用 | `core/snippets.py:38-100` 恰好 18 条内置 + `get_all():161-163` 合并用户/团队片段；`main_window.py:301` 挂载【源码】 |
| 11 | 结果集表格（排序/过滤/NULL 高亮）(191) | 部分可用 | 排序 `result_table.py:62,105,137 setSortingEnabled`；NULL 显示 :115；**过滤无 UI**（`column_filter` 只在死 mixin 里）【源码】 |
| 12 | 数据图表（10 种类型）(194) | 部分可用（宣称取巧） | 菜单实际打开的 `ChartDialog` 只实现 **bar/line/pie/scatter 4 种**（`ui/chart_view.py:101,139,186,233`）；10 个 `CHART_*` 常量在 `ui/chart_view_enhanced.py`，而该模块只被**死的 dashboard.py** 引用【源码+AST】 |
| 13 | ER 图（自动外键检测）(195) | 部分可用 | 并非读真实外键元数据：`ui/er_diagram.py:4` 自述"基于 column 命名 `<table>_id`"、:80-86 启发式匹配；布局还带 `random.randint`（:142-146）；表/列信息经 `SHOW COLUMNS` 真实读取（:61-69）【源码】 |
| 14 | 仪表盘（指标卡+图表卡+自动刷新）(196) | **死代码** | `ui/dashboard.py` 425 行、`DashboardWidget` 自动刷新实现（:220,229）；但 `DashboardWidget` 全仓只在 `main_window.py:87` 被 import，**实例化 0 次**（AST+全仓 grep），无任何菜单打开它【实测+源码】 |
| 15 | 数据血缘图 (197) | 部分可用 | 菜单"数据血缘"打开 `LineageDialog`（main_window.py:1238-1247），`core/lineage*.py` 3 模块可解析 SELECT/INSERT/CREATE；`LineageGraphView` 类本身实例化 0 次【源码+AST】 |
| 16 | 多环境管理（一键切换 + **生产保护**）(200) | 部分可用（保护=假） | 切换真实：`main_window.py:1204-1214`（切库+状态栏）；但 `core/environments.py:172-212 check_sql_safety()` **全仓无调用者**，`_execute_sql`(main_window.py:951-1027) 也不做任何 env 检查 → **生产环境写操作无二次确认**【源码+全仓 grep】 |
| 17 | SQL 审查工作流（提交-审核-执行）(201) | 部分可用 | `core/review_workflow.py` + `ReviewPanel/ReviewSubmitDialog` 实例化 1 次；但执行 SQL 不强制走审核（`_exec_current` 直接执行）【源码+AST】 |
| 18 | 数据库对比（差异 SQL + 一键同步）(202) | 可用 | `ui/db_compare.py:289-311` `_sync` 真在目标连接 `execute(stmt)`；`db/schema_diff.py` 真读 `SHOW COLUMNS`【源码】 |
| 19 | 快照/克隆（备份+一键复制）(203) | 可用 | `core/snapshot.py:310-348` `SHOW CREATE TABLE`+`SELECT *` 组装；:529 执行；:615-645 克隆到目标连接【源码】 |
| 20 | SQL 格式化 Ctrl+Shift+F (204) | 可用 | `core/sql_formatter.py` + `main_window.py:468-471` 菜单+快捷键【源码】 |
| 21 | 数据脱敏（8 种策略）(205) | 部分可用 | `core/masking.py:52` 定义 full/partial/phone/email/id_card/hash/replace/generalize/none（9 值、8 类策略），`mask_value:120-190` 真实实现；但规则默认空（实测 `rules=[]`），需用户建规则/`auto_detect_rules`【实测+源码】 |
| 22 | AI 自然语言转 SQL (208) | 部分可用 | `core/ai_integration.py:79-196` 真实现 OpenAI/Claude/Ollama HTTP；需 api_key（默认空），`AIAgent`/`AIChatPanel` 实例化 1 次；本环境未配 key 未实测调用【源码+实测】 |
| 23 | SQL 解释/优化建议 (209) | 部分可用 | `core/sql_profiler.py`（真实 EXPLAIN+评分）可用；但 `core/sql_auto_optimizer.py`(479 行)/`ai_assist.py`(386 行) **从入口不可达**，"AI 优化建议"主实现是死代码【AST】 |
| 24 | 索引推荐 (210) | 部分可用 | `core/health_score.py` 用 `SHOW INDEX` 真实检测缺失索引（旧审计实测）；独立"推荐器"`core/recommender.py` 从入口不可达【源码+AST】 |
| 25 | 支持 OpenAI/Claude/Ollama (211) | 可用（代码层） | 三个 provider 分支都在 `ai_integration.py:79-196`；Ollama 无需 key（:167-196）【源码】 |
| 26 | 会话共享 + 评论注解 (214) | 部分可用 | 评论/会话存储 `core/collaboration.py` 真实（内存+JSON）；**网络实时共享依赖 websockets，本环境未安装**：`net/collab_server.py:15-20` 捕获 ImportError，:78-82 `start()` 打印"需要 websockets"后返回 False【实测 import websockets 缺失】 |
| 27 | @提及 + 回复线程 (215) | 部分可用 | `core/collaboration.py` 有 Comment/mentions 结构；未端到端验证【源码】 |
| 28 | 团队配置库 (216) | 部分可用 | `core/team_config.py` 真导出/导入；但"加密"是 XOR+硬编码默认钥（§5.4）【源码】 |
| 29 | 云同步（加密导出/导入）(217) | 部分可用（加密弱） | `core/cloud_sync.py:58-120` 真打包 zip 再 XOR 加密；**无 MAC/无完整性校验**，错误密码不报错而是产出乱码（实测 roundtrip=True、wrong_pw 不抛异常）【实测+源码】 |
| 30 | SQL 性能分析（EXPLAIN + 评分）(220) | 部分可用 | `core/sql_profiler.py:142` 真 `EXPLAIN`；`ui/profiler_view.py` 实例化 1 次【源码+AST】 |
| 31 | 数据库健康度评分（4 维度）(221) | 部分可用 | `core/health_score.py` 真实 SQL（SHOW INDEX/COUNT/…，:142-242）；`HealthDashboard` 实例化 1 次；本次直接 `HealthScorer().score()` 因 API 名不符失败（类存在，方法名不同）【实测+源码】 |
| 32 | 实时性能监控（QPS 曲线）(222) | 部分可用 | `ui/perf_monitor.py:71-242` 真实 QTimer 采样 + `SHOW STATUS`；`core/performance_monitor.py:56` 后台线程采样【源码】 |
| 33 | 慢查询分析 (223) | 部分可用 | 同 #30，基于 EXPLAIN 的告警/评分；无独立慢日志采集【源码】 |
| 34 | 数据质量检查 (224) | 可用 | `core/data_quality.py:40-136`、`core/quality_v2.py:175-418` 全部真实 SQL；`DataQualityDialog`/`QualityV2Dialog` 实例化 1 次【源码+AST】 |
| 35 | 任务调度（cron 风格 + 失败重试）(227) | 部分可用 | **不是 cron 表达式**：`core/scheduler.py:31-36` 只有 once/interval/daily/weekly/monthly；重试真实：指数退避 `2**attempt`（:255-256），任务真跑 SQL（:219）；后台线程 :125【源码】 |
| 36 | 报表系统（Excel/HTML/CSV 导出）(228) | 部分可用 | 三个导出函数真实（`core/reports.py:106-230`，旧审计实测 CSV 19B/HTML 1,375B/XLSX 4,969B 落盘）；**默认模板为空**（实测 `templates: 0`；`reports.py:74 templates=[]`），Web 端 `/reports` 实测返回 `[]`【实测+源码】 |
| 37 | 工作流引擎（多步 SQL 编排）(229) | **入口不可达/隐藏** | `core/workflow.py:144-207` 真实 BEGIN/COMMIT/ROLLBACK；但 `MainWindow` 只把它做惰性属性（main_window.py:33,155,213），**没有任何菜单/面板打开 Workflow**，全仓无其他引用 → 用户不可达【AST+全仓 grep】 |
| 38 | 审计日志查看 (230) | 部分可用 | `ui/audit_viewer.py`（`_load`/筛选/CSV/HTML 导出）；数据来源与引擎审计表需真实库验证【源码】 |
| 39 | MySQL/SQLite 迁移向导 (231) | 部分可用 | `core/migration.py` 真实读写（pymysql/sqlite3，:305-431）；`MigrationWizard` 实例化 1 次【源码+AST】 |
| 40 | FastAPI 后端 (234) | 可用 | `create_app()` + TestClient 全流程 200【实测】 |
| 41 | REST API（JWT 认证）(235) | 可用但有安全缺陷 | 16 个 `/api/v1` 端点、JWT 验签真实（篡改/过期被拒）；自研 HMAC + 硬编码密钥（§5）【实测】 |
| 42 | 浏览器访问 (236) | 部分可用 | `GET /` 200、静态 JS 有登录+查询页（6 处 api 调用）；**但见 #43、以及 Web 端无法新建数据库连接**（§6.3）【实测】 |
| 43 | Prometheus 指标 (237) | **桩/不可达** | `web/metrics.py` 定义了 `/metrics` 与 `/api/v1/metrics/json`，但 `web/server.py:52-54` 只 `include_router(api.router)`，**metrics 路由从未挂载**；实测两者均 404【实测+源码】 |

**统计**（43 项）：可用 8 / 部分可用 22 / 死代码或入口不可达 7 / 桩 1 / 其余为组件级声明。按"用户能真正用起来"口径，README 的"具备 Navicat 核心功能 + 企业级能力"**明显高于实现**。

---

## 三、可达性 / 死代码统计

### 3.1 AST 导入图（根 = `lightdb_studio.app`）

| 指标 | 数值 |
|---|---:|
| 模块总数 | **134** |
| 可达模块 | **92（68.7%）** |
| 可达行数 | **24,920 / 39,262 ≈ 63.5%** |
| 不可达模块 | **42（31.3%）** |
| 不可达行数 | **14,336（36.5%）** |
| 函数/类 | 1,726 / 262（AST 统计） |

> 与旧审计 `docs/review/02_studio.md:1`（130 文件 / 37,280 行、可达 81 模块 / 22,283 行 = 59.8%）相比：**代码已增长 4 文件 / +2k 行、可达比例略升**，该文档的规模数字**已过时**，死代码结论**方向仍成立**。

### 3.2 不可达模块清单（42 个 / 14,336 行）

**core/（33 个，约 12.6k 行）**——"数据平台愿景"模块，仅被 tests/其他死模块引用：
`agent_react(763) federated_gov(521) data_lake(482) sql_auto_optimizer(479) digital_twin(468) autonomy(467) data_mesh(430) data_contract(422) data_science(420) slo(405) distributed(404) product_ops(390) ai_assist(386) cloud(383) ai_sql_gen(380) data_api(379) virtualization(378) lineage_closure(371) nlb(369) lineage_v2(368) fullstack_obs(366) ai_finetune(364) data_fabric(360) federation(357) recommender(334) cdc(321) observability(313) data_market(270) lineage_visual(224) team(209) shadow_table(206) workflow_visual(44) etl(656)`
（括号内为该文件物理行数；这些模块内部**有真实 SQL/算法实现**，例如 `data_science.py:40-369` 真发 `SELECT`，但**没有任何 UI/入口能调用它们** → 属于"写好了没接线"的死代码，而非纯桩。）

**ui/（6 个孤儿面板，1,287 行）**：
`import_wizard(469)`、`perf_panel(260)`、`ai_panel(197)`、`data_grid_enhanced(157)`、`cell_viewer(125)`、`multi_result(79)` —— 全部 import 数 0；离屏实测其中 `ImportWizard()`/`CellViewerDialog()`/`PerfPanel()` 因缺必填参数无法直接构造（`conn`/`value`/`conn_getter`），即**即使有人想用也没有调用约定**。【实测+AST】

**plugins/（2，150）**：`plugins/`（空包）与 `plugins/example_plugin.py`（121 行，不在扫描目录）。
**utils/（1，210）**：`utils/sql_completer.py`。

### 3.3 "import 了但从未实例化"的运行时死代码（比不可达更隐蔽）

| 模块 | 行数 | 情况 |
|---|---:|---|
| `ui/dashboard.py` | 425 | `DashboardWidget` 仅 `main_window.py:87` import，**实例化 0 次**；自动刷新代码永不执行 → README"仪表盘"落空【实测+AST】 |
| `ui/chart_view_enhanced.py` | 551 | `EnhancedChartWidget` 只被死的 dashboard.py 实例化 → 10 种图表永不出现【AST】 |
| `ui/lineage_view.py` 的 `LineageGraphView` | 369（模块） | 只实例化 `LineageDialog`；GraphView 类实例化 0 次【AST】 |
| `core/workflow.py` | 282 | 只挂在 `MainWindow._LAZY_MANAGERS`，无 UI 入口【全仓 grep】 |

### 3.4 `core/` 66 模块被 UI 真正调用的情况

- 被可达 UI **直接 import** 的 core 模块：约 25 个（snapshot、quality_v2、migration、ai_agent、lineage_enhanced、catalog、sql_profiler、alerts、scheduler、health_score、workflow、lineage、metrics、ai_integration、db_types、db_errors、data_map、reports、environments、snippets、cloud_sync、masking、data_quality、assets、plugin_manager、performance_monitor、collaboration、review_workflow、sql_formatter、team_config、fonts…）。
- **33 个 core 模块零 UI 引用**（§3.2 清单）——这是"数据平台愿景"的主体，约占 core 行数的 57%。
- 另有一批 core 模块虽被 import，但只在后台线程/控制器里运行（metrics/performance_monitor/alerts）：它们拿的是**同一条共享 `DBConnection`**（由 `_get_conn_by_name`/`connection_provider` 提供），与 UI 的查询共用 pymysql 连接，靠 `DBConnection.lock`（db/connection.py:154,222,244）串行化 → **并发查询会互相阻塞，且事务边界互相串扰**（autocommit=True，无事务）【源码+推断】。

---

## 四、代码质量

### 4.1 吞异常（`except ...: pass/continue`）分布

```
except 处理器总数: 429
吞异常(pass/continue-only): 172 个（40.1%），分布在 65 个模块
宽泛捕获 except Exception/BaseException: 356 个（83.0%）
裸 except: 0 个
```
Top 模块：`ui/main_window.py 9`、`core/etl.py 8`、`core/quality_v2.py 7`、`net/collab_server.py 7`、`ui/object_tree.py 7`、`core/ai_agent.py 6`、`core/health_score.py 6`。

后果举例：
- `ui/object_tree.py` 7 处吞异常（:140,219,234,249,264,279,343）→ 对象树某段加载失败时**只显示"⚠ 加载失败"或静默空节点**，用户无法区分"没权限/语法不支持/网络断"；其中 :308/:312 的 `SHOW CREATE PROCEDURE/FUNCTION`、:407/:421/:429 的 `DROP PROCEDURE/FUNCTION/TRIGGER` 等语法在 LightDB/MySQL 语义下并不可靠，异常被吞 → 过程/函数/触发器节点**恒为空且无提示**【源码+推断】。
- `db/connection.py:243-249 execute()` 在失败时 **返回 `(0, 错误文本)` 而不抛异常**；`ui/import_export.py:183-184` 又写成 `self.conn.execute_many(...); ok += len(batch)`，**完全不看返回值** → CSV 导入即使整批失败也显示"成功 N 行，失败 0 行"（旧审计已指出、本次复核未修复）【源码】。
- `core/snippet/cli` 类 `except Exception: pass` 在 JSON 解析处（如 `snippets.py:135-136`、`config.py:83-84`）会把坏配置**整体丢弃**，用户自定义数据凭空消失且无提示。

### 4.2 UI 线程阻塞（无 QThread / fetchall / 无超时）

- **全仓 `QThread` 出现 0 次**【实测 grep`lightdb_studio/**/*.py`】；`main_window.py` 只 import `QTimer`（:11）。
- SQL 执行全在 GUI 线程同步跑：`_exec_current`→`_execute_sql`（main_window.py:931-1027）逐个语句 `conn.query()`/`conn.execute()`，多条语句串行；`db/connection.py:216-233` 成功路径 `rows = cur.fetchall()` **一次性把全部结果读进内存**。
- 连接无读超时：`connect_timeout=10`，但 `pymysql.connect` 未设 `read_timeout`（db/connection.py:165-176）→ 一条慢 SQL 可**无限期冻结窗口**；`Settings.query_timeout`（config.py:66）**在 DBConnection 中未被使用**（全仓 grep 无引用）【源码】。
- 自动连接也在 GUI 线程：`_auto_restore` 逐条 `conn.connect()`（main_window.py:868-891），虽被 `QTimer.singleShot(0)` 推迟到首屏之后，但连接不可达时**每条最多等 10s**，期间界面卡死（代码注释也承认，:250-252）【源码】。
- 后台线程只用于"监控采样/调度"（`core/scheduler.py:125`、`core/metrics.py:153`、`core/performance_monitor.py:56`、`core/alerts.py:311` + `ai_integration.py:73` 的对话线程），**没有一条用于查询**。
- 实测：GUI 对真实服务端跑 3 条语句总耗时 **112ms**（小数据），说明链路可用但"无异步"属实【实测】。

### 4.3 资源泄漏 / 重复实现 / 其他

- **Web 会话连接池泄漏**：`_session_conns`（web/api.py:63）按 `username:连接名` 缓存 `DBConnection`，**无上限、无 TTL、无登出清理、无 close** → 长期运行必然连接泄漏【源码】。
- **Studio 长连接**：`_auto_restore` 建立的连接挂在对象树上，无心跳/重连策略（只用 `ping(reconnect=False)` 判定，db/connection.py:183-190），服务器重启后需手动刷新。
- **重复实现**：JWT 自研（web/auth.py）而 pyproject/requirements 里声明了 `python-jose`+`passlib`（未使用）【源码】；Web 权限表在 `auth.py:86-99` 与 `api.py:281-289` **各写一份** `_has_perm/perms`；主题/样式在 `ui/theme.py`（312 行）与 `web/static/style.css` 各自实现；`db/schema_diff.py` 与 `core/migration.py` 都做列比较。
- **配置写入非原子**：`Settings.save`/`AppConfig.save` 用 tmp+replace（config.py:86-91,118-126）→ 这一点是好的；但**明文密码**问题见 §5。
- **状态栏/错误信息**：`main_window.py:680-690` 已改为可选中复制的 QLineEdit，错误不截断（旧问题已修复）【源码】。
- **启动性能**：`main_window.py:126-131` 注释称"30+ 管理器构造 ~9ms"，实测离屏构造 + 500ms 事件循环无异常；`QTimer.singleShot(0)` 延迟插件扫描/自动连接（:244-265）【实测+源码】。

---

## 五、安全

### 5.1 Studio：连接密码与配置（P1）

- `ConnectionInfo.password: str` 明文（`config.py:20`）；`AppConfig.save()` 直接 `json.dump([asdict(c)])` 写 `~/.lightdb_studio/connections.json`（config.py:118-126）。
- **【实测】** 用临时 HOME 写一条 `password: "SuperSecret123"` 的连接后读回文件：`contains_plaintext: True`，文件内容即明文 JSON。
- **可利用性**：本地同用户进程/备份/同步盘泄露即得所有数据库口令；产品无主口令加密、无系统钥匙串集成（全仓无 keyring/getpass 引用）。风险评级 **P1**（本地攻击面，非远程）。

### 5.2 AI / 团队 / 云同步的"加密"（P1~P2）

| 位置 | 实现 | 问题 |
|---|---|---|
| `core/ai_integration.py:12,246-264` | `AIConfigStore.save` 用 `json.dump(vars(config))` 写 `lightdb_studio/ai_settings.json` | **api_key 明文落盘**；实测加载出 `AIConfig(api_key='')` 结构，路径由 `SETTINGS_KEY:12` 决定【源码】 |
| `core/team_config.py:29-49,84-85,101-102` | "加密保存密码"实为 **XOR + base64**；未提供密钥时用**硬编码默认钥 `lightdb_default`** | 导出文件中的 `password_encrypted` 可用硬编码钥直接还原【源码】 |
| `core/cloud_sync.py:24-50` | PBKDF2-SHA256(10 万轮) 派生密钥 + **XOR 流式加密**，无 MAC/无认证 | 密文可篡改不可检测；**实测错误密码不报错**（解密出乱码）；注释自述"演示用"【实测+源码】 |
| `core/masking.py:165-167` | 脱敏 `hash` 策略用 `sha256(s)[:16]` 无盐 | 仅作展示脱敏可接受，P3 |

### 5.3 Web 版认证（P0）

- **自研 HMAC-JWT**：`web/auth.py:24-41` 生成、:44-67 验签（`hmac.compare_digest`、检查 `exp`），未校验 header `alg`/`iss`/`aud`；**`SECRET = "lightdb_studio_secret_2025_change_me"` 硬编码在源码第 11 行**，无环境变量覆盖路径。
- **【实测】利用**：用该字符串按同算法伪造 `{"sub":"attacker","role":"admin"}` token → `GET /api/v1/me` 返回 `200 {"username":"attacker","role":"admin"}`。源码公开（开源仓库）→ **任何能访问 Web 服务的人可伪造任意角色（含 admin）**。过期 token 被正确拒绝（401）、篡改签名被拒（旧审计结论仍成立），但静态密钥让验签形同虚设。
- 默认口令：`web/server.py:17-22` 硬编码 `admin/admin123、dev/dev123、analyst/analyst123、viewer/viewer123`；`run_server` 启动横幅**把所有用户名/口令明文打印**（:90-93）；`start.py:11` 默认 `--host 0.0.0.0`（`run_server` 同样 :73）→ **局域网内未改密即可被接管**。登录为明文字符串比较（api.py:108 `user['password'] != req.password`），无常量时间比较/无失败限速/无锁定【源码】。
- 会话生命周期：token 默认 **7 天**（auth.py:24-25），**无 refresh、无 logout、无吊销**（OpenAPI 路径里无 logout/refresh，实测 `AUTH-RELATED PATHS: []`）；服务端 `_session_conns` 借用 `payload['sub']` 作为会话键（api.py:66-87）→ 同一用户多 token 共用连接、无清理【实测+源码】。

### 5.4 Web 版授权与注入（P0）

- **权限模型**：`admin/developer/analyst/viewer` 四角色（auth.py:86-99）；`/query` 写语句额外检查 `write`（api.py:246-249）。实测：无 token 401、过期 401、viewer 不能写（但本环境因无连接先 404）【实测】。
- **SQL 注入（已认证、任意角色即可）**：所有元数据/数据接口把路径参数与用户输入**直接 f-string 拼进反引号标识符**：
  - `web/api.py:309-312` `SELECT * FROM \`{table}\` ... ORDER BY \`{order_by}\` ... LIMIT {limit}` —— `table`/`order_by` 均可反引号逃逸注入；
  - `web/api.py:325-344` INSERT 的**列名**来自请求体 `data.keys()`；:347-369 UPDATE 的 set/where 列名、:372-391 DELETE 的 where 列名同理；
  - `web/api.py:187` `SELECT COUNT(*) FROM \`{t}\``（t 来自 SHOW TABLES，较低危）。
  - **可利用性**：结合 5.3 的伪造 admin token，攻击者可对已配置连接注入任意单条 SQL（pymysql 默认不开启多语句，不能直接堆叠 DROP；但可 `UNION SELECT` 读任意表、`INTO OUTFILE` 写文件、`SLEEP()` 盲注）。**P0**。
- **SSRF/内网探测**：`POST /api/v1/connections/test`（api.py:146-157）对任意 host:port 发起 `pymysql.connect`，且**只要求 `verify_token`（任意角色）**，无白名单。**实测**用 admin token 指向 127.0.0.1:18899 返回 `{"ok":true,"version":"5.7.30-LightDB"}` → 可做内网端口扫描/MySQL 口令爆破/击中内网服务【实测】。**P1**。
- **CORS**：`web/server.py:44-50` `allow_origins=['*']` + `allow_credentials=True` + 全方法/全头。**实测** OPTIONS 预检回显 `access-control-allow-origin: https://evil.example.com` 且 `allow-credentials: true` → 任意站点可发起"带凭证"的跨域请求（token 存 localStorage，需配合 XSS 才能完全接管，但配合 5.3 的伪造 token 风险显著升高）。**P1**。
- **信息泄露**：`GET /api/v1/history`（api.py:396-402）读的是**全局共享的** QueryHistory 文件，任意已登录用户（viewer 亦可）可看到他人在本机执行过的全部 SQL（实测返回历史条目）。**P2**。`/reports/{name}/run` 同理只需 verify_token，无角色限制（:422-442）。
- **路径处理**：Web 端无用户可控文件路径接口（`FileResponse/HTMLResponse` 只读 `static/` 固定文件；server.py:56-68），未发现路径遍历；Studio 的文件读写均经 QFileDialog（用户自选），未发现遍历【源码】。

### 5.5 可利用性小结

| 攻击者位置 | 可做的事 | 评级 |
|---|---|---|
| 局域网/可访问 Web 端口 | 用默认口令登录 → 或直接用源码里的 SECRET 伪造 admin → 若服务端预置了连接，则可 SQL 注入读/写库；可 SSRF 探测内网 | **P0** |
| 任意已认证用户 | 通过 table/order_by/列名注入执行任意单条 SQL；读全局历史；SSRF | **P0/P1** |
| 本机同用户/拿到备份 | 读 `connections.json` 明文口令、`ai_settings.json` 明文 api_key、团队配置 XOR 可逆 → 横向连接所有库 | **P1** |
| 拿到云同步包 | 无口令也能看到 zip 结构（XOR 流可离线爆破弱口令，且无完整性校验） | P2 |

---

## 六、Web 版

### 6.1 端点清单（OpenAPI 实测 17 条路径，`/api/v1` 下 16 条）

| 方法 | 路径 | 认证 | 备注 |
|---|---|---|---|
| GET | `/` | — | 静态首页（index.html） |
| POST | `/api/v1/login` | — | 默认口令硬编码（server.py:17-22） |
| GET | `/api/v1/health` | — | 200 `{ok,version,ts}` 实测 |
| GET | `/api/v1/me` | JWT | 实测 200/401/伪造 200 |
| GET | `/api/v1/connections` | JWT | 只列 `app_state.connections`（只读） |
| POST | `/api/v1/connections/test` | JWT（任意角色） | **SSRF**，实测可连 127.0.0.1:18899 |
| GET | `/api/v1/databases` | JWT | 需服务端已有连接 |
| GET | `/api/v1/databases/{db}/tables` | JWT | 逐表 `COUNT(*)`（N+1） |
| GET | `/api/v1/databases/{db}/tables/{table}/columns` | JWT | table 注入点 |
| GET | `/api/v1/databases/{db}/tables/{table}/data` | JWT | **order_by/table 注入点**（:309-312） |
| POST | `/api/v1/databases/{db}/tables/{table}/insert` | JWT+write | 列名注入 |
| PUT | `/api/v1/databases/{db}/tables/{table}/update` | JWT+write | 列名注入 |
| DELETE | `/api/v1/databases/{db}/tables/{table}/delete` | JWT+write | 列名注入 |
| POST | `/api/v1/query` | JWT+query/write | 实测无连接时 404 |
| GET | `/api/v1/history` | JWT | 全局历史泄露 |
| GET | `/api/v1/reports` | JWT | 默认空 `[]`（实测） |
| POST | `/api/v1/reports/{name}/run` | JWT | 模板存在才能跑 |

未挂载但存在的路由：`/metrics`、`/api/v1/metrics/json`（`web/metrics.py:9-30`，`server.py:52-54` 未 include）→ 实测 404。

### 6.2 启动与实测

- `web/server.py:28-70 create_app()` 成功；`uvicorn 0.54.0`、`httpx 0.28.1` 已装，**用 FastAPI TestClient 完成全流程**（任务允许优先 TestClient）：`GET /` 200、`/static/app.js` 200、health 200、login(admin/admin123) 200、无 token 401、伪造 admin 200、CORS 预检 200、`/metrics` 404、`/api/v1/query` 404"连接 'None' 不存在"【实测】。
- 未额外起临时 uvicorn：TestClient 已覆盖 ASGI 层（含中间件），且本机 18899 已被本任务的 LightDB 服务端占用用于 DB 端到端测试。

### 6.3 致命功能缺口：**Web 版无法建立数据库连接 → 无法真正查库**

- `web/start.py:23` 入口调用 `create_app()`（**不传 connections**）；`server.py:15-25` 的 `app_state['connections']` 默认空列表。
- 所有 API 中**没有 POST/PUT 新增连接的端点**（只有 `GET /connections` 只读 + `POST /connections/test` 试连后即丢）。
- `api.py:66-87 _get_conn()` 从 `app_state['connections']` 找连接，找不到即 404。
- **【实测】** 登录后 `POST /api/v1/query {"sql":"SELECT 1"}` → `404 {"detail":"连接 'None' 不存在"}`。
- 结论：README"Web 版可运行"只在"框架能起、登录能过"层面成立；**浏览器用户永远无法查询任何数据库**（除非改代码以 `create_app(connections=[...])` 编程式注入，CLI 入口没有这个参数）。**功能 P0**。

### 6.4 静态前端

- `web/static/index.html` 88 行（登录页 + 主页面 + 对象树/编辑器/结果表/历史容器，id 实测 17 个）；`app.js` 309 行、6 处 `api()` 调用（`/connections`、`/databases?connection=`、`/tables?connection=`、`/query`、`/history?limit=30`、`/me`）。
- 前端**没有连接管理表单**（只有下拉选择 `conn-select`），与 6.3 后端缺口一致【实测+源码】。
- `web/api.py` 使用 pydantic v2 已弃用的 `req.dict()`（:150）—— 可跑但会有 Deprecation 警告，P3。

### 6.5 与 Studio 的复用

- 复用 Studio 的 DB 层与配置模型：`web/api.py:11-12` `from lightdb_studio.db.connection import DBConnection`、`from lightdb_studio.config import ConnectionInfo`。
- 复用 Studio 的 `core.reports/QueryHistory`（api.py:400,409）；但 **Web 的会话连接池/权限表是另起炉灶**（_session_conns、_has_perm），与 Studio 的 `ConnectionInfo`/角色体系无统一。
- 依赖声明不一致：`requirements_web.txt` 要求 `python-jose/passlib`，代码**完全没用**（JWT 自研）；反之 `websockets` 在 requirements 里但本环境未安装（Studio 协作服务因此不可用，属**环境限制**）。

---

## 七、与 README 宣称的差异（重点）

| README 宣称 | 现实 | 性质 |
|---|---|---|
| "桌面端在 Python ≤3.11 无法启动"（README:21，旧审计） | **已修复**：离屏实测 MainWindow 正常构造，6 菜单【实测】 | 旧结论过时 |
| "Web 版可运行（17 API 端点，JWT 校验有效）"（README:22） | 端点 16 条 / JWT 真验签；**但无连接管理 → 无法查库** | 表述误导 |
| "具备 Navicat 的核心功能"（README:173） | 查询/编辑/对象树/导入导出(CSV)/表设计器可用；**无数据同步、无用户管理、无 SSH 隧道、无服务器监控面板、无备份调度**等 Navicat 核心能力；且大量宣称功能为死代码 | **夸大** |
| "企业级能力：AI 助手、数据血缘、SQL 审查、性能分析" | 均有实现但：AI 需外部 key、血缘/审查未强制闭环、`ai_assist`/`sql_auto_optimizer` 死代码 | 部分可用 |
| 大文本/图片/Hex 查看器、Excel 兼容复制 | 仅死代码（cell_viewer/data_grid_enhanced 不可达） | **虚假** |
| 仪表盘自动刷新、10 种图表 | DashboardWidget 从未实例化；菜单图表仅 4 种 | **虚假/夸大** |
| 生产环境保护（多环境） | 切换真实、**保护未接线**（check_sql_safety 无调用者） | **虚假** |
| Prometheus 指标 | /metrics 未挂载，404 | **虚假** |
| Web 版"浏览器访问" | 静态页真实，但后端无连接管理 → 不可用 | 误导 |
| 报表系统 | 导出真实现，默认 0 模板 | 部分可用 |
| "任务调度 cron 风格" | 是 once/interval/daily/weekly/monthly，**不是 cron 表达式** | 夸大 |

**与旧审计的核对**：
- `docs/AUDIT_REPORT.md:21`（桌面无法启动）、README:20-22 的三条 P0——**桌面启动、reports.py f-string 语法、agent_chat QLineEdit 导入** 均已修复【实测间接证明：全模块可 import、MainWindow 可构造】。
- **仍未修复**（旧审计已列、本次复核仍在）：连接密码明文（config.py:20）、Web 硬编码 SECRET（auth.py:11）、默认口令（server.py:17-22）、CORS 通配（server.py:44-50）、42 个死模块、172 处吞异常、UI 线程阻塞、CSV 导入统计错误（import_export.py:183-184）。
- **旧审计数字过时**：`docs/review/02_studio.md` 的 130 文件 / 37,280 行 / 81 可达模块 → 现 134 / 39,262 / 92（代码在增长，结论方向不变）。`docs/DEAD_CODE.md` 的"core 33 不可达、ui 6 孤儿面板"与本次 AST **完全一致**，该清单仍准确。

---

## 八、P0–P3 风险清单

### P0（必须修）
| # | 问题 | 证据 |
|---|---|---|
| P0-1 | Web JWT 密钥硬编码 + 自研实现 → 可伪造任意角色（实测伪造 admin 成功） | `web/auth.py:11,24-67`【实测】 |
| P0-2 | Web 默认口令硬编码 + 启动明文打印 + 默认绑定 0.0.0.0 → 未改密即被接管 | `web/server.py:17-22,73,90-93`；`web/start.py:11`【源码】 |
| P0-3 | Web 已认证 SQL 注入（table/db/order_by/列名全部 f-string 拼接反引号标识符） | `web/api.py:187,309-312,325-344,347-369,372-391`【源码】 |
| P0-4 | Web 版无连接管理端点，`lightdb-studio-web` 启动后无法查询任何库 | `web/start.py:23`；`web/server.py:15-25`；`api.py:66-87`；实测 `/query` 404【实测】 |

### P1（高优）
| # | 问题 | 证据 |
|---|---|---|
| P1-1 | Studio 连接密码明文落盘 `~/.lightdb_studio/connections.json` | `config.py:20,118-126`【实测】 |
| P1-2 | AI api_key 明文落盘 | `core/ai_integration.py:12,246-264`【源码】 |
| P1-3 | CORS `*` + `allow_credentials=True`（实测回显 evil origin） | `web/server.py:44-50`【实测】 |
| P1-4 | `/connections/test` 任意认证用户可让服务端连任意 host:port（SSRF/内网探测） | `web/api.py:146-157`【实测】 |
| P1-5 | UI 线程同步执行 SQL + `fetchall()` 全量 + 无读超时 → 慢查询/大结果冻结界面 | `main_window.py:951-1027`；`db/connection.py:216-233,165-176`；全仓 QThread=0【实测+源码】 |
| P1-6 | 40% except 处理器吞异常（172/429），静默失败/空节点/数据丢失 | 本次 AST 全量统计 + `object_tree.py:140-343`【源码】 |
| P1-7 | "生产保护"未接线：环境切换只切库，写操作无二次确认 | `environments.py:172-212` 无调用者；`main_window.py:1204-1214`【源码】 |

### P2（中优）
| # | 问题 | 证据 |
|---|---|---|
| P2-1 | 后台监控/调度线程与 UI 共用同一 DBConnection（RLock 串行化 → 互相阻塞；autocommit 无事务 → 跨线程语句交错） | `db/connection.py:154,222,244`；`core/scheduler.py:219` 等【源码+推断】 |
| P2-2 | 报表默认 0 模板；Web `/reports` 空、`run` 404；模板需手工建 | 实测 `templates: 0`；`reports.py:55-74`【实测+源码】 |
| P2-3 | 云同步 XOR 加密无认证/无 MAC，错误密码静默产出乱码 | `cloud_sync.py:24-50`【实测+源码】 |
| P2-4 | 团队配置 XOR + 硬编码默认钥 `lightdb_default` | `team_config.py:84-85,101-102`【源码】 |
| P2-5 | 42 个死模块 / 36.5% 行（33 个 core"数据平台愿景"模块），维护成本高、误导文档 | §3.2【AST】 |
| P2-6 | Web 无撤销/登出/refresh，token 7 天；`_session_conns` 无上限无关闭 | `auth.py:24-25`；`api.py:63-87`【实测+源码】 |
| P2-7 | `/api/v1/history` 任意已登录用户读全局历史（他人 SQL 泄露） | `api.py:396-402`【实测】 |
| P2-8 | 结果集无分页/无过滤；Excel 兼容复制/大文本查看均为死代码 | `result_table.py`；`data_grid_enhanced.py`【AST】 |

### P3（低优/清理）
| # | 问题 | 证据 |
|---|---|---|
| P3-1 | `get_tables(db)` 忽略 `db` 参数（公共 API 误导） | `db/connection.py:272-274`【源码】 |
| P3-2 | 对象树对 `SHOW CREATE PROCEDURE/FUNCTION/TRIGGER/PARTITION` 等非标准语法不发/被吞 → 节点恒空 | `object_tree.py:308-312,407-429`【源码】 |
| P3-3 | CSV 导入成功统计不看返回值（整批失败也报成功） | `import_export.py:183-184`；`db/connection.py:251-259`【源码】 |
| P3-4 | 插件系统 0 个随仓库插件；`example_plugin.py` 不在扫描目录且死代码 | `main_window.py:168-171`（扫描 `仓库根/plugins/`，空）；实测 0【实测】 |
| P3-5 | 调度器非 cron 表达式（仅预设周期） | `scheduler.py:31-36`【源码】 |
| P3-6 | `dashboards/`、`lightdb_import/` 与 Studio/Web 无关但被放在同一仓库，易被误读为产品能力 | §1.1【源码】 |
| P3-7 | 依赖不一致：requirements 声明 python-jose/passlib 未用；pydantic v2 下 `.dict()` 弃用 | `requirements_web.txt`；`api.py:150`【源码】 |

---

## 附录 A：实测环境与限制

- Python 3.11.9 / PySide6 6.6.0 / pymysql / fastapi / httpx 0.28.1 / openpyxl 3.1.5 已装；`websockets` **未装**（`requirements_web.txt` 有，属环境限制）。
- GUI 离屏测试：`QT_QPA_PLATFORM=offscreen`，构造 `QApplication+AppConfig+MainWindow`，`QTimer.singleShot(500, app.quit)` 后退出（未长期跑事件循环）。
- **沙箱限制**：DSH workspace-write 下 GUI 直接写 `%USERPROFILE%\.lightdb_studio` 会被拒（环境限制，非产品缺陷）；测试中把 `USERPROFILE/HOME` 重定向到工作区内 `_studio_web_tmp/home*` 完成配置落盘验证。shell 子进程还被拒写 `docs/**`，故运行日志落在工作区根 `_studio_web_tmp/`，报告与脚本仍按要求在 `docs/analysis_2026-10-19/tmp/studio-web/`。
- LightDB 服务端：`python -m lightdb.main --mode single --host 127.0.0.1 --port 18899 --data-dir _studio_web_tmp/srvdata`（临时，测后 kill）；端到端验证了 Studio DB 层与 GUI 执行路径。**引擎侧发现（供 engine 同学参考，不计入 Studio 缺陷）**：LightDB 不支持 `CREATE DATABASE`（返回 1064 语法错误），`DROP/CREATE DATABASE` 类管理语句缺失。

## 附录 B：可复现命令（脚本在 `docs/analysis_2026-10-19/tmp/studio-web/`）

```powershell
# 1) AST 可达性/吞异常/实例化统计
python docs\analysis_2026-10-19\tmp\studio-web\ast_scan.py
# 2) 离屏 GUI + 真实功能抽查（需先起 18899 服务端则跳过 DB 部分）
python docs\analysis_2026-10-19\tmp\studio-web\gui_probe.py
# 3) Web 端点/认证/CORS/伪造 token/SSRF（FastAPI TestClient）
python docs\analysis_2026-10-19\tmp\studio-web\web_probe.py
python docs\analysis_2026-10-19\tmp\studio-web\web_extra.py
# 4) Studio DB 层 + GUI 查询路径端到端（连 127.0.0.1:18899）
python docs\analysis_2026-10-19\tmp\studio-web\db_e2e_probe.py
# 5) 桩函数/死 UI 扫描
python docs\analysis_2026-10-19\tmp\studio-web\stub_scan.py
```

## 附录 C：本次关键实测输出摘要

```
[MainWindow] 6 menus: 文件/编辑/查询/表/工具/帮助; 42 actions; 15 toolbar; docks=2;
             right_tabs=['历史','片段','AI','Agent','协作','目录']; PLUGINS loaded: 0
[DB e2e]     CONNECT OK server_version=5.7.30-LightDB; CREATE TABLE/INSERT/SELECT/GROUP BY OK;
             错误字段保留: {'errno':1064,'sqlstate':'42000',...}; MainWindow._execute_sql x3 = 112.3ms
[Web]        GET / 200; POST /login(admin/admin123) 200; /me 无token 401;
             FORGED admin token /me -> 200 {"username":"attacker","role":"admin"};
             CORS evil origin -> access-control-allow-origin: https://evil.example.com +
             allow-credentials: true; GET /metrics -> 404; POST /query -> 404 连接 'None' 不存在
[Config]     connections.json contains_plaintext=True (SuperSecret123)
[CloudSync]  roundtrip=True; 错误密码解密不抛错（无完整性校验）
[Dead UI]    ImportWizard/CellViewerDialog/PerfPanel 缺必填参数无法构造；MultiResultView/AIPanel 可构造但无任何调用点
```
