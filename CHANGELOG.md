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
- **GUI 优化路线 P0 已实施（2026-10-20，用户要求）**：六项正确性/安全快赢全部落地：
  ① 结果表**数值/日期感知排序**（NULL 恒末尾，表头双向，88.5<270.5）；
  ② **结果过滤条**（列选择+大小写+清空+空态，1200 行过滤 40.6ms，不查库）；
  ③ **快照唯一性**（同名自动 `_2/_3` 并返回实际名、remove 删除全部同名、list_all
  去重、旧索引迁移告警）；
  ④ **本地口令加密**（新增 `core/credentials.py`：Windows DPAPI ctypes / 非 Windows
  Fernet / 显式 plain 回退；`connections.json` 不再明文，旧明文首次加载自动迁移，
  解密失败保留连接并显式告警）；
  ⑤ 连接对话框**口令显示/隐藏**（“测试连接”沿用 2003/1045 可行动错误提示）；
  ⑥ **错误面板统一**（查询/权限/连接分类 + 复制按钮），并修复 `show_message()`
  陈旧错误视图（旧 xfail 转正）；附带修复 `lightdb_studio/__init__.py` 包级延迟导入
  `from . import X` 的 RecursionError。
  新增 `lightdb_studio/core/credentials.py` 与
  `tests/test_studio_credentials.py`（8）/`tests/test_studio_p0_ui.py`（12）/
  `tests/test_snapshot_unique.py`（10）；真实 GUI 端到端 4/4 PASS、10 张截图。
  路线图与验收记录见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §9。
- **GUI 路线 P1 首批（P1-1/P1-2，2026-10-21）**：查询执行移出 UI 线程（`ui/workers.py`
  QThreadPool + 信号回主线程；F5/菜单/Ctrl+Return/对象树双击/EXPLAIN 全部走后台，执行中
  禁用动作+状态提示），新增“取消当前查询”（记录 `thread_id`、另一条连接 `KILL`、显式
  提示、自动重连 + 3s 兜底）与 `settings.query_timeout` 超时接线；结果读取改为
  `fetchmany` 分块 + `max_result_rows` 截断提示 + “加载更多”追加（不重写
  QAbstractTableModel，留待后续）；P0 排序/过滤/错误面板全部保留。
  新增 `tests/test_studio_async_query.py`（9 例，随机端口临时服务端）；Studio 回归 +
  新测试 69 passed；真实 GUI `p1_ui_check.py` 5/5 PASS、4 张截图。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §10。
- **GUI 路线 P1 第二批（P1-3/P1-6，2026-10-21）**：
  ① SQL 编辑器**自动补全**（QCompleter；关键字+表/列，`表.` 与最近 FROM/JOIN 上下文；
  尊重 `settings.editor_auto_complete=False`，Ctrl+Space 手动触发；不阻塞 UI 的 schema 后台缓存）；
  ② **SQL 错误位置高亮**（解析 `near '<token>'`/`at line N`/`Unknown column '<c>'`，
  选中并红底红下划线标记；定位不到不伪造；成功/新 SQL 清除）；
  ③ 对象树**懒加载**（展开表才查列/索引/触发器并缓存，重复展开不重查；100 张表展开
  0 列查询、~百 ms）+ **右键菜单**（复制表名/库名、生成 `SELECT * FROM t LIMIT 200`
  到新查询页、查看建表语句/列、刷新此表、统计/删除/清空）。
  新增 `tests/test_studio_p1_editor_tree.py`（19 例）；Studio 回归 + 新测试 88 passed；
  真实 GUI `p1_editor_tree_check.py` 3/3 PASS、6 张截图。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §11。
- **GUI 路线 P1 第三批（P1-4/P1-5，2026-10-21）**：
  ① 导入/导出**进度与取消**：500 行/批后台 worker + 进度条 + 取消（导入取消不回滚已提交
  批、显式提示；失败行逐行重试定位并可导出 CSV 报告；导出 CSV/XLSX 取消/失败删除不完整文件）；
  ② DataEditor **按列类型编辑**（DATE/DATETIME 校验规范化、BOOL 下拉、显式 NULL、INT/FLOAT
  数值、JSON 非法报错；非法单元格标红且阻止提交）+ **单事务原子提交**（BEGIN→逐条 UPDATE→
  COMMIT，失败 ROLLBACK + 显式提示；单条/批量同路径）。
  新增 `tests/test_studio_p1_import_editor.py`（19 例）；Studio 回归 + 新测试 107 passed；
  真实 GUI `p1_import_editor_check.py` 3/3 PASS、5 张截图。
  附带修复 `DBQueryError` 在 errno=None 时 `__str__` 递归（`error_message` 优先取 message）。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §12。
- **GUI P2 可视化（P2-2/P2-3，2026-10-21）**：
  ① **图表可配置化**（`ui/chart_view.py`）：`ChartConfig` + 多 Y 系列；X 轴列、Y 数值列、
  SUM/AVG/COUNT/MIN/MAX 聚合（重复 X 聚合、NULL 忽略、唯一 X 原值）、bar/line/pie/scatter
  + 标题实时重绘 + 图例；无有效数值列显式提示；**PNG/SVG 导出**（文件真实可解析）；
  配置 JSON save/load 并兼容旧单 y 配置，可写入报表模板 `chart` 字段。
  ② **报表设计器产品化**（`core/reports.py` + `ui/report_builder.py`）：`:name`/`?`
  参数化查询（字符串/注释内占位符不误伤；缺参/多参/混用显式 `ReportParameterError`）、
  参数输入对话框；模板 `params`/`preview_limit` + validate/upsert/rename/duplicate/remove；
  内嵌预览 50/100/500 行；HTML（转义）/CSV（utf-8-sig）/XLSX（openpyxl 可读）导出；
  旧模板缺字段自动默认。
  新增 `tests/test_studio_p2_visuals.py`（24 passed + 1 skipped，真实服务不可用时显式 skip）；
  Studio 回归 + 新测试 132 passed / 1 skipped；真实 GUI `p2_visual_check.py` 3/3 PASS、4 张截图。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §13。
- **GUI P2 元数据（P2-1/P2-4，2026-10-21）**：
  ① **ER 图真实关系**（`ui/er_diagram.py`）：真实 `SHOW CREATE TABLE` FOREIGN KEY 解析；
  用户手工关系 `RelationStore`（`relations.json` 持久化、可增删 round-trip）；三态优先级
  真实 FK > 手工 > 无关系，**禁止按 `_id`/命名猜测**；表盒 PK/FK/UQ/IDX + 关系连线/箭头，
  无关系显式提示；PNG 导出（可读校验）。
  ② **Schema 对比产品化**（`ui/db_compare.py`）：真实列/类型/NOT NULL/默认值/PK/唯一/索引
  差异矩阵；SQL 预览安全子集（CREATE TABLE、缺失索引 CREATE INDEX、高风险 DROP TABLE），
  表内变更显式“需手工处理（本引擎 ALTER 限制）”不生成假 ALTER；diff SQL 落盘；高风险
  DDL 二次确认；执行走目标连接并展示结果/错误。
  新增 `tests/test_studio_p2_schema.py`（15 passed）；Studio 回归 + 新测试
  **147 passed / 1 skipped**；真实 GUI `p2_schema_check.py` 3/3 PASS、5 张截图。
  已知限制：本引擎 parser 不支持独立 `CREATE INDEX`（预览保留语句、执行会显式报错；
  实际请在建表内联索引）。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §14。
- **GUI P2 备份（P2-5，2026-10-21）**：
  ① **保留策略** `SnapshotManager.prune(keep_last, max_age_days, dry_run)`：dry-run 候选清单、
  真实删除文件+索引、删除失败显式且保留索引条目；P0 同名 `_2/_3` 不误删；
  ② **定时快照**：快照目录 `schedule.json`（enabled/interval/keep_last/max_age/prefix/last_run/
  last_error）+ `due/schedule_status/run_due`；主窗口 60s QTimer 调 `run_due`（未启用/无连接跳过，
  失败写 stderr），无常驻线程；
  ③ **恢复预览与原子回滚**：`preview_restore` 统计 CREATE/DROP/INSERT 与风险；真实恢复前自动
  `pre_restore_<ts>` 紧急快照（失败中止恢复），恢复失败自动回滚并显式 `rolled_back`/
  `rollback_error`（回滚自身再失败只显式报告）；
  ④ UI：`ui/snapshot_dialog.py` 保留/定时设置面板、prune 预览+执行、恢复预览、运行到期检查、
  结果显式含紧急快照/回滚状态。
  新增 `tests/test_studio_p2_backup.py`（17 passed，含主窗口 QTimer 接线测试）；指定回归 +
  新测试 53 passed；全 studio 套件 163 passed/1 skipped；真实 GUI `p2_backup_check.py`
  4/4 PASS、4 张截图。路线图见
  [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §15。
- **GUI P2 插件 API（P2-8，2026-10-21）**：
  ① 示例插件 `plugins/example_plugin.py`（菜单“示例：插入 SELECT 1”+ 工具栏 + 安全
  `process_sql` + on_load/on_unload 日志）；
  ② `PluginManager` 产品化：`PluginInfo.status=loaded/disabled/failed`；`load_all` 每插件
  独立失败、error 可见不影响其他；`enable/disable`（disable 调 on_unload、移出 active/菜单/
  工具栏、状态持久化到可注入 `state_path`）；`reload_one/reload_all`（unload→清 sys.modules
  →重新 import→on_load，禁用保持禁用）；`build_menu_actions/build_toolbar_actions/
  install_actions(main_window)` + `add_listener`；process_sql 异常隔离；
  ③ 插件管理面板：name/version/status/path/error 列表 + 启用/禁用/重载/刷新/打开目录/安装，
  操作结果显式；
  ④ 主窗口 `MainWindow._install_plugin_actions()` 在启动恢复时安装插件 action（失败写
  stderr，只装一次避免重复）。
  新增 `tests/test_studio_p2_plugin.py`（16 passed，含主窗口安装接线）；全 studio 套件
  179 passed/1 skipped；compileall OK；真实 GUI `p2_plugin_check.py` 3/3 PASS、5 张截图。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §17。
- **GUI P2 i18n/可访问性（2026-10-21）**：
  ① `utils/i18n.py`：zh_CN/en_US 扩到 **134 key 且集合完全一致**（主窗口菜单/工具栏/状态栏/
  标签页/空态/设置对话框）；新增 `t_strict`/`missing()`/`missing_report()`/`check_coverage()`
  等；未知 key 原样返回并记录（不崩溃）、未知语言拒绝告警；
  ② 语言**热切换**：`main_window._retranslate_ui()` 即时刷新菜单/工具栏/状态栏/标签页/空态/
  accessibleName；设置对话框语言 Apply 即持久化 `settings.json` 并立即切换（新窗口读持久化值）；
  ③ 可访问性：关键控件 accessibleName/tooltip、合理 Tab 顺序（对象树→编辑器→结果表→标签→
  右侧面板）；高 DPI 明确“由系统/Qt6 自动控制”，不提供假设置；
  ④ 测试 `tests/test_studio_p2_i18n_a11y.py`（8 passed）；全 studio 套件 188 passed/1 skipped；
  真实 GUI `p2_i18n_check.py` 3/3 PASS、5 张中英截图。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §18。
- **GUI P2 Web/GUI 能力对齐（2026-10-21）→ P2 全部完成**：
  ① Web 新增 3 类共享 `lightdb_studio/core` 的能力：`POST /api/v1/sql/format`
  （core.sql_formatter）、报表模板 CRUD + 参数化 preview + html/csv/xlsx 导出
  （core.reports，模板目录可注入）、`POST /api/v1/quality/check`（core.data_quality）；
  ② 新端点沿用现有 token/权限，统一结构 JSON 错误（400/401/403），参数经 `%(name)s`
  安全绑定，不手拼 SQL；
  ③ `WEB_GUI_PARITY` 12 项矩阵（4 full / 2 partial / 6 not_applicable，GUI-only 能力
  明确标注原因），文档 `docs/WEB_GUI_PARITY.md` 由矩阵生成；
  ④ 测试 `tests/test_web_gui_parity.py`（10 passed）+ 现有 web 安全 22 = 32 passed；
  真实 TestClient `p2_web_parity_check.py` 4/4 PASS（report/parity_matrix/html/csv/xlsx）。
  路线图见 [`docs/GUI_OPTIMIZATION_ROADMAP.md`](docs/GUI_OPTIMIZATION_ROADMAP.md) §19。
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

### Fixed（2026-10-21，GitHub Windows runner 兼容）

- **`DATE_FORMAT` 非 ASCII 字面量**：`%Y年%m月%d日` 在 cp1252 环境（GitHub
  windows-latest）下 `strftime` 抛 `UnicodeEncodeError`，旧实现返回 `None`。
  新增 `datetime_funcs._format_dt()`：先尝试平台 `strftime`，失败时把非 ASCII
  字面量替换为 ASCII 占位符、格式化后还原；`DATE_FORMAT` / `FROM_UNIXTIME`
  改用它。新增回归测试
  `tests/test_v3_batch4.py::test_date_format_unicode_literals_on_ascii_locale`
  （用 wrapper 模拟 `UnicodeEncodeError`，不依赖运行机器的 locale）。
- **GitHub CI 输出编码**：workflow 加 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`；
  `scripts/ci_annotate_pytest.py` 强制 UTF-8 输出（注解步骤此前也因中文失败）。
- **多进程 Worker 元数据**：`RemoteDB` 补 `database_names()`/`visible_table_names()`，
  Master 快照带 `database_names`/`visible_tables`，修复 worker 上 `SHOW TABLES`
  1049 "No database selected"。
- **服务端 TLS 已实现**（P2 安全项）：`MySQLServer` 握手宣告 `CLIENT_SSL`、识别
  32 字节 SSLRequest 后 wrap socket 并在 TLS 通道继续认证；证书来源
  `ssl_cert`/`ssl_key` 或 `<data_dir>/ssl`（`SSLManager`/openssl）；不可用时启动
  显式报错不静默降级；TLS 可选（明文客户端仍可连）。测试 `tests/test_wire_ssl.py`
  用 `cryptography` 自签证书；`tests/test_batch14.py::test_ssl` 恢复端到端断言。

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
