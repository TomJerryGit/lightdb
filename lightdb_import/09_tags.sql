-- ============================================================
-- LightDB 资料标签表
-- ============================================================
SET NAMES utf8mb4;

TRUNCATE TABLE `lightdb_file_tags`;
TRUNCATE TABLE `lightdb_tags`;

INSERT INTO `lightdb_tags` (name, color) VALUES
('kernel',        '#569cd6'),   -- 1  数据库内核
('studio',        '#4ec9b0'),   -- 2  Studio 桌面版
('web',           '#dcdcaa'),   -- 3  Web 版
('deploy',        '#ce9178'),   -- 4  部署
('core',          '#89d185'),   -- 5  核心模块
('storage',       '#c586c0'),   -- 6  存储
('sql',           '#9cdcfe'),   -- 7  SQL
('security',      '#f48771'),   -- 8  安全
('performance',   '#d7ba7d'),   -- 9  性能
('ai',            '#c586c0'),   -- 10 AI
('governance',    '#4fc1ff'),   -- 11 数据治理
('monitor',       '#b5cea8'),   -- 12 监控
('test',          '#d16969'),   -- 13 测试
('tool',          '#ce9178'),   -- 14 工具
('critical',      '#ff0000'),   -- 15 严重问题
('cython',        '#ffa500'),   -- 16 Cython
('olap',          '#00bfff'),   -- 17 OLAP
('replication',   '#9370db'),   -- 18 复制
('docs',          '#808080'),   -- 19 文档
('config',        '#a9a9a9');   -- 20 配置

-- ---------- 文件-标签关联 ----------
-- 存储引擎
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/page.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/page.py'), 6),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/btree.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/btree.py'), 6),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/wal.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/wal.py'), 6),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/mvcc.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/storage/mvcc.py'), 6);

-- SQL
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/parser.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/parser.py'), 7),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/optimizer.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/optimizer.py'), 7),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/executor.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/sql/executor.py'), 7);

-- Cython
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/btree_pyx.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/btree_pyx.pyx'), 16),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/row_codec.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/row_codec.pyx'), 16),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/page_pyx.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/page_pyx.pyx'), 16),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/predicate_pyx.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/predicate_pyx.pyx'), 16),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/buffer_pool_pyx.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/buffer_pool_pyx.pyx'), 16),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/hash_table_pyx.pyx'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/cython/hash_table_pyx.pyx'), 16);

-- OLAP
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/vectorized_engine.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/vectorized_engine.py'), 17),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/aggregator.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/aggregator.py'), 17),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/scanner.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/scanner.py'), 17),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/parallel_olap.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/olap/parallel_olap.py'), 17);

-- 安全
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/privileges.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/privileges.py'), 8),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/ssl_support.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/ssl_support.py'), 8),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/audit.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/audit.py'), 8),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/password_policy.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/security/password_policy.py'), 8);

-- 监控
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/metrics_v3.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/metrics_v3.py'), 12),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/slow_query_analyzer.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/slow_query_analyzer.py'), 12),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/health_checker.py'), 1),
((SELECT id FROM lightdb_files WHERE file_path='lightdb/monitor/health_checker.py'), 12);

-- Studio AI
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_integration.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_integration.py'), 10),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_agent.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_agent.py'), 10),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/agent_react.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/agent_react.py'), 10),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_sql_gen.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_sql_gen.py'), 10),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_finetune.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/ai_finetune.py'), 10);

-- Studio 治理
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/lineage.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/lineage.py'), 11),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/lineage_enhanced.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/lineage_enhanced.py'), 11),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/catalog.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/catalog.py'), 11),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_contract.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_contract.py'), 11),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_mesh.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_mesh.py'), 11),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_fabric.py'), 2),
((SELECT id FROM lightdb_files WHERE file_path='lightdb_studio/lightdb_studio/core/data_fabric.py'), 11);

-- Web
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='web/server.py'), 3),
((SELECT id FROM lightdb_files WHERE file_path='web/api.py'), 3),
((SELECT id FROM lightdb_files WHERE file_path='web/auth.py'), 3),
((SELECT id FROM lightdb_files WHERE file_path='web/auth.py'), 8),
((SELECT id FROM lightdb_files WHERE file_path='web/metrics.py'), 3),
((SELECT id FROM lightdb_files WHERE file_path='web/metrics.py'), 12),
((SELECT id FROM lightdb_files WHERE file_path='web/start.py'), 3);

-- 部署
INSERT INTO `lightdb_file_tags` (file_id, tag_id) VALUES
((SELECT id FROM lightdb_files WHERE file_path='docker/Dockerfile'), 4),
((SELECT id FROM lightdb_files WHERE file_path='docker/docker-compose.yml'), 4),
((SELECT id FROM lightdb_files WHERE file_path='docker/k8s.yaml'), 4),
((SELECT id FROM lightdb_files WHERE file_path='docker/prometheus.yml'), 4);