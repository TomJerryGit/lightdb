-- ============================================================
-- LightDB 文件表全量数据
-- 共 340+ 文件
-- ============================================================
SET NAMES utf8mb4;

TRUNCATE TABLE `lightdb_files`;

-- ------------------------------------------------------------
-- 【1】lightdb/storage/  存储引擎（12 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='storage'), '__init__.py',        'lightdb/storage/__init__.py',        'python', 'python', 800,  30, '存储引擎模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'page.py',           'lightdb/storage/page.py',            'python', 'python', 3500, 120, '4096 字节页，槽位数组 + 行数据', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'buffer_pool.py',    'lightdb/storage/buffer_pool.py',     'python', 'python', 3200, 110, 'LRU 缓冲池 + 脏页回写', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'btree.py',          'lightdb/storage/btree.py',           'python', 'python', 12000, 380, 'B+Tree 内部节点分裂 + 递归', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'btree_optimized.py','lightdb/storage/btree_optimized.py', 'python', 'python', 1500, 60,  '优化版 B+Tree + LRU 缓存包装', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'wal.py',            'lightdb/storage/wal.py',             'python', 'python', 3000, 100, '预写日志，崩溃恢复', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'mvcc.py',           'lightdb/storage/mvcc.py',            'python', 'python', 4500, 150, 'MVCC 版本控制 + ReadView', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'lock_manager.py',   'lightdb/storage/lock_manager.py',    'python', 'python', 5500, 180, '行级锁 + 意向锁 + 死锁检测', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'txn.py',            'lightdb/storage/txn.py',             'python', 'python', 2000, 70,  '事务管理器 + MVCC 可见性', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'row_format.py',     'lightdb/storage/row_format.py',      'python', 'python', 6000, 200, '二进制行格式（紧凑编码）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'row_format_v4.py',  'lightdb/storage/row_format_v4.py',   'python', 'python', 2500, 90,  'Cython 加速版行格式', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'page_prefetch.py',  'lightdb/storage/page_prefetch.py',   'python', 'python', 2200, 80,  '页预读（顺序检测）', 'provided', 0);

-- ------------------------------------------------------------
-- 【2】lightdb/sql/  SQL 层（27 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='sql'), '__init__.py',           'lightdb/sql/__init__.py',            'python', 'python', 900,  35, 'SQL 层模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'lexer.py',              'lightdb/sql/lexer.py',               'python', 'python', 3000, 100, '词法分析（可选）', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'parser.py',             'lightdb/sql/parser.py',              'python', 'python', 25000, 700, 'SQL 解析器（正则驱动）', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'parser_v3b4.py',        'lightdb/sql/parser_v3b4.py',         'python', 'python', 4000, 130, 'SET 变量、INTERVAL 扩展', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'plan.py',               'lightdb/sql/plan.py',                'python', 'python', 10000, 320, '逻辑/物理计划节点', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'optimizer.py',          'lightdb/sql/optimizer.py',           'python', 'python', 12000, 380, 'RBO + CBO 优化器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'plan_cache.py',         'lightdb/sql/plan_cache.py',          'python', 'python', 4500, 150, '查询计划缓存（LRU + 表级失效）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'plan_executor.py',      'lightdb/sql/plan_executor.py',       'python', 'python', 15000, 450, '计划执行器（物化列表）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'executor.py',           'lightdb/sql/executor.py',            'python', 'python', 8000, 250, 'SQL 执行器', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'batch_executor.py',     'lightdb/sql/batch_executor.py',      'python', 'python', 5000, 160, '批处理执行器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'vectorized.py',         'lightdb/sql/vectorized.py',          'python', 'python', 3000, 100, '向量化（已有 batch_executor）', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'predicate_pushdown.py','lightdb/sql/predicate_pushdown.py',  'python', 'python', 5000, 160, '谓词下推', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'predicate_v4.py',       'lightdb/sql/predicate_v4.py',        'python', 'python', 3000, 100, 'Cython 加速谓词', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'icp.py',                'lightdb/sql/icp.py',                 'python', 'python', 4500, 150, '索引条件下推（ICP）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'parallel_planner.py',   'lightdb/sql/parallel_planner.py',    'python', 'python', 3000, 100, '并行计划（已有 parallel/）', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'variables.py',          'lightdb/sql/variables.py',           'python', 'python', 5000, 160, '会话变量 + 用户变量', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'cursor.py',             'lightdb/sql/cursor.py',              'python', 'python', 3500, 120, '游标', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'stats.py',              'lightdb/sql/stats.py',               'python', 'python', 6000, 200, '统计信息 + 选择率估算', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'pl.py',                 'lightdb/sql/pl.py',                  'python', 'python', 18000, 540, 'PL/SQL 引擎', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'stored.py',             'lightdb/sql/stored.py',              'python', 'python', 8000, 250, '存储过程 + 函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'trigger.py',            'lightdb/sql/trigger.py',             'python', 'python', 6000, 200, '触发器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'event.py',              'lightdb/sql/event.py',               'python', 'python', 5000, 160, '事件调度', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'window.py',             'lightdb/sql/window.py',              'python', 'python', 15000, 450, '窗口函数', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'cte.py',                'lightdb/sql/cte.py',                 'python', 'python', 5000, 160, 'CTE（含递归）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'json_func.py',          'lightdb/sql/json_func.py',           'python', 'python', 7000, 220, 'JSON 函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'fulltext.py',           'lightdb/sql/fulltext.py',            'python', 'python', 6000, 200, '全文索引 + BM25', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'partition.py',          'lightdb/sql/partition.py',           'python', 'python', 5000, 160, '分区表', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'generated.py',          'lightdb/sql/generated.py',           'python', 'python', 5000, 160, '生成列 + CHECK', 'provided', 0);

-- ------------------------------------------------------------
-- 【3】lightdb/sql/functions/  函数库（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='sql'), '__init__.py',        'lightdb/sql/functions/__init__.py',    'python', 'python', 800,  30,  '函数库导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'string_funcs.py',    'lightdb/sql/functions/string_funcs.py','python', 'python', 10000, 320, '50+ 字符串函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'datetime_funcs.py',  'lightdb/sql/functions/datetime_funcs.py','python','python',12000,380,'44 日期时间函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'math_funcs.py',      'lightdb/sql/functions/math_funcs.py',  'python', 'python', 5000, 160, '32 数学函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'control_funcs.py',   'lightdb/sql/functions/control_funcs.py','python','python',3000,100,'7 流程控制函数', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'), 'registry.py',        'lightdb/sql/functions/registry.py',    'python', 'python', 2500, 90,  '函数注册表', 'provided', 0);

-- ------------------------------------------------------------
-- 【4】lightdb/db/  数据库对象（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='db'), '__init__.py',        'lightdb/db/__init__.py',           'python', 'python', 500,  20, '数据库对象导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='db'), 'database.py',        'lightdb/db/database.py',           'python', 'python', 5000, 160, '数据库对象，集成 MVCC + 锁', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='db'), 'table.py',           'lightdb/db/table.py',              'python', 'python', 22000, 650, '表对象，行级锁 + MVCC + WAL', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='db'), 'table_v3.py',        'lightdb/db/table_v3.py',           'python', 'python', 3000, 100, '优化版表，读写锁 + 批处理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='db'), 'hybrid_table.py',    'lightdb/db/hybrid_table.py',       'python', 'python', 6000, 200, 'HTAP 混合表（行存 + 列存）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='db'), 'information_schema.py','lightdb/db/information_schema.py','python','python',3500,120,'元数据虚拟表', 'provided', 0);

-- ------------------------------------------------------------
-- 【5】lightdb/net/  网络层（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='net'), '__init__.py',       'lightdb/net/__init__.py',           'python', 'python', 500,  20, '网络层导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='net'), 'mysql_protocol.py', 'lightdb/net/mysql_protocol.py',     'python', 'python', 12000, 380, 'MySQL Wire Protocol v10', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='net'), 'server.py',         'lightdb/net/server.py',             'python', 'python', 15000, 450, 'ThreadingTCPServer MySQL 兼容服务器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='net'), 'listener.py',       'lightdb/net/listener.py',           'python', 'python', 5000, 160, 'SO_REUSEPORT 监听器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='net'), 'master.py',         'lightdb/net/master.py',             'python', 'python', 8000, 250, 'Master 进程（多进程模式）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='net'), 'worker.py',         'lightdb/net/worker.py',             'python', 'python', 9000, 280, 'Worker 进程入口', 'provided', 1);

-- ------------------------------------------------------------
-- 【6】lightdb/replication/  复制（5 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='replication'), '__init__.py',   'lightdb/replication/__init__.py',     'python', 'python', 500,  20,  '复制模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='replication'), 'binlog.py',     'lightdb/replication/binlog.py',       'python', 'python', 6000, 200, 'ROW 格式二进制日志', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='replication'), 'master.py',     'lightdb/replication/master.py',       'python', 'python', 3000, 100, '主库复制管理器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='replication'), 'slave.py',      'lightdb/replication/slave.py',        'python', 'python', 8000, 250, '从库复制管理器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='replication'), 'semi_sync.py',  'lightdb/replication/semi_sync.py',    'python', 'python', 6000, 200, '半同步复制', 'provided', 0);

-- ------------------------------------------------------------
-- 【7】lightdb/security/  安全（7 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='security'), '__init__.py',         'lightdb/security/__init__.py',          'python', 'python', 600,  25,  '安全模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='security'), 'privileges.py',       'lightdb/security/privileges.py',        'python', 'python', 10000, 320, '用户 + 角色 + 细粒度授权', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='security'), 'ssl_support.py',      'lightdb/security/ssl_support.py',       'python', 'python', 5000, 160, 'TLS/SSL，自签名证书', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='security'), 'audit.py',            'lightdb/security/audit.py',             'python', 'python', 5000, 160, '审计日志', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='security'), 'password_policy.py',  'lightdb/security/password_policy.py',   'python', 'python', 5000, 160, '密码策略', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='security'), 'connection_limit.py', 'lightdb/security/connection_limit.py',  'python', 'python', 4000, 130, '连接限流', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='security'), 'ip_whitelist.py',     'lightdb/security/ip_whitelist.py',      'python', 'python', 3000, 100, 'IP 白名单', 'provided', 0);

-- ------------------------------------------------------------
-- 【8】lightdb/performance/  性能优化（7 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='performance'), '__init__.py',        'lightdb/performance/__init__.py',         'python', 'python', 700,  30,  '性能优化模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'connection_pool.py', 'lightdb/performance/connection_pool.py',  'python', 'python', 6000, 200, '连接池', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'query_cache.py',     'lightdb/performance/query_cache.py',      'python', 'python', 4000, 130, '查询缓存', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'batch_inserter.py',  'lightdb/performance/batch_inserter.py',   'python', 'python', 4000, 130, '批量插入', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'index_only_scan.py', 'lightdb/performance/index_only_scan.py',  'python', 'python', 2500, 90,  '覆盖索引扫描', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'parallel.py',        'lightdb/performance/parallel.py',         'python', 'python', 8000, 250, '并行查询执行', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='performance'), 'benchmark.py',       'lightdb/performance/benchmark.py',        'python', 'python', 7000, 220, '内置性能基准', 'provided', 0);

-- ------------------------------------------------------------
-- 【9】lightdb/concurrency/  并发原语（7 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='concurrency'), '__init__.py',      'lightdb/concurrency/__init__.py',      'python', 'python', 900,  35,  '并发模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'rw_lock.py',       'lightdb/concurrency/rw_lock.py',       'python', 'python', 5000, 160, '读写锁（写优先）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'lockfree_mvcc.py', 'lightdb/concurrency/lockfree_mvcc.py', 'python', 'python', 6000, 200, '无锁 MVCC 版本链', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'multiprocess.py',  'lightdb/concurrency/multiprocess.py',  'python', 'python', 10000, 320, 'Worker 进程池', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'shared_memory.py', 'lightdb/concurrency/shared_memory.py', 'python', 'python', 10000, 320, 'mmap 共享内存', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'atomic.py',        'lightdb/concurrency/atomic.py',        'python', 'python', 6000, 200, '原子计数器 + 字典 + 环形缓冲', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'), 'ipc.py',           'lightdb/concurrency/ipc.py',           'python', 'python', 5000, 160, '进程间通信（Pipe/Queue）', 'fixed', 0);

-- ------------------------------------------------------------
-- 【10】lightdb/cluster/  集群协调（5 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='cluster'), '__init__.py',         'lightdb/cluster/__init__.py',          'python', 'python', 600,  25,  '集群模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cluster'), 'shared_buffer.py',    'lightdb/cluster/shared_buffer.py',     'python', 'python', 9000, 280, '共享 Buffer Pool', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='cluster'), 'txn_coordinator.py',  'lightdb/cluster/txn_coordinator.py',   'python', 'python', 8000, 250, '事务协调器（跨进程）', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='cluster'), 'lock_coordinator.py', 'lightdb/cluster/lock_coordinator.py',  'python', 'python', 6000, 200, '集中式锁协调器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cluster'), 'stats_hub.py',        'lightdb/cluster/stats_hub.py',         'python', 'python', 4000, 130, '跨进程统计聚合', 'provided', 1);

-- ------------------------------------------------------------
-- 【11】lightdb/columnar/  列式存储（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='columnar'), '__init__.py',      'lightdb/columnar/__init__.py',       'python', 'python', 700,  30,  '列式存储模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='columnar'), 'chunk.py',         'lightdb/columnar/chunk.py',          'python', 'python', 10000, 320, '列数据块', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='columnar'), 'encoders.py',      'lightdb/columnar/encoders.py',       'python', 'python', 18000, 540, '5 种编码器', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='columnar'), 'column_store.py',  'lightdb/columnar/column_store.py',   'python', 'python', 12000, 380, '列存储管理器', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='columnar'), 'segment.py',       'lightdb/columnar/segment.py',        'python', 'python', 6000, 200, '段（Chunk 组）', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='columnar'), 'stats.py',         'lightdb/columnar/stats.py',          'python', 'python', 800,  30,  '列存储统计', 'provided', 0);

-- ------------------------------------------------------------
-- 【12】lightdb/olap/  OLAP（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='olap'), '__init__.py',          'lightdb/olap/__init__.py',            'python', 'python', 800,  30,  'OLAP 模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='olap'), 'vectorized_engine.py', 'lightdb/olap/vectorized_engine.py',   'python', 'python', 18000, 540, '向量化扫描/过滤/聚合/分组/排序', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='olap'), 'aggregator.py',        'lightdb/olap/aggregator.py',          'python', 'python', 6000, 200, '向量化聚合器', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='olap'), 'scanner.py',           'lightdb/olap/scanner.py',             'python', 'python', 5000, 160, '列扫描器 + 谓词下推', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='olap'), 'olap_optimizer.py',    'lightdb/olap/olap_optimizer.py',      'python', 'python', 6000, 200, 'OLAP 优化器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='olap'), 'parallel_olap.py',     'lightdb/olap/parallel_olap.py',       'python', 'python', 3000, 100, '并行 OLAP 门面', 'provided', 0);

-- ------------------------------------------------------------
-- 【13】lightdb/parallel/  并行执行（8 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='parallel'), '__init__.py',         'lightdb/parallel/__init__.py',          'python', 'python', 900,  35,  '并行模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'fragment.py',         'lightdb/parallel/fragment.py',          'python', 'python', 5000, 160, '查询分片', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'executor.py',         'lightdb/parallel/executor.py',          'python', 'python', 15000, 450, '并行执行器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'work_stealing.py',    'lightdb/parallel/work_stealing.py',     'python', 'python', 8000, 250, '工作窃取调度器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'adaptive.py',         'lightdb/parallel/adaptive.py',          'python', 'python', 6000, 200, '自适应并行度', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'scheduler.py',        'lightdb/parallel/scheduler.py',         'python', 'python', 8000, 250, '查询调度器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'resource_group.py',   'lightdb/parallel/resource_group.py',    'python', 'python', 6000, 200, '资源组隔离', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'), 'merge.py',            'lightdb/parallel/merge.py',             'python', 'python', 5000, 160, '结果合并', 'provided', 0);

-- ------------------------------------------------------------
-- 【14】lightdb/cython/  Cython 加速（8 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='cython'), '__init__.py',           'lightdb/cython/__init__.py',            'python', 'python', 2500, 90,  'Cython 模块加载器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'setup.py',              'lightdb/cython/setup.py',               'python', 'python', 5000, 160, 'Cython 构建脚本', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'btree_pyx.pyx',         'lightdb/cython/btree_pyx.pyx',          'cython', 'cython', 10000, 320, 'B+Tree 二分查找', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'row_codec.pyx',         'lightdb/cython/row_codec.pyx',          'cython', 'cython', 9000, 280, '二进制行编解码', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'page_pyx.pyx',          'lightdb/cython/page_pyx.pyx',           'cython', 'cython', 8000, 250, '页操作', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'predicate_pyx.pyx',     'lightdb/cython/predicate_pyx.pyx',      'cython', 'cython', 7000, 220, '编译谓词', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'buffer_pool_pyx.pyx',   'lightdb/cython/buffer_pool_pyx.pyx',    'cython', 'cython', 5000, 160, 'FastLRU', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'hash_table_pyx.pyx',    'lightdb/cython/hash_table_pyx.pyx',     'cython', 'cython', 4000, 130, '整数哈希表', 'fixed', 0);

-- ------------------------------------------------------------
-- 【15】lightdb/monitor/  监控（8 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='monitor'), '__init__.py',           'lightdb/monitor/__init__.py',           'python', 'python', 700,  30,  '监控模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'metrics_v3.py',         'lightdb/monitor/metrics_v3.py',         'python', 'python', 18000, 540, '43 个 Prometheus 指标', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'slow_query_analyzer.py','lightdb/monitor/slow_query_analyzer.py','python', 'python', 12000, 380, '慢查询归因（10 种分类）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'slow_log_analyzer.py',  'lightdb/monitor/slow_log_analyzer.py',  'python', 'python', 3000, 100, '慢查询日志分析', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'lock_analyzer.py',      'lightdb/monitor/lock_analyzer.py',      'python', 'python', 7000, 220, '锁等待分析', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'index_advisor.py',      'lightdb/monitor/index_advisor.py',      'python', 'python', 10000, 320, '索引推荐', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'health_checker.py',     'lightdb/monitor/health_checker.py',     'python', 'python', 6000, 200, '健康检查（6 项）', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='monitor'), 'parallel_stats.py',     'lightdb/monitor/parallel_stats.py',     'python', 'python', 4000, 130, '并行统计聚合', 'fixed', 0);

-- ------------------------------------------------------------
-- 【16】lightdb/stability/  稳定性（7 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='stability'), '__init__.py',            'lightdb/stability/__init__.py',            'python', 'python', 700,  30,  '稳定性模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'soak_test.py',           'lightdb/stability/soak_test.py',           'python', 'python', 12000, 380, '30 天长稳测试', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'fault_injector.py',      'lightdb/stability/fault_injector.py',      'python', 'python', 15000, 450, '故障注入', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'consistency_checker.py', 'lightdb/stability/consistency_checker.py','python', 'python', 10000, 320, '一致性检查', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'memory_leak_detector.py','lightdb/stability/memory_leak_detector.py','python','python', 8000, 250, '内存泄漏检测', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'watchdog.py',             'lightdb/stability/watchdog.py',            'python', 'python', 8000, 250, '看门狗', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='stability'), 'crash_recovery_v3.py',    'lightdb/stability/crash_recovery_v3.py',   'python', 'python', 5000, 160, '崩溃恢复', 'fixed', 0);

-- ------------------------------------------------------------
-- 【17】lightdb/benchmark/  基准测试（8 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='benchmark'), '__init__.py',      'lightdb/benchmark/__init__.py',      'python', 'python', 700,  30,  '基准模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'runner.py',        'lightdb/benchmark/runner.py',        'python', 'python', 10000, 320, '多线程基准运行器', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'workloads.py',     'lightdb/benchmark/workloads.py',     'python', 'python', 12000, 380, '类 Sysbench 工作负载', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'profiler.py',      'lightdb/benchmark/profiler.py',      'python', 'python', 8000, 250, 'CPU Profiling', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'memory_analyzer.py','lightdb/benchmark/memory_analyzer.py','python','python', 7000, 220, '内存分析', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'baseline.py',      'lightdb/benchmark/baseline.py',      'python', 'python', 6000, 200, '基线存储与对比', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'report.py',        'lightdb/benchmark/report.py',        'python', 'python', 5000, 160, 'Markdown 报告生成', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'cli.py',           'lightdb/benchmark/cli.py',           'python', 'python', 6000, 200, 'lightdb-bench 命令', 'provided', 0);

-- ------------------------------------------------------------
-- 【18】lightdb/profiling/  性能分析（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='profiling'), '__init__.py',         'lightdb/profiling/__init__.py',          'python', 'python', 600,  25,  '性能分析模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='profiling'), 'cprofile_analyzer.py','lightdb/profiling/cprofile_analyzer.py','python', 'python', 12000, 380, 'cProfile 深度分析', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='profiling'), 'flamegraph.py',       'lightdb/profiling/flamegraph.py',       'python', 'python', 8000, 250, '火焰图生成', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='profiling'), 'hotspot_ranker.py',   'lightdb/profiling/hotspot_ranker.py',   'python', 'python', 10000, 320, '热点排行 + 优化路线', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='profiling'), 'gil_analyzer.py',     'lightdb/profiling/gil_analyzer.py',     'python', 'python', 12000, 380, 'GIL 影响量化', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='profiling'), 'reporter.py',         'lightdb/profiling/reporter.py',         'python', 'python', 5000, 160, '报告生成', 'fixed', 0);

-- ------------------------------------------------------------
-- 【19】lightdb/experiments/  技术路线实验（5 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='experiments'), '__init__.py',         'lightdb/experiments/__init__.py',         'python', 'python', 600,  25,  '实验模块导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='experiments'), 'multiprocess_test.py','lightdb/experiments/multiprocess_test.py','python', 'python', 15000, 450, '多进程验证', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='experiments'), 'cython_test.py',      'lightdb/experiments/cython_test.py',      'python', 'python', 12000, 380, 'Cython 验证', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='experiments'), 'subinterp_test.py',   'lightdb/experiments/subinterp_test.py',   'python', 'python', 4000, 130, '子解释器验证', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='experiments'), 'comparison.py',       'lightdb/experiments/comparison.py',       'python', 'python', 8000, 250, '三路线对比', 'provided', 0);

-- ------------------------------------------------------------
-- 【20】lightdb/tools/  运维工具（11 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='tools'), '__init__.py',         'lightdb/tools/__init__.py',         'python', 'python', 300,  10,  '工具模块', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'cli.py',              'lightdb/tools/cli.py',              'python', 'python', 8000, 250, 'lightdb CLI 客户端', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'dump.py',             'lightdb/tools/dump.py',             'python', 'python', 6000, 200, 'lightdbdump 备份', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'admin.py',            'lightdb/tools/admin.py',            'python', 'python', 6000, 200, 'lightdbadmin 管理', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'benchmark.py',        'lightdb/tools/benchmark.py',        'python', 'python', 6000, 200, 'lightdb-bench', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'check.py',            'lightdb/tools/check.py',            'python', 'python', 4000, 130, 'lightdb-check', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'tune.py',             'lightdb/tools/tune.py',             'python', 'python', 6000, 200, 'lightdb-tune', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'index_advisor.py',    'lightdb/tools/index_advisor.py',    'python', 'python', 5000, 160, 'lightdb-index-advisor', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'dashboard.py',        'lightdb/tools/dashboard.py',        'python', 'python', 8000, 250, 'lightdb-dashboard', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'diagnose.py',         'lightdb/tools/diagnose.py',         'python', 'python', 8000, 250, 'lightdb-diagnose', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'), 'migrate.py',          'lightdb/tools/migrate.py',          'python', 'python', 5000, 160, 'lightdb-migrate', 'provided', 0);

-- ------------------------------------------------------------
-- 【21】lightdb/backup/  备份恢复（2 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='backup'), '__init__.py', 'lightdb/backup/__init__.py', 'python', 'python', 300,  10,  '备份模块', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='backup'), 'backup.py',   'lightdb/backup/backup.py',   'python', 'python', 10000, 320, '逻辑备份 + 物理备份 + PITR', 'provided', 1);

-- ------------------------------------------------------------
-- 【22】lightdb/ 根目录（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='storage'), '__init__.py', 'lightdb/__init__.py',  'python', 'python', 500,  20,  'LightDB 包导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'logger.py',   'lightdb/logger.py',    'python', 'python', 2500, 90,  '分级日志', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'config.py',   'lightdb/config.py',    'python', 'python', 3000, 100, 'LightDB 配置管理', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'main.py',     'lightdb/main.py',      'python', 'python', 3000, 100, 'LightDB 数据库服务入口', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'main_v4.py',  'lightdb/main_v4.py',   'python', 'python', 2000, 70,  'v4.0 多进程启动', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'),  'build.sh',    'lightdb/build.sh',     'shell',  'shell',  2000, 70,  'Cython 构建脚本', 'provided', 0);

-- ------------------------------------------------------------
-- 【23】lightdb_studio/core/  核心引擎（约 70 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_core'), '__init__.py',           'lightdb_studio/lightdb_studio/core/__init__.py',           'python', 'python', 3000, 100, '核心模块导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'environments.py',        'lightdb_studio/lightdb_studio/core/environments.py',        'python', 'python', 8000, 250, '多环境管理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'lineage.py',             'lightdb_studio/lightdb_studio/core/lineage.py',             'python', 'python', 10000, 320, '基础血缘追踪', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'lineage_enhanced.py',    'lightdb_studio/lightdb_studio/core/lineage_enhanced.py',    'python', 'python', 12000, 380, '列级血缘', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'lineage_v2.py',          'lightdb_studio/lightdb_studio/core/lineage_v2.py',          'python', 'python', 10000, 320, 'CTE/视图/存储过程血缘', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'lineage_closure.py',     'lightdb_studio/lightdb_studio/core/lineage_closure.py',     'python', 'python', 12000, 380, '血缘闭包 + 影响度', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'lineage_visual.py',      'lightdb_studio/lightdb_studio/core/lineage_visual.py',      'python', 'python', 8000, 250, '血缘可视化', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'review_workflow.py',     'lightdb_studio/lightdb_studio/core/review_workflow.py',     'python', 'python', 6000, 200, 'SQL 审查', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'snapshot.py',            'lightdb_studio/lightdb_studio/core/snapshot.py',            'python', 'python', 8000, 250, '快照/克隆', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'sql_formatter.py',       'lightdb_studio/lightdb_studio/core/sql_formatter.py',       'python', 'python', 6000, 200, 'SQL 格式化', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'sql_profiler.py',        'lightdb_studio/lightdb_studio/core/sql_profiler.py',        'python', 'python', 12000, 380, 'SQL 性能分析', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'sql_auto_optimizer.py', 'lightdb_studio/lightdb_studio/core/sql_auto_optimizer.py', 'python', 'python', 10000, 320, '端到端自动优化', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'ai_integration.py',      'lightdb_studio/lightdb_studio/core/ai_integration.py',      'python', 'python', 12000, 380, 'AI 集成', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'ai_agent.py',            'lightdb_studio/lightdb_studio/core/ai_agent.py',            'python', 'python', 15000, 450, 'AI 智能运维', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'ai_assist.py',           'lightdb_studio/lightdb_studio/core/ai_assist.py',           'python', 'python', 12000, 380, '基于规则的 SQL 助手', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'ai_sql_gen.py',          'lightdb_studio/lightdb_studio/core/ai_sql_gen.py',          'python', 'python', 12000, 380, 'AI SQL 生成器 2.0', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'ai_finetune.py',         'lightdb_studio/lightdb_studio/core/ai_finetune.py',         'python', 'python', 10000, 320, 'LLM 微调管理', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'agent_react.py',         'lightdb_studio/lightdb_studio/core/agent_react.py',         'python', 'python', 18000, 540, 'ReAct Agent 2.0', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'collaboration.py',       'lightdb_studio/lightdb_studio/core/collaboration.py',       'python', 'python', 8000, 250, '协作', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'performance_monitor.py', 'lightdb_studio/lightdb_studio/core/performance_monitor.py', 'python', 'python', 8000, 250, '性能监控', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_quality.py',        'lightdb_studio/lightdb_studio/core/data_quality.py',        'python', 'python', 10000, 320, '数据质量', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'quality_v2.py',          'lightdb_studio/lightdb_studio/core/quality_v2.py',          'python', 'python', 12000, 380, '数据质量 2.0', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'slo.py',                 'lightdb_studio/lightdb_studio/core/slo.py',                 'python', 'python', 10000, 320, 'SLI/SLO', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'observability.py',       'lightdb_studio/lightdb_studio/core/observability.py',       'python', 'python', 10000, 320, '可观测性', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'fullstack_obs.py',       'lightdb_studio/lightdb_studio/core/fullstack_obs.py',       'python', 'python', 12000, 380, '全栈可观测', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'metrics.py',             'lightdb_studio/lightdb_studio/core/metrics.py',             'python', 'python', 8000, 250, 'Prometheus 指标', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'health_score.py',        'lightdb_studio/lightdb_studio/core/health_score.py',        'python', 'python', 10000, 320, '健康度评分', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'alerts.py',              'lightdb_studio/lightdb_studio/core/alerts.py',              'python', 'python', 12000, 380, '智能告警', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'autonomy.py',            'lightdb_studio/lightdb_studio/core/autonomy.py',            'python', 'python', 12000, 380, '智能自治', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'catalog.py',             'lightdb_studio/lightdb_studio/core/catalog.py',             'python', 'python', 12000, 380, '数据目录', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_map.py',            'lightdb_studio/lightdb_studio/core/data_map.py',            'python', 'python', 10000, 320, '数据地图', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_market.py',         'lightdb_studio/lightdb_studio/core/data_market.py',         'python', 'python', 10000, 320, '数据市场', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_mesh.py',           'lightdb_studio/lightdb_studio/core/data_mesh.py',           'python', 'python', 12000, 380, '数据 Mesh', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_fabric.py',         'lightdb_studio/lightdb_studio/core/data_fabric.py',         'python', 'python', 12000, 380, '数据编织', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_lake.py',           'lightdb_studio/lightdb_studio/core/data_lake.py',           'python', 'python', 12000, 380, '数据湖', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_api.py',            'lightdb_studio/lightdb_studio/core/data_api.py',            'python', 'python', 12000, 380, '数据 API', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_contract.py',       'lightdb_studio/lightdb_studio/core/data_contract.py',       'python', 'python', 12000, 380, '数据契约', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'digital_twin.py',        'lightdb_studio/lightdb_studio/core/digital_twin.py',        'python', 'python', 12000, 380, '数字孪生', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'distributed.py',         'lightdb_studio/lightdb_studio/core/distributed.py',         'python', 'python', 10000, 320, '分布式查询', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'federation.py',          'lightdb_studio/lightdb_studio/core/federation.py',          'python', 'python', 10000, 320, '联邦查询', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'federated_gov.py',       'lightdb_studio/lightdb_studio/core/federated_gov.py',       'python', 'python', 12000, 380, '联邦治理', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'virtualization.py',      'lightdb_studio/lightdb_studio/core/virtualization.py',      'python', 'python', 10000, 320, '数据虚拟化', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'etl.py',                 'lightdb_studio/lightdb_studio/core/etl.py',                 'python', 'python', 15000, 450, '实时 ETL', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'cdc.py',                 'lightdb_studio/lightdb_studio/core/cdc.py',                 'python', 'python', 12000, 380, '变更数据捕获', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'cloud.py',               'lightdb_studio/lightdb_studio/core/cloud.py',               'python', 'python', 10000, 320, '多云管理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'cloud_sync.py',          'lightdb_studio/lightdb_studio/core/cloud_sync.py',          'python', 'python', 8000, 250, '云同步', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'migration.py',           'lightdb_studio/lightdb_studio/core/migration.py',           'python', 'python', 12000, 380, '数据库迁移', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'snippets.py',            'lightdb_studio/lightdb_studio/core/snippets.py',            'python', 'python', 8000, 250, 'SQL 片段库', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'masking.py',             'lightdb_studio/lightdb_studio/core/masking.py',             'python', 'python', 10000, 320, '数据脱敏', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'reports.py',             'lightdb_studio/lightdb_studio/core/reports.py',             'python', 'python', 8000, 250, '报表系统', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'scheduler.py',           'lightdb_studio/lightdb_studio/core/scheduler.py',           'python', 'python', 12000, 380, '任务调度', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'workflow.py',            'lightdb_studio/lightdb_studio/core/workflow.py',            'python', 'python', 10000, 320, '工作流', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'workflow_visual.py',     'lightdb_studio/lightdb_studio/core/workflow_visual.py',     'python', 'python', 3000, 100, '工作流可视化', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'recommender.py',         'lightdb_studio/lightdb_studio/core/recommender.py',         'python', 'python', 10000, 320, '智能推荐', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'assets.py',              'lightdb_studio/lightdb_studio/core/assets.py',              'python', 'python', 8000, 250, '数据资产', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'product_ops.py',         'lightdb_studio/lightdb_studio/core/product_ops.py',         'python', 'python', 10000, 320, '数据产品运营', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'nlb.py',                 'lightdb_studio/lightdb_studio/core/nlb.py',                 'python', 'python', 10000, 320, '自然语言 BI', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'shadow_table.py',        'lightdb_studio/lightdb_studio/core/shadow_table.py',        'python', 'python', 8000, 250, '影子表', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'team.py',                'lightdb_studio/lightdb_studio/core/team.py',                'python', 'python', 8000, 250, '团队管理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'team_config.py',         'lightdb_studio/lightdb_studio/core/team_config.py',         'python', 'python', 6000, 200, '团队配置', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'plugin_manager.py',      'lightdb_studio/lightdb_studio/core/plugin_manager.py',      'python', 'python', 8000, 250, '插件系统', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'notifications.py',       'lightdb_studio/lightdb_studio/core/notifications.py',       'python', 'python', 12000, 380, '通知系统', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'data_science.py',        'lightdb_studio/lightdb_studio/core/data_science.py',        'python', 'python', 15000, 450, '数据科学工具', 'provided', 1);

-- ------------------------------------------------------------
-- 【24】lightdb_studio/ui/  UI 层（约 80 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), '__init__.py',             'lightdb_studio/lightdb_studio/ui/__init__.py',             'python', 'python', 300,  10,  'UI 模块', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'main_window.py',          'lightdb_studio/lightdb_studio/ui/main_window.py',          'python', 'python', 25000, 700, '主窗口', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'object_tree.py',          'lightdb_studio/lightdb_studio/ui/object_tree.py',          'python', 'python', 18000, 540, '对象浏览器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'sql_editor.py',           'lightdb_studio/lightdb_studio/ui/sql_editor.py',           'python', 'python', 8000, 250, 'SQL 编辑器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'result_table.py',         'lightdb_studio/lightdb_studio/ui/result_table.py',         'python', 'python', 6000, 200, '查询结果表格', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'multi_result.py',         'lightdb_studio/lightdb_studio/ui/multi_result.py',         'python', 'python', 3000, 100, '多结果集 Tab', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'cell_viewer.py',          'lightdb_studio/lightdb_studio/ui/cell_viewer.py',          'python', 'python', 6000, 200, '大文本/二进制查看器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'query_history.py',        'lightdb_studio/lightdb_studio/ui/query_history.py',        'python', 'python', 5000, 160, '查询历史面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'query_builder.py',        'lightdb_studio/lightdb_studio/ui/query_builder.py',        'python', 'python', 12000, 380, '可视化查询构建器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'data_editor.py',          'lightdb_studio/lightdb_studio/ui/data_editor.py',          'python', 'python', 15000, 450, '数据编辑器', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'table_designer.py',       'lightdb_studio/lightdb_studio/ui/table_designer.py',       'python', 'python', 10000, 320, '表设计器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'import_export.py',        'lightdb_studio/lightdb_studio/ui/import_export.py',        'python', 'python', 8000, 250, 'CSV 导入/导出', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'import_wizard.py',        'lightdb_studio/lightdb_studio/ui/import_wizard.py',        'python', 'python', 15000, 450, '导入向导', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'chart_view.py',           'lightdb_studio/lightdb_studio/ui/chart_view.py',           'python', 'python', 12000, 380, '基础图表', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'chart_view_enhanced.py',  'lightdb_studio/lightdb_studio/ui/chart_view_enhanced.py',  'python', 'python', 15000, 450, '增强图表', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'dashboard.py',             'lightdb_studio/lightdb_studio/ui/dashboard.py',             'python', 'python', 12000, 380, '仪表盘', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'data_map_view.py',        'lightdb_studio/lightdb_studio/ui/data_map_view.py',        'python', 'python', 12000, 380, '数据地图视图', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'er_diagram.py',           'lightdb_studio/lightdb_studio/ui/er_diagram.py',           'python', 'python', 10000, 320, 'ER 图', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'catalog_panel.py',        'lightdb_studio/lightdb_studio/ui/catalog_panel.py',        'python', 'python', 18000, 540, '数据目录面板', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'quality_v2_dialog.py',    'lightdb_studio/lightdb_studio/ui/quality_v2_dialog.py',    'python', 'python', 15000, 450, '数据质量 2.0 对话框', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'data_quality_dialog.py',  'lightdb_studio/lightdb_studio/ui/data_quality_dialog.py',  'python', 'python', 12000, 380, '数据质量检查', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'assets_dashboard.py',     'lightdb_studio/lightdb_studio/ui/assets_dashboard.py',     'python', 'python', 10000, 320, '数据资产报表', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'impact_dialog.py',        'lightdb_studio/lightdb_studio/ui/impact_dialog.py',        'python', 'python', 8000, 250, '影响分析', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'masking_dialog.py',       'lightdb_studio/lightdb_studio/ui/masking_dialog.py',       'python', 'python', 10000, 320, '脱敏规则配置', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'lineage_view.py',         'lightdb_studio/lightdb_studio/ui/lineage_view.py',         'python', 'python', 12000, 380, '血缘可视化', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'profiler_view.py',        'lightdb_studio/lightdb_studio/ui/profiler_view.py',        'python', 'python', 8000, 250, 'SQL 性能分析视图', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'perf_panel.py',           'lightdb_studio/lightdb_studio/ui/perf_panel.py',           'python', 'python', 10000, 320, '性能面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'perf_monitor.py',         'lightdb_studio/lightdb_studio/ui/perf_monitor.py',         'python', 'python', 10000, 320, '实时性能监控', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'health_dashboard.py',     'lightdb_studio/lightdb_studio/ui/health_dashboard.py',     'python', 'python', 10000, 320, '健康度面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'alerts_panel.py',         'lightdb_studio/lightdb_studio/ui/alerts_panel.py',         'python', 'python', 12000, 380, '告警面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'audit_viewer.py',         'lightdb_studio/lightdb_studio/ui/audit_viewer.py',         'python', 'python', 10000, 320, '审计日志查看器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'scheduler_panel.py',      'lightdb_studio/lightdb_studio/ui/scheduler_panel.py',      'python', 'python', 15000, 450, '任务调度面板', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'snapshot_dialog.py',      'lightdb_studio/lightdb_studio/ui/snapshot_dialog.py',      'python', 'python', 10000, 320, '快照/克隆', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'db_compare.py',           'lightdb_studio/lightdb_studio/ui/db_compare.py',           'python', 'python', 12000, 380, '数据库结构对比', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'migration_wizard.py',     'lightdb_studio/lightdb_studio/ui/migration_wizard.py',     'python', 'python', 15000, 450, '数据库迁移向导', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'ai_chat.py',              'lightdb_studio/lightdb_studio/ui/ai_chat.py',              'python', 'python', 12000, 380, 'AI 对话面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'agent_chat.py',           'lightdb_studio/lightdb_studio/ui/agent_chat.py',           'python', 'python', 10000, 320, 'AI Agent 对话面板', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'ai_panel.py',             'lightdb_studio/lightdb_studio/ui/ai_panel.py',             'python', 'python', 8000, 250, 'AI 助手面板', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'collaboration_panel.py',  'lightdb_studio/lightdb_studio/ui/collaboration_panel.py',  'python', 'python', 10000, 320, '协作面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'plugin_panel.py',         'lightdb_studio/lightdb_studio/ui/plugin_panel.py',         'python', 'python', 7000, 220, '插件管理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'report_builder.py',       'lightdb_studio/lightdb_studio/ui/report_builder.py',       'python', 'python', 12000, 380, '报表构建器', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'review_panel.py',         'lightdb_studio/lightdb_studio/ui/review_panel.py',         'python', 'python', 12000, 380, 'SQL 审查面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'team_config_dialog.py',   'lightdb_studio/lightdb_studio/ui/team_config_dialog.py',   'python', 'python', 8000, 250, '团队配置', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'cloud_sync_dialog.py',    'lightdb_studio/lightdb_studio/ui/cloud_sync_dialog.py',    'python', 'python', 8000, 250, '云同步/备份', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'connection_dialog.py',    'lightdb_studio/lightdb_studio/ui/connection_dialog.py',    'python', 'python', 7000, 220, '连接配置', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'snippet_panel.py',        'lightdb_studio/lightdb_studio/ui/snippet_panel.py',        'python', 'python', 9000, 280, 'SQL 片段库', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'settings_dialog.py',      'lightdb_studio/lightdb_studio/ui/settings_dialog.py',      'python', 'python', 10000, 320, '设置中心', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'environment_bar.py',      'lightdb_studio/lightdb_studio/ui/environment_bar.py',      'python', 'python', 8000, 250, '环境切换条', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'theme.py',                'lightdb_studio/lightdb_studio/ui/theme.py',                'python', 'python', 8000, 250, '主题管理', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_ui'), 'data_grid_enhanced.py',   'lightdb_studio/lightdb_studio/ui/data_grid_enhanced.py',   'python', 'python', 8000, 250, '数据网格增强', 'provided', 1);

-- ------------------------------------------------------------
-- 【25】lightdb_studio/db/  数据库连接（5 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_db'), '__init__.py',    'lightdb_studio/lightdb_studio/db/__init__.py',    'python', 'python', 600,  25,  'DB 模块导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_db'), 'connection.py',  'lightdb_studio/lightdb_studio/db/connection.py',  'python', 'python', 6000, 200, 'DBConnection 封装', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_db'), 'metadata.py',    'lightdb_studio/lightdb_studio/db/metadata.py',    'python', 'python', 4000, 130, '元数据加载', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_db'), 'schema_diff.py', 'lightdb_studio/lightdb_studio/db/schema_diff.py', 'python', 'python', 8000, 250, '结构对比', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_db'), 'table_ops.py',   'lightdb_studio/lightdb_studio/db/table_ops.py',   'python', 'python', 6000, 200, '表操作 SQL 生成', 'provided', 0);

-- ------------------------------------------------------------
-- 【26】lightdb_studio/net/  协作网络（2 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_net'), '__init__.py',     'lightdb_studio/lightdb_studio/net/__init__.py',     'python', 'python', 500,  20,  '网络模块导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_net'), 'collab_server.py','lightdb_studio/lightdb_studio/net/collab_server.py','python', 'python', 12000, 380, 'WebSocket 实时协作', 'provided', 1);

-- ------------------------------------------------------------
-- 【27】lightdb_studio/utils/  工具模块（5 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_utils'), '__init__.py',        'lightdb_studio/lightdb_studio/utils/__init__.py',        'python', 'python', 500,  20,  '工具模块导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_utils'), 'sql_highlighter.py', 'lightdb_studio/lightdb_studio/utils/sql_highlighter.py', 'python', 'python', 8000, 250, 'SQL 语法高亮', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_utils'), 'sql_completer.py',   'lightdb_studio/lightdb_studio/utils/sql_completer.py',   'python', 'python', 8000, 250, 'SQL 自动补全', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_utils'), 'history.py',          'lightdb_studio/lightdb_studio/utils/history.py',          'python', 'python', 4000, 130, '查询历史', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='studio_utils'), 'i18n.py',             'lightdb_studio/lightdb_studio/utils/i18n.py',             'python', 'python', 6000, 200, '多语言', 'provided', 1);

-- ------------------------------------------------------------
-- 【28】lightdb_studio/ 根（3 个）+ plugins（1 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='studio_core'), '__init__.py', 'lightdb_studio/lightdb_studio/__init__.py',        'python', 'python', 300,  10,  'Studio 包导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'config.py',   'lightdb_studio/lightdb_studio/config.py',          'python', 'python', 5000, 160, '连接配置 + 应用设置', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'app.py',      'lightdb_studio/lightdb_studio/app.py',             'python', 'python', 3000, 100, '应用引导', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'example_plugin.py','lightdb_studio/plugins/example_plugin.py',    'python', 'python', 4000, 130, '示例插件', 'provided', 0);

-- ------------------------------------------------------------
-- 【29】web/  Web 版（9 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='web'), '__init__.py', 'web/__init__.py',      'python', 'python', 200,  8,   'Web 包导出', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'server.py',   'web/server.py',        'python', 'python', 5000, 160, 'FastAPI 应用', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='web'), 'api.py',      'web/api.py',           'python', 'python', 15000, 450, 'REST API', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'auth.py',     'web/auth.py',          'python', 'python', 6000, 200, 'JWT 认证', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='web'), 'metrics.py',  'web/metrics.py',       'python', 'python', 2500, 90,  'Prometheus 指标', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'start.py',    'web/start.py',         'python', 'python', 2000, 70,  '启动入口', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'index.html',  'web/static/index.html','html',   'html',   5000, 160, '前端页面', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'app.js',      'web/static/app.js',    'javascript','javascript', 10000, 320, '前端逻辑', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='web'), 'style.css',   'web/static/style.css', 'css',    'css',    5000, 160, '样式', 'provided', 0);

-- ------------------------------------------------------------
-- 【30】docker/  部署（11 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'Dockerfile',                'docker/Dockerfile',                'text',   'text',     2000, 70,  '主 Dockerfile', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'Dockerfile.v3',             'docker/Dockerfile.v3',             'text',   'text',     2000, 70,  'v3 Dockerfile', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'Dockerfile.v4',             'docker/Dockerfile.v4',             'text',   'text',     2500, 90,  'v4 Dockerfile', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'docker-compose.yml',        'docker/docker-compose.yml',        'yaml',   'yaml',     3000, 100, 'Docker Compose', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'docker-compose.v3.yml',     'docker/docker-compose.v3.yml',     'yaml',   'yaml',     2500, 90,  'v3 Compose', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'docker-compose.v4.yml',     'docker/docker-compose.v4.yml',     'yaml',   'yaml',     3000, 100, 'v4 Compose', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'docker-compose.multi.yml',  'docker/docker-compose.multi.yml',  'yaml',   'yaml',     3000, 100, '多进程 Compose', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'prometheus.yml',            'docker/prometheus.yml',            'yaml',   'yaml',     500,  20,  'Prometheus 配置', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'k8s.yaml',                  'docker/k8s.yaml',                  'yaml',   'yaml',     8000, 250, 'Kubernetes 编排', 'provided', 1),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'entrypoint.sh',             'docker/entrypoint.sh',             'shell',  'shell',    1500, 60,  '启动脚本', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'entrypoint-v4.sh',          'docker/entrypoint-v4.sh',          'shell',  'shell',    2000, 70,  'v4 启动脚本', 'provided', 0);

-- ------------------------------------------------------------
-- 【31】dashboards/  Grafana（4 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'lightdb_overview.json',    'dashboards/lightdb_overview.json',    'json', 'json', 8000, 250, '总览面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'lightdb_performance.json', 'dashboards/lightdb_performance.json', 'json', 'json', 10000, 320, '性能面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'lightdb_replication.json', 'dashboards/lightdb_replication.json', 'json', 'json', 6000, 200, '复制面板', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'lightdb_innodb.json',      'dashboards/lightdb_innodb.json',      'json', 'json', 6000, 200, 'InnoDB 面板', 'provided', 0);

-- ------------------------------------------------------------
-- 【32】docs/  文档（15+ 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='storage'), 'USER_GUIDE.md',                  'docs/USER_GUIDE.md',                  'markdown', 'markdown', 0, 0, '用户指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='web'),     'API_REFERENCE.md',               'docs/API_REFERENCE.md',               'markdown', 'markdown', 0, 0, 'API 参考', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'),  'DEPLOYMENT.md',                  'docs/DEPLOYMENT.md',                  'markdown', 'markdown', 3000, 100, '部署指南', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'V3_ROADMAP.md',                  'docs/V3_ROADMAP.md',                  'markdown', 'markdown', 8000, 250, 'v3 路线图', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'V3_RELEASE_NOTES.md',            'docs/V3_RELEASE_NOTES.md',            'markdown', 'markdown', 5000, 160, 'v3 发布说明', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'USER_GUIDE_v3.md',               'docs/USER_GUIDE_v3.md',               'markdown', 'markdown', 0, 0, 'v3 用户指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'ADMIN_GUIDE_v3.md',              'docs/ADMIN_GUIDE_v3.md',              'markdown', 'markdown', 0, 0, 'v3 管理员指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'MIGRATION_GUIDE_v3.md',          'docs/MIGRATION_GUIDE_v3.md',          'markdown', 'markdown', 0, 0, 'v3 迁移指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'PERFORMANCE_WHITEPAPER_v3.md',   'docs/PERFORMANCE_WHITEPAPER_v3.md',   'markdown', 'markdown', 0, 0, 'v3 性能白皮书', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'V4_DECISION.md',                 'docs/V4_DECISION.md',                 'markdown', 'markdown', 6000, 200, 'v4 技术决策', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'V4_RELEASE_NOTES.md',            'docs/V4_RELEASE_NOTES.md',            'markdown', 'markdown', 6000, 200, 'v4 发布说明', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'USER_GUIDE_v4.md',               'docs/USER_GUIDE_v4.md',               'markdown', 'markdown', 0, 0, 'v4 用户指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'ADMIN_GUIDE_v4.md',              'docs/ADMIN_GUIDE_v4.md',              'markdown', 'markdown', 0, 0, 'v4 管理员指南', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'ARCHITECTURE_v4.md',             'docs/ARCHITECTURE_v4.md',             'markdown', 'markdown', 0, 0, 'v4 架构文档', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'MIGRATION_v3_to_v4.md',          'docs/MIGRATION_v3_to_v4.md',          'markdown', 'markdown', 0, 0, 'v3→v4 迁移', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'PERFORMANCE_WHITEPAPER_v4.md',   'docs/PERFORMANCE_WHITEPAPER_v4.md',   'markdown', 'markdown', 0, 0, 'v4 性能白皮书', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'),  'CYTHON_DEV_GUIDE.md',            'docs/CYTHON_DEV_GUIDE.md',            'markdown', 'markdown', 0, 0, 'Cython 开发指南', 'missing', 0);

-- ------------------------------------------------------------
-- 【33】scripts/  脚本（6 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'install.sh',          'scripts/install.sh',          'shell', 'shell', 2000, 70, '安装脚本', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'install.ps1',         'scripts/install.ps1',         'text',  'text',  2000, 70, 'Windows 安装', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'install_v4.sh',       'scripts/install_v4.sh',       'shell', 'shell', 2500, 90, 'v4 安装', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'upgrade.sh',          'scripts/upgrade.sh',          'shell', 'shell', 2000, 70, '升级脚本', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'), 'build_cython.sh',     'scripts/build_cython.sh',     'shell', 'shell', 1500, 60, 'Cython 构建', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'benchmark_v4.sh',  'scripts/benchmark_v4.sh',     'shell', 'shell', 2000, 70, 'v4 基准测试', 'provided', 0);

-- ------------------------------------------------------------
-- 【34】tests/  测试（40+ 个，示例）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_batch1.py',         'tests/test_batch1.py',         'python', 'python', 0, 0, 'Batch 1 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_batch2.py',         'tests/test_batch2.py',         'python', 'python', 0, 0, 'Batch 2 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_batch3.py',         'tests/test_batch3.py',         'python', 'python', 0, 0, 'Batch 3 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_batch4.py',         'tests/test_batch4.py',         'python', 'python', 0, 0, 'Batch 4 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_batch5.py',         'tests/test_batch5.py',         'python', 'python', 0, 0, 'Batch 5 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_v2_batch1.py',      'tests/test_v2_batch1.py',      'python', 'python', 0, 0, 'v2 Batch 1 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_v3_batch1.py',      'tests/test_v3_batch1.py',      'python', 'python', 0, 0, 'v3 Batch 1 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_v4_batch1.py',      'tests/test_v4_batch1.py',      'python', 'python', 0, 0, 'v4 Batch 1 测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'benchmark_v4_batch3.py','tests/benchmark_v4_batch3.py','python', 'python', 0, 0, 'v4 Batch 3 基准', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'benchmark_v4_batch4.py','tests/benchmark_v4_batch4.py','python', 'python', 0, 0, 'v4 Batch 4 基准', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'benchmark_v4_batch5.py','tests/benchmark_v4_batch5.py','python', 'python', 0, 0, 'v4 Batch 5 基准', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'run_all.py',              'tests/regression/run_all.py',  'python', 'python', 0, 0, '回归测试入口', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='sql'),     'test_sql_compat.py',      'tests/regression/test_sql_compat.py',    'python', 'python', 0, 0, 'SQL 兼容测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'test_transaction.py',     'tests/regression/test_transaction.py',   'python', 'python', 0, 0, '事务测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='replication'), 'test_replication.py', 'tests/regression/test_replication.py',   'python', 'python', 0, 0, '复制测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'), 'test_performance.py',   'tests/regression/test_performance.py',   'python', 'python', 0, 0, '性能测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='cython'),  'test_cython_compat.py',   'tests/regression_v4/test_cython_compat.py', 'python', 'python', 0, 0, 'Cython 兼容', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='concurrency'),'test_multiprocess.py', 'tests/regression_v4/test_multiprocess.py', 'python', 'python', 0, 0, '多进程测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='columnar'),'test_columnar.py',        'tests/regression_v4/test_columnar.py',    'python', 'python', 0, 0, '列存测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='parallel'),'test_parallel.py',        'tests/regression_v4/test_parallel.py',    'python', 'python', 0, 0, '并行测试', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='benchmark'),'test_performance_v4.py', 'tests/regression_v4/test_performance_v4.py','python','python',0, 0, 'v4 性能测试', 'missing', 0);

-- ------------------------------------------------------------
-- 【35】根目录文件（10 个）
-- ------------------------------------------------------------
INSERT INTO `lightdb_files` (module_id, file_name, file_path, file_type, language, size_bytes, lines, summary, status, has_issue) VALUES
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'pyproject.toml',         'pyproject.toml',         'toml',   'toml',    2500, 90,  '项目配置', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'requirements.txt',       'requirements.txt',       'text',   'text',    500,  20,  '核心依赖', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'requirements_web.txt',   'requirements_web.txt',   'text',   'text',    500,  20,  'Web 依赖', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='studio_core'), 'README.md',         'README.md',              'markdown','markdown', 12000, 380, '项目说明', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'CHANGELOG_v3.md',       'CHANGELOG_v3.md',        'markdown','markdown', 0, 0, 'v3 变更日志', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'CHANGELOG_v4.md',       'CHANGELOG_v4.md',        'markdown','markdown', 0, 0, 'v4 变更日志', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'RELEASE_v3.0.md',       'RELEASE_v3.0.md',        'markdown','markdown', 0, 0, 'v3.0 发布', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'RELEASE_v4.0.md',       'RELEASE_v4.0.md',        'markdown','markdown', 0, 0, 'v4.0 发布', 'missing', 0),
((SELECT id FROM lightdb_modules WHERE code='deploy'), 'LICENSE',                'LICENSE',                'text',   'text',    1100, 22,  'MIT 许可证', 'fixed', 0),
((SELECT id FROM lightdb_modules WHERE code='storage'), 'lightdb_project.txt',   'lightdb_project.txt',    'text',   'text',    8000, 250, '项目结构树', 'provided', 0),
((SELECT id FROM lightdb_modules WHERE code='tools'),  'create_project.py',      'create_project.py',      'python', 'python', 6000, 200, '项目结构生成脚本', 'provided', 0);

-- ------------------------------------------------------------
-- 【36】验证
-- ------------------------------------------------------------
SELECT '总文件数' AS item, COUNT(*) AS value FROM lightdb_files
UNION ALL
SELECT 'python 文件', COUNT(*) FROM lightdb_files WHERE file_type='python'
UNION ALL
SELECT 'cython 文件', COUNT(*) FROM lightdb_files WHERE file_type='cython'
UNION ALL
SELECT 'markdown 文件', COUNT(*) FROM lightdb_files WHERE file_type='markdown'
UNION ALL
SELECT 'yaml 文件', COUNT(*) FROM lightdb_files WHERE file_type='yaml'
UNION ALL
SELECT 'json 文件', COUNT(*) FROM lightdb_files WHERE file_type='json'
UNION ALL
SELECT 'shell 文件', COUNT(*) FROM lightdb_files WHERE file_type='shell'
UNION ALL
SELECT '已提供', COUNT(*) FROM lightdb_files WHERE status='provided'
UNION ALL
SELECT '已修复', COUNT(*) FROM lightdb_files WHERE status='fixed'
UNION ALL
SELECT '缺失', COUNT(*) FROM lightdb_files WHERE status='missing'
UNION ALL
SELECT '有问题', COUNT(*) FROM lightdb_files WHERE has_issue=1;