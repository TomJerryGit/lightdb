# LightDB 引擎核心分析报告（存储 / SQL / 数据库层）

> 分析范围：`lightdb/storage`、`lightdb/db`、`lightdb/sql`、`lightdb/backup`、`lightdb/experiments`、`lightdb/tools`
> 分析时间：2026-10-19 · 分析师：engine-core（Team「全面分析」）
> 代码基线：工作区未提交改动版（`table.py` / `executor.py` / `parser.py` / `plan_executor.py` + 未跟踪 `tests/test_sql_gaps.py`）
> 标记约定：**【实测】**=本次运行脚本/命令得到；**【源码】**=读代码确认（附 `file:line`）；**【文档】**=仓库文档宣称；**【推断】**=由源码/实测外推，未直接验证。
> 环境说明：Windows + Python 3.11.9 + DSH workspace-write 沙箱。**沙箱子进程不允许写 `docs/analysis_2026-10-19/`**，故实测脚本/临时库放在工作区根 `_engine_probe/`（脚本保留，数据目录已清理）；报告由 write 工具直接写入本路径。实测命令与原始输出见文末「附录 A」。

---

## 一、架构现实与调用链

### 1.1 总体模型（单库、单进程内嵌引擎）

- **单库模型**：一个 `Database(data_dir)` = 一个数据目录 = 一个库，没有 `CREATE DATABASE` / schema 维度；`SHOW DATABASES` 只报真实单库 + 真实可查的 `information_schema`【源码 database.py:195-204, 385-401】【实测 probe1：`SHOW DATABASES` → `[('default',), ('information_schema',)]`】。
- **元数据**：`_meta.json`（列定义、`unique_indexes`、`checks`、`partition`、`pending_ddl`）+ `_stored.json`（存储程序）+ `_triggers.json`（触发器）+ `<表>.btroot`（BTree 根页号）+ `<表>.free`（空闲页登记）。`save_meta()` 用 `tmp.replace()` 原子替换【源码 database.py:353-381】。
- **表对象**：`Database.tables: Dict[str, Table]`；`Table` 持有 `BufferPool` + `BTree` + `WAL` + `LockManager` + `MVCCManager` 引用【源码 database.py:320-328, table.py:293-340】。
- **SQL 链路**：`parser.parse()`（4158 行手写正则+递归下降）→ `Executor.execute_sql()`/`execute()` → `_dispatch()` 按 `command` 反射到 `_cmd_*`（executor.py:659-698）→ 单表简单查询走存储层 `Table.select()` 快路径，含 JOIN/子查询/CTE/窗口/UNION/聚合表达式时走 `_select_advanced()`（executor.py:1976+）或 `optimizer._build_logical()` + `plan_executor.PlanExecutor`（plan_executor.py:154 起）【源码】。
- **备份**：`backup/backup.py`（129 行）调用 `sql/logical_dump.py`（1668 行）做逻辑导出/恢复，`tools/dump.py` 同源；**没有物理备份/增量备份**【源码：logical_dump 被 backup.py、executor.py、tools/dump.py 引用；backup 目录仅 2 文件 133 行】。
- **规模**（AST 脚本实测）：目标范围 **71 个 .py / 35,079 行**；按文件：`executor.py` 8226、`parser.py` 4581、`table.py` 4380、`logical_dump.py` 1668、`plan_executor.py` 1405、`database.py` 1170、`pl.py` 1124；storage 14 文件 2827 行【实测：附录 A-6】。

### 1.2 存储格式（实测证实）

- **页**：固定 4096 B，24 B 页头（`[0:4]`魔数 `LDBP` 大端、page_id、lsn、page_type、n_slots、next_row_ptr），槽位数组向下增长、行数据向上增长【源码 page.py:1-33, 84-115】。
  **实测 `acct.dat` 十六进制解析**（附录 A-3）：`page 0: magic=b'LDBP' type=0(meta)`、`page 1: type=2(BTREE_LEAF)`、`page 2: type=1(HEAP) n_slots=6 next_row_ptr=3980`；每行 = **21 B 行头**（`xmin/xmax/flags/dlen/prev_page/prev_slot`）+ **JSON 文本载荷**，例如 `slot 2: off=4038 len=29 xmin=3 xmax=0 prev=(0,0) payload=b'[1, 999]'`【实测】。
- **结论：生产行格式是「21 B MVCC 头 + JSON payload」**，不是 `storage/row_format.py` 的紧凑二进制格式。`row_format.py`/`row_format_v4.py` 只被实验性 `db/hybrid_table.py`、`db/table_v3.py` 和 Cython fallback 引用，而 `hybrid_table.py`/`table_v3.py` 自身 **0 引用（死代码）**【实测 A-6 引用统计】【源码 table.py:1462-1476】。
- **BTree**：仅 `int` 主键 → `(page, slot)` 最新版本指针；键用 `struct.pack('<I')`（uint32）编码【源码 btree.py:99,173,202】，根页号存 `<表>.btroot`（1 B）【源码 table.py:994-1042】。**没有二级索引 DDL**（`CREATE INDEX`/`DROP INDEX` → 1064）【实测 probe1-B】，所有非「int PK 等值」条件走全表 `_visible_pks` + Python `_match`（table.py:2055-2113）【源码】。
- **BTree 键为 uint32 的直接后果**：`VARCHAR` 主键表可以 `CREATE TABLE` 成功，但任何 `INSERT` 都在 BTree 打包处抛 `required argument is not an integer`（错误码 1105），表从此**不可写**；`SELECT` 返回空集【实测 probe2-4】。这是「建表成功、表不可用」的假成功，见风险 R2。

### 1.3 WAL / checkpoint / 恢复

- **WAL = JSON Lines 文本**（非二进制帧、**无 CRC/校验和**）：每行 `{"lsn":…,"txn_id":…,"type":"INSERT/UPDATE/DELETE/COMMIT/ABORT","data":{…}}`；`data` 内含 `old`/`old_b`（旧行镜像）【源码 wal.py:126-147, 189-211】。实测样本：`{"lsn": 13, "txn_id": 4294967287, "type": "COMMIT", "data": {}}`（合成 autocommit 事务号 0xFFFFFFF7）【实测 A-4】。
- **log-before-data**：`log()` 后默认 flush 到 OS 才改页；显式 `COMMIT`/`ABORT` 再 fsync；autocommit 只 flush 不 fsync（掉电可能丢最后几条，README/注释已诚实声明）【源码 wal.py:1-45, 213-288】【文档 table.py:80-83】。
- **checkpoint = 阈值触发 + 显式入口**：默认 1 MB / 5000 条（`wal_checkpoint_limits`），顺序「脏页 fsync → BTree 根 fsync → WAL truncate（LSN 单调）」；表上有未提交事务时**跳过**，长事务会让 WAL 无界增长【源码 table.py:4270-4348, database.py:1113-1132】。
- **崩溃恢复 = WAL 全量分析 + 幂等 redo/undo，无页 LSN 联动**：`Database.__init__` → `_recover()` → `Table.recover_from_wal()`（database.py:343-351, table.py:2381-2470）；恢复成功后才 truncate WAL【源码】。
  **实测通过**（附录 A-2）：子进程写 5 条 autocommit 行 + 1 个显式事务（INSERT+UPDATE）后 `os._exit(9)`；重开 → `redo 5 条, undo 1 条`，读出 `(1,10)…(5,50)`（未提交的 id=6 被撤销、id=1 的 999 被恢复成 10），`integrity_scan` 全绿，WAL 关闭后 0 字节【实测】。
- **WAL 静默丢记录的通道**：`WAL.__init__`/`read_all` 对解析失败行 `except Exception: pass` 静默跳过（wal.py:165-181, 311-324）。JSON 无校验和 → 中间某行损坏/半行截断时无法区分「日志末尾撕裂」与「已提交记录丢失」，恢复会**静默少 redo 一条已提交写**【源码/推断】，见风险 R5。

### 1.4 MVCC / 事务 / 锁

- **行级 MVCC，版本链持久化**：行头 21 B（xmin/xmax/prev_page/prev_slot）；UPDATE 不原地覆写（旧行 xmax 写入真实事务号，新行另写一槽并 `prev` 指回），DELETE 只写墓碑+保留索引项；重启后沿 `prev` 链可回溯旧版本【源码 table.py:53-78, 925-981】。
  **实测快照隔离成立**：会话 A `BEGIN` 后，会话 B autocommit `UPDATE id=1 → 999` 提交，A 再读仍见旧值 100；滚动/回滚、并行未提交不可见等 6 项全部符合预期【实测 probe2-1】。
- **但隔离级别是「假开关」**：`Database.begin(isolation)` 把 `isolation` 存进 txn dict 后**再也不用**；`_cmd_begin` 直接 `db.begin()`；显式事务内**只有一份 ReadView**（BEGIN 时创建，后续语句不刷新）→ 实际行为恒为快照隔离（≈REPEATABLE READ），而服务端/变量模块对外宣称 `transaction_isolation=READ-COMMITTED`【源码 database.py:901-910, executor.py:8068-8072, net/server.py:703-704, sql/variables.py:22-23】。`SET TRANSACTION ISOLATION LEVEL …` 本身不可解析（1064）【实测 probe1-E】。见风险 R7。
- **事务号单调性**：启动时扫全表堆页行头 + 全部 WAL 求最大事务号推进计数器（database.py:258-305）——**启动成本 O(数据页数 + WAL 长度)**，大库启动会明显变慢【源码/推断】。
- **COMMIT 是 O(表数) 的 fsync 循环**：`Database.commit()` 对 `self.tables.values()` **每一张表**都 `wal.commit()`（fsync）+ `flush()`（脏页/BTree fsync）+ `purge_undo`，**不论该表是否参与本事务**（database.py:912-928）；`abort()` 同理（961-985）。
  **实测**：2 张表的小库 `COMMIT` 657 ms/次；7 张表时 probe1 中 `COMMIT` 实测 2.0–4.3 s/次；无事务时 `COMMIT` 0.3 ms【实测 probe1-E, probe2-E】。在有 10+ 表的库上，显式事务提交会到秒级——这是「表越多越慢」的结构性缺陷，见风险 R4。
- **锁**：`storage/lock_manager.py`（295 行）行级锁 + `Table._lock`（每表 RLock）+ 事务结束时 `lock_manager.release(tid)`【源码 table.py:1678-1687, database.py:926】；没有死锁检测/等待图（全局串行化靠表 RLock）【源码/推断】。
- **`storage/txn.py::TxnManager` 是死代码**：`Database.__init__` 构造了它（database.py:206），但 `begin/commit/abort` 全部走 `MVCCManager`，全仓库无其它 `txn_manager` 使用点【实测 A-6 类似引用统计 + 源码】。
- **prepared transaction / 两阶段提交 / SAVEPOINT**：均无（SAVEPOINT/ROLLBACK TO 1064）【实测 probe1-E】。

### 1.5 DDL 的内部实现（ALTER / pending_ddl）

- `ALTER TABLE` 仅支持 `ADD COLUMN` / `DROP COLUMN` / `RENAME TO` + `TRUNCATE`；其余变体（MODIFY/CHANGE/ADD INDEX/ADD CHECK/ADD FK…）显式 1064 并给出支持清单，**不静默成功**【源码 executor.py:767-835】【实测 probe1-F】。
- 结构变更用持久化 `pending_ddl` 做「半套变更」崩溃收尾：`save_meta()` 是唯一原子提交点，重启后 `_recover_pending_ddl()` 补完剩余行（database.py:240-254, 837-897）；DROP COLUMN 用 `update_row_shrink` 清零尾部，保证旧列值**真的从 .dat 消失**【源码 page.py:117-157, database.py:667-743】。这块设计是扎实的。
- DDL 隐式提交 + `_ddl_guard`：表上有**别的**未提交事务时直接报错（无元数据锁）【源码 database.py:520-522, executor.py:779-835】。

---

## 二、SQL 能力真实性矩阵

> 全部结论来自 probe1/probe2/probe_ft 三个进程内 `Executor.execute_sql()` 实测（原始输出见附录 A-1/A-5），仅标注【文档】处为文档宣称。「显式报错」= 返回 `type=error` 且错误码/报文可读，不是静默错答。

| 能力 | 状态 | 证据 |
|---|---|---|
| `CREATE/DROP TABLE`、`IF [NOT] EXISTS`、主键/`UNIQUE`/`DEFAULT`/`NOT NULL` | ✅ 真实现 | 【实测】`IF NOT EXISTS` 幂等；重复建表 1105；唯一冲突 1062 |
| `ALTER TABLE` ADD/DROP COLUMN、RENAME、TRUNCATE | ✅ 真实现（其余变体显式 1064） | 【实测】ADD COLUMN 回填 4 个旧版本；TRUNCATE 保留定义 |
| `CREATE/DROP INDEX`（二级索引） | ❌ 未实现（1064） | 【实测】`CREATE INDEX idx_age ON u1(age)` → `无法解析 SQL`；`SHOW INDEX` 只有 PRIMARY/唯一校验项 |
| `EXPLAIN` | ⚠️ 假精确：恒 `type=ALL,key=None` | 【实测】即使 `WHERE id=1`（int PK）也报 ALL，而执行实际走 BTree 点查（table.py:2060-2074） |
| 外键 | ❌ 定义期显式拒绝（诚实，1064） | 【实测】报「本引擎不支持外键…请在应用层保证引用完整性」 |
| `CREATE VIEW` / `CREATE EVENT` / `CREATE/DROP TRIGGER` 的 DROP / `DROP PROCEDURE` | ❌ 不可解析（1064） | 【实测 probe1-C, probe2-2】触发器建后**无法用 SQL 删除** |
| `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` / `SET autocommit=0` / `SET TRANSACTION ISOLATION` | ❌ 不可解析（1064） | 【实测 probe1-E】 |
| `INSERT`（多值）、`UPDATE`、`DELETE`、`AUTO_INCREMENT` 补值 | ✅ 真实现 | 【实测】多值 INSERT 原子；触发器中 `INSERT INTO tlog(msg)` 自动补 id=1 |
| `UPDATE ... SET col = 表达式`（`b=b+1`） | ❌ 显式 1105「无法将 'b + 1' 转换为 INT」 | 【实测 probe1-G】 |
| `INSERT ... SELECT` / `REPLACE` / `INSERT IGNORE` / `ON DUPLICATE KEY` | ❌ 显式 1064/1136 | 【实测 probe1-G：只支持 VALUES 形式】 |
| `WHERE`：AND/OR/LIKE/比较/`BETWEEN`/`IN`/`NOT`/`IS [NOT] NULL`/括号 | ✅ 真实现，NULL 三值正确 | 【实测】`id NOT IN (1,NULL)` → 空集（UNKNOWN 不通过）；`id BETWEEN 2 AND 3` 正确 |
| `ORDER BY` / `LIMIT n [OFFSET m]` / `LIMIT m,n` | ✅ 真实现 | 【实测】两条 LIMIT 语法都正确偏移 |
| JOIN：INNER/LEFT/RIGHT/CROSS + 表别名 + 聚合 | ✅ 真实现 | 【实测】`LEFT JOIN` 保留未匹配行（None 填充），别名前缀列正确 |
| 子查询：`IN/NOT IN/EXISTS`（含相关、嵌套）、标量投影、`FROM (子查询) 别名` | ✅ 真实现 | 【实测】`EXISTS` 相关、`FROM (SELECT…) x WHERE` 均正确；多行标量子查询显式报错 |
| CTE：非递归 / `WITH RECURSIVE`（上限 1000） | ✅ 真实现 | 【实测】`1..4` 递归正确 |
| `UNION` / `UNION ALL` / `INTERSECT` / `EXCEPT` | ✅ 真实现（后两者非 MySQL 语法但支持） | 【实测】UNION 去重、UNION ALL 保留重复 |
| `GROUP BY`/`HAVING`/`DISTINCT`/聚合 `COUNT/SUM/AVG/MIN/MAX` | ✅ 真实现 | 【实测】HAVING 聚合谓词、DISTINCT 均正确 |
| 聚合 DISTINCT：`COUNT(DISTINCT x)`、`COUNT(DISTINCT a,b)`、`SUM/AVG/MIN/MAX(DISTINCT x)` | ✅ 真实现（**未提交改动**） | 【实测 probe1-A + 未提交测试 15/15 通过】 |
| 窗口函数：`ROW_NUMBER/RANK/SUM/COUNT/LAG/LEAD` + `PARTITION BY/ORDER BY`/窗口帧 | ⚠️ 基本可用但解析窄 | 【实测】裸窗函数正确；**带别名不加 AS**（`ROW_NUMBER() OVER (…) rn`）→ 1105「列不存在」 |
| 标量函数 | ⚠️ 白名单：LOWER/UPPER/LENGTH/TRIM/ABS/ROUND/CONCAT/JSON_EXTRACT/JSON_UNQUOTE | 【实测】`UPPER/LENGTH/CONCAT` 正确；`NOW()/CURRENT_DATE/CAST/CASE/JSON_OBJECT` 显式报错并列出白名单 |
| JSON 类型 / `JSON_EXTRACT` / `doc->'$.k'` | ✅ 真实现 | 【实测】`{"k":1}` → `1` |
| 全文索引 `CREATE FULLTEXT INDEX` + `WHERE MATCH(body) AGAINST('…')` | ✅ 真实现（但 `CREATE TABLE … FULLTEXT KEY` 定义期显式拒绝） | 【实测 probe_ft：建索引 ok，MATCH 返回 `[(1,'hello world')]`，`fulltext_search` TF-IDF 分值】；`SHOW INDEX` **不列**全文索引 |
| 生成列 `AS (expr) STORED/VIRTUAL` | ⚠️ 定义被接受，语义**假**：显式给值时不重算 | 【实测】`b INT AS (a*2) STORED`，`INSERT (1,5,9)` 后 `b=9`（应为 10） |
| 表级/列级 `CHECK` | ✅ 真实现（3819） | 【实测】`age=-1` → 3819 HY000 |
| `PARTITION BY RANGE` | ⚠️ 仅元数据 | 【实测】建表 ok、`SHOW PARTITIONS` 报 `LESS THAN (100)`；物理仍是单 `.dat`，无裁剪（源码 table.py:771-807 仅存 spec） |
| 存储程序：`CREATE PROCEDURE/FUNCTION`、`CALL`、函数内嵌 SELECT | ⚠️ 部分真实现 | 【实测】`CALL p1()` 真执行过程体 INSERT；存储函数 `SELECT f1(3)` → 6；但 **`CALL` 丢弃过程内 SELECT 结果集**（返回 OK）；`DROP PROCEDURE` 不可解析 |
| 触发器：BEFORE/AFTER INSERT 真执行（含 `SET NEW.x`） | ⚠️ 部分真实现 + 一个锁表缺陷 | 【实测】合法触发器体真触发；**CREATE 不校验体**：`SET @x=1` 之类不支持的体 CREATE 返回 ok，之后该表**所有 INSERT 都 1105 失败** |
| 事务：`BEGIN/COMMIT/ROLLBACK`、MVCC 快照、物理回滚 | ✅ 真实现 | 【实测 probe2-1】 |
| 隔离级别语义（RC/RR/RU/SER） | ❌ 只有一种快照语义，级别参数无效 | 【源码 database.py:901-910 + 实测行为】 |
| 类型约束（INT 范围、VARCHAR 长度、DATETIME 格式） | ❌ **完全不校验**（静默接受） | 【实测】INT 列收 `2147483648`、VARCHAR(3) 收 `'abcd'`、DATETIME 收 `'not-a-date'` 全部 ok 且原样返回；【源码】coerce 只做 `int()/float()/str()`（table.py:237-265） |
| 视图 / 事件 / 外键强制 / 二级索引 / 物理分区裁剪 | ❌ 不存在 | 见上 |

**一句话**：解析/执行层在过去多轮修复后已经能跑通「单表 + JOIN + 子查询 + CTE + UNION + 窗口 + 聚合 + JSON + 全文」这类常见分析查询；**短板集中在 DDL 周边（索引/视图/事件/触发器与过程的生命周期）与「声明式约束不生效」（类型/生成列）**。

---

## 三、数据正确性 / 完整性风险

### 3.1 已实测成立的正确性保障（比旧审计文档描述的好）

1. **崩溃恢复可用**：硬杀进程后已提交行全部 redo，未提交事务被 undo，`integrity_scan` 全绿，WAL 干净关闭后截断为 0【实测 A-2】。
2. **`ROLLBACK` 是真回滚**：物理 undo 栈把旧行写回原页原槽位；实测 UPDATE+DELETE 后 ROLLBACK 恢复原值【实测 probe2-1b；源码 table.py:2207-2380】。
3. **快照隔离在进程内成立**（含 autocommit 更新对老快照不可见）【实测 probe2-1】。
4. **`integrity_scan(deep=True)` 可用**，输出 `dangling_prev/free_pages/orphan_btree_pages/bad_magic_pages…` 一组硬指标【实测 probe1-H】。
5. **错误码语义**大体正确：1146 表不存在、1062 重复键、3819 CHECK、1049 库不存在、1064 语法/不支持、1136 列数不匹配；`CREATE INDEX`/`ALTER MODIFY` 等不支持形态都是显式错误而非假 OK【实测】。

### 3.2 仍存在的风险（按严重度）

- **R1（P1）声明式类型约束完全不生效**：INT/BIGINT 越界、VARCHAR(n) 超长、DATETIME 非法串全部静默入库并原样读回（实测 `2147483648`、`'abcd'`、`'not-a-date'`）。`coerce` 只在**部分**路径做 `int()/float()`，不做范围/长度/格式校验；写入路径也没有 MySQL 的截断/告警语义【实测 probe2-5】【源码 table.py:237-265】。后果：应用以为数据库在保护它，实际任何脏数据都能落盘，且之后比较/索引/聚合行为不可预期（`int` 与 `str` 混存 → `_match` 比较可能 TypeError 或错误排序）。这是本次在引擎层找到的**最高价值的「静默错答/损坏」通道**。
- **R2（P1）非整型主键 = 建表成功但表不可用**：`VARCHAR PRIMARY KEY` 建表 ok，`INSERT` 永远 1105 `required argument is not an integer`（BTree 键 `struct.pack('<I')`），`SELECT` 返回空集。属于「定义期假成功 + 运行期全损」【实测 probe2-4；源码 btree.py:99,173,202】。文档 table.py:94 只承认「非整型主键不建索引」，实际比这严重。
- **R3（P1）触发器定义期不校验体，可把表「锁死」**：`CREATE TRIGGER … SET @x=1` 返回 `Trigger created`，此后该表**任何 INSERT 都 1105**（触发器体在运行时才解析），且 `DROP TRIGGER` 不可解析 → 用户无法再用 SQL 自救【实测 probe1-C】。`CREATE TRIGGER`/`_cmd_create_trigger`（executor.py:6777）没有做「体可执行性」校验。
- **R4（P2，性能/可用性）COMMIT 是 O(表数) fsync 循环**：提交/回滚对库内**所有表**逐一 `wal.commit()`（fsync）+ `flush()`，与事务是否涉及无关；实测 2 表 657 ms、7 表 2.0–4.3 s/次，空提交 0.3 ms【实测；源码 database.py:912-928】。表多了之后显式事务不可用，且长事务期间其它表也被迫刷盘。
- **R5（P2）WAL 无校验和 + 解析失败静默跳过**：JSON Lines 文本无 CRC，`except Exception: pass` 使损坏/半截行被静默忽略（wal.py:165-181, 311-324）。无法区分「尾部撕裂（安全）」和「中间损坏/丢已提交记录（数据丢失）」。建议至少给行加 `crc32` 并在恢复时对非尾行损坏**显式报错**。
- **R6（P2）「静默错答」通道仍有残留**：存储层 `_match` 对不认识的 `op` 直接 `return False`（table.py:2084-2113）→ 只要解析/路由层漏一条新语法到存储层，结果就是**静默空集**而不是报错。未提交的 BETWEEN 改动正是靠「三条路径都接 expr_pred」规避此坑；但这是纪律而非约束（无断言兜底）。另：`SELECT 1 IN ('1')` 投影位置仍 1105 报「列不存在」（显式，但能力矩阵缺口）【实测 probe1-A】。
- **R7（P2）隔离级别谎报**：对外 READ-COMMITTED，实际恒快照（见 1.4）；`SET … ISOLATION` 又不可解析【实测+源码】。
- **R8（P2）EXPLAIN 恒 ALL**：与真实执行（int PK 等值走 BTree 点查）不一致，监控/调优工具会被误导；`SHOW INDEX` 不列全文索引，`SHOW CREATE TABLE` 给 PK 列标 `NULL`（应为 NOT NULL）【实测 probe1-B/F、probe_ft】。
- **R9（P2）生成列语义假**：显式给生成列赋值被接受且不重算（`b=9` 而非 10）【实测 probe2-B】；VIRTUAL/STORED 均在写入时按「用户给的值」存。
- **R10（P3）`LIMIT -1` 静默当作无限制**（MySQL 报语法错误）【实测 probe2-5】。
- **R11（P3）维护操作与 DDL 慢**：空表 `TRUNCATE`/`ADD COLUMN` 2.3–2.6 s，7 表小库 `vacuum` 4.56 s / `checkpoint(force)` 4.18 s / `shutdown` 4.04 s，每表固定 3 页 12 KB（空表也占 3 页）【实测 probe1-F, probe2-7】。注意本机 fsync 在沙箱下偏慢，绝对值仅供参考；但「O(表数)/O(页数) + 全量扫描」的结构性成本是确定的【源码】。
- **R12（P3）每表固定文件开销**：`.dat` 3 页 + `.btroot` + `.wal`；小表多时磁盘/句柄开销线性增长；无表空间/压缩。

（未发现 P0 级：本轮未复现「崩溃丢已提交数据」「建表必失败」「错误假成功」这三类旧 P0；probe 中所有失败路径都返回了可读错误码。）

---

## 四、代码质量量化

以下为 AST 脚本（附录 A-6）对目标 6 个目录 71 个文件的统计：

| 指标 | 数值 | 说明 |
|---|---|---|
| 总行数 / 文件数 | **35,079 行 / 71 文件** | 任务说「约 4 万行」，基本一致 |
| 最大文件 | `sql/executor.py` **8226 行**、`sql/parser.py` **4581**、`db/table.py` **4380**、`sql/logical_dump.py` 1668、`sql/plan_executor.py` 1405 | 三大文件占 49% |
| 函数总数 | 1471 | |
| 超长函数 | **>100 行 33 个；>200 行 7 个** | 最长：`table.py:3612 _relocate_forward` 334 行、`parser.py:1193 _where` 329 行、`executor.py:1047 _cmd_select` 246、`parser.py:945 _select` 245、`table.py:3073 vacuum` 212 |
| 异常处理 | `except` 331 个；单语句 `pass` 69 个，其中 **吞 `Exception/BaseException` 36 处** | 大部分在清理/关闭路径（可接受），但 WAL 解析吞异常属数据风险（R5） |
| `global` 语句 | 1 | 无大面积全局状态（好） |
| TODO/FIXME | 1 | 反直觉地少 |
| 「静默」注释 | 306 处；「未实现/不支持」146 处 | 代码里大量「诚实化」注释，说明历轮审计把「不许静默」写进了规范 |
| 完全一致的重复函数体（≥12 行）| **0 组** | 没有复制粘贴级重复 |
| 死模块（同包 0 引用） | `db/hybrid_table.py`、`db/table_v3.py`、`storage/btree_optimized.py`、`sql/parser_v3b4.py`、`sql/predicate_v4.py`、`sql/icp.py`、`sql/cte.py`、`storage/txn.py`（TxnManager） | 约 500+ 行；`storage/row_format.py` 仅被实验/Cython fallback 引用（生产行格式是 JSON） |

**质量判断**：
- **重复不是主要问题**，**同构双路径**才是：`executor._eval_condition`（177 行，executor.py:2677）与 `plan_executor._eval_condition`（172 行，plan_executor.py:154）两套谓词求值；SELECT 有「单表快路径 / `_select_advanced` / 优化器计划路径」三条。每次补一个语法（如 BETWEEN、DISTINCT）都要在 2–3 处同时接线，**能力漂移**是常态——本次实测就撞到同一语义在单表路径能跑、投影位置仍报错（`1 IN ('1')`），窗口别名在一条路径失败。
- **超长函数集中在解析器和存储核心**（`_where` 329 行、`_relocate_forward` 334 行、`vacuum` 212 行），可维护性差，但注释密度高、边界写得很细，属于「难读但不糊涂」。
- **死代码/实验模块多**（hybrid_table/table_v3/row_format/btree_optimized/parser_v3b4/icp/predicate_v4/cte/txn），建议一次性清理或显式标注 `EXPERIMENTAL`，否则读者会误以为存在「二进制行格式 / 页预取 / ICP / CTE 模块」能力。

---

## 五、未提交改动评估（4 文件 +459/−20，另新增 `tests/test_sql_gaps.py`）

**改动意图**：补齐 3 类此前**不可用或会静默错答**的 SQL 语义（对应 README/审计中「BETWEEN / COUNT(DISTINCT) / IN NULL 三值」缺口）：

1. `parser.py`：新增 `_parse_between_predicate` + `_find_top_level_word`、`_between_expr()`（标量表达式内 BETWEEN 优先级最低）；`_split_top_level` 识别 BETWEEN 的结构性 AND 不误切；`_ExprParser` 支持聚合 `DISTINCT` 修饰（`COUNT(DISTINCT x[, y])`、`SUM/AVG/MIN/MAX(DISTINCT x)`）；非列左值 `IN (v1,v2)` 编译成 `expr_pred`；新增 `eval_between_truth`/`eval_in_list_truth`/`distinct_sql_values` 三值求值；`expr_*` 系列遍历函数补齐 between/values 位置。
2. `executor.py`：投影含 BETWEEN 时路由到逐行表达式求值（`_projection_has_between`）；聚合/属性收集补齐 `low/high/values`，避免 HAVING/列检查漏算。
3. `plan_executor.py`：计划路径的投影与函数检查补齐 BETWEEN。
4. `table.py`：存储层 `_aggregate` 遇到 `FN(DISTINCT …)` **显式抛错**（防止被绕过执行器时把 COUNT(DISTINCT) 退化成 COUNT）。
5. 新增 `tests/test_sql_gaps.py`：15 个测试锁死 BETWEEN 四位置/三值/NULL 边界、COUNT/SUM DISTINCT、空 IN 列表、IN/NOT IN NULL 三值、JOIN 计划路径不静默返回 0。

**完成度与风险**：

- 【实测】**测试通过**：`python -m pytest tests/test_sql_gaps.py -q` → **15 passed in 39.74s**（exit 0）。
- 【实测】BETWEEN（WHERE/投影）、`COUNT(DISTINCT)`、字面量左值 IN 在 WHERE 均正确；`NOT IN (1,NULL)` 空集语义正确。
- ⚠️ **仍有覆盖缺口**：`SELECT 1 IN ('1')`（**投影位置**的 IN 列表）实测返回 1105「列不存在」——同一类语义在 WHERE 修好了、投影没接（本地测试只测 WHERE/HAVING/ON 三处）。建议补 `SELECT` 投影 + ORDER BY 位置两条回归。
- ⚠️ **提交状态**：4 个核心文件 +459 行改动**未提交**、无 commit message；`git diff` 中 4 处文件互相依赖（parser 新增 helper 被 executor/plan_executor/table 引用），单独回退任一会 ImportError；建议作为**一个原子 commit** 提交（附测试），不要拆。
- ⚠️ **测试侧**：`tests/test_sql_gaps.py` 用仓库根固定目录 `data_sql_gaps_test`（clean 时 rmtree），并行/CRASH 残留可能导致 flaky；且 15 条用例 39.7 s，说明插入路径 I/O 成本高（与 R4/R11 一致）。
- 结论：**改动质量高、目标明确、有红线测试，方向正确；未提交 + 投影缺口是主要风险**。不修改源码的前提下，建议 Lead 将其作为「已完成待提交」项记录，并单独跟踪 `1 IN ('1')` 投影缺口。

---

## 六、与文档宣称的差异

### 6.1 README「本次会话已修复」清单（`README.md` 顶部 2026-10 实测状态）

**仍成立（本轮独立复证）**：建表可用（buffer_pool 先创建文件）、错误不再伪装 OK、服务端错误码透传、GROUP BY/DISTINCT/JOIN 别名/JOIN 聚合/子查询/HAVING/LIMIT OFFSET/递归 CTE/窗口帧/相关子查询/投影标量子查询、UPDATE/DELETE 旧版本落盘与跨重启老快照、Cython 仅回退未编译。这些在 probe 中都得到印证【实测】。

**README 未覆盖、本轮新发现（文档遗漏）**：非整型主键表不可写；触发器定义期不校验体可锁表且不可 DROP；生成列不重算；类型/长度/格式不校验；无二级索引且 EXPLAIN 假精确；`CALL` 丢弃结果集；`DROP PROCEDURE/TRIGGER/VIEW/EVENT/INDEX` 不可解析；COMMIT O(表数) fsync；WAL 无校验和。

### 6.2 历史审计文档（`docs/AUDIT_REPORT.md` 71 KB、`docs/review/01_engine.md`、`docs/FAILURE_TRIAGE.md`）

- **已过时（本轮或 README 之后已修复）**：§16.3「仍未支持」清单（IN/NOT IN/EXISTS 子查询、HAVING、LIMIT OFFSET、递归 CTE、窗口帧）——全部实测可用；§17.3「旧版本只存在 WAL/内存、重启后老快照查不回」——21 B 行头 + prev 链落盘已实现，README 与 tests/test_mvcc_persistence.py 有验证，本轮第 3.1 节亦间接验证崩溃恢复；§22.4「两处静默错答（投影标量子查询返回 NULL、同名表遮蔽）」——实测投影标量子查询返回正确值，遮蔽检测已存在（executor.py 的 `_ambiguous_outer_ref`）。
- **仍成立（本轮复现）**：§24.6/§25.5 的「仍显式报错」边界（JOIN 外层 WHERE/HAVING 标量子查询、多行/多列标量子查询、复合 JOIN ON 等）；SQL 层 `plan_executor.SeqScan` 不走 prev 链（table.py:100-103 自述）；不 vacuum 空间只增不减；旧 `.dat` 不兼容。
- **仍夸大/从未核实**：`docs/PERFORMANCE_WHITEPAPER_*` 的 QPS / rows-s / 加速比数字与 `tests/regression_v4/test_performance_v4.py` 的 12000/80000 QPS 目标，**从无仓库内基准产物**；README 已自行把它们降级为「设计目标」，本报告不再采信【文档+推断】。`docs/review/01_engine.md` 的实测基线（1501 rows/s 等）是旧轮数据，本轮环境与实现均不同，未复测（另一成员负责基准）。
- **宣称 vs 实现的具体差距**：文档/架构图里的「B+Tree 索引、MVCC、列存/向量化、页式二进制行格式」——引擎真实只有 **int PK 单键 BTree + JSON 行载荷 + 行级 MVCC**；OLAP/列存/Cython 已由 README 指认为「未接线」，与本次观察一致【文档+源码】。服务端宣称 `READ-COMMITTED` 与实现不符（R7）。

---

## 七、风险清单与优先级（P0–P3）

> P0 = 会静默丢/错数据且用户无从察觉；P1 = 明确的数据/可用性缺陷，有显式错误但影响面大；P2 = 性能/语义偏差/误导性元数据；P3 = 体验/卫生问题。

| 编号 | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| — | — | 本轮未发现 P0 级（旧 P0：认证恒失败/建表必失败/错误假成功 均已修复） | 【实测】probe1/2 | — |
| R1 | **P1** | 声明式类型约束不生效：INT 越界、VARCHAR 超长、DATETIME 非法串静默入库；混存类型后续比较不可预期 | 【实测 probe2-5】【源码 table.py:237-265】 | 写入路径强制范围/长度/格式校验，违规返回 1264/1406/1292 或按 MySQL 截断+warning；至少不得静默 |
| R2 | **P1** | 非整型主键：CREATE ok → INSERT 永久 1105，SELECT 空集（BTree 键 uint32 打包） | 【实测 probe2-4】【源码 btree.py:99,173,202】 | 定义期显式拒绝非整型 PK（1064/1235），或扩展 BTree 键编码为变长字节 |
| R3 | **P1** | 触发器定义期不校验体：CREATE ok 后该表所有写失败且无法 DROP | 【实测 probe1-C】【源码 executor.py:6777, 7935-7955】 | CREATE 时就地解析/试执行体，失败建表即报错；补 `DROP TRIGGER` |
| R4 | **P2** | COMMIT/ROLLBACK 对全库所有表逐个 fsync，O(表数)：2 表 657 ms，7 表 2–4 s/次 | 【实测 probe1-E, probe2-1】【源码 database.py:912-928】 | 只对参与事务/有脏页的表提交；或每库单 WAL 文件 |
| R5 | **P2** | WAL 无校验和 + 解析失败静默跳过，无法区分撕裂尾部与中间损坏 | 【源码 wal.py:165-181,311-324】【实测 A-4 文本格式】 | 行级 crc32；非尾行损坏恢复时显式报错；二进制帧替代 JSON |
| R6 | **P2** | 存储层 `_match` 未知 op 静默 `False`；新增语法依赖路由纪律 | 【源码 table.py:2084-2113】【实测：`1 IN ('1')` 投影仍 1105】 | 存储层未知 op 抛错；补投影位置 IN/表达式回归 |
| R7 | **P2** | 隔离级别谎报：对外 READ-COMMITTED，实际恒单快照；`SET … ISOLATION` 不可解析 | 【源码 database.py:901-910, net/server.py:703, sql/variables.py:22】【实测】 | 要么实现 RC 每语句新 ReadView，要么对外如实报 REPEATABLE-READ |
| R8 | **P2** | EXPLAIN 恒 `ALL/key=None`（int PK 等值也如此），与真实执行不符；SHOW INDEX 不列全文索引；SHOW CREATE TABLE 把 PK 标成 NULL | 【实测 probe1-B/F, probe_explain, probe_ft】 | EXPLAIN 读真实访问路径；补齐元数据 |
| R9 | **P2** | 生成列不重算：显式赋值被原样存储；VIRTUAL/STORED 语义相同 | 【实测 probe2-B】【源码 table.py:744-770】 | 写入路径忽略/校验生成列输入，按表达式计算 |
| R10 | P3 | `LIMIT -1` 静默当无限制；`CREATE TABLE … FULLTEXT KEY` 只在建表语句里被拒绝（需独立 DDL） | 【实测 probe2-5, probe1-D】 | 负 LIMIT 报 1064 |
| R11 | P3 | 维护操作慢且 O(表数/页数)：7 表小库 vacuum 4.56 s、checkpoint 4.18 s、shutdown 4.04 s；空表也占 3 页 12 KB | 【实测 probe2-7】【源码 table.py:3073,4270】 | 增量/懒检查点；按需建页 |
| R12 | P3 | 死模块与实验模块混在生产目录（hybrid_table/table_v3/row_format/btree_optimized/parser_v3b4/icp/predicate_v4/cte/TxnManager） | 【实测 A-6】 | 清理或迁移 `experimental/` |
| R13 | P3 | 未提交改动未形成 commit；`1 IN ('1')` 投影缺口 | 【实测 git status/diff, pytest 15 passed】 | 原子提交 + 补 2 条投影回归 |
| R14 | P3 | `CALL` 丢弃过程内 SELECT 结果集（返回 OK 无结果） | 【实测 probe2-2b】 | 收集并返回多个 resultset，或显式报「不支持结果集」 |

**投入建议（供 Lead 汇总判断）**：解析/执行层已经过了多轮修复，常见 SELECT/DML 语义可用、崩溃恢复可用、错误码可用——**引擎底座是真实的**，不是「PPT 数据库」。但决定它能否被信任继续投入的三个硬伤是：**R1 类型不校验（数据正确性）、R2 非整型 PK 不可用（DDL 假成功）、R3 触发器可锁表（定义期不校验）**；其次是 **R4 提交 O(表数)** 在稍大的 schema 上会让显式事务变得不可用。建议优先级：R1/R2/R3 先做（都是小改动、收益直接），R4/R5 次之（涉及 WAL/事务结构），R6–R9 作为一致性收尾，R10–R14 顺手即可。

---

## 附录 A：实测命令与原始输出（关键片段）

**A-1** probe1（功能矩阵，进程内 `Executor` API；脚本 `docs/analysis_2026-10-19/tmp/engine-core/probe1.py`，因沙箱限制实际副本在 `_engine_probe/probe1.py`；数据目录 `_engine_probe/data1`，已清理）：

```
[CREATE TABLE t1 (...)] type=ok msg=Table 't1' created
[INSERT INTO t1 VALUES (1,'x',10),(2,'y',20),(3,'z',30)] type=ok affected=3
[INSERT INTO t1 VALUES (1,2,3)] type=error code=1062 msg=主键重复: 1
[SELECT id, SUM(b) OVER (PARTITION BY a ORDER BY id) FROM t1] type=resultset rows=[(1,10),(2,20),(3,30),(5,3)]
[SELECT id, ROW_NUMBER() OVER (ORDER BY id) rn FROM t1] type=error code=1105 msg=列 'ROW_NUMBER() OVER (ORDER BY id) rn' 不存在
[SELECT id BETWEEN 2 AND 3 AS f FROM t1] type=resultset rows=[(0,),(1,),(1,),(0,)]
[SELECT 1 IN ('1')] type=error code=1105 msg=列 '1 IN ('1')' 不存在
[CREATE INDEX idx_age ON u1 (age)] type=error code=1064 msg=无法解析 SQL
[CREATE TABLE child (..., FOREIGN KEY ...)] type=error code=1064 msg=本引擎不支持外键…显式拒绝
[CREATE VIEW v1 AS SELECT ...] type=error code=1064 msg=无法解析 SQL
[SELECT JSON_EXTRACT(doc, '$.k') FROM js] type=resultset rows=[(1,)]
[SELECT id FROM t1 INTERSECT ...] type=resultset rows=[(1,)]
[CREATE TRIGGER trg1 BEFORE INSERT ON t1 FOR EACH ROW SET @x = 1] type=ok msg=Trigger 'trg1' created
[INSERT INTO t1 VALUES (9,'w',90)] type=error code=1105 msg=触发器 trg1 里的语句无法解析: SET @x = 1
[UPDATE t1 SET b = b + 1 WHERE id = 1] type=error code=1105 msg=无法将 'b + 1' 转换为 INT
[SELECT CASE WHEN ...] type=error code=1054 msg=列 CASE ... 不存在
[SELECT NOW(), ...] type=error code=1105 msg=不支持的函数 'NOW'…
[SELECT * FROM t1 LIMIT 2 OFFSET 1] type=resultset rows=[(2,'y',20),(3,'z',30)]
[EXPLAIN SELECT * FROM t1] type=resultset rows=[(1,'SIMPLE','t1','ALL',None,0,None)]
integrity_scan={'table':'t1',...,'dangling_prev':[],'bad_magic_pages':[],'index_entries':4,'chain_versions':4}
```

**A-2** 崩溃恢复（`_engine_probe/crash_test.py`，子进程 `os._exit(9)`）：

```
child: committed 5 rows + uncommitted txn, now hard exit   (rc=9)
[INFO] 崩溃恢复 ct: redo 5 条, undo 1 条
rows after recovery: [(1,10),(2,20),(3,30),(4,40),(5,50)]     ← id=6 未提交插入被撤销；id=1 的 999 恢复为 10
integrity: dangling_prev=[], bad_magic_pages=[], live_rows=5
wal size after clean shutdown: 0
```

**A-3** 布局 dump（`acct.dat`，4 行；21 B 行头 + JSON）：

```
page 0: magic=b'LDBP' pid=0 type=0(meta) n_slots=0 next_row_ptr=4096
page 1: magic=b'LDBP' pid=1 type=2(BTREE_LEAF) n_slots=0 next_row_ptr=4096
page 2: magic=b'LDBP' pid=2 type=1(HEAP) n_slots=6 next_row_ptr=3980
  slot 2: off=4038 len=29 xmin=3 xmax=0 dlen=8 prev=(0,0) payload=b'[1, 999]'
  slot 5: off=3980 len=29 xmin=4294967284 xmax=0 payload=b'[100, 1]'   ← 合成 autocommit 事务号
```

**A-4** WAL 样本（JSON Lines，合成事务号）：

```
{"lsn": 13, "txn_id": 4294967287, "type": "COMMIT", "data": {}}
{"lsn": 14, "txn_id": 4294967286, "type": "COMMIT", "data": {}}
```

**A-5** probe2（事务/触发器/窗口/类型/基准）与 probe_ft（全文）、probe_explain 关键输出：

```
[b autocommit update] type=ok aff=1
[a old snapshot should see 100?] rows=[(1,100),(2,200)]        ← 快照隔离成立
[ROLLBACK] → rows=[(1,999),(2,200)]                             ← 真回滚（999 是更早 autocommit 的更新）
[CREATE TRIGGER tri AFTER INSERT ... INSERT INTO tlog ...] ok；[INSERT INTO tt VALUES (1,10)] ok；[SELECT * FROM tlog] rows=[(1,'ins')]
[DROP TRIGGER tri] → 1064 无法解析 SQL
[CALL p1()]（体含 INSERT）→ ok 且 plog 有行；[CALL p2()]（体含 SELECT 1）→ ok 无结果集
[SELECT f1(3)] → [(6,)]
[SELECT id, RANK() OVER (ORDER BY v) FROM acct] rows=[(1,3),(2,1),(3,2)]
[CREATE TABLE vp (name VARCHAR(20) PRIMARY KEY, n INT)] ok；[INSERT INTO vp VALUES ('a',1)] → error 1105 required argument is not an integer
[INSERT INTO ty VALUES (1, 2147483648, 'abcd', 9223372036854775808, 1.5, '2020-01-01 00:00:00')] → ok（越界/超长照收）
[SELECT id FROM ty LIMIT -1] → 返回全部行
bench: SELECT point 2.5 ms/op, UPDATE 20.5 ms/op, SELECT full 9.4 ms/op, BEGIN 0.4 ms, COMMIT(无事务) 0.3 ms
COMMIT（2 表，有未提交/已提交事务）657 ms/次；7 表时 probe1 记录 2.0–4.3 s/次
vacuum=1 (4.56s)；checkpoint=7 (4.18s)；shutdown (4.04s)；崩溃恢复后 WAL=0
CREATE FULLTEXT INDEX ... ok；SELECT ... MATCH(body) AGAINST ('hello') → [(1,'hello world')]
EXPLAIN SELECT * FROM e WHERE id = 1 → [(1,'SIMPLE','e','ALL',None,0,'Using where')]   ← int PK 等值也报 ALL
```

**A-6** `_engine_probe/code_quality.py`（AST 统计）关键输出见第四节；引用统计显示 `db/hybrid_table.py`、`db/table_v3.py`、`storage/btree_optimized.py`、`sql/parser_v3b4.py`、`sql/predicate_v4.py`、`sql/icp.py`、`sql/cte.py` 同包引用数均为 0；`storage/row_format.py` 仅被 `cython/fallback/row_codec.py`、`db/hybrid_table.py`、`db/table_v3.py`、`storage/__init__.py`、`storage/row_format_v4.py` 引用（均非生产 SQL 路径）。

**A-7** 未提交改动回归：`python -m pytest tests/test_sql_gaps.py -q` → `15 passed in 39.74s`（exit 0）。

> 证据脚本与原始输出保留在 `_engine_probe/`（`probe1_stdout.txt`、`probe2_out.txt`、`crash_out.txt`、`code_quality.py`、`probe_ft.py`、`probe_explain.py` 等），临时数据目录已删除；`docs/analysis_2026-10-19/tmp/engine-core/` 下保留 `probe1.py`（同沙箱原因，pwsh 无法在该目录建子目录/写文件，故实验副本放在工作区根）。
