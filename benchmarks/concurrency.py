"""并发点查/范围查测量原语（阶段 2 第 5 项）。

本模块是根目录历史脚本 `bench_concurrency.py` 的**复制 + 收紧**：

* 保留原作者验证过的负载口径：固定墙钟窗口内多线程跑真实 SQL
  (`Executor.execute_sql`，每条 worker 独立 statement 状态字典)、
  `threading.Barrier` 齐步启动、每线程记录 p50/p95 延迟、
  先 warmup 再计时。
* **删掉了** `LockProbe`（包 `Table._lock` 统计 acq/wait/hold）——
  它依赖 `Table._lock` 的内部实现（阶段 2.4 未收口的读写锁改动
  正是在改这个），作为长期门禁会跟着实现漂移；那部分留在根目录
  历史脚本里。
* 新增：并发扩展比 (`scaling_ratio`)，即"总吞吐 / 单客户端吞吐"。
  阶段 2.4 已实测**进程内多线程不扩展**（8 客户端总吞吐低于单客户端，
  GIL + 表级锁），因此这个数字只做**如实报告**，不进任何门禁。

并发档**不是门禁指标**：进程内多线程吞吐受 GIL、机器负载、脏实现的
读写锁改动影响很大，把它写成硬阈值只会制造假红。门禁只用**单客户端
进程内点查**（`benchmarks/cases.py` 的 `point_inproc_*`）。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]

DEFAULT_DATA_DIR = os.path.join(HERE, 'data_bench_conc')


# --------------------------------------------------------------- 准备
def setup(nrows: int = 2000, data_dir: str = DEFAULT_DATA_DIR):
    """建库 + 建表 + 灌 nrows 行，返回 (db, tbl, ex, data_dir)"""
    from lightdb.db.database import Database
    from lightdb.logger import logger as _lg
    from lightdb.sql.executor import Executor

    try:
        _lg.set_level('ERROR')
    except Exception:
        pass
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    db = Database(data_dir)
    tbl = db.create_table('t', COLS)
    for i in range(1, nrows + 1):
        tbl.insert([i, i * 2])
    return db, tbl, Executor(db), data_dir


def set_auto_commit(ex, on: bool) -> bool:
    """关掉逐语句审计（如果该版本支持）。返回是否真的关掉了。

    逐语句审计会把每条 SELECT 的文本同步落盘，**在并发窗口里会变成
    磁盘 I/O 瓶颈**，掩盖真正的锁/执行开销。老版本没有这个开关时
    返回 False，不影响测量（只是每次语句多付一次审计写盘）。
    """
    for name in ('set_auto_commit', 'set_audit', 'enable_audit'):
        fn = getattr(ex, name, None)
        if callable(fn):
            try:
                fn(on if name == 'set_auto_commit' else not on)
                return True
            except Exception:
                pass
    return False


# --------------------------------------------------------------- 负载
def run(ex, nthreads: int, seconds: float, kind: str = 'point',
        nrows: int = 2000) -> dict:
    """固定墙钟窗口的多线程负载；返回总吞吐 / 每客户端吞吐 / p50 / p95"""
    counts = [0] * nthreads
    lat = [[] for _ in range(nthreads)]
    stop = threading.Event()
    barrier = threading.Barrier(nthreads)
    errors = []

    def worker(idx):
        st = {}
        c = 0
        barrier.wait()
        while not stop.is_set():
            k = (c * 7919 + idx) % nrows + 1
            t0 = time.perf_counter()
            try:
                if kind == 'point':
                    ex.execute_sql(f'SELECT * FROM t WHERE id = {k}', st)
                elif kind == 'range':
                    lo = (c * 13) % (nrows - 50)
                    ex.execute_sql(
                        f'SELECT COUNT(*) FROM t WHERE id > {lo} '
                        f'AND id <= {lo + 50}', st)
                else:
                    ex.execute_sql(
                        f'INSERT INTO t (id, v) VALUES '
                        f'({1000000 + idx * 100000 + c}, {c})', st)
            except Exception as e:                    # pragma: no cover
                errors.append(repr(e))
                break
            lat[idx].append((time.perf_counter() - t0) * 1000.0)
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
    return {
        'total_qps': sum(counts) / wall,
        'per_client_qps': sum(counts) / wall / nthreads,
        'p50_ms': allat[len(allat) // 2] if allat else 0.0,
        'p95_ms': allat[int(len(allat) * 0.95)] if allat else 0.0,
        'counts': counts,
        'wall_s': wall,
        'nqueries': sum(counts),
        'errors': errors[:3],
    }


def concurrency_round(kind: str = 'point', clients: int = 1,
                      seconds: float = 3.0, nrows: int = 2000,
                      warmup: float = 0.5,
                      data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """一轮：建表灌数 -> warmup -> 固定窗口负载。

    返回 `q_s`（总吞吐，供 `summarize` 取中位数）+ p50/p95/扩展比。
    """
    db = tbl = None
    try:
        db, tbl, ex, data_dir = setup(nrows, data_dir)
        set_auto_commit(ex, False)
        if warmup > 0:
            run(ex, clients, warmup, kind, nrows)
        res = run(ex, clients, seconds, kind, nrows)
        single = None
        if clients > 1:
            one = run(ex, 1, seconds, kind, nrows)
            single = one['total_qps']
        res.update({
            'kind': kind, 'clients': clients, 'nrows': nrows,
            'seconds': seconds, 'q_s': res['total_qps'],
            'single_client_qps': single,
            'scaling_ratio': (res['total_qps'] / single) if single else None,
        })
        return res
    finally:
        if db is not None:
            try:
                db.shutdown()
            except Exception:
                pass
        shutil.rmtree(data_dir, ignore_errors=True)
