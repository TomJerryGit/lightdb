# LightDB Studio

> 图形化数据库管理工具 · Navicat 的开源替代 · 纯 Python 实现

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)]()

---

## ⚠️ 当前实测状态（2026-10）

> 本节是 2026-10 代码审计的**审计时**实测结论，**优先于下方所有功能与性能描述**。完整报告见 [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md)，死代码清单见 [docs/DEAD_CODE.md](docs/DEAD_CODE.md)。其中若干 P0 缺陷已在同一次会话中修复，见本节末尾「本次会话已修复」。

- **服务端单进程模式**（`python -m lightdb.main --mode single`）可以监听端口并发出标准 MySQL v10 握手包（server version `5.7.30-LightDB`），但**认证校验存在缺陷，没有任何客户端能登录成功**：`lightdb/net/mysql_protocol.py:293` 的长度守卫排在空密码分支之前，`:304` 的比较少做一次 SHA1 —— 空密码与非空密码实测均被拒绝。
- **建表必然失败**：`lightdb/storage/buffer_pool.py:15` 用 `open(path, 'r+b')` 打开尚不存在的 `.dat` 文件，抛 `FileNotFoundError`。
- **所有执行错误被响应为 OK 包**：`lightdb/sql/executor.py:68` 把异常转成错误字典而不抛出，`lightdb/net/server.py:391-437` 的 `_send_execution_result()` 从不检查错误，DDL/DML 无条件回 OK 包 —— 客户端完全无法感知失败。
- **默认（多进程）模式无法启动**：`python -m lightdb.main` 在 8 核下自动走 multi，实测输出「Worker 池启动失败」后退出，端口从未被监听；`--check` 还会因裸 emoji `print` 在 cp936 控制台抛 `UnicodeEncodeError`（`lightdb/main.py:156`、`:162`）。
- **桌面端在 Python ≤ 3.11 无法启动**：`lightdb_studio/core/reports.py:172` 的 f-string 含反斜杠，属于 Python 3.12 才允许的语法。
- **Web 版可运行**（17 个 API 端点，JWT 校验有效），但存在硬编码签名密钥与默认口令等问题，详见审计报告。
- **文档中的性能数字未经仓库自身工具验证**：仓库内没有任何基准产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录不存在；`tests/regression_v4/test_performance_v4.py:13-18` 中的 12000 / 80000 QPS 目标**从未被任何测试断言**。这些数字应视为设计目标，而非实测结果。
- 一次完整测试运行的结果为 **268 个测试中 118 失败 / 150 通过**。

### ✅ 本次会话已修复（2026-10，含实测验证）

- **建表可用**：[`lightdb/storage/buffer_pool.py`](lightdb/storage/buffer_pool.py) 在数据文件缺失时先创建，不再抛 `FileNotFoundError`。
- **登录可用**：[`lightdb/net/mysql_protocol.py`](lightdb/net/mysql_protocol.py) 修正 `mysql_native_password` 校验（空密码分支前置、补上最后一次 SHA1），真实 pymysql 客户端实测登录成功。
- **错误不再伪装成功**：[`lightdb/net/server.py`](lightdb/net/server.py) 的 `_send_execution_result()` 现在会把 `{'type': 'error'}` 结果翻译为 ERR 包；实测查询不存在的表返回 `1146`。
- **客户端兼容**：新增 `SET NAMES` / `SET AUTOCOMMIT` 等会话语句确认，否则标准客户端在连接协商阶段就报 1064。
- **启动不再因 emoji 崩溃**：[`lightdb/main.py`](lightdb/main.py)、[`lightdb/main_v4.py`](lightdb/main_v4.py) 增加控制台编码保护并把裸 emoji 换成 ASCII 标记，`--check` 现已正常退出。
- **桌面端可启动**：[`lightdb_studio/core/reports.py`](lightdb_studio/core/reports.py) 的 f-string 改写为 Python 3.8+ 兼容；另修 [`agent_chat.py`](lightdb_studio/ui/agent_chat.py) 缺失的 `QLineEdit` 导入、[`sql_editor.py`](lightdb_studio/ui/sql_editor.py) 的 `QPlainTextEdit.ExtraSelection`、[`main_window.py`](lightdb_studio/ui/main_window.py) 的状态栏构建顺序。offscreen 实测 `MainWindow` 可构造（6 个菜单）。
- **打包与容器**：[`pyproject.toml`](pyproject.toml) 现在会打包 `lightdb` 并补上 `lightdb-server` 入口，Dockerfile 不再 COPY 不存在的 `main.py`。

**端到端实测**（真实 pymysql 客户端）：`CREATE TABLE` / `INSERT` / `SELECT *` / `COUNT(*)` / `UPDATE` / `DELETE` / `WHERE` 过滤全部返回正确结果。

**测试套件**：**163 通过 / 105 失败**（修复前 150 / 118）。剩余失败中约 32 个来自沙箱限制（禁止写工作区外文件、禁止多进程原语），其余是真实待修项：SQL 解析器不支持 CTE/窗口函数/UNION（`KeyError: 'rows'`）、OLAP 向量化引擎 [`vectorized_engine.py:45`](lightdb/olap/vectorized_engine.py#L45) 的 `NameError`、并行执行无加速等。

**仍未修复**：Windows 下多进程模式不可用 —— [`master.py:95-102`](lightdb/net/master.py#L95-L102) 从不建立 `conn_queue`，[`worker.py:124-129`](lightdb/net/worker.py#L124-L129) 因此立即退出，且始终无人监听端口；Linux/macOS 走 `SO_REUSEPORT` 自绑定路径，本机无法验证。

---

## 🎯 概述

LightDB Studio 是为 **LightDB / MySQL** 打造的图形化管理工具，覆盖从**日常查询**到**企业协作**的完整场景。

支持 **桌面版** 和 **Web 版**，具备 Navicat 的核心功能，并额外提供 AI 助手、数据血缘、SQL 审查、性能分析等企业级能力。

---

## ✨ 核心特性

### 📊 数据管理
- 数据网格编辑（内联修改、批量提交）
- 表设计器（可视化建表 / 改表）
- CSV/Excel/JSON 导入导出
- 大文本 / 图片 / Hex 查看器
- 复制粘贴（Excel 格式兼容）

### 🔍 查询能力
- SQL 编辑器（语法高亮 + 自动补全）
- 多标签查询
- 查询历史（搜索 / 收藏）
- SQL 片段库（18+ 内置模板）
- 结果集表格（排序 / 过滤 / NULL 高亮）

### 📈 可视化
- 数据图表（10 种类型：柱状/折线/饼图/雷达/热力图/...）
- ER 图（自动外键检测）
- 仪表盘（指标卡 + 图表卡 + 自动刷新）
- 数据血缘图

### 🚀 高级功能
- **多环境管理**（dev/staging/prod 一键切换 + 生产保护）
- **SQL 审查工作流**（提交-审核-执行）
- **数据库对比**（差异 SQL 生成 + 一键同步）
- **快照 / 克隆**（备份 + 一键复制）
- **SQL 格式化**（Ctrl+Shift+F）
- **数据脱敏**（8 种策略）

### 🤖 AI 助手
- 自然语言转 SQL
- SQL 解释 / 优化建议
- 索引推荐
- 支持 OpenAI / Claude / Ollama（本地）

### 👥 团队协作
- 会话共享 + 评论注解
- @提及 + 回复线程
- 团队配置库
- 云同步（加密导出/导入）

### 📊 监控与诊断
- SQL 性能分析（EXPLAIN + 评分）
- 数据库健康度评分（4 维度）
- 实时性能监控（QPS 曲线）
- 慢查询分析
- 数据质量检查

### 🔧 运维工具
- 任务调度（cron 风格 + 失败重试）
- 报表系统（Excel/HTML/CSV 导出）
- 工作流引擎（多步 SQL 编排）
- 审计日志查看
- MySQL / SQLite 迁移向导

### 🌐 Web 版本
- FastAPI 后端
- REST API（JWT 认证）
- 浏览器访问
- Prometheus 指标

---

## 🚀 快速开始

### 安装

```bash
# 桌面版（完整）
pip install lightdb[all]

# 仅 CLI / 服务
pip install lightdb

# 从源码开发
git clone https://github.com/lightdb/studio.git
cd studio
pip install -e ".[all]"