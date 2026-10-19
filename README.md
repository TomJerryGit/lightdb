# LightDB Studio

> 图形化数据库管理工具 · Navicat 的开源替代 · 纯 Python 实现

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)]()

---

## ⚠️ 当前实测状态（2026-10）

> 本节是 2026-10 代码审计的**审计时**实测结论，**优先于下方所有功能与性能描述**。完整报告见 [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md)，死代码清单见 [docs/DEAD_CODE.md](docs/DEAD_CODE.md)。其中若干 P0 缺陷已在同一次会话中修复，见本节末尾「本次会话已修复」。
>
> **接线口径（阶段 2 第 6 项）**：`lightdb/olap/**`（向量化）、`lightdb/columnar/**`（列存）与 Cython 加速层是**实验性**实现，**未接入 SQL 执行路径**（`Executor` / `plan_executor` 零引用），因此**不计入任何性能声明**；全部实测数字与白皮书对照表见 [benchmarks/README.md](benchmarks/README.md)。

- **服务端单进程模式**（`python -m lightdb.main --mode single`）可以监听端口并发出标准 MySQL v10 握手包（server version `5.7.30-LightDB`），但**认证校验存在缺陷，没有任何客户端能登录成功**：`lightdb/net/mysql_protocol.py:293` 的长度守卫排在空密码分支之前，`:304` 的比较少做一次 SHA1 —— 空密码与非空密码实测均被拒绝。
- **建表必然失败**：`lightdb/storage/buffer_pool.py:15` 用 `open(path, 'r+b')` 打开尚不存在的 `.dat` 文件，抛 `FileNotFoundError`。
- **所有执行错误被响应为 OK 包**：`lightdb/sql/executor.py:68` 把异常转成错误字典而不抛出，`lightdb/net/server.py:391-437` 的 `_send_execution_result()` 从不检查错误，DDL/DML 无条件回 OK 包 —— 客户端完全无法感知失败。
- **默认（多进程）模式无法启动**：`python -m lightdb.main` 在 8 核下自动走 multi，实测输出「Worker 池启动失败」后退出，端口从未被监听；`--check` 还会因裸 emoji `print` 在 cp936 控制台抛 `UnicodeEncodeError`（`lightdb/main.py:156`、`:162`）。
- **桌面端在 Python ≤ 3.11 无法启动**：`lightdb_studio/core/reports.py:172` 的 f-string 含反斜杠，属于 Python 3.12 才允许的语法。
- **Web 版可运行**（17 个 API 端点，JWT 校验有效），但存在硬编码签名密钥与默认口令等问题，详见审计报告。
- **文档中的性能数字未经仓库自身工具验证**：仓库内没有任何基准产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录不存在；`tests/regression_v4/test_performance_v4.py:13-18` 中的 12000 / 80000 QPS 目标**从未被任何测试断言**。这些数字应视为设计目标，而非实测结果。
- **复制（主从）不可用于生产（2026-10-19 E 项摘牌）**：`SlaveReplicator` 与 `SHOW SLAVE STATUS` 保留（状态语义为真），但复制链路是**实验性、单向、不可用于生产**的实现：位点来自"查询时重扫各表当前 WAL"的派生视图，**会回退**（ALTER / checkpoint 后重编号或归零）、**checkpoint 后事件丢失**、**DDL 不复制**，也没有 `START SLAVE` / `STOP SLAVE` / `CHANGE MASTER`；`MasterReplicator` / 半同步是死代码。**结论：不要把本引擎的复制当生产特性使用**，详见下文「🔁 复制支持边界」。

### 🔐 安全默认值（2026-10-19 P0 修复：认证 / Web / 连接限制）

> 2026-10-19 全面分析确认「默认部署 = 无认证远程数据库」是 P0：新库 root 空口令可登录、
> 默认监听 `0.0.0.0`、连接限制/白名单配置无人读取；Web 版硬编码 JWT 密钥与默认口令、
> 存在已认证 SQL 注入且没有新建连接端点。以下默认行为已修复（**破坏性变更，升级前必读**）：

- **root 不再空口令**（`lightdb/db/database.py`）：新数据目录首次启动生成随机口令并写入
  `<data_dir>/root.password`（日志只打印文件路径，**不打印明文**）；**旧数据目录**里 root 为空口令的，
  启动时自动迁移成随机口令（同样写 `root.password`）。
  - 显式口令：`LIGHTDB_ROOT_PASSWORD` 环境变量或 `--root-password <pwd>`（优先级最高）；
  - 本地开发/演示需要空口令时，必须显式降级：`--allow-empty-password` 或
    `LIGHTDB_ALLOW_EMPTY_PASSWORD=1`（启动会打 WARN；**禁止**用于网络可达环境）。
  - `run_studio.ps1` / `run_studio.cmd` 是绑定 `127.0.0.1` 的本地 demo，已显式携带该降级参数。
- **默认只监听回环**（`lightdb/config.py`）：`host` 默认 `127.0.0.1`；对外提供服务必须显式指定
  `--host 0.0.0.0`（或 `LIGHTDB_HOST` / config.json），启动会打印网络暴露提示。
- **连接限制真正生效**（`lightdb/net/server.py`）：`max_connections` / `max_conn_per_ip` 在握手前执行
  （超限回 1040），`ip_whitelist_enabled` / `ip_whitelist` 同样在准入阶段拦截（1130）；失败登录加 0.5s 延迟。
- **Web 版**（`web/`）：
  - JWT 密钥不再硬编码：`LIGHTDB_WEB_SECRET` > `~/.lightdb_studio/web_secret`（0600，自动生成）> 进程内随机；
  - 默认用户已删除：用 `LIGHTDB_WEB_USERS`（JSON）或 `--admin-password` / `LIGHTDB_WEB_ADMIN_PASSWORD` 配置；
    都没有时自动生成随机 admin 口令写入 `~/.lightdb_studio/web_admin.txt`（日志不打印明文）；
  - 表名/库名/列名/`order_by` 全部走标识符白名单校验（非法 400），取值继续 `%s` 参数化；
  - CORS 改为 `LIGHTDB_WEB_ORIGINS` 白名单（默认本机两个源，不再 `*`+credentials）；
  - 新增 `POST /api/v1/connections`（仅 admin）与 `web.start --connections-file`，Web 端可以真正配置连接并查库；
    `/connections/test` 仅 admin、支持 `LIGHTDB_WEB_ALLOWED_HOSTS` 白名单；`/metrics` 已真正挂载；
  - `web.start` 默认 `--host 127.0.0.1`。
- **测试兼容**：`tests/conftest.py` 对既有 700+ 空口令用例显式设置降级 env（可审计的显式降级，
  不是把安全默认改回不安全）；新默认行为由 `tests/test_auth_defaults.py`（11 例）与
  `tests/test_web_security.py`（22 例）单独验证。
- **部署**：`docker/entrypoint*.sh` 在未提供 `LIGHTDB_ROOT_PASSWORD` / `LIGHTDB_WEB_USERS` 时生成随机口令并
  echo 一次（容器重启会变，生产请显式注入）。

> 本次修复的详细证据、复现与仍未修复项（复制仍为**实验性、不可用于生产**等）见
> [docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md](docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md)。

### ⚠️ 性能声明状态说明（2026-10 阶段 0 收尾 + 阶段 2 第 6 项「摘牌」，**必读**）

> 本节覆盖 `README` / `docs/**` 里全部 OLAP / 列存 / 并行 / Cython 性能数字，
> 逐条给出**当前真实状态**。原始文档**不删除**，其段落内保留原文以便对照。
>
> **统一口径（阶段 2 第 6 项，方案 A）**：OLAP（`lightdb/olap/**` 向量化）与
> 列存（`lightdb/columnar/**`）是**实验性**实现，**未接入 SQL 路径**
> （`Executor` / `plan_executor` 零引用），因此**不计入任何性能声明**；
> 全部可复现实测数字与白皮书对照表在 [benchmarks/README.md](benchmarks/README.md)。
> 本阶段已删掉生产代码里唯一一条「悄悄走到向量化引擎」的分派
> （[`lightdb/parallel/executor.py`](lightdb/parallel/executor.py) 的
> `_serial_group_by` 分解回退），并给保留的实验性入口加了显式状态检查点
> （[`lightdb/olap/status.py`](lightdb/olap/status.py)）；锁测试见
> [`tests/test_olap_not_wired.py`](tests/test_olap_not_wired.py)。

- **未接入 SQL 执行路径**：`lightdb/olap/**`（向量化引擎）、`lightdb/columnar/**`
  （列存 `ColumnStore`）、`lightdb/parallel/**`（并行执行）在 `Executor` /
  `plan_executor` 里**零引用**；`lightdb/cython/*.pyx` 的编译产物
  （`.pyd` / `.so` / `.c`）在仓库里**0 个**（`predicate_v4.py` 只是引入了
  回退包装，`status()['any_accelerated']` 为 `False`）。因此阶段 0 已修好的
  向量化聚合/过滤错值**仍未接线**——错值目前不会在生产查询路径上出现，
  但这只是"没接线"，不是"已修复"。
- **以下数字是设计目标，不是实测**（对应能力均为**实验性、未接入 SQL 路径、
  不计入性能声明**）：`12,000 QPS`（单线程点查）、
  `92,000 QPS`（8 核）、`3.5M rows/s`（OLAP 扫描）、`85M rows/s`（大表聚合）、
  `41x`（列存加速比）、`12.5x`（Cython B+Tree）、`3.15x`（向量化过滤）——
  它们只出现在设计文档与 `PERF_TARGETS` 常量里，**没有任何仓库内产物或
  断言支撑**；逐条对照见 [benchmarks/README.md](benchmarks/README.md)。
- **可复现的实测数字**（来源 [docs/review/01_engine.md](docs/review/01_engine.md)，
  与 [docs/review/04_ROADMAP.md](docs/review/04_ROADMAP.md) 一致）。
  这些数字现在有**可复跑的门禁**：[benchmarks/README.md](benchmarks/README.md)
  （`python -m benchmarks.run --suite quick --gate`，未达标退出码 1；
  目标表、实测基线、白皮书对照与 CI 开关都在那一页）：

  | 项 | 实测 | 与文档声明的关系 |
  |---|---|---|
  | 逐行 INSERT | **1,501 rows/s** | 写路径由逐语句固定开销主导 |
  | 批量 INSERT（500 行/语句） | **1,356 rows/s** | **批量反而更慢**（WAL + 审计逐语句 `flush()`） |
  | 点查 | **888 q/s**（进程内 1.13 ms）/ **1,138 q/s**（TCP） | 白皮书 12,000 QPS → **差 13.5 倍** |
  | 范围扫描 / 聚合 | **4.8k / 11k rows/s** | 纯 Python 逐行量级 |
  | 并发点查 1→4→8 客户端 | 1,163 → 925 → **749 q/s（−36%）** | 并发不扩展而是**回退** |
  | WAL 运行期增长 | **≈200 B/行**，无 checkpoint/轮转 | 长跑实例磁盘无界增长 |
  | 10 轮 × 2000 行 UPDATE 后 `.dat` | **669 KB → 16 MB**（≈800 B/更新） | `vacuum()` 只释放槽位、不紧凑 |

- **仍未验证**：Cython **从未编译验证**（本机无 Cython、无 C 编译器）；
  并行执行只覆盖单表裸 `COUNT` 且实测 **0.49x 负加速**；列存/OLAP 的
  `GROUP BY` 返回 0 行、向量化 `SUM` 曾因 `int32` 溢出返回错值。
- **历史文档状态**：[docs/V4_DECISION.md](docs/V4_DECISION.md) 已被**有意截断**
  （44 行，含"原文档在此处截断，后续章节缺失"）以移除不可核实的数据；
  本 README **不补写**其中缺失的历史内容，只在此处如实标注该文件不完整。
  同类的"数据可信度声明"已在 [docs/ARCHITECTURE_v4.md](docs/ARCHITECTURE_v4.md)、
  [docs/PERFORMANCE_WHITEPAPER_v3.md](docs/PERFORMANCE_WHITEPAPER_v3.md)、
  [docs/PERFORMANCE_WHITEPAPER_v4.md](docs/PERFORMANCE_WHITEPAPER_v4.md)、
  [docs/V4_DECISION.md](docs/V4_DECISION.md)、[docs/V4_RELEASE_NOTES.md](docs/V4_RELEASE_NOTES.md)
  顶部；本次收尾补齐了其余仍把上述数字当实测陈述的文档。

- 一次完整测试运行的结果（2026-10-19，P0/P1 修复后新增 64 个用例）：**825 个用例中
  777 通过 / 24 失败 / 23 跳过 / 1 xfailed**。24 条失败与 P0 修复前对照**逐条相同**（无任何
  本次修复引入的失败）；分诊与逐条根因见
  [docs/analysis_2026-10-19/04_verification.md](docs/analysis_2026-10-19/04_verification.md)。

### ✅ 本次会话已修复（2026-10，含实测验证）

- **建表可用**：[`lightdb/storage/buffer_pool.py`](lightdb/storage/buffer_pool.py) 在数据文件缺失时先创建，不再抛 `FileNotFoundError`。
- **登录可用**：[`lightdb/net/mysql_protocol.py`](lightdb/net/mysql_protocol.py) 修正 `mysql_native_password` 校验（空密码分支前置、补上最后一次 SHA1），真实 pymysql 客户端实测登录成功。
- **错误不再伪装成功**：[`lightdb/net/server.py`](lightdb/net/server.py) 的 `_send_execution_result()` 现在会把 `{'type': 'error'}` 结果翻译为 ERR 包；实测查询不存在的表返回 `1146`。
- **客户端兼容**：新增 `SET NAMES` / `SET AUTOCOMMIT` 等会话语句确认，否则标准客户端在连接协商阶段就报 1064。
- **启动不再因 emoji 崩溃**：[`lightdb/main.py`](lightdb/main.py)、[`lightdb/main_v4.py`](lightdb/main_v4.py) 增加控制台编码保护并把裸 emoji 换成 ASCII 标记，`--check` 现已正常退出。
- **桌面端可启动**：[`lightdb_studio/core/reports.py`](lightdb_studio/core/reports.py) 的 f-string 改写为 Python 3.8+ 兼容；另修 [`agent_chat.py`](lightdb_studio/ui/agent_chat.py) 缺失的 `QLineEdit` 导入、[`sql_editor.py`](lightdb_studio/ui/sql_editor.py) 的 `QPlainTextEdit.ExtraSelection`、[`main_window.py`](lightdb_studio/ui/main_window.py) 的状态栏构建顺序。offscreen 实测 `MainWindow` 可构造（6 个菜单）。
- **打包与容器**：Dockerfile/entrypoint 可用，但 [`pyproject.toml`](pyproject.toml) 的
  `packages.find` **仍未包含 `lightdb` 引擎包**、也没有 `lightdb-server` 入口
  （`lightdb-studio-cli` 指向不存在的 `lightdb_studio.tools.cli`）——该问题仍待修，
  详见 [docs/COMPREHENSIVE_ANALYSIS_2026-10-19.md](docs/COMPREHENSIVE_ANALYSIS_2026-10-19.md) §10。
- **多进程模式已接线可用**：[`master.py`](lightdb/net/master.py) 现在会建立每 Worker 的连接队列与响应队列、运行执行服务线程、绑定端口并把 accept 到的连接交给 Worker；[`worker.py`](lightdb/net/worker.py) 通过 RPC 把 SQL 交回 Master 执行，连接句柄用 `socket.share/fromshare` 跨进程交接（Windows）。
- **SQL 能力补齐（窗口函数 / CTE / UNION）**：[`parser.py`](lightdb/sql/parser.py) 新增 `WITH [RECURSIVE] ... AS (...)` 与 `FUNC(...) OVER (PARTITION BY ... ORDER BY ...)` 解析，[`executor.py`](lightdb/sql/executor.py) 新增 UNION / UNION ALL 执行分支与窗口计算（ROW_NUMBER / RANK / DENSE_RANK / LAG / LEAD / SUM / AVG / COUNT / MIN / MAX）。同时修掉两个**静默错答**：`GROUP BY` 原先被完全忽略（`SELECT dept, COUNT(*) ... GROUP BY dept` 退化成全表一行）、`DISTINCT` 只在解析器里记录而执行路径从未使用。回归测试见 [`tests/test_v4_sql_features.py`](tests/test_v4_sql_features.py)。
- **带表别名的 JOIN 与 JOIN 聚合可用**：原先解析器不认识表别名，`FROM users u JOIN orders o ON ...` 的 JOIN 会被**静默丢弃**、查询退化成单表查询（返回看似正常但错误的结果）。现在别名会贯通解析器 → 优化器 → 扫描节点（列前缀用别名，两表同名列不再歧义），实测 `SELECT u.city, COUNT(*), SUM(o.amount) FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.city` 返回 `[('BJ',3,60),('SH',3,150)]`，LEFT JOIN 正确保留未匹配行，- **子查询条件与 HAVING 可用**：`WHERE id IN (SELECT ...)` / `NOT IN` / `EXISTS` 原先只解析不求值（单表路径报 `KeyError: 'rows'`），现在执行器先求值再下推（IN 展开为等值 or 链、EXISTS 非空即真、空集显式恒假/恒真，支持嵌套）；`HAVING COUNT(*) > 1` 这类聚合谓词原先直接 `SyntaxError`，现在解析为 `{'agg':...}` 并在分组阶段用聚合值过滤。实测：`IN`→15 行、`NOT IN`→5 行、`EXISTS`→20 行、`HAVING COUNT(*)>5`→`[('Beijing',12)]`。相关子查询仍不支持，但会显式报错而不是静默错答。
- **Cython 已实现纯 Python 回退（但从未编译）**：`lightdb/cython/fallback/` 薄包装既有纯 Python 实现（btree/page/row_codec/predicate/buffer_pool；hash_table 无对应物故复刻），`__init__` 改为「编译版优先 → 回退」，`status()` 标明来源；`setup.py` 不再静默 `exts=[]`、去掉硬编码 `/arch:AVX2`，`build_cython.sh` 缺条件时明确报错并非零退出；另静态修复 6 处必然编译失败的 `.pyx` 错误。`status()` 实测：6 项全 `python-fallback`、`any_accelerated=False`。**本机无 Cython 且无 C 编译器，编译未验证**；`row_format_v4`/`predicate_v4` 原先用 `_cy is not None` 谎报「使用 Cython」，已改为读 `any_accelerated`。
- **窗口帧 / OFFSET / 递归 CTE / 相关子查询 / NULL 三值语义可用**：
  - 窗口帧：`ROWS BETWEEN n PRECEDING|FOLLOWING AND CURRENT ROW|n FOLLOWING`、`UNBOUNDED` 各种组合、简写 `ROWS UNBOUNDED PRECEDING`，作用于 SUM/AVG/COUNT/MIN/MAX（ROWS 物理行语义），排名/LAG/LEAD 忽略帧；`RANGE`/`GROUPS` 帧仍不支持（显式报错）。
  - `LIMIT n OFFSET m` 与 MySQL 的 `LIMIT m, n` 都生效，JOIN 计划路径也真实生效（`Limit` 节点新增 offset）。
  - 递归 CTE：`WITH RECURSIVE t AS (base UNION [ALL] recursive)` 迭代到定点，**上限 1000 轮**、超限显式报错；实测 `SELECT 1 AS x UNION ALL SELECT x+1 FROM n WHERE x<5` → `1..5`。
  - 相关子查询：`WHERE EXISTS/NOT EXISTS/IN (SELECT ... WHERE 内层列 = 外层列)` 真正求值（内层行按子查询缓存，20 行 0.04s）；含 JOIN、GROUP BY/HAVING、或引用外层列做聚合的相关形态仍**显式报错**。
  - `NOT IN` 三值语义：子查询结果含 NULL 时整体不通过（实测 `NOT IN (SELECT uid ...)` → 0 行），`IN` 只匹配真值；字面量列表 `NOT IN (1,2,NULL)` 同样处理。
- **括号条件与 FROM 子查询可用**：
  - `_where` 重写为**按括号深度与子查询边界**切分 `AND`/`OR`（跟踪 `( )` 深度与 `' " \`` 字面量，先校验括号平衡、剥离平衡外层括号、再顶层 OR→AND）。因此 `WHERE (dept='Eng' OR dept='HR') AND salary > 99`、`WHERE EXISTS (SELECT 1 FROM o WHERE o.uid=u.id AND o.amt>=20)` 都能正确求值（实测分别 3 行、`[1,2]`）；括号不匹配/未闭合字符串显式报 `SyntaxError`。
  - `FROM (子查询) 别名` 原先直接报「暂不支持」，且解析器会把外层的 WHERE/ORDER BY/LIMIT **整段静默丢弃**；现在内层求值后以别名注册为内存关系复用高级路径，实测 `SELECT * FROM (SELECT dept, SUM(salary) AS tot FROM emp GROUP BY dept) t WHERE t.tot > 250 ORDER BY t.tot DESC` → `[('Eng',320),('HR',275)]`，`... LIMIT 2 OFFSET 1` → 正确偏移，嵌套两层也生效。
  - 窗口帧新增 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`（对等组语义，实测 Eng 全 320 / HR 全 595 / Sales 全 795）；其余 `RANGE` 组合与 `GROUPS` 仍显式报错。
- **UPDATE/DELETE 的旧版本真正落盘（快照隔离可跨重启）**：行头由 13 字节扩展为 **21 字节**（`xmin|xmax|flags|dlen|prev_page|prev_slot`），UPDATE 不再原地覆写——旧版本的 `xmax` **就地写入真实事务号**并保留在堆里，新版本另写一槽并通过 `prev` 指回旧槽，索引移向新版本；DELETE 只写墓碑（`xmax`）并保留索引项。因此**重启后用记录的快照参数重建读视图，仍能读到被覆盖前的旧值**。Lead 独立验证（含直接解析页上行头字节）：

  | 步骤 | 结果 |
  |---|---|
  | UPDATE 提交后 当前读 / 老快照读 | `[[1,'new']]` / `[[1,'old']]` |
  | 页上版本链（原始字节） | `@(2,0) [1,'old'] xmax=2 prev=(0,0)`、`@(2,1) [1,'new'] prev=(2,0)` |
  | shutdown 后重开：当前读 / 重建老快照读 | `[[1,'new']]` / **`[[1,'old']]`** |

  另实现 `Table.vacuum(horizon)` / `Database.vacuum()` 回收对所有活跃快照都不可见的旧版本并缝合 `prev` 链。**注意：旧 `.dat` 数据文件不兼容**（行头布局变了，需重新建库）。
- **相关子查询补全 / `NOT(...)` / `IS NULL` / 投影算术**：
  - 相关子查询原先遇到「含 JOIN」「含 GROUP BY/HAVING」「引用外层列做聚合」就直接报错；现在改为**外层列常量折叠后交正常执行路径**（对相同折叠值做按值缓存），实测 `WHERE (SELECT COUNT(*) FROM orders o WHERE o.uid=u.id) > 1` → `[(1,)]`、相关 `EXISTS` 含 JOIN → `[(1,),(2,)]`、相关 `EXISTS` 含 `GROUP BY/HAVING` → `[(1,)]`。过程中还修掉两个静默错答（嵌套子查询漏判外层引用 → 恒空；折叠缓存键漏 value 位 → 所有外层行共用第一行结果）。
  - `WHERE NOT (...)`（可嵌套、德摩根推到叶子）与 `col IS [NOT] NULL`（WHERE 与 HAVING 均支持）可用：实测 `NOT (dept='Eng')` → 5 行、`NOT (dept='Eng' OR dept='HR')` → 2 行、`amount IS NULL` → 1 行。
  - 投影算术：`SELECT id, salary*2 AS d FROM emp`、`salary/2`、`2*salary` 及 `FROM (... ) x WHERE x.d > 200` 均可用（除零返回 NULL 而非崩溃）。
  - **投影里的标量子查询已可求值**（原先逐行静默返回 `NULL`，是典型的「报错缺位」错答）：`SELECT id, (SELECT COUNT(*) FROM orders o WHERE o.uid=users.id) AS cnt FROM users` → `[(1,2),(2,1),(3,0)]`；`SUM` 无匹配行时返回**正确的 NULL**；非相关标量子查询只算一次（`[(1,3),(2,3),(3,3)]`）。**语义边界一律显式报错**：返回多行、多列、无法安全折叠时都报错，另有 6 处原先会「填 NULL」的分支（未知列哨兵、算术操作数解析失败、窗口取列失败、投影标量函数被误当聚合等）全部改为报错。外层表未起别名而被内层同名表遮蔽时也**报错并提示起别名**（原先静默返回空集），起别名后结果正确。
  - **WHERE/HAVING 标量上下文的行数语义已统一**（原先「取第一行」，是本族最后一块静默错答）：0 行→`NULL`（比较为 UNKNOWN，该行不通过，**不报错**）；1 行 1 列→取值比较；**多行/多列→显式报错**。实测多行报「标量子查询返回了多行（2 行）」、`NOT ((SELECT MAX …)=30)` → `[(1,)]`（无订单行的 UNKNOWN 不再被错误放行）。同时关闭三处**整段静默丢弃**：HAVING 里的子查询原先被 `return True` 丢掉（现在逐分组求值，引用非分组键则报错并提示）；JOIN 查询的 `WHERE (SELECT..)=1` 原先静默返回全部连接行（现在显式报错）；`_rewrite_subqueries` 的兜底原先 `pop` 掉条件（现在报错）。
  - **JOIN 查询里的标量子查询已接通**（此前是最后一处「标量子查询被拒绝」的地方）：JOIN 查询若含需逐连接行求值的子查询，计划执行器只算 `FROM+JOIN(ON)`（投影 `*`、列名保留 `u.`/`o.` 前缀），物化为内存关系后交**逐行折叠路径**（与单表相关子查询同一机制：`_fold_and_run` + `_scalar_from_rows`），WHERE/GROUP BY/HAVING/投影/DISTINCT/ORDER BY/LIMIT 全在执行器侧。实测 `SELECT u.id, (SELECT COUNT(*) FROM orders x WHERE x.uid=u.id) AS cnt FROM users u JOIN orders o ON o.uid=u.id` → `[(1,2),(1,2),(2,1)]`；相关子查询按折叠值缓存（3 连接行只算 2 次）、非相关只算 1 次。
  - **顺带修掉两处静默错答**：`ON o.uid=u.id`（JOIN 表写在左边）原先连接键解析不到 → **静默退化成笛卡尔积**（3×3=9 行，现 3 行）；`ORDER BY` 引用限定列名（`ORDER BY o.amt DESC`）在计划路径被**静默忽略**（现正确排序）。

**SQL 实测**（同实例、真实执行器）：窗口 4 项、CTE 4 项、UNION 4 项、GROUP BY/DISTINCT 4 项、普通路径回归 4 项 —— **17/17 通过**。

- **事务原子性与崩溃恢复可用**：
  - **持久化根因**：磁盘页魔数用 `'<I'` 写入 `0x4C444250`（小端落盘为 `b'PBDL'`），而读回时比较的是 `b'LDBP'`，于是**任何页都读不回来**——重启后只能靠 WAL 重放，UPDATE/DELETE 直接丢；修正字节序后磁盘页可正常读回。
  - **WAL 隐式提交**：autocommit 语句使用合成高位事务号且 WAL 无 COMMIT 记录，恢复时被当成 loser 撤销（连基行一起丢）；现在 `txn_id == 0` 或 `>= 合成号下限` 一律按隐式提交重放，且写入时 flush 到 OS（log-before-data）。
  - **loser 前像重建**：撤销 loser 事务时不再把「带删除标记的旧版本」原样写回（那会让行头永久 `xmax != 0`、恢复后整表不可见），而是按原值重建 `xmax = 0` 的前像。
  - **真回滚**：`BEGIN; INSERT/UPDATE/DELETE; ROLLBACK` 三种场景回滚后表内容与事务前完全一致，且主键可再次插入（INSERT 路径原先根本不传事务号，已在 SQL 层修掉）。
  - **锁泄漏**：autocommit 语句改用合成事务号并在语句结束释放，`BEGIN; UPDATE` 同一主键不再 5 秒超时。
- **经 MySQL 协议的事务已真实生效（B，2026-10-19）**：此前 `net/server.py` **每条语句新建 session dict**，`BEGIN` 写入的 txn 下一条语句就丢 → `BEGIN;INSERT;ROLLBACK` 不回滚、未提交行其他连接可见、断连泄漏 MVCC active。现在改为**连接级 session**（`user/database/txn/autocommit/variables/user_vars`），事务句柄跨语句存活；断连自动回滚；`SET autocommit=0/1`、`SET NAMES`、`SET @var` 真实生效（`SELECT @@autocommit`/`SHOW VARIABLES` 可观察，OK/EOF 包带真实 autocommit/in-trans 状态位）；执行错误的 **SQLSTATE 按真实语义回传**（1146→42S02、1054→42S22、1062/1048→23000 等，不再一律 HY000）。见 `tests/test_wire_transaction.py`（9 例）。
- **声明式类型约束已真正生效（C，2026-10-19，行为变更）**：原先 `INT` 越界（`2147483648`）、`VARCHAR(3)` 超长（`'abcdef'`）、非法 `DATETIME`（`'not-a-date'`）全部静默入库。现按列声明校验并返回 MySQL 语义码：整数/`DECIMAL(p,s)` 越界 → **1264**、`VARCHAR(n)`/`CHAR(n)` 按字符数超长 → **1406**、非法日期/时间 → **1292**、无法转整数 → **1366**；PK / `NOT NULL` 列显式 NULL → **1048**（省略主键仍保留本引擎的自动取号能力）。见 `tests/test_type_constraints.py`（10 例）。
- **DDL 假成功两项已修（F）**：
  - **非整型主键定义期显式拒绝**：`CREATE TABLE vp (name VARCHAR(20) PRIMARY KEY)` 原先返回 OK，但该表之后**任何 INSERT 永久 1105**、SELECT 空集（主键 B+Tree 键按 uint32 落盘）。现在 `CREATE TABLE` 定义期直接 1064，报文说明「本引擎主键仅支持整数类型 INT/BIGINT/TINYINT/SMALLINT」并建议字符串列改 `UNIQUE`；整数主键不受影响。见上方「索引声明支持边界」。
  - **触发器坏体不再锁表**：`CREATE TRIGGER ... FOR EACH ROW SET @x=1` 原先返回 OK，之后该表任何写入都在触发路径解析失败 → 永久 1105，且旧版没有 `DROP TRIGGER` 可自救。现在 `TriggerManager.create()` 在落库前用 `PLParser` + 触发器小解释器同一套规则校验（含 BEGIN/END 配对、`SET NEW/OLD.列`、IF/END IF、普通 SQL、NEW/OLD 列存在性），坏体定义期 1064/1054 **且不写入**；同时新增 `DROP TRIGGER [IF EXISTS]` 与 `SHOW TRIGGERS`。合法触发器（BEFORE/AFTER）仍正常触发。见上方「触发器支持边界」。
- **复制状态语义修正（E2）**：SQL 线程重放失败时，错误号取不到（内置异常如 `KeyError`/`SyntaxError`）不再报 `Last_SQL_Errno=0`，改为按错误文本映射（缺表 1146 / 语法 1064 / 唯一冲突 1062 / 其它 1105），保证「有 `Last_SQL_Error` ⇒ `Last_SQL_Errno` 非 0」；复制能力本身仍为**实验性、不可用于生产**（见上方「复制支持边界」）。

**事务实测**（Lead 亲自复核，非子代理自述）：初始 3 行 → 三种 ROLLBACK 后仍是 `[(1,'v1'),(2,'v2'),(3,'v3')]` → 连续重开 2 次行数不变（无重复行累积）→ 子进程 autocommit 写入后 `os._exit(1)` 硬杀，重开后该行仍在（日志 `崩溃恢复 t: redo 1 条, undo 0 条`）。

**协议层事务实测**（P1-B，真实 `MySQLServer` + pymysql，`tests/test_wire_transaction.py`）：`BEGIN;INSERT;ROLLBACK` 后本连接与另一连接均 0 行；`BEGIN;INSERT;COMMIT` 后均 1 行；未提交行对另一连接不可见；`SET autocommit=0;INSERT;ROLLBACK` 回滚、`COMMIT` 提交；断连未提交自动回滚且 MVCC active 不增长。

### 🔑 索引声明支持边界（`CREATE TABLE` 内）

| 声明 | 状态 |
|---|---|
| 表级 `PRIMARY KEY (col)`（单列） | ✅ 真正生效：成为主键 B+Tree，重复插入 **1062** —— 但**仅限整数类主键列**（见下一行） |
| **主键列类型** | ✅ 仅 `INT` / `BIGINT` / `TINYINT` / `SMALLINT`（含 `UNSIGNED`）可作主键；`VARCHAR(n)` / `CHAR` / `TEXT` / `JSON` / `DECIMAL` 等非整型声明 `PRIMARY KEY` 一律**定义期显式 1064**（报文说明原因：主键 B+Tree 的键按 uint32 落盘，非整型主键会让该表所有写入永久失败；字符串列请用 `UNIQUE` 约束代替） |
| 表级 `UNIQUE KEY/INDEX [名字] (col...)`（单列/多列）、`CONSTRAINT <名字> UNIQUE` | ✅ 真正强制：INSERT / UPDATE 冲突 **1062**（报文含键名/值）；重启后仍强制；`SHOW CREATE TABLE` 往返；`SHOW INDEX` 列出但 `Index_type` 如实报 **`UNIQUE-CHECK`**（不谎称 BTREE） |
| 非唯一 `KEY/INDEX`、`FULLTEXT`、`SPATIAL`、`FOREIGN KEY`、复合 `PRIMARY KEY (a, b)`、前缀/表达式索引、`USING BTREE\|HASH` | ❌ **显式 1064**（说明不支持原因/替代做法），绝不静默丢弃、不建出假列 |
| 列级 `col ... UNIQUE` | ✅ 真正强制（单列）：按 MySQL 惯例注册成"索引名 = 列名"的唯一约束，INSERT / UPDATE 冲突 **1062**（如 `Duplicate entry '1' for key 'a'`）；多个 NULL 可共存；重启后仍强制；`SHOW CREATE TABLE` 内联 `UNIQUE` 并可往返；`SHOW INDEX` 列出且 `Index_type` 如实报 **`UNIQUE-CHECK`**（不谎称 BTREE） |

> 表级与列级 UNIQUE 都**没有独立索引结构**，唯一性由写入路径（INSERT / UPDATE 的写入前整体检查）强制，因此 `SHOW INDEX` 的 `Index_type` 写 `UNIQUE-CHECK`，`Cardinality` 是真实的不同非 NULL 键值个数；多列 UNIQUE 支持，NULL 不参与唯一性判定。代价是每次写入做 **O(n) 全表扫描**（不是 B+Tree 点查）；逻辑备份 / 恢复会原样带回两类 UNIQUE 约束，老 `_meta.json` 里只有 `unique=true` 而没有 `unique_indexes` 条目的表，打开时会自动推导出列级唯一索引并继续强制。

### 🪝 触发器支持边界（2026-10-19 F2 修复）

| 能力 | 状态 |
|---|---|
| `CREATE TRIGGER 名 BEFORE|AFTER INSERT|UPDATE|DELETE ON 表 FOR EACH ROW <体>` | ✅ 可建；**落库前**校验触发器体，坏体**定义期显式失败且不写入**（不再出现"建成功、之后整表不可写"的假成功） |
| 触发器体形态 | `SET NEW.列 = 表达式` / `SET OLD.列 = 表达式`（**仅 BEFORE** 可赋值）、`IF 条件 THEN ... [ELSE ...] END IF`、普通 SQL（`INSERT/UPDATE/DELETE/SELECT/CALL`）；`SET @变量`、`ELSEIF`、BEGIN/END 不配对、引用不存在的列 → 定义期显式报错（1064 / 1054），不再等到第一次写入才暴雷 |
| `DROP TRIGGER [IF EXISTS] 名` | ✅ 可用（不存在时 1360；`IF EXISTS` 当成功），坏触发器可以自助清理 |
| `SHOW TRIGGERS` | ✅ 结果集（`Trigger/Event/Table/Statement/Timing/Created/...`），反映真实持久化定义 |
| 触发时机与事件 | BEFORE/AFTER + INSERT/UPDATE/DELETE；合法触发器在写入路径真实 fire（BEFORE 可改 NEW 行），执行失败显式报错 |

> 触发器定义持久化在数据目录的 `_triggers.json`；`BEFORE INSERT/UPDATE` 里的 `SET NEW.列 = ...` 真的会改写即将写入的行。上述校验与运行期使用**同一套**语句切分/解释规则，避免"建的时候允许、跑的时候解析失败"。

### 🔁 复制支持边界（实验性、单向、**不可用于生产**；2026-10-19 E1 摘牌）

| 项 | 状态 / 结论 |
|---|---|
| 定位 | **实验性、单向（主 → 从）、不可用于生产**；与 OLAP/列存同一口径：不计入任何生产可用性声明 |
| 位点语义 | ⚠️ `SHOW MASTER STATUS` / `SHOW BINLOG EVENTS` 来自"查询时重扫各表当前 WAL"的派生视图，**位点会回退**：ALTER 表（WAL 退休重编号）后共享位点可能变小；**checkpoint / WAL 轮转后旧事件被截断 → 该表事件归零、从库收不到** |
| DDL | ❌ **不复制**（DDL 不进事件流；从库需预先建好同样的 schema） |
| 控制语句 | ❌ 无 `START SLAVE` / `STOP SLAVE` / `CHANGE MASTER` / `RESET SLAVE`；`MasterReplicator` / `SemiSync*` 无调用者（死代码） |
| 状态语义 | ✅ `SHOW SLAVE STATUS` 报**真实**运行状态/连接/位点/最近错误：未配置时 `Slave_IO_State='No replication configured'`、运行标志 `'No'`、连接与位点 NULL；有 `Last_SQL_Error` 时 `Last_SQL_Errno` **一定非 0**（缺表 1146 / 语法 1064 / 唯一冲突 1062 / 其它 1105） |
| 结论 | **不要把复制当生产特性使用**。若确实需要主从复制，必须先完成"真正 append-only binlog（独立文件、单调位点、含 DDL、按事务成组）+ START SLAVE/CHANGE MASTER"改造（见 [docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md](docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md) §E 的中期方案） |

**多进程模式实测**（`python -m lightdb.main --mode multi --workers 2` + 真实 pymysql）：

| 验证项 | 结果 |
|---|---|
| 端口监听 | ✅ `127.0.0.1:8892` 被 Master 绑定 |
| 建表 / 插入 / 查询 | ✅ `CREATE TABLE` → 2 条 `INSERT` → `SELECT` 返回 `((1,100),(2,200))` |
| 连接分散到多个 Worker | ✅ 3 个并发客户端分布到 3 个 Worker 进程（各 1-2 个连接） |
| 跨进程数据一致 | ✅ 3 个客户端各自 `COUNT(*)` 均为 3，彼此可见 |
| Master 被强杀 | ✅ Worker 3 秒内自行退出，无孤儿进程，端口释放 |
| 优雅停机 | ✅ 端口释放，共享内存文件零残留 |

**这一版多进程的边界（务必了解）**：存储引擎**只由 Master 进程持有并串行执行 SQL**，Worker 负责连接与协议处理。这样做的原因是：多进程各自打开同一份文件型存储会各自持有 BufferPool 页缓存与 BTree root，交叉写回必然损坏数据（原设计正是如此，且从未跑起来过）。因此**多进程带来的是连接/协议层的多核利用与崩溃隔离，不是查询执行的 CPU 加速** —— 文档中「8 核 92,000 QPS」的说法与此无关。另外两点：跨 Worker 的元数据（`SHOW TABLES` / `SHOW COLUMNS`）有最多 5 秒延迟（各 Worker 每 5 秒刷新一次快照）；`SHOW PROCESSLIST` / `KILL` 与事务状态是每个 Worker 进程各自一份，没有汇总到 Master。

**端到端实测**（真实 pymysql 客户端）：`CREATE TABLE` / `INSERT` / `SELECT *` / `COUNT(*)` / `UPDATE` / `DELETE` / `WHERE` 过滤全部返回正确结果。

**测试套件**（2026-10-19 P0/P1 修复后的基线）：**825 用例 = 777 通过 / 24 失败 / 23 跳过 / 1 xfailed**（本轮优化开始时是 150 通过 / 118 失败；P0/P1 修复新增 64 例）。24 条失败与修复前对照**逐条相同** —— 没有一条由本轮修复引入（逐条分诊见 [docs/analysis_2026-10-19/04_verification.md](docs/analysis_2026-10-19/04_verification.md)）：其中约 2 条来自沙箱/平台限制（写 `~/.lightdb_studio`、`dashboards/` 被拒），其余是**既有**缺陷/未达成目标：行格式 DECIMAL 编解码、`SET @@var` 不支持、半同步/GRANT 语法未实现、`test_cli_tool` 的 readline 移植性、OLAP 列存 GROUP BY/压缩率/加速比未达标、`parser_v3b4`/虚拟表/联邦/Data API/数字孪生等历史模块、诊断 CLI 的 emoji/GBK 控制台问题。注意 `test_ipc_latency`、`test_diagnose_cli` 等环境敏感用例会逐次抖动（本次通过），**判据是改动领域零失败 + 干净 HEAD 副本对照**。

**仍未修复**：查询执行仍是 Master 单线程（存储层无法安全地多进程共享，见上）；行值 `(a,b) IN (SELECT x,y ...)` 与行值比较 `(a,b) = (1,2)`（现**显式报错**并给出 `EXISTS` 改写建议）、HAVING 标量子查询引用外层聚合/非分组键、外层引用藏在函数或算式里、子查询里再嵌标量子查询作比较右值、WHERE 里的算术谓词、投影标量函数、`FROM (子查询)` 之后直接 `JOIN` —— 以上均**显式报错**（不静默错答）；**Cython 从未编译**（已实现纯 Python 回退，`status()['any_accelerated']` 为 False；本机无 Cython 且无任何 C 编译器）；存储侧 `Database.vacuum()` 只释放槽位、不做页内紧凑，且旧 `.dat` 行头布局不兼容（需重建库）。Linux/macOS 下 Worker 走 `SO_REUSEPORT` 自绑定路径，本机（Windows）无法验证。

> 更正记录：`tests/test_batch7.py::test_union_all` 曾长期失败（`assert 24 == 8`），一度被误判为「重复行 / 测试隔离」缺陷。实际是**该用例的期望值算错**：`setup()` 的城市列表 `['Beijing','Shanghai','Beijing','Beijing','Shenzhen']` 里有三处 'Beijing'，因此 `i % 5 ∈ {2,3,4}` 都是 Beijing（12 行），正确期望是 `12 × 2 = 24`；原断言只按 `i % 5 == 2` 算成 4 行。实测索引 20 条、堆 20 行、`_find_pks` 返回 12 个**互不重复**的 pk —— 存储层无缺陷。期望值现已改为从同一份 `cities` 列表推导，`test_batch7.py` 9/9 全绿。

---

## 🎯 概述

LightDB Studio 是为 **LightDB / MySQL** 打造的图形化管理工具，覆盖从**日常查询**到**企业协作**的完整场景。

支持 **桌面版** 和 **Web 版**，具备 Navicat 的核心功能，并额外提供 AI 助手、数据血缘、SQL 审查、性能分析等企业级能力。

---

## ✨ 核心特性

### 📊 数据管理
- 数据网格编辑（内联修改、批量提交）
- 表设计器（可视化建表 / 改表）
- CSV/Excel/JSON 导入导出
- 大文本 / 图片 / Hex 查看器
- 复制粘贴（Excel 格式兼容）

### 🔍 查询能力
- SQL 编辑器（语法高亮 + 自动补全）
- 多标签查询
- 查询历史（搜索 / 收藏）
- SQL 片段库（18+ 内置模板）
- 结果集表格（排序 / 过滤 / NULL 高亮）

### 📈 可视化
- 数据图表（10 种类型：柱状/折线/饼图/雷达/热力图/...）
- ER 图（自动外键检测）
- 仪表盘（指标卡 + 图表卡 + 自动刷新）
- 数据血缘图

### 🚀 高级功能
- **多环境管理**（dev/staging/prod 一键切换 + 生产保护）
- **SQL 审查工作流**（提交-审核-执行）
- **数据库对比**（差异 SQL 生成 + 一键同步）
- **快照 / 克隆**（备份 + 一键复制）
- **SQL 格式化**（Ctrl+Shift+F）
- **数据脱敏**（8 种策略）

### 🤖 AI 助手
- 自然语言转 SQL
- SQL 解释 / 优化建议
- 索引推荐
- 支持 OpenAI / Claude / Ollama（本地）

### 👥 团队协作
- 会话共享 + 评论注解
- @提及 + 回复线程
- 团队配置库
- 云同步（加密导出/导入）

### 📊 监控与诊断
- SQL 性能分析（EXPLAIN + 评分）
- 数据库健康度评分（4 维度）
- 实时性能监控（QPS 曲线）
- 慢查询分析
- 数据质量检查

### 🔧 运维工具
- 任务调度（cron 风格 + 失败重试）
- 报表系统（Excel/HTML/CSV 导出）
- 工作流引擎（多步 SQL 编排）
- 审计日志查看
- MySQL / SQLite 迁移向导

### 🌐 Web 版本
- FastAPI 后端
- REST API（JWT 认证）
- 浏览器访问
- Prometheus 指标

---

## 🚀 快速开始

### 安装

```bash
# 桌面版（完整）
pip install lightdb[all]

# 仅 CLI / 服务
pip install lightdb

# 从源码开发
git clone https://github.com/lightdb/studio.git
cd studio
pip install -e ".[all]"