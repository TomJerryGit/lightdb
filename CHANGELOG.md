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
- **OLAP 接入 4A.2/4A.3/4A.5（决策 2）**：`lightdb/olap` 与 `lightdb/columnar` **显式接入**
  SQL 执行路径（单表聚合/分组 + 简单 WHERE/ORDER BY/LIMIT 白名单；**2026-10-20 起默认开启**，
  `LIGHTDB_OLAP_DISPATCH=0` 关闭；crosscheck 逐行比对，不一致显式报错；关闭时不加载
  OLAP 引擎模块）。新增 63 例端到端对拍 + 契约测试。并行分片重做：GROUP BY
  100k 行 w1→w2 实测 **2.75x**（纯 SUM 加速不足 1.2x，自动档保持串行）。**4A.5 门禁化**：
  `benchmarks/gate.py` 新增 `olap_parallel_groupby_speedup ≥1.2x` 与
  `olap_dispatch_crosscheck_mismatches ==0` 两条门禁；3 条性能类 skip 换成实测基线阈值
  并恢复正式测试（压缩率 2.5x、聚合 2.0M rows/s、并行 1.2x）。**dispatch 当前无显著性能收益
  （实测 0.44–1.10x），默认开启不代表"SQL 已加速"。**
- **Cython 4A.4（真实编译验证）**：用 `Cython 3.3.0 + ziglang 0.16.0`（D 盘 venv）实际编译
  6 个扩展（`scripts/build_cython_zig.py`，无需 MSVC）；修复 3 处 Cython 3 语法错误
  （row_codec/predicate/hash 的块内 cdef）和 2 个真实 C 层 bug：page 空页
  `next_row_ptr` 未初始化导致 insert_row 堆溢出、hash `remove` 后 `get` 命中残留。
  新增 `tests/test_cython_compiled_parity.py`（编译版↔回退版对拍；无产物环境自动 skip）。
  实测加速：btree 5.9–7.0x、row_codec 2.0–3.3x、predicate ~10–19x、hash ~16–21x、
  page ~20–33x。**产物不入库**（平台相关），Windows 上 Zig/MinGW ABI 仅建议开发验证，
  发布用 MSVC；CI/干净 checkout 自动走纯 Python 回退。
  **⚠️ 长跑暴露问题**：编译产物在单模块功能/对拍/微基准下通过，但一次全量测试长跑出现
  Windows 堆损坏（`0xc0000374`，未定位到单一模块）；因此**运行时不启用编译产物**
  （本机归档、不进仓库、不加载），默认纯 Python 回退；构建链路结论为"本地可编译、
  单模块可用"，不计入性能声明，待 MSVC 构建/后续定位后复验。
- **OLAP 接入 4A.1（正确性）**：修复编码器类型不对称/Dict NULL 错位/Delta 负数/BitPack
  负数/列存统计丢失/GroupBy COUNT(*)=0/AVG 崩溃/Filter 缺 IS NULL/优化器丢 OR/并行
  丢 where 等 12 类缺陷；新增 `tests/test_olap_correctness.py`（91 例行存↔列存对拍）。
- **真 binlog 阶段 2（决策 D）**：`max_bytes` 真实**日志轮转**（旧文件只读、跨文件全局
  lsn、从库跨轮转续传）；**binlog 写失败即拒绝提交**（回滚事务 + 1180/HY000，
  `LIGHTDB_BINLOG_STRICT=0` 可退化排障）；`SHOW MASTER STATUS` 报当前文件名+文件内位点、
  `SHOW BINLOG EVENTS IN 'file'` 可读历史文件；UPDATE/DELETE 镜像走 WAL 增量快路径
  （无触发器时零额外 SELECT，5k 行 A/B 约快 1-2×）；GTID 明确不实现、半同步死代码删除。
  测试：`tests/test_binlog_phase2.py`（19 例）。
- **预处理语句 COM_STMT_***（阶段 1 协议项）：PREPARE/EXECUTE/CLOSE/RESET 全链路，
  二进制参数/结果集编码，覆盖常用类型；参数数不符 1210、未知 statement 1243、不支持类型
  1235；PREPARE 阶段鉴权防元数据泄露；`tests/test_prepared_statements.py`（16 例）。
- **本地全量 GUI + DB 验证（2026-10-20，用户要求）**：真实运行 8898 服务 + Studio，
  数据库矩阵 **34/34**、GUI **45/45 场景 / 77 截图 / 25 对话框**（offscreen +
  windows 平台真实窗口探针）。发现并修复 3 个真实缺陷：
  ① UNION/UNION ALL 经 MySQL 文本协议返回 OK/0 行（`net/server.py` resultset 分支
  未登记 UNION）；② UNION 级 ORDER BY/LIMIT/OFFSET 被静默丢弃（parser 提升到 union
  层 + executor 合并后应用）；③ 并发 DDL 时 `Database.commit/abort` 遍历 tables
  `dictionary changed size during iteration`（改为快照）。新增
  `tests/test_wire_union.py`。登记 3 条 Studio UI 缺陷（结果表数值列按字符串字典序
  排序、无过滤 UI、快照同名不去重）与 2 条已知引擎限制（标量子查询比较右值、
  触发器 NEW 列数值表达式）——均显式报错、不静默错答。报告见
  [`docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md`](docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md)。
- **发布工程**：`CHANGELOG.md`、`docs/RELEASING.md`（发布前门禁/本地 CI 实跑记录/打标签）、
  `scripts/check_repo_hygiene.py`、`.gitattributes`；版本徽章/包元数据统一 4.0.0；
  仓库卫生清理（data_* 残留、遗留 worktree、双 LICENSE、旧调试产物）。

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
  当前基线见 README「测试套件」（0 failed）。

### Known limitations

- 复制：轮转/严格提交已实现；仍**无 GTID**（按位点复制）、**半同步不实现**（死代码已删）、
  无 `binlog.index`（启动 O(n) 扫描）；`binlog_strict` 覆盖 `Database.commit/commit_statement`，
  单行 autocommit 的 binlog 失败仍只告警（阶段 3 统一语句级内部事务）；WAL 与 binlog 非 2PC；
  绕过执行器的直接存储写不产生事件。
- 协议：预处理语句游标/FETCH、`SEND_LONG_DATA` 长参数、BIT/GEOMETRY 参数不支持（显式 1235）；
  `PREPARE` 不做语法校验（在 EXECUTE 回 1064）。
- 授权：账号 host 维度未建模（用户名唯一）；多进程 worker 本地内置分支未本地鉴权；
  PL/触发器按调用者权限执行（比 MySQL DEFINER 严格）；`SHOW VARIABLES`/`SELECT @@var` 未限制。
- OLAP/列存/并行/Cython：**已显式接入 SQL 路径并默认开启**（4A.2；`LIGHTDB_OLAP_DISPATCH=0`
  可关）；dispatch 当前无显著性能收益（0.44–1.10x），默认开启不代表"SQL 已加速"；
  GROUP BY 并行 w2 实测 2.75x，但纯 SUM 加速不足 1.2x（自动档串行）；float SUM/AVG
  分片合并理论末位差异（crosscheck 兜底）；Cython 已本机编译验证（Zig），但产物不入库、
  Windows 上 MinGW ABI 仅建议开发验证（发布用 MSVC）。
- 查询执行单线程；多进程仅连接层并行；`COMMIT` 为 O(表数) fsync。
- WAL 无 CRC、无日志轮转；`vacuum` 不做页内紧凑。
- 桌面端：无 QThread、大结果集同步 `fetchall` 会冻结 UI；连接密码本地明文存储。
