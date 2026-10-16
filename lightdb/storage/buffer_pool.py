"""缓冲池 - LRU + 脏页回写"""
import os
import threading
from collections import OrderedDict
from .page import Page, PAGE_SIZE, PAGE_MAGIC, PAGE_TYPE_HEAP


class BufferPool:
    def __init__(self, path: str, capacity: int = 1000):
        self.path = path
        self.capacity = capacity
        self.frames = OrderedDict()
        self.lock = threading.RLock()
        
        # 数据文件首次使用时并不存在，'r+b' 会直接抛 FileNotFoundError，
        # 导致 CREATE TABLE 永远失败（且异常被上层吞掉）。先确保文件存在。
        if not os.path.exists(path):
            with open(path, 'wb'):
                pass
        self.f = open(path, 'r+b')
        self.f.seek(0, 2)
        if self.f.tell() == 0:
            meta = Page(0, page_type=0)
            self.f.write(bytes(meta.data))
            self.f.flush()
        
        # 统计
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def get_page(self, page_id: int):
        with self.lock:
            if page_id in self.frames:
                self.frames.move_to_end(page_id)
                self.hits += 1
                return self.frames[page_id]
            
            self.misses += 1
            self.f.seek(page_id * PAGE_SIZE)
            data = self.f.read(PAGE_SIZE)
            if len(data) < PAGE_SIZE:
                return None
            if data[:4] != b'LDBP':
                return None
            
            page = Page(page_id, data)
            if len(self.frames) >= self.capacity:
                self._evict_one()
            self.frames[page_id] = page
            return page
    
    def new_page(self, page_type: int = PAGE_TYPE_HEAP, limit: int = None,
                 exact: int = None) -> Page:
        """
        分配一个新页。

        `exact` / `limit`（**页级回收专用**）: 只在 `[1, limit)` 这段已有页号
        里分配，文件**不增长**；`exact` 指定时**必须**用该页号（不可用则返回
        None）。B+Tree 重建时用它们把新树塞进"被丢弃的旧索引页"留下的空洞里
        —— 否则新树会追加到文件末尾，"回收"反而把文件变大
        （本批次实测: 4000 行删 2/3 的场景 1257472 → 1306624 B）。
        """
        with self.lock:
            if exact is not None:
                pid = exact
                if pid < 1:
                    return None
                if pid * PAGE_SIZE + PAGE_SIZE > self.file_size():
                    return None
                blob = self.read_page_bytes(pid)
                if blob is not None and blob[:4] == b'LDBP':
                    return None          # 已有内容: 不是空页
                page = Page(pid, page_type=page_type)
                self.f.seek(pid * PAGE_SIZE)
                self.f.write(bytes(page.data))
                self.f.flush()
                if len(self.frames) >= self.capacity:
                    self._evict_one()
                self.frames[pid] = page
                return page
            if limit is not None:
                for pid in range(1, limit):
                    if pid in self.frames and self.frames[pid] is not None:
                        continue
                    if pid * PAGE_SIZE + PAGE_SIZE > self.file_size():
                        continue
                    blob = self.read_page_bytes(pid)
                    if blob is not None and blob[:4] == b'LDBP':
                        continue          # 已有内容: 不是空页
                    page = Page(pid, page_type=page_type)
                    self.f.seek(pid * PAGE_SIZE)
                    self.f.write(bytes(page.data))
                    self.f.flush()
                    if len(self.frames) >= self.capacity:
                        self._evict_one()
                    self.frames[pid] = page
                    return page
                return None
            self.f.seek(0, 2)
            page_id = self.f.tell() // PAGE_SIZE
            page = Page(page_id, page_type=page_type)
            self.f.seek(page_id * PAGE_SIZE)
            self.f.write(bytes(page.data))
            self.f.flush()
            
            if len(self.frames) >= self.capacity:
                self._evict_one()
            self.frames[page_id] = page
            return page
    
    def total_pages(self) -> int:
        self.f.seek(0, 2)
        return self.f.tell() // PAGE_SIZE

    def file_size(self) -> int:
        """数据文件当前字节数（= total_pages * PAGE_SIZE，页 0 是元数据页）"""
        self.f.seek(0, 2)
        return self.f.tell()

    def read_page_bytes(self, page_id: int):
        """
        **绕过缓冲池**直接从文件读一页的原始字节（None = 超出 EOF / 不完整）。

        为什么需要: vacuum 的"页级回收"先要把候选页的引用关系一次性读出来，
        而这一段读发生在 `truncate()` **之前**。走 `get_page()` 会把每一页
        灌进 LRU 帧（500 帧的缓冲区在大文件上会被这批只读页全部挤掉，
        连带把脏页驱逐一轮）；走本方法只读文件、不碰缓存，也不影响
        命中率统计与淘汰计数。
        """
        if page_id < 0:
            return None
        with self.lock:
            self.f.seek(page_id * PAGE_SIZE)
            data = self.f.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            return None
        return data

    def truncate(self, n_pages: int) -> int:
        """
        把数据文件截断到 n_pages 页（页级回收的物理动作）。

        安全性（顺序即正确性）:
          1) `flush()` 先把**所有脏页写盘并 fsync** —— 被截断的页必须是
             内容已经落在更早页上的页，否则截断等于丢数据；
          2) 丢弃缓存里 >= n_pages 的帧（这些页号此后不再存在；
             磁盘上的页号是文件长度算出来的，所以不会有残留映射）；
          3) `self.f.truncate()` + `flush()` + `fsync`，文件长度真的变短。
             只 flush 不 fsync 的话，掉电后长度可能回退成旧值，而那段时间
             里新写的页会落在**旧长度之后**——文件长度与页号仍然自洽
             （页号 = 偏移 / PAGE_SIZE），不会错位，只是"看起来没缩小"。

        返回被截掉的页数。n_pages 必须 >= 1（页 0 是元数据页），
        且不得大于当前总页数（不允许把文件"截长"）。
        """
        with self.lock:
            cur = self.total_pages()
            if n_pages < 1 or n_pages >= cur:
                return 0
            self.flush()
            for pid in [p for p in self.frames if p >= n_pages]:
                page = self.frames.pop(pid)
                if page.dirty:          # 理论上 flush() 后不该有，防御性
                    self._write_page(page)
            self.f.seek(n_pages * PAGE_SIZE)
            self.f.truncate()
            self.f.flush()
            try:
                os.fsync(self.f.fileno())
            except OSError:
                pass
            return cur - n_pages
    
    def _evict_one(self):
        for pid, page in list(self.frames.items()):
            if page.pin == 0:
                if page.dirty:
                    self._write_page(page)
                del self.frames[pid]
                self.evictions += 1
                return
    
    def _write_page(self, page: Page):
        self.f.seek(page.page_id * PAGE_SIZE)
        self.f.write(bytes(page.data))
        page.dirty = False
    
    def flush(self):
        with self.lock:
            for page in self.frames.values():
                if page.dirty:
                    self._write_page(page)
            self.f.flush()
            os.fsync(self.f.fileno())
    
    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': self.hits / total if total else 0,
            'evictions': self.evictions,
            'pages_in_cache': len(self.frames),
            'total_pages': self.total_pages(),
        }
    
    def close(self):
        self.flush()
        self.f.close()