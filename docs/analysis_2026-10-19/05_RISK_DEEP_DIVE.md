# 高危问题深挖：证据链、复现、修复方案与验收（2026-10-19）

> 本文是《[LightDB 项目全面分析报告](../COMPREHENSIVE_ANALYSIS_2026-10-19.md)》§1.3 / §10 所列
> 最高危问题的**逐项深挖**：每条给出「现象 → 复现命令 → 根因（代码位置）→ 影响 → 修复方案（含代码级
> 改动点）→ 验收用例 → 工作量」，并在文末给出修复顺序、依赖关系与回归风险。
> 标注：【实测】本次运行；【源码】静态代码证据；【文档】仅文档宣称；【推断】未直接验证。
> **本次未修改任何源码**；`_analysis_tmp/lead/verify_client.py`（引擎）、`_analysis_tmp/lead/web_verify.py`
> （Web）是 Lead 的独立复现脚本。

---

## 0. 结论速览

| 编号 | 问题 | 级别 | 是否 Lead 复测 | 修复工作量 |
|---|---|---|---|---|
| A | 默认部署无认证（root 空口令 + 0.0.0.0 + 无 host/IP/连接限制） | **P0** | ✅ 复测 | 0.5–1 天 |
| B | 经 MySQL 协议的事务失效（BEGIN/ROLLBACK no-op、断连泄漏 MVCC） | **P1**（数据一致性上等价 P0） | ✅ 复测 | 1–2 天 |
| C | 声明式类型约束完全不校验（INT 越界/VARCHAR 超长/非法 DATETIME 静默入库） | **P1** | ✅ 复测 | 1 天 |
| D | Web 版认证可伪造（硬编码 JWT SECRET）+ 默认口令 + SQL 注入 + 无连接管理 | **P0** | ✅ 复测（伪造/默认口令/无连接/CORS/metrics；注入为源码+前次实测） | 2–3 天 |
| E | 复制不可用于生产（位点回退/事件丢失/DDL 不复制） | **P1** | 前次实测（服务端） | 0.5 天（摘牌）/ 1–2 周（真 binlog） |
| F | DDL 假成功：VARCHAR 主键表不可写 + 触发器坏体锁表 | **P1** | 前次实测 | 0.5–1 天 |

> 修复顺序建议：**A → B → C → F → D → E**（先堵"未授权访问"，再修"数据一致性"，再修"数据正确性"，
> 然后 Web 安全面，最后复制；D 可与 A/B 并行，因为改动面在 `web/` 与引擎无关）。

---

## 0.1 修复状态（2026-10-19）

> 用户确认后依次实施：**A（默认认证/监听/连接限制）+ D（Web 安全四项与连接管理）**，
> 随后继续 **B（协议层事务）+ C（类型约束）**；F/E 随后跟进。

| 编号 | 状态 | 说明 |
|---|---|---|
| A 默认认证/监听/连接限制 | ✅ **已修复**（commit `15476dc`） | `lightdb/db/database.py`（随机口令 + 旧库迁移）、`lightdb/config.py`（host 默认回环 + 2 个新配置字段）、`lightdb/main.py`/`main_v4.py`（`--root-password`/`--allow-empty-password`）、`lightdb/net/master.py`/`worker.py`（host 兜底与配置透传）、`lightdb/net/server.py`（`verify_request/shutdown_request` 准入：总量/per-IP/白名单 + 失败登录延迟）、`tests/conftest.py`（显式降级 env）、新增 `tests/test_auth_defaults.py`（11 例）、`run_studio.*`（本地 demo 显式降级）、`docker/entrypoint*.sh`（随机口令生成/输出一次） |
| D Web 安全四项 + 连接管理 | ✅ **已修复**（commit `9bf0486`） | `web/auth.py`（SECRET 改为 env/持久化/随机 + alg/exp 校验）、`web/server.py`（删默认用户、用户来源 env/参数/自动随机、CORS 白名单、`/metrics` 挂载、连接文件）、`web/api.py`（`_ident` 标识符白名单覆盖 db/table/order_by/列名、`POST /connections`、`/connections/test` 仅 admin + 主机白名单、登录常量时间 + 延迟）、`web/start.py`（默认回环、`--connections-file`/`--admin-password`）、新增 `tests/test_web_security.py`（22 例） |
| B 协议事务失效 | ✅ **已修复**（commit `ec41025`） | `lightdb/net/server.py`：连接级 session（txn/autocommit/variables/user_vars）替代每语句临时 dict、断连 `db.abort` 回滚、`SET autocommit/NAMES/@var` 真实生效、OK/EOF 状态位、SQLSTATE 透传与 errno 映射；新增 `tests/test_wire_transaction.py`（9 例） |
| C 类型约束不校验 | ✅ **已修复**（commit `1c26527`） | 新增 `lightdb/db/typecheck.py`（1264/1406/1292/1366/1048/3140 带 errno+sqlstate）；`db/table.py` 的 `coerce` 委托校验、INSERT/UPDATE 补 PK/NOT NULL（用 `_OMITTED` 哨兵区分省略与显式 NULL）；`sql/executor.py` 错误分支优先取异常 errno/sqlstate；新增 `tests/test_type_constraints.py`（10 例） |
| E 复制不可用 | ✅ **已摘牌 + 状态语义修正**（commit `59783e7`） | README 新增「🔁 复制支持边界」（实验性/单向/位点会回退/checkpoint 丢事件/DDL 不复制/无 START SLAVE/不可用于生产）；`replication/slave.py` 新增 `_infer_errno_from_text`，保证「有 `Last_SQL_Error` ⇒ `Last_SQL_Errno` 非 0」；新增 `tests/test_replication_honesty.py`（6 例）。**未做真 binlog 重写**（需 1–2 周，见 §E 中期方案） |
| F DDL 假成功（VARCHAR PK / 触发器） | ✅ **已修复**（commit `59783e7`） | parser 定义期拒绝非整型主键（1064）；`sql/trigger.py` 新增 `validate_trigger_body`（PLParser + 运行期同套语句形态 + NEW/OLD 列存在性），CREATE 落库前校验、坏体 1064/1054 不写入；新增 `DROP TRIGGER [IF EXISTS]` 与 `SHOW TRIGGERS`；新增 `tests/test_ddl_fakesuccess.py`（6 例） |

**B/C 修复后 Lead 验收**：`tests/test_wire_transaction.py` + `tests/test_type_constraints.py` = **19 passed**；
B 专项回归 143 passed（另 8 个线协议文件 55 passed / 5 failed 均既有）；C 专项 234 passed、额外回归 114 passed。

**Lead 独立验收（A）**：
```
[PASS] empty password rejected: errno=1045 Access denied for user 'root'@'127.0.0.1' (using password: YES)
root.password exists: 22 chars
[PASS] login with root.password: (1,)
listening on: ['127.0.0.1']
[PASS] loopback-only bind
[PASS] wrong password rejected: errno=1045 ...
# --allow-empty-password 显式降级模式：空口令可连、root.password 不生成（符合预期）
```

**Lead 独立验收（D）**：旧硬编码 SECRET 伪造 token → 401；`admin/admin123` → 401；
配置用户登录 → 200；`order_by`/`table` 注入 → 400；`/connections/test` viewer → 403；
`POST /connections` admin → 200、viewer → 403；evil origin 不回显 ACAO；`/metrics` → 200；未配置连接 `/query` → 404。

**A/D 已知遗留（非本次 P0 范围，记录供后续）**：
1. 多进程模式的 **Worker 自 accept 循环未接准入检查**（限流/IP 白名单目前只在 `MySQLServer` 单进程路径生效；
   认证本身在握手阶段、多进程同样生效）。
2. Windows 上 `chmod 0600` 无实际效果，`root.password`/`web_secret`/`web_admin.txt` 的访问控制依赖目录 ACL。
3. `docker-compose.v4/multi` 直接以命令行启动服务（绕过 entrypoint），随机口令只落数据卷，需 `docker exec` 读取；
   生产应显式注入 `LIGHTDB_ROOT_PASSWORD` / `LIGHTDB_WEB_USERS`。
4. `root_password` 与 `allow_empty_password` 同时设置会被 `Config.validate()` 判为冲突（有意，避免歧义）。
5. Web 的 `/history` 仍是全局历史（条目无 owner 字段，按用户隔离需改 `lightdb_studio` 的历史存储格式，P2）。
6. Web 的 `web/*.py` 重写后行尾由 CRLF 变为 LF（内容等价；`git diff --ignore-cr-at-eol` 可看真实变更），
   属仓库风格不一致，非功能问题。

**回归基线（修复后）**：见本节末尾「附录：A/D 修复后的测试与基准结果」。

---

## A. 默认部署 = 无认证的远程数据库（P0）

### A.1 现象与复现【实测】

```powershell
python -m lightdb.main --mode single --host 127.0.0.1 --port 18896 --data-dir _analysis_tmp\lead\verify_data
python _analysis_tmp/lead/verify_client.py
```

实测输出（节选）：

```
[PASS] root/'' login + SELECT 1: connected with empty password
[PASS] wrong password rejected: errno=1045 Access denied for user 'root'@'127.0.0.1' (using password: YES)
[PASS] unknown user rejected: errno=1045 ...
```

即：**认证算法本身是好的（错密码被拒），问题是默认账户没有口令、且默认对外监听**。另有服务端子报告实测：
同 IP 开 120 条连接全部成功（`config.max_connections=100`、`max_conn_per_ip=20` 无人读取）；
`ADD IP WHITELIST` 可写入并可 `SHOW`，但连接路径从不校验。

### A.2 根因链（4 处，缺一不可）【源码】

| 环节 | 位置 | 事实 |
|---|---|---|
| ① 内置 root 用空口令 | `lightdb/db/database.py:36-42`（`hash_native_password('') → ''`）、`:211-216`（`users['root']={'auth_hash': hash_native_password('')}`） | 新数据目录第一次启动即创建空口令 root |
| ② 空口令放行逻辑 | `lightdb/net/mysql_protocol.py:299-324`（`len(auth_response)==0 → stored_hash in (b'', b'\x00'*20)`） | 语义正确、但与①组合成"无口令即通过" |
| ③ 默认监听全网卡 | `lightdb/config.py:66`（`host="0.0.0.0"`） | 不限于本机 |
| ④ 无 host/IP/连接限制 | `lightdb/net/server.py:139-167`（只查 `db.users[user]`，不校验 `client_address`）、`config.py:68,97` 的 `max_connections/max_conn_per_ip` **全仓无消费者** | 任意来源、任意连接数 |

> 旧审计"认证恒失败"已过时；当前风险反转为"默认无认证"。

### A.3 影响（威胁模型）

能访问 8888/8899 端口的任何人（同机其他用户、局域网、公网映射）= **root 全库读写 + DDL/DCL +
`SHOW PROCESSLIST`/`SHOW AUDIT LOG`**；且权限体系未接线（见 D 报告），登录即全权。无需任何凭据。

### A.4 修复方案（建议 3 层一起做）

1. **首次启动口令策略**（`lightdb/db/database.py`）：
   - 新建数据目录时生成 16 字节随机口令（`secrets.token_urlsafe(16)`），写入 `<data_dir>/root.password`
     （权限 0600；仅当文件不存在时创建），并在日志中**只打印一次**提示"初始 root 口令见 root.password"；
   - 提供环境变量/CLI `LIGHTDB_ROOT_PASSWORD` 覆盖；空口令只允许 `--allow-empty-password` 显式开启
     （默认拒绝）；
   - 兼容旧库：`_meta.json` 里已有 root 且 hash 为空时，启动日志给出**显著警告**（P0 未修）。
2. **默认监听改回环**（`lightdb/config.py:66` → `127.0.0.1`；`docker/config` 里容器场景显式 0.0.0.0 并文档说明）。
3. **接线连接限制**（`lightdb/net/server.py` 的 `do_handshake` 前 / `MySQLServer.serve_forever`）：
   - 在 `MySQLServer` 维护 `per_ip_active` 与全局计数，accept 时检查 `max_connections/max_conn_per_ip`，
     超限回 `1040 Too many connections` 后关闭；
   - 校验 `cfg.ip_whitelist_enabled/ip_whitelist`（当前是死配置）；
   - 失败登录计数 + 简单退避（`sleep(2**min(n,5))`），防爆破。

### A.5 验收用例（新 `tests/test_auth_defaults.py`，走真实服务端）

- 全新 data_dir 启动后：`root/''` 必须 **1045**（默认拒绝空口令）；
- `root/<随机口令>` 200；错误口令 1045；
- 默认配置 `cfg.host == '127.0.0.1'`；
- 从非白名单 IP（模拟本机第二个接口不可行时用 `ip_whitelist=[]` + `enabled=True` 构造）连接 → 1045；
- 连接数超 `max_conn_per_ip` → 第 N+1 条 1040；
- 回归：`python -m pytest tests/test_mysql_connect.py tests/test_metadata_honesty.py -q` 全绿。

**工作量**：0.5–1 天（含测试）。**兼容性**：会改变"默认空口令可连"的既有测试契约
（`tests/test_mysql_connect.py` 等处需同步改为显式口令/`--allow-empty-password`），属**预期**变更，
不是回归。

---

## B. 经 MySQL 协议的事务是假的（P1，数据一致性上等价 P0）

### B.1 现象与复现【实测】

```powershell
# 服务端同上；pymysql 探针（Lead 独立复测，输出原文）
[PASS] root/'' login + SELECT 1
[FAIL] wire BEGIN/INSERT/ROLLBACK then COUNT: count after rollback=1 (expect 0)
[FAIL] second connection sees uncommitted row?: other-conn count=2 (expect 0 if isolation works)
```

服务端子报告另有：
- `SET autocommit=0` 被 ACK 但无效果；
- 断连后未回滚，`SHOW ENGINE INNODB STATUS` 的 MVCC active 事务数 24→45（`BEGIN` 20 次后直接断连）；
- `SHOW PROCESSLIST` 里看不到事务状态汇总。

### B.2 根因【源码】

```python
# lightdb/net/server.py:458-464  —— 每条语句新建 session
session = {
    'user': self.user or 'root',
    'database': self.database,
}
result = self.executor.execute(parsed, session)
```

- `BEGIN` 在 `Executor._cmd_begin` 把 txn 句柄写进**这个临时 dict**（`sql/executor.py:8068-8089`）；
  语句结束 dict 丢弃 → 下一条 `COMMIT/ROLLBACK/INSERT` 在**新 dict** 里执行，取不到 txn，
  `COMMIT/ROLLBACK` 静默 no-op（`_cmd_commit/_cmd_rollback` 找不到 txn 时返回 OK）；
- `INSERT` 因没有显式 txn 走 autocommit（`_next_autocommit_tid`），所以立即对其他连接可见；
- 连接关闭路径 `server.py:217-221` 只删会话表项，**不 rollback** → MVCC active 泄漏 →
  vacuum horizon 被永久钉住（空间/内存只增不减）；
- `server.py:437-439` 对任何 `SET ...` 直接回 OK，`autocommit` 状态既不保存也不生效。

**为什么 711 个测试是绿的**：测试大多直接构造 `Executor.execute_sql()` 并**自己持有 session dict**
（进程内路径事务是真的），而真实客户端走 `net/server.py` 的 per-statement session 路径——
典型的「测试绿 / 真实客户端红」跨层缺口。

### B.3 修复方案

1. **连接级 session 对象**（`net/server.py`）：
   - `setup()` 里创建 `self.session = {'user': None, 'database': None, 'txn': None, 'user_vars': {}, ...}`；
   - `do_handshake` 成功后填 `user`；`COM_INIT_DB`/`USE` 后同步 `database`；
   - `_handle_query_inner` 改为复用 `self.session`（删除 458-461 的临时 dict 构造），
     所有内置查询/`_show_*`/`executor.execute` 统一传它。
2. **事务生命周期**：`_dispatch` 对 `BEGIN/COMMIT/ROLLBACK` 正常转发执行器（当前已转发），
   但要让执行器把 txn 存在传入的 session 里（已有），并保证异常路径也释放；
   在 `handle()` 的 `finally` 里：若 `self.session.get('txn')` 存在 → 调 `db.rollback(txn)` 或
   `executor.execute_sql('ROLLBACK')`（用同一 session），再 `_remove_session`。
3. **`SET` 真实生效**（`server.py:437-439`）：至少解析
   `SET autocommit=0|1`、`SET NAMES <charset>`、`SET SESSION ...`（保存到 `self.session`），
   `autocommit=0` 时让执行器把每条 DML 包在隐式事务里（执行器已有 `db.autocommit` 概念，
   需要按会话而非全局—当前 `self.db.autocommit` 是全局属性，应改为 session 级）；
   不支持的 SET 返回 1064 或 warning，不要静默 ACK。
4. **补齐 SQLSTATE**（`server.py:520-525` 目前硬编码 `HY000`）：
   错误字典里已有 `sqlstate`（如 `UniqueViolationError.sqlstate='23000'`），改为
   `result.get('sqlstate') or classify_sqlstate(code)` 映射 1146→42S02、1054→42S22、1062→23000、
   1048→23000、1064→42000、1049→42000…

### B.4 验收用例（新 `tests/test_wire_transaction.py`，起真实服务端 + pymysql）

- `BEGIN; INSERT; ROLLBACK;` → 本连接与**另一连接**均 0 行；
- `BEGIN; INSERT; COMMIT;` → 两连接均 1 行；
- `SET autocommit=0; INSERT; ROLLBACK;` → 0 行；`SET autocommit=1` 后恢复自动提交；
- 断连（未提交）→ 其他连接不可见且 `SHOW ENGINE INNODB STATUS` 的 active 不增长；
- `SET NAMES latin1` 后 `SELECT @@character_set_client` 反映变化（或显式报不支持）；
- 回归：现有 761 用例中进程内路径不受影响；`tests/test_txn_atomicity.py`、`test_mvcc_persistence.py` 全绿。

**工作量**：1–2 天（改动集中在 `net/server.py` + `sql/executor.py` 的 session 读取点，需逐处核对
`_cmd_*` 是否假设“session 是本次语句新建”的——这是主要风险；建议先加连接级 session 但内容复制策略，
再逐条收敛）。**回归风险**：中高（服务端所有语句路径），必须在改动前后各跑一次全量并对照失败集合。

---

## C. 声明式类型约束完全不校验（P1）

### C.1 现象与复现【实测】

```
CREATE TABLE ty_probe (id INT PRIMARY KEY, big INT, s VARCHAR(3), d DATETIME);
INSERT INTO ty_probe VALUES (1, 2147483648, 'abcdef', 'not-a-date');   -- OK
SELECT big, s, d FROM ty_probe;  -- (2147483648, 'abcdef', 'not-a-date')
```

引擎核心报告另测：`BIGINT` 收 `9223372036854775808`、`DECIMAL` 收 `1.5`/浮点串、`DATETIME` 收任意串，
全部静默；部分路径 `int('abc')` 才会抛 1105。

### C.2 根因【源码】

`lightdb/db/table.py:237-265`：

```python
def coerce(value, col_type):
    ...
    if t in ('INT', 'BIGINT'):  return int(value)
    if t in ('FLOAT','DOUBLE','DECIMAL'): return float(value)
    if t == 'BOOLEAN': ...
    return str(value)          # ← 其余类型一律当字符串
```

- 无范围校验（INT 32 位 / BIGINT 64 位 / TINYINT/SMALLINT）；
- 无长度校验（`VARCHAR(n)` 的 n 被解析进类型串但 `coerce` 只取基类型名）；
- 无格式校验（DATE/DATETIME/TIME）；
- 无 DECIMAL 精度/标度校验；
- `NOT NULL` 在写入路径的检查是另一处（本次协议实测 PK 列 NULL 被接受，见 E2E 报告），也需一并核对。

### C.3 修复方案

1. `coerce()` 增加按 `col_type` 的严格校验（建议新模块 `lightdb/db/typecheck.py`，保持 `coerce` 薄）：
   - 整数：TINYINT(-128..127 / 0..255)、SMALLINT、INT、BIGINT 范围 + `UNSIGNED`；
   - 字符：`VARCHAR(n)` 按**字符数**校验（MySQL 语义；不是字节数），`CHAR(n)` 补空格/校验；
   - 时间：`DATE/DATETIME/TIMESTAMP/TIME` 用 `datetime.strptime` 多格式尝试，失败报错；
   - `DECIMAL(p,s)`：用 `decimal.Decimal`，校验总位数/小数位；
   - 类型参数缺失/非法：定义期（CREATE TABLE）就报 1064，不要等到写入。
2. 错误码按 MySQL：`1264 Out of range value`、`1406 Data too long`、`1292 Incorrect datetime value`、
   `1366 Incorrect integer value`、`1048 Column cannot be null`。
3. 是否严格模式：MySQL 默认 STRICT_TRANS_TABLES 报错；可为兼容提供
   `sql_mode` 配置（非严格时截断 + 写入 `SHOW WARNINGS`）。**建议第一期先一律严格报错**，
   避免"截断后静默"再次制造错值。
4. 同步修 `NOT NULL` 校验（PK/NOT NULL 列收到 NULL → 1048）与 `DEFAULT` 的类型校验。

### C.4 验收用例（新 `tests/test_type_constraints.py`）

- INT 越界 +1/-1 → 1264（边界值 min/max 正常）；
- VARCHAR(3) 收 `'abcd'` → 1406；收 `'中文'`（2 字符，6 字节）→ 成功（按字符数）；
- DATETIME 收 `'2020-13-01'`/`'not-a-date'` → 1292；
- DECIMAL(5,2) 收 `1234.56`/`1.999` → 1264；
- PK/NOT NULL 收 NULL → 1048；
- 回归：现有 `test_ddl_p1.py`、`test_index_declarations.py`、`test_insert_atomicity.py` 全绿。

**工作量**：1 天（含矩阵测试）。**风险**：中——会打破"接受任意值"的既有测试契约（预期行为变更），
需同步更新。

---

## D. Web 版：伪造管理员 + 默认口令 + SQL 注入 + 无法查库（P0 ×4）

### D.1 Lead 独立复测（FastAPI TestClient，输出原文）【实测】

```
== 2) default credentials (admin/admin123) ==       login: 200 {'username': 'admin', 'role': 'admin'}
== 3) forged token using the hardcoded SECRET ==    forged /me: 200 {"username":"attacker","role":"admin"}
== 4) forged admin token -> /api/v1/query ==        query: 404 {"detail":"连接 'None' 不存在"}
== 5) no token ==                                   no token /me: 401
== 6) CORS wildcard + credentials (evil origin) ==  ACAO: https://evil.example.com | ACAC: true
== 7) /metrics ==                                   404
== 8) /connections/test（任意已认证角色） ==        200 {"ok":false,"error":"...Can't connect...127.0.0.1..."}
```

即：**任何人都能用源码里的 SECRET 离线签出 admin token**；默认口令可直接登录；CORS 反射任意来源；
SSRF 测试端点真实发起出站连接；Prometheus 路由未挂载。SQL 注入为源码证据（`web/api.py` 的 f-string
拼接）+ 前次报告实测，本次未重复构造。

### D.2 根因（文件:行）【源码】

| # | 问题 | 位置 | 事实 |
|---|---|---|---|
| 1 | JWT SECRET 硬编码、无 env 覆盖 | `web/auth.py:11` | `SECRET = "lightdb_studio_secret_2025_change_me"`；`create_token/decode_token` 都用它 |
| 2 | 默认用户口令硬编码 + 启动明文打印 | `web/server.py:17-22,90-93`；`web/start.py:11`；`server.py:73` | `admin/admin123` 等四人；`run_server` 打印全部口令；默认 `0.0.0.0` |
| 3 | SQL 注入 | `web/api.py:309-312`（table/order_by）、`:336/360-361/384`（INSERT/UPDATE/DELETE 列名）、`:187`（表名 COUNT） | 标识符直接 f-string 拼进反引号，`order_by` 甚至无任何过滤 |
| 4 | 无连接管理 → 不可查库 | `web/start.py:23`（`create_app()` 无参）、`web/server.py:15-25`（connections 默认 `[]`）、`web/api.py:66-87`（`_get_conn` 找不到即 404） | CLI 启动路径永远 404 |
| 5 | 其他 | `web/server.py:44-50`（CORS `*`+credentials）、`web/api.py:146-157`（SSRF）、`:396-402`（全局历史） | — |

### D.3 修复方案（按文件）

1. **`web/auth.py`**：
   - `SECRET = os.environ.get('LIGHTDB_WEB_SECRET') or <启动时随机生成并打印一次>`；
     若用随机值，重启后 token 失效（可接受，或持久化到 `<data_dir>/web_secret`，0600）；
   - 校验 header `alg == 'HS256'`；可选 `iss/aud`；`exp` 已校验（保留）。
2. **`web/server.py`**：
   - 删除硬编码用户表；改为从环境变量/配置文件/`--admin-password` 注入；
   - `run_server` 不再打印口令（只打印"初始口令见 xxx"）；
   - CORS：`allow_origins=os.environ.get('LIGHTDB_WEB_ORIGINS','').split(',')`，去掉 `'*'`；
   - 登录加常量时间比较（`hmac.compare_digest`）与失败退避。
3. **`web/api.py`**：
   - 新增标识符校验函数 `_ident(s)`：`^[A-Za-z_][A-Za-z0-9_]*$`（或允许 `$`），不合法 → 400；
   - `order_by` 只允许 `columns` 白名单里的列名 + `ASC|DESC` 枚举，拼前再 `_ident()`；
   - INSERT/UPDATE/DELETE 的列名先 `_ident()`；值继续走 `%s` 参数化（已是）；
   - `table/db` 路径参数 `_ident()`；`_get_conn` 返回前统一 `switch_database(db)` 已做；
   - **新增连接管理**：`POST /api/v1/connections`（写 `app_state['connections']` + 持久化到
     `~/.lightdb_studio/web_connections.json`，密码走环境变量或加密存储），前端加"新建连接"表单；
     或明确接受"只读展示 + 由 `create_app(connections=...)` 注入"，那就在 CLI 加
     `--connections-file` 参数（最小改动，让 Web 版真能查库）。
   - SSRF：`/connections/test` 改为只允许已配置连接或 host 白名单（不接任意 host:port）；
   - `/history` 按用户隔离（`payload['sub']` 过滤）。
4. **`web/start.py`**：默认 `--host 127.0.0.1`；新增 `--connections-file`。

### D.4 验收用例（新 `tests/test_web_security.py`，TestClient）

- 不带 token → 401；篡改签名 → 401；过期 → 401；
- **用源码内任意字符串伪造 token → 必须 401**（修复后 SECRET 不在源码，测试改为从环境注入）；
- `admin/admin123` 默认口令 → 401（改为必须由配置提供）；
- `order_by="id` DESC -- "`、`table="t` -- "` → 400（标识符校验）；
- 配置一个真实连接后 `POST /query {"sql":"SELECT 1"}` → 200（证明 Web 可查库）；
- CORS：`Origin: https://evil.example.com` → 响应不带 ACAO 或只允许白名单；
- `/metrics` 要么挂载 200，要么从文档删除。

**工作量**：2–3 天（含连接管理与前端小改）。**风险**：低（`web/` 独立于引擎主路径）。

---

## E. 复制不可用于生产（P1）

### E.1 现象【实测/源码】

- `SHOW BINLOG EVENTS`/`SHOW MASTER STATUS` 的数据源是 `Executor._binlog_events()`：
  **查询时重新扫描每张表的当前 WAL**（`sql/executor.py:5689-5725`），按"表名排序 + 表内 lsn"重新编号；
- 后果（前次实测）：ALTER 一张表导致共享位点 14→12（WAL 退休重编号）；20,000 行的表 checkpoint 后
  WAL 截断 → 该表事件归 0（复制事件消失）；跨表事件重编号使从库跳过新事件；DDL 不进事件流；
  无 `START SLAVE/STOP SLAVE/CHANGE MASTER` 语法；`MasterReplicator`/`SemiSync*` 无调用者（死代码）。

### E.2 判定与修复路线【推断】

- **短期（推荐先做）**：把"复制"在 README/文档中**摘牌**为 experimental（与 OLAP/列存相同口径），
  删除"复制可用（真实位点）"表述；`SHOW SLAVE STATUS` 保留（它本身已诚实），但增加
  `Last_SQL_Errno` 语义修正（缺表错误不得报 0）。工作量 0.5 天。
- **中期（若确实要复制）**：实现真正的 append-only binlog（独立文件、事件带 CRC 与单调位点、
  DDL 也写入、按事务成组、支持 `START SLAVE`/`CHANGE MASTER`），从库 IO 线程按位点拉取而非
  `__BINLOG_FETCH__` 动态视图；补"从库预建 schema / 全量快照"流程。工作量 1–2 周，且必须先完成 B（事务）
  与 WAL 改造（同源依赖）。

### E.3 验收

- 短期：文档一致性测试（grep 不到"复制可用/真实位点"等表述）；`SHOW SLAVE STATUS` 在缺表错误时
  `Last_SQL_Errno != 0`。
- 中期：主库 INSERT/UPDATE/DELETE/DDL 后，从库 100% 追平；checkpoint/ALTER 后位点不倒退；
  重启后位点持久。

---

## F. DDL 假成功：非整型主键表不可写 + 触发器锁表（P1）

### F.1 非整型主键（现象/根因/修复）

- 现象【实测】：`CREATE TABLE vp (name VARCHAR(20) PRIMARY KEY, n INT)` → OK；
  `INSERT INTO vp VALUES ('a',1)` → 永久 `1105 required argument is not an integer`；`SELECT` 空集。
- 根因【源码】：B+Tree 键用 `struct.pack_into('<III', ..., k, ...)`（`storage/btree.py:173,202`）
  以 uint32 存 key，主键为字符串时 `struct` 抛 TypeError；写入路径把它包成 1105。
- 修复（二选一）：
  - **A（推荐，低成本）**：`CREATE TABLE` 定义期校验主键/唯一键列类型必须是整数类，
    否则 1064/1235 显式拒绝（并提示"本引擎主键仅支持 INT/BIGINT"）；同时更新 README 的支持边界。
  - B（长期）：B+Tree 键编码改为「类型前缀 + 变长字节」，需同步改搜索/分裂/序列化与 `.btroot`/页格式（大改）。
- 验收：`VARCHAR PRIMARY KEY` → 定义期 1064；整数 PK 全量回归绿。

### F.2 触发器坏体锁表（现象/根因/修复）

- 现象【实测】：`CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET @x=1` → `Trigger 'trg' created`；
  之后该表**任何 INSERT** → 1105「触发器 trg 里的语句无法解析」；且 `DROP TRIGGER` 不可解析（1064），
  用户无法用 SQL 自救。
- 根因【源码】：`db/trigger.py:67-73` 的 `create()` 只存 body 不校验；`trigger.py:116-125`
  `_run_trigger()` 在**触发时**才 `PLParser(body).parse_program()`；解析失败在写入路径变成 1105；
  解析器没有 `DROP TRIGGER` 语法。
- 修复：
  1. `TriggerManager.create()` 内先 `PLParser(body).parse_program()`（或与运行期同一套校验），
     失败 → 返回 1064/1105 且**不落库**；
  2. 补 `DROP TRIGGER [IF EXISTS] name` 解析与执行（`sql/parser.py` + `executor.py` 的 `_cmd_drop_trigger`）；
  3. `CREATE TRIGGER` 也校验 `SET NEW.col` 引用的列存在、类型合法；
  4. 运行期解析失败时不再让表永久不可写：记错误并禁用该触发器（或事务级回滚 + 明确错误码）。
- 验收：坏体 CREATE → 非 0 错误且 `SHOW TRIGGERS` 为空；合法触发器可 `DROP TRIGGER` 后恢复写入。

**工作量**：F 合计 0.5–1 天。

---

## G. 修复顺序、依赖与回归风险

```
A 认证默认（独立）──┐
D Web（独立）───────┼──▶ 可并行（不同目录，风险低）
                    │
B 连接级 session ───┼──▶ 依赖：无（但改动面大，先做）
C 类型校验 ─────────┘   依赖：D 无关
F DDL 假成功 ──▶ 依赖：无（小）
E 复制摘牌 ──▶ 依赖：B/WAL 改造（若做真 binlog）
```

**回归基线（改动前后各跑一次）**：

```powershell
python -m pytest tests -q -p no:cacheprovider          # 当前基线 711 passed / 25 failed / 24 skipped / 1 xfail
python -m benchmarks.run --suite quick --gate          # 当前基线 PASS 5/5
python -m pytest tests/test_sql_gaps.py -q             # 未提交 WIP 的 15 条红线
```

**注意事项**：
1. **每项单独 commit**（当前工作区还有未提交的 BETWEEN/DISTINCT WIP，建议先单独提交它，再开始修复）；
2. 不要用 `git stash`（历史事故记录在 `docs/HANDOFF.md`）；需要干净对照用 `git worktree`；
3. 所有"修复"必须带**该缺陷的锁测试**（新增测试在修复前应失败、修复后通过）；
4. 会改变既有测试契约的项（A 空口令、C 类型严格、B SET 语义）需逐条声明旧→新，不要偷偷改期望；
5. 文档同步：A/B/C/F 修完后更新 README 的"当前实测状态"与支持边界；E 摘牌要同步 3 份文档。

---

## H. 验收清单（修完后应能通过的端到端检查）

```powershell
# 1) 无认证默认不再成立
python -c "import pymysql; pymysql.connect(host='127.0.0.1',port=8899,user='root',password='')"   # 期望 1045

# 2) 协议事务真实
#    BEGIN;INSERT;ROLLBACK 后两连接均 0 行；COMMIT 后均 1 行；断连后 active 不增长

# 3) 类型错误显式
#    INT 越界 1264；VARCHAR 超长 1406；非法 DATETIME 1292；NOT NULL 1048

# 4) Web
#    用旧 SECRET 伪造 token → 401；admin/admin123 → 401；order_by 注入 → 400；
#    配置连接后 POST /query {"sql":"SELECT 1"} → 200；/metrics 200 或被文档删除

# 5) DDL
#    VARCHAR PRIMARY KEY → 定义期 1064；坏体触发器 CREATE → 报错且表可写；DROP TRIGGER 可用

# 6) 回归
python -m pytest tests -q -p no:cacheprovider
python -m benchmarks.run --suite quick --gate
```

---

## I. A/D + B/C/F/E 修复后的实测结果（2026-10-19）

### I.1 全量测试（修复后，与修复前对照）

```powershell
python -m pytest tests -q -p no:cacheprovider
# → 24 failed, 746 passed, 23 skipped, 1 xfailed, 1 warning in 619.08s
```

| 指标 | 修复前基线 | 修复后 | 说明 |
|---|---:|---:|---|
| 用例总数 | 761 | **794** | +33（新增 `test_auth_defaults.py` 11 例 + `test_web_security.py` 22 例） |
| passed | 711 | **746** | +35（新增 33 + 2 条环境敏感既有用例本次通过） |
| failed | 25 | **24** | **没有一条由本次修复引入**（见下） |
| skipped | 24 | 23 | 环境/平台跳过（`test_ipc_latency` 等抖动） |
| xfailed | 1 | 1 | — |

**24 条失败与基线逐条对照**：25 条基线失败中，`test_ipc_latency` 与
`test_v4_batch1::test_diagnose_cli` 本次通过（均为环境/负载敏感），其余 23 条仍在；
新增的 1 条 `test_v4_batch1::test_end_to_end_diagnosis` 已在**干净 HEAD worktree**
（`_wt_head`，git 67d0cd2）上单独复跑 → **同样失败**（诊断 CLI 打印 emoji `❌` 在 GBK
控制台抛 `UnicodeEncodeError`），证明是既有环境问题、与本次修复无关。
修复过程中曾出现 2 条由新 WARN 日志污染子进程 stdout 引起的失败（`test_checkpoint_wal`、
`test_insert_atomicity`），已通过「env-only 降级不打 WARN、只有显式 CLI/config 才打」修正，
复跑全绿。

### I.2 性能门禁（修复后）

```powershell
python -m benchmarks.run --suite quick --gate
# → 门禁结果: PASS （5/5 项达标），测量墙钟约 20.6 s
```

| 指标 | 修复后实测 | 门禁阈值 | 判定 |
|---|---:|---:|---|
| 逐行 INSERT（Table API，2,000 行档） | **6,118.6 rows/s** | ≥ 1,000 | PASS |
| 多值批量 / 逐行（同窗成对） | **1.488×** | ≥ 1.2× | PASS |
| 点查（进程内，2,000 行档） | **1,012.2 q/s** | ≥ 600 | PASS |
| WAL 峰值（5,000 行，默认阈值） | **468.6 KB** | ≤ 32 MB | PASS |
| 崩溃恢复（硬杀子进程） | **0.431 s** | < 5 s | PASS |
| 崩溃后丢失已提交行 / 泄漏未提交行 | **0 / 0** | = 0 | PASS |

> 与修复前基线（5,972 rows/s / 1.605× / 1,814.7 q/s / 468.6 KB / 0.364 s）相比在同机漂移
> 范围内（本机吞吐波动已知达 ±60%，判据仍是 `benchmarks/README.md` 的同窗成对方法）。

### I.3 新增安全用例

- `tests/test_auth_defaults.py`：**11 passed**（连跑 5 次稳定）——新库空口令 1045、
  `root.password` 口令可登录、旧库自动迁移、`--allow-empty-password`/env 显式降级生效、
  默认 `host=127.0.0.1`、per-IP/总量 1040、白名单 1130、释放后可重连。
- `tests/test_web_security.py`：**22 passed**——旧硬编码 SECRET 伪造 token 401、
  默认口令 401、标识符注入 400、CORS 白名单、`/metrics` 挂载、`POST /connections`
  仅 admin、未配置连接 404 语义保持等。

### I.4 P1（B/C/F/E）修复后的实测结果

```powershell
python -m pytest tests -q -p no:cacheprovider
# → 24 failed, 777 passed, 23 skipped, 1 xfailed, 1 warning in 694.54s
python -m benchmarks.run --suite quick --gate      # → PASS 5/5
```

| 指标 | P0 后（A/D） | P1 后（B/C/F/E） | 说明 |
|---|---:|---:|---|
| 用例总数 | 794 | **825** | +31（B 9 + C 10 + F 6 + E 6） |
| passed | 746 | **777** | +31，新增用例全部通过 |
| failed | 24 | **24** | 与 P0 后**逐条相同**（集合对比：new-only=[]、gone=[]）→ 零回归 |
| skipped / xfailed | 23 / 1 | 23 / 1 | — |

**性能门禁（P1 后）**：PASS 5/5 —— 逐行 INSERT 3,928 rows/s（门槛 1,000；比 P0 后的 6,118
低，属类型校验开销 + 同机漂移，仍远高于门槛）、批量/逐行 1.512×（≥1.2）、点查 1,404 q/s
（≥600）、WAL 468.6 KB（≤32 MB）、崩溃恢复 0.543 s（<5 s）、丢/漏行 0。

**B 验收**（Lead 复跑）：`tests/test_wire_transaction.py` 9 例全绿；`BEGIN;INSERT;ROLLBACK`
后两连接 0 行、`COMMIT` 后 1 行、未提交互不可见、`SET autocommit=0/1` 生效、断连回滚、
`test_mysql_connect` 等 143 例回归通过。
**C 验收**：`tests/test_type_constraints.py` 10 例全绿；指定 234 例 + 额外 114 例回归通过。
**F 验收**：`tests/test_ddl_fakesuccess.py` 6 例全绿；batch7/8+ddl_p1 73 例、v4_sql_features
130 例回归通过；非整型 PK 定义期 1064、坏触发器定义期失败且表可写、`DROP TRIGGER` 可用。
**E 验收**：`tests/test_replication_honesty.py` 6 例全绿（`Last_SQL_Errno` 语义）；README
复制段已摘牌且文档门禁 `test_olap_not_wired.py` 全绿。

### I.5 修复后的残余风险（记录，未在本轮解决）

1. **BIGINT 主键仍受 uint32 限制**：F1 允许 `BIGINT` 作主键，但主键 B+Tree 键仍按 uint32
   落盘，取值 > 4,294,967,295 时 INSERT 仍会 1105（显式报错，非静默）；根治需改键编码。
2. **Python API 建表未加同级校验**：`db.create_table(cols=[...primary=True])` 不走 SQL 解析器，
   非整型主键不会被定义期拒绝（仅 SQL 路径生效）。
3. **旧数据目录中的坏触发器不会自动禁用**：升级前已落库的坏体触发器在首次触发时仍报错，
   需用新增的 `DROP TRIGGER` 自助清理（文档已说明）。
4. **复制仍为实验性**：本轮只做摘牌 + 状态语义，未做 append-only binlog（1–2 周工程）。
5. 多进程 Worker 自 accept 循环未接入准入检查（限流/白名单只在单进程路径生效；认证在
   握手阶段、多进程同样生效）。
6. Windows 上 `chmod 0600` 无实际效果；`root.password` 等文件依赖目录 ACL。
7. `docker-compose.v4/multi` 绕过 entrypoint 直接启动服务：随机口令只落数据卷，需
   `docker exec` 读取或显式注入环境变量。
8. Web 前端已去掉预填默认口令与旧测试账号提示；未配置 `LIGHTDB_WEB_USERS` 时需读取
   `web_admin.txt`。
9. B 的遗留：`executor._active_session` 仍为全局共享（并发嵌套子查询的会话串用风险，既有）；
   无事务 SELECT 的 read view 不主动释放属 executor/db 层既有泄漏；`SET PASSWORD POLICY`
   经协议仍 ACK 不生效（保持旧行为）。

---

## 附：本文证据脚本与来源

| 证据 | 位置 |
|---|---|
| 引擎 P0/P1 复测（空口令/事务/类型/索引） | `_analysis_tmp/lead/verify_client.py` |
| Web P0 复测（伪造 token/默认口令/CORS/metrics/SSRF） | `_analysis_tmp/lead/web_verify.py` |
| 引擎侧完整实测（功能矩阵/崩溃恢复/行布局） | [01_engine_core.md](01_engine_core.md) §3 + 附录 A |
| 服务端完整实测（协议/认证/审计/复制/并发） | [02_service_security.md](02_service_security.md) §1–§4 |
| Web/Studio 完整证据 | [03_studio_web.md](03_studio_web.md) §5–§6 |
| 端到端/打包/测试基线 | [04_verification.md](04_verification.md) §2–§5 |
