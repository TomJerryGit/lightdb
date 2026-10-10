"""
共享内存封装（修复版）

修复:
- atomic_* 使用跨进程锁（文件锁，fcntl/msvcrt）
- 增加 CrossProcessLock 类
- 兼容 Linux/macOS/Windows
- 修正 HAS_REUSEPORT 检测
"""
import mmap
import os
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


# ==================== 平台检测 ====================

import sys
IS_LINUX = sys.platform.startswith('linux')
IS_MAC = sys.platform == 'darwin'
IS_WINDOWS = sys.platform == 'win32'

try:
    import socket
    HAS_REUSEPORT = (
        (IS_LINUX or IS_MAC)
        and hasattr(socket, 'SO_REUSEPORT'))
except Exception:
    HAS_REUSEPORT = False


# ==================== 跨进程锁 ====================

class CrossProcessLock:
    """
    跨进程文件锁。
    - Unix: fcntl.flock
    - Windows: msvcrt.locking
    """

    def __init__(self, path: str, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self._fd = None
        self._thread_lock = threading.RLock()

    def _open(self):
        if self._fd is None:
            # O_CREAT | O_RDWR
            self._fd = os.open(
                self.path,
                os.O_CREAT | os.O_RDWR,
                0o600)
        return self._fd

    @contextmanager
    def acquire(self):
        with self._thread_lock:
            fd = self._open()
            deadline = time.time() + self.timeout
            acquired = False

            while True:
                try:
                    if IS_WINDOWS:
                        import msvcrt
                        # 锁定 1 字节
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(
                            fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise TimeoutError(
                            f"获取跨进程锁超时: {self.path}")
                    time.sleep(0.001)

            try:
                yield
            finally:
                try:
                    if IS_WINDOWS:
                        import msvcrt
                        try:
                            msvcrt.locking(
                                fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass

    def close(self):
        with self._thread_lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None


# ==================== SharedBuffer ====================

class SharedBuffer:
    """
    共享缓冲区（跨进程）。
    使用 mmap 文件实现。
    """

    HEADER_SIZE = 64
    MAGIC = 0x4C444250  # 'LDBP'

    def __init__(self, name: str, size: int,
                 create: bool = False, dir: str = None):
        self.name = name
        self.size = size
        self.create = create
        self.lock = threading.RLock()

        dir = dir or str(
            Path.home() / '.lightdb_studio' / 'shm')
        Path(dir).mkdir(parents=True, exist_ok=True)
        self.path = os.path.join(dir, f'{name}.shm')
        self.lock_path = self.path + '.lock'

        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT | os.O_TRUNC
        fd = os.open(self.path, flags, 0o600)
        try:
            actual_size = self.HEADER_SIZE + size
            if create:
                os.ftruncate(fd, actual_size)
            self.mm = mmap.mmap(
                fd, actual_size, access=mmap.ACCESS_WRITE)
        finally:
            os.close(fd)

        if create:
            self._write_header()

        # ★ 跨进程锁
        self.xlock = CrossProcessLock(self.lock_path)

    def _write_header(self):
        struct.pack_into(
            '<IIQ', self.mm, 0,
            self.MAGIC, 1, self.size)

    def _read_header(self):
        magic, version, size = struct.unpack_from(
            '<IIQ', self.mm, 0)
        return {
            'magic': magic,
            'version': version,
            'size': size,
        }

    # ==================== 读写 ====================

    def write(self, offset: int, data: bytes):
        if offset + len(data) > self.size:
            raise ValueError(
                f"越界: {offset + len(data)} > {self.size}")
        with self.lock:
            self.mm.seek(self.HEADER_SIZE + offset)
            self.mm.write(data)

    def read(self, offset: int, length: int) -> bytes:
        if offset + length > self.size:
            raise ValueError("越界")
        with self.lock:
            self.mm.seek(self.HEADER_SIZE + offset)
            return self.mm.read(length)

    # ==================== 原子操作（跨进程）====================

    def atomic_increment(self, offset: int,
                        delta: int = 1) -> int:
        """
        ★ 跨进程原子自增。
        使用文件锁保证一致性。
        """
        with self.xlock.acquire():
            pos = self.HEADER_SIZE + offset
            val = struct.unpack_from('<q', self.mm, pos)[0]
            new_val = val + delta
            struct.pack_into('<q', self.mm, pos, new_val)
            return new_val

    def atomic_get(self, offset: int) -> int:
        with self.xlock.acquire():
            pos = self.HEADER_SIZE + offset
            return struct.unpack_from('<q', self.mm, pos)[0]

    def atomic_set(self, offset: int, value: int):
        with self.xlock.acquire():
            pos = self.HEADER_SIZE + offset
            struct.pack_into('<q', self.mm, pos, value)

    def atomic_compare_and_set(
            self, offset: int, expected: int,
            new_value: int) -> bool:
        """CAS 跨进程"""
        with self.xlock.acquire():
            pos = self.HEADER_SIZE + offset
            val = struct.unpack_from('<q', self.mm, pos)[0]
            if val == expected:
                struct.pack_into('<q', self.mm, pos, new_value)
                return True
            return False

    # ==================== 生命周期 ====================

    def close(self):
        with self.lock:
            try:
                self.mm.flush()
            except Exception:
                pass
            try:
                self.mm.close()
            except Exception:
                pass
            try:
                self.xlock.close()
            except Exception:
                pass

    def unlink(self):
        self.close()
        for p in (self.path, self.lock_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
            except Exception:
                pass


# ==================== SharedMemoryManager ====================

class SharedMemoryManager:
    def __init__(self, namespace: str = 'lightdb',
                 shm_dir: str = None):
        self.namespace = namespace
        # 显式目录优先；未指定时才回落到 ~/.lightdb_studio/shm。
        # Master / Worker 会统一传入 <data_dir>/shm，避免把运行期状态写到
        # 用户主目录，也避免多实例互相覆盖。
        self.shm_dir = shm_dir
        self.buffers = {}
        self._lock = threading.RLock()

    def create(self, name: str, size: int) -> SharedBuffer:
        with self._lock:
            full_name = f'{self.namespace}_{name}'
            buf = SharedBuffer(full_name, size, create=True,
                               dir=self.shm_dir)
            self.buffers[name] = buf
            return buf

    def open(self, name: str, size: int) -> SharedBuffer:
        with self._lock:
            if name in self.buffers:
                return self.buffers[name]
            full_name = f'{self.namespace}_{name}'
            buf = SharedBuffer(full_name, size, create=False,
                               dir=self.shm_dir)
            self.buffers[name] = buf
            return buf

    def close_all(self):
        with self._lock:
            for buf in self.buffers.values():
                try:
                    buf.close()
                except Exception:
                    pass
            self.buffers.clear()

    def destroy_all(self):
        with self._lock:
            for buf in self.buffers.values():
                try:
                    buf.unlink()
                except Exception:
                    pass
            self.buffers.clear()


# ==================== SharedCounter ====================

class SharedCounter:
    """跨进程安全计数器"""

    def __init__(self, shm: SharedBuffer, offset: int = 0):
        self.shm = shm
        self.offset = offset

    def increment(self, delta: int = 1) -> int:
        return self.shm.atomic_increment(self.offset, delta)

    def get(self) -> int:
        return self.shm.atomic_get(self.offset)

    def set(self, value: int):
        self.shm.atomic_set(self.offset, value)

    def cas(self, expected: int, new_value: int) -> bool:
        return self.shm.atomic_compare_and_set(
            self.offset, expected, new_value)


# ==================== SharedLock ====================

class SharedLock:
    """
    跨进程锁（基于共享内存 + CAS）。
    注意: 仅适合短临界区，长持有可能浪费 CPU。
    """

    def __init__(self, shm: SharedBuffer, offset: int = 0,
                 timeout: float = 5.0):
        self.shm = shm
        self.offset = offset
        self.timeout = timeout

    @contextmanager
    def acquire(self):
        deadline = time.time() + self.timeout
        while True:
            # 尝试 CAS 0 -> 1
            if self.shm.atomic_compare_and_set(
                    self.offset, 0, 1):
                break
            if time.time() > deadline:
                raise TimeoutError("获取共享锁超时")
            time.sleep(0.001)

        try:
            yield
        finally:
            self.shm.atomic_set(self.offset, 0)