# 发布收尾记录（2026-10-29，4.0.0 Wave 3 刷新）

## 1. 重新构建的发布产物（包含 beta.101–120 全部改动）

| 产物 | 大小 | SHA256 |
|---|---|---|
| `dist/installer/LightDB-Studio-Setup-4.0.0.exe` | 59,815,236 B | `a55957b324c96a606df267e91bfaa5c673b9b3952ce3b20fd29cfeef6c27084b` |
| `dist/release/LightDB-Studio-4.0.0-win64.zip` | 86,381,744 B | `f6b96e3243627d357c0f50e666734cb0fe5e7edf00d2aabd8b29c6bc0d66d3ce` |

构建来源：当前 main（beta.120）→ `build_studio.py`（onedir，exe 15,243,310 B，
`--smoke` exit 0）→ `build_installer.py`（Inno Setup 6，`--source`/`--out` 必须绝对路径，
否则 ISCC 在 `packaging/` 下解析相对路径失败）→ `Compress-Archive` 生成发布 zip →
`make_release_manifest.py` 重算 manifest。

## 2. 发布校验

```
python scripts/verify_release.py --release-dir dist/release
PASS=12 FAIL=0 WARN=2 SKIP=2 -> PASS
```

- manifest 版本 4.0.0 / zip SHA256/大小 / updater 解析 / `check_for_update`
  / installer SHA256/版本 全部 PASS
- WARN：未签名（无证书，按需求忽略）
- SKIP：SHA256SUMS 未生成（manifest 内已含 sha256）

## 3. 本批（beta.101–120）进入发布的功能增量

- **生产化 A**：`/metrics`（连接/QPS/错误/慢查询/WAL/checkpoint/备份/池）、慢查询日志、
  审计轮转
- **生产化 B**：连接池（线程复用/1040 超限/排队/池指标）、binlog **增量复制**
  （follow 持续追平/断点续传/1236 缺口显式/延迟指标）
- **引擎深水区**：视图 JOIN **内联**（1.14x 手写）、**复合主键 3–4 列 + BTree v3**
  （10k 行点查 3.5ms/前缀 6ms）+ v1/v2→v3 迁移、**LIKE 前缀索引**（29x）、
  **二进制 PREPARE** 会话隔离/错误码/大结果集
- **多进程只读分片原型**：100k 行复用快照 4 worker 端到端 **2.85x**、查询段 3.63x
- **UI**：Studio/Web 生产面板（复制状态、metrics 摘要、快照备份/恢复）、
  ER 拖拽缩放、图表交互、动态 i18n、CSV multipart 流式上传
- **发布工程**：CHANGELOG/RELEASE_NOTES 4.0.0、自动更新下发三分支、干净环境安装核验
- **CI**：全量套件加 `pytest-timeout`（300s/thread）防止挂起卡死；残留清理收窄；
  最终 CI **1755 tests / 0 failed** + benchmark + GUI perf + packaging 全绿

## 4. 待办（需管理员会话/证书）

- 新安装包的管理员会话安装 → `--smoke` → 卸载冒烟（脚本同上一轮，见
  `docs/INSTALLER_ACCEPTANCE_2026-10-27.md`）
- 代码签名（无证书，按用户要求忽略）
- onefile 版在正常桌面会话复验
