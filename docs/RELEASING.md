# 发布流程（Release Checklist）

> 适用：LightDB 引擎 + Studio 桌面端 + Web 版（单仓多产物）。
> 版本唯一来源：`lightdb/__init__.py::__version__`（`pyproject.toml` 用 `dynamic.version` 读取）。

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
