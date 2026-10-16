# LightDB 性能基准（`benchmarks/`）

> 阶段 2 第 5 项交付。本包是**仓库内唯一**按门禁方式复跑性能的入口。
> 根目录的 [`bench_heap_insert.py`](../bench_heap_insert.py) /
> [`bench_concurrency.py`](../bench_concurrency.py) 是**历史脚本**（阶段 2 基线
> 与 2.4 瓶颈量化时写的一次性工具），核心测量逻辑已被本包吸收
> （[`heap_insert.py`](heap_insert.py) / [`concurrency.py`](concurrency.py)），
> 两者**已被本包取代**；根目录那两份保留仅作为历史证据，不再维护。

---

## 0. 一句话结论

本仓库**没有**白皮书宣称的 12,000 QPS / 3.5M rows/s / 41x / 12.5x
（那些链路没有接进查询路径）。本包把**能复跑、可辩护**的数字定成
**"不回退"门禁**：逐行 INSERT ≥ 1,000 rows/s、多值批量 ≥ 1.2x 逐行、
进程内点查 ≥ 600 q/s、WAL ≤ 32 MB、崩溃恢复 < 5 s、崩溃不丢已提交行、
不泄漏未提交行。**它是回归护栏，不是性能承诺。**

复跑一条命令：

```powershell
python -m benchmarks.run --suite quick --out benchmarks/results.json   # 约 20 s
python -m benchmarks.run --suite quick --gate                          # 门禁判定，未达标退出码 1
```

---

## 1. 测量方法（先读这节，否则数字没有意义）

### 1.1 每条测量怎么取

| 环节 | 做法 | 为什么 |
|---|---|---|
| 状态 | 每轮**删库重建**，表从 0 行长到 N 行 | 轮与轮之间没有残留状态（B+Tree 高度、页缓存、WAL 段落都从零开始） |
| warmup | 每项第 1 轮**丢弃** | 第一轮含 import / 建库 / 页缓存冷启动 |
| 取值 | 其余轮次取**中位数**（另报 min/max/n） | 中位数比均值抗单个离群点 |
| 计时 | `time.perf_counter()` **只包住被测循环** | 不含建库、建表、灌数据准备 |
| 数据目录 | `benchmarks/data_bench*`，跑完即删 | 不污染仓库、不跨项串味 |
| 日志 | `quiet_logger()` 把全局 logger 压到 ERROR | `Executor.execute` 对 >0.5 s 的语句会无条件把**整条 AST** 打到 stdout，不压掉会把打日志的 I/O 计进批量语句耗时 |
| 审计 | `LIGHTDB_AUDIT_LEVEL=NONE` | 默认 DML 会把每条语句同步落盘到 audit.log，那是磁盘 I/O 不是引擎开销 |

### 1.2 ⚠️ 机器漂移：**顺序式 before/after 不可信**

本机吞吐在**同一个测量窗口内单调下滑**，而且能差出数倍。同一会话实测
（2026-10-16，2,000 行档、checkpoint 关）：

```
3,505 → 3,417 → 3,494 → 3,843 → 3,818 → 2,360 rows/s      （6 连跑，同代码）
3,471 → 1,374 rows/s（40 分钟内的两次 quick 档）             （同代码，−60%）
1,763 rows/s（full 档里同一项，另一个时段）                  （同代码）
```

即：**同一份代码、同一个档位，跨一小时观测到 1,374–6,373 rows/s**。
原作者在 20k 档也量到过 90 分钟内 1,750 → 820 rows/s。

**结论（铁律）**：任何"改前 vs 改后"的结论都必须用**同窗交错成对**测量
——A/B/A/B… 交替跑、取**每对比值**的中位数
（[`heap_insert.interleave_pair`](heap_insert.py) /
[`heap_insert.batch_vs_individual_round`](heap_insert.py) 就是这么测的）。
顺序式对照会把机器漂移当成改动收益（或损失）。

**门禁不受这条影响**：门禁比的是"当前实现 vs 固定阈值"，阈值已经留了
安全余量（见第 3 节）；但门禁**也不能**用来论证"某次改动提升了多少"。

### 1.3 checkpoint 开关（怎么关、为什么要关）

在线 checkpoint（WAL 轮转）由两个阈值触发，默认 **1 MB / 5000 条**
（`lightdb/db/table.py:151-154`）：

```powershell
$env:LIGHTDB_WAL_CHECKPOINT_BYTES = '1000000000000000'   # 阈值极大 -> 永不触发
$env:LIGHTDB_WAL_CHECKPOINT_RECORDS = '0'                # 0 = 该维度不限制
```

* **吞吐项**（逐行/批量 INSERT、点查）在脚本里显式走 `cfg='off'`
  （[`heap_insert.prepare_env`](heap_insert.py) 会设好这两个变量），
  目的是让"语句本身的开销"可测，不被周期性 checkpoint 的
  flush+fsync 噪声掩盖。
* **WAL 有界项**用**引擎默认阈值**（`cfg='default'`）——它测的正是
  checkpoint 有没有把 WAL 封住。
* `Table.wal_checkpoint_limits()` 是**每次调用**读环境变量的，所以进程内
  切换 cfg 是安全的；但环境变量必须在**创建表之前**生效（本包统一在
  `prepare_env()` 里设）。

### 1.4 耗时与内存注意

| 套件 | 墙上时间（本机实测） | 峰值内存 | 说明 |
|---|---|---|---|
| `quick` | **15–30 s**（门禁含 6 项测量） | < 400 MB | CI 用；行数取小档（2,000 / 5,000） |
| `full` | **~2–12 min**（本机干净副本 103 s；机器忙时更长） | < 800 MB | 含 TCP 点查、1/4 客户端并发、20k 行 WAL 档 |

* **同一时刻只能跑一个基准进程**：本机实测两边同时跑时，崩溃恢复
  8,000 行档从 1.15 s 涨到 **3.1 s**、崩溃恢复 2,000 行档也能被干扰到
  数秒级。第三方进程（杀毒、Windows 索引器、另一个基准）会**直接污染**
  绝对吞吐类指标。
* 崩溃恢复项会**起子进程并硬杀**（`os._exit(1)`），因此需要允许创建
  子进程；受限沙箱里若 `subprocess` 被禁，该项会直接抛错，请用
  `--only` 排除它。
* 数据目录默认写在 `benchmarks/` 下并**跑完删除**；CI 上建议用
  `--data-dir` 指到临时目录（`tests/test_benchmarks.py` 用
  `LIGHTDB_BENCH_DATA_DIR`）。
* 单进程内多线程点查（并发档）**不扩展**：GIL + 表级锁下多客户端
  总吞吐会**低于**单客户端（阶段 2.4 实测，见
  [`docs/review/06_PHASE2_4_FINDINGS.md`](../docs/review/06_PHASE2_4_FINDINGS.md)）。
  因此并发档只报告、**不进任何门禁**。

---

## 2. 实测基线（这是实测，**不是**白皮书数字）

**机器**：Windows 10（10.0.19045）× AMD64，Intel64 Family 6 Model 142
Stepping 10（8 逻辑核），Python 3.11.9（`D:\Program Files\Python311`）。
**日期**：2026-10-16。
**代码**：`adfe722`（干净副本 `.wt_clean`）为主；脏工作树 `fe4defa`
（含未收口的阶段 2.4 并发改动）作了同窗成对对照。

| # | 指标 | 实测（本机） | 口径 | 来源 |
|---|---|---|---|---|
| 1 | 逐行 INSERT（Table API） | 见下方「实测分布」：**中位 ~3,100–3,800 rows/s**，单轮观测 1,374–6,373 rows/s（2,000 行档）<br>1,764 / 1,534 rows/s（full 档 n=2/n=3，机器较忙时） | `Table.insert()`，autocommit，全新库 | 本包 `quick` / `full` |
| 2 | 逐行 INSERT（Table API，历史快照） | 1,766 rows/s（5,000 行档）<br>1,411 rows/s（20k 档） | 同上，但**测量窗口内机器已在下滑** | 历史脚本 `bench_summary.json` |
| 3 | 逐行 INSERT（完整 SQL 路径） | **4,922 rows/s**（1,000 行档，机器空闲时）<br>1,148 rows/s（同档，机器忙时）<br>历史 946 rows/s（5,000 行档） | 解析 + 执行 + 审计 | 本包 `full` |
| 4 | 多值批量 INSERT（500 行/语句） | 单条多值 4,486–5,442 rows/s（2,000/5,000 行档）vs 逐行 1,534–3,819；**同窗成对**比值：2,000 行档 **1.50–1.87x**（中位 ~1.6x），5,000 行档 1.0–1.7x | 单条多值 INSERT vs n 条单行 INSERT | 本包 `insert_batch_speedup_*` |
| 5 | 点查（进程内） | **中位 831–2,069 q/s**（2,000 行表；单轮观测 588–2,150）<br>1,095 / 2,185 q/s（5,000 行表）<br>1,140 q/s（历史快照） | `Executor.execute_sql('SELECT v ... WHERE id=?')` | 本包 `point_inproc_*` |
| 6 | 点查（TCP / pymysql） | 624–1,033 q/s（2,000 行表，机器忙/闲）<br>516 q/s（历史快照） | 真实 MySQL 协议，服务端在进程内线程 | 本包 `full` / 历史脚本 |
| 7 | WAL 有界 | 5,000 行期间峰值 **468.6 KB**；20,000 行 **478.3 KB**（多次复跑完全一致）<br>历史：200k 行后 ≤0.5 MB、220k ops 后 403 KB | 引擎默认阈值（1 MB / 5000 条）下写 N 行 | 本包 `wal_bounded_*` |
| 8 | 崩溃恢复 | **0.28–0.44 s**（2,000 行已提交）<br>**1.14–1.16 s**（8,000 行，单独跑）／2.2 s（与别的基准同时跑）<br>历史：30.5k 行 0.32 s（改前 **5.92 s**） | `Database()` 到 `SELECT` 拿到正确行数 | 本包 `crash_recovery_*` |
| 9 | 崩溃后数据完整性 | 丢失已提交 **0** 行 / 泄漏未提交 **0** 行（每次复跑都是 0） | 同 #8 | 本包 `recovery_*_rows` |
| 10 | 进程内多线程点查 | 1 客户端 889–898 q/s → 4 客户端 457–827 q/s（**总吞吐下降**）<br>范围查 1 客户端 9.8–14.7 → 4 客户端 7.9–12.9 q/s | 3 s 固定窗口 | 本包 `concurrency_*`（只报告） |

**实测分布（同代码、同档位、跨约 1 小时的 8 次 quick 档，2,000 行档）**

| 项 | 观测范围 | 中位 | 最差单轮 | 门禁阈值 | 余量（中位 → 阈值） |
|---|---|---|---|---|---|
| 逐行 INSERT | 1,374 – 6,373 rows/s | ~3,500 | 1,374 | 1,000 | 3.5× |
| 点查（进程内） | 588 – 2,150 q/s | ~1,400 | 588（单轮） | 600 | 2.3× |
| 批量/逐行比值 | 1.0 – 1.9 x | ~1.55 | 1.0（单对） | 1.2 | 1.3× |
| WAL 峰值 | 468.6 – 478.3 KB | — | 489.8 KB | 32 MB | 64× |
| 崩溃恢复（2,000 行） | 0.28 – 0.44 s | 0.39 | 3.1 s（**并发跑时**） | 5 s | 12.8× |

**两条必须记住的结论**：

1. **绝对吞吐在本机不可复现到 ±2 倍以内**（同一份代码、同一个档位、
   相隔 40 分钟可以从 6,373 掉到 1,374 rows/s）。所以门禁阈值留的是
   **数倍**余量，而不是 10–20%。
2. **同一时刻只能跑一个基准进程**：两个基准同时跑时，崩溃恢复 8,000 行
   档从 1.15 s 涨到 **3.1 s**，`full` 档里 2,000 行点查也能被压到
   588 q/s（门禁阈值 600 的下方）。需要绝对数字时先确认机器是空闲的。

**脏树 vs 干净副本**：同一时间窗内交替跑"TABLE 插入 2,000 行" 3 对，
结果 **0.867 / 1.031 / 1.030 → 中位 1.030x**，即在这个指标上
"2.4 未收口的锁改动"没有可测影响（差异落在 ±10% 抖动内）。
因此**本页所有门禁数字都以 `.wt_clean`（`adfe722`）为准**，避免依赖
未收口改动；脏树复跑（`results_dirty_quick.json`）在本包 5 条门禁上同样
PASS，逐项对照见第 6 节第 8 条。

---

## 3. 可辩护的目标（门禁阈值）

定义在 [`benchmarks/gate.py`](gate.py) 的 `TARGETS`（每项含 value /
better / basis / tolerance 四个字段，程序可读）。

**性质声明（必读）**：这些是**"不回退"门禁**，不是性能承诺、不是 SLA、
也不是给用户看的容量规划数字。依据一律是第 2 节的**本机实测**再留安全
余量；余量的唯一目的是**吸收机器漂移与 CI 抖动**，不是为了好看。

| 指标 | 目标值 | 依据（引用实测 + 余量） | 超时 / 抖动容忍 |
|---|---|---|---|
| 逐行 INSERT | **≥ 1,000 rows/s** | 实测 2,000 行档中位 ~3,500（观测 1,374–6,373）；历史 5,000 行档 1,766、20k 档 1,411。取 1,000：比**最差单轮观测 1,374** 还低 27%，比历史基线低 **~43%** | 门禁用中位数（quick 档 3 轮 → 2 个有效样本）；整套墙上时间上限 `LIGHTDB_BENCH_MAX_WALL`（默认 180 s） |
| 多值批量 | **≥ 1.2 × 逐行语句** | 同窗成对实测（2,000 行档，门禁用档）**1.50–1.87x**（中位 ~1.6x）；5,000 行档 1.0–1.7x | 3 对交错取**比值**中位数；单对比值抖动可达 ±40%，中位数把单点离群压下去 |
| 点查（进程内） | **≥ 600 q/s** | 实测 2,000 行表 588–2,150（中位 ~1,400）；历史 5,000 行表 1,140。取 600：与"被干扰到的最差单轮 588"同量级，留 **~57% 余量**（按中位算） | 中位数；实测 3 轮里出现 1 轮 <700 的概率不低，但 2 个有效样本同时 <600 才判 FAIL |
| WAL 有界 | **≤ 32 MB** | 实测 5,000 行 468.6 KB、20,000 行 478.3 KB（复跑稳定），200k 行历史值 ≤0.5 MB；由默认阈值 1 MB / 5000 条封顶 | 容忍 **64×**；这一项只抓"WAL 无界增长"，不抓小抖动 |
| 崩溃恢复 | **< 5 s** | 实测 0.28–0.44 s（2,000 行）/ 1.14 s（8,000 行，单独跑）；**改前 5.92 s** | 容忍 **~11×**（对 2,000 行档）；注意**并发跑别的基准时会涨到 3.1 s** |
| 已提交行不丢 | **= 0 行** | WAL redo 的语义底线（**数据丢失**，不是性能） | 硬判据，无容忍 |
| 未提交行不泄漏 | **= 0 行** | WAL undo 的语义底线（**隔离性**，不是性能） | 硬判据，无容忍 |

### 3.1 门禁的"重试"语义（为什么可以有，以及它洗不白什么）

干扰是**单向**的：杀毒、索引器、另一个基准进程只会让数字**更差**。
因此 `--gate` 在未达标时会**只重测未达标项**一次（`--retries`，默认 1），
按各自方向取**更有利**的一次：

```
门禁未达标 ['insert_rowwise_table'] -> 只重测 ['insert_rowwise_table_2000'] 一次
insert_rowwise_table_2000: 采用第 2 次尝试
```

* **真回退洗不白**：持续的慢（例如把 `_heap_insert` 的页缓存去掉）两次
  都会差，门禁照旧 FAIL。这一点有自检用例：
  [`tests/test_benchmarks.py::test_retry_is_bounded_and_cannot_wash_out_a_real_regression`](../tests/test_benchmarks.py)
  验证"一次差一次好 → PASS；两次都差 → FAIL"。
* **重试范围有界**：只重跑未达标的 case（不是整套），次数由
  `--retries` 固定；每次尝试的原始值都留在 `benchmarks/results.json`
  的 `gate.attempts` 与各 case 的 `samples` 里。
* 想关掉重试、只看第一次的结果：`--retries 0`。

### 3.2 明确**不**作为目标的东西

* **白皮书的 12,000 QPS / 3.5M rows/s / 41x / 12.5x**：不在目标表里。
  这些数字所在的 OLAP / 列存 / Cython 链路**没有接进 SQL 执行路径**，
  在查询路径上没有落点（对照见第 4 节）。追逐它们不是本门禁的目标。
* **TCP 点查吞吐**：实测 ~516 q/s（协议往返 + 服务端线程调度），
  抖动大且主要反映网络/线程而不是引擎，只报告不判定。
* **进程内并发扩展比**：GIL + 表级锁下**不扩展**（8 客户端低于
  1 客户端），这是已量化的事实，写成阈值只会制造假红；只如实报告。
* **mem / CPU / 磁盘占用**：本包不测。

---

## 4. 白皮书声明 vs 实测（对照表）

> 下表**不改**任何既有文档：既有 README / `docs/**` 里的原文与状态说明
> 一律保留，这里只做集中对照并给出**指针**。

| 白皮书声明 | 出处（指针） | 实测 / 现状 | 结论 |
|---|---|---|---|
| 单线程点查 **12,000 QPS** | [`docs/PERFORMANCE_WHITEPAPER_v4.md:68`](../docs/PERFORMANCE_WHITEPAPER_v4.md)、[`docs/V4_RELEASE_NOTES.md:42`](../docs/V4_RELEASE_NOTES.md) | **1,299–2,069 q/s**（2,000 行表，本包）/ 1,140 q/s（历史 5,000 行表） | **差 6–9 倍**；未实现，不作目标 |
| 8 核 **92,000 QPS** | [`README.md:49`](../README.md)、[`docs/USER_GUIDE.md:106`](../docs/USER_GUIDE.md) | 进程内多线程**不扩展**：8 客户端总吞吐低于单客户端 | **未实现**；GIL + 表级锁 |
| OLAP 扫描 **3.5M rows/s** | [`docs/PERFORMANCE_WHITEPAPER_v4.md:72`](../docs/PERFORMANCE_WHITEPAPER_v4.md) | 向量化/列存链路在 `Executor` / `plan_executor` 里**零引用**，本包查询路径实测是逐行量级（范围/聚合 ~5k–11k rows/s） | **没有落点**；未接入 |
| 大表聚合 **85M rows/s** | [`docs/PERFORMANCE_WHITEPAPER_v4.md:768`](../docs/PERFORMANCE_WHITEPAPER_v4.md) | 同上；`lightdb/olap/**` 在 SQL 执行路径**零引用**，本包实测聚合是逐行量级（~5k–11k rows/s） | **没有落点**；未接入 |
| 列存加速比 **41x** | [`docs/PERFORMANCE_WHITEPAPER_v4.md:93`](../docs/PERFORMANCE_WHITEPAPER_v4.md)、[`docs/ARCHITECTURE_v4.md:560`](../docs/ARCHITECTURE_v4.md) | `lightdb/columnar/**` 未接线 | **未实现**；无对照基线 |
| Cython B+Tree **12.5x** | [`docs/PERFORMANCE_WHITEPAPER_v4.md:92`](../docs/PERFORMANCE_WHITEPAPER_v4.md)、[`docs/DEAD_CODE.md:11`](../docs/DEAD_CODE.md) | `lightdb/cython/` 编译产物 **0 个**，`status()['any_accelerated'] == False`（无回退实现） | **未编译、未验证**；未实现 |
| 逐行 INSERT 的"批量更快"印象 | 历史脚本报告口径 | **顺序对照下批量并不更快**（实测 0.91–1.05x）；**同窗成对**下才是 1.44–1.72x | 用比值，不用顺序对照 |
| 并发"1→4→8 提升" | 阶段 0 摘要 | 1,163 → 925 → **749 q/s（−36%）**（[`README.md:65`](../README.md)） | **回退而非扩展**；如实报告 |

**权威指针**（本包不复制其结论，只指路）：

* [`docs/AUDIT_REPORT.md`](../docs/AUDIT_REPORT.md) —— 数据可信度审计
  （含"12000 QPS 从未被任何测试断言"的定位）。
* [`docs/review/00_SUMMARY.md`](../docs/review/00_SUMMARY.md)、
  [`docs/review/01_engine.md`](../docs/review/01_engine.md) —— 实测数字与判读。
* [`docs/review/04_ROADMAP.md:56`](../docs/review/04_ROADMAP.md) —— "先定可辩护
  的目标并写进基准门禁"这条要求的原始出处。
* [`docs/DEAD_CODE.md`](../docs/DEAD_CODE.md) —— 未接线链路清单。
* [`README.md:23-70`](../README.md) —— 阶段 0 加的"性能声明状态说明"
  （阶段 2 第 6 项补充了「实验性 / 未接入 SQL 路径 / 不计入性能声明」的统一口径）。

---

## 5. 怎么复跑

### 5.1 命令行

```powershell
# 快速档（约 20 s）：人读摘要 + JSON
python -m benchmarks.run --suite quick --out benchmarks/results.json

# 门禁判定：任一项不达标 -> 退出码 1（可直接给 CI 当判定）
python -m benchmarks.run --suite quick --gate

# 完整档（约 2–12 min）：含 TCP 点查与 1/4 客户端并发
python -m benchmarks.run --suite full --out benchmarks/results_full.json

# 只跑某几项 / 换数据目录
python -m benchmarks.run --suite quick --only point_inproc_2000 --data-dir %TEMP%\ldbench

# 干净副本对照（不依赖任何未提交改动；用 junction 把本包挂进去）
git worktree add --detach .wt_clean adfe722
cmd /c mklink /J .wt_clean\benchmarks %CD%\benchmarks
cd .wt_clean
python -m benchmarks.run --suite quick --out ..\benchmarks\results_wt_clean.json
cd .. && git worktree remove --force .wt_clean
```

退出码：`0` 跑完/门禁通过，`1` 门禁未达标或运行出错，`2` 没跑出任何测量值。

### 5.2 门禁的自检（证明它不是橡皮图章）

```powershell
# 把所有阈值放大 100 倍 -> 必然 FAIL -> 退出码 1（并观察"只重测未达标项"）
python -m benchmarks.run --suite quick --gate --target-scale 100
# 逐项覆盖：把点查阈值设成不可能达成的 999999
python -m benchmarks.run --suite quick --gate --target point_inproc=999999 --only point_inproc_2000
# 完全不重试（只看第一次）
python -m benchmarks.run --suite quick --gate --retries 0
```

实测结果（2026-10-16，本机）：

```
A) --target-scale 100                     -> FAIL（0/2 项达标）      exit=1
B) --target point_inproc=999999            -> FAIL（0/1 项达标）      exit=1
C) --target insert_rowwise_table=10000 --retries 2
     stderr: "门禁未达标 ['insert_rowwise_table'] -> 只重测
              ['insert_rowwise_table_2000'] 一次（干扰是单向的…）" ×2
             "insert_rowwise_table_2000: 采用第 3 次尝试"
     stdout: [FAIL] 4083.484 rows/s >= 10000.000 rows/s
                                          （重试过 2 次、仍 FAIL）   exit=1
D) 默认阈值（6 项全跑，含批量）             -> PASS（5/5 项达标）      exit=0
```

### 5.3 进 "CI"

本仓**没有** CI 服务，所以做成"**可被 CI 调用**"的东西：
[`tests/test_benchmarks.py`](../tests/test_benchmarks.py) 默认**全部跳过**，
只有显式开开关才跑。

```powershell
# 日常套件：不跑性能，也不会因为性能抖动变红
python -m pytest tests/test_benchmarks.py -q
# -> 12 skipped in 0.22s

# CI / 提交前自检：跑 quick 档并断言第 3 节的每一条目标（未达标只重测一次）
$env:LIGHTDB_RUN_BENCHMARKS = '1'
python -m pytest tests/test_benchmarks.py -q
# -> 12 passed in 20.01s
Remove-Item Env:\LIGHTDB_RUN_BENCHMARKS
```

`LIGHTDB_RUN_BENCHMARKS` 取 `''` / `0` / `false` / `no` / `off` 都算关闭；
另有 `LIGHTDB_BENCH_DATA_DIR`（数据目录）、`LIGHTDB_BENCH_MAX_WALL`
（整套墙上时间上限，默认 180 s）与 `LIGHTDB_BENCH_RETRIES`
（未达标时的重测次数，默认 1）。

等价的纯 CLI 判定（不依赖 pytest）：

```powershell
python -m benchmarks.run --suite quick --gate     # 未达标 exit 1
```

---

## 6. 未做到 / 已知风险

1. **没有接入真正的 CI 服务**。本仓没有 GitHub Actions / Jenkins 之类
   配置；本包只做到"可被 CI 调用的东西"（`--gate` 退出码 +
   可选开的 pytest 用例）。真正接 CI 需要另行决定跑在哪个 runner、
   用哪种机器（**换机器必须重测并重新定阈值**，见下条）。
2. **绝对吞吐类门禁在 CI 上仍可能抖动**。本机实测同代码同一小时内
   逐行 INSERT 1,374 ↔ 6,373 rows/s、点查 588 ↔ 2,150 q/s（杀毒/索引器/
   热节流都可能是外因）。**抖动幅度的依据**就是这些频次统计：跨约
   1 小时的 8 次 quick 档里，逐行 INSERT 的最差中位是 1,374（中位
   ~3,500），点查最差单轮 588（中位 ~1,400）。当前容忍度：
   * 逐行 INSERT 阈值 1,000 = 最差**中位**观测的 **73%**；
   * 点查阈值 600 = 最差**单轮**观测的 **~102%**（**这一项余量最小**：
     `full` 档里真的量到过 588 的单轮值；但门禁取 2 个有效样本的
     中位数，两个样本同时低于 600 才算 FAIL）；
   * 批量比值阈值 1.2 vs 最差单对比值 1.0（3 对取中位，通常 ~1.5）；
   * 崩溃恢复 5 s vs 最差 3.1 s（**并发跑时**；单独跑 0.28–1.16 s）。

   缓解手段是"取中位数 + 未达标只重测一次（`--retries`）"，
   **但不能保证 100% 不假红**。如果在 CI 上红了：先 `--retries 3` 重跑
   一次，并对比 `results.json` 里的 `min/max/samples` —— 如果 min 很低、
   max 正常，那是干扰；如果两次都稳定低于阈值，那是真回退。
3. **`full` 档的 `insert_batch_speedup_5000` 挡不住**：它观测到过
   1.0–1.7x，中位有时低到 1.115（与另一个基准同时跑时），所以**门禁
   绑定的是 2,000 行档**（`insert_batch_speedup_2000`，1.50–1.87x）。
   5,000 行档仍然会跑、会记录，但不判定。
4. **门禁不覆盖"批次/并发"路径**：TCP 点查、进程内多线程、
   `executemany` 语义都只报告。是否要给它们定阈值，要先有
   （a）能稳定复现的机器 和（b）明确的容量需求，否则就是拍脑袋。
5. **崩溃恢复项的阈值（<5 s）很宽**：它抓的是"恢复时间随运行时长
   增长"这类回归（改前 5.92 s），不是"恢复够不够快"。另外 quick 档
   只测 2,000 行档；8,000 行档（1.14 s）只在 `full` 档跑。
6. **`full` 档还没在 CI 意义上跑通**：本机跑过两次（干净副本 103 s 和
   111 s，见 `results_full_clean.json`），但比 quick 档长一个数量级，
   建议只在发版前手动跑；它还要跑 20k 行 WAL 档 + 8k 行崩溃恢复，
   更容易被第三方进程干扰。
7. **历史脚本仍在根目录**（按交付要求不删、不移动）。它们与
   `benchmarks/` 的逻辑**同源但目前是两份代码**；将来若改动测量口径，
   应只改本包，必要时把历史脚本标注为只读。
8. **脏工作树（阶段 2.4 未收口）仍在**。本包所有**门禁数字**以
   `.wt_clean`（`adfe722`）为准；脏树复跑（`results_dirty_quick.json`）
   在本包全部 5 条门禁上同样 PASS，且与干净副本数量级一致
   （同一小时内：逐行 INSERT 4,428 vs 4,081 rows/s、点查 1,463 vs
   1,299 q/s、批量比值 1.52 vs 1.56、崩溃恢复 0.36 vs 0.45 s），
   同窗成对对照（2,000 行 TABLE 插入 3 对）**中位比值 1.030x** ——
   即这批改动在本包指标上没有可测影响。但**不能**据此认为那批改动是
   安全的：它打破了 `tests/test_mvcc_persistence.py` 的一条既有断言，
   与本包无关。

---

## 7. 文件清单

| 文件 | 作用 |
|---|---|
| [`README.md`](README.md) | 本文件：方法、基线、目标、对照、CI、风险 |
| [`run.py`](run.py) | CLI 入口：`--suite quick\|full`、`--out`、`--gate`、`--only`、`--data-dir`、自检开关 |
| [`cases.py`](cases.py) | 每个测量项（case）的定义与套件编排 |
| [`gate.py`](gate.py) | 目标表 `TARGETS` + 判定 + 人读摘要 |
| [`heap_insert.py`](heap_insert.py) | INSERT / 点查 / WAL / 崩溃恢复的测量原语（含同窗交错成对） |
| [`concurrency.py`](concurrency.py) | 进程内多线程点查/范围查负载（只报告） |
| [`results.json`](results.json) | 最近一次 quick 档输出（含机器、git 版本、方法与逐项样本） |
| [`results_wt_clean.json`](results_wt_clean.json) | `.wt_clean`（`adfe722`）上的 quick 档输出 |
| [`results_full_clean.json`](results_full_clean.json) | `.wt_clean` 上的 full 档输出（含 TCP 点查与并发档） |
| [`results_dirty_quick.json`](results_dirty_quick.json) | 脏工作树（`fe4defa` + 未提交改动）上的 quick 档输出（对照用） |
| [`../tests/test_benchmarks.py`](../tests/test_benchmarks.py) | CI 入口：默认 skip，`LIGHTDB_RUN_BENCHMARKS=1` 时断言全部门禁 |

> 数据目录 `benchmarks/data_bench*` 是运行时产物，跑完即删，不进版本库。
