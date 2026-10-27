# 二进制一致性快照备份 / 恢复（SNAPSHOT_BACKUP）

> P2 交付：`lightdb/backup/snapshot.py` + `scripts/snapshot_backup.py` /
> `scripts/snapshot_restore.py`。面向**整库/单表的物理级快照**（与既有的逻辑
> SQL 备份 `BackupManager` 并存、互不替代）。
>
> 适用：离线维护窗口、发版前归档、把一份"打开即可用"的库搬到另一台机器、
> 单表物理归档。不适用于：在线热备（未冻结并发写入）、跨大版本升级迁移。

---

## 1. 一句话模型

**checkpoint → 文件级拷贝 → manifest（版本/表/行数/SHA256/样本/自增）→
恢复前全量校验 → 恢复 → 逐表自检（行数/关键值/SHOW CREATE/自增/WAL=0）**。

快照目录结构：

```
<snapshot>/
├── manifest.json          # 唯一元数据：见 §4
└── data/                  # 与 data-dir 同名的物理文件（可直接拷回）
    ├── _meta.json
    ├── root.password      # 若存在
    ├── users.dat  users.btroot  users.free  users.autoinc  users.wal(0B)
    └── orders.dat orders.btroot orders.free orders.wal(0B)
```

WAL 全部为 0 字节是"一致"的硬判据：快照前对每张表执行
`Table.checkpoint()`（脏页 fsync → BTree 根 fsync → WAL truncate），
checkpoint 后仍非零（有未提交事务）就**拒绝生成快照**。

---

## 2. 快速开始

```powershell
# 1) 整库快照（默认检测 127.0.0.1:8888 是否有服务，检测到会拒绝）
python scripts/snapshot_backup.py --data-dir data_demo --out backups/snap-20261027

# 2) 删除/迁移数据目录后恢复（目标目录非空默认拒绝）
python scripts/snapshot_restore.py --snapshot backups/snap-20261027 --data-dir data_restored

# 3) 只看快照内容
python scripts/snapshot_restore.py --snapshot backups/snap-20261027 --list

# 4) 单表快照 + 恢复（只含该表，可恢复到全新目录）
python scripts/snapshot_backup.py --data-dir data_demo --out snap-users --table users
python scripts/snapshot_restore.py --snapshot snap-users --data-dir data_users_only
```

机器可读报告：`--json report.json`（backup/restore 都支持）。

Python API：

```python
from lightdb.backup.snapshot import create_snapshot, restore_snapshot
create_snapshot('data_demo', 'backups/snap1')                  # 服务检测默认开
restore_snapshot('backups/snap1', 'data_restored', force=False)
```

---

## 3. 与运行中服务的协调（重要）

* **默认拒绝**：快照/恢复开始前会 TCP 探测 `127.0.0.1` 上的一批端口
  （默认 `8888`，可用 `--server-port 8899 --server-port 9000` 追加）。
  检测到监听 → 打印
  `检测到 LightDB 服务正在监听 ...，拒绝快照`，退出码 **3**。
* **`--force` 覆盖**：仍执行，但在报告/输出里写 WARNING。**它只保证
  "本工具 checkpoint 后文件拷贝一致"，不保证与并发写入的实时性**——在线
  强制快照可能得到"快照开始时刻之后"的混合状态。生产环境请先停服务。
* 自定义端口：端口检测无法猜到你改过的端口，**自定义端口必须显式传
  `--server-port`**；确认服务已停可用 `--no-server-check`（不建议）。
* 被其它进程（非 LightDB 服务）占用数据文件时，打开 Database 会失败并
  显式报错（不会拷出半套文件）。

退出码：`0` 成功 / `1` 失败（未预期错误、文件被占等）/ `2` 参数错误 /
`3` 拒绝（服务监听、目标非空未加 `--force` 等）——备份/恢复脚本一致。

---

## 4. manifest.json 格式（`schema = lightdb-binary-snapshot/1`）

```jsonc
{
  "schema": "lightdb-binary-snapshot/1",
  "format_version": 1,
  "lightdb_version": "4.0.0",              // 版本兼容校验基准
  "created_at": "2026-10-27T16:02:13+08:00",
  "source": {"data_dir": "...", "host": "...", "platform": "...", "python": "..."},
  "mode": "full" | "single_table",
  "table_filter": null | "users",
  "checkpoint": {
    "tables_checked": 2, "tables_total": 2,
    "wal_before_bytes": {"users": 123, "orders": 0},
    "wal_after_bytes":  {"users": 0,   "orders": 0},
    "all_wal_zero": true
  },
  "tables": [{
    "name": "users", "row_count": 3,
    "columns": [{"name": "id", "type": "INT", "null": "NO", "key": "PRI", ...}],
    "primary_key": ["id"],
    "show_create": "CREATE TABLE users (\n  id INT NULL PRIMARY KEY,\n ...)",
    "auto_increment_next": {"id": 4},
    "sample_first": [[1, "张三", 90], ...],   // 按主键升序前 3 行
    "sample_last":  [[3, "carol", 70], ...],  // 按主键降序前 3 行
    "sample_sql": {"first_sql": "...", "last_sql": "..."},
    "files": ["users.dat", "users.btroot", "users.free", "users.autoinc", "users.wal"]
  }],
  "tables_count": 2, "row_total": 10,
  "files": [{"name": "users.dat", "path": "data/users.dat",
             "size": 12288, "sha256": "..."}],
  "total_files": 11, "total_bytes": 26317,
  "users": ["root"],
  "warnings": [],
  "limitations": ["..."]
}
```

---

## 5. 恢复流程与自检

1. 读 manifest：JSON 合法性、`schema`/`format_version`、必需字段；
2. **版本校验**：`manifest.lightdb_version` 必须等于当前 `lightdb.__version__`，
   否则 `VersionMismatchError`（物理页/元数据格式可能变化）。确认可兼容时
   才加 `--allow-version-mismatch`（记录在报告 `mismatch_allowed=true`）；
3. 服务/目标检查：服务监听 → 拒绝；目标目录非空 → 拒绝，`--force` 时把
   旧目录**改名保留**为 `<target>.replaced-<时间戳>`（不删除旧数据）；
4. **先校验后拷贝**：逐文件比对 manifest 的 size + SHA256，任何不符 →
   `SnapshotIntegrityError`，**目标目录一个字节都不动**；
5. 拷贝 → 再校验一次 size/SHA256；
6. 恢复后自检（`--no-verify` 才能关闭，不建议）：
   * 表集合一致、逐表 `COUNT(*)` == manifest；
   * `SHOW CREATE TABLE` 逐字节一致；
   * 首/尾关键值样本一致；
   * `auto_increment_next` 一致（恢复后继续分配不跳号/不重用）；
   * 关闭后所有 `<table>.wal` == 0 字节。
7. 任一步失败 → 显式异常/退出码 1，不返回"成功"。

---

## 6. 实测性能（2026-10-27，本机 8 逻辑核，工作区含他人未提交改动）

| 数据规模 | 建库耗时 | 快照耗时 | 快照体积 | 恢复+自检耗时 | 结果 |
|---|---:|---:|---:|---:|---|
| 10,000 行（1 表，id/v 两列） | 3.88 s | **3.27 s** | 600.8 KB | **3.65 s** | 行数/SHOW CREATE/自增/WAL=0 一致 |
| 100,000 行（1 表，id/v 两列） | 65.97 s（1,516 rows/s） | **65.86 s** | 6.14 MB | **43.69 s** | 100,000 行一致，WAL=0 |

说明（100k 数字必须结合负载读）：
* 100k 行测量时本机有其它 agent 并行跑测试（引擎写入只有 1,516 rows/s，约为
  空闲时的 1/3）。快照/恢复耗时主要由**打开库后的 SQL 采集/校验查询**
  （`COUNT(*)`、`SHOW CREATE`、首尾样本扫描）与 checkpoint 构成；
  6.14 MB 的**文件拷贝 + 全量 SHA256 本身 < 1 s**，该部分才是随数据量线性的
  纯 I/O 成本。
* 10k 档在同一台机器上快照 3.27 s / 恢复 3.65 s，可看作是轻负载参考值；
  100k 档偏慢是当前机器/负载下的上界，不代表算法复杂度。
* 恢复耗时 = 校验全部 SHA256 + 拷贝 + 打开库逐表自检。

---

## 7. 边界与已知限制

* **不是在线热备**：不会冻结正在写入的事务；`--force` 仅避开端口检测，
  不消除不一致风险。请停机快照。
* **端口检测只覆盖显式传入端口**（默认 8888）；自定义端口需
  `--server-port`，否则可能漏检。
* **跨版本默认拒绝**：物理格式随引擎版本可能变化；`--allow-version-mismatch`
  只是放开检查，不保证能打开（打开失败会显式报错）。
* 数据目录中的**非数据文件**（审计日志、`backups/`、自定义配置等）不进入
  快照；快照只含 `_meta.json` / `root.password` / 每表 `.dat .btroot .free
  .autoinc .wal`。
* 单表快照的 `_meta.json` 是过滤后的（只保留该表、清空 views），恢复到全新
  目录后是一个"只含该表"的单库；不能直接合并进已有库（合并请走 SQL）。
* 快照不含 binlog/主从位点信息；复制拓扑需单独处理。
* 大库（>数 GB）会按文件整份拷贝 + 全量 SHA256，耗时为 O(数据量)；无增量快照。

---

## 8. 测试与回归

```powershell
python -m pytest tests/test_binary_snapshot.py -q            # 13 个（100k 默认 skip）
$env:LIGHTDB_SNAPSHOT_100K='1'; python -m pytest tests/test_binary_snapshot.py -q -s -k 100k
# 回归
python -m pytest tests/test_backup_restore.py tests/test_checkpoint_wal.py `
    tests/test_vacuum_pages.py tests/test_wal_checkpoint_p5.py -q
```

测试覆盖：manifest/文件哈希字段、快照→删库→恢复全链路（行数/关键值/
SHOW CREATE/自增/WAL=0）、损坏 manifest、SHA256 不符（且目标目录不被触碰）、
版本不兼容、真实 MySQLServer 监听时拒绝、`--force` 告警覆盖、单表快照、
目标非空保护、10k/100k 量化。
