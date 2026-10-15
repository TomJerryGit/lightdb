# 交接文档（给新会话）

**生成时间**：会话交接点 · **HEAD**：`66afdb8` · **工作区**：干净（仅剩一个删不掉的未跟踪空文件，见 §6）
**测试基线**：`python -m pytest tests -q -p no:cacheprovider` → **34 failed / 463 passed / 11 skipped**（失败数在 33–35 间抖动，见 §2）

---

## §1 立刻要做的任务：阶段 1 第 4 项「元数据诚实化」

原则：**元数据要么真实，要么显式报错，绝不伪造**。以下 4 处均已定位到文件行号。

### 1.1 `SELECT DATABASE()` 返回 `mysql_native_password`（明显的错值）

**已知事实（我已核实）**：

| 环节 | 位置 | 判断 |
|---|---|---|
| 错值出口 | `lightdb/net/server.py:260-263`：`if upper.startswith("SELECT DATABASE()")` → `[(self.database,)]` | 值来自服务端属性 `self.database` |
| `self.database` 的 3 个来源 | `:76` 初始 `"default"`；`:163-164` 握手 `if resp.database: self.database = resp.database`；`:219` 执行 `USE` 时赋值 | 源头只能是**握手解析**或 `USE` |
| 握手 database 字段解析 | `lightdb/net/mysql_protocol.py:274-278`：在 auth response 之后、plugin name 之前，有 `CLIENT_CONNECT_WITH_DB` 守卫 | **代码看是正确的** → 报告把该错值归因到这几行不成立 |
| 可疑点（假设，**未验证**） | `mysql_protocol.py:265-269`：`auth_len = data[offset]` 只按**1 字节**长度前缀解；而现代客户端会置 `CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA`，此时前缀是**长度编码整数**。若二者对不上，`offset` 漂移，database 就会读到 **plugin name**（正好是 `mysql_native_password`） | 需要运行时探针 |

**必须先做探针，不要直接改登录握手**（改错会让整个服务端登录不可用）。探针步骤：

```powershell
# 1) 起服务端（后台）
python -m lightdb.main --mode single --host 127.0.0.1 --port 8899 --data-dir _probe_db
# 2) 在 server.py:163 附近临时打印（或加环境变量开关），看握手解析结果
#    打印: repr(resp.database), repr(resp.plugin_name), repr(resp.username), resp.capabilities
# 3) 客户端连接时显式带 database
python -c "import pymysql; c=pymysql.connect(host='127.0.0.1',port=8899,user='root',password='',database='mysql_native_password'.replace('mysql_native_password','default')); cur=c.cursor(); cur.execute('SELECT DATABASE()'); print(cur.fetchall()); cur.execute('SHOW DATABASES'); print(cur.fetchall())"
```
判据：若 `resp.database` 打印出 `mysql_native_password`，则**确认是握手偏移漂移**（修 `auth_len` 的读法，按 `CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA` 分支用长度编码整数）；若 `resp.database` 正常而 `SELECT DATABASE()` 仍错，则问题在回答路径（检查 `self.database` 是否被别处覆盖）。

### 1.2 `SHOW DATABASES` 硬编码（**安全可改，先做这个**）

- 位置：`lightdb/net/server.py:290-293` → `[("default",), ("information_schema",)]`。
- 要求：返回引擎**真实**支持的库集合。本引擎实际是**单库模型**（`Database` 绑定一个 data_dir；`executor.py:7465` 用 `s.get('database','default')` 拼 `Tables_in_*`），所以**如实返回它真正支持的集合**并在注释里写明依据；不要继续返回与实现无关的固定列表。
- 验收：进程内 `execute_sql` 与经服务端 pymysql 两条路径都贴出实际结果集 + 依据说明。

### 1.3 `SHOW COLUMNS` 的三项伪造

| 问题 | 位置 | 要求 |
|---|---|---|
| `Default` 恒为 `None` | `lightdb/net/server.py:752` | 取真实默认值（列定义里已有） |
| `Extra` 由主键伪造 | `server.py:753-754` | 只在确有对应概念时给值（如未来支持 `AUTO_INCREMENT`）；**没有就留空字符串，不要靠主键推断** |
| `Type` 抹掉长度并归一化（`VARCHAR(20)` → `varchar`） | `lightdb/sql/parser.py:1698-1711` `_normalize_type` | 保留长度/精度：`varchar(20)`、`decimal(10,2)`、`int` |

**待读位置**：`SHOW COLUMNS` 的分派在 `server.py:321-324`（`if upper.startswith("SHOW COLUMNS") ... self._show_columns(sql)`），**下一步先读 `_show_columns` 函数体**（约 30 行）再动手。

### 1.4 切库不校验 + 单库语义

- `server.py:217-220`：`USE <db>` 不校验就 `self.database = db_name`；
- 要求：`USE` 不存在的库 → **显式报错**（1049 风格）；若引擎不支持多库，就明确报「单库模式，不支持切换」，**不要静默忽略**；
- 附带：`SHOW TABLES` 已用 `Tables_in_{self.database}`（`server.py:300`）——**这一处不是硬编码**，不要"顺手改"。

### 1.5 验收口径（必须都做）

1. **两条路径**：进程内 `Executor.execute_sql` + 经真实服务端 pymysql（起 `--mode single --port 8899 --data-dir <临时目录>`，用完停掉并删目录）。
2. 贴**实际结果集/报错原文**（不是"应该会"）。
3. 新增锁测试（断言 `Type` 含长度、`Default` 真实、`SELECT DATABASE()` 非 `mysql_native_password`、`USE` 错误库报错）。
4. 若既有断言编码了这些伪造行为 → **逐字声明旧→新**（本会话已有先例：测试也会锁定错误契约）。

---

## §2 验证方法（务必继承，这是本会话最有价值的沉淀）

### 2.1 判据用「三件套」，**不要用全量失败总数**

- (a) **改动领域零失败**；
- (b) **干净 HEAD `worktree` 对照无净新增失败**（`git worktree add --detach _wt HEAD`，用完 `git worktree remove --force _wt` + `git worktree prune`）；
- (c) **最小复现脚本**（裸 Python 或单条 pytest）。

失败总数在 **33–41 之间抖动**，原因见 §2.3。把「80 还是 82」当回归指标会得出错误结论。

### 2.2 派子代理的铁律（本会话的惨痛教训）

- **禁止 `git stash`**：本会话两次事故都由它引发——一次把 `README.md` 从工作区删掉并留下半坏的 index；另一次 `stash push` 中途权限失败，把部分文件回退成 HEAD 并使 `docs/`、`tests/`、`lightdb/` 一度**全部不可写**（子代理随后自行 `git stash pop` + 逐文件 SHA256 对齐才恢复）。需要干净对照 → **用 `git worktree`**。
- **禁止让子代理跑全量套件**：本会话连续 4 次子代理失败，**多起都发生在"跑 baseline/全量"阶段**（全量一次 5–9 分钟，极耗资源）。把基线数字直接给它，只让它跑**相关测试文件**。
- **要求"实际输出"**，禁止只讲设计；**每轮由 Lead 独立验收**（本会话所有交付的验收都是我做的，且多次发现子代理自述与实际不符，例如 `test_union_all` 的"3 倍重复行"其实是测试自己的算术错误）。
- **切片要小**：一次一件事。

### 2.3 已登记的 flaky 与「测试期望写错」（E 类）

- flaky：`test_v3_batch2::test_row_format_performance`（无 warmup 的墙钟对比）、`test_v2_batch6::test_federated_governance`（**曾是真实缺陷**：策略 id 毫秒撞号，已修）、`test_batch6::test_where_after_join`（WinError 32）、`test_react_agent`、`test_dashboard_generation`（写 `dashboards/*.json` 被沙箱 ACL 拒）、`test_encoder_auto_select`。
- E 类 10 条（**测试期望/桩写错，不是引擎缺陷**）：`test_explain_join`（把 EXPLAIN 第 0 列 int `id` 当计划树文本）、`test_react_agent`（桩 `FakeConn` 漏 `get_tables`）、`test_end_to_end_diagnosis`（`import subprocess` 只在别的用例函数内 → NameError）、`test_query_builder_sql`、`test_er_relations`、`test_memory_analyzer_structure`、`test_catalog_basic`、`test_registry`、`test_decision_doc`、`test_route_comparison`。
- 完整清单见 `docs/FAILURE_TRIAGE.md` §9（39 行主表 + A 类最小复现 + flaky 5 次序列 + E 类依据）。

---

## §3 项目当前状态

### 已完成
- **阶段 0（止血）5/5**：向量化聚合 int32 溢出、`VectorizedFilter` 空操作（+ 额外发现的空批截断 16311→16317）、`CHECK` 约束真生效（双写路径 + 持久化 + 多行 UPDATE 整体校验）、策略 id 撞号（含 3 处 fail-open → fail-closed）、**CHECK 进备份/恢复路径** + 性能声明加真实状态说明。
- **阶段 1 的 1+2**：GUI 显示完整错误（错误码 + SQLSTATE + 完整报文、可复制、不截断）、快照/备份失败必须失败（`ok:False` + 失败清单，绝不允许「0 个 CREATE TABLE 但 ok:True」）。

### 待做（阶段 1 剩余）
3. `KILL`/`PROCESSLIST` 语义为真（`server.py:701-726` 空实现；`Command` 不复位、`Time`=连接年龄、`Info` 不清）
4. **元数据诚实化（见 §1）**
5. 错误码语义修正（`executor.py:403-417`：NOT NULL/类型错都报 1062、未知列报 1146 且报文说"表不存在"）
6. 门禁清理（E 类 10 条 + 2 条 flaky + GUI 离屏冒烟测试）

### 失败构成（第二次分诊 §9）
**A 真实缺陷 24 / B 平台环境 3 / C flaky 2 / E 测试期望写错 10 / D 0**。
最高价值 A 类：向量化聚合与过滤（已修）、`CHECK`（已修）、`GRANT`/权限未实现、`ALTER TABLE ADD COLUMN` 返回 `None`、分区表假成功、列存 `GROUP BY` 返回 0 行、`COUNT(DISTINCT)`/`BETWEEN` 不可用、标量函数注册 132 个仅 9 个可用。

### 路线图
`docs/review/04_ROADMAP.md`（阶段 0/1/2/3/4 + 优先级矩阵 + KPI + 「不做」清单）。
评估与总报告：`docs/review/00_SUMMARY.md`、`01_engine.md`、`02_studio.md`、`03_integration_and_perf.md`。

---

## §4 关键提交（回退锚点）

```
66afdb8  fix(studio): 错误必须可见、快照失败必须失败（阶段 1 的 1+2）
3296c7d  feat(backup)+docs: CHECK 进备份/恢复；性能声明加真实状态说明（阶段 0 收尾）
1e6793e  fix: 阶段 0 止血 —— 向量化溢出/过滤器空列/CHECK 约束/策略 id 撞号
9a24b8f  docs: 优化路线图
1b72f89  docs: 全面评估总报告 + 三份分报告
0ea7850  docs: 第二次失败分诊（§9）
06a423c  feat(pl): 存储程序支持循环/游标/读表/SIGNAL
e59e98d  feat(sql): CREATE FUNCTION
5acec4f  feat(replication): SHOW SLAVE STATUS 语义为真
a63b6a3  (原始快照)
```
整轮回退：`git checkout a63b6a3 -- .`

---

## §5 环境事实

- Windows + **Python 3.11.9**（`D:\Program Files\Python311`）；**PySide6 6.6.0、pymysql 2.2.8 已装**；`Cython` **未装**、**无任何 C 编译器**、PyPI 不可达。
- DSH 沙箱：工作区可写；**禁写工作区外**（GUI 启动要写 `~/.lightdb_studio` → 被拒，需普通终端或 `QT_QPA_PLATFORM=offscreen` 绕开）；**禁多进程命名管道**（`mp.Queue/Pipe/Pool` → `WinError 5`，11 条 skip 由此而来，是能力探测不是失败）；控制台 **GBK**（emoji 会炸，`tests/conftest.py` 已加 `errors='replace'`）。
- 服务端：`python -m lightdb.main --mode single --host 127.0.0.1 --port 8899 --data-dir <临时目录>`（多进程模式在本沙箱**起不来**）。
- GUI：**必须** `python -m lightdb_studio.app`（`python lightdb_studio\app.py` 会因相对导入报错）。

---

## §6 遗留物

- `tests/test_zz_probe_tmp.py`：72 字节空测试文件（收集 0 用例），**沙箱拒绝删除**（`Remove-Item`/`del`/`[IO.File]::Delete` 全部被拒）。请在普通终端执行一次：`Remove-Item D:\lightdb_project\tests\test_zz_probe_tmp.py -Force`。工具目录里有 `diagnose-windows-sandbox-acl` 技能可诊断并修复这类 ACL 拒绝。

---

## §7 本会话最有价值的教训（建议新会话沿用）

1. **测试也会锁定错误的契约**：本会话修掉 10 条 E 类（测试期望写错），其中 3 条是**我自己**在上一轮写下的（`test_union_all` 的算术、严格 GROUP BY 的旧契约、"函数体含 WHILE/游标必须报错"——后者与 MySQL 13.1.12 相反）。**测试红灯 ≠ 实现有错**，先解剖数据。
2. **本项目最常见的缺陷形态是「结构对、语义假」**：列结构对齐了但值恒 0（`SHOW SLAVE STATUS`）、复制按 5 列解包而事件是 6 列（一行都同步不了）、快照返回 `ok:True` 但 0 个建表语句、策略被覆盖、CHECK 声明成功但不校验。**发现一个就沿同一条路再扫一遍**。
3. **性能声明必须绑定可复现命令**：白皮书 12,000 QPS / 3.5M rows/s / 41x / 12.5x 在查询路径上都没有落点（OLAP/列存/Cython 未接入、源码 0 引用、编译产物 0 个）。实测是：逐行 1,501 rows/s、批量 1,356 rows/s（批量更慢）、点查 888 q/s（进程内）、8 客户端并发 −36%。
4. **「先探针、再改、再验证」**：凡是登录握手、存储布局这类核心路径，**先写探针确认真实行为**再动手（本会话因坚持这一点避免了两次误改）。
