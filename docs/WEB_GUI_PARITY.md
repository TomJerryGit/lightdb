# Web / GUI 能力对齐矩阵（2026-10-21）

> 本文件由 `web/api.py::WEB_GUI_PARITY` 生成，矩阵是**代码内单一事实来源**；
> 测试 `tests/test_web_gui_parity.py` 会断言矩阵 key/状态合法性。

Web 与 GUI 共用 `lightdb_studio/core` 业务层；下列端点均复用 core 实现，
不在 Web 侧重复写一套逻辑。

## 1. 对齐矩阵

| key | GUI 能力 | Web 端点 | 状态 | 共享 core | 说明 |
|---|---|---|---|---|---|
| `query_execution` | SQL 编辑器执行查询 | `POST /api/v1/query` | **full** | `lightdb_studio.db.connection` | 参数/标识符校验与权限沿用现有 Web 查询链路 |
| `connection_management` | 连接管理 / 对象树 | `GET/POST /api/v1/connections, GET /databases*, GET /tables*, POST /connections/test` | **partial** | `lightdb_studio.config.ConnectionInfo` | Web 可增改/测试连接与浏览对象；编辑连接对话框是 GUI 专属交互 |
| `object_browser` | 对象浏览器（库/表/列） | `GET /databases, /tables, /columns` | **full** | `lightdb_studio.db.connection` | 表行数统计为 Web 补充能力 |
| `sql_format` | 编辑器「格式化 SQL」 | `POST /api/v1/sql/format` | **full** | `lightdb_studio.core.sql_formatter` | 与 GUI 同一 SQLFormatter；空 SQL 显式 400 |
| `chart` | 数据图表（可配置/PNG/SVG） | — | **not_applicable** | — | 图表依赖 Qt 渲染，Web 端以 API 数据为主；无对应端点 |
| `reports` | 报表设计器（模板/参数/预览/导出） | `GET/POST/DELETE /reports/templates, POST /reports/templates/{name}/preview, POST /reports/templates/{name}/export` | **full** | `lightdb_studio.core.reports` | 参数化 preview/export 复用 ReportEngine；html/csv/xlsx 真实生成 |
| `schema_compare` | Schema 对比 / ER 图 | — | **not_applicable** | — | 依赖 GUI 交互与 Qt 绘图；Web 后续可做 API 版 |
| `snapshot_backup` | 快照/备份/恢复 | — | **not_applicable** | — | 本地文件系统备份属于桌面端能力 |
| `data_quality` | 数据质量检查 | `POST /api/v1/quality/check` | **partial** | `lightdb_studio.core.data_quality` | 复用 DataQualityChecker；Web 当前按单表触发并返回 issues 摘要 |
| `import_export` | CSV 导入/导出（进度/取消） | — | **not_applicable** | — | 大文件流式导入导出与进度 UI 为桌面端能力 |
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
- Web 前端未做真实浏览器 DOM 运行验证（本机无浏览器/Node；静态扫描 + API 契约覆盖）；
- parity 中 GUI-only 能力（图表/ER/Schema 对比/快照备份/插件/i18n 等）标 `not_applicable`
  并说明原因，不假装 Web 已支持。
