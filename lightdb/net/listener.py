"""
监听器（修复版）

修复:
- 完善 SO_REUSEPORT 检测
- MasterListener 优雅停止 + 队列通知
- 平台兼容（Linux/macOS/Windows）
- 加日志
"""
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

from ..logger import logger


# ==================== 平台检测 ====================

IS_LINUX = sys.platform.startswith('linux')
IS_MAC = sys.platform == 'darwin'
IS_WINDOWS = sys.platform == 'win32'

HAS_REUSEPORT = (
    (IS_LINUX or IS_MAC)
    and hasattr(socket, 'SO_REUSEPORT')
)


# ==================== ReusePortListener ====================

class ReusePortListener:
    """
    SO_REUSEPORT 监听器（推荐）。
    每个 Worker 独立 bind 同一端口，内核负载均衡。
    """

    def __init__(self, host: str, port: int,
                 backlog: int = 128):
        self.host = host
        self.port = port
        self.backlog = backlog
        self.sock: Optional[socket.socket] = None

    def bind(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        if HAS_REUSEPORT:
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError as e:
                logger.warn(
                    f"SO_REUSEPORT 设置失败: {e}")

        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        sock.bind((self.host, self.port))
        sock.listen(self.backlog)

        self.sock = sock
        logger.info(
            f"ReusePort 监听 {self.host}:{self.port} "
            f"(reuseport={HAS_REUSEPORT})")
        return sock

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# ==================== MasterListener ====================

class MasterListener:
    """
    Master 监听器（Windows 降级方案）。
    Master bind，通过 socketpair 或队列转发给 Worker。
    """

    def __init__(self, host: str, port: int,
                 worker_pool=None, backlog: int = 128):
        self.host = host
        self.port = port
        self.backlog = backlog
        self.worker_pool = worker_pool
        self.sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread = None

    def bind(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        self.sock = sock
        logger.info(f"Master 监听 {self.host}:{self.port}")
        return sock

    def start_accept_loop(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name='MasterAccept')
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.5)
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if self._stop.is_set():
                    break
                logger.warn(f"accept 异常: {e}")
                time.sleep(0.1)
                continue
            except Exception:
                break

            if self.worker_pool:
                try:
                    self.worker_pool.dispatch(conn, addr)
                except Exception as e:
                    logger.warn(f"dispatch 失败: {e}")
                    try:
                        conn.close()
                    except Exception:
                        pass
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# ==================== 工厂 ====================

def create_listener(host: str, port: int,
                    mode: str = 'auto',
                    worker_pool=None,
                    backlog: int = 128):
    """
    创建监听器。
    mode:
      - 'auto': 自动（Linux/macOS: reuseport, Windows: master）
      - 'reuseport': 强制 SO_REUSEPORT
      - 'master': 强制 Master 转发
    """
    if mode == 'auto':
        mode = 'reuseport' if HAS_REUSEPORT else 'master'

    if mode == 'reuseport' and HAS_REUSEPORT:
        return ReusePortListener(host, port, backlog)
    return MasterListener(host, port, worker_pool, backlog)