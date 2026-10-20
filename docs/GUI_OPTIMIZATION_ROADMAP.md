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

## 2. P0 —— 正确性与安全快赢（1–2 周，建议先做）

| 项 | 做法 | 涉及文件 | 验收 | 估算 |
|---|---|---|---|---|
| **P0-1 数值/日期感知排序** | 用自定义 `SortableItem`（解析 int/float/日期，NULL 恒排最后）或改用 `QSortFilterProxyModel` + 类型化比较 | `ui/result_table.py` | offscreen 测试：amount 列 88.5 < 270.5；NULL 在两端行为固定；列头切换方向正确 | 0.5–1 人日 |
| **P0-2 结果过滤条** | 结果表顶部加过滤框 + 列选择 + 大小写开关；对已加载行即时过滤（不改 SQL） | `ui/result_table.py` | 输入 `paid` 只剩匹配行；清空恢复；与排序叠加正确；无匹配显示空态提示 | 1–2 人日 |
| **P0-3 快照唯一性与去重** | `create_snapshot` 同名自动加 `_2/_3` 或显式报"已存在"；`remove` 删除全部同名（或拒绝），加载旧索引时合并重复项并在 UI 提示 | `core/snapshot.py`、`ui/snapshot_dialog.py` | 单元测试：连续同名创建 → 名字唯一；删除后 `list_all()` 无重复；旧索引迁移不丢文件 | 0.5–1 人日 |
| **P0-4 本地口令加密** | Windows 用 DPAPI（`CryptProtectData`，ctypes，无需第三方依赖）加密 `password` 字段；旧明文 JSON 首次加载自动迁移；非 Windows 回退 `keyring`（可选依赖）或明确警告；UI 只显示掩码 | `config.py`、`db/connection.py` | 单元测试：保存后文件无明文；重新加载能连库；旧明文迁移后仍可连；迁移失败显式告警 | 1–2 人日 |
| **P0-5 连接对话框体验** | 增加"测试连接"按钮（复用 `connect()` 带超时）、口令显示/隐藏、失败原因区分（1045/2003/1049）、自动连接失败不静默（状态栏+错误气泡） | `ui/connection_dialog.py`、`ui/main_window.py` | 错误口令提示 1045；不可达提示 2003；测试连接不崩溃；截图对比 | 1 人日 |
| **P0-6 错误面板统一** | 查询错误在结果区显示错误码+SQLSTATE+可复制原文；连接错误/查询错误/权限错误分色标签 | `ui/query_tab` 相关、`core/db_errors.py` | 非法 SQL 显示 1064/42000；缺表 1146/42S02；一键复制 | 1 人日 |

> P0 合计约 **5–8 人日**，全部可独立提交、可回归测试，不改引擎。

## 3. P1 —— 响应性（2–4 周）

| 项 | 做法 | 涉及文件 | 验收 |
|---|---|---|---|
| **P1-1 查询后台执行** | `QThreadPool` + `QRunnable` 包装 `Connection.query/execute`；主线程只更新结果；长查询显示进度/耗时；取消用引擎 `KILL <id>`（`SHOW PROCESSLIST` 已有） | 新增 `ui/workers.py`、`ui/query_tab`、`db/connection.py` | 执行 1 亿行笛卡尔积/慢查询时主窗口仍可拖动、可取消；无跨线程 QWidget 访问（用信号槽） |
| **P1-2 流式/分页结果** | `query_stream(fetchmany(chunk=1000))` + `QAbstractTableModel` 虚拟化（只渲染可见行）；`max_result_rows` 真正生效；超出上限显式提示"已截断" | `db/connection.py`、`ui/result_table.py` | 10 万行结果首屏 <1s、内存有界；滚动流畅；截断有提示、可"继续加载" |
| **P1-3 编辑器增强** | 表/列名自动补全（来自对象树缓存）、SQL 语法错误行高亮（parser 报错位置）、常用片段插入、Ctrl+Enter 执行 | `ui/sql_editor.py`、`ui/main_window.py` | 输入 `SELECT * FROM cus` 可补全 customers；错误行标红；快捷键生效 |
| **P1-4 导入/导出进度与取消** | 分块 INSERT（如 500 行/批）、进度条、取消、失败行报告可导出 | `ui/import_dialog.py`、`ui/export_dialog.py`、`core/migration.py` | 10 万行 CSV 导入 UI 不冻结、可取消；失败行数/原因可见 |
| **P1-5 DataEditor 类型编辑** | 按列类型给编辑器（日期选择器、布尔下拉、NULL 开关、JSON 文本校验）；批量修改事务提交；乐观锁（updated_at/版本列，若表有） | `ui/data_editor.py` | 日期列不能输非法值；NULL 可设；批量 100 行提交原子（失败全回滚） |
| **P1-6 对象树懒加载与右键菜单** | 展开表才加载列/索引/触发器；右键：复制名/查看 DDL/刷新/生成 SELECT；搜索框支持模糊过滤 | `ui/object_tree.py` | 1000 张表首屏 <500ms；右键动作可用；搜索即过滤 |

## 4. P2 —— 功能深度（4–8 周）

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
