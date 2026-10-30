# Changelog — LightDB 4.0.0

> 本文件汇总 **2026-10-21 ~ 2026-10-28** 的 4.0.0 交付（95 个提交）。更早的
> beta.1 阶段记录见仓库根 [CHANGELOG.md](../CHANGELOG.md)；版本号唯一来源为
> `lightdb/__init__.py::__version__`（= **4.0.0**）。
>
> 验收基线：CI 首次完整全绿（提交 `0949a5c`，**1,598 tests / 0 failed** +
> benchmark / perf / packaging）；本文件所列功能均有对应测试或可复现证据脚本。

---

## [4.0.0] — 2026-10-28（Wave 2 收尾）

### 引擎 / MySQL 协议兼容

- **E1 第三方客户端兼容**：`SHOW` 元数据（FULL TABLES/COLUMNS/KEYS/STATUS/ENGINE/
  CHARSET 等）、静默语义、字符集严格化、`SAVEPOINT` 完整语义。
- **E2 兼容第二批**：`ENGINE` 别名、`ALTER ... MODIFY`、`CREATE INDEX`、
  `SHOW GRANTS`。
- **E3 兼容第三批**：视图定义、`PREPARE/EXECUTE/DEALLOCATE`、`SHOW BINARY LOGS`、
  旧客户端字符集转码。
- **Navicat 10 真实点击缺口修复**：通用 `NOT` 谓词（含 `NOT ISNULL`）、
  `SHOW PROCEDURE/FUNCTION STATUS (WHERE Db)`、`db.tbl` 限定名（含
  `SHOW COLUMNS/KEYS/CREATE/PARTITIONS`）、`SHOW FULL TABLES WHERE
  Table_type != 'VIEW'`、`BINARY` 一元表达式、`information_schema.EVENTS/
  TRIGGERS/ROUTINES`、`SHOW CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT`、
  `BETWEEN` 下推类型安全（数字/字符串/时间同类才下推，不兼容类型显式回退/报错）。
- **视图与 JOIN**：`INNER/LEFT/CROSS JOIN` 物化 + Hash Join；随后改为**视图 JOIN
  内联**（1.14x 手写替代 3.16x 物化，避免物化开销），WHERE/聚合/排序/限行齐全。
- **索引**：单列非唯一索引真实加速（点查 ~40x / 范围 ~14x / 排序 ~220x）；
  多列二级索引最左前缀 / 范围 / ORDER BY（10k 行 **19–246x**）。
- **ALTER 深水区**：`ADD/DROP COLUMN` 完整语义（FIRST/AFTER、默认回填、
  NOT NULL、自增、索引拒绝、崩溃恢复，16 tests）；`ALTER ADD/DROP PRIMARY KEY`
  与 `UNIQUE` 约束变更（重复/NULL/依赖错误码、SHOW 同步、重启/崩溃/备份一致）。
- **AUTO_INCREMENT 真实现**：省略/NULL/多行分配、显式推进、重启保持、
  `SHOW CREATE/STATUS`、`ALTER ... AUTO_INCREMENT`、并发唯一、非法显式拒绝；
  崩溃持久化（区间预留 `.autoinc` + fsync，硬杀绝不复用旧号，吞吐 1.01x）；
  `MODIFY` 省略 `AUTO_INCREMENT` 真正移除。
- **可靠性 / 生产化 A**：WAL checkpoint（大小/条数/时间触发，`SHOW WAL STATUS`）
  + `VACUUM FULL` 压实（磁盘 **-76.5%**）；WAL 命名多段轮转（保留 K 段/跨段恢复）；
  `/metrics`、慢查询日志、审计日志轮转。

### 桌面端 LightDB Studio

- **N1 视觉/交互**：Navicat 风格浅/深/高对比主题、Ribbon 分组工具栏、菜单重排、
  17 个内置 SVG 图标（主题着色/自动刷新）。
- **G1 深度对标**：结果网格、SQL 编辑器、对象树、连接管理、导入导出细节。
- **G2 / G2-b**：连接导入导出、SSL 字段（真正接线）、补全深度、JSON/SQL 导出、
  对象树拖拽、结构同步执行、可视化 EXPLAIN、用户管理、多窗口。
- **G3**：结果表 10 万行完整虚拟化、数据生成器、快捷键自定义、SSL 真实 TLS 补验。
- **表设计器闭环**：AUTO_INCREMENT UI（整数+主键/唯一联动、DDL 生成/回显）、
  最小 ALTER 保存（类型/NULL/默认/自增/起始号，预览+二次确认）、
  增删列保存（ADD FIRST/AFTER、DROP、最小顺序安全 ALTER）、去自增真实移除。
- **AI SQL 助手**：自然语言建表/存储过程生成（parser 校验、预览、确认执行）
  + 7 条离线示例库（13 tests）。
- **SSH 隧道**：paramiko 可选依赖、显式降级；连接/导入导出持久化；进程内
  paramiko SSH server 真实端到端 6 tests（RSA 主机密钥/密码+公钥/direct-tcpip）。
- **发布体验**：崩溃日志/脱敏、首启引导、`--smoke` 离屏冒烟、性能预算门禁、
  `--startup-timing` 启动分析。

### Web 版

- **P0/P1 对标**：表设计器（AUTO_INCREMENT/索引/预览）、CSV/SQL 导入导出、
  EXPLAIN、备份、结构对比、分页排序、连接 SSH/SSL 状态（10 tests）。
- **P2**：图表柱/折线 + 基础 ER 图 + 真实插件面板 + i18n（中英）+ CSV 流式
  导出与导入进度/取消；XSS 收口（5 tests）。
- **P3**：ER 拖拽/缩放/关联高亮交互、图表 tooltip/图例/柱线切换/导出、旧文案
  全量中英 i18n（5 tests）。
- **P3+**：动态 i18n 参数插值（未翻译清单缩至 2 项）、ER 大库按前缀分组按需
  渲染（100 表 1ms）、CSV multipart 流式上传（64 KiB 块 / 20 MB 显式 413）。

### 性能 / 基准

- **P5 统一 benchmark harness**：LightDB 进程内 + 真实 MySQL 协议双路径；
  seed 可复现、JSON 证据、`quick`/`full` 门禁、保守阈值（10 tests，quick PASS）。
- **P5.1 性能深化**：WAL 多段轮转 + 二级索引（点查 40x/范围 14x/排序 220x）；
  并发扩展性如实记录。
- **P3+ 并发扩展性（task-68）**：1/2/4/8/16 客户端 × 读/写小事务/混合矩阵；
  16 客户端并行效率 **3.4–6.5%**；直接表锁探针（读窗口 74.8% 线程时间等锁）、
  cProfile（读 47% 在 SQL 解析；写 95.8% 在 COMMIT flush）与线程栈采样证据；
  测量框架优化 1.81x；`--gate-retries` 与负载敏感阈值标定。
- **说明**：并发指标只报告、不进硬门禁；GIL + 单表锁 + 每事务 flush 是结构性
  限制，引擎级优化（组提交/解析缓存/读写锁拆分/多进程分片）列入 backlog。

### 备份 / 恢复

- **P2 二进制一致性快照**：checkpoint → 拷数据文件 → manifest（SHA256/大小）→
  恢复自检；10k 行 create 3.3s / restore 3.7s（13 tests）。
- **P3 备份增强**：跨版本快照兼容策略（同 format 允许 + WARNING；格式不兼容即使
  `--allow-version-mismatch` 也显式失败）；在线热备 `--online` 两种 quiesce
  后端（进程内写锁 / 跨进程 suspend）；真实并发写下无半提交、WAL=0、快照后写入
  不丢；10k 行在线阻塞窗口 **28–30 ms**（20 tests）。

### 发布工程 / CI

- **Windows 安装包**：Inno Setup 6.7.3 真实构建 `LightDB-Studio-Setup-4.0.0.exe`
  （57,061,774 B，SHA256 `c7a9b8d5…e923b744`，PrivilegesRequired=lowest）；
  `verify_release.py` 发布校验 **PASS=12 FAIL=0 WARN=2 SKIP=2**；安装/卸载人工
  验收记录（install=0 / smoke=0 / uninstall=0 / 目录清理）。
- **分发产物**：`dist/release` manifest 修正为 4.0.0（此前遗留 4.0.1 不一致）；
  onefile 单文件产物 83,175,351 B（版本资源未注入，以 manifest 为准）。
- **自动更新下发（本次新增）**：静态服务器/file:// 提供 `release_manifest.json`
  + 产物；客户端 `check_for_update → download_update`（SHA256/大小校验）；
  三分支验证（无更新 / 新版本 / 校验失败显式）通过；**不实现自动安装**。
- **CI**：首次完整全绿（1,598 tests / 0 failed），Windows self-hosted runner；
  benchmark / perf / packaging 步骤全部通过；JUnit 门禁 + 失败根因修复记录。
- **干净安装验证（本次）**：全新 USERPROFILE/新路径静默安装 → 文件/卸载器/快捷
  方式步骤完成，**Low IL 沙箱拒绝 HKCU 卸载注册表写入**（`RegCreateKeyEx code 5`）
  → 安装器自动回滚且清理干净（PASS）；安装树拷贝形态启动 + 插件加载 + 版本
  4.0.0 验证 PASS；onedir 冒烟/插件加载/首启目录 PASS；onefile 在本会话无法启动
  （退出码 0xFFFFFFFF、无解包目录）**SKIP 待正常会话复验**。

### 文档 / 记录

- 交付记录：`docs/CI_AND_RELEASE_2026-10-26.md`、`docs/analysis_2026-10-19/`
  系列、`docs/CONCURRENCY_SCALING.md`、`docs/SNAPSHOT_BACKUP.md`、P5/P5.1 记录。
- 新增本 `docs/CHANGELOG.md`、`docs/RELEASE_NOTES_4.0.0.md`、路线图收尾
  （`docs/review/04_ROADMAP.md` 末节）。

### 测试增量（本窗口新增/扩展，均进入回归）

| 批次 | 测试文件 | 用例数 |
|---|---|---:|
| E1–E3 / Navicat 修复 | test_show_meta / test_savepoint / test_views / … | 60+ |
| 索引 / WAL / VACUUM / 崩溃 | test_secondary_index / test_wal_checkpoint / test_vacuum_full / test_auto_increment* | 50+ |
| GUI G1–G3 / N1 / 设计器 / AI / SSH | test_studio_* / test_ssh_tunnel | 80+ |
| Web P0–P3+ | test_web_* | 40+ |
| Benchmark（P5 / P3+） | test_benchmark_harness / test_concurrency_scaling | 19+ |
| 备份 P2/P3 | test_binary_snapshot | 20 |
| 发布 / 安装 | test_release_installer / test_studio_release | 25+ |
| 自动更新（本次） | test_auto_update_distribution | 9 |


## [4.0.0] — 2026-10-29（Wave 3 收尾：生产化 / 引擎深水区 / 分片 / 发布刷新）

### 生产化
- `/metrics` 内嵌端点（连接/QPS/错误/慢查询/WAL/checkpoint/备份/池指标，Prometheus 文本 + JSON + /health；默认回环关闭）
- 慢查询日志（阈值 env、环形缓冲 + JSONL 轮转、口令脱敏、`__SLOW_QUERY_LOG__` 可读）
- 审计按大小/条数轮转（先落盘后切、崩溃安全、SHOW AUDIT LOG 行为不变）
- 连接池（线程复用/空闲缩容/健康检查、`LIGHTDB_POOL_*`、超限 1040、排队、池指标）
- **增量复制**（`--follow --interval` 持续追平；失败保留断点不跳过；1236 binlog 缺口显式
  + `--reset-position`；指数退避；状态原子写 + sha256；`/metrics` 复制延迟指标；
  1k 事件约 295 ev/s；单主无自动 failover）

### 引擎深水区
- **视图 JOIN 内联**：简单视图展开为派生表再 Hash Join（1.14x 手写，原物化 3.16x），
  复杂视图安全回退物化；修复物化路径 NULL 键误匹配
- **复合主键 3–4 列** + **BTree v3 页格式**（16B 键、叶 24B/内部 20B）；10k 行
  点查 3.5ms / 前缀 6.0ms / 范围 6.6ms；v1/v2→v3 迁移脚本（真实旧库迁移校验 OK）
- **LIKE 前缀索引**（10k 行 29x）；`%x%` 保持全扫
- **二进制 PREPARE**（COM_STMT_*）：参数类型绑定、会话隔离、1243/1210/1064 错误码、
  RESET、大结果集；SEND_LONG_DATA/游标 FETCH 显式 1235
- **多进程只读分片原型**：主键区间分片 + 快照每 worker 独立进程；100k 行复用快照
  4 worker 端到端 **2.85x**、查询段 3.63x（线程对照 0.96x，确认 GIL 瓶颈）；
  快照准备开销为产品化主要障碍；仅只读/单表/BETWEEN，不支持形态显式 1105

### UI
- Studio/Web 生产面板：复制状态（SHOW SLAVE STATUS + 状态文件回退）、`/metrics` 摘要、
  二进制快照备份/恢复（恢复二次确认、不自动执行）
- Web P3+：ER 拖拽/缩放/关联高亮、图表 tooltip/图例/柱线切换/本地导出、
  动态 i18n（未翻译清单缩至 2 项）、ER 大库按前缀分组按需渲染（100 表 1ms）、
  CSV multipart 流式上传（64KiB 分块，超限 413）
- UI 闭环：Playwright E2E（无浏览器显式 skip）、多窗口生命周期与共享设置、
  快捷键全覆盖/作用域/恢复默认

### 发布工程 / CI
- 安装包与 zip 重新构建（Inno 需绝对路径）；`verify_release` PASS=12/0
- CHANGELOG / RELEASE_NOTES / 路线图定稿；自动更新下发三分支（下载 + SHA256，不自动安装）
- CI：全量套件加 `pytest-timeout`（300s/thread）防挂起；残留清理收窄为 runner 进程特征；
  最终 **1755 tests / 0 failed** + benchmark + GUI perf + packaging 全绿

### 已知限制（新增）
- 复制：单主、无自动 failover、单线程 apply、schema 必须一致
- 分片：仅只读/单表/整数主键 BETWEEN；快照/进程启动开销大，未接入 server 路由
- 复合主键 ≤4 列、每分量 signed-32；BTree v3 需旧库迁移
- 进程内多线程受 GIL + 表锁 + 每事务 flush 限制；组提交/解析缓存/读写锁拆分未落地


## [4.0.0] — 2026-10-30（P4 收尾：增量备份链 / 分片路由 / 复制协议层）

### 备份与恢复
- **增量备份链**（`scripts/backup_chain.py`）：全量基线快照 + **行级差分增量**
  （含真实 AUTO_INCREMENT 值）+ binlog DDL；`restore --to N/--latest` 在 staging
  应用并逐表校验（行数/首末行/全表摘要/SHOW CREATE/自增/WAL=0）通过后才安装目标；
  `prune --keep N` 用 rebase 保持链连续；断链/缺段/篡改/版本不兼容显式失败且不碰目标。
  10k 行：基线 849KB/8.64s，1000 行变化增量 52KB（**6.1%**）/2.92s，恢复链 4.75s。

### 服务端
- **分片接入 MySQL 协议路由**：`LIGHTDB_SHARDING_ENABLED` + `LIGHTDB_SHARDING_MIN_ROWS`
  自动路由可安全分片 SELECT（单表/整数单列主键/BETWEEN/无 JOIN 聚合排序限流子查询）；
  结果集编码与普通查询一致；**快照复用 + 常驻 worker 池**（快照创建从每查询 ~13s
  降为 quiesce 拷贝 ~0.6s，复用后 **4.2x**）；`/metrics` 分片指标；不支持形态/事务/
  写安全回退，worker 崩溃/超时显式 1105。
- **复制协议层**：服务端 `COM_BINLOG_DUMP`（非 GTID；响应为自定义 JSON 流，
  文档明确列出与 MySQL binlog v4 差异，不假装兼容）；binlog 未启用 1381、文件/位点
  1236、权限 1044/1227；`replica_apply.py --protocol` 原始 socket 握手拉取，复用
  幂等 apply/断点/状态文件；断线续传、轮转跨文件、301 ev/s（协议开销 3.7%）。

### CI
- 全量步骤加 `timeout-minutes(50)`；配合 `pytest-timeout` 双层兜底。
- 最终 CI：**1798 tests / 0 failed** + benchmark + GUI perf + packaging 全绿
  （提交 `2d7ad7b`），前序 `0e85388` 同代码亦绿。
- 根因记录：CI 挂起多与本机并行本地全量/其他项目负载争端口有关；CI 运行期间
  不再并行跑本地全量测试。

### 已知限制（P4 新增）
- 增量链：仅静默离线；schema 变更必须能在 binlog 区间捕获；不支持 TRUNCATE/RENAME；
  版本需完全一致；恢复粒度为增量段。
- 分片：原型级单表 BETWEEN，无分布式事务/JOIN 分片；结果=快照时刻（刷新/TTL 控制）。
- 复制协议：非 MySQL binlog v4，GTID 未实现；单主、无自动 failover、单线程 apply。

### 已知限制 / 未完成（详见路线图 backlog）

- 自动更新**不含自动安装/替换**（下载+校验后由用户手动安装）。
- 安装包**未代码签名**（SmartScreen 可能告警）；正式分发需签名证书。
- Low IL/受限会话无法完成真实安装（注册表写）和开始菜单快捷方式验证，需正常
  完整性用户会话复验；onefile 产物同需正常会话复验。
- 进程内并发不扩展（GIL + 表级锁 + 每事务 flush），多进程查询分片属架构级 backlog。
- 复制体系（`Seconds_Behind_Master`、binlog 字节位点/轮转）仍不完整。
- Cython 产物不随发布（需 MSVC 构建；当前默认纯 Python 回退）。
