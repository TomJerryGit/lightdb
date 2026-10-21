# LightDB + Studio vs Navicat + MySQL 对比与下一步路线（2026-10-21）

> 目的：回答"与 Navicat + MySQL 相比，功能宽度/深度/UI/性能如何，下一步该开发什么"。
> 原则：**只用本机可复现事实 + 源码/公开能力**；未实测的横比明确标注"未做"。

## 0. 结论摘要（先看这里）

1. **不要正面对标"完整 Navicat + MySQL 替代"**：MySQL 是 30 年积累的通用数据库，
   Navicat 是成熟的商业客户端；LightDB 4.0.0 是纯 Python 引擎 + Qt Studio，
   在 SQL 语义广度、第三方客户端兼容、性能量级上短期无法追平。
2. **推荐定位：轻量、零安装、可嵌入的 MySQL 兼容数据库 + 现代轻量 GUI**，
   主场景=教学/演示/CI 夹具/边缘小型数据；同时让 Studio 能连 MySQL/其它库，
   做成"一个现代、轻、好用的数据库工作台"（差异化：数据安全预览/回滚、图表报表/
   Schema 对比/快照备份内置、中英双语、可扩展插件、零安装 demo）。
3. **下一步最高 ROI 不是加面板，而是两件事**：
   - **第三方客户端兼容 + 诚实性修复**（Navicat 10/mysql CLI 走老 libmysql，
     当前一批 `SHOW ...` 语句 1064；`PARTITION BY`/`CREATE PROCEDURE` 被接受但
     未实现——静默丢语义比报错更危险）；
   - **数据安全与恢复可信**（备份/恢复、WAL checkpoint/压实、崩溃恢复压测）。
4. **性能**：同机同负载横比**尚未做**（本机 MySQL 5.0 root 口令未知；且 5.0 是
   2008 版本）。LightDB 有绝对实测基线（见 §4），下一步先建统一 benchmark harness，
   再谈优化目标；Cython 在 Windows 堆损坏根因未定位前不重启。

## 1. 对比环境（本机实测事实）

| 项 | 实证 |
|---|---|
| MySQL | 本机 Windows 服务 `MySQL` 运行中，端口 **3306**；CLI `MySQL Server 5.0/bin/mysql.exe` → **Ver 14.12 Distrib 5.0.51b（2008 年，32 位）** |
| Navicat | `D:\Program Files\Navicat For MySQL 工具软件\Navicat for MySQL\navicat.exe` → **Navicat for MySQL 10.0.11（2012 年）**，PremiumSoft |
| LightDB | 本仓库 **4.0.0**（纯 Python 引擎；数据 `_analysis_tmp/gui_full/data`，8898 demo） |
| Studio | LightDB Studio v4.0.0（PySide6，本轮已完成 P0–P3） |
| 未做项 | MySQL root 口令未知 → **未做同负载性能横比**；Navicat 10 未做完整人工点击矩阵（建议下一轮补齐） |

**协议兼容实证**：用本机 **MySQL 5.0 老客户端**（与 Navicat 10 同代 libmysql 系）
直连 LightDB 8898，以下全部成功：`SELECT VERSION()`、`SHOW DATABASES`、
`SHOW TABLES`、`DESC`、`SHOW CREATE TABLE`、`SHOW INDEX`、`EXPLAIN`、
`CREATE TABLE/INSERT/UPDATE/DELETE`、`BEGIN/COMMIT`、DECIMAL/中文数据（pymysql 路径）。
说明**基础 wire 协议与常用 SQL 是可用的**。

## 2. 功能宽度对比（引擎 + GUI）

### 2.1 引擎 SQL 面（本机 8898 实测探针）

| 能力 | 结果 | 说明 |
|---|---|---|
| DDL（CREATE/DROP TABLE、ADD/DROP COLUMN、RENAME） | ✅ | 基础表操作可用 |
| ALTER MODIFY COLUMN | ❌ 明确 1064 | 只支持 ADD/DROP/RENAME；Navicat 表设计器改列类型会失败 |
| `CREATE INDEX`（独立语句） | ❌ 1064 | 索引只能在 CREATE TABLE 内联；Schema 对比已按"需手工"处理 |
| `CREATE VIEW` | ❌ 1064 | 视图不实现 |
| `CREATE TRIGGER` | ⚠️ 部分 | 仅支持 `SET NEW.x = 表达式` / `SET OLD.x`，不支持 `SET @var` 等 |
| `CREATE PROCEDURE` + `CALL` | ❌ **静默语义缺失** | 语句被接受，`CALL` 返回空、过程体未执行（必须改为显式报错或实现） |
| `CREATE EVENT` | ❌ 1064 | — |
| `FOREIGN KEY` | ❌ 明确拒绝 | 错误信息诚实；ER 图只能用手工关系（已实现） |
| `FULLTEXT` | ❌ 明确拒绝 | 提示"建表后单独执行"，但独立 CREATE FULLTEXT INDEX 也未实现（提示需修正） |
| `PARTITION BY` | ✅ 已实现（本轮消除静默） | batch12/13/15/v4 已锁定实现；本轮起 `SHOW CREATE TABLE` 如实回显分区子句 |
| WINDOW 函数 / CTE / JSON_EXTRACT | ✅ | 本轮 probe 全部成功 |
| `SAVEPOINT` / `PREPARE`（SQL 层） | ✅ SAVEPOINT 已实现；PREPARE 仍 1064 | SAVEPOINT/RELEASE/ROLLBACK TO（嵌套/重名/1305/无事务语义）已测试；协议层 prepared 已支持，SQL 层 PREPARE 暂不做 |
| `SHOW COLLATION/CHARSET/ENGINES/TABLE STATUS/FULL COLUMNS/FULL TABLES/WARNINGS` | ✅ E1 已实现 | 列名对齐 MySQL 5.7、值真实；mysql 5.0 老客户端元数据序列实跑通过 |
| `SHOW GRANTS`/`CREATE USER`/`GRANT` | ⚠️ 部分 | 口令策略严格；GRANT `IDENTIFIED BY` 不支持（要求 CREATE USER / SET PASSWORD） |
| 字符集 | ✅ 严格化 | 非法 UTF-8→1366（不再落 U+FFFD）；gbk→1115；latin1 仅记录连接元数据不转码（既有测试锁定） |
| 复制/binlog | ⚠️ 单主单从 | append-only binlog + 位点复制；无 GTID/半同步 |
| TLS | ✅ 本轮已接线 | 服务端 CLIENT_SSL/SSLRequest + 证书配置；客户端 SSL 连接可用 |

### 2.2 Studio vs Navicat 10（GUI 宽度）

| 能力 | LightDB Studio 4.0.0 | Navicat for MySQL 10（2012） |
|---|---|---|
| 连接管理 | ✅ 多连接/测试/密码显隐/DPAPI 加密 | ✅ 更成熟；**SSH 隧道/HTTP 隧道/SSL 客户端证书**更强 |
| 对象树/库表浏览 | ✅ 懒加载、右键菜单、生成 SELECT、DDL | ✅ 成熟稳定，含更多对象类型（视图/函数/事件等） |
| SQL 编辑器 | ✅ 自动补全、错误高亮、格式化、执行/取消、历史 | ✅ 成熟（高亮/补全/多窗口/片段），深度更强 |
| 数据浏览/编辑 | ✅ 分页、类型编辑、单事务批量提交 | ✅ 成熟网格（虚拟化、筛选、二进制/JSON 编辑更全） |
| 导入/导出 | ⚠️ **导出 CSV/XLSX；导入仅 CSV** | ✅ CSV/Excel/JSON/SQL/TXT 多格式向导 |
| 备份/恢复 | ✅ SQL 快照+定时保留+恢复预览+紧急快照回滚（安全设计更强） | ✅ 备份/还原向导（mysqldump 风格），但缺少"恢复前预览/失败回滚" |
| 结构对比 | ✅ 差异矩阵+安全 SQL 预览+diff 导出 | ✅ 结构同步（双向、可执行） |
| 数据传输/同步 | ⚠️ 迁移向导较浅 | ✅ 数据同步/结构同步/数据传输是一等能力 |
| ER 图 | ✅ 真实 FK/手工关系三态（引擎无 FK 强制） | ✅ 成熟 ER/模型设计 |
| 查询构建器 | ✅ 有（浅） | ✅ 成熟可视化构建器 |
| 图表/报表 | ✅ 可配置图表 PNG/SVG + 报表模板/参数/导出 | ⚠️ 报表构建器（Navicat 10 有 Report，但图表配置弱于新版） |
| 任务调度 | ✅ 快照定时（60s QTimer） | ✅ 调度器（备份/报表/传输/查询） |
| 用户/权限管理 | ⚠️ SQL/API 可用，无完整 GUI | ✅ 用户/权限管理器 |
| 服务器监控 | ✅ 性能面板/慢查询/告警（面板较多） | ✅ Server Monitor |
| 多语言/主题/可访问性 | ✅ 中英热切换、深色主题、a11y、高 DPI 说明 | ⚠️ Navicat 10 老版本主题/高 DPI 支持弱 |
| 插件/扩展 | ✅ PluginBase + 启停/重载 + 示例插件 | ❌ 无插件生态 |
| 零安装 demo | ✅ run_studio.ps1 + 首启引导 + 内嵌引擎 | ❌ 必须另装 MySQL 服务 |
| 自动更新/崩溃日志 | ✅ 更新检查/下载校验、滚动日志+脱敏+crash 文件 | ❌ 无（老版本） |
| Web 版 | ✅ API + 前端（format/报表/质量） | ❌ |

**宽度结论**：Navicat 10 在"多格式导入/传输同步/用户管理/SSH 隧道"更宽；
Studio 在"现代 UX/安全恢复/图表报表/在线更新/插件/双语/Web"更现代。
**深度结论**：引擎 SQL 语义深度差距大（视图/索引/存储过程/分区/SAVEPOINT/字符集/
外键等），GUI 网格虚拟化、编辑器智能、数据编辑细节仍需补。

## 3. UI/UX 对比（主观 + 可观察事实）

- **Studio 优势**：统一深色主题、字号/高 DPI 处理、中英热切换、快捷键卡、
  错误面板可复制、首启引导、对象树懒加载、导入导出进度/取消、恢复前预览与回滚，
  启动快（本机 **687ms**，见 §4），窗口结构清晰（对象树/查询/结果/工具面板）。
- **Navicat 优势**：多年打磨的表格交互（列宽/筛选/排序/编辑手感）、大量向导、
  多窗口/多连接并行、对象类型齐全、导入导出配置丰富、右键上下文完整、文档完善。
- **Studio 需补的 UX**：结果表**完整虚拟化**（10 万行仍用 QTableWidget）、
  多窗口/标签管理、编辑器补全深度（函数级/上下文）、数据网格的二进制/JSON 编辑、
  导入格式向导（Excel/JSON/SQL）、错误恢复与重连体验、暗色/亮色两套主题差异化。

## 4. 性能对比

### 4.1 LightDB 绝对实测（本仓库基线，非横比）

| 指标 | 实测 |
|---|---|
| 逐行 INSERT | **1,501 rows/s**；批量（500/语句）**1,356 rows/s** |
| 点查 | **888 q/s**（进程内 1.13ms）/ **1,138 q/s**（TCP） |
| 范围扫描 / 聚合 | **4.8k / 11k rows/s** |
| 并发点查 1→8 客户端 | 1,163 → **749 q/s（−36%）**（并发回退） |
| WAL 运行期增长 | ≈200 B/行，**无 checkpoint/轮转**（长跑磁盘无界增长） |
| GUI 启动 / 1200 行渲染 / RSS 增量 | **687ms / 379ms / +56MB**（目标预算内） |
| 测试基线 | **1250 用例 / 1209 passed / 0 failed**；CI 全绿（含 GUI smoke/perf） |

### 4.2 与 MySQL/Navicat 的横比状态

- **未做**同机同负载横比：本机 MySQL 5.0 root 口令未知，且 5.0 与 LightDB 目标
  （MySQL 5.7 wire）差异过大；直接拿"MySQL 5.0 性能"对比会误导。
- 从量级判断：纯 Python 逐行引擎与 C 引擎的 INSERT/点查差距在**1–2 个数量级**，
  这是架构决定的，短期不应以"性能超越 MySQL"为目标；应在**小数据/嵌入式/演示/
  CI 夹具**场景做到"够快 + 启动快 + 零依赖"。
- **下一步必须做的性能工作**：统一 benchmark harness（同机、同数据、同 SQL、
  同一客户端），覆盖 INSERT/点查/范围扫描/聚合/并发/10 万行分页；为 LightDB 和
  可获得的 MySQL 版本各出一份 JSON 报告 + 回归门槛。

## 5. 定位与产品策略（建议）

### 选项对比

| 方向 | 说明 | 风险 |
|---|---|---|
| A. 轻量 MySQL 兼容引擎 + 现代 GUI（推荐） | 教学/演示/CI/边缘；零安装、可嵌入、启动快、数据安全工具强 | 市场小；需把"正确性/兼容/可靠"做到可信 |
| B. 纯多数据库 GUI 客户端（Navicat 替代） | Studio 已能连 MySQL；做成现代轻量 Navicat | 与 Navicat/DBeaver/DataGrip 正面竞争，需海量功能 |
| C. HTAP/OLAP 性能叙事 | — | 不成立（dispatch 实测 0.44–1.10x，Cython 曾堆损坏），**不要做** |

**推荐 A + B-lite**：
- 引擎保持"轻量 MySQL 兼容"定位，**先把正确性/兼容/恢复做硬**，不追功能宽度；
- Studio 同时支持连接 MySQL/其它库，做"轻量现代数据库工作台"；核心差异化是
  **数据安全预览/回滚、快照+定时保留、Schema 对比、图表/报表、双语、插件、零安装 demo**；
- 对外表述避免"替代 MySQL/Navicat"，改为"轻量 MySQL 兼容数据库 + 现代 GUI 工作台"。

## 6. 下一步开发优化路线（按 ROI 排序，可直接排期）

### P4-A 第三方客户端兼容 + 诚实性（建议 2 周，最高优先）

为什么先做：Navicat 10 / mysql CLI 是用户判断"像不像 MySQL"的第一入口；当前一批
`SHOW` 语句 1064 + 静默丢分区/过程语义，会让第三方 GUI 报错或"看起来成功实际没生效"，
比缺功能更伤信任。

| 任务 | 状态/验收 |
|---|---|
| 1. 实现 `SHOW TABLE STATUS / FULL COLUMNS / FULL TABLES / COLLATION / CHARSET / ENGINES / WARNINGS / GRANTS / CREATE VIEW` 等元数据语句 | ✅ **E1 已完成 2026-10-21**（除 SHOW GRANTS/CREATE VIEW）：列名对齐 MySQL 5.7、值真实不编造；mysql 5.0 老客户端元数据序列实跑通过（18 个新测试） |
| 2. **静默语义审计**：`PARTITION BY`、`CREATE PROCEDURE/CALL`、表选项等 | ✅ **E1 已完成**：PARTITION/存储过程原本已实现（batch12/13/15/v4 锁定），改为 `SHOW CREATE` 如实回显、`CALL` 返回结果集、多结果集显式 1235；表选项 ENGINE=InnoDB→1064、CHARSET=gbk→1115、COLLATE→1273、ON UPDATE/ROW_FORMAT→显式错误、生成列赋值→3105 |
| 3. 字符集：非法/未支持字节 → 1366；`SET NAMES` 校验 | ✅ **E1 已完成**：文本/预处理/COM_INIT_DB/FIELD_LIST 严格 UTF-8→1366（不落 U+FFFD）；gbk→1115；latin1 仅记录连接元数据不转码（既有测试锁定） |
| 4. SAVEPOINT / RELEASE SAVEPOINT / ROLLBACK TO SAVEPOINT | ✅ **E1 已完成**（嵌套/重名/不存在 1305/无事务语义）；已知限制：UPDATE/DELETE 回滚到 SAVEPOINT 后再 COMMIT 的崩溃恢复暂缺补偿 WAL（进程内正确） |
| 5. `CREATE INDEX` / `CREATE VIEW` / `ALTER MODIFY COLUMN`：实现（工作量中） | ⏳ 待 E2；Schema 对比/表设计器当前显式提示"需手工/引擎限制" |
| 6. Navicat 10 人工兼容矩阵（连接/对象树/查询/数据编辑/导入导出/备份/ER） | ⏳ 待做（本轮已用 mysql 5.0 老客户端覆盖元数据序列；建议下一步真实点击 Navicat 10 记录 bug 列表） |

> E1 偏差说明：原计划"PARTITION/PROCEDURE 改 1064"，实测二者已真实实现且被
> batch12/13/15/v4 测试锁定，强改会回退既有测试；因此改为**消除静默**（回显真实语义、
> CALL 返回结果、多结果集显式错误）。`ENGINE=InnoDB` 显式拒绝是"禁止静默"的代价：
> 用 mysqldump/Navicat 恢复时需先剥离该表选项。

### P4-B Studio 深度对齐（2–6 周）

| 任务 | 价值 |
|---|---|
| SSH 隧道 + SSL 客户端证书 + 连接导入/导出 | 远程数据库是 Navicat 核心场景 |
| 导入 XLSX/JSON/SQL（当前仅 CSV）；导入向导列映射/预览/失败行 | 数据工作日常 |
| 跨连接数据传输 / 结构同步（复用 Schema 对比引擎，双向可执行） | 最常用的"迁移/同步"能力 |
| 用户/权限管理 GUI；服务器监控/慢查询聚合；可视化 EXPLAIN | 运维闭环 |
| 结果表 `QAbstractTableModel` 完整虚拟化（10 万行）、多窗口、编辑器补全深度 | 大库体验 |
| 主题亮色版、图表/报表持久化模板、数据生成器 | 差异化体验 |

### P5 性能与可靠性（持续，与 P4 并行）

1. 建统一 benchmark harness（LightDB + 可获得 MySQL，同机同数据同 SQL），出 JSON
   报告 + 门槛；先测绝对基线，再谈优化。
2. 存储/恢复硬化：**WAL checkpoint/轮转**（当前无界增长）、崩溃恢复压测、
   备份/恢复往返校验、页/索引压实（vacuum 只释放槽位不紧凑）。
3. 热点优化：批量 INSERT 的 WAL/审计 fsync 合并、点查路径、索引选择；目标示例
   （待 harness 标定）：单连接点查 ≥2k qps、逐行 INSERT ≥3k rows/s、1M 行聚合 <2s。
4. GUI 性能预算已有门禁（启动<5s 硬限/2s 目标，1200 行<3s/500ms 目标）；把 10 万行
   虚拟化、10k 行渲染 <1s 纳入。
5. **Cython 不重启**：Windows 堆损坏（0xc0000374）根因未定位前保持纯 Python；
   若再尝试，必须独立进程隔离 + 崩溃回归长跑。

### P6 工程质量与交付（与 P4/P5 并行）

- MSI 安装包/代码签名/自动更新通道（框架已就绪）；干净 Windows 环境验证 onefile `--smoke`；
- Web 端真实浏览器 E2E（当前无浏览器环境，仅有静态+契约测试）；
- 安全审计（认证/授权/日志脱敏/TLS 全链路）；崩溃日志外发 opt-in（当前本地）；
- 文档：Navicat 对比矩阵、兼容性验收清单、用户指南（已重写）持续更新。

## 7. 90 天执行建议（可调整）

| 周 | 重点 | 交付 |
|---|---|---|
| 1–2 | P4-A E1：SHOW 兼容 + 静默语义 + 字符集严格化 + SAVEPOINT | ✅ 已完成 2026-10-21：18 个新测试；mysql 5.0 老客户端元数据序列实跑通过 |
| 3–4 | SAVEPOINT、CREATE INDEX/VIEW、ALTER MODIFY；Navicat 人工矩阵 bug 修复 | 第三方客户端兼容清单 v1 |
| 5–8 | Studio：SSH 隧道、XLSX/JSON 导入、跨连接传输/结构同步、用户管理、结果虚拟化 | Studio 对比 Navicat 10 的差距表缩小 ≥50% |
| 9–12 | benchmark harness + WAL/崩溃恢复硬化 + 安装包/签名/浏览器 E2E | 性能/可靠性报告 + MSI/签名产物 + 验收清单 |

## 8. 明确不做 / 暂不做

- ❌ 追 MySQL 8 全兼容（stored procedure 引擎、EVENT、FULLTEXT、空间类型、GTID、
  多源复制、窗口/CTE 之外的复杂语义）——除非有明确付费场景；
- ❌ 与 Navicat 拼"导入导出/传输同步/BI"的完整套件；先做高频 20%；
- ❌ 用 OLAP/Cython/性能数字做营销（实测无收益/曾堆损坏）；
- ❌ 在第三方兼容与恢复可信之前加更多"演示型面板"。

## 9. 本轮现场发现并已修的真实缺陷

- `SQLEditor.append_sql()` 使用 `cur.End`（PySide6 6.x 枚举作用域化后不存在），
  点示例插件「插入 SELECT 1」会抛 `AttributeError`；已改为
  `QTextCursor.MoveOperation.End` + 回归测试（`test_sql_editor_append_sql_uses_scoped_enum`）。

## 10. 证据与复现

- 探针脚本：`_analysis_tmp/lead/feature_probe.py`、`feature_probe2.py`
  （连接 8898，逐条 SQL 打印 OK/FAIL；本文件表格即其输出摘要）。
- 老客户端兼容：`C:\Program Files (x86)\MySQL\MySQL Server 5.0\bin\mysql.exe
  -h127.0.0.1 -P8898 -uroot --protocol=TCP -e "..."` 实跑成功。
- 本机 MySQL/Navicat 版本：`mysql --version`、`navicat.exe` 文件版本 10.0.11.0。
- 全部手工/探针查询均未修改用户 MySQL（3306）；LightDB demo 使用独立
  `_analysis_tmp/gui_full/data`。
