# LightDB 项目全面分析报告

> **分析对象**：`D:\lightdb_project` —— LightDB 数据库引擎 + LightDB Studio 桌面端 + Web 版
> **分析日期**：2026-10-19（Asia/Shanghai）
> **代码基线**：`git 67d0cd2`（main）+ 工作区 4 个未提交改动（`parser.py +433`、`executor.py`、
> `plan_executor.py`、`table.py`，合计 `+459/−20`）+ 未跟踪 `tests/test_sql_gaps.py`
> **方法**：5 路并行独立分析（引擎核心 / 服务端安全 / Studio+Web / 可验证性）+ Lead 独立复核，
> 全程**未修改任何源码**；仅新增本报告与 `docs/analysis_2026-10-19/` 证据目录。
> **证据标注**：**【实测】**=本次运行取证；**【源码】**=静态代码证据（附 `文件:行`）；
> **【文档】**=仅见文档宣称；**【推断】**=有依据但未直接验证。
> **数字口径**：本报告引用的 OLAP / 列存 / 并行 / Cython 性能数字（`3.5M rows/s`、`41x`、`12.5x`、
> `12,000 / 92,000 QPS` 等）均为**历史宣称/设计目标**；这些能力**未接入 SQL 执行路径**、
> 属**实验性**实现、**不计入性能声明**（引用只为核对文档与现实差距）。
> **证据索引**：[00_overview.md](analysis_2026-10-19/00_overview.md)（总体架构/工程/文档）·
> [01_engine_core.md](analysis_2026-10-19/01_engine_core.md)（引擎 297 行）·
> [02_service_security.md](analysis_2026-10-19/02_service_security.md)（服务/安全/复制 381 行）·
> [03_studio_web.md](analysis_2026-10-19/03_studio_web.md)（Studio/Web 416 行）·
> [04_verification.md](analysis_2026-10-19/04_verification.md)（测试/基准/端到端 319 行）+
> [05_RISK_DEEP_DIVE.md](analysis_2026-10-19/05_RISK_DEEP_DIVE.md)（高危问题逐项证据/复现/修复方案）+
> `analysis_2026-10-19/logs/` 原始日志。

---

## 1. 执行摘要

### 1.1 一句话结论

**这不是一个可交付的数据库产品或 Navicat 替代品，而是一套「有真实内核、但产品级底线普遍缺失」的
AI 批量生成项目**：引擎的存储/MVCC/WAL/SQL 执行内核确实能跑（711 个测试通过、崩溃恢复与基准门禁
实测成立），但**默认部署无认证（P0）、协议层事务完全失效（P1）、声明式类型约束不校验（P1）、
Web 版可伪造管理员并可注入 SQL（P0）且根本连不上数据库（P0）**；桌面端 36.5% 代码从入口不可达，
文档宣称普遍高于实现。**它适合作为「数据库内核学习/研究项目」，不适合任何生产或多人环境使用。**

### 1.2 关键数字（本次实测/统计）

| 维度 | 数字 | 来源 |
|---|---|---|
| 代码规模 | Python 约 **129,000 行 / 407 文件**（非测试源码 96,627 行 + 测试 30,789 行 + 根脚本 1,776 行） | 【实测】AST |
| 三个产物体量 | 引擎 174 文件 **54,765** 行；Studio 134 文件 **39,256** 行；Web 6 文件 710 行 | 【实测】 |
| 测试 | **761 用例 = 711 passed / 25 failed / 24 skipped / 1 xfailed**（31:07，4 agent 并发负载下膨胀） | 【实测】 |
| 基准门禁 | `quick --gate` **PASS 5/5**：逐行 INSERT 5,972 rows/s、点查 1,814 q/s、WAL 峰值 468.6 KB、崩溃恢复 0.364 s、崩溃丢行 0 | 【实测】 |
| 死代码 | 严格口径 74 个模块零引用；旧审计 BFS 口径 175 个不可达（约 154 真死）；Studio 侧 **42/134 模块、36.5% 行**从入口不可达 | 【实测】+【文档】 |
| Git | **72 commit / 1 作者 / 9 天**（2026-10-10 → 10-19），累计 `+161,608 / −6,093` 行；**无 CI、无分支、无 tag、无发布** | 【实测】 |
| 文档 | 27 份 .md；9 份残留 `==== BEGIN FILE:` 生成器标记、标注虚构的 2025 年发布日期 | 【实测】 |
| 仓库卫生 | 47 个 `data_*` 测试目录（7.1 MB）、2 个备份目录、1 个注册遗留 worktree、约 20 个根目录调试产物 | 【实测】 |

### 1.3 十个最重要的发现

| # | 级别 | 发现 | 关键证据 |
|---|---|---|---|
| 1 | **P0** | **默认部署 = 无认证的远程数据库**：新库只有 `root` 空口令账户，`root/''` 可登录；默认绑定 `0.0.0.0`；不校验 host/IP；连接数无上限（单 IP 120 条实测成功） | 【Lead 实测】【服务端实测】`db/database.py:36-42,211-216` `mysql_protocol.py:310-312` `config.py:66` |
| 2 | **P0** | **Web 版认证可被源码级伪造**：`web/auth.py:11` 硬编码 JWT SECRET，实测用该密钥伪造 `role=admin` token 通过；默认口令 `admin/admin123` 且启动明文打印 | 【实测】`web/auth.py:11,24-67` `web/server.py:17-22,90-93` |
| 3 | **P0** | **Web 版已认证 SQL 注入**：table/db/order_by/列名全部 f-string 拼接反引号标识符；配合伪造 token 可读任意表/`SLEEP()` 盲注/`INTO OUTFILE` | 【源码+实测】`web/api.py:187,309-312,325-391` |
| 4 | **P0** | **Web 版根本连不上数据库**：无任何"新建连接"端点，`create_app()` 无连接 → `/api/v1/query` 恒 404「连接 'None' 不存在」 | 【实测】`web/start.py:23` `web/server.py:15-25` `api.py:66-87` |
| 5 | **P1** | **经 MySQL 协议的事务是假的**：`server.py` 每条语句新建 session dict，`BEGIN` 存入的 txn 下一条语句就丢 → `BEGIN; INSERT; ROLLBACK` 不回滚、未提交行其他连接可见；断连不回滚并泄漏 MVCC active（24→45） | 【Lead 实测】【服务端实测】`net/server.py:458-464` |
| 6 | **P1** | **声明式类型约束完全不校验**：INT 越界、VARCHAR(n) 超长、非法 DATETIME 全部静默入库并原样读回（`2147483648` / `'abcdef'` / `'not-a-date'`） | 【Lead 实测】【源码】`db/table.py:237-265` |
| 7 | **P1** | **DDL "假成功"仍有两处致命形态**：`VARCHAR PRIMARY KEY` 建表成功但 INSERT 永久 1105；`CREATE TRIGGER` 不校验体，坏体触发后整表不可写且 `DROP TRIGGER` 不支持 | 【实测】`btree.py:99,173,202` `executor.py:6777` |
| 8 | **P1** | **复制不可用于生产**：binlog 是"查询时重扫各表当前 WAL"的动态视图，位点会回退（14→12）、checkpoint 后事件消失、DDL 不复制、无 `START SLAVE` | 【实测】`executor.py:5689-5757` `replication/binlog.py` |
| 9 | **P1** | **打包链路与 README 相反**：wheel 不含 `lightdb` 引擎包、无 `lightdb-server` 入口、`lightdb-studio-cli` 指向不存在的模块（README:91 声称已修） | 【实测】`pyproject.toml:74-79` |
| 10 | **P1** | **Studio 的能力是"宽而浅"**：真正 DB 访问层仅 670 行（1.7%），而 core 21.7k 行多为从不被 UI 调用的"数据平台愿景"；`except: pass` 占 40%（172/429）、全仓 0 个 QThread、密码/AI key 明文落盘 | 【实测+源码】03 报告 §3/§4/§5 |

### 1.4 定位判断（这软件到底是什么）

| 使用场景 | 结论 | 依据 |
|---|---|---|
| 学习数据库内核（页/B+Tree/WAL/MVCC/崩溃恢复） | ✅ **有价值** —— 这些是真实实现，且有测试与文档 | 01 报告 §1、§3.1 |
| 作为 MySQL 兼容数据库（替换 MySQL） | ❌ **不可** —— 无预处理语句、无二级索引、SQL 子集、类型不校验、事务在协议层失效 | 02 报告 §1/§2、04 报告 §4 |
| 作为 Navicat 类桌面管理工具 | ⚠️ **部分可用** —— 连接/查询/编辑/表设计/CSV 导出入能干活；事务、分页、数据同步、SSH、连接池、错误呈现等工具必备能力缺失或有缺陷 | 03 报告 §2 |
| 作为 Web 数据库管理平台 | ❌ **不可用** —— 无法建连接（功能 P0）+ 认证与注入漏洞（安全 P0） | 03 报告 §5/§6 |
| 用于生产/多人/公网环境 | ❌ **绝对不可** —— 默认无认证、假事务、静默错值、无 CI/无发布流程 | 本报告 §5、§8、§11 |

**公允地说**：这个项目并不像早期审计所描述的"PPT 骨架"——经过多轮修复，引擎已经能真实建表、
跑 JOIN/子查询/CTE/窗口/UNION、崩溃恢复已验证、错误码已大幅诚实化、性能声明也已被主动降级为
"设计目标"（`benchmarks/README.md` 的诚实程度在同类 AI 项目中罕见）。它的问题是**工程收口和
产品级底线**，不是"完全没有实现"。

---

## 2. 软件形态与生成背景

### 2.1 单仓多产品

| 产物 | 实体 | 规模 | 入口 |
|---|---|---|---|
| **LightDB 引擎 v4.0.0**（MySQL 协议兼容、纯 Python） | `lightdb/` | 174 py / 54,765 行（另有 6 个 `.pyx`，0 个编译产物） | `python -m lightdb.main --mode single` |
| **LightDB Studio 桌面版**（PySide6，Navicat 对标） | `lightdb_studio/` | 134 py / 39,256 行 | `python -m lightdb_studio.app` |
| **LightDB Studio Web 版**（FastAPI） | `web/` | 6 py / 710 行 + 静态前端 | `python -m web.start` |
| 文档知识库导入工具 | `lightdb_import/` | 2 py + 10 SQL | 手工 `mysql` 导入 |
| 运维资产 | `docker/`（17）、`scripts/`（6）、`dashboards/`（4 Grafana JSON）、`benchmarks/`（6） | — | 多为批次生成，未在本机验证 |

- **单库模型**：一个数据目录 = 一个库，无 `CREATE DATABASE`；`SHOW DATABASES` 只报
  `default` + `information_schema`【实测】。
- **Studio 与 Web 都走网络协议**（Studio 用 PyMySQL；Web 复用 `lightdb_studio.db.connection`），
  因此 **GUI/Web 的能力上限被服务端支持面硬限制**——这是"GUI 提供了服务端不支持的功能入口"的根源。

### 2.2 生成方式证据（决定如何看待其代码与文档）

1. 根目录 `create_project.py` 按 `lightdb_project.txt`（429 行目录树）批量 `touch()` 空文件；
   `1.0 lightdb 运行/lightdb运行文档.txt` 是用户执行该脚本的终端转写（含 V2/V3/V4 分批"运行验证"记录）。
2. 测试文件名即批次号（`test_v2_batch3.py`、`test_v4_batch5.py`），共 10 余个 batch 文件。
3. **9 份文档首行残留 `==== BEGIN FILE: docs/xxx.md ====` 生成器标记**（`ADMIN_GUIDE_v3/v4`、
   `ARCHITECTURE_v4`、`USER_GUIDE_v3/v4`、`MIGRATION_GUIDE_v3`、`MIGRATION_v3_to_v4`、
   `PERFORMANCE_WHITEPAPER_v3/v4`），且均标注
   **2025-02-01 / 2025-08-01** 的"发布日期"——而代码在 2026-10 才出现：**文档中的版本历史是虚构的**。
4. `docs/V4_DECISION.md` 被有意截断（"原文档在此处截断，后续章节缺失"）。
5. Git 首个 commit（2026-10-10）一次性提交 **69,538 行**原始快照；之后 9 天 71 个 commit 增加约 16 万行。

---

## 3. 架构现实

### 3.1 运行时拓扑

```
┌──────────────────────────── 客户端 ────────────────────────────┐
│ LightDB Studio（PySide6，134 文件）      Web（FastAPI + 静态 SPA）│
│   └─ db/connection.py（pymysql，670 行）   └─ 复用 Studio 的 DB 层 │
└───────────────┬───────────────────────────────┬─────────────────┘
                │ MySQL v10 文本协议（仅 5/26 条 COM_* 命令）      │
┌───────────────▼───────────────────────────────────────────────┐
│ lightdb/net/server.py（1,025 行，ThreadingTCPServer）            │
│   └─ sql/executor.py（8,226 行，按 command 反射分发到 _cmd_*）    │
│       ├─ 单表快路径 Table.select()                                │
│       ├─ 高级内存路径 _select_advanced()（JOIN/子查询/CTE/窗口）   │
│       └─ optimizer.py + plan_executor.py（计划路径，含 Limit 下推）│
│   └─ db/（Database=单库；Table=页+WAL+BTree+MVCC+锁）              │
│       ├─ storage/page.py（4KB 页、24B 页头、槽位数组）             │
│       ├─ storage/btree.py（仅 int PK → 最新版本指针，无二级索引）   │
│       ├─ storage/wal.py（JSON Lines、无 CRC、log-before-data）     │
│       └─ storage/mvcc.py（21B 行头 + 版本链持久化、快照隔离）       │
└────────────────────────────────────────────────────────────────┘
```

### 3.2 关键架构事实与代价

| 决策 | 现实 | 代价 |
|---|---|---|
| **存储格式** | 生产行格式 = **21 B MVCC 行头 + JSON 文本载荷**；`storage/row_format.py` 的紧凑二进制格式是死代码（仅实验/Cython 回退引用） | 空间/解析开销大；JSON 序列化是写路径主成本之一 |
| **索引** | **只有 int 主键 B+Tree**；无二级索引 DDL（`CREATE INDEX` 1064）；非 int PK 建表后不可写 | 非主键条件全表扫描；VARCHAR PK 表"建表成功即报废"（P1） |
| **WAL/事务** | JSON Lines WAL、无 CRC、checkpoint 阈值触发；MVCC 快照隔离在进程内成立 | WAL 损坏无法区分"尾部撕裂"与"中间丢已提交记录"；COMMIT 对**全库所有表**逐个 fsync（2 表 657ms、7 表 2–4.3s） |
| **执行路径** | 单表快路径 / 高级内存路径 / 计划路径 **三条并存**，谓词求值两套 | 同一语义要在 2–3 处接线 → "能力漂移"（同一 SQL 在 WHERE 可用、投影报错；裸窗函数可用、带别名报错） |
| **并发** | 单进程查询单线程（GIL + 表锁）；多进程 Master 独占存储、查询仍串行 | 4 线程并发实测 **0.70×**（负扩展）；多进程只提升连接/协议吞吐，不是查询加速 |
| **包依赖** | `db ↔ sql` 双向依赖；UI 直接 import DB 层 129 次 | 分层不成立，难以单测/替换 |

### 3.3 死代码与重复实现（可维护性主因）

- **严格口径**（同仓内 0 次 import）**74 个模块**：引擎侧 `sql/cte.py`、`sql/cursor.py`、`sql/icp.py`、
  `sql/batch_executor.py`、`sql/predicate_v4.py`、`sql/parser_v3b4.py`、`db/hybrid_table.py`、
  `db/table_v3.py`、`storage/btree_optimized.py`、`monitor/parallel_stats.py`、
  `stability/crash_recovery_v3.py`、`profiling/reporter.py` 等；Studio 侧 33 个 `core/` 概念模块
  （`data_mesh`/`federated_gov`/`digital_twin`/`etl`/`autonomy`/`agent_react`…）+ 5 个孤儿 UI 面板
  + `utils/sql_completer.py`（导致"自动补全"勾选项实际无效）。
- **旧审计 BFS 口径**：296 个非测试模块中 175 不可达（约 154 真死代码）。
- **整包未接线**：`lightdb/monitor`（8/8）、`stability`（7/7）、`performance`（7/7）从主入口 0 可达；
  `parallel` 仅"单表裸 COUNT"一条真实使用；`security` 仅 audit/password_policy 部分接线，
  **privileges/ssl/connection_limit 完全死代码**。
- **重复实现**：血缘 5 份（3 个存储文件）、AI 客户端 4 份（仅 `ai_integration` 真 HTTP）、
  数据质量 2 套同名 `QualityIssue`、审计 2 套、binlog 2 套、ParallelExecutor 2 套、
  谓词求值 2 套。

---

## 4. 功能能力矩阵

### 4.1 引擎 SQL 能力（34+ 项实测，完整表见 01 报告 §2）

| 状态 | 能力 |
|---|---|
| ✅ **真实可用** | `CREATE/DROP TABLE`（主键/UNIQUE/DEFAULT/NOT NULL）、`ALTER ADD/DROP COLUMN/RENAME`、`TRUNCATE`、多值 `INSERT`、`UPDATE`（常量）、`DELETE`、`AUTO_INCREMENT` 补值、WHERE（AND/OR/LIKE/比较/BETWEEN/IN/NOT/IS NULL/NULL 三值）、`ORDER BY`/`LIMIT n OFFSET m`、JOIN（INNER/LEFT/RIGHT/CROSS + 别名 + 聚合）、子查询（IN/EXISTS/相关/标量/`FROM (子查询)`）、CTE（含 `WITH RECURSIVE`）、UNION/UNION ALL/INTERSECT/EXCEPT、GROUP BY/HAVING/DISTINCT、聚合 + `COUNT(DISTINCT)`（未提交改动）、窗口函数（ROW_NUMBER/RANK/SUM/LAG/LEAD + 分区/帧）、CHECK 约束（3819）、JSON 类型与 `->`/`JSON_EXTRACT`、全文索引 `MATCH...AGAINST`、存储过程/函数/触发器（部分）、事务与崩溃恢复（进程内）、逻辑备份/恢复、UNIQUE 唯一性真强制（含重启后） |
| ⚠️ **部分/有缺陷** | 窗口别名（不加 AS 报错）、生成列（显式赋值不重算）、分区表（仅元数据、无裁剪）、触发器（体不校验、可锁表、无法 DROP）、`CALL` 丢弃过程内结果集、EXPLAIN 恒 `ALL/key=None`（与真实 BTree 点查不符）、隔离级别谎报 READ_COMMITTED、`SHOW INDEX` 不列全文索引、`SELECT DATABASE()` 返回 None |
| ❌ **显式报错（诚实）** | `CREATE INDEX`、`CREATE VIEW`、`CREATE EVENT`、`DROP TRIGGER/PROCEDURE`、`SAVEPOINT`、`SET TRANSACTION ISOLATION`、外键定义、非唯一 KEY/FULLTEXT KEY/复合 PK、`UPDATE SET col=表达式`、`INSERT...SELECT`、`NOW()/CAST/CASE` 等标量函数（白名单外）、`LIMIT -1` 接受但语义不同 |
| 🔴 **静默错误/假成功（最危险）** | **类型约束不校验**（INT 越界/VARCHAR 超长/非法 DATETIME 静默入库）、**VARCHAR 主键表建表成功后不可写**、**触发器坏体锁表且无法自救**、生成列不重算、PK 列允许 NULL（经协议实测）、`_match` 未知 op 静默返回 False（潜在静默空集通道） |

**一句话**：常见分析型 SELECT/DML 已可跑通，**短板在 DDL 生命周期与"声明式约束不生效"**。

### 4.2 MySQL 协议与服务端（完整矩阵见 02 报告 §1）

- ✅ 握手/认证插件、文本查询/结果集/EOF、`COM_FIELD_LIST`、`KILL`（真断连）、`SHOW PROCESSLIST`
  （Command/Time 已修复）、`SHOW CREATE TABLE/TABLES/COLUMNS/INDEX/MASTER STATUS/AUDIT LOG` 等。
- ❌ **预处理语句全缺**（`COM_STMT_PREPARE/EXECUTE/...` 全部 1047）→ 主流 JDBC/ODBC、ORM
  server-side prepare 直接不可用；**多语句**公告 `CLIENT_MULTI_STATEMENTS` 却不拆句（协议谎报）；
  **SSL 未公告**（have_ssl=DISABLED）；执行错误 **SQLSTATE 全部 HY000**（1146/1054/1062 的 MySQL
  SQLSTATE 42S02/42S22/23000 全错）；`SET ...` 一律回 OK 但无效果（autocommit/names/sql_mode 全假）。
- 会话/连接：无连接上限、无失败锁定、不校验包长上限（DoS 面）；`SHOW STATUS` 的 Connections/
  Queries 语义与 MySQL 不同。

### 4.3 Studio 桌面端（README 43 项宣称逐项判定，见 03 报告 §2）

**统计：可用 8 / 部分可用 22 / 死代码或不可达 7 / 桩 1**（另有若干组件级声明）。

- ✅ **可用**：连接管理、多标签查询、语法高亮、SQL 片段（18 条）、数据库对比、快照/克隆、
  SQL 格式化、数据质量检查、结果表格（排序/NULL 高亮）、CSV 导入导出、表设计器（基础）、
  报表导出（CSV/HTML/XLSX 真落盘）、图表（bar/line/pie/scatter 4 种）。
- ⚠️ **部分**：数据网格编辑、查询历史（"收藏"未实现）、ER 图（启发式非真外键）、血缘（可解析但
  仅表级）、多环境（切换真、**生产保护未接线**）、SQL 审查（执行不强制走审核）、数据脱敏（默认空规则）、
  AI 助手（需外部 key）、健康度/性能监控/慢查询（基于 EXPLAIN）、调度器（interval/daily 而非 cron）、
  迁移向导、协作（依赖未安装的 websockets）。
- 🔴 **死代码/虚假**：**仪表盘**（`DashboardWidget` 从未实例化）、**Hex/大文本查看器**、
  **Excel 兼容复制**（`EnhancedDataGridMixin` 从未被 mixin）、**10 种图表**（菜单只 4 种，其余在死模块）、
  **自动补全**（`sql_completer.py` 零引用）、**工作流引擎**（无任何 UI 入口）、**生产保护**
  （`check_sql_safety()` 无调用者）。
- 🧱 **桩**：Prometheus 指标（`web/metrics.py` 路由从未挂载，`/metrics` 404）。

### 4.4 Web 版（03 报告 §5/§6）

- 17 条路径（16 个 `/api/v1` + 首页）；JWT 验签真实（篡改/过期被拒），但**认证密钥硬编码**。
- **功能 P0**：没有"新建连接"端点 → 浏览器用户无法查询任何数据库。
- **安全 P0**：伪造 admin token、默认口令、SQL 注入（table/order_by/列名）、CORS `*`+credentials、
  `/connections/test` SSRF、`/history` 全局历史泄露、token 7 天无吊销。

---

## 5. 正确性、数据完整性与安全风险（P0/P1）

### 5.1 P0（已证明，须立即修）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| **P0-1** | **默认无认证**：新库仅 `root` 空口令账户；默认 `0.0.0.0`；无 host/IP 校验、无连接上限 | 【Lead 实测】root/'' 登录成功；错误口令 1045；单 IP 120 连接全成功；`db/database.py:36-42,211-216`、`mysql_protocol.py:310-312`、`config.py:66` | 任何能连到端口者 = 全库完全控制（权限层未接线，见 P1-1） |
| **P0-2** | **Web 认证可伪造**：`SECRET` 硬编码源码；自研 HMAC-JWT | 【实测】`web/auth.py:11,24-67` 伪造 admin token → `/me` 200 | 源码公开 → 任何能访问 Web 服务者可冒充任意角色 |
| **P0-3** | **Web SQL 注入**（已认证） | 【源码+实测】`web/api.py:187,309-312,325-391` f-string 拼反引号标识符 | 读任意表/盲注/写文件；结合 P0-2 无需有效账号 |
| **P0-4** | **Web 无连接管理 → 无法查库** | 【实测】`/api/v1/query` → 404「连接 'None' 不存在」；`web/start.py:23` | Web 版作为产品不可用 |
| **P0-5** | **协议层事务失效**：每语句新建 session，`BEGIN/ROLLBACK` no-op；断连不回滚并泄漏 MVCC active（实测 24→45） | 【Lead 实测】`BEGIN;INSERT;ROLLBACK` 后 count=1、未提交行他连接可见；`net/server.py:458-464` | 所有经 MySQL 协议的事务都是假的（数据一致性事故） |
| **P0-6** | **Web 默认口令 + 启动明文打印 + 0.0.0.0** | `web/server.py:17-22,73,90-93` | 未改密即被接管 |

### 5.2 P1（可证明的高危缺陷）

| 领域 | 问题 | 证据 |
|---|---|---|
| 引擎 | **类型/长度/格式约束完全不校验**（INT 越界、VARCHAR 超长、非法日期静默入库） | 【Lead 实测】`db/table.py:237-265` |
| 引擎 | **VARCHAR 主键建表成功但 INSERT 永久 1105、SELECT 空集** | 【实测】`btree.py:99,173,202` |
| 引擎 | **触发器定义期不校验体**：坏体 CREATE 返回成功 → 整表写入 1105，且 `DROP TRIGGER` 不支持 | 【实测】`executor.py:6777` |
| 引擎 | **COMMIT/ROLLBACK O(表数) fsync**：2 表 657ms、7 表 2–4.3s/次 | 【实测】`db/database.py:912-928` |
| 引擎 | WAL 无 CRC + 解析失败静默跳过（无法区分尾部撕裂与中间损坏） | 【源码】`wal.py:165-181,311-324` |
| 引擎 | 隔离级别谎报 READ-COMMITTED（实际恒快照）；EXPLAIN 恒 ALL；生成列不重算 | 【源码+实测】`database.py:901-910`、`net/server.py:703` |
| 服务端 | **权限体系完全未接线**：GRANT/REVOKE/SHOW GRANTS 1064；登录即全权 | 【实测】`PrivilegeManager` 零生产调用；实测无授权用户可 DDL/删表 |
| 服务端 | **审计明文记录 `IDENTIFIED BY` 口令，且 `SHOW AUDIT LOG` 无权限校验**——任意登录用户可读回（实测 102 条、18 条含明文口令） | 【实测】`sql/executor.py:131-345` |
| 服务端 | **双引号密码静默降级为空口令账户** | 【实测】`sql/parser.py:419-428` |
| 服务端 | 预处理语句全缺、`SET` 全吞、SQLSTATE 全 HY000、常用 SHOW 缺约 40% | 【实测】02 报告 §1 |
| 复制 | binlog 是 WAL 当前窗口动态视图：位点回退、checkpoint 后事件消失、DDL 不复制、无 START SLAVE；半同步死代码 | 【实测】`executor.py:5689-5757` |
| 并发 | 4 线程并发 0.70×、400 次 SELECT 1 并行 0.58×（负扩展）；多进程查询仍 Master 单线程 | 【实测】02 报告 §4 |
| Studio | 连接密码 / AI api_key **明文落盘**；团队配置 XOR + 硬编码默认钥；云同步无 MAC | 【实测+源码】`config.py:20,118-126`、`ai_integration.py:246-264`、`team_config.py:84-85` |
| Studio | **全仓 0 个 QThread**，SQL 在 GUI 线程同步执行 + `fetchall()` 全量 + 无读超时 → 大结果/慢查询冻结界面 | 【源码】`main_window.py:951-1027`、`db/connection.py:216-233` |
| Studio | 40% 异常处理器吞异常（172/429），静默失败/空节点/数据丢失 | 【实测】03 报告 §4 |
| 打包 | wheel 缺 `lightdb` 引擎包；`lightdb-server` 入口不存在；`lightdb-studio-cli` 断链；pyproject 依赖缺 psutil/numpy | 【实测】`pyproject.toml:74-79` |

---

## 6. 性能与扩展性（实测，非白皮书）

### 6.1 本次基准门禁（`python -m benchmarks.run --suite quick --gate`，13.6s，**PASS 5/5**）

| 指标 | 本次实测 | 门禁阈值 | 与历史基线 |
|---|---:|---:|---|
| 逐行 INSERT（2,000 行档） | **5,972 rows/s**（5,644–6,301） | ≥1,000 | 高于中位 ~3,500，处区间上沿 |
| 多值批量 / 逐行 | **1.605×** | ≥1.2× | 一致 |
| 点查（进程内） | **1,814.7 q/s**（1,634–1,996） | ≥600 | 区间上沿 |
| WAL 峰值（5,000 行） | **468.6 KB** | ≤32 MB | 与基线逐位一致 |
| 崩溃恢复（硬杀） | **0.364 s**；已提交丢行 0；未提交泄漏 0 | <5s / =0 / =0 | 一致 |

> 门禁是"回归护栏"不是性能承诺；本次全量 pytest 31:07 属 4 agent 并发负载膨胀，**不可当作性能回归**。

### 6.2 结构性性能事实（【实测】+【源码】）

| 项 | 实测/结构 | 说明 |
|---|---|---|
| 点查 vs 白皮书 | 1,815 q/s vs **12,000 QPS** → 差 **6.6×**（README 旧口径 13.5× 基于 888 q/s） | 白皮书数字仍无落点 |
| 并发扩展 | 1→4 客户端 **0.70×**、400×`SELECT 1` 并行 **0.58×**（历史 0.49×） | GIL + 表锁，负扩展 |
| OLAP/列存/并行/Cython | 未接入 SQL 路径（`Executor`/`plan_executor` 零引用）、0 个编译产物 | 3.5M/85M rows/s、41×、12.5× 均无落点 |
| 事务开销 | 空 COMMIT 0.3ms；2 表 657ms；7 表 2–4.3s | O(表数) fsync 循环 |
| 维护操作 | 空表 TRUNCATE/ADD COLUMN 2.3–2.6s；7 表 vacuum 4.56s / checkpoint 4.18s / shutdown 4.04s | 本机 fsync 偏慢，但 O(表数/页数) 结构确定 |
| 启动成本 | 约 1.5s（小库）；启动需扫全表堆页 + 全 WAL 求最大事务号（O(页数+WAL)） | 大库会明显变慢 |
| 每表固定开销 | `.dat` 3 页 12KB + `.btroot` + `.wal` | 小表多时线性膨胀 |

### 6.3 并发/多进程结论

- **单进程**：查询执行单线程，协议层线程只带来排队与锁竞争 → 并发越多越慢。
- **多进程**（`--mode multi`）：Master 独占存储并**单线程串行**执行所有 SQL，Worker 只摊薄协议解析；
  `SharedTxnCoordinator/SharedBufferPool` 在 Worker 侧**无人使用**（架构文档"共享内存突破 GIL /
  接近线性扩展"是夸大）。本沙箱禁命名管道，multi 无法启动（环境限制，非代码回归）。

---

## 7. 测试与工程化状况

### 7.1 测试结果（本次全量，【实测】）

```
761 用例 = 711 passed / 25 failed / 24 skipped / 1 xfailed   （31:07，负载膨胀）
失败分类：真实缺陷/缺口约 17–19（含 3 条"设计目标未达成"）· 环境限制 2 · flaky 1 · 测试自身 3–5
```

- **真实缺陷代表**：GRANT 1064、半同步语句 1064、`SET @@var` 不支持、`UPDATE SET v=v+1` 1105、
  列存 GROUP BY 返回 0、行格式 DECIMAL 编解码 TypeError、虚拟表/联邦/Data API 过滤失效、
  数字孪生 snapshot None、`parser_v3b4` 变量执行 None 下标、`row_format` 回归。
- **环境限制（2）**：写 `~/.lightdb_studio/reports/`、写 `dashboards/` 被沙箱拒绝。
- **flaky（1）**：`test_ipc_latency`（100µs 阈值过紧，隔离重跑 2/2 通过）。
- **测试自身（3–5）**：`test_parser_v3b4` 下标 None、`test_concurrent_load` 引用已删除属性等。
- **相对旧审计**：HANDOFF 的"34 failed / 463 passed"已完全过时（套件从 ~500 膨胀到 761）；
  README 中"268 测试 118 失败""299 通过 83 失败"两个数字在同一文件内互相矛盾且都过时。
- **覆盖盲区**：GUI 交互（离屏冒烟仅构造窗口，无 widget 行为测试）、Web 认证/注入、协议层事务
  （测试走进程内 `session` 路径，所以 711 绿仍掩盖了 P0-5）、复制数据正确性、多进程。

### 7.2 基准门禁（可复跑，本次 PASS）

`benchmarks/` 是仓库内**唯一按门禁复跑性能的入口**，且 `benchmarks/README.md` 明确写了
"机器漂移、必须同窗成对测量、门禁只做回归栏杆"——这份文档的工程诚实度优于项目其余部分。

### 7.3 工程化缺失

| 项 | 状态 |
|---|---|
| CI | **无**（无 `.github/`、`.gitlab-ci.yml`、`Jenkinsfile`、`tox.ini`、`Makefile`） |
| 发布/版本 | 无 tag、无 CHANGELOG（文档提到的 `CHANGELOG_v3.md` 等文件不存在）；`lightdb.__version__=4.0.0` 而 pyproject 版本 1.0.0 |
| 依赖锁定 | 无 lock 文件；pyproject 与 requirements.txt 不一致（缺 psutil/numpy） |
| 打包 | wheel 缺引擎包、入口断链（P1） |
| 仓库卫生 | 47 个 data_* 目录、2 个备份目录、1 个遗留 worktree（`_wt_head`）、约 20 个根目录调试产物、`build/`+egg-info、`LICENSE` 与 `LICENSE.txt` 重复、`plugins/` 空目录 |
| 提交规范 | 良好：`feat(scope):`/`fix:`/`docs:`，正文写根因与验证；但**没有一次发布收口** |

---

## 8. 文档可信度

### 8.1 分级（27 份 .md）

| 级别 | 文档 | 判定 |
|---|---|---|
| **A 现行、较可信** | `benchmarks/README.md`、`docs/review/00–06`、`FAILURE_TRIAGE.md`、`DEAD_CODE.md`、`AUDIT_REPORT.md` §1–25、README 顶部"当前实测状态" | 带来源标注、多为实测；但均为**历史快照**，部分结论已被后续修复推翻 |
| **B 交接/参考** | `HANDOFF.md`（HEAD 66afdb8 / 34 failed 已过期）、`API_REFERENCE.md`、`USER_GUIDE.md` | 需按新 HEAD 重测 |
| **C 历史虚构/营销** | `PERFORMANCE_WHITEPAPER_v3/v4`、`V3/V4_RELEASE_NOTES`、`V4_DECISION`（截断）、`ADMIN_GUIDE_v3/v4`、`MIGRATION_*`、`USER_GUIDE_v3/v4`、`V3_ROADMAP` | 虚构 2025 发布日期、残留生成器标记；正文 QPS/加速比无落点（已加免责声明但未删除） |
| **D 桩** | `DEPLOYMENT.md`（9 行 / 145 B） | 仅标题 |

### 8.2 README 的内部矛盾（门面文件，影响最大）

1. **同一缺陷既在"当前问题"又在"已修复"**：第 18–20 行称认证缺陷/建表必失败/默认多进程起不来，
   第 85–92 行又列为"本次会话已修复"。
2. **测试计数三处不一致且全过时**：第 81 行"268 测试 118 失败 / 150 通过"、第 161 行"299 通过 /
   83 失败 / 1 跳过"——实际 761 用例（711/25/24+1xf）。
3. **性能表述两套口径**：状态表 1,501 rows/s vs `benchmarks/README.md` 中位 3,100–3,800；
   "无 checkpoint/轮转"已过时（现在有阈值 checkpoint）。
4. **打包声明与事实相反**：第 91 行"pyproject 会打包 lightdb 并补上 lightdb-server 入口"——
   wheel 实测 0 个 lightdb 文件、无 lightdb-server 入口、`lightdb-studio-cli` 断链。
5. README 在"快速开始"的 pip 命令处**戛然而止**（255 行），无使用示例/架构/API；安装包名
   （`lightdb`/`lightdb[all]`）与 pyproject 的 `lightdb-studio` 不一致。

### 8.3 文档-现实抽查（10 条，见 04 报告 §7）

**4 条已过时**（认证失败/建表失败、268 测试计数、299 测试计数、SHOW CREATE/ALTER/KILL 等"全部 1064"）、
**2 条夸大或不成立**（打包声明、跨层事务真回滚）、**3 条仍成立**（offscreen MainWindow、UNIQUE 约束/
`SHOW INDEX` 诚实、Cython 未编译）、**1 条无法验证**（多进程真实可用性）。

---

## 9. 代码质量与可维护性

| 指标 | 数值 | 说明 |
|---|---|---|
| 最大文件 | `sql/executor.py` 8,226 行、`sql/parser.py` 4,581、`db/table.py` 4,380 | 三大文件占引擎 1/3 |
| 超长函数 | >100 行 33 个、>200 行 7 个（最长 `table.py::_relocate_forward` 334 行、`parser.py::_where` 329 行，引擎范围） | 解析器/存储核心尤甚 |
| 吞异常 | Studio 172/429（40%）；引擎 `except Exception: pass` 36 处（WAL 解析吞异常属数据风险） | 静默失败的温床 |
| 重复函数体 | 完全一致的复制粘贴 0 组 | 但"同构双路径"大量存在（比复制更隐蔽） |
| 全局状态 | 引擎 `global` 语句 1 处 | 相对干净 |
| 注释密度 | 引擎高（大量"为什么修/旧实现错在哪"审计式注释） | 好的一面，但也出现"注释代替测试"倾向 |
| 死代码 | 74 模块零引用（严格口径）；~154 真死（BFS 口径）；Studio 36.5% 行不可达 | 维护成本与误导风险 |

**判断**：代码"难读但不糊涂"——历轮审计把"不许静默失败"写进了规范，错误码/元数据诚实化有真实投入；
但**三条执行路径 + 多套同构实现**导致能力漂移成为常态，且死代码与"配置了但不生效"的配置项
（audit_level/ssl/ip_whitelist/max_connections/semi_sync…）持续误导使用者。

---

## 10. 风险清单（汇总，P0–P3）

> 完整分领域清单见 01/02/03/04 报告；**每条 P0/P1 的复现命令、根因代码位置、代码级修复方案与验收用例
> 见 [05_RISK_DEEP_DIVE.md](analysis_2026-10-19/05_RISK_DEEP_DIVE.md)**；此处为跨层去重后的总表。

### P0（立即修，共 6 条）
1. 默认 root 空口令 + 0.0.0.0 + 无 host/IP/连接限制 → 未授权完全控制（引擎）
2. Web JWT SECRET 硬编码 → 可伪造任意角色（Web）
3. Web 已认证 SQL 注入（table/order_by/列名）（Web）
4. Web 无连接管理端点 → 无法查库（Web 功能）
5. 协议层 `BEGIN/COMMIT/ROLLBACK` no-op + MVCC active 泄漏（服务端）
6. Web 默认口令 + 启动明文打印 + 0.0.0.0（Web）

### P1（高优，共 17 条，按域归纳）
- **数据正确性**：类型/长度/格式不校验；VARCHAR PK 表不可写；触发器坏体锁表；PK 允许 NULL（协议层）；
  `UPDATE SET col=表达式` 1105；生成列不重算。
- **安全/权限**：权限体系未接线（登录即全权）；审计明文口令 + 任意用户可读；双引号密码降级为空口令；
  预处理语句缺失；Studio 密码/AI key 明文落盘；CORS `*`+credentials；SSRF。
- **事务/复制**：COMMIT O(表数) fsync；复制位点回退/事件丢失/DDL 不复制。
- **工程**：打包链路（wheel/入口/依赖）；并行执行器启动期循环导入静默回退；全仓 0 QThread + 吞异常 40%。

### P2（中优，共 ~20 条）
SQLSTATE 全 HY000、`SHOW STATUS` 语义错、SHOW 命令缺 40%、连接无上限/包长不校验、
隔离级别谎报、EXPLAIN 恒 ALL、`SHOW DATABASES` 固定两库、`SELECT DATABASE()` 为 None、
多进程 `RemoteDB` 缺方法（源码推断）、复制错误不停止、慢查询阈值配置不生效、
并发负扩展、Web 全局历史泄露/无登出、云同步无 MAC、团队配置硬编码钥、42 个死模块等。

### P3（低优/清理）
`LIMIT -1` 语义、`SHOW ENGINE INNODB STATUS` 泄露路径、用户枚举 oracle、`readline` 顶层 import、
`web/api.py` 的 pydantic `.dict()` 弃用、VERSION 不一致（4.0.0 vs 1.0.0）、仓库卫生（47 个 data_*、
`_wt_head`、根目录 ~20 个调试产物、双 LICENSE、空 plugins/）、8 份虚构文档等。

---

## 11. 路线图建议（按"影响正确性 > 安全 > 可用性 > 性能"）

### 阶段 0：止血（1–2 天，必须做）
1. **认证**：首次启动生成随机 root 口令（或强制改密）；默认监听改 `127.0.0.1`；补 host/IP/连接数限制。
2. **Web**：JWT SECRET 从环境变量读取；默认口令改随机/强制修改；去除启动明文打印；CORS 白名单；
   修复 table/order_by/列名注入（参数化/标识符白名单）；补连接管理端点（或删掉 Web 版宣称）。
3. **协议事务**：`session/txn` 挂到连接对象（`server.py:458` 改为 per-connection session），实现
   `SET autocommit/session`；断连回滚事务。
4. **打包**：`packages.find` 加 `lightdb*`；补 `lightdb-server` 入口；修/删 `lightdb-studio-cli`；
   pyproject 依赖对齐 requirements。
5. **数据正确性最小集**：类型/长度/格式校验（返回 1264/1406/1292 或截断+warning）；
   非整型 PK 定义期显式拒绝；触发器 CREATE 时就地校验体；PK 列 NULL 拒绝。

### 阶段 1：可信度（3–5 天）
6. 权限体系接线（至少 SELECT/INSERT/UPDATE/DELETE 四种权限 + GRANT/REVOKE/SHOW GRANTS）；审计脱敏 + SHOW AUDIT LOG 权限校验。
7. SQLSTATE 修正、`SET` 语句真实生效、`UPDATE SET 表达式`、`SHOW CREATE TABLE`/PK 元数据细节、
   `SELECT DATABASE()` 返回 `default`、`SHOW DATABASES` 去硬编码。
8. 并行执行器循环导入修复（或显式关闭并日志说明）；`test_ipc_latency` 阈值放宽；
   环境性用例标 `skipif`；E 类测试期望修正。
9. **文档对齐**：重写 README（删"已修复/未修复"混排、更新测试基线 761、删打包虚假声明、
   补使用示例）；删除或明确标注 8 份虚构文档；`docs/DEAD_CODE.md` 加"当前状态"。
10. 清理：删除/迁移 ~154 个死模块（至少 74 个零引用模块）、清 `data_*` 目录、移除遗留 worktree、
    双 LICENSE 合并、根目录调试产物移入 `_archive/`。

### 阶段 2：Studio 与 Web 收口（1–2 周）
11. Studio：SQL 执行移入 QThread + 分页/流式 + 读超时；错误原文完整显示；连接密码/AI key 加密
    （系统钥匙串或主口令）；生产保护真正接线；删除虚假功能入口（仪表盘/Hex/Excel 复制/工作流）
    或补齐入口；补 GUI 离屏行为测试。
12. Web：连接管理界面/端点；WebSocket 或去掉实时宣称；补 logout/refresh；history 按用户隔离。

### 阶段 3：性能与架构（2–4 周，按收益排序）
13. WAL 行级 CRC + 非尾行损坏显式报错；二进制/紧凑行格式替换 JSON 载荷；
    COMMIT 只对参与事务的表 fsync；batch_insert 接线（回写优势）；审计批量 flush。
14. 统一三条 SELECT 执行路径（消除"WHERE 可用、投影报错"类能力漂移）；`EXPLAIN` 反映真实访问路径；
    隔离级别要么实现 RC 要么如实报 RR。
15. 决策 OLAP/列存/Cython/并行：要么接入 SQL 路径并做门禁，要么斩断/移出生产包（建议后者，当前无收益）。
16. 引入 CI（pytest 分片 + benchmark 门禁 + 打包校验 + `python -m compileall`），加版本号单一来源。

### 明确"不做"清单
- 不要继续堆"企业级"概念模块（data_mesh/federated_gov/digital_twin 等）——它们既无 UI 入口也无数据源。
- 不要在没有预处理语句/二级索引/类型校验之前宣称"MySQL 兼容"或"Navicat 替代"。
- 不要在单线程 Python 引擎上追求并发/OLAP 数字；先把单机单线程的契约正确性与吞吐做扎实。
- 不要用文档数字反推实现目标；性能只认 `benchmarks/` 门禁的实测。

---

## 12. 结论

1. **软件性质**：一个 AI 批量生成、经多轮审计修复的「数据库内核 + 桌面管理工具 + Web 壳」研究型项目；
   内核真实、上层宽而浅，产品级底线（认证、授权、事务、类型校验、打包、CI）普遍缺失。
2. **最大风险**：不是"功能少"，而是**"看起来成功、实际不是"**——默认无口令、协议事务 no-op、
   类型不校验、Web 可伪造管理员、触发器可锁表、复制位点回退。这些缺陷不会在正常测试中暴露
   （711 个测试通过），但会在真实客户端与生产环境造成数据/安全事故。
3. **投入判断**：
   - 若目标是**学习/研究数据库内核**：值得保留与继续，重点补齐存储层测试与文档；
   - 若目标是**内部原型/单机小工具**：完成阶段 0+1（约 1 周）后可用，但必须单机、离线、无敏感数据；
   - 若目标是**对外发布/生产使用/替代 MySQL 或 Navicat**：不建议，需重做安全模型、协议完整性、
     打包与发布流程，工作量接近重写上层。
4. **最短可行改进路径**：先做阶段 0 的 5 项（约 1–2 天），即可把"默认部署即被控制"与"协议事务假成功"
   两个最高危问题消掉；之后用阶段 1 把"宣称 = 现实"，再用阶段 2/3 决定产品方向。

---

## 附录 A：证据与文件索引

| 文件 | 内容 |
|---|---|
| [docs/analysis_2026-10-19/00_overview.md](analysis_2026-10-19/00_overview.md) | 总体架构/依赖/工程实践/文档分级/Lead 复核 |
| [docs/analysis_2026-10-19/01_engine_core.md](analysis_2026-10-19/01_engine_core.md) | 引擎存储/SQL/正确性/代码质量/未提交改动（297 行） |
| [docs/analysis_2026-10-19/02_service_security.md](analysis_2026-10-19/02_service_security.md) | 协议/认证/权限/审计/复制/并发/监控（381 行） |
| [docs/analysis_2026-10-19/03_studio_web.md](analysis_2026-10-19/03_studio_web.md) | Studio 功能矩阵/可达性/质量/安全 + Web（416 行） |
| [docs/analysis_2026-10-19/04_verification.md](analysis_2026-10-19/04_verification.md) | 测试/基准/端到端/打包/文档抽查（319 行） |
| `docs/analysis_2026-10-19/logs/` | pytest / 基准门禁 / E2E / 打包 / GUI / Web 原始日志 |
| `_vb_tmp/` | 全量 pytest 完整原始 stdout（UTF-16LE 118KB + UTF-8 转码） |
| `_engine_probe/`、`tmp_service_security/`、`_studio_web_tmp/` | 各子报告实测脚本与原始输出 |
| [benchmarks/README.md](../benchmarks/README.md) | 性能门禁方法/基线/白皮书对照（现行可信） |
| [docs/AUDIT_REPORT.md](AUDIT_REPORT.md)、[docs/review/](review/) | 历史审计（本报告已逐条标注过时/仍成立） |

## 附录 B：复现命令（关键项）

```powershell
# 1) 全量测试（本机 31:07，单机空闲约 3-5 分钟量级）
python -m pytest tests -q -p no:cacheprovider

# 2) 性能门禁（约 15 秒，实测 PASS 5/5）
python -m benchmarks.run --suite quick --gate

# 3) 服务端 + pymysql 端到端
python -m lightdb.main --mode single --host 127.0.0.1 --port 18896 --data-dir _tmp_data
python -c "import pymysql; c=pymysql.connect(host='127.0.0.1',port=18896,user='root',password=''); cur=c.cursor(); cur.execute('CREATE TABLE t(id INT PRIMARY KEY, v INT)'); cur.execute('INSERT INTO t VALUES (1,2)'); cur.execute('SELECT * FROM t'); print(cur.fetchall())"

# 4) GUI 离屏冒烟
$env:QT_QPA_PLATFORM='offscreen'; python _vb_tmp\gui_smoke.py

# 5) 打包内容检查
python -c "from setuptools import find_packages; print(find_packages(include=['lightdb*','lightdb_studio*','web*']))"
pip wheel . -w _tmp_wheel --no-deps --no-build-isolation
```

## 附录 C：分析边界与未验证项

1. **多进程模式**（`--mode multi`）在本沙箱因命名管道/共享内存被禁无法启动；其结果仅来自源码推断，
   未在普通 Windows/Linux 环境复核。
2. **Windows 专属路径**（`socket.share`/`msvcrt.locking`）、`SO_REUSEPORT`（Linux/macOS）、
   Docker 构建（无 daemon）、AI 外部 API（无 key）、复制长期稳定性均未实测。
3. 本次全量 pytest 与 4 个 agent 并发，墙钟与计时类用例不具性能可比性；基准门禁在测试结束后运行，
   数字可用。
4. 失败分类（真实缺陷/环境/flaky/测试自身）含主观判断，边界用例已在 04 报告 §2.2 逐条标注。
5. `_vb_tmp/`、`_engine_probe/`、`tmp_service_security/`、`_studio_web_tmp/`、`_analysis_tmp/`
   为本次分析的临时证据目录（未跟踪），保留供复核；47 个 `data_*` 目录为项目历史遗留，未清理。
