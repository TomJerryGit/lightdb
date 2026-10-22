# P5 性能与可靠性记录（2026-10-22）

## 1. Benchmark harness（`benchmarks/competitive_harness.py`）

- 命令：`python -m benchmarks.competitive_harness --suite quick|full --gate`
- 可复现：seed=20261019、数据集 SHA256、warmup、多次采样；JSON 含版本/环境/
  median/best/worst；结果写 `_analysis_tmp/bench/harness_*.json`
- 不假横比：无 MySQL 时 `reference_db.available=false` + 明确“仅 LightDB 基线”
- **quick --gate PASS 11/11（29s）**：逐行 INSERT 4,370 rows/s、批量 3,977（2.06x）、
  进程内点查 1,080 q/s、范围扫描 11,281 rows/s、聚合 14,760 rows/s、深分页 13.3 q/s；
  TCP 点查/范围/聚合/分页 649/11.2/15.0/15.8 q/s；并发 1/4/8=1,510/1,179/1,154 q/s
  （仅报告，已知不扩展）；错误 0
- **full --gate PASS 11/11（117s，5,000 行）**
- 阈值取实测中位 20–30%，每项 basis 写明；既有 `benchmarks.run --suite quick --gate` 仍 PASS

## 2. WAL checkpoint（`lightdb/db/table.py` 等）

- 按 **size / records / time** 触发；`LIGHTDB_WAL_CHECKPOINT_SECONDS` 默认 60s
- 实测：6000 行长跑 WAL 峰值 ≈1MB 后退休归零；记录阈值 100 时最大 98 条、8 次 checkpoint
- `SHOW WAL STATUS`：真实 Wal_bytes/Wal_records/阈值/Checkpoints/最近原因/退休量/Dat_bytes
- checkpoint 后重启数据正确、WAL=0；未提交事务硬杀崩溃恢复正确
- 边界：WAL 用 truncate 退休（非命名多段轮转）；时间触发在下一次写入时检查（无后台线程）

## 3. 数据压实（`VACUUM FULL` / `Database.compact()`）

- 离线显式操作；安全门：无未提交事务；成功后 Table 对象被替换，需重新 `get_table()`
- 实测 400 行 × 8 轮 UPDATE（3600 版本）：`.dat` **139,264 → 32,768 B（-76.5%）**，
  索引项/行 400→400、dump sha 一致、重启一致、重复压实 `skipped=no_gain`
- 崩溃注入（pending_ddl 已写/已替换首文件）重开后数据完整、pending 清空、WAL=0
- 边界：不自动执行（避免误伤在线业务）；不会压缩 `btroot` 以外的外部资源

## 4. 测试

- 新增 `tests/test_benchmark_harness.py`（10 passed）+ `tests/test_wal_checkpoint_p5.py`（10 passed）
- 高风险回归：checkpoint/vacuum/insert_atomicity/txn_atomicity/batch12-15/v4_sql_features/
  backup_restore/mvcc + P5 = 241 passed
- 已知 flaky 修复：CI 上 `SHOW AUDIT LOG` 首次查询为空 → 审计 sink 初始化失败时
  首条记录惰性重建（`b59bf65`），本地 v4_sql_features 130 passed
