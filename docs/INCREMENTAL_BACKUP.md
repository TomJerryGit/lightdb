# 增量备份链（INCREMENTAL_BACKUP）

> P4 交付。实现：`scripts/backup_chain.py`；测试：`tests/test_incremental_backup_chain.py`
> （18 pytest 用例）；基线/快照复用：`lightdb/backup/snapshot.py`（未改动）。
> 量化证据：`_analysis_tmp/incremental_backup_bench.json`。

## 0. 一句话结论

用 **一致性全量快照做基线 + 行级差分做增量 + binlog 区间内 DDL SQL** 组成可验证
的增量链：`init` → 多次 `incr` → `restore --to N/--latest`。恢复后逐表对比
**行数 / 主键首末值 / 全表行摘要 / SHOW CREATE / AUTO_INCREMENT** 并要求
**WAL=0**；断链/缺段/篡改/版本不兼容均显式失败且**不触碰目标目录**；
`--keep N` 通过 rebase 把旧增量压成新基线，链保持连续。

10k 行实测（本机 2026-10-29，主测量）：基线 **849 KB / 8.64s**；1000 行变化
（500 更新 + 500 新增）增量 **52 KB / 2.92s**（≈基线 6.1%）；恢复
（基线 + 1 增量）**4.75s**，WAL=0、状态校验通过。同日另一轮并行负载下测得
init/incr/restore 为 **4.97s / 11.42s / 8.91s**（大小不变），耗时随机器负载
漂移，增量大小与比例是稳定指标。

## 1. 方案与设计理由

### 1.1 为什么不用"binlog DML 回放"

引擎 binlog 的 DML 行事件对 AUTO_INCREMENT 列记录的是 `"NULL"`（见
`lightdb/replication/append_only_binlog.py` 的 INSERT 事件 data.row），
而不是实际分配的主键值；直接回放无法逐值复现自增主键（会在恢复库上重新
分配新号）。为保证恢复后与源库**逐行一致（含自增主键）**，本实现采用：

* **基线**：复用 `create_snapshot()`（checkpoint → WAL=0 → 拷贝数据文件）；
* **增量**：打开静默数据目录（checkpoint 后）扫描全表行，与链内缓存的
  上次状态做**行级差分**：
  - 主键新增 → `inserts`（完整行值，含真实 AUTO_INCREMENT 值）；
  - 主键消失 → `deletes`（主键值）；
  - 主键相同但行摘要变化 → `updates`（完整新行值；恢复时 delete+insert）；
  - 同时从 binlog 区间捕获 **DDL SQL**（CREATE/ALTER/DROP TABLE、索引/
    视图），恢复时先回放 DDL 再应用行差分；
* **恢复**：基线快照 → 顺序应用增量（DDL → 行差分）→ 恢复
  AUTO_INCREMENT → checkpoint（WAL=0）→ 状态指纹校验 → 才安装到目标。

### 1.2 一致性边界

| 项 | 说明 |
|---|---|
| 一致性时刻 | 每次 `init`/`incr` 捕获时的 **checkpoint 后（WAL=0）静默时刻** |
| 写假设 | 运行期间**必须无服务监听、无并发写**；`incr` 检测到 8888 服务运行即拒绝 |
| 未提交事务 | 因要求静默+checkpoint，不进入增量；恢复后不会出现未提交数据 |
| 增量内容 | 两个静默时刻之间的行级差异 + 该区间已提交的 DDL SQL |
| schema 变更 | 必须在 binlog 区间内找到对应 DDL（默认 binlog 启用）；找不到 → 显式拒绝，不猜测 |
| 恢复原子性 | 全部校验通过前**不触碰目标目录**；失败只清理 staging |
| 版本 | manifest 记录 `lightdb_version`/`format_version`；不兼容显式失败（不做迁移） |

> 注意：行级差分需要**全表扫描**（每次 `incr` 的耗时与表大小相关，而不是只与
> 变化行数相关）；这是"不依赖 binlog DML 回放、保证自增主键逐值一致"的代价，
> §5 有实测。

## 2. 链结构与 manifest

`chain_manifest.json`（原子写：`*.tmp → rename`）：

```jsonc
{
  "schema": "lightdb-backup-chain/1",
  "format_version": 1,
  "lightdb_version": "4.0.0",
  "created_at": "...", "updated_at": "...",
  "source_data_dir": "D:/data/db",
  "keep": 3,
  "entries": [
    {"id": 0, "kind": "full", "dir": "full_0000",
     "binlog_start": 22410, "binlog_end": 22410,
     "manifest_sha256": "<snapshot manifest.json sha256>",
     "snapshot_files": 7, "snapshot_bytes": 869763, "snapshot_row_total": 10000,
     "state": {"t": {"columns": [...], "primary_key": "id",
                     "row_count": 10000, "row_digest": "<sha256>",
                     "first_row": [...], "last_row": [...],
                     "show_create": "CREATE TABLE ...", 
                     "auto_increment_next": {"id": 10001}}}},
    {"id": 1, "kind": "incr", "file": "incr_0001_...json",
     "binlog_start": 22410, "binlog_end": 23000,
     "size": 53440, "sha256": "<file sha256>",
     "stats": {"tables_changed": 1, "inserts": 500, "updates": 500,
               "deletes": 0, "ddl_count": 0},
     "state": { /* 本段结束状态指纹，同上 */ }}
  ]
}
```

增量文件（`incr_*.json`）：

```jsonc
{"schema": "lightdb-backup-increment/1", "format_version": 1, "id": 1,
 "lightdb_version": "4.0.0",
 "binlog_start": 22410, "binlog_end": 23000, "binlog_gap": false,
 "ddl": ["ALTER TABLE ..."],
 "tables": {"t": {"inserts": [[...]], "updates": [[...]], "deletes": [3, 4]}},
 "state": { /* 状态指纹 */},
 "stats": {...}}
```

状态指纹（恢复后逐项校验）：
* `row_count`：逐表行数；
* `first_row` / `last_row`：主键首/末行的完整值（"关键值"）；
* `row_digest`：对 `(主键, 行内容 SHA256)` 排序后再 SHA256 的**全表摘要**，
  能发现任意行的增删改（比只比行数强）；
* `show_create`：`SHOW CREATE TABLE`（归一化 AUTO_INCREMENT 数值，避免自增
  推进被误判成 schema 变化）；
* `auto_increment_next`：`Table.auto_increment_next_values()` 精确值；
* 另有 **WAL=0** 校验（恢复后每个表 `.wal` 必须为 0 字节）。

## 3. 命令与语义

```powershell
# 1) 全量基线（要求数据目录静默；复用 snapshot 一致性快照）
python scripts/backup_chain.py init --data-dir D:/db --chain-dir D:/chain

# 2) 增量（可重复；--keep N 成功后自动清理旧增量）
python scripts/backup_chain.py incr --data-dir D:/db --chain-dir D:/chain
python scripts/backup_chain.py incr --data-dir D:/db --chain-dir D:/chain --keep 3

# 3) 恢复：--to N（0=仅基线）或 --latest；目标非空需 --force
python scripts/backup_chain.py restore --chain-dir D:/chain --target D:/restore --to 2
python scripts/backup_chain.py restore --chain-dir D:/chain --target D:/restore --latest

# 4) 保留策略（独立命令；自动 rebase）
python scripts/backup_chain.py prune --chain-dir D:/chain --keep 2

# 5) 10k 量化
python scripts/backup_chain.py bench --rows 10000 --changes 1000 `
    --json _analysis_tmp/incremental_backup_bench.json
```

退出码：
* `0` 成功（含 `incr` 无变化 noop）；
* `1` 通用链错误（如服务运行中/目标非空/参数错）；
* `3` 版本不兼容；`4` 链完整性（断链/缺段/sha 不符/状态指纹不符）；
* `5` 不支持变更（TRUNCATE/RENAME、schema 变更无 binlog DDL 等）。

`--keep N` 语义：**保留基线 + 最新 N 个增量**。若增量数 > N：
1. 把"基线 + 前 (总数−N) 个增量"恢复到 staging 并校验状态；
2. 用该 staging 生成**新的全量基线**（复用 snapshot）；
3. 保留最新 N 个增量，重新编号并同步改写增量文件内 id/sha256/size；
4. manifest 原子替换后删除被淘汰的旧文件。
因此 prune 后链仍然连续（新基线 `binlog_end` = 第一个保留增量的
`binlog_start`），下一次 `incr` 可继续追加；行级状态缓存会在需要时从链重建。

## 4. 恢复后的校验清单

`restore` 在安装目标前完成以下全部校验（任一失败 → 报错、目标不变）：

1. manifest schema/format/lightdb 版本兼容；
2. 链段 id 连续、首段 full、binlog 范围连续（`incr[i].start == incr[i-1].end`）；
3. 每个段文件存在、大小/sha256 与 manifest 一致；基线 snapshot manifest sha 一致；
4. 基线用 `restore_snapshot(verify=True)` 恢复到 staging（行数/关键值/
   SHOW CREATE/自增/WAL=0 由 snapshot 自检覆盖）；
5. 顺序应用增量：DDL SQL（失败即报错）→ `deletes` → `inserts` → `updates`
   （delete+insert 完整行）；
6. 还原 `AUTO_INCREMENT` 下一值并 `checkpoint(force=True)`；
7. 重新采集恢复库状态指纹，与目标段 `state` **逐字段比较**（行数/首末行/
   全表摘要/SHOW CREATE/自增）；
8. WAL=0 校验；
9. 全部通过后才 `copytree`/替换目标目录（目标非空且无 `--force` 在**开始前**
   就拒绝）。

## 5. 量化（10k 行，本机 2026-10-29）

| 项 | 数值 |
|---|---|
| 基线快照大小 | **869,763 B ≈ 849.4 KB**（7 文件） |
| 基线 `init` 耗时 | **8.64 s**（同日另一轮负载下 4.97 s；含状态扫描 + snapshot + 校验） |
| 增量大小（500 更新 + 500 新增） | **53,440 B ≈ 52.2 KB**（≈基线的 **6.1%**） |
| 增量 `incr` 耗时 | **2.92 s**（另一轮负载下 11.42 s；含 10k 行全表扫描/差分 + 状态缓存写入） |
| 恢复链耗时（基线+1 增量，10,500 行） | **4.75 s**（另一轮 8.91 s；含恢复、行应用、状态重扫校验） |
| 恢复后校验 | WAL=0、状态指纹通过、行数 10,500 |
| 测试覆盖 | 18 passed（含 10k 量化用例，约 101s） |

说明：本机有并行负载，绝对秒数会漂移（同一天两轮：init 4.97–8.64s、
incr 2.92–11.42s、restore 4.75–8.91s）；**增量大小**是稳定指标（≈变化行
数据量），**增量耗时**主要被"全表扫描做行差分"主导（与表大小相关），这是
本方案换取 AUTO_INCREMENT 逐值正确的已知代价。若数据量继续增大，可把基线
hash/行缓存按表分片或改用引擎侧变更跟踪（见 §7）。

## 6. 不支持 / 限制

* **仅静默离线**：服务运行中拒绝（TCP 探测）；不处理并发写。
* **DDL 限制**：不支持 `TRUNCATE` / `RENAME` / `DROP DATABASE`（显式 5xx
  错误）；schema 变更必须能在 binlog 区间捕获到对应 DDL（默认 binlog 启用；
  绕过 Executor 的 `Database.create_table` 等直接 API 变更会被检测到但拒绝）。
* **基线/snapshot 限制**：沿用 `docs/SNAPSHOT_BACKUP.md`（单库、无跨库一致性）；
  恢复目标需空目录或 `--force`。
* **恢复粒度**：只能恢复到增量段边界（`--to N`/`--latest`），不做段内时间点。
* **状态缓存**：链内 `chain_state.json.gz` 保存逐行主键/摘要/行值用于差分
  （含数据内容，等同备份本身敏感级别）；prune 后删除，下一次 incr 会从链
  重建（多一次恢复开销）。
* **版本**：链与当前引擎版本必须完全一致（不做迁移）；格式变化显式失败。
* **多列 AUTO_INCREMENT**：恢复自增使用 `Table.set_auto_increment`（引擎按
  首个自增列语义），多自增列场景未纳入本原型测试。

## 7. 后续建议

1. 增量耗时与表大小线性相关（行差分扫描）；可改为读取 binlog 变更主键集合 +
   按主键抓取行镜像（省掉无条件全表摘要），但需解决 AUTO_INCREMENT 实际值
   的记录（引擎侧补 `effective_row` 字段）。
2. 持久化状态缓存/分表 hash 可降低大表开销；或引入引擎侧 change-tracking。
3. 支持在线快照 + 增量链（复用 `--online` quiesce），把"静默离线"升级为
   "服务运行中一致性快照"。
4. 加 PITR（段内 binlog 位置恢复）与链加密/去重。

## 8. 复跑与证据

```powershell
python -m pytest tests/test_incremental_backup_chain.py -q
python scripts/backup_chain.py bench --rows 10000 --changes 1000 `
    --json _analysis_tmp/incremental_backup_bench.json
```

证据：`_analysis_tmp/incremental_backup_bench.json`、`incremental_backup_bench.log`、
`incremental_chain_tests.log`。回归：backup/snapshot/replication 相关测试 +
`compileall`（见交付记录）。
