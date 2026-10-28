# 生产就绪说明：/metrics + 慢查询日志 + 审计轮转（P1-A / task-70）

> 对应代码：`lightdb/net/observability.py`（采集/渲染/慢查询/轮转配置）、
> `lightdb/net/metrics_server.py`（内嵌 HTTP 端点）、`lightdb/net/server.py`
> （连接/语句/错误打点、随服务启动端点）、`lightdb/sql/executor.py`
> （慢查询记录、`__SLOW_QUERY_LOG__` 虚拟表、审计文件轮转）、
> `lightdb/db/database.py`（checkpoint 计数、慢查询器生命周期）。
> 测试：`tests/test_production_metrics.py`（12 用例）。
>
> 本文件只描述**已实现且被测试覆盖**的能力；未实现的（如 Prometheus
> 拉取鉴权、慢查询按用户配额、审计加密）在文末"未做/边界"里如实列出。

---

## 1. `/metrics` 端点（Prometheus text 格式）

### 1.1 启动

内嵌在 `MySQLServer` 生命周期里，**默认关闭**；用环境变量显式开启（不改
config.json，避免与既有配置契约冲突）：

```bash
# Windows PowerShell
$env:LIGHTDB_METRICS_PORT = "9091"      # >0 固定端口；0 = OS 动态端口；缺省/负数 = 关闭
$env:LIGHTDB_METRICS_HOST = "127.0.0.1" # 默认回环地址；需要外部抓取时显式改
python -m lightdb.main --mode single --host 127.0.0.1 --port 8888
```

```bash
curl -s http://127.0.0.1:9091/metrics | head
curl -s http://127.0.0.1:9091/health           # -> ok
curl -s 'http://127.0.0.1:9091/metrics?format=json'   # 结构化快照（排障/测试）
```

端点行为：

| 路径 | 说明 |
|---|---|
| `GET /metrics` | Prometheus text（`Content-Type: text/plain; version=0.0.4`） |
| `GET /metrics?format=json` | 同一份数据的 JSON（runtime/connections/slow/wal/checkpoint） |
| `GET /health` | `ok`（存活探针，不做 DB 查询） |
| 其它路径 | 404；非 GET → 405 |

端点**启动失败不阻断数据库**（端口占用等只告警并继续服务）。

### 1.2 指标清单（全部真实采集；无数据时 0 + `#` 说明）

| 指标 | 类型 | 数据源 |
|---|---|---|
| `lightdb_up` | gauge | 常量 1（端点存活） |
| `lightdb_connections_active` | gauge | `MySQLServer.connection_stats()` 真实准入计数 |
| `lightdb_connections_active_by_ip{ip}` | gauge | 同上（按来源 IP） |
| `lightdb_connections_total` | counter | `verify_request` 准入通过次数 |
| `lightdb_connections_rejected_total` | counter | 连接数/每 IP 超限 + 白名单拒绝 |
| `lightdb_auth_failures_total` | counter | 登录失败（`_auth_failure`） |
| `lightdb_queries_total` | counter | COM_QUERY + COM_STMT_EXECUTE（`_dispatch` 打点） |
| `lightdb_errors_total` | counter | 所有 ERR 包（`_send_packet` 拦截，含鉴权/权限/执行/语法） |
| `lightdb_errors_by_code_total{errno}` | counter | 同上，按 MySQL errno 分类 |
| `lightdb_qps{window="5s"/"60s"}` | gauge | 秒级桶滑窗（真实语句数/窗口） |
| `lightdb_uptime_seconds` | gauge | 服务启动至今 |
| `lightdb_slow_queries` | gauge | 执行器 `SlowQueryLog` 已记录慢查询数 |
| `lightdb_slow_query_max_ms` | gauge | 已记录慢查询的最大耗时 |
| `lightdb_slow_query_threshold_ms` | gauge | 当前阈值（`LIGHTDB_SLOW_QUERY_MS`） |
| `lightdb_slow_query_top_count{rank,cmd,sql}` | gauge | top10（归一化 SQL 前缀作标签） |
| `lightdb_slow_query_top_max_ms{rank,cmd,sql}` | gauge | 对应 top 的最大耗时 |
| `lightdb_slowlog_file_bytes` / `_rotations_total` / `_write_errors_total` | gauge/counter | 慢查询文件真实大小、轮转次数、写失败次数 |
| `lightdb_wal_bytes{table}` / `lightdb_wal_bytes_total` | gauge | 各表 `wal.size_bytes()` 真实值（checkpoint 后会归 0） |
| `lightdb_checkpoints_total` / `lightdb_checkpoint_tables_total` | counter | `Database.checkpoint()` 真实调用/退休表数 |
| `lightdb_tables` | gauge | 当前表数 |
| `lightdb_binlog_events` / `lightdb_binlog_position_bytes` | gauge | 真 append-only binlog（未启用/未创建时 0 + 说明；scrape **不会**创建 binlog 文件） |
| `lightdb_backup_files` / `lightdb_backup_bytes` / `lightdb_snapshot_dirs` | gauge | 扫描备份目录（`LIGHTDB_BACKUP_DIR` > `<data_dir>/../backups`）真实产物 |
| `lightdb_process_memory_rss_bytes` | gauge | psutil RSS（不可用时 0 + 注释，线程数用 threading 兜底） |
| `lightdb_process_threads` | gauge | 进程真实线程数 |

取值原则：**取不到就输出 0 并用 `#` 注释说明来源/原因**（例如"备份目录
不存在"、"binlog 未启用"、"无慢查询"），不编造"看起来正常"的数字。

### 1.3 安全边界

* 默认只监听 `127.0.0.1`（需要外部抓取时显式设置 HOST，并自行做网络 ACL）；
* 端点无鉴权（Prometheus 标准做法；生产应放在专用内网/反向代理后）；
* 指标标签里的 SQL 已归一化（字面量→`?`、数字→`0`、截断 80 字符），
  不暴露 SQL 原文全文；慢查询日志本体做口令脱敏（见 §2.3）。

---

## 2. 慢查询日志

### 2.1 配置（环境变量；不新增 config.json 字段）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIGHTDB_SLOW_QUERY_MS` | `500` | 阈值（毫秒）；`0` = 记录所有语句（测试/排障） |
| `LIGHTDB_SLOWLOG_ENABLED` | `1` | `0` = 只进内存环形缓冲、不落盘 |
| `LIGHTDB_SLOWLOG_FILE` | `<data_dir>/slow.log` | 落盘路径 |
| `LIGHTDB_SLOWLOG_MAX_BYTES` | `10485760`（10MB） | 单文件阈值，超过后轮转 |
| `LIGHTDB_SLOWLOG_KEEP` | `3` | 保留历史文件数（`slow.log.1..N`） |
| `LIGHTDB_SLOWLOG_BUFFER` | `200` | 内存环形缓冲条数 |
| `LIGHTDB_SLOWLOG_SQL_MAX` | `500` | SQL 截断字符数 |
| `LIGHTDB_SLOWLOG_FSYNC` | `0` | 每条记录是否 fsync（慢查询稀少时可开） |

### 2.2 记录内容与读取

每条慢语句记录：`ts`（时间）/ `user` / `elapsed_ms`（真实耗时）/ `cmd` /
`success`（1/0）/ `errno`（失败时的 MySQL errno）/ `sql`（截断+脱敏原文）。
**成功与失败语句都记**（失败带 errno，便于排"又慢又错"的语句）。

三种读法（同一份数据）：

```sql
-- 1) SQL 虚拟表（只读；进程内/网络均可）
SELECT ts, user, elapsed_ms, cmd, success, errno, sql
FROM __SLOW_QUERY_LOG__ ORDER BY elapsed_ms DESC LIMIT 10;
```

```python
# 2) 进程内 API
executor.slowlog.snapshot()   # total/max/阈值/top10/文件大小/轮转次数
executor.slowlog.rows()       # ts,user,elapsed_ms,cmd,success,errno,sql
```

3) 文件：`<data_dir>/slow.log`（JSONL，一行一条；轮转文件 `slow.log.1..N`）。

> 为什么没有 `SHOW SLOW QUERIES`：`lightdb/sql/parser.py` 不在本任务改动范围；
> 虚拟表 + 文件已覆盖同一份真实数据。

### 2.3 脱敏与轮转（崩溃安全）

* **口令脱敏**：`IDENTIFIED BY '...'` / `PASSWORD='...'` / `SET PASSWORD FOR ...`
  里的字面量替换成 `'***'`，慢查询日志即使被收集也不会泄露凭据；
  审计日志按既有契约保留原文（`SHOW AUDIT LOG` 行为不变）。
* **轮转**：每次 append+flush 成功后检查 `bytes >= max_bytes` → `slow.log`
  →`slow.log.1`→…（`os.replace` 从旧到新），超出 `keep` 的删除；轮转后立刻
  建回空的当前文件。任何一步失败只告警 + `write_errors` 计数，**当前文件
  继续写、内存记录不丢**；崩溃最坏留下编号空洞，不会让已写内容消失。
* 内存环形缓冲只保留最近 `BUFFER` 条（默认 200）；top 榜按归一化 SQL 聚合，
  最多 1000 个不同形态（超出淘汰最旧形态）。

---

## 3. 审计轮转（`SHOW AUDIT LOG` 行为不变）

### 3.1 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIGHTDB_AUDIT_MAX_BYTES` | `10485760`（10MB） | 单文件大小阈值（`<=0` 关闭按大小） |
| `LIGHTDB_AUDIT_MAX_RECORDS` | `100000` | 单文件条数阈值（`<=0` 关闭按条数） |
| `LIGHTDB_AUDIT_KEEP` | `5` | 保留历史文件数（`audit.log.1..N`） |

### 3.2 语义

* **内存环形缓冲 `recent`（2000 条）与 `SHOW AUDIT LOG` 完全不变**：
  轮转只影响磁盘文件，不影响查询行为与返回形状；
* 轮转发生在**本次批量 append 成功之后**（整批已落盘）：先删最旧
  `audit.log.N`，再 `N-1→N` … 最后 `audit.log→.1`；任何一步失败只告警、
  继续写当前文件 → **不丢任何记录**；
* 轮转后立刻建回空 `audit.log`（"当前审计日志"始终存在）；
* **重启可查询**：启动时按 `audit.log.N … .1, audit.log` 顺序载入内存缓冲，
  因此轮转后的历史仍能通过 `SHOW AUDIT LOG` 读到（受 2000 条环形上限约束）；
* **崩溃安全**：每批 append+flush 后才轮转；半行/缺当前文件/编号空洞在
  载入时跳过坏行、不整文件失败（测试覆盖"追加半行""删掉当前文件"两种
  崩溃形态后仍能查询已落盘记录）。

---

## 4. 验证

```bash
# P1-A 新测试（12 例：指标真实计数/端点行为/慢查询阈值与脱敏/审计轮转/崩溃容错）
python -m pytest tests/test_production_metrics.py -q

# P1-B 新测试（12 例连接池复用/超限/排队/断连/长事务/指标/吞吐 + 10 例复制
# SHOW BINARY LOGS/全量同步/断点续传/幂等/损坏显式/1k 延迟/CLI）
python -m pytest tests/test_connection_pool.py tests/test_replication_prototype.py -q

# 回归（协议/事务/审计/批量/安全/备份/E3 兼容等）
python -m pytest tests/test_wire_transaction.py tests/test_batch14.py \
  tests/test_v4_sql_features.py tests/test_batch8.py tests/test_batch15.py \
  tests/test_mysql_compat_e3.py tests/test_type_constraints.py \
  tests/test_phase0_integrity.py -q

# 语法检查（Windows 沙箱下把 pycache 指到可写目录）
$env:PYTHONPYCACHEPREFIX = "$PWD\tmp_pycache"
python -m compileall -q lightdb tests/test_production_metrics.py \
  tests/test_connection_pool.py tests/test_replication_prototype.py \
  scripts/replica_apply.py
```

实测（本机，2026-10-28）：`/metrics` 抓取一次约数毫秒；慢查询阈值 0ms 时
可完整记录并轮转；审计 10 条/文件阈值下 40 条记录轮转后文件总行数 = 40、
`SHOW AUDIT LOG` 行数不变、重启后仍为 40 行。

---

## 5. 未做 / 边界（如实列出，不夸大）

1. `/metrics` **无鉴权**、无 TLS：默认回环 + 生产侧应当用内网/反代/ACL；
   若需要 token 鉴权，可在 `metrics_server.py` 增加 `LIGHTDB_METRICS_TOKEN`
   （未实现，避免把鉴权语义做一半）；
2. 慢查询 **只记录最外层语句**（子查询/CTE 不重复计）；没有"按用户/库
   聚合"的独立指标（top 榜按 SQL 形态）；
3. 慢查询 SQL 截断到 `LIGHTDB_SLOWLOG_SQL_MAX`（默认 500 字符）；超长 SQL
   的中段语义不可见（截断是显式的，不是静默丢）；
4. 审计轮转按"每批 append 后"检查阈值：文件可能略微超过阈值一个批次的
   大小（批量写入的代价，记录完整性优先）；
5. 备份/快照指标是**目录扫描**（只读、不缓存），大规模备份目录的 scrape
   开销随文件数增长；快照系统自己的元数据没有暴露（只统计产物大小）；
6. `lightdb/net/worker.py` / 多进程 worker 侧不启动 `/metrics`（指标在
   Master/单进程侧采集；多进程模式当前仍在生产化排期外）。

---

## 6. 连接池（P1-B / task-71）

### 6.1 实现与语义

`lightdb/net/pool.py::ConnectionPool`，由 `MySQLServer` 在 `__init__` 时创建、
`process_request` 把已 accept 的连接交给池。**池化的是"连接处理线程"，
不是会话**：

* 每个连接仍由 `MySQLHandler` 独占一个 worker 线程处理**整个生命周期**；
  `session`（事务句柄/用户变量/autocommit/当前库）按连接新建 →
  **会话隔离不变**；
* 连接结束后 worker 归还池，下一条连接**复用线程**（省线程创建/销毁）；
* 忙 worker 不会被心跳/超时回收 → **长事务/长查询期间连接一直占用一个
  worker 与一个准入名额，不会被复用或中途换线程**；
* 空闲超过 `idle_timeout` 且总数大于 `min_idle` 的 worker 退出（缩容）；
* 健康检查按 `health_interval` 清理意外退出的 worker 并补足 `min_idle`，
  `dead_workers` 计数可见；
* 默认启用；`LIGHTDB_POOL_ENABLED=0` 退回"每连接一线程"旧行为。

### 6.2 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIGHTDB_POOL_ENABLED` | `1` | `0` = 关闭池，退回每连接一线程 |
| `LIGHTDB_POOL_MAX_WORKERS` | `max_connections` | worker 线程上限 |
| `LIGHTDB_POOL_MIN_IDLE` | `1` | 常驻空闲 worker 下限 |
| `LIGHTDB_POOL_IDLE_TIMEOUT` | `60` | 空闲缩容阈值（秒，最小 1） |
| `LIGHTDB_POOL_OVERFLOW` | `reject` | 超限策略：`reject` 显式 1040 / `queue` 排队 |
| `LIGHTDB_POOL_QUEUE_TIMEOUT` | `5.0` | queue 模式等待上限（秒） |
| `LIGHTDB_POOL_QUEUE_SIZE` | `64` | 排队上限（满则拒绝） |
| `LIGHTDB_POOL_HEALTH_INTERVAL` | `5.0` | 健康检查最小间隔（秒） |
| `LIGHTDB_POOL_LONG_RUNNING_S` | `30` | "长连接/长事务"统计阈值（秒） |

超限时在**握手前**回 ERR 1040（`Too many connections (pool ...)`），客户端
（pymysql 等）拿到明确错误码；`queue` 模式在 TCP 已建立后等待，超时同样 1040。

### 6.3 指标

* `/metrics`：`lightdb_pool_workers{state="busy|idle|total"}`、
  `lightdb_pool_max_workers`、`lightdb_pool_workers_created_total`、
  `lightdb_pool_assignments_total`、`lightdb_pool_reused_total`、
  `lightdb_pool_rejected_total`、`lightdb_pool_queue_timeouts_total`、
  `lightdb_pool_idle_shrinks_total`、`lightdb_pool_dead_workers_total`、
  `lightdb_pool_jobs_queued`、`lightdb_pool_jobs_pending_accept`、
  `lightdb_pool_long_running`（附 overflow/min_idle 策略注释）；
* `SHOW STATUS`：`Pool_workers_total/busy/idle`、`Pool_max_workers`、
  `Pool_workers_created`、`Pool_assignments`、`Pool_reused`、`Pool_jobs_queued`、
  `Pool_jobs_pending_accept`、`Pool_queue_waits`、`Pool_queue_timeouts`、
  `Pool_rejected`、`Pool_idle_shrinks`、`Pool_dead_workers`、
  `Pool_long_running`、`Pool_overflow_policy`。

### 6.4 量化（本机 2026-10-28，SELECT 1 ×200/客户端，8 worker 池）

| 客户端数 | 总耗时 wall | 吞吐 QPS |
|---|---|---|
| 1 | 0.194 s | 1029.0 |
| 4 | 0.715 s | 1118.4 |
| 8 | 1.642 s | 974.7 |

诚实解读：wall 时间随客户端数近线性增长、QPS 基本持平 —— 单语句执行路径
受 GIL/串行会话调度限制，**连接池带来的是线程复用与资源上限（不再每连接
新建线程、可缩容、可排队/显式拒绝），不是查询并行加速**。短连接场景（反复
建连/断连）的收益最大。

---

## 7. binlog 复制原型（P1-B / task-71）

### 7.1 链路

```
主库 binlog 文件（append-only）
  ├─ SHOW BINARY LOGS            ← 复用既有 E3 实现（2 列 Log_name/File_size；
  │                                 binlog 未启用 → 1381，不伪造清单）
  ├─ SHOW MASTER STATUS          ← 当前 File/Position
  └─ SELECT * FROM __BINLOG_FETCH__ WHERE lsn > N LIMIT k
        （内部导出：pos/lsn/txn_id/event_type/table_name/data；
          lsn = 跨 binlog 文件的全局字节偏移，可跨轮转单调续传）
            ↓  pymysql（真实协议）
  目标库 Database/Executor（从库/备份节点）
        ├─ DDL → 目标 Executor 执行（幂等预判）
        └─ DML → 行镜像直写存储层（不触发目标侧触发器，与 SQL 线程一致）
            ↓
  replica_state.json（每批 flush+fsync 一次；原子 tmp+replace）
```

代码：`lightdb/net/replica_proto.py::ReplicaPrototype`；CLI：
`scripts/replica_apply.py`。

```bash
# 一次性追平（无新事件则 0 事件退出；退出码 0/1/2）
python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
    --target-data-dir /tmp/replica-data

# 持续跟随（Ctrl+C 停止）
python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
    --target-data-dir /tmp/replica-data --follow --interval 0.2

# 只看主库 binlog 清单/位点（--json 便于脚本消费）
python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
    --target-data-dir /tmp/replica-data --show-binary-logs --json
```

### 7.2 幂等 / 断点续传 / 错误显式

* **DML 幂等**：INSERT 主键冲突 → idempotent；UPDATE/DELETE 按主键
  affected=0 → idempotent；目标缺表/列不匹配 → `ReplicaApplyError`；
* **DDL 幂等**：CREATE TABLE 目标已存在、DROP TABLE 目标不存在、TRUNCATE、
  ALTER ADD 列已存在/ALTER DROP 列已不存在 → idempotent；其余错误显式
  `ReplicaApplyError`（带 pos/lsn/event_type/table 上下文）；
* **断点续传**：状态含 `{master_file, lsn, seq, applied, idempotent, errors,
  last_event_type, last_apply_ts}`；每批 apply 完 `flush+fsync` 一次（每事件
  不 fsync，Windows 上每条 fsync ≈0.2s 会让 1k 事件不可用）；崩溃最多回放
  最后一批事件，重放按幂等处理；
* **损坏事件显式**：非 6 列 / payload 非 JSON / 未知 event_type / 主库不可达 /
  状态文件损坏 / 起始 binlog 已被清理 → 明确异常，**绝不静默跳过**；
* apply 期间 `suspend_binlog()`：目标库不会把重放事件再写进自己的 binlog。

### 7.3 量化（本机 2026-10-28）

单线程 apply：主库 1001 条事件（1 DDL + 1000 INSERT）apply 耗时 **1.825s**
（约 **548.5 events/s**；主库写 1000 行耗时 1.042s，含网络往返）。延迟口径
为"拉取→应用完成"的处理延迟；**不伪造** MySQL `Seconds_Behind_Master`
（导出接口不含事件时间戳）。

---

## 8. P1-B 原型边界（如实列出）

1. **单主、无自动 failover**：主库不可达直接报错退出；没有选主/切主/多主；
2. **单线程 apply**：严格按事件顺序串行应用，无并行/无冲突解决；
3. **schema 必须一致**：DML 按行镜像 + 主键 apply；DDL 按 binlog 原始 SQL
   重放；不做 schema diff/修复；
4. **不触发目标侧触发器/外键级联**（行级复制的固有语义）；存储过程/函数
   副作用不在行镜像里；
5. **binlog 保留策略**：主库 binlog 被清理/重建后，断点续传拒绝继续
   （显式检测起始文件是否还在，不猜测位点）；
6. 复制状态文件在目标数据目录（非集群元数据），不做多副本/高可用；
7. 连接池的排队发生在已建立 TCP、未握手阶段（客户端会看到握手响应延迟），
   不是 MySQL 侧的真实"连接代理"；池不替代 `max_connections` 准入。
