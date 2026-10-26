# 真实使用路径自动验证报告（2026-10-26）

环境：本机 Windows，LightDB 4.0.0，Server 127.0.0.1:8899（演示库 12 张表），
Studio v4.0.0（便携版 `dist/release/LightDB-Studio-4.0.0-win64`，N1 深色主题 + Ribbon）。

说明：本会话所有进程受 **Low 完整性沙箱** 限制（见下），GUI 连续点击自动化不稳定；
以下验证以"协议/程序化 + 已采集的真实 GUI 截图"为依据，逐项标注证据与方式。

## 1. 连接 + 表列表/表数据

- 服务端：8899 监听、`SHOW FULL TABLES` = **12 张表**（协议实测）
- 表数据：`SELECT COUNT(*) FROM customers` = **200**；`SELECT * ... LIMIT` 正常（协议实测）
- Studio 真实 GUI：已启动（窗口 `LightDB Studio v4.0.0` + 欢迎向导），
  N1 深色主题/Ribbon 截图见 `_analysis_tmp/gui_full/v1_studio_launch.png` 与
  `shots_n1_theme/`、`shots_n2_icons/`
- 自动连接失败时 Studio 给出**真实错误 + 修复命令**（截图 `image.png`，2003/10061），不静默

## 2. 表设计器（AUTO_INCREMENT / 增删列）

GUI 自动化点击受限，改为**按设计器实际生成的 SQL 序列**在 8899 上自动验证：

- `ALTER TABLE t MODIFY COLUMN ...`（类型/NULL/默认/自增）→ 引擎真实执行 ✅
- `ALTER TABLE t AUTO_INCREMENT=n` → 下一起始号生效 ✅
- `ALTER TABLE t MODIFY COLUMN col INT NOT NULL`（去自增）→ 真移除、STATUS=NULL ✅
- `ALTER TABLE t ADD COLUMN x ... FIRST/AFTER` → 列顺序生效 ✅
- `ALTER TABLE t DROP COLUMN x` → 数据清除；索引列 1091/末列 1090 拒绝 ✅
- 崩溃恢复/重启一致性由引擎测试锁定
- Studio 侧"预览 + 二次确认 + 逐条报告 + 失败保留"由 `tests/test_studio_alter_*`（19 passed）锁定

## 3. AI SQL 助手（真实 Key）

- 真实 DeepSeek 调用：建表生成 valid=True，SQL 合理；存储过程生成 valid=True 并**如实标注**
  引擎不兼容（过程体含 SELECT → CALL 不返回结果集，UI 禁用执行）
- 证据：`_analysis_tmp/lead/ai_e2e_report.json`
- 生成的建表 SQL 已真实落库：`CREATE TABLE users_ai ... AUTO_INCREMENT` → INSERT 自动 id=1 ✅
  （`_analysis_tmp/lead/ai_table_exec.py` 输出）
- offscreen GUI 渲染：`ai_real_gui_table.png` / `ai_real_gui_proc.png`（本机缺字体，结构 OK）
- 离线示例库 7 条 parser 通过

## 4. 导入/导出/备份

- 协议/引擎层：`LOCK TABLES`/`SELECT ... INTO OUTFILE` 类兼容、导入导出 SQL 路径经
  `test_studio_navicat_g1/g2` 与引擎 batch 测试锁定
- Studio GUI：导入向导入口真实截图 PASS（`捕获.PNG` 2026-10-22：步骤 1/8、格式列表正常）
- 备份向导：Navicat 点击受焦点限制未取得截图 → **未记 PASS**；本地备份/恢复引擎测试通过

## 5. 环境限制（如实记录）

- 本会话进程 `Mandatory Label\Low Mandatory Level`：向普通目录（`D:\`、用户 Temp、
  `C:\Windows\Temp`）创建文件/目录被拒（Error 5）→ 安装器解包失败；
  安装冒烟需在管理员/正常会话执行（命令已提供）
- GitHub 网络不稳定：self-hosted runner 的 broker 长连接会被中断导致 listener 退出
  （日志 `RunServer IOException SocketException(995)`）；CI run #71 显示 Cancelled(40s)
- 因此本报告不把"GUI 连续点击/安装卸载"标为已通过；对应功能以协议实测 + 代码测试 + 截图证据为准

## 结论

核心真实使用路径（连接、表列表、表数据、设计器 SQL 语义、AI 生成并落库、导入入口）已通过
自动/协议验证；剩余需人工在正常桌面会话完成的是：安装/卸载冒烟、Studio 内连续点击体验、
Navicat 数据网格/备份向导截图。
