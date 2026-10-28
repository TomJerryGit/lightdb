"""并发扩展性测量与瓶颈定位（P3+）。

本模块是 `benchmarks.competitive_harness` 的并发扩展性补充：给定同一份数据集，
测量 1/2/4/8/16 个并发客户端在三种负载下的表现，并给出：

* **吞吐**：总 q/s（或 txn/s）、每客户端 q/s；
* **延迟**：p50 / p95 / p99 / mean（ms，全部采样合并后按百分位）；
* **扩展效率**：`throughput(N) / throughput(1)` 的加速比，以及
  `throughput(N) / (N * throughput(1))` 的并行效率（100% = 线性扩展）；
* **错误**：窗口内异常/执行器错误计数与样例；
* **瓶颈证据**：`sampling_profile()` 用 `sys._current_frames()` 采样得到
  各线程热点栈（区分「在引擎里跑」与「等锁/GIL 让出」），
  `cprofile_profile()` 用 cProfile 给出单客户端 CPU 热点函数占比。

设计原则（与 `benchmarks/README.md` 的「只报告、不判定」一致）：

* 并发吞吐**不设硬门禁**（受 GIL、机器负载、其它 agent 干扰很大），只报告
  数字与「明显回退」提示；harness 的 quick/full 门禁不因本模块变化；
* **固定数据集 + 一次 setup**：同一矩阵内只建库一次、warmup 一次，客户端数
  递增复用同一数据库（写负载单独追加 id 空间），减少测量自身开销；
* 单客户端基线只测一次并供所有 N 复用（对比 `concurrency_round` 为每个
  clients>1 再跑一遍单客户端窗口的旧做法，矩阵总耗时显著降低）。

用法::

    python -m benchmarks.competitive_harness --suite quick --scaling
    python -m benchmarks.competitive_harness --scaling --scaling-profile
    python -m benchmarks.scaling --clients 1,2,4,8,16 --seconds 1.5 --profile
"""
from __future__ import annotations

import cProfile
import io
import json
import os
import pstats
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from benchmarks import concurrency as C            # noqa: E402
from benchmarks.run import (                       # noqa: E402
    _utf8_streams, git_dirty, git_rev, machine_info)

SCHEMA = 'lightdb-concurrency-scaling/1'
DEFAULT_CLIENTS = (1, 2, 4, 8, 16)
DEFAULT_KINDS = ('read', 'write', 'mixed')
DEFAULT_SECONDS = 1.0
DEFAULT_WARMUP = 0.3
DEFAULT_NROWS = 2000
WRITE_ID_BASE = 10_000_000
PROFILE_TOP = 15


# ==================== 基础统计 ====================

def _pctl(sorted_vals, q):
    """百分位（最近秩法；空列表返回 0.0）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def _latency_summary(latencies_ms: list) -> dict:
    vals = sorted(latencies_ms)
    if not vals:
        return {'n': 0, 'p50_ms': 0.0, 'p95_ms': 0.0, 'p99_ms': 0.0,
                'mean_ms': 0.0, 'max_ms': 0.0}
    return {
        'n': len(vals),
        'p50_ms': round(_pctl(vals, 0.50), 4),
        'p95_ms': round(_pctl(vals, 0.95), 4),
        'p99_ms': round(_pctl(vals, 0.99), 4),
        'mean_ms': round(statistics.mean(vals), 4),
        'max_ms': round(vals[-1], 4),
    }


# ==================== 固定窗口负载 ====================

def _run_window(ex, nthreads: int, seconds: float, kind: str = 'read',
                nrows: int = DEFAULT_NROWS, write_base: int = WRITE_ID_BASE,
                errors_limit: int = 5) -> dict:
    """一个固定墙钟窗口；返回吞吐/延迟百分位/错误。

    kind:
      * `read`  —— 点查（主键 `WHERE id = k`）
      * `write` —— 小事务（BEGIN; INSERT; COMMIT），每个 worker 独立 id 段
      * `mixed` —— 交替（读、写小事务各 50%）
    """
    counts = [0] * nthreads
    latencies = [[] for _ in range(nthreads)]
    errors = []
    stop = threading.Event()
    barrier = threading.Barrier(nthreads)

    def _is_err(result) -> bool:
        return isinstance(result, dict) and result.get('type') == 'error'

    def worker(idx):
        st = {}
        c = 0
        # 每个 worker 独立 10 万 id 段：16 worker × 10 万 = 160 万 < 窗口间
        # 10,000,000 的步长，也避开了 warmup 的 +5,000,000 偏移（修复高
        # 客户端数下 warmup 与测量窗口主键相撞的 harness 自身 bug）。
        base = write_base + idx * 100_000
        barrier.wait()
        try:
            while not stop.is_set():
                t0 = time.perf_counter()
                if kind == 'read':
                    k = (c * 7919 + idx) % nrows + 1
                    r = ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', st)
                    if _is_err(r):
                        raise RuntimeError(f'read error: {r}')
                elif kind == 'mixed':
                    if c % 2 == 0:
                        k = (c * 7919 + idx) % nrows + 1
                        r = ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', st)
                    else:
                        n = base + c
                        r = ex.execute_sql('BEGIN', st)
                        if not _is_err(r):
                            r = ex.execute_sql(
                                f'INSERT INTO t (id, v) VALUES ({n}, {c})', st)
                            if not _is_err(r):
                                r = ex.execute_sql('COMMIT', st)
                    if _is_err(r):
                        raise RuntimeError(f'mixed error: {r}')
                else:                                # write：小事务
                    n = base + c
                    r = ex.execute_sql('BEGIN', st)
                    if not _is_err(r):
                        r = ex.execute_sql(
                            f'INSERT INTO t (id, v) VALUES ({n}, {c})', st)
                        if not _is_err(r):
                            r = ex.execute_sql('COMMIT', st)
                    if _is_err(r):
                        raise RuntimeError(f'write error: {r}')
                latencies[idx].append((time.perf_counter() - t0) * 1000.0)
                c += 1
        except Exception as exc:                     # noqa: BLE001
            if len(errors) < errors_limit:
                errors.append({'client': idx, 'error': repr(exc)})
        finally:
            counts[idx] = c

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(nthreads)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=60)
    wall = time.perf_counter() - t0
    all_lat = [x for lst in latencies for x in lst]
    lat = _latency_summary(all_lat)
    total = sum(counts)
    qps = (total / wall) if wall > 0 else 0.0
    return {
        'kind': kind,
        'clients': nthreads,
        'wall_s': round(wall, 4),
        'n_ops': total,
        'throughput_qps': round(qps, 2),
        'per_client_qps': round(qps / nthreads, 2),
        'counts': counts,
        'latency': lat,
        'errors': len(errors),
        'error_samples': errors,
    }


# ==================== 扩展性矩阵 ====================

def scaling_matrix(clients=None, kinds=None, seconds: float = DEFAULT_SECONDS,
                   nrows: int = DEFAULT_NROWS,
                   warmup: float = DEFAULT_WARMUP,
                   data_dir: str = None, setup_cache: bool = True) -> dict:
    """跑完整扩展性矩阵（同一数据集，客户端数递增）。

    `setup_cache=True` 时整个矩阵只建库一次（推荐）；传 False 可为每次测量
    重新建库做对照，用于量化「测量框架优化」前后的耗时差异。
    """
    clients = [int(c) for c in (clients or DEFAULT_CLIENTS)]
    kinds = list(kinds or DEFAULT_KINDS)
    data_dir = data_dir or os.path.join(HERE, 'data_bench_scaling')
    t_all = time.perf_counter()
    results = []
    total_windows = 0
    shared = None
    try:
        if setup_cache:
            shared = C.setup(nrows, data_dir)
            C.set_auto_commit(shared[2], False)
        for kind_index, kind in enumerate(kinds):
            win = 0
            baseline = None
            kind_base = WRITE_ID_BASE + kind_index * 100_000_000
            for n in clients:
                if not setup_cache:
                    db, tbl, ex, _ = C.setup(nrows, data_dir)
                    C.set_auto_commit(ex, False)
                else:
                    db, tbl, ex, _ = shared
                try:
                    if warmup > 0:
                        _run_window(
                            ex, max(1, min(n, 2)), warmup, kind, nrows,
                            write_base=(kind_base + win * 10_000_000
                                        + 5_000_000))
                    r = _run_window(
                        ex, n, seconds, kind, nrows,
                        write_base=kind_base + win * 10_000_000)
                    r['window_index'] = win
                    r['baseline_qps'] = None
                    r['speedup_vs_1'] = None
                    r['efficiency_pct'] = None
                    if n == 1:
                        baseline = r['throughput_qps']
                        r['speedup_vs_1'] = 1.0
                        r['efficiency_pct'] = 100.0
                    elif baseline:
                        r['speedup_vs_1'] = round(
                            r['throughput_qps'] / baseline, 3)
                        r['efficiency_pct'] = round(
                            100.0 * r['throughput_qps'] / (baseline * n), 2)
                    results.append(r)
                    total_windows += 1
                    win += 1
                finally:
                    if not setup_cache:
                        try:
                            db.shutdown()
                        except Exception:            # pragma: no cover
                            pass
    finally:
        if shared is not None:
            try:
                shared[0].shutdown()
            except Exception:                        # pragma: no cover
                pass
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    summary = {}
    for kind in kinds:
        rows = [r for r in results if r['kind'] == kind]
        base = next((r['throughput_qps'] for r in rows if r['clients'] == 1),
                    None)
        best = max(rows, key=lambda r: r['throughput_qps']) if rows else None
        summary[kind] = {
            'baseline_qps_1client': base,
            'best_clients': best['clients'] if best else None,
            'best_qps': best['throughput_qps'] if best else None,
            'efficiency_pct': {str(r['clients']): r['efficiency_pct']
                               for r in rows},
            'conclusion': _kind_conclusion(rows),
        }
    return {
        'schema': SCHEMA,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'kind': 'matrix',
        'environment': machine_info(),
        'python_version': sys.version.split()[0],
        'git_rev': git_rev(),
        'git_dirty': git_dirty(),
        'params': {
            'clients': clients, 'kinds': kinds, 'seconds': seconds,
            'warmup_s': warmup, 'nrows': nrows,
            'method': '同一数据集、一次 setup；固定墙钟窗口；'
                      '吞吐=完成操作数/窗口墙钟；延迟=所有客户端采样合并'
                      '百分位；扩展效率=吞吐(N)/(N*吞吐(1))',
            'note': '进程内多线程：受 Python GIL + 引擎表级锁影响；'
                    '数字受机器负载影响，跨窗对比须同窗成对',
        },
        'results': results,
        'summary': summary,
        'total_windows': total_windows,
        'total_wall_s': round(time.perf_counter() - t_all, 2),
        'setup_cache': setup_cache,
    }


def _kind_conclusion(rows) -> str:
    if not rows:
        return '无数据'
    base = next((r['throughput_qps'] for r in rows if r['clients'] == 1), None)
    if not base:
        return '缺少 1 客户端基线'
    worst = min(rows, key=lambda r: r['efficiency_pct']
                if r['efficiency_pct'] is not None else 1e9)
    if worst['efficiency_pct'] is not None and worst['efficiency_pct'] < 60:
        return ('不扩展：%d 客户端并行效率仅 %.1f%%（%s 为最差）；'
                '瓶颈见 sampling/cProfile 证据'
                % (worst['clients'], worst['efficiency_pct'], worst['kind']))
    return '基本可扩展：最差并行效率 %.1f%%' % worst['efficiency_pct']


# ==================== 瓶颈证据：线程栈采样 ====================

def _frame_key(frame) -> str:
    """栈帧标识：lightdb 代码保留 `lightdb/.../file.py:func`，其余只留文件名。

    保留 lightdb 前缀是为了在采样聚合里把「引擎内热点」与 threading/IO 等
    区分开（`engine_*` 只统计以 `lightdb/` 开头的帧）。
    """
    code = frame.f_code
    path = code.co_filename.replace('\\', '/')
    segments = [s for s in path.split('/') if s]
    try:
        idx = len(segments) - 1 - segments[::-1].index('lightdb')
        rel = '/'.join(segments[idx:])
    except ValueError:
        rel = segments[-1] if segments else path
    return '%s:%s' % (rel, code.co_name)


def sampling_profile(nrows: int = DEFAULT_NROWS, clients: int = 8,
                     seconds: float = 1.0, kind: str = 'mixed',
                     interval: float = 0.002,
                     data_dir: str = None) -> dict:
    """运行一个并发窗口，同时用 `sys._current_frames()` 采样所有 worker 栈。

    输出：
      * `samples`：样本数；
      * `blocked_fraction`：样本首帧落在 threading/lock 等待的比例
        （≈ GIL 让出/锁等待的粗指标）；
      * `engine_leaf_top` / `engine_stack_top`：lightdb.* 栈帧计数；
      * `top_leaf`：所有叶帧 Top（含 threading 等待）。
    """
    data_dir = data_dir or os.path.join(HERE, 'data_bench_scaling_prof')
    db, tbl, ex, _ = C.setup(nrows, data_dir)
    C.set_auto_commit(ex, False)
    stop = threading.Event()
    errors = []
    workers = []
    barrier = threading.Barrier(clients)

    def worker(idx):
        st = {}
        c = 0
        base = WRITE_ID_BASE + idx * 1_000_000
        barrier.wait()
        try:
            while not stop.is_set():
                if kind == 'read' or (kind == 'mixed' and c % 2 == 0):
                    k = (c * 7919 + idx) % nrows + 1
                    ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', st)
                else:
                    n = base + c
                    ex.execute_sql('BEGIN', st)
                    ex.execute_sql(
                        f'INSERT INTO t (id, v) VALUES ({n}, {c})', st)
                    ex.execute_sql('COMMIT', st)
                c += 1
        except Exception as exc:                     # noqa: BLE001
            errors.append(repr(exc))
        finally:
            workers[idx]['done'] = True

    workers = [{'tid': None, 'done': False} for _ in range(clients)]
    threads = []
    for i in range(clients):
        t = threading.Thread(target=worker, args=(i,), daemon=True)
        threads.append(t)
    try:
        for t in threads:
            t.start()
        time.sleep(min(0.1, seconds / 2))            # 等线程进入热循环
        leaf = {}
        engine_leaf = {}
        engine_stack = {}
        blocked = 0
        samples = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            frames = sys._current_frames()
            samples += 1
            for t in threads:
                fr = frames.get(t.ident)
                if fr is None:
                    continue
                stack = []
                f = fr
                while f is not None:
                    stack.append(_frame_key(f))
                    f = f.f_back
                if not stack:
                    continue
                leaf_key = stack[0]
                leaf[leaf_key] = leaf.get(leaf_key, 0) + 1
                if leaf_key.startswith('threading.py:'):
                    blocked += 1
                for k in stack:
                    if k.startswith('lightdb/'):
                        engine_stack[k] = engine_stack.get(k, 0) + 1
                for k in stack:
                    if k.startswith('lightdb/'):
                        engine_leaf[k] = engine_leaf.get(k, 0) + 1
                        break
            time.sleep(interval)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
        try:
            db.shutdown()
        except Exception:                            # pragma: no cover
            pass
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)

    def top(d, n=PROFILE_TOP):
        return [{'frame': k, 'count': v}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

    return {
        'kind': 'sampling',
        'kind_load': kind,
        'clients': clients,
        'seconds': seconds,
        'interval_ms': round(interval * 1000, 2),
        'samples': samples,
        'worker_samples': sum(leaf.values()),
        'blocked_fraction': round(blocked / max(1, sum(leaf.values())), 4),
        'top_leaf': top(leaf),
        'engine_leaf_top': top(engine_leaf),
        'engine_stack_top': top(engine_stack),
        'errors': errors[:5],
        'note': '叶帧=线程当时正在执行的函数。⚠️ blocked_fraction 只统计 Python '
                '栈首帧是 threading.py 的样本；引擎用的是 C 级 RLock，线程阻塞在 '
                'C 层时栈仍停在 lightdb 代码里，因此该比例会**严重低估**锁等待——'
                '锁争用请看 lock_contention_profile() 的直接测量。',
    }


# ==================== 瓶颈证据：表锁争用直接测量 ====================

class _TrackedLock:
    """包装 `threading.RLock`，统计等待时间/持有时间（benchmark 侧探针）。

    只用于**测量窗口内**替换 `Table._lock`；`with`/`acquire`/`release`
    全部委托原锁，不改变加锁语义（读完即还原）。
    """

    def __init__(self, inner, name):
        self._inner = inner
        self.name = name
        self.acquires = 0
        self.waits = 0
        self.wait_total = 0.0
        self.wait_max = 0.0
        self.hold_total = 0.0
        self.hold_t0 = 0.0

    def acquire(self, blocking=True, timeout=-1):
        t0 = time.perf_counter()
        ok = self._inner.acquire(blocking, timeout)
        waited = time.perf_counter() - t0
        self.acquires += 1
        if waited > 0.0002:                      # >0.2ms 记为一次可见等待
            self.waits += 1
            self.wait_total += waited
            self.wait_max = max(self.wait_max, waited)
        self.hold_t0 = time.perf_counter()
        return ok

    def release(self):
        try:
            self.hold_total += time.perf_counter() - self.hold_t0
        except Exception:                        # pragma: no cover
            pass
        return self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def stats(self):
        return {
            'table': self.name,
            'acquires': self.acquires,
            'waits': self.waits,
            'wait_total_ms': round(self.wait_total * 1000, 2),
            'wait_max_ms': round(self.wait_max * 1000, 2),
            'avg_wait_ms': round(self.wait_total * 1000 / max(1, self.waits), 3),
            'hold_total_ms': round(self.hold_total * 1000, 2),
        }


def lock_contention_profile(nrows: int = DEFAULT_NROWS, clients: int = 8,
                            seconds: float = 1.0, kind: str = 'read',
                            data_dir: str = None) -> dict:
    """在窗口内包装每张表的 `Table._lock`，直接量化锁争用。

    这是读/写「不扩展」假设的**直接证据**：单张表级 RLock 串行化所有
    读写；统计「等锁次数/总等待时间/持锁时间」。不依赖 cProfile 对 C 级
    RLock 阻塞的盲区（线程阻塞在 C 层时 Python 栈仍停在 lightdb 代码）。
    """
    data_dir = data_dir or os.path.join(HERE, 'data_bench_scaling_prof3')
    db, tbl, ex, _ = C.setup(nrows, data_dir)
    C.set_auto_commit(ex, False)
    originals = {}
    tracked = []
    try:
        for tname, table in list(db.tables.items()):
            orig = table._lock
            originals[tname] = orig
            tl = _TrackedLock(orig, tname)
            table._lock = tl
            tracked.append(tl)
        window = _run_window(ex, clients, seconds, kind, nrows,
                             write_base=WRITE_ID_BASE + 500_000_000)
    finally:
        for tname, orig in originals.items():
            try:
                db.tables[tname]._lock = orig
            except Exception:                    # pragma: no cover
                pass
        try:
            db.shutdown()
        except Exception:                        # pragma: no cover
            pass
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    denom = max(1e-9, window['wall_s'] * clients)
    tables = []
    for tl in tracked:
        s = tl.stats()
        s['wait_frac_of_thread_time'] = round(
            100.0 * (s['wait_total_ms'] / 1000.0) / denom, 2)
        s['hold_frac_of_thread_time'] = round(
            100.0 * (s['hold_total_ms'] / 1000.0) / denom, 2)
        tables.append(s)
    return {
        'kind': 'lock-contention',
        'kind_load': kind,
        'clients': clients,
        'seconds': seconds,
        'window': window,
        'locks': tables,
        'note': 'wait_frac_of_thread_time = 所有客户端线程处于"等这张表锁"的'
                '时间占比（越高说明串行化越严重）；hold_frac 为持锁执行时间。'
                '引擎使用单张表级 RLock（读也加锁），这是并发不扩展的直接原因。',
    }


# ==================== 瓶颈证据：cProfile（单客户端 CPU 热点） ====================

def cprofile_profile(nrows: int = DEFAULT_NROWS, ops: int = 400,
                     kind: str = 'mixed', data_dir: str = None) -> dict:
    """单客户端下 cProfile 热点（CPU 时间分布；不反映 GIL 等待）。"""
    data_dir = data_dir or os.path.join(HERE, 'data_bench_scaling_prof2')
    db, tbl, ex, _ = C.setup(nrows, data_dir)
    C.set_auto_commit(ex, False)
    st = {}
    profiler = cProfile.Profile()
    try:
        for _i in range(20):                         # 预热（不进 profile）
            ex.execute_sql('SELECT * FROM t WHERE id = 1', st)
        profiler.enable()
        for i in range(ops):
            k = (i * 7919) % nrows + 1
            if kind == 'read' or (kind == 'mixed' and i % 2 == 0):
                ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', st)
            else:
                n = WRITE_ID_BASE + i
                ex.execute_sql('BEGIN', st)
                ex.execute_sql(f'INSERT INTO t (id, v) VALUES ({n}, {i})', st)
                ex.execute_sql('COMMIT', st)
        profiler.disable()
    finally:
        try:
            db.shutdown()
        except Exception:                            # pragma: no cover
            pass
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)

    out = io.StringIO()
    stats = pstats.Stats(profiler, stream=out)
    stats.sort_stats('cumulative')
    stats.print_stats(30)
    text = out.getvalue()
    entries = []
    for line in text.splitlines():
        try:
            parts = line.split(None, 5)
            if len(parts) < 6 or not parts[0].isdigit() and not parts[1].isdigit():
                continue
            ncalls, tottime, cumtime = parts[0], parts[1], parts[3]
            func = parts[5]
            if ':' not in func:
                continue
            entries.append({'ncalls': ncalls, 'tottime_s': float(tottime),
                            'cumtime_s': float(cumtime), 'func': func})
        except Exception:                            # noqa: BLE001
            continue
    entries = entries[:PROFILE_TOP]
    total_cum = entries[0]['cumtime_s'] if entries else 0.0
    for e in entries:
        e['cum_pct'] = round(100.0 * e['cumtime_s'] / total_cum, 1) if total_cum else 0.0
    return {
        'kind': 'cprofile',
        'kind_load': kind,
        'ops': ops,
        'top': entries,
        'raw_top': '\n'.join(text.splitlines()[:60]),
    }


# ==================== 测量框架优化前后对比 ====================

def _setup_once_seconds(nrows: int, tries: int = 2) -> float:
    """单独测一次「建库+灌 nrows 行」的中位耗时（用于估算旧框架的 setup 开销）。"""
    import statistics
    import tempfile
    times = []
    for _ in range(tries):
        d = tempfile.mkdtemp(prefix='lightdb_setup_probe_')
        t0 = time.perf_counter()
        db, _tbl, _ex, _ = C.setup(nrows, os.path.join(d, 'data_bench'))
        times.append(time.perf_counter() - t0)
        try:
            db.shutdown()
        except Exception:                            # pragma: no cover
            pass
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    return round(statistics.median(times), 2)


def _cache_comparison(nrows: int) -> dict:
    """交错 A/B 对照：同一数据集一次 setup（新）vs 每个客户端数重新建库（旧）。

    旧实现（`concurrency_round` 风格）对每个客户端数都重新建库、并为
    clients>1 再跑一遍单客户端窗口；新实现整个矩阵只建库一次、单客户端
    基线只测一次并复用。为抵御「机器吞吐随时间漂移」，采用 A/B/A/B 交错
    成对测量并取中位数（与 benchmarks/README 的漂移告警口径一致）。
    """
    import statistics
    cfg = {'clients': [1, 2, 4], 'kinds': ['read'], 'seconds': 0.2,
           'warmup': 0.05, 'nrows': min(nrows, 1000)}

    def run(cached: bool) -> float:
        return scaling_matrix(clients=cfg['clients'], kinds=cfg['kinds'],
                              seconds=cfg['seconds'], nrows=cfg['nrows'],
                              warmup=cfg['warmup'],
                              setup_cache=cached)['total_wall_s']

    pairs = []
    for _ in range(2):
        c = run(True)
        r = run(False)
        pairs.append({'cached_wall_s': c, 'recreated_wall_s': r,
                      'speedup_x': round(r / max(0.01, c), 2)})
    cached_med = round(statistics.median([p['cached_wall_s'] for p in pairs]), 2)
    recreated_med = round(statistics.median(
        [p['recreated_wall_s'] for p in pairs]), 2)
    setup_s = _setup_once_seconds(cfg['nrows'])
    saved_setups = len(cfg['clients']) - 1
    return {
        'config': cfg,
        'pairs_interleaved': pairs,
        'cached_wall_s_median': cached_med,
        'recreated_wall_s_median': recreated_med,
        'speedup_x': round(recreated_med / max(0.01, cached_med), 2),
        'setup_once_s': setup_s,
        'structured_saving': {
            'saved_setups': saved_setups,
            'estimated_saving_s': round(setup_s * saved_setups, 2),
            'explain': '旧框架每个客户端数重建一次库（本项目 %d 个客户端数 → '
                       '%d 次 setup）；新框架只 setup 一次，预计节省 %d×%.2fs'
                       % (len(cfg['clients']), len(cfg['clients']),
                          saved_setups, setup_s),
        },
        'note': 'cached=同一数据集一次 setup + 1 客户端基线复用；'
                'recreated=每个客户端数重新建库（旧 concurrency_round 风格）。'
                '交错 A/B×2 取中位；机器负载仍在漂移时以 pairs 原始值为准。',
    }


def run_scaling_suite(clients=None, kinds=None, seconds: float = 1.0,
                      nrows: int = DEFAULT_NROWS,
                      warmup: float = DEFAULT_WARMUP,
                      profile: bool = False,
                      profile_seconds: float = 1.0,
                      compare_cache: bool = True,
                      data_dir: str = None, out: str = None) -> dict:
    """跑矩阵（可选瓶颈证据），写 JSON，返回报告。"""
    t0 = time.perf_counter()
    matrix = scaling_matrix(clients=clients, kinds=kinds, seconds=seconds,
                            nrows=nrows, warmup=warmup, data_dir=data_dir)
    report = {
        'schema': SCHEMA,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'kind': 'suite',
        'matrix': matrix,
        'profile': None,
        'cache_comparison': None,
        'total_wall_s': 0.0,
    }
    if profile:
        prof = {
            'sampling_mixed_8': sampling_profile(
                nrows=nrows, clients=8, seconds=profile_seconds,
                kind='mixed'),
            'sampling_read_8': sampling_profile(
                nrows=nrows, clients=8, seconds=profile_seconds,
                kind='read'),
            'lock_contention_mixed_8': lock_contention_profile(
                nrows=nrows, clients=8, seconds=profile_seconds,
                kind='mixed'),
            'lock_contention_read_8': lock_contention_profile(
                nrows=nrows, clients=8, seconds=profile_seconds,
                kind='read'),
            'cprofile_read': cprofile_profile(nrows=nrows, ops=300,
                                              kind='read'),
            'cprofile_write_small': cprofile_profile(nrows=nrows, ops=12,
                                                     kind='write'),
        }
        report['profile'] = prof
    if compare_cache and matrix['total_windows'] > 2:
        report['cache_comparison'] = _cache_comparison(nrows)
    report['total_wall_s'] = round(time.perf_counter() - t0, 2)
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        tmp = out + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, out)
        report['_out'] = os.path.abspath(out)
    return report


def format_scaling_summary(report: dict) -> str:
    """人读摘要（ASCII 标记）。"""
    lines = ['并发扩展性矩阵 · %s' % report['generated_at']]
    m = report['matrix']
    lines.append('客户端=%s · 负载=%s · 窗口=%ss · 数据集=%d 行'
                 % (m['params']['clients'], m['params']['kinds'],
                    m['params']['seconds'], m['params']['nrows']))
    lines.append('-' * 92)
    lines.append('  %-6s %-8s %10s %10s %8s %8s %8s %6s %9s %7s'
                 % ('kind', 'clients', 'qps', 'per_cli', 'p50ms',
                    'p95ms', 'p99ms', 'errors', 'speedup', 'eff%'))
    for r in m['results']:
        lat = r['latency']
        lines.append('  %-8s %7d %10.2f %10.2f %8.3f %8.3f %8.3f %6d %9s %7s'
                     % (r['kind'], r['clients'], r['throughput_qps'],
                        r['per_client_qps'], lat['p50_ms'], lat['p95_ms'],
                        lat['p99_ms'], r['errors'],
                        '%.2f' % (r['speedup_vs_1'] or 0),
                        '%.1f' % (r['efficiency_pct'] or 0)))
    lines.append('-' * 92)
    for kind, s in m['summary'].items():
        lines.append('  [%s] 1 客户端 %.1f q/s → 最佳 %s 客户端 %.1f q/s；%s'
                     % (kind, s['baseline_qps_1client'] or 0,
                        s['best_clients'], s['best_qps'] or 0,
                        s['conclusion']))
    if report.get('profile'):
        p = report['profile']['sampling_mixed_8']
        lines.append('  采样(mixed/8cli)：样本 %d，blocked_fraction=%.1f%%；'
                     'engine 热点: %s'
                     % (p['worker_samples'],
                        100.0 * p['blocked_fraction'],
                        ', '.join(x['frame'].split('/')[-1]
                                  for x in p['engine_leaf_top'][:5])))
        lc = report['profile'].get('lock_contention_mixed_8')
        if lc and lc.get('locks'):
            top = sorted(lc['locks'], key=lambda x: -x['wait_total_ms'])[0]
            lines.append('  表锁争用(mixed/8cli)%s 表：等锁 %d 次 / 总等 %.1f ms'
                         '（线程时间占比 %.1f%%）/ 单次最长 %.2f ms'
                         % (top['table'], top['waits'],
                            top['wait_total_ms'],
                            top['wait_frac_of_thread_time'],
                            top['wait_max_ms']))
        cp = report['profile'].get('cprofile_read')
        if cp and cp.get('top'):
            lines.append('  cProfile(单客户端 read) 热点: %s'
                         % ', '.join('%s %.0f%%' % (e['func'].split('/')[-1],
                                                     e['cum_pct'])
                                     for e in cp['top'][:4]))
    if report.get('cache_comparison'):
        c = report['cache_comparison']
        lines.append('  测量框架优化（交错 A/B×2 中位）：cached %.2fs vs '
                     '逐客户端重建库 %.2fs（%.2fx）；单次 setup≈%.2fs，'
                     '旧框架多付 %d 次 setup≈%.2fs'
                     % (c['cached_wall_s_median'], c['recreated_wall_s_median'],
                        c['speedup_x'], c['setup_once_s'],
                        c['structured_saving']['saved_setups'],
                        c['structured_saving']['estimated_saving_s']))
    lines.append('总墙钟: %.2f s' % report['total_wall_s'])
    if report.get('_out'):
        lines.append('JSON: %s' % report['_out'])
    return '\n'.join(lines)


def main(argv=None) -> int:
    """独立 CLI（与 competitive_harness --scaling 等价）。"""
    import argparse
    ap = argparse.ArgumentParser(
        description='LightDB 并发扩展性测量（1/2/4/8/16 客户端；读/写/混合 + 瓶颈采样）')
    ap.add_argument('--clients', default=','.join(str(c) for c in DEFAULT_CLIENTS))
    ap.add_argument('--kinds', default=','.join(DEFAULT_KINDS))
    ap.add_argument('--seconds', type=float, default=DEFAULT_SECONDS)
    ap.add_argument('--nrows', type=int, default=DEFAULT_NROWS)
    ap.add_argument('--warmup', type=float, default=DEFAULT_WARMUP)
    ap.add_argument('--profile', action='store_true', help='附带采样/cProfile 证据')
    ap.add_argument('--profile-seconds', type=float, default=1.0)
    ap.add_argument('--out', default=os.path.join(REPO_ROOT, '_analysis_tmp',
                                                  'bench',
                                                  'concurrency_scaling.json'))
    ap.add_argument('--json-only', action='store_true')
    args = ap.parse_args(argv)
    _utf8_streams()
    clients = [int(c) for c in args.clients.split(',') if c.strip()]
    kinds = [k.strip() for k in args.kinds.split(',') if k.strip()]
    report = run_scaling_suite(clients=clients, kinds=kinds,
                               seconds=args.seconds, nrows=args.nrows,
                               warmup=args.warmup, profile=args.profile,
                               profile_seconds=args.profile_seconds,
                               out=args.out)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_scaling_summary(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
