# CI 全绿确认（2026-10-29 第二轮，提交 da71efe / beta.119）

四条 backlog 线（复合主键 3–4 列、增量复制、多进程只读分片、生产化 UI）及全部
收尾修复完成后的最终 CI 运行。

## 运行信息

- 触发提交：`da71efe`（beta.119 之后的空提交；代码与 beta.119 相同）
- 运行时间：07:33:09 → 08:08:59 UTC（约 36 分钟）
- `Job result: Succeeded`

## 步骤结果（全部 success）

| 步骤 | 结果 |
|---|---|
| Kill stale test processes（已收窄为仅 runner 进程特征） | ✅ |
| Sync mirror / checkout / verify python / install / compileall / GUI smoke | ✅ |
| **Full test suite（JUnit 门禁 + pytest-timeout 300s 保护）** | ✅ **1755 tests / 0 failed / 0 error / 45 skipped** |
| Annotate failed tests / Upload pytest | ✅（零失败） |
| **Benchmark gate (quick)** | ✅ |
| **GUI perf budget** | ✅ |
| **Packaging check** | ✅ |
| **Job result** | ✅ **Succeeded** |

本地同版本全量（带 `--timeout=180`）：**1710 passed, 44 skipped, 1 xfailed, 0 failed**（24 分钟）。

## 本轮新增修复（beta.114–119）

1. **BTree v3 页格式**（复合主键 3–4 列）→ Cython/纯 Python 回退同步，v1/v2→v3 迁移脚本
2. **增量复制**：follow 持续追平、状态 checksum、1236 缺口显式、复制延迟指标
3. **多进程只读分片原型**：100k 行 4 worker 端到端 2.85x、查询段 3.63x
4. **生产面板**：Studio/Web 复制状态 + metrics 摘要 + 快照备份/恢复（9 tests）
5. **CI 稳定性修复**：
   - 残留清理只匹配 runner 进程特征（不再误杀开发机手动 pytest）
   - 全量套件加 **pytest-timeout**（300s/thread）：CI 曾出现测试挂起导致 job 被取消，
     现在超时会打印线程栈定位而不是整轮卡死
   - 排查并清理 runner 多实例/陈旧工作区导致的 checkout 失败

## 迁移实测（BTree v3）

真实旧演示库（v1，13 表）经 `migrate_btree_format.py` 迁移到 v3：行数全一致、
`校验=OK`，迁移后数据可读写且复合主键点查正常。

## 结论

四条 backlog 线全部落地并通过 CI（1755 tests / 0 failed + benchmark + GUI perf +
packaging）。已知边界（已写入 docs）：
- 分片原型为只读/单表/BETWEEN，且快照准备开销是产品化主要障碍（100k 复用快照 2.85x）
- 复制为单主无自动 failover、单线程 apply
- 复合主键最多 4 列且每分量 signed-32
- BTree v3 需要旧库先迁移
- 代码签名无证书（按用户要求忽略）
