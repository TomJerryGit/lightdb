"""
看门狗（Watchdog）
- 监控 DB 进程
- 崩溃自动重启
- 死锁自动检测
- 内存超限自动重启
"""
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


@dataclass
class WatchdogConfig:
    check_interval_s: int = 10
    # 重启条件
    restart_on_crash: bool = True
    restart_on_hang: bool = True
    hang_threshold_s: int = 60
    restart_on_oom: bool = True
    memory_limit_mb: int = 2048
    # 限流
    max_restarts_per_hour: int = 10
    # 命令
    start_command: List[str] = None
    pid_file: str = ""


class Watchdog:
    """看门狗"""

    def __init__(self, config: WatchdogConfig = None):
        self.config = config or WatchdogConfig()
        self.running = False
        self._thread = None
        self.restart_history: List[float] = []
        self.stats = {
            'checks': 0,
            'crashes_detected': 0,
            'restarts': 0,
            'hangs_detected': 0,
            'oom_detected': 0,
        }
        self._callbacks: List[Callable] = []

    # ==================== 生命周期 ====================

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='Watchdog')
        self._thread.start()
        print(f"🐕 看门狗启动，检查间隔 {self.config.check_interval_s}s")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self.running:
            try:
                self._check()
                self.stats['checks'] += 1
            except Exception as e:
                print(f"看门狗异常: {e}")
            time.sleep(self.config.check_interval_s)

    # ==================== 检查 ====================

    def _check(self):
        pid = self._get_pid()

        # 1. 进程存活
        if pid is None:
            if self.config.restart_on_crash:
                self.stats['crashes_detected'] += 1
                print(f"⚠️ 检测到崩溃，尝试重启...")
                self._restart()
            return

        # 2. 内存超限
        if self.config.restart_on_oom:
            try:
                import psutil
                proc = psutil.Process(pid)
                rss_mb = proc.memory_info().rss / 1024 / 1024
                if rss_mb > self.config.memory_limit_mb:
                    self.stats['oom_detected'] += 1
                    print(f"⚠️ 内存超限 {rss_mb:.0f}MB > "
                          f"{self.config.memory_limit_mb}MB")
                    self._restart()
                    return
            except Exception:
                pass

        # 3. 服务响应检查
        if self.config.restart_on_hang:
            resp_time = self._check_responsive()
            if resp_time is not None and \
               resp_time > self.config.hang_threshold_s:
                self.stats['hangs_detected'] += 1
                print(f"⚠️ 服务无响应 {resp_time:.0f}s")
                self._restart()

        # 通知回调
        for cb in self._callbacks:
            try:
                cb(pid)
            except Exception:
                pass

    def _get_pid(self) -> Optional[int]:
        # 优先读 pid 文件
        if self.config.pid_file:
            try:
                with open(self.config.pid_file) as f:
                    return int(f.read().strip())
            except Exception:
                pass

        # 通过 pgrep
        try:
            out = subprocess.check_output(
                ['pgrep', '-f', 'lightdb.*main.py'],
                stderr=subprocess.DEVNULL)
            pids = out.decode().strip().split('\n')
            return int(pids[0]) if pids and pids[0] else None
        except Exception:
            return None

    def _check_responsive(self) -> Optional[float]:
        """检查服务响应"""
        t0 = time.time()
        try:
            import pymysql
            conn = pymysql.connect(
                host='127.0.0.1', port=8888,
                user='root', password='',
                connect_timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.close()
            return time.time() - t0
        except Exception:
            return None

    # ==================== 重启 ====================

    def _restart(self):
        # 检查重启限流
        now = time.time()
        hour_ago = now - 3600
        self.restart_history = [t for t in self.restart_history
                                 if t > hour_ago]

        if len(self.restart_history) >= self.config.max_restarts_per_hour:
            print(f"🚫 重启次数过多 "
                  f"({len(self.restart_history)}/h)，暂缓")
            return

        # 杀旧进程
        pid = self._get_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(3)
                # 强杀
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except Exception:
                pass

        # 启动新进程
        try:
            if self.config.start_command:
                subprocess.Popen(
                    self.config.start_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True)
            else:
                subprocess.Popen(
                    [sys.executable, 'main.py'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True)

            self.restart_history.append(now)
            self.stats['restarts'] += 1
            print(f"✓ 已重启 (第 {self.stats['restarts']} 次)")

        except Exception as e:
            print(f"❌ 重启失败: {e}")

    # ==================== 统计 ====================

    def on_check(self, callback: Callable):
        self._callbacks.append(callback)

    def stats_report(self) -> str:
        lines = [
            f"🐕 看门狗统计",
            f"  检查次数: {self.stats['checks']}",
            f"  检测崩溃: {self.stats['crashes_detected']}",
            f"  检测卡死: {self.stats['hangs_detected']}",
            f"  检测 OOM: {self.stats['oom_detected']}",
            f"  重启次数: {self.stats['restarts']}",
        ]
        return '\n'.join(lines)


# ==================== CLI ====================

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=int, default=10)
    ap.add_argument('--memory-limit', type=int, default=2048)
    ap.add_argument('--no-restart', action='store_true')
    args = ap.parse_args()

    config = WatchdogConfig(
        check_interval_s=args.interval,
        memory_limit_mb=args.memory_limit,
        restart_on_crash=not args.no_restart,
    )

    wd = Watchdog(config)
    wd.start()

    try:
        while True:
            time.sleep(60)
            print()
            print(wd.stats_report())
    except KeyboardInterrupt:
        wd.stop()
        print("\n👋 看门狗停止")


if __name__ == '__main__':
    main()