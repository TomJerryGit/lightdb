"""基准套件 CLI 入口（阶段 2 第 5 项）。

用法::

    # 快速档 + 人读摘要 + JSON（约 40 s）
    python -m benchmarks.run --suite quick --out benchmarks/results.json

    # 完整档（约 10 min，含 TCP 点查与并发档）
    python -m benchmarks.run --suite full --out benchmarks/results_full.json

    # 门禁：任一项低于目标 -> 退出码 1（可用于 CI 判定）
    python -m benchmarks.run --suite quick --gate

    # 自检门禁本身：把所有阈值放大 100 倍 -> 必然不达标 -> 退出码 1
    python -m benchmarks.run --suite quick --gate --target-scale 100
    python -m benchmarks.run --suite quick --gate \
        --target insert_rowwise_table=999999

    # 只跑某几项
    python -m benchmarks.run --suite quick --only point_inproc_2000

    # 在干净 worktree 里跑（数据目录也隔离）
    python .wt_clean/../benchmarks/run.py --suite quick --data-dir .wt_clean/bdata

退出码:
    0 = 跑完（且 `--gate` 时全部门禁达标）
    1 = `--gate` 有未达标项 / 参数或运行错误
    2 = 没有跑出任何测量值（配置错误）
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from benchmarks import cases, gate as gate_mod          # noqa: E402
from benchmarks import heap_insert as H                  # noqa: E402

SUITES = ('quick', 'full')


def _safe_write(text: str, stream=None):
    """往 stderr 写一行进度；写不进去也不影响测量与退出码。

    背景：Windows 控制台在管道/重定向下 stderr 可能抛 OSError；
    进度输出绝不能让整个基准跑失败。
    """
    stream = stream or sys.stderr
    try:
        stream.write(text + '\n')
        stream.flush()
    except Exception:
        pass


def _utf8_streams():
    """中文 Windows 控制台默认 GBK(cp936)，本包输出含 ⟨−⟩ / ≥ / → 等字符。

    与 `tests/conftest.py` 同一处理：把标准流改成"无法编码的字符就替换"，
    保证在 cp936 控制台里也不会因为打印而中断测量。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')


# --------------------------------------------------------------- 元信息
def machine_info() -> dict:
    return {
        'platform': f'{platform.system()} {platform.release()}',
        'machine': platform.machine(),
        'processor': platform.processor() or os.environ.get('PROCESSOR_IDENTIFIER'),
        'cpu_count': os.cpu_count(),
        'python': sys.version.split()[0],
        'executable': sys.executable,
    }


def git_rev(root: str = REPO_ROOT) -> str:
    try:
        p = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                           cwd=root, capture_output=True, text=True,
                           timeout=20)
        return p.stdout.strip() or '?'
    except Exception:                                    # pragma: no cover
        return '?'


def git_dirty(root: str = REPO_ROOT) -> bool:
    try:
        p = subprocess.run(['git', 'status', '--porcelain'],
                           cwd=root, capture_output=True, text=True,
                           timeout=30)
        return bool(p.stdout.strip())
    except Exception:                                    # pragma: no cover
        return False


# --------------------------------------------------------------- 输出格式
def human_size(nbytes: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(nbytes) < 1024 or unit == 'GB':
            return f'{nbytes:.1f} {unit}' if unit != 'B' else f'{nbytes:.0f} B'
        nbytes /= 1024.0
    return f'{nbytes:.1f} GB'


def fmt_value(name: str, unit: str, value) -> str:
    if value is None:
        return 'n/a'
    if unit == 'bytes':
        return f'{human_size(value)}  ({value:.0f} B, 目标 ≤ {human_size(_target_for(name))})'
    return f'{value:,.1f} {unit}'


def _target_for(name: str):
    t = gate_mod.target_of(name)
    return gate_mod.TARGETS[t]['value'] if t else 0.0


def summary_lines(results: dict, timings: dict, suite: str) -> list:
    lines = [f'LightDB 性能基准 · suite={suite} · '
             f'{time.strftime("%Y-%m-%d %H:%M:%S")}',
             f'python {sys.version.split()[0]} · '
             f'{platform.system()} {platform.release()} · '
             f'{os.cpu_count()} 逻辑核 · git {git_rev()}'
             + ('（工作树有未提交改动）' if git_dirty() else ''),
             '-' * 78]
    for name in sorted(k for k in results if not k.startswith('_')):
        res = results[name]
        meta = cases.CASE_META.get(name, {})
        unit = meta.get('unit', '')
        val = res.get('metric_median')
        mark = ''
        tname = gate_mod.target_of(name)
        if tname and val is not None:
            thr = gate_mod.TARGETS[tname]
            if thr['better'] == 'higher':
                ok = val >= thr['value']
            else:
                ok = val <= thr['value']
            mark = '  [门禁 PASS]' if ok else '  [门禁 FAIL]'
        elif tname:
            mark = '  [门禁 FAIL: 无测量值]'
        lines.append(f'  {name}')
        lines.append(f'      {meta.get("metric", "")}')
        extra = ''
        if res.get('min') is not None and res.get('max') is not None:
            extra = (f'  (min {res["min"]:,.1f} / max {res["max"]:,.1f}'
                     f' / n={res.get("n_samples")})')
        lines.append(f'      = {fmt_value(name, unit, val)}{extra}{mark}')
        if res.get('rows') is not None:
            lines.append(f'      规模: {res["rows"]} 行 · 最终 WAL '
                         f'{human_size(res.get("wal_final") or 0)} · '
                         f'checkpoint 阈值 {res.get("checkpoint_bytes")}')
        if meta.get('note'):
            lines.append(f'      注: {meta["note"]}')
    lines.append('-' * 78)
    total = sum(timings.values()) if timings else 0.0
    lines.append(f'  合计测量墙钟 {total:.1f} s'
                 + (f'（最慢: '
                    + ', '.join(f'{k}={v:.1f}s' for k, v in
                                sorted(timings.items(), key=lambda kv: -kv[1])[:3])
                    + '）' if timings else ''))
    return lines


# --------------------------------------------------------------- 主体
def build_payload(suite, results, timings, g=None) -> dict:
    timings = timings or {}
    cases_out = {k: v for k, v in results.items() if not k.startswith('_')}
    for k, v in cases_out.items():
        meta = cases.CASE_META.get(k, {})
        v.setdefault('metric', meta.get('metric'))
        v.setdefault('unit', meta.get('unit'))
        tname = gate_mod.target_of(k)
        v['gate_target'] = ({'name': tname,
                             'value': gate_mod.TARGETS[tname]['value'],
                             'better': gate_mod.TARGETS[tname]['better'],
                             'unit': gate_mod.TARGETS[tname]['unit'],
                             'basis': gate_mod.TARGETS[tname]['basis'],
                             'tolerance': gate_mod.TARGETS[tname]['tolerance']}
                            if tname else None)
        v['wall_s'] = timings.get(k)
    payload = {
        'schema': 'lightdb-benchmarks/1',
        'suite': suite,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'generated_ts': time.time(),
        'git_rev': git_rev(),
        'git_dirty': git_dirty(),
        'machine': machine_info(),
        'method': {
            'warmup': '每项第 1 轮丢弃，其余取中位数',
            'rounds': 'quick 档每项 2 轮（1 有效样本）；full 档 3-4 轮',
            'fresh_state': '每轮删库重建，表从 0 行长到 N 行',
            'timing': 'time.perf_counter() 只包住被测循环（不含建库/建表）',
            'checkpoint': '除非注明，走引擎默认阈值（1 MB / 5000 条）；'
                          '吞吐项用 LIGHTDB_WAL_CHECKPOINT_BYTES=1e15 关闭',
            'drift_warning': '本机吞吐在测量窗口内单调下滑，跨版本对照'
                             '必须同窗交错成对（benchmarks/heap_insert.'
                             'interleave_pair），顺序式 before/after 不可信',
        },
        'targets': gate_mod.TARGETS,
        'cases': cases_out,
        'total_wall_s': sum(timings.values()),
    }
    if g is not None:
        payload['gate'] = g
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='python -m benchmarks.run',
        description='LightDB 可复跑性能基准（阶段 2 第 5 项）')
    ap.add_argument('--suite', default='quick', choices=SUITES)
    ap.add_argument('--out', default=None,
                    help='JSON 输出路径（默认只打到 stdout）')
    ap.add_argument('--gate', action='store_true',
                    help='按 benchmarks/gate.py 的目标判定；未达标退出码 1')
    ap.add_argument('--target-scale', type=float, default=None,
                    help='自检用：把全部阈值乘这个系数（例如 100 让门禁必红）')
    ap.add_argument('--target', action='append', default=[],
                    metavar='NAME=VALUE',
                    help='自检用：逐项覆盖阈值，可重复')
    ap.add_argument('--only', action='append', default=[],
                    help='只跑指定 case，可重复')
    ap.add_argument('--data-dir', default=None,
                    help='数据目录（默认 benchmarks/data_bench*，跑完删除）')
    ap.add_argument('--json-only', action='store_true',
                    help='只输出 JSON（不打印人读摘要）')
    ap.add_argument('--keep-going', action='store_true',
                    help='单项失败不中断整套（失败项记为"无测量值"，'
                         '门禁会把该目标判为 FAIL 并照常返回 1）')
    ap.add_argument('--retries', type=int, default=1,
                    help='门禁未达标时，只重测未达标的那几项并取更有利的'
                         '一次（默认 1 次）。干扰是单向的（只会更差），'
                         '真回退两次都差；0 = 不重试')
    args = ap.parse_args(argv)
    _utf8_streams()

    # ---- 阈值（含自检覆盖）----
    overrides = {}
    for item in args.target:
        if '=' not in item:
            print(f'--target 需要 NAME=VALUE 形式，收到 {item!r}',
                  file=sys.stderr)
            return 2
        k, v = item.split('=', 1)
        overrides[k.strip()] = float(v)
    targets = gate_mod.override_targets(overrides, args.target_scale)

    # ---- 跑测量 ----
    def progress(name):
        _safe_write(f'  ... {name}')

    failures = {}

    def guarded_run_case(name, data_dir=None):
        """单项失败时记一笔并返回占位结果（门禁会把该目标判 FAIL）"""
        try:
            return cases.run_case(name, data_dir=data_dir)
        except Exception as e:                      # noqa: BLE001
            if not args.keep_going:
                raise
            failures[name] = f'{type(e).__name__}: {e}'
            _safe_write(f'  !! {name} 失败（--keep-going）：{failures[name]}')
            return {name: {'metric_median': None, 'min': None, 'max': None,
                           'n_samples': 0, 'samples': [],
                           'error': failures[name]}}, \
                {'case': name, 'wall_s': 0.0}

    try:
        if args.keep_going:
            names = cases.case_names(args.suite)
            if args.only:
                unknown = [n for n in args.only if n not in names]
                if unknown:
                    print(f'参数错误: --only 里有不属于 suite={args.suite} '
                          f'的 case: {unknown}', file=sys.stderr)
                    return 2
                names = [n for n in names if n in args.only]
            results, timings = {}, {}
            for name in names:
                progress(name)
                res, meta = guarded_run_case(name, data_dir=args.data_dir)
                results.update(res)
                timings[name] = meta['wall_s']
            results['_timings'] = timings
        else:
            results = cases.run_suite(args.suite, only=args.only,
                                      data_dir=args.data_dir, progress=progress)
    except KeyError as e:
        print(f'参数错误: {e}', file=sys.stderr)
        return 2

    timings = results.pop('_timings', {})
    if not results:
        print('没有跑出任何测量值', file=sys.stderr)
        return 2

    # ---- 门禁 ----
    g = None
    if args.gate:
        g = gate_mod.evaluate(results, targets)
        # 干扰是单向的（只会让数字更差），所以未达标时**只重测未达标项**，
        # 取两次里更有利的一次；真回退两次都差，重试洗不白。
        attempts = [results]
        for _ in range(max(0, args.retries)):
            if g['passed']:
                break
            redo = gate_mod.cases_for_targets(results, g['failed'])
            if not redo:
                break
            _safe_write(f'  门禁未达标 {g["failed"]} -> 只重测 {redo} 一次'
                        f'（干扰是单向的，真回退两次都差）')
            try:
                again = cases.run_cases(redo, data_dir=args.data_dir)
            except Exception as e:                   # noqa: BLE001
                _safe_write(f'  重测失败: {type(e).__name__}: {e}')
                break
            again.pop('_timings', None)
            attempts.append(again)
            results, picks = gate_mod.merge_best(attempts, targets)
            for name, idx in picks.items():
                if idx:
                    _safe_write(f'  {name}: 采用第 {idx + 1} 次尝试')
            g = gate_mod.evaluate(results, targets)
        g['attempts'] = len(attempts)
        g['retried'] = len(attempts) > 1

    payload = build_payload(args.suite, results, timings, g)
    if failures:
        payload['failed_cases'] = failures

    # ---- 输出 ----
    if not args.json_only:
        print('\n'.join(summary_lines(payload['cases'], timings, args.suite)))
    if args.out:
        out_path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        if not args.json_only:
            print(f'# JSON 写入 {out_path}')
    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False))
    elif g is not None:
        print(gate_mod.format_checks(g))
    else:
        print('# 未启用 --gate（只报告，不判定）')

    H.cleanup_dir(args.data_dir or H.DEFAULT_DATA_DIR)
    if args.data_dir is None:
        H.cleanup_dir(os.path.join(REPO_ROOT, 'benchmarks', 'data_bench_conc'))

    if g is not None and not g['passed']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
