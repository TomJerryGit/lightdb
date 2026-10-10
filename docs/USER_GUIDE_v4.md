==== BEGIN FILE: docs/USER_GUIDE_v4.md ====

# LightDB v4.0 用户指南

**版本**：4.0.0
**更新日期**：2025-08-01
**适用对象**：应用开发者、数据分析师、SQL 使用者

---

## 目录

1. 快速开始
2. 连接数据库
3. SQL 参考
4. v4.0 新特性
5. HTAP 混合表
6. 列式存储与 OLAP
7. 窗口函数
8. JSON 支持
9. 常见问题

---

## 1. 快速开始

### 1.1 30 秒跑起来

Docker 一键启动（推荐）：

    docker run -d --name lightdb \
        -p 8888:8888 \
        -v lightdb-data:/app/data \
        lightdb/server:4.0.0

或从源码启动：

    python -m lightdb.main --mode multi --workers 8 --port 8888

### 1.2 用 mysql 客户端连接

LightDB 兼容 MySQL Wire Protocol v10，可直接使用任意 MySQL 客户端：

    mysql -h 127.0.0.1 -P 8888 -u root

或使用 LightDB 自带的 CLI：

    lightdb -h 127.0.0.1 -P 8888 -u root

### 1.3 第一条 SQL

    CREATE TABLE users (
        id INT PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        age INT,
        created_at DATETIME DEFAULT NOW()
    );

    INSERT INTO users VALUES (1, 'Alice', 28, NOW());
    INSERT INTO users VALUES (2, 'Bob', 32, NOW());

    SELECT * FROM users WHERE age > 25;

---

## 2. 连接数据库

### 2.1 连接参数

| 参数     | 默认值        | 说明       |
|----------|--------------|------------|
| host     | 127.0.0.1    | 服务器地址 |
| port     | 8888         | 监听端口   |
| user     | root         | 用户名     |
| password | （空）        | 密码       |
| charset  | utf8mb4      | 字符集     |
| database | default      | 默认库     |

### 2.2 各语言示例

Python（PyMySQL）：

    import pymysql

    conn = pymysql.connect(
        host='127.0.0.1', port=8888,
        user='root', password='',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE age > %s", (25,))
        for row in cur.fetchall():
            print(row)

Java（JDBC）：

    String url = "jdbc:mysql://127.0.0.1:8888/default" +
                 "?useSSL=false&serverTimezone=Asia/Shanghai";
    Connection conn = DriverManager.getConnection(url, "root", "");

Go（database/sql）：

    db, _ := sql.Open("mysql", "root:@tcp(127.0.0.1:8888)/default")
    rows, _ := db.Query("SELECT * FROM users WHERE age > ?", 25)

### 2.3 连接池建议

- 连接数：每个应用实例 5-20 条连接
- 超时：connect_timeout=10，read_timeout=30
- autoReconnect：建议关闭，由连接池处理重连

---

## 3. SQL 参考

### 3.1 支持情况总览

| 类别       | 支持 | 说明                          |
|------------|:----:|-------------------------------|
| DDL        | 是   | CREATE / DROP / ALTER TABLE   |
| DML        | 是   | INSERT / UPDATE / DELETE      |
| JOIN       | 是   | INNER / LEFT / RIGHT / CROSS  |
| 子查询     | 是   | 标量、IN、EXISTS              |
| 事务       | 是   | BEGIN / COMMIT / ROLLBACK     |
| 窗口函数   | 是   | ROW_NUMBER / RANK / LAG 等    |
| CTE        | 是   | 普通 + 递归                   |
| 存储过程   | 是   | CREATE PROCEDURE / CALL       |
| 触发器     | 是   | BEFORE / AFTER                |
| 事件调度   | 是   | CREATE EVENT ... ON SCHEDULE  |
| JSON       | 是   | JSON_EXTRACT / SET / REMOVE   |
| 全文索引   | 是   | FULLTEXT + BM25               |
| 分区表     | 是   | HASH / RANGE / LIST           |
| 生成列     | 是   | GENERATED ALWAYS AS           |
| CHECK 约束 | 是   | -                             |

与 MySQL 5.7 综合兼容度：95%（v3.0 为 80%）。

### 3.2 数据类型

    INT           -- 32 位整数
    BIGINT        -- 64 位整数
    DECIMAL(M,D)  -- 定点小数
    FLOAT         -- 单精度
    DOUBLE        -- 双精度
    VARCHAR(N)    -- 变长字符串
    TEXT          -- 长文本
    DATETIME      -- 日期时间
    DATE          -- 日期
    BOOLEAN       -- 布尔
    JSON          -- JSON 文档

### 3.3 索引

主键（建表时）：

    id INT PRIMARY KEY

唯一索引：

    CREATE UNIQUE INDEX idx_email ON users(email);

普通索引：

    CREATE INDEX idx_age ON users(age);

复合索引（最左前缀原则）：

    CREATE INDEX idx_city_age ON users(city, age);

全文索引：

    CREATE FULLTEXT INDEX idx_content ON articles(content);

---

## 4. v4.0 新特性

### 4.1 多进程架构（对用户透明）

v4.0 默认启用多进程（Worker 数 = CPU 核数）。从用户角度：

- 连接方式不变：还是连 host:port
- 并发能力提升：8 核可从 8,200 QPS 提升到 92,000 QPS
- 会话无状态：任何请求可能落到任意 Worker

注意：如果使用长事务，请确保事务内不依赖连接级的临时状态（如 SET @x = 1 后跨事务使用），因为可能落到不同 Worker。建议会话变量在单个事务内使用。

### 4.2 Cython 加速（自动启用）

v4.0 内置 6 个 Cython 模块：

- B+Tree 查询
- 行编解码
- 页操作
- 谓词过滤
- Buffer Pool
- 哈希表

用户侧无需任何配置，服务启动时会自动检测。如果编译失败会自动回退到 Python 版本，功能不受影响。

### 4.3 列式存储 + 向量化查询

v4.0 新增 OLAP 引擎，适合大数据量聚合分析：

    SELECT region, SUM(amount), AVG(amount), COUNT(*)
    FROM orders
    GROUP BY region;

底层使用 NumPy 向量化 + 列式压缩（3-60x），无 SQL 语法变化。

---

## 5. HTAP 混合表

### 5.1 什么是混合表

- 普通表：只有行存，OLTP 快，OLAP 慢
- 普通列存：只有列存，OLAP 快，OLTP 慢
- 混合表：行存 + 列存双写，OLTP 和 OLAP 都快

### 5.2 创建混合表

    CREATE TABLE orders (
        id INT PRIMARY KEY,
        user_id INT,
        amount DECIMAL(10,2),
        region VARCHAR(32),
        created_at DATETIME
    ) ENGINE=HYBRID;

### 5.3 使用

OLTP 写入：

    INSERT INTO orders VALUES (1, 100, 99.9, 'cn-bj', NOW());

OLAP 聚合（自动走列存）：

    SELECT region, SUM(amount) FROM orders GROUP BY region;

### 5.4 同步策略

| 模式     | 触发条件      | 说明       |
|----------|---------------|------------|
| 阈值触发 | 累积 1000 行  | 默认       |
| 时间触发 | 每 60 秒      | 默认       |
| 手动触发 | FLUSH COLUMNAR orders; | 立即同步 |

---

## 6. 列式存储与 OLAP

### 6.1 5 种编码

| 编码    | 适用场景         | 压缩比  |
|---------|------------------|---------|
| Plain   | 通用             | 1x      |
| Dict    | 低基数字符串     | 5-20x   |
| RLE     | 大量重复         | 10-50x  |
| Delta   | 单调递增整数     | 5-15x   |
| BitPack | 小整数           | 3-10x   |

系统自动选择，无需手动指定。

### 6.2 OLAP 查询示例

大表聚合（3.5M rows/s）：

    SELECT
        DATE_FORMAT(created_at, '%Y-%m') AS month,
        region,
        SUM(amount) AS total,
        COUNT(*) AS cnt
    FROM orders
    WHERE created_at >= '2025-01-01'
    GROUP BY month, region
    ORDER BY month DESC, total DESC;

### 6.3 并行查询

大数据量查询会自动并行（阈值 10,000 行）：

    SELECT COUNT(*) FROM orders WHERE amount > 1000;

内部自动分片并行 COUNT。

---

## 7. 窗口函数

### 7.1 排名函数

    SELECT
        name,
        score,
        ROW_NUMBER() OVER (ORDER BY score DESC) AS rn,
        RANK()       OVER (ORDER BY score DESC) AS rk,
        DENSE_RANK() OVER (ORDER BY score DESC) AS drk
    FROM students;

### 7.2 分组内排名

    SELECT
        dept, name, salary,
        ROW_NUMBER() OVER (
            PARTITION BY dept ORDER BY salary DESC
        ) AS rank_in_dept
    FROM employees;

### 7.3 移动平均

    SELECT
        trade_date,
        close_price,
        AVG(close_price) OVER (
            ORDER BY trade_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS ma7
    FROM stocks;

### 7.4 LAG / LEAD

    SELECT
        trade_date,
        close_price,
        LAG(close_price, 1)  OVER (ORDER BY trade_date) AS prev_close,
        LEAD(close_price, 1) OVER (ORDER BY trade_date) AS next_close
    FROM stocks;

---

## 8. JSON 支持

### 8.1 存储

    CREATE TABLE events (
        id INT PRIMARY KEY,
        data JSON
    );

    INSERT INTO events VALUES (
        1,
        '{"user": "alice", "action": "login", "ip": "1.2.3.4"}'
    );

### 8.2 查询

提取字段：

    SELECT JSON_EXTRACT(data, '$.user') FROM events;

路径操作符：

    SELECT data->'$.user', data->>'$.ip' FROM events;

条件查询：

    SELECT * FROM events WHERE data->'$.action' = 'login';

类型检查：

    SELECT JSON_TYPE(data), JSON_VALID(data) FROM events;

### 8.3 修改

    UPDATE events
    SET data = JSON_SET(data, '$.status', 'success')
    WHERE id = 1;

    UPDATE events
    SET data = JSON_REMOVE(data, '$.ip')
    WHERE id = 1;

---

## 9. 常见问题

### Q1: 为什么我的查询变慢了？

1. 用 EXPLAIN 查看执行计划
2. 检查是否走了索引
3. 检查是否命中缓冲池

    EXPLAIN SELECT * FROM users WHERE age > 25;
    SHOW STATUS LIKE '%buffer_pool%';

### Q2: 连接数超限怎么办？

    SHOW STATUS LIKE 'Threads_connected';
    SET GLOBAL max_connections = 500;

### Q3: 如何查看慢查询？

    lightdb-check -h 127.0.0.1 -P 8888 -u root
    lightdbadmin status

### Q4: 支持哪些客户端？

任何 MySQL 5.7 兼容客户端，包括：

- mysql 命令行
- Navicat、DBeaver、DataGrip
- PyMySQL、JDBC、GORM

### Q5: 如何查看版本？

    SELECT VERSION();
    -- 5.7.30-LightDB

---

## 附录：SQL 命令速查

库/表管理：

    SHOW DATABASES;
    SHOW TABLES;
    SHOW COLUMNS FROM users;
    DESC users;

事务：

    BEGIN;
    UPDATE users SET age = 30 WHERE id = 1;
    COMMIT;
    -- 或 ROLLBACK;

性能：

    EXPLAIN SELECT ...;
    SHOW STATUS;
    SHOW STATS;
    CHECK TABLE users;

权限：

    CREATE USER 'alice' IDENTIFIED BY 'password';
    GRANT SELECT ON *.* TO 'alice';
    SHOW GRANTS FOR 'alice';

---

反馈：https://github.com/lightdb/lightdb/issues

==== END FILE ====