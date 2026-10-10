"""
CPU Profiler 封装
"""
import cProfile
import pstats
import io
import os
import sys
import threading
import time
from typing import Dict, List


class CPUProfiler:
    """CPU Profiling 工具"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True,
            cursorclass=pymysql.cursors.Cursor,
        )

    # ==================== Profile ====================

    def profile(self, workload: str = 'oltp_rw',
                duration: int = 10,
                output: str = 'profile.out',
                format: str = 'pstats') -> Dict:
        """
        对指定负载进行 Profiling
        注意：只能 profile 本地进程（如果 DB 与测试同一进程）

        更常用的方式：
        - 用 py-spy 附加到 DB 进程（推荐）
        - 或在 DB 进程内启用 profile 钩子
        """
        print(f"🔍 Profiling 模式: {format}")
        print(f"   负载: {workload}")
        print(f"   时长: {duration}s")
        print()

        # 尝试 py-spy
        if self._try_pyspy(workload, duration, output, format):
            return self._read_pyspy_result(output)

        # 降级：cProfile 采样本地客户端
        print("⚠️ py-spy 不可用，使用 cProfile 采样本地 SQL 处理")
        return self._cprofile_mode(workload, duration, output)

    def _try_pyspy(self, workload, duration, output, format) -> bool:
        """尝试使用 py-spy（推荐，可附加到运行中的进程）"""
        import shutil
        import subprocess

        if not shutil.which('py-spy'):
            return False

        # 找 LightDB 进程 PID
        pid = self._find_lightdb_pid()
        if not pid:
            print("⚠️ 未找到 LightDB 进程（需先启动 lightdb-server）")
            return False

        print(f"✅ 找到 LightDB 进程: PID={pid}")

        # 启动 py-spy 后台
        if format == 'flamegraph':
            cmd = ['py-spy', 'record', '-p', str(pid),
                   '-o', output, '-d', str(duration),
                   '--format', 'speedscope']
        else:
            cmd = ['py-spy', 'record', '-p', str(pid),
                   '-o', output, '-d', str(duration)]

        print(f"📊 采样中（{duration}s）...")
        # 同时跑负载
        prof_thread = subprocess.Popen(cmd)

        self._run_workload(workload, duration)

        prof_thread.wait(timeout=duration + 10)
        print(f"✅ Profile 输出: {output}")
        return True

    def _find_lightdb_pid(self):
        """查找 LightDB 服务进程"""
        try:
            import subprocess
            # Linux/Mac
            try:
                out = subprocess.check_output(
                    ['pgrep', '-f', 'lightdb.*main.py'],
                    stderr=subprocess.DEVNULL)
                pids = out.decode().strip().split('\n')
                return int(pids[0]) if pids and pids[0] else None
            except Exception:
                pass

            # Windows
            try:
                out = subprocess.check_output(
                    ['wmic', 'process', 'where',
                     "name='python.exe'", 'get', 'processid,commandline'],
                    stderr=subprocess.DEVNULL)
                for line in out.decode().split('\n'):
                    if 'lightdb' in line and 'main.py' in line:
                        parts = line.strip().split()
                        for p in reversed(parts):
                            if p.isdigit():
                                return int(p)
            except Exception:
                pass
        except Exception:
            pass
        return None

    def _run_workload(self, workload: str, duration: int):
        """执行负载"""
        from .workloads import get_workload
        cls = get_workload(workload)
        if not cls:
            return

        threads = 4
        stop = threading.Event()

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
        time.sleep(duration)
        stop.set()
        for t in ts:
            t.join(timeout=5)

    def _cprofile_mode(self, workload, duration, output) -> Dict:
        """cProfile 采样（本地）"""
        profiler = cProfile.Profile()
        profiler.enable()

        self._run_workload(workload, duration)

        profiler.disable()
        profiler.dump_stats(output)

        return self._parse_pstats(output)

    def _parse_pstats(self, path: str) -> Dict:
        """解析 pstats"""
        stats = pstats.Stats(path)
        stats.sort_stats('cumulative')

        # Top 20
        hotspots = []
        stream = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = stream
        try:
            stats.print_stats(20)
        finally:
            sys.stdout = old_stdout

        # 解析纯文本输出
        text = stream.getvalue()
        lines = text.split('\n')
        for line in lines:
            parts = line.split()
            if len(parts) >= 6 and parts[0].replace('.', '').isdigit():
                try:
                    ncalls = parts[0]
                    tottime = float(parts[1])
                    cumtime = float(parts[3])
                    func = ' '.join(parts[5:])
                    hotspots.append({
                        'ncalls': ncalls,
                        'tottime': tottime,
                        'cumtime': cumtime,
                        'function': func,
                    })
                except (ValueError, IndexError):
                    pass

        return {'hotspots': hotspots}

    def _read_pyspy_result(self, path: str) -> Dict:
        if not os.path.exists(path):
            return {'hotspots': []}
        try:
            with open(path, 'r', errors='ignore') as f:
                content = f.read()
            return {'raw': content[:10000], 'path': path}
        except Exception:
            return {'hotspots': []}

    # ==================== 格式化 ====================

    def format_hotspots(self, result: Dict) -> str:
        lines = []
        lines.append("=" * 80)
        lines.append("  CPU Profiling 结果")
        lines.append("=" * 80)

        hotspots = result.get('hotspots', [])
        if hotspots:
            lines.append(f"\n{'ncalls':>10s}  {'tottime':>10s}  "
                          f"{'cumtime':>10s}  {'function':s}")
            lines.append("-" * 80)
            for h in hotspots[:20]:
                lines.append(
                    f"{h['ncalls']:>10s}  {h['tottime']:>10.3f}  "
                    f"{h['cumtime']:>10.3f}  {h['function']}")
        else:
            lines.append("\n无热点数据（可能 py-spy 已生成火焰图）")
            if 'path' in result:
                lines.append(f"输出文件: {result['path']}")

        lines.append("\n" + "=" * 80)
        return '\n'.join(lines)