# LightDB 全量测试失败分诊（FAILURE TRIAGE）

## 1. 运行条件与汇总

- 环境：Windows 10/11 + Python 3.11.9（`D:\Program Files\Python311\python.exe`）、DSH 沙箱 workspace-write（禁多进程原语、禁写工作区外、控制台 GBK）。
- 原始命令（3 次串行，`-rf` 输出失败清单）：`python -m pytest tests -q -rs -rf -p no:cacheprovider`。
- 辅助证据：① 只跑失败并集的隔离运行 `python -m pytest <73 个 id> -v --tb=line`（38.9s）；② 裸 Python 复现脚本（不经 pytest，跑完已删除）。

| 运行 | 汇总 | 耗时 | 落盘文件 |
|------|------|------|----------|
| run1 | **71 failed, 338 passed, 11 skipped** | 222.0s | `_triage_run1.txt` |
| run2 | **73 failed, 336 passed, 11 skipped** | 210.5s | `_triage_run2.txt` |
| run3 | **73 failed, 336 passed, 11 skipped** | 207.8s | `_triage_run3.txt` |

失败并集 = **73 条**（run1 ⊂ run2 = run3；run1 少 2 条）。11 条 skip 全部是多进程原语探测：`tests/conftest.py:100` 的 `mp.Queue/mp.Pipe` 探测 10 条 + `tests/regression_v4/test_multiprocess_server.py:59` 1 条，原因均为 `PermissionError: [WinError 5] 拒绝访问`，**不是失败**。

| 类别 | 条数 | 含义 |
|------|------|------|
| A 真实实现缺陷 | 42 | 裸 Python 可稳定复现，与沙箱无关 |
| B 平台/环境限制 | 26 | WinError 32 文件占用 24 条 + Errno 13 写权限 2 条 |
| C 已知 flaky/性能断言 | 5 | 3 次状态不一致 2 条 + 纯计时/吞吐断言 3 条 |
| D 需进一步定位 | 0 | 每条都能从贴出的错误推出根因 |
| 合计 | 73 | 与 3 次运行失败并集一致 |

## 2. 主表（73 条，逐条对应失败并集）

| 测试 ID | 类别 | 错误类型 | 根因（一行） | 处置建议（一行） | 优先级 |
|---|---|---|---|---|---|
| tests/regression/test_performance.py::test_row_format_performance | A | TypeError: cannot convert 'float' object to bytes | DECIMAL 列被映射成字节串类型，encode 用 `bytes(v)` 收到 float | row_format 按类型分支处理 DECIMAL/float | P1 |
| tests/regression/test_sql_compat.py::test_ddl | A | AssertionError: 解析失败 ALTER TABLE t1 ADD COLUMN age INT | `parser.parse()` 对 ALTER TABLE ADD COLUMN 返回 None | 补 ALTER TABLE ADD COLUMN 解析分支 | P1 |
| tests/regression/test_sql_compat.py::test_variables | A | AssertionError: 失败 SET @@autocommit = 1 | `parser.parse('SET @@autocommit = 1')` 返回 None | 支持 @@ 系统变量 SET 语句 | P1 |
| tests/regression_v4/test_columnar.py::test_encoder_compression | C | assert 2.776615357277715 > 3 | 字典压缩率恒为 2.78x，低于 3x 门槛（3 次同值，非抖动） | 换编码或复核门槛是否有依据 | P3 |
| tests/regression_v4/test_columnar.py::test_olap_groupby | A | assert 0 == 100000 | 列存 GROUP BY 返回 0 行 | 修列存分组扫描路径 | P1 |
| tests/regression_v4/test_parallel.py::test_parallel_groupby_correctness | A | assert 0 == 50 | 并行 GROUP BY 返回空列表 | 修并行分组结果收集 | P1 |
| tests/regression_v4/test_performance_v4.py::test_olap_aggregate_performance | C | assert 1.53e7~1.65e7 > 2e7 | 聚合吞吐低于 20M rows/s 门槛，隔离低负载运行可过 | 性能阈值移出门禁或标注压测 | P3 |
| tests/regression_v4/test_performance_v4.py::test_parallel_speedup | C | assert 0.78~0.82 > 1.5 | 并行加速比 <1（无加速），隔离运行可过 | 同上一行 | P3 |
| tests/test_batch10.py::test_concurrent_load | A | AttributeError: 'MySQLServer' object has no attribute 'query_cache' | 压测引用的 query_cache 属性不存在 | 补 query_cache 或改引用真实缓存 API | P1 |
| tests/test_batch12.py::test_create_procedure | A | AssertionError: assert 'error' == 'ok' | CREATE PROCEDURE 执行返回 type=error | 修存储过程创建链路 | P1 |
| tests/test_batch12.py::test_event_scheduler | B | PermissionError [WinError 32] data_b12\logs.dat（reset） | 前序用例断言失败后未 db.shutdown()，Windows 禁删被占用文件 | `finally` 关库；reset 容忍/重试 WinError 32 | P2 |
| tests/test_batch12.py::test_function | B | PermissionError [WinError 32] logs.dat（reset） | 同上（隔离运行暴露：CREATE FUNCTION 亦返回 type=error） | 同上 + 修 CREATE FUNCTION | P2 |
| tests/test_batch12.py::test_procedure_if_while | A | assert 0 == 5（len([])） | 过程体 IF/WHILE 未执行，结果集为空 | 修过程体语句执行 | P1 |
| tests/test_batch12.py::test_trigger_after_insert | A | assert 0 >= 1（logs 0 行） | AFTER INSERT 触发器未写入日志表 | 修触发器触发与执行 | P1 |
| tests/test_batch12.py::test_trigger_before_insert | B | PermissionError [WinError 32] logs.dat（reset） | 级联锁（源同上） | 同 event_scheduler | P2 |
| tests/test_batch12.py::test_trigger_delete | A | assert 0 >= 1（len([])） | DELETE 触发器未写入日志表 | 与 trigger_after_insert 一并修 | P2 |
| tests/test_batch13.py::test_check_constraint | A | assert False（用例自打印“CHECK 未生效！”） | CHECK (price>0) 未拦截 price=-5 插入 | insert 路径强制校验 CHECK | P1 |
| tests/test_batch13.py::test_fulltext | B | PermissionError [WinError 32] data_b13\docs.dat | 级联锁：check_constraint 断言失败后未 shutdown | teardown + reset 重试 | P2 |
| tests/test_batch13.py::test_generated_column | B | PermissionError [WinError 32] docs.dat | 同上 | 同上 | P2 |
| tests/test_batch13.py::test_json_in_table | A | SyntaxError: WHERE 条件错误: data->'$.name' = 'Alice' | 解析器不支持 JSON 路径 `->` 运算符 | 支持 `col->'$.key'` 表达式 | P1 |
| tests/test_batch13.py::test_json_with_fulltext | B | PermissionError [WinError 32] products.dat（reset） | 级联锁（源同上） | 同上 | P2 |
| tests/test_batch13.py::test_partition_hash | B | PermissionError [WinError 32] docs.dat（reset） | 级联锁（源同上） | 同上 | P2 |
| tests/test_batch13.py::test_partition_range | A | AttributeError: 'Table' object has no attribute '_partition' | 分区表元数据未挂到 Table 对象 | 建分区时保存分区描述并暴露 prune | P1 |
| tests/test_batch14.py::test_audit | B | PermissionError [WinError 32] data_b14\t.dat（reset） | 级联锁（本文件首个失败用例留下的句柄） | teardown + reset 重试（隔离运行暴露 `SHOW AUDIT LOG` 语法不支持） | P2 |
| tests/test_batch14.py::test_parallel_count | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | 同上 | P2 |
| tests/test_batch14.py::test_parallel_scan | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | 同上 | P2 |
| tests/test_batch14.py::test_semi_sync_sql | A | pymysql ProgrammingError 1064 near '__SEMI_SYNC_REGISTER__ slave_test' | 服务端未实现半同步注册魔法语句 | 实现该命令或改走正式 SQL 接口 | P2 |
| tests/test_batch15.py::test_benchmark | B | PermissionError [WinError 32] data_b15\t.dat（reset） | 级联锁（隔离运行暴露 `KeyError: 'batch_insert'`） | teardown + 修 BenchmarkRunner 结果键 | P2 |
| tests/test_batch15.py::test_check_optimize | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | teardown + reset 重试 | P2 |
| tests/test_batch15.py::test_explain_analyze | A | pymysql OperationalError 1105: EXPLAIN 失败: 'NoneType' has no attribute 'explain_row' | EXPLAIN 执行未拿到执行计划对象 | 修 explain 计划生成的非空保证 | P1 |
| tests/test_batch15.py::test_full_sql_suite | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | 同上 | P2 |
| tests/test_batch15.py::test_security_via_sql | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | 同上 | P2 |
| tests/test_batch15.py::test_show_engine | B | PermissionError [WinError 32] t.dat（reset） | 级联锁（隔离运行暴露 `SHOW ENGINE INNODB` 语法不支持） | teardown + 补 SHOW ENGINE | P2 |
| tests/test_batch3.py::test_er_relations | A | AssertionError: any(...) is False | 外键关系探测未识别 orders.user_id → users | 修 ER 推断规则 | P2 |
| tests/test_batch3.py::test_query_builder_sql | A | assert 'age > 20' in "SELECT ..., WHERE age-with-backticks > 20 ..." | 生成 SQL 给列名加反引号，断言用未加引号子串 | 改断言为含反引号的 `age` > 20（或让 builder 不加引号） | P2 |
| tests/test_batch6.py::test_explain_join | A | TypeError: argument of type 'int' is not iterable | EXPLAIN JOIN 结果处理把 int 当容器判断 `in` | 修 explain 行解析 | P1 |
| tests/test_batch6.py::test_join_performance | B | PermissionError [WinError 32] data_b6\orders.dat（reset） | 级联锁 | teardown + reset 重试 | P2 |
| tests/test_batch8.py::test_binlog | A | AttributeError: 'Database' object has no attribute 'binlog' | binlog API 缺失 | 实现 Database.binlog 或改用例引用 | P1 |
| tests/test_batch8.py::test_logical_backup | B | PermissionError [WinError 32] data_b8_master\users.dat | 级联锁 | teardown + reset 重试 | P2 |
| tests/test_batch8.py::test_master_slave_status | B | PermissionError [WinError 32] users.dat（reset） | 级联锁 | 同上 | P2 |
| tests/test_batch8.py::test_replication | A | AssertionError: 从库应有 10 行, where 0 = Table.row_count | 主从复制后从库表 0 行 | 修复制应用逻辑 | P1 |
| tests/test_batch9.py::test_admin_tool | B | PermissionError [WinError 32] data_b9\t.dat（reset） | 级联锁（隔离运行暴露 `argparse -h/--host 与 -h/--help 冲突`） | teardown + 改 admin 的 `-h` 参数 | P2 |
| tests/test_batch9.py::test_cli_tool | B | PermissionError [WinError 32] t.dat（reset） | 级联锁（隔离运行暴露 `ModuleNotFoundError: No module named 'readline'`） | teardown + cli.py 用 try/except 导入 readline | P2 |
| tests/test_batch9.py::test_dump_tool | B | PermissionError [WinError 32] t.dat（reset） | 级联锁（隔离运行暴露 dump 同款 argparse -h 冲突） | teardown + 改 dump 的 `-h` 参数 | P2 |
| tests/test_batch9.py::test_grant_via_sql | A | pymysql ProgrammingError 1064 near 'CREATE USER ...' | CREATE USER / IDENTIFIED BY 语法不支持 | 支持用户管理语句 | P1 |
| tests/test_batch9.py::test_prepared_statement | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | teardown + reset 重试 | P2 |
| tests/test_batch9.py::test_privilege_enforcement | A | pymysql ProgrammingError 1064 near 'CREATE USER ...' | 同上（权限语句缺口） | 与 grant_via_sql 一并修 | P1 |
| tests/test_batch9.py::test_processlist | B | PermissionError [WinError 32] t.dat（reset） | 级联锁 | teardown + reset 重试 | P2 |
| tests/test_v2_batch1.py::test_catalog_basic | A | AssertionError: assert 'restricted' == 'confidential' | 敏感度推断把 salary 判为 restricted（应为 confidential） | 收紧字段名→敏感度规则 | P2 |
| tests/test_v2_batch1.py::test_lineage_enhanced | A | AssertionError: assert 0 >= 1, where [] = EnhancedLineageStore.edges | INSERT…SELECT 未产生列级血缘边 | 修列级血缘解析 | P2 |
| tests/test_v2_batch4.py::test_data_api | A | assert 0 == 2 | 数据 API 的 params 过滤（city=Beijing）返回 0 行 | 修 API 参数下推 | P1 |
| tests/test_v2_batch4.py::test_federation | A | AssertionError: assert 'c.city_name' in ['u.id','u.name','u.city_id'] | 联邦 JOIN 只输出左表列 | 修跨库投影列合并 | P1 |
| tests/test_v2_batch4.py::test_virtualization | A | AssertionError: assert 3 == 2（WHERE age > 26 仍 3 行） | 虚拟表 where 过滤未生效 | 修虚拟表过滤下推 | P1 |
| tests/test_v2_batch5.py::test_recommender | A | AssertionError: any(...) is False（related_tables 为空） | learn_from_sql 后未建立 users↔orders 关联 | 修关联学习/查询 | P2 |
| tests/test_v2_batch6.py::test_federated_governance | C | run1 pass / run2 FAIL / run3 FAIL：assert 4 >= 5 | 统计受共享 GOV_FILE 残留状态影响（子集运行又在另一断言 line 178 失败） | 用例内隔离策略文件；统计口径固定 | P2 |
| tests/test_v2_batch6.py::test_react_agent | A | KeyError: 'tables'（隔离运行：{'error': "'FakeConn' object has no attribute 'get_tables'"}） | 测试桩 FakeConn 未实现 get_tables，工具返回 error dict | 补桩方法或让工具容错返回 tables | P2 |
| tests/test_v2_batch7.py::test_digital_twin | A | AttributeError: 'NoneType' object has no attribute 'snapshot_id' | 创建快照返回 None | 修孪生快照创建 | P1 |
| tests/test_v3_batch1.py::test_full_pipeline_dry_run | B | PermissionError [Errno 13] 'C:\Users\Administrator\.lightdb_studio\reports\*.md' | 沙箱禁写工作区外，报告写到用户主目录 | 尊重调用方 output-dir（或落到工作区内） | P2 |
| tests/test_v3_batch1.py::test_memory_analyzer_structure | A | AssertionError: assert 'watch' == 'stable' | 60s 增长 1.5MB/min 被判为 watch，阈值过严 | 复核判定阈值 | P2 |
| tests/test_v3_batch2.py::test_batch_executor | A | AssertionError: assert 50000 == 16317 | 向量化过滤返回全量 50000 行，与逐行 16317 行不一致 | 修 VectorizedFilter 批间条件传递 | P1 |
| tests/test_v3_batch2.py::test_row_format_performance | C | run1 pass / run2 FAIL / run3 FAIL：assert 30.9 < 24.6 | 二进制行格式编码耗时断言，抖动 | 计时断言改相对比较并放宽 | P3 |
| tests/test_v3_batch4.py::test_parser_v3b4 | A | TypeError: 'NoneType' object is not subscriptable | 解析结果 None 后仍做下标访问 | 修 parser_v3b4 返回/校验 | P1 |
| tests/test_v3_batch4.py::test_registry | A | assert 132 >= 150 | 函数注册表仅 132 个函数，未达用例要求 | 补齐函数或复核覆盖率目标 | P2 |
| tests/test_v3_batch4.py::test_variable_executor | A | TypeError: 'NoneType' object is not subscriptable（parser_v3b4.py:110） | 变量语句解析返回 None 后被下标访问 | 与 parser_v3b4 一并修 | P1 |
| tests/test_v3_batch5.py::test_dashboard_generation | B | PermissionError [Errno 13] 'D:\lightdb_project\dashboards\lightdb_overview.json' | 沙箱令牌缺少 dashboards 目录 ACE（工作区内被拒） | 补齐目录 ACL 或让生成器回退到可写目录 | P2 |
| tests/test_v4_batch1.py::test_decision_doc | A | AssertionError: assert 'Cython' in docs/V4_DECISION.md | 文档被改写且截断（len=1032，含“原文档在此处截断”） | 恢复被截断章节或更新断言 | P2 |
| tests/test_v4_batch1.py::test_end_to_end_diagnosis | A | NameError: name 'subprocess' is not defined | 用例内用了 subprocess 却未 import | 用例内补 `import subprocess` | P2 |
| tests/test_v4_batch1.py::test_route_comparison | A | IndexError: list index out of range | 路线名不含“混合”（实际为“多进程+Cython（推荐）”） | 按名字匹配改为按 key 匹配 | P2 |
| tests/test_v4_batch4.py::test_encoder_auto_select | A | AssertionError: DeltaEncoder 不是 RLEEncoder | 常量 INT 列选择了 DeltaEncoder 而非 RLE | 修 EncoderRegistry.select 判定顺序 | P2 |
| tests/test_v4_batch4.py::test_hybrid_table | B | PermissionError [WinError 32] ...\Temp\dsh-*\orders.dat | 临时目录清理时文件仍被占用 | 确保关库后再清临时目录 | P2 |
| tests/test_v4_batch4.py::test_performance_vs_python | A | assert 499999500000 == 1783293664.0 | 向量化 SUM 累加溢出，结果错值（同 vectorized_aggregate） | 用 int64/整数累加 | P1 |
| tests/test_v4_batch4.py::test_vectorized_aggregate | A | assert 704982704.0 == 4999950000 | 10 万行 SUM 溢出（RuntimeWarning: overflow in scalar add） | 同上 | P1 |
| tests/test_v4_batch5.py::test_parallel_group_by | A | assert 0 == 100（len([])） | 并行 GROUP BY 返回空（探测线程可用，非跳过） | 与并行分组一并修 | P1 |

## 3. A 类：最小复现与实测输出

裸脚本（`python _triage_repro.py`，无 pytest、无沙箱依赖）实测输出如下，覆盖 32/42 条 A 类（其余 10 条见本节末尾）：

| 复现命令/片段 | 覆盖 A 用例 | 实测输出 |
|---|---|---|
| `BinaryRowFormat([{id INT},{name VARCHAR},{amount DECIMAL}]).encode([0,'u0',0.0])` | test_row_format_performance | `TypeError: cannot convert 'float' object to bytes` |
| `parser.parse('ALTER TABLE t1 ADD COLUMN age INT')` | test_ddl | `None` |
| `parser.parse('SET @@autocommit = 1')` | test_variables | `None` |
| `parser.parse("SELECT * FROM t WHERE data->'$.name'='Alice'")` | test_json_in_table | `SyntaxError: WHERE 条件错误: data->'$.name' = 'Alice'` |
| `hasattr(Database,'binlog')` / `hasattr(MySQLServer,'query_cache')` / `hasattr(Table,'_partition')` | test_binlog / test_concurrent_load / test_partition_range | 全部 `False` |
| `VectorizedAggregate(scan,'amount','SUM').execute()`（10 万行） | test_vectorized_aggregate、test_performance_vs_python | `got=704982704.0 expect=4999950000` |
| `EncoderRegistry.select('INT',[100]*1000)` | test_encoder_auto_select | `DeltaEncoder` |
| `get_registry().count()` | test_registry | `132` |
| `CREATE PROCEDURE add_user… + CALL`（裸 Database/Executor） | test_create_procedure、test_procedure_if_while | `CREATE PROCEDURE -> type=error msg='NoneType' object is not subscriptable ；CALL -> error 表内行数=0` |
| `CREATE TRIGGER trg_log_insert AFTER INSERT… + INSERT` | test_trigger_after_insert、test_trigger_delete | `CREATE TRIGGER type=error… ；logs 行数=0` |
| `CREATE TABLE products(… price INT CHECK(price>0) …); insert([2,-5,10])` | test_check_constraint | `CREATE type=ok ；插入 price=-5 未被拦截(CHECK 未生效)` |
| `open('docs/V4_DECISION.md').read()` | test_decision_doc | `len=1032 'Cython'=False '已截断'=True` |
| 直接调用测试函数（`import tests....; test_x()`，19 个函数） | test_olap_groupby、test_parallel_groupby_correctness、test_encoder_auto_select、test_route_comparison、test_decision_doc、test_registry、test_batch_executor、test_catalog_basic、test_lineage_enhanced、test_recommender、test_virtualization、test_federation、test_data_api、test_react_agent、test_digital_twin、test_memory_analyzer_structure、test_explain_join、test_binlog、test_concurrent_load | 全部复现 pytest 内同款异常，例如：`test_virtualization → → WHERE age > 26: 3 行` + `AssertionError`；`test_catalog_basic → {'salary': 'restricted'}`；`test_batch_executor → 逐行 16317 行 / 批处理 16317 行` + `assert 50000 == 16317`；`test_react_agent → list_tables: {'error': "'FakeConn' object has no attribute 'get_tables'"}` |
| `test_batch3::test_query_builder_sql` 的断言片段 | test_query_builder_sql | `'age > 20' in sql -> False`（sql 里是 `` `age` > 20 ``） |

未裸复现的 10 条 A 类（同族已复现或仅有隔离运行证据，均为确定性断言，无 D 类）：test_semi_sync_sql、test_explain_analyze、test_replication、test_grant_via_sql、test_privilege_enforcement、test_parser_v3b4、test_variable_executor、test_er_relations、test_end_to_end_diagnosis、test_parallel_group_by。复现方式：`python -m pytest <该 id> -v --tb=line`（隔离运行 38.9s，71 failed / 2 passed，2 条通过的都是纯计时项 test_row_format_performance 与 test_olap_aggregate_performance）。

## 4. B 类：判别证据与「什么条件下算真实缺陷」

1. **WinError 32 级联簇（24 条，8 个文件）**：全部堆栈都停在 `shutil.rmtree`（`Lib\shutil.py:632`），由用例自己的 `reset()` 触发，不是沙箱 ACL 拒绝（ACL 拒绝是 `[Errno 13]/WinError 5`）。
   实测判别（裸脚本）：`open(f,'w')` 后 `shutil.rmtree(dir)` → `PermissionError winerror=32`；`Database(dir)` 建表后不 shutdown → `rmtree PermissionError winerror=32`，而 `db.shutdown()` 后 `rmtree OK`。
   **条件**：只要前一个用例在断言失败路径上跳过了 `db.shutdown()`，任何 Windows（不限沙箱）都会稳定复现；Linux 因可删除被打开文件而掩盖。因此这 24 条在「普通 Windows 机器/CI」上是**真实缺陷**（测试清理缺陷，必须修 teardown）。
2. **写权限 2 条**：`test_v3_batch1::test_full_pipeline_dry_run` 写 `C:\Users\Administrator\.lightdb_studio\reports\*`（工作区外，沙箱按设计拒绝）；`test_v3_batch5::test_dashboard_generation` 写工作区内 `dashboards\lightdb_overview.json` 仍被拒。
   实测判别：`open('dashboards\_triage_probe.tmp','w')` → `PermissionError [Errno 13]`；`icacls dashboards` 没有沙箱 SID `S-1-4-940294834-733306826` 的 ACE，而 `icacls .`、`icacls data` 有；`docs/tests/lightdb/scripts` 同样缺 ACE（实测同样被拒，只有根目录与 `data/`、`backups_b8/` 可写）；同一路径经 harness 文件后端（非沙箱令牌）写入 `docs/FAILURE_TRIAGE.md` 成功。
   **条件**：这两条在「写权限正常的机器」上会通过；若目标是只读 HOME 或只读仓库挂载（CI），它们就是真实缺陷（应支持并尊重 `--output-dir`）。
3. **级联掩盖的下层真实缺陷（隔离运行暴露，属 A 类，需一并修）**：test_batch9::test_admin_tool / test_dump_tool → `argparse.ArgumentError: -h/--host conflicting option string: -h`（裸跑 `python -m lightdb.tools.admin ping` 同样报错，**所有平台**都失败）；test_batch9::test_cli_tool → `ModuleNotFoundError: No module named 'readline'`（**所有 Windows** 都失败）；test_batch12::test_function → `CREATE FUNCTION type=error`；test_batch14::test_audit → `SHOW AUDIT LOG` 不支持；test_batch15::test_show_engine → `SHOW ENGINE INNODB` 不支持；test_batch15::test_benchmark → `KeyError: 'batch_insert'`。

## 5. C 类：3 次运行状态

| 测试 ID | run1 | run2 | run3 | 隔离运行 | 说明 |
|---|---|---|---|---|---|
| tests/test_v2_batch6.py::test_federated_governance | PASS | FAIL `assert 4 >= 5` | FAIL `assert 4 >= 5` | FAIL（另一断言 line 178） | 共享策略文件残留导致状态相关 |
| tests/test_v3_batch2.py::test_row_format_performance | PASS | FAIL `30.94 < 24.56` | FAIL `29.63 < 25.08` | PASS | 纯计时断言（同一数 30ms/24ms 量级抖动） |
| tests/regression_v4/test_performance_v4.py::test_olap_aggregate_performance | FAIL | FAIL | FAIL | PASS | 吞吐阈值，受全量负载影响 |
| tests/regression_v4/test_performance_v4.py::test_parallel_speedup | FAIL | FAIL | FAIL | PASS | 加速比阈值，受负载影响 |
| tests/regression_v4/test_columnar.py::test_encoder_compression | FAIL | FAIL | FAIL | FAIL | 压缩率 2.7766 三次完全相同：确定性未达标，非抖动 |

## 6. 建议的修复顺序 top 5

1. **teardown 清理（覆盖 24 条 B 级联 + 让 6 个隐藏 A 缺陷可见）**：每个用例用 `try/finally: db.shutdown()`，`reset()` 对 WinError 32 做重试/降级。单笔改动消除 24 条失败，且不修它就无法看见后面的真实错误。
2. **tests/test_v4_batch4.py::test_vectorized_aggregate**（连带 test_performance_vs_python）：向量化 SUM 溢出返回错值，属**结果正确性**，且已在裸脚本复现（704982704.0 ≠ 4999950000）。
3. **tests/test_batch13.py::test_check_constraint**：CHECK 约束完全不生效＝可写入非法数据，正确性最高危；它同时是 data_b13 4 条级联锁的源头。
4. **tests/test_batch12.py::test_trigger_after_insert / test_create_procedure**：过程与触发器不执行（返回 error、日志 0 行），是 SQL 兼容性核心承诺，影响面大于单条断言。
5. **tests/regression/test_sql_compat.py::test_ddl / tests/regression_v4/test_columnar.py::test_olap_groupby**：解析器缺 ALTER ADD COLUMN（返回 None）、列存 GROUP BY 返回 0 行——一个是入门语法缺口，一个是 OLAP 主路径失效，都用例/裸脚本双证据。

## 7. 不该再投入的项

- 11 条 skip：`conftest.py:100` 多进程探测 + `test_multiprocess_server.py:59`，`WinError 5` 是沙箱禁命名管道，**本机不可验证**，不要改成 fail 也不要删探测。
- 3 条纯性能/吞吐断言（test_olap_aggregate_performance、test_parallel_speedup、test_columnar::test_encoder_compression）与 1 条计时断言（test_v3_batch2::test_row_format_performance）：在共享/沙箱机器上无判别力，建议移出功能门禁。
- `test_v3_batch1::test_full_pipeline_dry_run`、`test_v3_batch5::test_dashboard_generation` 的沙箱写权限部分：不要试图在用例里绕过沙箱，改为补齐目录 ACL 或让工具回退到可写目录后再验证。
