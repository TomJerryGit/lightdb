"""LightDB 备份自动化：定时全量备份 + 校验 + 保留策略 + 每周恢复演练。

用法（可被 Windows 计划任务调用）：
  python scripts/backup_automation.py run   --data-dir D:\\lightdb\\data --out D:\\backup --keep 14 --drill
  python scripts/backup_automation.py drill --out D:\\backup
  python scripts/backup_automation.py status --out D:\\backup

退出码：0=成功；1=失败；2=检测到运行中的服务（备份需静默，拒绝执行）。
输出：<out>/status.json（最近一次结果）、<out>/logs/auto-YYYYMMDD.log。
备份目录命名：full-YYYYmmdd-HHMMSS（含 manifest.json）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
SNAPSHOT_BACKUP = REPO / 'scripts' / 'snapshot_backup.py'
SNAPSHOT_RESTORE = REPO / 'scripts' / 'snapshot_restore.py'


def now_ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(out: Path, msg: str):
    line = f'[{now_ts()}] {msg}'
    print(line, flush=True)
    try:
        logs = Path(out) / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f'auto-{datetime.now():%Y%m%d}.log', 'a',
                  encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def list_backups(out: Path) -> list[Path]:
    out = Path(out)
    if not out.is_dir():
        return []
    items = [d for d in out.iterdir()
             if d.is_dir() and d.name.startswith('full-')
             and (d / 'manifest.json').is_file()]
    return sorted(items, key=lambda p: p.name)


def select_retention(backups: list[Path], keep: int):
    """返回 (保留列表, 删除列表)。按名称（时间戳）升序，保留最新 keep 个。"""
    backups = sorted(backups, key=lambda p: p.name)
    keep = max(1, int(keep))
    if len(backups) <= keep:
        return list(backups), []
    return backups[-keep:], backups[:-keep]


def _run(cmd: list[str], timeout=3600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding='utf-8', errors='replace')
    return r


def _looks_like_server_running(stdout: str, stderr: str) -> bool:
    text = (stdout or '') + (stderr or '')
    return ('运行中的服务' in text or '拒绝' in text and '服务' in text
            or 'server' in text.lower() and 'running' in text.lower())


def verify_backup(backup: Path, out: Path, log_fn) -> dict:
    """恢复到临时目录并依赖 snapshot_restore 的自检（行数/关键值/WAL=0）。"""
    tmp = Path(tempfile.mkdtemp(prefix='lightdb-verify-', dir=str(out)))
    try:
        r = _run([PYTHON, str(SNAPSHOT_RESTORE), '--snapshot', str(backup),
                  '--data-dir', str(tmp), '--no-server-check'], timeout=3600)
        ok = r.returncode == 0
        tail = (r.stdout or r.stderr or '').strip().splitlines()[-3:]
        log_fn(f"verify {'OK' if ok else 'FAIL'} rc={r.returncode}: "
               f"{' | '.join(tail)}")
        return {'ok': ok, 'rc': r.returncode, 'tail': tail}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_status(out: Path, payload: dict):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'status.json'
    payload = {'schema': 'lightdb-backup-status/1', 'at': now_ts(), **payload}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding='utf-8')
    return path


def cmd_run(args) -> int:
    out = Path(args.out).expanduser()
    data = Path(args.data_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if not data.is_dir():
        log(out, f'[error] 数据目录不存在: {data}')
        write_status(out, {'ok': False, 'error': f'data dir not found: {data}'})
        return 1
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = out / f'full-{stamp}'
    log(out, f'[backup] start data={data} dest={dest}')
    r = _run([PYTHON, str(SNAPSHOT_BACKUP), '--data-dir', str(data),
              '--out', str(dest)])
    if r.returncode != 0:
        msg = (r.stdout or '') + (r.stderr or '')
        if _looks_like_server_running(r.stdout, r.stderr):
            log(out, f'[refused] 检测到运行中的服务，备份需先停止写入: {msg[-400:]}')
            write_status(out, {'ok': False, 'refused': True,
                               'error': 'server is running; stop it first'})
            return 2
        log(out, f'[error] 备份失败 rc={r.returncode}: {msg[-800:]}')
        write_status(out, {'ok': False, 'error': msg[-2000:]})
        return 1
    log(out, f'[backup] ok: {(r.stdout or "").strip()[-300:]}')

    verify = {'ok': True, 'skipped': True}
    if args.drill:
        verify = verify_backup(dest, out, lambda m: log(out, m))
        if not verify['ok']:
            write_status(out, {'ok': False, 'backup': str(dest),
                               'verify': verify,
                               'error': 'restore drill failed'})
            return 1

    backups = list_backups(out)
    keep_list, delete_list = select_retention(backups, args.keep)
    deleted = []
    for d in delete_list:
        try:
            shutil.rmtree(d)
            deleted.append(d.name)
            log(out, f'[retention] deleted {d.name}')
        except OSError as e:
            log(out, f'[retention] 删除失败 {d.name}: {e}')
    status_path = write_status(out, {
        'ok': True, 'backup': str(dest), 'backup_name': dest.name,
        'verify': verify, 'kept': [p.name for p in keep_list],
        'deleted': deleted, 'keep': args.keep, 'data_dir': str(data),
    })
    log(out, f'[done] backup ok, status={status_path}')
    return 0


def cmd_drill(args) -> int:
    out = Path(args.out).expanduser()
    backups = list_backups(out)
    if not backups:
        log(out, f'[error] 没找到可演练的备份（{out}/full-*）')
        write_status(out, {'ok': False, 'error': 'no backup found'})
        return 1
    latest = backups[-1]
    log(out, f'[drill] 使用最新备份: {latest.name}')
    verify = verify_backup(latest, out, lambda m: log(out, m))
    status_path = write_status(out, {
        'ok': verify['ok'], 'drill': True, 'backup': str(latest),
        'verify': verify,
        'error': None if verify['ok'] else 'drill restore failed',
    })
    log(out, f"[drill] {'PASS' if verify['ok'] else 'FAIL'} status={status_path}")
    return 0 if verify['ok'] else 1


def cmd_status(args) -> int:
    out = Path(args.out).expanduser()
    backups = list_backups(out)
    status_file = out / 'status.json'
    payload = {'out': str(out), 'backups': [p.name for p in backups],
               'count': len(backups)}
    if status_file.is_file():
        try:
            payload['last_status'] = json.loads(
                status_file.read_text(encoding='utf-8'))
        except Exception as e:
            payload['last_status_error'] = str(e)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='LightDB 备份自动化')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_run = sub.add_parser('run', help='备份 + 可选校验 + 保留策略')
    p_run.add_argument('--data-dir', required=True)
    p_run.add_argument('--out', required=True)
    p_run.add_argument('--keep', type=int, default=14)
    p_run.add_argument('--drill', action='store_true', help='备份后立即恢复校验')
    p_run.set_defaults(func=cmd_run)
    p_drill = sub.add_parser('drill', help='对最新备份做恢复演练')
    p_drill.add_argument('--out', required=True)
    p_drill.set_defaults(func=cmd_drill)
    p_status = sub.add_parser('status', help='查看备份与最近状态')
    p_status.add_argument('--out', required=True)
    p_status.set_defaults(func=cmd_status)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
