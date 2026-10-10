# LightDB 死代码与重复实现清单

**生成时间**：2026-10
**数据来源**：[docs/AUDIT_REPORT.md](AUDIT_REPORT.md) 的同一次审计
**重要说明**：本文档**只做清单与建议，不删除任何模块**。判定为「不可达」的模块仍可能被未来的功能接线使用，删除需由维护者决策。

---

## 1. 判定方法

1. 用 AST 解析全部非测试 `.py` 文件，提取 `Import` / `ImportFrom`（含相对导入按 `__init__.py` 逐级回退解析），构建模块导入图。
2. 以 4 个真实入口为根做 BFS：
   - `lightdb.main`（单进程 / `--check` / `--version`）
   - `lightdb.main_v4`（多进程）
   - `lightdb_studio.app`（桌面端）
   - `web.start`（Web 版）
3. 未被 BFS 覆盖的模块记为「不可达」。
4. **人工核对动态导入**，避免误判：
   - [`lightdb/cython/__init__.py:18`](../lightdb/cython/__init__.py#L18) 用 `__import__` 加载二进制扩展；
   - [`lightdb/tools/__init__.py:58`](../lightdb/tools/__init__.py#L58) 用 `importlib.import_module` 按名字加载工具；
   - [`lightdb_studio/ui/__init__.py:146`](../lightdb_studio/ui/__init__.py#L146) 通过模块级 `__getattr__` + `_LAZY` 注册表（51 条）延迟导入面板。
   - [`lightdb/sql/executor.py:82-85`](../lightdb/sql/executor.py#L82-L85) 用 `getattr(self, f'_cmd_{cmd}')` 反射分发，但其 handler 全在 `executor.py` 自身，不涉及文件级导入。
5. 排除目录：`tests/`、`lightdb_import/`；排除根目录临时脚本（`create_project.py`、`_diag_*.py`、`_smoke_db.py`）。

---

## 2. 总量结论

| 指标 | 数值 |
|---|---:|
| 非测试模块总数 | 296 |
| 从 4 个入口可达 | **121** |
| 不可达 | **175** |
| 其中「包 `__init__` 统计假象」 | 约 21 |
| **真实死代码（估算）** | **约 154** |

**什么是「包 `__init__` 统计假象」**：当某模块只以 `from pkg.sub import X` 形式被导入时，Python 会自动执行 `pkg/__init__.py`，但 AST 扫描不会把 `pkg` 本身记为被导入对象。（仅 `lightdb/__init__.py`、`lightdb_studio/__init__.py` 这类被显式导入的包除外。）因此 `lightdb.storage`、`lightdb.sql`、`lightdb_studio.ui` 等「裸包名」出现在不可达列表里属于统计口径造成的假象，**不代表该包的子模块未被使用**。

---

## 3. 按包分组的不可达统计

| 包 / 目录 | 不可达模块数 | 说明 |
|---|---:|---|
| `lightdb_studio.core` | 33 | 大量「企业级」概念模块，仅被 `tests/` 引用 |
| `lightdb`（裸包名） | 19 | 多为包 `__init__` 假象，需逐项甄别 |
| `lightdb.sql` | 15 | 独立 SQL 子系统，多数未接入 executor |
| `lightdb.tools` | 9 | CLI 工具，独立入口 |
| `lightdb.benchmark` | 8 | 基准框架，独立入口 |
| `lightdb.monitor` | 7 | 监控，独立入口 |
| `lightdb.parallel` | 7 | 并行执行，未接入查询路径 |
| `lightdb_studio`（裸包名） | 7 | 含 `lightdb_studio.core` 包 `__init__` 等假象 |
| `lightdb.performance` | 6 | **整包未接线** |
| `lightdb.security` | 6 | **整包未接线**（权限/SSL/审计/白名单等） |
| `lightdb.stability` | 6 | 稳定性工具，独立入口 |
| `lightdb_studio.ui` | 6 | 孤儿面板（见 §5） |
| `lightdb.columnar` | 5 | 列存，未接入查询路径 |
| `lightdb.olap` | 5 | 向量化/OLAP，未接入 |
| `lightdb.profiling` | 5 | 性能剖析工具，独立入口 |
| `lightdb.sql.functions` | 5 | **约 200 个内置函数零生产引用** |
| `lightdb.concurrency` | 4 | 并发原语（`rw_lock`/`atomic`/`ipc`/`lockfree_mvcc`） |
| `lightdb.experiments` | 4 | 实验脚本，本就是独立入口 |
| `lightdb.replication` | 4 | 复制链路 |
| `lightdb.storage` | 4 | `btree_optimized`、`page_prefetch`、`row_format`、`row_format_v4` |
| `lightdb.db` | 2 | `hybrid_table`、`table_v3` |
| `web` | 2 | 见 §6 |
| `lightdb.backup` | 1 | `backup.backup`（`self.db.binlog` 属性不存在，构造即崩） |
| `lightdb.cluster` | 1 | `cluster.lock_coordinator`（docstring 自认「进程内」） |
| `lightdb.cython` | 1 | `cython.setup`（构建脚本，非运行期模块） |
| `lightdb_studio.net` | 1 | `collab_server` |
| `lightdb_studio.plugins` | 1 | `example_plugin` |
| `lightdb_studio.utils` | 1 | `sql_completer`（全库零处导入；`sql_highlighter` 实际由 [`ui/sql_editor.py:11`](../lightdb_studio/ui/sql_editor.py#L11) 使用） |
| **合计** | **175** | |

---

## 4. 服务端（`lightdb/`）重点死代码

### 4.1 整包未接线

- `lightdb/performance/`：`connection_pool.py`、`query_cache.py`、`batch_inserter.py`、`index_only_scan.py`、`parallel.py`、`benchmark.py`。`MySQLServer` 只持有 `config`/`db`/`executor`/`start_time`，**从未引用本包**。
- `lightdb/security/`：`privileges.py`、`ssl_support.py`、`audit.py`、`password_policy.py`、`connection_limit.py`、`ip_whitelist.py`。实现相对完整，但只在 `tests/` 里被实例化 —— 这也意味着**线上没有任何鉴权/审计/白名单**。
- `lightdb/monitor/`、`lightdb/stability/`、`lightdb/benchmark/`、`lightdb/profiling/`、`lightdb/experiments/`、`lightdb/tools/`：这些属于「独立入口」性质（本应由 CLI 触发），可接受，但当前 `lightdb/tools/cli.py` 在 Windows 上因 `import readline`（`cli.py:10`）直接 ImportError。

### 4.2 子系统内部死代码

- `lightdb/sql/`：`predicate_pushdown.py`、`predicate_v4.py`、`icp.py`、`parser_v3b4.py`、`variables.py`、`cursor.py`、`batch_executor.py`、`json_func.py`、`fulltext.py`、`partition.py`、`generated.py`、`cte.py`、`plan_cache.py`、`trigger.py`、`functions/*`。注意 [`sql/executor.py:163`](../lightdb/sql/executor.py#L163) 只用到了 `db.information_schema`，`db/` 目录中也**没有任何**对 trigger / fulltext / generated / check / partition 的引用 —— 即 DML 不会触发触发器、不维护全文索引、不算生成列、不校验 CHECK。
- `lightdb/sql/window.py`：虽被 [`sql/plan_executor.py:627`](../lightdb/sql/plan_executor.py#L627) 懒加载，但优化器从不构造 Window 节点 → 实际不可达。
- `lightdb/storage/`：`btree_optimized.py`（`bulk_insert` 只是 for 循环、`CachedBTree` 无引用）、`page_prefetch.py`（每次访问新建线程，且从不被实例化）、`row_format.py`（活路径实际用 `json.dumps`）、`row_format_v4.py`（依赖未编译的 Cython）。
- `lightdb/db/`：`table_v3.py`（`insert` 只编码不写入、`scan` 用未定义的 `self.bt`）、`hybrid_table.py`（「双写」只 `_pending_rows.append`，且入口 `olap_aggregate` 会触发向量化引擎的 `NameError`）。
- `lightdb/storage/txn.py`：被 [`db/database.py:35`](../lightdb/db/database.py#L35) `new` 出来后不再使用。
- `lightdb/concurrency/`：`rw_lock.py`、`atomic.py`、`ipc.py`、`lockfree_mvcc.py` 四个进程内原语均未接入运行路径（`lockfree_mvcc.prune()` 还会抛 `FrozenInstanceError`）。

---

## 5. 桌面端（`lightdb_studio/`）重点死代码

### 5.1 孤儿 UI 面板（6 个 + 包自身）

以下模块**既不可达、也不在** [`lightdb_studio/ui/__init__.py`](../lightdb_studio/ui/__init__.py) 的 `_LAZY` 延迟注册表（51 条）内，因此没有任何代码路径能加载它们：

```
lightdb_studio.ui.ai_panel
lightdb_studio.ui.cell_viewer
lightdb_studio.ui.data_grid_enhanced
lightdb_studio.ui.import_wizard
lightdb_studio.ui.multi_result
lightdb_studio.ui.perf_panel
```

值得注意的是，[`ui/main_window.py`](../lightdb_studio/ui/main_window.py) 已实际装配约 40 个面板/对话框，说明 UI 层整体是接上的 —— 这 6 个属于**被后续重写取代后残留的旧面板**（例如 `perf_monitor.py` 与 `perf_panel.py`、`import_export.py` 与 `import_wizard.py`、`ai_chat.py`/`agent_chat.py` 与 `ai_panel.py`）。

### 5.2 `lightdb_studio/core/` 中未被 UI 引用的模块（33 个）

```
agent_react      ai_assist        ai_finetune      ai_sql_gen
autonomy         cdc              cloud            data_api
data_contract    data_fabric      data_lake        data_market
data_mesh        data_science     digital_twin     distributed
etl              federated_gov    federation       fullstack_obs
lineage_closure  lineage_v2       lineage_visual   nlb
observability    product_ops      recommender      shadow_table
slo              sql_auto_optimizer  team          virtualization
workflow_visual
```

这些模块**算法本身常是真的**（真 DFS、真哈希 JOIN、真皮尔逊相关），但驱动数据全靠用户手工登记，且没有任何 UI 入口 —— 用户点不到。典型如 `product_ops.py`：`consumer_count` / `query_count` / `last_used` 全代码库无递增点，健康度的「使用活跃度」维度恒为 0 分。

### 5.3 其余桌面端死代码

- [`utils/sql_completer.py`](../lightdb_studio/utils/sql_completer.py)：定义 `SQLCompleter`（`:44`），但**全库零处导入** → [`ui/settings_dialog.py`](../lightdb_studio/ui/settings_dialog.py) 中「自动补全」相关勾选项实际不生效。
- [`net/collab_server.py`](../lightdb_studio/net/collab_server.py)：协作服务器，无调用点（UI 的协作面板仅做本地会话共享）。
- [`plugins/example_plugin.py`](../lightdb_studio/plugins/example_plugin.py)：示例插件；且 [`core/plugin_manager.py:74`](../lightdb_studio/core/plugin_manager.py#L74) 与 [`ui/main_window.py:129-131`](../lightdb_studio/ui/main_window.py#L129-L131) 拼出的插件目录解析为 `D:\lightdb_project\plugins`（不存在，真实目录是 `lightdb_studio/plugins`），**示例插件永远不会被加载**，且启动时会在项目根外新建目录。
- [`core/plugin_manager.py`](../lightdb_studio/core/plugin_manager.py)：模块本身被 `main_window` 导入，但 `get_menu_items` / `get_toolbar_buttons` / `process_sql` 三个钩子**无任何调用点** → 插件无法向菜单/工具栏/SQL 流程注入能力。

---

## 6. `web/` 的 2 个不可达模块

- `web`（包 `__init__`，属统计假象）。
- [`web/metrics.py`](../web/metrics.py)：定义了 `/metrics` 与 `/api/v1/metrics/json`，但 [`web/server.py:53-54`](../web/server.py#L53-L54) 只注册了 `api.router`，**该路由从未挂载**（OpenAPI 中不存在），Prometheus 抓取端点实际不可用。

---

## 7. 重复实现

以下为「同一功能存在多份互不联通实现」的清单。它们不是死代码，但会造成行为不一致与维护陷阱。

### 7.1 数据血缘 —— 5 份实现、3 个不同存储文件

| 模块 | 存储文件 | 粒度 | 是否被 UI 使用 |
|---|---|---|---|
| [`core/lineage.py:15`](../lightdb_studio/core/lineage.py#L15) | `~/.lightdb_studio/lineage.json` | 表级 | ✅ 唯一被 `lineage_view` 使用 |
| [`core/lineage_enhanced.py:16`](../lightdb_studio/core/lineage_enhanced.py#L16) | `~/.lightdb_studio/lineage_v2.json` | 列级 | ❌ |
| [`core/lineage_v2.py:17`](../lightdb_studio/core/lineage_v2.py#L17) | `~/.lightdb_studio/lineage_deep.json` | 对象级 | ❌ |
| `core/lineage_closure.py` | 依赖上述某一份 | 影响面评估 | ❌ |
| `core/lineage_visual.py` | 无（仅做图数据转换） | 可视化 | ❌ |

附带缺陷：[`tests/test_v2_batch1.py:169`](../tests/test_v2_batch1.py#L169) 试图 monkeypatch `ENHANCED_LINEAGE_FILE`，但该类已在 [`lineage_enhanced.py:54`](../lightdb_studio/core/lineage_enhanced.py#L54) 把常量取进实例属性 → 补丁失效。

**建议**：保留 `lineage.py` 作为唯一表级来源，把列级/对象级能力合并为同一存储的不同视图，其余标注为 deprecated。

### 7.2 数据质量 —— 2 份实现、同名类

- [`core/data_quality.py:14`](../lightdb_studio/core/data_quality.py#L14) 定义 `QualityIssue`（v1 规则模型），UI 对应 `data_quality_dialog.py`。
- [`core/quality_v2.py:63`](../lightdb_studio/core/quality_v2.py#L63) **再次定义同名 `QualityIssue`**（v2 规则模型），UI 对应 `quality_v2_dialog.py`。

两个规则模型互不兼容，用户在不同对话框里看到的是两套结果。

**建议**：合并为单一 `QualityIssue`，v2 规则作为 v1 的扩展，保留一个对话框。

### 7.3 AI 客户端 —— 4 份实现，只有 1 份是真 HTTP

| 模块 | 说明 |
|---|---|
| [`core/ai_integration.py`](../lightdb_studio/core/ai_integration.py) | ✅ **唯一真实实现**：用 `urllib` 真调 OpenAI / Claude / Ollama |
| [`core/ai_agent.py:47`](../lightdb_studio/core/ai_agent.py#L47) | 自建 client（UI 的 `ai_panel`/`agent_chat` 用它） |
| [`core/ai_sql_gen.py:64`](../lightdb_studio/core/ai_sql_gen.py#L64) | 自建 client + 示例库 |
| [`core/agent_react.py:135`](../lightdb_studio/core/agent_react.py#L135) | 自建 client；**是真 ReAct 循环 + 8 个真工具**（`_execute_tool` 真调 `conn.query`，含写操作 `:375`），但完全未接 UI |

**建议**：以 `ai_integration.py` 为唯一 provider 抽象层，其余三份改为调用它；`agent_react` 值得接线到 UI（它是这四份里能力最强的）。

### 7.4 告警 / 可观测 —— 6 个模块各写各的 JSON，互不联通

`core/alerts.py`、`core/metrics.py`、`core/performance_monitor.py`、`core/observability.py`、`core/fullstack_obs.py`、`core/slo.py` 各自维护独立存储与采集路径：`alerts.py:141` 的 `add_sample` 需要人工喂样本，`metrics.py:170` 是真采集，二者不共享数据源；「域（domain）」概念在 `data_map.py`、`data_mesh.py`、`federated_gov.py` 中**各定义了一次**。

**建议**：确定一个采集与存储中枢（`metrics.py`），其余模块只做消费与呈现。

---

## 8. 处理建议（按风险排序）

1. **不要批量删除**。先对 `lightdb/performance/`、`lightdb/security/`、`lightdb/sql/functions/` 这三组做决策：要么接线进主路径（安全层建议优先接线），要么显式标注为未实现 —— 当前「实现了但没接上」是最危险的中间态。
2. **孤儿 UI 面板（§5.1）与 `web/metrics.py`（§6）** 可安全删除或标注 deprecated，风险最低。
3. **重复实现（§7）** 先收敛血缘与质量两组（同名类会造成真实困惑），AI 客户端组建议合并到 `ai_integration`。
4. **`lightdb/tools/migrate.py`** 在目录树与文档中被宣称存在但实际缺失，应补齐或从文档中删除（见 [AUDIT_REPORT.md §11](AUDIT_REPORT.md)）。
5. 建议在 CI 中加入一条「导入图报告」，把本文档的统计固化为可复现的度量，避免死代码规模继续增长。

---

## 9. 如何复核本文档

可达性统计由一次性 AST 脚本产出，未随仓库提交（避免增加新的死代码）。复核要点：

- 用 `ast.walk` 收集每个模块的 `Import` / `ImportFrom`；对 `level > 0` 的相对导入，从当前模块名逐级去掉末段拼接候选名，取第一个真实存在的模块；
- 根集合为 `lightdb.main`、`lightdb.main_v4`、`lightdb_studio.app`、`web.start`；
- 模块名到文件名的映射需同时考虑 `x.py` 与 `x/__init__.py`；
- 排除 `tests/`、`lightdb_import/`；
- 结果需按 §1.4 人工核对动态导入后再计入「真实死代码」。
