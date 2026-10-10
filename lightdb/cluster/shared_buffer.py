"""
共享 Buffer Pool
- 多进程共享页缓存
- 只读页共享，写页本地
"""
import hashlib
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..concurrency.shared_memory import (
    SharedMemoryManager, SharedBuffer, SharedCounter)
from ..storage.page import PAGE_SIZE, Page


class SharedBufferPool:
    """
    共享缓冲池
    设计：
    - 共享内存中分配固定大小的页槽
    - 页 ID 到槽位的映射每个进程独立
    - 只读命中直接读共享内存
    - 写入时 COW：拷到本地，稍后刷盘
    """

    SLOT_COUNT = 1024         # 默认共享 1024 页（16MB）
    SLOT_META_SIZE = 64       # 每槽元数据

    def __init__(self, name: str = 'buffer_pool',
                 slot_count: int = None,
                 create: bool = False):
        self.name = name
        self.slot_count = slot_count or self.SLOT_COUNT
        self.create = create

        # 计算总大小
        self.meta_size = self.slot_count * self.SLOT_META_SIZE
        self.data_size = self.slot_count * PAGE_SIZE
        self.total_size = self.meta_size + self.data_size

        # 打开共享内存
        self.mgr = SharedMemoryManager(namespace='lightdb')
        if create:
            self.shm = self.mgr.create(name, self.total_size)
        else:
            self.shm = self.mgr.open(name, self.total_size)

        # 本地元数据
        self.slot_map: Dict[int, int] = {}   # page_id -> slot_idx
        self.free_slots = list(range(self.slot_count))
        self.local_lock = threading.RLock()

        # 统计（共享）
        self.meta_base = 0
        self.data_base = self.meta_size

    # ==================== 槽位管理 ====================

    def _meta_offset(self, slot_idx: int) -> int:
        """元数据偏移"""
        return slot_idx * self.SLOT_META_SIZE

    def _data_offset(self, slot_idx: int) -> int:
        """数据偏移"""
        return self.data_base + slot_idx * PAGE_SIZE

    def _write_slot_meta(self, slot_idx: int, page_id: int,
                          dirty: bool, ref: int):
        """写槽元数据"""
        import struct
        off = self._meta_offset(slot_idx)
        data = struct.pack('<Q??I', page_id, dirty, False, ref)
        # 补齐到 64 字节
        data += b'\x00' * (self.SLOT_META_SIZE - len(data))
        self.shm.write(off, data)

    def _read_slot_meta(self, slot_idx: int) -> Dict:
        """读槽元数据"""
        import struct
        off = self._meta_offset(slot_idx)
        data = self.shm.read(off, self.SLOT_META_SIZE)
        page_id, dirty, _pad, ref = struct.unpack_from(
            '<Q??I', data, 0)
        return {
            'page_id': page_id,
            'dirty': dirty,
            'ref': ref,
        }

    # ==================== 页操作 ====================

    def get_page(self, page_id: int) -> Optional[bytes]:
        """读取页（只读）"""
        with self.local_lock:
            slot_idx = self.slot_map.get(page_id)
            if slot_idx is None:
                return None

            # 从共享内存读
            off = self._data_offset(slot_idx)
            return self.shm.read(off, PAGE_SIZE)

    def put_page(self, page_id: int, data: bytes):
        """写入页"""
        with self.local_lock:
            slot_idx = self.slot_map.get(page_id)
            if slot_idx is None:
                if not self.free_slots:
                    # 简单淘汰：丢第一个
                    old_pid = next(iter(self.slot_map))
                    slot_idx = self.slot_map.pop(old_pid)
                    self.free_slots.append(slot_idx)
                else:
                    slot_idx = self.free_slots.pop()
                self.slot_map[page_id] = slot_idx

            self.shm.write(self._data_offset(slot_idx), data)
            self._write_slot_meta(slot_idx, page_id, dirty=True, ref=1)

    def invalidate(self, page_id: int):
        """失效某页"""
        with self.local_lock:
            slot_idx = self.slot_map.pop(page_id, None)
            if slot_idx is not None:
                self.free_slots.append(slot_idx)

    def flush(self):
        """刷盘（同步本地页）"""
        self.shm.mm.flush()

    def close(self):
        self.mgr.close_all()

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        with self.local_lock:
            return {
                'slot_count': self.slot_count,
                'used_slots': len(self.slot_map),
                'free_slots': len(self.free_slots),
                'total_size_mb': self.total_size / 1024 / 1024,
            }


# ==================== 兼容 v3.0 接口 ====================

class CompatBufferPool:
    """
    兼容 v3.0 BufferPool 接口的共享池
    可无缝替换
    """

    def __init__(self, path: str, capacity: int = 1000,
                 shared: bool = False, name: str = None):
        self.path = path
        self.capacity = capacity
        self.shared = shared

        if shared:
            self.shared_pool = SharedBufferPool(
                name=name or 'default',
                slot_count=capacity,
                create=True)

        self.local_pages: Dict[int, Page] = {}
        self.local_lock = threading.RLock()

    def get_page(self, pid: int) -> Optional[Page]:
        if self.shared:
            raw = self.shared_pool.get_page(pid)
            if raw:
                return Page(pid, raw)
        with self.local_lock:
            return self.local_pages.get(pid)

    def new_page(self, page_type: int = 1) -> Page:
        # 简化：本地创建
        with self.local_lock:
            pid = max(self.local_pages.keys(), default=0) + 1
            page = Page(pid, page_type=page_type)
            self.local_pages[pid] = page
            if self.shared:
                self.shared_pool.put_page(pid, bytes(page.data))
            return page

    def flush(self):
        if self.shared:
            self.shared_pool.flush()

    def close(self):
        if self.shared:
            self.shared_pool.close()