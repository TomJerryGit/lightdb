==== BEGIN FILE: docs/MIGRATION_GUIDE_v3.md ====

# LightDB v3.0 数据库迁移指南

**目标版本**：LightDB v3.0.x
**更新日期**：2025-02-01
**适用对象**：从 MySQL / MariaDB / SQLite 迁移到 LightDB 的用户

---

## 目录

1. 概述
2. 兼容性矩阵
3. 迁移前准备
4. 迁移方式选择
5. 数据类型映射
6. SQL 语法差异
7. 迁移步骤
8. 数据验证
9. 常见问题
10. 回滚方案

---

## 1. 概述

### 1.1 本文档范围

本文档介绍如何将**已有数据库**迁移到 **LightDB v3.0**：

- MySQL 5.7 → LightDB v3.0
- MariaDB 10.x → LightDB v3.0
- SQLite 3.x → LightDB v3.0
- MySQL 8.0 → LightDB v3.0（部分特性需降级）

**注意**：本文档**不涉及** LightDB v3.0 → v4.0 的版本升级，那部分请参考 `MIGRATION_v3_to_v4.md`。

### 1.2 迁移的三大任务

| 任务         | 说明                          | 工具            |
|--------------|-------------------------------|-----------------|
| 结构迁移     | 表、索引、约束、视图、存储过程| lightdb-migrate |
| 数据迁移     | 行数据从源库搬到目标库        | lightdb-migrate |
| 应用适配     | 修改应用中不兼容的 SQL/驱动   | 手工            |

### 1.3 迁移前需回答的问题

1. **数据量多大？** 决定迁移耗时和方式
2. **有多少张表？** 决定是否分批迁移
3. **有存储过程/触发器吗？** 需要手工检查语法
4. **有外键约束吗？** 需要按依赖顺序迁移
5. **能接受停机吗？** 决定用离线还是在线迁移

---

## 2. 兼容性矩阵

### 2.1 数据类型兼容性

MySQL → LightDB v3.0：

| MySQL 类型        | LightDB v3.0      | 兼容 | 说明                |
|-------------------|-------------------|:----:|---------------------|
| TINYINT           | INT               | 是   | 自动升为 INT        |
| SMALLINT          | INT               | 是   | -                   |
| MEDIUMINT         | INT               | 是   | -                   |
| INT / INTEGER     | INT               | 是   | 完全兼容            |
| BIGINT            | BIGINT            | 是   | -                   |
| DECIMAL(M,D)      | DECIMAL(M,D)      | 是   | 完全兼容            |
| FLOAT             | FLOAT             | 是   | -                   |
| DOUBLE            | DOUBLE            | 是   | -                   |
| BIT               | INT               | 是   | 转换为 INT          |
| DATE              | DATE              | 是   | -                   |
| DATETIME          | DATETIME          | 是   | -                   |
| TIMESTAMP         | DATETIME          | 是   | 转为 DATETIME       |
| TIME              | TEXT              | 是   | 降级为 TEXT         |
| YEAR              | INT               | 是   | -                   |
| CHAR(N)           | VARCHAR(N)        | 是   | 等价                |
| VARCHAR(N)        | VARCHAR(N)        | 是   | -                   |
| TINYTEXT          | TEXT              | 是   | 统一为 TEXT         |
| TEXT              | TEXT              | 是   | -                   |
| MEDIUMTEXT        | TEXT              | 是   | 统一为 TEXT         |
| LONGTEXT          | TEXT              | 是   | 统一为 TEXT         |
| BLOB              | TEXT              | 是   | 转为 TEXT（二进制安全） |
| JSON              | JSON              | 是   | -                   |
| ENUM              | VARCHAR(64)       | 是   | 转为 VARCHAR        |
| SET               | VARCHAR(255)      | 是   | 转为 VARCHAR        |
| GEOMETRY          | TEXT              | 部分 | 转为 WKT 字符串     |

**结论**：类型层面基本无阻塞，但注意：

- `TINYINT` 会升为 `INT`，占用更多空间
- `TIMESTAMP` 转为 `DATETIME`，失去时区自动转换
- `ENUM`/`SET` 转为 `VARCHAR`，失去约束
- `BLOB` 转为 `TEXT`，二进制数据用 base64 编码

### 2.2 SQL 语法兼容性

| SQL 特性             | MySQL 5.7 | LightDB v3.0 | 说明                       |
|----------------------|:---------:|:------------:|----------------------------|
| SELECT / JOIN        | 是        | 是           | 完全兼容                   |
| 子查询               | 是        | 是           | 完全兼容                   |
| UNION                | 是        | 是           | -                          |
| 窗口函数             | 是        | 是           | 8 种排名 + 5 种取值        |
| CTE                  | 是        | 是           | 普通 + 递归                |
| 存储过程             | 是        | 是           | 支持 IF/WHILE/CURSOR       |
| 触发器               | 是        | 是           | BEFORE/AFTER               |
| 事件调度             | 是        | 是           | -                          |
| 视图                 | 是        | 是           | -                          |
| 分区表               | 是        | 是           | HASH/RANGE/LIST            |
| 全文索引             | 是        | 是           | BM25 排序                  |
| JSON 函数            | 是        | 是           | 主要函数支持               |
| 生成列               | 是        | 是           | STORED/VIRTUAL             |
| CHECK 约束           | 是        | 是           | -                          |
| 外键                 | 是        | 是           | -                          |
| 用户变量 @var        | 是        | 是           | 支持                       |
| 会话变量 @@var       | 是        | 是           | 支持                       |
| PREPARE/EXECUTE      | 是        | 部分         | 预备语句支持有限           |
| 存储函数             | 是        | 是           | -                          |
| LOAD DATA            | 是        | 部分         | 建议改用 lightdb-migrate   |
| mysqldump 语法       | 是        | 是           | 兼容导出文件格式           |

**综合兼容度**：MySQL 5.7 的 **92%**。

### 2.3 客户端兼容性

| 客户端             | 兼容性 | 说明                |
|--------------------|:------:|---------------------|
| mysql client 8.0   | 完全   | 直接可用            |
| mysql client 5.7   | 完全   | 直接可用            |
| PyMySQL 1.x        | 完全   | 无需修改代码        |
| mysql-connector    | 完全   | 无需修改代码        |
| JDBC 8.x           | 完全   | 无需修改代码        |
| Go database/sql    | 完全   | 无需修改代码        |
| Node mysql2        | 完全   | 无需修改代码        |
| Navicat / DBeaver  | 完全   | 直接连接            |
| MySQL Workbench    | 部分   | 元数据查询可能异常  |
| PHP PDO            | 完全   | 无需修改代码        |

### 2.4 不支持的特性

以下 MySQL 特性在 LightDB v3.0 中**不支持或部分支持**：

| 特性                     | 状态   | 建议                       |
|--------------------------|:------:|----------------------------|
| 存储引擎选择（MyISAM 等）| 无     | LightDB 只有一种引擎       |
| 主从 GTID                | 无     | 用 position 复制           |
| MySQL 8.0 角色           | 无     | 用 GRANT 直接授权          |
| MySQL 8.0 窗口框架       | 部分   | 支持 ROWS，不支持 GROUPS   |
| 空间索引（R-Tree）       | 无     | 用普通索引 + 几何函数      |
| 加密函数 AES_ENCRYPT     | 部分   | 需应用层处理               |
| PLUGIN 系统              | 无     | 无插件机制                 |
| 性能模式（performance_schema）| 无 | 用 information_schema     |
| X Protocol               | 无     | 用 MySQL Protocol          |
| 多源复制                 | 无     | 单源复制                   |

---

## 3. 迁移前准备

### 3.1 环境检查

**源库信息采集**：

    -- 版本
    SELECT VERSION();

    -- 表数量
    SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'your_db';

    -- 数据量
    SELECT table_name,
           table_rows,
           ROUND(data_length/1024/1024, 2) AS data_mb
    FROM information_schema.tables
    WHERE table_schema = 'your_db';

    -- 字符集
    SHOW VARIABLES LIKE 'character_set%';

    -- 存储引擎分布
    SELECT engine, COUNT(*)
    FROM information_schema.tables
    WHERE table_schema = 'your_db'
    GROUP BY engine;

**目标库检查**：

    # LightDB 是否可连接
    lightdb-check -h 127.0.0.1 -P 8888 -u root

    # 磁盘空间
    df -h /app/data

    # 内存
    free -h

### 3.2 备份源库

**全量备份**（必做）：

    mysqldump -h source-host -u root -p \
        --single-transaction \
        --routines \
        --triggers \
        --events \
        --databases your_db \
        > /backup/source_full.sql

**物理备份**（推荐）：

    # 使用 XtraBackup 或冷备
    systemctl stop mysql
    tar czf /backup/mysql_data.tar.gz /var/lib/mysql
    systemctl start mysql

### 3.3 生成迁移清单

    python -m lightdb.tools.migrate inspect \
        --source mysql \
        --source-host source-host \
        --source-user root \
        --source-password pass \
        --source-db your_db \
        --output /tmp/migration_report.json

输出示例：

    {
      "source": {
        "type": "mysql",
        "version": "5.7.40",
        "database": "your_db",
        "tables": 45,
        "total_rows": 12500000,
        "total_size_mb": 3200
      },
      "type_mapping": {
        "TINYINT": 12,
        "INT": 30,
        "BIGINT": 8,
        "VARCHAR": 45,
        "TEXT": 5,
        "DATETIME": 20,
        "DECIMAL": 6
      },
      "warnings": [
        "3 张表使用了 ENUM 类型，将转为 VARCHAR",
        "2 张表使用了 TIMESTAMP，将转为 DATETIME",
        "1 个存储过程包含 MySQL 特有语法"
      ],
      "estimated_time_minutes": 45
    }

### 3.4 停机窗口规划

| 数据量   | 预估停机时长 |
|----------|:------------:|
| < 1 GB   | 5 分钟       |
| 1-10 GB  | 15-30 分钟   |
| 10-100 GB| 1-2 小时     |
| > 100 GB | 使用在线迁移 |

---

## 4. 迁移方式选择

### 4.1 三种方式

**方式 A：mysqldump + lightdb 导入**（推荐小库）

- 优点：简单、通用、可审查
- 缺点：大数据量慢、锁表

**方式 B：lightdb-migrate 直连迁移**（推荐中等规模）

- 优点：自动类型映射、进度可见、可断点续传
- 缺点：需要目标库已启动

**方式 C：CDC 在线迁移**（推荐生产）

- 优点：接近零停机、可灰度切换
- 缺点：配置复杂

### 4.2 选择建议

| 场景                | 推荐方式 |
|---------------------|:--------:|
| 数据量 < 1 GB       | A        |
| 数据量 1-50 GB      | B        |
| 数据量 > 50 GB      | C        |
| 不能停机            | C        |
| 允许 4 小时停机     | B        |

---

## 5. 数据类型映射

### 5.1 自动映射

lightdb-migrate 会自动完成映射，无需手动处理。映射规则如下：

**整数族**：

    TINYINT     -> INT
    SMALLINT    -> INT
    MEDIUMINT   -> INT
    INT         -> INT
    INTEGER     -> INT
    BIGINT      -> BIGINT

**小数族**：

    DECIMAL(M,D) -> DECIMAL(M,D)   # 保持精度
    NUMERIC      -> DECIMAL
    FLOAT        -> FLOAT
    DOUBLE       -> DOUBLE
    REAL         -> DOUBLE

**字符族**：

    CHAR(N)      -> VARCHAR(N)
    VARCHAR(N)   -> VARCHAR(N)
    TINYTEXT     -> TEXT
    TEXT         -> TEXT
    MEDIUMTEXT   -> TEXT
    LONGTEXT     -> TEXT
    ENUM         -> VARCHAR(64)
    SET          -> VARCHAR(255)

**时间族**：

    DATE         -> DATE
    TIME         -> TEXT
    DATETIME     -> DATETIME
    TIMESTAMP    -> DATETIME
    YEAR         -> INT

**其他**：

    BOOLEAN      -> BOOLEAN
    BIT          -> INT
    BINARY(N)    -> TEXT
    VARBINARY(N) -> TEXT
    BLOB         -> TEXT
    JSON         -> JSON

### 5.2 需要手工注意的映射

**TIMESTAMP 转 DATETIME**

MySQL 的 TIMESTAMP 会自动处理时区，LightDB 的 DATETIME 不处理。

迁移时需要显式转换时区：

    -- 数据迁移时
    SELECT CONVERT_TZ(created_at, '+00:00', '+08:00') FROM t;

**ENUM 转 VARCHAR**

丢失约束，但内容保留。如果需要恢复约束，用 CHECK：

    ALTER TABLE t ADD CONSTRAINT chk_status
        CHECK (status IN ('active', 'inactive', 'deleted'));

**BLOB 转 TEXT**

二进制数据会在迁移时进行 base64 编码：

    -- 源数据
    BLOB: 0x89504E47...

    -- 目标数据（TEXT）
    TEXT: 'iVBORw0KGgo...'  -- base64

应用侧需要相应解码。

---

## 6. SQL 语法差异

### 6.1 差异清单

| 语法点              | MySQL              | LightDB v3.0       | 影响               |
|---------------------|--------------------|--------------------|--------------------|
| 反引号              | 支持               | 支持               | 无                 |
| LIMIT a, b          | 支持               | 只支持 LIMIT n OFFSET m | 需改写        |
| IFNULL              | 支持               | 支持               | 无                 |
| DATE_ADD            | 支持               | 支持               | 无                 |
| GROUP_CONCAT        | 支持               | 部分支持           | 复杂用法可能失败   |
| ON DUPLICATE KEY    | 支持               | 部分支持           | 需改用 REPLACE     |
| INSERT IGNORE       | 支持               | 支持               | 无                 |
| REPLACE INTO        | 支持               | 支持               | 无                 |
| LOAD DATA INFILE    | 支持               | 不支持             | 改用批量 INSERT    |
| SELECT INTO OUTFILE | 支持               | 不支持             | 改用 dump          |
| SET GLOBAL          | 支持               | 支持               | 无                 |
| SHOW PROCESSLIST    | 支持               | 支持               | 无                 |
| SHOW ENGINE INNODB  | 支持               | 部分支持           | 用 SHOW STATUS     |
| SELECT @@GLOBAL.x   | 支持               | 部分支持           | 常用变量支持       |

### 6.2 常见 SQL 改写

**LIMIT a, b**：

    -- MySQL
    SELECT * FROM users LIMIT 100, 50;

    -- LightDB
    SELECT * FROM users LIMIT 50 OFFSET 100;

**ON DUPLICATE KEY UPDATE**：

    -- MySQL
    INSERT INTO users (id, name) VALUES (1, 'Alice')
    ON DUPLICATE KEY UPDATE name = 'Alice';

    -- LightDB v3.0
    REPLACE INTO users (id, name) VALUES (1, 'Alice');

**GROUP_CONCAT 复杂用法**：

    -- MySQL
    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR '|')
    FROM users GROUP BY city;

    -- LightDB v3.0（简化用法）
    SELECT GROUP_CONCAT(name) FROM users GROUP BY city;

**时间戳转换**：

    -- MySQL
    SELECT UNIX_TIMESTAMP(created_at) FROM t;

    -- LightDB
    SELECT UNIX_TIMESTAMP(created_at) FROM t;  -- 兼容

### 6.3 存储过程差异

**变量声明**：

    -- MySQL
    DECLARE v_id INT DEFAULT 0;
    DECLARE v_name VARCHAR(64);

    -- LightDB v3.0（兼容）
    DECLARE v_id INT DEFAULT 0;
    DECLARE v_name VARCHAR(64);

**游标**：

    -- MySQL
    DECLARE cur CURSOR FOR SELECT id FROM users;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

    -- LightDB v3.0（兼容）
    DECLARE cur CURSOR FOR SELECT id FROM users;
    -- CONTINUE HANDLER 语法兼容

**存储过程元数据查询**：

    -- MySQL
    SELECT ROUTINE_NAME FROM information_schema.routines;

    -- LightDB v3.0
    SHOW PROCEDURES;

---

## 7. 迁移步骤

### 7.1 方式 A：mysqldump 导入

**第一步：导出源库结构**

    mysqldump -h source-host -u root -p \
        --no-data --routines --triggers --events \
        --databases your_db \
        > /tmp/schema.sql

**第二步：修改 dump 文件（关键）**

用编辑器打开 `schema.sql`，替换 MySQL 特有语法：

    # 1. 移除 ENGINE=InnoDB
    sed -i 's/ENGINE=InnoDB//g' /tmp/schema.sql

    # 2. 移除 CHARSET/COLLATE
    sed -i 's/DEFAULT CHARSET=[a-z0-9_]*//g' /tmp/schema.sql
    sed -i 's/COLLATE=[a-z0-9_]*//g' /tmp/schema.sql

    # 3. 移除 AUTO_INCREMENT=数字
    sed -i 's/AUTO_INCREMENT=[0-9]*//g' /tmp/schema.sql

    # 4. 移除 ROW_FORMAT=xxx
    sed -i 's/ROW_FORMAT=[A-Z]*//g' /tmp/schema.sql

    # 5. 移除 KEY_BLOCK_SIZE
    sed -i 's/KEY_BLOCK_SIZE=[0-9]*//g' /tmp/schema.sql

**第三步：导入结构**

    lightdb -h 127.0.0.1 -P 8888 -u root < /tmp/schema.sql

**第四步：导出源库数据**

    mysqldump -h source-host -u root -p \
        --no-create-info \
        --databases your_db \
        --skip-extended-insert \
        > /tmp/data.sql

**第五步：导入数据**

    lightdb -h 127.0.0.1 -P 8888 -u root < /tmp/data.sql

### 7.2 方式 B：lightdb-migrate 直连迁移

**第一步：启动目标库**

    docker run -d --name lightdb \
        -p 8888:8888 \
        -v /data/lightdb:/app/data \
        lightdb/server:3.0.0

**第二步：执行迁移**

    python -m lightdb.tools.migrate \
        --source mysql \
        --source-host source-host \
        --source-port 3306 \
        --source-user root \
        --source-password pass \
        --source-db your_db \
        --target-host 127.0.0.1 \
        --target-port 8888 \
        --target-user root \
        --target-db default \
        --batch-size 1000

**迁移输出**：

    ==========================================
      LightDB v3.0 数据库迁移
    ==========================================
      源:   mysql://source-host:3306/your_db
      目标: lightdb://127.0.0.1:8888/default
      批大小: 1000

    [1/5] 检查连接...      OK
    [2/5] 读取源表结构...  45 张表
    [3/5] 创建目标表...
      users       OK
      orders      OK
      products    OK
      ...
    [4/5] 迁移数据...
      users:     100,000 行  [==========] 100%
      orders:  1,000,000 行  [==========] 100%
      ...
    [5/5] 校验...
      行数:     12500000 == 12500000
      校验和:   OK

    ==========================================
      迁移成功，耗时 32 分钟
    ==========================================

**第三步：验证**

    lightdb-check -h 127.0.0.1 -P 8888 -u root

### 7.3 方式 C：CDC 在线迁移

**第一步：启动目标库（独立实例）**

    docker run -d --name lightdb-v3 \
        -p 8889:8888 \
        -v /data/lightdb-v3:/app/data \
        lightdb/server:3.0.0

**第二步：全量初始化**

    python -m lightdb.tools.migrate \
        --source mysql --source-host source-host \
        --target-host 127.0.0.1 --target-port 8889 \
        --mode initial

**第三步：开启 CDC 增量同步**

    python -m lightdb.tools.migrate \
        --source mysql --source-host source-host \
        --target-host 127.0.0.1 --target-port 8889 \
        --mode cdc \
        --cdc-mode binlog \
        --position-file /data/cdc_position.json

**第四步：等待追平**

监控延迟：

    python -m lightdb.tools.migrate --status

输出：

    {
      "source_position": 1234567,
      "target_position": 1234567,
      "lag_seconds": 0.5,
      "status": "synced"
    }

**第五步：切换流量**

1. 应用停止写入源库
2. 等待 CDC 延迟归零
3. 修改应用配置指向 LightDB（端口 8889）
4. 应用开始使用 LightDB

**第六步：验证观察**

观察 24 小时无异常后下线源库。

---

## 8. 数据验证

### 8.1 结构验证

**表数量**：

    -- 源库
    SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'your_db';

    -- 目标库
    SELECT COUNT(*) FROM information_schema.tables;

**列定义**：

    -- 源库
    SHOW COLUMNS FROM users;

    -- 目标库
    SHOW COLUMNS FROM users;

### 8.2 行数验证

**逐表行数对比**：

    #!/bin/bash
    for table in users orders products; do
        src=$(mysql -h source-host -u root -ppass your_db \
              -e "SELECT COUNT(*) FROM $table" -sN)
        dst=$(mysql -h 127.0.0.1 -P 8888 -u root default \
              -e "SELECT COUNT(*) FROM $table" -sN)
        if [ "$src" == "$dst" ]; then
            echo "OK   $table: $src"
        else
            echo "FAIL $table: src=$src, dst=$dst"
        fi
    done

### 8.3 校验和验证

**逐表校验和**：

    -- 源库
    SELECT MD5(GROUP_CONCAT(id ORDER BY id)) FROM users;

    -- 目标库
    SELECT MD5(GROUP_CONCAT(id ORDER BY id)) FROM users;

两次结果应一致。

### 8.4 抽样验证

随机抽取 100 行对比：

    #!/bin/bash
    mysql -h source-host -u root -ppass your_db \
        -e "SELECT * FROM users ORDER BY RAND() LIMIT 100" \
        > /tmp/src_sample.txt

    mysql -h 127.0.0.1 -P 8888 -u root default \
        -e "SELECT * FROM users ORDER BY RAND() LIMIT 100" \
        > /tmp/dst_sample.txt

    diff /tmp/src_sample.txt /tmp/dst_sample.txt

### 8.5 功能验证清单

- [ ] 版本正确（SELECT VERSION()）
- [ ] 库存在（SHOW DATABASES）
- [ ] 表齐全（SHOW TABLES）
- [ ] 索引创建（SHOW COLUMNS）
- [ ] 主键正常（SELECT id FROM t LIMIT 1）
- [ ] 外键生效（插入违规数据应失败）
- [ ] 事务可用（BEGIN/ROLLBACK）
- [ ] 视图可查
- [ ] 存储过程可调用
- [ ] 触发器生效
- [ ] 权限正确（SHOW GRANTS）

---

## 9. 常见问题

### Q1: 迁移过程中报 "Duplicate entry"

原因：主键冲突，通常是分表迁移时未清理目标表。

修复：

    -- 迁移前清空目标表
    TRUNCATE TABLE users;

    -- 或使用 INSERT IGNORE / REPLACE INTO

### Q2: 迁移后字符乱码

原因：字符集不匹配。

检查：

    SHOW VARIABLES LIKE 'character_set%';

修复：

    -- 1. 确认目标库为 utf8mb4
    SET NAMES utf8mb4;

    -- 2. 重新迁移
    python -m lightdb.tools.migrate \
        --charset utf8mb4 \
        ...

### Q3: 大表迁移超时

原因：单次批量太大或网络慢。

修复：

    # 减小批大小
    python -m lightdb.tools.migrate --batch-size 100

    # 或分表迁移
    python -m lightdb.tools.migrate --tables users

### Q4: 迁移后查询变慢

原因：缺少索引或计划缓存未预热。

修复：

    -- 1. 检查索引
    SHOW COLUMNS FROM users;

    -- 2. 重建索引
    CREATE INDEX idx_name ON users(name);

    -- 3. 预热
    SELECT * FROM users LIMIT 1000;

    -- 4. ANALYZE TABLE 更新统计
    ANALYZE TABLE users;

### Q5: 存储过程迁移失败

原因：使用了 LightDB 不支持的过程语法。

修复：

1. 导出源过程定义：

       SHOW CREATE PROCEDURE proc_name;

2. 手工审查语法

3. 用兼容语法重写

4. 在 LightDB 中创建：

       CREATE PROCEDURE proc_name(...)
       BEGIN
           ...
       END;

### Q6: 触发器不生效

原因：可能是语法不兼容或权限问题。

检查：

    SHOW TRIGGERS;

修复：

    -- 重新创建触发器
    DROP TRIGGER trigger_name;
    CREATE TRIGGER ...;

### Q7: 外键约束报错

原因：外键依赖顺序问题。

修复：

    -- 1. 迁移时关闭外键检查
    SET FOREIGN_KEY_CHECKS = 0;
    -- ... 迁移 ...
    SET FOREIGN_KEY_CHECKS = 1;

    -- 2. 按依赖顺序迁移表
    -- 先迁移被引用表，再迁移引用表

### Q8: 迁移后自增值不对

原因：AUTO_INCREMENT 未正确初始化。

修复：

    -- 检查当前最大 ID
    SELECT MAX(id) FROM users;

    -- 手动设置下一个 ID
    ALTER TABLE users AUTO_INCREMENT = 100001;

---

## 10. 回滚方案

### 10.1 何时回滚

- 迁移后应用报大量错误
- 数据校验失败
- 性能无法接受
- 关键功能不可用

### 10.2 回滚步骤

**方式 A：源库仍在运行**

只需切回应用配置：

    # 修改应用配置
    database.url = "jdbc:mysql://source-host:3306/your_db"

重启应用，无需数据回滚。

**方式 B：源库已下线**

第一步，恢复源库：

    tar xzf /backup/mysql_data.tar.gz -C /
    systemctl start mysql

第二步，从 LightDB 导出增量（CDC 期间的写入）：

    lightdbdump -h 127.0.0.1 -P 8888 -u root \
        -D default --since "2025-02-01 00:00:00" \
        -o /tmp/increment.sql

第三步，导入源库：

    mysql -h source-host -u root -ppass your_db < /tmp/increment.sql

第四步，切回应用。

### 10.3 回滚检查清单

- [ ] 源库数据完整
- [ ] 源库权限正常
- [ ] 应用配置已切回
- [ ] 数据一致性验证
- [ ] 业务功能恢复正常

---

## 附录 A：迁移命令速查

**环境检查**：

    python -m lightdb.tools.migrate inspect \
        --source mysql \
        --source-host SOURCE_HOST \
        --source-db DB_NAME

**干跑**：

    python -m lightdb.tools.migrate --dry-run \
        --source mysql --source-host SOURCE_HOST \
        --target-host 127.0.0.1 --target-port 8888

**全量迁移**：

    python -m lightdb.tools.migrate \
        --source mysql --source-host SOURCE_HOST \
        --target-host 127.0.0.1 --target-port 8888 \
        --batch-size 1000

**只迁移结构**：

    python -m lightdb.tools.migrate --schema-only ...

**只迁移数据**：

    python -m lightdb.tools.migrate --data-only ...

**指定表**：

    python -m lightdb.tools.migrate \
        --tables users orders products ...

**CDC 模式**：

    python -m lightdb.tools.migrate --mode cdc \
        --source mysql --source-host SOURCE_HOST \
        --target-host 127.0.0.1 --target-port 8888

**状态查看**：

    python -m lightdb.tools.migrate --status

---

## 附录 B：迁移检查清单

**迁移前**：

- [ ] 已备份源库（逻辑 + 物理）
- [ ] 已完成环境检查
- [ ] 已生成迁移报告
- [ ] 已确认停机窗口
- [ ] 已通知相关方
- [ ] 已准备回滚方案

**迁移中**：

- [ ] 目标 LightDB 已启动
- [ ] 迁移脚本已执行
- [ ] 迁移日志无 ERROR
- [ ] 迁移耗时符合预期

**迁移后**：

- [ ] 表数量一致
- [ ] 行数一致
- [ ] 校验和一致
- [ ] 索引已创建
- [ ] 主键正常
- [ ] 外键生效
- [ ] 视图可查
- [ ] 存储过程可调用
- [ ] 触发器生效
- [ ] 应用可连接
- [ ] 核心业务可跑通

**观察期（24 小时）**：

- [ ] 无异常错误日志
- [ ] 性能符合预期
- [ ] 数据无丢失
- [ ] 无回滚需求

---

## 附录 C：MySQL 与 LightDB 语法速查对照

| 功能         | MySQL 5.7                    | LightDB v3.0                |
|--------------|------------------------------|-----------------------------|
| 版本查询     | SELECT VERSION()             | 同                          |
| 库列表       | SHOW DATABASES               | 同                          |
| 表列表       | SHOW TABLES                  | 同                          |
| 表结构       | SHOW COLUMNS FROM t          | 同                          |
| 索引信息     | SHOW INDEX FROM t            | 同                          |
| 建表语句     | SHOW CREATE TABLE t          | 同                          |
| 过程列表     | SHOW PROCEDURE STATUS        | SHOW PROCEDURES             |
| 函数列表     | SHOW FUNCTION STATUS         | SHOW FUNCTIONS              |
| 触发器列表   | SHOW TRIGGERS                | 同                          |
| 事件列表     | SHOW EVENTS                  | 同                          |
| 变量         | SHOW VARIABLES               | 同                          |
| 状态         | SHOW STATUS                  | 同                          |
| 进程列表     | SHOW PROCESSLIST             | 同                          |
| 引擎状态     | SHOW ENGINE INNODB STATUS    | SHOW STATUS                 |
| 权限         | SHOW GRANTS FOR u            | 同                          |
| 主从状态     | SHOW SLAVE STATUS            | 同                          |
| 主库状态     | SHOW MASTER STATUS           | 同                          |
| 表统计       | ANALYZE TABLE t              | ANALYZE TABLE t             |
| 表检查       | CHECK TABLE t                | 同                          |
| 表优化       | OPTIMIZE TABLE t             | 不支持                      |

---

文档反馈：migration@lightdb.io

==== END FILE ====