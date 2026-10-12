"""
``page_pyx`` 的纯 Python 回退
=============================

复用的既有实现：``lightdb/storage/page.py`` 的 ``Page``（4096 字节页、
24 字节页头、槽位数组向下增长 / 行数据向上增长）。页布局常量直接
从该模块 re-export，保证回退与既有存储层、以及 .pyx 三者同一套偏移。

回退相对 ``Page`` 做的最小重映射（为了与 .pyx 的对外行为一致）:
    1. ``.pyx`` 的 ``RawPage.__cinit__`` 用 ``memset`` 清零整页，
       ``next_row_ptr`` 因此为 0（而不是 ``Page.__init__`` 的 PAGE_SIZE）；
       这里显式对齐，使 ``free_space()`` 等返回值也一致。
    2. ``.pyx`` 的 ``insert_row`` 返回 ``bint``；``Page.insert_row``
       返回 slot_id / None。回退按 ``is not None`` 转成 bool。
    3. ``.pyx`` 的 ``from_bytes`` 要求恰好 4096 字节，否则 ValueError。

未在本机验证：本机无 Cython、无 C 编译器，以下是按 .pyx 源码逐行
静态比对得到的等价性判断，**没有**任何编译产物做过对照。
"""
import struct

from lightdb.storage.page import (
    PAGE_HEADER_SIZE,
    PAGE_MAGIC,
    PAGE_SIZE,
    Page as _PyPage,
)

__all__ = [
    'PAGE_HEADER_SIZE',
    'PAGE_MAGIC',
    'PAGE_SIZE',
    'RawPage',
    'page_batch_checksum',
    'page_get_row',
    'page_n_slots',
]

_STRUCT_U16 = struct.Struct('<H')
_STRUCT_U16x2 = struct.Struct('<HH')


class RawPage:
    """页对象（纯 Python 回退，包装 ``storage.page.Page``）"""

    __slots__ = ('_page', '_dirty')

    def __init__(self, page_id=0):
        self._page = _PyPage(page_id)
        # 对齐 .pyx 的 memset 语义：整页清零后只写 magic + page_id，
        # 槽位数组为空、行区指针指向页尾（.pyx 里 next_row_ptr 初值就是
        # 页尾，行数据从后向前增长；若置 0 则任何行都塞不进页）。
        self._page.data[17:21] = b'\x00\x00' + struct.pack('<H', PAGE_SIZE)
        self._page.dirty = False
        self._dirty = False

    # ---------- 与 .pyx 对齐的属性 ----------

    @property
    def id(self):
        return self._page.page_id

    @property
    def is_dirty(self):
        return self._dirty

    def __len__(self):
        return PAGE_SIZE

    # ---------- 序列化 ----------

    def to_bytes(self):
        b = bytes(self._page.data)
        if len(b) != PAGE_SIZE:  # 防御：Page 永远是 4096，这里不该触发
            b = b[:PAGE_SIZE].ljust(PAGE_SIZE, b'\x00')
        return b

    def from_bytes(self, data):
        if len(data) != PAGE_SIZE:
            raise ValueError(f"页大小错误: {len(data)}")
        self._page.data[:] = bytes(data)
        self._page.page_id = struct.unpack_from('<I', self._page.data, 4)[0]
        self._dirty = False

    # ---------- 槽位头 ----------

    def get_n_slots(self):
        return _STRUCT_U16.unpack_from(self._page.data, 17)[0]

    def set_n_slots(self, n):
        _STRUCT_U16.pack_into(self._page.data, 17, int(n))

    def get_next_row_ptr(self):
        return _STRUCT_U16.unpack_from(self._page.data, 19)[0]

    def set_next_row_ptr(self, p):
        _STRUCT_U16.pack_into(self._page.data, 19, int(p))

    # ---------- 行操作 ----------

    def insert_row(self, row_bytes):
        """返回 True 成功 / False 空间不足（对齐 .pyx 的 bint）"""
        slot_id = self._page.insert_row(bytes(row_bytes))
        if slot_id is None:
            return False
        self._dirty = True
        return True

    def get_row(self, slot_id):
        return self._page.get_row(int(slot_id))

    def delete_row(self, slot_id):
        ok = self._page.delete_row(int(slot_id))
        if ok:
            self._dirty = True
        return ok

    def free_space(self):
        return (self.get_next_row_ptr()
                - PAGE_HEADER_SIZE
                - self.get_n_slots() * 4)


# ==================== 无对象页操作（与 .pyx 的 cpdef 对应）====================

def page_n_slots(page_data):
    """读页内槽位数（偏移 17，uint16 小端）"""
    if len(page_data) < 19:
        return 0
    return _STRUCT_U16.unpack_from(page_data, 17)[0]


def page_get_row(page_data, slot_id):
    """快速取行；槽位为空 / 越界返回 None"""
    slot_off = PAGE_HEADER_SIZE + int(slot_id) * 4
    if slot_off + 4 > len(page_data):
        return None
    off, ln = _STRUCT_U16x2.unpack_from(page_data, slot_off)
    if off == 0 or ln == 0:
        return None
    return bytes(page_data[off:off + ln])


def page_batch_checksum(page_data):
    """
    快速校验和 —— 逐字节复刻 .pyx 的算法（不是通用 CRC）:

        crc = 0xFFFFFFFF
        for i in range(0, n, 4):
            crc = (crc >> 8) ^ ((crc ^ data[i]) & 0xFF)
        return crc & 0x7FFFFFFF

    注意 .pyx 只取每 4 字节的首字节参与，回退保持一致（含其"非标准"之处）。
    """
    crc = 0xFFFFFFFF
    n = len(page_data)
    for i in range(0, n, 4):
        crc = (crc >> 8) ^ ((crc ^ page_data[i]) & 0xFF)
        crc &= 0xFFFFFFFF
    return crc & 0x7FFFFFFF
