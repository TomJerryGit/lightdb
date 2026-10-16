# LightDB 引擎现状评估（功能 / 性能 / 可信度）

> ⚠️ **接线口径（2026-10 阶段 2 第 6 项）**：本报告引用的 OLAP / 列存 / Cython
> 性能数字对应的能力属**实验性、未接入 SQL 执行路径、不计入性能声明**；
> 实测对照表见 [../../benchmarks/README.md](../../benchmarks/README.md)。

**评估时间**：2026-10-14 · **对象**：`D:\lightdb_project` 的 `lightdb/` 引擎本体（不含 `lightdb_studio/`、`web/`）
**方法**：静态阅读 + 隔离运行（本机实测） + 复用既有已验证文档。全程**未改动任何源码或测试**；临时脚本与临时数据目录已删除。
**环境**：Windows 10/11 · Python 3.11.9 · numpy 1.26.4 · pymysql 2.2.8 · DSH 沙箱 workspace-write（禁工作区外写、禁多进程命名管道）
**证据约定**：`【实测】`= 本次本机运行所得（命令/输出在文中或脚本中）；`【文档】`= 既有文档章节的已复核结论，未在本次重跑；`【推断】`= 从代码结构推得，未直接测量。

**基准复跑命令**（本次）：
```
python -m pytest tests -q -rs -rf -p no:cacheprovider --timeout=300   → 38 failed, 411 passed, 11 skipped, 264s
python -m pytest <6 个关键 id> -q --tb=line -p no:cacheprovider        → 6 failed（全部稳定复现）
```
38 条失败 = §9 分诊的 39 条并集 − 1 条本次翻转的 C 类（`test_v2_batch6::test_federated_governance` 本次 PASS）。
类别仍为 **A 24 / B 3 / C 2 / E 10**（`docs/FAILURE_TRIAGE.md` §9.2）。

---

## 1. 功能覆盖（逐项标注）

判定口径：**可用**=实测端到端正确且无静默错答；**部分可用**=主路径可用但有明确边界；**显式报错**=不支持但会报错（不静默错答，属可接受工程状态）；**未实现/无效**=写了但不起作用（最危险）。

| # | 能力 | 状态 | 验证依据 |
|---|---|---|---|
| 1 | DDL：CREATE/DROP TABLE、CREATE USER | **可用** | 【实测】`CREATE TABLE`、`DROP TABLE`、`CREATE USER 'bob' IDENTIFIED BY 'secret123'`→ok |
| 2 | DDL：ALTER TABLE ADD COLUMN | **未实现** | 【实测】`parser.parse('ALTER TABLE t ADD COLUMN age INT')`→`None`；分诊 #2 |
| 3 | DML：INSERT/UPDATE/DELETE、多行 VALUES | **可用** | 【实测】批量与逐行 INSERT/UPDATE/DELETE 均正确；【文档】AUDIT §17 回滚三场景 |
| 4 | DQL 基础：SELECT/WHERE/ORDER BY/LIMIT/OFFSET/IN/IS NULL/NOT | **可用** | 【实测】`LIMIT 1 OFFSET 0`、`id IN (1,2)`、`IS NULL`、投影算术全部正确 |
| 5 | WHERE 的 `BETWEEN` | **显式报错** | 【实测】`WHERE id BETWEEN 1 AND 5` → `SyntaxError: WHERE 条件错误`（解析器无该算子） |
| 6 | `COUNT(DISTINCT x)` | **显式报错** | 【实测】`SELECT COUNT(DISTINCT g) FROM s` → err 1146「缺少列」。`SELECT DISTINCT` 可用 |
| 7 | JOIN（含表别名、LEFT JOIN、JOIN+聚合） | **可用** | 【实测】`bench(20000) JOIN dim(100) + GROUP BY` 243ms，100 组正确；【文档】AUDIT §16.5/§25 |
| 8 | 子查询：IN/NOT IN/EXISTS、相关子查询、FROM 子查询、投影标量子查询 | **可用**（有边界） | 【文档】AUDIT §18/§20/§22–§25，Lead 逐项复核；仍显式报错的形态见 §24.6/§25.5 |
| 9 | CTE（含 RECURSIVE） | **可用**（上限 1000 轮） | 【文档】AUDIT §16/§19 |
| 10 | 窗口函数（ROW_NUMBER/RANK/LAG/LEAD/SUM OVER/ROWS 帧） | **部分可用** | 【文档】AUDIT §19/§20.3；`RANGE`（非默认）/`GROUPS` 帧显式报错 |
| 11 | 聚合 COUNT/SUM/AVG/MIN/MAX/TOTAL/GROUP_CONCAT/STDDEV/VARIANCE | **可用** | 【实测】`(1,5,5.0,5,5)`、`STDDEV`=0.0、空集 `AVG/SUM`→`None`、`COUNT`→0（语义正确） |
| 12 | GROUP BY（严格模式） | **可用** | 【实测】`SELECT g,v,COUNT(*) ... GROUP BY g` 被拒并提示 ONLY_FULL_GROUP_BY（`v` 非分组键）。偏差：返回 `code=None`，客户端拿不到错误码 |
| 13 | 标量函数 | **部分可用**（9 个） | 【实测】可用：`LOWER/UPPER/LENGTH/TRIM/ABS/ROUND/CONCAT/JSON_EXTRACT/JSON_UNQUOTE`；**显式报错**：`NOW/CURDATE/DATE_FORMAT/SUBSTRING/SUBSTR/REPLACE/COALESCE/IFNULL/IF/NULLIF/CAST/GREATEST/LEAST/RAND/UUID/MD5/SHA1/VERSION/DATABASE/USER/CONNECTION_ID/LAST_INSERT_ID`。`sql/functions/` 注册表有 **132** 个函数（分诊 #31），绝大多数**未接入执行器** |
| 14 | JSON 路径 | **可用** | 【实测】`JSON_EXTRACT(data,'$.name')`→`'Alice'`；`WHERE data->'$.name'='Alice'` 过滤正确（分诊 #21 已修） |
| 15 | 存储过程 / 存储函数 | **可用** | 【实测】`CREATE PROCEDURE p1() BEGIN INSERT ... END` + `CALL p1()` 真写入（`docs` 出现 `(99,'x')`）；`CREATE FUNCTION`→ok |
| 16 | 触发器 | **可用**（需 `FOR EACH ROW`） | 【实测】`CREATE TRIGGER ... FOR EACH ROW BEGIN INSERT ... END` 后插入，日志行 `(1000,'logged')` 真实落表。`FOR EACH ROW` 缺失即 `SyntaxError` |
| 17 | 事件调度器 | **部分可用** | 【文档】AUDIT §16.4、FAILURE_TRIAGE §8.3#9（`test_event_scheduler` 现 PASS）；本次未独立复测 |
| 18 | SHOW 系（TABLES/DATABASES/COLUMNS/STATS/ENGINE INNODB/AUDIT LOG/MASTER STATUS/SLAVE STATUS） | **可用** | 【实测】全部返回 `resultset` |
| 19 | CHECK TABLE / OPTIMIZE TABLE / ANALYZE TABLE | **可用** | 【实测】前两者 `resultset`，`ANALYZE TABLE docs`→`ok analysed (4 rows)` |
| 20 | EXPLAIN | **可用**（MySQL 表格形状） | 【实测】返回列 `id/select_type/table/type/key/rows/Extra`；分诊 #13/#36 的失败是**测试把 `row[0]`(int) 当计划树文本搜索**，非引擎缺陷 |
| 21 | EXPLAIN ANALYZE | **部分可用** | 分诊 §4 曾报 `'NoneType' has no attribute 'explain_row'`；本轮 38 条失败中**不含**该项，推断已修，未独立复测 = 【推断】 |
| 22 | 事务（BEGIN/COMMIT/ROLLBACK、原子性、崩溃恢复） | **可用** | 【文档】AUDIT §17.2（Lead 亲自复核：三种 ROLLBACK 后内容一致；硬杀子进程后已提交行保住，日志 `redo 1 条, undo 0 条`） |
| 23 | MVCC 快照跨重启 | **可用但有重大前提** | 【实测·本次独立复现】`R before=[[1,'old']]` → `UPDATE`+`COMMIT` → `current=[[1,'new']]`、`R still=[[1,'old']]` → `shutdown()` 重开 → `restart cur=[[1,'new']]`、**`restart snap=[[1,'old']]`**（日志 `崩溃恢复…已持久化 3 条`）。**前提**：`tests/test_mvcc_persistence.py:29-31` 明写「SQL 层 `plan_executor.SeqScan` 只看索引头那版、不走 `prev` 链，**SQL 语句读不到老快照**」——只有 Python API `Table.select(..., read_view=)` 能读历史版本。本次 SQL 层探测确认 `BEGIN` 不返回 `read_view`，**SQL 层快照读不予采信** |
| 24 | 持久化 / WAL / 崩溃恢复 | **可用**（有边界） | 【实测】关闭再开 20000 行不丢（reopen 131ms）；【文档】AUDIT §17.1 三处根因已修。**边界**：WAL 只由重启/关闭时 `truncate`；`checkpoint()`/`compact_before()` **全仓无生产调用者**（第 3 节） |
| 25 | 备份恢复 | **部分可用** | 【文档】FAILURE_TRIAGE §8.3#2 / §8.4：恢复先建表的缺陷已修，`test_batch8::test_logical_backup` 现已 PASS；本轮 38 条失败中不含该类 |
| 26 | 复制 | **部分可用** | 【文档】AUDIT §6/§15.4 + `tests`；【实测】`SHOW MASTER STATUS`/`SHOW SLAVE STATUS` 返回 resultset；分诊 #10 `__SEMI_SYNC_REGISTER__` 魔法语句（`test_semi_sync_sql`）仍 1064 = 半同步注册仍缺 |
| 27 | 权限与审计 | **显式报错 / 无效** | 【实测】`CREATE USER`→ok 但 `GRANT SELECT ON *.* TO 'bob'`→**error**；分诊 #15/#16；【文档】AUDIT §6/§15.4「权限模型未接入调用链，能登录者即全权」。审计：`_AuditSink` 默认 `DML` 级、**每条语句 `flush()` 落盘**且无轮转（第 3 节） |
| 28 | 分区表 | **未实现（语法被接受）** | 【实测】`CREATE TABLE pt (...) PARTITION BY HASH(id) PARTITIONS 4`→`ok`（**假成功**）；分诊 #15 `AttributeError: 'Table' object has no attribute '_partition'` |
| 29 | 全文索引 | **可用** | 【实测】`CREATE FULLTEXT INDEX ft ON docs(body)`→ok；`MATCH(body) AGAINST('hello')`→正确命中 `(1,'hello world')`；`test_batch13.py::test_fulltext`/`test_json_with_fulltext` 本轮**通过**（不在 38 条失败内） |
| 30 | 生成列 | **可用** | 【实测】`up VARCHAR(20) GENERATED ALWAYS AS (UPPER(name)) STORED` → 插入 `'alice'` 读回 `'ALICE'`；`test_batch13.py::test_generated_column` 本轮**通过** |
| 31 | OLAP 向量化 | **不可信** | 【实测】`VectorizedAggregate(10 万行).SUM` → **704982704.0**（应为 4999950000）；且 `COUNT([1,None,3])`=3（应 2）、`SUM/AVG/MIN/MAX` 遇 NULL 直接抛 `TypeError`（§2、§4） |
| 32 | 并行查询 `Executor.parallel` | **部分可用（仅单表裸 COUNT）** | 【实测】`COUNT(*)` over 30000 行 526ms，`parallel.stats={'parallel_queries':1,'fragments_executed':2}`；同表 `COUNT(*) WHERE v>10` 4.77s、`SUM(v)` 5.57s，**parallel 计数不增**。源码 `executor.py:822` 只在 `_is_plain_count` 命中时调 `_parallel_count`；`plan_executor.py` **零** `parallel` 引用 |
| 33 | 多进程架构 | **本机不可验证** | 【实测】`python -m lightdb.main --mode multi --workers 4` → `PermissionError [WinError 5]` 于 `mp.Queue()`（沙箱禁命名管道），服务未监听。【文档】AUDIT §15.3 接线后可用（Windows `socket.share` 交接句柄），Linux 走 `SO_REUSEPORT` 本机未验证 |
| 34 | 单进程协议端到端 | **可用** | 【实测】真实 pymysql 连接成功（`5.7.30-LightDB`），CREATE/INSERT/SELECT/COUNT 全通 |

**统计**（34 项）：**可用 18**、**部分可用 8**、**显式报错 3**（BETWEEN、COUNT DISTINCT、GRANT）、**未实现/无效 5**（ALTER ADD COLUMN、CHECK 约束、分区、向量化聚合、审计轮转/权限接线中的「未接线」面）。
**最关键的缺口**：① 向量化与批处理两条「性能卖点」路径**返回错值**；② CHECK 约束**声明成功但不校验**；③ 权限/GRANT 未实现而登录即全权；④ 标量函数只接了 9/132；⑤ `ALTER TABLE ADD COLUMN`、`BETWEEN`、`COUNT(DISTINCT)` 这类入门语法缺失。

---

## 2. 性能实测（本机量级）

全部数字为**本机单次运行**（Windows、Python 3.11、单进程、无并发负载），仅代表量级。2.1 节走进程内 Python API（不含协议解析与网络），2.2 节走真实 TCP。

### 2.1 行存 OLTP（`_bench_engine.py`，表 `bench(id PK, grp, val, name)`）

| 操作 | 规模 | 结果 |
|---|---|---|
| 逐行 INSERT | 2000 行 | 1.33s → **1,501 rows/s**（0.67 ms/语句） |
| 批量 INSERT（500 行/语句） | 20000 行 | 14.75s → **1,356 rows/s**（**批量不更快**） |
| 点查（PK `=`） | 2000 次 | 2.25s → **888 q/s，1.13 ms/次** |
| 范围扫描（`id>=100 AND id<=20000`） | 19,900 行 | 4.15s → **4,793 rows/s**（≈0.21 ms/行） |
| GROUP BY（100 组） | 20000 行 | 1.82s → **10,988 rows/s** |
| 裸 `COUNT(*)+SUM` | 20000 行 | 1.82s → **10,964 rows/s** |
| `ORDER BY val DESC LIMIT 100` | 20000 行 | 3.21s → **6,239 rows/s**（全表物化 + 排序，非 top-N） |
| JOIN `bench JOIN dim` + GROUP BY | 20000×100 | **243.6 ms**，100 组 |
| 关闭重开（WAL 恢复） | 20000 行 | **131 ms**，行数 20000 不变 |

**判读**：写路径 ~1.5k 行/s 是**逐语句固定开销**主导（WAL flush + 审计 `flush()` + 锁 + JSON 编解码），与批量无关；读路径聚合/范围扫描都是 ~5k–11k 行/s 的 Python 逐行量级；点查 888 q/s 与白皮书目标 12,000 QPS 相差 **13.5 倍**（`docs/PERFORMANCE_WHITEPAPER_v4.md:37`，该数字文档自身已标为「设计目标」）。

### 2.2 协议层（真实 pymysql，单进程 `--mode single`，端口 8899）

| 场景 | 结果 |
|---|---|
| 500 条单行 INSERT | **1,254 stmt/s** |
| 4500 行批量 INSERT | **2,487 rows/s** |
| 300 次点查 | **1,138 q/s（0.88 ms）** |
| 1 客户端并发点查 | 1,163 q/s |
| 4 客户端并发点查 | **925 q/s**（聚合，反而 −20%） |
| 8 客户端并发点查 | **749 q/s**（聚合，−36%） |

**判读**：协议层单语句吞吐与进程内一致；**并发不扩展而是回退**（多客户端争用同一 Executor 与 GIL）。

### 2.3 OLAP 向量化 vs 行式（`_probe_olap.py`，10 万行 0..99999，int32 列存）

| 路径 | 耗时 | 吞吐 | 结果 |
|---|---|---|---|
| `VectorizedAggregate.SUM`（NumPy） | 11.6–24.2 ms | **4.1M–8.6M rows/s** | **704982704.0 ≠ 4999950000（错值）**，`RuntimeWarning: overflow encountered in scalar add` |
| 纯 Python `scan`+`sum` | 10.9–37.2 ms | 2.7M–9.2M rows/s | **4999950000（正确）**，`speedup vect vs python = 0.93x`（另一次 0.86x） |

**根因（实测确认）**：`VectorizedScan.next_batch` 用 `np.array(values)`，Windows 上 `np.array(list[int])` 默认 **int32**（实测 `batch col type = ndarray dtype = int32`），10 万行和 4.99e9 溢出 int32，而 `_execute_numpy` 在 `vectorized_engine.py:201` 又做一次 `float(sum(results))`（标量 Python 累加，二次告警）。**该路径不仅慢于朴素 Python，而且返回错值**。
**额外失效**（同一实例）：`COUNT` 把 NULL 计入（`[1,None,3]`→3，应 2）；`SUM/AVG/MIN/MAX` 含 NULL 时抛 `TypeError`（`np.array([1,None,3])` 变 object dtype）；`MIN/MAX` 返回 float 而非整数；`OLAPOptimizer` 在没有任何聚合别名时构造 `VectorizedGroupBy`，`HybridTable.olap_aggregate({'aggregations':[{'func':'SUM',...}]})` 直接 `KeyError: 'alias'`。
**可达性**：`Executor`/`plan_executor` 源码中 `HybridTable` **0 命中**、`VectorizedAggregate` **0 命中**、`olap` **0 命中**；`plan_executor` 中 `ColumnStore`/`Vectorized`/`parallel` 均 **0 命中**。即**整条向量化 OLAP 链路不在 SQL 执行路径上**，`docs/PERFORMANCE_WHITEPAPER_v4.md` 的 3.5M rows/s / 85M rows/s / 41x 与本次实测的查询路径无关。

### 2.4 批处理执行器（`_probe_olap.py`，5 万行 `pred = age>50 and score<50000`，期望 16,317 行）

| 路径 | 行数 | 耗时 |
|---|---|---|
| 逐行 Python 谓词 | 16,317 | 3.7–19.1 ms |
| `BatchPipeline` + 两个 `VectorizedFilter` | **50,000（错）** | 8.3–28.6 ms（**0.44x–0.67x**） |
| 直接串联 `VectorizedFilter` 节点 | **50,000（错）** | 7.0–28.9 ms |

**根因（实测确认）**：`BatchScan.next_batch()` 返回 `Batch([], rows)` —— **`columns` 恒为空列表**（实测 `BatchScan.columns = []`），而 `VectorizedFilter.next_batch` 第一件事是 `if self.column not in batch.columns: return batch`（`batch_executor.py:122-123`）⇒ **向量化过滤永远是空操作**。因此分诊 #28「`assert 50000 == 16317`」不是条件传递问题，而是**整条向量化过滤从未生效**。可信度：**该路径完全不可信（静默返回全量行）**。

### 2.5 并行路径

`COUNT(*)` over 30000 行：**526 ms**，`fragments_executed=2`（真走了 `ParallelExecutor`）；同一表 `COUNT(*) WHERE v>10`：**4,774 ms**（≈9 倍慢，串行路径）；`SUM(v)`：**5,568 ms**。`tests/regression_v4/test_performance_v4.py::test_parallel_speedup` 隔离运行 **0.49x**（串行 128ms / 并行 261ms）。结论：**并行只覆盖单表裸 `COUNT`；且即使在覆盖范围内也是负加速**（`ThreadPoolExecutor` + GIL，`parallel/executor.py:50`），与分诊 §9.9 一致。

### 2.6 内存 / 文件增长（`_probe_vac.py`、服务端目录观测）

| 观测 | 数据 |
|---|---|
| `bench.dat` 20,000 行（4 列） | **1,499,136 B ≈ 75 B/行** |
| WAL 运行期增长（服务端 13,001 行） | `net.wal` **2,595,814 B ≈ 200 B/行**，随批次单调增长，**无运行期回收** |
| WAL 关闭后 | `net.wal` = **0 B**（关闭/重启时 `truncate`） |
| `audit.log` | 进程内 22,000 条语句 → **350 KB**；服务端 502 条 → 77 KB；**无轮转、无上限** |
| 10 轮 × 2000 行 UPDATE（单行值 40 字符） | 669 KB → **16,020,009 B（16.0 MB）**，即 ≈800 B/更新 |
| `Database.vacuum()`（空闲库） | **回收 0 个版本**；文件 **16,020,009 B → 16,020,009 B（不变）** |
| 随后 `shutdown()` | 16.0 MB → **1,711,430 B（1.7 MB）** |

**判读**：① 版本链只增不减，未 vacuum 时 UPDATE 使文件按「行数×更新次数×行长」膨胀，回溯代价 O(版本数)；② `vacuum()` 在本机空闲库上回收 0 —— 与 `test_mvcc_persistence.py:27-28` 的说明一致（horizon 取 `oldest_active_txn()`），但**实测并未让文件变小**；③ **文件变小只发生在 `shutdown()` 的全量重写**（进程被强杀即丢失这一压缩）；④ WAL 与审计日志都是**无轮转**的单向增长文件。

---

## 3. 架构层面的制约

| # | 制约 | 影响面 | 依据 |
|---|---|---|---|
| 1 | **无运行期 checkpoint / WAL 轮转**：`WAL.checkpoint()`、`compact_before()` 全仓**无生产调用者**（仅 `wal.py` 自身定义），`Table` 只在启动与关闭调 `wal.truncate()` | 运行期 WAL 单调增长（实测 ≈200 B/行）；长跑实例磁盘无界、恢复时间随 WAL 线性增长 | 【实测】+ grep 全仓 |
| 2 | **审计每条语句 `flush()` 且无轮转**：`_AuditSink` 默认级别 `DML`（`executor.py:117`），`AuditLogger.record` 写一行即 `flush()` | 写路径每条 DML 一次 fsync 级系统调用，直接压低插入吞吐；`audit.log` 无界增长（实测 22k 语句 350 KB） | 【实测】+ `security/audit.py:54-55` |
| 3 | **单线程执行**：SQL 只在 Master/单进程的单一 Executor 串行执行；无并发执行 | 8 客户端并发点查 1,163→**749 q/s（−36%）**；多进程只多核化「连接与协议」，不是查询 CPU | 【实测】+ 【文档】AUDIT §15.4 |
| 4 | **向量化 / 列存层与 SQL 执行器完全未接线**（`executor.py` 对 `HybridTable`/`VectorizedAggregate`/`olap` 零引用；`plan_executor` 对 `ColumnStore`/`Vectorized`/`parallel` 零引用） | 白皮书的 OLAP 性能宣称在查询路径上无落点；列存 `scan` 只读内存解码副本、从不读落盘数据，`compression_ratio` 由 `len(values)*10` 估算（`columnar/column_store.py:251-263`） | 【实测】源码命中计数 |
| 5 | **`BatchScan` 不携带列名**（`Batch([], rows)`），`VectorizedFilter` 靠列名查表 | 整条批处理/向量化过滤链路静默失效（返回全量行） | 【实测】 |
| 6 | **版本链无页内紧凑、回溯 O(版本数)**：`prev_page/prev_slot` 逐版本串联，`vacuum` 只释放槽位不重排页 | UPDATE 密集表文件膨胀（实测 800 B/更新，16 MB/2000 行×10 轮）；历史读越旧越慢 | 【实测】+ 【文档】AUDIT §21.4 |
| 7 | **页/行格式为 Python 对象 + JSON 载荷**（`row_format` 主链路 `json.dumps`，见 AUDIT §7），Cython 从未编译（本机 `*.pyd/*.so/*.c` 计数 = **0**，6 个 `.pyx` 仅源码，`status()['any_accelerated']=False`） | 编解码是逐语句固定开销的主要来源；`BinaryRowFormatV4` 的 3–4x 宣称无落点 | 【实测】文件存在性 + 【文档】AUDIT §18.3 |
| 8 | **`ORDER BY` 无 top-N**：全表物化后排序 | `ORDER BY ... LIMIT 100` 于 20k 行耗 3.2s（6,239 rows/s），与结果集大小无关 | 【实测】 |
| 9 | **错误码语义缺口**：部分路径返回 `code=None`（如严格 GROUP BY 拒绝） | 客户端无法按码分支处理；与「错误不再伪装成功」的修复方向相反 | 【实测】 |
| 10 | **多进程为「Master 独占存储 + Worker 只转发协议」**（AUDIT §15.2） | 查询 CPU 无法多核扩展；跨 Worker 元数据最多 5s 延迟、`SHOW PROCESSLIST`/事务状态不汇总 | 【文档】AUDIT §15.4 |

---

## 4. 可信度评估（结合 38 条失败中的 24 条 A 类）

### 4.1 目前不可信（按风险排序）

| 风险 | 能力 | 证据 | 危害 |
|---|---|---|---|
| P0 | **向量化聚合返回错值** | 【实测】`SUM` 10 万行 → `704982704.0`（应 4999950000）+ `overflow encountered in scalar add`；A 类 #38/#39（`test_performance_vs_python`、`test_vectorized_aggregate` 隔离稳定失败） | 静默错值。**当前不在 SQL 路径上**，故尚未污染 SQL 结果；一旦接线即成数据正确性事故 |
| P0 | **批处理 `VectorizedFilter` 不生效，返回全量行** | 【实测】5 万行 → 50,000 行（应 16,317）；根因 `BatchScan.columns=[]`；A 类 #28 | 静默错值（过滤条件被吞） |
| P0 | **CHECK 约束完全不生效** | 【实测】`CREATE TABLE ck (... CHECK(price>0))` → `ok`；`INSERT ... VALUES (2,-5)` → `ok`，表内实存 `[(1,100),(2,-5)]`；A 类 #9（`test_check_constraint`） | 非法数据可入库；且有「约束已生效」的假象 |
| P1 | **权限模型未接入；GRANT 未实现** | 【实测】`CREATE USER` ok 但 `GRANT` → error；【文档】AUDIT §6/§15.4「能登录者即全权」；A 类 #15/#16 | 无访问控制；`test_privilege_enforcement` 的「弱口令被拒」断言是**假阳性**（任何 CREATE USER 都因后续 GRANT 失败） |
| P1 | **分区表语法被接受但不生效** | 【实测】`PARTITION BY HASH(...) PARTITIONS 4` → `ok`；A 类 #15（`Table._partition` 不存在） | 假成功：用户以为分区裁剪生效 |
| P1 | **列存 GROUP BY 返回 0 行** | A 类 #5（`assert 0 == 100000`，隔离稳定） | 静默空结果 |
| P1 | **SQL 层读不到历史快照** | 【实测】`BEGIN` 不返回 `read_view`，无历史快照语法；【文档】`tests/test_mvcc_persistence.py:29-31` 明写 SQL `SeqScan` 不走 `prev` 链（仅 Python API 可用） | 「快照隔离跨重启」只能经 Python API 验证；SQL 用户无法使用 |
| P2 | `ALTER TABLE ADD COLUMN` / `BETWEEN` / `COUNT(DISTINCT)` 缺失 | 【实测】三者分别 `None` / `SyntaxError` / err 1146；A 类 #2、#6 | 入门语法缺口（显式报错，可绕行） |
| P2 | 132 个注册函数中仅 9 个可用 | 【实测】`NOW/DATE_FORMAT/SUBSTRING/COALESCE/IF/CAST` 等均报「不支持」；A 类 #31 | 兼容性承诺与实现脱节（显式报错） |
| P2 | 性能结论类测试无判别力 | 4 条纯计时/吞吐断言（分诊 §9.9）；`test_row_format_performance` 本轮抖动 | 性能回归无法靠测试发现 |
| P3 | 旧 `.dat` 数据文件不兼容（行头 13→21 B） | 【文档】AUDIT §21.4 | 升级需重建库 |

### 4.2 「可用但有前提」清单

| 能力 | 前提 |
|---|---|
| MVCC 快照跨重启 | 只用 Python API `Table.select(..., read_view=)`（本次已独立复现「重启后重建老快照仍读到 `old`」）；SQL 层读不到历史版本；且**不能对要读的版本做 vacuum** |
| 事务与崩溃恢复 | 不要跳过 `shutdown()`：**页压缩与 WAL 截断都发生在 shutdown**（实测 16 MB → 1.7 MB 仅由 shutdown 触发） |
| 触发器 | 必须写 `FOR EACH ROW` |
| 子查询 / 窗口 / CTE | 按 §24.6、§25.5 的显式报错清单绕行（行值 IN、HAVING 引用非分组键、`RANGE`/`GROUPS` 帧、外层引用藏在函数/算式里等） |
| 多进程模式 | 本机（沙箱）无法启动（WinError 5 禁命名管道）；且多进程≠查询加速；跨 Worker 元数据有 ≤5s 延迟、`SHOW PROCESSLIST` 不汇总 |
| 点查性能 | 888 q/s（进程内）/ 1,138 q/s（TCP）是**单客户端**数字；并发上升即回退 |
| 全文索引 / 生成列 | 本次单表小样本实测正确，未做规模与并发验证 |
| 聚合函数 | `AVG/SUM` 空集返回 `NULL` 正确；但 `COUNT(DISTINCT)` 不可用 |

### 4.3 测试套件可信度

- **本轮复跑**：`38 failed, 411 passed, 11 skipped`（264s）。与分诊 §9 的 39 条并集差 1 条（`test_federated_governance` 本次 PASS，属 C 类时钟竞态）。
- A 类 24 条中，本次**独立复跑 6 条全部稳定失败**：`test_vectorized_aggregate`、`test_batch_executor`、`test_check_constraint`、`test_olap_groupby`、`test_ddl`、`test_parallel_speedup`（0.49x）。
- 10 条失败是**测试自身写错**（E 类，分诊 §9.7），不代表引擎缺陷；4 条纯计时/吞吐断言在共享机器上无判别力（§9.9）。**性能类门禁当前不可作为判据**。
- 11 条 skip 全部是多进程原语探测（沙箱 `WinError 5`），**不是通过**，多进程能力在本机始终未被验证。

---

## 5. 一页结论

- **能用的**：单机进程内/协议端的 CRUD、JOIN、CTE、窗口、子查询、GROUP BY（严格）、UNION、JSON 路径、全文索引、生成列、存储过程/函数/触发器、SHOW 系、EXPLAIN、事务与崩溃恢复、MVCC（经 Python API）。量级：**写 ~1.5k 行/s、点查 ~0.9k q/s、聚合/扫描 ~5k–11k 行/s、JOIN 20k×100 约 0.24s**。
- **不能信的**：向量化聚合（错值）、批处理过滤（不生效）、CHECK 约束（无效）、权限/GRANT（未实现）、分区（假成功）、列存 GROUP BY（0 行）。这些**静默给出错误结果**，优先级高于任何性能优化。
- **最制约的三件事**：① 无 checkpoint/WAL 轮转 + 审计逐语句 flush（文件与延迟双增长）；② 单线程执行 + 线程池 GIL（并发回退 36%）；③ OLAP/列存/Cython 三层均未接入 SQL 执行路径（性能宣称无落点）。
- 与文档的偏差需按 `docs/PERFORMANCE_WHITEPAPER_v4.md` 卷首的「数据可信度声明」处理：该文档数字为**设计目标**，本次实测未复现任何一项。

---

## 6. 阶段 0 收尾后的更新（2026-10 追加，上文保持评估时原貌）

- **性能数字的落点不变**：`lightdb/olap/**`、`lightdb/columnar/**`、
  `lightdb/parallel/**` 在 `Executor` / `plan_executor` 里仍是**零引用**，
  `lightdb/cython/*.pyx` 的编译产物仍是 **0 个**。因此 §2.3 第 3 条
  （"OLAP / 列存 / Cython 三层全部未接入 SQL 执行路径"）与本文引用的
  白皮书数字（3.5M / 85M rows/s、41x、12.5x）**继续有效**，仍是设计目标。
  本文 §2.2 的实测数字是当前**唯一**可复现的性能基线：逐行 INSERT
  **1,501 rows/s**、批量 INSERT **1,356 rows/s**（批量反而更慢）、点查
  **888 q/s** 进程内（1.13 ms）/ **1,138 q/s** TCP（0.88 ms）、并发 1→4→8
  客户端 1,163→925→**749 q/s（−36%）**、WAL **≈200 B/行**、
  10 轮 × 2000 行 UPDATE 后 `.dat` **669 KB→16 MB**（≈800 B/更新）。
- **已修复（阶段 0）**：向量化聚合 `int32` 溢出错值、`VectorizedFilter`
  批间条件失效、`CHECK` 约束完全不生效、联邦策略 id 撞号。其中 CHECK
  额外补齐了**备份/恢复路径**：`TableSchema.canonical_ddl`（`SHOW CREATE
  TABLE` 与逻辑 dump 共用的唯一 DDL 实现）现在导出列级内联 CHECK 与表级
  `CONSTRAINT <name> CHECK (...)`，dump 里新增 `-- lightdb:check` /
  `-- lightdb:checks` 哨兵，恢复端读回逐条对账（缺失 → `RestoreError`）。
  **向量化聚合/过滤的两个错值仍处于"未接线"状态** —— 修的是算法，
  没有接线，所以既不会在生产路径出错，也**没有**任何性能收益。
