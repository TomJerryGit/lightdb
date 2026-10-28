# CI 与发布就绪记录（2026-10-26）

## CI（self-hosted runner 2）

- 新 runner `lightdb-local-runner2`（`D:\actions-runner-lightdb2`），标签
  `self-hosted, windows, lightdb-local`；注册后由 `runner_watchdog.py`
  （pythonw + CREATE_NO_WINDOW，WMI 派生）守护：退出后 10s 重启，循环开始前
  自动 `git fetch` 同步本地镜像 `D:\lightdb_mirror.git`。
- 本机 runner 专属 `GIT_CONFIG_GLOBAL=D:\actions-runner-lightdb2\gitconfig-ci`：
  把 `https://github.com/TomJerryGit/lightdb` 重写到本地镜像 → **checkout 不依赖外网**。
- 已修复的 CI 环境问题：
  - 缺 pwsh7 → `shell: powershell`（PS 5.1）
  - `actions/setup-python` 下载失败 → 改用系统 Python（`python --version` 校验）
  - `SHOW FULL TABLES WHERE` 等测试问题 → 见对应提交
  - benchmark/perf/packaging 秒失败且空日志：PS 5.1 `$ErrorActionPreference=Stop`
    下 `*>`/`2>&1` 重定向原生 stderr 会中断步骤 → 改为 `cmd /c "python ... > log 2>&1"`
  - Full test suite 全绿却失败：try/catch + 显式 `exit 0`（JUnit 为唯一门禁）
- 最新验证：JUnit **1503 passed / 0 failed / 39 skipped**；benchmark 步骤修复后
  待下一轮完整 run 验证 benchmark → GUI perf → packaging。

## 发布校验（离线可复现）

- `dist/release` 已重生成 4.0.0 manifest（此前遗留 4.0.1 不一致）：
  `python scripts/make_release_manifest.py --version 4.0.0 --channel stable --dist dist/release --base-url ...`
- `python scripts/verify_release.py --release-dir dist/release` → **PASS**
  （PASS=12 FAIL=0 WARN=2 SKIP=2）：
  - 版本/manifest/产物名/SHA256/大小/updater 解析/check_for_update 全部 PASS
  - 安装包 `dist/installer/LightDB-Studio-Setup-4.0.0.exe`（57,061,774 B）SHA256 与
    build_report 一致
  - WARN：`LightDB-Studio-Setup-4.0.0.exe` 未签名（SmartScreen 风险）；无证书时签名 SKIP
- 安装/卸载冒烟仍需管理员或正常完整性会话（Low IL 沙箱无法写开始菜单/注册表）。

## 待办

- CI 完整绿一次（benchmark/perf/packaging 实际执行）→ **已完成 2026-10-27**
  （提交 `0949a5c`：1,598 tests / 0 failed + benchmark/perf/packaging 全绿）
- 安装/卸载人工冒烟 + 可选代码签名证书
  - 2026-10-28 复核：全新 USERPROFILE/新路径静默安装在 Low IL 受限会话被
    拒绝写 HKCU 卸载注册表（`RegCreateKeyEx code 5`），安装器已自动回滚且清理
    干净；安装树拷贝/onedir 版启动、插件加载、版本 4.0.0 验证 PASS；
    正常完整性会话的人工冒烟仍待做（见 `docs/RELEASE_NOTES_4.0.0.md` §5）。
- Navicat 剩余 GUI 截图（表数据/设计表保存/备份向导）
- SSH 真实隧道联调（paramiko + 本机 SSH server）→ **已完成**（6 tests，
  进程内 paramiko SSH server 真实端到端）
- 性能深化（WAL 命名多段轮转、二级索引范围加速、>100 万行虚拟表索引）
  → WAL 多段轮转 + 单列/多列索引已完成；>100 万行虚拟表索引待做
- 自动更新下发链路 → **已完成**（`scripts/verify_auto_update.py` PASS=11，
  三分支；不实现自动安装）

