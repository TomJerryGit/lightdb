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
    
    def new_page(self, page_type: int = PAGE_TYPE_HEAP) -> Page:
        with self.lock:
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