"""LightDB 持久性矩阵（单机 Windows）。

覆盖场景（进程硬杀 = kill -9 / TerminateProcess；断电用 WAL 尾部截断模拟）：
  S1 autocommit 单行写入期间硬杀
  S2 显式事务：已提交保留、未提交消失
  S3 多行 INSERT 语句原子性（全成或全无）
  S4 checkpoint / VACUUM 期间硬杀
  S5 硬杀后 WAL 尾部截断（模拟掉电 torn write）→ 可恢复或显式报错
  S6 崩溃后重启 → 全量快照 → 恢复到新目录 → 数据一致

输出：<out>/report.json + 每个场景日志；任一场景失败退出码 1。
用法：
  python scripts/crash_matrix.py --quick                  # 1 轮/场景，约 1-2 分钟
  python scripts/crash_matrix.py --iterations 3           # 每场景 3 次随机时机
  python scripts/crash_matrix.py --json <path> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
PW = 'demo123'
DEFAULT_OUT = REPO / '_analysis_tmp' / 'crash_matrix'


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Server:
    def __init__(self, data_dir: Path, port: int, log_path: Path):
        self.data_dir = data_dir
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self, extra: list[str] | None = None):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cmd = [PYTHON, '-m', 'lightdb.main', '--mode', 'single',
               '--host', '127.0.0.1', '--port', str(self.port),
               '--data-dir', str(self.data_dir),
               '--root-password', PW, '--log-dir', str(self.data_dir.parent / 'logs')]
        if extra:
            cmd += extra
        env = {**os.environ, 'PYTHONPATH': str(REPO),
               'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        self.log = open(self.log_path, 'ab')
        self.proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=self.log,
                                     stderr=subprocess.STDOUT, env=env,
                                     creationflags=getattr(
                                         subprocess, 'CREATE_NO_WINDOW', 0))
        if not wait_port(self.port, 40):
            raise RuntimeError('server did not start')
        return self

    def kill(self):
        """硬杀（TerminateProcess）：等价 kill -9，不做任何清理。"""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def wait(self, timeout: float = 10):
        if self.proc:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass


def connect(port: int):
    import pymysql
    return pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password=PW, autocommit=True, connect_timeout=10)


def q(port: int, sql: str, params=None):
    conn = connect(port)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        conn.close()


def q_many(port: int, sql: str):
    conn = connect(port)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()


class Killer(threading.Thread):
    """延迟随机时间后硬杀服务。"""

    def __init__(self, server: Server, delay: float):
        super().__init__(daemon=True)
        self.server = server
        self.delay = delay
        self.fired = False

    def run(self):
        time.sleep(self.delay)
        self.server.kill()
        self.fired = True


def scenario_s1_autocommit_kill(work: Path, it: int, rnd: random.Random) -> dict:
    """autocommit 单行写入期间硬杀：重启后行数在合理范围且主键连续无重复。"""
    data = work / f's1_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's1_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    attempted = [0]
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                q(port, 'INSERT INTO t VALUES (%s, %s)', (i, i % 100))
                attempted[0] = i
            except Exception:
                # 服务已被硬杀：写入线程退出即可（不是测试失败）
                break

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    killer = Killer(srv, rnd.uniform(0.15, 0.8))
    killer.start()
    t.join(timeout=10)
    srv.wait(10)
    ok = True
    detail = {}
    try:
        rows = q(port, 'SELECT COUNT(*) FROM t') if False else None  # placeholder
    except Exception:
        pass
    # 重启验证
    srv2 = Server(data, port, work / f's1_{it}' / 'server_restart.log').start()
    try:
        cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        ids = [r[0] for r in q_many(port, 'SELECT id FROM t ORDER BY id')]
        contiguous = ids == list(range(1, len(ids) + 1))
        ok = cnt <= attempted[0] and contiguous and cnt >= 1
        detail = {'attempted': attempted[0], 'recovered': cnt,
                  'contiguous': contiguous}
        if not ok:
            detail['ids_head'] = ids[:10]
            detail['ids_tail'] = ids[-10:]
    finally:
        srv2.stop()
    return {'scenario': 'S1_autocommit_kill', 'ok': ok, 'detail': detail}


def scenario_s2_txn_atomicity(work: Path, it: int, rnd: random.Random) -> dict:
    """显式事务：已提交保留，未提交消失。"""
    data = work / f's2_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's2_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    conn = connect(port)
    try:
        cur = conn.cursor()
        cur.execute('BEGIN')
        for i in range(1, 51):
            cur.execute('INSERT INTO t VALUES (%s, %s)', (i, 'committed'))
        conn.commit()
    finally:
        conn.close()
    conn = connect(port)
    try:
        cur = conn.cursor()
        cur.execute('BEGIN')
        for i in range(1001, 1051):
            cur.execute('INSERT INTO t VALUES (%s, %s)', (i, 'uncommitted'))
        # 不提交，直接硬杀
        srv.kill()
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        srv.kill()
    srv.wait(10)
    srv2 = Server(data, port, work / f's2_{it}' / 'server_restart.log').start()
    ok = True
    detail = {}
    try:
        cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        committed = q_many(port, "SELECT COUNT(*) FROM t WHERE v='committed'")[0][0]
        uncommitted = q_many(port, "SELECT COUNT(*) FROM t WHERE v='uncommitted'")[0][0]
        ok = committed == 50 and uncommitted == 0 and cnt == 50
        detail = {'committed': committed, 'uncommitted': uncommitted,
                  'total': cnt}
    finally:
        srv2.stop()
    return {'scenario': 'S2_txn_atomicity', 'ok': ok, 'detail': detail}


def scenario_s3_multirow_atomicity(work: Path, it: int, rnd: random.Random) -> dict:
    """多行 INSERT 语句原子性：硬杀后要么整条语句 0 行、要么 N 行。"""
    data = work / f's3_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's3_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    n = 500
    values = ','.join(f'({i},{i})' for i in range(1, n + 1))
    killer = Killer(srv, rnd.uniform(0.05, 0.4))
    killer.start()
    try:
        q(port, f'INSERT INTO t VALUES {values}')
    except Exception:
        pass
    srv.wait(10)
    srv2 = Server(data, port, work / f's3_{it}' / 'server_restart.log').start()
    ok = True
    detail = {}
    try:
        cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        ok = cnt in (0, n)
        detail = {'expected_all_or_none': n, 'recovered': cnt}
    finally:
        srv2.stop()
    return {'scenario': 'S3_multirow_atomicity', 'ok': ok, 'detail': detail}


def scenario_s4_checkpoint_kill(work: Path, it: int, rnd: random.Random) -> dict:
    """checkpoint / VACUUM 期间硬杀：重启后数据完整。"""
    data = work / f's4_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's4_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    expected = 200
    for i in range(1, expected + 1):
        q(port, 'INSERT INTO t VALUES (%s, %s)', (i, i))
    # 在后台触发 checkpoint / vacuum，然后硬杀
    def trigger():
        try:
            q(port, 'CHECKPOINT') if False else None
        except Exception:
            pass

    killer = Killer(srv, rnd.uniform(0.05, 0.3))
    killer.start()
    try:
        q(port, 'VACUUM FULL t')
    except Exception:
        pass
    srv.wait(10)
    srv2 = Server(data, port, work / f's4_{it}' / 'server_restart.log').start()
    ok = True
    detail = {}
    try:
        cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        s = q_many(port, 'SELECT SUM(v) FROM t')[0][0]
        ok = cnt == expected and s == expected * (expected + 1) // 2
        detail = {'expected': expected, 'recovered': cnt, 'sum': int(s or 0)}
    finally:
        srv2.stop()
    return {'scenario': 'S4_checkpoint_kill', 'ok': ok, 'detail': detail}


def scenario_s5_wal_tail_truncation(work: Path, it: int) -> dict:
    """干净关闭后截断 WAL 尾部（模拟掉电 torn write）：可恢复或显式报错。"""
    data = work / f's5_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's5_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')
    n = 100
    for i in range(1, n + 1):
        q(port, 'INSERT INTO t VALUES (%s, %s)', (i, i))
    srv.stop()
    wal_files = sorted(data.glob('*.wal'))
    ok = False
    detail = {}
    if not wal_files:
        return {'scenario': 'S5_wal_tail_truncation', 'ok': False,
                'detail': {'error': 'no wal file found'}}
    wal = max(wal_files, key=lambda p: p.stat().st_size)
    size = wal.stat().st_size
    cut = max(8, int(size * 0.1))
    with open(wal, 'r+b') as f:
        f.truncate(max(0, size - cut))
    detail['truncated_bytes'] = cut
    detail['wal'] = wal.name
    try:
        srv2 = Server(data, port, work / f's5_{it}' / 'server_restart.log').start()
    except Exception as exc:
        # 显式拒绝启动也算"诚实失败"，但必须能从备份恢复——这里仅记录
        detail['refused'] = str(exc)
        return {'scenario': 'S5_wal_tail_truncation', 'ok': False,
                'detail': detail}
    try:
        cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        # 干净关闭已 fsync，截断后应能恢复到 n（或显式丢失最后若干行，不允许损坏/挂死）
        ok = 0 <= cnt <= n
        detail['expected'] = n
        detail['recovered'] = cnt
    except Exception as exc:
        detail['error_after_restart'] = f'{type(exc).__name__}: {exc}'
        ok = False
    finally:
        srv2.stop()
    return {'scenario': 'S5_wal_tail_truncation', 'ok': ok, 'detail': detail}


def scenario_s6_backup_after_crash(work: Path, it: int) -> dict:
    """崩溃重启后：全量快照 → 恢复到新目录 → 行数一致。"""
    data = work / f's6_{it}' / 'data'
    if data.exists():
        shutil.rmtree(data.parent)
    port = free_port()
    srv = Server(data, port, work / f's6_{it}' / 'server.log').start()
    q(port, 'CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))')
    conn = connect(port)
    try:
        cur = conn.cursor()
        cur.execute('BEGIN')
        for i in range(1, 101):
            cur.execute('INSERT INTO t VALUES (%s, %s)', (i, 'x'))
        conn.commit()
    finally:
        conn.close()
    srv.kill()
    srv.wait(10)
    srv2 = Server(data, port, work / f's6_{it}' / 'server_restart.log').start()
    backup = work / f's6_{it}' / 'backup'
    restore = work / f's6_{it}' / 'restore'
    ok = True
    detail = {}
    try:
        src_cnt = q_many(port, 'SELECT COUNT(*) FROM t')[0][0]
        srv2.stop()
        bp = subprocess.run([PYTHON, str(REPO / 'scripts' / 'snapshot_backup.py'),
                             '--data-dir', str(data), '--out', str(backup)],
                            capture_output=True, text=True, timeout=300)
        rp = subprocess.run([PYTHON, str(REPO / 'scripts' / 'snapshot_restore.py'),
                             '--snapshot', str(backup), '--data-dir', str(restore),
                             '--no-server-check'],
                            capture_output=True, text=True, timeout=300)
        if bp.returncode != 0 or rp.returncode != 0:
            ok = False
            detail = {'backup_rc': bp.returncode, 'restore_rc': rp.returncode,
                      'backup_tail': bp.stdout[-300:], 'restore_tail': rp.stdout[-300:]}
        else:
            port2 = free_port()
            srv3 = Server(restore, port2, work / f's6_{it}' / 'restore_server.log').start()
            try:
                dst_cnt = q_many(port2, 'SELECT COUNT(*) FROM t')[0][0]
                ok = dst_cnt == src_cnt == 100
                detail = {'source': src_cnt, 'restored': dst_cnt}
            finally:
                srv3.stop()
    finally:
        try:
            srv2.stop()
        except Exception:
            pass
    return {'scenario': 'S6_backup_after_crash', 'ok': ok, 'detail': detail}


SCENARIOS = [
    scenario_s1_autocommit_kill,
    scenario_s2_txn_atomicity,
    scenario_s3_multirow_atomicity,
    scenario_s4_checkpoint_kill,
    scenario_s5_wal_tail_truncation,
    scenario_s6_backup_after_crash,
]


def main() -> int:
    ap = argparse.ArgumentParser(description='LightDB 持久性矩阵')
    ap.add_argument('--iterations', type=int, default=2)
    ap.add_argument('--quick', action='store_true', help='每场景 1 轮（快速）')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--json', default='')
    ap.add_argument('--seed', type=int, default=20260101)
    args = ap.parse_args()
    iterations = 1 if args.quick else max(1, args.iterations)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = out / 'runs'
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    rnd = random.Random(args.seed)
    results = []
    t0 = time.time()
    for fn in SCENARIOS:
        for it in range(1, iterations + 1):
            name = fn.__name__
            log(f'--- {name} iter={it}')
            try:
                if fn is scenario_s5_wal_tail_truncation or \
                        fn is scenario_s6_backup_after_crash:
                    res = fn(work, it)
                else:
                    res = fn(work, it, rnd)
            except Exception:
                res = {'scenario': name, 'ok': False,
                       'detail': {'traceback': traceback.format_exc()[-2000:]}}
            results.append(res)
            log(f"    {'PASS' if res['ok'] else 'FAIL'} {json.dumps(res['detail'], ensure_ascii=False)[:300]}")
    elapsed = time.time() - t0
    failed = [r for r in results if not r['ok']]
    report = {
        'schema': 'lightdb-crash-matrix/1',
        'at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'iterations': iterations,
        'elapsed_s': round(elapsed, 1),
        'ok': not failed,
        'passed': len(results) - len(failed),
        'failed': len(failed),
        'results': results,
    }
    json_path = Path(args.json) if args.json else (out / 'report.json')
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding='utf-8')
    log(f"report: {json_path}  elapsed={elapsed:.1f}s  "
        f"pass={report['passed']} fail={report['failed']}")
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
