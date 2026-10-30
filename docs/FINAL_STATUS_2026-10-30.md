# LightDB 项目最终状态（2026-10-30）

## 1. 交付概况

LightDB 4.0.0：纯 Python 实现的 MySQL 兼容数据库引擎 + 桌面端 LightDB Studio + Web 版，
已具备安装包（Inno Setup）、自动更新 manifest、增量/全量备份、复制原型、分片原型、
可观测性（/metrics、慢查询、审计轮转）与完整 CI（自托管 runner）。

- 版本单一来源：`lightdb/__init__.py::__version__ = 4.0.0`
- 仓库远端：`https://github.com/TomJerryGit/lightdb`（main 已含 beta.127 文档提交）
- CI：**1798 tests / 0 failed / 0 error / 46 skipped** + benchmark + GUI perf +
  packaging 全部通过（提交 `2d7ad7b`；`0e85388` 同代码亦绿）

## 2. 发布产物

| 产物 | 位置 | 大小 | SHA256 |
|---|---|---|---|
| 安装包 | `dist/installer/LightDB-Studio-Setup-4.0.0.exe` | 59,815,236 B | `a55957b3…084b` |
| 便携 zip | `dist/release/LightDB-Studio-4.0.0-win64.zip` | 86,381,744 B | `f6b96e32…d3ce` |
| 便携目录 | `dist/studio/LightDB-Studio/` | — | — |

- `verify_release.py`：PASS=12 / FAIL=0 / WARN=2（未签名，无证书按需求忽略）/ SKIP=2
- 安装/启动/卸载冒烟（管理员会话，新安装包）：**0 / 0 / 0**，目录清理 True
- 自动更新：manifest 检查/下载/SHA256 校验三分支通过（不自动安装）

## 3. 已交付能力（按迭代）

| 方向 | 内容 |
|---|---|
| 引擎核心 | WAL 多段轮转+checkpoint、AUTO_INCREMENT 持久化、二级索引（单列/多列最左前缀/LIKE 前缀）、ALTER 增删列/主键/唯一、复合主键 3–4 列 + BTree v3（旧库迁移）、视图 JOIN 内联、BTree v2/v3 迁移、二进制 PREPARE、TRUNCATE/VACUUM 等 |
| 兼容性 | MySQL 协议/元数据/SHOW/SAVEPOINT/E1–E3、Navicat 10 全流程（连接/对象树/表列表/表数据/设计器/备份执行）真实点击验收 PASS |
| 生产化 | `/metrics`（连接/QPS/错误/慢查询/WAL/checkpoint/池/分片/复制）、慢查询日志、审计轮转、连接池、binlog 增量复制（follow/断点/1236/延迟指标）、复制协议层（COM_BINLOG_DUMP 自定义流） |
| 备份 | 全量二进制快照（一致性校验/恢复自检）、**增量备份链**（基线+行级差分+恢复+保留策略） |
| 性能 | 多列索引 19–246x、LIKE 前缀 29x、视图 JOIN 内联 1.14x 手写、分片原型 100k 行 4 worker 2.85x（复用快照后服务端 4.2x） |
| 客户端 | Studio（N1 主题/Ribbon/图标、表设计器、SQL 编辑器、AI SQL 助手、SSH/SSL、多窗口、快捷键）、Web（P0–P3：设计器/导入导出/EXPLAIN/备份/结构对比/图表/ER/插件/i18n/流式）、生产面板（复制状态/metrics/快照） |
| 发布工程 | Inno 安装包、便携 zip、manifest、自动更新下发、CHANGELOG/RELEASE_NOTES、干净环境安装核验、CI 稳定性 |
| 验证 | Navicat 真实点击、真实 SSH 隧道（进程内 paramiko server）、Playwright E2E（无浏览器显式 skip）、安装/卸载冒烟、基准与并发分析 |

## 4. 已知限制 / 剩余 backlog

- **复制**：单主、无自动 failover、单线程 apply、schema 必须一致；协议流非 MySQL binlog v4
- **分片**：原型级（只读/单表/整数主键 BETWEEN），快照准备开销与进程启动仍显著
- **并发**：进程内多线程受 GIL + 表锁 + 每事务 commit flush 限制（已量化，组提交/解析缓存/读写锁仅建议）
- **引擎边界**：触发器/事件/外键/CHECK/FULLTEXT 显式拒绝；复合主键 ≤4 列 signed-32；
  多列 LIKE '%x%' 全扫；无 INSERT ... SELECT
- **发布**：代码签名无证书（SmartScreen WARN）；MSI 未构建；onefile 需正常会话复验
- **UI**：真浏览器 E2E 因环境无法安装 Chromium 仅显式 skip；旧 Web 动态文案仍有 2 项不可译
- **其他**：`dashboards/*.json` 为其他会话改动（未处理）

## 5. 关键文档索引

- `docs/CHANGELOG.md`、`docs/RELEASE_NOTES_4.0.0.md`、`docs/review/04_ROADMAP.md`
- `docs/CI_GREEN_2026-10-29_WAVE2.md`、`docs/CI_GREEN_2026-10-30_WAVE4.md`
- `docs/NAVICAT10_ACCEPTANCE_2026-10-27.md`、`docs/INSTALLER_ACCEPTANCE_2026-10-27.md`
- `docs/MIGRATION_BTREE_V2.md`、`docs/SNAPSHOT_BACKUP.md`、`docs/INCREMENTAL_BACKUP.md`
- `docs/PRODUCTION_READINESS.md`、`docs/CONCURRENCY_SCALING.md`、`docs/MULTIPROCESS_SHARDING.md`
- `docs/RELEASE_2026-10-29_WAVE3.md`、`docs/DELIVERY_2026-10-27_R3_P2.md`
