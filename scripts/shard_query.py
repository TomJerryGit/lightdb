"""多进程只读查询分片：命令行入口 + 量化基准。

用法::

    # 对已有数据目录执行分片查询（与单进程基线对比）
    python scripts/shard_query.py --data-dir D:/data/db \
        --sql "SELECT id, v FROM t WHERE id BETWEEN 1 AND 1000" --workers 4 --baseline

    # 量化基准：10k/100k 行，1/2/4/8 worker；附进程内线程对比
    python scripts/shard_query.py --bench --rows 10000,100000 \
        --workers 1,2,4,8 --repeats 2 --json _analysis_tmp/sharding_bench.json

输出/证据：stdout 表格 + 可选 JSON（机器信息、各阶段耗时、加速比、开销）。

设计边界见 `lightdb/net/sharding.py` / `docs/MULTIPROCESS_SHARDING.md`：
仅只读、单表、整数单列主键、`WHERE pk BETWEEN a AND b`；不支持形态显式
1105；快照一致性为“checkpoint 后、无写入期间”。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.net import sharding as S  # noqa: E402


def _quiet():
    os.environ.setdefault('LIGHTDB_AUDIT_LEVEL', 'NONE')
    os.environ.setdefault('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                                # pragma: no cover
        pass


def _median(xs):
    import statistics
    return statistics.median(xs)


def build_dataset(data_dir, rows: int, batch: int = 500) -> Path:
    """用多值 INSERT 快速建表（id 1..rows, v=id*37%1000）。"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    data_dir = Path(data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir))
    try:
        ex = Executor(db)
        ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v INT)', {})
        t0 = time.perf_counter()
        for start in range(1, rows + 1, batch):
            end = min(rows, start + batch - 1)
            values = ', '.join('(%d, %d)' % (i, (i * 37) % 1000)
                               for i in range(start, end + 1))
            res = ex.execute_sql('INSERT INTO t (id, v) VALUES ' + values, {})
            if isinstance(res, dict) and res.get('type') == 'error':
                raise RuntimeError(f'建数据失败: {res}')
        build_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        db.checkpoint(force=True)
        ckpt_s = time.perf_counter() - t0
    finally:
        db.shutdown()
    print('  数据集: %d 行（导入 %.2fs + checkpoint %.2fs）'
          % (rows, build_s, ckpt_s))
    return data_dir


def _run_single(sql, data_dir, repeats: int) -> dict:
    samples = []
    for _ in range(max(1, repeats)):
        r = S.execute_single(sql, data_dir)
        samples.append(r['query_s'])
    return {'mode': 'single-process', 'samples_s': samples,
            'median_s': _median(samples),
            'rows': r['row_count']}


def _run_threads(sql, data_dir, workers_list, repeats: int) -> dict:
    out = {}
    for w in workers_list:
        samples = []
        for _ in range(max(1, repeats)):
            r = S.execute_threads(sql, data_dir, workers=w)
            samples.append(r['total_s'])
            row_count = r['row_count']
        out[w] = {'mode': 'in-process-threads', 'samples_s': samples,
                  'median_s': _median(samples), 'rows': row_count}
    return out


def _run_sharded(sql, data_dir, workers_list, repeats: int,
                 snapshot_dir=None, timeout_s=300) -> dict:
    out = {}
    for w in workers_list:
        samples, last = [], None
        for _ in range(max(1, repeats)):
            r = S.execute_sharded(sql, data_dir, workers=w,
                                  timeout_s=timeout_s,
                                  snapshot_dir=snapshot_dir)
            samples.append(r['timing']['total_s'])
            last = r
        out[w] = {
            'mode': 'multiprocess-sharded', 'workers': w,
            'samples_s': samples, 'median_s': _median(samples),
            'rows': last['row_count'], 'shards': len(last['shards']),
            'timing': last['timing'],
            'worker_query_s_max': last.get('worker_query_s_max'),
            'worker_query_s_sum': last.get('worker_query_s_sum'),
            'worker_open_s_total': last.get('worker_open_s_total'),
            'snapshot': last.get('snapshot'),
        }
    return out


def bench(rows_list, workers_list, repeats: int, do_threads: bool,
          reuse_snapshot: bool, timeout_s: float, json_out: str = '') -> dict:
    from lightdb.backup import snapshot as snap_mod

    _quiet()
    work = Path(tempfile.mkdtemp(prefix='lightdb_shard_bench_'))
    report = {
        'schema': 'lightdb-sharding-bench/1',
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'environment': {'platform': platform.platform(),
                        'python': platform.python_version(),
                        'cpu_count': os.cpu_count()},
        'workers': list(workers_list), 'repeats': repeats,
        'consistency': 'snapshot after checkpoint (WAL=0), no writes '
                       'during run; per-worker data copy',
        'cases': {},
    }
    try:
        for rows in rows_list:
            print('=' * 90)
            print('数据规模 %d 行' % rows)
            data_dir = build_dataset(work / ('db_%d' % rows), rows)
            sql = 'SELECT id, v FROM t WHERE id BETWEEN 1 AND %d' % rows
            base = _run_single(sql, data_dir, repeats)
            print('  单进程：median %.3fs（rows=%d）'
                  % (base['median_s'], base['rows']))
            threads = _run_threads(sql, data_dir, workers_list,
                                   max(1, repeats - 1)) if do_threads else {}
            for w, r in threads.items():
                print('  线程 %d：median %.3fs（加速 %.2fx）'
                      % (w, r['median_s'],
                         base['median_s'] / max(1e-9, r['median_s'])))
            # 现场快照
            snap_root = work / ('snap_%d' % rows)
            snap_info = None
            if reuse_snapshot:
                t0 = time.perf_counter()
                snap_info = snap_mod.create_snapshot(data_dir, snap_root,
                                                     force=True)
                snap_info = {'duration_s': time.perf_counter() - t0,
                             'row_total': snap_info.get('row_total'),
                             'total_bytes': snap_info.get('total_bytes'),
                             'wal_zero': snap_info.get('wal_zero')}
                print('  快照准备：%.3fs（复用给后续所有 worker 数）'
                      % snap_info['duration_s'])
            sharded_fresh = _run_sharded(
                sql, data_dir, workers_list, max(1, repeats - 1),
                snapshot_dir=None, timeout_s=timeout_s)
            for w, r in sharded_fresh.items():
                print('  多进程新快照 %d：median %.3fs（总加速 %.2fx，'
                      '查询段 %.3fs/片）'
                      % (w, r['median_s'],
                         base['median_s'] / max(1e-9, r['median_s']),
                         r.get('worker_query_s_max') or 0))
            sharded_reuse = {}
            if reuse_snapshot and snap_info:
                sharded_reuse = _run_sharded(
                    sql, data_dir, workers_list, max(1, repeats - 1),
                    snapshot_dir=snap_root, timeout_s=timeout_s)
                for w, r in sharded_reuse.items():
                    print('  多进程复用快照 %d：median %.3fs（总加速 %.2fx，'
                          '查询段 %.3fs/片）'
                          % (w, r['median_s'],
                             base['median_s'] / max(1e-9, r['median_s']),
                             r.get('worker_query_s_max') or 0))
            report['cases'][str(rows)] = {
                'sql': sql, 'single_process': base,
                'in_process_threads': threads,
                'multiprocess_fresh_snapshot': sharded_fresh,
                'multiprocess_reused_snapshot': sharded_reuse,
                'snapshot': snap_info,
            }
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if json_out:
        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                     encoding='utf-8')
        print('JSON: %s' % p)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='LightDB 多进程只读查询分片（仅只读/单表/整数主键 BETWEEN）')
    ap.add_argument('--data-dir', default='', help='已有数据目录（查询模式）')
    ap.add_argument('--sql', default='',
                    help='SELECT ... WHERE pk BETWEEN a AND b')
    ap.add_argument('--workers', default='4', help='分片/进程数，逗号分隔（基准模式）')
    ap.add_argument('--timeout', type=float, default=300.0,
                    help='worker 总超时秒数（默认 300）')
    ap.add_argument('--baseline', action='store_true',
                    help='查询模式附带单进程基线对比')
    ap.add_argument('--json', default='', help='JSON 输出路径')
    ap.add_argument('--bench', action='store_true', help='量化基准模式')
    ap.add_argument('--rows', default='10000,100000',
                    help='基准数据规模（逗号分隔，默认 10000,100000）')
    ap.add_argument('--repeats', type=int, default=2,
                    help='每项测量重复次数（默认 2，中位数）')
    ap.add_argument('--threads', action='store_true',
                    help='基准中同时测进程内多线程对照')
    ap.add_argument('--reuse-snapshot', action='store_true',
                    help='基准中复用同一快照测各 worker 数（分离快照开销）')
    args = ap.parse_args(argv)
    _quiet()
    if args.bench:
        rows_list = [int(x) for x in args.rows.split(',') if x.strip()]
        workers_list = [int(x) for x in args.workers.split(',') if x.strip()]
        report = bench(rows_list, workers_list, args.repeats, args.threads,
                       args.reuse_snapshot, args.timeout, args.json)
        print('=' * 90)
        print('基准完成；用例: %s' % sorted(report['cases']))
        return 0
    if not args.data_dir or not args.sql:
        print('参数错误: 查询模式需要 --data-dir 与 --sql（或使用 --bench）',
              file=sys.stderr)
        return 2
    workers = max(1, int(args.workers.split(',')[0]))
    try:
        r = S.execute_sharded(args.sql, args.data_dir, workers=workers,
                              timeout_s=args.timeout)
    except S.ShardingError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 3
    base = None
    if args.baseline:
        base = S.execute_single(args.sql, args.data_dir)
        print('单进程: %.3fs（rows=%d）'
              % (base['query_s'], base['row_count']))
    print(S.format_report(r))
    if base is not None:
        print('加速比（端到端/单进程查询）: %.2fx'
              % (base['query_s'] / max(1e-9, r['timing']['total_s'])))
    payload = dict(r)
    if base is not None:
        payload['baseline_single'] = base
    payload['env'] = {'python': platform.python_version(),
                      'platform': platform.platform()}
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                     encoding='utf-8')
        print('JSON: %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
