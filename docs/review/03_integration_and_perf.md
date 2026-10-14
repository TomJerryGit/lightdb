# 03 图形管理软件 × 引擎 配合度评估（含 `web/` 前端层）

对象：`lightdb_studio/**`（PySide6 桌面 GUI）、`web/**`（FastAPI）、`lightdb/**`（引擎 v4.0.0）。
环境：Windows，Python 3.11.9，pymysql 2.2.8，PySide6 6.6.0；实测服务端 `python -m lightdb.main --mode single --host 127.0.0.1 --port 8899 --data-dir <tmp>`。
标注：【实测】本机跑出输出；【代码阅读】只读源码；【推断】由前两者推出；【未验证】沙箱阻断，附复现步骤。

## 0. 结论速览

**判断：有明显断层。** 读路径（`SELECT`/`SHOW TABLES`/`SHOW COLUMNS`/`INSERT`/`UPDATE`/`DELETE`/`EXPLAIN`/事务语句/`SET`）配合良好；
**DDL 与会话控制路径基本断裂**：`ALTER TABLE`、`SHOW CREATE TABLE`、`TRUNCATE`、`SHOW INDEX`、`SHOW PROCEDURES/TRIGGERS/EVENTS`、`DROP PROCEDURE/TRIGGER`、`CREATE INDEX`、`KILL` 全部不支持或空实现，而 GUI 的结构管理、快照/迁移、健康评分、性能监控**必须依赖**它们。
错误能到达客户端，但呈现层把它截断或丢弃。

## 1. GUI 如何连数据库

### 1.1 只有一条路：MySQL 协议客户端，没有进程内路径

```
grep -rn "Database\(|Executor\(|from lightdb|import lightdb|MySQLServer" lightdb_studio
→ 仅 2 处命中，均非引擎调用：lightdb_studio/__init__.py:61（注释）、plugins/example_plugin.py:10
```
【代码阅读】GUI 是 100% 的 MySQL 协议客户端；`Database`/`Executor` 只活在服务端进程（`lightdb/main.py:216-224`）与测试里。
**优势**：GUI 不持有存储生命周期，DB 崩溃/重启不带走 GUI；可连任意 MySQL 兼容端点；协议是唯一契约。
**风险**：GUI 能力上限被服务端 `parser`/`executor` 支持面硬性限制（第 2 节的后果）；多进程下元数据经快照中转（第 3.2 节），变成"隔离但陈旧"。
【实测】协议本身开销很小：`SELECT 1` 0.163→0.314 ms/次（×1.9）、点查 0.717→1.137 ms（×1.6）、200 行全表 20.9→26.6 ms（×1.3）（进程内 `Executor.execute_sql` vs pymysql）。
### 1.2 调用链（文件:行）

```
新建连接  MainWindow._new_connection                      ui/main_window.py:595-605
          DBConnection(info).connect()                    db/connection.py:25-49
            pymysql.connect(..., connect_timeout=10)      db/connection.py:33-44
            SELECT VERSION()                              db/connection.py:46 → net/server.py:254-258
          ObjectTree.add_connection → MetadataLoader.databases()   ui/main_window.py:611, ui/object_tree.py:81,97-104
            SHOW DATABASES                                db/metadata.py:9-13 → db/connection.py:125-127
执行 SQL  MainWindow._exec_current                        ui/main_window.py:657-663
          MainWindow._execute_sql                         ui/main_window.py:677-738
            _split_statements()                           ui/main_window.py:740-776
            读语句 conn.query(stmt)                        ui/main_window.py:707 → db/connection.py:84-94
            写语句 conn.execute(stmt)                      ui/main_window.py:711 → db/connection.py:96-107
            DDL 后 object_tree.refresh_all()               ui/main_window.py:717-719
── 网络 ──→
          MySQLSession._dispatch/_handle_query             net/server.py:207-236
            内建 SHOW/KILL/会话分支                        net/server.py:248-333
            parser.parse(sql)                              net/server.py:337-350 → sql/parser.py:2203
            Executor.execute(parsed, session)              net/server.py:357-363 → sql/executor.py:398
            _send_execution_result：error→ERR 包          net/server.py:399-...
```

## 2. GUI 依赖的 DB 接口 & 不匹配清单
### 2.1 GUI 实际调用的 API（全量）

`DBConnection`：`connect close is_connected cursor query execute execute_many switch_database get_databases get_tables get_columns`（`db/connection.py:25-146`）。
`MetadataLoader`：`databases tables columns table_row_count procedures functions triggers events partitions create_table_sql`（`db/metadata.py:9-77`）；SQL 生成 `db/table_ops.py`、`db/schema_diff.py`。
SQL 字面量统计（脚本扫描 `lightdb_studio/**`）：`SELECT COUNT(*)` 30、`SELECT` 24、`DESC` 23、`EXPLAIN` 14、`INSERT` 14、`CREATE TABLE` 10、**`SHOW CREATE` 10**、**`ALTER` 9**、**`TRUNCATE` 8**、`SHOW STATUS` 5、`CREATE INDEX` 3、`SHOW PROCESSLIST` 3、`ALTER TABLE` 2、`SHOW INDEX/PROCEDURES/TRIGGERS/EVENTS` 各 1。
### 2.2 不匹配清单（grep 逐项 + 实测）

引擎支持面 = `parser._parse_inner`(`parser.py:96-189`) + `parser._parse_ops_stmt`(`:215-342`) + `executor._dispatch`(`executor.py:467-475`，无 handler → `_err(1064,"不支持的语句")`) + `net/server.py:248-333` 内建分支。

| # | GUI 调用方（文件:行） | 引擎行为 | 实测输出 |
|---|---|---|---|
| 1 | `SHOW DATABASES` `db/connection.py:126` | 服务端**硬编码** `[("default",),("information_schema",)]` `net/server.py:290-294` | `['default','information_schema']` |
| 2 | `SHOW CREATE TABLE` `metadata.py:72`、`object_tree.py:355`、`snapshot.py:96/250`、`migration.py:207`、`shadow_table.py:96` | 无分支 → 1064 | `(1064, "... syntax near 'SHOW CREATE TABLE gui_t'")` |
| 3 | `ALTER TABLE ADD/MODIFY/DROP COLUMN` `schema_diff.py:61/73/77/86/91`、`ai_agent.py:446` | 无分支 → 1064 | `(1064, ... 'ALTER TABLE gui_t ADD COLUMN age INT'")` |
| 4 | `TRUNCATE TABLE` `object_tree.py:375`、`import_export.py:150`、`import_wizard.py:363` | 无分支 → 1064 | `(1064, ... 'TRUNCATE TABLE gui_t'")` |
| 5 | `SHOW INDEX FROM t` `health_score.py:142`、`sql_auto_optimizer.py:359` | 无分支 → 1064 | `(1064, ... 'SHOW INDEX FROM gui_t'")` |
| 6 | `SHOW PROCEDURES/TRIGGERS/EVENTS` `metadata.py:37/51/58` | 无分支（仅 `SHOW FUNCTIONS` 支持，`parser.py:329-331`）→ 1064 | 对象树对应文件夹恒为空 |
| 7 | `DROP PROCEDURE`/`DROP TRIGGER` `object_tree.py:390/406` | 无分支 → 1064 | `(1064, ...)` |
| 8 | `CREATE INDEX` `ai_agent.py:415` | 无分支（`server.py:424-429` 列了它但在 OK 分支，实际不可达） | 1064 |
| 9 | `KILL n` `perf_panel.py:219`、`perf_monitor.py:222`、`autonomy.py:303` | `net/server.py:701-726` 只校验 sid 存在即回 OK，**无取消/关闭动作** | 返回 OK；会话仍在 PROCESSLIST 且仍可执行 SQL |
| 10 | `SHOW COLUMNS` 的 `default` `connection.py:133-146` | `net/server.py:752` 写死 `default = None` | 建表带 `DEFAULT 'x@y'` → GUI 读到 `'default': None` |
| 11 | `SHOW COLUMNS` 的 `extra` | `net/server.py:753-754` 用 `col['primary']` 伪造 `auto_increment`；引擎**无自增概念**（`parser._column_def:1647-1696` 不解析 `AUTO_INCREMENT`） | 建表带 `AUTO_INCREMENT` 成功；不填 id 插入 → `(1062,'列 id 不允许为空')` |
| 12 | `SHOW COLUMNS` 的 `Type` | `parser._normalize_type:1698-1711` 用 `re.sub(r'\(.*?\)','')` **抹掉长度**，并把 INTEGER/SMALLINT→INT、TIMESTAMP→DATETIME、TINYTEXT→TEXT 归一化 | `VARCHAR(100)` → GUI 读到 `'VARCHAR'` |
| 13 | `CREATE TABLE ... ) ENGINE=InnoDB` `table_ops.py:86` | `parser._create_table:1536` 取 tail 只用于 PARTITION，engine 静默丢弃 | 返回 OK，无提示 |
| 14 | `switch_database('no_such_db')` `connection.py:118-123` | `net/server.py:217-220` 不校验库；`SHOW TABLES` 也不按库过滤（`:296-303` 直读 `self.db.tables`） | 切库"成功"，`SHOW TABLES` 仍返回全部表 |
| 15 | 登录即指定 database | `mysql_protocol.py:274-278` + `server.py:163-164` 解析错位 | `database='default'` → `SELECT DATABASE()` = `'mysql_native_password'`，PROCESSLIST 的 `db` 列同错 |

第 10/11/12 条使 **GUI 结构对比必然失真**：`schema_diff.column_def_from_meta`（`schema_diff.py:99-122`）按 `VARCHAR(100)` 解析长度、按 `extra` 判自增，遇到引擎的 `VARCHAR`/伪造 extra 就产生假"列已变更" → 生成 `ALTER` → 再撞第 3 条的 1064。

## 3. 元数据与会话状态一致性
### 3.1 元数据来源

GUI **不读任何内部对象**，全部走 SQL：`SHOW DATABASES`(`connection.py:126`)、`SHOW TABLES`(`:130`)、`SHOW COLUMNS FROM`(`:134`)；GUI 侧**无缓存**（`metadata.py` 每次现查），对象树只在 `add_connection`、节点展开、DDL 成功后重建（`object_tree.py:81-104/161-257`、`main_window.py:717-719`）。
### 3.2 多进程 5 秒陈旧：GUI 会显示陈旧数据

- Worker 持有 `RemoteDB`（`net/worker.py:213/236`），`SHOW TABLES`/`SHOW COLUMNS` 读它的 `tables` 投影（`net/server.py:296-303`、`:728-766`）。
- 该快照**每 5 秒才刷新一次**：`_start_snapshot_refresher(..., interval: float = 5.0)`（`net/worker.py:230/281-282`）；快照由 Master `_snapshot()` 提供（`net/master.py:211-229`），而 SQL 由 Master **实时**执行（`master.py:206-208`）。
- 【推断】Worker A 上 `CREATE TABLE` 立即在 Master 生效，但**同一连接**紧接着的 `SHOW TABLES` 读本 Worker 旧快照 → 最多 5 秒内看不到新表；GUI 在 DDL 后立刻 `refresh_all()`（`main_window.py:719`）会把这个"空"显示给用户。
- 单进程无此问题：`SHOW TABLES` 直读进程内 `Database.tables`；【实测】建表后同连接 `get_tables()` 立刻返回 `['gui_t']`。
- 【未验证】多进程无法在本沙箱启动：`multiprocessing.Queue()` 需要命名管道，`net/master.py:86-87` 抛 `PermissionError: [WinError 5] 拒绝访问`。
  机器上验证：`python -m lightdb.main --mode multi --workers 2 --host 127.0.0.1 --port 8898 --data-dir <tmp>`；开 6 条 pymysql 连接，一条 `CREATE TABLE mp_probe`，其余**立即**轮询 `SHOW TABLES`，看是否 ≤5s 收敛。
### 3.3 `SHOW PROCESSLIST` / `KILL` 在 GUI 可见，但不准确

GUI 入口：`ui/perf_panel.py:177-194`、`ui/perf_monitor.py:200-210`、`core/autonomy.py:290-305`。【实测】：

```
SHOW PROCESSLIST  -> ('3','root','127.0.0.1','mysql_native_password','Query','0','','SHOW PROCESSLIST')
空闲 3 秒后再查    -> Command='Query' Time='3' State='' Info='SHOW PROCESSLIST'   ← 从不回 Sleep
KILL 3 返回 OK，之后 -> 该会话仍在 PROCESSLIST，且“受害者连接仍可执行 SQL: (1,)”
KILL 999999        -> (1094, 'Unknown thread id: 999999')   ← 只有这一支是对的
```
原因（`net/server.py:678-699`）：`Command` 只在 `_update_session(command='Query',...)`(`:243`) 被设为 `Query`，**从不复位**；`Time` 用 `now - info['start']`(`:691`)，而 `start` 是**连接建立时刻**(`:181`) 即"连接年龄"；`Info` 永远保留最后一条 SQL。
后果：性能面板看着"有查询在跑"其实早已结束；`KILL` 按钮（`perf_panel.py:196-224`）无效却弹"已终止 N 个连接"——`conn.execute()` 不抛异常（`connection.py:96-107` 转成返回值），`:218-221` 的 `try/except` 是死代码，返回值 `(0, err)` 被丢弃。

## 4. 错误呈现：两条路径分别实测
### 4.1 协议层：错误没有被当成 OK（本会话修的缺陷已生效）【实测】

`net/server.py:399-410` 优先把执行器的 `{'type':'error'}` 翻成 ERR 包。错误码**原样到达**客户端：

```
SELECT * FROM no_such_table -> ProgrammingError: (1146, "表不存在: '表 no_such_table 不存在'")
ALTER TABLE gui_t ...       -> (1064, "You have an error in your SQL syntax near '...'")
INSERT 主键重复             -> (1062, '主键重复: 1')
INSERT NOT NULL 违反        -> (1062, '列 nn 不允许为空')
SELECT nope FROM err_t      -> (1146, "表不存在: '列 nope 不存在'")
INSERT 'abc' 到 INT 列      -> (1062, "无法将 'abc' 转换为 INT")
```
**但引擎自己的语义码大面积错误**（`executor.py:403-417`：`KeyError→_err(1146,..)`、`ValueError→_err(1062,..)`、`Exception→_err(1105,..)`）：NOT NULL 应为 1048/1364 却报 **1062**；类型转换应为 1366/1292 却报 1062；未知列应为 **1054** 却报 1146 且报文说"表不存在"；唯一键冲突报文写成"主键重复"。
另：`server.py:405-410` 把 SQLSTATE 一律写 `'HY000'`（`42S02`/`23000` 丢失）。
### 4.2 路径 A：桌面 GUI —— 错误到达了但被丢弃

`ResultTable.show_error(error)`（`ui/result_table.py:102-110`）**完全不使用入参**，只把标签设成"错误"。【实测】（离屏实例化真实 GUI，方法见 §6.1）：

```
传入 show_error 的文本 : (1062, "Duplicate entry '1' for key 'PRIMARY'")
info_label.text()      : '错误'     ← 原文不可见；time_label='' ；表格 0x0
```
唯一能看到错误的地方是状态栏，且被**截断到 80 字符**（`main_window.py:726-728`）【实测】：

```
SQL : ALTER TABLE gui_t ADD COLUMN zz INT
  status_label : '错误: (1064, "You have an error in your SQL syntax near \'ALTER TABLE gui_t ADD COLUMN '
  （near 后的语句片段被截掉）
```
反例：库同步对话框是唯一把错误列全的流程（`ui/db_compare.py:306-322`）。
### 4.3 路径 B：`web/` —— 呈现比桌面 GUI 完整【实测】

`POST /api/v1/query` 一律 HTTP 200，但 body 明确带 `ok:false` 与原文：

```
'ALTER TABLE gui_t ADD COLUMN w INT' -> {'ok': False, ..., 'error': "(1064, \"... syntax near 'ALTER TABLE gui_t ADD COLUMN w INT'\")"}
'SELECT * FROM no_such_table'        -> {'ok': False, ..., 'error': "(1146, \"表不存在: '表 no_such_table 不存在'\")"}
```
前端 `web/static/app.js:168-191` 把 `data.error` 渲染成红色错误块 → 与桌面版 `show_error` 丢文本直接对照。
### 4.4 "吞成空结果/成功"名单（部分）

- `db/metadata.py` 全部 10 个方法都是 `except Exception: return []/0/""`（`:12,18,24,32,39,46,53,60,67,75`）→ 不支持的 SHOW 在对象树里表现为"空文件夹"而不是错误。
- 统计口径：`lightdb_studio/**` 共 **192** 处裸 `except Exception:`，其中 **149** 处紧跟 `pass`/`continue`/`return []`（脚本统计）。
- 【实测·最严重】`core/snapshot.py`：`create_snapshot` 的 `SHOW CREATE TABLE`(`:96`) 报 1064 被 `:102 except: pass` 吞掉，快照文件**一个 CREATE TABLE 都没有**：`含 CREATE TABLE: False`、`含 INSERT: True`；`restore_snapshot` 在 903/904 条失败时仍返回 `{'ok': True, 'executed': 1, 'failed': 903, ...}`（`snapshot.py:186-190`）→ **备份/恢复实际不可用且不报警**。
- `core/health_score.py:137-144`：`SHOW INDEX` 的 1064 被 `except: continue` 吞掉 → `indexes` 恒空 → 对每张表都给出"表无任何索引"的错误结论。

## 5. `web/`（FastAPI）这一层的定位
### 5.1 模块清单与可用性

| 模块 | 行数 | 作用 | 状态 |
|---|---|---|---|
| `server.py` | 99 | `create_app()`/`run_server()`；挂 `api.router`、`/static`、`/` | 可用【实测】 |
| `api.py` | 443 | REST `/api/v1/*` | 可用【实测】 |
| `auth.py` | 99 | 自研 HMAC-JWT + 角色权限 | 可用【实测】 |
| `start.py` | 36 | `lightdb-studio-web` 入口（`pyproject.toml:66`） | 可用 |
| `metrics.py` | 30 | Prometheus `/metrics` | **死代码**：从未 `include_router`（`server.py:53-54` 只 include api）；实测 `/metrics`、`/api/v1/metrics/json` 均 404 |
| `static/*` | 88+174 | 单页管理界面（登录/对象树/查询/历史） | 可用 |

注册路由（实测）：`/api/v1/{health,login,me,connections,connections/test,databases,databases/{db}/tables,/{table}/columns,/{table}/data,/insert,/update,/delete,query,history,reports,reports/{name}/run}`、`/static`、`/`。
### 5.2 重叠、共用引擎、独立连接与鉴权

- **共用引擎**：`web/api.py:11-12` 直接复用 `lightdb_studio.db.connection.DBConnection` 与 `config.ConnectionInfo`，连库方式与 GUI **同一条代码路径**（PyMySQL → MySQL 协议服务端），并复用 `lightdb_studio.utils.history.QueryHistory`、`core.reports.ReportEngine`（`api.py:400/409`）。→ web **不是另一种接入，而是同一前端库的 Web 壳**，第 2 节的断层在 web 上同样成立。
- **重叠范围**：对象树、SQL 执行、表数据增删改、查询历史、报表与桌面版一一对应；桌面版另有 40+ 面板（快照/迁移/血缘/健康评分/数据质量/协作）web 没有。
- **独立连接方式**：`api.py:63-87` 自建 `_session_conns`，键是 `payload['sub']`（**用户名**，不是 token）→ 同一用户多个并发会话**共享一条 DBConnection**（互相阻塞/串味），且字典**永不回收**；实测键 `['admin:default','admin:e2e','dev:e2e','analyst:e2e']`。GUI 侧同样是"每连接配置一条长连接被所有面板共享 + `RLock` 串行"（`db/connection.py:22/86/102`），两者都无连接池。
- **独立鉴权**：`auth.py:11` 硬编码 `SECRET`，`:24-41` 自签 JWT（**未使用** `pyproject.toml:41` 声明的 `python-jose`）；用户表与**明文口令**硬编码在 `server.py:17-22`（`run_server` 还会打印出来，`:90-92`）；CORS `allow_origins=['*']` 且 `allow_credentials=True`（`:44-50`）。实测 `/health` 免鉴权 200，其余端点 401/403 正常，只读角色写操作 403。
- **死参数/桩**：`create_app(db=..., executor=...)` 写入 `app_state['db']`/`['executor']`（`server.py:28-35`），但全树**无任何读取点**（grep 仅 2 处写入），实测恒为 `None` → "进程内直连引擎"是预留未实现。
- **入口缺陷**：`pyproject.toml:67` 声明 `lightdb-studio-cli = "lightdb_studio.tools.cli:main"`，但 `lightdb_studio/tools` 不存在（实测 `Test-Path → False`、`ModuleNotFoundError: No module named 'lightdb_studio.tools'`）。

## 6. 端到端实测（服务端 + pymysql，走 GUI 自己的 DB 层）
### 6.1 方法

```powershell
python -m lightdb.main --mode single --host 127.0.0.1 --port 8899 --data-dir D:\lightdb_project\_tmp_review\data
python _tmp_review/e2e_gui_path.py     # 直接用 GUI 的 DBConnection/MetadataLoader/table_ops/schema_diff（不 import PySide6）
```
`~/.lightdb_studio` 在本沙箱**不可写**（实测 `PermissionError: [Errno 13] ...\.lightdb_studio\_probe.tmp`），故 GUI 全量启动改用**离屏直接实例化**：`QT_QPA_PLATFORM=offscreen` + `MainWindow(AppConfig())`，只把 `MainWindow.history.add` 换成 no-op（唯一被替换的是查询历史落盘），SQL 执行/结果渲染/错误呈现全为真实代码；实测 `MainWindow 构造: OK`。
### 6.2 典型 GUI 场景（建表→插入→查询→改结构→元数据→错误码）

```
[连接] SELECT VERSION() -> '5.7.30-LightDB'
1  SHOW DATABASES -> ['default','information_schema']                                  ★硬编码
   SHOW TABLES    -> []
2  建表 table_ops.build_create_table（含 ENGINE=InnoDB/AUTO_INCREMENT/UNIQUE/DEFAULT）
   -> affected=0 err=None                                                              ✅ 但 tail 被忽略
3  插入不带自增 id -> (1062, '列 id 不允许为空')                                        ★无自增
4  LIMIT 1 OFFSET 0 / OFFSET 1 -> 正常，列 ['id','name','email']                        ✅
   get_tables() -> ['gui_t']；table_row_count -> 0                                      ✅
   get_columns -> [{'name':'id','type':'INT','key':'PRI','default':None,'extra':'auto_increment'},
                   {'name':'email','type':'VARCHAR','key':'UNI','default':None,'extra':''}]
                                                                 ★长度丢失、DEFAULT 丢失、extra 伪造
   create_table_sql() 即 SHOW CREATE TABLE -> ''（1064 被 metadata.py:75 吞成空串）        ★
5  ALTER TABLE ADD/MODIFY COLUMN（SchemaDiffer.to_sql）-> (1064, ...)                    ★
   TRUNCATE TABLE -> (1064, ...)；SHOW INDEX FROM -> 1064；DROP TRIGGER/PROCEDURE -> 1064 ★
6  SELECT * FROM no_such_table -> (1146, "表不存在: '表 no_such_table 不存在'")           ✅ 码正确
7  SHOW PROCESSLIST 8 列正常；KILL 1 返回 OK；之后会话仍在 → KILL 无效                    ★
8  switch_database('no_such_db') 成功，SHOW TABLES 照旧返回全部表                        ★
9  SET autocommit=1 / BEGIN / COMMIT 均回 OK                                            ✅
```
### 6.3 离屏 GUI 中"用户可见"的结果

```
SELECT 1                    status='完成 (0.0 ms)'                 result='返回 1 行'   ✅
SELECT * FROM no_such_table status='错误: (1146, "表不存在: ...")'  result.info='错误' 表格 0x0  ★原文丢失
SHOW CREATE TABLE gui_t     status='错误: (1064, ...)'             result.info='错误'   ★
ALTER TABLE gui_t ADD ...   status='错误: (1064, ... ADD COLUMN '  ← 80 字符截断          ★
INSERT 主键重复              status="错误: (1062, '主键重复: 1')"    result.info='错误'   ★
对象浏览器：库列表 ['🗄 default','🗄 information_schema']；表文件夹 1 项；
            存储过程/触发器/事件 文件夹各 0 项（引擎 1064 被吞成空）；函数 0 项（确实没定义函数）
PerfPanel：processlist 8 列正常；_kill_selected 弹"已终止 1 个连接"，KILL 前 1 条/后 1 条  ★假成功
```

## 7. 性能配合

| 项 | 现状 | 证据 |
|---|---|---|
| 协议开销 | 小：`SELECT 1` ×1.9、点查 ×1.6、200 行全表 ×1.3（绝对值 +0.15～+5.7 ms） | 【实测】`perf_protocol.py`，进程内 `Executor.execute_sql` vs pymysql |
| 执行线程 | **无**工作线程，SQL 在 UI 线程同步执行 → 长查询/大结果集直接冻结窗口 | grep `QThread\|QRunnable\|QThreadPool\|moveToThread\|concurrent.futures` in `lightdb_studio/ui` → 0 命中 |
| 结果集获取 | `cur.fetchall()` 全量物化，无服务端游标/流式 | `db/connection.py:91` |
| 主查询路径分页/限流 | **无**：SQL 原样发送，不注入 LIMIT | `ui/main_window.py:684/707` |
| 结果渲染 | 逐格 `QTableWidgetItem` + `resizeColumnsToContents()`，无虚拟化 | `ui/result_table.py:54-89`；【实测】离屏 `show_result`：1000×10 **220 ms**、5000×10 **734 ms**、20000×10 **2448 ms**（全阻塞 UI 线程） |
| 死配置 | `Settings.query_timeout`/`max_result_rows`（默认 0/100000）只被设置对话框读写，查询路径从不读取 | `config.py:41-42`、`ui/settings_dialog.py:157-232`，grep 无其它引用 |
| 表数据编辑器分页 | 有 `LIMIT page_size OFFSET`（默认 200），但每翻页额外跑一次全表 `SELECT COUNT(*)` | `ui/data_editor.py:129-137` |
| 连接池 | **无**：`lightdb/performance/connection_pool.py` 仅被 `tests/test_batch10.py:43-48` 使用 | grep `ConnectionPool` → 只有 tests；`db/connection.py:22/86/102`；`core/scheduler.py:121-127`（后台线程也用同一条连接） |
| 超时 | 只有 `connect_timeout=10`；**无** read/query 超时，无取消按钮 | `db/connection.py:33-44` |
| 长查询取消 | 不可用：`KILL` 空实现（§3.3），GUI 也无客户端取消机制 | 【实测】；`net/server.py:701-726` |
| 服务端发送 | 一次性把整结果集写包（无流控） | `net/server.py:375-397 _send_resultset` |

## 8. 断层清单（现象 + 证据 + 影响 + 建议）

1. **`ALTER TABLE` 完全不支持**。现象：GUI 改结构/库结构同步/表设计器（编辑既有表）全部 1064。证据：`parser.py:96-189` 无分支、`executor._dispatch:467-475` 回 1064；【实测】两条 ALTER 均 1064；生成点 `db/schema_diff.py:55-93`、`ui/table_designer.py:206-225`（只能 CREATE）。影响：结构管理事实上不可用，只能 DROP+CREATE（丢数据）。建议：引擎补 `ADD/MODIFY/DROP COLUMN`、`ADD/DROP PRIMARY KEY`；补齐前 GUI 禁用并明确提示。
2. **`SHOW CREATE TABLE` 不支持且被吞成空**。现象：快照/备份/迁移/影子表产出**无 DDL** 的快照，恢复必然失败却返回 `ok: True`。证据：引擎无分支 → 1064；`core/snapshot.py:96-103` 裸 `except: pass`；`:186-190` 恒 `'ok': True`；【实测】快照 `含 CREATE TABLE: False`、`restore failed=903 且 ok=True`。影响：备份/恢复/迁移静默失效（最危险）。建议：实现 `SHOW CREATE TABLE`（`sql/logical_dump.py:630` 已有 DDL 生成逻辑可复用）；GUI 去掉裸 except，`ok` 应反映 `failed == 0`。
3. **`KILL` 空实现 + `SHOW PROCESSLIST` 陈旧**。现象：性能面板"终止连接"是假的；长查询无法取消；空闲连接永远显示在跑。证据：`net/server.py:701-726`（只查 sid）、`:678-699`（`Command` 不复位、`Time`=连接年龄 `:181/691`、`Info` 不清理）；【实测】KILL 后会话仍在且连接可用；`ui/perf_panel.py:213-223` 忽略返回值弹"已终止 1 个连接"。影响：失去唯一运维手段。建议：Master/Worker 加会话取消令牌，PROCESSLIST 维护 per-statement 状态，GUI 依据返回值报错。
4. **错误呈现被丢弃/截断**。现象：结果面板只显示"错误"二字；状态栏 80 字符截断；149/192 处裸 except 把 DB 错误吞成空结果或成功。证据：`ui/result_table.py:102-110`（实测原文不可见）、`ui/main_window.py:726-728`（实测报错被截半）、`db/metadata.py:12-75`、`core/health_score.py:143`、`core/snapshot.py:186-190`。影响：用户无法诊断任何 SQL 失败，"表无索引"之类结论是假的。建议：`show_error` 显示全文（可折叠/复制），元数据层区分"不支持/失败"与"空"。
5. **元数据与类型语义不可信**。现象：`SHOW DATABASES` 硬编码（`net/server.py:290-294`）；切库不校验且 `SHOW TABLES` 不按库过滤（`:217-220/296-303`）；`Default` 恒空（`:752`）、`Extra` 由主键伪造（`:753-754`）、`Type` 丢长度并归一化（`parser.py:1698-1711`）；登录指定 database 时 `SELECT DATABASE()`/PROCESSLIST.db 错（`mysql_protocol.py:274-278`）；多进程元数据最长陈旧 5 秒（`net/worker.py:281-282`）。影响：表结构显示、结构对比、库列表、健康评分、库间同步全部失真（必然产生假差异 → 生成 ALTER → 再撞断层 1）。建议：引擎补齐 DEFAULT/自增/长度元数据，`SHOW DATABASES`/切库走真实目录；GUI 不在字段缺失时臆断。
6. **连接/线程/超时模型不适合交互式工具**。现象：单条长连接被所有面板共享（`RLock` 串行）、SQL 在 UI 线程（0 处 QThread）、`fetchall` 全量、无 LIMIT 注入、无 read/query 超时、无结果虚拟化（20000×10 实测 2.45 s）。影响：一次 `SELECT *` 冻结整个 GUI 且无法取消。建议：查询移入工作线程 + 结果流式/分页 + 连接池 + 可取消（依赖断层 3）。

## 9. 未覆盖 / 沙箱限制与机器上复现

1. **多进程模式**：沙箱禁命名管道，`net/master.py:86-87` 抛 `PermissionError: [WinError 5]`，故 §3.2 的 5 秒陈旧**只有代码证据**。复现：`python -m lightdb.main --mode multi --workers 2 --host 127.0.0.1 --port 8898 --data-dir <tmp>` → 6 条 pymysql 连接 → 一条 `CREATE TABLE` → 其余立刻 `SHOW TABLES`。
2. **桌面 GUI 全量启动**：写 `~/.lightdb_studio` 被拒（实测 `PermissionError`），`history.add` 会让**每次查询**抛异常（`utils/history.py:37/54`），故只做离屏实例化 + 该单点 no-op。未覆盖：菜单/对话框交互、主题、`net/collab_server.py` websocket、AI 面板真实网络行为、拖拽与右键菜单。复现：正常桌面（HOME 可写）下 `python -m lightdb_studio.app`，按 §6.3 逐条观察。
3. **进行中长查询的取消**：三表交叉连接被引擎优化到 0.05 s（实测），造不出 >5 s 语句，故"取消"只有 KILL 语义级证据（如需强证据，用 10^8 行交叉连接或大表全量排序重跑 §3.3）。
4. **码 1819**：全树仅 `executor.py:5743-5765`（`CREATE USER` 密码策略不满足）使用，GUI 无调用点，故不在不匹配清单中。
