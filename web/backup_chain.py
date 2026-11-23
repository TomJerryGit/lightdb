# -*- coding: utf-8 -*-
"""task-90：Web 增量备份链 API（真实 scripts/backup_chain.py 子进程）。

- GET  /api/v1/backup-chain/list    列链/段（时间/大小/校验）
- POST /api/v1/backup-chain/init|incr|prune|restore  异步任务
- GET  /api/v1/backup-chain/status?job_id=  轮询进度/日志/结果
- restore 必须 confirm=true；绝不自动恢复；CLI 返回码/错误原样返回。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import require_permission, verify_token

router = APIRouter(prefix="/api/v1/backup-chain", tags=["backup-chain"])
ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / 'scripts' / 'backup_chain.py'
DEFAULT_ROOT = os.environ.get('LIGHTDB_BACKUP_CHAIN_DIR', '')
JOBS = {}
JOBS_LOCK = threading.Lock()


def _err(status, message, code, **extra):
    d = {'detail': str(message), 'code': code}
    d.update(extra)
    raise HTTPException(status, detail=d)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _fmt_size(size):
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def load_manifest(chain_dir):
    path = Path(chain_dir) / 'chain_manifest.json'
    if not path.exists():
        raise FileNotFoundError(
            f'不是备份链目录（缺少 chain_manifest.json）: {chain_dir}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('chain_manifest.json 根节点不是对象')
    if not isinstance(data.get('entries'), list) or not data['entries']:
        raise ValueError('备份链 manifest 无 entries')
    return data


def verify_chain(chain_dir):
    chain_dir = Path(chain_dir)
    try:
        m = load_manifest(chain_dir)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': str(exc), 'entries': []}
    problems, entries = [], []
    for e in m['entries']:
        row = dict(e)
        row['verified'] = False
        try:
            if e.get('kind') == 'full':
                full = chain_dir / str(e.get('dir') or '')
                if not (full / 'manifest.json').is_file():
                    problems.append(f"基线 #{e.get('id')} 缺少 snapshot manifest")
                else:
                    row['size'] = int(e.get('snapshot_bytes') or 0)
                    row['verified'] = True
            else:
                p = chain_dir / str(e.get('file') or '')
                if not p.is_file():
                    problems.append(f"增量 #{e.get('id')} 文件缺失")
                else:
                    row['size'] = p.stat().st_size
                    if e.get('size') is not None and int(e['size']) != row['size']:
                        problems.append(f"增量 #{e.get('id')} 大小不符")
                    elif str(e.get('sha256') or '') and \
                            _sha256_file(p) != e['sha256']:
                        problems.append(f"增量 #{e.get('id')} sha256 不符")
                    else:
                        row['verified'] = True
        except Exception as exc:                      # noqa: BLE001
            problems.append(f"段 #{e.get('id')} 校验失败: {exc}")
        entries.append(row)
    return {'ok': not problems, 'problems': problems, 'entries': entries,
            'manifest': m, 'chain_dir': str(chain_dir)}


def chain_rows(chain_dir):
    v = verify_chain(chain_dir)
    if not v.get('manifest'):
        return {'ok': False, 'error': v.get('error', '链解析失败'),
                'rows': [], 'path': str(chain_dir)}
    rows = []
    for e, ve in zip(v['manifest']['entries'], v['entries']):
        state = e.get('state') or {}
        rows.append({
            'id': e.get('id'), 'kind': e.get('kind'),
            'created_at': e.get('created_at') or '',
            'size': int(ve.get('size') or 0),
            'size_text': _fmt_size(ve.get('size')),
            'verified': bool(ve.get('verified')),
            'sha256': (e.get('sha256') or e.get('manifest_sha256') or '')[:12],
            'stats': e.get('stats') or {},
            'tables': len(state),
            'rows': sum(int(s.get('row_count') or 0) for s in state.values()),
        })
    return {'ok': v['ok'], 'problems': v.get('problems', []), 'rows': rows,
            'path': str(chain_dir),
            'source_data_dir': v['manifest'].get('source_data_dir', ''),
            'created_at': v['manifest'].get('created_at', '')}


def list_chains(root):
    root = Path(root or '.')
    if not root.is_dir():
        return {'ok': False, 'error': f'备份链根目录不存在: {root}', 'chains': []}
    chains = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if (child / 'chain_manifest.json').exists():
            chains.append({'name': child.name, **chain_rows(child)})
    return {'ok': True, 'chains': chains, 'root': str(root)}


def preview_restore(chain_dir, to: Optional[int] = None):
    v = verify_chain(chain_dir)
    if not v.get('ok'):
        return {'ok': False, 'error': '链完整性校验失败: ' +
                '; '.join(v.get('problems') or [])[:500], 'preview': {}}
    entries = v['manifest']['entries']
    max_incr = len(entries) - 1
    want = max_incr if to is None else int(to)
    if want < 0 or want > max_incr:
        return {'ok': False, 'error': f'目标段越界: {want}（有效 0..{max_incr}）',
                'preview': {}}
    selected = entries[:want + 1]
    tables = set()
    stats = {'inserts': 0, 'updates': 0, 'deletes': 0, 'ddl_count': 0}
    for e in selected[1:]:
        tables.update((e.get('state') or {}).keys())
        for k in stats:
            stats[k] += int((e.get('stats') or {}).get(k) or 0)
    last = selected[-1].get('state') or {}
    return {'ok': True, 'preview': {
        'target_segment': want, 'applied_increments': want,
        'entries': len(selected), 'tables_affected': len(tables),
        **stats, 'tables_total': len(last),
        'rows_total': sum(int(s.get('row_count') or 0)
                          for s in last.values())},
        'table_rows': [{'table': t,
                        'rows': int((last[t] or {}).get('row_count') or 0),
                        'verified': True} for t in sorted(last)]}


def _build_args(action, chain_dir='', data_dir='', target='', to=None,
                keep=None, force=False, json_path=''):
    args = [sys.executable, '-u', str(CLI), action]
    if action in ('init', 'incr'):
        args += ['--data-dir', str(data_dir), '--chain-dir', str(chain_dir)]
        if action == 'incr' and keep is not None:
            args += ['--keep', str(int(keep))]
    elif action == 'prune':
        args += ['--chain-dir', str(chain_dir), '--keep', str(int(keep))]
    elif action == 'restore':
        args += ['--chain-dir', str(chain_dir), '--target', str(target)]
        if to is not None:
            args += ['--to', str(int(to))]
        if force:
            args += ['--force']
    if json_path:
        args += ['--json', str(json_path)]
    return args


def run_chain_cli(action, chain_dir='', data_dir='', target='', to=None,
                  keep=None, force=False, timeout=600, log_cb=None):
    report_path = ''
    try:
        fd, report_path = tempfile.mkstemp(prefix='chain_web_', suffix='.json')
        os.close(fd)
    except Exception:
        report_path = ''
    args = _build_args(action, chain_dir, data_dir, target, to, keep, force,
                       report_path)
    out = []

    def emit(line):
        out.append(line.rstrip())
        if log_cb:
            log_cb(line.rstrip())

    try:
        proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace', bufsize=1)
        for line in proc.stdout:
            emit(line)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': '\n'.join(out), 'stderr': '',
                'error': f'备份链 CLI 超时（{timeout}s）', 'report': {}}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': '\n'.join(out), 'stderr': '', 'error': str(exc),
                'report': {}}
    report = {}
    if report_path and os.path.exists(report_path):
        try:
            report = json.loads(Path(report_path).read_text(encoding='utf-8'))
        except Exception as exc:                      # noqa: BLE001
            report = {'report_error': str(exc)}
        try:
            os.unlink(report_path)
        except OSError:
            pass
    error = ''
    if rc != 0:
        error = next((ln for ln in reversed(out) if ln.strip()), '') or \
            f'CLI 退出码 {rc}'
    return {'ok': rc == 0, 'returncode': rc, 'command': args,
            'stdout': '\n'.join(out), 'stderr': '', 'error': error,
            'report': report, 'action': action,
            'chain_dir': str(chain_dir), 'target': str(target)}


def _new_job(action):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {'job_id': jid, 'action': action, 'state': 'running',
                     'started_at': time.time(), 'finished_at': None,
                     'log': [], 'report': {}, 'ok': None,
                     'returncode': None, 'error': ''}
        # 保留最近 50 个任务，防内存增长
        if len(JOBS) > 50:
            for old in sorted(JOBS, key=lambda k: JOBS[k]['started_at'])[:10]:
                JOBS.pop(old, None)
    return jid


def _set_log(jid, line):
    with JOBS_LOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job['log'].append(str(line))
        if len(job['log']) > 2000:
            job['log'] = job['log'][-2000:]


def _finish_job(jid, result):
    with JOBS_LOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job.update({'state': 'done' if result.get('ok') else 'failed',
                    'finished_at': time.time(), 'ok': bool(result.get('ok')),
                    'returncode': result.get('returncode'),
                    'error': result.get('error') or '',
                    'report': result.get('report') or {},
                    'result': result})


def _start_job(action, **kwargs):
    jid = _new_job(action)

    def _run():
        try:
            result = run_chain_cli(
                action, log_cb=lambda line: _set_log(jid, line), **kwargs)
        except Exception as exc:                      # noqa: BLE001
            result = {'ok': False, 'error': str(exc), 'returncode': None,
                      'report': {}}
        _finish_job(jid, result)

    threading.Thread(target=_run, daemon=True).start()
    return jid


class InitReq(BaseModel):
    chain_dir: str
    data_dir: str


class IncrReq(BaseModel):
    chain_dir: str
    data_dir: str = ''
    keep: Optional[int] = None


class PruneReq(BaseModel):
    chain_dir: str
    keep: int


class RestoreReq(BaseModel):
    chain_dir: str
    target: str
    to: Optional[int] = None
    force: bool = False
    confirm: bool = False


@router.get('/list')
async def backup_list(root: str = '',
                      payload: dict = Depends(verify_token)):
    target = root or DEFAULT_ROOT
    if not target:
        _err(400, '未配置备份链根目录；请传 root 或设置 '
                  'LIGHTDB_BACKUP_CHAIN_DIR', 'backup_root_missing')
    return list_chains(target)


@router.get('/status')
async def backup_status(job_id: str = Query(...),
                        payload: dict = Depends(verify_token)):
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job:
        _err(404, f'任务不存在: {job_id}', 'job_not_found')
    return job


@router.get('/preview')
async def backup_preview(chain_dir: str, to: Optional[int] = None,
                         payload: dict = Depends(verify_token)):
    return preview_restore(chain_dir, to)


@router.post('/init')
async def backup_init(req: InitReq,
                      payload: dict = Depends(require_permission('admin'))):
    if not req.chain_dir or not req.data_dir:
        _err(400, 'chain_dir 与 data_dir 必填', 'backup_args_missing')
    return {'ok': True, 'job_id': _start_job(
        'init', chain_dir=req.chain_dir, data_dir=req.data_dir)}


@router.post('/incr')
async def backup_incr(req: IncrReq,
                      payload: dict = Depends(require_permission('admin'))):
    if not req.chain_dir:
        _err(400, 'chain_dir 必填', 'backup_args_missing')
    data_dir = req.data_dir
    if not data_dir:
        try:
            data_dir = load_manifest(req.chain_dir).get('source_data_dir', '')
        except Exception as exc:                      # noqa: BLE001
            _err(400, f'链不存在/不可读: {exc}', 'backup_chain_missing')
    return {'ok': True, 'job_id': _start_job(
        'incr', chain_dir=req.chain_dir, data_dir=data_dir, keep=req.keep)}


@router.post('/prune')
async def backup_prune(req: PruneReq,
                       payload: dict = Depends(require_permission('admin'))):
    if not req.chain_dir or int(req.keep) < 0:
        _err(400, 'chain_dir 必填且 keep 合法', 'backup_args_missing')
    return {'ok': True, 'job_id': _start_job(
        'prune', chain_dir=req.chain_dir, keep=int(req.keep))}


@router.post('/restore')
async def backup_restore(req: RestoreReq,
                         payload: dict = Depends(require_permission('admin'))):
    if not req.chain_dir or not req.target:
        _err(400, 'chain_dir 与 target 必填', 'backup_args_missing')
    if not req.confirm:
        _err(400, '恢复备份链必须显式 confirm=true；Web 不会自动恢复',
             'confirmation_required')
    return {'ok': True, 'job_id': _start_job(
        'restore', chain_dir=req.chain_dir, target=req.target,
        to=req.to, force=bool(req.force))}
