SET NAMES utf8mb4;

-- ========== 版本 ==========
TRUNCATE TABLE `lightdb_versions`;
INSERT INTO `lightdb_versions` (version, codename, release_date, summary, mysql_compat, score) VALUES
('1.0.0', '基础版', '2024-06-01', 'MySQL 协议 + 基础 SQL + 存储引擎 + 事务 + 复制 + 权限', '基础', 3.5),
('2.0.0', 'Enterprise Ready', '2025-02-01', '补齐 MySQL 8.0 特性：窗口函数、CTE、存储过程、JSON、全文索引、分区表', '~90%', 5.0),
('3.0.0', 'Production Ready', '2025-02-01', '性能提升 33%，稳定性到 30 天稳定，43 个核心指标', '80%', 7.0),
('4.0.0', 'Performance Breakthrough', '2025-08-01', '突破 GIL，多进程 + Cython + 列式存储 + 并行查询 + HTAP', '125%', 8.8);

-- ========== 模块 ==========
TRUNCATE TABLE `lightdb_modules`;
INSERT INTO `lightdb_modules` (code, name, category, layer, description, sort_order) VALUES
('storage', '存储引擎', '内核', 'kernel', '页管理、B+Tree、WAL、MVCC、锁、事务', 10);

-- ========== 已知问题 ==========
TRUNCATE TABLE `lightdb_issues`;
INSERT INTO `lightdb_issues` (module_code, file_name, severity, category, title, description, suggestion) VALUES
('concurrency', 'shared_memory.py', 'critical', 'bug', '跨进程原子性不足', 'atomic_increment 仅用 threading.Lock，多进程同时自增会丢更新', '改用 multiprocessing.Lock 或 fcntl.flock');

-- ========== 文件 ==========
TRUNCATE TABLE `lightdb_files`;
-- 共 0 个文件

