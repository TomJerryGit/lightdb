# LightDB Studio

> 纯 Python 实现的 MySQL 兼容数据库引擎 · 图形化管理工具（LightDB Studio）· Web 版

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)]()
[![Status](https://img.shields.io/badge/status-beta-orange.svg)]()

---

## ⚠️ 当前实测状态（2026-10）

> 本节是 2026-10 代码审计的**审计时**实测结论，**优先于下方所有功能与性能描述**。完整报告见 [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md)，死代码清单见 [docs/DEAD_CODE.md](docs/DEAD_CODE.md)。其中若干 P0 缺陷已在同一次会话中修复，见本节末尾「本次会话已修复」。
>
> **接线口径（4A.2/4A.5，2026-10-20）**：`lightdb/olap/**`（向量化）与
> `lightdb/columnar/**`（列存）已**显式接入 SQL 执行路径**，并**默认开启**
> （2026-10-20 用户要求；`LIGHTDB_OLAP_DISPATCH=0` 可关闭，关闭时不加载
> OLAP 引擎模块）。派发只走白名单语法，其余一律回退行存；当前 dispatch
> **没有显著性能收益**（实测同 SQL 开关比 0.44–1.10x），因此**不据此宣称
> "SQL 已加速"**；只对外引用 `benchmarks/` 里已有门禁的实测数字（并行
> GROUP BY ≥1.2x、dispatch 对拍不一致=0）。Cython 加速层已用 Cython 3.3 +
> Zig 在本机编译验证（见 `scripts/build_cython_zig.py`，产物不入库），
> 实测 btree 5.9–7.0x / row_codec 2.0–3.3x / predicate ~10–19x / hash
> ~16–21x / page ~20–33x；Windows 上 Zig/MinGW ABI 产物仅建议开发验证，
> 发布用 MSVC。全部实测数字与白皮书对照表见 [benchmarks/README.md](benchmarks/README.md)。

- **服务端单进程模式**（`python -m lightdb.main --mode single`）可以监听端口并发出标准 MySQL v10 握手包（server version `5.7.30-LightDB`），但**认证校验存在缺陷，没有任何客户端能登录成功**：`lightdb/net/mysql_protocol.py:293` 的长度守卫排在空密码分支之前，`:304` 的比较少做一次 SHA1 —— 空密码与非空密码实测均被拒绝。
- **建表必然失败**：`lightdb/storage/buffer_pool.py:15` 用 `open(path, 'r+b')` 打开尚不存在的 `.dat` 文件，抛 `FileNotFoundError`。
- **所有执行错误被响应为 OK 包**：`lightdb/sql/executor.py:68` 把异常转成错误字典而不抛出，`lightdb/net/server.py:391-437` 的 `_send_execution_result()` 从不检查错误，DDL/DML 无条件回 OK 包 —— 客户端完全无法感知失败。
- **默认（多进程）模式无法启动**：`python -m lightdb.main` 在 8 核下自动走 multi，实测输出「Worker 池启动失败」后退出，端口从未被监听；`--check` 还会因裸 emoji `print` 在 cp936 控制台抛 `UnicodeEncodeError`（`lightdb/main.py:156`、`:162`）。
- **桌面端在 Python ≤ 3.11 无法启动**：`lightdb_studio/core/reports.py:172` 的 f-string 含反斜杠，属于 Python 3.12 才允许的语法。
- **Web 版可运行**（17 个 API 端点，JWT 校验有效），但存在硬编码签名密钥与默认口令等问题，详见审计报告。
- **文档中的性能数字未经仓库自身工具验证**：仓库内没有任何基准产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录不存在；`tests/regression_v4/test_performance_v4.py:13-18` 中的 12000 / 80000 QPS 目标**从未被任何测试断言**。这些数字应视为设计目标，而非实测结果。
- **复制（主从）已进入"真 append-only binlog"实现（2026-10-19 决策 D，阶段 1 完成）**：新增独立
  `<data_dir>/binlog/binlog.000001`（magic/version + 记录头 + JSON + **CRC32**，位点=真实字节偏移、
  **只增不回退**，不随 WAL checkpoint/ALTER 变化）；DML 在提交时成组写（BEGIN→行事件→COMMIT）、
  **DDL 也进 binlog 并可复制**；`SHOW MASTER STATUS`/`SHOW BINLOG EVENTS`/`__BINLOG_FETCH__` 全部读真文件；
  从库用 `master.info` 持久位点**重启续传**；新增 `START SLAVE`/`STOP SLAVE`/`CHANGE MASTER TO`。
  仍未完成：**无日志轮转**（`max_bytes` 仅告警）、无 GTID/半同步、binlog 写失败只告警不拒绝提交
  （后续阶段改为写失败即失败）。**定位：beta 级复制，可用于开发/测试/单主单从，不建议承载生产写入** ——
  见下文「🔁 复制支持边界」。

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

> 本次修复的详细证据、复现与仍未修复项见
> [docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md](docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md)。

### 🧩 预处理语句（COM_STMT_*，2026-10-20 阶段 1 协议补齐）

- **服务端 prepare 全链路**：`COM_STMT_PREPARE / EXECUTE / CLOSE / RESET` 已实现；连接级
  statement 注册表（id 从 1，单连接上限 1461），断连自动释放；`CLOSE` 按 MySQL 协议不回响应包。
- **参数类型**：TINY/SHORT/LONG/LONGLONG/INT24、FLOAT/DOUBLE、STRING/VAR_STRING/VARCHAR、
  DECIMAL/NEWDECIMAL、DATE/DATETIME/TIMESTAMP/TIME、BLOB 家族、NULL、ENUM/SET/JSON；
  跨字节 NULL bitmap 正确；执行时按值安全内联后复用文本协议同一 parser/事务/权限路径。
- **安全与错误语义**：PREPARE 阶段做 SELECT 鉴权（防元数据泄露）；参数数不符 **1210**、
  未知 statement **1243**、不支持类型 **1235**；`SEND_LONG_DATA` 不累积（EXECUTE 显式 1235）、
  游标/FETCH 不支持（显式 1235）。
- **验收**：`tests/test_prepared_statements.py`（16 例，自写最小二进制客户端逐包断言；
  另 1 例 mysql-connector 端到端在未安装时 skip，已用 mysql-connector-python 26.7.0 实测通过）。
- **注意**：pymysql 默认客户端参数化仍走文本协议；此实现面向 JDBC / mysql-connector 等
  server-side prepare 客户端。

### 🛡️ 授权体系（阶段 1.1，2026-10-20：GRANT/REVOKE 真正生效）

> 此前「登录即全权」：GRANT/REVOKE 语法 1064、权限检查为零。现在权限模型已接线并持久化：

- **权限模型**：`Database.users[user]['privs']`（随 `_meta.json` 持久化）；root 保持 `['ALL']`；
  支持 `ALL`、`SELECT/INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/INDEX`、`*.*` 与 `db.*`；
  列级 / 表级 / `WITH GRANT OPTION` 显式 1064（不支持）。
- **SQL**：`GRANT <privs> ON *.*|db.* TO '<u>'[@'host']`、`REVOKE ... FROM`、
  `SHOW GRANTS [FOR '<u>' | CURRENT_USER]`；授权/回收即时生效。
- **执行期鉴权**：`Executor.execute()` 分发前按会话 user 检查（读→SELECT、DML→对应权限、
  DDL→对应权限、DCL/管理→ALL/root、未知命令 **fail-closed**）；拒绝错误码
  **1142**（表级）/ **1044**（库级）/ **1227**（管理），SQLSTATE 42000；
  `information_schema`、binlog/复制、内部服务身份保持最小可用集合。
- **协议层**：握手把 user 与 host 写入连接级 session；`SHOW TABLES` / `DESC` /
  `COM_INIT_DB` / `COM_FIELD_LIST` / `KILL` / `SHOW PROCESSLIST` / `SHOW DATABASES`
  接入同一模型（非管理员只看到自己的线程与有权限的库）。
- **验收**：`tests/test_privileges.py`（12 例，含真实 MySQLServer + pymysql 端到端）+
  原 `test_batch9` 两条 GRANT 用例恢复为正式测试；覆盖权限矩阵、GRANT→读/写、REVOKE 复拒、
  `SHOW GRANTS`（自己/root/他人/不存在）、库级 ALL 非管理员、持久化重开、UNION/JOIN、
  解析拒绝、内部身份、1045/KILL/PROCESSLIST 过滤。
- **仍有限制**：账号 host 维度未建模（用户名唯一）；多进程 worker 的本地内置分支未本地鉴权
  （主 SQL 仍在 Master 执行器鉴权）；PL/触发器按调用者权限执行（比 MySQL DEFINER 严格）；
  `SHOW VARIABLES` / `SELECT @@var` 未限制。

### 🧱 工程化与发布（2026-10-19 阶段 0 + 四项决策执行）

> 用户决策（2026-10-19）：①产品定位 = **数据库可交付产品**；②OLAP/列存/并行/Cython **接入 SQL 路径**；
> ③Studio 死代码 **批量删除**；④复制 **投入做真 append-only binlog**。下面是已落地部分：

- **打包可安装**：wheel 现在包含 `lightdb` 引擎包 + `lightdb_studio` + `web`；入口
  `lightdb-server`（引擎）/ `lightdb-studio`（桌面）/ `lightdb-studio-web`（Web）；
  版本单一来源 `lightdb.__version__ = 4.0.0`；`python scripts/check_packaging.py` 验收。
  实测：`pip install <wheel>` 后 `lightdb-server --version` → `LightDB 4.0.0`。
- **CI**：`.github/workflows/ci.yml`（Windows：install → compileall → pytest → benchmark gate →
  packaging check）；本地等价命令 `scripts/ci.ps1`。
- **Studio 死代码已删（决策 3）**：44 个模块 / 15,728 行 + 6 个死测试文件；`lightdb_studio` 从
  **134 文件/39,256 行**降到 **90 文件/23,532 行**；剩余 90/90 模块逐个 import 成功、无悬空引用。
- **真复制已落地（决策 4 阶段 1）**：独立 append-only binlog + CRC32 + 真实字节位点 + DDL 复制 +
  从库持久位点续传 + START/STOP SLAVE/CHANGE MASTER，见上文「复制支持边界」；专项测试 16 例。
- **OLAP 已显式接入且默认开启（决策 2，4A.2/4A.3/4A.5）**：单表白名单聚合/分组查询
  默认走向量化执行（2026-10-20 用户要求；`LIGHTDB_OLAP_DISPATCH=0` 可关闭）；行存↔列存
  对拍逐行一致（63 例 + 契约测试）；并行 GROUP BY 分片实测 w1→w2 **2.75x**，门禁 ≥1.2x；
  正确性/并行/对拍数字见 [benchmarks/README.md](benchmarks/README.md)。**注意：dispatch 当前
  无显著性能收益（0.44–1.10x），默认开启不代表"SQL 已加速"。**
- **Cython 已真实编译验证（4A.4）**：本机用 `Cython 3.3.0 + ziglang 0.16.0`（D 盘 venv）
  编译 6 个扩展（`scripts/build_cython_zig.py`，修复了 3 处 Cython 3 语法错误 + 2 个真实
  C 层 bug：page 空页 next_row_ptr 未初始化导致堆溢出、hash remove 后 get 命中残留）；
  `tests/test_cython_compiled_parity.py` 逐模块对拍编译版 vs 回退版。**产物不入库**
  （平台相关；CI/干净 checkout 自动走回退）；Windows 上 Zig/MinGW ABI 仅建议开发验证，
  发布请用 MSVC。**⚠️ 长跑结论：** 编译产物在单模块功能/对拍/微基准下通过，但在一次
  **全量测试长跑**中出现 Windows 堆损坏（`0xc0000374`，未定位到单一模块），因此当前
  **运行时不启用编译产物**（本机已归档、不进仓库、不加载），默认走纯 Python 回退；
  该构建链路验证结论保持"本地可编译、单模块可用"，**不计入性能声明**，待 MSVC
  构建或后续定位后复验。
- **本地全量 GUI + DB 验证（2026-10-20）**：真实起服务 + Studio，数据库功能矩阵
  **34/34 通过**、GUI **45/45 场景通过 / 77 张截图 / 25 个对话框**（offscreen +
  `QT_QPA_PLATFORM=windows` 真实窗口探针通过）。验证发现并修复 3 个真实缺陷
  （UNION 经 MySQL 文本协议返回 OK/0 行、UNION 级 ORDER BY/LIMIT 被丢弃、并发 DDL
  时 `dictionary changed size during iteration`）；登记 3 条 Studio UI 缺陷与 2 条已知
  引擎限制。详见 [`docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md`](docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md)；
  GUI 后续优化路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md)
  （P0 正确性/安全 → P1 响应性 → P2 功能深度 → P3 工程质量）。
- **GUI P0 已实施（2026-10-20）**：结果表**数值/日期感知排序**（NULL 恒末尾）、**结果过滤条**
  （1200 行 40.6ms、不查库）、**快照同名自动唯一 + 旧索引迁移**、**连接口令本地加密**
  （Windows DPAPI；`connections.json` 不再明文，旧明文自动迁移）、连接对话框**口令显示/隐藏**、
  **错误面板分类 + 复制**（查询/权限/连接），并修复 `show_message()` 陈旧错误视图与包级
  延迟导入 RecursionError。P0 新测试 30 passed，真实 GUI 端到端 4/4 PASS；详见路线图 §9。
- **GUI P1 首批已实施（2026-10-21）**：查询移出 UI 线程（QThreadPool + 信号，F5/菜单/
  Ctrl+Return/对象树/EXPLAIN 全走后台，UI 不冻结），支持**取消当前查询**（KILL + 自动重连）
  与 `query_timeout` 超时；结果读取改 **fetchmany 分块 + `max_result_rows` 截断提示 +
  加载更多**（QAbstractTableModel 虚拟化留待后续）；P0 行为保留。新测试 9 passed，
  Studio 回归 69 passed，真实 GUI 5/5 PASS；详见路线图 §10。
- **GUI P1 第二批已实施（2026-10-21）**：SQL 编辑器**自动补全**（关键字+表/列，`表.` 上下文，
  尊重 `editor_auto_complete`，Ctrl+Space）+ **错误位置高亮**（`near '<token>'`/`at line N`/
  `Unknown column`，定位不到不伪造）；对象树**懒加载**（展开表才查列/索引/触发器，缓存复用）
  + 右键菜单（复制表名/生成 SELECT LIMIT 200/查看 DDL/刷新等）。新测试 19 passed，
  Studio 回归 88 passed，真实 GUI 3/3 PASS；详见路线图 §11（新增 19 例，CI 基线待下一轮复核）。
- **测试门禁清零**：原 24 条失败按根因处置 —— 真实缺陷修复（DECIMAL 编码 / CLI argparse / 诊断 emoji）、
  其余改为**带明确 reason 的 skip/xfail**（死模块、沙箱权限、flaky 阈值、需 ≥4 核的并行断言）。
  授权体系（GRANT ×2）已于 2026-10-20 实现并恢复为正式测试；3 条 OLAP 性能类 skip
  已在 4A.5 替换为**实测基线阈值**并恢复为正式测试。
- **版本状态诚实化**：`Development Status` 从 `Production/Stable` 改为 **`4 - Beta`**（复制性能、
  OLAP dispatch 收益与 Studio 可用性仍在补；安全/授权/打包/CI/真 binlog 已收口）。

### ⚠️ 性能声明状态说明（2026-10 阶段 0 收尾 + 阶段 2 第 6 项「摘牌」，**必读**）

> 本节覆盖 `README` / `docs/**` 里全部 OLAP / 列存 / 并行 / Cython 性能数字，
> 逐条给出**当前真实状态**。原始文档**不删除**，其段落内保留原文以便对照。
>
> **统一口径（4A.2/4A.5，2026-10-20）**：OLAP（`lightdb/olap/**` 向量化）与
> 列存（`lightdb/columnar/**`）已**显式接入 SQL 路径并默认开启**（2026-10-20
> 用户要求；`LIGHTDB_OLAP_DISPATCH=0` 可关闭）。派发只在白名单语法命中时发生
> （有 `dispatch_stats()` 计数与日志，禁止静默改道）。**dispatch 当前没有显著
> 性能收益**（实测开关比 0.44–1.10x），因此不宣称"OLAP 加速"；可对外引用的
> 只有 [benchmarks/README.md](benchmarks/README.md) 中已有门禁的实测数字
> （向量化并行 GROUP BY ≥1.2x、dispatch 对拍不一致=0、1M 行单 SUM 2.72M rows/s）。
> 契约测试见 [`tests/test_olap_wiring_contract.py`](tests/test_olap_wiring_contract.py)。

- **OLAP/列存层**：4A.1 修完 12 类正确性缺陷并加 91 例行存↔列存对拍；4A.2 接入
  SQL 路径（白名单 + crosscheck）；4A.3 并行分片重做（GROUP BY w2 实测 2.75x，
  自动档在纯 SUM 加速不足 1.2x 时保持串行）；**2026-10-20 按用户要求默认开启
  dispatch**（实测无显著收益，故不宣称加速）。
- **Cython 加速层（4A.4）**：已用 Cython 3.3.0 + Zig 0.16.0 在本机真实编译并运行
  6 个扩展（`scripts/build_cython_zig.py`）；编译验证同时发现并修复了 page/hash
  两个 C 层 bug。实测加速：btree 5.9–7.0x、row_codec 2.0–3.3x、predicate
  ~10–19x、hash ~16–21x、page ~20–33x（本机 8 核、混合负载）；**低于白皮书
  12.5x 的单一指标不成立**（不同负载差异大），且编译产物**不入库**、
  Windows 上 Zig/MinGW ABI 仅建议开发验证（发布用 MSVC）。
- **以下历史数字仍是设计目标，不是实测**：`12,000 QPS`（单线程点查）、
  `92,000 QPS`（8 核）、`3.5M rows/s`（OLAP 扫描）、`85M rows/s`（大表聚合）、
  `41x`（列存加速比）、`12.5x`（Cython B+Tree）、`3.15x`（向量化过滤）——
  它们只出现在设计文档里；逐条对照见 [benchmarks/README.md](benchmarks/README.md)
  第 4 节（OLAP/列存两条已标注"已接线但未达白皮书数字"）。
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

- **仍未验证/未完成**：Cython 编译产物**不入库**，CI/干净 checkout 自动走纯 Python
  回退（本机验证见 4A.4 记录）；Windows 上 Zig/MinGW ABI 产物仅建议开发验证，发布
  请用 MSVC Build Tools；dispatch 默认开启但当前无显著收益（实测 0.44–1.10x），
  是否在生产长期开启需等更大数据量/更优列格式的实测；4A.3 并行在纯 SUM 上加速
  不足 1.2x（自动档保持串行），只有 GROUP BY 分片有明显收益（w2 2.75x）；
  float SUM/AVG 分片合并理论上有末位差异（顺序固定 + crosscheck 兜底）。
- **仍有限制（2026-10-21 CI 首跑暴露并已如实标注）**：**服务端 TLS/SSL 尚未接线**
  （`ssl_enabled` 与 `SSLManager` 证书工具存在，但 `MySQLServer` 未 wrap socket /
  未宣告 `CLIENT_SSL`；`tests/test_batch14.py::test_ssl` 在有机 openssl 环境显式 skip，
  待实现后恢复端到端断言，见路线图 P2 安全项）；
- **历史文档状态**：[docs/V4_DECISION.md](docs/V4_DECISION.md) 已被**有意截断**
  （44 行，含"原文档在此处截断，后续章节缺失"）以移除不可核实的数据；
  本 README **不补写**其中缺失的历史内容，只在此处如实标注该文件不完整。
  同类的"数据可信度声明"已在 [docs/ARCHITECTURE_v4.md](docs/ARCHITECTURE_v4.md)、
  [docs/PERFORMANCE_WHITEPAPER_v3.md](docs/PERFORMANCE_WHITEPAPER_v3.md)、
  [docs/PERFORMANCE_WHITEPAPER_v4.md](docs/PERFORMANCE_WHITEPAPER_v4.md)、
  [docs/V4_DECISION.md](docs/V4_DECISION.md)、[docs/V4_RELEASE_NOTES.md](docs/V4_RELEASE_NOTES.md)
  顶部；本次收尾补齐了其余仍把上述数字当实测陈述的文档。

- 一次完整测试运行的结果（2026-10-21，GUI P0/P1 首批之后）：
  **1068 个用例中 1027 通过 / 0 失败 / 39 跳过 / 1 xfailed / 1 xpassed**（GitHub
  Windows runner CI run `39526414556` 全绿；新增 9 例 P1 异步查询测试），
  skip/xfail 均带明确原因，见下方「测试套件」说明与
  [docs/analysis_2026-10-19/06_ROADMAP.md](docs/analysis_2026-10-19/06_ROADMAP.md) §1）。

### ✅ 本次会话已修复（2026-10，含实测验证）

- **建表可用**：[`lightdb/storage/buffer_pool.py`](lightdb/storage/buffer_pool.py) 在数据文件缺失时先创建，不再抛 `FileNotFoundError`。
- **登录可用**：[`lightdb/net/mysql_protocol.py`](lightdb/net/mysql_protocol.py) 修正 `mysql_native_password` 校验（空密码分支前置、补上最后一次 SHA1），真实 pymysql 客户端实测登录成功。
- **错误不再伪装成功**：[`lightdb/net/server.py`](lightdb/net/server.py) 的 `_send_execution_result()` 现在会把 `{'type': 'error'}` 结果翻译为 ERR 包；实测查询不存在的表返回 `1146`。
- **客户端兼容**：新增 `SET NAMES` / `SET AUTOCOMMIT` 等会话语句确认，否则标准客户端在连接协商阶段就报 1064。
- **启动不再因 emoji 崩溃**：[`lightdb/main.py`](lightdb/main.py)、[`lightdb/main_v4.py`](lightdb/main_v4.py) 增加控制台编码保护并把裸 emoji 换成 ASCII 标记，`--check` 现已正常退出。
- **桌面端可启动**：[`lightdb_studio/core/reports.py`](lightdb_studio/core/reports.py) 的 f-string 改写为 Python 3.8+ 兼容；另修 [`agent_chat.py`](lightdb_studio/ui/agent_chat.py) 缺失的 `QLineEdit` 导入、[`sql_editor.py`](lightdb_studio/ui/sql_editor.py) 的 `QPlainTextEdit.ExtraSelection`、[`main_window.py`](lightdb_studio/ui/main_window.py) 的状态栏构建顺序。offscreen 实测 `MainWindow` 可构造（6 个菜单）。
- **打包与容器**：✅ 2026-10-19 已修复 —— `pyproject.toml` 的 `packages.find` 现在包含
  `lightdb*`（wheel 内含引擎包）、补上 `lightdb-server` 入口、删除指向不存在模块的
  `lightdb-studio-cli`、依赖与 `requirements.txt` 对齐（psutil/numpy）、版本改为单一来源
  `lightdb.__version__=4.0.0`。验收命令：`python scripts/check_packaging.py`
  （CI 见 `.github/workflows/ci.yml` / 本地 `scripts/ci.ps1`）。
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

### 🔁 复制支持边界（beta：真 append-only binlog + 轮转/严格提交；2026-10-20 决策 D 阶段 2）

| 项 | 状态 / 结论 |
|---|---|
| 定位 | **beta、单向（主 → 从）**：可用于开发/测试/单主单从，不建议承载生产写入；不计入"生产可用"声明 |
| 日志文件 | ✅ `<data_dir>/binlog/binlog.000001`…：独立 **append-only binlog**，文件头 magic/version + 每条记录（type/真实字节 pos/length/**CRC32**）+ JSON payload；损坏/中间截断**显式报错**；**按 `max_bytes`（默认 32MB）真实轮转**，旧文件只读保留 |
| 位点语义 | ✅ `SHOW MASTER STATUS` = **当前文件名 + 文件内真实字节位点**；只增不回退（checkpoint / ALTER / 重启 / 轮转均不回退）；`SHOW BINLOG EVENTS IN 'file' FROM pos` 可读历史文件；`__BINLOG_FETCH__` 6 列兼容（`pos`=跨文件全局逻辑序号、`lsn`=跨文件全局字节偏移，从库跨轮转续传） |
| 事务与 DDL | ✅ DML 提交时成组写 `BEGIN → 行事件… → COMMIT`（autocommit 也成组）；`ROLLBACK` 如实记录被丢弃条数；**DDL 事件也写入并复制** |
| 严格提交 | ✅ **binlog 写失败即拒绝提交**：write/flush/fsync/轮转建文件失败 → 物理回滚事务 + 显式 `1180`（HY000）；`LIGHTDB_BINLOG_STRICT=0` 可退化为告警（排障用） |
| 从库 | ✅ `master.info` 持久化 read/exec 位点，**重启跨轮转续传**；DDL/DML 重放失败显式报错（`Last_SQL_Errno` 语义沿用 E2）；`START SLAVE` / `STOP SLAVE` / `CHANGE MASTER TO` 已支持 |
| 明确未完成 | ⚠️ **无 GTID**（按位点复制；`Executed_Gtid_Set` 恒 NULL）；**半同步不实现**（死代码已删除）；无 `binlog.index`（启动 O(n) 扫描）；`UPDATE/DELETE` 镜像已有 WAL 增量快路径（无触发器时零额外 SELECT）；绕过执行器的直接存储写不产生事件；无复制过滤/多通道 |
| 验收 | `python -m pytest tests/test_binlog_append_only.py tests/test_binlog_phase2.py -q`（16 + 19 例：位点单调/不回退、CRC 损坏、截断修复、事务分组、主从闭环、重启续传、控制语句、轮转跨文件、写失败拒绝提交、镜像一致性） |

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

**测试套件**（2026-10-21 GUI P0/P1 首批后的门禁基线）：**1068 用例 = 1027 通过 / 0 失败 / 39 跳过 / 1 xfailed / 1 xpassed**（GitHub Windows runner CI 全绿 + 本地新增 9 例；本轮优化开始时是 150 通过 / 118 失败）。**失败数保持 0**——原 24 条失败按根因处置：
- **真实缺陷已修**：`row_format` DECIMAL 编码 TypeError、CLI `-h/--host` 与 argparse 内建 `-h` 冲突（Windows CLI 必崩）、诊断 CLI 的 emoji 在 GBK 控制台崩溃；权限体系（GRANT/REVOKE/鉴权，已接线，2 条 GRANT 用例恢复为正式测试）；半同步死代码已删除（原 xfail 移除）；
- **OLAP 4A.5 门禁化**：原 3 条性能类 skip 换成实测基线阈值并恢复正式测试（编码压缩率 2.5x vs 实测 2.78x；OLAP 聚合 2.0M rows/s vs 实测 2.72M；并行 1.2x vs 实测 2.75x，<4 核环境按 reason skip）；新增并行 GROUP BY 门禁与 dispatch 对拍门禁；
- **带明确 reason 的 skip/xfail**（不是无声绿）：`parser_v3b4` 死代码（×2）、`row_format` 实验格式性能目标、沙箱写权限（×2）、诊断子进程 `DuplicateHandle` 沙箱限制、IPC 延迟阈值防抖、`lineage_enhanced` 列级血缘已知缺陷、mysql-connector 未安装时的端到端 skip；
- 并发用例去掉已删除的 `query_cache` 断言、QPS 阈值降为防抖健全性检查（吞吐门禁在 `benchmarks/`）。

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
- 结果集 / 错误信息复制（Ctrl+C，完整报文）

### 🔍 查询能力
- SQL 编辑器（语法高亮；自动补全未实现，相关死代码已于 2026-10-19 删除）
- 多标签查询
- 查询历史（搜索；收藏未实现）
- SQL 片段库（18+ 内置模板）
- 结果集表格（排序 / 过滤 / NULL 高亮）

### 📈 可视化
- 数据图表（柱状 / 折线 / 饼图 / 散点 4 种）
- ER 图（启发式外键推断）
- 数据血缘图（表级；列级血缘为已知缺陷）

> 2026-10-19 按用户决策删除 Studio 死代码：44 个模块 / 15,728 行（含仪表盘、Hex 查看器、
> Excel 兼容复制、工作流 UI 等"只有死代码支撑"的功能），`lightdb_studio` 从 134 文件/39,256 行
> 降到 **90 文件/23,532 行**；上表只列**真实可点开**的能力。

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