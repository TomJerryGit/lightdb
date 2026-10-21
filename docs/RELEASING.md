# 发布流程（Release Checklist）

> 适用：LightDB 引擎 + Studio 桌面端 + Web 版（单仓多产物）。
> 版本唯一来源：`lightdb/__init__.py::__version__`（`pyproject.toml` 用 `dynamic.version` 读取）。

## 0. CI 状态说明（2026-10-21 更新）

- 工作流：[`.github/workflows/ci.yml`](.github/workflows/ci.yml)（Windows：install → compileall →
  GUI smoke（offscreen，fail-fast）→ pytest → benchmark gate → GUI perf budget → packaging check；
  触发 push / pull_request / workflow_dispatch）。
- **GitHub Actions 已实跑并全绿**（`origin = https://github.com/TomJerryGit/lightdb.git`）：
  - run `39525357327`（head `e729525`）→ **success**：日志显示全量 pytest、benchmark gate、
    packaging check 全部通过（此前 3 轮失败的根因依次修复：cp1252 编码、SSL 限制如实
    skip、多进程 worker 元数据、DATE_FORMAT 非 ASCII 字面量、runtime 缺 setuptools/wheel）；
  - run `39526414556`（head `a900389`，GUI P1 首批）→ **success**，全部 13 个 step 成功。
  - run `39531144802`（head `cc65c82`，GUI P1 第二批）→ **success**，全部 13 个 step 成功
    （全量 pytest 1087 用例 + benchmark gate + packaging）。
  - run `39534182111`（head `7087f0b`，GUI P1 第三批）→ **success**，全部 13 个 step 成功
    （全量 pytest 1106 用例 + benchmark gate + packaging）。
  - run `39535395829`（head `8882232`，GUI P2 可视化）→ **success**，全部 13 个 step 成功
    （全量 pytest 1131 用例 + benchmark gate + packaging）。
  - run `39548734259`（head `b3435b1`，P2 元数据/备份/TLS/插件合并）→ **success**，
    全量 pytest 1182 用例 + benchmark gate + packaging 全过。
  - 仅剩 GitHub 的 Node.js 20 deprecation warning（actions 自身，与本仓库代码无关）。
- 推送命令：
  ```powershell
  git push -u origin main --tags
  ```
- **Cython 编译产物不参与 CI/发布**：CI 无 Cython/编译器，运行时自动走纯 Python 回退
  （本机 Zig 构建产物已归档不启用，见 README「Cython 已真实编译验证」告警）。

### 本地 CI 实跑记录（2026-10-21 更新）

| 步骤 | 命令/步骤 | 结果 |
|---|---|---|
| 全量 pytest | `python -m pytest tests -q -p no:cacheprovider` | **1182 用例 = 1140 passed / 0 failed / 40 skipped / 1 xfailed / 1 xpassed**（2026-10-21 P2 元数据/备份/TLS/插件后；GitHub CI run `39548734259` 同版本全绿） |
| 基准门禁 | `python -m benchmarks.run --suite quick --gate` | **PASS 7/7**（原 5 条 + OLAP 并行 GROUP BY ≥1.2x、dispatch 对拍 0 不一致） |
| 打包检查 | `python scripts/check_packaging.py` | PASS（wheel 含引擎包 + 3 入口，版本 4.0.0 单一来源） |
| 仓库卫生 | `python scripts/check_repo_hygiene.py` | PASS（提交 GUI 验证报告后复跑通过） |
| 全量 GUI+DB 验证 | `_analysis_tmp/gui_full/db_verify.py` + `gui_driver.py` | DB **34/34**、GUI **45/45 场景 / 77 截图 / 25 对话框**（报告 `docs/analysis_2026-10-19/07_GUI_FULL_VERIFICATION.md`） |

> 说明：本轮抓到一个只在**编译版 Cython** 运行时出现的 Windows 堆损坏（`0xc0000374`，
> 全量长跑 20% 处，未定位单一模块）；归档编译产物、改回纯 Python 回退后上表全绿。
> 编译产物不入库，CI/发布路径本来就走回退，因此不影响发布门禁。
>
> 排障备注：Windows PowerShell 5.1 下用 `*>` 重定向整个 CI 输出时，长时 pytest 进程
> 偶发被终止（`$LASTEXITCODE=-1`，无 summary）；改用 `> ci.log 2>&1` 后完整跑通。
> 建议用户/CI 都用普通 `>` 重定向。


## 1. 发布前门禁（必须全部通过）

```powershell
# 一条命令：install + compileall + 全量 pytest + 基准门禁 + 打包检查
.\scripts\ci.ps1

# 或者分步：
python -m compileall -q lightdb lightdb_studio web
python -m pytest tests -q -p no:cacheprovider
python -m benchmarks.run --suite quick --gate
python scripts/check_packaging.py
```

验收口径：
- `pytest` **0 failed**（skip/xfail 必须每条带明确 reason；总数与基线差异需说明）；
- `benchmarks --gate` PASS（5/5）；
- `check_packaging` PASS（wheel 含 `lightdb/lightdb_studio/web`、三个入口、版本单一来源）。

## 2. 版本与文档

1. 更新 `lightdb/__init__.py::__version__`（唯一来源），确认 `scripts/check_packaging.py` 版本校验通过；
2. 更新 `CHANGELOG.md`（Added / Changed / Fixed / Known limitations）；
3. 更新 `README.md` 顶部「当前实测状态」：测试基线、性能门禁、未完成项；
4. 更新 `docs/analysis_2026-10-19/06_ROADMAP.md` 的进度表与 KPI；
5. `docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md` 的残余风险清单与实现一致；
6. 桌面版发布时确认 `lightdb_studio/__init__.py::__version__` 与
   `lightdb/__init__.py::__version__` 一致（Studio 窗口标题/「关于」/更新检查都读它）。

## 3. 仓库卫生

```powershell
python scripts/check_repo_hygiene.py
```

要求：
- 无 `data_*` 测试残留目录（测试应使用 `tmp_path` / 临时目录）；
- 无遗留 git worktree（`git worktree list` 只应有主工作区）；
- 根目录无调试脚本/日志/一次性产物（`_diag_*.py`、`debug.log`、`result_after.json` 等）；
- `LICENSE` 与 `LICENSE.txt` 合并为一份；
- `.gitattributes` 明确行尾策略（当前仓库混用 CRLF/LF，见 roadmap 阶段 0.4）。

## 4. 安装冒烟（发布物可信性）

```powershell
# 构建 wheel 并装进隔离 venv（--system-site-packages 便于复用本机依赖）
python -c "from setuptools import build_meta; print(build_meta.build_wheel('_analysis_tmp/release_wheel'))"
python -m venv --system-site-packages _analysis_tmp/release_venv
_analysis_tmp/release_venv/Scripts/python.exe -m pip install --no-deps --no-index _analysis_tmp/release_wheel/*.whl
_analysis_tmp/release_venv/Scripts/lightdb-server.exe --version    # 期望 LightDB <version>
_analysis_tmp/release_venv/Scripts/lightdb-studio-web.exe --help   # 期望用法输出
```

## 4b. 桌面端打包（PyInstaller，桌面版发布物）

```powershell
# 目录版（推荐；本机已构建并验证 --smoke）→ dist/studio/LightDB-Studio/
python scripts/build_studio.py

# 单文件版 → dist/studio-onefile/LightDB-Studio.exe（构建可用；受限环境见下）
python scripts/build_studio.py --onefile

# 可选签名（未配置证书时显式 skip，不假装已签名）
.\scripts\sign_windows.ps1 -Exe "dist\studio\LightDB-Studio\LightDB-Studio.exe"

# 打包 onedir 目录为 zip 并生成更新 manifest
Compress-Archive -Path dist\studio\LightDB-Studio\* `
    -DestinationPath dist\release\LightDB-Studio-<version>-win64.zip
python scripts/make_release_manifest.py --version <version> --channel stable `
    --dist dist\release --base-url https://example.com/downloads
```

校验要求：
- `scripts/build_studio.py` 构建后**真实执行** `LightDB-Studio.exe --smoke`
  （offscreen + 临时 HOME + `LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN=1`），失败即构建失败；
- 本机实测（2026-10-21）：onedir 产物 exe **12,859,533 bytes**、`--smoke` exit 0；
  zip **83,395,707 bytes**；`make_release_manifest` 产出的 manifest 经
  `lightdb_studio.utils.updater` 解析、`file://` 下载、SHA256/大小校验全部通过；
- **onefile 限制（如实标注）**：构建可成功（~83MB），但本仓库的沙箱环境禁止
  bootloader 解压写文件（`PYI-...: Failed to extract VCRUNTIME140.dll ... Permission denied`），
  因此 onefile 的**运行验证未完成**（构建成功不等于可运行）。在正常 Windows 桌面应可用；
  若启动卡在解压（安全软件/沙箱阻止 TEMP 写入），用
  `python scripts/build_studio.py --onefile --onefile-tmpdir <可写目录>` 重新构建；
  发布前务必在干净 Windows 环境手工跑一次 `--smoke`。
- 签名可选：无证书时脚本提示 SmartScreen 风险并跳过；有证书时用 `signtool`
  SHA256 + 时间戳并 `Get-AuthenticodeSignature` 校验。

## 5. 打标签

```powershell
git tag -a v<version> -m "LightDB <version>"
git push origin main --tags     # 需要用户/维护者执行（本仓库暂无远端）
```

## 6. 发布说明（Release Notes）

- 公开 `CHANGELOG.md` 对应版本段；
- 明确 **Known limitations**（复制 beta、授权/OLAP 状态、单线程、WAL 无 CRC 等）；
- **禁止**把白皮书/设计目标数字写成实测能力（见 README「性能声明状态说明」）。
