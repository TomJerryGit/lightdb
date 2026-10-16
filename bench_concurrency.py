"""阶段 2.4（查询并发）交付基准：进程内多线程点查/范围查询吞吐 + 表锁统计。

用法（每一个 (变体, 客户端数, 场景) 都是一个独立子进程，避免变体之间
互相污染 GIL / 缓冲区 / MVCC 状态）::

    python bench_concurrency.py --variant after  --root .            \
        --kind point --clients 8 --seconds 3 --out result.json
    python bench_concurrency.py --variant before --root ../_base     \
        --kind point --clients 8 --seconds 3 --out result.json
    python bench_concurrency.py --pair --base-root ../_base          \
        --pairs 4 --seconds 3 --out paired.json

设计要点（与验收要求对应）:

* **同窗交错成对**：`--pair` 会为每个 (场景, 客户端数) 交替跑
  `before / after / before / after ...`（≥4 对），并报**每对的中位比值**
  与**全部样本**。本机吞吐在测量窗口内单调下滑（同代码同进程实测
  单线程点查 1680 → 490 q/s，见交付说明），顺序式 before/after 会把
  下滑误记成"改造收益"，所以必须交错。
* **锁统计**：把 `Table._lock` 换成带计数的包装器，分别统计
  `acq / avg_wait / max_wait / avg_hold / max_hold / total_hold`。
  只读语句现在走**共享**锁，因此读场景的统计口径是"共享获取"。
* 数据目录每个子进程独享，用完即删；不写任何持久化状态。
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]


# ==================== 表锁探针 ====================

class LockProbe:
    """包装 `TableRWLock`（或旧代码里的 `RLock`），记录等待/持有时长。

    只读语句走**共享**路径（`read` / `acquire_shared`），写语句走排他，
    两条路径分开统计，避免把"共享获取"和"排他获取"混成一个数字。
    """

    def __init__(self, real, name):
        self._real = real
        self.name = name
        self._t0 = threading.local()
        #: 旧代码（阶段 2.4 之前）的 `Table._lock` 是 `threading.RLock`：
        #: 没有共享路径，只读语句走的也是**排他**获取。为了让同一个探针
        #: 同时能量"改前/改后"，这里探测能力并按实际情况归口统计。
        self.has_shared = hasattr(real, 'acquire_shared')
        self.reset()

    # ---------- 记账 ----------
    def reset(self):
        self.sh = {'acq': 0, 'wait_ns': 0, 'hold_ns': 0,
                   'max_wait': 0, 'max_hold': 0}
        self.ex = {'acq': 0, 'wait_ns': 0, 'hold_ns': 0,
                   'max_wait': 0, 'max_hold': 0}

    def _begin(self, d):
        t1 = time.perf_counter_ns()
        d['acq'] += 1
        self._t0.started = t1

    def _end(self, d):
        t = time.perf_counter_ns()
        h = t - self._t0.started
        d['hold_ns'] += h
        if h > d['max_hold']:
            d['max_hold'] = h

    def _wait(self, d, t0):
        w = time.perf_counter_ns() - t0
        d['wait_ns'] += w
        if w > d['max_wait']:
            d['max_wait'] = w

    # ---------- 共享 ----------
    def acquire_shared(self):
        t0 = time.perf_counter_ns()
        if self.has_shared:
            self._real.acquire_shared()
            d = self.sh
        else:
            self._real.acquire()          # 旧代码：只读也是排他
            d = self.ex
        self._begin(d)
        self._wait(d, t0)
        return True

    def release_shared(self):
        self._end(self.sh if self.has_shared else self.ex)
        if self.has_shared:
            self._real.release_shared()
        else:
            self._real.release()

    @property
    def read(self):
        return _Guard(self.acquire_shared, self.release_shared)

    # ---------- 排他 ----------
    def acquire_exclusive(self):
        t0 = time.perf_counter_ns()
        if self.has_shared:
            self._real.acquire_exclusive()
        else:
            self._real.acquire()
        d = self.ex
        self._begin(d)
        self._wait(d, t0)
        return True

    def release_exclusive(self):
        self._end(self.ex)
        if self.has_shared:
            self._real.release_exclusive()
        else:
            self._real.release()

    @property
    def write(self):
        return _Guard(self.acquire_exclusive, self.release_exclusive)

    def acquire(self, *a, **kw):
        return self.acquire_exclusive()

    def release(self):
        return self.release_exclusive()

    def __enter__(self):
        self.acquire_exclusive()
        return self

    def __exit__(self, *a):
        self.release_exclusive()
        return False

    def __getattr__(self, item):
        return getattr(self._real, item)

    # ---------- 报告 ----------
    @staticmethod
    def _fmt(d):
        n = max(d['acq'], 1)
        return {
            'acq': d['acq'],
            'avg_wait_ms': d['wait_ns'] / n / 1e6,
            'max_wait_ms': d['max_wait'] / 1e6,
            'avg_hold_ms': d['hold_ns'] / n / 1e6,
            'max_hold_ms': d['max_hold'] / 1e6,
            'total_hold_s': d['hold_ns'] / 1e9,
        }

    def report(self):
        return {'shared': self._fmt(self.sh), 'exclusive': self._fmt(self.ex)}


class _Guard:
    __slots__ = ('_a', '_r')

    def __init__(self, a, r):
        self._a, self._r = a, r

    def __enter__(self):
        self._a()
        return self

    def __exit__(self, *exc):
        self._r()
        return False


# ==================== 数据准备 ====================

def setup(root, nrows=2000, data_dir=None):
    if root not in sys.path:
        sys.path.insert(0, root)
    data = data_dir or os.path.join(root, 'data_bench_conc')
    if os.path.exists(data):
        shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data, exist_ok=True)
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.logger import logger as _lg
    try:
        _lg.set_level('ERROR')
    except Exception:
        pass
    db = Database(data)
    tbl = db.create_table('t', COLS)
    for i in range(1, nrows + 1):
        tbl.insert([i, i * 2])
    ex = Executor(db)
    return db, tbl, ex, data


# ==================== 负载 ====================

def run(ex, nthreads, seconds, kind='point', nrows=2000):
    counts = [0] * nthreads
    stop = threading.Event()
    lat = [[] for _ in range(nthreads)]
    barrier = threading.Barrier(nthreads)
    sql_error = []

    def worker(idx):
        s = {}
        c = 0
        barrier.wait()
        while not stop.is_set():
            k = (c * 7919 + idx) % nrows + 1
            t0 = time.perf_counter()
            try:
                if kind == 'point':
                    ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', s)
                elif kind == 'range':
                    lo = (c * 13) % (nrows - 50)
                    ex.execute_sql(
                        f'SELECT COUNT(*) FROM t WHERE id > {lo} '
                        f'AND id <= {lo + 50}', s)
                else:
                    ex.execute_sql(
                        f'INSERT INTO t (id, v) VALUES '
                        f'({1000000 + idx * 100000 + c}, {c})', s)
            except Exception as e:                # pragma: no cover
                sql_error.append(repr(e))
                break
            lat[idx].append((time.perf_counter() - t0) * 1000)
            c += 1
        counts[idx] = c

    ths = [threading.Thread(target=worker, args=(i,))
           for i in range(nthreads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in ths:
        t.join()
    wall = time.perf_counter() - t0
    allat = sorted(x for l in lat for x in l)
    p50 = allat[len(allat) // 2] if allat else 0.0
    p95 = allat[int(len(allat) * 0.95)] if allat else 0.0
    return {
        'total_qps': sum(counts) / wall,
        'per_client_qps': sum(counts) / wall / nthreads,
        'p50_ms': p50,
        'p95_ms': p95,
        'counts': counts,
        'wall_s': wall,
        'errors': sql_error[:3],
    }


def one_run(root, variant, kind, clients, seconds, nrows=2000,
            warmup=0.5):
    db, tbl, ex, data = setup(root, nrows)
    probe = LockProbe(tbl._lock, 'Table._lock')
    tbl._lock = probe
    try:
        if warmup > 0:
            run(ex, clients, warmup, kind, nrows)
        probe.reset()
        res = run(ex, clients, seconds, kind, nrows)
        res['lock'] = probe.report()
        res['mvcc'] = db.mvcc.stats()
        res['variant'] = variant
        res['kind'] = kind
        res['clients'] = clients
        res['seconds'] = seconds
        res['root'] = root
        return res
    finally:
        try:
            db.shutdown()
        except Exception:
            pass
        shutil.rmtree(data, ignore_errors=True)


# ==================== 成对调度 ====================

def spawn(root, variant, kind, clients, seconds, nrows):
    cmd = [sys.executable, os.path.join(HERE, 'bench_concurrency.py'),
           '--one', '--root', root, '--variant', variant,
           '--kind', kind, '--clients', str(clients),
           '--seconds', str(seconds), '--nrows', str(nrows)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return {'error': (p.stderr or '')[-800:], 'variant': variant,
                'kind': kind, 'clients': clients,
                'total_qps': 0.0}
    line = [l for l in p.stdout.splitlines() if l.startswith('{')]
    if not line:
        return {'error': 'no json: ' + p.stdout[-500:], 'variant': variant,
                'kind': kind, 'clients': clients, 'total_qps': 0.0}
    return json.loads(line[-1])


def paired(base_root, work_root, pairs, seconds, kinds, clients_list,
           nrows):
    out = []
    for kind in kinds:
        for n in clients_list:
            runs = {'before': [], 'after': []}
            ratios = []
            for i in range(pairs):
                for variant, root in (('before', base_root),
                                      ('after', work_root)):
                    r = spawn(root, variant, kind, n, seconds, nrows)
                    runs[variant].append(r)
                b = runs['before'][-1]['total_qps']
                a = runs['after'][-1]['total_qps']
                ratios.append(a / b if b else float('nan'))
            entry = {
                'kind': kind, 'clients': n,
                'before_qps': [round(r['total_qps'], 1)
                               for r in runs['before']],
                'after_qps': [round(r['total_qps'], 1)
                              for r in runs['after']],
                'ratios': [round(x, 3) for x in ratios],
                'median_ratio': round(statistics.median(ratios), 3),
                'before_median_qps': round(
                    statistics.median(
                        [r['total_qps'] for r in runs['before']]), 1),
                'after_median_qps': round(
                    statistics.median(
                        [r['total_qps'] for r in runs['after']]), 1),
                'before_lock': [r.get('lock', {}) for r in runs['before']],
                'after_lock': [r.get('lock', {}) for r in runs['after']],
                'before_p50': [round(r.get('p50_ms', 0), 2)
                               for r in runs['before']],
                'after_p50': [round(r.get('p50_ms', 0), 2)
                              for r in runs['after']],
            }
            out.append(entry)
            print(f"{kind:6s} n={n}  before={entry['before_qps']}  "
                  f"after={entry['after_qps']}  "
                  f"ratios={entry['ratios']}  "
                  f"median={entry['median_ratio']}")
            sys.stdout.flush()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--one', action='store_true')
    ap.add_argument('--pair', action='store_true')
    ap.add_argument('--root', default=HERE)
    ap.add_argument('--base-root', default=None)
    ap.add_argument('--variant', default='work')
    ap.add_argument('--kind', default='point',
                    choices=['point', 'range', 'write'])
    ap.add_argument('--clients', type=int, default=1)
    ap.add_argument('--seconds', type=float, default=3.0)
    ap.add_argument('--nrows', type=int, default=2000)
    ap.add_argument('--pairs', type=int, default=4)
    ap.add_argument('--kinds', default='point,range')
    ap.add_argument('--clients-list', default='1,2,4,8')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.one:
        res = one_run(args.root, args.variant, args.kind, args.clients,
                      args.seconds, args.nrows)
        print(json.dumps(res, ensure_ascii=False))
        return

    kinds = [k for k in args.kinds.split(',') if k]
    clients_list = [int(c) for c in args.clients_list.split(',') if c]
    data = paired(args.base_root, args.root, args.pairs, args.seconds,
                  kinds, clients_list, args.nrows)
    out = {
        'base_root': args.base_root,
        'work_root': args.root,
        'pairs': args.pairs,
        'seconds': args.seconds,
        'nrows': args.nrows,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'results': data,
    }
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False)[:200])


if __name__ == '__main__':
    main()
