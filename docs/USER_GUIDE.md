# LightDB 用户指南

**适用版本**：v3.0 / v4.0
**更新日期**：2025-08-01

---

## 1. 连接数据库

LightDB 兼容 MySQL Wire Protocol v10，可直接用任意 MySQL 客户端连接。

| 参数 | 默认值 |
|------|--------|
| host | 127.0.0.1 |
| port | 8888 |
| user | root |
| password | （空） |
| charset | utf8mb4 |

**Python（PyMySQL）**

    import pymysql
    conn = pymysql.connect(host='127.0.0.1', port=8888,
                           user='root', password='', charset='utf8mb4')

**JDBC**

    jdbc:mysql://127.0.0.1:8888/default?useSSL=false

**Go**

    sql.Open("mysql", "root:@tcp(127.0.0.1:8888)/default")

---

## 2. 基础操作

**建表 / 插入 / 查询**

    CREATE TABLE users (
        id INT PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        created_at DATETIME DEFAULT NOW()
    );

    INSERT INTO users VALUES (1, 'Alice', NOW());

    SELECT * FROM users WHERE id = 1;

**事务**

    BEGIN;
    UPDATE users SET name = 'Bob' WHERE id = 1;
    COMMIT;   -- 或 ROLLBACK;

**索引**

    CREATE INDEX idx_name ON users(name);
    CREATE UNIQUE INDEX idx_email ON users(email);

---

## 3. 高级特性

**窗口函数**

    SELECT name, salary,
           ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn
    FROM employees;

**CTE**

    WITH top_users AS (
        SELECT id, name FROM users ORDER BY created_at DESC LIMIT 10
    )
    SELECT * FROM top_users;

**JSON**

    SELECT data->'$.user' FROM events WHERE id = 1;
    UPDATE events SET data = JSON_SET(data, '$.status', 'ok') WHERE id = 1;

**全文索引**

    CREATE FULLTEXT INDEX ft ON articles(content);
    SELECT * FROM articles WHERE MATCH(content) AGAINST('数据库');

**分区表**

    CREATE TABLE logs (id INT PRIMARY KEY, created_at DATE)
    PARTITION BY RANGE(YEAR(created_at)) (
        PARTITION p2024 VALUES LESS THAN (2025),
        PARTITION pmax  VALUES LESS THAN MAXVALUE
    );

---

## 4. v3.0 / v4.0 差异

| 维度 | v3.0 | v4.0 |
|------|:----:|:----:|
| 进程模型 | 单进程 | 多进程 |
| MySQL 5.7 兼容 | 92% | 95% |
| 单线程点查 | 6,500 QPS | 12,000 QPS |
| 8 核点查 | 8,200 QPS | 92,000 QPS |
| OLAP 扫描 | 不支持 | 3.5M rows/s |
| 列式存储 | 不支持 | 支持 |
| HTAP 混合表 | 不支持 | 支持 |

**v4.0 使用注意**：

- 会话变量 `SET @x = 1` 后**不要跨事务使用**（会话可能落到不同 Worker）
- 临时表仅在**当前 Worker** 有效
- 长事务建议 < 30 秒

---

## 5. 常见问题

**Q：v3.0 支持多核吗？**
A：不支持。受 GIL 限制，多核利用率约 12%。如需多核请升级 v4.0。

**Q：如何提升性能？**
A：优先加索引 → 增大 Buffer Pool → 优化 SQL（见管理员指南）。

**Q：支持哪些客户端？**
A：任何 MySQL 5.7 兼容客户端，包括 mysql client、Navicat、DBeaver、PyMySQL、JDBC、Go、Node。

**Q：v3.0 数据能升级到 v4.0 吗？**
A：能，自动迁移。详见 `MIGRATION_v3_to_v4.md`。

---

## 相关文档

| 需求 | 文档 |
|------|------|
| 部署 / 监控 / 备份 | `ADMIN_GUIDE_v4.md` |
| 架构 / 性能原理 | `ARCHITECTURE_v4.md` |
| 性能数据 / 调优 | `PERFORMANCE_WHITEPAPER_v4.md` |
| 从 MySQL 迁移 | `MIGRATION_GUIDE_v3.md` |
| v3 → v4 升级 | `MIGRATION_v3_to_v4.md` |
| REST API | `API_REFERENCE.md` |