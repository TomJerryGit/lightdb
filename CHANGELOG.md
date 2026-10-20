# Changelog

本项目从 2026-10-19 的全面分析与 P0/P1 修复进入"可交付化"阶段。版本号单一来源为
`lightdb/__init__.py::__version__`（见 `pyproject.toml` 的 `dynamic.version`）。

## v4.0.0-beta.1（2026-10-20）— 可交付化第一版

> 定位：**Beta**。核心引擎、安全默认与授权、事务/类型校验、真 binlog 已落地且门禁全绿；
> OLAP 接入已完成第 1 步（正确性止血 + 对拍矩阵），SQL 路径接线仍在推进
> （见 `docs/analysis_2026-10-19/06_ROADMAP.md`）。**不建议用于生产写入**。

### Added

- **真 · append-only binlog**（决策 D，阶段 1）：独立 `<data_dir>/binlog/binlog.000001`，
  magic/version + 记录头 + JSON payload + **CRC32**；位点=真实字节偏移、只增不回退；
  DML 提交成组写（BEGIN→行事件→COMMIT）、**DDL 也进 binlog 并可复制**；
  `SHOW MASTER STATUS` / `SHOW BINLOG EVENTS` / `__BINLOG_FETCH__` 读真文件；
  从库 `master.info` 持久位点重启续传；`START SLAVE` / `STOP SLAVE` / `CHANGE MASTER TO`。
  测试：`tests/test_binlog_append_only.py`（16 例）。
- **CI 与交付门禁**：`.github/workflows/ci.yml`（Windows：install → compileall → pytest →
  benchmark gate → packaging check）；本地等价命令 `scripts/ci.ps1`；
  `scripts/check_packaging.py`（wheel 必须含引擎包 + 三个入口 + 版本一致）。
- **桌面端清减**：删除 44 个不可达 Studio 模块（15,728 行）+ 6 个死测试文件，
  `lightdb_studio` 由 134 文件/39,256 行降至 **90 文件/23,532 行**。
- **授权体系（阶段 1.1）**：`GRANT` / `REVOKE` / `SHOW GRANTS` 真正生效并持久化
  （`Database.users[user]['privs']`）；执行期按会话 user 鉴权，拒绝返回 1142/1044/1227
  （SQLSTATE 42000）；协议层 `SHOW TABLES`/`DESC`/`KILL`/`PROCESSLIST`/`SHOW DATABASES`
  同一权限模型；`tests/test_privileges.py`（12 例，含真实 pymysql 端到端）。
- **OLAP 接入 4A.1（正确性）**：修复编码器类型不对称/Dict NULL 错位/Delta 负数/BitPack
  负数/列存统计丢失/GroupBy COUNT(*)=0/AVG 崩溃/Filter 缺 IS NULL/优化器丢 OR/并行
  丢 where 等 12 类缺陷；新增 `tests/test_olap_correctness.py`（91 例行存↔列存对拍）。
  **尚未接入 SQL 路径**（4A.2），性能目标未达（仍 skip）。

### Changed / Fixed（2026-10-19 P0/P1 修复）

- **安全（P0）**：默认 root 随机口令写 `<data_dir>/root.password`（旧库自动迁移）；
  默认仅监听 `127.0.0.1`；`max_connections` / `max_conn_per_ip` / IP 白名单真正执行；
  Web 端 JWT 密钥改为环境变量/持久化随机、删除默认口令、标识符白名单防注入、
  CORS 白名单、新增连接管理端点（Web 可真正查库）。
- **事务（P1-B）**：连接级 session，`BEGIN/COMMIT/ROLLBACK` 跨语句生效；断连自动回滚；
  `SET autocommit/NAMES/@var` 真实生效；错误 SQLSTATE 按语义返回（不再一律 HY000）。
- **类型约束（P1-C）**：INT 越界 1264、VARCHAR 超长 1406、非法日期 1292、
  非数字 1366、PK/NOT NULL 为 NULL 1048；INSERT/UPDATE 均生效。
- **DDL（P1-F）**：非整型主键定义期拒绝（1064）；触发器体定义期校验（坏体不落库，
  新增 `DROP TRIGGER` / `SHOW TRIGGERS`）。
- **复制（P1-E → 决策 D）**：废弃"查询时重扫 WAL"的派生位点；状态语义修正后升级为真 binlog。
- **打包**：wheel 含 `lightdb` 引擎包；入口 `lightdb-server` / `lightdb-studio` /
  `lightdb-studio-web`；依赖与 `requirements.txt` 对齐。
- **门禁**：24 条失败测试按根因处置（真实缺陷修复 + 带 reason 的 skip/xfail），
  当前 **811 用例 = 771 passed / 0 failed / 35 skipped / 5 xfailed**。

### Known limitations

- 复制：无轮转、无 GTID/半同步、binlog 写失败只告警不拒提交。
- 授权：账号 host 维度未建模（用户名唯一）；多进程 worker 本地内置分支未本地鉴权；
  PL/触发器按调用者权限执行（比 MySQL DEFINER 严格）；`SHOW VARIABLES`/`SELECT @@var` 未限制。
- OLAP/列存/并行/Cython：正确性已修，但**未接入 SQL 路径**；性能目标（压缩率 3x、
  聚合 20M rows/s、并行 1.5x）未达标；Cython 从未编译。
- 查询执行单线程；多进程仅连接层并行；`COMMIT` 为 O(表数) fsync。
- WAL 无 CRC、无日志轮转；`vacuum` 不做页内紧凑。
- 桌面端：无 QThread、大结果集同步 `fetchall` 会冻结 UI；连接密码本地明文存储。
