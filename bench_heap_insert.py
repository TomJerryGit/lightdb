"""_heap_insert 修复前后：INSERT 吞吐基准（阶段 2 后续基线）

用法（同一个脚本分别在「修复前 worktree」与「当前工作树」里跑）:

    # 当前工作树（after）
    python bench_heap_insert.py --label after --out result_after.json

    # 修复前对照（before）
    cd .wt_before && python bench_heap_insert.py --label before --out result_before.json

测量方式:
  * 每个配置 (variant, size, cfg) 跑 ROUNDS 次；第 1 次是 **warmup 丢弃**，
    其余 3 次取 **中位数**（另附 min/max）。
  * 每轮都从**全新数据目录**开始（CREATE TABLE + N 行 INSERT），
    因此每轮的表都从 0 行长到 N 行，不同轮之间无残留状态。
  * 计时只包住插入循环（不含建库/建表），用 time.perf_counter()。
  * variant 显式声明调用路径:
      table   = Table.insert()            （引擎内部 API，autocommit）
      sql     = Executor.execute_sql()    （完整 SQL 语句路径，含解析 + 审计）
      batch_multi = 一条多值 INSERT ... VALUES (..),(..),..（500 行/语句）
      batch_executemany = executemany 等价的逐语句序列（见报告说明）
  * 是否 autocommit: 是（txn_id=0，每条语句即一个事务）。
  * 是否显式 checkpoint(): **不调用**；checkpoint 只可能由阈值触发，
    开关由环境变量控制（见下）。

checkpoint 开关（cfg=off 时脚本自己 setdefault，必须在 import lightdb 之前生效）:
    LIGHTDB_WAL_CHECKPOINT_BYTES = 10**15      # 阈值大 -> 永不触发
    LIGHTDB_WAL_CHECKPOINT_RECORDS = 0         # 0 = 该维度不限制
  cfg=default 时不设这两个变量（走引擎默认 1 MB / 5000 条）。
  修复前代码（59ad0d3）根本没有 checkpoint 机制，环境变量对它完全无效，
  脚本会用 feature probe 记录这件事。

数据目录: 脚本所在目录下的 data_bench_heap/（每轮删掉重建，跑完删掉）。

本报告用到的命令（全部可原样复现；before 侧把脚本复制到
`git worktree add --detach .wt_before 59ad0d3` 里再跑同样的命令）:

    # ① 逐行 autocommit（核心，1k/5k/20k），checkpoint 默认
    python bench_heap_insert.py --label after --out result_after.json \
        --sizes 1000,5000,20000 --batch-sizes 5000,20000
    # ② 同上一组但 checkpoint 关掉（只跑 cfg=off，1k/20k 共 5 个有效样本）
    python bench_heap_insert.py --label after_recheck --out result_after_recheck.json \
        --sizes 1000,20000 --rounds 6 --skip-batch --skip-point --skip-default
    # ③ 逐行 SQL 路径（解析 + 审计）
    python bench_heap_insert.py --label after_sql --out result_after_sql.json \
        --sql-variant --sizes 1000,5000 --rounds 4 --skip-batch --skip-point --skip-off
    # ④ TCP 批量：pymysql.executemany vs 500 行多值 INSERT
    python bench_heap_insert.py --label after_tcpbatch --out result_after_tcpbatch.json \
        --batch-tcp 5000 --skip-rowwise --skip-batch --skip-point --rounds-tcp 3
    # ⑤ 消机器漂移的交错 A/B（self=当前树，other=另一个 checkout 的脚本）
    python bench_heap_insert.py --label pair20_off --out result_pair20_off.json \
        --pair .wt_before/bench_heap_insert.py --pair-n 20000 --pair-cfg off --pairs 3
    python bench_heap_insert.py --label ckpt_cost --out result_pair_ckptcost.json \
        --pair bench_heap_insert.py --pair-n 20000 --pair-cfg off \
        --pair-other-cfg default --pairs 3

    # 功能回归（基线一致）
    python -m pytest tests/test_txn_atomicity.py tests/test_mvcc_persistence.py \
        tests/test_checkpoint_wal.py -q
"""
import argparse
import gc
import json
import os
import shutil
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

COLS = [{'name': 'id', 'type': 'INT', 'primary': True},
        {'name': 'v', 'type': 'INT'}]
DATA_DIR = os.path.join(HERE, 'data_bench_heap')
BATCH = 500
POINT_ROWS = 2000
POINT_QUERIES = 1000
TCP_PORT = 8971


# --------------------------------------------------------------- checkpoint 开关
def apply_checkpoint_env(cfg):
    if cfg == 'off':
        os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] = str(10 ** 15)
        os.environ['LIGHTDB_WAL_CHECKPOINT_RECORDS'] = '0'
    else:
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_BYTES', None)
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_RECORDS', None)


# --------------------------------------------------------------- 基础工具
def fresh_dir():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def new_table(name='t'):
    from lightdb.db.database import Database
    db = Database(DATA_DIR)
    tbl = db.create_table(name, COLS)
    return db, tbl


def wal_size(name='t'):
    p = os.path.join(DATA_DIR, name + '.wal')
    return os.path.getsize(p) if os.path.exists(p) else 0


def count_checkpoints(tbl):
    """包一层 tbl.checkpoint，返回一个可读的计数器"""
    box = {'n': 0}
    orig = getattr(tbl, 'checkpoint', None)
    if orig is None:
        return box
    def wrapped(*a, **kw):
        box['n'] += 1
        return orig(*a, **kw)
    tbl.checkpoint = wrapped
    return box


def has_checkpoint_mechanism():
    """修复前代码没有 checkpoint：feature probe"""
    from lightdb.db.table import Table
    return hasattr(Table, 'wal_checkpoint_bytes') and \
        hasattr(Table, 'checkpoint')


def quiet_logger(level='ERROR'):
    """把全局 logger 压到 ERROR。

    必要性: `Executor.execute` 在语句耗时 >0.5s 时无条件 `logger.warn(...)`
    并把**整条 AST**（500 行多值 INSERT 时是几十 KB）打到 stdout；全局
    logger 的级别固定 INFO、不吃 LIGHTDB_LOG_LEVEL。不压掉的话，批量语句
    的计时里混进了打日志的 I/O，before/after 会同时被污染。
    """
    try:
        from lightdb.logger import logger as _lg
        _lg.set_level(level)
    except Exception:
        pass


def median_of(samples, key):
    vals = sorted(s[key] for s in samples)
    return statistics.median(vals)


def summarize(samples):
    """丢掉第 1 次（warmup），其余取中位数；只有 1 次时不丢"""
    kept = samples[1:] if len(samples) > 1 else samples[:]
    out = {
        'warmup_dropped': [] if len(samples) == 1 else samples[:1],
        'kept': kept,
        'median_rows_s': median_of(kept, 'rows_s'),
        'min_rows_s': min(s['rows_s'] for s in kept),
        'max_rows_s': max(s['rows_s'] for s in kept),
        'median_secs': median_of(kept, 'secs'),
        'median_wal_bytes_after': median_of(kept, 'wal_after'),
        'median_checkpoints': median_of(kept, 'checkpoints'),
    }
    return out


# --------------------------------------------------------------- 各测量项
def one_rowwise_round(n, variant):
    """一轮：N 行逐行 autocommit INSERT，返回耗时/吞吐/checkpoint 次数"""
    fresh_dir()
    db, tbl = new_table()
    ck = count_checkpoints(tbl)
    f = lambda i: [i, i % 1000]

    if variant == 'table':
        t0 = time.perf_counter()
        for i in range(1, n + 1):
            tbl.insert(f(i))
        secs = time.perf_counter() - t0
    elif variant == 'sql':
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        t0 = time.perf_counter()
        for i in range(1, n + 1):
            r = ex.execute_sql(f"INSERT INTO t VALUES ({i}, {i % 1000})")
            if isinstance(r, dict) and r.get('type') == 'error':
                raise RuntimeError(f'INSERT 失败: {r}')
        secs = time.perf_counter() - t0
    else:
        raise ValueError(variant)

    wal = wal_size()
    db.shutdown()
    return {'n': n, 'variant': variant, 'secs': secs,
            'rows_s': n / secs if secs > 0 else 0.0,
            'wal_after': wal, 'checkpoints': ck['n'],
            't_wall': time.time()}


def one_batch_round(n, variant, batch=BATCH):
    """一轮：批量插入。batch_multi = 多值 INSERT；batch_exec = 逐语句 executemany 等价"""
    fresh_dir()
    db, tbl = new_table()
    ck = count_checkpoints(tbl)
    n_stmts = (n + batch - 1) // batch

    if variant == 'batch_multi':
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
    elif variant == 'batch_exec':
        # pymysql.executemany 默认就是"逐条 execute"（协议层不带多值合并），
        # 这里在**同一条 SQL 模板 + 参数列表**的语义下复现同一条路径。
        from lightdb.sql.executor import Executor
        ex = Executor(db)
        t0 = time.perf_counter()
        for s in range(n_stmts):
            lo = s * batch + 1
            hi = min(lo + batch - 1, n)
            for i in range(lo, hi + 1):
                r = ex.execute_sql(f'INSERT INTO t VALUES ({i}, {i % 1000})')
                if isinstance(r, dict) and r.get('type') == 'error':
                    raise RuntimeError(f'INSERT 失败: {r}')
        secs = time.perf_counter() - t0
    else:
        raise ValueError(variant)

    wal = wal_size()
    db.shutdown()
    return {'n': n, 'variant': variant, 'batch': batch, 'statements': n_stmts,
            'secs': secs, 'rows_s': n / secs if secs > 0 else 0.0,
            'wal_after': wal, 'checkpoints': ck['n'], 't_wall': time.time()}


def point_inproc_round(nq=POINT_QUERIES):
    """点查（进程内 Executor.execute_sql）"""
    from lightdb.sql.executor import Executor
    ex = Executor(db_global)
    for i in range(200):                      # warmup（不进结果）
        ex.execute_sql(f'SELECT v FROM t WHERE id = {i % POINT_ROWS + 1}')
    t0 = time.perf_counter()
    for i in range(nq):
        r = ex.execute_sql(f'SELECT v FROM t WHERE id = {i % POINT_ROWS + 1}')
        if isinstance(r, dict) and r.get('type') == 'error':
            raise RuntimeError(f'点查失败: {r}')
    secs = time.perf_counter() - t0
    return {'nq': nq, 'secs': secs, 'q_s': nq / secs}


def point_tcp_round(nq=POINT_QUERIES):
    """点查（真实 MySQL 协议 + pymysql），服务端在进程内线程里"""
    import threading
    import pymysql
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    fresh_dir()
    sv_db = Database(DATA_DIR)
    tbl = sv_db.create_table('t', COLS)
    for i in range(1, POINT_ROWS + 1):
        tbl.insert([i, i % 1000])
    sv_db.shutdown()

    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', TCP_PORT
    sv_db = Database(DATA_DIR)
    ex = Executor(sv_db)
    srv = MySQLServer(cfg, sv_db, ex)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.5)
    conn = pymysql.connect(host='127.0.0.1', port=TCP_PORT, user='root',
                           password='', autocommit=True)
    cur = conn.cursor()
    try:
        for i in range(200):                  # warmup
            cur.execute('SELECT v FROM t WHERE id = %s',
                        (i % POINT_ROWS + 1,))
            cur.fetchall()
        t0 = time.perf_counter()
        for i in range(nq):
            cur.execute('SELECT v FROM t WHERE id = %s',
                        (i % POINT_ROWS + 1,))
            cur.fetchall()
        secs = time.perf_counter() - t0
    finally:
        cur.close()
        conn.close()
        srv.shutdown()
        srv.server_close()
        sv_db.shutdown()
    return {'nq': nq, 'secs': secs, 'q_s': nq / secs}


def one_batch_tcp_round(n, batch=BATCH):
    """一轮：真实 MySQL 协议下 pymysql.executemany（500 行/调用）+ 多值 INSERT

    这是"批量是否仍然更慢"最贴近客户端现场的量法：同一个连接、同一个 cursor，
    executemany 走 pymysql 的逐条 execute，multi 走一条 500 行的多值语句。
    """
    import threading
    import pymysql
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    fresh_dir()
    sv_db = Database(DATA_DIR)
    sv_db.create_table('t', COLS)
    sv_db.shutdown()

    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', TCP_PORT
    sv_db = Database(DATA_DIR)
    ex = Executor(sv_db)
    srv = MySQLServer(cfg, sv_db, ex)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.5)
    conn = pymysql.connect(host='127.0.0.1', port=TCP_PORT, user='root',
                           password='', autocommit=True)
    cur = conn.cursor()
    n_stmts = (n + batch - 1) // batch
    res = {}
    try:
        cur.execute('DELETE FROM t')          # 只清行，不删表
        # -- executemany（逐条 execute 语义）
        t0 = time.perf_counter()
        for s in range(n_stmts):
            lo = s * batch + 1
            hi = min(lo + batch - 1, n)
            cur.executemany('INSERT INTO t VALUES (%s, %s)',
                            [(i, i % 1000) for i in range(lo, hi + 1)])
        res['executemany_secs'] = time.perf_counter() - t0
        cur.execute('DELETE FROM t')
        # -- 单条多值 INSERT（500 行/语句）
        t0 = time.perf_counter()
        for s in range(n_stmts):
            lo = s * batch + 1
            hi = min(lo + batch - 1, n)
            vals = ','.join(f'({i},{i % 1000})' for i in range(lo, hi + 1))
            cur.execute(f'INSERT INTO t VALUES {vals}')
        res['multi_secs'] = time.perf_counter() - t0
        cur.execute('SELECT COUNT(*) FROM t')
        res['rows'] = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()
        srv.shutdown()
        srv.server_close()
        sv_db.shutdown()
    return res


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--label', default='current')
    ap.add_argument('--out', default=None)
    ap.add_argument('--rounds', type=int, default=4, help='含 1 次 warmup')
    ap.add_argument('--rounds-sql', type=int, default=4,
                    help='SQL 逐行路径的轮数（含 1 次 warmup）')
    ap.add_argument('--sizes', default='1000,5000,20000')
    ap.add_argument('--skip-rowwise', action='store_true',
                    help='跳过逐行档（配合 --rounds 复核批量）')
    ap.add_argument('--sql-sizes', default='1000,5000',
                    help='SQL 逐行路径的档位（20k 太慢，默认不跑）')
    ap.add_argument('--batch-sizes', default='5000,20000')
    ap.add_argument('--batch', type=int, default=BATCH)
    ap.add_argument('--batch-tcp', type=int, default=0,
                    help='额外跑一组真实 MySQL 协议（pymysql.executemany vs '
                         '多值 INSERT），参数是总行数；0=不跑')
    ap.add_argument('--rounds-tcp', type=int, default=3,
                    help='TCP 批量轮数（含 1 次 warmup）')
    ap.add_argument('--skip-point', action='store_true')
    ap.add_argument('--skip-batch', action='store_true')
    ap.add_argument('--skip-default', action='store_true',
                    help='跳过 cfg=default（只跑 cfg=off）')
    ap.add_argument('--skip-off', action='store_true',
                    help='跳过 cfg=off')
    ap.add_argument('--sql-variant', action='store_true',
                    help='逐行用 SQL 路径（含解析+审计）而不是 Table.insert')
    ap.add_argument('--audit-level', default='NONE',
                    help="LIGHTDB_AUDIT_LEVEL（'NONE' 关闭逐语句审计落盘；"
                         "'DML' 复现默认值）")
    ap.add_argument('--log-level', default='ERROR',
                    help='LIGHTDB_LOG_LEVEL（ERROR 时慢查询告警不进控制台）')
    ap.add_argument('--silent', action='store_true', help='子进程模式，只写 JSON')
    ap.add_argument('--pair', metavar='OTHER_SCRIPT',
                    help='与本树交错跑 OTHER_SCRIPT 所在树（消机器漂移）')
    ap.add_argument('--pair-n', type=int, default=20000)
    ap.add_argument('--pair-cfg', default='off', choices=['off', 'default'])
    ap.add_argument('--pair-other-cfg', default=None,
                    choices=['off', 'default'],
                    help='另一侧用不同 checkpoint 配置（默认同一配置）；'
                         '形如 --pair <本文件> --pair-cfg off '
                         '--pair-other-cfg default 可量 checkpoint 自身代价')
    ap.add_argument('--pairs', type=int, default=3)
    args = ap.parse_args()

    os.environ['LIGHTDB_AUDIT_LEVEL'] = args.audit_level
    os.environ['LIGHTDB_LOG_LEVEL'] = args.log_level
    quiet_logger()

    if args.pair:
        res = interleaved_pair(args.pair_n, args.pair_cfg, args.pair,
                               args.pairs, args.pair_other_cfg)
        out = args.out or f'result_pair_{args.label}.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f'# 交错结果写入 {out}')
        sys.stdout.flush()
        os._exit(0)

    sizes = [int(x) for x in args.sizes.split(',') if x]
    batch_sizes = [int(x) for x in args.batch_sizes.split(',') if x]
    results = {'label': args.label, 'rounds': args.rounds,
               'rounds_sql': args.rounds_sql, 'sizes': sizes,
               'sql_sizes': args.sql_sizes,
               'batch_sizes': batch_sizes, 'batch': args.batch,
               'audit_level': args.audit_level, 'log_level': args.log_level,
               'python': sys.version.split()[0],
               'cwd': HERE, 'git': git_rev(), 'cells': {}}

    # ---------- 逐行 ----------
    variant = 'sql' if args.sql_variant else 'table'
    rounds = args.rounds_sql if variant == 'sql' else args.rounds
    use_sizes = ([int(x) for x in args.sql_sizes.split(',') if x]
                 if variant == 'sql' else sizes)
    for cfg in ([] if args.skip_rowwise else
                (['default'] if args.skip_off else
                 (['off'] if args.skip_default else ['default', 'off']))):
        apply_checkpoint_env(cfg)
        has = has_checkpoint_mechanism()
        results['cells'].setdefault('rowwise', {})
        for n in use_sizes:
            key = f'{variant}/{cfg}/{n}'
            samples = [one_rowwise_round(n, variant)
                       for _ in range(rounds)]
            results['cells']['rowwise'][key] = summarize(samples)
            results['cells']['rowwise'][key]['checkpoint_mechanism'] = has
            if not args.silent:
                print(f'[rowwise {variant} cfg={cfg} n={n}] '
                      f"{results['cells']['rowwise'][key]['median_rows_s']:.1f} rows/s")

    # ---------- 批量 ----------
    if not args.skip_batch:
        apply_checkpoint_env('default')
        results['cells']['batch'] = {}
        for n in batch_sizes:
            for bv in ('batch_multi', 'batch_exec'):
                key = f'{bv}/default/{n}'
                samples = [one_batch_round(n, bv, args.batch)
                           for _ in range(args.rounds)]
                results['cells']['batch'][key] = summarize(samples)
                if not args.silent:
                    print(f"[batch {bv} n={n} batch={args.batch}] "
                          f"{results['cells']['batch'][key]['median_rows_s']:.1f} rows/s")

    # ---------- 批量（真实 MySQL 协议 / pymysql.executemany） ----------
    if args.batch_tcp:
        apply_checkpoint_env('default')
        results['cells'].setdefault('batch_tcp', {})
        rounds_res = [one_batch_tcp_round(args.batch_tcp, args.batch)
                      for _ in range(args.rounds_tcp)]
        kept = rounds_res[1:] if len(rounds_res) > 1 else rounds_res
        for meth in ('executemany', 'multi'):
            vals = sorted(r[f'{meth}_secs'] for r in kept)
            med = statistics.median(vals)
            results['cells']['batch_tcp'][f'{meth}/{args.batch_tcp}'] = {
                'rows_s': args.batch_tcp / med, 'median_secs': med,
                'min_rows_s': args.batch_tcp / max(vals),
                'max_rows_s': args.batch_tcp / min(vals),
                'secs_samples': [round(v, 2) for v in vals],
                'rows_written': kept[0]['rows']}
            if not args.silent:
                print(f"[batch_tcp {meth} n={args.batch_tcp} batch={args.batch}] "
                      f"{args.batch_tcp/med:.1f} rows/s (median {med:.2f}s)")

    # ---------- 点查 ----------
    if not args.skip_point:
        global db_global
        apply_checkpoint_env('default')
        fresh_dir()
        from lightdb.db.database import Database as _DB
        db_global = _DB(DATA_DIR)
        tbl = db_global.create_table('t', COLS)
        for i in range(1, POINT_ROWS + 1):
            tbl.insert([i, i % 1000])
        results['cells']['point'] = {}
        samples = [point_inproc_round() for _ in range(args.rounds)]
        results['cells']['point']['inproc'] = summarize(
            [{'rows_s': s['q_s'], 'secs': s['secs'], 'wal_after': 0,
              'checkpoints': 0} for s in samples])
        print(f"[point inproc] "
              f"{results['cells']['point']['inproc']['median_rows_s']:.1f} q/s")
        db_global.shutdown()
        samples = [point_tcp_round() for _ in range(args.rounds)]
        results['cells']['point']['tcp'] = summarize(
            [{'rows_s': s['q_s'], 'secs': s['secs'], 'wal_after': 0,
              'checkpoints': 0} for s in samples])
        print(f"[point tcp] "
              f"{results['cells']['point']['tcp']['median_rows_s']:.1f} q/s")

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    gc.collect()
    out = args.out or f'result_{args.label}.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    if not args.silent:
        print(f'# 结果写入 {out}')
    # 显式退出：确保 TCP 服务端线程/端口不会把进程挂住（先前实测有残留进程）
    sys.stdout.flush()
    os._exit(0)


def git_rev():
    import subprocess
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                              cwd=HERE, capture_output=True, text=True).stdout.strip()
    except Exception:
        return '?'


# --------------------------------------------------------------- 交错 A/B（消漂移）
def interleaved_pair(n, cfg, other_script, pairs, cfg_other=None):
    """同一时间窗内交错跑 `本树` 与 `other_script` 所在树，得到成对吞吐比。

    为什么要这个模式：本机在整个测量时段内吞吐**单调下滑**（同代码 20k 档
    从 ~1750 掉到 ~820 rows/s，2 分钟内 -25%）。先跑完 A 再跑 B 的对照组会
    把"机器漂移"混进结论。这里每个 pair 内 A/B 只隔一次运行，取成对比值。
    """
    import subprocess
    cfg_other = cfg_other or cfg
    self_script = os.path.abspath(__file__)
    other_script = os.path.abspath(other_script)
    out = {'n': n, 'cfg_self': cfg, 'cfg_other': cfg_other, 'pairs': [],
           'self_script': self_script, 'other_script': other_script}
    for i in range(pairs):
        a = run_one_subprocess(
            [sys.executable, '-u', self_script, '--label', 'pair_run',
             '--sizes', str(n), '--rounds', '1', '--silent',
             '--skip-batch', '--skip-point',
             '--skip-off' if cfg == 'default' else '--skip-default',
             '--out', f'_pair_self_{i}.json'], cwd=HERE)
        b = run_one_subprocess(
            [sys.executable, '-u', other_script, '--label', 'pair_run',
             '--sizes', str(n), '--rounds', '1', '--silent',
             '--skip-batch', '--skip-point',
             '--skip-off' if cfg_other == 'default' else '--skip-default',
             '--out', f'_pair_other_{i}.json'],
            cwd=os.path.dirname(other_script))
        out['pairs'].append({
            'self_rows_s': a, 'other_rows_s': b,
            'ratio': (a / b) if b else None})
        print(f'  pair {i+1}: self={a:.0f} other={b:.0f} '
              + (f'ratio={a/b:.2f}x' if b else 'ratio=? (other 失败)'))
    if len(out['pairs']) >= 2:
        out['median_ratio'] = statistics.median(
            p['ratio'] for p in out['pairs'] if p['ratio'])
    return out


def run_one_subprocess(cmd, cwd=None, env=None):
    """跑一轮 bench（子进程），返回 rows_s"""
    import subprocess
    r = subprocess.run(cmd, cwd=cwd or HERE, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        return 0.0
    out = cmd[cmd.index('--out') + 1]
    p = os.path.join(cwd or HERE, out)
    d = json.load(open(p, encoding='utf-8'))
    cell = list(d['cells']['rowwise'].values())[0]
    os.remove(p)
    return cell['median_rows_s']


if __name__ == '__main__':
    main()
