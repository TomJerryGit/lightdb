"""
连接池 - 复用已建立的数据库会话
减少握手开销，限制最大连接数
"""
import threading
import time
from collections import deque


class ConnectionSlot:
    """一条物理连接"""
    __slots__ = ('sock', 'created_at', 'last_used', 'in_use',
                 'server_id', 'user', 'database', 'handled_queries')

    def __init__(self, sock, server_id):
        self.sock = sock
        self.created_at = time.time()
        self.last_used = time.time()
        self.in_use = False
        self.server_id = server_id
        self.user = None
        self.database = 'default'
        self.handled_queries = 0

    def touch(self):
        self.last_used = time.time()

    def age(self) -> float:
        return time.time() - self.created_at


class ConnectionPool:
    """
    连接池
    - 空闲复用
    - 超时回收
    - 最大连接限制
    """
    def __init__(self, max_size: int = 100, idle_timeout: float = 600.0,
                 max_lifetime: float = 3600.0):
        self.max_size = max_size
        self.idle_timeout = idle_timeout
        self.max_lifetime = max_lifetime

        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.idle = deque()
        self.in_use = set()
        self.total_created = 0
        self.total_reused = 0
        self.total_closed = 0

    def acquire(self) -> ConnectionSlot:
        with self.cond:
            while True:
                # 有空闲连接
                while self.idle:
                    slot = self.idle.popleft()
                    # 检查是否过期
                    if slot.age() > self.max_lifetime:
                        self._close_slot(slot)
                        continue
                    slot.in_use = True
                    slot.touch()
                    self.total_reused += 1
                    self.in_use.add(slot)
                    return slot

                # 未达上限 → 需要创建新连接
                if len(self.in_use) + len(self.idle) < self.max_size:
                    return None   # 调用方负责创建，然后调用 register()

                # 达到上限 → 等待
                self.cond.wait(timeout=1.0)
                self._reap_idle()

    def register(self, slot: ConnectionSlot):
        """创建新连接后注册"""
        with self.cond:
            slot.in_use = True
            slot.touch()
            self.in_use.add(slot)
            self.total_created += 1
            self.cond.notify_all()

    def release(self, slot: ConnectionSlot, reusable: bool = True):
        """归还连接"""
        with self.cond:
            self.in_use.discard(slot)
            if reusable and slot.age() <= self.max_lifetime:
                slot.in_use = False
                slot.touch()
                self.idle.append(slot)
            else:
                self._close_slot(slot)
            self.cond.notify_all()

    def _close_slot(self, slot):
        try:
            slot.sock.close()
        except Exception:
            pass
        self.total_closed += 1

    def _reap_idle(self):
        """清理超时空闲连接"""
        now = time.time()
        while self.idle:
            slot = self.idle[0]
            if now - slot.last_used > self.idle_timeout:
                self.idle.popleft()
                self._close_slot(slot)
            else:
                break

    def close_all(self):
        with self.lock:
            for s in self.idle:
                self._close_slot(s)
            for s in self.in_use:
                self._close_slot(s)
            self.idle.clear()
            self.in_use.clear()

    def stats(self) -> dict:
        with self.lock:
            return {
                'idle': len(self.idle),
                'in_use': len(self.in_use),
                'total_created': self.total_created,
                'total_reused': self.total_reused,
                'total_closed': self.total_closed,
                'max_size': self.max_size,
            }