"""
原子操作工具
"""
import threading
from typing import Callable


class AtomicCounter:
    """进程内原子计数器"""

    def __init__(self, initial: int = 0):
        self._value = initial
        self._lock = threading.Lock()

    def increment(self, delta: int = 1) -> int:
        with self._lock:
            self._value += delta
            return self._value

    def decrement(self, delta: int = 1) -> int:
        return self.increment(-delta)

    def get(self) -> int:
        with self._lock:
            return self._value

    def set(self, value: int):
        with self._lock:
            self._value = value

    def compare_and_set(self, expected: int, new_value: int) -> bool:
        with self._lock:
            if self._value == expected:
                self._value = new_value
                return True
            return False


class AtomicDict:
    """线程安全字典"""

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def delete(self, key):
        with self._lock:
            return self._data.pop(key, None)

    def update(self, key, update_func: Callable):
        with self._lock:
            current = self._data.get(key)
            new_value = update_func(current)
            self._data[key] = new_value
            return new_value

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def items(self):
        with self._lock:
            return list(self._data.items())

    def __len__(self):
        with self._lock:
            return len(self._data)


class AtomicRingBuffer:
    """无锁环形缓冲（单生产者单消费者）"""

    def __init__(self, capacity: int = 1024):
        self.capacity = capacity
        self.buffer = [None] * capacity
        self.head = 0   # 写入位置
        self.tail = 0   # 读取位置
        self.lock = threading.Lock()

    def push(self, item) -> bool:
        with self.lock:
            next_head = (self.head + 1) % self.capacity
            if next_head == self.tail:
                return False   # 满
            self.buffer[self.head] = item
            self.head = next_head
            return True

    def pop(self):
        with self.lock:
            if self.tail == self.head:
                return None   # 空
            item = self.buffer[self.tail]
            self.buffer[self.tail] = None
            self.tail = (self.tail + 1) % self.capacity
            return item

    def __len__(self):
        with self.lock:
            return (self.head - self.tail) % self.capacity