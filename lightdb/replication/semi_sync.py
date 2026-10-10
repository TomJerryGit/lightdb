"""
半同步复制
主库提交事务时，等待至少 N 个从库确认收到 binlog。
- 超时后降级为异步
- 独立线程收集从库 ACK
"""
import threading
import time
from collections import defaultdict


class SemiSyncMaster:
    """主库侧半同步控制器"""

    def __init__(self, binlog, timeout: float = 1.0, required_acks: int = 1):
        self.binlog = binlog
        self.timeout = timeout
        self.required_acks = required_acks

        self.enabled = False
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)

        # 位点 -> 已确认的从库集合
        self.ack_map = defaultdict(set)
        # 已注册的从库
        self.slaves = set()

        # 统计
        self.stats = {
            'sync_ok': 0,
            'sync_timeout': 0,
            'async_fallback': 0,
            'avg_wait_ms': 0.0,
            'total_wait_ms': 0.0,
        }

    def enable(self):
        with self.lock:
            self.enabled = True

    def disable(self):
        with self.lock:
            self.enabled = False

    def register_slave(self, slave_id):
        with self.lock:
            self.slaves.add(slave_id)

    def unregister_slave(self, slave_id):
        with self.lock:
            self.slaves.discard(slave_id)

    def wait_for_ack(self, position: int) -> bool:
        """
        提交后等待从库确认
        返回 True=达成半同步，False=超时降级
        """
        if not self.enabled or not self.slaves:
            return False

        required = min(self.required_acks, len(self.slaves))
        deadline = time.time() + self.timeout
        start = time.time()

        with self.cond:
            while len(self.ack_map.get(position, set())) < required:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.stats['sync_timeout'] += 1
                    self.stats['async_fallback'] += 1
                    self.stats['total_wait_ms'] += (time.time() - start) * 1000
                    self._update_avg()
                    return False
                self.cond.wait(timeout=min(remaining, 0.1))

        elapsed_ms = (time.time() - start) * 1000
        self.stats['sync_ok'] += 1
        self.stats['total_wait_ms'] += elapsed_ms
        self._update_avg()
        return True

    def ack(self, slave_id, position: int):
        """从库确认收到某位点"""
        with self.cond:
            self.ack_map[position].add(slave_id)
            # 清理老位点
            for p in list(self.ack_map.keys()):
                if p < position - 100:
                    del self.ack_map[p]
            self.cond.notify_all()

    def _update_avg(self):
        total = self.stats['sync_ok'] + self.stats['sync_timeout']
        if total > 0:
            self.stats['avg_wait_ms'] = self.stats['total_wait_ms'] / total

    def status(self):
        with self.lock:
            return {
                'enabled': self.enabled,
                'timeout': self.timeout,
                'required_acks': self.required_acks,
                'slaves': len(self.slaves),
                **self.stats,
            }


class SemiSyncSlave:
    """从库侧 ACK 发送器"""

    def __init__(self, master_client, slave_id=None):
        self.master_client = master_client
        self.slave_id = slave_id or f"slave-{int(time.time() * 1000) % 100000}"
        self.last_ack_pos = 0
        self.enabled = True

    def ack(self, position: int):
        """向主库发送 ACK"""
        if not self.enabled or position <= self.last_ack_pos:
            return
        try:
            self.master_client.execute(
                f"__SEMI_SYNC_ACK__ {self.slave_id} {position}")
            self.last_ack_pos = position
        except Exception:
            pass

    def register(self):
        """注册到主库"""
        try:
            self.master_client.execute(f"__SEMI_SYNC_REGISTER__ {self.slave_id}")
        except Exception:
            pass

    def unregister(self):
        try:
            self.master_client.execute(f"__SEMI_SYNC_UNREGISTER__ {self.slave_id}")
        except Exception:
            pass