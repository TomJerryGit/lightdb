"""可辩护的性能目标（门禁阈值）定义与判定（阶段 2 第 5 项）。

## 这些数字是什么

**是"不回退"门禁，不是性能承诺。**

* 每个指标的阈值都由 `benchmarks/README.md` 里那组**本机实测**数字
  推导而来（实测值 × 安全余量），余量的作用是**避免 CI 抖动误报**
  （本机吞吐在测量窗口内单调下滑，实测同代码 20k 档 90 分钟内
  1,750 → 820 rows/s）。
* 门禁通过只代表"没有回退到阈值以下"，**不代表**任何对外性能承诺。
* 白皮书的 `12,000 QPS` / `3.5M rows/s` / `41x` / `12.5x`
  **不在本表内** —— 它们所在的 OLAP / 列存 / Cython 链路未接入
  SQL 执行路径，在查询路径上没有落点（对照表见 README）。追逐这些
  数字不是门禁的目标。

## 阈值表

| 指标 | 阈值 | 方向 | 实测基线 | 余量 |
|---|---|---|---|---|
| `insert_rowwise_table_*` | ≥ 1,000 rows/s | 越高越好 | 1,766（5,000 行档） | ~43% |
| `insert_batch_speedup_*` | ≥ 1.2 x | 越高越好 | 1.6 x（同窗成对，2,000 行档） | ~20% |
| `point_inproc_*` | ≥ 600 q/s | 越高越好 | 1,140（进程内） | ~47% |
| `wal_bounded_*` | ≤ 32 MB | 越低越好 | ≤ 0.5 MB（200k 行峰值） | 64x |
| `crash_recovery_*` | < 5 s | 越低越好 | 0.32 s（30.5k 行） | 15.6x |
| `recovery_missing_rows` | == 0 | 越低越好 | 0 | 硬判据 |
| `recovery_leaked_rows` | == 0 | 越低越好 | 0 | 硬判据 |

行数档位不影响阈值方向：本机在 1k→20k 档内吞吐是**单调下滑**的，
因此 2,000 行档（CI）比 5,000 行档（基线）更容易达标，阈值对小档
不是"因为宽松才过"。反过来说，**大档更容易红**，所以门禁不用大档。
"""
from __future__ import annotations

import copy

#: 门禁阈值。`value` 单位见 `unit`；`better` 是 'higher' 或 'lower'。
TARGETS = {
    'insert_rowwise_table': {
        'value': 1000.0, 'unit': 'rows/s', 'better': 'higher',
        'tolerance': '中位数；40% 余量，容忍单轮 −40% 抖动',
        'basis': '实测 1,766 rows/s（Table API，5,000 行档；'
                 '20k 档 1,411 rows/s）。取 1,000 留 ~43% 余量，'
                 '覆盖机器漂移与 CI 抖动',
    },
    'insert_batch_speedup': {
        'value': 1.2, 'unit': 'x', 'better': 'higher',
        'tolerance': '3 轮同窗交错成对，取比值中位数；容忍 ~20% 回退',
        'basis': '同窗交错成对实测（2,000 行档，门禁用档）1.50–1.87x、中位 '
                 '~1.6x；5,000 行档 1.1–1.7x（并发跑 full 档时被干扰到 '
                 '1.115，见 README 第 6 节）。一侧是 ceil(n/500) 条多值 '
                 'INSERT（3,752 rows/s），另一侧是 n 条单行 INSERT '
                 '（2,379 rows/s）。**不是**白皮书式的 3.5x：顺序对照下'
                 '批量与逐行基本持平甚至更慢（实测 0.91–1.05x），所以这里'
                 '只把"批量明显劣于逐行"当成回退。取 1.2x 留 ~20% 余量',
    },
    'point_inproc': {
        'value': 600.0, 'unit': 'q/s', 'better': 'higher',
        'tolerance': '中位数；容忍 −47% 抖动',
        'basis': '实测 1,140 q/s（进程内点查）。TCP 档实测 516 q/s，'
                 '明显更低且受协议/线程调度影响，故不作门禁',
    },
    'wal_bounded': {
        'value': 32 * 1024 * 1024, 'unit': 'bytes', 'better': 'lower',
        'tolerance': '窗口内峰值；容忍 64x',
        'basis': '实测 200k 行后 WAL 峰值 ≤ 0.5 MB、220k ops 后 403 KB，'
                 '由 checkpoint 阈值（默认 1 MB / 5000 条）封顶。'
                 '32 MB 只用于抓"WAL 无界增长"这一类回归',
    },
    'crash_recovery': {
        'value': 5.0, 'unit': 's', 'better': 'lower',
        'tolerance': '绝对墙钟；容忍 15.6x',
        'basis': '实测 30.5k 行崩溃恢复 0.32 s（改前 5.92 s）。'
                 '取 5 s 只要求"恢复不再随运行时长无界增长"',
    },
    'recovery_missing_rows': {
        'value': 0.0, 'unit': 'rows', 'better': 'lower',
        'tolerance': '硬判据，不允许任何已提交行丢失',
        'basis': 'WAL redo 的语义底线（本项是数据丢失回归，不是性能）',
    },
    'recovery_leaked_rows': {
        'value': 0.0, 'unit': 'rows', 'better': 'lower',
        'tolerance': '硬判据，不允许任何未提交行泄漏成可见行',
        'basis': 'WAL undo 的语义底线（本项是隔离性回归，不是性能）',
    },
    # ---------- OLAP 4A.5：门禁化（2026-10-20，阈值来自实测基线） ----------
    'olap_parallel_groupby_speedup': {
        'value': 1.2, 'unit': 'x', 'better': 'higher',
        'tolerance': '单/多线程结果先逐值一致；计时 warmup+2 轮取中位；'
                     '容忍 ~56% 回退',
        'basis': '2026-10-20 本机实测：100k 行 GROUP BY（100 组，SUM+COUNT）'
                 'w1 中位 2.75 s → w2 中位 1.00 s = 2.75x（8 核）；'
                 '取 1.2x 只抓「并行完全失效/负加速」，不宣称线性扩展',
    },
    'olap_dispatch_crosscheck_mismatches': {
        'value': 0.0, 'unit': 'rows', 'better': 'lower',
        'tolerance': '硬判据，任何不一致行即失败',
        'basis': '4A.2 显式 dispatch 的正确性底线：同 SQL 在开关前后'
                 '（crosscheck 模式）逐行一致；不一致 = 结果错，不是性能问题',
    },
}

#: 只报告、不判定门的量（会输出，永远算 pass）
INFORMATIONAL = {
    'point_tcp': 'TCP 点查（协议往返 + 服务端线程）——实测 ~500 q/s，'
                 '版本间抖动大，仅作参考',
    'concurrency_point': '进程内多线程点查——阶段 2.4 实测不扩展'
                         '（8 客户端总吞吐低于单客户端），仅作如实报告',
    'concurrency_range': '进程内多线程范围查——同上',
    'insert_rowwise_sql': 'SQL 逐行 INSERT（含解析 + 审计）——实测 ~946 rows/s '
                          '（5,000 行档），比 Table API 慢 ~2x，仅作参考',
    'olap_dispatch_agg': 'OLAP dispatch 开关同 SQL 耗时比——2026-10-20 实测 '
                         '0.44–1.10x（当前无收益）；默认保持关闭，'
                         '仅作事后判读，不据此启用',
}

#: 门禁类指标的前缀（用于把 case 名归到某个 target）
PREFIX_TARGETS = {
    'insert_rowwise_table': 'insert_rowwise_table',
    'insert_batch_speedup': 'insert_batch_speedup',
    'point_inproc': 'point_inproc',
    'wal_bounded': 'wal_bounded',
    'crash_recovery': 'crash_recovery',
    'olap_parallel_groupby': 'olap_parallel_groupby_speedup',
    'olap_dispatch_crosscheck': 'olap_dispatch_crosscheck_mismatches',
}

def target_of(case_name: str):
    """case 名 -> 它适用的 target 名（None = 只报告，不判定）"""
    for prefix, target in PREFIX_TARGETS.items():
        if case_name == prefix or case_name.startswith(prefix + '_'):
            return target
    return None


def override_targets(overrides=None, scale=None):
    """返回一份 target 副本，可整体缩放 / 逐项覆盖。

    * `scale`：只作用于**数值**阈值（乘这个系数）。用于自检门禁本身
      （把目标调到不可能达标，验证 `--gate` 真的返回非零）。
    * `overrides`：`{target_name: value}` 逐项替换。

    两种方式都只改内存里的副本，不动本模块的默认表。
    """
    t = copy.deepcopy(TARGETS)
    if scale is not None:
        for v in t.values():
            v['value'] = v['value'] * float(scale)
            v['basis'] = f"[scale={scale} 自检] " + v['basis']
    for name, val in (overrides or {}).items():
        if name not in t:
            raise KeyError(f'未知的目标名: {name}（可选: {sorted(t)}）')
        t[name]['value'] = float(val)
        t[name]['basis'] = f'[人工覆盖={val} 自检] ' + t[name]['basis']
    return t


def evaluate(results: dict, targets: dict = None) -> dict:
    """把 `benchmarks.run` 的测量结果判成 通过/失败。

    `results` 形如::

        {'insert_rowwise_table_2000': {'metric_median': 1300.0, ...},
         'recovery_missing_rows': {'metric_median': 0.0, ...}, ...}
    """
    targets = targets or TARGETS
    checks = []

    # ---------- 直接指标 ----------
    for case_name, res in sorted(results.items()):
        tname = target_of(case_name)
        if tname is None:
            continue
        checks.append(_check(tname, case_name, res.get('metric_median'),
                             targets[tname], found=True))

    # ---------- 汇总 ----------
    failed = [c for c in checks if not c['passed']]
    return {
        'passed': not failed,
        'n_checks': len(checks),
        'n_failed': len(failed),
        'checks': checks,
        'failed': [c['target'] for c in failed],
        'info': INFORMATIONAL,
    }


def cases_for_targets(results: dict, failed_targets) -> list:
    """给定未达标的目标名，找出需要重测的 case 名（用于门禁重试）

    崩溃恢复的硬判据（`recovery_*_rows`）由同一个 case 产出，所以
    `crash_recovery_*` 未达标时也要把它的子 case 一起列出。
    """
    failed = set(failed_targets)
    names = []
    for case_name in results:
        t = target_of(case_name)
        if t is None:
            continue
        if t in failed or (t == 'crash_recovery'
                           and failed & {'recovery_missing_rows',
                                         'recovery_leaked_rows'}):
            names.append(case_name)
    return names


def merge_best(attempts, targets: dict = None):
    """把多次尝试的测量结果按**最有利**（各自方向）合并成一份。

    重试的语义：干扰（杀毒/索引器/别的基准进程）是**单向**的——它只会让
    数字更差。所以"两次都差"才判定回退；一次好一次差按好的算。
    真正回退的代码两次都会差，重试不会把它洗白。

    返回 `(merged_results, picks)`，`picks['<case>']` 记录用了第几次尝试。
    """
    targets = targets or TARGETS
    merged, picks = {}, {}
    for idx, results in enumerate(attempts):
        for name, res in (results or {}).items():
            if name.startswith('_'):
                continue
            val = (res or {}).get('metric_median')
            if val is None:
                merged.setdefault(name, res)
                continue
            if name not in merged or merged[name].get('metric_median') is None:
                merged[name] = res
                picks[name] = idx
                continue
            t = target_of(name)
            better_higher = True
            if t and targets.get(t, {}).get('better') == 'lower':
                better_higher = False
            cur = merged[name]['metric_median']
            take = (val > cur) if better_higher else (val < cur)
            if take:
                merged[name] = res
                picks[name] = idx
    return merged, picks


def _check(target: str, measured_from: str, value, spec: dict,
           found: bool) -> dict:
    if not found or value is None:
        return {
            'target': target, 'measured_from': measured_from,
            'value': None, 'threshold': spec['value'], 'unit': spec['unit'],
            'better': spec['better'], 'passed': False,
            'reason': '没有拿到该项测量值（case 未运行或运行失败）',
            'basis': spec['basis'], 'tolerance': spec['tolerance'],
        }
    val = float(value)
    ok = (val >= spec['value']) if spec['better'] == 'higher' \
        else (val <= spec['value'])
    return {
        'target': target, 'measured_from': measured_from,
        'value': val, 'threshold': spec['value'], 'unit': spec['unit'],
        'better': spec['better'], 'passed': bool(ok),
        'reason': (f"{val:.3f} {spec['unit']} "
                   f"{'>=' if spec['better'] == 'higher' else '<='} "
                   f"{spec['value']:.3f} {spec['unit']}"
                   + ('' if ok else '  ← 不达标（回退到门禁阈值以下）')),
        'basis': spec['basis'], 'tolerance': spec['tolerance'],
    }


def format_checks(gate: dict) -> str:
    """人读的门禁摘要（`--gate` 模式输出）"""
    lines = ['', '=' * 78,
             f"门禁结果: {'PASS' if gate['passed'] else 'FAIL'}"
             f"  （{gate['n_checks'] - gate['n_failed']}/{gate['n_checks']} 项达标）",
             '=' * 78]
    for c in gate['checks']:
        mark = 'PASS' if c['passed'] else 'FAIL'
        lines.append(f"  [{mark}] {c['target']:<22} {c['reason']}")
        lines.append(f"         依据: {c['basis']}")
        lines.append(f"         容忍: {c['tolerance']}")
    if gate['failed']:
        lines.append('')
        lines.append('  未达标: ' + ', '.join(gate['failed']))
    lines.append('=' * 78)
    return '\n'.join(lines)
