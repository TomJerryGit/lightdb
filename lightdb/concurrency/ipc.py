"""
进程间通信（IPC）
- 支持 Pipe / Queue / SharedMemory 三种方式
- 统一接口
"""
import multiprocessing as mp
import pickle
import struct
import time
from typing import Any, Optional


class IPCPipe:
    """基于 Pipe 的 IPC（双向）"""

    def __init__(self, conn=None):
        if conn is None:
            self.parent_conn, self.child_conn = mp.Pipe()
        else:
            self.parent_conn = None
            self.child_conn = conn

    def send(self, obj: Any):
        self.child_conn.send(obj)

    def recv(self, timeout: float = None):
        if timeout and self.child_conn.poll(timeout):
            return self.child_conn.recv()
        elif not timeout:
            return self.child_conn.recv()
        return None

    def close(self):
        try:
            if self.parent_conn:
                self.parent_conn.close()
            if self.child_conn:
                self.child_conn.close()
        except Exception:
            pass


class IPCQueue:
    """基于 Queue 的 IPC（多生产者多消费者）"""

    def __init__(self, maxsize: int = 1000):
        self.queue = mp.Queue(maxsize=maxsize)

    def put(self, obj: Any, timeout: float = None):
        self.queue.put(obj, timeout=timeout)

    def get(self, timeout: float = None):
        try:
            return self.queue.get(timeout=timeout)
        except Exception:
            return None

    def empty(self) -> bool:
        return self.queue.empty()

    def qsize(self) -> int:
        try:
            return self.queue.qsize()
        except NotImplementedError:
            return -1

    def close(self):
        try:
            self.queue.close()
        except Exception:
            pass


class IPCChannel:
    """
    高性能 IPC 通道（基于 Pipe + 定长帧）
    适合高频消息传递
    """

    HEADER = struct.Struct('<I')   # 4 字节长度

    def __init__(self, conn=None):
        self.pipe = IPCPipe(conn)

    def send_msg(self, obj: Any):
        """发送：4 字节长度 + pickle 数据"""
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        self.pipe.child_conn.send_bytes(self.HEADER.pack(len(data)) + data)

    def recv_msg(self, timeout: float = None) -> Optional[Any]:
        """接收"""
        deadline = (time.time() + timeout) if timeout else None
        # 读 header
        if not self.pipe.child_conn.poll(timeout):
            return None
        header = self.pipe.child_conn.recv_bytes(4)
        if len(header) < 4:
            return None
        length = self.HEADER.unpack(header)[0]
        data = self.pipe.child_conn.recv_bytes(length)
        return pickle.loads(data)

    def close(self):
        self.pipe.close()


# ==================== 便捷函数 ====================

def create_pipe_pair():
    """创建一对连接的 Pipe"""
    parent, child = mp.Pipe()
    return IPCPipe(conn=parent), IPCPipe(conn=child)


def create_queue(maxsize: int = 1000) -> IPCQueue:
    return IPCQueue(maxsize)


# ==================== 使用示例 ====================

def _example_worker(conn):
    """子进程示例"""
    pipe = IPCPipe(conn=conn)
    while True:
        msg = pipe.recv(timeout=1.0)
        if msg is None:
            continue
        if msg == '__quit__':
            break
        pipe.send(f"echo: {msg}")
    pipe.close()


if __name__ == '__main__':
    parent_pipe, child_pipe = create_pipe_pair()
    p = mp.Process(target=_example_worker, args=(child_pipe.child_conn,))
    p.start()

    parent_pipe.send('hello')
    print(parent_pipe.recv(timeout=2))

    parent_pipe.send('__quit__')
    p.join(timeout=2)