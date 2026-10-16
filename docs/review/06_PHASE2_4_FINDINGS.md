# 阶段 2 第 4 项（查询并发）—— 瓶颈已量化，修复未收口

**状态**：**未提交**。工作树里留着一份**未完成**的读写锁实现（改动 2 个文件 + 新增 2 个文件），
它**打破了 1 条既有测试**，因此不能进主干。
**已验证基线**：`adfe722`（阶段 2.3 第①②步）。

---

## 1 瓶颈已量化（本轮最有价值的产出，务必保留）

探针 `_probe_conc.py`（152 行，进程内多线程，逐客户端数统计锁的
`acq / avg_wait / max_wait / avg_hold / max_hold / total_hold`）：

| 场景 | 客户端数 | 总吞吐 | `Table._lock` avg_hold | avg_wait | p50 | p95 |
|---|---|---|---|---|---|---|
| 点查 | 4 | 518.3 q/s | 0.338 ms | 1.99 ms | 6.2 ms | 10.5 ms |
| 点查 | **8** | **423.7 q/s**（**低于**单客户端 → 负加速） | 0.604 ms（max 344 ms） | 1.25 ms | 14.2 ms | 42.0 ms |
| **范围查询** | 1 | 6.6 q/s | **147.4 ms**（max 777.9 ms） | 0.003 ms | 106.9 ms | 780.1 ms |
| 范围查询 | 2 | 9.7 q/s | 101.0 ms | 95.5 ms | 207.3 ms | 242.9 ms |
| 范围查询 | 4 | 7.8 q/s | 126.2 ms | **357.2 ms** | 410.4 ms | 1081.9 ms |
| 范围查询 | **8** | 9.7 q/s | 100.7 ms | **639.6 ms** | 805.5 ms | 953.9 ms |

**结论（三条）**：
1. `BufferPool.lock` 与 `WAL.lock` **都不是瓶颈**（avg_hold 分别为 0.004 ms 与 0 ms；读路径 `WAL.lock` `acq=0`）。
2. **`Table._lock` 被只读路径整条语句持有**：范围查询平均 **147 ms**、最长 **778 ms**。
3. 后果是**零读并发**：等待时间随客户端数线性增长（0.003 → 95.5 → 357.2 → 639.6 ms），
   于是 8 客户端总吞吐反而低于单客户端（点查 423.7 q/s 的负加速就是这么来的）。

**方向**：只读语句必须在**取好 MVCC 快照后释放**（或改用共享锁）`Table._lock`，扫描走快照，
而**不是整条扫描都持排他锁**。这是下一步的核心。

---

## 2 已实现但未收口的部分（工作树内，未提交）

- 新增 `lightdb/concurrency/table_rwlock.py`（读者-写者锁）。
- 改动 `lightdb/db/table.py`、`lightdb/sql/executor.py`（只读路径改用共享锁）。
- 新增 `tests/test_concurrency.py`、`bench_concurrency.py`（同窗成对基准脚本）。

**验收结果（Lead 实测）**：
```
pytest tests/test_concurrency.py + txn_atomicity + mvcc_persistence + checkpoint_wal
     + insert_atomicity + phase0_integrity + backup_restore + batch7 + vacuum_pages
-> 1 failed / 94 passed
FAILED tests/test_mvcc_persistence.py::test_vacuum_reclaims_dead_versions_and_splices_chain
```
失败的是**既有测试**（不改断言的前提下必须保留其行为）：并发/锁改动破坏了
**vacuum 的版本回收与链拼接**。诊断打印显示链状态与期望不符，例如
`pk=1 @(2,2) values=[1, 'v2'] xmin=3 xmax=0 prev=(2,1)`、`状态: {1: (2, 2)}`。

**为什么没继续**：本轮派出的两个子代理都在中途失败（一个量完瓶颈后卡死 41 分钟，一个零报告退出），
且本会话**无法删除/覆写**子代理写过的文件（文件所有权保护）→ 无法自行回退，只能如实登记。

---

## 3 下一步（建议按此顺序）

1. **先修那条既有测试**：`test_vacuum_reclaims_dead_versions_and_splices_chain` 必须恢复绿色
   （不许改断言）。最可能的冲突点：只读共享锁让 `vacuum` 无法取到排他锁、或被共享锁挡住，
   导致该回收的版本没回收 / 链没拼接。
2. **再同窗成对重测**（本机吞吐在测量窗口内单调下滑，禁止顺序式对照）：
   点查 1/2/4/8、范围查询 1/2/4/8 的改前/改后总吞吐与中位比值，以及 `Table._lock` 的
   avg_hold/avg_wait 改前/改后。目标：**8 客户端总吞吐 ≥ 单客户端**、范围查询等待显著下降。
3. **明确进程内上限**：GIL 之下 CPU 密集部分无法真并行，必须如实给出"最多 N 倍"
   （例如 8 客户端总吞吐最多等于单客户端的 N 倍），**不要**宣称线性加速；跨进程并行不在本步范围
   （沙箱还禁多进程原语）。
4. **硬门禁**：`test_txn_atomicity`、`test_mvcc_persistence`、`test_checkpoint_wal`、
   `test_insert_atomicity`、`test_phase0_integrity`、`test_backup_restore`、
   `test_batch7`（**9/9**）、`tests/test_vacuum_pages.py` 全绿；既有断言改动 = 0；
   并发写结果正确、无脏读、N 客户端后数据一致。

---

## 4 回退（若决定放弃本轮实现）

本会话无法删除/覆写子代理写过的文件（`git checkout` 报 `unable to unlink`，
ACL 诊断结论 `NOT_THIS_CLASS`＝文件所有权保护，非权限缺失）。**在普通终端**执行：
```powershell
cd D:\lightdb_project
git checkout -- lightdb/db/table.py lightdb/sql/executor.py
Remove-Item lightdb\concurrency\table_rwlock.py, tests\test_concurrency.py -Force
git status
```
（`_probe_conc.py` / `_probe_conc.log` / `bench_concurrency.py` / `_bench_conc_pairs.json` /
`_base_conc/` 等探测物可按需保留为证据或一并删除。）
