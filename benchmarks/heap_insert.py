"""INSERT / 点查 / WAL / 崩溃恢复的**可复用测量原语**（阶段 2 第 5 项）。

本模块是根目录历史脚本 `bench_heap_insert.py` 核心逻辑的**复制 + 收紧**：

* 保留原作者已验证的测量口径（每轮从**全新数据目录**开始、计时只包住
  插入循环、第 1 轮 warmup 丢弃、其余取中位数、显式声明调用路径、
  压掉慢查询告警日志）。
* **删掉了**只服务于"修复前 worktree 对照"的部分（`feature probe`、
  `--pair` 子进程调度、`--sql-variant` / TCP 批量等一次性档位）——
  那部分留在根目录历史脚本里；本包只保留**可以当成门禁长期跑**的量。
* 新增 `run_case()`：单进程、单测量点、结果直接给 `benchmarks.run` 用。

测量铁律（见 README）：本机吞吐在测量窗口内**单调下滑**，所以跨版本/
跨代码的对照必须**同窗交错成对**（`interleave_pair`），顺序式
before/after 不可信；门禁只比"当前实现 vs 固定阈值"，不受此影响，
但**绝不**用顺序对照去论证"改好了多少"。

checkpoint 开关（必须在 import lightdb 之前/创建表之前生效）::

    LIGHTDB_WAL_CHECKPOINT_BYTES = 10**15    # 阈值大 -> 永不触发
    LIGHTDB_WAL_CHECKPOINT_RECORDS = 0       # 0 = 该维度不限制

`Table.wal_checkpoint_limits()` 是**每次调用**读环境变量的，所以进程内
切换 cfg 是安全的；但为了不给读者留坑，切换一律走 `checkpoint_env()`。
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:                       # 允许直接跑本模块
    sys.path.insert(0, REPO_ROOT)

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]

#: 默认数据目录（在 benchmarks/ 下，不进版本库，跑完即删）
DEFAULT_DATA_DIR = os.path.join(HERE, 'data_bench')

#: 多值 INSERT 的默认"每条语句行数"（与历史脚本一致）
BATCH = 500


# --------------------------------------------------------------- 环境开关
def checkpoint_env(cfg: str):
    """设置 checkpoint 阈值。cfg='off' 关掉（阈值极大），其它走引擎默认。"""
    if cfg == 'off':
        os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] = str(10 ** 15)
        os.environ['LIGHTDB_WAL_CHECKPOINT_RECORDS'] = '0'
    else:
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_BYTES', None)
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_RECORDS', None)


def quiet_logger(level: str = 'ERROR'):
    """把全局 logger 压到 ERROR。

    必要性（原作者实测）：`Executor.execute` 在语句耗时 >0.5s 时无条件
    `logger.warn(...)` 并把**整条 AST**（几百行多值 INSERT 是几十 KB）
    打到 stdout；全局 logger 级别固定 INFO、不吃 `LIGHTDB_LOG_LEVEL`。
    不压掉的话，批量语句的计时里混进了打日志的 I/O。
    """
    try:
        from lightdb.logger import logger as _lg
        _lg.set_level(level)
    except Exception:
        pass


def prepare_env(cfg: str = 'default', audit: str = 'NONE'):
    """测量前的统一环境准备（必须在 lightdb 首次使用前调用）"""
    os.environ['LIGHTDB_AUDIT_LEVEL'] = audit
    os.environ['LIGHTDB_LOG_LEVEL'] = 'ERROR'
    checkpoint_env(cfg)
    quiet_logger()


# --------------------------------------------------------------- 基础工具
def fresh_dir(data_dir: str = DEFAULT_DATA_DIR) -> str:
    """删掉重建数据目录（每轮独立，避免轮间残留状态）"""
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def cleanup_dir(data_dir: str = DEFAULT_DATA_DIR):
    shutil.rmtree(data_dir, ignore_errors=True)
    gc.collect()


def new_table(data_dir: str = DEFAULT_DATA_DIR, table: str = 't'):
    from lightdb.db.database import Database
    db = Database(data_dir)
    return db, db.create_table(table, COLS)


def file_size(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


def wal_size(data_dir: str = DEFAULT_DATA_DIR, table: str = 't') -> int:
    return file_size(os.path.join(data_dir, table + '.wal'))


def count_checkpoints(tbl):
    """包一层 tbl.checkpoint，返回一个可读的计数器 dict"""
    box = {'n': 0}
    orig = getattr(tbl, 'checkpoint', None)
    if orig is None:
        return box

    def wrapped(*a, **kw):
        box['n'] += 1
        return orig(*a, **kw)

    try:
        tbl.checkpoint = wrapped
    except Exception:                       # pragma: no cover - 只读属性时
        pass
    return box


def qs(stats, key, default=None):
    """从 stats 里安全取数（改前/改后的 stats 字段可能不同）"""
    try:
        return stats[key]
    except Exception:
        return default


# --------------------------------------------------------------- 单项测量
def rowwise_round(n: int, variant: str = 'table',
                  data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """一轮：N 行**逐行 autocommit** INSERT。

    variant='table' -> `Table.insert()`（引擎内部 API）
    variant='sql'   -> `Executor.execute_sql()`（完整 SQL 路径，含解析+审计）
    """
    fresh_dir(data_dir)
    db, tbl = new_table(data_dir)
    ck = count_checkpoints(tbl)
    try:
        if variant == 'table':
            t0 = time.perf_counter()
            for i in range(1, n + 1):
                tbl.insert([i, i % 1000])
            secs = time.perf_counter() - t0
        elif variant == 'sql':
            from lightdb.sql.executor import Executor
            ex = Executor(db)
            t0 = time.perf_counter()
            for i in range(1, n + 1):
                r = ex.execute_sql(f'INSERT INTO t VALUES ({i}, {i % 1000})')
                if isinstance(r, dict) and r.get('type') == 'error':
                    raise RuntimeError(f'INSERT 失败: {r}')
            secs = time.perf_counter() - t0
        else:
            raise ValueError(variant)
        wal = wal_size(data_dir)
    finally:
        db.shutdown()
    return {'n': n, 'variant': variant, 'secs': secs,
            'rows_s': n / secs if secs > 0 else 0.0,
            'wal_after': wal, 'checkpoints': ck['n'], 't_wall': time.time()}


def batch_round(n: int, batch: int = BATCH,
                data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """一轮：**单条多值 INSERT**，每条语句 batch 行（历史脚本的 batch_multi）"""
    fresh_dir(data_dir)
    db, tbl = new_table(data_dir)
    ck = count_checkpoints(tbl)
    n_stmts = (n + batch - 1) // batch
    try:
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        t0 = time.perf_counter()
        for s in range(n_stmts):
            lo = s * batch + 1
            hi = min(lo + batch - 1, n)
            vals = ','.join(f'({i},{i % 1000})' for i in range(lo, hi + 1))
            r = ex.execute_sql(f'INSERT INTO t VALUES {vals}')
            if isinstance(r, dict) and r.get('type') == 'error':
                raise RuntimeError(f'批量 INSERT 失败: {r}')
        secs = time.perf_counter() - t0
        wal = wal_size(data_dir)
    finally:
        db.shutdown()
    return {'n': n, 'variant': 'batch_multi', 'batch': batch,
            'statements': n_stmts, 'secs': secs,
            'rows_s': n / secs if secs > 0 else 0.0,
            'wal_after': wal, 'checkpoints': ck['n'], 't_wall': time.time()}


def batch_vs_individual_round(n: int = 5000, batch: int = BATCH,
                              data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """**同窗交错成对**测量"多值批量 vs 等价逐行语句"的比值。

    这是 `_heap_insert` / 语句级原子性那类改动唯一站得住的批量判据
    （本机实测：把 batch 语句与本应被它替代的 N 条单行语句放在**同一
    时间窗内交替**跑，比值才是可信的；顺序式对照会把机器吞吐下滑混
    进结论）。

    一侧 = 单条 `INSERT INTO t VALUES (..),(..),...`（`ceil(n/batch)` 条语句）
    另一侧 = n 条单行 `INSERT INTO t VALUES (..)`
    两侧都在全新的数据目录上跑、都从 0 行长到 n 行。

    返回 `ratio = batch_rows_s / individual_rows_s` 以及两侧的逐轮样本。
    """
    from lightdb.sql.executor import Executor

    def _batch_once():
        fresh_dir(data_dir)
        db, tbl = new_table(data_dir)
        n_stmts = (n + batch - 1) // batch
        try:
            ex = Executor(db)
            t0 = time.perf_counter()
            for s in range(n_stmts):
                lo = s * batch + 1
                hi = min(lo + batch - 1, n)
                vals = ','.join(f'({i},{i % 1000})' for i in range(lo, hi + 1))
                r = ex.execute_sql(f'INSERT INTO t VALUES {vals}')
                if isinstance(r, dict) and r.get('type') == 'error':
                    raise RuntimeError(f'批量 INSERT 失败: {r}')
            secs = time.perf_counter() - t0
        finally:
            db.shutdown()
        return n / secs if secs > 0 else 0.0

    def _individual_once():
        fresh_dir(data_dir)
        db, tbl = new_table(data_dir)
        try:
            ex = Executor(db)
            t0 = time.perf_counter()
            for i in range(1, n + 1):
                r = ex.execute_sql(f'INSERT INTO t VALUES ({i}, {i % 1000})')
                if isinstance(r, dict) and r.get('type') == 'error':
                    raise RuntimeError(f'INSERT 失败: {r}')
            secs = time.perf_counter() - t0
        finally:
            db.shutdown()
        return n / secs if secs > 0 else 0.0

    pairs = []
    for _ in range(3):
        b = _batch_once()
        i = _individual_once()
        pairs.append({'batch_rows_s': b, 'individual_rows_s': i,
                      'ratio': (b / i) if i else None})
    ratios = [p['ratio'] for p in pairs if p['ratio']]
    return {'n': n, 'batch': batch, 'pairs': pairs,
            'ratio': statistics.median(ratios) if ratios else 0.0,
            'batch_rows_s': statistics.median([p['batch_rows_s'] for p in pairs]),
            'individual_rows_s': statistics.median(
                [p['individual_rows_s'] for p in pairs])}


def point_inproc_round(nrows: int = 2000, nq: int = 600,
                       data_dir: str = DEFAULT_DATA_DIR,
                       warmup: int = 200) -> dict:
    """一轮：点查（进程内 `Executor.execute_sql`，走真实 SQL 解析 + 索引）"""
    fresh_dir(data_dir)
    db, tbl = new_table(data_dir)
    try:
        for i in range(1, nrows + 1):
            tbl.insert([i, i % 1000])
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        for i in range(warmup):              # warmup（不进结果）
            ex.execute_sql(f'SELECT v FROM t WHERE id = {i % nrows + 1}')
        t0 = time.perf_counter()
        for i in range(nq):
            r = ex.execute_sql(f'SELECT v FROM t WHERE id = {i % nrows + 1}')
            if isinstance(r, dict) and r.get('type') == 'error':
                raise RuntimeError(f'点查失败: {r}')
        secs = time.perf_counter() - t0
    finally:
        db.shutdown()
    return {'nq': nq, 'nrows': nrows, 'secs': secs,
            'q_s': nq / secs if secs > 0 else 0.0}


def point_tcp_round(nrows: int = 2000, nq: int = 600, port: int = 8977,
                    data_dir: str = DEFAULT_DATA_DIR,
                    warmup: int = 200) -> dict:
    """一轮：点查（真实 MySQL 协议 + pymysql，服务端在进程内线程里）

    注意：TCP 档比进程内档慢（多一次协议往返 + 服务端线程切换），
    实测 ~500 q/s，因此**不作为门禁指标**，只在 full 档作为参考量。
    """
    import threading

    import pymysql

    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    fresh_dir(data_dir)
    seed = Database(data_dir)
    tbl = seed.create_table('t', COLS)
    for i in range(1, nrows + 1):
        tbl.insert([i, i % 1000])
    seed.shutdown()

    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', port
    db = Database(data_dir)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.5)
    conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password='', autocommit=True)
    cur = conn.cursor()
    try:
        for i in range(warmup):
            cur.execute('SELECT v FROM t WHERE id = %s', (i % nrows + 1,))
            cur.fetchall()
        t0 = time.perf_counter()
        for i in range(nq):
            cur.execute('SELECT v FROM t WHERE id = %s', (i % nrows + 1,))
            cur.fetchall()
        secs = time.perf_counter() - t0
    finally:
        cur.close()
        conn.close()
        srv.shutdown()
        srv.server_close()
        db.shutdown()
    return {'nq': nq, 'nrows': nrows, 'secs': secs,
            'q_s': nq / secs if secs > 0 else 0.0}


# --------------------------------------------------------------- 崩溃恢复
CRASH_CHILD = r'''
import json, os, sys
sys.path.insert(0, {root!r})
from lightdb.db.database import Database
from lightdb.db.table import Table

Table.wal_checkpoint_bytes = {threshold!r}
Table.wal_checkpoint_records = 0

db = Database({data!r})
tbl = db.get_table('t')

for i in range(1, {n_committed} + 1):
    tbl.insert([i, i % 1000])

t = db.begin()
tbl.insert([{n_committed} + 1, 200], txn_id=t['txn_id'],
           read_view=t['read_view'])
db.commit(t)

t2 = db.begin()
for i in range({n_committed} + 2, {n_committed} + 2 + {n_uncommitted}):
    tbl.insert([i, 999], txn_id=t2['txn_id'], read_view=t2['read_view'])

tbl.bp.flush()                     # 最坏情况：未提交的脏页也被刷进数据文件
sys.stdout.write(json.dumps({{
    'wal': os.path.getsize(os.path.join({data!r}, 't.wal')),
    'rows_written': {n_committed} + 1,
}}))
sys.stdout.flush()
os._exit(1)                        # 硬杀：不 flush WAL 缓冲、不 close
'''


def crash_recovery_round(n_committed: int = 2000, n_uncommitted: int = 50,
                         threshold: int = 256 * 1024,
                         data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """一轮：硬杀子进程 -> 父进程重启恢复。

    返回:
      * `recover_secs`  —— 从 `Database(data_dir)` 到第一个 `SELECT COUNT(*)`
                           拿到正确行数之间的墙钟时间（**用户真实感知的恢复时间**）
      * `rows_seen` / `missing` / `leaked` —— 已提交行是否全在、未提交行是否泄漏
      * `wal_at_crash` / `wal_after` —— 崩溃时与恢复后的 WAL 字节数
    """
    fresh_dir(data_dir)
    seed_db, _ = new_table(data_dir)
    seed_db.shutdown()

    code = CRASH_CHILD.format(
        root=REPO_ROOT, data=data_dir, threshold=threshold,
        n_committed=n_committed, n_uncommitted=n_uncommitted)
    proc = subprocess.run([sys.executable, '-c', code],
                          capture_output=True, text=True)
    if proc.returncode != 1:                       # pragma: no cover
        raise RuntimeError(
            f'崩溃子进程退出码应为 1（os._exit(1)），实际 {proc.returncode}；'
            f'stderr={proc.stderr[-800:]}')
    info = json.loads(proc.stdout.strip().splitlines()[-1])

    from lightdb.db.database import Database
    t0 = time.perf_counter()
    db = Database(data_dir)                        # 这里做 WAL redo + undo
    tbl = db.get_table('t')
    _cols, rows = tbl.select(['id'])          # select 返回 (cols, rows)
    got = {r[0] for r in rows}
    recover_secs = time.perf_counter() - t0
    wal_after = wal_size(data_dir)
    db.shutdown()

    expected = set(range(1, n_committed + 2))
    missing = sorted(expected - got)
    leaked = sorted(i for i in range(n_committed + 2,
                                     n_committed + 2 + n_uncommitted)
                    if i in got)
    return {
        'n_committed': n_committed, 'n_uncommitted': n_uncommitted,
        'threshold': threshold,
        'recover_secs': recover_secs,
        'rows_seen': len(got), 'rows_expected': len(expected),
        'missing': len(missing), 'leaked': len(leaked),
        'wal_at_crash': info['wal'], 'wal_after': wal_after,
        'crash_exit_code': proc.returncode,
    }


# --------------------------------------------------------------- 汇总
def median_of(samples, key):
    return statistics.median([s[key] for s in samples])


def summarize(samples, key='rows_s', warmup_drop: bool = True) -> dict:
    """第 1 轮当 warmup 丢掉，其余取中位数（只有 1 轮时不丢）"""
    kept = samples[1:] if (warmup_drop and len(samples) > 1) else samples[:]
    vals = sorted(float(s[key]) for s in kept)
    secs = [float(s['secs']) for s in kept if 'secs' in s]
    out = {
        'metric_median': statistics.median(vals),
        'min': vals[0], 'max': vals[-1], 'n_samples': len(vals),
        'median_secs': statistics.median(secs) if secs else None,
        'samples': [{k: v for k, v in s.items() if k != 't_wall'}
                    for s in samples],
        'warmup_dropped': bool(warmup_drop and len(samples) > 1),
    }
    # 抖动 = (max-min)/median。门禁只用中位数；抖动值只用于事后判读
    out['spread_ratio'] = ((vals[-1] - vals[0]) / out['metric_median']
                           if out['metric_median'] else None)
    return out


# --------------------------------------------------------------- 同窗交错成对
def interleave_pair(n, cfg, other_root, pairs=3, variant='table',
                    self_root=REPO_ROOT, data_dir=None):
    """同一时间窗内交错跑 `self_root` 与 `other_root`，得到**成对**吞吐比。

    为什么必须这样量（原作者实测，本机复现）：吞吐在测量窗口内单调下滑
    （同代码 20k 档 90 分钟内 1,750 → 820 rows/s），先跑完 A 再跑 B 会把
    "机器漂移"混进结论。门禁阈值是静态的，不需要这个；只有论证
    "某次改动值多少" 时才用它。

    每个子进程用 `PYTHONPATH` 指定要加载的 lightdb 根目录，然后调用**本
    包的** `rowwise_round` —— 因此两侧代码同源、口径逐字一致。
    """
    out = {'n': n, 'cfg': cfg, 'pairs': [], 'self_root': self_root,
           'other_root': other_root, 'variant': variant}
    for i in range(pairs):
        a = _pair_child(self_root, n, cfg, variant)
        b = _pair_child(other_root, n, cfg, variant)
        out['pairs'].append({'self_rows_s': a, 'other_rows_s': b,
                             'ratio': (a / b) if b else None})
    ratios = [p['ratio'] for p in out['pairs'] if p['ratio']]
    out['median_ratio'] = statistics.median(ratios) if ratios else None
    return out


def _pair_child(root, n, cfg, variant):
    code = (
        'import json,sys\n'
        f'sys.path.insert(0, {REPO_ROOT!r})\n'
        'from benchmarks import heap_insert as H\n'
        f'H.prepare_env({cfg!r})\n'
        f'r = H.rowwise_round({n}, {variant!r})\n'
        'sys.stdout.write(json.dumps({"rows_s": r["rows_s"]}))\n')
    env = dict(os.environ)
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    p = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, env=env)
    if p.returncode != 0:                            # pragma: no cover
        return 0.0
    try:
        return float(json.loads(p.stdout.strip().splitlines()[-1])['rows_s'])
    except Exception:                                # pragma: no cover
        return 0.0
