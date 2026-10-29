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
]
