# LightDB Studio 4.0.0 发布说明

> 版本：**4.0.0**（2026-10-28）· 平台：Windows x64 · 状态：**正式发布候选**
> 详细技术变更见 [CHANGELOG.md](CHANGELOG.md)；完整基准见
> [benchmarks/README.md](../benchmarks/README.md)。

LightDB 4.0.0 是一个**纯 Python 实现的 MySQL 兼容数据库引擎 + 桌面管理工具
（LightDB Studio）+ Web 版**。本版本把"可演示"推进到"可安装、可日常使用"：

- MySQL 协议/元数据/DDL/DML 兼容性覆盖到 Navicat 10 真实操作链路；
- 桌面端完成 Navicat 风格界面、表设计器闭环、AI SQL 助手、SSH/SSL 连接；
- Web 版补齐设计器/图表/ER 图/导入导出/中英文切换；
- 引擎具备真实二级索引、WAL checkpoint、一致性备份与崩溃恢复；
- 提供 Windows 安装包与自动更新**下载**（不自动安装）。

---

## 一、亮点

### 1. 桌面端（LightDB Studio）
- **连接**：MySQL 协议连接、SSH 隧道、SSL、连接配置导入/导出。
- **对象树 / 表数据**：浏览、分页/排序、10 万行结果虚拟化不卡顿、多窗口。
- **表设计器**：新建/编辑表，列类型、NULL、默认值、AUTO_INCREMENT、索引、
  主键/唯一键修改，DDL 预览 + 二次确认 + 真实引擎错误展示。
- **SQL 编辑器**：补全、格式化、执行/取消、结果导出（CSV/JSON/SQL）、
  可视化 EXPLAIN、结构对比、备份。
- **AI SQL 助手**（离线示例库 + 可配置模型）：自然语言生成建表/存储过程 SQL，
  先预览校验再执行，不会静默改数据。
- **体验**：浅/深/高对比主题、Ribbon 工具栏、首启引导、崩溃日志（脱敏）、
  启动计时（`--startup-timing`）。

### 2. 引擎与兼容性
- 真实 **AUTO_INCREMENT**（重启/崩溃不复用旧号）、**二级索引**（点查/范围/
  排序大幅加速）、**ALTER 增删列 / 主键 / 唯一约束**、**视图 + JOIN**、
  **WAL checkpoint + VACUUM FULL**（磁盘可压实约 76%）。
- 第三方客户端兼容：Navicat 10 真实点击验收通过（SHOW 元数据、NOT 谓词、
  `SHOW CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT`、information_schema、
  `db.tbl` 限定名、`BETWEEN` 类型安全等）。
- 安全/权限：GRANT/REVOKE 真实生效、密码认证、审计日志轮转、`/metrics`。

### 3. Web 版
- 中英文界面（未翻译项显式清单）、表设计器、CSV/SQL 导入导出（流式上传）、
  EXPLAIN、备份、结构对比、图表（hover/图例/柱线切换/导出 SVG/PNG）、
  ER 图（拖拽/缩放/关联高亮）、真实插件面板、多标签会话。

### 4. 备份与可用性
- 二进制一致性快照：`checkpoint → 拷贝 → manifest(SHA256) → 恢复自检`。
- 在线热备 `--online`：写窗口内阻塞写入（10k 行实测阻塞 ~28 ms），快照点一致、
  WAL=0、无半提交；快照后写入不丢。
- 跨版本策略：同格式版本的快照允许恢复并给 WARNING；格式不兼容显式失败。

---

## 二、安装与首次启动

### 系统要求
- Windows 10/11 x64；Python 3.8+（源码运行）；磁盘约 300 MB（安装后）。
- 安装包：`LightDB-Studio-Setup-4.0.0.exe`（57 MB，Inno Setup，无需管理员）。

### 安装
1. 运行 `LightDB-Studio-Setup-4.0.0.exe`，按向导下一步（默认安装到
   `%LOCALAPPDATA%\Programs\LightDB Studio`）。
2. 从开始菜单启动 **LightDB Studio**。
3. 首次启动会创建 `~/.lightdb_studio`（配置/日志）；可用 `--smoke` 做无界面
   自检（退出码 0 表示窗口可正常构造）。

> ⚠️ 安装包当前**未代码签名**，Windows SmartScreen 可能提示"未知发布者"，
> 请核对 SHA256 后选择"仍要运行"：
> `c7a9b8d59bc40657cf0f7dded5091895bf09ea78d1e2a8f0c5721979e923b744`。
> 若安装被安全策略拒绝（例如受限会话无法写卸载注册表），请用正常用户会话或以
> 管理员身份运行安装器。

### 源码 / 绿色使用
```powershell
pip install -r requirements.txt
python -m lightdb_studio          # 桌面端（源码）
python -m lightdb.main --mode single   # 数据库服务端
python -m benchmarks.competitive_harness --suite quick   # 自检基准
```

---

## 三、自动更新（仅下载，不自动安装）

4.0.0 起提供最小可用的更新下发链路：

1. 服务端/静态服务器提供 `release_manifest.json`（版本、产物 URL、SHA256、大小）
   与安装包产物（支持 HTTP/HTTPS 或 `file://`）。
2. 客户端 `updater.check_for_update(manifest_url, 当前版本)` 解析并做版本比较；
3. 有新版本时 `updater.download_update(...)` 下载到临时目录，**强制校验 SHA256
   与大小**（缺失/不匹配显式报错且不留半成品文件）；
4. **不会自动安装或替换任何文件**——由用户确认后手动运行安装包。

验证脚本：`python scripts/verify_auto_update.py`（本地 HTTP + file://；三分支：
无更新 / 新版本可下载 / 校验失败显式；输出 PASS/FAIL 与 JSON）。

---

## 四、升级 / 卸载

- **升级**：直接运行新版本安装包会按固定 AppId 覆盖安装/升级。
- **卸载**：设置 → 应用 → LightDB Studio → 卸载，或用安装目录 `unins000.exe`。
  卸载仅删除程序文件与开始菜单项，**用户数据/连接配置默认保留**（在
  `~/.lightdb_studio`），如需彻底清理请手动删除该目录。
- 升级前建议先停止正在运行的 Studio；数据库数据目录请单独备份
  （可用 `--online` 快照，见下方文档）。

---

## 五、已知问题与限制

1. **安装包未签名**（无代码签名证书）：SmartScreen 告警；正式分发前建议签名。
2. **受限会话安装限制**：在 Low Integrity/受限沙箱中，安装器可能因无法写
   HKCU 卸载注册表项而回滚（实测 `RegCreateKeyEx code 5`）；正常用户会话可完成。
3. **onefile 单文件版**：产物已生成，但本次在受限会话中无法启动验证
   （退出码 0xFFFFFFFF、无解包目录），需正常桌面会话复验；分发推荐 onedir 安装包。
4. **自动更新不含自动安装**（设计如此）；更新包由用户手动运行。
5. **并发扩展性**：进程内多线程受 Python GIL + 表级锁 + 每事务 flush 限制，
   16 客户端并行效率 3.4–6.5%（详见 `docs/CONCURRENCY_SCALING.md`）；
   多核扩展需多进程查询分片（backlog）。
6. **复制体系不完整**：`Seconds_Behind_Master`、binlog 字节位点/轮转等仍待补齐。
7. **Cython 加速产物不随发布**：默认纯 Python；如自建需 MSVC 编译并自行验证。
8. 部分修复/对比证据为一次性脚本（`_analysis_tmp/`、`docs/analysis_2026-10-19/`），
   不进运行时依赖。

---

## 六、验收与可信度

- **测试**：CI 首次完整全绿（1,598 tests / 0 failed + benchmark/perf/packaging）。
- **发布校验**：`python scripts/verify_release.py --release-dir dist/release`
  → PASS=12 / FAIL=0（WARN=2：未签名；SKIP=2：无证书签名）。
- **安装验证（本次）**：全新用户配置/新路径 → 安装器完成文件拷贝+快捷方式+
  卸载器，Low IL 拒绝注册表写入自动回滚且清理干净；安装树拷贝启动/插件加载/
  版本 4.0.0 PASS；onedir 冒烟/插件/首启目录 PASS；onefile SKIP（见上）。
- **自动更新验证**：`scripts/verify_auto_update.py` → PASS=11 / FAIL=0。
- **证据位置**：
  `_analysis_tmp/gui_full/wave2_clean_install/wave2_clean_install_result.json`、
  `_analysis_tmp/gui_full/wave2_auto_update_check.json`、
  `dist/installer/build_report.json`、`dist/release/release_manifest.json`。

## 七、文档

- [CHANGELOG.md](CHANGELOG.md) — 4.0.0 全部变更批次。
- [SNAPSHOT_BACKUP.md](SNAPSHOT_BACKUP.md) — 二进制快照/在线热备/跨版本策略。
- [CONCURRENCY_SCALING.md](CONCURRENCY_SCALING.md) — 并发扩展性与瓶颈证据。
- [benchmarks/README.md](../benchmarks/README.md) — 基准口径与门禁。
- [review/04_ROADMAP.md](review/04_ROADMAP.md) — 路线图完成项与剩余 backlog。
