# LightDB Studio

> 图形化数据库管理工具 · Navicat 的开源替代 · 纯 Python 实现

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)]()

---

## ⚠️ 当前实测状态（2026-10）

> 本节是 2026-10 代码审计的**审计时**实测结论，**优先于下方所有功能与性能描述**。完整报告见 [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md)，死代码清单见 [docs/DEAD_CODE.md](docs/DEAD_CODE.md)。其中若干 P0 缺陷已在同一次会话中修复，见本节末尾「本次会话已修复」。

- **服务端单进程模式**（`python -m lightdb.main --mode single`）可以监听端口并发出标准 MySQL v10 握手包（server version `5.7.30-LightDB`），但**认证校验存在缺陷，没有任何客户端能登录成功**：`lightdb/net/mysql_protocol.py:293` 的长度守卫排在空密码分支之前，`:304` 的比较少做一次 SHA1 —— 空密码与非空密码实测均被拒绝。
- **建表必然失败**：`lightdb/storage/buffer_pool.py:15` 用 `open(path, 'r+b')` 打开尚不存在的 `.dat` 文件，抛 `FileNotFoundError`。
- **所有执行错误被响应为 OK 包**：`lightdb/sql/executor.py:68` 把异常转成错误字典而不抛出，`lightdb/net/server.py:391-437` 的 `_send_execution_result()` 从不检查错误，DDL/DML 无条件回 OK 包 —— 客户端完全无法感知失败。
- **默认（多进程）模式无法启动**：`python -m lightdb.main` 在 8 核下自动走 multi，实测输出「Worker 池启动失败」后退出，端口从未被监听；`--check` 还会因裸 emoji `print` 在 cp936 控制台抛 `UnicodeEncodeError`（`lightdb/main.py:156`、`:162`）。
- **桌面端在 Python ≤ 3.11 无法启动**：`lightdb_studio/core/reports.py:172` 的 f-string 含反斜杠，属于 Python 3.12 才允许的语法。
- **Web 版可运行**（17 个 API 端点，JWT 校验有效），但存在硬编码签名密钥与默认口令等问题，详见审计报告。
- **文档中的性能数字未经仓库自身工具验证**：仓库内没有任何基准产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录不存在；`tests/regression_v4/test_performance_v4.py:13-18` 中的 12000 / 80000 QPS 目标**从未被任何测试断言**。这些数字应视为设计目标，而非实测结果。
- 一次完整测试运行的结果为 **268 个测试中 118 失败 / 150 通过**。

### ✅ 本次会话已修复（2026-10，含实测验证）

- **建表可用**：[`lightdb/storage/buffer_pool.py`](lightdb/storage/buffer_pool.py) 在数据文件缺失时先创建，不再抛 `FileNotFoundError`。
- **登录可用**：[`lightdb/net/mysql_protocol.py`](lightdb/net/mysql_protocol.py) 修正 `mysql_native_password` 校验（空密码分支前置、补上最后一次 SHA1），真实 pymysql 客户端实测登录成功。
- **错误不再伪装成功**：[`lightdb/net/server.py`](lightdb/net/server.py) 的 `_send_execution_result()` 现在会把 `{'type': 'error'}` 结果翻译为 ERR 包；实测查询不存在的表返回 `1146`。
- **客户端兼容**：新增 `SET NAMES` / `SET AUTOCOMMIT` 等会话语句确认，否则标准客户端在连接协商阶段就报 1064。
- **启动不再因 emoji 崩溃**：[`lightdb/main.py`](lightdb/main.py)、[`lightdb/main_v4.py`](lightdb/main_v4.py) 增加控制台编码保护并把裸 emoji 换成 ASCII 标记，`--check` 现已正常退出。
- **桌面端可启动**：[`lightdb_studio/core/reports.py`](lightdb_studio/core/reports.py) 的 f-string 改写为 Python 3.8+ 兼容；另修 [`agent_chat.py`](lightdb_studio/ui/agent_chat.py) 缺失的 `QLineEdit` 导入、[`sql_editor.py`](lightdb_studio/ui/sql_editor.py) 的 `QPlainTextEdit.ExtraSelection`、[`main_window.py`](lightdb_studio/ui/main_window.py) 的状态栏构建顺序。offscreen 实测 `MainWindow` 可构造（6 个菜单）。
- **打包与容器**：[`pyproject.toml`](pyproject.toml) 现在会打包 `lightdb` 并补上 `lightdb-server` 入口，Dockerfile 不再 COPY 不存在的 `main.py`。
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

**SQL 实测**（同实例、真实执行器）：窗口 4 项、CTE 4 项、UNION 4 项、GROUP BY/DISTINCT 4 项、普通路径回归 4 项 —— **17/17 通过**。

- **事务原子性与崩溃恢复可用**：
  - **持久化根因**：磁盘页魔数用 `'<I'` 写入 `0x4C444250`（小端落盘为 `b'PBDL'`），而读回时比较的是 `b'LDBP'`，于是**任何页都读不回来**——重启后只能靠 WAL 重放，UPDATE/DELETE 直接丢；修正字节序后磁盘页可正常读回。
  - **WAL 隐式提交**：autocommit 语句使用合成高位事务号且 WAL 无 COMMIT 记录，恢复时被当成 loser 撤销（连基行一起丢）；现在 `txn_id == 0` 或 `>= 合成号下限` 一律按隐式提交重放，且写入时 flush 到 OS（log-before-data）。
  - **loser 前像重建**：撤销 loser 事务时不再把「带删除标记的旧版本」原样写回（那会让行头永久 `xmax != 0`、恢复后整表不可见），而是按原值重建 `xmax = 0` 的前像。
  - **真回滚**：`BEGIN; INSERT/UPDATE/DELETE; ROLLBACK` 三种场景回滚后表内容与事务前完全一致，且主键可再次插入（INSERT 路径原先根本不传事务号，已在 SQL 层修掉）。
  - **锁泄漏**：autocommit 语句改用合成事务号并在语句结束释放，`BEGIN; UPDATE` 同一主键不再 5 秒超时。

**事务实测**（Lead 亲自复核，非子代理自述）：初始 3 行 → 三种 ROLLBACK 后仍是 `[(1,'v1'),(2,'v2'),(3,'v3')]` → 连续重开 2 次行数不变（无重复行累积）→ 子进程 autocommit 写入后 `os._exit(1)` 硬杀，重开后该行仍在（日志 `崩溃恢复 t: redo 1 条, undo 0 条`）。

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

**测试套件**：**244 通过 / 79 失败 / 1 跳过**（本轮优化开始时是 150 通过 / 118 失败）。剩余失败中约 28 项来自沙箱/平台限制（禁止写工作区外文件、禁止多进程命名管道、Windows 不允许删除已打开的文件），其余是真实待修项：WHERE 里的 `NOT (...)` 与 `IS NULL`、OLAP 向量化路径仍受 COUNT 计 NULL/AVG 未实现限制、并行执行无加速、Cython 从未编译等。

**仍未修复**：查询执行仍是 Master 单线程（存储层无法安全地多进程共享，见上）；WHERE 里的 `NOT (...)` 与 `IS NULL`、`FROM (子查询)` 之后直接 `JOIN`、子查询内层 SELECT 的算术投影、非默认 `RANGE`/`GROUPS` 帧、相关子查询里含 JOIN/GROUP BY 或引用外层列做聚合的形态，均**显式报错**（不静默错答）；**Cython 从未编译**（已实现纯 Python 回退，`status()['any_accelerated']` 为 False；本机无 Cython 且无任何 C 编译器）；事务侧 UPDATE/DELETE 的旧版本不落盘（重启后老快照查不到）、墓碑无 vacuum。Linux/macOS 下 Worker 走 `SO_REUSEPORT` 自绑定路径，本机（Windows）无法验证。

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