# LightDB 全仓库可验证性证据报告（测试 / 基准 / 端到端 / 打包 / 文档一致性）

> 角色：verify-bench（「全面分析」团队 · 可验证性）
> 日期：2026-10-19（+08:00）· 工作区：`D:\lightdb_project`
> 基线：`git 67d0cd2` + **4 个未提交修改**（`lightdb/db/table.py`、`lightdb/sql/executor.py`、`lightdb/sql/parser.py`、`lightdb/sql/plan_executor.py`，+459/−20），另有未跟踪的 `tests/test_sql_gaps.py`
> 标签约定：**【实测】**=本次亲自运行；**【源码】**=读源码/静态检查；**【文档】**=文档原文；**【推断】**=由证据推理、未直接验证
>
> **数字口径**：本文引用的 OLAP / 列存 / 并行 / Cython 性能数字（`3.5M rows/s`、`85M rows/s`、
> `41x`、`12.5x`、`12,000 / 92,000 QPS` 等）均为**历史宣称/设计目标**；这些能力
> **未接入 SQL 执行路径**、属**实验性**实现，**不计入性能声明**，本文引用它们只为核对文档与实测的差距。

---

## 0. 一句话结论

**【实测】** 当前工作区：**全量 761 用例 = 711 passed / 25 failed / 24 skipped / 1 xfailed（31:07）**；**基准门禁 `quick --gate` 5/5 PASS**（数字落在 `benchmarks/README.md` 实测区间内）；**单进程服务端 + pymysql 端到端主干可用**（连接/认证/DDL/DML/SHOW/ALTER/TRUNCATE/LIMIT/中文均通过，错误码基本正确）；但发现 **3 个跨层/打包级硬伤**：① 经 MySQL 协议 **BEGIN/ROLLBACK 完全无效**（每条语句用全新 session，事务状态不跨语句）；② **wheel 不含 `lightdb` 引擎包**，`lightdb-studio-cli` 入口指向不存在的模块（README 的「已修复」声明与事实相反）；③ 服务端启动时 **并行执行器 import 失败（循环导入）并静默回退串行**。文档层面 10 条抽查断言中 4 条已过时、2 条夸大/不成立、3 条仍成立、1 条无法验证。

---

## 1. 环境与方法

| 项 | 值 | 来源 |
|---|---|---|
| OS / 控制台 | Windows 10 19045 · PowerShell · 控制台 GBK | 【实测】 |
| Python | 3.11.9（`D:\Program Files\Python311`） | 【实测】 |
| pytest | **9.1.1**（`pyproject.toml` 只要求 `pytest>=7.0`） | 【实测】 |
| 关键依赖 | pymysql 2.2.8 / PySide6 6.6.0 / fastapi 0.142.1 / httpx 0.28.1 / setuptools 84.0.0 / wheel 0.46.3 | 【实测】 |
| 缺失依赖 | `Cython`、`python-jose`、`passlib`、`bcrypt` **均未安装**（Web 认证为自研 HMAC-JWT，不依赖后两者，可跑通） | 【实测】 |
| Git 基线 | `67d0cd2`（HEAD）· 工作树脏：4 个已修改源文件 + 未跟踪 `tests/test_sql_gaps.py` | 【实测】 |

**测量条件声明（重要）**：全量 pytest 与另外 4 个并行分析 agent 的脚本**同时运行**，机器非空闲。因此：
- 墙钟 **1867.56s（31:07）**，远高于历史文档记录的 200–300s（【文档】`docs/AUDIT_REPORT.md:211` 290.44s、`docs/HANDOFF.md:9` 约 3–5min）→ 属**负载膨胀**，不得当作回归；
- 计时类用例（IPC 延迟、QPS、并行加速比）受干扰：`test_ipc_latency` 全量失败、隔离两跑均通过（见 §2.3）；
- 基准门禁在全量 pytest **结束后**（机器相对空闲）运行，数字接近 `benchmarks/README.md` 区间上沿。

**文件写入限制**：本 agent 的 shell（pwsh/python）**无法写 `docs/analysis_2026-10-19/**`**（该目录由其他 agent 创建，未继承 DSH 沙箱写权限 ACE，报 `[sandbox: file access denied under workspace-write mode]`），只能用 DSH `write` 工具写入。故：
- `docs/analysis_2026-10-19/logs/` 下的日志均经 write 工具落盘（UTF-8）；
- **全量 pytest 的完整原始 stdout（UTF-16LE，118,804 B，含全部 traceback）** 保存在工作区 `_vb_tmp/pytest_full.log`（原始字节）与 `_vb_tmp/pytest_full_sanitized.log`（UTF-8 转码）；`docs/analysis_2026-10-19/logs/pytest_full.log` 保留**原始进度行 + 25 条 FAILED 清单 + 汇总行**及指针。这是环境限制导致的偏差，不是有意裁剪。

---

## 2. 全量测试

### 2.1 命令与结果

```powershell
cd D:\lightdb_project
python -m pytest tests -q -p no:cacheprovider        # 墙钟 1867.56s = 31:07
```

**【实测】** 原始汇总行（`logs/pytest_full.log`）：

```
25 failed, 711 passed, 24 skipped, 1 xfailed, 5 warnings in 1867.56s (0:31:07)
```

| 指标 | 数值 | 与历史文档对照 |
|---|---:|---|
| 收集用例总数 | **761**（25+711+24+1） | 【文档】README:81「268 个测试」/ README:161「299 通过 83 失败 1 跳过」/ HANDOFF:9「34 failed, 463 passed, 11 skipped」——**均与当前不符（套件已膨胀）** |
| passed | **711** | 同上 |
| failed | **25** | README:161 称「83 失败」、HANDOFF 称「34 failed」→ 数量级已变 |
| skipped | **24** | README:161 称「1 跳过」→ 当前 24（平台/环境类 skip 明显增多，-q 未打印原因） |
| xfailed | 1 | — |
| warnings | 5 | 其中 1 条子进程 `UnicodeDecodeError: 'gbk' codec` |

**结论**：任务给的「761 用例」【实测】成立；README 中的 `268/299/34-failed` 等测试基线**全部过时**（详见 §7）。

### 2.2 失败清单与根因分类（25 条）

分类口径：**A 真实缺陷/功能缺口 · B 环境/平台限制 · C flaky（抖动）· E 测试自身期望或桩写错**。判据：隔离重跑（`--tb=line`，24 failed / 1 passed）见 `logs/pytest_rerun.log`。

| # | 用例 | 类 | 一句话根因（原文摘录） |
|---|---|---|---|
| 1 | `tests/regression/test_performance.py::test_row_format_performance` | A | `lightdb/storage/row_format.py:89: TypeError: cannot convert 'float' object to bytes` —— 列声明 `DECIMAL` 但行值给 `1.5`，`BinaryRowFormat` 未处理 DECIMAL 落入字符串分支 `bytes(v)`。该用例历史上曾被文档标为「已修好」（AUDIT:330），当前又回归失败 |
| 2 | `tests/regression/test_sql_compat.py::test_variables` | A | `AssertionError: 失败: SET @@autocommit = 1`（`assert None is not None`）——用户变量/系统变量 `SET @@x` 不支持 |
| 3 | `tests/regression_v4/test_columnar.py::test_encoder_compression` | A/E | `assert 2.776 > 3`，`→ 字典压缩: 2.8x` —— 压缩率设计目标未达到（实验性列存） |
| 4 | `tests/regression_v4/test_columnar.py::test_olap_groupby` | A | `assert 0 == 100000` —— 列存 `GROUP BY` 返回 0 行（历史已知缺陷，未接线/无效） |
| 5 | `tests/regression_v4/test_performance_v4.py::test_olap_aggregate_performance` | A/目标 | `assert 1,347,019 > 20,000,000 rows/s` —— 设计目标（20M rows/s）未达成；且该链路未接入 SQL 路径 |
| 6 | `tests/regression_v4/test_performance_v4.py::test_parallel_speedup` | A/目标 | `并行加速不足: 0.55x`（`assert 0.552 > 1.5`；全量时 0.18x）—— 并行执行仅线程+GIL，负加速 |
| 7 | `tests/test_batch10.py::test_concurrent_load` | E+C | 全量：`AssertionError: QPS 太低`（42）；隔离：`AttributeError: 'MySQLServer' object has no attribute 'query_cache'`（QPS 174 过了断言后暴露）—— 测试引用了已被移除的属性，且 QPS 阈值受负载影响 |
| 8 | `tests/test_batch14.py::test_semi_sync_sql` | A | `ProgrammingError: (1064, ... near '__SEMI_SYNC_REGISTER__ slave_test'")` —— 半同步注册的内部语句不存在 |
| 9 | `tests/test_batch9.py::test_grant_via_sql` | A | `(1064, ... near 'GRANT SELECT, INSERT ON *.* TO 'bob'')` —— GRANT 语法未实现（显式报错） |
| 10 | `tests/test_batch9.py::test_privilege_enforcement` | A/E | `(1819, "Your password does not satisfy the current policy requirements（用户 'readonly'@%：密码长度至少 6 位）")` —— 测试用短密码创建用户；测试期望与口令策略冲突 |
| 11 | `tests/test_batch9.py::test_cli_tool` | A(移植性) | 子进程 stderr：`lightdb/tools/cli.py:10, in <module> import readline → ModuleNotFoundError` —— CPython on Windows 无 readline，源码顶层 import 未保护（Linux 下不失败） |
| 12 | `tests/test_v2_batch1.py::test_lineage_enhanced` | A | `assert 0 >= 1`，`len(EnhancedLineageStore().edges) == 0` —— 列级血缘 0 条 |
| 13 | `tests/test_v2_batch4.py::test_virtualization` | A | `assert 3 == 2` —— 虚拟表 `WHERE age > 26` 未生效，返回全部 3 行 |
| 14 | `tests/test_v2_batch4.py::test_federation` | A | `assert 'c.city_name' in ['u.id', 'u.name', 'u.city_id']` —— 联邦 JOIN 丢列 |
| 15 | `tests/test_v2_batch4.py::test_data_api` | A | `assert 0 == 2` —— Data API `city=Beijing` 过滤后 0 行 |
| 16 | `tests/test_v2_batch5.py::test_recommender` | A | `assert False`（`→ users 关联表:` 为空）—— 推荐器找不出关联表 |
| 17 | `tests/test_v2_batch6.py::test_react_agent` | E/A | `assert 0 >= 1`（`→ 记忆检索: 0 条`）；工具调用与解析均通过，仅记忆检索为 0（HANDOFF 曾列其为 E 类桩问题） |
| 18 | `tests/test_v2_batch7.py::test_digital_twin` | A | `AttributeError: 'NoneType' object has no attribute 'snapshot_id'` —— 创建孪生后 snapshot 为 None |
| 19 | `tests/test_v3_batch1.py::test_full_pipeline_dry_run` | B | `PermissionError: [Errno 13] ... 'C:\Users\Administrator\.lightdb_studio\reports\benchmark_dry_run_*.md'` —— 沙箱禁止写用户主目录 |
| 20 | `tests/test_v3_batch4.py::test_parser_v3b4` | E | `TypeError: 'NoneType' object is not subscriptable`（测试 `tests/test_v3_batch4.py:331` 下标了 None；HANDOFF 已登记为 E 类） |
| 21 | `tests/test_v3_batch4.py::test_variable_executor` | A | `lightdb/sql/parser_v3b4.py:110: TypeError: 'NoneType' ...` —— `SET @x`/`SELECT @x` 后库内代码对 None 下标，属实现缺陷 |
| 22 | `tests/test_v3_batch5.py::test_dashboard_generation` | B | `PermissionError: ... 'D:\lightdb_project\dashboards\lightdb_overview.json'` —— 沙箱 ACL 拒绝写 `dashboards/`（历史已登记环境性用例） |
| 23 | `tests/test_v4_batch1.py::test_diagnose_cli` | A+B | `AttributeError: 'NoneType' object has no attribute 'lower'`（`test_v4_batch1.py:291`）+ 子进程读线程 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x8b` —— 诊断 CLI 取值 None + 控制台 GBK 解码 |
| 24 | `tests/test_v4_batch4.py::test_encoder_auto_select` | A/E | `assert isinstance(DeltaEncoder, RLEEncoder) == False`，`→ 低基数 VARCHAR → Dict` —— 编码器自动选择结果与期望不符（实验性列存） |
| 25 | `tests/regression_v4/test_multiprocess.py::test_ipc_latency` | **C flaky** | 全量：`assert 259.18µs < 100`；**隔离重跑 2 次均通过**（0.36s/0.37s）且全量复跑列表已不含它 → 负载敏感的计时 flaky，非确定性缺陷 |

**分类小计（按上表主类）**：A 真实缺陷/缺口 **17**；A/目标未达成 2（#5/#6）；A+/E 混合 3（#7/#10/#17/#23/#24 中按主因归并后 E 类约 3）；B 环境限制 **2**（#19/#22）；C flaky **1**（#25）；E 测试自身 **3**（#17/#20/#10 部分）。与文档声称的「约 28 项平台限制、其余真实待修」相比：**平台限制实际只有 2 条**（大幅下降），真实缺陷仍占多数。

### 2.3 flaky 复核（【实测】）

```powershell
python -m pytest "tests/regression_v4/test_multiprocess.py::test_ipc_latency" -q -p no:cacheprovider --tb=line
# 连续 2 次：1 passed in 0.37s / 1 passed in 0.36s
```
全量下失败（259.18µs > 100µs 阈值）、隔离 2/2 通过 → 判 **C 类 flaky（计时阈值 100µs 过紧，负载下必红）**，建议放宽阈值或标记 `skipif` 负载。其余 24 条隔离重跑结果与全量一致（确定性失败），**不存在仅全量失败的系统性隔离问题**。

---

## 3. 基准门禁

### 3.1 命令与结果

```powershell
python -m benchmarks.run --suite quick --gate      # 测量墙钟 13.6s，总进程 15.3s，退出码 0
```

**【实测】门禁结果：`PASS （5/5 项达标）`**，原始日志 `logs/bench_gate.log`。实测值 vs `benchmarks/README.md` 基线（§2 实测基线 / §3 门禁阈值）：

| 指标 | 本次实测 | benchmarks/README 实测区间（第 2 节） | 门禁阈值 | 判定 |
|---|---:|---|---:|---|
| 逐行 INSERT（Table API，2,000 行档） | **5,972.5 rows/s**（min 5,644 / max 6,301） | 1,374–6,373（中位 ~3,500）；1,764 / 1,534（full 档） | ≥ 1,000 | **PASS**，处于区间上沿 |
| 多值批量/逐行比值（同窗成对） | **1.605x**（1.6–1.8） | 1.50–1.87x（中位 ~1.6）；5,000 档 1.0–1.7 | ≥ 1.2x | **PASS**，与基线一致 |
| 点查（进程内） | **1,814.7 q/s**（1,634–1,996） | 588–2,150（中位 ~1,400） | ≥ 600 | **PASS**，区间内偏上 |
| WAL 峰值（5,000 行，默认阈值） | **468.6 KB**（479,863 B） | 468.6–478.3 KB | ≤ 32 MB | **PASS**，与基线**逐位一致** |
| 崩溃恢复（2,000 行，硬杀子进程） | **0.364 s** | 0.28–0.44 s | < 5 s | **PASS** |
| 崩溃后丢失已提交行 | **0** | 0 | = 0 | **PASS** |
| 崩溃后泄漏未提交行 | **0** | 0 | = 0 | **PASS** |
| 单条多值 INSERT（500 行/语句） | 8,393.1 rows/s | 4,486–5,442 | 只报告 | — |

**结论**：门禁**通过**，且五项数字全部落在 `benchmarks/README.md` 记录的实测分布内（WAL 468.6KB、rowe-wise 上沿、点查区间内），说明「门禁 + 基线」文档与当前工作区一致，**没有性能回退证据**。注：`--gate` 模式**不写** `benchmarks/results.json`（该文件 mtime 仍为 2026-10-16 15:20，未变化），门禁明细仅在 stdout —— 本报告与 `logs/bench_gate.log` 已按 stdout 完整记录；如需 results.json 副本须显式加 `--out`。注意：结论仅覆盖 `quick` 档（TCP 点查、并发档、20k WAL 档在 `full` 未跑）；并发「不扩展」的已知事实未复测。

### 3.2 与 README / 白皮书数字对照（【实测】vs【文档】）

> **口径提醒**：本节引用的 `12,000 QPS` / `92,000` / `3.5M rows/s` / `41x` / `12.5x` 等均为
> **历史宣称/设计目标**；对应的 OLAP / 列存 / 并行 / Cython 能力**未接入 SQL 执行路径**、
> 属**实验性**实现、**不计入性能声明**。引用它们只是为了让「文档 vs 实测」的差距可核对。

- README:61–67 的「可复现实测数字」表（逐行 INSERT 1,501 rows/s、批量 1,356、点查 888 q/s）是**更早的单次结果**，当前基准中位/区间已更新（`benchmarks/README.md`：逐行中位 ~3,500、点查中位 ~1,400）；README 正文未同步这个更细的区间 → **已过时（方向仍对：远低于白皮书 12,000 QPS）**。
- 白皮书 12,000 QPS / 92,000 QPS / 3.5M rows/s / 41x / 12.5x：**均无可复现证据**；本次点查 1,814.7 q/s ⇒ 与 12,000 差 **6.6×**（README 写的 13.5× 基于旧的 888 q/s）。基准白皮书对照表（`benchmarks/README.md` §4）结论仍成立。

---

## 4. 端到端运行时（真实服务端 + pymysql）

### 4.1 启动与命令行

```powershell
python -m lightdb.main --mode single --host 127.0.0.1 --port 18898 --data-dir _vb_tmp/data
python -m lightdb.main --version   # → "LightDB 4.0.0"，退出码 0
python -m lightdb.main --check     # → "[OK] 配置校验通过" + 配置表，退出码 0
```

**【实测】** `--check`/`--version` 均正常退出（README:20 曾称 `--check` 因 emoji 崩 `UnicodeEncodeError`；README:89 称已修 —— **当前成立**）。服务端 stdout 见 `logs/e2e.log`，端口 127.0.0.1:18898 监听成功，13 个连接会话完成登录。

**启动即暴露的缺陷**【实测】：
```
[WARN] 初始化并行执行器失败（回退串行）: cannot import name 'ParallelExecutor'
       from partially initialized module 'lightdb.parallel.executor' (most likely due to a circular import)
[WARN] 慢查询 0.644s: SELECT ... COUNT(*) ...   # 对 2 行表做 COUNT(*) 被记为慢查询
```
→ **A**：并行执行器循环导入（每次启动必现，功能静默降级串行）；`COUNT(*)` 在 2 行表上耗时 0.644s 也值得单独排查（首条查询含初始化开销，被记成慢查询）。

### 4.2 成功/失败矩阵（完整原文见 `logs/e2e.log`）

| 类别 | 验证项 | 结果 | 原证据摘录 |
|---|---|---|---|
| 连接/认证 | 空密码 root 连接、`SELECT 1` | ✅ | `server_version='5.7.30-LightDB'`（与 README:17 一致） |
| | 错误密码 | ✅ 1045 | `Access denied for user 'root'@'127.0.0.1' (using password: YES)` |
| | `SELECT DATABASE()` | ⚠️ 返回 `None` | `(None,)`（不再是历史错值 `mysql_native_password`，但也不是 `default`） |
| DDL | `CREATE TABLE` / `DROP TABLE` | ✅ | `SHOW TABLES -> (('t_e2e',),)` |
| | `CREATE INDEX ...` 独立语句 | ❌ **1064** | `You have an error in your SQL syntax near 'CREATE INDEX idx_age ...'` |
| | `ALTER TABLE ADD/DROP COLUMN` | ✅ | `columns=['id','name','age','score']`；DROP 亦返回 OK |
| | `TRUNCATE TABLE` | ✅ | — |
| | `SHOW CREATE TABLE` + 回放建表 | ✅ | 输出 `CREATE TABLE ... DEFAULT 'abc' DEFAULT 7`，**回放建表成功**（`REPLAY_RESULT ok=True`） |
| DML | INSERT（含无列名/多行/引号/反斜杠/中文） | ✅ | 中文往返：`(5, '中文测试', 60)` 原样读回 |
| | UPDATE 常量赋值 | ✅ | — |
| | **UPDATE 算术赋值 `SET v = v + 1`** | ❌ **1105** | `无法将 'v + 1' 转换为 INT`（MySQL 常见写法不支持） |
| | DELETE | ✅ | — |
| 查询 | `LIMIT n OFFSET m` / `LIMIT m,n` | ✅ | 返回一致 |
| SHOW | `SHOW DATABASES` | ⚠️ 固定两库 | `('default',), ('information_schema',)`（硬编码，未随实际库变化） |
| | `SHOW TABLES` / `FROM db` / `SHOW COLUMNS` / `DESCRIBE` | ✅ | `SHOW COLUMNS` 返回 `Type='VARCHAR(50)'`、`Default='abc'/'7'`、`Extra=''` —— **真实默认值与长度都正确**（历史「Default 恒 None/长度被抹」缺陷已修） |
| | `SHOW INDEX` | ✅ | 主键 `Index_type='BTREE'`；UNIQUE 索引如实报 `UNIQUE-CHECK`（不谎称 BTREE） |
| 事务 | `BEGIN; INSERT; COMMIT` | ⚠️ 提交"看起来成功" | `id101_visible_from_other_conn = 1` |
| | **`BEGIN; INSERT; ROLLBACK`** | ❌ **回滚无效（跨连接可见）** | 回滚后另一连接 `SELECT COUNT(*) WHERE id=100 -> 1`（行未撤销） |
| | `autocommit=False` 隐式事务 + ROLLBACK | ❌ 同样无效 | `id102_visible_from_other_conn = 1` |
| 错误码 | 缺表 | ✅ 1146 | `表 no_such_table_xyz 不存在` |
| | 语法错 | ✅ 1064 | — |
| | 主键重复 | ✅ 1062 | `主键重复: 1` |
| | 未知列 | ✅ 1054 | `列 no_such_col 不存在`（历史曾报 1146 且报文说表不存在 → 已修） |
| | NOT NULL 违例 | ✅ **1048** | `列 id 不允许为空`（历史文档称 NOT NULL 报 1062 → 已修） |
| | `USE` 不存在库 | ✅ 1049 | `Unknown database 'no_such_db_xyz'` |
| | `DROP TABLE` 不存在表 | ⚠️ 1105 | `表 no_such_table_xyz 不存在`（MySQL 为 1051，语义码不精确） |
| 约束 | PRIMARY KEY 列插入 NULL | ❌ 接受 | `INSERT INTO p_e2e (id,name) VALUES (NULL,'x')` 返回 OK（MySQL 应 1048）——**PK 的 NULL 校验缺失** |
| | 列级 UNIQUE / 表级 UNIQUE | ✅ | 重复插入均 1062：`Duplicate entry '1' for key 'a'`；多 NULL 可共存；`SHOW INDEX` 报 `UNIQUE-CHECK` |
| | 非唯一 KEY / FOREIGN KEY / 复合 PK | ✅ 显式 1064 | 报文明确解释不支持原因与替代做法（与 README:141 声明一致） |
| 其他 | DEFAULT 生效 | ✅ | `INSERT (id) VALUES (1)` 读回 `(1,'abc',7)` |
| | 事务 CLI 相关 `SET autocommit = 0` | ⚠️ 被 ACK 但未生效 | 服务端对任何 `SET ...` 直接回 OK（`server.py:437-439`），不维护会话变量 |
| | 中文/特殊字符 | ✅ | 见上；特殊字符、`O'Reilly`、反斜杠均正确往返 |

**事务失效根因【源码】**：`lightdb/net/server.py:458-464` 对**每条 SQL 语句**都新建 `session = {'user':..., 'database':...}` 后调用 `executor.execute(parsed, session)`；`BEGIN` 写入的 `session['txn']` 随语句结束丢弃，下一条语句在**新 session** 里执行 → 协议层事务语义整体不存在。进程内 `Executor.execute_sql` + 调用方持有 session 的路径（tests 覆盖的路径）不受影响 —— **这是「测试绿 / 真实客户端红」的典型跨层缺口**。

**覆盖边界（未验证/不适用）**：本次只测 `--mode single`；多进程模式（命名管道/`socket.share`）在受限沙箱下无法验证（【推断】与 HANDOFF 一致），`--mode multi` 未运行；Grafana/Prometheus、复制、权限（GRANT 1064）未做端到端。

---

## 5. 打包 / 入口 / 部署

### 5.1 wheel 内容（【实测】`pip wheel . -w <tmp> --no-deps --no-build-isolation`）

```
Created wheel for lightdb-studio: lightdb_studio-1.0.0-py3-none-any.whl  (449,932 B)  PIP_EXIT=0
TOPLEVEL: ['lightdb_studio', 'lightdb_studio-1.0.0.dist-info', 'web']
LIGHTDB_ENGINE_FILES: []  count=0
```

**结论：`pyproject.toml` 的 `[tool.setuptools.packages.find] include = ["lightdb_studio*", "web*"]` 确实漏掉了 `lightdb` 引擎包。** 装出来的 `lightdb-studio` 无法 import `lightdb`（`lightdb-studio-web` 的 `web/api.py:11` 只依赖 `lightdb_studio`，能跑；但服务端入口 `python -m lightdb.main` 在安装环境不可用），**`lightdb-server` 命令行入口也不存在**（README:91 与此相反）。

### 5.2 console_scripts（【实测】+【源码】）

| 入口 | 目标 | 判定 |
|---|---|---|
| `lightdb-studio` | `lightdb_studio.app:run` | ✅ 模块与 `run()` 存在（`app.py:59`） |
| `lightdb-studio-web` | `web.start:main` | ✅ 存在（`web/start.py`） |
| `lightdb-studio-cli` | `lightdb_studio.tools.cli:main` | ❌ **`lightdb_studio/tools/` 目录不存在**（`module_file_exists=False`）——与 `docs/review/00_SUMMARY.md:88` 指出的入口缺陷**至今未修** |
| `lightdb-server` | 无此 entry | ❌ README:91 声称「补上 `lightdb-server` 入口」与事实不符（`lightdb/main.py:63` 的 `prog='lightdb-server'` 只是 argparse 程序名） |

### 5.3 依赖/部署一致性（【实测】详见 `logs/packaging_check.log`）

| 检查 | 结果 |
|---|---|
| `requirements.txt` vs pyproject `dependencies` | pyproject 核心只 `pymysql`，requirements.txt 多 `psutil`、`numpy`（**不一致**；两者均被实际代码使用） |
| `requirements-dev.txt` vs `[dev]` | 一致（pytest/pytest-cov/black/ruff/httpx） |
| `requirements_web.txt` vs `[web]` | 基本一致，requirements_web 多 `websockets` |
| Dockerfile（3 个）/ compose（4 个） | 引用的 `Dockerfile`/`entrypoint.sh`/`entrypoint-v4.sh`/`config*.json` **全部存在**（按 compose 文件所在 `docker/` 解析）；v3/v4 的 `COPY --from=builder` 为多阶段构建，正常 |
| entrypoint 引用的 python 模块 | `lightdb.main`、`web.start` 存在；`lightdb.tools.*`、`lightdb.benchmark.cli` **未逐个核对**（【推断】风险点） |
| `scripts/*.sh` | 4 个 `.sh` 为 CRLF 行尾，`#!/bin/bash`；Windows 原生不可运行（需 Git Bash/WSL），`install_v4.sh`/`upgrade.sh` 同理 —— 平台属性，非缺陷 |
| `run_studio.cmd` / `run_studio.ps1` | 结构可用：cmd 转调 powershell 执行 ps1；ps1 以 `Start-Process python -m lightdb.main --mode single ... --data-dir data_demo` 起服务再 `python -m lightdb_studio.app`；`data_demo/` 存在。**未真跑**（避免安装脚本副作用） |
| CI 配置 | **不存在**：无 `.github/`、`.gitlab-ci.yml`、`.circleci`、`azure-pipelines.yml`、`Jenkinsfile`、`.travis.yml`、`tox.ini`、`setup.cfg`、`Makefile` —— 仓库无任何 CI 流水线【实测】 |
| Cython 产物 | 全仓库 `0` 个 `.pyd/.so/.c`；根目录无 `setup.py`（仅 `lightdb/cython/setup.py`）；`import Cython` → `ModuleNotFoundError` → README「从未编译」**仍成立** |

---

## 6. GUI / Web 冒烟

### 6.1 GUI（offscreen，【实测】`logs/gui_smoke.log`）

```powershell
$env:QT_QPA_PLATFORM='offscreen'; python _vb_tmp/gui_smoke.py   # 退码 0
```
- `QApplication created, platform=offscreen`；`AppConfig() constructed`；**`MainWindow constructed`**（无异常）；
- `menus=6`（文件/编辑/查询/表/工具/帮助）；主 tabs=1（查询）；右侧 tabs=6（历史/片段/AI/Agent/协作/目录）；
- `show()` → `isVisible=True`；`QTimer.singleShot(500, quit)` → 事件循环退出 `rc=0, timer_fired=True`。
- 结论：**README:90「offscreen MainWindow 可构造（6 个菜单）」仍成立**；`docs/review/00_SUMMARY.md:68` 的 `tabs=1/right_tabs=6/menus=6` 也与当前一致。
- 未构造真实数据库连接（`AppConfig` 使用默认 HOME，未触发写 `~/.lightdb_studio` 拒绝）。

### 6.2 Web（FastAPI TestClient，【实测】`logs/web_smoke.log`）

- `create_app()` 成功，`/` 返回 200（2505 B 单页）、`/docs`、`/openapi.json` 正常；`/health` → **404**，`/api/v1/health` → `200 {"ok": true, "version": "1.0.0"}`；
- `POST /api/v1/login`（admin/admin123）→ 200 返回 HMAC-JWT；`GET /api/v1/me` 带 token → 200 `{"username":"admin","role":"admin"}`；错误密码 → 401；无 token → 401；
- `GET /api/v1/connections` → `[]`；`GET /metrics` → **404**（`web/metrics.py` 是死代码，与 review 结论一致）；`GET /api/v1/databases`（无连接）→ 404 `连接 'None' 不存在`；
- 路由统计【源码】：`web/api.py` 有 **16 个** `/api/v1` 路由 + 首页 1 条 → README:22「17 个 API 端点」**可视为仍成立**（按「API 路由 16 + 页面 1」口径）。
- 说明：本机未装 `python-jose/passlib/bcrypt`，Web 认证不依赖它们，故可完整冒烟；用 TestClient 未起真实 uvicorn 端口。

---

## 7. 文档-现实一致性抽查（10 条关键断言）

| # | 文档断言（出处） | 判定 | 本次证据 |
|---|---|---|---|
| 1 | 「服务端认证校验有缺陷、没有任何客户端能登录成功；建表必然失败」（README:17-18） | **已过时（自相矛盾）** | 【实测】pymysql 空密码登录成功、`CREATE TABLE`/SELECT 正常；README:86 自身已把这两条列为「已修复」。当前**段落内同时存在过时现状与已修复声明**，读者易误解 |
| 2 | 「一次完整测试运行：268 个测试中 118 失败 / 150 通过」（README:81） | **已过时** | 【实测】761 收集 / 711 passed / 25 failed / 24 skipped / 1 xfailed。该句是审计时点快照，却仍以「一次完整测试运行的结果」的现状口径书写 |
| 3 | 「测试套件：299 通过 / 83 失败 / 1 跳过」（README:161） | **已过时** | 【实测】711/25/24+1xf；且失败数并非「±2 抖动」，本轮 25 条中 24 条隔离重跑确定性复现 |
| 4 | 「桌面端 offscreen 实测 MainWindow 可构造（6 个菜单）」（README:90 / review:68） | **仍成立** | 【实测】menus=6、tabs=1、right_tabs=6、事件循环 500ms 正常退出 |
| 5 | 「pyproject 现在会打包 `lightdb` 并补上 `lightdb-server` 入口」（README:91） | **不成立（与事实相反）** | 【实测】wheel 仅含 `lightdb_studio`/`web`/dist-info，`lightdb` 0 文件；console_scripts 无 `lightdb-server`；`lightdb-studio-cli` 指向不存在的 `lightdb_studio.tools.cli` |
| 6 | 「列级/表级 UNIQUE 真正强制；SHOW INDEX 如实报 UNIQUE-CHECK；SHOW CREATE TABLE 可往返；非唯一 KEY/FK/复合 PK 显式 1064」（README:135-144） | **仍成立** | 【实测】重复插入 1062、多 NULL 共存、`UNIQUE-CHECK`、SHOW CREATE 回放建表成功、三类不支持声明均 1064 且带说明 |
| 7 | 「BEGIN/INSERT/UPDATE/DELETE + ROLLBACK 真回滚，三种场景回滚后表内容与事务前完全一致」（README:126-130） | **仍夸大（跨层不成立）** | 【实测】经真实 pymysql：`BEGIN; INSERT; ROLLBACK` 后**另一连接仍能查到该行**（count 7→8，id100 可见）；`SET AUTOCOMMIT` 仅被 ACK。源码证据见 §4.2。该声明只对进程内 `session` 直调路径成立，对协议客户端不成立 |
| 8 | 「多进程模式已接线可用（Master/Worker、socket.share）」（README:92,146-158） | **无法验证** | 受限沙箱下命名管道/多进程不可靠（【推断】）；本次未运行 `--mode multi`，`tests/regression_v4/test_multiprocess.py::test_ipc_latency` 为负载敏感 flaky。不证伪也不证实 |
| 9 | 「SHOW CREATE TABLE / ALTER TABLE / TRUNCATE / SHOW INDEX 全部 1064」（review/00_SUMMARY:81，2026-10-13） | **已过时** | 【实测】四者全部可用：ALTER ADD/DROP COLUMN OK、TRUNCATE OK、SHOW CREATE TABLE 带默认值、SHOW INDEX 返回主键/UNIQUE 行 |
| 10 | 「SHOW COLUMNS 的 default 恒 None、extra 由主键伪造、Type 被抹掉长度；SELECT DATABASE() 返回 mysql_native_password」（HANDOFF:50-52 与 review:81） | **已过时（已修）** | 【实测】`SHOW COLUMNS`: `Type='VARCHAR(50)'`、`Default='abc'/'7'`、`Extra=''`；`SELECT DATABASE()` 返回 `None`（不再是插件名）。残留问题：`SHOW DATABASES` 仍硬编码两库、`SELECT DATABASE()` 为 None 而非 `default` |

补充抽查（不占上表名额）：
- 「Cython 编译产物 0 个 / 从未编译」（README:40-46,69）：**仍成立**【实测】0 个 `.pyd/.so/.c`、`import Cython` 失败。
- 「V4_DECISION.md 已被有意截断（44 行）」（README:72-74）：**已过时**——当前文件 **66 行**，截断标记 `原文档在此处截断，后续章节缺失` 仍在【实测】。
- 「仓库内没有任何基准产物」（README:23）：**已过时**——`benchmarks/results*.json`（2026-10-16）已入库。
- 「测试基线 34 failed / 463 passed / 11 skipped」（HANDOFF:9）：**已过时**（历史提交点快照，与当前 761 套件不可比）。
- `benchmarks/README.md`「quick 套件 15–30s、门禁 5 项」：**仍成立**【实测】测量墙钟 13.6s、总 15.3s、5/5 PASS。

---

## 8. 偏差、限制与未覆盖项（诚实声明）

1. **沙箱文件限制**：`docs/analysis_2026-10-19/**` 对本 agent 的 shell 不可写（ACL 未继承沙箱 SID）；日志经 DSH `write` 工具落盘。全量 pytest 的**完整 traceback 原文**（118 KB）保存在 `_vb_tmp/pytest_full.log` / `_vb_tmp/pytest_full_sanitized.log`，`logs/pytest_full.log` 为摘要+清单+指针（**这是本次唯一未按「原始输出整份入 logs/」交付的项**，原因是沙箱写权限 + 文本体量）。
2. **测试时长**：31:07 为多 agent 并发负载下的膨胀值，不可用于性能判断；单机空闲历史值 200–300s（文档）。
3. **失败分类主观性**：A/E/B 主类判定以「隔离日志显示的实现栈位置 + 历史 HANDOFF 分类」为准；#3/#5/#6/#10/#17/#24 存在 A/E 边界（设计目标 vs 测试期望）。
4. **skipped 24 条原因未展开**：`-q` 不打印 skip 原因，未额外跑 `-rs`（时间成本）。
5. **环境缺失**：未装 Cython（无法验证编译路径）、未装 python-jose/passlib/bcrypt（Web 用不上，但 `[web]` 依赖清单与运行时不符）、`~/.lightdb_studio` 写入受限。
6. **未跑**：`--mode multi`、full benchmark 套件、真实 uvicorn 端口、GUI 真实连库、Docker 构建（无 Docker daemon）。
7. **临时文件**：`_vb_tmp/` 保留为证据目录（含原始日志与本次自写脚本）；构建产物 `build/`、`lightdb_studio.egg-info/`、E2E 数据目录已清理。`docs/.../logs/.keep_test`、`_write_probe.txt` 为沙箱探测残留（无内容价值，因无删除权限未能清除）。

---

## 9. 附件（原始日志）

| 文件 | 内容 |
|---|---|
| `docs/analysis_2026-10-19/logs/pytest_full.log` | 全量 pytest 命令/进度/25 条 FAILED/汇总 + 完整日志指针 |
| `docs/analysis_2026-10-19/logs/pytest_rerun.log` | 25 条失败用例 `--tb=line` 隔离重跑（24 failed / 1 passed，flaky 确认） |
| `docs/analysis_2026-10-19/logs/bench_gate.log` | `benchmarks.run --suite quick --gate` 原始输出（PASS 5/5） |
| `docs/analysis_2026-10-19/logs/e2e.log` | 服务端 stdout + 3 个 pymysql 探针（主矩阵/约束/SHOW）完整原文 |
| `docs/analysis_2026-10-19/logs/packaging_check.log` | pyproject/入口/requirements/docker/scripts/CI/Cython/wheel 静态检查原文 |
| `docs/analysis_2026-10-19/logs/gui_smoke.log` | offscreen MainWindow 冒烟 |
| `docs/analysis_2026-10-19/logs/web_smoke.log` | FastAPI TestClient 冒烟（登录/JWT/端点） |
| `_vb_tmp/pytest_full.log`（工作区，非 docs） | **全量 pytest 完整原始 stdout**（UTF-16LE，118,804 B） |
| `_vb_tmp/pytest_full_sanitized.log` | 上述日志的 UTF-8 转码（完整 traceback 可读） |

---

## 10. 给 Lead 的修复优先级建议（【推断】）

1. **P0 协议事务**：`server.py` 每语句新建 session → 把 `session`/`txn` 挂到连接对象上（并真正处理 `SET AUTOCOMMIT`），否则任何真实客户端的事务都是假的。
2. **P0 打包**：`packages.find include` 加 `lightdb*`、补 `lightdb-server` 入口、删除或实现 `lightdb-studio-cli`（三处一并修，README:91 才成立）。
3. **P1 并行执行器循环导入**：启动即 WARN 回退串行（`lightdb/parallel/executor.py`），影响并行能力宣称。
4. **P1 数据正确性类**：`UPDATE SET col = col + expr`（1105）、PK 允许 NULL、虚拟表/联邦/Data API 过滤（测试 #13-15）、`readline` 顶层 import（Windows 可移植性）。
5. **P2 测试/文档**：`test_ipc_latency` 阈值放宽；README 测试基线（268/299/34）与 README:91 打包声明更新；`dashboards/`、`~/.lightdb_studio` 写权限类用例标注为环境性 skip 而不是 fail。
