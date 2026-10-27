# Web / GUI 能力对齐矩阵（2026-10-27，task-60 更新）

> 本文件由 `web/api.py::WEB_GUI_PARITY` 生成，矩阵是**代码内单一事实来源**；
> 测试 `tests/test_web_gui_parity.py` 会断言矩阵 key/状态合法性。

Web 与 GUI 共用 `lightdb_studio/core` 业务层；下列端点均复用 core 实现，
不在 Web 侧重复写一套逻辑。

## 1. 对齐矩阵

| key | GUI 能力 | Web 端点 | 状态 | 共享 core | 说明 |
|---|---|---|---|---|---|
| `query_execution` | SQL 编辑器执行查询 | `POST /api/v1/query` | **full** | `lightdb_studio.db.connection` | 参数/标识符校验与权限沿用现有 Web 查询链路 |
| `connection_management` | 连接管理 / 对象树 | `GET/POST /api/v1/connections, GET /databases*, GET /tables*, POST /connections/test` | **partial** | `lightdb_studio.config.ConnectionInfo` | Web 可增改/测试连接与浏览对象；列表只读展示 SSH/SSL 状态（不回显口令/私钥）；编辑连接对话框是 GUI 专属交互 |
| `object_browser` | 对象浏览器（库/表/列） | `GET /databases, /tables, /columns` | **full** | `lightdb_studio.db.connection` | 表行数统计、分页/排序数据浏览为 Web 补充能力 |
| `table_designer` | 表设计器（列编辑/AUTO_INCREMENT/索引） | `GET/POST /api/v1/databases/{db}/tables/{table}/design` | **partial** | `lightdb_studio.db.connection` | 预览/保存列增删改、AUTO_INCREMENT、索引增删；主键/唯一变更显式拒绝（引擎 ALTER 变体限制） |
| `result_paging` | 数据网格分页/排序 | `GET /databases/{db}/tables/{table}/data?limit&offset&order_by` | **full** | `lightdb_studio.db.connection` | 返回 total/limit/offset/order_by；前端网格分页排序 |
| `explain` | EXPLAIN 执行计划 | `POST /api/v1/explain` | **full** | `lightdb_studio.db.connection` | 仅 SELECT/WITH；错误显式 400 |
| `sql_format` | 编辑器「格式化 SQL」 | `POST /api/v1/sql/format` | **full** | `lightdb_studio.core.sql_formatter` | 与 GUI 同一 SQLFormatter；空 SQL 显式 400 |
| `chart` | 数据图表（可配置/PNG/SVG） | — | **not_applicable** | — | 图表依赖 Qt 渲染，Web 端以 API 数据为主；无对应端点 |
| `reports` | 报表设计器（模板/参数/预览/导出） | `GET/POST/DELETE /reports/templates, POST /reports/templates/{name}/preview, POST /reports/templates/{name}/export` | **full** | `lightdb_studio.core.reports` | 参数化 preview/export 复用 ReportEngine；html/csv/xlsx 真实生成 |
| `schema_compare` | Schema 对比 / ER 图 | `GET /api/v1/schema/compare?source_db=&target_db=` | **partial** | `lightdb_studio.db.connection` | 只读结构对比（缺失表/列差异/类型）；Web 不做自动同步写入 |
| `snapshot_backup` | 快照/备份/恢复 | `POST /api/v1/backup` | **partial** | `lightdb_studio.db.connection` | 真实生成逻辑 SQL 备份（SHOW CREATE + INSERT）；不含恢复/二进制快照（桌面端文件系统能力） |
| `data_quality` | 数据质量检查 | `POST /api/v1/quality/check` | **partial** | `lightdb_studio.core.data_quality` | 复用 DataQualityChecker；Web 当前按单表触发并返回 issues 摘要 |
| `import_export` | CSV 导入/导出（进度/取消） | `GET .../export?fmt=csv\|sql; POST .../import; POST /api/v1/query/export` | **full** | `lightdb_studio.db.connection` | 表 CSV/SQL 导出、CSV 导入（显式行错误）、查询结果 CSV/SQL 导出；大文件流式进度/取消未做（P2） |
| `plugins` | 插件启用/禁用/菜单注入 | — | **not_applicable** | — | 插件作用于本地 GUI 进程，Web 进程不加载插件 |
| `i18n` | 中英文热切换 | — | **not_applicable** | — | Web 是 API，错误/数据为程序消费；Web UI 文案暂未 i18n |

## 2. 本轮新增的共享 core 端点

### 2.1 `POST /api/v1/sql/format`

- 复用 `lightdb_studio.core.sql_formatter.SQLFormatter`；
- 请求 `{"sql": "...", "minify": false}` → 返回格式化 SQL；空 SQL / 异常显式 400；
- 鉴权：沿用 Web 现有 token 依赖。

### 2.2 报表模板（复用 `core.reports.ReportEngine`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/reports/templates` | 列出模板 |
| POST | `/api/v1/reports/templates` | 保存/更新模板（名称/SQL 非空校验，`invalid_template` 400） |
| DELETE | `/api/v1/reports/templates/{name}` | 删除模板（不存在 404） |
| POST | `/api/v1/reports/templates/{name}/preview` | 参数化预览：`:name`/`?` 经 `%(name)s` 安全绑定；缺参/多参 400；默认用模板 `preview_limit` |
| POST | `/api/v1/reports/templates/{name}/export?format=html\|csv\|xlsx` | 真实生成文件（`FileResponse`，Content-Type/文件名正确，临时文件后台清理）；非法格式 400 |

模板持久化目录可注入（`create_app(..., reports_dir=...)` / `LIGHTDB_WEB_REPORTS_DIR`），
测试使用 tmp 目录。

### 2.3 `POST /api/v1/quality/check`

- 复用 `lightdb_studio.core.data_quality.DataQualityChecker`；
- 返回 `issues` + `summary`（error/warning/info 合计=total）；
- 缺表/非法表名/查询失败显式 400（不静默 500、不默认全库扫描）。

## 3. 验证

- `tests/test_web_gui_parity.py`：10 passed（未认证 401 / format / 模板 CRUD+tmp 持久化 /
  参数化 preview + limit + 缺参 400 / 三格式导出真实文件 / quality / parity 矩阵完整性）；
- `tests/test_web_security.py` 22 + 新测试 = **32 passed**；
- 真实 TestClient 证据：`_analysis_tmp/gui_full/p2_web_parity_check.py` 4/4 PASS，
  产物 `p2_web_parity/`（report JSON、`parity_matrix.json`、html/csv/xlsx）。

### 2.4 Web 前端入口（2026-10-21 接入）

`web/static/` 已把下列能力接成可操作 UI（不再只有 API）：

| 能力 | 入口 | 说明 |
|---|---|---|
| SQL 格式化 | 查询工具栏「🎨 格式化」 | 调 `/sql/format`，成功替换编辑器内容；loading/空 SQL/失败显式提示 |
| 报表模板 | 右侧「报表」Tab | 列表/保存当前 SQL/参数化预览/删除二次确认/HTML·CSV·XLSX 导出（正确文件名） |
| 数据质量 | 右侧「质量」Tab | 表名检查 → summary + issues 表格（级别/分类/表/列/消息） |

前端统一：非 2xx 提取嵌套 `detail`、401 提示登录过期、`setBusy` 防重复提交；
**移除 innerHTML**，后端文本一律 `textContent`/DOM 渲染（防 XSS）。
测试：`tests/test_web_static_features.py`（11 passed，含 TestClient 契约与 OpenAPI
路由对齐）；证据 `_analysis_tmp/gui_full/p3_web_frontend_check.py` 3/3 PASS。

## 4. 已知限制

- 报表/质量端点复用现有 `_get_conn`，真实环境需先配置连接；
- quality 复用逐列查询实现，大表较慢；
- Web 前端未做真实浏览器 DOM 运行验证（本机无浏览器/Node；静态扫描 + TestClient
  API 契约 + 真实 in-process 引擎测试覆盖；Playwright E2E 显式 skip）；
- 仍为 Desktop-only / 限制项：图表渲染、ER 图、二进制快照/恢复、插件、i18n、
  DDL 主键/唯一约束变更、大文件流式导入导出进度/取消（P2）。

## 5. task-60 审计缺口与 P0/P1 实现（2026-10-27）

审计对象：`docs/WEB_GUI_PARITY.md` + `web/api.py` + `web/static/app.js`。
审计结论：此前 Web 只有查询/对象浏览/报表/质量/格式化，缺少表设计器、导入导出、
EXPLAIN、备份、结构对比、数据分页排序与连接安全字段展示；这些是 Navicat/Studio
核心工作流，列为 P0/P1。

| 优先级 | 缺口 | 状态 | 实现 / 端点 |
|---|---|---|---|
| P0 | 表设计器：列编辑、AUTO_INCREMENT、索引、预览/保存 | 已实现（partial） | `GET/POST /api/v1/databases/{db}/tables/{table}/design`；dry_run 预览、逐条执行、真实引擎错误显式；主键/唯一变更仍拒绝 |
| P0 | CSV/SQL 导入导出、查询结果导出 | 已实现 | `GET .../export?fmt=csv\|sql`、`POST .../import`、`POST /api/v1/query/export`；CSV 行错误显式返回 |
| P0 | 结果分页/排序、错误展示一致性 | 已实现 | `/data?limit&offset&order_by&order_desc` 返回 total；前端数据页/分页/排序；统一 `{detail,code}` |
| P1 | EXPLAIN | 已实现 | `POST /api/v1/explain`（仅 SELECT/WITH，显式 400） |
| P1 | 备份入口 | 已实现（partial） | `POST /api/v1/backup` 生成逻辑 SQL 备份（不伪造二进制快照） |
| P1 | 结构同步入口 | 已实现（partial） | `GET /api/v1/schema/compare` 只读对比；不做自动写入 |
| P1 | 连接 SSH/SSL 字段状态展示 | 已实现 | `GET /api/v1/connections` 返回 `ssh/ssl` 状态与 has_ca/has_key；口令/私钥不回显 |
| P2 | 图表/ER、二进制快照恢复、插件、i18n、大文件流式进度/取消 | 未做 | 依赖桌面 Qt/文件系统或需独立流式设计；明确不伪造 |

前端入口：查询工具栏新增「表数据 / 设计表 / EXPLAIN / 导出结果 / 导入 CSV /
备份 / 结构对比」；对象树下方连接 SSH/SSL 状态；数据页含分页、排序、导出；
设计器面板支持列 JSON 预览与 ALTER 保存确认。

测试：`tests/test_web_parity_gaps.py`（10 passed + 1 真浏览器显式 skip；含 fake
核心 API 契约与真实 in-process LightDB 引擎设计器/分页/导出/EXPLAIN/备份 roundtrip）；
回归 `tests/test_web_*.py` 全绿（51 passed, 1 skipped）。
