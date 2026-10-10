==== BEGIN FILE: docs/USER_GUIDE_v3.md ====

# LightDB v3.0 用户指南

**版本**：3.0.0
**更新日期**：2025-02-01
**适用对象**：应用开发者、数据分析师、SQL 使用者

---

## 目录

1. v3.0 简介
2. 快速开始
3. 连接数据库
4. SQL 参考
5. 过程语言（PL）
6. JSON 与全文索引
7. 分区表
8. 常见问题

---

## 1. v3.0 简介

### 1.1 版本定位

v3.0 是 LightDB 第一个**生产可用**的版本：

- 性能：MySQL 5.7 的 80%
- 稳定性：通过 30 天长稳测试
- 兼容性：MySQL 5.7 语法 92%
- 部署：单进程 Docker 一键启动

### 1.2 与 v2.0 的差异

| 特性          | v2.0     | v3.0           |
|---------------|----------|----------------|
| 单线程点查    | 5,500 QPS| 6,500 QPS      |
| 批量插入      | 55K rows/s | 70K rows/s   |
| 行格式        | JSON     | 二进制（3x）   |
| 批处理        | 无       | 1024 行/批     |
| 向量化        | 无       | NumPy          |
| 计划缓存      | 无       | 有             |
| 页预读        | 无       | 自适应         |
| 长稳测试      | 未验证   | 30 天通过      |
| 监控指标      | 少量     | 43 个          |

### 1.3 适用场景

- 中小型业务（QPS < 5,000）
- 开发/测试环境
- 单机部署
- 无 OLAP 需求

---

## 2. 快速开始

### 2.1 Docker 启动

    docker run -d --name lightdb \
        -p 8888:8888 \
        -v lightdb-data:/app/data \
        lightdb/server:3.0.0

### 2.2 源码启动

    python -m lightdb.main --mode single --port 8888

### 2.3 第一条 SQL

    mysql -h 127.0.0.1 -P 8888 -u root

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

## 3. 连接数据库

### 3.1 连接参数

| 参数     | 默认值    | 说明       |
|----------|-----------|------------|
| host     | 127.0.0.1 | 服务器地址 |
| port     | 8888      | 监听端口   |
| user     | root      | 用户名     |
| password | （空）    | 密码       |
| charset  | utf8mb4   | 字符集     |

### 3.2 各语言示例

Python（PyMySQL）：

    import pymysql

    conn = pymysql.connect(
        host='127.0.0.1', port=8888,
        user='root', password='',
        charset='utf8mb4',
    )
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE age > %s", (25,))
    for row in cur.fetchall():
        print(row)

Java（JDBC）：

    String url = "jdbc:mysql://127.0.0.1:8888/default";
    Connection conn = DriverManager.getConnection(url, "root", "");

Go：

    db, _ := sql.Open("mysql", "root:@tcp(127.0.0.1:8888)/default")

### 3.3 连接池建议

- 连接数：每应用实例 3-10 条
- 超时：connect_timeout=10，read_timeout=30
- v3.0 是单进程，会话是稳定的，无跨进程问题

---

## 4. SQL 参考

### 4.1 支持情况

| 类别       | 支持 | 说明                       |
|------------|:----:|----------------------------|
| DDL        | 是   | CREATE / DROP / ALTER      |
| DML        | 是   | INSERT / UPDATE / DELETE   |
| JOIN       | 是   | INNER / LEFT / RIGHT       |
| 子查询     | 是   | 标量、IN、EXISTS           |
| 事务       | 是   | BEGIN / COMMIT / ROLLBACK  |
| 窗口函数   | 是   | 8 种排名 + 5 种取值        |
| CTE        | 是   | 普通 + 递归                |
| 存储过程   | 是   | CREATE PROCEDURE / CALL    |
| 触发器     | 是   | BEFORE / AFTER             |
| 事件调度   | 是   | CREATE EVENT               |
| JSON       | 是   | JSON_EXTRACT / SET / REMOVE|
| 全文索引   | 是   | FULLTEXT + BM25            |
| 分区表     | 是   | HASH / RANGE / LIST        |
| 生成列     | 是   | GENERATED ALWAYS AS        |
| CHECK 约束 | 是   | -                          |
| 游标       | 是   | DECLARE / OPEN / FETCH     |
| 会话变量   | 是   | @@var / @var               |

与 MySQL 5.7 综合兼容度：92%。

### 4.2 数据类型

    INT / BIGINT / DECIMAL / FLOAT / DOUBLE
    VARCHAR(N) / CHAR(N) / TEXT / BLOB
    DATE / DATETIME / TIME / TIMESTAMP
    BOOLEAN
    JSON

### 4.3 索引

主键：

    CREATE TABLE users (
        id INT PRIMARY KEY,
        ...
    );

唯一索引：

    CREATE UNIQUE INDEX idx_email ON users(email);

普通索引：

    CREATE INDEX idx_age ON users(age);

复合索引：

    CREATE INDEX idx_city_age ON users(city, age);

全文索引：

    CREATE FULLTEXT INDEX idx_content ON articles(content);

### 4.4 事务

    BEGIN;
    UPDATE users SET age = 30 WHERE id = 1;
    INSERT INTO logs VALUES (1, 'update', NOW());
    COMMIT;

    -- 或回滚
    ROLLBACK;

隔离级别：

    SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;

支持的隔离级别：

- READ UNCOMMITTED
- READ COMMITTED（默认）
- REPEATABLE READ
- SERIALIZABLE

---

## 5. 过程语言（PL）

### 5.1 存储过程

创建：

    CREATE PROCEDURE add_user(IN p_id INT, IN p_name VARCHAR(64))
    BEGIN
        INSERT INTO users(id, name) VALUES(p_id, p_name);
    END;

调用：

    CALL add_user(100, 'Charlie');

带 OUT 参数：

    CREATE PROCEDURE count_users(OUT p_count INT)
    BEGIN
        SELECT COUNT(*) INTO p_count FROM users;
    END;

    CALL count_users(@cnt);
    SELECT @cnt;

### 5.2 流程控制

IF / ELSEIF / ELSE：

    IF score >= 90 THEN
        SET grade = 'A';
    ELSEIF score >= 80 THEN
        SET grade = 'B';
    ELSE
        SET grade = 'C';
    END IF;

WHILE：

    WHILE i < 10 DO
        SET i = i + 1;
    END WHILE;

### 5.3 变量

用户变量：

    SET @x = 100;
    SELECT @x;

会话变量：

    SELECT @@version;
    SET @@session.autocommit = 0;

### 5.4 游标

    CREATE PROCEDURE iterate_users()
    BEGIN
        DECLARE done INT DEFAULT 0;
        DECLARE v_id INT;
        DECLARE cur CURSOR FOR SELECT id FROM users;
        DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

        OPEN cur;
        read_loop: LOOP
            FETCH cur INTO v_id;
            IF done THEN
                LEAVE read_loop;
            END IF;
            -- 处理 v_id
        END LOOP;
        CLOSE cur;
    END;

### 5.5 触发器

    CREATE TRIGGER before_insert_user
    BEFORE INSERT ON users
    FOR EACH ROW
    BEGIN
        IF NEW.age < 0 THEN
            SET NEW.age = 0;
        END IF;
    END;

### 5.6 事件调度

    CREATE EVENT cleanup_old_logs
    ON SCHEDULE EVERY 1 DAY
    DO
        DELETE FROM logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);

---

## 6. JSON 与全文索引

### 6.1 JSON

存储：

    CREATE TABLE events (
        id INT PRIMARY KEY,
        data JSON
    );

    INSERT INTO events VALUES (
        1,
        '{"user": "alice", "action": "login"}'
    );

查询：

    SELECT JSON_EXTRACT(data, '$.user') FROM events;
    SELECT data->'$.user' FROM events;
    SELECT data->>'$.action' FROM events;

修改：

    UPDATE events
    SET data = JSON_SET(data, '$.status', 'success')
    WHERE id = 1;

其他函数：

    JSON_KEYS    -- 返回所有键
    JSON_LENGTH  -- 返回长度
    JSON_TYPE    -- 返回类型
    JSON_VALID   -- 是否合法
    JSON_OBJECT  -- 构造对象
    JSON_ARRAY   -- 构造数组

### 6.2 全文索引

创建：

    CREATE TABLE articles (
        id INT PRIMARY KEY,
        title VARCHAR(200),
        content TEXT,
        FULLTEXT INDEX ft_content (content)
    );

查询：

    -- 基本检索
    SELECT * FROM articles
    WHERE MATCH(content) AGAINST('数据库');

    -- 布尔模式
    SELECT * FROM articles
    WHERE MATCH(content) AGAINST('+数据库 -MySQL' IN BOOLEAN MODE);

**BM25 排序**：默认按相关度排序，无需指定。

**中文支持**：2-gram 分词。

---

## 7. 分区表

### 7.1 HASH 分区

    CREATE TABLE logs (
        id INT PRIMARY KEY,
        msg TEXT
    )
    PARTITION BY HASH(id) PARTITIONS 4;

### 7.2 RANGE 分区

    CREATE TABLE orders (
        id INT PRIMARY KEY,
        created_at DATE
    )
    PARTITION BY RANGE(YEAR(created_at)) (
        PARTITION p2023 VALUES LESS THAN (2024),
        PARTITION p2024 VALUES LESS THAN (2025),
        PARTITION p2025 VALUES LESS THAN (2026),
        PARTITION pmax VALUES LESS THAN MAXVALUE
    );

### 7.3 LIST 分区

    CREATE TABLE customers (
        id INT PRIMARY KEY,
        region VARCHAR(32)
    )
    PARTITION BY LIST(region) (
        PARTITION p_north VALUES IN ('beijing', 'tianjin'),
        PARTITION p_south VALUES IN ('shanghai', 'hangzhou'),
        PARTITION p_default VALUES IN (NULL, 'unknown')
    );

### 7.4 分区裁剪

查询时自动裁剪：

    -- 只扫描 p2025
    SELECT * FROM orders WHERE created_at BETWEEN '2025-01-01' AND '2025-12-31';

---

## 8. 常见问题

### Q1: v3.0 支持多核吗？

不支持。v3.0 是单进程架构，受 GIL 限制，多核利用率约 12%。如需多核请升级到 v4.0。

### Q2: 性能如何提升？

三个方向：

1. 加索引（最常见）
2. 加 LIMIT
3. 避免 `SELECT *`

参考：

    EXPLAIN SELECT ...;     -- 查看执行计划
    SHOW STATUS;            -- 查看状态
    SHOW STATS;             -- 查看表统计

### Q3: 慢查询怎么排查？

    -- 1. 查看慢查询数
    SHOW STATUS LIKE 'Slow_queries';

    -- 2. 分析
    EXPLAIN SELECT ...;

    -- 3. 用工具
    lightdb-check -h 127.0.0.1 -P 8888 -u root
    lightdb-index-advisor -h 127.0.0.1

### Q4: 支持哪些客户端？

任何 MySQL 5.7 兼容客户端，包括：

- mysql client
- Navicat / DBeaver / DataGrip
- PyMySQL / JDBC / GORM

### Q5: 数据能升级到 v4.0 吗？

能，且自动迁移。详见 `MIGRATION_v3_to_v4.md`。

---

## 附录：v3.0 新增函数

v3.0 相比 v2.0 新增了 150+ 个函数：

| 类别       | 数量    | 示例                              |
|------------|:-------:|-----------------------------------|
| 字符串函数 | 48      | CONCAT_WS、SUBSTRING_INDEX        |
| 日期函数   | 44      | DATE_FORMAT、TIMESTAMPDIFF        |
| 数学函数   | 32      | ROUND、POW、LOG                   |
| 控制函数   | 7       | IF、IFNULL、COALESCE              |
| JSON 函数  | 12      | JSON_EXTRACT、JSON_SET            |

---

## 附录：SQL 命令速查

    -- 库表
    SHOW DATABASES;
    SHOW TABLES;
    SHOW COLUMNS FROM users;
    DESC users;

    -- 事务
    BEGIN;
    COMMIT;
    ROLLBACK;

    -- 性能
    EXPLAIN SELECT ...;
    SHOW STATUS;
    SHOW STATS;

    -- 权限
    CREATE USER 'alice' IDENTIFIED BY 'password';
    GRANT SELECT ON *.* TO 'alice';
    SHOW GRANTS FOR 'alice';

---

反馈：https://github.com/lightdb/lightdb/issues

==== END FILE ====