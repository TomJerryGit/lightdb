# LightDB 项目总体架构、工程实践与文档体系（Lead 独立分析）

> 分析时间：2026-10-19 · 分析者：Lead（Team「全面分析」）
> 范围：仓库级（5 个顶层 Python 包 + docs + 脚本/部署/数据目录）；细分代码结论见同目录
> `01_engine_core.md` / `02_service_security.md` / `03_studio_web.md` / `04_verification.md`。
> 标注：**【实测】**本次运行取证；**【源码】**静态代码证据（附 `文件:行`）；**【文档】**仅见于文档；
> **【推断】**有依据但未直接验证。除 `docs/analysis_2026-10-19/` 与临时目录外**未改动任何仓库内容**。

---

## 1. 这个仓库到底是什么

仓库是**「一个数据库引擎 + 两个客户端 + 一批部署/文档资产」的单仓多产品项目**，不是单一应用：

| 产物 | 实体 | 入口 | 真实状态（一句话） |
|---|---|---|---|
| LightDB 数据库引擎（v4.0.0） | `lightdb/` 174 个 .py，54,765 行 | `python -m lightdb.main` | 有真实存储/MVCC/WAL/SQL 执行内核，SQL 覆盖面中等；**默认部署无认证（P0）** |
| LightDB Studio 桌面版（pyproject 版本 1.0.0） | `lightdb_studio/` 134 个 .py，39,256 行 | `python -m lightdb_studio.app` | 能启动、能连库查询；**36.5% 行死代码**，大量宣称功能只有死代码支撑 |
| LightDB Studio Web 版 | `web/` 6 个 .py，710 行 + 静态前端 | `python -m web.start` | 框架/登录/前端可跑，**但没有任何新建连接的端点 → 浏览器用户永远查不了库（P0）** |
| 文档知识库导入工具 | `lightdb_import/`（2 py + 10 SQL + 执行.txt） | `mysql` CLI 手工执行 | 与产品功能无关的一次性建库脚本 |
| 运维资产 | `docker/` 17 文件、`scripts/` 6 个 shell、`dashboards/` 4 个 Grafana JSON、`benchmarks/` | 见各自 README | 多为早期批次生成的"像那么回事"的资产，未在本机验证 |

**生成方式有直接证据（【源码/文档】）**：
1. 根目录 `create_project.py`（4.2 KB）按 `lightdb_project.txt`（429 行目录树）批量 `touch()` 空文件，
   `1.0 lightdb 运行/lightdb运行文档.txt` 是用户执行它的终端转写；
2. 测试文件名即批次号（`test_v2_batch1.py`、`test_v4_batch5.py`），审计报告与文档亦以"批次"叙述；
3. **9 份**文档首行残留 `==== BEGIN FILE: docs/xxx.md ====` 生成器标记
   （`ADMIN_GUIDE_v3/v4`、`ARCHITECTURE_v4`、`USER_GUIDE_v3/v4`、`MIGRATION_GUIDE_v3`、
   `MIGRATION_v3_to_v4`、`PERFORMANCE_WHITEPAPER_v3/v4`），且标注 **2025-02-01 / 2025-08-01** 的"发布日期"，
   而实际代码 2026-10 才出现——**文档中的版本历史是虚构的**；
4. `docs/V4_DECISION.md` 被有意截断（66 行，正文含"原文档在此处截断"）。

**结论**：这是一套 **AI 批量生成 → 多轮审计/修复** 的产物。2026-10-10 首个 commit 一次性提交
69,538 行原始快照；此后 9 天内 71 个 commit 增加约 16.2 万行（`+161,608 / −6,093`，含文档与测试），
当前工作区 129k 行 Python（含测试）。"是否能作为产品交付"与"代码量/文档量"完全脱钩。

---

## 2. 规模与技术栈（精确数字，【实测】AST/文件统计）

| 包 | .py 文件 | 物理行 | 说明 |
|---|---:|---:|---|
| `lightdb/`（引擎） | 174 | **54,765** | 另有 6 个 `.pyx`（约 1,262 行），0 个编译产物 |
| `lightdb_studio/`（桌面） | 134 | **39,256** | core 66 文件 21,743 行（55.4%）、ui 51 文件 15,390 行（39.2%）、**db 访问层 5 文件仅 670 行（1.7%）** |
| `web/` | 6 | 710 | +3 个静态文件（HTML 88 / JS 309 / CSS） |
| `lightdb_import/` | 2 | 219 | +10 个 SQL |
| `benchmarks/` | 6 | 1,677 | 有可复跑门禁 `python -m benchmarks.run --suite quick --gate` |
| `tests/` | 75 | **30,789** | 761 个用例（`--collect-only` 实测） |
| 根目录临时/生成脚本 | 10 | 1,776 | `create_project.py`、`bench_*.py`、`_diag_*.py` 等 |

- 技术栈：纯 Python 3（引擎无 C 扩展），桌面 PySide6，Web FastAPI + 自研 HMAC-JWT，客户端 pymysql；
  `requirements.txt` 另声明 psutil/numpy（pyproject 未声明，见 §5）。
- **引擎规模分布**：`sql` 27+6 文件 23,098 行（executor.py 单文件 8,226 行）、`db` 7 文件 6,006 行、
  `storage` 14 文件 3,332 行、`net` 6 文件 2,616 行；OLAP/columnar/parallel/cython 合计约 6.4k 行，
  已被 README 摘牌为"实验性、未接入 SQL 路径"。
- 测试/源码比：30,789 / 96,627 ≈ **0.32**（测试行数约为非测试代码 1/3，属较高投入）；
  但 GUI 与 Web 相关用例占比低（见 04 报告）。

---

## 3. 架构与依赖现实

### 3.1 运行时形态

```
                 ┌───────────────────────────────┐
  pymysql ─────▶ │ lightdb/net/server.py          │  MySQL v10 文本协议（5/26 条 COM_* 命令）
  (Studio/Web)   │  └─ sql/executor.py (8,226 行)  │
                 │      ├─ Table (db/table.py)     │  ← 单库模型：1 data_dir = 1 database
                 │      │   ├─ BufferPool/Page    │     真实 4KB 页 + B+Tree(int PK) + JSON 行
                 │      │   ├─ WAL (JSONL)        │     无 CRC、log-before-data、checkpoint
                 │      │   └─ MVCC 21B 行头      │     版本链持久化、快照隔离（进程内）
                 │      └─ plan_executor / 高级路径 │
                 └───────────────────────────────┘
      Studio GUI ──(PyMySQL)──▶ 上面同一条协议路径（无进程内直连）
      Web ──(复用 lightdb_studio.db.connection.DBConnection)──▶ 同上
```

- **单库模型**：没有 `CREATE DATABASE`；`SHOW DATABASES` 只报 `default` + `information_schema`
  （【实测】服务端）。
- **引擎是进程内嵌的**：`MySQLServer` 直接持有 `Database`/`Executor`；Studio/Web 都通过网络协议访问，
  因而 **GUI 的能力上限被服务端 SQL/元数据支持面硬限制**（旧评估"断层"结论方向仍成立，但缺口已缩小）。
- **查询执行三条路径并存**（【源码】+01 报告）：单表快路径 `Table.select` / `AdvancedExecutor` 内存路径 /
  `optimizer + plan_executor` 计划路径。同一语义要在 2–3 处分别接线——这是"能力漂移"（同一 SQL 在不同
  位置行为不一致）的结构性来源。
- **多进程模式**（`--mode multi`）：Master 独占存储并**单线程串行**执行所有 SQL，Worker 只做协议/连接；
  沙箱禁命名管道，本机无法启动（环境限制，非代码回归）。

### 3.2 包间依赖（AST 解析，数字为 import 次数 Top 边）

```
lightdb_studio.ui → lightdb_studio.core   129
lightdb.db        → lightdb.storage        50
lightdb_studio.ui → lightdb_studio.db      28
lightdb.cython    → lightdb.storage        22
lightdb.db        → lightdb.sql            21   ← 循环：sql → db 10
lightdb.performance → lightdb.sql          13
lightdb.backup    → lightdb.sql            10
lightdb.cluster   → lightdb.concurrency    10
lightdb.sql       → lightdb.db             10
lightdb.db        → lightdb.olap            8
lightdb.net       → lightdb.logger/cluster  8/8
```

问题点（【源码/实测】）：
1. **`lightdb/db` ↔ `lightdb/sql` 双向依赖**：`db.table` 反向 import `sql.checks/generated/partition/fulltext`
   （CHECK、生成列、分区、全文由 SQL 层提供），`sql.executor` 又 import `db.table/database`。
   可运行但耦合方向错误，单元难拆。
2. **UI 直接持有 DB 层**（129+28 次 import，无 service 层）：GUI 逻辑与协议/连接细节耦合，测试困难。
3. **同构双实现**：谓词求值 2 套（`executor._eval_condition` 177 行 vs `plan_executor._eval_condition` 172 行）、
   审计 2 套、binlog 2 套、ParallelExecutor 2 套、血缘 5 份、AI 客户端 4 份、数据质量 2 份
   （详见 01/02/03 报告与 `docs/DEAD_CODE.md` §7）。

### 3.3 死代码规模（独立复核）

- **Lead 的 AST 扫描（严格口径：同仓内 0 次被 import 的模块）**：**74 个**，其中引擎侧
  `sql/cte.py`、`sql/cursor.py`、`sql/icp.py`、`sql/batch_executor.py`、`sql/predicate_v4.py`、
  `sql/parser_v3b4.py`、`db/hybrid_table.py`、`db/table_v3.py`、`storage/btree_optimized.py`、
  `monitor/parallel_stats.py`、`stability/crash_recovery_v3.py`、`profiling/reporter.py` 等；
  Studio 侧 33 个 `core/`"数据平台愿景"模块（`data_mesh`/`federated_gov`/`digital_twin`/`etl`/
  `autonomy`/`agent_react` 等）+ 5 个孤儿 UI 面板 + `utils/sql_completer.py`。
- 旧审计 `docs/DEAD_CODE.md` 用「4 入口 BFS」口径给 **175 不可达 / 约 154 真实死代码**；两口径方向一致。
- Studio 专项（03 报告）：**42/134 模块、14,336/39,262 行（36.5%）从入口不可达**，其中 33 个 core 模块；
  另有"import 了但从未实例化"的运行时死代码（`DashboardWidget`、`CellViewerDialog`、`check_sql_safety`、
  `Workflow` 无入口等）。

---

## 4. 入口点与可运行形态

| 入口 | 命令 | 实测/证据结果 |
|---|---|---|
| 引擎单进程 | `python -m lightdb.main --mode single` | ✅ 启动、监听、pymysql 可登录（【Lead 实测】18896 端口） |
| 引擎默认（auto→multi） | `python -m lightdb.main` | 8 核下自动选 multi；本沙箱 `WinError 5` 无法启动（环境限制），普通机器可启动性未复核 |
| 引擎 `--check` / `--version` | `python -m lightdb.main --check` | 配置校验可用（04 报告实测） |
| 桌面端 | `python -m lightdb_studio.app` | ✅ 离屏 MainWindow 构造成功：6 菜单/42 项/15 工具栏/2 Dock/6 右侧标签（03 报告实测） |
| Web | `python -m web.start` | ✅ TestClient/uvicorn 可起；登录/JWT 有效；**但无连接管理 → `/query` 恒 404** |
| 引擎 CLI 工具 | `python -m lightdb.tools.<x>` | 多可 import；`tools/cli.py` 在 Windows 因 `import readline` 失败（旧审计，需复核） |

**打包/发布链路存在明确缺陷（【源码】pyproject.toml:74-79，04 报告将给 wheel 实测）**：
- `[tool.setuptools.packages.find] include = ["lightdb_studio*", "web*"]` **不包含 `lightdb` 引擎包**；
  而 README 第 91 行声称"pyproject 现在会打包 lightdb 并补上 lightdb-server 入口"——与当前文件不符；
- `console_scripts` 的 `lightdb-studio-cli = "lightdb_studio.tools.cli:main"` 指向**不存在的模块**
  （`lightdb_studio/tools/` 目录不存在）；
- `dependencies` 仅 `pymysql`，但引擎 import `psutil`/`numpy`（requirements.txt 有、pyproject 没有）；
- 无 `lightdb-server` 入口（README 宣称有）。仓库无 `CHANGELOG*`/`RELEASE_*` 中提到的部分文件。

---

## 5. 工程实践与仓库卫生

### 5.1 Git 历史（【实测】git 命令）

- **72 个 commit、单一作者 `hedongwei`、跨度 2026-10-10 → 2026-10-19（9 天）**；
  分布：10/10 10 个、10/12 12、10/13 7、10/14 13、10/15 14、10/16 14、10/19 2；
- **无分支/无 PR/无 tag**（仅 `main` 与 `remotes/backup/main` 两条），无 CI 配置（`.github/` 不存在）；
- commit 消息规范良好（`feat(scope):`/`fix:`/`docs:` 等，24 feat / 22 fix / 13 docs / 4 chore / 3 test / 3 perf），
  正文常写"根因 + 验证"，是历轮审计沉淀的规范；
- 累计 `+161,608 / −6,093` 行、856 文件变更；最大 commit 为初始快照（436 文件 / +101,998 行）；
- **当前工作区有未提交 WIP**：`parser.py +433`、`executor.py`、`plan_executor.py`、`table.py`
  共 `+459/−20`，外加未跟踪 `tests/test_sql_gaps.py`（BETWEEN / COUNT(DISTINCT) / IN 三值，
  专项测试 15/15 通过，但**未提交且投影位置 `1 IN ('1')` 仍不可用**，01 报告 §5）。

### 5.2 仓库卫生（【实测】）

- **47 个 `data_*` 测试数据目录**（合计 7.1 MB）+ `data/` + `backups_b8/` + `backups_bak/` 留在工作区；
- **`_wt_head/` 是注册在 `git worktree list` 里的遗留 worktree（detached HEAD, 7.3 MB）**——
  上一轮审计用"干净 HEAD 对照"的产物，未清理（`.gitignore` 的 `_wt_*/` 只遮挡了显示）；
- 根目录约 20 个临时/调试产物：`debug.log`、`_final_full.log`(149 KB)、`_test_batch*.log`、
  `_diag_auth.py`、`_smoke_db.py`、`_gap_probe.py`、`bench_summary.json`、`result_after.json` 等；
- `build/` 与 `lightdb_studio.egg-info/` 为本次分析期间 wheel 构建所生（04 报告），`plugins/` 为空目录；
- `LICENSE` 与 `LICENSE.txt` 内容重复（各 1,088 B）；
- 优点：`.gitignore` 覆盖较全（数据、日志、临时脚本、`_wt_*`），`d118779` 起主动停止跟踪临时文件，
  `5578bff` 补了忽略规则——**说明维护者已意识到卫生问题，但存量未清**。

### 5.3 环境与平台

- 仅 Windows 实测：`scripts/*.sh`、`docker/entrypoint*.sh`、`SO_REUSEPORT` 自绑定路径均未在本机验证；
- 依赖外部服务的功能（AI: OpenAI/Claude/Ollama、协作: websockets、Web: uvicorn）在本机多不可用或缺 key；
- 控制台 GBK 导致 emoji 崩溃问题已在 `main.py` 修复（README §本次会话已修复，本次未复现）。

---

## 6. 文档体系与可信度分级（27 份 .md，【源码+文档】）

| 级别 | 文档 | 判定 |
|---|---|---|
| **A. 现行、可信度较高** | `benchmarks/README.md`（门禁+机器漂移说明+实测基线）、`docs/review/00_SUMMARY.md` 及 01/02/03/04/05/06（带标注【实测】/【源码】/【推断】）、`docs/FAILURE_TRIAGE.md`（506 行失败分诊）、`docs/DEAD_CODE.md`、`docs/AUDIT_REPORT.md` §1–25（滚动追加）、`README.md` 顶部"当前实测状态" | 结论大体可用；但均为**历史快照**，其中相当一部分已被后续修复推翻（见 §6.2） |
| **B. 交接/说明类** | `docs/HANDOFF.md`（HEAD 66afdb8 / 34 failed 基线——**已过期**）、`docs/API_REFERENCE.md`（Web API 参考）、`docs/USER_GUIDE.md` | 需按新 HEAD 重测 |
| **C. 历史虚构/营销文档（正文不可信）** | `PERFORMANCE_WHITEPAPER_v3/v4`、`V3_RELEASE_NOTES`、`V4_RELEASE_NOTES`、`V4_DECISION`（截断）、`ADMIN_GUIDE_v3/v4`、`MIGRATION_GUIDE_v3`、`MIGRATION_v3_to_v4`、`USER_GUIDE_v3/v4`、`V3_ROADMAP` | 均标注 2025 年发布、含白皮书 QPS/加速比；2026-10 已在正文前加"未接线/设计目标"声明，但**正文数字与能力仍未删除**，首行还残留 `==== BEGIN FILE:` 生成标记 |
| **D. 桩** | `docs/DEPLOYMENT.md`（9 行 / 145 B） | 仅标题+一条命令 |

### 6.1 README 自身的内部矛盾（重要，因为它是唯一"门面"）

1. **测试计数前后矛盾**：第 81 行"一次完整测试运行 268 个测试中 118 失败 / 150 通过"，
   第 161 行又写"**299 通过 / 83 失败 / 1 跳过**"；而**当前实际是 761 个用例**（04 报告给实测结果）。
2. **同一缺陷既在"当前问题"又在"已修复"**：第 18–20 行称建表必失败/认证缺陷/默认多进程起不来，
   第 85–92 行又列为"本次会话已修复"。
3. **性能数字两套口径并存**：状态表写逐行 INSERT 1,501 rows/s，而 `benchmarks/README.md` 的
   同机 quick 档实测中位 3,100–3,800 rows/s；"无 checkpoint/轮转"也已过时（现在有阈值 checkpoint）。
4. **`.gitignore`/README 中的中文在 GBK 控制台下显示为乱码**（文件本身 UTF-8，属环境问题，非文档损坏）。
5. README **在"快速开始"的 pip 安装命令处戛然而止**（255 行，无使用示例、无架构、无 API、无 LICENSE 段）。
   且安装命令 `pip install lightdb[all]`/`pip install lightdb` 的包名与 pyproject 的 `lightdb-studio` 不一致。

### 6.2 旧审计结论的时过境迁（多份子报告已逐条核对）

- **已修复/已过时**：认证恒失败、建表必失败、错误伪装 OK、`--check` emoji 崩溃、桌面端 SyntaxError、
  `GROUP BY`/`DISTINCT` 忽略、JOIN 别名静默丢弃、`SHOW DATABASES` 硬编码、`SELECT DATABASE()` 错值、
  `SHOW COLUMNS` 的 Default/Extra/Type 伪造、`KILL` 空实现、`PROCESSLIST` 陈旧、多进程接线、事务原子性。
- **仍成立**：无预处理语句（COM_STMT_* 全 1047）、无 SSL、无权限体系、能登录即全权、
  无二级索引、`EXPLAIN` 恒 ALL、类型约束不校验、死代码规模、GUI 吞异常/无 QThread、
  Web 硬编码密钥与默认口令、Cython 未编译、OLAP/列存未接线。
- **仍夸大**：复制"真实位点可用"（实际位点会回退/事件会丢/DDL 不复制）、白皮书全部性能数字、
  多进程"共享内存突破 GIL"、半同步、Prometheus 指标、Navicat 对标能力。

---

## 7. Lead 独立复核（与子报告交叉验证，【Lead 实测】）

为防"子代理自述≠事实"，Lead 用独立脚本在 127.0.0.1:18896 起单进程服务端并复测关键结论：

| 断言 | Lead 复测结果 |
|---|---|
| `02` 报告 P0：默认 `root/''` 可登录 | ✅ 复现：空口令连接成功；错误口令/不存在用户均 1045（认证逻辑本身正常，**缺陷是默认账户无口令**） |
| `02` 报告 P1：wire 层 `BEGIN/ROLLBACK` 失效 | ✅ 复现：`BEGIN; INSERT; ROLLBACK;` 后 `COUNT(*)=1`（应为 0）；**另一连接在未提交时即可见该行** |
| `01` 报告 R1：类型约束不校验 | ✅ 复现：`INT` 收 `2147483648`、`VARCHAR(3)` 收 `'abcdef'`、`DATETIME` 收 `'not-a-date'`，全部静默入库并原样读回 |
| `01` 报告：无二级索引且显式报错 | ✅ 复现：`CREATE INDEX` → 1064（显式，非静默） |
| `02` 报告：元数据已诚实化 | ✅ 复现：`SHOW DATABASES`=`default,information_schema`；`SHOW TABLES/COLUMNS/CREATE TABLE` 真实可用 |
| 服务端可独立启动 | ✅ 约 1.5 s 启动、监听、pymysql 全链路可用；测后已杀进程并清理数据目录 |

> 复核脚本：`_analysis_tmp/lead/verify_client.py`（运行期临时目录已清理）；原始输出见本次会话记录。

---

## 8. 供汇总报告使用的判断

1. **不是空壳，但也不是可交付产品**：引擎/桌面端有真实可运行的内核与查询链路（远好于旧审计的
   "五个 P0 全挂"），但**认证、权限、事务语义、类型校验、Web 连接管理**这几个"产品级底线"仍缺失或
   形同虚设；桌面端 63.5% 可执行、Web 版不可用、文档宣称普遍高于实现。
2. **最大风险不是"功能少"，而是"假成功/静默错误"**：默认无口令（P0）、类型不校验（P1）、
   wire 事务 no-op（P1）、触发器可锁表（P1）、Web 可伪造 admin + SQL 注入（P0）、复制位点会回退（P1）。
   这些都属于"用户以为安全/以为成功，实际不是"。
3. **工程上最稀缺的是"收口"**：72 个 commit 全部是"继续加功能/继续修 bug"，没有一次发布、
   没有 CI、没有依赖锁定、没有清理存量（47 个数据目录、死代码 14k+ 行、未提交 WIP）；
   文档系统同时存在"最诚实"（benchmarks/review）与"最虚构"（白皮书/v3-v4 指南）两极。
4. **投入建议**（详见最终报告）：先做"安全与正确性止血"（认证/权限/事务/类型校验/Web 密钥与注入），
   再做"宣称与现实对齐"（删除或标注死代码/未接线模块、重写 README/白皮书），最后才谈性能与扩展；
   性能方向应聚焦"契约正确的单机吞吐"（当前逐行写约 1k–3.8k rows/s、并发负扩展），
   而不是从未接线的 OLAP/列存/Cython 数字。
