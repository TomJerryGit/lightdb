"""
30 天长稳测试
- 持续运行混合负载
- 定时采样指标
- 自动检测异常
- 输出稳定性报告
"""
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional


SOAK_DIR = Path.home() / '.lightdb_studio' / 'soak'
SOAK_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SoakSample:
    """采样点"""
    elapsed_s: float
    timestamp: float
    qps: float = 0
    tps: float = 0
    errors: int = 0
    rss_mb: float = 0
    cpu_pct: float = 0
    active_conns: int = 0
    slow_queries: int = 0
    # 逻辑一致性
    checksum: str = ""
    row_count: int = 0


@dataclass
class SoakConfig:
    duration_hours: int = 24          # 测试时长
    sample_interval_s: int = 300      # 采样间隔
    workload_mix: str = "oltp_rw"     # 负载类型
    threads: int = 8
    rows: int = 100000
    # 检查项
    check_memory: bool = True
    check_data: bool = True
    check_crash: bool = True
    # 阈值
    memory_growth_mb_per_hour: float = 5.0
    error_rate_threshold: float = 0.001


class SoakTest:
    """长稳测试"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password='', config: SoakConfig = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.config = config or SoakConfig()

        self.samples: List[SoakSample] = []
        self.errors: List[Dict] = []
        self.start_time = 0
        self.end_time = 0
        self._stop = threading.Event()
        self._load_threads = []

        # 报告文件
        self.report_path = SOAK_DIR / f"soak_{int(time.time())}.json"

    # ==================== 主入口 ====================

    def run(self):
        """运行长稳测试"""
        self.start_time = time.time()
        duration_s = self.config.duration_hours * 3600

        print(f"🚀 长稳测试开始")
        print(f"   时长: {self.config.duration_hours}h")
        print(f"   负载: {self.config.workload_mix} "
              f"× {self.config.threads} 线程")
        print(f"   采样: 每 {self.config.sample_interval_s}s")
        print(f"   报告: {self.report_path}")
        print()

        # 准备数据
        self._setup()

        # 启动负载
        self._start_load()

        # 采样循环
        try:
            self._sample_loop(duration_s)
        except KeyboardInterrupt:
            print("\n⚠️ 用户中断")
        finally:
            self._stop.set()
            self._join_load()
            self.end_time = time.time()
            self._save_report()
            self._print_summary()

    def _setup(self):
        """准备测试数据"""
        import pymysql
        print("📦 准备测试数据...")

        conn = pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True)

        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS soak_test")
        cur.execute("""
            CREATE TABLE soak_test (
                id INT PRIMARY KEY,
                v INT NOT NULL,
                c VARCHAR(120) NOT NULL
            )
        """)

        # 批量插入
        batch = []
        for i in range(1, self.config.rows + 1):
            batch.append((i, i % 10000, f'value_{i}'))
            if len(batch) >= 500:
                placeholders = ','.join(['(%s,%s,%s)'] * len(batch))
                cur.execute(
                    f"INSERT INTO soak_test VALUES {placeholders}",
                    [v for r in batch for v in r])
                batch = []
        if batch:
            placeholders = ','.join(['(%s,%s,%s)'] * len(batch))
            cur.execute(
                f"INSERT INTO soak_test VALUES {placeholders}",
                [v for r in batch for v in r])

        conn.close()
        print(f"   ✓ {self.config.rows} 行准备完成")

    def _start_load(self):
        """启动负载线程"""
        from ..benchmark.workloads import get_workload

        cls = get_workload(self.config.workload_mix)
        if not cls:
            print(f"❌ 未知负载: {self.config.workload_mix}")
            return

        import pymysql
        for _ in range(self.config.threads):
            t = threading.Thread(
                target=self._worker, args=(cls,),
                daemon=True)
            t.start()
            self._load_threads.append(t)

        print(f"✓ 已启动 {self.config.threads} 个负载线程")

    def _worker(self, cls):
        """负载线程"""
        import pymysql
        while not self._stop.is_set():
            try:
                conn = pymysql.connect(
                    host=self.host, port=self.port,
                    user=self.user, password=self.password,
                    autocommit=True)
                wl = cls(conn, table='soak_test')
                wl._max_id = self.config.rows
                while not self._stop.is_set():
                    try:
                        wl.execute_once()
                    except Exception as e:
                        self.errors.append({
                            'ts': time.time(),
                            'error': str(e)[:200],
                        })
                conn.close()
            except Exception:
                time.sleep(1)

    def _join_load(self):
        for t in self._load_threads:
            t.join(timeout=5)

    # ==================== 采样 ====================

    def _sample_loop(self, duration_s):
        """采样循环"""
        next_sample = time.time()
        last_log = time.time()

        while True:
            if self._stop.is_set():
                break
            elapsed = time.time() - self.start_time
            if elapsed > duration_s:
                print("\n✓ 长稳测试完成")
                break

            if time.time() >= next_sample:
                try:
                    sample = self._take_sample(elapsed)
                    if sample:
                        self.samples.append(sample)
                except Exception as e:
                    print(f"采样异常: {e}")
                next_sample = time.time() + self.config.sample_interval_s

            # 每小时打印状态
            if time.time() - last_log >= 60:
                self._log_status(elapsed)
                last_log = time.time()

            time.sleep(1)

    def _take_sample(self, elapsed) -> Optional[SoakSample]:
        import pymysql
        sample = SoakSample(
            elapsed_s=elapsed,
            timestamp=time.time())

        try:
            # 进程内存
            try:
                import psutil
                pid = self._find_pid()
                if pid:
                    proc = psutil.Process(pid)
                    sample.rss_mb = proc.memory_info().rss / 1024 / 1024
                    sample.cpu_pct = proc.cpu_percent()
            except Exception:
                pass

            # 状态
            conn = pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                autocommit=True)
            cur = conn.cursor()
            cur.execute("SHOW STATUS")
            status = dict(cur.fetchall())

            def _int(v):
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return 0

            sample.active_conns = _int(
                status.get('Threads_connected', 0))

            # 数据校验
            if self.config.check_data:
                cur.execute(
                    "SELECT COUNT(*), SUM(id) FROM soak_test")
                row = cur.fetchone()
                sample.row_count = row[0] if row else 0
                # 简单校验和
                import hashlib
                cur.execute(
                    "SELECT id FROM soak_test ORDER BY id LIMIT 1000")
                ids = [str(r[0]) for r in cur.fetchall()]
                sample.checksum = hashlib.md5(
                    ','.join(ids).encode()).hexdigest()[:16]

            conn.close()

        except Exception as e:
            self.errors.append({
                'ts': time.time(),
                'error': f"sample: {str(e)[:200]}",
            })
            return None

        return sample

    def _find_pid(self):
        try:
            import subprocess
            out = subprocess.check_output(
                ['pgrep', '-f', 'lightdb.*main.py'],
                stderr=subprocess.DEVNULL)
            pids = out.decode().strip().split('\n')
            return int(pids[0]) if pids and pids[0] else 0
        except Exception:
            return 0

    def _log_status(self, elapsed):
        """实时状态"""
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)

        if self.samples:
            last = self.samples[-1]
            # QPS 变化
            qps = 0
            if len(self.samples) >= 2:
                prev = self.samples[-2]
                dt = last.elapsed_s - prev.elapsed_s
                if dt > 0:
                    qps = (last.elapsed_s - prev.elapsed_s) / dt

            print(f"  [{h:02d}h{m:02d}m] "
                  f"RSS={last.rss_mb:.1f}MB  "
                  f"Conn={last.active_conns}  "
                  f"Rows={last.row_count}  "
                  f"Errors={len(self.errors)}")
        else:
            print(f"  [{h:02d}h{m:02d}m] 采样中...")

    # ==================== 报告 ====================

    def _save_report(self):
        report = {
            'config': asdict(self.config),
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_hours': (self.end_time - self.start_time) / 3600,
            'samples': [asdict(s) for s in self.samples],
            'errors': self.errors[-1000:],
            'error_count': len(self.errors),
            'analysis': self._analyze(),
        }
        with open(self.report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def _analyze(self) -> dict:
        """分析结果"""
        if len(self.samples) < 2:
            return {'error': '样本不足'}

        # 内存增长
        first = self.samples[0]
        last = self.samples[-1]
        growth_mb = last.rss_mb - first.rss_mb
        hours = (last.elapsed_s - first.elapsed_s) / 3600
        growth_per_hour = growth_mb / hours if hours > 0 else 0

        # 错误率
        total_time = self.end_time - self.start_time
        error_rate = len(self.errors) / max(1, total_time / 3600)

        # 数据一致性
        checksums = {s.checksum for s in self.samples if s.checksum}
        data_consistent = len(checksums) <= 1

        # 结论
        issues = []
        if growth_per_hour > self.config.memory_growth_mb_per_hour:
            issues.append(
                f"内存增长 {growth_per_hour:.2f} MB/h 超过阈值 "
                f"{self.config.memory_growth_mb_per_hour} MB/h")

        if not data_consistent:
            issues.append(
                f"数据校验和不一致 ({len(checksums)} 种)")

        # 判断
        passed = len(issues) == 0

        return {
            'passed': passed,
            'memory_growth_mb': round(growth_mb, 2),
            'memory_growth_mb_per_hour': round(growth_per_hour, 3),
            'total_errors': len(self.errors),
            'error_rate_per_hour': round(error_rate, 2),
            'data_consistent': data_consistent,
            'issues': issues,
            'samples_count': len(self.samples),
        }

    def _print_summary(self):
        analysis = self._analyze()
        hours = (self.end_time - self.start_time) / 3600

        print("\n" + "=" * 60)
        print("  长稳测试报告")
        print("=" * 60)
        print(f"  时长: {hours:.2f} 小时")
        print(f"  采样: {analysis.get('samples_count', 0)} 次")
        print(f"  错误: {analysis.get('total_errors', 0)} 次")
        print(f"  内存增长: {analysis.get('memory_growth_mb', 0):+.2f} MB "
              f"({analysis.get('memory_growth_mb_per_hour', 0):+.3f} MB/h)")
        print(f"  数据一致: "
              f"{'✅' if analysis.get('data_consistent') else '❌'}")

        if analysis.get('passed'):
            print(f"\n  ✅ 长稳测试通过！")
        else:
            print(f"\n  ❌ 长稳测试失败：")
            for issue in analysis.get('issues', []):
                print(f"     - {issue}")

        print(f"\n  报告: {self.report_path}")
        print("=" * 60)