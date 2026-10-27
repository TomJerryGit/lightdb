# 二进制一致性快照备份 / 恢复（SNAPSHOT_BACKUP）

> P2 + P3 交付：`lightdb/backup/snapshot.py` + `scripts/snapshot_backup.py` /
> `scripts/snapshot_restore.py`。面向**整库/单表的物理级快照**（与既有的逻辑
> SQL 备份 `BackupManager` 并存、互不替代）。
>
> 适用：离线维护窗口、发版前归档、把一份"打开即可用"的库搬到另一台机器、
> 单表物理归档；P3 起支持**在线热备（quiesce）**与**跨版本兼容策略**。
> 不适用于：跨大版本（格式不兼容）迁移、增量备份。

---

## 1. 一句话模型

**checkpoint / quiesce → 文件级拷贝 → manifest（版本/表/行数/SHA256/样本/
自增/是否在线）→ 恢复前全量校验 → 恢复 → 逐表自检（行数/关键值/SHOW CREATE/
自增/WAL=0）**。

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

WAL 全部为 0 字节是"一致"的硬判据：离线快照对每张表执行
`Table.checkpoint()`（脏页 fsync → BTree 根 fsync → WAL truncate），
checkpoint 后仍非零（有未提交事务）就**拒绝生成快照**。
在线快照的 quiesce 窗口保证"拷贝时刻无并发写"，并在快照副本上最终确认
WAL=0（见 §3.1）。

---

## 2. 快速开始

```powershell
# 1) 离线整库快照（默认检测 127.0.0.1:8888 是否有服务，检测到会拒绝）
python scripts/snapshot_backup.py --data-dir data_demo --out backups/snap-20261027

# 2) 删除/迁移数据目录后恢复（目标目录非空默认拒绝）
python scripts/snapshot_restore.py --snapshot backups/snap-20261027 --data-dir data_restored

# 3) 只看快照内容（含 online 段 / 版本 / 表行数）
python scripts/snapshot_restore.py --snapshot backups/snap-20261027 --list

# 4) 单表快照 + 恢复（只含该表，可恢复到全新目录）
python scripts/snapshot_backup.py --data-dir data_demo --out snap-users --table users
python scripts/snapshot_restore.py --snapshot snap-users --data-dir data_users_only

# 5) 在线热备（quiesce）：需要定位服务进程（--server-pid 或 psutil 自动探测）
python scripts/snapshot_backup.py --data-dir data --out snap-online `
    --online --server-port 8899 --server-pid 12345
```

机器可读报告：`--json report.json`（backup/restore 都支持）。

Python API：

```python
from lightdb.backup.snapshot import create_snapshot, restore_snapshot

create_snapshot('data_demo', 'backups/snap1')            # 离线（服务检测默认开）
create_snapshot('data_demo', 'backups/snap2', online=True,
                live_db=server_db)                        # 进程内在线（写锁 quiesce）
restore_snapshot('backups/snap1', 'data_restored', force=False)
restore_snapshot('backups/snap1', 'data_restored',
                 allow_version_mismatch=True)             # 仅放行"格式兼容"的版本差异
```

---

## 3. 与运行中服务的协调（重要）

* **离线快照默认拒绝**：开始前 TCP 探测 `127.0.0.1` 上的一批端口（默认
  `8888`，可用 `--server-port 8899 --server-port 9000` 追加）。检测到监听 →
  `检测到 LightDB 服务正在监听 ...，拒绝快照`，退出码 **3**。
* **`--force`（离线兜底，不安全）**：仍执行但写 WARNING；**它只保证
  "本工具 checkpoint 后文件拷贝一致"，不保证与并发写入的实时性**。
  要在线一致性请用 `--online`（§3.1），或停机做离线快照。
* 自定义端口：端口检测无法猜到你改过的端口，**必须显式传 `--server-port`**；
  确认服务已停可用 `--no-server-check`（不建议）。
* 被其它进程（非 LightDB 服务）占用数据文件时，打开 Database 会失败并
  显式报错（不会拷出半套文件）。

退出码：`0` 成功 / `1` 失败 / `2` 参数错误 / `3` 拒绝（离线模式服务监听、
目标非空未加 `--force`）/ `5` 在线 quiesce 无法建立（无进程、权限被拒、
等锁超时）——两个脚本一致。

### 3.1 在线热备（`--online`，quiesce）

`--online` 在**写入被阻断的窗口**内完成"WAL 落盘 + 文件拷贝"，窗口结束立刻
释放，服务继续接受写入。两种后端：

| 后端 | 触发方式 | 阻断手段 | 一致性保证 | 适用 |
|---|---|---|---|---|
| **进程内写锁** | Python API：`create_snapshot(online=True, live_db=server_db)` | 按表名排序获取所有目标表 `Table._lock`（写锁）→ `db.checkpoint()` → 拷贝 → 释放 | 窗口内无写入；checkpoint 后 WAL=0；拷贝为同一时刻 | 服务与备份工具同进程（嵌入式/运维工具/测试） |
| **跨进程 suspend** | CLI `--online`（`--server-pid` 或 psutil 按端口自动探测） | OS 级暂停服务进程 → 原样拷贝（crash-consistent 镜像）→ 恢复进程 → **在快照副本上**做 WAL 恢复 + checkpoint | 冻结期间无写入；副本恢复后是确定的已提交状态（WAL=0），未提交事务被 undo | 独立服务进程；需要 OS 进程暂停权限 |

一致性与边界：

* **无半提交**：窗口内写事务被完全阻断；跨进程 suspend 模式拷贝到的是
  崩溃一致镜像，副本恢复时已提交的 redo、未提交的 undo，不会出现"一半写入"
  的数据进入快照（测试用成对行语句验证：快照中每对行要么都在、要么都不在）。
* **快照后写入不丢**：窗口释放后服务继续；已提交的新写全部保留（测试实测
  快照 1,236 行后继续写到 1,566 行，最终 live 行数 == 2×总提交数）。
* **阻塞时长 = checkpoint + 拷贝**（不含哈希/副本恢复；后者在窗口外）。
  10k 行实测阻塞窗口 **28.1 ms**（checkpoint 7.0 ms + 拷贝 20.8 ms），
  并发写客户端最多等这一窗口（见 §6）。
* **与 `--force` 的区别**：`--force` 只是"忽略端口检测"，拷贝时服务仍在写，
  结果可能不一致；`--online` 真正阻断写入后再拷贝，窗口结束立即恢复。
* **受限环境**：跨进程 suspend 在禁止 `PROCESS_SUSPEND_RESUME` 的沙箱/令牌
  下会显式 `OnlineQuiesceError`（CLI 退出码 5），**绝不假装 quiesce 成功**；
  此时可改用进程内 `live_db` 后端，或停机做离线快照。

---

## 4. manifest.json 格式（`schema = lightdb-binary-snapshot/1`）

```jsonc
{
  "schema": "lightdb-binary-snapshot/1",
  "format_version": 1,                     // 格式兼容性判据（不兼容 -> 无条件失败）
  "lightdb_version": "4.0.0",              // 版本差异策略见 §5.1
  "created_at": "2026-10-27T16:02:13+08:00",
  "source": {"data_dir": "...", "host": "...", "platform": "...", "python": "..."},
  "mode": "full" | "single_table",
  "table_filter": null | "users",
  "checkpoint": {
    "tables_checked": 2, "tables_total": 2,
    "wal_before_bytes": {"users": 123, "orders": 0},
    "wal_after_bytes":  {"users": 0,   "orders": 0},
    "all_wal_zero": true,
    "strategy": "checkpoint -> copy / quiesce 窗口 + 副本恢复"
  },
  "online": {                              // P3：在线热备信息
    "enabled": true,
    "backend": "in-process-write-lock" | "process-suspend",
    "quiesced_pid": 12345,
    "blocked_window_s": 0.0281,           // 写入被阻断的窗口
    "checkpoint_s": 0.0070, "copy_s": 0.0208, "lock_wait_s": 0.0,
    "recovered_on_copy": true,             // 跨进程模式：副本上做过 WAL 恢复
    "resume_ok": true
  },
  "tables": [{
    "name": "users", "row_count": 3,
    "columns": [{"name": "id", "type": "INT", "null": "NO", "key": "PRI", ...}],
    "primary_key": ["id"],
    "show_create": "CREATE TABLE users (\n  id INT NULL PRIMARY KEY,\n ...)",
    "auto_increment_next": {"id": 4},
    "sample_first": [[1, "张三", 90], ...],
    "sample_last":  [[3, "carol", 70], ...],
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

离线快照的 `online` 段为 `{"enabled": false, "backend": null}`。

---

## 5. 恢复流程与自检

1. 读 manifest：JSON 合法性、`schema`/`format_version`、必需字段；
2. **版本兼容校验**（§5.1）；
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

### 5.1 跨版本兼容策略（P3）

| 情况 | 默认 | `--allow-version-mismatch` |
|---|---|---|
| `schema` / `format_version` 不兼容 | `ManifestError`（显式失败） | **仍失败**（该开关只放行"格式兼容"的版本差异，不能强开不兼容格式） |
| lightdb 版本不同（同 schema/format） | `VersionMismatchError`（显式失败） | 放行，并在报告 `version_check.warnings` / `warnings` 里写 **WARNING**；恢复后仍逐表自检，物理格式若实际不兼容会在打开时显式报错 |

测试覆盖两条路径：伪造 lightdb 版本 + 同格式 → 默认失败、放行后 WARNING 且
恢复成功；伪造 `format_version=999` → 即使放行也 `ManifestError`，且目标
目录不被创建。

---

## 6. 实测性能（2026-10-27，本机 8 逻辑核，工作区含他人未提交改动）

| 场景 | 数据规模 | 建库耗时 | 快照 / 阻塞 | 快照体积 | 恢复+自检 | 结果 |
|---|---|---:|---|---:|---:|---|
| 离线快照 | 10,000 行（1 表 id/v） | 7.21 s | **3.02 s** | 600.8 KB | **2.57 s** | 行数/SHOW CREATE/自增/WAL=0 一致 |
| 离线快照 | 100,000 行（1 表 id/v） | 65.97 s（1,516 rows/s） | **65.86 s** | 6.14 MB | **43.69 s** | 100,000 行一致，WAL=0 |
| **在线热备（进程内写锁）** | 10,000 行 | 11.02 s | **阻塞窗口 0.0281 s**（checkpoint 0.0070 + 拷贝 0.0208，锁等待 0） | 600.8 KB | 13.37 s（含副本后处理） | 10,000 行一致，WAL=0 |
| **在线热备（真实 server + 并发写）** | 快照 1,236 行（618 事务），随后继续写 | — | **阻塞窗口 0.0168–0.0283 s** | — | — | 无半提交；快照后写到 1,566 行全部保留 |

说明：
* **在线阻塞窗口 = checkpoint + 文件拷贝**；SHA256 哈希与副本 WAL 恢复/采集
  都在窗口外做，因此 10k 行的窗口只有 **~28 ms**，客户端最多停这么久。
* "快照+后处理 13.37 s" 不是阻塞时间：其中包含在**快照副本**上打开引擎、
  跑 `COUNT(*)`/`SHOW CREATE`/样本查询（当前引擎这些查询本身较慢）；
  服务端在窗口结束即恢复服务，不受这部分影响。
* 100k 离线数字受并发负载影响（引擎写入仅 1,516 rows/s）；6.14 MB 的
  **文件拷贝 + 全量 SHA256 本身 < 1 s**，快照大头是 SQL 采集/校验查询与
  checkpoint，不代表纯 I/O 成本。
* 恢复耗时 = 校验全部 SHA256 + 拷贝 + 打开库逐表自检。

---

## 7. 边界与已知限制

* **`--force` 不是在线热备**：只跳过端口检测，不阻断写入；要在线一致性用
  `--online`。`--online` 的阻塞窗口虽短，但确实会阻塞写入（窗口内客户端
  变慢/等待），数据库越大窗口越长。
* **跨进程 suspend 需要 OS 权限**：受限沙箱/受限令牌下 `psutil.suspend()`
  会被拒绝 → 显式 `OnlineQuiesceError`（CLI 退出码 5），不会假成功；此时用
  进程内 `live_db` 后端或停机快照。
* **端口检测只覆盖显式传入端口**（默认 8888）；自定义端口需 `--server-port`。
* **跨版本**：默认拒绝；`--allow-version-mismatch` 只放行 schema/format 兼容
  的版本差异，且仍有 WARNING；格式不兼容永远显式失败。
* 数据目录中的**非数据文件**（审计日志、`backups/`、自定义配置等）不进入
  快照；快照只含 `_meta.json` / `root.password` / 每表 `.dat .btroot .free
  .autoinc .wal`。
* 单表快照的 `_meta.json` 是过滤后的（只保留该表、清空 views），恢复到全新
  目录后是一个"只含该表"的单库；不能直接合并进已有库（合并请走 SQL）。
* 快照不含 binlog/主从位点信息；复制拓扑需单独处理。
* 大库（>数 GB）按文件整份拷贝 + 全量 SHA256，耗时 O(数据量)；无增量快照。

---

## 8. 测试与回归

```powershell
# 20 个用例（100k 行默认 skip；设 LIGHTDB_SNAPSHOT_100K=1 显式跑）
python -m pytest tests/test_binary_snapshot.py -q
$env:LIGHTDB_SNAPSHOT_100K='1'; python -m pytest tests/test_binary_snapshot.py -q -s -k 100k

# 回归（backup/restore/checkpoint/vacuum/P5 + 快照）
python -m pytest tests/test_binary_snapshot.py tests/test_backup_restore.py `
    tests/test_checkpoint_wal.py tests/test_vacuum_pages.py `
    tests/test_wal_checkpoint_p5.py -q
```

P3 新增覆盖：
* 跨版本两条路径（同格式放行 + WARNING；格式不兼容即使放行也失败）；
* 在线热备：真实 `MySQLServer` + 并发 pymysql 写客户端 → 快照点一致
  （无半提交、WAL=0）、快照后写入不丢；跨进程 suspend 后端管道
  （monkeypatch 模拟 suspend/resume）；quiesce 失败显式报错；定位不到服务
  进程显式失败；10k 行阻塞/总时长量化。
