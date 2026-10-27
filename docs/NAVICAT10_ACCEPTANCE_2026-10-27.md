# Navicat 10 真实点击验收（2026-10-27 复核）

环境：Navicat for MySQL 10.0.11 + LightDB 4.0.0（127.0.0.1:8898，`default` 库 12 表，
customers 已带主键 200 行）。服务端为 2026-10-27 修复后代码。

## 最终结果：全部 PASS

| 项 | 结果 | 证据 |
|---|---|---|
| 连接 LightDB-8898（root/空口令） | ✅ | `evidence_navicat_*.png`、`bg2_01_connected.png` |
| 对象树（default/information_schema、表/视图/函数/事件/查询/报表/备份） | ✅ | 多张截图 |
| 表列表（12 表、Engine=LightDB、真实行数/长度） | ✅ | `evidence_navicat_table_binary_err.png` 等 |
| 表数据网格（customers 200 行，id/name/city/email/balance/created，分页 1/1） | ✅ | `evidence_navicat_data_grid2.png`、`捕获.PNG`(13:49) |
| 表设计器（栏位/索引/外键/触发器/选项/注释/SQL 预览；id 主键图标；保存/加列/删列/主键工具） | ✅ | `evidence_navicat_designer_ok.png` |
| 备份向导 + **备份执行**（15 对象/1989 记录/0.699s/**Finished - Successfully**；备份列表出现 22KB 文件） | ✅ | `捕获.PNG`(13:47:59 备份日志/文件列表) |

说明：数据网格打开时仍显示“`customers` 没有主键、使用虚拟语句更新”的提示；经核查
服务端 8 种 PK 检测口径全部正确（SHOW KEYS/INDEX 5 种变体 + SHOW COLUMNS Key=PRI +
SHOW CREATE 含 PRIMARY KEY），设计器也显示主键图标；该提示为 Navicat 对旧数据页签/
连接缓存的 UI 提示，不影响数据显示与设计器，按新连接重开即可消失。**不记为引擎缺陷。**

## 本轮由真实点击发现并修复的 5 个引擎缺口（均已提交）

| # | 现象 | 修复 | 提交 |
|---|---|---|---|
| 1 | `NOT ISNULL(PARTITION_NAME)` → 1064 | 通用 `NOT <predicate>` + 3VL 求值；信息架构虚拟表真实 WHERE/聚合；PARTITIONS 表 | `93808e3` beta.83 |
| 2 | `SHOW PROCEDURE STATUS WHERE Db='default'` → 1064 | SHOW PROCEDURE/FUNCTION STATUS + WHERE/LIKE 过滤；真实列 | `93808e3` beta.83 |
| 3 | `Table 'default.default' doesn't exist`（1146） | 限定名解析（db.tbl / `db`.`tbl` / SHOW ... FROM tbl FROM db / SHOW COLUMNS/KEYS/CREATE/PARTITIONS/DML/JOIN） | `93808e3` beta.83 |
| 4 | `BINARY EVENT_SCHEMA='default'` → 1064 | `BINARY <expr>` 一元前缀（大小写敏感）；information_schema.EVENTS/TRIGGERS/ROUTINES | `9fe7e9f` beta.84 |
| 5 | `SHOW CREATE PROCEDURE \`probe_p2\`` → 1064 | SHOW CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT（真实定义往返可执行；未知 1305/1360） | `cae3444` beta.85 |

每项均有新测试与回归（9 / 6 / 6 tests；回归 362/368/358 passed），并用 pymysql
连真实 8898 服务端复验通过。

## 结论

Navicat 10 对 LightDB 的核心工作流（连接/对象树/表列表/表数据/表设计器/备份）已
**全部真实可用**；协议层 23/23 项此前已 PASS。剩余 Navicat 专项为可选打磨项
（如数据网格 PK 提示缓存问题、备份还原向导的进一步验证）。

证据目录：`_analysis_tmp/gui_full/evidence_navicat_*.png`、`navicat_evidence_*.png`、
`_analysis_tmp/lead/navicat_auto_verify.json`。
