==== BEGIN FILE: docs/MIGRATION_v3_to_v4.md ====

# LightDB v3.0 到 v4.0 迁移指南

**源版本**：3.0.x
**目标版本**：4.0.x
**更新日期**：2025-08-01
**适用对象**：DBA、运维工程师、应用开发者

---

## 目录

1. 迁移前必读
2. 兼容性矩阵
3. 迁移路径选择
4. 迁移步骤
5. 数据迁移
6. 配置迁移
7. 应用代码调整
8. 回滚方案
9. 迁移后验证
10. 常见问题

---

## 1. 迁移前必读

### 1.1 影响范围

v3.0 到 v4.0 是一次**架构级升级**，主要变化：

| 维度       | v3.0            | v4.0                     | 影响                |
|------------|-----------------|--------------------------|---------------------|
| 进程模型   | 单进程          | 多进程（N 个 Worker）    | 运维方式变化        |
| 行格式     | JSON            | 二进制 + Cython          | 数据文件不兼容      |
| 存储引擎   | BufferPool + B+Tree | 同（但加入共享内存）| 需数据迁移          |
| 事务协调   | 进程内 TxnManager | 跨进程 SharedTxnCoordinator | 内部透明       |
| 配置项     | 30 个           | 40 个（新增 10 个）      | 需新增字段          |
| 部署方式   | Docker 单容器   | Docker Compose + K8s     | 建议改用 compose    |

### 1.2 停机窗口估算

| 数据量   | 迁移耗时（预估） |
|----------|:---------------:|
| < 1 GB   | < 5 分钟        |
| 1-10 GB  | 10-30 分钟      |
| 10-100 GB| 1-3 小时        |
| > 100 GB | 半天以上        |

**建议**：先在测试环境跑一次，记录实际耗时。

### 1.3 前置条件

- Python 3.8+
- 目标机器有 C 编译器（gcc/clang）—— 用于 Cython 编译
- 磁盘空间：原数据量的 2 倍（旧数据 + 新数据）
- 已完成全量备份（详见第 4 节）

---

## 2. 兼容性矩阵

### 2.1 数据兼容性

| 数据类型     | v3 存储格式 | v4 存储格式 | 自动迁移 |
|--------------|-------------|-------------|:--------:|
| INT          | JSON 数值   | 4 字节二进制| 是       |
| BIGINT       | JSON 数值   | 8 字节二进制| 是       |
| VARCHAR/TEXT | JSON 字符串 | 长度前缀 + UTF-8 | 是   |
| DECIMAL      | JSON 数值   | 8 字节 double | 是（精度注意） |
| DATETIME     | JSON 字符串 | 4 字节 timestamp | 是   |
| BOOLEAN      | JSON true/false | 1 字节   | 是       |
| JSON         | JSON 字符串 | 保留为 TEXT | 是       |

**结论**：v4.0 提供自动数据迁移工具，无需手动处理。

### 2.2 SQL 语法兼容性

| 语法特性           | v3.0 | v4.0 | 说明              |
|--------------------|:----:|:----:|-------------------|
| 基础 SELECT        | 是   | 是   | 完全兼容          |
| JOIN               | 是   | 是   | 完全兼容          |
| 子查询             | 是   | 是   | 完全兼容          |
| 事务               | 是   | 是   | 完全兼容          |
| 窗口函数           | 是   | 是   | v3.0 后期版本已支持 |
| CTE                | 是   | 是   | 完全兼容          |
| JSON               | 是   | 是   | 完全兼容          |
| 存储过程           | 是   | 是   | 完全兼容          |
| FULLTEXT           | 是   | 是   | 完全兼容          |
| 分区表             | 是   | 是   | 完全兼容          |
| 生成列             | 是   | 是   | 完全兼容          |

**结论**：应用侧 SQL 无需修改。

### 2.3 Wire Protocol 兼容性

| 客户端             | v3.0 | v4.0 | 说明              |
|--------------------|:----:|:----:|-------------------|
| mysql client 8.0   | 是   | 是   | 直接可用          |
| PyMySQL 1.x        | 是   | 是   | 直接可用          |
| JDBC 8.x           | 是   | 是   | 直接可用          |
| Go database/sql    | 是   | 是   | 直接可用          |
| Navicat / DBeaver  | 是   | 是   | 直接可用          |

**结论**：客户端无需任何修改。

### 2.4 API 兼容性

v4.0 的 CLI 工具命令行参数与 v3.0 完全一致：

    lightdb -h host -P port -u user
    lightdbdump -h host -P port -u user -o file
    lightdbadmin -h host -P port -u user status

**结论**：运维脚本无需修改。

---

## 3. 迁移路径选择

### 3.1 三种路径

**路径 A：原地升级**（推荐小数据量）

- 停机、备份、升级二进制、启动
- 优点：简单，最快
- 缺点：停机时间长

**路径 B：逻辑迁移**

- 导出 SQL、启动 v4、导入
- 优点：可跨机器、跨 OS
- 缺点：大数据量耗时长

**路径 C：蓝绿部署**（推荐生产）

- v3 和 v4 同时运行，切换流量
- 优点：可快速回滚，停机接近 0
- 缺点：需要双倍资源

### 3.2 选择建议

| 场景              | 推荐路径 |
|-------------------|:--------:|
| 开发/测试环境     | A        |
| 小数据量生产      | A        |
| 大数据量生产      | C        |
| 跨机器/跨 OS      | B        |

---

## 4. 迁移步骤

### 4.1 准备阶段

**第一步：备份**（必做）

    # 逻辑备份
    lightdbdump -h 127.0.0.1 -P 8888 -u root \
        -o /backup/pre_v4_full.sql

    # 物理备份
    tar czf /backup/pre_v4_data_$(date +%Y%m%d).tar.gz /app/data

    # 配置文件备份
    cp /app/config.json /backup/config_v3.json

**第二步：环境检查**

    # Python 版本
    python3 --version    # >= 3.8

    # C 编译器
    gcc --version        # 或 clang --version

    # 磁盘空间（至少 2 倍数据量）
    df -h /app

    # 内存
    free -h

**第三步：生成迁移报告**

    python -m lightdb.tools.migrate \
        --source-version 3.0.0 \
        --target-version 4.0.0 \
        --dry-run

输出包含：

- 表数量、总行数
- 数据类型分布
- 索引数量
- 预估耗时

### 4.2 路径 A：原地升级

**第一步：停止服务**

    # systemd
    systemctl stop lightdb

    # Docker
    docker stop lightdb-server

**第二步：执行升级脚本**

    cd /opt/lightdb/scripts
    ./upgrade.sh --from 3.0.0 --to 4.0.0

脚本自动执行：

1. 停止服务
2. 备份数据
3. 兼容性检查
4. 拉取 v4 镜像
5. 数据迁移（v3 JSON -> v4 二进制）
6. 启动验证
7. 失败自动回滚

**第三步：验证**

    lightdb-check -h 127.0.0.1 -P 8888 -u root

**第四步：手动验证**

    mysql -h 127.0.0.1 -P 8888 -u root -e "
        SELECT VERSION();
        SHOW TABLES;
        SELECT COUNT(*) FROM 你的主表;"

### 4.3 路径 B：逻辑迁移

**第一步：导出 v3 数据**

    lightdbdump -h 127.0.0.1 -P 8888 -u root \
        -o /backup/v3_full.sql

    # 大文件建议分表导出
    lightdbdump -t users -o /backup/v3_users.sql
    lightdbdump -t orders -o /backup/v3_orders.sql

**第二步：启动 v4**

    docker run -d --name lightdb-v4 \
        -p 8888:8888 \
        -v /data/lightdb-v4:/app/data \
        lightdb/server:4.0.0

**第三步：导入**

    lightdb -h 127.0.0.1 -P 8888 -u root < /backup/v3_full.sql

**第四步：验证**

    lightdb-check -h 127.0.0.1 -P 8888 -u root

### 4.4 路径 C：蓝绿部署

**第一步：v3 与 v4 同时运行**

    # v3 监听 8888
    docker run -d --name lightdb-v3 \
        -p 8888:8888 lightdb/server:3.0.0

    # v4 监听 8889
    docker run -d --name lightdb-v4 \
        -p 8889:8889 lightdb/server:4.0.0

**第二步：双写阶段**

应用同时写 v3 和 v4（业务侧改造）。

**第三步：数据同步**

    python -m lightdb.tools.migrate \
        --source-host 127.0.0.1 --source-port 8888 \
        --target-host 127.0.0.1 --target-port 8889 \
        --mode sync

**第四步：切换流量**

    # 修改负载均衡配置，把 8888 指向 v4
    # 或修改应用配置指向 8889

**第五步：观察**

至少观察 24 小时，确认无异常。

**第六步：下线 v3**

    docker stop lightdb-v3

---

## 5. 数据迁移

### 5.1 自动迁移工具

v4.0 提供 `lightdb-migrate` 工具：

    python -m lightdb.tools.migrate \
        --source /app/data-v3 \
        --target /app/data-v4 \
        --batch-size 500

工具自动处理：

1. **格式转换**：JSON 行 -> 二进制行
2. **索引重建**：B+Tree 重新构建
3. **WAL 清理**：v3 WAL 不迁移
4. **统计信息**：重新采样
5. **校验**：行数 + 校验和

### 5.2 迁移过程示例

输出示例：

    ==========================================
      LightDB v3.0 -> v4.0 数据迁移
    ==========================================
      源目录:   /app/data-v3
      目标目录: /app/data-v4
      批大小:   500

    [1/6] 扫描源数据...
          找到 15 张表，共 1,234,567 行

    [2/6] 检查磁盘空间...
          需要: 2.5 GB
          可用: 20 GB
          通过

    [3/6] 迁移表数据...
      users:     100,000 行  [==========] 100%
      orders:  1,000,000 行  [==========] 100%
      products:   50,000 行  [==========] 100%
      ...
      完成

    [4/6] 重建索引...
      15 个索引  [==========] 100%

    [5/6] 采样统计信息...
      完成

    [6/6] 校验...
      行数:     1234567 == 1234567
      校验和:   a3f5b8c1 == a3f5b8c1
      通过

    ==========================================
      迁移成功
      耗时: 45.3 秒
    ==========================================

### 5.3 分表迁移

超大表（> 1 亿行）建议分表迁移：

    python -m lightdb.tools.migrate \
        --source /app/data-v3 \
        --target /app/data-v4 \
        --tables users orders \
        --batch-size 10000

### 5.4 增量同步

v3 运行期间产生的增量数据：

    python -m lightdb.tools.migrate \
        --mode incremental \
        --source-host 127.0.0.1 --source-port 8888 \
        --target-host 127.0.0.1 --target-port 8889 \
        --since "2025-08-01 00:00:00"

---

## 6. 配置迁移

### 6.1 新增字段

v4.0 相比 v3.0 新增 10 个字段：

| 字段                | 默认值 | 说明                    |
|---------------------|:------:|-------------------------|
| workers             | 0      | 0 = CPU 核数            |
| parallel_workers    | 8      | 单查询并行度            |
| shm_dir             | (空)   | 共享内存目录            |
| txn_coordinator     | 空     | shared_memory           |
| lock_coordinator    | 空     | shared_memory           |
| ssl_enabled         | false  | SSL 开关                |
| audit_level         | DDL    | 审计级别                |
| semi_sync_enabled   | false  | 半同步开关              |
| max_conn_per_ip     | 100    | 每 IP 最大连接数        |
| password_deny_weak  | true   | 拒绝弱密码              |

### 6.2 配置对照

v3.0 config.json：

    {
      "host": "0.0.0.0",
      "port": 8888,
      "max_connections": 100,
      "buffer_pool_size": 1000,
      "data_dir": "/app/data",
      "workers": 1,
      "log_level": "INFO"
    }

v4.0 config.json：

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
      "shm_dir": "/dev/shm",

      "default_isolation": "READ COMMITTED",
      "autocommit": true,
      "wal_enabled": true,

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
      "password_require_digit": false,
      "password_deny_weak": true
    }

### 6.3 关键参数调整

| 参数              | v3.0 | v4.0 建议             | 说明              |
|-------------------|:----:|:---------------------:|-------------------|
| max_connections   | 100  | 500                   | v4 并发能力更强   |
| buffer_pool_size  | 1000 | 内存 / 4KB × 0.6      | 见下方公式        |
| workers           | 1    | CPU 核数              | 关键！            |
| parallel_workers  | 2    | 同 workers            | -                 |
| slow_query_threshold | 0.5 | 0.5                | 保持不变          |

**Buffer Pool 计算公式**：

    buffer_pool_size = (物理内存 GB × 1024 × 1024 × 0.6) / 4KB

例：16 GB 内存 -> buffer_pool_size = (16 × 1024 × 1024 × 0.6) / 4 = 2,516,582 页

---

## 7. 应用代码调整

### 7.1 无需调整的部分

- SQL 语法：完全兼容
- Wire Protocol：完全兼容
- 客户端库：完全兼容
- 连接参数：完全兼容

### 7.2 需要注意的部分

**会话变量作用域**（重要）

v4.0 多进程下，会话可能落到任意 Worker：

    -- v3.0：安全，同一进程
    SET @x = 100;
    BEGIN;
    INSERT INTO t VALUES (@x);
    COMMIT;

    -- v4.0：不安全，会话可能切换
    SET @x = 100;          -- 落在 Worker 0
    BEGIN;                  -- 可能落在 Worker 1
    INSERT INTO t VALUES (@x);   -- @x 可能为空
    COMMIT;

**修复方案**：把变量设置和使用放在同一事务内：

    BEGIN;
    SET @x = 100;
    INSERT INTO t VALUES (@x);
    COMMIT;

**临时表注意事项**（重要）

v4.0 下，临时表只在当前 Worker 有效：

    -- v3.0：可用
    CREATE TEMPORARY TABLE tmp (...);
    SELECT * FROM tmp;
    SELECT * FROM tmp;   -- 同一会话

    -- v4.0：可能失败
    -- 会话切换后临时表不存在

**修复方案**：改用普通表 + 唯一表名前缀，或用单连接会话。

**长事务注意事项**

v4.0 建议事务时长 < 30 秒。

### 7.3 代码审查清单

- [ ] 是否有跨 SQL 语句的 `SET @var`？
- [ ] 是否使用临时表？
- [ ] 是否有超过 30 秒的长事务？
- [ ] 是否依赖连接级的 `LAST_INSERT_ID`？
- [ ] 是否依赖 `SELECT @@session.xxx` 返回值？

以上任意一项为"是"，都需要调整。

---

## 8. 回滚方案

### 8.1 回滚触发条件

- 迁移后服务无法启动
- 数据校验失败
- 性能不如预期
- 应用出现兼容性问题

### 8.2 快速回滚（原地升级）

    cd /opt/lightdb/scripts
    ./upgrade.sh --rollback

脚本自动：

1. 停止 v4 服务
2. 恢复 v3 二进制
3. 恢复 v3 数据
4. 恢复 v3 配置
5. 启动 v3 服务
6. 验证

### 8.3 手动回滚

第一步，停止 v4：

    systemctl stop lightdb

第二步，恢复数据：

    rm -rf /app/data
    tar xzf /backup/pre_v4_data_20250801.tar.gz -C /app/

第三步，恢复配置：

    cp /backup/config_v3.json /app/config.json

第四步，启动 v3 镜像：

    docker run -d --name lightdb \
        -p 8888:8888 \
        -v /app/data:/app/data \
        lightdb/server:3.0.0

第五步，验证：

    lightdb-check -h 127.0.0.1 -P 8888 -u root

### 8.4 蓝绿回滚

只需把负载均衡配置切回 8888（v3 仍在运行）：

    # 修改 nginx/haproxy 配置
    upstream lightdb {
        server 127.0.0.1:8888;   # 切回 v3
        # server 127.0.0.1:8889;  # v4
    }

---

## 9. 迁移后验证

### 9.1 功能验证清单

**基础功能**

    -- 1. 版本
    SELECT VERSION();
    -- 期望: 5.7.30-LightDB

    -- 2. 库表
    SHOW DATABASES;
    SHOW TABLES;

    -- 3. 数据量
    SELECT COUNT(*) FROM 你的主表;

**SQL 功能**

    -- 4. JOIN
    SELECT * FROM t1 JOIN t2 ON t1.id = t2.id LIMIT 10;

    -- 5. 事务
    BEGIN;
    INSERT INTO test VALUES (1);
    ROLLBACK;
    SELECT COUNT(*) FROM test WHERE id = 1;
    -- 期望: 0

    -- 6. 窗口函数
    SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t LIMIT 10;

    -- 7. CTE
    WITH cte AS (SELECT 1 AS x) SELECT * FROM cte;

**性能验证**

    -- 8. EXPLAIN
    EXPLAIN SELECT * FROM t WHERE id = 1;

    -- 9. 状态
    SHOW STATUS LIKE '%buffer_pool%';

    -- 10. 表统计
    SHOW STATS;

**进程验证**

    # 11. Worker 数
    docker exec lightdb ps aux | grep worker
    # 期望: 数量 == CPU 核数

    # 12. Cython 状态
    docker exec lightdb python -c "
    from lightdb.cython import status
    print(status())"

### 9.2 数据一致性验证

**逐行校验**（v3 vs v4）

    # 从 v3 导出
    lightdbdump -h 127.0.0.1 -P 8888 -u root -D default -o v3.sql

    # 从 v4 导出
    lightdbdump -h 127.0.0.1 -P 8889 -u root -D default -o v4.sql

    # 比较（忽略时间戳等差异）
    diff <(grep INSERT v3.sql | sort) <(grep INSERT v4.sql | sort)

**行数校验**

    # v3
    mysql -h 127.0.0.1 -P 8888 -u root -e "
        SELECT table_name, table_rows
        FROM information_schema.tables"

    # v4
    mysql -h 127.0.0.1 -P 8889 -u root -e "
        SELECT table_name, table_rows
        FROM information_schema.tables"

### 9.3 性能对比

跑一遍基准测试：

    # 迁移前（v3）
    lightdb-bench --host 127.0.0.1 --port 8888 \
        --workload all --duration 30

    # 迁移后（v4）
    lightdb-bench --host 127.0.0.1 --port 8889 \
        --workload all --duration 30 --baseline v3.0.base

预期提升：

| 指标         | 提升幅度  |
|--------------|:---------:|
| 单线程点查   | +85%      |
| 8 核并发     | +1000%    |
| OLAP 扫描    | 新增      |

---

## 10. 常见问题

### Q1: 迁移后无法启动，报 "shm open failed"

原因：`/dev/shm` 权限问题或空间不足。

检查：

    df -h /dev/shm
    ls -la /dev/shm/

修复：

    chmod 1777 /dev/shm
    # 或挂载更大的 tmpfs
    mount -t tmpfs -o size=2g tmpfs /dev/shm

### Q2: 迁移后 Worker 数为 1

原因：`workers=0` 时按 CPU 核数，但容器限制 CPU 后 `os.cpu_count()` 返回宿主机核数，或返回值异常。

修复：

    # 明确指定
    environment:
      - LIGHTDB_WORKERS=8

### Q3: 迁移后 Cython 模块不可用

原因：目标机器缺少 C 编译器。

检查：

    docker exec lightdb python -c "
    from lightdb.cython import status
    print(status())"

修复：

    apt-get install build-essential
    # 重新构建镜像

**注意**：Cython 不可用只影响性能，不影响功能。

### Q4: 迁移后查询变慢

原因：多进程下会话切换，导致缓存不命中。

排查：

    # 检查是否有多进程问题
    docker exec lightdb ps aux | grep worker

    # 检查缓冲池命中率
    SHOW STATUS LIKE 'Innodb_buffer_pool_read%';

优化：

    # 增大 buffer_pool_size
    SET GLOBAL buffer_pool_size = 16384;

    # 或调整连接池，让同一会话尽量落到同一 Worker

### Q5: 迁移后应用报 "Unknown column"

原因：**极少见**，可能是数据迁移时列名大小写变化。

检查：

    SHOW COLUMNS FROM 表名;
    SHOW VARIABLES LIKE 'lower_case_table_names';

修复：

    # 如果列名大小写敏感
    SET GLOBAL lower_case_table_names = 1;

### Q6: 迁移后数据不一致

原因：迁移过程未完成或中断。

检查：

    python -m lightdb.tools.migrate --verify-only \
        --source /app/data-v3 \
        --target /app/data-v4

修复：重新迁移。

### Q7: 迁移耗时过长

原因：一次性迁移大数据量。

优化：

    # 分批迁移
    python -m lightdb.tools.migrate \
        --batch-size 10000 \
        --parallel 4

### Q8: 如何验证迁移成功？

三步验证：

1. **功能**：跑通应用的核心业务
2. **数据**：校验行数 + 校验和
3. **性能**：跑基准测试对比

任一环节失败，都建议回滚。

---

## 附录 A：迁移脚本速查

    # 干跑（不实际迁移）
    python -m lightdb.tools.migrate --dry-run \
        --source /app/data-v3 --target /app/data-v4

    # 完整迁移
    python -m lightdb.tools.migrate \
        --source /app/data-v3 --target /app/data-v4

    # 只迁移指定表
    python -m lightdb.tools.migrate \
        --source /app/data-v3 --target /app/data-v4 \
        --tables users orders

    # 只校验
    python -m lightdb.tools.migrate --verify-only \
        --source /app/data-v3 --target /app/data-v4

    # 增量同步
    python -m lightdb.tools.migrate --mode incremental \
        --source-host 127.0.0.1 --source-port 8888 \
        --target-host 127.0.0.1 --target-port 8889

---

## 附录 B：迁移检查清单

迁移前：

- [ ] 已完成全量备份
- [ ] 已备份配置文件
- [ ] 已通知相关方
- [ ] 已确认停机窗口
- [ ] 已准备回滚方案
- [ ] 已在测试环境演练

迁移中：

- [ ] 停止 v3 服务
- [ ] 执行迁移脚本
- [ ] 观察迁移日志
- [ ] 迁移完成无错误

迁移后：

- [ ] 服务可启动
- [ ] 数据完整
- [ ] 应用可连接
- [ ] 核心业务可跑通
- [ ] 性能符合预期
- [ ] 监控正常

观察期（24 小时）：

- [ ] 无异常错误日志
- [ ] 性能稳定
- [ ] 数据无丢失
- [ ] 无回滚需求

---

## 附录 C：与 v3.0 的关键差异速查

| 项目             | v3.0              | v4.0                       | 是否需要调整   |
|------------------|-------------------|----------------------------|:--------------:|
| 连接方式         | host:port         | host:port                  | 否             |
| SQL 语法         | -                 | -                          | 否             |
| 配置文件         | 30 字段           | 40 字段                    | 是（新增）     |
| 部署方式         | 单容器            | Compose                    | 建议           |
| 会话变量         | 进程内            | 跨进程（需注意）           | 是             |
| 临时表           | 单进程可用        | 单 Worker 有效             | 是             |
| 长事务           | 无限制            | 建议 < 30 秒               | 是             |
| 内存要求         | 4 GB              | 8 GB                       | 是             |

---

文档反馈：migration@lightdb.io

==== END FILE ====