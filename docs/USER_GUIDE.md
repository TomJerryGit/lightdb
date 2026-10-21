# LightDB Studio 用户指南与速查

> 适用版本：**4.0.0**（LightDB 引擎 + Studio 桌面端 + Web 版单仓）。
> 本文覆盖：快速开始、**快捷键卡**、**常见错误码对照（1045/2003/1146/1062 等）**、
> 首启引导、更新检查、崩溃/日志、打包发布。发布流程见
> [`docs/RELEASING.md`](RELEASING.md)，Web 能力见
> [`docs/WEB_GUI_PARITY.md`](WEB_GUI_PARITY.md)。

## 1. 快速开始

### 1.1 一键本地 demo（推荐）

在仓库根目录：

```powershell
.\run_studio.cmd          # 双击也可；等价于 powershell -File .\run_studio.ps1
```

`run_studio.ps1` 做两件事：

1. 若 `127.0.0.1:8899` 没有服务在听，就在后台启动**单进程** LightDB 服务端
   （数据目录默认 `data_demo`；多进程模式在沙箱里因命名管道不可用而无法启动）；
2. 前台启动 Studio GUI；GUI 会用 `%USERPROFILE%\.lightdb_studio\connections.json`
   里保存的连接（auto_connect）自动连上，对象树可浏览。

关闭：关闭 GUI 窗口。后台服务会继续运行；脚本结束时会把停止命令打印出来。

> **安全说明（务必读）**：`run_studio.ps1` 是**本地 demo**，监听 `127.0.0.1` 并显式传
> `--allow-empty-password`（root 空口令，仅本地）。这是**显式、可审计的降级**；
> 生产/网络暴露环境不要用该参数。生产应使用强口令（引擎首次启动会生成随机 root
> 口令并写到 `<data_dir>/root.password`，或显式设置 `LIGHTDB_ROOT_PASSWORD`）。

### 1.2 手动启动

```powershell
# 终端 A：启动服务端（本地演示）
python -m lightdb.main --mode single --host 127.0.0.1 --port 8899 `
    --data-dir data_demo --allow-empty-password

# 终端 B：启动 Studio
python -m lightdb_studio.app
```

### 1.3 Web 版

```powershell
python -m web.start          # 默认 http://127.0.0.1:8000
```

Web 与 GUI 共用 `lightdb_studio/core` 业务层；能力对照见
[`docs/WEB_GUI_PARITY.md`](WEB_GUI_PARITY.md)。

## 2. 快捷键卡

| 快捷键 | 功能 | 菜单位置 |
|---|---|---|
| `F5` | **执行**当前 SQL | 查询 → 执行 |
| `Ctrl+E` | `EXPLAIN` 当前 SQL | 查询 → EXPLAIN |
| `Ctrl+Enter` | 执行（编辑器内） | —（编辑器快捷键） |
| `Ctrl+Space` | SQL 自动补全（手动触发） | —（编辑器） |
| `Ctrl+N` | 新建查询 | 文件 → 新建查询 |
| `Ctrl+O` | 打开 SQL 文件 | 文件 → 打开 SQL 文件 |
| `Ctrl+S` | 保存 SQL | 文件 → 保存 SQL |
| `Ctrl+W` | 关闭当前标签 | 编辑 → 关闭当前标签 |
| `Ctrl+Shift+F` | 格式化 SQL | 编辑 → 格式化 SQL |
| `Ctrl+G` | 数据图表 | 查询 → 数据图表 |
| `Ctrl+T` | 新建表（表设计器） | 表 → 新建表 |
| `F9` | 刷新对象浏览器 | 工具 → 刷新对象浏览器 |
| `Ctrl+H` | 显示/隐藏工具面板 | 工具 → 显示/隐藏工具面板 |
| `Ctrl+Shift+A` | AI 对话面板 | 工具 → AI 对话 |
| `Ctrl+,` | 设置 | 工具 → 设置 |
| `Ctrl+Q` | 退出 | 文件 → 退出 |

其它常用入口（无全局快捷键）：数据库对比、ER 图、快照/克隆、迁移、数据质量、
健康度、告警、任务调度、报表管理、插件管理、审计日志（均在「工具」菜单）。

## 3. 常见错误码对照

GUI 会把错误显示在结果区/错误面板，并尽量解析 `near '<token>'`、`at line N`；
错误可一键复制。以下为真实 pymysql/引擎错误码与处理建议：

| 错误码 | 典型消息 | 常见原因 | 处理 |
|---|---|---|---|
| **1045** (`28000`) | `Access denied for user 'root'@'…'` | 口令错误；生产库未显式提供 root 口令（首次启动生成到 `<data_dir>/root.password`）；账号未授权 | 核对连接口令；查看 `<data_dir>/root.password`；用 `GRANT` 授权 |
| **2003** (`HY000`) | `Can't connect to MySQL server on '127.0.0.1'` | 服务端未启动/端口不对/被防火墙拦截；`run_studio.ps1` 未启动成功 | 先启动服务端；确认 `--port` 与连接页端口一致；检查 8899 是否被占用 |
| **1146** (`42S02`) | `Table 'db.t' doesn't exist` | 表名拼错/未选库/表已被删；GUI 对象树缓存过期 | 检查当前库（`USE db`）；刷新对象树（`F9`）；确认表名大小写 |
| **1062** (`23000`) | `Duplicate entry '…' for key 'PRIMARY'` | 主键/唯一键冲突（重复插入或更新） | 改用新的主键值；先 `SELECT` 确认；批量导入时导出失败行报告 |
| 1049 (`42000`) | `Unknown database 'x'` | 库不存在/连接串库名错 | 选择已有库或先 `CREATE DATABASE x` |
| 1064 (`42000`) | `You have an error in your SQL syntax… near '…'` | SQL 语法错误 | 看 `near` 定位；用「格式化 SQL」辅助 |
| 1054 (`42S22`) | `Unknown column 'x'` | 列名拼错/别名作用域错误 | 用自动补全（`Ctrl+Space`）确认列名 |
| 1146/1064 类 | 错误面板红色高亮 | — | 检查 SQL 后重试；错误可复制反馈 |

> 引擎/协议层当前**不实现**的 MySQL 能力会显式报错（见 README「已知限制」），
> 不会静默假装成功。

## 4. 首次启动引导

首次启动（`~/.lightdb_studio/settings.json` 中 `first_run_done` 未置位）会显示
**首启向导**：

1. 欢迎 / 产品定位；
2. 本地 demo 说明：`run_studio.ps1` 一键启动、默认端口 8899、root 空口令仅本地、
   生产请用强口令；
3. 下一步：新建连接（「文件 → 新建连接」）或直接连 `127.0.0.1:8899`
   （root / 空口令，仅本地）。

完成或「跳过」都会持久化 `first_run_done=True`。测试/打包冒烟可用
`LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN=1` 抑制。

## 5. 检查更新（手动通道）

Studio「帮助 → 检查更新...」**不会自动下载/安装**，只做版本检查与提示：

```powershell
$env:LIGHTDB_UPDATE_MANIFEST_URL = 'https://example.com/downloads/release_manifest.json'
# 可选：stable / beta / nightly，默认 stable
$env:LIGHTDB_UPDATE_CHANNEL = 'stable'
python -m lightdb_studio.app
```

manifest 格式见 `lightdb_studio/utils/updater.py` 模块文档；发布脚本生成：

```powershell
python scripts/build_studio.py --manifest --version 4.0.0 --channel stable `
    --base-url https://example.com/downloads
```

`updater.download_update()` 强制校验 SHA256，校验失败会删除半成品文件；
是否替换由用户/发布脚本决定（程序不自动安装）。

## 6. 日志与崩溃

- 正常日志：`~/.lightdb_studio/logs/studio.log`（滚动，5×2MB，UTF-8）；
  可用 `LIGHTDB_STUDIO_LOG_DIR` 覆盖目录。
- 未捕获异常：写 `~/.lightdb_studio/logs/crash_<时间戳>.log`（完整 traceback，
  已脱敏），同时写主日志。
- **日志/崩溃文件不应出现明文口令**：`utils/crashlog.redact()` 会替换
  `password=…`、`IDENTIFIED BY '…'`、`Authorization: Bearer …`、`token/secret/api_key=…`。
  若发现泄露，请按安全问题处理并附脱敏后的日志片段。
- 打包产物可用 `LightDB-Studio.exe --smoke` 做无界面启动验证（测试/CI 用）。

## 7. 打包 / 签名 / 发布

```powershell
# 目录版（推荐，启动快）→ dist/studio/LightDB-Studio/
python scripts/build_studio.py

# 单文件版 → dist/studio-onefile/LightDB-Studio.exe
python scripts/build_studio.py --onefile

# 可选代码签名（未配置证书时显式 skip，不算失败）
.\scripts\sign_windows.ps1 -Exe "dist\studio\LightDB-Studio\LightDB-Studio.exe"

# 生成 release_manifest.json（自动更新通道）
python scripts/make_release_manifest.py --version 4.0.0-beta.19 --channel stable `
    --dist dist/studio --base-url https://example.com/downloads
```

打包脚本会在构建后真实执行 `LightDB-Studio.exe --smoke`（offscreen、临时 HOME、
`LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN=1`）验证产物可启动；冒烟失败视为构建失败。
签名是**可选**步骤：未配置证书时明确跳过并提示 SmartScreen 风险，不假装已签名。

## 8. 插件示例

仓库自带示例插件 `plugins/example_plugin.py`（菜单「示例：插入 SELECT 1」+ 工具栏）。
「工具 → 插件管理」可启用/禁用/重载/打开目录/安装插件；插件状态持久化在
`%USERPROFILE%\.lightdb_studio\plugins_state.json`。插件加载失败会在列表显示 error，
不影响其它插件。

## 9. 更多

- 测试基线/已知限制：README「测试套件」「已知限制」；
- GUI 优化路线图与每批实施记录：`docs/GUI_OPTIMIZATION_ROADMAP.md`；
- 发布门禁/CI 记录：`docs/RELEASING.md`；
- Web 能力对照：`docs/WEB_GUI_PARITY.md`。
