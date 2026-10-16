"""
空闲页登记表（页级回收的**持久化**部分）

问题（阶段 2 第 3 项 / 本模块要修的）:
  阶段 2.1 的 `Table._space_pages` 只活在内存里。vacuum 紧凑化出来的
  空洞、以及"插满后还剩一点空隙"的堆页，登记表随进程一起消失：
  * 重启后 `_heap_insert` 不知道任何旧页还能放东西，只能 `new_page()`
    追加 → `.dat` 因为一次重启就白白长回去；
  * 登记表本身也不会持久化"哪些页是空的"，所以 `vacuum` 无法安全截断
    文件尾部。

设计（为什么是**旁挂文件**而不是放进页/元数据）:
  * 放进页 0（元数据页）会引入循环依赖: 页 0 属于 `.dat`，而 `.dat` 的
    页内容由 BufferPool 刷盘；空闲表若存在页 0 里，截断/膨胀文件都要
    先改页 0，页 0 又必须最后落盘 —— 边界条件太多；
  * 放进 `_meta.json`（Database 元数据）会把存储层的页号写进 SQL 层的
    元数据，drop/rename/备份路径都要同步，且 `save_meta()` 不 fsync；
  * 旁挂 `<table>.free` 只有存储层知道，写盘用"临时文件 + fsync +
    os.replace"（与 `btroot` 同一套原子替换思想），单文件即可保证
    "要么旧内容、要么新内容"，不存在半截状态。

格式（纯文本，一行一个页号，便于人工诊断）:
    #lightdb-freelist v1 pages=<n>
    <page_id>
    <page_id>
    ...

**它只是提示，不是真相**。真正的判据永远是"这一页现在是不是堆页、
`free_space()` 够不够"（见 `Table._find_heap_page_with_space` 与
`Table._validate_free_pages`）。所以:
  * 文件损坏 / 被截断 → 解析失败即当作空表，退回"追加新页"，
    只损失复用机会，不会错用页面；
  * 页号越界（文件变小过）→ 校验时丢弃；
  * 页号被别的页型占用（`.dat` 缩小后又长回来、页号被 B+Tree 复用）
    → 校验时按页型丢弃。

不变量: 登记表里**只允许**出现"当前是 PAGE_TYPE_HEAP 且
`free_space() > 0`"的页号；任何登记/读取路径都要重新校验，
不允许"相信文件里写的"。
"""

import os
from pathlib import Path

FREELIST_HEADER = '#lightdb-freelist v1'


class FreePageRegistry:
    """
    一张表的空闲堆页登记表。

    `pages` 是**保序**的页号列表（后进先出复用: 刚紧凑化的页优先填，
    局部性好）。为了兼容阶段 2.1 的调用方，`Table._space_pages` 直接
    是这一份列表对象（同一个 list，不是拷贝）。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.pages = []
        #: 上一次落盘时的内容哈希/长度，用于跳过无变化的重复写（性能）
        self._saved = None
        #: 加载时解析失败的页号数（诊断用）
        self.corrupt_entries = 0

    # ==================== 磁盘 <-> 内存 ====================

    def load(self):
        """从磁盘读回页号列表（任何异常都退化为空表，绝不抛给启动路径）"""
        self.pages = []
        self._saved = None
        self.corrupt_entries = 0
        if not self.path.exists():
            return self.pages
        try:
            text = self.path.read_text(encoding='utf-8')
        except Exception:
            return self.pages
        seen = set()
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            try:
                pid = int(s)
            except (TypeError, ValueError):
                self.corrupt_entries += 1
                continue
            if pid < 1 or pid in seen:
                self.corrupt_entries += 1
                continue
            seen.add(pid)
            self.pages.append(pid)
        self._saved = self._snapshot()
        return self.pages

    def save(self, force: bool = False) -> bool:
        """
        原子落盘: 写临时文件 → fsync → `os.replace` → 目录 fsync。

        返回是否真的写了（内容没变且 force=False 时跳过写盘）。
        写盘失败只记录不抛（登记表是提示，不该让 INSERT 失败）。
        """
        snap = self._snapshot()
        if not force and snap == self._saved:
            return False
        body = [FREELIST_HEADER + f' pages={len(self.pages)}']
        body.extend(str(p) for p in self.pages)
        data = ('\n'.join(body) + '\n').encode('utf-8')
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.path)
            self._fsync_dir()
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False
        self._saved = snap
        return True

    def _fsync_dir(self):
        """目录项 fsync（POSIX 需要；Windows 不支持，忽略失败）"""
        try:
            fd = os.open(str(self.path.parent), os.O_RDONLY)
        except Exception:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _snapshot(self):
        return tuple(self.pages)

    # ==================== 登记 / 注销 ====================

    def add(self, pid: int) -> bool:
        """登记一个空闲页（已登记则把它挪到末尾 = 最近使用优先）"""
        if pid < 1:
            return False
        try:
            self.pages.remove(pid)
        except ValueError:
            pass
        self.pages.append(pid)
        return True

    def discard(self, pid: int) -> bool:
        try:
            self.pages.remove(pid)
            return True
        except ValueError:
            return False

    def keep_within(self, n_pages: int):
        """丢弃 >= n_pages 的登记项（文件被截断到 n_pages 之后调用）"""
        before = len(self.pages)
        self.pages = [p for p in self.pages if 1 <= p < n_pages]
        return before - len(self.pages)

    def clear(self):
        self.pages = []

    def __len__(self):
        return len(self.pages)

    def __contains__(self, pid):
        return pid in self.pages

    def __iter__(self):
        return iter(self.pages)

    def stats(self):
        return {
            'free_pages': len(self.pages),
            'corrupt_entries': self.corrupt_entries,
        }
