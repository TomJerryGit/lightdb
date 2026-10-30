"""多进程只读查询分片原型（突破 GIL 验证，P4 backlog）。

目标：对**单表、无 JOIN/聚合**的 `SELECT ... WHERE <pk> BETWEEN a AND b`
按主键范围切成 N 片，交给 N 个**独立 Python 进程**执行，结果按主键合并排序。
仅用于验证"多进程能否绕过 GIL + 表级锁实现只读查询扩展"，不是通用 SQL 路由。

一致性方案（重要）
==================

引擎当前没有"同一 data-dir 的多进程只读打开"（`Database.__init__` 会建目录/
读写元数据/WAL 恢复，且多实例共享文件不安全）。因此本原型采用：

1. master 用 `lightdb.backup.snapshot.create_snapshot()` 生成**一次性离线快照**
   （内部先 `db.checkpoint()`；WAL 必须为 0，否则拒绝，保证快照一致）；
2. 每个 worker 复制快照的 `data/` 到自己的临时目录后再打开（每个 worker 独占
   一份文件，避免并发 WAL/元数据写冲突）；
3. 分片范围互不重叠，master 按主键把各片结果归并排序。

一致性 = **快照时刻**（checkpoint 后、无写入期间）；本原型**拒绝在快照生成
期间有写入的目录**（依赖 snapshot 的 WAL=0 检查）。快照生成、每 worker 复制、
进程启动、结果回传都计入开销并单独报告。

不支持形态
==========
以下全部**显式抛** `UnsupportedShardQuery`（MySQL 错误码 1105，HY000），
绝不静默给出错误结果：JOIN / 聚合 / GROUP BY / HAVING / DISTINCT / 子查询 /
UNION / CTE / LIMIT/OFFSET / ORDER BY、非 BETWEEN 谓词、PK 非单列或非整数、
SELECT 不包含主键列（无法合并）、表不存在/复合主键等。

用法（API）::

    from lightdb.net.sharding import execute_sharded, execute_single
    r = execute_sharded('SELECT * FROM t WHERE id BETWEEN 1 AND 100',
                        data_dir='D:/data/db', workers=4)
    print(r['rows'], r['timing'], r['consistency'])

CLI 见 `scripts/shard_query.py`；量化数据与边界见 `docs/MULTIPROCESS_SHARDING.md`。
"""
from __future__ import annotations

import json
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[2])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ..logger import logger  # noqa: E402  （分片路由/回退日志）

#: 不支持形态统一错误码（MySQL 1105 / HY000）
MYSQL_UNKNOWN_ERROR = 1105
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_INT_TYPE_RE = re.compile(r'^(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT)$')
_BOOTSTRAP = (
    'import sys; '
    'from lightdb.net.sharding import _worker_main; '
    '_worker_main(sys.argv[1], sys.argv[2])'
)
STARTUP_TIMEOUT_DEFAULT = 60.0
#: 分片临时目录放在工作区内（沙箱/杀软对系统 temp 的读取偶发拒绝；
#: 工作区路径对 master 与 worker 子进程都稳定可读写）
TMP_BASE = Path(REPO_ROOT) / '_analysis_tmp' / 'sharding_runs'


def _make_tmp_root() -> Path:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix='run_', dir=str(TMP_BASE)))


class ShardingError(Exception):
    """分片执行错误基类（默认 1105，显式失败，不静默）。"""

    code = MYSQL_UNKNOWN_ERROR
    sqlstate = 'HY000'

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {'error': self.message, 'code': self.code,
                'sqlstate': self.sqlstate, 'type': type(self).__name__,
                **self.extra}


class UnsupportedShardQuery(ShardingError):
    """不支持的查询形态（显式 1105；不猜测、不降级成错误答案）。"""


class ShardWorkerError(ShardingError):
    """worker 崩溃/超时/执行失败（显式；含 worker 序号与退出码/耗时）。"""


# ==================== 查询解析与校验 ====================

def _unsupported(reason: str, **extra):
    raise UnsupportedShardQuery(f'不支持的分片查询: {reason}', **extra)


def parse_range_query(sql: str) -> dict:
    """解析并校验 `SELECT ... WHERE <pk> BETWEEN a AND b`，返回 plan。

    只校验 SQL 结构；表存在性/主键列/类型由 `_table_meta()` 在拿到数据目录
    元数据后校验（避免对不支持形态白做快照）。
    """
    if not isinstance(sql, str) or not sql.strip():
        _unsupported('SQL 为空')
    from lightdb.sql.parser import Parser
    try:
        ast = Parser().parse(sql)
    except Exception as exc:                         # noqa: BLE001
        _unsupported(f'SQL 解析失败: {exc}')
    if not isinstance(ast, dict) or ast.get('command') != 'SELECT':
        _unsupported('仅支持单条 SELECT 查询')
    if ast.get('joins'):
        _unsupported('不支持 JOIN')
    for key, label in (('group_by', 'GROUP BY'), ('having', 'HAVING'),
                       ('distinct', 'DISTINCT'), ('windows', '窗口函数'),
                       ('subquery', '子查询'), ('limit', 'LIMIT'),
                       ('offset', 'OFFSET')):
        if ast.get(key):
            _unsupported(f'不支持 {label}')
    if ast.get('ctes'):
        _unsupported('不支持 CTE')
    if ast.get('union') or ast.get('union_parts'):
        _unsupported('不支持 UNION')
    if ast.get('order_by'):
        _unsupported('不支持 ORDER BY（结果统一按主键升序合并）')
    if len(ast.get('projection') or []) == 0:
        _unsupported('缺少 SELECT 列')
    for entry in ast.get('projection') or []:
        # 允许 ('col', name) 与 '*'；其余（函数/表达式/字面量）一律拒绝
        if isinstance(entry, (tuple, list)) and entry:
            if entry[0] in ('*', 'col') and (entry[0] == '*' or len(entry) > 1):
                continue
        _unsupported('SELECT 仅支持列名或 *（不支持表达式/聚合函数）')
    table = ast.get('table')
    if not table or not _IDENT_RE.match(str(table)):
        _unsupported('仅支持单表且表名必须为简单标识符')
    if ast.get('no_from'):
        _unsupported('不支持无 FROM 查询')
    where = ast.get('where')
    pred = where.get('expr_pred') if isinstance(where, dict) else None
    if not pred or pred.get('op') != 'BETWEEN':
        _unsupported('仅支持 WHERE <pk> BETWEEN a AND b 单谓词')
    left = pred.get('left') or {}
    if left.get('kind') != 'col' or not _IDENT_RE.match(str(left.get('name'))):
        _unsupported('BETWEEN 左值必须是主键列')
    low = (pred.get('low') or {}).get('value')
    high = (pred.get('high') or {}).get('value')
    if isinstance(low, bool) or isinstance(high, bool) or not isinstance(low, int) \
            or not isinstance(high, int):
        _unsupported('BETWEEN 边界必须是整数字面量（本原型仅支持整数主键）')
    columns = ast.get('columns') or []
    wanted = None
    if columns != ['*']:
        wanted = [str(c) for c in columns]
        for name in wanted:
            if not _IDENT_RE.match(name):
                _unsupported(f'不支持的 SELECT 列: {name!r}')
    return {
        'sql': sql.strip().rstrip(';').strip(),
        'table': str(table),
        'pred_col': str(left.get('name')),
        'wanted': wanted,
        'lo': int(low),
        'hi': int(high),
    }


def _read_meta(data_dir) -> dict:
    path = Path(data_dir) / '_meta.json'
    if not path.is_file():
        raise ShardingError(f'数据目录缺少 _meta.json: {path}')
    try:
        meta = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:                         # noqa: BLE001
        raise ShardingError(f'_meta.json 读取失败: {exc}') from exc
    return meta


def _table_meta(data_dir, table: str) -> dict:
    """从 _meta.json 读取单表主键/列/类型（只读，不打开 Database）。"""
    tables = (_read_meta(data_dir) or {}).get('tables') or {}
    if table not in tables:
        raise ShardingError(f'表不存在: {table!r}（现有: {sorted(tables)}）')
    cols = tables[table].get('cols') or []
    if not cols:
        raise ShardingError(f'表 {table!r} 没有列元数据')
    pk_cols = [c.get('name') for c in cols if c.get('primary')]
    if len(pk_cols) != 1:
        _unsupported(f'表 {table!r} 主键必须为单列（实际 {pk_cols}）')
    pk = str(pk_cols[0])
    types = {str(c.get('name')): str(c.get('type') or '').upper().split('(')[0]
             for c in cols}
    if not _INT_TYPE_RE.match(types.get(pk, '')):
        _unsupported(f'主键 {pk!r} 必须是整数类型（实际 {types.get(pk)!r}）')
    return {'table': table, 'pk': pk, 'columns': [str(c.get('name')) for c in cols],
            'types': types}


def _resolve_plan(plan: dict, meta: dict) -> dict:
    """把 SQL plan 与表元数据对齐：校验 PK/列/类型，算出 select 与 PK 下标。"""
    table, pk = meta['table'], meta['pk']
    if plan['pred_col'] != pk:
        _unsupported(f'BETWEEN 列 {plan["pred_col"]!r} 不是主键 {pk!r}')
    columns = list(meta['columns'])
    wanted = plan['wanted']
    if wanted is None:
        select_sql = '*'
        result_columns = columns
        pk_index = columns.index(pk)
    else:
        for name in wanted:
            if name not in columns:
                raise ShardingError(f'列不存在: {name!r}（表 {table!r}）')
        if pk not in wanted:
            _unsupported(f'SELECT 必须包含主键列 {pk!r}（否则无法按 key 合并）')
        select_sql = ', '.join(wanted)
        result_columns = wanted
        pk_index = wanted.index(pk)
    return {
        'table': table, 'pk': pk, 'pk_index': pk_index,
        'select_sql': select_sql, 'result_columns': result_columns,
        'lo': plan['lo'], 'hi': plan['hi'],
    }


def _split_range(lo: int, hi: int, workers: int) -> list:
    """把闭区间 [lo, hi] 切成至多 workers 个互不重叠的连续整数子区间。"""
    if hi < lo:
        return []
    total = hi - lo + 1
    n = max(1, min(int(workers), total))
    base = total // n
    extra = total % n
    shards, start = [], lo
    for i in range(n):
        size = base + (1 if i < extra else 0)
        end = start + size - 1
        shards.append({'lo': start, 'hi': end})
        start = end + 1
    return shards


# ==================== 快照准备 ====================

def _create_snapshot(data_dir, snapshot_root) -> dict:
    from lightdb.backup import snapshot as snap
    t0 = time.perf_counter()
    result = snap.create_snapshot(data_dir, snapshot_root, force=True)
    elapsed = time.perf_counter() - t0
    if not result.get('wal_zero'):
        raise ShardingError('快照 WAL 非零，拒绝用于分片查询（可能存在未提交事务）')
    return {'result': result, 'elapsed_s': elapsed,
            'snapshot_dir': str(snapshot_root),
            'data_dir': str(Path(snapshot_root) / 'data')}


def _snapshot_data_dir(snapshot_dir) -> Path:
    data = Path(snapshot_dir) / 'data'
    if not data.is_dir():
        raise ShardingError(f'快照缺少 data/ 子目录: {snapshot_dir}')
    return data


# ==================== worker 进程 ====================

def _worker_main(task_path: str, result_path: str) -> None:
    """worker 进程入口（独立 Python 进程；由 master 以 subprocess 启动）。

    通过文件交换 task/result（本沙箱禁 Windows 命名管道，multiprocessing
    Pipe/Queue 不可用），行结果用 pickle 回传便于 10 万行级别测量。
    """
    task = {}
    try:
        with open(task_path, 'rb') as f:
            task = pickle.load(f)
        inject = task.get('_inject') or {}
        if inject.get('crash'):
            os._exit(7)                              # 模拟 worker 崩溃
        if inject.get('sleep'):
            time.sleep(float(inject['sleep']))
        if inject.get('error'):
            raise RuntimeError(str(inject['error']))
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        t0 = time.perf_counter()
        t_open = time.perf_counter()
        db = Database(task['data_dir'])
        open_s = time.perf_counter() - t_open
        try:
            ex = Executor(db)
            t_q = time.perf_counter()
            res = ex.execute_sql(task['sql'], {})
            query_s = time.perf_counter() - t_q
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise RuntimeError(f'查询失败: {res}')
            elapsed = time.perf_counter() - t0
            payload = {'ok': True, 'columns': res.get('columns') or [],
                       'rows': res.get('rows') or [],
                       'elapsed_s': elapsed, 'open_s': open_s,
                       'query_s': query_s, 'pid': os.getpid(),
                       'lo': task.get('lo'), 'hi': task.get('hi')}
        finally:
            try:
                db.shutdown()
            except Exception:                        # pragma: no cover
                pass
    except BaseException as exc:                     # noqa: BLE001
        payload = {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                   'trace': __import__('traceback').format_exc()[-4000:],
                   'pid': os.getpid(), 'lo': task.get('lo'),
                   'hi': task.get('hi')}
    tmp = result_path + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(payload, f)
    os.replace(tmp, result_path)
    raise SystemExit(0 if payload.get('ok') else 3)


def _worker_env() -> dict:
    env = dict(os.environ)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['LIGHTDB_AUDIT_LEVEL'] = 'NONE'
    env['LIGHTDB_LOG_LEVEL'] = 'ERROR'
    return env


# ==================== 分片执行 ====================

def execute_sharded(sql: str, data_dir, workers: int = 4,
                    timeout_s: float = 120.0, snapshot_dir=None,
                    keep_snapshot: bool = False,
                    _worker_inject: dict = None) -> dict:
    """只读分片执行入口；返回 rows/columns/分片明细/各阶段耗时/一致性说明。

    参数
    ----
    workers      : 期望分片/进程数（实际会被请求范围行数截断）
    timeout_s    : 单个 worker 等待总超时（超时显式 ShardWorkerError）
    snapshot_dir : 复用已有快照根目录（含 manifest.json + data/）；为空则现场生成
    keep_snapshot: 调试用；保留生成/复制的临时目录
    _worker_inject: 仅测试用（{'crash':True}/{'sleep':n}/{'error':'msg'}）
    """
    t_start = time.perf_counter()
    timings = {}
    data_dir = Path(data_dir)
    plan = parse_range_query(sql)
    timings['parse_s'] = time.perf_counter() - t_start
    # 先读数据目录元数据（fail-fast，不支持形态不生成快照）
    meta = _table_meta(data_dir, plan['table'])
    resolved = _resolve_plan(plan, meta)
    shards = _split_range(plan['lo'], plan['hi'], workers)
    report = {
        'schema': 'lightdb-multiprocess-sharding/1',
        'sql': plan['sql'], 'table': resolved['table'],
        'primary_key': resolved['pk'], 'range': [plan['lo'], plan['hi']],
        'workers_requested': max(1, int(workers)),
        'shards': shards,
        'columns': resolved['result_columns'], 'rows': [],
        'row_count': 0,
        'timing': timings,
        'consistency': {
            'mode': 'snapshot-copy-per-worker',
            'snapshot_moment': 'checkpoint 后、无写入期间',
            'writes_allowed': False, 'transactions': False,
            'note': '引擎不支持同一 data-dir 多进程只读打开；每 worker 独占一份'
                    '快照 data/ 拷贝后打开，避免 WAL/元数据写冲突。',
        },
        'worker_errors': [],
        'worker_pids': [],
    }
    tmp_root = _make_tmp_root()
    snapshot_created = False
    worker_dirs = []
    try:
        if not shards:
            # 空区间（hi < lo）：无需快照/进程
            report['shards'] = []
            for key in ('snapshot_s', 'copy_s', 'spawn_s', 'exec_s',
                        'merge_s'):
                timings[key] = 0.0
            report['timing']['total_s'] = time.perf_counter() - t_start
            return report

        if snapshot_dir is None:
            snap_root = tmp_root / 'snapshot'
            t0 = time.perf_counter()
            snap_info = _create_snapshot(data_dir, snap_root)
            timings['snapshot_s'] = time.perf_counter() - t0
            snapshot_created = True
            report['snapshot'] = {
                'dir': str(snap_root) if keep_snapshot else None,
                'duration_s': snap_info['elapsed_s'],
                'wal_zero': snap_info['result'].get('wal_zero'),
                'row_total': snap_info['result'].get('row_total'),
                'total_bytes': snap_info['result'].get('total_bytes'),
                'mode': snap_info['result'].get('mode'),
            }
            if not keep_snapshot:
                report['snapshot']['dir'] = None
        else:
            snap_root = Path(snapshot_dir)
            if not _snapshot_data_dir(snap_root).is_dir():
                raise ShardingError(f'快照目录无效: {snapshot_dir}')
            timings['snapshot_s'] = 0.0
            report['snapshot'] = {'dir': str(snap_root), 'reused': True}
        snap_data = _snapshot_data_dir(snap_root)
        try:
            snap_meta = _table_meta(snap_data, plan['table'])
            if snap_meta['pk'] != resolved['pk']:
                raise ShardingError('快照主键与当前元数据不一致（快照损坏？）')
        except ShardingError:
            raise
        report['consistency']['snapshot_dir_used'] = (str(snap_root)
                                                       if keep_snapshot else None)
        # 每 worker 一份 data/ 拷贝（避共享文件）
        t0 = time.perf_counter()
        for i, shard in enumerate(shards):
            worker_dir = Path(tempfile.mkdtemp(prefix='lightdb_shard_w%d_' % i))
            shutil.copytree(snap_data, worker_dir / 'data')
            worker_dirs.append(worker_dir / 'data')
        timings['copy_s'] = time.perf_counter() - t0

        t0 = time.perf_counter()
        procs, tasks = [], []
        env = _worker_env()
        for i, (shard, wdir) in enumerate(zip(shards, worker_dirs)):
            shard_sql = (f"SELECT {resolved['select_sql']} FROM "
                         f"{resolved['table']} WHERE {resolved['pk']} BETWEEN "
                         f"{shard['lo']} AND {shard['hi']}")
            task = {'sql': shard_sql, 'data_dir': str(wdir),
                    'lo': shard['lo'], 'hi': shard['hi'],
                    '_inject': _worker_inject if _worker_inject else None}
            task_path = str(tmp_root / ('task_%d.pkl' % i))
            result_path = str(tmp_root / ('result_%d.pkl' % i))
            with open(task_path, 'wb') as f:
                pickle.dump(task, f)
            proc = subprocess.Popen(
                [sys.executable, '-c', _BOOTSTRAP, task_path, result_path],
                cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            procs.append({'proc': proc, 'task_path': task_path,
                          'result_path': result_path, 'shard': shard, 'idx': i})
        timings['spawn_s'] = time.perf_counter() - t0

        t0 = time.perf_counter()
        results = _wait_workers(procs, timeout_s, tmp_root)
        timings['exec_s'] = time.perf_counter() - t0
        rows = []
        columns = None
        worker_query_s, worker_open_s = [], []
        for item in results:
            shard = item['shard']
            payload = item['payload']
            report['worker_pids'].append(payload.get('pid'))
            shard['rows'] = len(payload.get('rows') or [])
            shard['elapsed_s'] = round(float(payload.get('elapsed_s') or 0), 4)
            shard['query_s'] = round(float(payload.get('query_s') or 0), 4)
            worker_query_s.append(float(payload.get('query_s') or 0))
            worker_open_s.append(float(payload.get('open_s') or 0))
            if columns is None and payload.get('columns'):
                columns = payload['columns']
            if columns is not None and payload.get('columns') not in (
                    None, columns):
                raise ShardWorkerError(
                    '各分片返回的列不一致（快照/查询不一致）',
                    worker=item['idx'])
            rows.extend(payload.get('rows') or [])
        report['worker_open_s_total'] = round(sum(worker_open_s), 4)
        report['worker_query_s_sum'] = round(sum(worker_query_s), 4)
        report['worker_query_s_max'] = round(max(worker_query_s or [0.0]), 4)
        t0 = time.perf_counter()
        if columns:
            pk_index = resolved['pk_index']
            if pk_index >= len(columns):
                raise ShardingError('主键列未出现在结果列中，无法归并')
            rows.sort(key=lambda r: r[pk_index])
        timings['merge_s'] = time.perf_counter() - t0
        report['columns'] = columns or resolved['result_columns']
        report['rows'] = rows
        report['row_count'] = len(rows)
        timings['total_s'] = time.perf_counter() - t_start
        if snapshot_created:
            report['snapshot']['total_s'] = timings['snapshot_s']
        return report
    finally:
        for item in locals().get('procs', []) or []:
            proc = item['proc']
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:                    # pragma: no cover
                    pass
        if not keep_snapshot:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _read_pickle_retry(path) -> dict:
    """读 worker 结果文件；Windows/杀软偶发瞬时 PermissionError 时重试。"""
    last = None
    for _ in range(100):
        try:
            with open(path, 'rb') as f:
                return pickle.load(f)
        except (PermissionError, OSError, EOFError) as exc:
            last = exc
            time.sleep(0.01)
    raise last


def _wait_workers(procs: list, timeout_s: float, tmp_root: Path) -> list:
    """等待 worker 完成：崩溃/超时都显式报错（不静默返回部分结果）。"""
    deadline = time.perf_counter() + max(0.1, float(timeout_s))
    pending = list(procs)
    done = []
    while pending:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            for item in pending:
                if item['proc'].poll() is None:
                    item['proc'].kill()
            names = [f"shard{item['idx']}[{item['shard']['lo']}.."
                     f"{item['shard']['hi']}]" for item in pending]
            raise ShardWorkerError(
                f'worker 超时（{timeout_s}s 内未完成）: {names}', timeout_s=timeout_s)
        progressed = False
        for item in list(pending):
            rp = Path(item['result_path'])
            if rp.is_file():
                try:
                    payload = _read_pickle_retry(rp)
                except Exception as exc:             # noqa: BLE001
                    raise ShardWorkerError(
                        f"worker 结果文件损坏（shard {item['idx']}）: {exc}",
                        worker=item['idx']) from exc
                item['payload'] = payload
                pending.remove(item)
                done.append(item)
                progressed = True
                continue
            code = item['proc'].poll()
            if code is not None:
                raise ShardWorkerError(
                    f"worker 进程异常退出（shard {item['idx']}, exitcode={code}, "
                    f"range={item['shard']['lo']}..{item['shard']['hi']}）",
                    worker=item['idx'], exitcode=code)
        if not progressed:
            time.sleep(0.01)
    for item in done:
        payload = item['payload']
        if not payload.get('ok'):
            raise ShardWorkerError(
                f"worker 执行失败（shard {item['idx']}）: "
                f"{payload.get('error')}",
                worker=item['idx'], trace=payload.get('trace'))
    # 收尾 join，确保没有僵尸进程
    for item in done:
        try:
            item['proc'].wait(timeout=10)
        except Exception:                            # pragma: no cover
            item['proc'].kill()
    return done


# ==================== 对比基线：单进程 / 进程内多线程 ====================

def execute_single(sql: str, data_dir) -> dict:
    """单进程基线：打开数据目录（已 checkpoint/无写入）执行原查询。"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    t0 = time.perf_counter()
    db = Database(str(data_dir))
    try:
        ex = Executor(db)
        t1 = time.perf_counter()
        res = ex.execute_sql(sql, {})
        elapsed = time.perf_counter() - t1
        if not isinstance(res, dict) or res.get('type') == 'error':
            raise ShardingError(f'基线查询失败: {res}')
        return {'columns': res.get('columns') or [],
                'rows': res.get('rows') or [],
                'row_count': len(res.get('rows') or []),
                'query_s': elapsed,
                'total_s': time.perf_counter() - t0,
                'mode': 'single-process'}
    finally:
        try:
            db.shutdown()
        except Exception:                            # pragma: no cover
            pass


def execute_threads(sql: str, data_dir, workers: int = 4) -> dict:
    """对照：同一分片方案用进程内线程池执行（共享一个 Database）。

    预期因 GIL + 表级 RLock 接近不扩展；用于与多进程结果对比。
    """
    from concurrent.futures import ThreadPoolExecutor
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    plan = parse_range_query(sql)
    meta = _table_meta(Path(data_dir), plan['table'])
    resolved = _resolve_plan(plan, meta)
    shards = _split_range(plan['lo'], plan['hi'], workers)
    t0 = time.perf_counter()
    db = Database(str(data_dir))
    try:
        def run_shard(shard):
            ex = Executor(db)
            shard_sql = (f"SELECT {resolved['select_sql']} FROM "
                         f"{resolved['table']} WHERE {resolved['pk']} BETWEEN "
                         f"{shard['lo']} AND {shard['hi']}")
            t1 = time.perf_counter()
            res = ex.execute_sql(shard_sql, {})
            return {'columns': res.get('columns') or [],
                    'rows': res.get('rows') or [],
                    'elapsed_s': time.perf_counter() - t1}
        with ThreadPoolExecutor(max_workers=max(1, len(shards))) as pool:
            parts = list(pool.map(run_shard, shards))
        columns = parts[0]['columns'] if parts else resolved['result_columns']
        rows = [r for p in parts for r in p['rows']]
        rows.sort(key=lambda r: r[resolved['pk_index']])
        return {'columns': columns, 'rows': rows,
                'row_count': len(rows), 'shards': shards,
                'total_s': time.perf_counter() - t0,
                'mode': 'in-process-threads',
                'worker_elapsed_s': [p['elapsed_s'] for p in parts]}
    finally:
        try:
            db.shutdown()
        except Exception:                            # pragma: no cover
            pass


# ==================== 服务端分片引擎（task-85） ====================
#
# 与上面的"一次性原型"区别：
# * **快照复用**：同一份在线快照在数据未变更时被后续查询复用（不再是每个
#   查询都生成一份；这是原型的主要瓶颈）；数据变更通过文件指纹检测后刷新；
# * **常驻只读 worker 池**：每个 worker 进程启动一次、打开自己的快照副本
#   一次，之后通过任务/结果文件复用（避免每次查询 spawn + copy + open）；
# * **MySQL 协议路由**：`MySQLServer` 对符合条件的 SELECT 自动走这里，
#   结果字典与执行器结果集同形（`type=resultset` + columns/rows），复用
#   现有结果集编码路径；
# * **指标**：requests/sharded/fallbacks/errors/timeouts/latency/snapshot_age/
#   workers 等，经 `/metrics` 暴露；
# * 快照使用快照模块的**进程内写锁 quiesce**（`online=True, live_db=...`）：
#   刷快照期间阻塞写入、checkpoint 后拷贝文件，保证快照一致；只读负载下
#   快照被长期复用。
#
# 语义边界：分片结果 = **查询开始时最近一次快照**的已提交数据（快照时刻
# 一致性）；有显式事务/写语句/多库/不支持形态一律回退普通执行（不静默错答）；
# worker 崩溃/超时显式 1105。

import hashlib
import threading
import uuid

#: worker 池进程入口
_POOL_BOOTSTRAP = (
    'import sys; '
    'from lightdb.net.sharding import _pool_worker_main; '
    '_pool_worker_main(sys.argv[1])'
)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    try:
        val = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        val = int(default)
    return max(minimum, val)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    try:
        val = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        val = float(default)
    return max(minimum, val)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'off', 'no', '')


def sharding_enabled_by_env() -> bool:
    """`LIGHTDB_SHARDING_ENABLED`（默认关闭；服务端显式开启才路由）"""
    return _env_flag('LIGHTDB_SHARDING_ENABLED', False)


def _data_fingerprint(data_dir) -> str:
    """数据目录文件指纹（表文件 + _meta.json 的 size/mtime；用于检测变更）

    只统计**表数据文件**（`<table>.dat/.wal/.btroot/.free/.autoinc/...`）与
    `_meta.json`；audit.log / slow.log / binlog / replica 状态等非查询数据
    文件不计入（它们变化不影响 SELECT 结果，不应触发快照刷新）。
    目录里没有 `_meta.json` 时返回空指纹（由后续层按普通错误处理）。
    """
    data_dir = Path(data_dir)
    meta_path = data_dir / '_meta.json'
    h = hashlib.sha256()
    try:
        st = meta_path.stat()
        h.update(f'_meta.json|{st.st_size}|{st.st_mtime_ns};'.encode())
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        tables = sorted((meta.get('tables') or {}).keys())
    except Exception as exc:                         # noqa: BLE001
        raise ShardingError(f'读取数据目录元数据失败: {exc}') from exc
    for table in tables:
        prefix = f'{table}.'
        try:
            children = sorted(data_dir.iterdir())
        except OSError as exc:                       # pragma: no cover - 防御
            raise ShardingError(f'读取数据目录失败: {exc}') from exc
        for p in children:
            if not p.is_file() or not p.name.startswith(prefix):
                continue
            try:
                s = p.stat()
            except OSError:                           # pragma: no cover
                continue
            h.update(f'{p.name}|{s.st_size}|{s.st_mtime_ns};'.encode())
    return h.hexdigest()


def _snapshot_valid(snapshot_root) -> bool:
    """快照根目录是否是可用的表数据副本（含 data/_meta.json）"""
    root = Path(snapshot_root)
    try:
        meta = root / 'data' / '_meta.json'
        if not meta.is_file():
            return False
        raw = json.loads(meta.read_text(encoding='utf-8'))
        return bool(raw.get('tables'))
    except Exception:                                # pragma: no cover - 防御
        return False


def _pool_worker_main(worker_dir: str) -> None:
    """常驻只读 worker：打开自己的快照副本一次，循环处理任务文件

    协议（避免 Windows 命名管道限制）：
    * master 把任务 pickle 原子写入 `<worker_dir>/in/<name>.tmp` → 改名；
    * worker 轮询 `in/*.pkl`，执行 `SELECT ... BETWEEN`，把结果 pickle
      原子写入 `<worker_dir>/out/<name>.pkl`，然后删除任务文件；
    * master 放 `<worker_dir>/stop` 即退出。
    """
    wdir = Path(worker_dir)
    in_dir = wdir / 'in'
    out_dir = wdir / 'out'
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = None
    try:
        # 测试注入（仅测试用；不影响正常路径）：
        # * CRASH=1：worker 启动即崩溃 → 主进程必须显式报 1105；
        # * SLEEP=n：每个分片任务前 sleep n 秒 → 用于触发超时路径。
        if os.environ.get('LIGHTDB_SHARDING_TEST_WORKER_CRASH') == '1':
            os._exit(7)
        _test_sleep = float(
            os.environ.get('LIGHTDB_SHARDING_TEST_WORKER_SLEEP') or 0.0)
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor
        db = Database(str(wdir / 'data'))
        ex = Executor(db)
        while True:
            if (wdir / 'stop').is_file():
                break
            names = sorted(in_dir.glob('*.pkl'))
            for task_path in names:
                payload = {}
                try:
                    # Windows/杀软偶发瞬时 PermissionError：与 result 读取
                    # 一样做短重试（不是吞错误；重试仍失败会进 error payload）
                    task = _read_pickle_retry(task_path)
                    if _test_sleep > 0:
                        time.sleep(_test_sleep)
                    t0 = time.perf_counter()
                    res = ex.execute_sql(task['sql'], {})
                    elapsed = time.perf_counter() - t0
                    if not isinstance(res, dict) or res.get('type') == 'error':
                        raise RuntimeError(f'分片查询失败: {res}')
                    payload = {'ok': True,
                               'columns': list(res.get('columns') or []),
                               'rows': [tuple(r) for r in
                                        (res.get('rows') or [])],
                               'elapsed_s': elapsed, 'pid': os.getpid(),
                               'lo': task.get('lo'), 'hi': task.get('hi')}
                except BaseException as exc:         # noqa: BLE001
                    payload = {
                        'ok': False,
                        'error': f'{type(exc).__name__}: {exc}',
                        'trace': __import__('traceback')
                        .format_exc()[-4000:],
                        'pid': os.getpid(),
                    }
                out_path = out_dir / task_path.name
                tmp = str(out_path) + '.tmp'
                try:
                    with open(tmp, 'wb') as f:
                        pickle.dump(payload, f)
                    os.replace(tmp, out_path)
                except Exception:                    # pragma: no cover - 磁盘
                    pass
                try:
                    task_path.unlink()
                except OSError:                      # pragma: no cover
                    pass
            time.sleep(0.005)
    finally:
        if db is not None:
            try:
                db.shutdown()
            except Exception:                        # pragma: no cover
                pass
        raise SystemExit(0)


class _ShardPoolWorker:
    """常驻 worker 进程句柄（任务/结果文件 IPC）"""

    def __init__(self, engine, idx: int, generation: int):
        self.engine = engine
        self.idx = int(idx)
        self.generation = int(generation)
        self.root = Path(engine.pool_root) / f'gen{generation}' / f'w{idx}'
        self.root.mkdir(parents=True, exist_ok=True)
        data_src = Path(engine.snapshot_root) / 'data'
        data_dst = self.root / 'data'
        engine.metrics['workers_created_total'] += 1
        shutil.copytree(data_src, data_dst)
        self.proc = subprocess.Popen(
            [sys.executable, '-c', _POOL_BOOTSTRAP, str(self.root)],
            cwd=REPO_ROOT, env=_worker_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def submit(self, name: str, sql: str, lo: int, hi: int):
        task = {'sql': sql, 'lo': int(lo), 'hi': int(hi)}
        in_dir = self.root / 'in'
        in_dir.mkdir(parents=True, exist_ok=True)
        tmp = str(in_dir / (name + '.tmp'))
        with open(tmp, 'wb') as f:
            pickle.dump(task, f)
        os.replace(tmp, in_dir / name)

    def wait(self, name: str, deadline: float) -> dict:
        out_path = self.root / 'out' / name
        while True:
            if out_path.is_file():
                try:
                    return _read_pickle_retry(out_path)
                except Exception as exc:             # noqa: BLE001
                    raise ShardWorkerError(
                        f'worker 结果文件损坏（worker {self.idx}）: {exc}',
                        worker=self.idx) from exc
            code = self.proc.poll()
            if code is not None:
                raise ShardWorkerError(
                    f'worker 进程异常退出（worker {self.idx}, '
                    f'exitcode={code}）', worker=self.idx, exitcode=code)
            if time.perf_counter() > deadline:
                self.kill()
                raise ShardWorkerError(
                    f'worker 超时（worker {self.idx}，'
                    f'{self.engine.timeout_s}s 内未返回分片结果）',
                    worker=self.idx)
            time.sleep(0.005)

    def stop(self):
        try:
            (self.root / 'stop').write_text('1', encoding='utf-8')
        except OSError:                              # pragma: no cover
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:                            # pragma: no cover
            self.kill()

    def kill(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception:                            # pragma: no cover
            pass


class ShardedQueryEngine:
    """服务端只读分片引擎：快照复用 + 常驻 worker 池 + 协议结果集

    由 `MySQLServer` 在 `LIGHTDB_SHARDING_ENABLED=1` 时创建；`route()` 返回
    `None` 表示"不路由/回退普通执行"，返回 `{'type':'resultset', ...}` 表示
    分片成功；worker 崩溃/超时抛 `ShardWorkerError`（code=1105，显式）。
    """

    FALLBACK_REASONS = ('unsupported', 'below_threshold', 'no_table',
                        'session_transaction', 'session_database',
                        'permission', 'snapshot_error', 'shard_error')

    def __init__(self, live_db=None, data_dir=None, workers: int = None,
                 min_rows: int = None, timeout_s: float = None,
                 snapshot_ttl: float = None, pool_root=None,
                 epoch_provider=None):
        self.live_db = live_db
        self.epoch_provider = epoch_provider
        base = data_dir if data_dir is not None else (
            getattr(live_db, 'data_dir', None) if live_db is not None else None)
        if base is None:
            raise ShardingError('ShardedQueryEngine 需要 data_dir 或 live_db')
        self.data_dir = Path(base).resolve()
        self.workers = max(1, int(workers if workers is not None else
                                  _env_int('LIGHTDB_SHARDING_WORKERS', 2, 1)))
        self.min_rows = max(0, int(min_rows if min_rows is not None else
                                   _env_int('LIGHTDB_SHARDING_MIN_ROWS',
                                            20000, 0)))
        self.timeout_s = max(0.5, float(
            timeout_s if timeout_s is not None else
            _env_float('LIGHTDB_SHARDING_TIMEOUT', 120.0, 0.5)))
        self.snapshot_ttl = max(0.0, float(
            snapshot_ttl if snapshot_ttl is not None else
            _env_float('LIGHTDB_SHARDING_SNAPSHOT_TTL', 0.0, 0.0)))
        root = pool_root or os.environ.get('LIGHTDB_SHARDING_POOL_ROOT')
        if root:
            self.pool_root = Path(root)
        else:
            TMP_BASE.mkdir(parents=True, exist_ok=True)
            self.pool_root = TMP_BASE / (
                f'server_{os.getpid()}_{uuid.uuid4().hex[:8]}')
        self.pool_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self.snapshot_root = None
        self._snapshot_fp = None
        self._snapshot_epoch = 0
        self._snapshot_created_at = 0.0
        self.generation = 0
        self._workers = []
        self._qid = 0
        self.metrics = {
            'requests_total': 0, 'sharded_total': 0,
            'fallbacks_total': 0, 'workers_created_total': 0,
            'worker_errors_total': 0, 'timeouts_total': 0,
            'snapshot_created_total': 0, 'snapshot_reused_total': 0,
            'snapshot_failures_total': 0, 'rows_sharded_total': 0,
            'query_latency_ms_last': 0.0, 'query_latency_ms_total': 0.0,
            'fallbacks_by_reason': {r: 0 for r in self.FALLBACK_REASONS},
            'last_fallback_reason': None,
        }

    # ---------- 指标 ----------
    def metrics_snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.metrics)
            snap['fallbacks_by_reason'] = dict(
                self.metrics['fallbacks_by_reason'])
            age = (time.time() - self._snapshot_created_at
                   if self._snapshot_created_at else 0.0)
            snap.update({
                'enabled': True,
                'workers': len(self._workers),
                'workers_configured': self.workers,
                'min_rows': self.min_rows,
                'snapshot_age_seconds': round(age, 3),
                'snapshot_generation': self.generation,
                'snapshot_ready': self.snapshot_root is not None,
                'rows_sharded_total': self.metrics['rows_sharded_total'],
            })
            total = self.metrics['sharded_total']
            lat_total = self.metrics['query_latency_ms_total']
            snap['query_latency_ms_avg'] = (
                round(lat_total / total, 3) if total else 0.0)
            return snap

    def record_fallback(self, reason: str):
        with self._lock:
            self.metrics['fallbacks_total'] += 1
            self.metrics['last_fallback_reason'] = reason
            if reason not in self.metrics['fallbacks_by_reason']:
                self.metrics['fallbacks_by_reason'][reason] = 0
            self.metrics['fallbacks_by_reason'][reason] += 1

    # ---------- 路由 ----------
    def route(self, sql: str, session=None) -> dict:
        """尝试分片执行；返回 resultset dict 或 None（回退普通执行）

        * 不支持的 SELECT 形态/低于阈值/无表/事务中/多库 → None（安全回退，
          有明确指标原因；不静默错答）；
        * 快照生成失败 → None（回退普通执行，保证正确性；指标记录）；
        * worker 崩溃/超时 → 抛 `ShardWorkerError`（1105，显式，不静默回退）。
        """
        if self._closed:
            return None
        text = str(sql or '').strip().lstrip('(').strip()
        if not text.upper().startswith('SELECT'):
            return None
        with self._lock:
            self.metrics['requests_total'] += 1
        if isinstance(session, dict) and session.get('txn'):
            self.record_fallback('session_transaction')
            return None
        try:
            plan = parse_range_query(text)
        except UnsupportedShardQuery:
            self.record_fallback('unsupported')
            return None
        try:
            meta = _table_meta(self.data_dir, plan['table'])
            resolved = _resolve_plan(plan, meta)
        except UnsupportedShardQuery:
            self.record_fallback('unsupported')
            return None
        except ShardingError:
            self.record_fallback('no_table')
            return None
        span = plan['hi'] - plan['lo'] + 1
        if span < self.min_rows:
            self.record_fallback('below_threshold')
            return None
        try:
            return self._execute(plan, resolved, span)
        except ShardWorkerError:
            raise
        except ShardingError as exc:
            logger.warn(f'分片执行失败，回退普通执行: {exc}')
            self.record_fallback('shard_error')
            return None

    # ---------- 执行 ----------
    def _execute(self, plan: dict, resolved: dict, span: int) -> dict:
        with self._lock:
            t0 = time.perf_counter()
            self._ensure_snapshot()
            self._ensure_workers()
            n = min(self.workers, span)
            shards = _split_range(plan['lo'], plan['hi'], n)
            self._qid += 1
            qid = f'q{self._qid}'
            columns = None
            rows = []
            workers = list(self._workers)
            try:
                for i, shard in enumerate(shards):
                    shard_sql = (
                        f"SELECT {resolved['select_sql']} FROM "
                        f"{resolved['table']} WHERE {resolved['pk']} BETWEEN "
                        f"{shard['lo']} AND {shard['hi']}")
                    w = workers[i % len(workers)]
                    w.submit(f'{qid}_s{i}.pkl', shard_sql,
                             shard['lo'], shard['hi'])
                deadline = time.perf_counter() + self.timeout_s
                for i, shard in enumerate(shards):
                    w = workers[i % len(workers)]
                    try:
                        payload = w.wait(f'{qid}_s{i}.pkl', deadline)
                    except ShardWorkerError as exc:
                        self.metrics['worker_errors_total'] += 1
                        if ('超时' in str(exc)
                                or 'timeout' in str(exc).lower()):
                            self.metrics['timeouts_total'] += 1
                        raise
                    if not payload.get('ok'):
                        self.metrics['worker_errors_total'] += 1
                        raise ShardWorkerError(
                            f"worker 执行失败（worker {w.idx}）: "
                            f"{payload.get('error')}",
                            worker=w.idx, trace=payload.get('trace'))
                    cols = payload.get('columns') or []
                    if columns is None:
                        columns = cols
                    elif cols != columns:
                        raise ShardWorkerError(
                            '各分片返回的列不一致（快照/查询不一致）',
                            worker=w.idx)
                    rows.extend(tuple(r) for r in (payload.get('rows') or []))
                pk_index = resolved['pk_index']
                rows.sort(key=lambda r: r[pk_index])
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self.metrics['sharded_total'] += 1
                self.metrics['rows_sharded_total'] += len(rows)
                self.metrics['query_latency_ms_last'] = round(elapsed_ms, 3)
                self.metrics['query_latency_ms_total'] += elapsed_ms
                return {
                    'type': 'resultset',
                    'columns': columns or list(resolved['result_columns']),
                    'rows': rows,
                    'sharding': {
                        'workers': len(workers), 'shards': len(shards),
                        'latency_ms': round(elapsed_ms, 3),
                        'snapshot_generation': self.generation,
                    },
                }
            except ShardWorkerError:
                raise
            except ShardingError:
                raise
            except Exception as exc:                 # noqa: BLE001
                self.metrics['worker_errors_total'] += 1
                raise ShardWorkerError(f'分片执行异常: {exc}') from exc

    # ---------- 快照复用 ----------
    def _current_epoch(self) -> int:
        """服务端写入纪元（服务端每条非 SELECT 语句 +1；独立使用时恒 0）"""
        if self.epoch_provider is None:
            return 0
        try:
            return int(self.epoch_provider())
        except Exception:                        # pragma: no cover - 防御
            return 0

    def _ensure_snapshot(self):
        """数据未变（指纹 + 写入纪元）且 TTL 未到 → 复用；否则重建快照

        变更检测双保险：
        * `_data_fingerprint`：表数据文件大小/mtime + _meta.json（捕获任何
          带外写入）；
        * 服务端写入纪元：每条非 SELECT 语句 +1（快照拷贝窗口外的写入也
          能被立刻识别，避免"文件指纹因快照自身 checkpoint 变化"的干扰）。
        快照拷贝在表写锁 quiesce 窗口内进行（checkpoint 后复制原始表文件），
        不依赖快照模块的 manifest/采集流程，拷贝开销仅毫秒级。
        """
        try:
            fp = _data_fingerprint(self.data_dir)
        except ShardingError:
            raise
        epoch = self._current_epoch()
        if self.snapshot_root is not None:
            if fp == self._snapshot_fp and epoch == self._snapshot_epoch:
                self.metrics['snapshot_reused_total'] += 1
                return
            if (self.snapshot_ttl > 0 and self._snapshot_created_at
                    and time.time() - self._snapshot_created_at
                    < self.snapshot_ttl):
                # TTL 内允许有界陈旧（显式配置；默认 0 = 变更即刷新）
                self.metrics['snapshot_reused_total'] += 1
                return
        self._refresh_snapshot()

    def _refresh_snapshot(self):
        """建立一致的只读快照副本（quiesce 写锁 + checkpoint + 原始文件拷贝）

        * `live_db` 存在：用快照模块的 `_TableLocks`（同一把表写锁）建立
          quiesce 窗口 → `live_db.checkpoint()` → 校验 WAL=0 → 复制表数据
          文件；窗口内写入被阻塞，保证副本一致；
        * 独立使用（无 live_db）：直接复制数据目录（调用方需保证无写入），
          用于测试/静态数据目录。
        * 拷贝窗口外若有写入（epoch/指纹变化），最多重试 3 次，持续写入
          则显式失败 → 上层安全回退普通执行。
        """
        self.generation += 1
        for _attempt in range(3):
            epoch_before = self._current_epoch()
            snap_root = (Path(self.pool_root) / f'gen{self.generation}' /
                         'snapshot')
            data_out = snap_root / 'data'
            try:
                if self.live_db is not None:
                    self._copy_live_data_consistent(data_out)
                else:
                    shutil.copytree(self.data_dir, data_out)
            except Exception as exc:             # noqa: BLE001
                self.metrics['snapshot_failures_total'] += 1
                raise ShardingError(f'快照生成失败: {exc}') from exc
            epoch_after = self._current_epoch()
            if epoch_after == epoch_before:
                break
            # 拷贝后立刻有新写入：该副本可能不含这些写入，重试一次
            shutil.rmtree(snap_root, ignore_errors=True)
            self.metrics['snapshot_failures_total'] += 1
        else:
            raise ShardingError(
                '主库持续写入，无法建立一致快照（请降低写入频率或稍后重试）')
        if not _snapshot_valid(snap_root):
            self.metrics['snapshot_failures_total'] += 1
            raise ShardingError(f'快照目录无效: {snap_root}')
        # 新快照 → 旧的常驻 worker 必须重建（它们打开的是旧副本）
        self._stop_workers()
        self.snapshot_root = snap_root
        self._snapshot_epoch = epoch_before
        try:
            self._snapshot_fp = _data_fingerprint(self.data_dir)
        except ShardingError:
            self._snapshot_fp = None
        self._snapshot_created_at = time.time()
        self.metrics['snapshot_created_total'] += 1
        self.metrics['snapshot_age_seconds'] = 0.0

    def _copy_live_data_consistent(self, data_out: Path):
        """quiesce 窗口内复制 live_db 的原始表数据文件（不生成 manifest）"""
        from lightdb.backup import snapshot as snap_mod
        if not self.live_db.tables:
            raise ShardingError('数据目录里没有任何表，无法建立分片快照')
        targets = sorted(self.live_db.tables.keys())
        locks = snap_mod._TableLocks(  # 与在线热备同一把表写锁
            self.live_db, targets, min(10.0, self.timeout_s))
        with locks:
            checked = self.live_db.checkpoint(force=False)
            if int(checked) < len(targets):
                raise ShardingError(
                    f'checkpoint 仅完成 {checked}/{len(targets)} 张表'
                    f'（存在未提交事务？），拒绝建立分片快照')
            wal_non_zero = {}
            data_dir = Path(self.live_db.data_dir)
            for t in targets:
                p = data_dir / f'{t}.wal'
                if p.is_file() and p.stat().st_size:
                    wal_non_zero[t] = p.stat().st_size
            if wal_non_zero:
                raise ShardingError(
                    f'checkpoint 后 WAL 非零，拒绝建立分片快照: {wal_non_zero}')
            names = snap_mod._selected_files(data_dir, targets)
            data_out.mkdir(parents=True, exist_ok=True)
            for name in names:
                src = data_dir / name
                if src.is_file():
                    shutil.copy2(src, data_out / name)


    def _ensure_workers(self):
        gen_ok = all(w.generation == self.generation and w.alive
                     for w in self._workers)
        if self._workers and gen_ok:
            return
        self._stop_workers()
        if self.snapshot_root is None:               # pragma: no cover - 防御
            raise ShardingError('没有可用快照，无法启动 worker')
        self._workers = [_ShardPoolWorker(self, i, self.generation)
                         for i in range(self.workers)]

    def _stop_workers(self):
        old = self._workers
        self._workers = []
        for w in old:
            try:
                w.stop()
            except Exception:                        # pragma: no cover
                pass

    def refresh(self):
        """显式刷新快照（外部可调用；例如运维确认数据已变）"""
        with self._lock:
            self._refresh_snapshot()
            self._ensure_workers()
            return {'generation': self.generation,
                    'snapshot': str(self.snapshot_root)}

    def close(self):
        """停止 worker、删除本引擎临时目录（幂等）"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_workers()
            try:
                shutil.rmtree(self.pool_root, ignore_errors=True)
            except Exception:                        # pragma: no cover
                pass
            self.snapshot_root = None


# ==================== CLI/人读 ====================

def format_report(report: dict) -> str:
    t = report.get('timing') or {}
    lines = [
        '多进程只读分片: %s' % report.get('sql'),
        '表=%s 主键=%s 范围=%s 分片=%d 行数=%d'
        % (report.get('table'), report.get('primary_key'),
           report.get('range'), len(report.get('shards') or []),
           report.get('row_count', 0)),
        '耗时: 解析 %.3fs · 快照 %.3fs · 复制 %.3fs · 启动 %.3fs · '
        '执行 %.3fs · 合并 %.3fs · 总计 %.3fs'
        % (t.get('parse_s', 0), t.get('snapshot_s', 0),
           t.get('copy_s', 0), t.get('spawn_s', 0),
           t.get('exec_s', 0), t.get('merge_s', 0), t.get('total_s', 0)),
        '一致性: %s' % (report.get('consistency') or {}).get('mode'),
    ]
    return '\n'.join(lines)


__all__ = [
    'ShardingError', 'UnsupportedShardQuery', 'ShardWorkerError',
    'parse_range_query', 'execute_sharded', 'execute_single',
    'execute_threads', 'format_report', 'MYSQL_UNKNOWN_ERROR',
    '_worker_main',
    # task-85：服务端路由/快照复用/worker 池
    'ShardedQueryEngine', 'sharding_enabled_by_env',
]
