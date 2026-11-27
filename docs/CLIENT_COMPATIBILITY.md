# 客户端兼容矩阵

> 用真实客户端跑同一组冒烟 SQL（连接 / SELECT 1 / SHOW TABLES / DDL / DML /
> 事务回滚 / 清理），结果以 `scripts/client_matrix.py` 的 JSON 为准。

## 1. 当前矩阵（2026-11-27，Windows 10 本机）

| 客户端 | 版本 | 结果 | 说明 |
|---|---|---|---|
| **pymysql** | 1.1.x | ✅ 通过 | CI 全量测试长期覆盖 |
| **SQLAlchemy**（mysql+pymysql） | 2.1.9 | ✅ 通过 | Engine/Connection、DDL/DML、显式事务回滚 |
| **Navicat 10** | 10.x | ✅ 通过（人工点击验收） | 见 `docs/NAVICAT10_ACCEPTANCE_2026-10-27.md`：连接/对象树/表列表/表数据/设计器/备份执行 |
| **DBeaver** | — | ⚠️ 未安装/未验证 | 本机无 DBeaver；按 §3 清单人工核对 |
| **mysql CLI** | **5.0.51b** | ❌ 已知不兼容 | 认证包协商失败（`ERROR 2013 Lost connection ... reading authorization packet`）。5.0 为 EOL 客户端；建议使用 MySQL 5.6+ 客户端或 GUI 客户端 |
| **JDBC**（MySQL Connector/J） | — | ⚠️ 未验证 | 本机有 Java 8 但无 connector jar，未纳入矩阵 |

复跑：

```powershell
python scripts/client_matrix.py --json _analysis_tmp\client_matrix.json
# 输出 automated[]（pymysql/sqlalchemy/mysql-cli）与 gui_manual[]（Navicat/DBeaver 检测+清单）
```

测试：`tests/test_client_compatibility.py`（pymysql 必有；SQLAlchemy 未安装则
skip；mysql CLI 5.0 按已知不兼容 skip；CI 上 sqlalchemy 未装时自动 skip）。

## 2. 已知兼容性边界

- **MySQL 5.0 CLI 不兼容**：旧客户端认证包协商失败。Navicat 10（同时代 GUI）
  可用，但不代表任意 5.0 工具链都可用。
- SQLAlchemy 只验证了 `mysql+pymysql` 方言的基础能力；ORM 高级特性（复杂
  relationship 级联、方言特有类型、`create_all` 全量 DDL 等）未系统验证。
- 连接参数：`charset=utf8mb4` 可用；未实现全部 MySQL session 变量。
- 事务隔离级别：仅单机实现，未对标 MySQL 各隔离级别的语义全集。

## 3. GUI 客户端人工核对清单（DBeaver / Navicat）

1. 新建 MySQL 连接：主机 `127.0.0.1`、端口 `8898`（或你的实例端口）、用户
   `root`、口令（`root.password` 或启动参数指定）。
2. 对象树：能看到 `default` 库与 13 张表（或你的表数），双击可展开列/索引。
3. 执行 `SELECT 1;` 与 `SHOW TABLES;` 成功。
4. 打开表数据（分页 200 行）正常，滚动/排序不报错。
5. 用设计器建表/改表并提交，`SHOW CREATE TABLE` 与设计一致。
6. 断开/重连正常；SSH/SSL 连接按需验证（Studio 支持 SSH 隧道，本矩阵不覆盖）。

## 4. 建议

- 对外宣传写"**MySQL 协议子集**兼容；已用 pymysql/SQLAlchemy/Navicat 10 验证
  基础用法"，不要写"兼容 MySQL 客户端生态"。
- 新客户端接入前先跑 `scripts/client_matrix.py` + §3 清单，把结果补进本表。
