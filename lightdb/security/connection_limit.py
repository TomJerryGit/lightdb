"""
连接限流
- 每 IP 最大并发连接
- 每 IP 登录失败次数限制（防暴力破解）
- 失败黑名单自动过期
"""
import threading
import time
from collections import defaultdict, deque


class ConnectionLimiter:
    def __init__(self, max_per_ip: int = 20,
                 max_failed: int = 5,
                 ban_seconds: int = 300,
                 window_seconds: int = 60):
        self.max_per_ip = max_per_ip
        self.max_failed = max_failed
        self.ban_seconds = ban_seconds
        self.window_seconds = window_seconds

        self.lock = threading.RLock()
        self.active = defaultdict(int)             # ip -> 活跃连接数
        self.failed = defaultdict(deque)           # ip -> [ts, ...]
        self.banned = {}                           # ip -> until_ts
        self.stats = {'rejected_conn': 0, 'rejected_login': 0, 'banned': 0}

    def check_connection(self, ip: str) -> tuple:
        """检查是否允许建立连接"""
        now = time.time()
        with self.lock:
            # 黑名单
            until = self.banned.get(ip, 0)
            if until > now:
                self.stats['rejected_conn'] += 1
                return False, f"IP {ip} 已被封禁，剩余 {int(until-now)} 秒"

            if self.active[ip] >= self.max_per_ip:
                self.stats['rejected_conn'] += 1
                return False, f"IP {ip} 连接数超限 ({self.max_per_ip})"
            return True, None

    def on_connect(self, ip: str):
        with self.lock:
            self.active[ip] += 1

    def on_disconnect(self, ip: str):
        with self.lock:
            if self.active[ip] > 0:
                self.active[ip] -= 1
            if self.active[ip] == 0:
                del self.active[ip]

    def record_login_failure(self, ip: str):
        with self.lock:
            now = time.time()
            dq = self.failed[ip]
            dq.append(now)
            # 清理窗口外的
            while dq and now - dq[0] > self.window_seconds:
                dq.popleft()

            if len(dq) >= self.max_failed:
                self.banned[ip] = now + self.ban_seconds
                self.stats['banned'] += 1
                self.stats['rejected_login'] += 1
                return True
            return False

    def record_login_success(self, ip: str):
        with self.lock:
            self.failed.pop(ip, None)

    def unban(self, ip: str):
        with self.lock:
            self.banned.pop(ip, None)
            self.failed.pop(ip, None)

    def status(self):
        with self.lock:
            now = time.time()
            return {
                'active_ips': len(self.active),
                'active_conns': sum(self.active.values()),
                'banned': {ip: int(until - now) for ip, until in self.banned.items()
                           if until > now},
                **self.stats,
            }