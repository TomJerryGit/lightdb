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

修复历史:
- 事务原子性批次: 磁盘魔数写入字节序修正。
  旧实现用 struct.pack_into('<I', ...) 写入 0x4C444250，落盘字节是
  b'PBDL'，而 BufferPool.get_page 是按字节比较 b'LDBP' 的，
  导致**任何页都无法从磁盘读回**（进程内只靠内存帧工作）。
  崩溃后重开数据库时所有页读回 None：数据文件其实早已写好，
  但索引/堆都"看不见"了，只能靠 WAL 重放整表，删除与更新更是直接丢失。
  现在改为按大端写入魔数，落盘字节恰好是 b'LDBP'，与 BufferPool 校验一致。
  注意: 修正前写出的历史数据文件（首 4 字节为 b'PBDL'）本来就读不回来，
  修正后依旧不兼容（BufferPool 拒绝），需要重建表。
"""
import struct

PAGE_SIZE = 4096
PAGE_HEADER_SIZE = 24
PAGE_MAGIC = 0x4C444250  # "LDBP"（按大端写入磁盘，字节序 4C 44 42 50）

PAGE_TYPE_META = 0
PAGE_TYPE_HEAP = 1
PAGE_TYPE_BTREE_LEAF = 2
PAGE_TYPE_BTREE_INTERNAL = 3


class Page:
    __slots__ = ('data', 'page_id', 'dirty', 'pin')
    
    def __init__(self, page_id: int = 0, data: bytes = None, page_type: int = PAGE_TYPE_HEAP):
        if data is None:
            self.data = bytearray(PAGE_SIZE)
            # 大端写入 → 落盘字节为 b'LDBP'，与 BufferPool 的校验一致
            struct.pack_into('>I', self.data, 0, PAGE_MAGIC)
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

    def restore_row(self, slot_id: int, row_bytes: bytes) -> bool:
        """
        把 row_bytes 恢复到**指定槽位**（事务回滚 / 崩溃恢复 undo 用）。

        与 insert_row 的区别：槽位号可控。删除过的槽位（off/ln 为 0）
        会重新挂上数据；槽位已有数据且空间够则原地覆盖。
        返回 False 表示该页容纳不下（调用方需换页或走 insert_row）。
        """
        if slot_id >= self.n_slots:
            return False
        off, ln = struct.unpack_from('<HH', self.data, self._slot_off(slot_id))

        if off != 0 and ln >= len(row_bytes):
            # 原地覆盖
            self.data[off:off + len(row_bytes)] = row_bytes
            struct.pack_into('<HH', self.data, self._slot_off(slot_id),
                             off, len(row_bytes))
            self.dirty = True
            return True

        # 需要一段新空间（槽位为空，或旧位置太小）
        new_off = self.next_row_ptr - len(row_bytes)
        if new_off < PAGE_HEADER_SIZE + self.n_slots * 4:
            return False
        self.data[new_off:new_off + len(row_bytes)] = row_bytes
        struct.pack_into('<HH', self.data, self._slot_off(slot_id),
                         new_off, len(row_bytes))
        self._set_next_row_ptr(new_off)
        self.dirty = True
        return True

    def free_space(self) -> int:
        return self.next_row_ptr - PAGE_HEADER_SIZE - self.n_slots * 4

    def compact(self) -> int:
        """
        页内紧凑化：把存活行压紧到页尾，槽位号与内容都不变。

        为什么需要: `delete_row` 只把槽位置零，行数据仍占着原来的
        偏移，`free_space()` 不会变大、`insert_row` 也复用不到那些
        空洞 —— 页只增不减，这正是"vacuum 回收了版本但空间回收 0"
        的另一半原因（另一半是 horizon 判定，见 Table.vacuum）。

        安全性: 槽位号不变 ⇒ 索引/prev 指针里的 (page, slot) 依然有效，
        只有槽位里的行偏移被改写；所有读路径都通过槽位取行
        （`get_row`），不存在缓存偏移的调用方。
        返回值 = 本次紧凑化后新增的可复用字节数（>= 0）。
        """
        before = self.free_space()
        n = self.n_slots
        live = []
        for i in range(n):
            off, ln = struct.unpack_from('<HH', self.data,
                                         self._slot_off(i))
            if off == 0 or ln == 0:
                continue
            live.append((i, bytes(self.data[off:off + ln])))

        cursor = PAGE_SIZE
        for i, rb in live:
            cursor -= len(rb)
            self.data[cursor:cursor + len(rb)] = rb
            struct.pack_into('<HH', self.data, self._slot_off(i),
                             cursor, len(rb))
        self._set_next_row_ptr(cursor)
        self.dirty = True
        return self.free_space() - before
    
    def iter_rows(self):
        for i in range(self.n_slots):
            rb = self.get_row(i)
            if rb is not None:
                yield i, rb