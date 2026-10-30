# 多进程只读查询分片原型（MULTIPROCESS_SHARDING）

> P4 backlog 交付（verify-bench）。目标：验证"**多进程能否绕过 Python GIL +
> 表级锁**，让只读范围查询真正并行"，并给出量化边界与一致性说明。
>
> 代码：`lightdb/net/sharding.py`（原型/API）、`scripts/shard_query.py`（CLI +
> 基准）、`tests/test_multiprocess_sharding.py`（26 用例）。
> 测量证据：`_analysis_tmp/sharding_bench.json`（含 10k/100k 行、1/2/4/8 worker、
> 进程内线程对照、各阶段耗时）。

---

## 0. 结论摘要

* 多进程分片**能突破 GIL**：100k 行、**复用快照**时，4 worker 端到端
  **12.79s vs 单进程 36.51s = 2.85x**（2 worker 2.48x、1 worker 1.78x）；
  只计查询执行段（worker 各自扫描自己的主键区间）4 worker 对单进程查询
  **3.63x**。进程内多线程对照：2 线程 0.96x、4 线程 0.77x、8 线程 0.47x
  ——**确认线程不扩展、多进程是绕过 GIL 的方向**。
* **快照开销是端到端收益的分水岭**：现场快照一次 10k 行 ≈ 8.9s、100k 行
  ≈ 42.9s（含 COUNT/样本查询/SHA 校验，本机并行负载下）；100k 每查询新做
  快照时 4 worker 仅 **1.09x**，10k 则只有 **0.20–0.29x**（被快照+进程启动
  开销吃掉）。**复用快照后** 100k 才体现 2.48–2.85x。
* 10k 规模即使复用快照也只有 **0.66x**（查询 ~4.2s，固定开销 ~2s+），说明
  该原型只适合"大结果集 + 可复用快照/只读副本"场景，不适合小查询默认入口。
* **一致性 = 快照时刻**：master 用 `lightdb.backup.snapshot.create_snapshot()`
  在 checkpoint（WAL=0）后生成快照；每个 worker **独占一份快照 data/ 拷贝**
  后再打开（引擎当前不支持同一 data-dir 多进程只读打开），分片结果按主键
  归并排序。运行期间**只读、无事务、无写**。
* 仅支持 `SELECT ... FROM 单表 WHERE <整数单列主键> BETWEEN a AND b`；
  JOIN/聚合/GROUP BY/子查询/ORDER BY/LIMIT/非主键谓词等一律**显式 1105**，
  绝不静默降级成错误答案。worker 崩溃/超时/执行失败显式报错。
* 该原型**未接入 MySQL 协议/服务端路由**（`lightdb/net/server.py` 未改），
  属于可行性验证 + 后续架构决策依据，不是生产查询入口。

---

## 1. 方案架构

```
master 进程
  parse/validate SQL（AST；拒绝不支持形态 1105）
  split pk 区间 [lo,hi] → N 个互不重叠子区间
  create_snapshot(data_dir)  ← checkpoint 后、WAL=0、无写入
  ├─ worker 0: copy(snapshot/data) → 独立目录 → Database(只读使用) → SELECT 子区间
  ├─ worker 1: ...
  └─ worker N-1
  merge：按主键列排序（分片区间互斥，无需去重），返回 columns/rows
```

**为什么用子进程而不是 multiprocessing**：本机沙箱/Windows 下
`multiprocessing.Pipe/Queue` 依赖命名管道，受限会话会被拒绝（WinError 5）；
原型改用 `subprocess.Popen([sys.executable, '-c', bootstrap, task, result])`
启动**独立 Python 进程**，task/result 用 pickle 文件交换（worker 原子写
`*.tmp → rename`）。这同样绕开 GIL（每 worker 独立解释器/独立 GIL），
并把"进程启动 + IPC"作为可测量开销暴露出来。

**为什么每 worker 复制快照**：`Database.__init__` 会建目录、读写 `_meta.json`、
可能做 WAL 恢复；多个进程共享同一 data-dir 的只读语义引擎并不支持。
快照 `data/` 复制到每 worker 独立临时目录后打开，避免 WAL/元数据并发写冲突
（打开副本可能做少量恢复写，但只写自己的副本）。复制量 ≈ 表文件总量，
实测 ≤ 数百 ms（10k/100k 规模）。

**范围切分**：闭区间 `[lo, hi]` 均分为至多 N 段（`hi<lo` → 空区间立即返回，
不做快照；N 大于区间行数时自动截断为行数段）。分片 SQL 由 master 从 AST
重建：`SELECT <列> FROM <表> WHERE <pk> BETWEEN <shard_lo> AND <shard_hi>`。

---

## 2. 一致性边界（重要）

| 项 | 说明 |
|---|---|
| 一致性时刻 | 快照生成时（`create_snapshot` 内部先 `db.checkpoint()`，WAL 必须为 0；非零直接拒绝） |
| 写假设 | **运行期间 master/worker 都不写原 data-dir**；有并发写则快照可能不一致（本原型不负责在线 quiesce；在线热备另见 `docs/SNAPSHOT_BACKUP.md`） |
| 每 worker 数据 | 独立 `snapshot/data/` 拷贝（打开后只做 SELECT；引擎若有恢复/元数据写也只落在副本） |
| 快照复用 | `execute_sharded(..., snapshot_dir=...)` 可复用同一快照（benchmark 中"复用快照"行即此路径）；复用期间源数据若变化，快照仍代表旧时刻 |
| 结果正确性 | 分片区间互斥且并集 = 请求区间 → 行集合 = 原查询结果；master 按主键排序，与单进程基线**逐行一致**（测试覆盖乱序/边界/列子集） |
| 事务 | 不支持：查询在每 worker 独立 autocommit 连接上执行，无跨分片事务/MVCC 快照协调 |
| DDL/写 | 不支持：执行前数据目录必须已 checkpoint 且静默 |

**开销是被刻意暴露的**：快照一次（含 COUNT/样本查询/SHA256 校验）+ 每 worker
复制 data/ + 进程启动（Python 解释器 + lightdb 导入）+ pickle 回传 + 归并；
§6 给出各阶段实测。快照可复用时应复用（bench 中"复用快照"路径），否则每次
查询都付快照成本——这正是它目前**不适合作为每查询默认入口**的原因。

---

## 3. 支持 / 不支持形态

### 支持
```sql
SELECT * FROM t WHERE id BETWEEN 100 AND 900;
SELECT id, v FROM t WHERE id BETWEEN 1 AND 1000;
-- 表：单表；主键：单列整数；谓词：仅 BETWEEN 字面量边界（可含主键列在 SELECT 中）
```

### 不支持（显式 `1105` / HY000，`UnsupportedShardQuery`）
| 形态 | 原因 |
|---|---|
| JOIN / 子查询 / UNION / CTE | 需要跨表/跨查询规划，本原型只做单表分片 |
| 聚合（COUNT/SUM/…）/ GROUP BY / HAVING | 分片后聚合需二次归并，本原型未做 |
| DISTINCT / 窗口函数 | 同上，需全局语义 |
| ORDER BY / LIMIT / OFFSET | 全局排序/限行与分片合并语义冲突（结果统一按主键升序） |
| 非 BETWEEN 谓词（`>`, `IN`, `OR`, `AND` 组合…） | 无法保证按主键连续切片 |
| 非整数主键 / 复合主键 / 无主键 | 不能按数值区间切分或无法按 key 合并 |
| SELECT 不含主键列 | 无法按 key 归并（例如 `SELECT v ...`） |
| UPDATE/DELETE/DDL、多语句 | 只读原型 |
| 表不存在/数据目录无效 | 显式 `ShardingError`（同样 1105） |

worker 侧失败同样显式：崩溃（exitcode≠0）、超时、执行错误分别抛
`ShardWorkerError`（含 worker 序号/区间/exitcode/错误文本），**不会返回部分
结果**。

---

## 4. API 与 CLI

### Python API（`lightdb/net/sharding.py`）
```python
from lightdb.net.sharding import execute_sharded, execute_single, execute_threads

r = execute_sharded(
    'SELECT id, v FROM t WHERE id BETWEEN 1 AND 100000',
    data_dir='D:/data/db',      # 已 checkpoint、无写入
    workers=4,                   # 期望分片数（会被范围行数截断）
    timeout_s=120,               # 单 worker 等待总超时（超时显式）
    snapshot_dir=None,           # 复用已有快照根（含 manifest.json + data/）
)
print(r['row_count'], r['timing'], r['consistency'])
```

返回字段：`columns` / `rows` / `row_count` / `shards`（各片 lo/hi/rows/query_s）/
`timing`（parse/snapshot/copy/spawn/exec/merge/total）/ `worker_query_s_max,sum`/
`worker_open_s_total`/`snapshot`（wal_zero/row_total/bytes）/ `consistency`/
`worker_pids` / `worker_errors`。

### CLI
```powershell
# 单次分片查询（可选单进程基线对比）
python scripts/shard_query.py --data-dir D:/data/db `
    --sql "SELECT id, v FROM t WHERE id BETWEEN 1 AND 1000" --workers 4 --baseline

# 量化基准（10k/100k；1/2/4/8 worker；附线程对照与快照复用）
python scripts/shard_query.py --bench --rows 10000,100000 `
    --workers 1,2,4,8 --repeats 1 --threads --reuse-snapshot `
    --json _analysis_tmp/sharding_bench.json
```

---

## 5. 测试覆盖

`tests/test_multiprocess_sharding.py`（26 用例）：

* **逐行一致**：与 `execute_single` 基线比较（1/2/4 worker、`*` 与列子集、
  多进程 vs 进程内线程 vs 单进程三方一致）；
* **边界**：空表、单行、`lo==hi`、跨片全量、1/2000 边界、workers>行数、
  `lo>hi` 空区间短路（不生成快照）；
* **不支持形态**：13 种参数化（JOIN/聚合/无 WHERE/非主键/浮点边界/
  ORDER BY/LIMIT/DISTINCT/表达式/子查询/UPDATE/不选主键）+ 复合主键/无主键/
  表不存在/坏数据目录，全部断言 `code=1105` 且消息含"不支持的分片查询"；
* **worker 失败**：注入崩溃（exitcode=7）、超时（1s vs sleep 8）、执行异常，
  断言 `ShardWorkerError` 且信息可用于排障。

运行：`python -m pytest tests/test_multiprocess_sharding.py -q`。

---

## 6. 量化结果（10k / 100k 行）

> 测量环境：Windows 10 / Python 3.11.9 / 8 逻辑核；本机当时有其他 agent 并行
> 任务（绝对数字偏慢，且不同轮次会漂移；单进程基线 100k 行查询 36.5s 明显慢于
> 静息基线，属当前负载下的相对对照）；每项 `--repeats 1`（受总时长限制）。
> 结论请看**相对关系**与各阶段开销，而不是绝对秒数。

### 6.1 10k 行（`SELECT id, v FROM t WHERE id BETWEEN 1 AND 10000`）

单进程基线：**4.177s**（10,000 行）。快照现场准备一次：**8.915s**。

| 模式 | 1 worker | 2 worker | 4 worker | 8 worker |
|---|---:|---:|---:|---:|
| 进程内线程（总耗时/加速） | 3.898s / 1.07x | 4.960s / 0.84x | 8.080s / 0.52x | 11.153s / 0.37x |
| 多进程·每次新快照 | 15.467s / 0.27x | 14.524s / 0.29x | 16.902s / 0.25x | 21.300s / 0.20x |
| 多进程·**复用快照** | 6.319s / 0.66x | 6.303s / 0.66x | 9.299s / 0.45x | 10.320s / 0.40x |
| 查询段加速（复用快照，worker 查询 max vs 单进程） | 0.98x | 1.02x | 0.83x | 0.75x |

复用快照时的阶段耗时（1/4 worker）：snapshot 0；copy **0.05/0.23s**；
spawn **0.10/0.70s**；exec **6.15/8.34s**；merge ≈0.01s。

**判读**：10k 查询本身只有 ~4.2s，而每次新快照要 ~8.9s，端到端必然更慢；
即使复用快照，进程启动 + 每 worker 复制 + 结果回传的固定开销（~2s）仍超过
并行查询省下的时间（尤其本机负载让 worker 查询变慢）。**10k 不适合分片**。

### 6.2 100k 行（`SELECT id, v FROM t WHERE id BETWEEN 1 AND 100000`）

单进程基线：**36.511s**（100,000 行）。快照现场准备一次：**42.882s**。

| 模式 | 1 worker | 2 worker | 4 worker | 8 worker |
|---|---:|---:|---:|---:|
| 进程内线程（总耗时/加速） | 44.475s / 0.82x | 37.899s / 0.96x | 47.295s / 0.77x | 77.571s / 0.47x |
| 多进程·每次新快照 | 47.609s / 0.77x | 37.964s / 0.96x | 33.582s / 1.09x | 48.936s / 0.75x |
| 多进程·**复用快照** | 20.496s / 1.78x | 14.694s / **2.48x** | 12.791s / **2.85x** | 17.549s / 2.08x |
| 查询段加速（复用快照，worker 查询 max vs 单进程） | 2.04x | 3.03x | **3.63x** | 2.67x |

复用快照时的阶段耗时（1/4/8 worker）：snapshot 0；copy **0.04/0.11/0.20s**；
spawn **0.01/0.07/0.16s**；exec **20.41/12.57/17.16s**；merge **0.03/0.04/0.02s**。
每次新快照时：snapshot **21.9–33.3s**、exec 11.5–18.6s、总 33.6–48.9s。

**判读**：
* **复用快照**时 4 worker 端到端 **2.85x**（2 worker 2.48x），8 worker 反而
  退化到 2.08x（本机 8 核同时有其他负载 + 每 worker 复制/启动，过订阅）；
* **每次新快照**时快照成本（21.9–33.3s）≈ 基线查询的 60–90%，最好也只有
  **1.09x**——说明"每查询快照"不可行，必须复用快照/只读副本/worker 池；
* **注意 1 worker 也有 1.78x**：worker 在独立新进程里查同样 100k 行 17.9s，
  而 master 进程基线 36.5s——除了并行本身，**进程隔离（master 在 100k 行
  构建后堆较大/GC 压力）** 也贡献了部分加速，不能把 2.85x 全部归因于切分。
  保守看"同模式 4 worker vs 1 worker" = 20.50→12.79s ≈ **1.60x** 的分片收益。
* 进程内线程 100k 最好也只有 0.96x（2 线程），4/8 线程退化——GIL+表锁限制
  与 `docs/CONCURRENCY_SCALING.md` 结论一致。

### 6.3 开销与判读

| 开销项（复用快照） | 10k | 100k | 判读 |
|---|---:|---:|---|
| 快照生成（一次性，可复用） | ~8.9s | ~42.9s | **最大开销**；含 checkpoint/COUNT/样本查询/SHA256，本机负载下偏慢 |
| 每 worker data/ 复制 | 0.05–0.42s | 0.02–0.21s | 小（本地文件复制） |
| 进程启动 spawn | 0.10–2.82s | 0.01–0.21s | Python 解释器 + lightdb 导入；worker 数越多越明显 |
| worker 执行 | 6.2–9.1s | 12.6–20.4s | 查询主体，真正吃 CPU 的部分（多进程并行） |
| 结果归并 | ~0.01–0.04s | ~0.02–0.04s | 小（pickle 回传 + 按主键排序） |

**何时值得用**：结果集大（查询本身 ≥ 数秒）+ 快照可复用/持久只读副本 +
能接受多进程启动延迟；**何时不该用**：小查询（10k 及以下）、无法复用快照、
需要强一致实时结果。

---

## 7. 后续建议（未落地）

1. **快照开销**是当前最大成本（10k 行快照数秒，含 COUNT/样本/SHA 校验）；
   若要产品化，需要"持久化只读副本"或引擎原生只读打开/只读快照句柄，
   避免每次查询复制数据。
2. **worker 池化**：当前每查询现启进程（Python 启动 + lightdb 导入 ~1s/进程）；
   持久 worker 池可显著降低端到端延迟。
3. **IPC 优化**：pickle 文件回传 100k 行有序列化/磁盘开销；可换共享内存/
   压缩列式结果，或只传聚合/分页结果（但那就不是"逐行一致"验证口径）。
4. **协议接入**：若要对外服务，需要在 `lightdb/net/server.py` 增加只读路由
   （解析→校验→快照→分片→合并），并定义并发写/快照失效语义；本原型未改
   server，避免影响现有 wire 行为。
5. **与 REPLICATION/只读副本结合**：更现实的方向是只读副本 + 多进程分片，
   而不是对主数据目录复制快照。

---

## 8. 复跑命令与证据

```powershell
# 测试
python -m pytest tests/test_multiprocess_sharding.py -q
# 基准（10k/100k；约 5-10 分钟，视机器负载）
python scripts/shard_query.py --bench --rows 10000,100000 --workers 1,2,4,8 `
    --repeats 1 --threads --reuse-snapshot --json _analysis_tmp/sharding_bench.json
```

证据文件：

* `_analysis_tmp/sharding_bench.json` — 基准全部原始数据（samples/阶段耗时/
  快照信息/线程对照）；
* `_analysis_tmp/sharding_bench.log` — 基准 stdout；
* `tests/test_multiprocess_sharding.py` 回归。

边界复述：**只读、单表、整数单列主键、BETWEEN**；一致性为快照时刻；
不支持形态显式 1105；不是生产查询入口。

---

## 9. 服务端路由（task-85 落地）

> §7 的"快照复用 / worker 池 / 协议接入"三条已在 task-85 落地为
> `lightdb/net/sharing.py::ShardedQueryEngine` + `MySQLServer` 路由；
> 本文 §1–§6 是**一次性原型** `execute_sharded()` 的口径与量化，服务端路径
> 复用同一套解析/切片/合并语义，但快照与 worker 生命周期不同（见下）。

### 9.1 路由规则（默认关闭）

`LIGHTDB_SHARDING_ENABLED=1` 时，`MySQLServer` 在 `_execute_sql_text` 解析
之后、执行器之前尝试路由。**仅当全部满足**才走分片：

* 单条 `SELECT`，单表，整数单列主键；
* `WHERE <pk> BETWEEN a AND b`（整数字面量），无 JOIN / 聚合 / GROUP BY /
  HAVING / DISTINCT / 窗口 / 子查询 / UNION / CTE / ORDER BY / LIMIT / OFFSET；
* 投影只含列名或 `*`，且包含主键列（否则无法按主键合并）；
* `(hi - lo + 1) >= LIGHTDB_SHARDING_MIN_ROWS`（默认 20000）；
* 会话不在显式事务中、当前库为服务端主库、表级 SELECT 权限通过。

其余情况**安全回退普通执行**（结果/错误与未启用分片完全一致），并计入
`fallbacks_by_reason`；**绝不静默返回错误结果**。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LIGHTDB_SHARDING_ENABLED` | `0` | 是否启用协议层路由 |
| `LIGHTDB_SHARDING_WORKERS` | `2` | 常驻 worker 进程数 |
| `LIGHTDB_SHARDING_MIN_ROWS` | `20000` | BETWEEN 范围最小行数阈值 |
| `LIGHTDB_SHARDING_TIMEOUT` | `120` | 单次分片查询总超时（秒） |
| `LIGHTDB_SHARDING_SNAPSHOT_TTL` | `0` | 数据变更后仍复用旧快照的秒数（0=变更即刷新） |
| `LIGHTDB_SHARDING_POOL_ROOT` | 工作区 `_analysis_tmp/sharding_runs/...` | 快照/worker 临时目录 |

### 9.2 快照复用 + 常驻 worker 池

* **快照**：首次查询时在 quiesce 窗口内 `live_db.checkpoint()`（复用快照模块
  的 `_TableLocks` 表写锁）→ 校验 WAL=0 → 复制表数据文件到
  `<pool>/genN/snapshot/data`；**拷贝窗口毫秒级**，不含原型的一次性
  manifest/COUNT/样本/SHA 采集（那部分只用于备份快照）；
* **复用**：后续查询若"表数据文件指纹 + 服务端写入纪元"均未变 → 直接复用
  同一快照（`snapshot_reused_total` 增长，`snapshot_created_total` 不增长）；
  服务端写入纪元 = 每条非 SELECT 语句成功后 +1（避免纯读下因快照自身
  checkpoint 改变文件 mtime 而误刷新）；带外写入由文件指纹兜底；
* **刷新**：数据变更 → 重建快照（重新 quiesce；重试 3 次仍持续写入则显式
  失败并回退普通执行）；快照 TTL>0 时允许有界陈旧；
* **worker 池**：每个 worker 进程启动时复制一份快照 data/ 并**打开一次
  Database/Executor**，之后经任务/结果文件（pickle，原子 replace）复用；
  不再每查询 spawn/copy/open；worker 数量 = shards 数上限；
* 每次分片查询串行化（引擎锁），多客户端并发正确但分片查询彼此不并行；
  非分片查询不受影响。

### 9.3 指标（`/metrics`，服务端进程）

`lightdb_sharding_enabled`、`_workers`、`_min_rows`、`_requests_total`、
`_sharded_total`、`_fallbacks_total{reason}`、`_worker_errors_total`、
`_timeouts_total`、`_snapshot_created_total`、`_snapshot_reused_total`、
`_snapshot_age_seconds`、`_query_latency_ms_last/avg`、`_rows_total`；
未启用时全部输出 0 + 注释。

### 9.4 错误/一致性语义

* **快照/拷贝失败、持续写入、权限不足、不支持形态、低阈值、事务中** →
  回退普通执行（正确性优先，指标记录原因）；
* **worker 崩溃/超时** → 显式 **1105**（消息含 worker 序号/退出码/超时），
  不返回部分结果、不静默回退；下一查询会重建死掉的 worker；
* 一致口径：结果 = **查询开始时最近一次快照**的已提交数据（快照时刻一致）；
  快照刷新期间写入被表写锁阻塞（quiesce 窗口），窗口外写入会在下一次查询
  触发刷新；与"同一时刻单进程执行逐行一致"在只读/写后静默两类测试下验证。

### 9.5 复跑

```powershell
# 服务端路由（真实 pymysql：一致/回退/并发/指标/worker 崩溃超时）
python -m pytest tests/test_server_sharding_routing.py -q
# 原型 + 引擎（快照复用/worker 池/回退/带外写入刷新）
python -m pytest tests/test_multiprocess_sharding.py -q
```

边界：仍是**原型级**只读加速路径（单表/整数单列主键/BETWEEN），无分布式
事务、无跨表 JOIN 分片、无副本一致性协议；分片结果不保证反映快照之后
的写入（由刷新/TTL 控制）。
