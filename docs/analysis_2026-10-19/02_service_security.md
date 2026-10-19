# LightDB 服务端 / 安全 / 复制 / 并发层分析（2026-10-19）

> 分析对象：`lightdb/net`、`lightdb/security`、`lightdb/replication`、`lightdb/concurrency`、`lightdb/cluster`、`lightdb/monitor`、`lightdb/stability`、`lightdb/performance`、`lightdb/parallel`（47 个 .py，11,939 行）。
> 方法：静态阅读 + AST 可达性分析 + **真实服务端 + pymysql 2.2.8 实测**（端口 18899，数据目录 `tmp_service_security/data`）+ 原始 MySQL 协议字节探针（自写客户端，验证 SQLSTATE / COM_* / 握手能力位）+ 主从复制真实进程内联调。
> 标注约定：**【实测】**＝本次运行取证；**【源码】**＝静态代码证据；**【文档】**＝历史审计/设计文档结论；**【推断】**＝有依据但本次未能运行时证实。
> 环境限制（如实记录）：① DSH 沙箱**禁多进程命名管道**，`multiprocessing.Queue()` 抛 `PermissionError [WinError 5]`，多进程模式本机无法启动（与 `docs/HANDOFF.md:139`、`docs/FAILURE_TRIAGE.md:15` 记录一致）；② 沙箱对 `docs/` 子树的 shell 写入被拒（`PermissionError`），运行时数据/脚本放在工作区根 `tmp_service_security/`，本报告通过文件工具落盘。

---

## 一、协议与运行时现实

### 1.1 启动与握手【实测】

`python -m lightdb.main --mode single --host 127.0.0.1 --port 18899 --data-dir tmp_service_security/data` 正常启动（4.0.0，约 1.4s），pymysql 2.2.8 可完成连接。原始字节探针读到的握手：

- protocol=10，server version=`5.7.30-LightDB`，auth plugin=`mysql_native_password`，auth_plugin_data_len=21，seed=20 字节（`net/mysql_protocol.py:160-181`）
- capability flags = **0x0BA20F**：LONG_PASSWORD | FOUND_ROWS | LONG_FLAG | CONNECT_WITH_DB | PROTOCOL_41 | TRANSACTIONS | SECURE_CONNECTION | MULTI_STATEMENTS | MULTI_RESULTS | PLUGIN_AUTH（`net/mysql_protocol.py:42-53`）
- **未公告** `CLIENT_SSL(0x800)`、`CLIENT_DEPRECATE_EOF(0x1000000)`、`CLIENT_COMPRESS`、`CLIENT_LOCAL_FILES`。

真实库名是 `default`（`lightdb/db/database.py:47` DEFAULT_DATABASE='default'）：`database='default'` 可登录，`database='lightdb'`/`'mysql'`/`'no_such_db'` → 1049。`SHOW DATABASES` 返回 2 行：`default`、`information_schema`。

### 1.2 COM_* 命令覆盖矩阵【实测 + 源码】

| 命令 | 支持 | 实测证据 | 源码 |
|---|---|---|---|
| COM_QUIT(0x01) / COM_PING(0x0e) | ✅ | `ping()` 返回正常 | `net/server.py:288-292` |
| COM_INIT_DB(0x02)（=USE） | ✅（校验库存在） | `select_db('no_such')` → 1049；`select_db('default')` OK | `net/server.py:294-305` |
| COM_QUERY(0x03) | ✅ 文本协议 | 全部 SQL 实测路径 | `net/server.py:307-309` |
| COM_FIELD_LIST(0x04) | ✅（真实列类型） | `t_probe` 返回 2 列定义 | `net/server.py:940-958` |
| COM_STMT_PREPARE(0x16)/EXECUTE(0x17)/CLOSE(0x19)/SEND_LONG_DATA(0x18)/RESET(0x1a)/FETCH(0x1c) | ❌ **全部不支持** | 原始协议逐个发包 → `1047/08S01 "Unknown command 0x16/0x17/…"` | 常量在 `net/mysql_protocol.py:61-63`，但 `net/server.py:284-317` 只分发上面 5 条 |
| COM_CHANGE_USER(0x11)/RESET_CONNECTION(0x1f)/STATISTICS(0x0a)/PROCESS_INFO(0x0b)/REFRESH(0x08)/SHUTDOWN(0x09) | ❌ | 逐个返回 1047/08S01 | 同上 |
| 未知命令 0x00/0xff | 1047/08S01 | 实测 | `net/server.py:315-317` |

**结论：服务端只实现 5 条命令，预处理（二进制协议）语句完全缺失。** 主流 JDBC/ODBC、mysql-connector-python 的 prepared 模式、部分 ORM（默认 server-side prepare）会直接失败；`docs/AUDIT_REPORT.md:161` 的「无预处理语句」结论**仍成立**（审计后未修）。另注意：本机 pymysql 2.2.8 的 `Connection.cursor(prepared=True)` API 不存在（`TypeError`），故客户端侧无法用 pymysql 验证，原始协议探针是唯一证据。

### 1.3 结果集、错误码与 SQLSTATE

- 结果集走**文本协议**：列数包 → 列定义（`catalog='def'`, schema='lightdb'）→ EOF → 行 → EOF（`net/server.py:490-512`、`net/mysql_protocol.py:207-243`）。因未公告 `DEPRECATE_EOF`，EOF 包符合协议。
- 列类型**按前 100 行实际 Python 值推断**（`net/server.py:638-687`）：首行 NULL → `MYSQL_TYPE_NULL`；int → LONG/LONGLONG；str → VAR_STRING（长度按最大字符×4）。`SELECT 1` 实测返回 LONGLONG。类型随数据变化，客户端 `DESCRIBE` 所得与查询结果所得可能不一致【源码+实测】。
- 执行错误一律翻译为 `errno=result['code']`，但 **SQLSTATE 硬编码 `'HY000'`**（`net/server.py:520-525`）。原始协议实测：

| SQL | errno | SQLSTATE | MySQL 期望 | 判定 |
|---|---|---|---|---|
| `SELECT * FROM no_such_table` | 1146 | **HY000** | 42S02 | 错误 |
| `SELECT no_such_col FROM t_probe` | 1054 | **HY000** | 42S22 | 错误 |
| `INSERT … 主键重复` | 1062 | **HY000** | 23000 | 错误 |
| `DROP TABLE no_such_table` | 1105（MySQL 同码，但消息为 "表不存在"） | HY000 | 42S02 | 部分 |
| `THIS IS NOT SQL` / 参数残缺 | 1064 | 42000 | 42000 | 正确 |
| 未知命令 | 1047 | 08S01 | HY000 | 轻微偏差 |
| 登录失败/未知库 | 1045 / 1049 | 28000 / 42000 | 同 | 正确 |

→ 依赖 SQLSTATE 的客户端（JDBC 的 SQLState 分支、ORM 唯一键冲突识别）会把「表不存在/主键冲突」当通用错误。**【实测】P2。**

### 1.4 会话状态与 SET 语句【实测】

`net/server.py:437-439` 把**任何** `SET …` 直接回 OK，不进入执行器：

| 语句 | 实测结果 |
|---|---|
| `SET NAMES latin1` | OK；随后 `SELECT @@character_set_client` 仍 `utf8mb4` |
| `SET autocommit=0` | OK；`SELECT @@autocommit` 仍 `'1'` |
| `SET SESSION sql_mode=""`、`SET @x=1` | OK，无效果 |
| `SET PASSWORD POLICY min_length=1` | OK 但未生效（server 层拦截，`lightdb/sql/executor.py:6414-6422` 注释自认「经 pymysql 不会到达这里」） |

这是「**结构对、语义假**」的典型：客户端以为会话参数已生效。此外服务端也不维护用户变量、连接字符集、autocommit 状态（`self.db.autocommit` 是全局 DB 属性，`net/server.py:701-702`）。

### 1.5 连接生命周期 / SHOW 命令矩阵

- 连接生命周期：`socketserver.ThreadingTCPServer` 每连接一线程，socket 超时 600s（`net/server.py:75`）；`_read_packet` **不校验包长上限**（`net/server.py:103-111`），`max_allowed_packet` 只在 SHOW VARIABLES 里写死 64MB（`net/server.py:705`），实际不限制 → 超大包可占内存【源码】。COM_QUIT 正常关闭；KILL 见下。
- `SHOW STATUS`：`Connections` = **当前会话数**（MySQL 是累计连接数），实测 120 连接后仍报 10；`Queries/Questions` = 本进程执行器累计；`Bytes_received/Bytes_sent` = **NULL**（诚实化处理，旧审计说写死 '0'，已修复）；`Threads_running` 按 Command=Query 真实统计；`Uptime` 真实。**部分修复，Connections/Queries 语义仍假**（`net/server.py:771-806`）。
- `SHOW PROCESSLIST`：实测空闲连接 `Command=Sleep, Time=NULL, Info=''`，执行中 `Query/0/…` —— 旧审计「Command 不复位、Time=连接年龄」**已修复**（`net/server.py:321-336, 808-838`）。
- `KILL`：实测真正关闭目标连接（受害者后续查询报 `2013 Lost connection`）；`KILL QUERY` 显式返回 1105 拒绝并说明本引擎无法中止单语句（**诚实**，`net/server.py:840-898`）。旧审计「KILL 只回 OK」**已过时**。
- 连接上限：`config.max_connections=100`、`max_conn_per_ip=20`（`lightdb/config.py:68,97`）**无任何代码读取**（`net/` 中无引用），实测同一 IP 连开 **120 条连接全部成功** → 无限连接 DoS 面【实测】。
- 支持的 SHOW：`SHOW DATABASES/TABLES/VARIABLES/STATUS/PROCESSLIST/COLUMNS/DESC/SHOW CREATE TABLE/MASTER STATUS/SLAVE STATUS/REPLICA STATUS/BINLOG EVENTS/BINARY LOG STATUS/INDEX/SHOW ENGINE INNODB STATUS/SHOW AUDIT LOG/SHOW IP WHITELIST/SHOW SECURITY`，以及 `CHECK TABLE`/`OPTIMIZE TABLE`。其中 `SHOW ENGINE INNODB STATUS` 由真实统计拼接（可信），但**暴露数据目录绝对路径**；`SHOW AUDIT LOG` 返回真实落盘审计（实测 102 条，见 §2）【实测】。
- **不支持**（均实测 1064）：`SHOW ENGINES`、`SHOW WARNINGS/ERRORS`、`SHOW GRANTS`、`SHOW FULL TABLES`、`SHOW COLLATION/CHARSET/TABLE STATUS/TRIGGERS/EVENTS/PROFILES`、`SHOW CREATE DATABASE`、`SHOW BINARY LOGS`（注意与受支持的 `SHOW BINARY LOG STATUS` 不同）、`SHOW AUDIT STATUS`、`SHOW SLAVE HOSTS`、`SHOW MASTER LOGS`。
- 多语句：公告了 `CLIENT_MULTI_STATEMENTS` 但 COM_QUERY 不拆句，`SELECT 1; SELECT 2` 实测返回 `1105 "列 '1; SELECT 2' 不存在"` → **协议谎报能力位**（`net/mysql_protocol.py:50` + `net/server.py:307-309`）【实测】。

### 1.6 协议兼容性小结

| 维度 | 状态 | 关键证据 |
|---|---|---|
| 握手/认证插件 | ✅ 真实 | 原始字节探针；pymysql 登录成功 |
| 文本查询/结果集/EOF | ✅ 基本正确 | 实测 |
| 预处理语句 | ❌ 完全不支持 | 全部 COM_STMT_* → 1047/08S01 |
| 多语句 | ❌ 公告但未实现 | 实测 1105 |
| 错误码 | ⚠️ 码部分正确 | errno 1146/1054/1062 有，1105 混用 |
| SQLSTATE | ❌ 执行错误全部 HY000 | `net/server.py:520-525` |
| 会话变量/SET | ❌ 全部吞掉 | `net/server.py:437-439` |
| SSL | ❌ 未公告、`have_ssl=DISABLED` | 实测 pymysql 报 2026 |
| 事务（wire 层） | ❌ BEGIN/COMMIT/ROLLBACK 失效 | 见 §4.3 |
| SHOW 命令面 | ⚠️ 约 40% 常用 SHOW 缺失 | 实测 1064 清单 |
| 连接限制/超时 | ❌ 限制不生效，包长不校验 | 实测 120 连接 |

`docs/AUDIT_REPORT.md:161` 的协议矩阵（无预处理/多语句不拆/无 SSL/SHOW STATUS 硬编码）**大部分仍成立**，仅 KILL、SHOW STATUS 的字节数与 Threads_running、PROCESSLIST 状态三处已修复。

---

## 二、认证 / 权限 / 安全

### 2.1 安全矩阵（全部为本轮实测或源码确证）

| # | 安全面 | 声称/文档 | 现状 | 可利用性 | 证据 |
|---|---|---|---|---|---|
| 1 | 认证 | mysql_native_password | **默认 root 空口令可登录**；空口令 hash 为 `''` | **P0：任何能连到端口者以 root 身份完全控制数据库** | 实测 `root/''` 登录成功；`db/database.py:36-42,211-216`；`mysql_protocol.py:299-324` |
| 2 | 主机/来源校验 | 用户带 `'user'@'host'` | **不校验 host**，也不校验 IP 白名单；登录只查 `db.users[user]` | 任意来源 IP 均可（默认监听 `0.0.0.0`，`config.py:66`） | `net/server.py:139-167` 无 host/ip 检查；实测 IP 白名单加入后仍可连 |
| 3 | 权限体系 | GRANT/REVOKE/细粒度 | **完全未接线**：`GRANT`/`REVOKE`/`SHOW GRANTS` 语法 1064；`PrivilegeManager` 仅 `security/__init__` 与 tests 引用，主流程无实例化 | 任意登录用户（实测无授权 svc_c）可 INSERT/UPDATE/DELETE/CREATE/DROP/SHOW PROCESSLIST/SHOW SECURITY；**能登录即全权** | 实测 1064；可达性分析：privileges 不可达；`security/privileges.py:186-193` |
| 4 | 用户管理 | CREATE/DROP USER | `CREATE USER … IDENTIFIED BY '…'` 可用；`ALTER USER` 报 1235 NotSupported；`IDENTIFIED BY ''` 被密码策略拒绝 | — | 实测 |
| 5 | 密码策略 | min_length=6/deny_weak | **部分接线**：单引号强口令校验生效（'1'/‘123456’/‘abc123’ → 1819）；但 `IDENTIFIED BY NULL`/省略 IDENTIFIED/双引号字面量都会**静默建出空口令账户** | 双引号写法 `IDENTIFIED BY "secret9"` → OK + 空口令可登录（MySQL 会当字符串字面量设密码）→ 静默安全降级 | 实测 svc_null/svc_nopwd/svc_dq 空口令登录成功；`sql/parser.py:419-428` 只认单引号 |
| 6 | 审计 | 记录全部 DCL/DDL/DML | **默认开启**，DML 级，JSONL 落 `<data_dir>/audit.log`；实测记录 46 条含 DCL/DDL/DML 与失败语句，**不含 SELECT**（级别语义）；但**不脱敏**：CREATE USER 明文口令落盘；且 `SHOW AUDIT LOG` 无权限校验，**任意登录用户可读到全部审计** | **P1：低权用户读审计 → 得到管理员建号明文口令 → 冒充其他账户**（实测 svc_c 读到 102 条，其中 18 条含 `IDENTIFIED BY`） | 实测；`sql/executor.py:131-345`；`security/audit.py:47` |
| 7 | 审计级别配置 | config.audit_level（默认 DDL） | **配置不生效**：`_AuditSink` 只读环境变量 `LIGHTDB_AUDIT_LEVEL`，默认 DML；`MySQLServer(cfg…)` 不传 cfg.audit_level（executor 注释自认） | 审计级别与配置文档不符 | 实测 `SHOW SECURITY.audit.level=DML`；`sql/executor.py:145-149,209-212,392-400` |
| 8 | SSL/TLS | ssl_enabled / SSLManager | **未实现**：不公告 CLIENT_SSL、`have_ssl=DISABLED`；`ssl_support.py` 死代码；config.ssl_enabled 无人读 | 明文传输口令/数据 | 实测 pymysql `ssl=` → `2026 SSL is required but the server doesn't support it` |
| 9 | IP 白名单 | ip_whitelist_enabled | **未接线**：`ADD IP WHITELIST '10.9.9.9'` 可写入内存并可 `SHOW` 出来，但 accept/握手不查它（从 127.0.0.1 继续可连） | 白名单形同虚设 | 实测；`sql/executor.py:415-425,6447-6463` |
| 10 | 连接限制/防爆破 | max_conn_per_ip=20 | **未接线**：`ConnectionLimiter` 死代码；实测单 IP 120 连接全成功；无失败计数/锁定 | 暴力破解、连接耗尽 | 实测；可达性：connection_limit 不可达 |
| 11 | 错误信息 | — | 不存在用户（无 `using password: YES`）与密码错（有）消息不同 → 用户枚举 oracle；`SHOW ENGINE INNODB STATUS` 泄露数据目录绝对路径；`SHOW PROCESSLIST` 任意用户可看他人 SQL | 信息收集 | 实测 |
| 12 | SQL 注入面 | 参数化 | 服务端文本协议无服务端参数化（pymysql 客户端侧转义）；执行路径为自研 parser→AST，未发现直接拼接执行；审计/慢日志原样回写 SQL（日志注入/伪造风险） | 低（未证实注入） | 源码阅读 |
| 13 | 安全配置项 | config 中 8 个安全字段 | `max_connections/max_conn_per_ip/ssl_enabled/ip_whitelist_enabled/audit_level/semi_sync_enabled/password_*` 中，仅 password_* 真正被读取（构造 PasswordPolicy）；其余在 `net/` 无消费者 | 配置项是摆设 | `lightdb/config.py:46-57,63-107` + grep |

### 2.2 关键结论

1. **认证形同虚设**（P0，已证明）：默认数据目录新建即只有 `root` 空口令账户（`db/database.py:211-216` + `hash_native_password('')==''`），`mysql_native_password` 对空 auth_response 且 stored_hash 为空直接放行（`net/mysql_protocol.py:310-312`）。配合默认 `host=0.0.0.0`（`config.py:66`）与无 IP/连接限制，**默认部署 = 无认证的远程数据库**。
2. **授权层不存在**（P1，已证明）：能与不能只取决于「是否登录」，登录后全权；`PrivilegeManager` 是整个 `lightdb/security` 包里最完整却完全没用上的模块。`docs/AUDIT_REPORT.md:163`「权限模型不生效，能登录者即全权」**仍成立**；`docs/DEAD_CODE.md:66,94`「security 整包未接线」**部分过时**（audit/password_policy 已部分接线，ip_whitelist 实例化但不生效），其余仍成立。
3. **审计已真实落盘但有三处失真**：① 级别由环境变量而非配置控制；② 明文口令入日志（`security/audit.py:47` 的『含 IDENTIFIED BY 明文』风险成为事实）；③ `SHOW AUDIT LOG` 无权限校验，任意登录用户（实测低权账户 svc_c）可读到全部审计，其中 18 条含 `IDENTIFIED BY '…'` 明文 → 凭据横向获取。`docs/review/06` 的「审计未脱敏」结论**仍成立**，且比旧结论更严重（可远程读）。
4. **密码策略本身可用**（强口令 1819、弱口令拒绝均实测），但存在「空口令合法通道」与「双引号静默降级」两个绕过口，且 `IDENTIFIED BY ''` 被拒与代码注释「空密码不参与强度校验」自相矛盾（`docs/FAILURE_TRIAGE.md:340` #16 **仍成立**）。
5. 文档夸大：`docs/ADMIN_GUIDE_v4.md:347-359`（GRANT/SHOW GRANTS）、`:381`（IP 白名单）、`:392`（SSL/TLS）、`:420`（审计配置）所描述能力均**不成立/部分不成立**。

---

## 三、复制

### 3.1 复制矩阵（静态 + 实测）

| 组件 | 声称 | 现状 | 证据 |
|---|---|---|---|
| binlog 格式 | "ROW 格式二进制日志" | **不是 MySQL 二进制格式**：`replication/binlog.py` 是 JSONL 文件；而 `SHOW BINLOG EVENTS`/`SHOW MASTER STATUS`/`__BINLOG_FETCH__` 的数据源是**查询时重新扫描各表当前 WAL** 动态生成的事件列表（每次调用重新编号） | `sql/executor.py:5689-5735,5747-5757`；`replication/binlog.py:39-142` |
| 主库写入 binlog | 每次 DML 写 binlog | `MasterReplicator` **无调用者**（可达性分析不可达）；`Database.binlog` 属性存在但只被 backup 使用 | reach 结果；`replication/master.py:5-37`；`db/database.py:1040-1051` |
| `SHOW MASTER STATUS` | File/Position | 恒 `binlog.000001`；Position=**当前可见事件数**，非持久位点；实测 **会回退**（ALTER 触发该表 WAL 退休后 14→12；另 4047→4041） | 实测；`sql/executor.py:5747-5757` |
| 事件完整性 | — | 实测写入 20000 行后，binlog 中该表事件=0（阈值 checkpoint 退休 WAL，`t_big.wal` 0 字节）→ **事件随 WAL 退休而消失** | 实测；`db/table.py:4268-4348` |
| 位点稳定性 | — | 事件按「表名排序后拼接」重新编号；前序表新增事件会把后面表的位点推高，但**已提交给从库的 watermark 之后不再回读** → 从库永远看不到这些新事件 | 实测：从库 Read_Pos=4046 后，主库 t_rep5 新 INSERT/UPDATE/DELETE 未同步 |
| 从库 IO/SQL 线程 | 真实拉取/重放 | 程序内构造 `SlaveReplicator` 可启动，io/sql 线程真实跑（Read_Pos/Relay_Pos/Exec_Pos 为真实值）；**但仅此一条通道** | 实测 |
| 初始同步 | — | 无快照/全量阶段；从库必须**手工预建同构表**；DDL 不进 binlog → 新表永远不复制 | 实测：主库 CREATE TABLE t_rep6+INSERT 后从库表清单仍只有 t_rep5 |
| 从库应用容错 | — | 缺表事件只记错误继续跑；`Last_SQL_Errno` 记录为 **0**（KeyError 无 errno）而 `Last_SQL_Error` 有文本 → 与 MySQL「0=无错误」语义冲突；实测 4044 条错误被隐藏在同一状态里 | 实测 `SHOW SLAVE STATUS` 输出 |
| `SHOW SLAVE STATUS` | 真实 | 57 列按 MySQL 5.7 顺序；未配置时诚实报 `No replication configured`/`No`/NULL（**旧审计『恒报未运行/值恒 0』已修复**）；已登记复制器时动态列为真实值（Master_Host/Port/Log_File/Read/Exec/Relay/Errors） | 实测（从库进程内渲染 + 主库未配置渲染）；`sql/executor.py:5777-5925`；`replication/slave.py:473-528` |
| `Seconds_Behind_Master` | 延迟 | 基本恒 NULL，仅「已追平=0」；事件无时间戳所以算不出延迟（诚实但运维不可用） | `replication/slave.py:440-459` |
| START/STOP SLAVE、CHANGE MASTER | 运维命令 | **语法不支持**（1064）；`SHOW BINARY LOGS` 也不支持；无 GTID | 实测 |
| 半同步 | `semi_sync_enabled` | **死代码**：`SemiSyncMaster/SemiSyncSlave` 无生产调用者；魔法语句 `__SEMI_SYNC_REGISTER__/__SEMI_SYNC_ACK__` 服务端未实现（1064） | reach；`replication/semi_sync.py:12-140`；`docs/FAILURE_TRIAGE.md:55` |
| 主从真实可用性 | "复制可用（真实位点）" | **实验性单向行级复制**：仅当从库已手工同构 schema 才能同步「按表名排序靠后」的表的历史/新事件；跨表事件丢失、位点回退、DDL 不复制、无运维命令、错误不停止。生产不可用 | 实测（probe5/probe6）|

### 3.2 复制判定

- 【实测】本仓的「复制」是**基于 WAL 当前窗口的服务端视图**，不是持久 binlog。证据链：`SHOW BINLOG EVENTS` 里出现了 4000 行 `t_wal2` 事件（WAL 未退休），而 20000 行 `t_big` 事件为 0（WAL 已 checkpoint 退休，`t_big.wal=0B`）；ALTER 一张表导致共享位点从 14→12。
- 【实测】`SlaveReplicator` 首次启动能同步已有行（初始 `[(1,'a'),(2,'b')]` 被正确重放），但随后主库 INSERT/UPDATE/DELETE 均未同步（从库内容不变），Read_Pos=4046、Exec_Pos=42、Last_SQL_Errno=0、`stats={'io_events':4046,'sql_events':2,'errors':4044}`——**「看似在跑，实际不生效」**。
- 【实测】DDL 不复制：主库建新表并写入，从库无该表，无明确 DDL 队列/快照机制。
- 【源码】`SHOW SLAVE STATUS` 的逐列注释非常讲究（不编造 GTID/server_uuid/master.info），这一部分是全仓「诚实化改造」做得最好的；但动态列的真实性无法弥补数据流本身不可靠。
- 【文档判定】
  - `docs/AUDIT_REPORT.md:162`「复制不通（db.binlog AttributeError / __BINLOG_FETCH__ 无命令 / semi_sync 无调用者）」→ **部分过时**：`db.binlog` 属性与 `__BINLOG_FETCH__` 已实现、程序内可部分同步；semi_sync 无调用者**仍成立**。整体结论应从「不通」改判为「半通但不可生产」。
  - `docs/review/00_SUMMARY.md:18`「复制（真实位点）可用」→ **仍夸大**：位点是真实计算值，但会回退、会丢事件，不能作为复制依据。
  - `docs/ARCHITECTURE_v4.md` §9（binlog/半同步）、`docs/ADMIN_GUIDE_v4.md:678`（半同步降级排查）→ **夸大/不实**（半同步从未接线）。

---

## 四、多进程与并发扩展性

### 4.1 多进程架构（源码事实）

- `main.py --mode auto`（默认）在 `cpu>1` 时选 **multi**（`lightdb/main.py:177-188`）→ `main_v4.run_multi` → `net/master.py::run_master`。
- Master **独占** `Database`/`Executor`；Worker **不打开存储**，通过 `exec_queue` 把 `(parsed, session)` 发回 Master，由 Master 的**单个执行服务线程** `_exec_loop` 执行（`net/master.py:157-209`）。→ **查询执行在 Master 内完全串行**，Worker 只摊薄协议解析/连接处理，并额外引入一次跨进程往返。
- Windows 下连接从 Master 交给 Worker：`socket.share(pid)` 字节入队 → Worker `socket.fromshare`（`net/worker.py:405-412`）；POSIX 直接传 socket（fork 语义）。
- 共享组件由 Master 创建：`SharedTxnCoordinator`、`StatsHub`、`SharedBufferPool`（`net/master.py:93-153`）；**Worker 只打开 StatsHub**（`net/worker.py:300-314`），txn/buffer 组件在 Worker 侧无任何使用者 → 所谓「共享内存突破 GIL」的共享事务/共享缓冲**实际不参与任何查询路径**。
- `net/master.py:211-229` 的元数据快照（用户表+表结构）由 Worker 每 5s 拉一次（`net/worker.py:281-297`）→ 多进程下新建用户/表最长 5s 后才可见；且 `RemoteDB`（`net/worker.py:136-159`）**没有实现 `database_names()`/`visible_table_names()`**，而 `net/server.py:225-258` 的 SHOW DATABASES/SHOW TABLES 依赖它们 → 【源码推断】多进程模式下 `SHOW DATABASES` 返回空、`SHOW TABLES` 报 1049。

### 4.2 本环境无法启动 multi（如实记录）

```
python -m lightdb.main --mode multi --workers 2 …
  File "lightdb/net/master.py", line 86, in _init_queues
    self.conn_queues = [mp.Queue() for _ in range(self.worker_count)]
PermissionError: [WinError 5] 拒绝访问。
```
`mp.Process` 的 spawn 同样在 `DuplicateHandle` 处 `WinError 5`。【实测】这是 **DSH 沙箱禁命名管道**的环境限制，与 `docs/HANDOFF.md:139`、`docs/FAILURE_TRIAGE.md:15`、`docs/review/03_integration_and_perf.md:88` 完全一致，**不是本轮代码回归**，也不能据此判定 multi 在普通 Windows 上不可用；`docs/AUDIT_REPORT.md:340-375`（§15「多进程接线后实测可用」）本次**无法复核**。

### 4.3 单进程并发实测（扩展性核心证据）

| 负载 | 串行 | 4 连接/线程并行 | 加速比 |
|---|---|---|---|
| 4 次 `SELECT COUNT(*),SUM(v) FROM t_big`（2 万行全表） | 22.159 s | 31.449 s | **0.70×** |
| 400 次 `SELECT 1` | 1.128 s | 1.942 s | **0.58×** |
| 20000 行单条多值 INSERT（参考） | 19.05 s（服务端日志） | — | — |

→ **并发不但不加速，反而更慢**（GIL + 执行器/存储层锁竞争 + 每连接线程开销）。`ThreadingTCPServer` 的线程只带来协议层并发，查询执行并无并行。【实测】旧审计 `docs/review/03` 与 `FAILURE_TRIAGE.md:331`（并行 0.56~0.89×）方向**仍成立**。

### 4.4 事务/并发正确性（严重）

- **wire 层事务失效（实测）**：`BEGIN` + INSERT（未 COMMIT）后，另一连接立即看到该行；`ROLLBACK` 后行仍在；`SET autocommit=0` 无效。根因【源码】：`net/server.py:458-461` 每条语句**新建 session dict**，而 `Executor._cmd_begin` 把事务句柄存进 `s['txn']`（`sql/executor.py:8068-8089`），下一条语句的 session 已是新字典 → COMMIT/ROLLBACK 取不到 txn，静默 no-op；`BEGIN` 创建的 txn 则永久泄漏。
- **MVCC active 泄漏（实测）**：`SHOW ENGINE INNODB STATUS` 读到的 MVCC 统计：BEGIN+ROLLBACK 后 active 24→25（回滚未释放）；20 条连接各 `BEGIN`+INSERT 后直接断开，active 24→45、next_txn_id 29→50。连接断开路径 `net/server.py:217-221` 只删除会话表项，**不回滚事务**。→ active 事务集合单调增长 → vacuum horizon 被永久钉住 → 旧版本/undo 只增不减（长时间运行的实例内存/空间持续膨胀）。【实测+源码】
- 结论：底层 MVCC/锁管理器虽已实现（另一成员分析），但**经服务端协议无法获得可用的显式事务**；对文档宣称的「事务与崩溃恢复可用」是重大反例（P1）。
- 连接级隔离：无会话级快照/隔离级别；`@@transaction_isolation` 恒 READ-COMMITTED 字符串【实测】。

### 4.5 共享内存 / 锁协调器 / lockfree 判定

| 组件 | 状态 | 证据 |
|---|---|---|
| `SharedMemoryManager/SharedBuffer/SharedCounter/SharedLock` | ✅ 进程内实测可用（mmap 文件 + 跨进程文件锁 `msvcrt.locking`）；跨进程可用性因沙箱禁多进程未能验证 | `concurrency/shared_memory.py:38-109,114-353`；实测 counter 1,2,2；SharedLock OK |
| `SharedTxnCoordinator` / `StatsHub` / `SharedBufferPool` | ✅ 创建/统计/销毁实测通过（如 StatsHub aggregate、BufferPool stats） | `cluster/*`；实测 |
| `LockCoordinator` | ❌ **未接线**（仅 `cluster/__init__` 引用），主流程无人使用 | 可达性分析 |
| Worker 侧 txn/buffer 共享 | ❌ Worker 只打开 StatsHub；txn_coord/buffer_pool 无消费者 | `net/worker.py:300-314`；`net/master.py:137-153` |
| `lockfree_mvcc.VersionChain/TableMVCC` | ❌ **「无锁」夸大**：`add_version/mark_deleted/prune` 全部用 `threading.Lock`（`concurrency/lockfree_mvcc.py:33,77,82,95`）；且生产代码零调用（仅 `concurrency/__init__`、tests）→ 死代码 | 源码 + 可达性 |
| `concurrency/atomic.py`、`concurrency/ipc.py` | ❌ 无主流程引用（ipc.py 143 行**无任何 import**） | 可达性 |
| `concurrency/rw_lock.py` | ⚠️ 仅被 `db/table_v3.py` 引用，而 `table_v3` 不被 `db/__init__`/`database.py` 使用 → 实际也是死路径 | 可达性 + grep |
| `concurrency/multiprocess.WorkerPool` | ✅ multi 模式使用（本沙箱不可运行）；崩溃自动重启、socket.share 交接代码完整 | `net/master.py:261-273`；`concurrency/multiprocess.py:37-338` |

### 4.6 扩展性判定

【实测+源码】单进程：查询执行单线程（GIL+锁），并发负扩展（0.58–0.70×）。多进程：**协议层并行、查询层仍由 Master 单线程串行**（`net/master.py:164-209`），收益是连接多核化与崩溃隔离，**不是查询加速**。`docs/ARCHITECTURE_v4.md:29` 的「多进程实现现状」声明与源码一致（属**已诚实化**），但 `docs/ARCHITECTURE_v4.md` 正文 §3「多进程 + 共享内存突破 GIL …接近线性扩展」与 `docs/PERFORMANCE_WHITEPAPER_v4.md:91/449`（8 核利用率 13%→95%）仍是**夸大**；`docs/review/00_SUMMARY.md:28`「多进程（连接层并行、查询仍单线程）」**仍成立**。`docs/USER_GUIDE_v4.md:201` 的说明（多进程收益不是查询加速）**仍成立**。

---

## 五、监控 / 稳定性 / 性能 / 并行模块真实性

### 5.1 AST 可达性结论（从 `main.py`/`main_v4.py`/`net/server.py`/`net/master.py`/`sql/executor.py` 出发）

| 目录 | 文件数 | 主流程可达 | 结论 |
|---|---:|---:|---|
| net | 6 | 5 | 仅 `binlog` 无关；全部接线 |
| security | 7 | 3 | audit/password_policy/ip_whitelist 部分接线；**privileges / ssl_support / connection_limit 死代码** |
| replication | 5 | 1 | 仅 `binlog`（`Database.binlog` 惰性属性）；slave/master/semi_sync 需外部程序构造 |
| concurrency | 7 | 2 | multiprocess/shared_memory（multi 模式）；其余死代码 |
| cluster | 5 | 3 | shared_buffer/stats_hub/txn_coordinator（multi 模式）；lock_coordinator 死 |
| **monitor** | 8 | **0** | **全部未接入服务端**（仅 `tools/check.py`、`tools/index_advisor.py` 离线 CLI 引用） |
| **stability** | 7 | **0** | **全部未接线**（仅 `stability/__init__` 自引用）；`crash_recovery_v3.py` 无任何 import |
| **performance** | 7 | **0** | **全部未接线**（仅 `performance/__init__` 自引用） |
| parallel | 9 | 6 | 仅 `Executor._parallel_count`（单表裸 COUNT）真实使用；scheduler/resource_group 仅 `__init__` |

### 5.2 逐项判定

| 模块 | 代表能力 | 判定 | 证据 |
|---|---|---|---|
| monitor/health_checker.py | 健康检查 | **未接线**（离线工具可用） | 仅 `tools/check.py:25` |
| monitor/metrics_v3.py | Prometheus 指标（含 `lightdb_semi_sync_avg_wait_ms`） | **未接线**；无 /metrics 暴露；半同步指标因功能未接线恒 0 | `monitor/metrics_v3.py:83,301-326` |
| monitor/slow_query_analyzer.py、slow_log_analyzer.py | 慢查询分析 | **未接线**；真正慢查询只是 executor 直接 `logger.slow`，阈值硬编码 0.5s，不读 `config.slow_query_threshold` | `sql/executor.py:525-536`；可达性 |
| monitor/index_advisor.py、lock_analyzer.py | 索引建议/锁分析 | **未接线**（index_advisor 有离线 CLI） | 可达性 |
| monitor/parallel_stats.py | 并行统计 | **纯死代码**（71 行，无任何 import） | 可达性 |
| stability/watchdog.py | watchdog | **未接线** | 可达性 |
| stability/memory_leak_detector.py、soak_test.py、fault_injector.py、consistency_checker.py | 泄漏/浸泡/故障注入/一致性 | **未接线**（只在 tests） | 可达性 |
| stability/crash_recovery_v3.py | 崩溃恢复 | **纯死代码**（无 import；实际恢复在 `db/table.py:recover_from_wal`） | 可达性 |
| performance/batch_inserter.py | 批量插入 | **未接线**；实测 2 万行单条 INSERT 19.05s（≈1050 rows/s） | 可达性+实测 |
| performance/connection_pool.py、query_cache.py | 连接池/查询缓存 | **未接线**；服务端无连接池、`SHOW VARIABLES` 中 query_cache OFF 与实际一致（诚实） | 可达性 |
| performance/parallel.py | `ParallelExecutor` | **未接线**，且与 `parallel/executor.py` 是**同功能两套实现** | 可达性 |
| performance/benchmark.py、index_only_scan.py | 基准/覆盖索引 | **未接线** | 可达性 |
| parallel/executor.py | 分片并行聚合 | **唯一接线处**：`Executor._parallel_count`（单表裸 COUNT，行数≥阈值才启用；`sql/executor.py:1271-1341`）；内部 `ThreadPoolExecutor` → GIL 下无加速；本轮 4 并发 COUNT 实测 0.70× | 源码+实测 |
| parallel/scheduler.py、resource_group.py | 并行调度/资源组 | **未接线**（仅 `__init__`） | 可达性 |
| parallel/adaptive.py、fragment.py、merge.py、work_stealing.py、table_view.py | 并行碎片/合并/工作窃取 | 被 `parallel/executor.py` 内部使用（可达），但整个并行路径只服务裸 COUNT | 可达性 |

**「指标假数据」要点**（区别于死代码）：`SHOW STATUS` 的 Connections/Queries 语义错（见 §1.5）；`SHOW ENGINE INNODB STATUS` 是真实统计拼接（可信）；metric 侧的 half-sync 指标对应功能从未接线；`SHOW VARIABLES` 大量值写死（如 `transaction_isolation` 恒 READ-COMMITTED、`sql_mode` 恒固定串、`max_connections` 151 与 config 无关），客户端据此调参会误导【实测】。

`docs/review/00_SUMMARY.md:27-30` 把「复制/审计/权限/并行/多进程」的可用性混在一起描述；其中「并行只覆盖单表裸 COUNT」**仍成立**，「权限/审计未接线」**部分过时**（审计已部分接线），「复制可用」**仍夸大**。

---

## 六、代码质量

### 6.1 量化（`tmp_service_security/quality.py` + `reach.py` 实测统计）

| 目录 | 文件 | 行数 | 代码行 | 注释行 | 文档串 | 裸 except | TODO |
|---|---:|---:|---:|---:|---:|---:|---:|
| net | 6 | 2,616 | 2,245 | 207 | 53 | 33 | 0 |
| security | 7 | 648 | 562 | 23 | 18 | 2 | 0 |
| replication | 5 | 884 | 765 | 40 | 29 | 10 | 0 |
| concurrency | 7 | 1,302 | 1,083 | 77 | 45 | 20 | 0 |
| cluster | 5 | 859 | 716 | 51 | 34 | 0 | 0 |
| monitor | 8 | 1,450 | 1,228 | 107 | 38 | 13 | 0 |
| stability | 7 | 1,840 | 1,551 | 112 | 45 | 18 | 0 |
| performance | 7 | 710 | 622 | 26 | 26 | 5 | 0 |
| parallel | 9 | 1,630 | 1,372 | 133 | 60 | 1 | 0 |
| **合计** | **61（含包内文件）** | **11,939** | **10,144** | **776** | **348** | **102** | **0** |

- 注释+文档串占非空行 **11.1%**；`net` 与 `parallel` 注释密度最高（大量「为什么修、旧实现错在哪」的审计式注释）。
- 目标目录中 **16/47 个文件（34%）** 能从主入口可达；monitor/stability/performance 为 **0%**。
- 完全无 import 的死文件：`concurrency/ipc.py`(143 行)、`monitor/parallel_stats.py`(71 行)、`stability/crash_recovery_v3.py`(74 行)。
- 重复实现（同一职责两套代码，维护漂移风险）：`ParallelExecutor`×2（`parallel/executor.py` 454 行 vs `performance/parallel.py` 140 行）；审计×2（`security/audit.py` vs `sql/executor.py:_AuditSink`）；binlog×2（`replication/binlog.py` JSONL vs `executor._binlog_events` WAL 扫描）；事务协调器×2（`cluster/txn_coordinator.py` 内 `TxnCoordinator` + `SharedTxnCoordinator`）；协议常量散落在 `mysql_protocol.py` 与 `server.py`。
- 设计层面的「诚实化」是真实投入：`SHOW SLAVE STATUS` 逐列来源注释、`SHOW STATUS` 的 NULL 而非假 0、KILL QUERY 显式拒绝、`_AuditSink` 批量落盘不丢记录、`safe_maybe_checkpoint` 等。这些说明历轮审计把「不许静默」写进了规范，但也造成**注释代替测试/文档**的倾向。
- 未修复的已知缺口被写进注释而非代码：`net/server.py` 把 `cfg.audit_level` 传不进去（`sql/executor.py:145-149`）；`SET PASSWORD POLICY` 经 pymysql 不可达（`sql/executor.py:6414-6422`）；`KILL QUERY` 不支持 —— 可信度尚可，但「注释承认」不等于「功能可用」。
- `except Exception:` 102 处：`net/master.py:169-171` 的 `except Exception: continue` 会静默丢弃执行请求【源码】；worker 用异常分支判断队列关闭（`net/worker.py:376-381`）；多处 `pass` 吞掉资源关闭/日志错误。

### 6.2 历史文档逐条核对（本轮范围内的旧结论）

| 旧结论（文档:行） | 现状 | 标注 |
|---|---|---|
| `AUDIT_REPORT.md:80` 认证恒失败（`mysql_protocol.py:293`） | **已过时**：pymysql 登录成功，空/非空口令分支都正确 | 【实测】 |
| `AUDIT_REPORT.md:130-149` 建表必失败、执行错误被伪装成功 | **已过时**：CREATE TABLE 成功；失败以 ERR 包返回（1146/1054/1062/1105） | 【实测】 |
| `AUDIT_REPORT.md:161` 无预处理语句 | **仍成立**（COM_STMT_* 全部 1047） | 【实测】 |
| 同上：公告 MULTI_STATEMENTS 却不拆 | **仍成立**（实测 1105） | 【实测】 |
| 同上：无 SSL | **仍成立**（have_ssl=DISABLED，pymysql 2026） | 【实测】 |
| 同上：KILL 只回 OK 不断连接 | **已过时**：KILL 真断连（受害者 2013）；KILL QUERY 显式拒绝 | 【实测】 |
| 同上：SHOW STATUS 数值硬编码 | **部分过时**：Bytes=NULL 诚实、Threads_running 真实、Uptime 真实；但 Connections/Queries 语义仍错 | 【实测】 |
| `AUDIT_REPORT.md:162` 复制不通（binlog 属性缺失/__BINLOG_FETCH__ 无命令/semi_sync 无调用者） | **部分过时**：属性/虚拟表已实现，程序内可部分同步；半同步无调用者**仍成立**；整体应改判「半通但不可生产」 | 【实测+源码】 |
| `AUDIT_REPORT.md:163` 安全空壳、net/sql 从不调用 | **部分过时**：审计默认开启并落盘、密码策略接入 CREATE USER、IP 白名单对象被实例化；**权限体系/SSL/连接限制仍空壳**，「能登录者即全权」**仍成立** | 【实测+源码】 |
| `AUDIT_REPORT.md:340-375` 多进程接线后实测可用；查询仍 Master 单线程 | 「接线」源码存在；本沙箱**无法复核**（WinError 5 环境限制）；「查询仍单线程」**仍成立**（源码+单进程实测负扩展一致） | 【源码+实测】 |
| `DEAD_CODE.md:66,94` security 整包未接线 | **部分过时**（3/7 文件部分接线） | 【源码】 |
| `review/03:68` KILL 空实现、PROCESSLIST 陈旧 | **已过时**（KILL 生效、Command 复位 Sleep、Time 仅执行中给秒） | 【实测】 |
| `review/03:82-90` 多进程元数据最长 5s 陈旧 | **仍成立**（`net/worker.py:281-297` 每 5s 刷新）；本沙箱不可运行验证 | 【源码】 |
| `review/03:237` SHOW DATABASES 硬编码 / SELECT DATABASE() 错值 / Default 恒空 / Extra 伪造 / 切库不校验 | **已过时**：实测 SHOW DATABASES 真实 2 库、DATABASE() 正确（None/default）、DESC 的 Default/Extra 来自真实列定义、USE 不存在报 1049 | 【实测】 |
| `review/00_SUMMARY.md:18` 「复制（真实位点）可用」「权限/审计未接线」 | 复制：**仍夸大**（位点会回退/事件会丢/DDL 不复制）；权限：**仍成立**；审计：**部分过时**（已默认开启落盘） | 【实测】 |
| `FAILURE_TRIAGE.md:339` #15 GRANT 未实现 | **仍成立**（1064） | 【实测】 |
| `FAILURE_TRIAGE.md:340` #16 `IDENTIFIED BY ''` 被误判弱口令、与文档矛盾 | **仍成立**（'' → 1819；NULL/省略 → 空口令账户可建） | 【实测】 |
| `FAILURE_TRIAGE.md:262` test_security_via_sql 弱密码断言是假阳性 | **已过时**：CREATE USER 已支持，弱密码 1819 是真实断言（但权限链仍卡 GRANT） | 【实测】 |
| `FAILURE_TRIAGE.md:236-267` `SHOW AUDIT LOG` 语法不支持（1064） | **已过时**：`SHOW AUDIT LOG` 现返回真实审计记录（实测 102 条）——但这带来新的越权读取问题（见 §2） | 【实测】 |
| `FAILURE_TRIAGE.md:15` 沙箱禁多进程原语 | **仍成立**（本次同样 WinError 5） | 【实测】 |
| `ADMIN_GUIDE_v4.md:347-359,381,392,420` GRANT/SHOW GRANTS、IP 白名单、SSL、审计配置 | **夸大/不实**：GRANT/SHOW GRANTS/SSL 未实现，IP 白名单不生效；审计文件真实存在但级别由环境变量而非配置控制 | 【实测】 |
| `ARCHITECTURE_v4.md` §3「多进程+共享内存突破 GIL、接近线性扩展」、§9 半同步 | **夸大**：共享 txn/buffer 未被 Worker 使用、查询串行；半同步死代码 | 【源码】 |
| `USER_GUIDE_v4.md:201` 多进程收益是连接/协议多核化而非查询加速 | **仍成立**（诚实描述） | 【源码】 |
| `analysis_2026-10-19/01_engine_core.md:198` 旧 P0（认证恒失败/建表必失败/错误假成功）均已修复 | **范围一致**（本报告 P0 是新的认证缺失：默认空口令，而非「认证恒失败」） | 【实测】 |

---

## 七、P0–P3 风险清单

### P0（已证明，默认部署即被利用）

1. **默认 root 空口令 + 无 host/IP 校验 + 默认监听 0.0.0.0 = 未授权完全控制数据库**
   - 实测：`pymysql.connect(user='root', password='')` 成功；任意口令/不存在用户均 1045，说明认证逻辑本身是好的，但默认账户无口令。
   - 源码：`db/database.py:36-42`（空口令 hash 为 `''`）、`:211-216`（内置 root）、`net/mysql_protocol.py:310-312`（空 hash + 空响应放行）、`net/server.py:139-167`（不校验 host）、`config.py:66`（host 默认 0.0.0.0）。
   - 影响：能连到 8888 端口者即可 DDL/DML/DCL/读全部数据（权限层未接线，见 P1-1），无审计阻断（审计只记录）。
   - 修复优先级建议：强制首次启动生成随机口令/仅监听回环 + 权限接线 + 连接限制（三者缺一仍有风险）。

### P1（可证明的高危语义/工程缺陷）

1. **权限体系完全未接线**：GRANT/REVOKE/SHOW GRANTS 语法 1064；`PrivilegeManager` 零生产调用；任意登录用户全权（实测 svc_c 无授权可 DDL/删表/看 PROCESSLIST/SHOW SECURITY）。`net/`+`sql/` 无任何 `check()` 调用点。
2. **显式事务经 wire 层失效并泄漏 MVCC 活跃事务**：BEGIN/COMMIT/ROLLBACK 因 session dict 每语句重建而 no-op；实测 ROLLBACK 不回滚、未提交行他连接可见；断连不回滚；MVCC active 单调增长（24→25→45）。
3. **复制不可用于生产**：binlog 是 WAL 当前窗口的动态视图，位点会回退（实测 14→12、4047→4041）、checkpoint 后事件消失（2 万行事件=0）、跨表事件因重编号被跳过（实测新 DML 未同步）、DDL 不复制、无 START SLAVE；半同步死代码。
4. **双引号密码静默降级为空口令账户**：`CREATE USER "u"@"%" IDENTIFIED BY "secret"` → 返回 OK，账户可空口令登录（实测），MySQL 语义下应为字符串字面量设置密码。
5. **审计明文记录口令且任意登录用户可读**：audit.log 中 `CREATE USER … IDENTIFIED BY "secret9"` 原文落盘（实测），且 `SHOW AUDIT LOG` 无权限校验——实测低权用户 svc_c 读回 102 条审计、18 条含明文口令 → 凭据泄露 + 横向冒充。
   证据：`sql/executor.py:131-345`（`_AuditSink.record` 不做脱敏/不做权限过滤）、`sql/executor.py` 的 SHOW AUDIT LOG 处理路径只调用 `self.audit.rows()`。
6. **预处理语句完全缺失**（COM_STMT_* 全 1047），主流客户端/ORM 二进制协议路径不可用；同时公告 MULTI_STATEMENTS 却不实现（协议谎报）。
7. **SET 语句全部被吞**（autocommit/names/sql_mode/sql_log_bin 等一律 OK 无效，实测），会话隔离/字符集协商是假的。
8. **配置项与实现脱节的安全假象**：`audit_level/ssl_enabled/ip_whitelist_enabled/max_connections/max_conn_per_ip/semi_sync_enabled` 均无实际消费者（仅 password_* 生效），运维按文档配置会得到零效果。

### P2（正确性/兼容性/可用性，已被证据支持）

1. SQLSTATE 统一 HY000：1146/1054/1062 的 SQLSTATE 错误（实测原始包），依赖 SQLSTATE 的客户端行为异常。
2. 连接无上限（实测 120 条全成功）、`_read_packet` 不校验包长、无失败登录锁定 → DoS/暴力破解面。
3. `SHOW STATUS.Connections/Queries` 语义与 MySQL 不同（当前会话数/进程内累计）；`SHOW VARIABLES` 大量写死值（max_connections=151、transaction_isolation、sql_mode）。
4. 多进程未实测但存在源码级缺陷：`RemoteDB` 缺 `database_names()/visible_table_names()` → multi 下 SHOW DATABASES/SHOW TABLES 异常（1049/空）；元数据 5s 陈旧；Worker 不使用共享 txn/buffer（架构宣称与实现不符）。
5. `SHOW SLAVE STATUS` 的 `Last_SQL_Errno=0` 与 `Last_SQL_Error` 非空并存、`Seconds_Behind_Master` 恒 NULL → 运维误判；复制错误不停止线程只计数。
6. 常用 SHOW 缺失（ENGINES/WARNINGS/ERRORS/GRANTS/TABLE STATUS/TRIGGERS/PROFILES/BINARY LOGS）；`SHOW CREATE DATABASE` 无。
7. monitor/stability/performance 整包（22 文件、4,000 行）未接线，`docs/ADMIN_GUIDE_v4.md` 却按「有监控/有 soak/watchdog/有指标」描述。
8. 慢查询阈值配置 `slow_query_threshold` 不生效（硬编码 0.5s）。
9. 并发负扩展（0.70×/0.58×）与并行/多进程文档的加速宣称不符（文档侧已部分诚实化，但正文仍有夸大）。

### P3（低危/信息/代码质量）

1. 用户枚举 oracle（不存在用户与错密码错误消息不同）；`SHOW ENGINE INNODB STATUS` 泄露数据目录路径；`SHOW PROCESSLIST` 任意用户可见他人 SQL 文本。
2. `KILL QUERY` 不支持（已诚实拒绝）；无法取消单条长查询。
3. 错误消息拼接内部异常文本（`str(e)[:200]`），可能泄露实现细节。
4. 两套 ParallelExecutor/两套审计/两套 binlog/两套事务协调器，维护漂移风险；`concurrency/ipc.py`、`monitor/parallel_stats.py`、`stability/crash_recovery_v3.py` 零引用死文件；`lockfree_mvcc` 名不副实（用 Lock 且无调用者）。
5. 102 处裸 `except Exception`，其中 `net/master.py:169-171` 会静默丢执行请求。
6. 审计只覆盖执行器层：登录失败、未知库、KILL、SHOW 等协议层操作不入审计（P3，取证完整性）。

---

## 附录 A：实测环境与复现命令

- Python 3.11.9（`D:\Program Files\Python311`），pymysql 2.2.8。
- 服务端：`python -m lightdb.main --mode single --host 127.0.0.1 --port 18899 --data-dir tmp_service_security/data`（一个后台作业）。
- 探针脚本（工作区根，因沙箱拒写 `docs/`）：
  - `tmp_service_security/probe1.py` → pymysql 层：认证/DDL/DML/错误码/预处理/多语句/SSL/事务/DCL/SHOW 矩阵/连接上限
  - `tmp_service_security/probe2_raw.py` → 原始协议：握手能力位、SQLSTATE、COM_STMT_*/COM_CHANGE_USER、COM_FIELD_LIST、未知命令
  - `tmp_service_security/probe3.py` → 认证/密码策略/权限/IP 白名单/审计/事务/并发/SLAVE STATUS
  - `tmp_service_security/probe4.py` → 并发计时、SHOW 矩阵、共享内存组件、多进程能力探测
  - `tmp_service_security/probe5.py` → binlog 位点回退/事件丢失、MVCC 泄漏、shm 组件
  - `tmp_service_security/probe6.py` → 主从复制最小闭环（预建表→初始同步→增量不同步→DDL 不复制→位点回退）
  - `tmp_service_security/reach.py` / `quality.py` → AST 可达性 / 代码质量统计
  - 结果快照：`tmp_service_security/probe*_result.txt`、`server.log`
- 多进程：`python -m lightdb.main --mode multi --workers 2 --host 127.0.0.1 --port 18898 --data-dir tmp_service_security/data_multi` → **WinError 5（沙箱禁命名管道）**，见 `tmp_service_security/server_multi.log`；非代码回归，属环境限制。

## 附录 B：本轮未能验证的项（如实声明）

1. 多进程模式真实运行行为（连接交接、会话/统计汇总、元数据 5s 陈旧、SHOW DATABASES 空表问题）——沙箱禁命名管道，仅源码推断。
2. Windows `socket.share/fromshare` 与 `msvcrt.locking` 跨进程语义（单进程可用）。
3. SSL/TLS、IP 白名单、连接限制的「修复后」行为（当前均未接线，无从验证）。
4. `SlowQueryAnalyzer` 等 monitor 的「真实数据质量」——模块未接线，仅能判定可调用性与数据来源，未在服务端路径上运行。
5. 复制在「从库已有完整同构 schema + 只按表名字典序追加数据」的极窄场景下的长期稳定性——本轮只验证到一个增量不同步的最小反例。
