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

## 4. 建议的"下一个 3 个动作"（如果只做三天）

1. **打包 + CI**（阶段 0.1/0.2）：让"能安装、能门禁"成立——这是从项目变成产品的分水岭。
2. **权限体系接线**（阶段 1.1）：最后一个安全 P1（登录即全权）。
3. **测试门禁清零**（阶段 0.3 + §1 B/C/D/E）：24 条失败 → 0，让后续每次改动都有可信基线。

## 5. KPI（每次阶段收口测一次）

| 指标 | 当前 | 阶段 0 目标 | 阶段 1 | 阶段 2 | 阶段 3 |
|---|---:|---:|---:|---:|---:|
| 用例通过 / 失败 | 777 / 24 | 失败=0（显式 skip 除外） | 全部安全用例通过 | 存储故障注入通过 | GUI 行为测试进 CI |
| 打包 | wheel 缺引擎 | `pip install` 可跑 server | — | — | GUI/Web 可安装 |
| 基准门禁 | PASS 5/5 | 保持 | 保持 | COMMIT 延迟 O(参与表) | — |
| 安全项 | P0/P1 六项闭环 | — | 权限/审计/预编译完成 | — | 凭据加密 |
| 死代码行占比 | 引擎 ~74 模块零引用；Studio 36.5% | — | — | — | <10% |
| 文档-实现一致 | README 已对齐 P0/P1 | 打包声明成真 | 权限/协议文档 | 存储边界文档 | 功能表 100% 真实 |

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
