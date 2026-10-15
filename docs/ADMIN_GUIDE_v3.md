==== BEGIN FILE: docs/ADMIN_GUIDE_v3.md ====

# LightDB v3.0 管理员指南

**版本**：3.0.0
**更新日期**：2025-02-01
**适用对象**：DBA、运维工程师

---

## 目录

1. 部署
2. 配置
3. 监控
4. 备份与恢复
5. 安全
6. 性能调优
7. 故障排查
8. 升级到 v4.0
9. 常见错误

---

## 1. 部署

### 1.1 硬件建议

v3.0 是单进程架构，推荐配置：

| 场景     | CPU   | 内存   | 磁盘      | 说明          |
|----------|:-----:|:------:|:---------:|---------------|
| 开发     | 2 核  | 4 GB   | 20 GB     | 单机          |
| 测试     | 4 核  | 8 GB   | 50 GB     | 单机          |
| 生产     | 8 核  | 16 GB  | 200 GB SSD| 单机 + 只读副本 |

注意：v3.0 无法利用多核，核数多于 4 无明显收益。如果有多核需求，请规划升级到 v4.0。

### 1.2 Docker 部署

单机版：

    docker run -d --name lightdb \
        -p 8888:8888 \
        -v /data/lightdb:/app/data \
        -e LIGHTDB_LOG_LEVEL=INFO \
        lightdb/server:3.0.0

使用 docker-compose：

    docker-compose -f docker-compose.v3.yml up -d

docker-compose.v3.yml 包含：

- lightdb 服务（8888）
- studio-web 服务（8080）
- prometheus（9090）
- grafana（3000）

### 1.3 K8s 部署

    kubectl apply -f k8s.yaml

注意：v3.0 的 k8s.yaml 使用镜像 `lightdb:1.0.0`，请替换为 `lightdb/server:3.0.0`。

### 1.4 系统参数优化

/etc/sysctl.conf：

    net.core.somaxconn = 65535
    net.ipv4.tcp_max_syn_backlog = 65535
    net.ipv4.tcp_tw_reuse = 1
    fs.file-max = 2097152
    vm.swappiness = 10

生效：

    sysctl -p

/etc/security/limits.conf：

    * soft nofile 65536
    * hard nofile 65536
    * soft nproc 65536
    * hard nproc 65536

### 1.5 部署验证

第一步，健康检查：

    lightdb-check -h 127.0.0.1 -P 8888 -u root

第二步，版本确认：

    mysql -h 127.0.0.1 -P 8888 -u root -e "SELECT VERSION()"
    -- 期望: 5.7.30-LightDB

第三步，状态：

    lightdbadmin status

---

## 2. 配置

### 2.1 配置文件（config.json）

    {
      "host": "0.0.0.0",
      "port": 8888,
      "max_connections": 100,

      "buffer_pool_size": 1000,
      "page_size": 4096,
      "data_dir": "/app/data",
      "backup_dir": "/app/backups",

      "workers": 1,
      "parallel_workers": 2,

      "slow_query_threshold": 0.5,
      "max_allowed_packet": 16777216,

      "log_level": "INFO",

      "wal_enabled": true,
      "autocommit": true
    }

### 2.2 关键参数说明

| 参数                  | 建议值              | 说明                       |
|-----------------------|---------------------|----------------------------|
| workers               | 1                   | v3.0 单进程，固定为 1      |
| parallel_workers      | 2                   | 单查询并行度               |
| buffer_pool_size      | 内存 / 4KB × 0.4    | 单位：页                   |
| max_connections       | 100-500             | 视并发而定                 |
| slow_query_threshold  | 0.5                 | 秒                         |

**Buffer Pool 计算公式**：

    buffer_pool_size = (物理内存 GB × 1024 × 1024 × 0.4) / 4KB

例：16 GB 内存 -> buffer_pool_size = (16 × 1024 × 1024 × 0.4) / 4 = 1,677,721 页

v3.0 是单进程，Buffer Pool 全部由本进程使用，无需考虑多进程场景。

### 2.3 环境变量覆盖

优先级：命令行 > 环境变量 > 配置文件 > 默认值

    export LIGHTDB_HOST=0.0.0.0
    export LIGHTDB_PORT=8888
    export LIGHTDB_LOG_LEVEL=INFO
    export LIGHTDB_DATA_DIR=/app/data
    export LIGHTDB_BUFFER_POOL_SIZE=1000
    export LIGHTDB_SLOW_QUERY_THRESHOLD=0.5

### 2.4 运行时修改

会话级：

    SET SESSION max_connections = 200;

全局级：

    SET GLOBAL slow_query_threshold = 1.0;

查看：

    SHOW VARIABLES LIKE 'max_connections';
    SHOW STATUS;

---

## 3. 监控

### 3.1 监控栈

    LightDB :8888/metrics  <--  Prometheus :9090  <--  Grafana :3000

docker-compose.v3.yml 已包含完整监控栈。

### 3.2 Prometheus 配置

config/prometheus.yml：

    global:
      scrape_interval: 15s
      evaluation_interval: 15s

    scrape_configs:
      - job_name: 'lightdb-server'
        static_configs:
          - targets: ['lightdb:8888']
        metrics_path: '/metrics'
        scrape_interval: 10s

      - job_name: 'lightdb-studio-web'
        static_configs:
          - targets: ['studio-web:8080']
        metrics_path: '/metrics'

### 3.3 43 个核心指标

v3.0 提供 43 个监控指标，按类别分组：

可用性（1 个）：

    lightdb_uptime_seconds

连接（6 个）：

    lightdb_threads_connected
    lightdb_threads_running
    lightdb_threads_created
    lightdb_connections_total
    lightdb_connections_aborted
    lightdb_connections_current

查询（7 个）：

    lightdb_queries_total
    lightdb_queries_per_second
    lightdb_query_duration_seconds
    lightdb_com_select
    lightdb_com_insert
    lightdb_com_update
    lightdb_com_delete

事务（4 个）：

    lightdb_transactions_total
    lightdb_transactions_active
    lightdb_transactions_committed
    lightdb_transactions_rollback

锁（4 个）：

    lightdb_lock_waits_total
    lightdb_lock_wait_seconds
    lightdb_deadlocks_total
    lightdb_row_locks_current

缓冲池（6 个）：

    lightdb_buffer_pool_pages_total
    lightdb_buffer_pool_pages_free
    lightdb_buffer_pool_pages_dirty
    lightdb_buffer_pool_hit_rate
    lightdb_buffer_pool_reads_total
    lightdb_buffer_pool_write_requests

磁盘 IO（4 个）：

    lightdb_disk_read_bytes
    lightdb_disk_write_bytes
    lightdb_disk_read_iops
    lightdb_disk_write_iops

复制（6 个）：

    lightdb_binlog_bytes_total
    lightdb_binlog_position
    lightdb_slave_io_running
    lightdb_slave_sql_running
    lightdb_slave_lag_seconds
    lightdb_semi_sync_avg_wait_ms

表级（4 个）：

    lightdb_table_rows
    lightdb_table_size_bytes
    lightdb_table_scans_total
    lightdb_table_row_locks_current

服务器（4 个）：

    lightdb_uptime_seconds
    lightdb_server_memory_bytes
    lightdb_server_cpu_usage
    lightdb_server_open_files

### 3.4 告警规则

config/alerts.yml 已包含 8 大类告警：

| 类别   | 关键告警                                                    |
|--------|-------------------------------------------------------------|
| 可用性 | LightDBDown、StudioWebDown、LightDBRestartFrequent          |
| 连接   | HighConnectionUsage、ConnectionSaturation                   |
| 查询   | QPSDrop、QPSSurge、HighSlowQueryRate、QueryP99High          |
| 事务   | HighTransactionRollbackRate、LongRunningTransactions        |
| 锁     | HighLockWaits、DeadlocksDetected、ManyRowLocks              |
| 缓冲池 | LowBufferPoolHitRate、HighDirtyPages                        |
| 复制   | ReplicationLag、SlaveIOStopped、SemiSyncWaitHigh            |
| 磁盘   | DiskSpaceLow、HighCPUUsage、HighMemoryUsage、HighLoadAverage|

### 3.5 Grafana 面板

v3.0 提供 4 个预设 Dashboard：

- Overview — QPS / 连接 / 慢查询 / 死锁
- Performance — 延迟分位 / IOPS / 缓冲池
- Replication — 主从延迟 / Binlog 位置
- InnoDB — 缓冲池页分布 / 行锁

导入方式：

    # 打开 Grafana -> Dashboards -> Import -> 上传 JSON
    docker cp dashboards/lightdb_overview.json lightdb-grafana-v3:/tmp/

---

## 4. 备份与恢复

### 4.1 逻辑备份

全量导出：

    lightdbdump -h 127.0.0.1 -P 8888 -u root \
        -D default -o backup.sql

仅结构：

    lightdbdump --no-data -o schema.sql

仅数据：

    lightdbdump --no-create -o data.sql

指定表：

    lightdbdump -t users orders -o selected.sql

### 4.2 从备份恢复

    lightdb -h 127.0.0.1 -P 8888 -u root < backup.sql

### 4.3 物理备份

第一步，停止写入（或进入只读）：

    SET GLOBAL read_only = 1;

第二步，触发 WAL checkpoint：

    FLUSH TABLES;
    FLUSH LOGS;

第三步，复制数据目录：

    tar czf /backup/physical_$(date +%Y%m%d).tar.gz /app/data

第四步，恢复只写：

    SET GLOBAL read_only = 0;

### 4.4 PITR（时间点恢复）

    python -m lightdb.backup.backup pitr \
        --dump /backup/full.sql \
        --binlog-dir /app/data \
        --target-time "2025-02-01 12:00:00"

### 4.5 备份策略建议

| 类型       | 频率   | 保留    | 存储          |
|------------|:------:|:-------:|---------------|
| 全量逻辑   | 每天   | 7 天    | 本地 + 异地   |
| 增量物理   | 每小时 | 48 小时 | 本地          |
| Binlog     | 实时   | 7 天    | 本地          |
| 月度归档   | 每月   | 12 个月 | 对象存储      |

### 4.6 快照 / 克隆

v3.0 内置快照功能：

    -- 通过 Studio 或 CLI
    python -m lightdb.tools.snapshot create --name snap_20250201
    python -m lightdb.tools.snapshot restore --name snap_20250201

快照导出为 SQL 文件，可恢复到其他实例。

---

## 5. 安全

### 5.1 用户与权限

创建用户：

    CREATE USER 'app'@'10.%' IDENTIFIED BY 'StrongPass123!';

授予最小权限（推荐）：

    GRANT SELECT, INSERT, UPDATE ON default.* TO 'app'@'10.%';

查看权限：

    SHOW GRANTS FOR 'app'@'10.%';

撤销：

    REVOKE DELETE ON default.* FROM 'app'@'10.%';

删除：

    DROP USER 'app'@'10.%';

### 5.2 密码策略

config.json 中配置：

    {
      "password_min_length": 6,
      "password_require_digit": false,
      "password_deny_weak": true
    }

常见弱密码黑名单：123456、password、admin 等 20+ 个。

### 5.3 IP 白名单

    {
      "ip_whitelist_enabled": true,
      "ip_whitelist": [
        "10.0.0.0/8",
        "192.168.1.0/24",
        "127.0.0.1"
      ]
    }

### 5.4 SSL/TLS

第一步，生成自签名证书：

    mkdir -p /app/data/certs
    openssl req -x509 -newkey rsa:2048 -days 365 -nodes \
        -keyout /app/data/certs/server.key \
        -out /app/data/certs/server.crt \
        -subj "/CN=lightdb.local"

第二步，修改 config.json：

    {
      "ssl_enabled": true,
      "ssl_cert": "/app/data/certs/server.crt",
      "ssl_key": "/app/data/certs/server.key"
    }

### 5.5 连接限流

    {
      "max_conn_per_ip": 20,
      "max_failed_per_ip": 5,
      "ban_seconds": 300
    }

超过 5 次登录失败，IP 封禁 5 分钟。

### 5.6 审计日志

    {
      "audit_level": "DDL"
    }

级别说明：

- NONE — 不记录
- DCL — 授权操作
- DDL — 建表/删表（推荐）
- DML — 增删改
- ALL — 包括 SELECT

审计日志位置：/app/data/audit.log

查看最近 100 条：

    tail -100 /app/data/audit.log | python -m json.tool

---

## 6. 性能调优

### 6.1 快速诊断

    lightdb-check -h 127.0.0.1 -P 8888 -u root

输出：

- 6 项健康检查
- 通过 / 警告 / 失败 状态
- 快速定位问题

### 6.2 参数调优

    lightdb-tune -h 127.0.0.1 -P 8888 -u root

基于实测环境的建议：

- Buffer Pool = 物理内存 × 0.4
- workers = 1（v3.0 固定）
- max_connections = 100-500

### 6.3 慢查询优化

第一步，找出慢查询：

    SHOW STATUS LIKE 'Slow_queries';

第二步，EXPLAIN 分析：

    EXPLAIN SELECT * FROM orders WHERE user_id = 123;

第三步，索引推荐：

    lightdb-index-advisor -h 127.0.0.1 --from-history

### 6.4 常见性能陷阱

| 反模式                              | 优化                                          |
|-------------------------------------|-----------------------------------------------|
| SELECT *                            | 明确列出列                                    |
| LIKE '%xxx'                         | 用全文索引或后缀匹配                          |
| WHERE YEAR(col) = 2025              | WHERE col >= '2025-01-01' AND col < '2026-01-01' |
| id = '123'（字符串）                | id = 123（数字）                              |
| 大表无 LIMIT                        | 加 LIMIT                                      |
| 未使用索引的 JOIN                   | 加索引或改用 Hash Join                        |
| NOT IN                              | 改用 LEFT JOIN ... IS NULL                    |

### 6.5 v3.0 特有的性能特性

> #### ⚠️ 状态说明（2026-10 补充，原文表格保留未删）
>
> 下表的 **3-4x / 3.15x / 20%** **未在 SQL 执行路径上验证**：向量化过滤
> （`VectorizedFilter`）与列存/OLAP 未接入 `Executor` / `plan_executor`
> （源码零引用），`lightdb/cython/` 的 `.pyx` 编译产物为 **0 个**。实测数字见
> [review/01_engine.md](review/01_engine.md)：逐行 INSERT **1,501 rows/s**、
> 批量 INSERT **1,356 rows/s**、点查 **888 q/s**（进程内）/ **1,138 q/s**（TCP）、
> 并发 8 客户端 **−36%**、WAL **≈200 B/行**、10 轮 UPDATE 后 `.dat`
> **669 KB→16 MB**。请把下表读作**设计目标/对照基线**，不是本机实测值。

| 特性           | 说明                                       |
|----------------|--------------------------------------------|
| 二进制行格式   | 相比 v2.0 的 JSON，编解码提速 3-4x         |
| 批处理         | 一次处理 1024 行，减少函数调用             |
| 向量化过滤     | NumPy 加速，提速 3.15x                     |
| 计划缓存       | 相同 SQL 复用执行计划                      |
| 自适应页预读   | 顺序访问时预读，提升 20%                   |

### 6.6 提升 v3.0 性能的方法

由于 v3.0 是单进程（受 GIL 限制），提升性能的手段有限：

1. **加索引**：最有效
2. **增大 Buffer Pool**：减少磁盘 IO
3. **优化 SQL**：避免全表扫描
4. **提高单核性能**：换更好的 CPU

如果上述方法已用尽仍不够用，请升级到 v4.0（多进程，8 核利用率从 12% 提升到 95%）。

---

## 7. 故障排查

### 7.1 服务无法启动

第一步，查看日志：

    docker logs lightdb --tail 100

第二步，检查配置：

    lightdb-check --config /app/config.json

第三步，检查端口：

    netstat -tlnp | grep 8888

第四步，检查数据目录：

    ls -la /app/data/

常见错误：

- Address already in use — 端口被占用
- Permission denied — 数据目录权限
- Config validation failed — 配置错误

### 7.2 连接失败

第一步，网络连通性：

    telnet lightdb 8888

第二步，防火墙：

    iptables -L | grep 8888

第三步，用户权限：

    mysql -h 127.0.0.1 -P 8888 -u root -e "SELECT USER()"

### 7.3 性能突然下降

第一步，实时监控：

    lightdbadmin status
    lightdbadmin processlist

第二步，找出阻塞：

    lightdbadmin processlist | grep -i "locked"

第三步，慢查询：

    SHOW STATUS LIKE 'Slow_queries';

第四步，锁等待：

    SHOW ENGINE INNODB STATUS;

### 7.4 主从延迟

第一步，查看延迟：

    lightdbadmin slave

第二步，检查从库 IO 线程：

    SHOW SLAVE STATUS\G

常见原因：

- 从库单线程重放（v3.0 不支持并行）
- 大事务
- 从库机器性能差
- 网络延迟

### 7.5 死锁

第一步，查看最近死锁：

    SHOW ENGINE INNODB STATUS\G

第二步，死锁日志：

    grep -i "deadlock" /app/logs/lightdb.log

常见原因：

- 加锁顺序不一致
- 大事务持锁过久
- 缺少索引导致全表锁

### 7.6 内存泄漏

v3.0 通过了 30 天长稳测试，正常情况下无内存泄漏。如怀疑有泄漏：

    # 监控 RSS
    while true; do
        ps -o rss= -p $(pgrep -f lightdb.main)
        sleep 60
    done

    # 或
    python -m lightdb.stability.memory_leak_detector --duration 3600

---

## 8. 升级到 v4.0

### 8.1 是否应该升级？

v4.0 相比 v3.0 提升：

> ⚠️ **状态说明（2026-10 补充）**：下表 **12,000 / 92,000 QPS、3.5M rows/s**
> **未在查询路径上验证**，属设计目标。OLAP/列存未接入 SQL 执行路径（零引用）、
> `lightdb/cython/` 无任何编译产物（`any_accelerated=False`，从未编译验证）、
> 并行只覆盖单表裸 `COUNT` 且实测负加速。本机实测（[review/01_engine.md](review/01_engine.md)）：
> 点查 **888 q/s** 进程内 / **1,138 q/s** TCP（与 12,000 QPS 相差 **13.5 倍**）、
> 并发 1→8 客户端 **−36%**、逐行 INSERT **1,501 rows/s**、批量 INSERT
> **1,356 rows/s**（批量反而更慢）。原文表格保留以便对照，**不补写**未验证内容。

| 指标         | v3.0     | v4.0       | 提升    |
|--------------|:--------:|:----------:|:-------:|
| 单线程点查   | 6,500    | 12,000     | +85%    |
| 8 核点查     | 8,200    | 92,000     | +1022%  |
| OLAP 扫描    | 不支持   | 3.5M rows/s| 新增    |

**建议**：

- 单机开发/测试：可选升级
- 生产有并发压力：强烈建议升级
- 有 OLAP 需求：必须升级

### 8.2 升级前置检查

- [ ] 已备份 v3 数据和配置
- [ ] 已确认 v4 版本号
- [ ] 已阅读 `MIGRATION_v3_to_v4.md`
- [ ] 已在测试环境演练
- [ ] 已准备回滚方案

### 8.3 快速升级

    cd /opt/lightdb/scripts
    ./upgrade.sh --from 3.0.0 --to 4.0.0

脚本自动：

1. 停止服务
2. 备份数据
3. 兼容性检查
4. 升级二进制
5. 数据迁移
6. 启动验证
7. 失败自动回滚

### 8.4 回滚

    ./upgrade.sh --rollback

---

## 9. 常见错误

### 9.1 启动后立刻退出

原因：可能是数据目录权限问题。

检查：

    ls -la /app/data/
    # 期望: lightdb 用户可读写

修复：

    chown -R lightdb:lightdb /app/data
    chmod 755 /app/data

### 9.2 查询报 "Table 'xxx' doesn't exist"

原因：可能是切换了数据库但未 `USE`。

修复：

    USE default;
    SELECT * FROM users;

或明确指定：

    SELECT * FROM default.users;

### 9.3 慢查询日志为空

原因：`slow_query_threshold` 设置过大。

检查：

    SHOW VARIABLES LIKE 'slow_query_threshold';

修复：

    SET GLOBAL slow_query_threshold = 0.5;

### 9.4 缓冲区命中率低

原因：Buffer Pool 太小。

检查：

    SHOW STATUS LIKE 'Innodb_buffer_pool%';

修复：

    # 编辑 config.json
    # buffer_pool_size 从 1000 提到 2000000
    # 重启服务

### 9.5 复制中断

原因：从库报错或主库 binlog 被清理。

检查：

    SHOW SLAVE STATUS\G
    # 看 Last_Error 字段

修复：

    -- 跳过错误（谨慎）
    SET GLOBAL sql_slave_skip_counter = 1;
    START SLAVE;

    -- 或重新建立复制
    RESET SLAVE ALL;

---

## 附录：日常运维命令

状态检查：

    lightdbadmin status
    lightdbadmin processlist
    lightdbadmin variables | grep -i innodb

备份：

    lightdbdump -o /backup/daily_$(date +%Y%m%d).sql

索引推荐：

    lightdb-index-advisor --from-history

健康检查：

    lightdb-check -h 127.0.0.1 -P 8888 -u root

监控：

    curl http://localhost:8888/metrics | grep lightdb_

---

## 附录：v3.0 与 v4.0 运维差异速查

> ⚠️ **状态说明（2026-10 补充）**：下表的 `Cython = 6 个模块` 与
> `OLAP = 列存 + 向量化` 是**设计目标**：`lightdb/cython/` 下 6 个 `.pyx`
> 均**无编译产物**（`.pyd`/`.so`/`.c` 为 0 个，`status()['any_accelerated']`
> 为 `False`，本机无 Cython 与 C 编译器 → **从未编译验证**）；列存/OLAP
> **未接入 SQL 执行路径**（`Executor` / `plan_executor` 零引用），列存
> `GROUP BY` 返回 0 行、并行只覆盖单表裸 `COUNT` 且实测负加速。原文表格
> 保留以便对照；实测数字见 [review/01_engine.md](review/01_engine.md)。

| 项目           | v3.0          | v4.0               |
|----------------|---------------|--------------------|
| 进程模型       | 单进程        | 多进程 + 共享内存  |
| Worker 数      | 1（固定）     | CPU 核数           |
| Buffer Pool    | 4 GB 内存的 40%| 16 GB 内存的 60%  |
| Cython         | 无            | 6 个模块           |
| OLAP           | 无            | 列存 + 向量化      |
| 会话变量       | 进程内        | 跨进程（注意）     |
| 临时表         | 全可用        | 单 Worker 有效     |
| 部署           | 单容器        | Compose / K8s      |
| 内存要求       | 4 GB          | 8 GB               |
| 监控指标       | 43 个         | 43 个 + OLAP 相关  |

---

紧急联系：oncall@lightdb.io
文档问题：https://github.com/lightdb/lightdb/issues

==== END FILE ====