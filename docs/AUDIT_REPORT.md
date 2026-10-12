# LightDB 项目审计报告

**审计时间**：2026-10
**审计对象**：`D:\lightdb_project`（lightdb 服务端 + lightdb_studio 桌面端 + web 版）
**审计方式**：静态代码审查（全量阅读 + AST 可达性分析）+ 只读运行验证（导入探测、原始 MySQL 协议直连、pytest 全量运行）
**是否改动源码**：否。本报告只描述现状，未修改 `lightdb/`、`lightdb_studio/`、`web/` 下任何文件。

---

## 1. 结论摘要

本仓库**不是一个可用的数据库**，而是一份由 AI 按 V1→V4 批次生成的**项目骨架**：文档所宣称的能力（12,000 QPS、Cython 12.5x、列存 41x、MySQL 兼容度 95%）在代码中基本找不到支撑；四个入口没有一个能在当前环境下完整跑起来；核心引擎存在**认证恒失败**、**建表必失败**、**所有执行错误被伪装成成功**三处一票否决缺陷。

生成方式有直接证据：[create_project.py](../create_project.py) 按 [lightdb_project.txt](../lightdb_project.txt) 的目录树批量 `touch()` 出空文件，随后由 AI 分批填充内容；测试文件名直接是 `test_v2_batch3.py` / `test_v4_batch5.py` 这类批次编号，[tests/test_batch3.py](../tests/test_batch3.py) 文件头写明「v2.0 Batch 3 测试 - AI生成」；根目录还残留 `_diag_auth.py`、`_smoke_db.py`、`_test_batch14.log` 等上一轮调试产物。

尽管如此，仍需公允地指出：**存储层的若干基础算法是真实实现**（4096 字节页、LRU 缓冲池、B+Tree 分裂、按 key 的条件变量与三色 DFS 死锁检测），并非用 `dict` 冒充。问题集中在「没有端到端跑通」「文档远超实现」「错误被静默吞掉」这三类。

---

## 2. 规模数据

| 范围 | .py 文件数 | 代码行数 | 体积 |
|---|---:|---:|---:|
| 全仓库 | 351 | **69,538** | 433 个文件 / 3,403 KB |
| `lightdb/`（服务端） | 160 | 25,445 | 923.4 KB |
| `lightdb_studio/`（桌面端） | 130 | 31,950 | 1,253 KB |
| `tests/` | 49 | 11,050 | 410.1 KB |
| `web/` | 6 | 578 | 20.5 KB |
| `lightdb_import/` | 2 | 189 | 6.4 KB |

其他事实：无 `.git` 目录；3 个 0 字节文件（`tests/test_batch1.py`、`tests/benchmark_v4_batch4.py`、`tests/benchmark_v4_batch5.py`）；全仓库**没有任何 `.pyd` / `.so` / `.c`**，即 Cython 扩展从未编译过。

---

## 3. 四个入口的实测状态

> **后续进展**：本节的 multi 与 `--check` 两项、桌面端启动问题均已在后续会话修复，多进程模式的接线与实测见 **§15**。

| 入口 | 实测结果 |
|---|---|
| `python -m lightdb.main`（默认 `auto` → 8 核下走 multi） | ❌ 日志输出「Worker 池启动失败」后退出，**8888 端口从未被 bind**，进程不会接受任何连接 |
| `python -m lightdb.main --check` | ❌ `UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'`，崩在 [`main.py:162`](../lightdb/main.py#L162) 的裸 emoji `print`（[`main.py:156`](../lightdb/main.py#L156) 同样） |
| `python -m lightdb.main --mode single` | ⚠️ **能监听、能发出标准握手包**。实测 TCP 8899 端口可连，收到 86 字节协议 v10 握手：server version `5.7.30-LightDB`、auth plugin `mysql_native_password`。**但没有任何客户端能登录成功**（见 §4.1） |
| 桌面端 `lightdb_studio.app` | ❌ `SyntaxError`，在 Python ≤ 3.11 上无法启动（见 §4.4） |
| Web 版 `python -m web.start` | ✅ `create_app()` 成功，`web/api.py` 的 17 个端点可用，JWT 校验真实有效（篡改签名被拒），静态前端确实调用 API。但其默认凭据与密钥存在严重问题（见 §9） |

多进程模式的失败是**三重独立阻断**，不只是编码问题：

1. Windows 下 [`concurrency/multiprocess.py:73`](../lightdb/concurrency/multiprocess.py#L73) 等 8 处裸 `print` 含 emoji（🚀✅⚠️🛑），在 cp936 控制台抛 `UnicodeEncodeError`，被 [`net/master.py:141`](../lightdb/net/master.py#L141) 捕获后判定「Worker 池启动失败」；而 master 的 ✓/✗ 输出经 logger，被 [`logger.py:153`](../lightdb/logger.py#L153) 的 `except: pass` 静默吞掉。
2. spawn 模式下 worker 目标为绑定方法，pickle 含 `RLock` 的对象会失败。
3. 即便修好前两项：`MasterServer.start()` 全程不调用 `create_listener` / `MasterListener`（后者全仓库无调用点），[`net/master.py:95-102`](../lightdb/net/master.py#L95-L102) 的 `shared_state` 里也没有 `conn_queue` 键，worker 读到 `None` 后立即退出 —— **始终无人监听端口**。

---

## 4. 三处一票否决缺陷

### 4.1 认证恒失败（`lightdb/net/mysql_protocol.py`）

```python
293:    if len(stored_hash) != 20:
294:        return False
295:    if len(auth_response) == 0:
296:        # 空密码
297:        return stored_hash == b'\x00' * 20 or stored_hash == b''
...
304:    return candidate == stored_hash      # candidate = SHA1(password)
```

两处独立错误：

- **[`:293`](../lightdb/net/mysql_protocol.py#L293) 的长度守卫排在空密码分支之前**。而 root 的存储哈希就是空串（[`db/database.py:42`](../lightdb/db/database.py#L42) 用 `hash_password('')` 得到 `b''`），长度恒为 0，因此**空密码必然被拒**。
- **[`:304`](../lightdb/net/mysql_protocol.py#L304) 少做一次 SHA1**。标准 `mysql_native_password` 校验应为 `SHA1(resp XOR SHA1(seed+stored)) == stored`；这里把 `candidate`（即 `SHA1(password)`）直接与 `stored`（即 `SHA1(SHA1(password))`）比较，**非空密码同样必然失败**。

实测（进程内直接调用该函数，未启动服务器）：

```
password='secret123'  stored_len=20  resp_len=20  -> 服务端判定=False  (标准实现应为 True)
password=''           stored_len= 0  resp_len= 0  -> 服务端判定=False  (标准实现应为 True)
```

这是 fail-closed 的彻底损坏，**不是认证绕过**（不存在安全绕过面），而是任何人都进不去。

### 4.2 建表必失败（`lightdb/storage/buffer_pool.py:15`）

```python
15:        self.f = open(path, 'r+b')      # 文件不存在 → FileNotFoundError
16:        self.f.seek(0, 2)
17:        if self.f.tell() == 0:          # 恰好写了"空文件则初始化首页"的逻辑
18:            meta = Page(0, page_type=0)
```

调用链：`CREATE TABLE` → [`db/database.py:99-108`](../lightdb/db/database.py#L99-L108) `create_table()` → `Table(...)` → [`db/table.py:72`](../lightdb/db/table.py#L72) `BufferPool(self.heap_path)`。由于 `Table.__init__` 只创建目录、从不创建 `.dat` 文件，`open(path, 'r+b')` 必然抛 `FileNotFoundError`；而 17–20 行那段「空文件初始化」逻辑**永远不可达**（应改用 `'a+b'` 之类的模式）。同一错误在上一轮会话留下的 [\_test_batch14.log](../_test_batch14.log) 中已出现，说明**从未被修复过**。

### 4.3 所有执行错误被伪装成成功

两段代码叠加，导致客户端**无法感知任何失败**：

- [`sql/executor.py:68-70`](../lightdb/sql/executor.py#L68-L70)：捕获所有异常，`logger.error` 后**返回错误字典而不抛出**。
- [`net/server.py:391-437`](../lightdb/net/server.py#L391-L437) `_send_execution_result()`：**从不检查 `result` 中的错误**。DDL/DML（CREATE/DROP/INSERT/UPDATE/DELETE/BEGIN/COMMIT…）**无条件回 OK 包**；SELECT 只要 `type != 'resultset'` 也回 OK 包。

只有 [`net/server.py:349-355`](../lightdb/net/server.py#L349-L355) 在 `executor.execute()` **抛出异常**时才回 ERR 包 —— 但 executor 已经把异常全部转成了返回值，所以这条路径实际上不会触发。

---

## 5. 端到端实验原始结果

**方法**：在进程内启动单进程服务器（`MySQLServer` + 真实 `Database`/`Executor`），临时将该缺陷认证函数置为放行（**仅为隔离变量、专测引擎层**），用自行实现的 MySQL 文本协议客户端连接，逐条发送 SQL 并打印原始响应包字节。

```
login pkt: 00000002000000            # 登录成功（认证已被放行）
CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(2 -> len=7 first=00 hex=00000002000000
INSERT INTO t VALUES (1,'a',10)                    -> len=7 first=00 hex=00000002000000
SELECT * FROM t                                    -> len=7 first=00 hex=00000002000000
SELECT COUNT(*) FROM t                             -> len=7 first=00 hex=00000002000000
SELECT 1                                           -> len=1 first=01 hex=01

db.tables = []                       # 表根本没建出来
data files = []                      # 数据目录里没有任何文件
服务端日志: 执行错误: [Errno 2] No such file or directory: '_e2e_data2\\t.dat'
```

解读：

- `00000002000000` 是 7 字节 **OK 包**（affected_rows=0、last_insert_id=0）。CREATE TABLE / INSERT / SELECT 全部收到 OK 包，**但表不存在、数据目录为空**，服务端只是把 `FileNotFoundError` 记进日志后回了成功。
- 唯一返回真实结果集的是 `SELECT 1`（响应首字节 `01` = 列数），因为它在 [`net/server.py:248-252`](../lightdb/net/server.py#L248-L252) 被**硬编码特例**处理。
- 换言之：即使绕过认证，**也无法建表**；而失败对客户端完全不可见。

---

## 6. 分层真实性评级

| 层 | 真实度 | 依据 |
|---|---|---|
| 页 / 缓冲池 / B+Tree / 锁 / MVCC 算法 | **较高（真算法）** | 4096B 页 + 槽数组；`OrderedDict` LRU + 脏页 `fsync`；真分裂、根退化、叶子链表；按 key 的条件变量、兼容矩阵、三色 DFS 死锁检测 |
| 事务 / 崩溃恢复 / ACID | **不支持** | `xmax` 恒写 0（[`db/table.py:206`](../lightdb/db/table.py#L206)）；update 原地覆写、delete 物理删行，abort 只回滚内存 undo；恢复只重放 INSERT 且结束即 `wal.truncate()`；[`storage/wal.py:161`](../lightdb/storage/wal.py#L161) 跳过 `txn_id == 0` 的记录，而 INSERT 默认 `txn_id=0`；[`db/table.py:151`](../lightdb/db/table.py#L151) 的锁永不释放（[`db/database.py:150`](../lightdb/db/database.py#L150) 只 release tid）→ 后续对同一主键的 `BEGIN;UPDATE` 必然 5 秒超时 |
| SQL 引擎 | **玩具级** | [`sql/parser.py`](../lightdb/sql/parser.py) 是正则解析器（非词法分析）；CTE、窗口函数、`IS NULL`、括号条件、`COUNT(DISTINCT)`、`HAVING COUNT(*)`、`OFFSET`、`BETWEEN` 均不支持；`sql/functions/` 整包（约 200 个函数）**零生产引用**，SQL 里写 `UPPER()` 也不可用 |
| 优化器 | **基本不生效** | 单表查询根本不过计划（[`sql/executor.py:190-193`](../lightdb/sql/executor.py#L190-L193) 直接调 `tbl.select()`），优化结果只存进 `_last_plan`；[`sql/plan_executor.py:260-265`](../lightdb/sql/plan_executor.py#L260-L265) 的 `_exec_IndexScan` 直接 `return self._exec_SeqScan(node)` |
| 网络协议 | 中等（文本协议真，其余缺） | 握手/收包/COM_QUERY/文本结果集编码真实；**无预处理语句**（COM_STMT_PREPARE 落到 Unknown command）、公告了 `CLIENT_MULTI_STATEMENTS` 却不拆语句、无 SSL、`KILL` 只回 OK 不断连接、`SHOW STATUS` 数值硬编码 |
| 复制 | **不通** | [`replication/master.py:10`](../lightdb/replication/master.py#L10) 访问不存在的 `db.binlog` → 构造即 `AttributeError`；从库用魔法 SQL `SELECT * FROM __BINLOG_FETCH__`（[`slave.py:108`](../lightdb/replication/slave.py#L108)），服务端无此命令；`semi_sync` 逻辑真实但**无任何调用者** |
| 安全（权限 / SSL / 审计 / 密码策略 / 白名单 / 连接限制） | **空壳** | 实现相对完整，但 `net/` 与 `sql/` **从不调用**，仅被 `tests/` 实例化；权限模型不生效，能登录者即全权 |
| Cython 加速 | **0%** | 6 个 `.pyx` 是真 Cython 源码，但无任何编译产物；[`lightdb/cython/__init__.py:20`](../lightdb/cython/__init__.py#L20) 捕获 ImportError 后置 `None`，**包内没有同名纯 Python 回退模块**，所谓「自动回退」不存在；引擎从不 import 它，测试用 `if bt is None: return` 假通过（[`tests/test_v4_batch3.py:34`](../tests/test_v4_batch3.py#L34)） |
| 列存 / 编码器 | **约 40%** | Plain/Dict/Delta round-trip 正确；但 BitPack 在 [`columnar/encoders.py:430`](../lightdb/columnar/encoders.py#L430) 用 `max(0, v)` **损坏负数**（`[3,-5,10]` → `[3,0,10]`），且会被自动选中；解码按声明类型而非实际值，混合类型整块错位；[`columnar/column_store.py:100`](../lightdb/columnar/column_store.py#L100) 的 scan 只读内存副本、从不解码落盘数据 → 查询期零压缩收益；`compression_ratio` 由 `len(values)*10` 估算而来 |
| 向量化引擎 | **0%（构造即崩）** | [`olap/vectorized_engine.py:45`](../lightdb/olap/vectorized_engine.py#L45) 写的是 `store.scan(...)`，而参数名是 `column_store` → 抛 `NameError`；`VectorizedScan`、`OLAPOptimizer.optimize()`、`db/hybrid_table.py:125` 的 `olap_aggregate()` 全部不可用 |
| 并行 | **0% 加速（实测更慢）** | [`parallel/executor.py:30`](../lightdb/parallel/executor.py#L30) 只用线程，GIL 未绕过；`where` 入参被忽略（条件为永假仍返回全表 → 静默错误结果）；[`parallel/merge.py:124`](../lightdb/parallel/merge.py#L124) 的 AVG 分支只有 `pass` → 并行 group_by 的 AVG 全为 None；子审计实测 40 万行同一负载：并行 0.269s vs 串行 0.170s = **0.63×** |
| 运维 / 监控 / 基准 / 稳定性工具 | **结论层不可信** | 无指标源时健康检查仍输出「命中率 100.00%、累计 0 次死锁、全部 PASS」；`soak_test` 通过与否**不看错误数**；故障注入在 Windows 上多数场景零注入仍判 `success` |

---

## 7. 文档承诺 vs 代码现实

[docs/V4_RELEASE_NOTES.md](V4_RELEASE_NOTES.md) 与两份性能白皮书的核心宣称：

| 文档宣称 | 代码现实 |
|---|---|
| 单线程点查 12,000 QPS | **无任何测试断言过该目标**。它只写在 [`tests/regression_v4/test_performance_v4.py:13-18`](../tests/regression_v4/test_performance_v4.py#L13-L18) 的 `PERF_TARGETS` 字典里，而该字典的 `single_thread_point_select` 与 `multi_process_point_select`（80000）**从未被引用**；实际断言只用了 `olap_scan_rows_per_sec` 与 `olap_aggregate_rows_per_sec`（同文件 `:85`、`:117`） |
| 8 核点查 92,000 QPS（MySQL 的 328%） | 多进程模式**无人监听端口**，且两项 QPS 目标均无断言 |
| 大表聚合 85M rows/s（MySQL 的 17000%）、OLAP 提升 41x | 向量化引擎构造即崩（见 §6） |
| Cython 加速 12.5x，「可选（自动回退）」 | 从未编译，且**没有回退实现** |
| 二进制行格式 3–4x、压缩比 3–60x | 活路径实际走 `json.dumps`（[`db/table.py:126`](../lightdb/db/table.py#L126)），`BinaryRowFormat` 在主链路上是死的 |
| MySQL 5.7 兼容度 95% | 连 `IS NULL`、括号条件、`COUNT(DISTINCT)`、`HAVING` 都不支持 |
| 从 v3.0 数据无缝升级 | [`db/table_v3.py:33-47`](../lightdb/db/table_v3.py#L33-L47) 的 `insert` 只编码不写入、`scan` 使用未定义的 `self.bt`，整类不可用 |
| 综合得分 8.8/10（v1→v4 自评） | 无评估口径与数据来源 |

**仓库内没有任何基准产物**：

- `lightdb/benchmark/baseline.py:11` 定义的基准存储目录 `~/.lightdb_studio/benchmarks` **不存在**；`~/.lightdb_studio/` 下只有空的 `reports/`、`soak/` 两个目录。
- 因此白皮书中「每表 100 万行、预热 30 秒、正式测试 120 秒、重复 3 次取中位数」的方法学描述**没有对应的原始数据**可核对。
- 文档自身的完整性也有缺口：[docs/V4_DECISION.md](V4_DECISION.md) 在「2.2 GIL 影响量化」标题处**直接截断**。

---

## 8. 死代码统计

以 4 个入口（`lightdb.main`、`lightdb.main_v4`、`lightdb_studio.app`、`web.start`）为根做 AST 导入图 BFS（含相对导入解析）：

- **296 个非测试模块中，仅 121 个从入口可达；175 个不可达。**
- 扣除约 21 个「包 `__init__` 因直接导入子模块而未单独出现」的统计假象后，**约 154 个模块是真实死代码**，接近代码库的一半。

分组明细与重复实现清单见 [docs/DEAD_CODE.md](DEAD_CODE.md)。

---

## 9. 测试与工具可信度

**测试套件**（`python -m pytest tests -q --timeout=180`）：

```
118 failed, 150 passed, 1 warning in 290.44s (0:04:50)
```

- 268 个测试：**150 通过 / 118 失败**，失败集中在各批次测试（`test_batch6/7/9/11/12/13` 各 7–9 项，`test_v4_batch4` 8 项）。
- 失败原因与 §4.2 同源：上一轮会话的 [\_test_batch14.log](../_test_batch14.log) 里就是同一个 `FileNotFoundError: ...\data_b14\t.dat` —— **这些红灯从来没被修过**。
- 测试风格是「打印式脚本」：全仓库测试共 **768 个 `assert` 对 1,823 个 `print`**，49 个测试文件里**只有 1 处** `pytest.raises` / `unittest.mock` / `MagicMock`；另有 3 个 0 字节测试文件。
- 部分测试通过「被测对象不可用就提前返回」来假通过，例如 [`tests/test_v4_batch3.py:34`](../tests/test_v4_batch3.py#L34) 的 `if bt is None: return`。

**运维/监控工具存在编造指标**（详见 §10）。

---

## 10. 安全问题清单

| 位置 | 问题 |
|---|---|
| [`web/auth.py:11`](../web/auth.py#L11) | HMAC 签名密钥硬编码为 `"lightdb_studio_secret_2025_change_me"`，且无处可配 |
| [`web/server.py:18-21`](../web/server.py#L18-L21) | 4 个账号的**明文口令**（`admin123` / `dev123` 等），且启动时回显 |
| [`web/static/index.html`](../web/static/index.html) | 登录框预填 `admin/admin123`，并展示全部测试账号 |
| [`web/api.py:226`](../web/api.py#L226) | 权限判定只看 SQL 首个词 → `SELECT 1; DROP TABLE t` 以读权限放行（连接为 `autocommit=True`，无多语句防护） |
| [`web/api.py:309-311`](../web/api.py#L309-L311)、[`api.py:187`](../web/api.py#L187) | `order_by`、表名直接拼进 SQL |
| [`lightdb_studio/core/team_config.py:29`](../lightdb_studio/core/team_config.py#L29) | `_xor_encrypt` 用 XOR 冒充加密，默认键为 `lightdb_default` |
| [`lightdb/security/audit.py:47`](../lightdb/security/audit.py#L47) | 审计若启用会把原始 SQL（含 `IDENTIFIED BY '明文'`）不脱敏落盘 |
| [`lightdb/security/password_policy.py:100`](../lightdb/security/password_policy.py#L100) | 口令历史用无盐 SHA-256 |
| [`lightdb_studio/config.py:89`](../lightdb_studio/config.py:89) | 连接口令明文落盘，全库无 keyring / 加密 |
| 数据库服务端 | 无 TLS（服务端未公告 `CLIENT_SSL`，`have_ssl=DISABLED`）；IP 白名单、连接限制、权限校验均无调用者，暴力尝试无锁定；任何能登录者即全权 |

补充：[`web/metrics.py`](../web/metrics.py) 定义了 `/metrics` 路由，但 [`web/server.py:53-54`](../web/server.py#L53-L54) 只注册了 `api.router`，**该端点从未挂载**。

---

## 11. 目录树宣称但不存在的文件

以下文件在 [lightdb_project.txt](../lightdb_project.txt) 的目录树中被列出，但实际不存在：

```
lightdb/sql/lexer.py              lightdb/sql/vectorized.py
lightdb/sql/parallel_planner.py   lightdb/tools/migrate.py
docs/CYTHON_DEV_GUIDE.md          CHANGELOG_v3.md
CHANGELOG_v4.md                   RELEASE_v3.0.md
RELEASE_v4.0.md                   tests/test_v3_batch6.py
tests/test_v4_batch6.py
```

缓解因素：这些名字**没有任何生产代码 import**（AST 扫描未发现指向不存在模块的导入），因此不会造成 `ImportError`；但 `lightdb/tools/migrate.py` 被 [`tools/__init__.py`](../lightdb/tools/__init__.py)、`docker/entrypoint.sh`、[docs/MIGRATION_GUIDE_v3.md](MIGRATION_GUIDE_v3.md) 引用，属于「宣称有、实际无」的功能缺口。

另外，目录树与 [docs/ARCHITECTURE_v4.md](ARCHITECTURE_v4.md) 都未提及 `lightdb_studio/ui/` 下实际存在的 `ai_chat.py`、`collaboration_panel.py`、`cdc.py`、`cloud_sync.py`、`data_science.py`、`plugin_manager.py`、`team_config.py` 等文件，说明目录树与实际代码**双向不一致**。

---

## 12. 「伪企业级功能」证据

以下代码把**模拟值或常量**包装成产品能力输出：

| 位置 | 证据 |
|---|---|
| [`lightdb_studio/core/cloud.py:212`](../lightdb_studio/core/cloud.py#L212) | `n.qps = threads_running * 50   # 简化估算`；`:237` `storage_cost = node.disk_gb * 0.0001`（`:31` 硬编码价目表，却输出「月度成本/优化建议」） |
| [`lightdb_studio/core/distributed.py:350`](../lightdb_studio/core/distributed.py#L350) | `# 模拟准备（实际应开启事务）`；`:328` `begin_distributed_txn` 返回 `participants: []` 的纯字典 |
| [`lightdb_studio/core/data_fabric.py:316`](../lightdb_studio/core/data_fabric.py#L316) | `'algorithm': 'sha256-xor-demo'`（自造 XOR 流密码），且 `encryption_key` 明文存入 `fabric.json` |
| [`lightdb_studio/core/ai_finetune.py:277`](../lightdb_studio/core/ai_finetune.py#L277) | `_simulate_training` 用 `random.gauss(0, 0.05)` 编造 loss 曲线；`:327` 伪造 `output_model` 名称写入 jobs，`:211` 自称「模拟器」 |
| [`lightdb_studio/core/fullstack_obs.py:255`](../lightdb_studio/core/fullstack_obs.py#L255) | 根因分析按事件类型挑一条并硬编码 `'probability': 0.8`，无因果算法 |
| [`lightdb_studio/core/assets.py:130`](../lightdb_studio/core/assets.py#L130) | `table_growth_trend()` 注释「简化：基于当前快照」后直接 `return []` |
| [`lightdb_studio/core/product_ops.py:87`](../lightdb_studio/core/product_ops.py#L87) | `consumer_count` / `query_count` / `last_used` 全代码库**无任何递增点**，健康度的「使用活跃度」维度恒为 0 |
| [`lightdb/monitor/index_advisor.py:143`](../lightdb/monitor/index_advisor.py#L143) | `estimated_gain_pct = min(90, count * 5)`，报告 `:226` 把它当「预估收益」打印 |
| [`lightdb/profiling/hotspot_ranker.py:142-148`](../lightdb/profiling/hotspot_ranker.py#L142-L148) | 用 `0.6 + 0.4/4`、`/3`、`*0.8` 等常数算出「预估加速比」，`:198` 打印「预估加速 N.NNx」；`:28-44` 按函数名子串查表给 60%/80% 收益 |
| [`lightdb/monitor/health_checker.py:66,76,85`](../lightdb/monitor/health_checker.py#L66-L85) | 命中率默认 1.0、lag 0、deadlock 0 → **无任何指标源时 `check_all()` 仍全 PASS**，输出「命中率 100.00%」「累计 0 次死锁」；`:56` `max_conns = 151` 硬编码 |
| [`lightdb/monitor/lock_analyzer.py:77,97`](../lightdb/monitor/lock_analyzer.py#L77-L97) | `blocking_pid = 0` 使建图条件永假 → 阻塞链/等待图永远显示「（无阻塞链）」 |
| [`lightdb/stability/soak_test.py:372`](../lightdb/stability/soak_test.py#L372) | `passed = len(issues) == 0`，而 `issues` 只含内存/校验和 → **百万次报错仍判「长稳测试通过」**；`:56` 的 `error_rate_threshold` 从未被引用 |
| [`lightdb/stability/fault_injector.py:258-261`](../lightdb/stability/fault_injector.py#L258-L261) | 非 Linux 的网络中断只是 `sleep(5)`，`:270` 仍 `success=True`；`:420` `recovered=True` 无条件 |
| [`lightdb/experiments/comparison.py:213`](../lightdb/experiments/comparison.py#L213) | 「达到 MySQL 5.7 的 120%+」为无数据断言（`:60` 兜底 8.0、`:75` 兜底 3.0） |
| [`lightdb/storage/row_format.py:226`](../lightdb/storage/row_format.py#L226) | `(t1-t0)/(t1-t0)` 恒等于 1.00x 的「加速比」 |

---

## 13. 修复优先级建议

**P0 — 不修则任何功能都无从验证**

1. [`lightdb/storage/buffer_pool.py:15`](../lightdb/storage/buffer_pool.py#L15)：`'r+b'` → 可创建模式（如 `'a+b'`），否则 `CREATE TABLE` 永远失败。
2. [`lightdb/net/mysql_protocol.py:293`](../lightdb/net/mysql_protocol.py#L293)、[`:304`](../lightdb/net/mysql_protocol.py#L304)：把长度守卫移入空密码分支之后，并改为 `hashlib.sha1(candidate).digest() == stored_hash`，否则无人能登录。
3. [`lightdb/net/server.py:391-437`](../lightdb/net/server.py#L391-L437)：`_send_execution_result` 必须检查 `result` 中的错误码并回 ERR 包；同时 [`sql/executor.py:68`](../lightdb/sql/executor.py#L68) 应保留错误类型与码值，不要一律 1105。

**P1 — 让四个入口至少能启动**

4. [`lightdb/main.py:156,162`](../lightdb/main.py#L156)：去掉裸 emoji `print`（或统一走 logger），否则 `--check` 在 cp936 控制台必崩。
5. 多进程：为 `WorkerPool` 建立 `conn_queue` 并让 `MasterListener` 真正被调用，否则 multi 模式恒定不可用；Windows 下还需解决 `RLock` 的 pickle 问题。
6. [`lightdb_studio/core/reports.py:172`](../lightdb_studio/core/reports.py#L172)：f-string 内含反斜杠，Python ≤ 3.11 语法错误 → 桌面端 0% 可运行。修好后还有 [`ui/agent_chat.py:110`](../lightdb_studio/ui/agent_chat.py#L110) 未导入 `QLineEdit`、[`ui/sql_editor.py:105`](../lightdb_studio/ui/sql_editor.py#L105) 使用不存在的 `QPlainTextEdit.ExtraSelection`、[`ui/main_window.py:144-146`](../lightdb_studio/ui/main_window.py#L144-L146) 的构建顺序问题。
7. `requirements.txt` 未声明 PySide6、fastapi 等；本环境也未安装 `pymysql`（`lightdb/tools/`、`benchmark/`、`lightdb_studio/db/` 在需要时会 ImportError）。

**P2 — 让结论可信**

8. 在 CI 中让测试红灯成为阻断项；当前 118 个失败长期存在。
9. 让基准与监控工具改为「无数据就报错/返回未知」，而不是用默认值伪装成满分健康度（§12）。
10. 文档中的性能数字要么补上可复现的基准产物，要么明确标注为设计目标（本报告已在 §7 与各文档卷首声明中处理）。

**P3 — 需要重写而非修补**

11. 事务与崩溃恢复（页级回滚、redo/undo、`xmax` 真正生效、恢复重放 UPDATE/DELETE）。
12. SQL 解析器（正则 → 词法/语法分析）、优化器接线、`sql/functions/` 与执行器打通。
13. Cython 接入主路径并补回退实现；列存查询期真正走编码数据；并行改为多进程或明确放弃。
14. 权限/审计/白名单/连接限制接入 `net/` 与 `sql/` 调用链。

---

## 14. 修复记录（同一次会话，2026-10）

审计完成后在同一工作区实施了第一批修复，并用**真实 pymysql 客户端**与全量测试套件复验。原提交已通过 `git init` + 首次提交（`a63b6a3`）留存，可用 `git diff a63b6a3` 查看全部改动。

| 原缺陷 | 修复 | 验证 |
|---|---|---|
| §4.2 建表必失败 `buffer_pool.py:15` | 文件缺失时先创建 `.dat`，再以 `'r+b'` 打开 | `CREATE TABLE` 成功，落盘 `t.dat/t.btroot/t.wal/_meta.json` |
| §4.1 认证恒失败 `mysql_protocol.py:293/:304` | 空密码分支前置；比较改为 `hashlib.sha1(candidate).digest() == stored_hash` | 真实 pymysql 登录成功（此前必 1045） |
| §4.3 错误伪装成 OK `server.py:391-437` | `_send_execution_result()` 优先识别 `{'type':'error'}` 并回 ERR 包 | 查询不存在的表返回 `1146`，不再回 OK |
| 客户端连接协商失败（审计后新发现） | 新增 `SET NAMES` / `SET AUTOCOMMIT` 等会话语句确认（`server.py`） | pymysql 连接阶段通过（此前 `SET NAMES utf8mb4` 报 1064） |
| §7/P1 `--check` 因 emoji 崩溃 | `main.py`/`main_v4.py` 增加 `reconfigure(errors='replace')` 控制台保护，裸 emoji 改 ASCII | `python -m lightdb.main --check` 退出码 0 |
| §3 桌面端 0% 可运行 | `core/reports.py` f-string 改写为 3.8+ 兼容；补 `agent_chat.py` 的 `QLineEdit` 导入；`sql_editor.py` 改用 `QTextEdit.ExtraSelection`；`main_window.py` 状态栏构建前置 | offscreen 实测 `MainWindow` 构造成功（6 菜单）；`_highlight_current_line()` 生效 |
| §7 打包把 `lightdb` 排除 | `pyproject.toml` 打包 `lightdb*`，版本统一 4.0.0，删掉指向不存在模块的 `lightdb-studio-cli`，补 `lightdb-server` 入口 | `find_packages` 命中 30 个包；入口函数均存在 |
| Dockerfile `COPY main.py`（不存在） | 改为 COPY 真实目录 + entrypoint，compose/k8s 入口自洽 | 全部 COPY 源与卷路径 `Test-Path` 为 True，9 个 YAML 解析通过 |

**复验数据**：全量测试由 **150 通过 / 118 失败** 变为 **163 通过 / 105 失败**，耗时 290s → 201s；本次直接修好 14 个用例（`test_inner_join`、`test_left_join`、`test_cross_join`、`test_where_after_join`、`test_join_count`、`test_group_by`、`test_distinct`、`test_tpch_simplified`、`test_btree_split`、`test_mvcc_isolation`、`test_batch_insert_performance`、`test_diff_databases_live`、`test_show_stats`、`test_row_format_performance`）。

**关于 1 个「回归」**：`tests/test_v2_batch6.py::test_federated_governance` 在复验中出现失败，但用 `git stash` 回到原始提交后**同样失败**（`assert not result['allowed']`），因此不是本次改动引入，而是该用例自身存在顺序/状态依赖。它在 §9 的首次运行中偶发通过。

**剩余 105 个失败的归因**：约 32 个来自沙箱限制（禁止写工作区外文件、禁止多进程原语，`PermissionError`）；其余为真实待修项 —— `vectorized_engine.py:45` 的 `NameError: store`（8 个）、CTE/窗口/UNION 未实现导致的 `KeyError: 'rows'`/`'columns'`（8 个）、`Database.binlog` 等属性缺失（5 个）、并行无加速与 Cython 未编译的性能类断言（约 8 个），以及 SQL 解析器语法缺口等。

**本次未做**：事务原子性与崩溃恢复、SQL 解析器重写、权限/审计接线。

---

## 15. 多进程模式接线（后续会话，2026-10）

审计时的结论是「多进程模式默认启用却恒定不可用」。后续会话完成了接线，实测可用。

### 15.1 原设计为什么不可能工作

1. **无人监听**：`MasterServer.start()` 从不调用 `create_listener`/`MasterListener`，worker 在无 `SO_REUSEPORT` 的平台（Windows）只走队列路径，于是没有任何进程 bind 端口。
2. **Worker 立即退出**：`shared_state` 里没有 `conn_queue`，而 `WorkerPool._pending_conns` 是另一个队列且 `dispatch()` 无人调用。
3. **`shared_state` 不可序列化**：里面塞了 `Database`/`Executor`/共享内存对象（含文件句柄与 `RLock`），Windows spawn 下无法 pickle；即便在 Linux fork 下能启动，每个 Worker 也各自持有一份**独立的存储副本**。
4. **共享内存路径硬编码**：`SharedBuffer` 默认写 `~/.lightdb_studio/shm`；`SharedTxnCoordinator(...)` 漏了 `create=True`，打开不存在的文件必然失败；`SharedBufferPool` 没有 `destroy()`，停机后 `.shm` 文件永久残留。

第 3 点是架构性的：多进程各自打开同一份文件型存储，会各自维护 BufferPool 页缓存与 BTree root，交叉写回**必然损坏数据**。所以正确的接线不是「把 Database 复制给每个 Worker」，而是**让 Master 独占存储**。

### 15.2 接线后的架构

- **Master（存储与调度）**：独占 `Database`/`Executor`；创建共享内存组件与队列；运行执行服务线程；在无 `SO_REUSEPORT` 的平台绑定端口，把 accept 到的连接按轮询交给 Worker；停机时停监听 → 发哨兵 → 停 Worker → 销毁共享内存（含 unlink）。
- **Worker（连接与协议）**：从自己的连接队列取连接（Windows 用 `socket.share(pid)` / `socket.fromshare()` 交接句柄）；通过 `exec_queue` 把 SQL 发回 Master，从**本 Worker 专属的响应队列**取结果（按 `req_id` 路由，避免并发串包）；元数据（用户表/表结构）通过 `snapshot` 请求同步。
- **共享内存目录**：统一为 `<data_dir>/shm`（可用 `shm_dir` 配置覆盖），不再写用户主目录。

### 15.3 实测结果

| 验证项 | 结果 |
|---|---|
| `python -m lightdb.main --mode multi --workers 2` | ✅ 端口被绑定，Worker 就绪 |
| 建表 / 插入 / 查询（真实 pymysql） | ✅ `CREATE TABLE` → 2×`INSERT` → `SELECT` 返回 `((1,100),(2,200))` |
| 3 个并发客户端 | ✅ 分散到 3 个 Worker 进程 |
| 跨进程可见性 | ✅ 3 个客户端各自 `COUNT(*)` 都是 3 |
| Master 被强杀 | ✅ Worker 3 秒内自行退出（探活），无孤儿进程，端口释放 |
| 优雅停机 | ✅ 端口释放，共享内存文件零残留 |
| 多进程相关测试 | ✅ `tests/test_v4_batch2.py` + `tests/regression_v4/test_multiprocess.py` 共 15 项全通过 |

### 15.4 边界（不要误解为性能已达标）

- **查询执行仍是 Master 单线程**：多进程的收益是连接与协议层多核化 + 崩溃隔离，**不是**查询执行的 CPU 加速。文档中「8 核 92,000 QPS」与本次改动无关，仍属未验证的设计目标。
- **仍是 `mysql_native_password` 且无 TLS**，权限模型依旧未接入调用链。
- 会话状态（`SHOW PROCESSLIST`/`KILL`、事务）是**每 Worker 进程各自一份**，没有汇聚到 Master。
- 元数据（`SHOW TABLES`/`SHOW COLUMNS`）每个 Worker 各持一份快照，**最多 5 秒延迟**（后台线程每 5 秒向 Master 刷新一次）；已为该行为补了回归测试。
- Linux/macOS 走 `SO_REUSEPORT` 由 Worker 自绑定，本机（Windows）无法验证该路径。

---

## 16. SQL 能力补齐（后续会话，2026-10）

审计时结论：解析器是正则驱动，CTE 不存在、窗口函数被当成不透明列名、UNION 解析出来却没有执行分支、GROUP BY 与 DISTINCT 被静默忽略。后续会话补齐了前四项。

### 16.1 做了什么

| 能力 | 原先 | 现在 |
|---|---|---|
| 窗口函数 | 不存在（`ROW_NUMBER() OVER (...)` 被当作列名） | 解析器抽出 `func/args/partition_by/order_by/alias`；执行器取到基础行后计算 ROW_NUMBER / RANK / DENSE_RANK / LAG / LEAD / SUM / AVG / COUNT / MIN / MAX（有 ORDER BY 为累计值，无则整分区值） |
| CTE | `WITH` 直接解析失败 | `WITH [RECURSIVE] a AS (...), b AS (...)` 解析为内存关系（**不建真实表文件**，避免主键唯一性把有重复值的 CTE 结果吃掉），后面的 CTE 可引用前面的；主体复用现有查询引擎 |
| UNION / UNION ALL | 解析出 `{'command':'UNION',...}` 但**丢弃了 ALL 标记**，执行器又没有 UNION 分支（测试报 `KeyError: 'rows'`） | 按左结合语义合并：UNION 对已累积结果去重（含同一分支内部重复），UNION ALL 直接追加；列数不一致返回 1222 |
| GROUP BY | **被完全忽略**：`SELECT dept, COUNT(*) ... GROUP BY dept` 返回全表一行的聚合 | 真正分组，复用存储层 `Table._aggregate` 计算聚合列 |
| DISTINCT | 解析器记录了 `distinct`，**执行路径从未使用** | 在分组/窗口之后去重，再 ORDER BY / LIMIT |

GROUP BY 与 DISTINCT 属于**静默错答**：不报错、结果错。仓库自带的 `tests/test_batch7.py::test_group_by` 只断言 `sum(COUNT(*)) == 20`，所以「退化成一行的全表聚合」也能通过 —— 这类测试对缺陷无鉴别力。

### 16.2 实测

同实例、真实执行器，17 项断言全部通过（并已固化为 [`tests/test_v4_sql_features.py`](../tests/test_v4_sql_features.py)，5 组用例）：

| 组 | 内容 | 结果 |
|---|---|---|
| 窗口 | ROW_NUMBER 分区、RANK/DENSE_RANK、LAG/LEAD、SUM OVER 累计（120→220） | ✅ |
| CTE | 单 CTE + 外层 WHERE、多 CTE + UNION ALL、CTE + 窗口（列名带出 `rn`）、CTE + 聚合 | ✅ |
| UNION | UNION 去重（含同分支内部重复）、UNION ALL 不去重、三重 UNION | ✅ |
| 分组/去重 | GROUP BY + COUNT、GROUP BY + ORDER BY 别名、DISTINCT、`u.dept` 限定名 | ✅ |
| 回归 | `COUNT(*)`、`SELECT *`、`ORDER BY ... LIMIT`、`WHERE` 单表路径 | ✅ |

顺带修掉 [`vectorized_engine.py:45`](../lightdb/olap/vectorized_engine.py#L45) 的 `store.scan` → `self.store.scan`（未定义名字，构造 `VectorizedScan` 直接 `NameError`，是 OLAP 向量化不可用的直接原因；解掉该报错后向量化路径仍受 COUNT 计 NULL、AVG 未实现等问题限制）。

### 16.3 仍未支持（诚实清单）
- `WHERE ... IN (SELECT ...)`、`NOT IN (SELECT ...)`、`EXISTS`（后者目前是「子查询非空即全真」的简化实现）
- `HAVING`（解析器无法处理 `HAVING COUNT(*) > 1` 这类聚合谓词）
- ~~带表别名的 JOIN~~ / ~~JOIN 上的聚合~~ —— **已在后续会话修复**，见 §16.5
- `LIMIT n OFFSET m` 的 OFFSET、递归 CTE（能解析，结果不保证）
- 窗口帧（`ROWS BETWEEN ...`）、`FIRST_VALUE/LAST_VALUE/NTILE`

### 16.4 顺带修掉的测试环境问题

新增 [`tests/conftest.py`](../tests/conftest.py)：把标准流改成「无法编码的字符就替换」。中文 Windows 控制台是 GBK，而测试里有 1800 多处 `print` 带 emoji；这类 `UnicodeEncodeError` 会**在 `db.shutdown()` 之前**中断测试，插入的数据还留在 BufferPool 内存里没落盘，导致后续共用同一数据目录的测试读到空表（这正是 `test_batch7.py::test_union` 长期失败的真正原因）。

### 16.5 带表别名的 JOIN 与 JOIN 聚合（后续会话补）

**根因**：解析器不认识表别名。吃完 `FROM users` 之后再遇到 ` u JOIN orders o ...`，JOIN 正则要求紧跟 `JOIN`，于是在 `u` 处匹配失败 → **整个 JOIN 被静默丢弃**，查询退化成单表查询；而优化器与 `plan_executor` 本来就实现了 `HashJoin` / `NestedLoopJoin` / `GroupBy` / `_resolve_col`（含限定名与歧义检测），能力一直是有的。

**修法**：
1. 解析器吃可选别名（`u` 或 `AS u`），带关键字黑名单（`JOIN`/`WHERE`/`GROUP`/... 不能被当成别名），别名随 `table_alias` / `joins[i]['alias']` 进入 AST。
2. 优化器把别名传进 `SeqScan`，`_exec_SeqScan` 的列前缀改用别名（`u.id` 而不是 `users.id`）——否则两表同名列会被 `_resolve_col` 判成歧义。
3. 单表路径：带限定名的列/条件/排序（`SELECT u.name ... WHERE u.city=?`）原先因存储层只认裸列名而报「列不存在」；现在统一归一化（只改 `column` 字段，`value` 原样保留），并让带限定引用的单表查询走统一高级路径，输出列名按裸名给出（MySQL 风格）。

**实测**（Lead 自测 9 项全通过）：

| 用例 | 结果 |
|---|---|
| `SELECT u.city, COUNT(*), SUM(o.amount) FROM users u JOIN orders o ON u.id=o.user_id GROUP BY u.city` | `[('BJ',3,60),('SH',3,150)]` |
| 两表同名列限定投影 `SELECT u.id, o.user_id ...` | 6 行，不报歧义 |
| `LEFT JOIN` 未匹配行 | `('e', None)` 保留 |
| 别名单表 `WHERE u.city='BJ'` / `GROUP BY u.city` / `ORDER BY u.name` | ✅ |
| 无别名 JOIN、普通单表查询、窗口函数 | ✅ 无回归 |

回归测试：`tests/test_v4_sql_features.py::test_aliased_join_and_aggregate`。顺带说明：`tests/test_batch7.py::test_tpch_simplified` 与 `tests/test_batch6.py::test_explain_join` 是两个**过时/环境相关**用例——前者此前"通过"是因为表为空时 `_aggregate` 短路成 `None`（假通过），后者期望的「计划树文本」与 `EXPLAIN` 实际返回的 MySQL 风格表格（`cols=[id, select_type, table, ...]`，`row[0]` 恒为 int）不符，从来不可能通过。

---

## 17. 事务原子性与崩溃恢复（后续会话，2026-10）

审计结论是「事务不能支撑 ACID：无页级回滚、`xmax` 恒 0、恢复只重放 INSERT、启动即 truncate」。后续会话修好了三处根因，并实测通过。

### 17.1 三处根因

| # | 根因 | 后果 | 修法 |
|---|---|---|---|
| 1 | **磁盘页读不回来**：页魔数用 `'<I'` 写入 `0x4C444250`，小端落盘是 `b'PBDL'`，而 `BufferPool.get_page` 比较的是 `b'LDBP'` | **任何页都无法从磁盘读回**，重启后只能靠 WAL 重放整表；UPDATE/DELETE 直接丢数据 | 改用大端写魔数，磁盘页可正常读回 |
| 2 | **autocommit 写入被误判为 loser**：autocommit 语句使用合成高位事务号，而 WAL 没有对应的 COMMIT 记录 | 恢复时把**已提交**的 autocommit 行当未提交事务撤销，重启后整表消失 | `txn_id == 0` 或 `>= 合成号下限` 一律按隐式提交重放；`WAL.log()` 默认 flush 到 OS（log-before-data） |
| 3 | **loser 撤销把旧版本写成永久删除**：撤销时把「带删除标记的旧版本」原样写回页 | 行头 `xmax` 永久非 0 → 恢复后整表不可见（`[]`） | 撤销前按原值重建 `xmax = 0` 的前像 |

配套修复：`lock_manager` 等待图 `_drop_waiter`（死锁检测由必失败变为 10/10 通过）；`mvcc` 增加 `UndoAction` 物理 undo 栈；`table`/`database` 接线真 abort（物理回滚）、COMMIT 顺序、崩溃 redo/undo、`xmax` 写真实事务号、autocommit 语句用合成事务号并在语句结束释放锁（不再泄漏）。SQL 层配合两处：`_cmd_insert` 传入 `txn['txn_id']` 与 `read_view`（原先无条件 `txn_id=0`，事务内 INSERT 根本无法回滚）；`execute_sql` 修掉 `session or {}`（空字典是 falsy，会新建字典丢掉调用方会话，导致 `BEGIN` 写的 `s['txn']` 传不回去、三步各自 autocommit、ROLLBACK 静默失效）。

### 17.2 Lead 亲自复核的实测（不是子代理自述）

```
初始              : [(1,'v1'), (2,'v2'), (3,'v3')]
回滚 INSERT 后    : [(1,'v1'), (2,'v2'), (3,'v3')]
回滚 UPDATE 后    : [(1,'v1'), (2,'v2'), (3,'v3')]
回滚 DELETE 后    : [(1,'v1'), (2,'v2'), (3,'v3')]
连续重开 2 次     : 行数不变（无重复行累积，排除 WAL 重放导致的行膨胀）
硬杀子进程后重开  : [(1,'v1'), (2,'v2'), (3,'v3'), (77,'x')]   # 已提交的 autocommit 写入保住
                  日志：崩溃恢复 t: redo 1 条, undo 0 条
```

新增 [`tests/test_txn_atomicity.py`](../tests/test_txn_atomicity.py)（15 项）通过。

### 17.3 仍未做到

- **旧版本只存在于 WAL 与内存 MVCC 链**：页上的 UPDATE 是原地覆写、DELETE 是物理删行，旧版本的 `xmax` 没有真正落盘；因此**重启后老快照查不回被覆盖的旧版本**（快照隔离只在同一次运行内成立）。
- `recover_from_wal()` 末尾仍然 truncate（现在是先 flush 再 truncate，接近「恢复完成后截断」，但没有「恢复成功才截断」的判定）。
- 进程内回滚 INSERT 只摘索引项、不回收堆槽位（空间浪费，不影响可见性）。
- Windows 上文件句柄未及时释放：若干「删除数据目录」的测试报 `PermissionError [WinError 32]`（文件被占用），属平台相关，Linux 下删除已打开的文件不受限。

---

## 18. 子查询条件 / HAVING / Cython 回退（后续会话，2026-10）

### 18.1 IN / NOT IN / EXISTS 子查询条件

**根因同 §16.5**：解析器（`_where`）**已经**能产出 `{'op': 'IN'|'NOT IN'|'EXISTS', 'subquery': <parsed>}`，`plan_executor` 也有 `_eval_subquery_exists`；缺的是求值接线——单表路径的 `Table._match` 只认 `column+op+value`（连 `IN+values` 都不支持），于是测试报 `KeyError: 'rows'`。

**修法**：执行器侧预处理，把子查询条件改写成存储层能处理的形式再下推（未改 `db/`）：
- `EXISTS(sub)` → 执行一次子查询，非空即真；恒真就地删除条件，恒假用空 `or` 列表（`any([]) == False`），并在 and/or 里做布尔化简；
- `col IN (sub)` → 取子查询首列去重值集 → `{'or': [等值链]}`；空集显式恒假；
- `col NOT IN (sub)` → `{'and': [不等链]}`；空集显式恒真；
- 子查询经 `self.execute` 递归求值，支持嵌套；**相关子查询显式报错**（不静默错答）。

实测：`IN` → 15 行、`NOT IN` → 5 行、`EXISTS` → 20 行、`NOT EXISTS` → 0 行、空子查询 `IN`→0 / `NOT IN`→20。

### 18.2 HAVING

`_where` 处理不了左值为聚合函数的谓词（`HAVING COUNT(*) > 1` 直接 `SyntaxError`）。现在产出 `{'agg': 'COUNT(*)', 'op': '>', 'value': 1}`；统一路径在 `executor._group_rows` 分组时算出 SELECT 与 HAVING 两侧聚合后用 `_eval_condition` 过滤，计划路径在 `plan_executor._exec_GroupBy` 预算 HAVING 聚合经 `agg_values` 传入。实测：`HAVING COUNT(*) > 5` → `[('Beijing',12)]`；`HAVING city='Beijing'` 也可用。为支持 `EXISTS (SELECT 1 FROM orders)` 顺带补了常量投影。

### 18.3 Cython 接入与回退

**环境事实**：本机无 Cython（`ModuleNotFoundError`）、**无任何 C 编译器**（gcc/cl/clang 全缺）、PyPI 不可达 → **无法编译验证**。

已交付且可验证的部分：`lightdb/cython/fallback/` 薄包装既有纯 Python 实现（btree/page/row_codec/predicate/buffer_pool；hash_table 无对应物故逐行复刻），`__init__` 改为「编译版优先 → 纯 Python 回退」，`status()` 标明来源；`setup.py` 不再静默 `exts=[]`、去掉硬编码 `/arch:AVX2`；`build_cython.sh` 缺条件时明确报错并非零退出；静态修复 6 处必然编译失败的 `.pyx` 错误（缺 `cimport`、`cpdef` 指针参数、`cdef` 内 `import`、`nogil` 带 Python 对象等）。

实测 `status()`：6 项全 `python-fallback`、`all_available: true`、`any_accelerated: false`、`compiled: []`。`test_v4_batch3` + `test_cython_compat` 由「6 个模块全为 None、8 个测试 `if mod is None: return` 空跑」变为真正执行；新增 `tests/test_cython_fallback.py` 24 项通过。

**诚实性收尾**：`row_format_v4.py` / `predicate_v4.py` 原先用 `_cy is not None` 判断加速，回退上线后恒为 True、会谎报「使用 Cython」，改为读 `status()['any_accelerated']`；`regression_v4/test_performance_v4.py::test_cython_row_codec` 原先在无编译版时拿 Python 比 Python（假通过），现在无编译版即跳过加速断言。

**未验证**：`.pyx` 能否真正编译（需 Cython ≥3.0 + MSVC/gcc）；回退与 `.pyx` 的逐字节等价性（5 处边界差异已记入 `fallback/__init__.py`）；`build_cython.sh` 的 bash 运行（沙箱禁命名管道）。

### 18.4 一次误判的纠正：`test_union_all` 不是缺陷，是期望值算错

排查过程与结论（记录下来，避免下次重犯）：

1. 现象：`tests/test_batch7.py::test_union_all` 报 `assert 24 == 8`，即查到 3 倍行数；只跑 `test_group_by` + `test_union_all` 即可复现。
2. 两次最小复现（2 行写入不 shutdown 连续打开 3 次；20 行 `Table.insert` + shutdown + 连续打开 3 次）**都未重现**，且重放日志为 `redo 2 条`、行数稳定 ⇒ 一开始怀疑的「WAL 重放不幂等 / 堆里追加重复行」被**证伪**。
3. 改为解剖真实失败现场（`data_b7`）：索引 `bt.count() = 20`、堆页 20 行、`SELECT *` 20 行；而 `_find_pks(city='Beijing')` 返回 `[2,3,5,7,8,10,12,13,15,17,18,20]` —— **12 个互不重复的 pk**，没有重复。
4. 对着 `setup()` 的数据生成读：`cities = ['Beijing','Shanghai','Beijing','Beijing','Shenzhen']`，`cities[i % len(cities)]` 让 `i % 5 ∈ {2,3,4}` 都是 Beijing ⇒ 真实行数是 **12**，`UNION ALL` 两条分支即 **24**。而原断言写的是 `sum(1 for u in range(1,21) if u % 5 == 2) * 2 = 8` —— **测试自己的算术错误**。

结论：存储层（索引、堆、`_find_pks`）都正确；`test_batch7.py` 的期望值已改为从同一份 `cities` 列表推导，修正后该文件 **9/9 全绿**（含 `test_group_by_having`、`test_in_subquery`、`test_not_in`、`test_exists`、`test_tpch_simplified`）。

教训：本项目里「测试红灯」有两种来源——被实现掩盖的假通过（如 `test_group_by` 只断言总数、`test_cython_row_codec` 拿 Python 比 Python）与**测试自身的错误期望**（如本例）。两者都不能靠改实现来"修"，必须先解剖真实数据。

---

## 附录：审计方法与边界

- **静态**：全量阅读核心模块 + grep 扫描（`TODO`/`pass`/`import random` 等模式）+ AST 导入图可达性分析（自建脚本，含相对导入解析）。
- **运行（只读）**：`import` 探测（`lightdb` 158/160 成功，失败者仅 `lightdb.cython.setup` 因 GBK 输出、`lightdb.tools.cli` 因 `import readline`）、原始协议直连实验、`pytest` 全量运行一次。
- **未做**：未编译 Cython、未在 Linux 下复现、未做磁盘持久化与崩溃恢复实验。
- **环境提示**：本机为 Windows + Python 3.11.9，cp936 控制台；部分失败（emoji `UnicodeEncodeError`）带有平台相关性，报告中已逐条区分「平台相关」与「平台无关」。
- 本报告数字来自一次完整审计，**未在本次归档时重新测量**；如需复核，请按上表给出的命令与文件行号自行验证。
