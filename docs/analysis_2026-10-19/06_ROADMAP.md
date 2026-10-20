# LightDB 后续路线图（2026-10-19 修复后）

> 本文是《[全面分析报告](../COMPREHENSIVE_ANALYSIS_2026-10-19.md)》§11 路线图与
> [05 高危深挖](05_RISK_DEEP_DIVE.md) 的**接续版**：P0/P1 六项已闭环，本文给出之后怎么走。
> 旧版路线图 [docs/review/04_ROADMAP.md](../review/04_ROADMAP.md) 为 2026-10-13 评估时点产物，
> 已被本文取代（历史价值保留）。
> 原则：**正确性 > 安全 > 可用性 > 性能**；每项必须有可复跑的验收命令；不再新增
> "概念模块/未接线能力"；无法验证的宣称一律摘牌。

---

## 0. 当前基线（2026-10-19 收口）

| 维度 | 状态 |
|---|---|
| 已修复 | **P0-A** 默认认证/回环监听/连接限制（`15476dc`）· **P0-D** Web 密钥/默认口令/注入/连接管理（`9bf0486`）· **P1-B** 协议层事务（`ec41025`）· **P1-C** 类型/长度/日期/NOT NULL 校验（`1c26527`）· **P1-F** DDL 假成功（`59783e7`）· **P1-E** 复制摘牌 + 状态语义（`59783e7`） |
| 测试 | **825 用例 = 777 passed / 24 failed / 23 skipped / 1 xfailed**；24 条失败与修复前逐条相同（零回归） |
| 性能门禁 | `benchmarks.run --suite quick --gate` **PASS 5/5**（INSERT ~3.9k rows/s、点查 ~1.4k q/s、WAL 469KB、崩溃恢复 0.54s） |
| 打包 | wheel 仍**不含 `lightdb` 引擎包**；`lightdb-server` 入口不存在；`lightdb-studio-cli` 指向不存在的模块 |
| CI | **无**（无任何 CI 配置） |
| 文档 | 分析/修复文档已入库；README 已按修复后状态更新；v3/v4 历史营销文档仍在（已加免责声明） |

**结论**：内核与关键安全底线已经可用于「单机、受控、小规模」场景；距离「可交付产品」还差
**打包/CI、授权体系、协议完整性、Studio/Web 收口、存储可靠性余项**五块。

---

## 0.5 决策记录与执行进度（2026-10-19 用户拍板）

用户对路线的四个决策点答复如下，本文据此调整执行顺序：

| # | 决策 | 对路线图的影响 |
|---|---|---|
| 1 | **产品定位：数据库可交付产品** | 阶段 0/1/2/3 全部为必做（不再是"可选"）；新增**发布门槛**：可 pip 安装、CI 门禁全绿、安全清单（认证/权限/审计/凭据）通过、版本与文档一致、已知缺陷清单公开。里程碑：`v4.1 交付底座` → `v4.2 授权与协议` → `v4.3 存储可靠性` → `v5.0 OLAP 接入 + 真复制` |
| 2 | **OLAP/列存/并行/Cython：接入 SQL 路径（1–2 个月）** | 由"阶段 4 终局二选一"改为**必做接入**，新增 §2.5「OLAP 接入专项计划」；实验性 skip 在接入完成后逐条恢复为正式门禁 |
| 3 | **Studio 死代码：批量删除** | ✅ **已执行**（commit `32f479f`）：删除 44 个死模块（15,728 行）+ 6 个死测试文件；`lightdb_studio` 134 文件/39,256 行 → **90 文件/23,532 行**。阶段 3 只剩"保留功能的可用性收口" |
| 4 | **复制：投入 1–2 周做真 append-only binlog** | 由"摘牌或 1–2 周二选一"改为**立即投入**，见 §2.6「真复制专项计划」；`semi_sync` 死代码在 M4 决策删除 |

### 执行进度（截至 2026-10-20 中午）

| 工作项 | 状态 | 证据 |
|---|---|---|
| 阶段 0.1 打包修复 | ✅ 已完成（wheel 含引擎包/3 入口/版本单一来源；venv 实测 `lightdb-server --version`） | `pyproject.toml`、`scripts/check_packaging.py` |
| 阶段 0.2 CI | ✅ 已落地并**本地完整实跑**（compileall + 全量 pytest + benchmark gate + packaging check）；本地 `origin` 已指向 `TomJerryGit/lightdb`（用户创建），推送后 GitHub Actions 触发（本机网络/认证就绪后） | `.github/workflows/ci.yml`、`d7988b3`、`scripts/ci.ps1` |
| 阶段 0.3 失败测试清零 | ✅ 已完成（真实缺陷修复 + 带 reason 的 skip/xfail；24 → 0） | `tests/regression/*`、`test_batch9/10/14`、`test_v3_batch*`、`test_v4_batch*` 等 |
| 阶段 0.4/0.5 收尾 | ✅ 发版材料（CHANGELOG/RELEASING/版本标记一致）、仓库卫生（data_* / worktree / 双 LICENSE / 调试产物 / .gitattributes）、`ci.ps1` 可执行性 | commit `d7988b3`；`scripts/check_repo_hygiene.py` PASS |
| **阶段 1.1 授权体系** | ✅ 已完成（GRANT/REVOKE/SHOW GRANTS + 执行期鉴权 1142/1044/1227 + 协议层元数据过滤；真实 pymysql 端到端） | commit `bb2dce5`；`tests/test_privileges.py` 12 例 |
| **阶段 1 协议项（COM_STMT）** | ✅ 已完成（PREPARE/EXECUTE/CLOSE/RESET、二进制参数/结果集、1210/1243/1235、PREPARE 鉴权） | commit `02a4e2c`；`tests/test_prepared_statements.py` 16 例 |
| **决策 2 OLAP 4A.1** | ✅ 正确性止血完成（12 类缺陷 + 91 例对拍矩阵；取消 2 个 skip） | commit `d6170b1` |
| **决策 2 OLAP 4A.2/4A.3** | ✅ 列存/OLAP **显式接入 SQL 路径**（2026-10-20 起**默认开启**，`LIGHTDB_OLAP_DISPATCH=0` 可关闭；crosscheck + dispatch_stats；63 例端到端对拍）+ 并行分片重做（GROUP BY w2 实测 2.75x） | commit `d26b7ee`、`16bcd1d` |
| **决策 2 OLAP 4A.5** | ✅ 两条门禁（并行 GROUP BY ≥1.2x、dispatch 对拍不一致=0）；3 条性能类 skip 换成实测基线阈值并恢复正式测试；quick 门禁由 5 项扩到 7 项 | 本次提交（benchmarks/cases.py + gate.py + tests/regression_v4） |
| **决策 2 OLAP 4A.4 Cython** | ✅ **真实编译验证**（Cython 3.3.0 + Zig 0.16.0，D 盘 venv；6 个扩展编译通过；修 3 处 Cython 3 语法 + 2 个 C 层 bug；对拍测试；产物不入库/CI 走回退） | commit `92b033c`；`tests/test_cython_compiled_parity.py` |
| **本地全量 GUI+DB 验证** | ✅ 数据库矩阵 **34/34**、GUI **45/45 场景 / 77 截图 / 25 对话框**（offscreen + windows 平台窗口）；发现并修 3 个真实缺陷（UNION 协议/ORDER BY·LIMIT、并发 tables 迭代）；登记 3 条 UI 缺陷 + 2 条引擎限制 | commit `16bcd1d`；`docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md` |
| 决策 3 Studio 删除 | ✅ 已完成（134→90 文件；39,256→23,532 行；悬空 import=0） | commit `32f479f` |
| **决策 4 真 binlog** | ✅ **阶段 1+2 完成**（文件/CRC/位点/SHOW/主从闭环 + 轮转/严格提交/UPDATE·DELETE 镜像快路径；GTID 不做、半同步移除） | commits `903d8e4`、`3b48126`；`test_binlog_append_only.py` 16 + `test_binlog_phase2.py` 19 |

---

## 1. 24 条失败测试的处置计划（先做，因为它就是"剩余工作清单"）

| 处置 | 用例 | 动作 | 工作量 |
|---|---|---|---|
| **A. 真实缺陷（实现修复）** | `test_batch9::test_grant_via_sql`、`test_batch9::test_privilege_enforcement` | 接线权限体系（见阶段 1）；当前 GRANT 语法 1064、登录即全权 | 3–5 天 |
| | `regression/test_performance::test_row_format_performance` | `BinaryRowFormat` 对 DECIMAL 走 `bytes(float)` 抛 TypeError；该格式是死代码/仅 Cython fallback，可修 1 处编码或在门禁里标 experimental | 0.5 天 |
| | `test_batch14::test_semi_sync_sql` | 半同步是死代码；**选择：删除死代码 + 删除该测试**（推荐）或实现 | 0.5 天 / 2 周 |
| **B. 死模块/旧契约（删除或归档）** | `test_v3_batch4::test_parser_v3b4`、`test_variable_executor`、`test_batch10::test_concurrent_load`（引用已删除的 `srv.query_cache`） | `parser_v3b4`、`query_cache` 均为死代码；删除模块与其测试，或改为显式 skip 并注明 | 0.5 天 |
| | `test_v2_batch1::test_lineage_enhanced`、`test_v2_batch4::test_virtualization/federation/data_api`、`test_v2_batch5::test_recommender`、`test_v2_batch6::test_react_agent`、`test_v2_batch7::test_digital_twin` | 全是 `lightdb_studio/core/` 无 UI 入口的"数据平台愿景"模块（33 个死模块）；**选择：批量归档/删除**（推荐）或逐个接线 | 1–2 天 |
| **C. 实验性未接线（显式 skip + 门禁）** | `test_columnar::test_encoder_compression/olap_groupby`、`test_performance_v4::test_olap_aggregate_performance/test_parallel_speedup`、`test_v4_batch4::test_encoder_auto_select` | OLAP/列存/并行已由 README 摘牌；测试改为 `skipif(reason="experimental, not wired")`，数字目标移入 benchmark 的"报告项"而非断言 | 0.5 天 |
| **D. 环境/平台（改 skip 条件）** | `test_v3_batch1::test_full_pipeline_dry_run`、`test_v3_batch5::test_dashboard_generation`（沙箱写 `~/.lightdb_studio`、`dashboards/` 被拒）、`test_v4_batch1::test_end_to_end_diagnosis`（诊断 CLI 在 GBK 控制台打印 emoji `❌`） | 前两条加 `skipif(不可写)`；诊断 CLI 改 ASCII 标记（顺手修 `lightdb/tools/diagnose.py` 的 emoji） | 0.5 天 |
| **E. flaky（阈值/隔离）** | `test_ipc_latency`（100µs 阈值过紧） | 放宽阈值或标 `skipif(负载)` | 0.2 天 |

> 目标：**24 → 0**。其中 B/C/D/E 约 1–2 天即可让门禁"全绿且每条 skip 有理由"，剩余 A 类
> 随阶段 1/2 解决。**"全绿"不等于全部实现——skip 必须带明确的 experimental/环境原因。**

---

## 2. 阶段路线图

### 阶段 0 · 交付底座（1–2 天，最高性价比）

| # | 任务 | 验收 |
|---|---|---|
| 0.1 | **修复打包**：`packages.find` 加 `lightdb*`；补 `lightdb-server` 入口；实现/删除 `lightdb-studio-cli`；pyproject 依赖补 `psutil`/`numpy` 并加 optional extras 分层 | `pip wheel .` 后 wheel 内含 `lightdb/`；`pip install <wheel>` 后 `lightdb-server --version`、`python -m lightdb.main --check` 可用 |
| 0.2 | **引入 CI**（GitHub Actions 或本地 `scripts/ci.ps1` 均可）：全量 pytest 分片 + benchmark 门禁 + 打包校验 + `compileall` | PR/提交级门禁一条命令可跑；结果可复现 |
| 0.3 | **测试门禁清零**（§1 的 B/C/D/E 部分） | `pytest` 只剩 A 类/显式 skip；每条 skip 有 reason |
| 0.4 | **仓库卫生**：清 47 个 `data_*` 遗留目录（改为 tmp_path fixture）、移除 `_wt_head` 遗留 worktree、`LICENSE`/`LICENSE.txt` 合并、根目录调试产物归档、`.gitattributes` 统一行尾 | `git status` 干净；仓库体积下降；新 clone 无历史测试残留 |
| 0.5 | **版本单一来源**：`lightdb.__version__` 与 pyproject 统一（当前 4.0.0 vs 1.0.0） | `--version` 与包元数据一致 |

### 阶段 1 · 授权与协议完整性（3–5 天，安全收口）

| # | 任务 | 验收 |
|---|---|---|
| 1.1 | **权限体系接线**：`GRANT/REVOKE/SHOW GRANTS` 语法 + `security/privileges.py` 真正进入 `Executor`/`Server` 检查（SELECT/INSERT/UPDATE/DELETE/DDL 五类）；root 默认 ALL | 无授权用户越权操作返回 1142/1044；授权后可用；`SHOW GRANTS` 真实 |
| 1.2 | **预处理语句**（COM_STMT_PREPARE/EXECUTE/CLOSE）：至少支持参数占位与类型绑定 | pymysql `cursor(prepared=True)`/mysql-connector 的 server-side prepare 路径可用 |
| 1.3 | **审计与敏感信息**：`CREATE USER ... IDENTIFIED BY` 落盘脱敏；`SHOW AUDIT LOG` 加权限；失败登录审计 | audit.log 无明文口令；越权读审计被拒 |
| 1.4 | **多进程准入**：Worker 自 accept 路径也检查 `max_connections/max_conn_per_ip/IP 白名单`（单进程已生效） | multi 模式下的限流测试（普通 Windows/Linux 上跑） |
| 1.5 | **剩余协议口径**：`SHOW DATABASES` 去硬编码（仅真实单库）；`SELECT DATABASE()` 返回 `default`；SHOW 命令补齐常用项；`SET PASSWORD POLICY` 经协议可达 | 元数据一致性测试；pymysql 兼容矩阵 |

### 阶段 2 · 存储正确性与可靠性余项（1–2 周，数据信任）

| # | 任务 | 验收 |
|---|---|---|
| 2.1 | **WAL 行级 CRC + 损坏检测**：尾部撕裂可恢复、中间损坏显式报错（当前 JSONL 无校验和、解析失败静默跳过） | 故障注入测试（截断/翻转字节）区分"安全恢复"与"必须拒绝启动" |
| 2.2 | **COMMIT 只刷参与事务的表**（当前 O(表数) fsync：7 表 2–4s/次） | 多表库 COMMIT 延迟随参与表数而非总表数增长 |
| 2.3 | **隔离级别诚实化**：实现 READ COMMITTED 每语句新 ReadView，或对外如实报 REPEATABLE READ（当前谎报 RC） | `@@transaction_isolation` 与真实行为一致 |
| 2.4 | **EXPLAIN 反映真实访问路径**（当前恒 `ALL/key=None`） | int PK 等值查询 EXPLAIN 走 `const/ref` 语义 |
| 2.5 | **生成列真正重算**（显式赋值不重算）；**Python API `db.create_table` 补主键类型校验**；**BIGINT PK uint32 编码限制**（改变长键或定义期限制） | 新增锁测试；README 边界更新 |
| 2.6 | **页回收/空间收缩**（阶段 2.3 未达成项：`.dat` 只增不减；vacuum 只释放槽位） | 删除 90% 行 + vacuum 后文件显著缩小；完整性扫描全绿 |

### 阶段 3 · Studio / Web 产品收口（2–4 周，可用性）

| # | 任务 | 验收 |
|---|---|---|
| 3.1 | **死代码清理**：42/134 不可达模块 + 33 个 core 愿景模块 → 删除或移入 `experimental/`；README 功能表按"真实可用"重写 | 不可达行占比从 36.5% 降到 <10%；功能表 100% 可点开 |
| 3.2 | **GUI 线程模型**：SQL 执行移入 QThread + 分页/流式读取 + 读超时（当前同步 + `fetchall` 会冻结界面） | 10 万行查询 UI 不冻结；离屏 GUI 行为测试进 CI |
| 3.3 | **凭据安全**：连接密码/AI key 加密（系统钥匙串或主口令），不再明文 `connections.json` | 文件扫描无明文口令 |
| 3.4 | **生产保护接线**：`check_sql_safety` 真正拦截 prod 环境写操作；环境切换提示 | 用例：prod 环境 UPDATE 需二次确认 |
| 3.5 | **Web 端补全**：前端"新建连接"表单（后端端点已有）、按用户历史、logout/refresh、token 吊销；`/metrics` 保持可用 | 浏览器全流程：登录→建连接→查询→导出 |
| 3.6 | **真实功能补齐/摘牌**：仪表盘/Hex 查看器/Excel 复制/工作流——要么接进入口，要么从 README 删除 | 文档-实现一致性检查 |

### 阶段 4 · 性能与架构决策（4 周+，需要维护者拍板）

前置决策（四选一，建议先做 4.1/4.2，其余视目标）：
1. **4.1 三条 SELECT 执行路径统一**（单表快路径/高级内存路径/计划路径 → 一套语义），消除"WHERE 可用、投影报错"类能力漂移。验收：同一 SQL 在三条路径结果一致（对拍测试）。
2. **4.2 存储格式**：JSON 行载荷 → 紧凑二进制（含 DECIMAL/日期原生编码），同时修 `row_format.py` 死代码现状。验收：空间下降 + 基准吞吐不退化。
3. **4.3 并发/多进程**：当前单线程查询（并发负扩展 0.58–0.70×）、多进程只做连接层并行。选择：接受"单机单线程"定位并写入文档，或做读写分离/分片（大工程，需重新设计存储共享）。
4. **4.4 OLAP/列存/并行/Cython 终局**：要么接入 SQL 路径并纳入基准门禁（至少 1–2 个月），要么**彻底移出生产包**（推荐，当前无收益且持续误导）。验收：仓库中不再有"设计目标"被当成能力描述。

### 2.5 OLAP/列存/并行/Cython 接入专项计划（决策 2：1–2 个月，必做）

目标：把 `lightdb/olap/**`、`lightdb/columnar/**`、`lightdb/parallel/**`、`lightdb/cython/**`
**真正接入 SQL 执行路径**，并把性能数字变成可复跑门禁（不再只是设计目标）。分 5 步：

| 步骤 | 内容 | 状态 / 验收 |
|---|---|---|
| 4A.1 正确性止血 | 修 OLAP 聚合的已知错值：`COUNT` 计 NULL 语义、`AVG` 未实现、`int32` 溢出、列存 `GROUP BY` 返回 0 行、`VectorizedFilter` 列映射；每项加对拍测试 | ✅ 完成：12 类缺陷 + 91 例对拍（`d6170b1`） |
| 4A.2 显式 dispatch | 在 `optimizer`/`plan_executor` 增加**明确的**接入点（按表存储格式/聚合形态），并保证默认行为不变（行存）；`status()` 如实报告哪条路径生效；删除任何"悄悄走到向量化"的分派 | ✅ 完成：`sql_dispatch.try_vectorized_select` + Executor 钩子（默认关闭、白名单、crosscheck、`dispatch_stats`），63 例对拍（`d26b7ee`）；✅ 不支持形态一律回退行存 |
| 4A.3 并行修正 | 修 `parallel/executor.py` 的负加速：做有意义的并行分片，或删除并行并如实声明单线程 | ✅ 完成：向量化聚合 64K 分片 + 线程池；GROUP BY w2 2.75x；纯 SUM 加速不足 1.2x → 自动档串行（结果逐值一致） |
| 4A.4 Cython 可验证 | CI 可选安装 Cython + 编译器跑 `build_ext`；无编译器环境显式回退并在 status/日志标明 | ⏳ 未做（本机无 Cython/编译器；仍是纯 Python 回退，不计入性能声明） |
| 4A.5 门禁化 | 为 OLAP/列存设定**可辩护**阈值（同窗成对、基于实测基线），纳入 `benchmarks.run --gate`；取消实验性 skip | ✅ 完成：`olap_parallel_groupby_speedup ≥1.2x`、`olap_dispatch_crosscheck_mismatches ==0`；3 条 skip 换实测阈值恢复；quick PASS 7/7（30.6s） |

### 2.6 真复制专项计划（决策 4：1–2 周，进行中）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 文件与读写 | `<data_dir>/binlog/binlog.000001`：header+记录长度+JSON+CRC32；`BinlogReader` 支持任意 pos 读取、损坏显式报错；DML 事务成组、DDL 事件、位点=真实字节偏移且只增不回退 | `tests/test_binlog_append_only.py`：位点单调、checkpoint/ALTER 不回退、CRC 损坏报错、事务分组 |
| M2 SHOW 接线 | `SHOW MASTER STATUS` / `SHOW BINLOG EVENTS` / `__BINLOG_FETCH__` 全部读新文件（不再扫 WAL） | 断点/位点断言；旧"位点回退"复现用例转为通过 |
| M3 主从闭环 | `replication/slave.py` 按持久化位点拉取、SQL 线程重放 DML+DDL、重启续传；`START SLAVE`/`STOP SLAVE`/`CHANGE MASTER TO` 最小语法 | 进程内主从：DML+DDL 100% 同步；从库重启续传；`SHOW SLAVE STATUS` 真实 |
| M4 半同步决策 | `semi_sync` 死代码删除（或实现）；`Last_SQL_Errno` 语义保持非 0（E2 已修） | 代码/文档一致；`test_batch14::test_semi_sync_sql` 的 xfail 改删除 |
| M5 文档与门禁 | 更新「复制支持边界」为"可用（实验→可用）"，写明已知边界（无 GTID/无过滤/单通道）；测试进 CI | README/05/06 一致；CI 含复制测试 |

### 明确"不做"清单

- 不再追求白皮书 QPS/加速比数字（Python 单线程 + JSON 行格式的物理上限）。
- 不做 MySQL 8 全兼容（窗口/CTE 已有，但视图/事件/外键/二级索引是另一个量级）。
- 不复活 `lightdb_studio/core/` 的"数据平台愿景"模块（无数据源、无入口）。
- 不在未完成 4.1/4.2 前宣传"生产级数据库"或"Navicat 替代"。
- 不再用"文档 + 注释"代替测试与门禁。

---

## 3. 里程碑与依赖

```
阶段 0（打包/CI/门禁清零）──┬─▶ 阶段 1（权限/协议）──▶ 阶段 2（存储可靠性）──▶ 阶段 4（性能/架构）
                            └─▶ 阶段 3（Studio/Web 收口）───────────────────────┘
```

- 阶段 0 是**所有后续工作的地基**（没有 CI 和可安装包，后面的修复无法验收/交付）。
- 阶段 1 的权限是最后一个"安全 P1"；建议与阶段 0 并行（改动面在 `security/` + `executor` + `server`）。
- 阶段 3 可与 1/2 并行（`lightdb_studio/`、`web/` 与引擎独立）。
- 阶段 4 的 4.1（路径统一）建议在阶段 2 后做，避免与存储改动冲突。

## 4. 建议的"下一个 3 个动作"

> 2026-10-20 下午更新：阶段 0/1、OLAP 4A.1–4A.5、真 binlog 阶段 2 均已完成。现在的下一步：

1. **阶段 2 存储可靠性**：WAL 行级 CRC + 损坏检测、COMMIT 只刷参与表（当前 O(表数) fsync）、
   隔离级别诚实化（实现 RC 或如实报 RR）、EXPLAIN 真实访问路径、生成列重算、页回收/vacuum。
2. **阶段 1 收尾 + 交付验证**：`SET PASSWORD POLICY` 落地、审计越权保护；把仓库推到 GitHub
   让 `.github/workflows/ci.yml` 真实触发（当前无远端，本地 CI 已实跑）；补 4A.4（Cython
   编译验证，需可编译环境）。
3. **阶段 3 Studio/Web 可用性收口**：QThread/分页/读超时、连接密码加密、Web 前端连接表单、
   按用户历史；之后进入阶段 4 架构项（三条 SELECT 路径统一 / 紧凑二进制行格式 / 并发定位）。

## 5. KPI（每次阶段收口测一次）

| 指标 | 当前（2026-10-20 下午） | 阶段 0 目标 | 阶段 1 | 阶段 2 | 阶段 3 |
|---|---:|---:|---:|---:|---:|
| 用例通过 / 失败 | 见下方"实测基线"（0 failed；skip/xfail 全部带 reason） | ✅ 失败=0（显式 skip 除外） | ✅ 权限/预编译已完成；审计部分 | 存储故障注入通过 | GUI 行为测试进 CI |
| 打包 | ✅ wheel 含引擎 + 3 入口（venv 实测） | ✅ 已修 | — | — | GUI/Web 可安装 |
| 基准门禁 | **PASS 7/7**（含 2 条 OLAP 门禁） | ✅ 已修 | ✅ 保持 | COMMIT 延迟 O(参与表) | — |
| 安全项 | ✅ P0/P1 六项 + 授权体系 + COM_STMT 闭环 | — | 审计/`SET PASSWORD POLICY` 仍缺 | — | 凭据加密 |
| OLAP 接线/门禁 | ✅ 4A.1–4A.5 完成（**2026-10-20 起 dispatch 默认开启**，可 `LIGHTDB_OLAP_DISPATCH=0` 关闭；并行 GROUP BY 2.75x、对拍 0 不一致；dispatch 当前无显著收益 0.44–1.10x） | — | — | — | — |
| Cython 加速层 | ✅ 4A.4 本机真实编译 6 个扩展（Cython+Zig；btree 5.9–7.0x 等）；产物不入库、CI 走纯 Python 回退 | — | — | — | MSVC 发布构建 |
| GUI/DB 端到端 | ✅ 2026-10-20 本地全量验证：DB 34/34、GUI 45/45、77 截图、25 对话框；UI 3 缺陷/引擎 2 限制已登记 | — | — | GUI 行为测试进 CI | — |
| 死代码行占比 | Studio 不可达已清零；引擎仍有部分零引用模块 | — | — | — | 引擎 <10% |
| 文档-实现一致 | README/CHANGELOG/roadmap/benchmarks 已同步 OLAP 接线与门禁状态 | ✅ 打包声明成真 | 协议文档 | 存储边界文档 | 功能表 100% 真实 |

### 实测基线（2026-10-20 下午，全量 pytest + benchmark）

- 全量 pytest：**1059 用例 = 1018 passed / 0 failed / 39 skipped / 1 xfailed /
  1 xpassed**（17:30，8 核本机、纯 Python Cython 回退运行时；GUI P0 新增 30 例，
  1 条旧 xfail 修复转正）；
- `benchmarks.run --suite quick --gate`：**PASS 7/7**（原 5 条 + OLAP 并行 GROUP BY ≥1.2x、
  dispatch 对拍不一致=0）；
- `scripts/check_packaging.py`：PASS（wheel 含引擎包 + 3 入口，版本 4.0.0 单一来源）；
  `scripts/check_repo_hygiene.py`：PASS（提交本报告后复跑通过）。


---

## 附：本轮修复的提交锚点（回退参考）

```
99dd785 docs: P1-B/C/F/E 修复说明、支持边界与修复后基线
d135277 feat(sql): BETWEEN/DISTINCT/IN 三值语义收口（前序会话未提交工作）
59783e7 fix(ddl+repl): P1-F DDL 假成功与 P1-E 复制摘牌/状态语义
1c26527 fix(sql): P1-C 声明式类型约束真正生效
ec41025 fix(txn): P1-B 协议层事务真正生效
476ffd7 docs: 安全默认说明与 2026-10-19 全面分析报告
9bf0486 fix(security): P0-D Web 认证密钥/默认口令/标识符注入/连接管理
15476dc fix(security): P0-A 默认认证/回环监听/连接限制
```
