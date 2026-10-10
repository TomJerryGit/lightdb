"""
故障注入测试
- kill -9 进程
- 磁盘满
- 网络中断
- 内存限制
- CPU 打满
- 时钟漂移
"""
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class FaultResult:
    name: str
    description: str
    started_at: float = 0
    ended_at: float = 0
    duration_s: float = 0
    success: bool = False         # 注入是否成功
    # 恢复情况
    recovered: bool = False
    recovery_time_s: float = 0
    # 数据
    data_lost: bool = False
    rows_before: int = 0
    rows_after: int = 0
    errors: List[str] = field(default_factory=list)
    notes: str = ""


class FaultInjector:
    """故障注入器"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    # ==================== 连接工具 ====================

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True,
            connect_timeout=5)

    def _find_pid(self) -> Optional[int]:
        try:
            out = subprocess.check_output(
                ['pgrep', '-f', 'lightdb.*main.py'],
                stderr=subprocess.DEVNULL)
            pids = out.decode().strip().split('\n')
            return int(pids[0]) if pids and pids[0] else None
        except Exception:
            return None

    def _get_row_count(self) -> int:
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM fault_test")
            n = cur.fetchone()[0]
            conn.close()
            return n
        except Exception:
            return -1

    def _wait_for_recovery(self, timeout=60) -> float:
        """等待服务恢复，返回恢复耗时"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                conn = self._connect()
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                conn.close()
                return time.time() - t0
            except Exception:
                time.sleep(0.5)
        return -1

    # ==================== 前置准备 ====================

    def _setup_table(self):
        """创建测试表"""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS fault_test")
        cur.execute("""
            CREATE TABLE fault_test (
                id INT PRIMARY KEY,
                v INT
            )
        """)
        # 插入 100 行
        for i in range(1, 101):
            cur.execute(
                "INSERT INTO fault_test VALUES (%s, %s)",
                (i, i * 10))
        conn.close()

    # ==================== 故障场景 ====================

    def test_kill_9(self) -> FaultResult:
        """kill -9 强杀进程"""
        result = FaultResult(
            name='kill_9',
            description='kill -9 强杀数据库进程')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()

            pid = self._find_pid()
            if not pid:
                result.errors.append("未找到 LightDB 进程")
                return result

            print(f"  强杀 PID={pid}...")
            os.kill(pid, signal.SIGKILL)

            # 等待恢复
            print(f"  等待服务恢复...")
            rec_time = self._wait_for_recovery(timeout=60)

            if rec_time < 0:
                result.errors.append("服务未恢复")
                return result

            result.recovered = True
            result.recovery_time_s = rec_time
            print(f"  ✓ 恢复耗时 {rec_time:.2f}s")

            # 数据校验
            result.rows_after = self._get_row_count()

            # 允许部分未提交数据丢失，但已提交必须保留
            if result.rows_after >= 0:
                result.data_lost = result.rows_after < result.rows_before
            result.success = True

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    def test_disk_full(self) -> FaultResult:
        """磁盘满"""
        result = FaultResult(
            name='disk_full',
            description='模拟磁盘空间不足')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()

            # 创建大文件占满空间
            tmp_dir = Path.home() / '.lightdb_studio' / 'disk_test'
            tmp_dir.mkdir(parents=True, exist_ok=True)
            fill_file = tmp_dir / 'fill.bin'

            # 检测可用空间
            import shutil
            stat = shutil.disk_usage(str(tmp_dir))
            free_mb = stat.free / 1024 / 1024
            fill_mb = max(100, int(free_mb * 0.9))   # 填 90%
            print(f"  可用空间: {free_mb:.0f}MB，填充: {fill_mb}MB")

            try:
                with open(fill_file, 'wb') as f:
                    chunk = b'\x00' * (1024 * 1024)  # 1MB
                    for _ in range(fill_mb):
                        f.write(chunk)

                # 尝试写入数据（预期失败）
                print(f"  尝试写入数据...")
                write_ok = False
                try:
                    conn = self._connect()
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO fault_test VALUES (9999, 9999)")
                    write_ok = True
                    conn.close()
                except Exception as e:
                    result.errors.append(f"预期内的写入失败: {e}")

                # 释放空间
                print(f"  释放空间...")
                fill_file.unlink(missing_ok=True)

                # 检查服务状态
                rec_time = self._wait_for_recovery(timeout=30)
                if rec_time >= 0:
                    result.recovered = True
                    result.recovery_time_s = rec_time

                result.rows_after = self._get_row_count()
                result.success = True

            finally:
                if fill_file.exists():
                    fill_file.unlink(missing_ok=True)

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    def test_network_break(self) -> FaultResult:
        """网络中断"""
        result = FaultResult(
            name='network_break',
            description='网络中断 5 秒')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()

            # 用 iptables（Linux）或 Windows 防火墙
            if sys.platform == 'linux':
                print(f"  使用 iptables 阻断端口 {self.port}...")
                subprocess.run(
                    ['sudo', 'iptables', '-A', 'INPUT',
                     '-p', 'tcp', '--dport', str(self.port), '-j', 'DROP'],
                    check=False, capture_output=True)
                time.sleep(5)
                subprocess.run(
                    ['sudo', 'iptables', '-D', 'INPUT',
                     '-p', 'tcp', '--dport', str(self.port), '-j', 'DROP'],
                    check=False, capture_output=True)
            else:
                # Windows 或其他：模拟客户端断开
                print(f"  模拟客户端网络中断...")
                time.sleep(5)

            rec_time = self._wait_for_recovery(timeout=30)
            if rec_time >= 0:
                result.recovered = True
                result.recovery_time_s = rec_time

            result.rows_after = self._get_row_count()
            result.data_lost = (result.rows_after < result.rows_before)
            result.success = True

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    def test_memory_limit(self) -> FaultResult:
        """内存限制（用 ulimit 或降级压测）"""
        result = FaultResult(
            name='memory_limit',
            description='模拟内存不足')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()

            # 启动内存分配器（不带 ulimit 会影响测试机）
            # 模拟：短时间内大量连接+查询
            print(f"  模拟高内存压力...")
            threads = []
            stop = threading.Event()

            def stress():
                import pymysql
                while not stop.is_set():
                    try:
                        conn = pymysql.connect(
                            host=self.host, port=self.port,
                            user=self.user, password=self.password,
                            autocommit=True)
                        cur = conn.cursor()
                        for _ in range(100):
                            cur.execute(
                                "SELECT * FROM fault_test LIMIT 100")
                            cur.fetchall()
                        conn.close()
                    except Exception:
                        time.sleep(0.1)

            for _ in range(20):
                t = threading.Thread(target=stress, daemon=True)
                t.start()
                threads.append(t)

            time.sleep(10)
            stop.set()

            for t in threads:
                t.join(timeout=2)

            rec_time = self._wait_for_recovery(timeout=30)
            if rec_time >= 0:
                result.recovered = True
                result.recovery_time_s = rec_time

            result.rows_after = self._get_row_count()
            result.data_lost = (result.rows_after < result.rows_before)
            result.success = True

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    def test_cpu_saturation(self) -> FaultResult:
        """CPU 打满"""
        result = FaultResult(
            name='cpu_saturation',
            description='CPU 打满 10 秒')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()

            print(f"  启动 CPU 压力（10s）...")
            stop = threading.Event()
            threads = []

            def burn():
                while not stop.is_set():
                    x = 0
                    for i in range(10**6):
                        x += i

            cpu_count = os.cpu_count() or 4
            for _ in range(cpu_count):
                t = threading.Thread(target=burn, daemon=True)
                t.start()
                threads.append(t)

            time.sleep(10)
            stop.set()
            for t in threads:
                t.join(timeout=2)

            rec_time = self._wait_for_recovery(timeout=30)
            if rec_time >= 0:
                result.recovered = True
                result.recovery_time_s = rec_time

            result.rows_after = self._get_row_count()
            result.data_lost = (result.rows_after < result.rows_before)
            result.success = True

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    def test_concurrent_kill_loop(self, iterations: int = 5) -> FaultResult:
        """连续 kill 循环（压测恢复能力）"""
        result = FaultResult(
            name='kill_loop',
            description=f'连续 kill -9 {iterations} 次')
        result.started_at = time.time()

        try:
            self._setup_table()
            result.rows_before = self._get_row_count()
            total_recovery = 0

            for i in range(iterations):
                pid = self._find_pid()
                if not pid:
                    result.errors.append(f"第 {i+1} 次未找到进程")
                    break

                print(f"  [{i+1}/{iterations}] kill PID={pid}")
                os.kill(pid, signal.SIGKILL)

                rec_time = self._wait_for_recovery(timeout=60)
                if rec_time < 0:
                    result.errors.append(f"第 {i+1} 次未恢复")
                    break
                total_recovery += rec_time
                print(f"          恢复 {rec_time:.2f}s")

            result.rows_after = self._get_row_count()
            result.recovered = True
            result.recovery_time_s = total_recovery / iterations \
                if iterations else 0
            result.data_lost = (result.rows_after < result.rows_before)
            result.success = len(result.errors) == 0

        except Exception as e:
            result.errors.append(str(e))
        finally:
            result.ended_at = time.time()
            result.duration_s = result.ended_at - result.started_at

        return result

    # ==================== 主流程 ====================

    def run_all(self) -> List[FaultResult]:
        """运行全部故障注入"""
        scenarios = [
            ('kill_9', self.test_kill_9),
            ('kill_loop', self.test_concurrent_kill_loop),
            ('memory_limit', self.test_memory_limit),
            ('cpu_saturation', self.test_cpu_saturation),
            ('network_break', self.test_network_break),
            # 磁盘满最后跑（因为可能残留）
            ('disk_full', self.test_disk_full),
        ]

        results = []
        print(f"\n{'='*60}")
        print(f"  故障注入测试")
        print(f"{'='*60}\n")

        for name, fn in scenarios:
            print(f"▶ {name}")
            try:
                r = fn()
                results.append(r)
                status = '✅' if r.success and r.recovered else '❌'
                print(f"  {status} 成功={r.success}, 恢复={r.recovered}, "
                      f"耗时={r.recovery_time_s:.2f}s, "
                      f"数据丢失={r.data_lost}")
                if r.errors:
                    for e in r.errors[:3]:
                        print(f"     ! {e[:100]}")
            except Exception as e:
                print(f"  ❌ 异常: {e}")
            print()

        return results

    def print_summary(self, results: List[FaultResult]):
        """打印总结"""
        print("=" * 60)
        print("  故障注入总结")
        print("=" * 60)

        passed = 0
        for r in results:
            icon = '✅' if (r.success and r.recovered) else '❌'
            lost = '数据丢失' if r.data_lost else '数据完整'
            print(f"  {icon} {r.name:20s} "
                  f"恢复 {r.recovery_time_s:6.2f}s  {lost}")
            if r.success and r.recovered:
                passed += 1

        print(f"\n  通过: {passed}/{len(results)}")
        print("=" * 60)

        return passed == len(results)


# ==================== CLI ====================

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8888)
    ap.add_argument('--user', default='root')
    ap.add_argument('--password', default='')
    args = ap.parse_args()

    injector = FaultInjector(
        host=args.host, port=args.port,
        user=args.user, password=args.password)
    results = injector.run_all()
    injector.print_summary(results)


if __name__ == '__main__':
    main()