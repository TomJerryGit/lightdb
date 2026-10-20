# 发布流程（Release Checklist）

> 适用：LightDB 引擎 + Studio 桌面端 + Web 版（单仓多产物）。
> 版本唯一来源：`lightdb/__init__.py::__version__`（`pyproject.toml` 用 `dynamic.version` 读取）。

## 0. CI 状态说明（2026-10-20）

- 工作流：[`.github/workflows/ci.yml`](.github/workflows/ci.yml)（Windows：install → compileall →
  pytest → benchmark gate → packaging check；触发 push / pull_request / workflow_dispatch）。
- **GitHub Actions 尚未实跑**：当前仓库唯一远端是本地裸仓库 `D:\lightdb_backup.git`
  （`git remote -v` 无 github 远端），没有 GitHub 仓库/runner。工作流 YAML 已用 PyYAML
  解析校验（`name: ci`，单 job `test`，7 个 step 齐全）。
- 本地等价门禁已实跑（见下方「本地 CI 实跑记录」）；推送到 GitHub 后 Actions 会自动执行同一套步骤：
  ```powershell
  git remote add origin https://github.com/<org>/<repo>.git
  git push -u origin main --tags
  ```

### 本地 CI 实跑记录（2026-10-20 中午，`scripts/ci.ps1 -SkipInstall` 一次跑通）

| 步骤 | 命令/步骤 | 结果 |
|---|---|---|
| 全量 CI | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1 -SkipInstall > ci.log 2>&1` | **`CI: ALL CHECKS PASSED`（exit 0）** |
| compileall | ci.ps1 内（`PYTHONPYCACHEPREFIX` 重定向，兼容受限 sandbox） | PASS（6.5s） |
| 全量 pytest | `python -m pytest tests -q -p no:cacheprovider` | **949 用例 = 913 passed / 0 failed / 34 skipped / 2 xfailed**（9:50） |
| 基准门禁 | `python -m benchmarks.run --suite quick --gate` | **PASS 5/5**（逐行 INSERT 2,628.9 rows/s、批量/逐行 2.130×、点查 1,251.4 q/s、WAL 468.6 KB、崩溃恢复 0.373 s） |
| 打包检查 | `python scripts/check_packaging.py` | PASS（wheel 含引擎包 + 3 入口，版本 4.0.0 单一来源） |
| 仓库卫生 | `python scripts/check_repo_hygiene.py` | PASS |

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
5. `docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md` 的残余风险清单与实现一致。

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

## 5. 打标签

```powershell
git tag -a v<version> -m "LightDB <version>"
git push origin main --tags     # 需要用户/维护者执行（本仓库暂无远端）
```

## 6. 发布说明（Release Notes）

- 公开 `CHANGELOG.md` 对应版本段；
- 明确 **Known limitations**（复制 beta、授权/OLAP 状态、单线程、WAL 无 CRC 等）；
- **禁止**把白皮书/设计目标数字写成实测能力（见 README「性能声明状态说明」）。
