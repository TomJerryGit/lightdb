"""仓库卫生检查（发布前用；报告为主，--strict 时非零退出）。

检查项（对应 docs/RELEASING.md §3）：
1. 测试残留 `data_*` 目录（数量/体积）——测试应使用 tmp_path/临时目录；
2. git worktree：除主工作区外不应有遗留 worktree（如 `_wt_head`）；
3. 根目录调试/一次性产物（被跟踪的 `_*.py`、`*.log`、`result_after.json` 等）；
4. `LICENSE` / `LICENSE.txt` 是否重复；
5. 版本一致性：lightdb.__version__ / pyproject dynamic / README / CHANGELOG；
6. `.gitattributes` 行尾策略是否存在（当前仓库 CRLF/LF 混用）；
7. 未跟踪文件是否落在允许的 evidence 目录之外。

用法：
    python scripts/check_repo_hygiene.py [--strict]
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 允许存在的未跟踪目录（分析证据/临时工作区；发布时可清理，不影响仓库卫生判定）
ALLOWED_UNTRACKED_PREFIXES = (
    '_analysis_tmp/', '_engine_probe/', '_studio_web_tmp/', '_vb_tmp/',
    '_gap_data/', '_gap_after.txt', 'tmp_service_security/',
    'docs/analysis_2026-10-19/logs/', 'docs/analysis_2026-10-19/tmp/',
)

#: 根目录一次性产物模式（被跟踪即告警）
SCRATCH_PATTERNS = (
    r'^_\w+\.py$', r'^_\w+\.log$', r'^debug\.log$', r'^result_after\.json$',
    r'^bench_summary\.json$', r'^_final_full\.log$',
)

#: 体积告警阈值（MB）
DATA_DIR_WARN_MB = 20.0


def _git(*args) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                          text=True, encoding='utf-8', errors='replace').stdout


def _base_version(v: str) -> str:
    """归一化版本后缀（4.0.0-beta.1 / 4.0.0b1 → 4.0.0）用于跨文档一致性比较"""
    return re.split(r'[-+ ]', str(v).strip())[0]


def main() -> int:
    strict = '--strict' in sys.argv
    problems = []
    warnings = []

    # 1) data_* 残留
    data_dirs = sorted(p for p in ROOT.glob('data_*') if p.is_dir())
    total = sum(f.stat().st_size for d in data_dirs for f in d.rglob('*')
                if f.is_file())
    mb = total / (1024 * 1024)
    print(f"[i] data_* 残留目录: {len(data_dirs)} 个 / {mb:.1f} MB")
    if data_dirs:
        warnings.append(f'{len(data_dirs)} 个 data_* 测试残留目录（{mb:.1f} MB）')
        if mb > DATA_DIR_WARN_MB:
            problems.append(f'data_* 体积过大: {mb:.1f} MB > {DATA_DIR_WARN_MB} MB')

    # 2) worktree
    wts = [ln for ln in _git('worktree', 'list').splitlines() if ln.strip()]
    print(f"[i] git worktree: {len(wts)} 个")
    for ln in wts:
        print('    ', ln.strip())
    if len(wts) > 1:
        warnings.append(f'存在 {len(wts) - 1} 个额外 worktree（发布前应移除）')

    # 3) 根目录一次性产物（只查被跟踪文件）
    tracked = _git('ls-files').splitlines()
    scratch = [p for p in tracked
               if '/' not in p and any(re.match(rx, p) for rx in SCRATCH_PATTERNS)]
    if scratch:
        problems.append(f'被跟踪的根目录一次性产物: {scratch}')
    print(f"[i] 被跟踪的根目录一次性产物: {len(scratch)} 个")

    # 4) 双 LICENSE
    lic1, lic2 = ROOT / 'LICENSE', ROOT / 'LICENSE.txt'
    if lic1.exists() and lic2.exists():
        same = lic1.read_bytes() == lic2.read_bytes()
        all_tracked = 'LICENSE' in tracked and 'LICENSE.txt' in tracked
        if same and all_tracked:
            warnings.append('LICENSE 与 LICENSE.txt 内容相同且都被跟踪（应合并）')
    print(f"[i] LICENSE 重复: {'是' if lic1.exists() and lic2.exists() else '否'}")

    # 5) 版本一致性
    sys.path.insert(0, str(ROOT))
    try:
        from lightdb import __version__ as engine_version
    except Exception as e:                    # noqa: BLE001
        problems.append(f'无法导入 lightdb 读取版本: {e}')
        engine_version = '?'
    version_marks = {'lightdb.__version__': engine_version}
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    version_marks['pyproject.dynamic'] = (
        'dynamic.version' if 'dynamic = ["version"]' in pyproject else '静态版本')
    if (ROOT / 'README.md').exists():
        readme = (ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
        m = re.search(r'version-([0-9][^\s\-\]]*)-blue', readme)
        version_marks['README badge'] = m.group(1) if m else '未找到'
    if (ROOT / 'CHANGELOG.md').exists():
        cl = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8', errors='replace')
        m = re.search(r'^##\s*v?([0-9][^\s（(]*)', cl, re.MULTILINE)
        version_marks['CHANGELOG 顶部'] = m.group(1) if m else '未找到'
    print('[i] 版本标记:', version_marks)
    bases = {_base_version(v) for k, v in version_marks.items()
             if k != 'pyproject.dynamic' and v != '未找到'}
    if len(bases) > 1:
        warnings.append(f'版本标记不一致（归一化后）: {version_marks}')

    # 6) .gitattributes
    if not (ROOT / '.gitattributes').exists():
        warnings.append('缺少 .gitattributes（行尾策略未声明；当前 CRLF/LF 混用）')

    # 7) 未跟踪文件越界
    porcelain = _git('status', '--porcelain').splitlines()
    outside = []
    for ln in porcelain:
        if not ln.startswith('?? '):
            continue
        path = ln[3:].strip().strip('"').replace('\\', '/')
        if not any(path == p.rstrip('/') or path.startswith(p)
                   for p in ALLOWED_UNTRACKED_PREFIXES):
            outside.append(path)
    if outside:
        warnings.append(f'允许目录之外的未跟踪文件: {outside[:10]}')
    print(f"[i] 允许目录之外的未跟踪条目: {len(outside)} 个")

    # 汇总
    print()
    for w in warnings:
        print(f'[WARN] {w}')
    for p in problems:
        print(f'[FAIL] {p}')
    if not problems:
        print('[PASS] 仓库卫生检查通过（仅有警告）')
        return 0
    print(f'\n[FAIL] 发现 {len(problems)} 个必须修复的问题')
    return 1 if strict else 0


if __name__ == '__main__':
    sys.exit(main())
