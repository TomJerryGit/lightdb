# LightDB 全面评估总报告（引擎 / 图形客户端 / 配合度）

评估日期：2026-10-13 · 方法：三路并行独立评估（引擎、GUI、配合度+Web），每份都标【实测】/【文档】/【推断】来源
分报告：[01_engine.md](01_engine.md)（200 行）· [02_studio.md](02_studio.md)（116 行）· [03_integration_and_perf.md](03_integration_and_perf.md)（245 行）
既有材料：[../AUDIT_REPORT.md](../AUDIT_REPORT.md) §1–§25 · [../FAILURE_TRIAGE.md](../FAILURE_TRIAGE.md) §4/§8/§9 · [../../README.md](../../README.md)

---

## 一、一句话结论

**引擎有真实内核但性能卖点未接线；GUI 是"宽而浅"的演示外壳；两者之间有明显断层——GUI 提供了服务端并不支持的功能入口，且把错误信息在展示层丢掉了。**

- 引擎：SQL 能力实测 **可用 18 / 部分可用 8 / 显式报错 3 / 未实现或无效 5**（34 项）。事务+MVCC 快照跨重启、JOIN/CTE/窗口/子查询/严格 GROUP BY/JSON/全文/生成列/存储程序/复制/备份 均可用。
- GUI：130 个 .py / 37,280 行，**真正的 DB 访问层只占 1.4%（5 文件 527 行）**；**40.2% 代码从入口不可达**；27 项功能 = 可用 8 / 部分 12 / 桩或死代码 7；**GUI 零测试**。
- 配合度：GUI **只有一条路**连库（PyMySQL → `lightdb/net` MySQL 协议服务端，无进程内路径），因此**能力上限被服务端支持面硬限**。

---

## 二、引擎（功能 / 性能 / 制约）

### 2.1 功能覆盖（34 项逐项实测）
- **可用 18**：DDL/DML/DQL 主干、`JOIN`（含别名/LEFT/多表）、子查询（含相关）、CTE（含递归）、窗口（含 `ROWS`/`RANGE` 默认帧）、`UNION`、`GROUP BY` 严格模式、`HAVING`、标量函数（9 个）、JSON 路径 `->`/`->>`、全文索引、生成列、存储过程/函数/触发器（含循环/游标/`SIGNAL`）、`SHOW` 系、`EXPLAIN`/`ANALYZE`、事务与崩溃恢复、备份恢复、复制（真实位点）、`CHECK TABLE`/`OPTIMIZE`。
- **部分可用 8**：多进程（连接层并行、查询仍单线程）、并行执行（只覆盖单表裸 `COUNT`）、权限/审计（未接线）、列存/OLAP（未接入 SQL 路径）、分区表（假成功）、`Seconds_Behind_Master`（只在追平时为 0）、Cython（回退可用、未编译）。
- **显式报错 3**（可绕行、不静默）：`BETWEEN`、`COUNT(DISTINCT)`、`GRANT`。
- **未实现或无效 5（最危险）**：`ALTER TABLE ADD COLUMN`（`parse()` 返回 `None`）、**`CHECK` 约束**、**分区表**（回 ok 但不生效）、**向量化聚合**（错值）、审计/权限未接线。
- 标量函数注册表里有 132 个，**实测仅 9 个可用**（`NOW`/`DATE_FORMAT`/`SUBSTRING`/`COALESCE`/`IF`/`CAST` 全报错）。

### 2.2 性能（本机单进程单客户端）
| 项 | 实测 | 说明 |
|---|---|---|
| 逐行 INSERT | **1,501 rows/s** | |
| 批量 INSERT（500 行/语句） | **1,356 rows/s** | **批量反而更慢**：WAL `flush` + 审计逐语句 `flush()` 主导 |
| 点查 | **888 q/s**（进程内 1.13ms）/ **1,138 q/s**（TCP） | 白皮书 12,000 QPS → **差 13.5 倍** |
| 范围扫描 / 聚合 | 4.8k / 11k rows/s | |
| 向量化 SUM（10 万行） | **704982704.0（错值）**，0.93x vs 朴素 Python（后者算对） | `np.array` 默认 `int32` 溢出 |
| 并发点查 1→4→8 客户端 | 1,163 → 925 → **749 q/s（−36%）** | 并发不扩展而是回退 |
| WAL / UPDATE 膨胀 | ≈200 B/行、≈800 B/更新；`vacuum()` 回收 **0**，仅 `shutdown()` 把 16 MB 压回 1.7 MB | 无 checkpoint/轮转 |

### 2.3 架构层最制约的三件事
1. **无 checkpoint / WAL 轮转**，且审计逐语句 `flush()` → 运行期文件无界增长，直接压低写吞吐（批量优势被吃掉）。
2. **单线程执行 + 线程池受 GIL** → 并发回退 36%；并行只覆盖单表裸 `COUNT` 且实测 **0.49x 负加速**。
3. **OLAP / 列存 / Cython 三层全部未接入 SQL 执行路径**（源码 0 引用、编译产物 0 个）→ 白皮书里的 3.5M/85M rows/s、41x、12.5x 在查询路径上**没有落点**。

### 2.4 目前不可信（按风险）
① 向量化聚合返回错值（一旦接线即成数据事故）；② 批处理 `VectorizedFilter` 完全不生效（实测 50,000 行 vs 应 16,317；根因 `BatchScan.columns` 恒空）；③ `CHECK` 约束无效（能写入 `price=-5`）；④ `GRANT`/权限未实现（登录即全权）；⑤ 分区表假成功；⑥ 列存 `GROUP BY` 返回 0 行。

---

## 三、图形客户端（`lightdb_studio`）

### 3.1 规模与可达性
130 文件 / 37,280 行。占比最高的是**数据平台"愿景"模块 22.3%（8,302 行）**、质量治理 15.1%、AI 11.2%；**DB 访问层仅 1.4%**。AST 导入图：**只有 59.8% 的行从 `app.py` 可达，40.2% 不可达**；另有 8 个已 import 但**从未实例化**的 UI 模块（2,263 行，含 `dashboard` 425、`chart_view_enhanced` 551、`import_wizard` 469）。

### 3.2 功能盘点（27 项 = 可用 8 / 部分 12 / 桩或死代码 7）
- **可用 8**：连接管理（**密码明文**）、结果表格、表设计器、数据编辑器、查询历史/片段、报表导出（CSV/HTML/XLSX 实测真落盘）、图表、`EXPLAIN` 分析。
- **部分可用 12**：SQL 编辑器（真走协议、多语句切分实测 5/5 正确，但 `autocommit=True` 无事务、`fetchall` 无分页、GUI 线程同步执行、无补全）；对象浏览器（`SHOW PROCEDURES/FUNCTIONS/PARTITIONS` 是 LightDB 私有语句，连 MySQL 恒空且异常被吞）。
- **桩或死代码 7**：**仪表盘**（只 import 未实例化）、**AI Agent**（实测 `ai_agent.py:103` 引用不存在的 `lightdb_studio.logger`，异常被 `:143` 吞掉 → `diagnose('performance')` **实发 0 条 SQL**、恒报"0 个严重问题"）、ReAct Agent（763 行全仓无引用）、脱敏规则（不影响结果集）、智能告警（从不 `start_monitoring`）、插件系统（`plugins/` 目录实测为空，钩子无人调用）。

### 3.3 代码质量
① **`except … pass` 149 处，分布在 51.5% 的文件里**——已实证造成 AI 诊断静默失效与快照静默漏表；② **UI 层 0 个 `QThread`**，查询/导入/健康检查全在 GUI 线程；③ 缺失模块造成"假功能"（`logger`）；④ `_close_tab` 只 `removeTab` 不 `deleteLater`（每次关页泄漏）；⑤ CSV 导入忽略 `execute_many` 返回值（它从不抛异常）→ 失败仍报"成功 N 行"。

### 3.4 可运行性（实测）
`python lightdb_studio\app.py` → `ImportError`（相对导入）；`python -m lightdb_studio.app` + `QT_QPA_PLATFORM=offscreen` **可进入事件循环**；离屏构造 `MainWindow` 成功（tabs=1、right_tabs=6、menus=6），DataEditor 分页/脏标记/主键保护、MetricCard 真跑 SQL、ReportEngine 真导出均可跑通。写 `~/.lightdb_studio` 被沙箱拒是**环境限制**，但目录不存在时 `config.py:71 mkdir` 会让应用直接起不来。

### 3.5 定位判断
**"带数据库管理外壳的数据治理演示/课程项目"，不是可用的数据库管理工具**。依据：功能宽而浅（DB 层 1.4%）、40.2% 代码不可达、关键功能"看起来实现了"、半数文件吞错、**GUI 零测试**、依赖私有方言却自称类 Navicat；工具必备能力（事务、分页/流式、多结果集、连接池、SSH、备份恢复、补全、跨线程）全缺。可用的 8 项连上库后确能干活，适合内部原型试点。

---

## 四、两者配合度（含 `web/` 层）

### 4.1 连接路径与代价
**唯一路径**：`ui/main_window.py:595-605` → `db/connection.py:33 (pymysql.connect)` → `lightdb/net/server.py:207-236` → `sql/executor.py:398`。**没有进程内直连**（`grep` 全仓仅 2 处命中且非引擎调用）。协议开销实测很小：`SELECT 1` 0.163→0.314ms（×1.9）、点查 ×1.6、200 行 ×1.3。风险在于**能力上限被服务端支持面硬限**，以及多进程模式下元数据经 5s 快照中转。

### 4.2 接口不匹配清单（15 条，全部 grep + 实测）
`SHOW DATABASES` 被服务端**硬编码** `['default','information_schema']`；**`SHOW CREATE TABLE` / `ALTER TABLE` / `TRUNCATE` / `SHOW INDEX` / `SHOW PROCEDURES|TRIGGERS|EVENTS` / `DROP PROCEDURE|TRIGGER` / `CREATE INDEX` 全部 1064**；`KILL` **空实现**；`SHOW COLUMNS` 的 `default` 恒 `None`、`extra` 由主键伪造、`Type` 被抹掉长度；引擎**无 `AUTO_INCREMENT`** 概念；`ENGINE=InnoDB` 静默丢弃；切库不校验且 `SHOW TABLES` 不按库过滤；登录指定 database 时 `SELECT DATABASE()` 错成 `mysql_native_password`。

### 4.3 错误呈现
**协议两条路径都正确带回错误码（未被当 OK）** —— 本会话修好的 `server.py:399-410` 生效，实测 1146/1064/1062 原样到达客户端。
但展示层有三处问题：① 引擎自身**语义码大面积错误**（NOT NULL → 1062、未知列 → 1146 且报文说"表不存在"）；② **桌面 GUI 丢弃错误原文**（`ui/result_table.py:102-110` 完全不使用入参，实测 `info_label` 只剩"错误"二字），状态栏截断 80 字符（实测 ALTER 报错被截半）；③ `web/` 反而完整（返回 `ok:false + error` 并渲染红块）。

### 4.4 `web/` 层
**同一前端库的 Web 壳，不是独立接入**：`web/api.py:11-12` 直接复用 `lightdb_studio.db.connection.DBConnection`（同样走协议），并复用 history/reports。可用 REST + 单页界面；**死代码** `web/metrics.py`（实测 `/metrics` 404）、`create_app(db,executor)` 参数无人读取；入口缺陷 `lightdb-studio-cli` 指向不存在的 `lightdb_studio.tools`。安全面：自研 HMAC-JWT、`SECRET` 与 `admin/admin123` 明文硬编码、`CORS *` + credentials、`_session_conns` 以用户名为键且**永不回收**。

### 4.5 端到端实测（真实服务端 + GUI 真实 DB 层 + 离屏 MainWindow）
**通过**：`SELECT` / `SHOW TABLES` / `SHOW COLUMNS` / `INSERT` / `UPDATE` / `LIMIT-OFFSET` 分页 / `BEGIN`-`COMMIT` / 错误码回传。
**失败**：`ALTER`、`TRUNCATE`、`SHOW CREATE`（且 `metadata.py:75` 吞成 `''`）、`SHOW INDEX`、`DROP TRIGGER/PROCEDURE` 全 1064；`AUTO_INCREMENT` 无效（不填 id 插入 → `(1062,'列 id 不允许为空')`）；`KILL` 返回 OK 但会话仍在；切换不存在的库"成功"。

### 4.6 配合度结论：**有明显断层**
| # | 断层 | 影响 |
|---|---|---|
| ① | `ALTER TABLE` 完全不支持 | GUI 改结构 / 库同步 / 表设计器编辑**不可用** |
| ② | `SHOW CREATE TABLE` 缺失且被裸 `except` 吞掉 | 快照文件**0 个 CREATE TABLE**，`snapshot.py:186-190` 在失败时仍返回 `ok:True` → **备份静默失效** |
| ③ | `KILL` 空实现 + `PROCESSLIST` 陈旧（Command 不复位、Time=连接年龄） | 性能面板"终止连接"**假成功**，长查询无法取消 |
| ④ | 错误被丢弃/截断 + GUI 内 192 处裸 `except`（149 处直接 `pass`） | 用户**无法诊断**；健康评分因 `SHOW INDEX` 失败而对每表报"无索引" |
| ⑤ | 元数据/类型语义不可信（default/extra/type/库列表/库切换/5s 陈旧） | 结构对比**必然产生假差异**并生成跑不通的 `ALTER` |
| ⑥ | 单连接共享 + UI 线程 `fetchall` + 无 read 超时 + 无虚拟化 | 离屏 `show_result` 20000×10 实测 **2448ms 阻塞 UI 线程**，一次 `SELECT *` 即可冻结界面 |

---

## 五、建议的优先级（按影响正确性 > 可用性 > 性能）

1. **止血静默错值**：向量化聚合（`int32` 溢出）、`VectorizedFilter`（`BatchColumns` 空）、`CHECK` 约束。三者都能"成功返回错结果"。
2. **OLAP/列存/Cython 三层要么接线要么摘牌**：目前既不在 SQL 路径上，又在文档/白皮书里作为性能卖点。
3. **GUI 的两处致命依赖**：错误原文必须显示（`result_table.py`）；快照/备份在 `SHOW CREATE` 失败时必须**失败**而不是 `ok:True`。
4. **`KILL`/`PROCESSLIST` 的真实性**：假成功比不支持更危险。
5. **元数据诚实化**（`SHOW DATABASES`/`COLUMNS` 的 default/extra/type、库切换校验、`AUTO_INCREMENT`）。
6. **性能三件事**：checkpoint/WAL 轮转 + 审计批量 flush（换回批量写优势）；查询路径去掉 GIL 依赖（多进程/子进程）；`ALTER TABLE` 与 `BETWEEN`/`COUNT(DISTINCT)` 等入门语法补齐。
7. **测试门禁**：先清 E 类 10 条（测试期望写错）与 2 条 flaky（时钟刻度撞号、无 warmup 计时），让 39 条读数可信；GUI 需要至少一组离屏冒烟测试。

> 引用来源：三份分报告的每一条结论都标了【实测】/【文档】/【推断】；本总报告只做收敛，未新增未经证据支持的判断。
