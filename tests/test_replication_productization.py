"""复制产品化：多副本注册列表 + failover 演练在未就绪时显式拒绝。"""
import json
import os
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _env():
    return {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}


def test_list_replicas_empty_status_dir(tmp_path):
    status = tmp_path / 'status'
    status.mkdir()
    script = os.path.join(ROOT, 'scripts', 'replica_apply.py')
    proc = subprocess.run(
        [sys.executable, script, '--list-replicas', '--status-dir', str(status)],
        capture_output=True, text=True, timeout=120, cwd=ROOT,
        encoding='utf-8', errors='replace', env=_env())
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


def test_failover_drill_refuses_without_fresh_replica(tmp_path):
    from lightdb.db.database import Database
    target = tmp_path / 'target'
    db = Database(str(target))
    db.shutdown()
    script = os.path.join(ROOT, 'scripts', 'replica_failover_drill.py')
    proc = subprocess.run(
        [sys.executable, script, '--target-data-dir', str(target),
         '--master-host', '127.0.0.1', '--master-port', str(_free_port()),
         '--json'],
        capture_output=True, text=True, timeout=180, cwd=ROOT,
        encoding='utf-8', errors='replace', env=_env())
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    out = proc.stdout or ''
    start = out.find('{')
    assert start >= 0, (proc.returncode, out, proc.stderr)
    data = json.loads(out[start:])
    assert data.get('ok') is False or data.get('result') in (
        'error', 'refused'), data
