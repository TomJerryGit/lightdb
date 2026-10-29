# -*- coding: utf-8 -*-
"""task-83：生产化能力 Web API（复制状态 / Prometheus 摘要 / 二进制快照 CLI）。

真实数据源：
- 复制：当前连接 `SHOW SLAVE STATUS`，失败/无行时读复制状态文件；
  缺失字段显示 "—"，兼容 task-81 增量复制新增字段（未知字段不丢弃）。
- 指标：拉取配置的 Prometheus URL（http/https），解析 QPS/连接/错误/
  慢查询/WAL/checkpoint/pool；不可达显式 400，不伪造数据。
- 快照：调用现有 `scripts/snapshot_backup.py` / `snapshot_restore.py`；
  恢复必须 `confirm=true`，绝不自动执行；返回真实返回码/日志。
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import api
from .auth import require_permission, verify_token

router = APIRouter(prefix="/api/v1", tags=["production"])
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = os.environ.get('LIGHTDB_REPLICA_STATE', '')
DEFAULT_METRICS_URL = os.environ.get('LIGHTDB_METRICS_URL', '')


def _err(status, message, code, **extra):
    detail = {'detail': str(message), 'code': str(code)}
    detail.update(extra)
    return HTTPException(status, detail=detail)


# ==================== Prometheus 解析 ====================

_METRIC_ALIASES = {
    'qps': ('queries_per_second', 'qps'),
    'connections': ('connections_current', 'connections', 'threads_connected'),
    'errors': ('error', 'errors', 'aborted', 'failed'),
    'slow': ('slow',),
    'wal': ('wal',),
    'checkpoint': ('checkpoint',),
    'pool': ('buffer_pool', 'pool'),
}


def parse_prometheus(text: str) -> dict:
    """解析 Prometheus 文本；按类别取第一个匹配指标，返回可展示摘要。"""
    samples = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            left, value = line.rsplit(None, 1)
            value = float(value)
        except (ValueError, IndexError):
            continue
        name, labels = left, ''
        if '{' in left:
            name, rest = left.split('{', 1)
            labels = rest.rsplit('}', 1)[0]
        samples.append({'name': name.strip(), 'labels': labels,
                        'value': value, 'line': line})
    summary = {}
    for category, patterns in _METRIC_ALIASES.items():
        hit = None
        for s in samples:
            low = s['name'].lower()
            if any(p in low for p in patterns):
                hit = s
                break
        summary[category] = ({
            'name': hit['name'], 'labels': hit['labels'],
            'value': hit['value'],
        } if hit else {'name': None, 'labels': '', 'value': None,
                       'missing': '—'})
    summary['sample_count'] = len(samples)
    summary['raw'] = samples[:200]
    return summary


def fetch_metrics(url: str, timeout: float = 5.0) -> str:
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError(f'非法 metrics URL（仅 http/https）: {url!r}')
    req = urllib.request.Request(url, headers={'Accept': 'text/plain'})
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        raw = resp.read(2 * 1024 * 1024)
        return raw.decode('utf-8', errors='replace')


# ==================== 复制状态 ====================

_REPL_FIELDS = [
    ('Master_Host', 'master_host'), ('Master_Port', 'master_port'),
    ('Master_Log_File', 'master_file'), ('Read_Master_Log_Pos', 'read_pos'),
    ('Exec_Master_Log_Pos', 'exec_pos'), ('Relay_Master_Log_File', 'relay_file'),
    ('Seconds_Behind_Master', 'lag_seconds'),
    ('Slave_IO_Running', 'io_running'), ('Slave_SQL_Running', 'sql_running'),
    ('Last_Error', 'last_error'), ('Last_IO_Error', 'last_io_error'),
    ('Last_SQL_Error', 'last_sql_error'),
]


def _show_slave_status(conn):
    cols, rows = conn.query('SHOW SLAVE STATUS')
    if not rows:
        return None
    row = rows[0]
    idx = {str(c).lower(): i for i, c in enumerate(cols or [])}
    out = {}
    for db_name, key in _REPL_FIELDS:
        i = idx.get(db_name.lower())
        out[key] = row[i] if i is not None and i < len(row) else None
    return out


def _state_file_status(path: str):
    if not path:
        return None, '未配置 LIGHTDB_REPLICA_STATE / state_file'
    p = Path(path)
    if not p.exists():
        return None, f'复制状态文件不存在: {path}'
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception as exc:                          # noqa: BLE001
        return None, f'复制状态文件无法解析: {exc}'
    if not isinstance(raw, dict):
        return None, '复制状态文件格式非法（应为对象）'
    fields = {
        'master_host': raw.get('master_host'),
        'master_port': raw.get('master_port'),
        'master_file': raw.get('master_file') or raw.get('relay_file'),
        'read_pos': raw.get('master_position') or raw.get('read_pos'),
        'exec_pos': raw.get('last_seq') or raw.get('seq') or raw.get('lsn'),
        'lag_seconds': (round(float(raw.get('lag_ms')) / 1000.0, 3)
                        if raw.get('lag_ms') is not None else
                        raw.get('lag_seconds')),
        'io_running': raw.get('io_running'),
        'sql_running': raw.get('sql_running'),
        'last_error': raw.get('last_error') or raw.get('error'),
        'last_io_error': raw.get('last_io_error'),
        'last_sql_error': raw.get('last_sql_error'),
        'last_event_type': raw.get('last_event_type'),
        'applied': raw.get('applied'),
        'idempotent': raw.get('idempotent'),
        'errors': raw.get('errors'),
        'state_path': str(p),
    }
    return {'fields': fields, 'raw': raw,
            'unknown_keys': sorted(set(raw) - set(fields))}, ''


def replication_status(conn=None, state_file: str = '') -> dict:
    show, show_error = None, ''
    if conn is not None:
        try:
            show = _show_slave_status(conn)
        except Exception as exc:                      # noqa: BLE001
            show_error = str(exc)
    state, state_error = _state_file_status(
        state_file or DEFAULT_STATE_FILE)
    if show:
        fields = show
        source = 'SHOW SLAVE STATUS'
        extra = {}
    elif state:
        fields = state['fields']
        source = 'state_file:' + fields.get('state_path', '')
        extra = {'unknown_keys': state.get('unknown_keys', []),
                 'state_raw': state.get('raw')}
    else:
        return {'ok': True, 'configured': False,
                'source': None,
                'fields': {},
                'missing': [k for _n, k in _REPL_FIELDS],
                'last_error': '',
                'show_slave_error': show_error,
                'state_file_error': state_error,
                'notice': '未配置复制：SHOW SLAVE STATUS 无数据且无可用复制状态文件'}
    display = {}
    for _name, key in _REPL_FIELDS:
        value = fields.get(key)
        display[key] = '—' if value is None or value == '' else str(value)
    for key in ('last_event_type', 'applied', 'idempotent', 'errors'):
        if key in fields:
            display[key] = '—' if fields.get(key) is None else str(fields[key])
    return {'ok': True, 'configured': True, 'source': source,
            'fields': display, 'raw_fields': fields,
            'last_error': str(fields.get('last_error') or ''),
            'unknown_fields': sorted(extra.get('unknown_keys', [])),
            'show_slave_error': show_error,
            'state_file_error': state_error,
            **{k: v for k, v in extra.items() if k not in ('unknown_keys',)}}


@router.get('/replication/status')
async def get_replication_status(
        connection: Optional[str] = None,
        state_file: str = '',
        payload: dict = Depends(verify_token)):
    conn = api._get_conn(payload['sub'], connection)
    try:
        return replication_status(conn, state_file)
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'读取复制状态失败: {exc}', 'replication_status_failed')


@router.get('/metrics/summary')
async def get_metrics_summary(
        url: str = '',
        connection: Optional[str] = None,
        timeout: float = Query(5.0, ge=0.5, le=30),
        payload: dict = Depends(verify_token)):
    target = (url or DEFAULT_METRICS_URL).strip()
    if not target:
        # 未显式配置时允许用连接的 host/port 推导（引擎 metrics 端口通常
        # 独立；推导失败/不可达会显式报错，不伪造数据）
        try:
            conn = api._get_conn(payload['sub'], connection)
            cfg = getattr(conn, 'config', None)
            if cfg is not None and getattr(cfg, 'host', None):
                target = f'http://{cfg.host}:{int(cfg.port)}/metrics'
        except Exception:
            target = ''
    if not target:
        raise _err(400, '未配置 metrics URL；请传入 url 或设置 LIGHTDB_METRICS_URL',
                   'metrics_url_missing')
    try:
        text = fetch_metrics(target, timeout=timeout)
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'metrics 不可达/解析失败: {exc}',
                   'metrics_unreachable', url=target)
    return {'ok': True, 'url': target, 'summary': parse_prometheus(text)}


# ==================== 二进制快照 CLI ====================

class SnapshotRequest(BaseModel):
    data_dir: str = ''
    out: str = ''
    table: str = ''
    online: bool = False
    force: bool = False
    server_pid: Optional[int] = None
    timeout: float = 120.0


class RestoreRequest(BaseModel):
    snapshot: str = ''
    data_dir: str = ''
    force: bool = False
    confirm: bool = False
    timeout: float = 120.0


def _run_cli(script: str, args: list, timeout: float) -> dict:
    cmd = [sys.executable, str(ROOT / 'scripts' / script)] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=float(timeout), cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        return {'ok': False, 'returncode': None, 'command': cmd,
                'stdout': (exc.stdout or '') if isinstance(exc.stdout, str) else '',
                'stderr': f'超时（{timeout}s）',
                'error': f'快照 CLI 超时 {timeout}s'}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'returncode': None, 'command': cmd,
                'stdout': '', 'stderr': str(exc), 'error': str(exc)}
    return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
            'command': cmd, 'stdout': proc.stdout or '',
            'stderr': proc.stderr or '',
            'error': '' if proc.returncode == 0 else
                     (proc.stderr or proc.stdout or f'exit {proc.returncode}')}


@router.post('/snapshot/backup')
async def snapshot_backup(req: SnapshotRequest,
                          payload: dict = Depends(require_permission('admin'))):
    if not req.data_dir or not req.out:
        raise _err(400, 'data_dir 与 out 必填', 'snapshot_args_missing')
    args = ['--data-dir', req.data_dir, '--out', req.out]
    if req.table:
        args += ['--table', req.table]
    if req.online:
        args += ['--online']
    if req.force:
        args += ['--force']
    if req.server_pid:
        args += ['--server-pid', str(int(req.server_pid))]
    res = _run_cli('snapshot_backup.py', args, req.timeout)
    return {'ok': res['ok'], **res}


@router.post('/snapshot/restore')
async def snapshot_restore(req: RestoreRequest,
                           payload: dict = Depends(require_permission('admin'))):
    if not req.snapshot or not req.data_dir:
        raise _err(400, 'snapshot 与 data_dir 必填', 'snapshot_args_missing')
    # 恢复是高危操作：必须显式确认，绝不自动执行
    if not req.confirm:
        raise _err(400, '恢复快照必须显式 confirm=true；Web 不会自动恢复',
                   'confirmation_required')
    args = ['--snapshot', req.snapshot, '--data-dir', req.data_dir]
    if req.force:
        args += ['--force']
    res = _run_cli('snapshot_restore.py', args, req.timeout)
    return {'ok': res['ok'], **res}
