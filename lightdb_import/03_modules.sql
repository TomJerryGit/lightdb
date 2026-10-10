INSERT INTO `lightdb_modules`
(code, name, category, layer, description, sort_order) VALUES
-- 数据库内核
('storage',      '存储引擎',   '内核', 'kernel', '页管理、B+Tree、WAL、MVCC、锁、事务', 10),
('sql',          'SQL 层',     '内核', 'kernel', '解析、优化、执行、函数库、过程语言', 20),
('db',           '数据库对象', '内核', 'kernel', 'Database、Table、InformationSchema', 30),
('net',          '网络层',     '内核', 'kernel', 'MySQL 协议、服务器、多进程 Master/Worker', 40),
('replication',  '复制',       '内核', 'kernel', 'Binlog、主从、半同步', 50),
('security',     '安全',       '内核', 'kernel', '权限、SSL、审计、密码策略', 60),
('performance',  '性能优化',   '内核', 'kernel', '连接池、查询缓存、批处理、并行', 70),
('concurrency',  '并发原语',   '内核', 'kernel', '读写锁、无锁 MVCC、共享内存、IPC', 80),
('cluster',      '集群协调',   '内核', 'kernel', '共享 Buffer、事务协调、锁协调、统计', 90),
('columnar',     '列式存储',   '内核', 'kernel', 'Chunk、编码器、ColumnStore、Segment', 100),
('olap',         'OLAP',       '内核', 'kernel', '向量化引擎、聚合器、扫描器、优化器', 110),
('parallel',     '并行执行',   '内核', 'kernel', '分片、执行器、工作窃取、资源组', 120),
('cython',       'Cython 加速','内核', 'kernel', 'B+Tree、行编解码、谓词、页操作', 130),
('monitor',      '监控',       '内核', 'kernel', '指标、慢查询、锁分析、索引推荐、健康度', 140),
('stability',    '稳定性',     '内核', 'kernel', '长稳测试、故障注入、一致性、看门狗', 150),
('benchmark',    '基准测试',   '内核', 'kernel', 'Sysbench 类负载、基线对比、报告', 160),
('profiling',    '性能分析',   '内核', 'kernel', 'cProfile、火焰图、GIL、热点排行', 170),
('experiments',  '技术路线',   '内核', 'kernel', '多进程、Cython、子解释器验证', 180),
('tools',        '运维工具',   '内核', 'kernel', 'CLI、备份、诊断、调优、基准', 190),
('backup',       '备份恢复',   '内核', 'kernel', '逻辑备份、物理备份、PITR', 200),
-- Studio 桌面版
('studio_core',  'Studio 核心','Studio', 'studio', 'AI、血缘、治理、质量、协作等 60+ 模块', 300),
('studio_ui',    'Studio UI',  'Studio', 'studio', '主窗口、编辑器、图表、面板等 80+ 模块', 310),
('studio_db',    'Studio DB',  'Studio', 'studio', '连接、元数据、结构对比、表操作', 320),
('studio_net',   'Studio 网络','Studio', 'studio', 'WebSocket 实时协作', 330),
('studio_utils', 'Studio 工具','Studio', 'studio', '语法高亮、补全、历史、i18n', 340),
-- Web 版
('web',          'Web 版',     'Web',    'web',    'FastAPI + JWT + Prometheus', 400),
-- 部署
('deploy',       '部署',       '部署',   'deploy', 'Docker、K8s、Prometheus、Grafana', 500);