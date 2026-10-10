"""
内存分析
- 通过 SHOW STATUS 采集 DB 侧内存
- 长时间监控趋势
- 检测泄漏
"""
import json
import time
import threading
from typing import Dict, List

class MemoryAnalyzer:
    """内存分析器"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        # ★ 修复：初始化 _start_time，避免 _sample 里 AttributeError
        self._start_time = time.time()

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True)

    # ==================== 主入口 ====================

    def analyze(self, duration: int = 60,
                interval: int = 5,
                workload: str = 'oltp_rw') -> Dict:
        """采样内存使用，检测泄漏"""
        # ★ 修复：每次 analyze 都重置起始时间
        self._start_time = time.time()

        print(f"🔍 内存分析：{duration}s，采样间隔 {interval}s")
        print()

        samples = []
        stop = threading.Event()

        if workload:
            load_thread = threading.Thread(
                target=self._run_load, args=(workload, stop),
                daemon=True)
            load_thread.start()
            print(f"  ✓ 已启动负载: {workload}")

        start = time.time()
        try:
            while time.time() - start < duration:
                s = self._sample()
                if s:
                    samples.append(s)
                    print(f"  [{s['elapsed_s']:5.0f}s] "
                          f"RSS={s['rss_mb']:7.1f}MB  "
                          f"conns={s['connections']}  "
                          f"queries={s['queries']}")
                time.sleep(interval)
        finally:
            stop.set()

        result = self._analyze(samples, duration)
        return result

    def _sample(self) -> Dict:
        """采集一次"""
        try:
            import psutil
            pid = self._find_pid()
            if not pid:
                rss_mb = 0
            else:
                proc = psutil.Process(pid)
                rss_mb = proc.memory_info().rss / 1024 / 1024
        except Exception:
            rss_mb = 0

        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SHOW STATUS")
            status = dict(cur.fetchall())
            conn.close()

            def _int(v):
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return 0

            # ★ 修复：使用 self._start_time（__init__ 已初始化）
            return {
                'elapsed_s': time.time() - self._start_time,
                'rss_mb': rss_mb,
                'connections': _int(status.get('Threads_connected', 0)),
                'queries': _int(status.get('Questions', 0)),
                'bytes_recv': _int(status.get('Bytes_received', 0)),
                'bytes_sent': _int(status.get('Bytes_sent', 0)),
                'timestamp': time.time(),
            }
        except Exception:
            return None

    def _find_pid(self):
        """查找 LightDB 进程"""
        try:
            import subprocess
            out = subprocess.check_output(
                ['pgrep', '-f', 'lightdb.*main.py'],
                stderr=subprocess.DEVNULL)
            pids = out.decode().strip().split('\n')
            return int(pids[0]) if pids and pids[0] else 0
        except Exception:
            return 0

    def _run_load(self, workload: str, stop):
        from .workloads import get_workload
        cls = get_workload(workload)
        if not cls:
            return

        threads = 4

        def worker():
            try:
                conn = self._connect()
                wl = cls(conn, table='sbtest1')
                cur = conn.cursor()
                cur.execute("SELECT MAX(id) FROM sbtest1")
                r = cur.fetchone()
                wl._max_id = r[0] if r and r[0] else 100000
                while not stop.is_set():
                    try:
                        wl.execute_once()
                    except Exception:
                        pass
                conn.close()
            except Exception:
                pass

        ts = [threading.Thread(target=worker, daemon=True)
              for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=300)

    def _analyze(self, samples: List[Dict], duration: int) -> Dict:
        if not samples:
            return {'error': '无样本'}

        xs = [s['elapsed_s'] for s in samples]
        ys = [s['rss_mb'] for s in samples]

        n = len(xs)
        if n >= 2:
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            num = sum((xs[i] - mean_x) * (ys[i] - mean_y)
                       for i in range(n))
            den = sum((x - mean_x) ** 2 for x in xs)
            slope = num / den if den > 0 else 0
            growth_per_min = slope * 60
        else:
            growth_per_min = 0

        first_rss = samples[0]['rss_mb']
        last_rss = samples[-1]['rss_mb']
        max_rss = max(s['rss_mb'] for s in samples)

        if growth_per_min > 5:
            status = 'leak_suspected'
        elif growth_per_min > 1:
            status = 'watch'
        else:
            status = 'stable'

        return {
            'duration_s': duration,
            'samples': len(samples),
            'rss_initial_mb': first_rss,
            'rss_final_mb': last_rss,
            'rss_max_mb': max_rss,
            'rss_growth_mb': last_rss - first_rss,
            'growth_per_min_mb': growth_per_min,
            'status': status,
            'raw_samples': samples,
        }

    def format_report(self, result: Dict) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("  内存分析报告")
        lines.append("=" * 60)
        lines.append(f"  时长:        {result.get('duration_s', 0)}s")
        lines.append(f"  样本:        {result.get('samples', 0)}")
        lines.append(f"  RSS 初始:    {result.get('rss_initial_mb', 0):.1f} MB")
        lines.append(f"  RSS 最终:    {result.get('rss_final_mb', 0):.1f} MB")
        lines.append(f"  RSS 峰值:    {result.get('rss_max_mb', 0):.1f} MB")
        lines.append(f"  总增长:      {result.get('rss_growth_mb', 0):+.1f} MB")
        lines.append(f"  每分钟增长:  {result.get('growth_per_min_mb', 0):+.3f} MB/min")
        lines.append("")

        status = result.get('status', 'unknown')
        icon = {'stable': '✅', 'watch': '⚠️',
                'leak_suspected': '🔴'}.get(status, '❓')
        label = {'stable': '稳定', 'watch': '观察',
                 'leak_suspected': '疑似内存泄漏'}.get(status, status)
        lines.append(f"  结论: {icon} {label}")
        lines.append("=" * 60)
        return '\n'.join(lines)