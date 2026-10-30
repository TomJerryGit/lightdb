# GUI 优化记录（2026-10-30，三批）

对 LightDB Studio 桌面端做连接/查询/视觉/性能三批优化，均为真实 UI 改动 + 新测试 +
截图证据，已推送（beta.129/130/131）。

## 批次 A：连接对话框 + 对象树 + 打包修复（beta.129）

- **修复打包版 SSH**：`packaging/lightdb_studio.spec` 补 paramiko/cryptography/
  bcrypt/nacl hiddenimports，重建 exe 后对话框显示“paramiko 可用”
  （此前打包版显示“未安装 paramiko，SSH 隧道不可用”）
- **修复默认端口**：新建连接取上次连接端口，否则 **8898**；主/远程端口联动
- 连接对话框：空主机/端口校验、**测试连接**（真实连接 + 可行动错误码）、
  口令显示切换、进程内记忆上次值（不记口令）、SSH/SSL 真实状态、
  底部“服务未启动时”启动命令提示、“保存后立即连接”勾选
- 对象树：表名过滤自动懒加载并展开匹配、双击表打开数据、右键菜单
  （打开表/设计表/导出/复制表名）、刷新保留展开与选中、120 表过滤 <2s

## 批次 B：查询页 + 视觉/主题（beta.130）

- 查询状态栏：`完成 (12.5 ms) · 3 行`；取消显式反馈；大结果集“已取 N 行 / 限制 M 行”
- 结果表：表头**本地类型化排序**（NULL 固定末尾，不重发 SQL）、列宽按列名持久化、
  双击表头自适应、多行/含表头/CSV 复制
- 结果区：即时过滤（选列/区分大小写）+ **CSV/JSON/SQL 快捷导出**
- 错误体验：错误码 + SQLSTATE + 行号分区、编辑器错误行高亮
- 主题：`DESIGN_TOKENS` 增 divider/dpi、暗色表格交替行/选中/工具条/输入框细节、
  `high_dpi_info`/`scale_token`（125%/150%）、QSplitter 分栏比例持久化（`layout_store.py`）

## 批次 C：性能与启动（beta.131）

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| MainWindow 构造（受控） | 1.015 s | **0.169 s** |
| 入口 → 窗口可交互 | 7.029 s | **1.984 s** |
| 进程 wall（含 smoke） | 8.51 s | **3.21 s** |
| 1500 表展开 | 1500 节点 / 0.063 s | **500 上限 / 0.029 s** |
| 10 万行 show_result | — | **0.252 s，0 个 QTableWidgetItem** |
| 10 万行稀疏访问 | — | **0.006 s** |
| 10 万行 CSV 导出 | — | **0.436 s（分块）** |
| 关闭 tab 释放后 RSS | 173.6 MB | **154.1 MB** |

实现：Ribbon 延迟 80ms 构建 + 启动打点（`--startup-timing`/`get_startup_marks`）、
虚拟模型不建全量 item、单库节点上限 500（超出显式提示）、批量插入关动画、
>200 表过滤 220ms 防抖、关闭查询 tab 显式 `release()`。

## 验证

- 新测试：A 8 passed、B 11 passed、C 5 passed；回归 G1/G2/G3/p0/smoke/theme/ribbon/
  icons/perf 相关均通过；`compileall lightdb_studio` OK
- 截图证据：`_analysis_tmp/gui_full/shots_task87|88|89/`
- 发布产物已用 GUI 优化后的代码重建：
  - `dist/installer/LightDB-Studio-Setup-4.0.0.exe` 62,565,231 B
    sha256 `21b164cb65e6a58a…`
  - `dist/release/LightDB-Studio-4.0.0-win64.zip` 90,318,532 B
    sha256 `e8b470b337af91a2…`
  - `verify_release`：PASS=12 / FAIL=0（未签名 WARN 忽略）

## 剩余

- 高 DPI 仅 offscreen 125% 截图验证（150% 为 token 计算测试），未真机缩放
- 500 表以上仅显示前 500（需过滤缩小）
- 导出仍为分块同步（未后台线程/进度弹窗）
- 列宽仅在用户拖动/双击后保存
