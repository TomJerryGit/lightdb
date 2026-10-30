# CI 全绿确认（2026-10-30 第四轮，提交 2d7ad7b / beta.126）

P4 三项 backlog（增量备份链、分片接入 server 路由、复制协议层）及 CI 稳定性修复后的
最终 CI 运行。

## 运行信息

- 触发提交：`2d7ad7b`（beta.126，CI 全量步骤加 timeout-minutes(50)）
- 运行时间：06:35:32 → 07:07:14 UTC（约 32 分钟）
- `Job result: Succeeded`

## 步骤结果（全部 success）

| 步骤 | 结果 |
|---|---|
| Kill stale test processes / Sync mirror / checkout / verify python / install / compileall / GUI smoke | ✅ |
| **Full test suite（JUnit 门禁；步骤级 timeout 50 分钟）** | ✅ **1798 tests / 0 failed / 0 error / 46 skipped** |
| Annotate / Upload pytest | ✅ |
| **Benchmark gate (quick)** | ✅ |
| **GUI perf budget** | ✅ |
| **Packaging check** | ✅ |
| **Job result** | ✅ **Succeeded** |

前一轮 `0e85388`（beta.125 复制协议层）也已 **Succeeded**（junit 1798/0，同代码验证）。

## 本轮 backlog 交付（beta.123–125）

| 项 | 结果 |
|---|---|
| **增量备份链**（beta.123） | 基线快照 + 行级差分增量（含真实 AUTO_INCREMENT 值）+ binlog DDL；`restore --to/--latest` staging 校验后安装；`prune --keep` rebase；10k 行增量仅 **6.1%**（52KB/2.92s） |
| **分片接入 server 路由**（beta.124） | 协议层自动路由单表 BETWEEN SELECT（开关+阈值）；**快照复用 + 常驻 worker 池**（快照创建 13s→0.6s，复用后 **4.2x**）；`/metrics` 分片指标；不支持形态/事务安全回退，worker 崩溃超时显式 1105 |
| **复制协议层**（beta.125） | 服务端 `COM_BINLOG_DUMP`（非 GTID，自定义 JSON 流，binlog 轮转/1236/权限校验）+ `replica_apply --protocol` 断点续传；**301 ev/s**（协议开销 3.7%）；与内部导出模式逐行/lsn/applied 一致 |

## CI 稳定性修复（beta.108/118/119/126）

- 残留 pytest 孤儿清理（WMI Terminate 兜底；收窄为 runner 进程特征，不误杀开发者进程）
- 全量套件 `pytest-timeout`（300s/thread）：避免挂起卡死并打印栈
- 全量步骤 `timeout-minutes(50)`：任务级兜底
- 根因记录：CI 挂起多与本机同时跑本地全量测试/其他项目负载争夺端口有关 →
  之后 CI 运行期间不再并行跑本地全量测试（改为分块/串行验证）

## 结论

P4 backlog 全部落地并通过 CI（1798 tests / 0 failed + benchmark + GUI perf + packaging）。
代码与文档已推送远端（beta.123–126）。
剩余 backlog 见 `docs/review/04_ROADMAP.md`：复制产品化（failover/多副本）、
组提交等并发优化、触发器/外键等引擎边界、代码签名（无证书）等。
