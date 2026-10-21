# -*- coding: utf-8 -*-
"""P3-2 GUI 性能预算：启动 / 1200 行结果渲染 / RSS 增量。

用法::

    python scripts/gui_perf_budget.py

- offscreen 构造真实 MainWindow（auto_connect=False，不扫描插件）；
- 1200 行结果用真实 ResultTable 渲染并计时；
- 通过 psutil 读取进程 RSS 增量；
- **硬上限**（超限 exit 1）：启动 < 5000ms、渲染 < 3000ms、RSS < 500MB；
- **目标预算**（只 WARN，不 fail）：2000ms / 500ms / 300MB；
- JSON 输出到 stdout，并写入 `_analysis_tmp/gui_perf_budget.json`；
- 无外部服务端依赖，CI 可重复运行。
"""
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# 必须在导入 lightdb_studio 之前把 HOME 改道，避免写真实用户目录
_TMP_HOME = Path(tempfile.mkdtemp(prefix='gui_perf_budget_home_'))
os.environ['HOME'] = str(_TMP_HOME)
os.environ['USERPROFILE'] = str(_TMP_HOME)

HARD_LIMITS = {'startup_ms': 5000.0,
               'render_1200_ms': 3000.0,
               'rss_delta_mb': 500.0}
TARGET_BUDGET = {'startup_ms': 2000.0,
                 'render_1200_ms': 500.0,
                 'rss_delta_mb': 300.0}

_ROWS_1200 = [[i, f'item-{i:04d}', i * 1.5] for i in range(1, 1201)]


def _rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:                                  # noqa: BLE001
        return None


def main() -> int:
    sys.path.insert(0, str(ROOT))
    result = {
        'startup_ms': 0.0,
        'render_1200_ms': 0.0,
        'rss_delta_mb': 0.0,
        'rss_supported': True,
        'row_count': 0,
        'budgets': {'hard': dict(HARD_LIMITS),
                    'target': dict(TARGET_BUDGET)},
        'warnings': [],
        'hard_failures': [],
        'ok': True,
        'platform': platform.platform(),
        'python': sys.version.split()[0],
    }
    warnings = result['warnings']
    hard_failures = result['hard_failures']

    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    # 基线 RSS：QApplication 之后、MainWindow 之前
    baseline_rss = _rss_mb()
    if baseline_rss is None:
        result['rss_supported'] = False
        warnings.append('psutil 不可用：RSS 指标跳过（不判硬上限）')

    # ---------- 启动耗时 ----------
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow

    t0 = time.perf_counter()
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None       # 不扫描插件/不自动连接
    win.show()
    app.processEvents()
    result['startup_ms'] = round((time.perf_counter() - t0) * 1000, 2)

    # ---------- 1200 行渲染 ----------
    from lightdb_studio.ui.result_table import ResultTable
    rt = ResultTable()
    rt.resize(1100, 560)
    rt.show()
    app.processEvents()
    t1 = time.perf_counter()
    rt.show_result(['id', 'name', 'amount'], _ROWS_1200, 1.0)
    app.processEvents()
    result['render_1200_ms'] = round((time.perf_counter() - t1) * 1000, 2)
    result['row_count'] = int(rt.table.rowCount())

    # ---------- RSS 增量 ----------
    after_rss = _rss_mb()
    if baseline_rss is not None and after_rss is not None:
        result['rss_delta_mb'] = round(max(0.0, after_rss - baseline_rss), 2)
    else:
        result['rss_delta_mb'] = 0.0

    # 清理窗口（closeEvent 会停调度/关连接/卸插件）
    try:
        rt.close()
        win.close()
    except Exception:
        pass
    app.processEvents()

    # ---------- 判定 ----------
    for key, limit in HARD_LIMITS.items():
        value = result.get(key, 0.0) or 0.0
        if key == 'rss_delta_mb' and not result['rss_supported']:
            continue
        if value > limit:
            hard_failures.append(
                f'{key}={value} 超过硬上限 {limit}')
    for key, budget in TARGET_BUDGET.items():
        value = result.get(key, 0.0) or 0.0
        if key == 'rss_delta_mb' and not result['rss_supported']:
            continue
        if value > budget:
            warnings.append(f'{key}={value} 超过目标预算 {budget}（WARN，不 fail）')
    if result['row_count'] != 1200:
        hard_failures.append(f'1200 行结果实际 rowCount={result["row_count"]}')
    result['ok'] = not hard_failures

    # ---------- 输出 ----------
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    out_dir = ROOT / '_analysis_tmp'
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / 'gui_perf_budget.json').write_text(text, encoding='utf-8')
    except Exception as exc:                           # noqa: BLE001
        print(f'WARN 写入 _analysis_tmp/gui_perf_budget.json 失败: {exc}',
              file=sys.stderr)
    for warn in warnings:
        print(f'WARN {warn}', file=sys.stderr)
    for failure in hard_failures:
        print(f'HARD FAIL {failure}', file=sys.stderr)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
