"""
页预读（Page Prefetch）
- 检测访问模式（顺序/随机）
- 顺序扫描时预读后续页
- 异步 IO
"""
import os
import threading
from collections import deque
from typing import Optional


class PrefetchBuffer:
    """预读缓冲"""

    def __init__(self, bp, prefetch_count: int = 8):
        self.bp = bp
        self.prefetch_count = prefetch_count
        # 预读队列：page_id -> bool
        self.prefetched = set()
        self.last_page = None
        self.sequential_run = 0

    def access(self, page_id: int):
        """访问某页时触发预读"""
        # 检测顺序模式
        if self.last_page is not None:
            if page_id == self.last_page + 1:
                self.sequential_run += 1
            elif page_id != self.last_page:
                self.sequential_run = 0

        self.last_page = page_id

        # 顺序访问达到阈值 → 预读
        if self.sequential_run >= 3:
            self._do_prefetch(page_id)

    def _do_prefetch(self, current: int):
        """启动后台预读"""
        t = threading.Thread(
            target=self._prefetch_worker,
            args=(current,),
            daemon=True)
        t.start()

    def _prefetch_worker(self, current: int):
        end = current + self.prefetch_count
        for pid in range(current + 1, end + 1):
            if pid in self.prefetched:
                continue
            try:
                page = self.bp.get_page(pid)
                if page is not None:
                    self.prefetched.add(pid)
            except Exception:
                break


class AdaptivePrefetcher:
    """自适应预读器"""

    def __init__(self, bp):
        self.bp = bp
        self.history = deque(maxlen=20)
        self.seq_score = 0

    def record(self, page_id: int):
        """记录访问"""
        if self.history:
            last = self.history[-1]
            if page_id == last + 1:
                self.seq_score = min(10, self.seq_score + 1)
            else:
                self.seq_score = max(0, self.seq_score - 2)

        self.history.append(page_id)

    def prefetch_count(self) -> int:
        """根据序列化程度决定预读数"""
        if self.seq_score >= 8:
            return 16
        elif self.seq_score >= 5:
            return 8
        elif self.seq_score >= 2:
            return 4
        return 0

    def trigger(self):
        """触发预读"""
        n = self.prefetch_count()
        if n == 0 or not self.history:
            return
        last = self.history[-1]
        for pid in range(last + 1, last + n + 1):
            try:
                self.bp.get_page(pid)
            except Exception:
                break