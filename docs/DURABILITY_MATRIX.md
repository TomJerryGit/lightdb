# 持久性矩阵（Durability Matrix）

> 目标：把"崩溃/掉电后数据会怎样"从口号变成可复跑的测试与明确的持久性档位。
> 运行脚本：`python scripts/crash_matrix.py --quick`（约 1 分钟）或
> `--iterations 3`（约 2–3 分钟）；结果写 `<out>/report.json`。

## 1. 持久性档位（源码事实，非承诺）

| 提交方式 | 落盘点 | 进程硬杀（kill -9） | 断电 |
|---|---|---|---|
| 单行 autocommit（默认） | WAL `flush_to_os()`（行记录 + COMMIT 记录） | ✅ 已提交行不丢（OS 缓冲仍在） | ⚠️ 可能丢最后若干条（未 fsync） |
| 多行 `INSERT ... VALUES (...),(...)` | 语句级 flush（不 fsync） | ✅ 语句全成或全无 | ⚠️ 同上 |
| 显式 `BEGIN ... COMMIT` | `WAL.commit()` → **fsync** | ✅ 已提交不丢 | ✅ 已提交不丢 |
| `CHECKPOINT` / `VACUUM FULL` / 正常关闭 | 脏页 fsync → BTree 根 fsync → WAL 截断 | — | ✅ 已完成 checkpoint 的数据不丢 |

依据：`lightdb/storage/wal.py`（`commit/commit_autocommit/flush_to_os/_flush_locked`）、
`lightdb/db/table.py::_retire_wal/checkpoint`。

**结论**：需要"掉电也不丢"的关键写入请使用显式事务（COMMIT 会 fsync）；
autocommit 属于"进程崩溃不丢、掉电可能丢尾部"的档位（与文档一致，不夸大）。

## 2. 已覆盖场景与结果（2026-11-26，Windows 10 本机）

脚本 `scripts/crash_matrix.py`，每场景多次随机时机（`--iterations`）：

| 场景 | 做法 | 通过判据 | 实测 |
|---|---|---|---|
| S1 autocommit 写入中硬杀 | 随机延迟 TerminateProcess，重启 | 行数 ≤ 已尝试数、主键连续、≥1 行 | ✅ 3/3 |
| S2 显式事务原子性 | 提交 50 行后开新事务写 50 行未提交即硬杀 | 已提交 50 行在、未提交 0 行 | ✅ 3/3 |
| S3 多行 INSERT 原子性 | 500 行单语句写入中硬杀 | 恢复后 0 或 500 行 | ✅ 3/3（观察到 0 与 500 两种分支） |
| S4 checkpoint/VACUUM 中硬杀 | 200 行后 VACUUM FULL 进行中硬杀 | 恢复 200 行、SUM 一致、无重复 | ✅ 3/3（修复后） |
| S5 WAL 尾部截断（torn write 模拟） | 硬杀后截断 10% WAL 尾部再启动 | 能恢复或显式报错，不静默损坏；丢失量 ≤ WAL 尾部 | ✅ 3/3（100→90，尾部未 fsync 的 10 条丢失，符合档位） |
| S6 崩溃后备份/恢复 | 硬杀→重启→全量快照→恢复到新目录→启动校验 | 源与恢复行数一致 | ✅ 3/3 |

矩阵结果：**18/18 PASS**（`_analysis_tmp/crash_matrix/report.json`）。

## 3. 矩阵发现并修复的真实缺陷（beta.160）

**现象**：`VACUUM FULL` 进行中被硬杀后重启，`SELECT COUNT(*)` 出现 350 行
（distinct id=200），即 150 行重复；BTree 索引项 350。

**根因**：`checkpoint` 的正确顺序是"脏页 fsync → BTree 根 fsync → WAL 截断"。
若在"数据页已落盘、根指针尚未落盘"的窗口被硬杀：
- 磁盘上的旧根指针看不到这些新行；
- recovery 按 WAL redo 时索引找不到旧版本，便追加新版本并写索引；
- 但**堆页里已落盘的旧版本仍然可见**（xmax=0），于是同一主键出现两份可见行；
- 同时恢复出的索引项与堆版本不一致（350 项）。

**修复**（`lightdb/db/table.py`）：
1. 恢复期建立"堆内 pk → 已落盘版本"索引，redo 遇到索引缺失时优先复用已落盘
   版本（不追加重复版本）。
2. 有真实 redo/undo 时，恢复末尾**用堆页真实版本重建 BTree**：按主键去重，
   每个 pk 只保留一个链头；干净重启（WAL 为空）不做重建，保持原有
   free-page 布局。
3. 回归测试：`tests/test_recovery_interrupted_checkpoint.py`（确定性复现：
   数据页 flush 后 `os._exit(0)`，重开校验 200 行/200 distinct/索引 200 项）。

## 4. 复跑方法

```powershell
# 快速（1 轮/场景，~1-2 分钟）
python scripts/crash_matrix.py --quick --out _analysis_tmp\crash_matrix_quick

# 标准（每场景 3 轮随机时机）
python scripts/crash_matrix.py --iterations 3 --out _analysis_tmp\crash_matrix
```

每个场景的现场（数据目录、服务端日志、报告）保留在 `<out>/runs/<scenario>/`，
失败时不清理现场，便于分析。

## 5. 限制与后续

- 本矩阵用 `TerminateProcess`（等价 kill -9）与 WAL 尾部截断**模拟**掉电；
  **真实断电/磁盘写缓存丢弃**仍需 VM（VM 快照后强制断电）或物理断电验证，
  见 `docs/CLEAN_VM_TEST.md`（Windows Sandbox/Hyper-V）。
- 未覆盖：磁盘满、只读文件系统、杀毒软件锁文件、超长 checkpoint（>2s）窗口。
  建议后续加：小 VHD 填满磁盘、随机 kill 于 1 万行 CHECKPOINT、连续 100 次
  kill 的疲劳测试。
- 每次发布前建议至少跑一次 `--iterations 3`；重大存储改动跑 100 次疲劳测试。


## 6. 首次完整运行结果（2026-11-26）

- `python scripts/crash_matrix.py --iterations 3`：**18/18 PASS**（153s）
- 修复后复跑 `--iterations 2`：**12/12 PASS**（99s）
- 确定性回归测试：`tests/test_recovery_interrupted_checkpoint.py` **1 passed**
- 持久性相关回归：`wal/checkpoint/vacuum/备份/复合主键/崩溃/原子性` **78 passed / 1 skipped**
- CI：提交 `84e6cd9`（beta.162）全绿 **1860 tests / 0 failed / 45 skipped**
  + benchmark / GUI perf / packaging

### 6.1 顺带修复：metrics 405 在 Windows 高负载下的 RST

`tests/test_production_metrics.py::test_metrics_health_404_405` 在 CI（高负载）
连续两次失败：POST `/metrics` 期望 405，客户端收到 `ConnectionAbortedError
[WinError 10053]`。根因是 HTTP 处理程序未读取请求体就关闭连接，Windows 内核
回 RST。修复：`metrics_server._method_not_allowed()` 先按 `Content-Length`
（或 chunked）读掉请求体，再写 405；本地 17/17、CI 全绿验证。

### 6.2 发布前检查清单（持久性）

- [ ] `python scripts/crash_matrix.py --iterations 3` 全绿
- [ ] `python -m pytest tests/test_recovery_interrupted_checkpoint.py -q` 通过
- [ ] 备份/恢复演练（`docs/BACKUP_RESTORE_GUIDE.md`）通过
- [ ] 真机断电验证（VM，见 `docs/CLEAN_VM_TEST.md`）——建议每季度一次
