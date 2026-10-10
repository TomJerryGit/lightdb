==== BEGIN FILE: docs/ADMIN_GUIDE_v4.md ====

# LightDB v4.0 管理员指南

**版本**：4.0.0
**更新日期**：2025-08-01
**适用对象**：DBA、SRE、运维工程师

---

## 目录

1. 部署
2. 配置
3. 监控
4. 备份与恢复
5. 安全
6. 性能调优
7. 故障排查
8. 升级
9. 常见错误

---

## 1. 部署

### 1.1 硬件建议

| 场景        | CPU   | 内存   | 磁盘          | 说明         |
|-------------|:-----:|:------:|:-------------:|--------------|
| 开发        | 2 核  | 4 GB   | 20 GB         | 单机         |
| 测试        | 4 核  | 16 GB  | 100 GB        | 单机         |
| 生产（小）  | 8 核  | 32 GB  | 500 GB SSD    | 单机         |
| 生产（大）  | 16+ 核| 64+ GB | 1 TB+ NVMe    | 主从 + 副本  |

内存分配建议：

- Buffer Pool：物理内存的 60-70%
- 系统 + 其他：剩余 30%

### 1.2 Docker 部署（推荐）

单机版：

    docker run -d --name lightdb \
        -p 8888:8888 \
        -p 9090:9090 \
        -v /data/lightdb:/app/data \
        -e LIGHTDB_WORKERS=8 \
        -e LIGHTDB_BUFFER_POOL_SIZE=16384 \
        --ulimit nofile=65536:65536 \
        lightdb/server:4.0.0

使用 docker-compose：

    docker-compose -f docker-compose.v4.yml up -d

### 1.3 K8s 部署

    kubectl apply -f k8s.yaml

k8s.yaml 包含：

- Namespace lightdb
- ConfigMap（配置）
- PVC（20GB 数据卷）
- Deployment（服务 + Studio Web）
- Service（ClusterIP + LoadBalancer）

注意：镜像必须使用 entrypoint-v4.sh 里的 server 命令。不要直接覆盖 command，否则会绕过初始化（见第 9 节常见错误）。

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

第二步，Cython 模块状态：

    docker exec lightdb python -c "
    from lightdb.cython import status
    print(status())"

第三步，多进程 Worker 数：

    docker exec lightdb ps aux | grep worker

---

## 2. 配置

### 2.1 配置文件（config.json）

    {
      "host": "0.0.0.0",
      "port": 8888,
      "max_connections": 500,

      "buffer_pool_size": 8192,
      "page_size": 4096,
      "data_dir": "/app/data",
      "backup_dir": "/app/backups",

      "workers": 8,
      "parallel_workers": 8,

      "slow_query_threshold": 0.5,
      "max_allowed_packet": 16777216,

      "log_level": "INFO",
      "log_dir": "/app/logs",

      "ssl_enabled": false,
      "audit_level": "DDL",
      "semi_sync_enabled": false,

      "max_conn_per_ip": 100,
      "ip_whitelist_enabled": false,

      "password_min_length": 8,
      "password_deny_weak": true
    }

### 2.2 关键参数说明

| 参数                  | 建议值             | 说明                       |
|-----------------------|--------------------|----------------------------|
| workers               | CPU 核数           | 多进程 Worker 数           |
| parallel_workers      | 同 workers         | 单查询并行度               |
| buffer_pool_size      | 内存 / 4KB × 0.6   | 单位：页                   |
| max_connections       | 500-2000           | 视并发而定                 |
| slow_query_threshold  | 0.5                | 秒                         |
| audit_level           | DDL                | NONE/DCL/DDL/DML/ALL       |

### 2.3 环境变量覆盖

优先级：命令行 > 环境变量 > 配置文件 > 默认值

    export LIGHTDB_HOST=0.0.0.0
    export LIGHTDB_PORT=8888
    export LIGHTDB_WORKERS=8
    export LIGHTDB_BUFFER_POOL_SIZE=16384
    export LIGHTDB_LOG_LEVEL=INFO
    export LIGHTDB_LOG_DIR=/app/logs
    export LIGHTDB_SLOW_QUERY_THRESHOLD=0.5

### 2.4 运行时修改

会话级（当前连接有效）：

    SET SESSION max_connections = 1000;

全局级（重启后失效）：

    SET GLOBAL slow_query_threshold = 1.0;

查看：

    SHOW VARIABLES LIKE 'max_connections';
    SHOW STATUS;

---

## 3. 监控

### 3.1 监控栈

    LightDB :8888/metrics  <--  Prometheus :9090  <--  Grafana :3000

docker-compose.v4.yml 已包含完整监控栈。

### 3.2 Prometheus 配置

config/prometheus.yml：

    scrape_configs:
      - job_name: 'lightdb-server'
        static_configs:
          - targets: ['lightdb:8888']
        metrics_path: '/metrics'
        scrape_interval: 10s

      - job_name: 'node-exporter'
        static_configs:
          - targets: ['node-exporter:9100']

### 3.3 43 个核心指标

可用性：

    lightdb_uptime_seconds

连接：

    lightdb_threads_connected
    lightdb_threads_running
    lightdb_connections_total
    lightdb_connections_aborted

查询：

    lightdb_queries_per_second
    lightdb_slow_queries_total
    lightdb_query_duration_seconds_bucket

事务：

    lightdb_transactions_active
    lightdb_transactions_committed
    lightdb_transactions_rollback

锁：

    lightdb_lock_waits_total
    lightdb_deadlocks_total
    lightdb_row_locks_current

缓冲池：

    lightdb_buffer_pool_pages_total
    lightdb_buffer_pool_pages_free
    lightdb_buffer_pool_pages_dirty
    lightdb_buffer_pool_hit_rate

复制：

    lightdb_slave_lag_seconds
    lightdb_slave_io_running
    lightdb_slave_sql_running

### 3.4 告警规则

config/alerts.yml 已包含 8 大类告警：

| 类别   | 关键告警                                                    |
|--------|-------------------------------------------------------------|
| 可用性 | LightDBDown、StudioWebDown                                  |
| 连接   | HighConnectionUsage、ConnectionSaturation                   |
| 查询   | QPSDrop、HighSlowQueryRate、QueryP99High                    |
| 事务   | HighTransactionRollbackRate、LongRunningTransactions        |
| 锁     | DeadlocksDetected、ManyRowLocks                             |
| 缓冲池 | LowBufferPoolHitRate、HighDirtyPages                        |
| 复制   | ReplicationLag、SlaveIOStopped                              |
| 磁盘   | DiskSpaceLow、HighCPUUsage、HighMemoryUsage                 |

### 3.5 Grafana 面板

4 个预设 Dashboard：

- Overview — QPS / 连接 / 慢查询 / 死锁
- Performance — 延迟分位 / IOPS / 缓冲池
- Replication — 主从延迟 / Binlog 位置
- InnoDB — 缓冲池页分布 / 行锁

导入方式：

    # 打开 Grafana -> Dashboards -> Import -> 上传 JSON
    docker cp dashboards/lightdb_overview.json lightdb-grafana:/tmp/

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

第二步，触发 WAL checkpoint（确保数据落盘）：

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
        --target-time "2025-08-01 12:00:00"

### 4.5 备份策略建议

| 类型       | 频率   | 保留    | 存储          |
|------------|:------:|:-------:|---------------|
| 全量逻辑   | 每天   | 7 天    | 本地 + 异地   |
| 增量物理   | 每小时 | 48 小时 | 本地          |
| Binlog     | 实时   | 7 天    | 本地          |
| 月度归档   | 每月   | 12 个月 | 对象存储      |

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
      "password_min_length": 8,
      "password_require_digit": true,
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
      "max_conn_per_ip": 50,
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

    lightdb-diagnose --mode all --duration 30

输出：

- 火焰图 flame_*.svg
- 热点报告 hotspot_report.txt
- GIL 分析 gil_analysis.json
- 路线对比 route_comparison.txt

### 6.2 参数调优

    lightdb-tune -h 127.0.0.1 -P 8888 -u root

基于实测环境的建议：

- Buffer Pool = 物理内存 × 0.6
- workers = CPU 核数
- parallel_workers = CPU 核数

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

    SELECT * FROM information_schema.slow_queries LIMIT 10;

第四步，锁等待：

    SHOW ENGINE INNODB STATUS;

### 7.4 主从延迟

第一步，查看延迟：

    lightdbadmin slave

第二步，检查从库 IO 线程：

    SHOW SLAVE STATUS\G

常见原因：

- 从库单线程重放（v4.0 支持并行）
- 大事务
- 从库机器性能差
- 网络延迟

### 7.5 死锁

第一步，查看最近死锁：

    SELECT * FROM information_schema.deadlocks LIMIT 5;

第二步，死锁日志：

    grep -i "deadlock" /app/logs/lightdb.log

常见原因：

- 加锁顺序不一致
- 大事务持锁过久
- 缺少索引导致全表锁

---

## 8. 升级

### 8.1 从 v3.0 升级到 v4.0

第一步，备份：

    lightdbdump -o backup_v3.sql
    tar czf backup_v3_data.tar.gz /app/data

第二步，使用升级脚本（推荐）：

    ./upgrade.sh --from 3.0.0 --to 4.0.0

脚本自动执行：

1. 停止服务
2. 备份数据
3. 兼容性检查
4. 升级二进制
5. 数据迁移
6. 启动验证
7. 失败自动回滚

### 8.2 手动升级步骤

第一步，停服：

    systemctl stop lightdb

第二步，备份：

    tar czf /backup/pre_upgrade.tar.gz /opt/lightdb

第三步，拉取新版本：

    docker pull lightdb/server:4.0.0

第四步，更新 compose：

    vim docker-compose.yml
    # image: lightdb/server:4.0.0

第五步，重启：

    docker-compose up -d

### 8.3 兼容性说明

| 从     | 到     | 兼容性                  |
|--------|--------|-------------------------|
| 3.0.x  | 3.0.y  | 完全兼容                |
| 3.0.x  | 4.0.y  | 数据兼容，需迁移        |
| 4.0.x  | 4.0.y  | 完全兼容                |

### 8.4 回滚

    ./upgrade.sh --rollback

或：

    docker-compose down
    docker run -v /backup/pre_upgrade.tar.gz ... lightdb/server:3.0.0

---

## 9. 常见错误

### 9.1 K8s 部署后 503

原因：command 覆盖了 entrypoint，绕过初始化。

错误写法：

    command: ["python", "main.py"]

正确写法：

    args: ["server"]
    env:
      - name: LIGHTDB_MODE
        value: "multi"

### 9.2 多进程 Worker 数不对

原因：workers=0 时按 CPU 核数，但容器限制 CPU 后 os.cpu_count() 可能返回宿主机的核数。

修复：

    environment:
      - LIGHTDB_WORKERS=4       # 明确指定

### 9.3 半同步总是降级

原因：v4.0.0 早期版本 ACK 通道未实现（已修复）。

确认版本：

    docker exec lightdb python -c "import lightdb; print(lightdb.__version__)"

### 9.4 Cython 编译失败

原因：缺少 C 编译器。

修复：

    apt-get install build-essential

或跳过 Cython（自动回退）：

    export LIGHTDB_SKIP_CYTHON=1

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

诊断：

    lightdb-diagnose --mode all

监控：

    curl http://localhost:8888/metrics | grep lightdb_

---

紧急联系：oncall@lightdb.io
文档问题：https://github.com/lightdb/lightdb/issues

==== END FILE ====