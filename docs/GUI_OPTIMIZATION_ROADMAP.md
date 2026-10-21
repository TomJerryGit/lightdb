# LightDB Studio GUI 优化路线图（2026-10-20）

> 依据：2026-10-20 本机全量 GUI+DB 验证（[07_GUI_FULL_VERIFICATION.md](analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md)）、
> 盘面分析（[03_studio_web.md](analysis_2026-10-19/03_studio_web.md)）与源码事实。
> 每条标 **【源码】/【实测】/【建议】**；工作量是工程估算，不是承诺。

## 0. 现状（先看清事实）

**好的部分（【实测】45/45 场景通过）**：主窗口/对象树、查询编辑器、结果表、DataEditor、
表设计器、导入导出、图表、ER 图、快照、报表、SQL 格式化、数据质量、健康度/告警/调度/
脱敏/团队/云同步/插件/审计/血缘/对比/迁移/性能面板、主题切换、连接/设置/关于、AI/Agent/
协作/目录惰性面板，全部能通过真实 UI 路径打开并工作。

**已确认的 3 个 UI 缺陷（【实测】，详见证报告 §5）**：

| # | 缺陷 | 根因（【源码】） |
|---|---|---|
| 1 | 数值列按字符串字典序排序（88.5 排在 270.5 前） | `lightdb_studio/ui/result_table.py:62/113/137` 用 `QTableWidgetItem` 文本排序 |
| 2 | 结果表无过滤 UI | 仅 `setSortingEnabled`；旧 `data_grid_enhanced.column_filter` 已随死代码删除 |
| 3 | 快照同名不去重 | `core/snapshot.py:219` 允许同名建快照；`:679 remove()` 只删首个匹配 |

**架构事实（【源码】）**：

- **UI 线程同步执行 DB 操作**：`rg "QThread|QThreadPool" lightdb_studio` = **0 命中**；
  查询/DDL/导入导出都在主线程，长查询会冻结界面。
- **一次性取全量结果**：`lightdb_studio/db/connection.py:228` `cur.fetchall()`；
  结果表是 `QTableWidget`（按行控件，10 万行会非常慢/占内存）。
- **连接口令明文落盘**：`lightdb_studio/config.py` 的 `ConnectionInfo.password` 原样
  写入 `~/.lightdb_studio/connections.json`（`asdict` 序列化，无加密）。
  已有 `cloud_sync.py` 的导出加密，但**本地存储加密没有**。
- 已有可复用基础：DataEditor 已分页（`data_editor.py:33` 默认 200 行/页）、
  `settings.json` 已有 `page_size`/`max_result_rows` 配置、`db_errors.py` 已能做
  错误码/SQLSTATE 映射、`html`/`csv`/`xlsx` 报表导出已跑通。
- GUI 自动化基础已存在：`_analysis_tmp/gui_full/gui_driver.py`（真实 MainWindow +
  QAction/QTest + 截图，45 场景）可直接裁剪成回归测试。

## 1. 优化原则

1. **先正确性与安全，再性能，再做功能深度**——结果表排错/口令明文属于"用户会踩到的
   硬伤"，优先于花哨面板。
2. **不阻塞 UI**：一切可能超过 ~100ms 的 DB/IO 操作必须可取消、有进度、不冻结主线程。
3. **结果可验证**：每个优化项必须有 offscreen GUI 测试或截图对比；性能数字用可复跑
   基准，不写"感觉更快"。
4. **不重复实现引擎语义**：事务/权限/SQL 语义仍在引擎；GUI 只做展示、交互与安全存储。
5. **不加"演示性假功能"**：现有未接线面板宁可隐藏/降级，也不要伪造数据。

## 2. P0 —— 正确性与安全快赢（✅ 2026-10-20 已完成，见 §9）

| 项 | 做法 | 涉及文件 | 验收 | 估算 |
|---|---|---|---|---|
| **P0-1 数值/日期感知排序** | 用自定义 `SortableItem`（解析 int/float/日期，NULL 恒排最后）或改用 `QSortFilterProxyModel` + 类型化比较 | `ui/result_table.py` | offscreen 测试：amount 列 88.5 < 270.5；NULL 在两端行为固定；列头切换方向正确 | 0.5–1 人日 |
| **P0-2 结果过滤条** | 结果表顶部加过滤框 + 列选择 + 大小写开关；对已加载行即时过滤（不改 SQL） | `ui/result_table.py` | 输入 `paid` 只剩匹配行；清空恢复；与排序叠加正确；无匹配显示空态提示 | 1–2 人日 |
| **P0-3 快照唯一性与去重** | `create_snapshot` 同名自动加 `_2/_3` 或显式报"已存在"；`remove` 删除全部同名（或拒绝），加载旧索引时合并重复项并在 UI 提示 | `core/snapshot.py`、`ui/snapshot_dialog.py` | 单元测试：连续同名创建 → 名字唯一；删除后 `list_all()` 无重复；旧索引迁移不丢文件 | 0.5–1 人日 |
| **P0-4 本地口令加密** | Windows 用 DPAPI（`CryptProtectData`，ctypes，无需第三方依赖）加密 `password` 字段；旧明文 JSON 首次加载自动迁移；非 Windows 回退 `keyring`（可选依赖）或明确警告；UI 只显示掩码 | `config.py`、`db/connection.py` | 单元测试：保存后文件无明文；重新加载能连库；旧明文迁移后仍可连；迁移失败显式告警 | 1–2 人日 |
| **P0-5 连接对话框体验** | 增加"测试连接"按钮（复用 `connect()` 带超时）、口令显示/隐藏、失败原因区分（1045/2003/1049）、自动连接失败不静默（状态栏+错误气泡） | `ui/connection_dialog.py`、`ui/main_window.py` | 错误口令提示 1045；不可达提示 2003；测试连接不崩溃；截图对比 | 1 人日 |
| **P0-6 错误面板统一** | 查询错误在结果区显示错误码+SQLSTATE+可复制原文；连接错误/查询错误/权限错误分色标签 | `ui/query_tab` 相关、`core/db_errors.py` | 非法 SQL 显示 1064/42000；缺表 1146/42S02；一键复制 | 1 人日 |

> P0 合计约 **5–8 人日**，全部可独立提交、可回归测试，不改引擎。

## 3. P1 —— 响应性（P1-1/1-2/1-3/1-4/1-5/1-6 已完成 2026-10-21，见 §10/§11/§12；仅 QAbstractTableModel 完整虚拟化留作后续增强）

| 项 | 做法 | 涉及文件 | 验收 |
|---|---|---|---|
| **P1-1 查询后台执行** | `QThreadPool` + `QRunnable` 包装 `Connection.query/execute`；主线程只更新结果；长查询显示进度/耗时；取消用引擎 `KILL <id>`（`SHOW PROCESSLIST` 已有） | 新增 `ui/workers.py`、`ui/query_tab`、`db/connection.py` | 执行 1 亿行笛卡尔积/慢查询时主窗口仍可拖动、可取消；无跨线程 QWidget 访问（用信号槽） |
| **P1-2 流式/分页结果** | `query_stream(fetchmany(chunk=1000))` + `QAbstractTableModel` 虚拟化（只渲染可见行）；`max_result_rows` 真正生效；超出上限显式提示"已截断" | `db/connection.py`、`ui/result_table.py` | 10 万行结果首屏 <1s、内存有界；滚动流畅；截断有提示、可"继续加载" |
| **P1-3 编辑器增强** | 表/列名自动补全（来自对象树缓存）、SQL 语法错误行高亮（parser 报错位置）、常用片段插入、Ctrl+Enter 执行 | `ui/sql_editor.py`、`ui/main_window.py` | 输入 `SELECT * FROM cus` 可补全 customers；错误行标红；快捷键生效 |
| **P1-4 导入/导出进度与取消** | 分块 INSERT（如 500 行/批）、进度条、取消、失败行报告可导出 | `ui/import_dialog.py`、`ui/export_dialog.py`、`core/migration.py` | 10 万行 CSV 导入 UI 不冻结、可取消；失败行数/原因可见 |
| **P1-5 DataEditor 类型编辑** | 按列类型给编辑器（日期选择器、布尔下拉、NULL 开关、JSON 文本校验）；批量修改事务提交；乐观锁（updated_at/版本列，若表有） | `ui/data_editor.py` | 日期列不能输非法值；NULL 可设；批量 100 行提交原子（失败全回滚） |
| **P1-6 对象树懒加载与右键菜单** | 展开表才加载列/索引/触发器；右键：复制名/查看 DDL/刷新/生成 SELECT；搜索框支持模糊过滤 | `ui/object_tree.py` | 1000 张表首屏 <500ms；右键动作可用；搜索即过滤 |

## 4. P2 —— 功能深度（✅ 全部 9 项已完成 2026-10-21，见 §13–§19）

1. **ER 图/关系图**：当前引擎没有真实外键约束（`CREATE TABLE` 接受 FOREIGN KEY 但
   是否强制需核实【待验证】），ER 图应**只用真实元数据**（主键/索引/列注释）或让用户
   手工登记关系；不要凭命名猜。验收：ER 图节点=真实表、边=真实关系/手工关系。
2. **图表可配置化**：X/Y 轴选择、聚合方式、导出 PNG/SVG、保存图表配置到工作区。
3. **报表设计器**：参数化查询 + 模板保存/加载 + HTML/CSV/XLSX（PDF 可选依赖
   LibreOffice）；验收：2 个模板端到端生成、参数替换正确。
4. **Schema 对比/迁移 UI 产品化**：DBCompare/Migration 从"能打开"到"生成可执行 SQL、
   预览差异、事务执行"；高风险操作二次确认。
5. **快照/备份产品化**：定时快照、保留策略、恢复前预览、恢复失败原子回滚。
6. **Web/GUI 能力对齐**：二者共用 `lightdb_studio/core` 的业务层；Web 先补齐连接管理
   （已实现）与权限；GUI 不重复实现 Web 的 JWT 概念。
7. **可访问性与国际化**：Tab 顺序/快捷键全覆盖、高 DPI 缩放、屏幕阅读器标签；
   `zh_CN`/`en_US` 文案抽取（现有 `language` 设置只存值，需真正加载翻译）。
8. **插件 API**：定义稳定接口（菜单/面板扩展点），当前 `plugin_manager` 只加载 0 个
   插件，先做"安装/启用/禁用/示例插件"闭环。
9. **服务端 TLS/SSL 接线（安全，2026-10-21 CI 暴露的已知限制）**：`MySQLServer`
   在握手阶段识别 `CLIENT_SSL`/SSLRequest，用 `SSLManager.make_server_context()`
   wrap socket 并按 MySQL 协议继续认证；握手宣告 `CLIENT_SSL`；`Config` 增加
   `ssl_cert`/`ssl_key`；恢复 `tests/test_batch14.py::test_ssl` 的端到端断言
   （当前在有机 openssl 环境显式 skip）。验收：pymysql `ssl={...}` 连接成功且
   `SHOW STATUS LIKE 'Ssl_cipher'` 非空。

## 5. P3 —— 工程质量与交付（持续）

1. **GUI 自动化测试进 CI**：把 `gui_driver.py` 裁剪出 8–10 个无网络/无服务依赖的
   offscreen 冒烟场景（启动、tab、对象树、结果表排序/过滤、错误 surface、主题、
   连接对话框），在 GitHub Actions 里跑（Windows runner 已有 Xvfb 等价物：offscreen）；
   截图产物作为 artifact 供人工抽查。**这是防 GUI 回归最高性价比的投入。**
2. **性能预算门禁**：启动 ≤2s、1200 行结果渲染 ≤500ms、10 万行分页首屏 ≤1s、
   空闲内存 ≤300MB；超标打印 WARN（先不进 CI 门禁，稳定后再定阈值）。
3. **发布工程**：PyInstaller 单文件/目录包、代码签名（可选）、版本自动更新通道；
   安装/卸载干净；首次启动引导（建本地 demo 库/连接向导）。
4. **崩溃与日志**：`sys.excepthook` + Qt message handler 写滚动日志；崩溃报告
   默认本地、opt-in 上传；日志中禁止出现口令（现有 `db_errors` 已避敏感信息，需审计）。
5. **用户文档与示例**：快捷键卡、常见错误对照（1045/2003/1146/1062）、
   `run_studio.ps1` 一键本地 demo（当前已可用，文档化）。

## 6. 里程碑与 KPI

| 阶段 | 时间 | 交付 | 成功度量 |
|---|---|---|---|
| M0 | 1–2 周 | P0 六项 | 缺陷 1/2/3 清零；口令文件无明文（脚本检查）；GUI 回归 8 例 pass |
| M1 | 2–4 周 | P1 六项 | 慢查询下 UI 不冻结（event loop 采样 <100ms 卡顿）；10 万行分页可用；导入 10 万行可取消 |
| M2 | 4–8 周 | P2 前五项 | ER/报表/对比/快照至少各 1 个端到端验收；i18n 切换生效 |
| M3 | 持续 | P3 | GUI 冒烟进 CI 且稳定绿；启动/渲染预算达标；安装包可交付 |

## 7. 明确不做（避免重复过去的坑）

- 不做"演示性面板"：未接线的数据平台愿景功能保持隐藏/删除，不伪造数据。
- 不在 GUI 里重写事务/权限/SQL 语义；遇到引擎不支持的能力，显式提示而不是假装成功。
- 不为追求"面板数量"新增功能；每新增一个面板必须同时有：可达入口 + 空态 + 错误态 +
  offscreen 测试。
- 不在 P0/P1 阶段做 UI 大改版（换框架/换主题体系）；先修正确性/响应性，视觉重做放 M2 之后。

## 8. 参考

- 验证报告：[07_GUI_FULL_VERIFICATION.md](analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md)
- Studio/Web 分析：[03_studio_web.md](analysis_2026-10-19/03_studio_web.md)
- 可复用 GUI 驱动：`_analysis_tmp/gui_full/gui_driver.py`（45 场景真实 UI 路径）
- 截图证据：`_analysis_tmp/gui_full/shots/`（77 张）

## 9. 实施记录：P0 已完成（2026-10-20）

六项全部落地，新增/修改文件与验收证据如下（真实 GUI 路径 + offscreen 测试）：

| P0 项 | 实现 | 验收证据 |
|---|---|---|
| 数值/日期感知排序 | `ui/result_table.py`：单元格保存原始值（`Qt.UserRole`），`sort_by()` 类型化 key 重排；NULL 恒排末尾；表头点击双向 | 88.5<270.5、负数/小数、NULL 固定、表头 QTest 点击；`tests/test_studio_p0_ui.py`（12 例） |
| 结果过滤条 | 输入框 + 列下拉 + 大小写 + 清空 + "显示 X/Y 行"；行级 `setRowHidden`（不查库）；空态提示 | `paid` → 300/1200 行、**40.6ms**、清空恢复、排序叠加；同 12 例测试 |
| 快照唯一性/去重 | `core/snapshot.py`：同名自动 `_2/_3` 并返回实际名；`remove` 删全部同名；`list_all()` 去重；旧索引迁移（文件保留）并告警 | p0_snap / p0_snap_2；`tests/test_snapshot_unique.py`（10 例） |
| 本地口令加密 | 新增 `core/credentials.py`：Windows DPAPI（ctypes，无新依赖）/非 Windows Fernet 回退/显式 plain 回退；`config.save()` 加密、`load()` 解密 + 旧明文自动迁移 | `tests/test_studio_credentials.py`（8 例：往返/落盘无明文/迁移/损坏保留连接） |
| 连接测试 + 密码显示 | `ui/connection_dialog.py`：已有"测试连接"（复用 2003/1045 可行动错误提示）；新增显示/隐藏切换 | 对话框 offscreen 测试；`1045→权限、2003→连接` 分类复用 |
| 错误面板统一 | `ui/result_table.py`：查询/权限/连接三类分类 + 复制按钮（写入剪贴板）；顺带修复 `show_message()` 陈旧错误视图缺陷（旧 xfail 转正） | `tests/test_studio_p0_ui.py` + `tests/test_studio_smoke.py`（xfail 移除） |
| 附带修复 | `lightdb_studio/__init__.py` PEP 562 `__getattr__` 用 `from . import X` 会 RecursionError（被 `from lightdb_studio import config` 触发）→ 改 `importlib.import_module` | `test_package_lazy_import_fromlist_does_not_recurse` |

**验证汇总**：P0 新增测试 **30 passed**；Studio/snapshot 回归 **104 passed + 1 xpassed**（原 xfail 已转正）；
真实 GUI + 8898 端到端 `p0_ui_check.py` **4/4 PASS**、10 张截图（`_analysis_tmp/gui_full/shots_p0/`）；
全量 pytest 见 README 测试基线。遗留风险：ResultTable 不再走 Qt 内置排序（外部直接调
`table.sortItems()` 仍是文本序，代码内已改为 `sort_by`）；旧索引迁移同名保留最新、旧 .sql 文件
仍留盘（UI 告警）；非 Windows 平台无 cryptography 时退化为显式 `plain:` 并告警。

## 10. 实施记录：P1 首批（P1-1/P1-2）已完成（2026-10-21）

| P1 项 | 实现 | 验收证据 |
|---|---|---|
| P1-1 后台查询 | 新增 `ui/workers.py`（QThreadPool+`QueryWorker`/`QuerySignals`，DB 调用全部在 worker 线程，信号回主线程）；`MainWindow._execute_sql_async()` 接入 F5/菜单/编辑器 Ctrl+Return/对象树双击/EXPLAIN；执行中禁用执行动作+状态栏“执行中…”，完成恢复；同步 `_execute_sql()` 保留兼容 | 慢查询 0.5s 期间 QTimer 触发 ≥8 次（UI 不冻结）；`tests/test_studio_async_query.py` 9 例 |
| P1-1 取消 | “查询 → 取消当前查询”QAction；记录 `thread_id()`，另一条 root 连接 `KILL <id>`，显式提示“已取消”；KILL 后自动重连+3s watchdog；连接状态清理 | 取消后状态显式、重连后查询成功；engine 无 KILL QUERY 的限制写进注释/风险 |
| P1-1 超时 | `settings.query_timeout>0` → QTimer 触发取消（同取消路径），显式“查询超时” | 1s 超时 + 1.8s 慢查询用例 |
| P1-2 有界结果 | `db/connection.py::query_chunked()`（`fetchmany` 分块、`max_rows` 截断 + 多读一行判断 truncated）；`ResultTable.show_result(truncated=...)` 提示；`加载更多`按钮信号 → worker 取更大上限追加并重新应用过滤/排序；不重写 QAbstractTableModel（后续项） | `max_result_rows=100` → 100 行+截断提示；加载更多 200→250 后按钮隐藏；行序正确 |
| 回归 | P0 排序/过滤/错误分类/复制保持；`test_studio_smoke/p0_ui/startup_lazy/autoconnect/db_errors/error_surface` + 新测试 **69 passed**；compileall OK | 真实 GUI `p1_ui_check.py` **5/5 PASS**、4 张截图（`_analysis_tmp/gui_full/shots_p1/`） |

**未完成/后续**：取消是 KILL 连接（服务器端语句可能继续收尾）；“加载更多”重跑同一 SQL
取更大上限（无持久游标，无 ORDER BY 时可能重复/遗漏）——完整 QAbstractTableModel 虚拟化、
编辑器补全/错误高亮、导入导出进度、DataEditor 类型编辑、对象树懒加载仍属 P1 剩余项。

## 11. 实施记录：P1 第二批（P1-3/P1-6）已完成（2026-10-21）

| P1 项 | 实现 | 验收证据 |
|---|---|---|
| P1-3 自动补全 | `ui/sql_editor.py`：QCompleter + QStringListModel，候选=SQL 关键字+表名+列名；上下文感知（`表.` 前缀、最近 FROM/JOIN 表只给该表列）；`settings.editor_auto_complete=False` 时不弹且不读 schema；Ctrl+Space 手动触发；Ctrl+Return/F5 保留 | 假 schema 候选正确；`表.` 上下文只给该表列；关闭时 provider 调用数 0；Ctrl+Return 仍触发执行信号 |
| P1-3 错误位置高亮 | 解析 `near '<token>'` / `at line N` / `Unknown column '<c>'`，在 SQL 中定位后选中 + 红底红下划线 ExtraSelection；定位不到只清空、不伪造；成功/新 SQL 清除 | `near 'SELCT 1'` → 选中 token、line=2、ExtraSelection≥2；`Unknown column`/`at line` 均定位；token 不在 SQL 返回 None 且不高亮 |
| P1-6 对象树懒加载 | `ui/object_tree.py`：表节点展开才 `get_columns` + `SHOW INDEX`/`SHOW TRIGGERS`，结果进 `_table_cache`；重复展开命中缓存、右键“刷新此表”强制重查；列节点带类型/主键标记 + 索引/触发器文件夹 | 未展开 get_columns=0；展开一次=1；折叠再展开仍=1；强制刷新=2；100 张表展开 0 列查询（~百 ms） |
| P1-6 右键菜单 | 表：复制表名 / 生成 `SELECT * FROM t LIMIT 200` 到新查询页 / 查看建表语句 / 查看列 / 刷新此表 / 统计行数 / 删除 / 清空；表文件夹：刷新 / 复制库名；库：切换 / 复制 / 刷新；`build_context_menu()` 可测试 | 菜单文本齐全；复制→剪贴板；生成 SELECT→新 tab 内容正确；DDL→`SHOW CREATE TABLE` |
| 接线 | `ui/main_window.py`：schema 缓存后台线程刷新（连接建立/DDL/手动，≤200 表）+ 编辑器 schema provider；`new_query_requested` → 新查询页填 SQL；错误→高亮、成功→清除；设置开关传播 | 新测试 **19 passed**；Studio 回归 + 新测试 **88 passed**；compileall OK |
| GUI 证据 | `_analysis_tmp/gui_full/p1_editor_tree_check.py` | **3/3 PASS**、6 张截图（`shots_p1b/`）；既有 77/10/4 张证据未动 |

**风险**：①补全上下文是启发式（`表.`/最近 FROM-JOIN），无 parser 级作用域；②schema 缓存
无 TTL（连接/DDL/手动刷新触发），大库首次候选可能延迟；③错误 token 取首个匹配，重复 token
可能指向第一处；④右键菜单测试直接调 handler、真实菜单位路径由 GUI 证据覆盖。
**P1 剩余**：QAbstractTableModel 虚拟化（其余已在本节与 §12 完成）。

## 12. 实施记录：P1 第三批（P1-4/P1-5）已完成（2026-10-21）

| P1 项 | 实现 | 验收证据 |
|---|---|---|
| P1-4 导入进度/取消 | `ui/import_export.py`：`chunk_rows`/`execute_chunked_import`（500 行/批、进度回调、取消语义、批失败逐行重试定位失败行）、`ImportWorker(QRunnable)`；`ImportCSVDialog` 后台分块 + 进度条 + 取消 + 失败行报告按钮（CSV 行号/错误/值可导出） | 1000 行 → 批次 `[500,500]`、进度 `(500,1000)→(1000,1000)`；取消后仅 1 批且显式 `cancelled=True`（已提交批不回滚）；失败行 `[3,5]`、成功 998、报告 CSV 正确 |
| P1-4 导出进度/取消 | `write_export_chunked`（CSV/XLSX 分块、进度、取消/失败删除不完整文件）、`ExportWorker`；`ExportCSVDialog` 增加 CSV/XLSX 格式选择 + 后台导出 + 进度 + 取消 | CSV 1200 行分块（500/批）、XLSX 正常；取消 → `ExportCancelled` + 文件删除 + 信号 |
| P1-5 DataEditor 类型编辑 | `editor_kind_for_type` + `validate_typed_value`（DATE/DATETIME 校验规范化、BOOL、显式 NULL、INT/FLOAT 数值、JSON 非法报错）；`TypedCellDelegate` 按列提供 `QDateEdit/QDateTimeEdit/QComboBox/QLineEdit`；非法单元格标红+tooltip+阻止提交（0 SQL） | `INT→int`、`DATE→QDateEdit`、`BOOL→QComboBox`、`DOUBLE→QLineEdit+validator`、`JSON→QLineEdit`；非法 JSON/日期/数值均拒绝且 0 SQL |
| P1-5 原子提交 | `build_pending_statements()` / `execute_atomic()` / `commit_statements()`：BEGIN→逐条 UPDATE→COMMIT，失败 ROLLBACK+显式提示+刷新；单条与批量同路径 | 多单元格合并 1 条 UPDATE：`BEGIN→UPDATE→COMMIT`；注入失败：`BEGIN→UPDATE→ROLLBACK` + `rolled_back=True`；单条同路径 |
| 附带修复 | `db/connection.py::error_message()` 优先取 `DBQueryError.message`；修复 errno=None 时 `__str__`↔`format_db_error` 相互递归（P1 真实服务端探测暴露） | `tests/test_studio_db_errors.py::test_dbqueryerror_with_no_errno_does_not_recurse` |
| 验证 | 新测试 **19 passed**；Studio 回归 + 新测试 **107 passed**；compileall OK；真实 GUI `p1_import_editor_check.py` **3/3 PASS**、5 张截图（`shots_p1c/`） | 既有 77/10/4/6 张证据未动 |

**风险**：①导入取消不回滚已提交批（UI/文档显式）；失败行号为导入顺序号；②XLSX 用普通
Workbook 分批 append（超大结果集内存高于 write_only）；③DataEditor 事务依赖引擎
BEGIN/COMMIT、无主键表仍拒绝编辑；④非法值只前端拦截，DB 类型错误仍由事务失败回滚提示。
**P1 剩余**：仅 QAbstractTableModel 完整虚拟化（当前有界 fetchmany+截断+加载更多，够用）。

## 13. 实施记录：P2-2/P2-3 已完成（2026-10-21）

| P2 项 | 实现 | 验收证据 |
|---|---|---|
| P2-2 图表可配置化 | `ui/chart_view.py`：`ChartConfig` / `parse_chart_config`（兼容旧单 y/中文类型聚合名）；多 Y 系列 + SUM/AVG/COUNT/MIN/MAX 聚合（重复 X 聚合、NULL 忽略、唯一 X 原值）；X/Y/类型/标题实时重绘 + 图例；无有效数值列显式提示；PNG/SVG 导出（QImage/XML 真实校验）；配置 JSON save/load | 5 种聚合在重复 X/NULL/非数值列上正确；PNG 14.5KB 可读、SVG 4.7KB XML 可解析；旧配置 `{'y':'sales','type':'折线图','agg':'平均'}` 可规范化；配置往返 |
| P2-3 报表参数化/模板 | `core/reports.py`：`extract_sql_params`（`:name`/`?`，跳过字符串/注释/反引号）、`bind_params`（`:name`→pymysql `%()s`，缺参/多参/混用显式 `ReportParameterError`）、模板 `params`/`preview_limit` + `validate/upsert/rename/duplicate/remove_by_name/names`、`preview_query/run_query/export_data`、`ReportEngine(base_dir=...)`；旧模板缺字段自动默认 | 参数转换/缺参显式不查库；模板保存校验/CRUD；重开 engine 后 params+chart+preview_limit round-trip；旧 JSON 兼容 |
| P2-3 设计器 | `ui/report_builder.py`：`ParamInputDialog`（无参数不弹、空值缺参显式）；安全 `conn.query(sql, params)`；内嵌预览 50/100/500 行；模板新建/保存校验/重命名/复制/删除；HTML/CSV/XLSX 导出（真实文件校验）；从预览构建图表并写入模板 `chart` | HTML 转义 `<script>`/`&`/NULL；CSV 行数 + utf-8-sig；XLSX openpyxl 可读；模板图表配置持久化 |
| 验证 | 新增 `tests/test_studio_p2_visuals.py`：**24 passed + 1 skipped**（8898 不可用时真实 e2e 显式 skip）；全部 Studio 回归 + 新测试 **132 passed / 1 skipped**；compileall OK；真实 GUI `p2_visual_check.py` **3/3 PASS**、4 张截图（`shots_p2/`） | 既有 77/10/4/6/5 张证据未动 |

**风险**：图表 limit 为原始行截断后再分组；HTML 导出暂未嵌入图表 SVG（`chart_svg` 形参已预留）；
参数解析为启发式扫描（非完整 parser，空白参数视为缺参、qmark 默认值不持久化）；
XLSX 导出沿用整表内存实现。**P2 剩余**：ER 图真实关系、Schema 对比/迁移产品化、快照/备份
产品化、Web/GUI 对齐、i18n/可访问性、插件 API、服务端 TLS 接线。

## 14. 实施记录：P2-1/P2-4 已完成（2026-10-21）

| P2 项 | 实现 | 验收证据 |
|---|---|---|
| P2-1 ER 真实关系 | `ui/er_diagram.py`：从 `SHOW CREATE TABLE` 解析真实 FOREIGN KEY；手工关系 `RelationStore` 持久化 `relations.json`（增删 round-trip）；三态优先级=真实 FK > 手工 > 无关系，**禁止按 `_id`/命名猜测**；表盒显示 PK/FK/UQ/IDX + 连线/箭头；无关系显式提示；PNG 导出 QImage 校验 | 15 个新测试：三态/持久化/无命名猜测/PNG 可读；真实 GUI 截图（真实 FK ER 图、无关系提示、手工关系） |
| P2-4 Schema 对比 | `ui/db_compare.py`：真实快照（`get_columns`+`SHOW INDEX`）差异矩阵（列/类型/NOT NULL/默认值/PK/唯一/索引）；SQL 预览安全子集（`CREATE TABLE`、缺失索引 `CREATE INDEX`、高风险 `DROP TABLE`）；表内变更显式“需手工处理（本引擎 ALTER 限制）”不生成假 ALTER；diff SQL 落盘；高风险二次确认；执行走目标连接并展示结果/错误 | 差异矩阵正确；预览含 CREATE TABLE/INDEX + 手工说明、无 `ALTER TABLE`；diff 800B 落盘；不确认不执行；执行 3 条含二次确认 |
| 验证 | 新增 `tests/test_studio_p2_schema.py`：**15 passed**；全部 Studio 回归 + 新测试 **147 passed / 1 skipped**；compileall OK；真实 GUI `p2_schema_check.py` **3/3 PASS**、5 张截图（`shots_p2_schema/`） | 既有证据未动 |

**风险/限制**：①本引擎 parser 实测不支持独立 `CREATE INDEX`（`parse→None`），SQL 预览仍按需求
生成 `CREATE INDEX` 供参考，但**执行会显式报错**；真正建索引需在 `CREATE TABLE` 内声明或等引擎
支持；②手工关系默认存 `~/.lightdb_studio/relations.json`（可注入路径）；③索引以 `SHOW INDEX`
为准，查询失败回退 columns key；④`DROP TABLE` 属高风险可执行项，已二次确认。
**P2 剩余**：快照/备份产品化（进行中）、Web/GUI 对齐、i18n/可访问性、插件 API、服务端 TLS 接线。

## 15. 实施记录：P2-5 已完成（2026-10-21）

| P2 项 | 实现 | 验收证据 |
|---|---|---|
| 保留策略 prune | `core/snapshot.py::prune(keep_last, max_age_days, dry_run, now)`：dry-run 返回候选不删文件；真实执行删除文件+索引并返回报告；文件删除失败保留索引条目、`ok=False` 且 errors 显式；P0 同名 `_2/_3` 不误删 | keep_last=2 dry-run 只列候选；真实删除最旧文件+索引；max_age 按天删；`dup/dup_2/dup_3` 只删最旧；注入删除失败 → ok=False + errors + 条目保留 |
| 定时快照 | 快照目录 `schedule.json`（enabled/interval_minutes/keep_last/max_age_days/name_prefix/last_run/last_error）；`due(now)`、`schedule_status()`、`run_due(conn)`（到点创建 `prefix_时间戳`、更新 last_run、执行保留清理；失败写 last_error 不标记成功）；**主窗口 60s QTimer 调 `run_due`**（无需常驻线程；未启用/无连接跳过，失败写 stderr） | schedule.json round-trip；due 判断；run_due 创建 auto_* 并更新 last_run、二次调用不创建；`test_main_window_snapshot_tick_wires_run_due` 验证定时器接线 |
| 恢复预览 | `preview_restore(snapshot)`：CREATE/DROP/INSERT 语句计数、涉及表、风险摘要；文件缺失显式失败 | CREATE=2/DROP=2/INSERT=3、表 t1/t2、风险含 DROP/CREATE/INSERT |
| 恢复原子回滚 | `restore_snapshot(..., dry_run, create_emergency, rollback_on_failure)`：dry_run 只预览；真实恢复前自动 `pre_restore_<ts>` 紧急快照（失败中止恢复）；恢复失败自动回滚并显式 `rolled_back`/`rollback_error`（回滚再失败只显式报告，不二次回滚） | dry_run 0 SQL/不建紧急快照；成功恢复自动建紧急快照并落盘；注入 INSERT 失败 → ok=False、rolled_back=True、紧急快照存在；回滚持续失败 → rollback_error 显式；紧急快照自身失败 → 中止恢复、0 SQL |
| UI | `ui/snapshot_dialog.py`：manager 可注入；保留/定时设置面板；prune 预览+执行；恢复预览；运行到期检查；创建/恢复结果显式含紧急快照/回滚状态 | 设置保存并重读；prune dry-run 预览文本；恢复预览摘要；run_due 到期创建/未启用不创建 |
| 验证 | 新测试 `tests/test_studio_p2_backup.py`：**17 passed**（含主窗口定时器接线）；指定回归 + 新测试 **53 passed**；全 studio 套件 **163 passed / 1 skipped**；compileall OK；真实 GUI `p2_backup_check.py` **4/4 PASS**、4 张截图（`shots_p2b/`） | 既有证据未动 |

**风险**：①无后台常驻线程（QTimer 60s + 手动“运行到期检查”）；②回滚用紧急快照按语句顺序执行，
回滚自身再失败仅显式报告；③prune 按去重后列表计数、删除时清同名旧索引项；④dry_run 只解析不预校验
可执行性（真实执行失败显式返回）。**P2 剩余**：Web/GUI 对齐、i18n/可访问性、插件 API、服务端 TLS 接线。

## 16. 实施记录：服务端 TLS/SSL 已接线（安全项，2026-10-21）

此前 CI 首跑把「服务端 TLS 未接线」暴露为已知限制；本批已真正实现：

| 项 | 实现 | 验收 |
|---|---|---|
| 协议握手 | `mysql_protocol.make_handshake_v10(..., ssl_enabled)` 在能力位宣告 `CLIENT_SSL` | `tests/test_wire_ssl.py`：pymysql 带 `ssl={...}` 握手成功 |
| SSLRequest 处理 | `MySQLHandler.do_handshake()`：收到 32 字节且含 `CLIENT_SSL` 的 SSLRequest → `SSLContext.wrap_socket(server_side=True)` → 在 TLS 通道内读取并继续认证；明文客户端不受影响（TLS 可选） | TLS 查询 `SELECT 1`/DDL/DML 通过；明文客户端仍可连 |
| 证书来源 | `Config.ssl_cert`/`ssl_key` 显式路径；未给时 `<data_dir>/ssl` 已有证书或 `SSLManager`（openssl CLI）自动生成；都不可用 → **构造 MySQLServer 时显式 RuntimeError**，绝不静默降级为明文 | `test_ssl_enabled_with_bad_cert_fails_explicitly`（显式给不存在路径，避免依赖 openssl 是否存在） |
| 配置/环境变量 | `LIGHTDB_SSL_CERT` / `LIGHTDB_SSL_KEY`（新增） | Config 字段测试 |
| 回归 | 恢复 `tests/test_batch14.py::test_ssl` 端到端断言（有机 openssl 环境真实跑，不再 skip） | 本地/CI 均通过 |

测试：新增 `tests/test_wire_ssl.py`（3 passed，`cryptography` 生成自签证书，不依赖 openssl CLI）；
`test_batch14` SSL 用例恢复断言；`test_wire_ssl + test_batch14 + test_mysql_connect +
test_wire_transaction` = 19 passed。**P2 剩余**：Web/GUI 对齐、i18n/可访问性、插件 API。

## 17. 实施记录：P2-8 插件 API 已完成（2026-10-21）

| P2 项 | 实现 | 验收证据 |
|---|---|---|
| 示例插件 | 新增 `plugins/example_plugin.py`：`PluginBase` 子类，菜单「示例：插入 SELECT 1」+ 工具栏「✨ 示例插件」+ 安全 `process_sql`（仅 rstrip）+ on_load/on_unload 日志 | 加载成功、name/version 正确、菜单/工具栏文本正确、回调安全触发 |
| PluginManager 产品化 | `PluginInfo.status=loaded/disabled/failed`；`load_all` 每插件独立失败、error 可见且不影响其他；`enable/disable`（disable 调 on_unload、移出 active/菜单/工具栏、状态持久化到可注入 `state_path`，默认 `~/.lightdb_studio/plugins_state.json`）；`reload_one/reload_all`（unload→清 sys.modules→重新 import→on_load，禁用保持禁用）；`build_menu_actions/build_toolbar_actions/install_actions(main_window)`；`add_listener`；process_sql 异常隔离 | 4 类坏插件（语法/无基类/导入异常/继承错）隔离且 error；启停持久化 round-trip；reload 旧实例 unload + sys.modules 清理；菜单/工具栏 action 真安装 QMainWindow；状态文件损坏回退 |
| UI | `ui/plugin_panel.py`：name/version/status/path/error 列表 + 启用/禁用/重载/刷新/打开目录/安装；操作结果显式 | 面板 5 列、启停/重载/刷新/安装结果显式、坏插件 failed+error 可见 |
| 主窗口接线（Lead 补） | `MainWindow._install_plugin_actions()` 在启动恢复（legacy 与延迟两路径）调用 `plugin_mgr.install_actions(self)`；失败写 stderr，不静默；只在启动安装一次避免菜单/工具栏重复 | `test_main_window_installs_plugin_actions_on_startup` |
| 验证 | 新测试 `tests/test_studio_p2_plugin.py`：**16 passed**（含主窗口安装接线）；全 studio 套件 **179 passed / 1 skipped**；compileall OK；真实 GUI `p2_plugin_check.py` **3/3 PASS**、5 张截图（`shots_p2_plugin/`） | 既有证据未动 |

**风险**：reload 会重新实例化插件（实例内存状态丢失）；禁停状态默认写用户目录（可注入）；
主窗口只在启动安装一次，面板启停后不自动重装（避免重复 action，后续可加 listener 清理）。
**P2 剩余**：Web/GUI 能力对齐（i18n/可访问性已在本轮完成，见 §18）。

## 18. 实施记录：P2 i18n/可访问性已完成（2026-10-21）

| 项 | 实现 | 验收证据 |
|---|---|---|
| i18n 字典 | `utils/i18n.py`：zh_CN/en_US 扩到 **134 key 且集合完全一致**；新增 `t_strict`/`missing()`/`missing_keys()`/`missing_report()`/`check_coverage()`/`key_set()`/`reset_missing()`；未知 key 原样返回并记录、格式化失败记录、未知语言拒绝并告警 | 两语言 key 集合一致无空值、coverage ok；`t('result.rows', n=3)` 中英格式化正确；未知 key 回退+记录；`t_strict` 缺失抛 KeyError |
| 语言热切换 | `main_window.py`：菜单/菜单项/工具栏全部走 i18n key；状态栏 ready/connected/disconnected；查询标签页/编辑器 placeholder/结果空态；`_retranslate_ui()` 切换时即时刷新；设置关闭后重新应用/重译 | 切 en → 菜单 `&File`、工具栏 `▶ Execute`、状态 `Ready`、连接 `Disconnected`、标签 `Query`、空态英文；切回 zh 恢复；已翻译项无 CJK |
| 设置持久化 | `settings_dialog.py`：标题/tab/标签/按钮 i18n；语言 Apply 即 `settings.save()` + `i18n.set_lang()` 立即切换；高 DPI 行明确写“自动（由系统/Qt6 High-DPI 控制）”，不提供假设置 | 设置切 en → `settings.json` 写入、`Settings.load()` round-trip、主窗口立即英文；新窗口读持久化 en；高 DPI 说明存在 |
| 可访问性 | `_setup_accessibility()`：主窗口/对象树/编辑器/结果表/工具栏/状态栏 accessibleName+tooltip；`_setup_tab_order()`：对象树→编辑器→结果表→中央标签→右侧面板；QAction 文本/提示非空 | a11y 非空、Tab 顺序 5 控件无重复且顺序正确；执行/取消动作文本+提示非空 |
| 验证 | 新增 `tests/test_studio_p2_i18n_a11y.py`：**8 passed**；全 studio 套件 **188 passed / 1 skipped**；compileall OK；真实 GUI `p2_i18n_check.py` **3/3 PASS**、5 张截图（`shots_p2_i18n/`：中文主窗口、设置 zh/en、英文主窗口、切回中文） | 既有证据未动 |

**风险**：本轮覆盖主窗口+设置对话框；历史面板/env bar/插件/快照等其余面板文案仍中文（后续批次）；
高 DPI 依据 Qt6 自动策略不覆盖；主题/字体品牌名保持双语。**P2 剩余**：Web/GUI 能力对齐。

## 19. 实施记录：P2 Web/GUI 能力对齐已完成（2026-10-21）→ **P2 全部完成**

| 项 | 实现 | 验收证据 |
|---|---|---|
| 共享 core：SQL 格式化 | `POST /api/v1/sql/format` 复用 `core.sql_formatter`；空 SQL/异常显式 400 | 正常格式化/minify；空 SQL 400 `empty_sql` |
| 共享 core：报表 | `web/api.py` 新增模板 CRUD（GET/POST/DELETE `/reports/templates`）、参数化 `preview`、`export?format=html|csv|xlsx`；内部用 `core.reports.ReportEngine`（validate/参数绑定/preview/export_data）；模板目录可注入（`create_app(reports_dir=...)` / `LIGHTDB_WEB_REPORTS_DIR`）；`FileResponse` 正确 Content-Type/文件名+后台清理 | 模板 save/list/upsert/delete round-trip（tmp 落盘）；named 参数绑定为 `%(name)s`；缺参/多参 400；模板 `preview_limit` 生效；三格式导出真实文件（openpyxl 可读 xlsx） |
| 共享 core：数据质量 | `POST /api/v1/quality/check` 复用 `core.data_quality.DataQualityChecker`；返回 issues+summary；缺表/非法表名/查询失败显式 400 | 200+summary 合计=total；缺表 400 `table_required`；注入式表名 400 |
| Parity 矩阵 | `web/api.py::WEB_GUI_PARITY` 12 项（4 full / 2 partial / 6 not_applicable，含 shared_core 与原因）；文档 `docs/WEB_GUI_PARITY.md` 由矩阵生成 | 测试断言 key 齐全/状态合法/full/partial 带端点；共享 core ≥3 |
| 认证/错误 | 新端点沿用 `verify_token` / `require_permission('write')`；统一结构 JSON 错误、不静默 500 | 未认证 401、viewer 保存 403、非法参数 400 |
| 验证 | 新增 `tests/test_web_gui_parity.py`：**10 passed**；现有 web 安全 22 + 新 = **32 passed**；compileall web+studio OK；真实 TestClient `p2_web_parity_check.py` **4/4 PASS**（报告+parity_matrix.json+html/csv/xlsx 产物） | 既有证据未动 |

**风险**：Web 前端 `static/` 本轮未加新入口（先对齐 API）；报表/质量端点复用 `_get_conn`，真实环境需先配连接；
quality 逐列查询较慢；GUI-only 能力在 parity 中标 `not_applicable` 并说明，不假装 Web 已支持。
**P2（9 项）全部完成**（P2-1/2/3/4/5/8 + 服务端 TLS + i18n/可访问性 + Web/GUI 对齐）。

