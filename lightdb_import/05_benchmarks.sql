INSERT INTO `lightdb_benchmarks`
(scene, v3_value, v4_value, mysql57_value, unit, v3_ratio, v4_ratio) VALUES
('单线程点查 QPS',     6500,  12000, 8000,  'QPS',      81.25, 150.00),
('8 核点查 QPS',       8200,  92000, 28000, 'QPS',      29.29, 328.57),
('并发 TPS',           0,     22000, 5500,  'TPS',      0,     400.00),
('批量插入 rows/s',    0,     150000, 80000, 'rows/s',  0,     187.50),
('复杂 JOIN rows/s',   0,     35000, 28000, 'rows/s',   0,     125.00),
('OLAP 扫描 rows/s',   0,     3500000, 200000, 'rows/s', 0,     1750.00),
('大表聚合 rows/s',    0,     85000000, 500000, 'rows/s', 0,    17000.00),
('事务提交延迟 ms',    1.0,   0.7,   0.5,   'ms',       50.00, 71.43);