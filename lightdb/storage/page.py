"""
页式存储 - 4096字节页
布局:
  [0:4]   魔数 "LDBP"
  [4:8]   page_id
  [8:16]  lsn
  [16]    page_type
  [17:19] n_slots
  [19:21] next_row_ptr
  [21:24] reserved
  [24:]   槽位数组(向下增长) + 行数据(向上增长)
"""
import struct

PAGE_SIZE = 4096
PAGE_HEADER_SIZE = 24
PAGE_MAGIC = 0x4C444250  # "LDBP"

PAGE_TYPE_META = 0
PAGE_TYPE_HEAP = 1
PAGE_TYPE_BTREE_LEAF = 2
PAGE_TYPE_BTREE_INTERNAL = 3


class Page:
    __slots__ = ('data', 'page_id', 'dirty', 'pin')
    
    def __init__(self, page_id: int = 0, data: bytes = None, page_type: int = PAGE_TYPE_HEAP):
        if data is None:
            self.data = bytearray(PAGE_SIZE)
            struct.pack_into('<I', self.data, 0, PAGE_MAGIC)
            struct.pack_into('<I', self.data, 4, page_id)
            struct.pack_into('<Q', self.data, 8, 0)
            self.data[16] = page_type
            struct.pack_into('<H', self.data, 17, 0)
            struct.pack_into('<H', self.data, 19, PAGE_SIZE)
        else:
            self.data = bytearray(data)
        self.page_id = struct.unpack_from('<I', self.data, 4)[0]
        self.dirty = False
        self.pin = 0
    
    @property
    def page_type(self) -> int:
        return self.data[16]
    
    @property
    def n_slots(self) -> int:
        return struct.unpack_from('<H', self.data, 17)[0]
    
    @property
    def next_row_ptr(self) -> int:
        return struct.unpack_from('<H', self.data, 19)[0]
    
    def _set_n_slots(self, n):
        struct.pack_into('<H', self.data, 17, n)
    
    def _set_next_row_ptr(self, p):
        struct.pack_into('<H', self.data, 19, p)
    
    def _slot_off(self, i):
        return PAGE_HEADER_SIZE + i * 4
    
    def get_row(self, slot_id: int):
        if slot_id >= self.n_slots:
            return None
        off, ln = struct.unpack_from('<HH', self.data, self._slot_off(slot_id))
        if off == 0 or ln == 0:
            return None
        return bytes(self.data[off:off + ln])
    
    def insert_row(self, row_bytes: bytes):
        """返回 slot_id 或 None（空间不足）"""
        n = self.n_slots
        new_next = self.next_row_ptr - len(row_bytes)
        slot_end = PAGE_HEADER_SIZE + (n + 1) * 4
        if new_next < slot_end:
            return None
        self.data[new_next:new_next + len(row_bytes)] = row_bytes
        struct.pack_into('<HH', self.data, self._slot_off(n), new_next, len(row_bytes))
        self._set_n_slots(n + 1)
        self._set_next_row_ptr(new_next)
        self.dirty = True
        return n
    
    def delete_row(self, slot_id: int) -> bool:
        if slot_id >= self.n_slots:
            return False
        struct.pack_into('<HH', self.data, self._slot_off(slot_id), 0, 0)
        self.dirty = True
        return True
    
    def update_row_inplace(self, slot_id: int, row_bytes: bytes) -> bool:
        """如果空间足够则原地更新"""
        if slot_id >= self.n_slots:
            return False
        off, ln = struct.unpack_from('<HH', self.data, self._slot_off(slot_id))
        if off == 0 or ln < len(row_bytes):
            return False
        self.data[off:off + len(row_bytes)] = row_bytes
        struct.pack_into('<HH', self.data, self._slot_off(slot_id), off, len(row_bytes))
        self.dirty = True
        return True
    
    def free_space(self) -> int:
        return self.next_row_ptr - PAGE_HEADER_SIZE - self.n_slots * 4
    
    def iter_rows(self):
        for i in range(self.n_slots):
            rb = self.get_row(i)
            if rb is not None:
                yield i, rb