"""
`.dat` 页引用关系扫描（页级回收的"安全性判据"）

页级回收（真正截断文件）只有一个前提: **被截掉的页上不能有任何
还活着的东西，也不能有人指着它**。`.dat` 是混排文件，页号同时被
四处引用，缺一不可:

  1. B+Tree 的**结构指针**（根页号 + 内部节点的 (first | key→child)）
     —— 指到树页；
  2. B+Tree 叶子的**兄弟链** `next` —— 指到树页（保证 `scan()` 连续）；
  3. 行头里的 `prev_page/prev_slot` —— **版本链**，可以指到任意堆页
     上的槽位，包括墓碑（`xmax != 0`）和已经不承载活跃行的老版本；
  4. B+Tree 叶子里的 `(key → page_id, slot_id)` 索引项 —— 指到版本链
     头部（活跃行）。

一个页只要满足"没有活跃行、没有任何版本把它当 prev、不是可达树页、
不在树链上"，它上面就真的什么都没有了，截断它不丢任何可读的东西。
本模块就是把这个判据**一次性算清楚**，并把结果暴露成可以断言的结构。

为什么必须绕过缓冲池按原始字节读（`bp.read_page_bytes`）:
  截断在"扫描之后"发生，扫描期间走 `get_page()` 会把整份文件灌进
  500 帧的 LRU，把脏页全部驱逐一遍；本模块只读文件字节，不产生
  任何缓存副作用。

与 `Table.vacuum` 的关系: vacuum 负责"把死版本从页上摘掉 + 缝合
prev 链 + 紧凑化页内空洞"；本模块负责"扫出还能截到哪里"。
两者都对 (page, slot) 引用关系负责，所以放在同一个扫描里读一次。
"""

import struct

from .page import (
    PAGE_HEADER_SIZE, PAGE_SIZE, PAGE_MAGIC,
    PAGE_TYPE_HEAP, PAGE_TYPE_BTREE_LEAF, PAGE_TYPE_BTREE_INTERNAL,
)
from .mvcc import SYNTHETIC_TXN_FLOOR

#: 行头（与 Table._encode_row 一致）: xmin4|xmax4|flags1|dlen4|prev4|prev4
ROW_HEADER = 21


class PageInfo:
    """单页的引用关系（扫描结果，纯数据，便于断言/打印）"""

    __slots__ = ('page_id', 'page_type', 'active_rows', 'dead_rows',
                 'referenced_by', 'live_versions')

    def __init__(self, page_id, page_type):
        self.page_id = page_id
        self.page_type = page_type
        #: 页上"仍被索引/链引用为活跃版本"的行数（xmax == 0 或
        #: xmax 是**已提交**淘汰号 —— 单看行头分不出，这里只按
        #: "被别处当作 prev 引用" + "有 xmax==0" 归类，见 scan）
        self.active_rows = 0
        #: 页上仅作为历史版本/墓碑存在、且**没有** xmax==0 版本的行数
        self.dead_rows = 0
        #: 有多少个 (page,slot) 被当作 prev 指向本页
        self.referenced_by = 0
        #: 本页上"没有 xmax 的活版本"数（vacuum 后仍保留的当前版本）
        self.live_versions = 0


class PageLiveness:
    """
    扫描结果。

    * `live_pages`   —— 可达树页 + 树链页 + 有活跃行的堆页 + 被 prev
                        引用到的堆页。**只有不在这个集合里的页才可截断**。
    * `info`         —— page_id -> PageInfo
    * `orphan_btree` —— 不可达的树页（回收候选，诊断用）
    * `dangling_prev`—— 指向"不存在/已空"槽位的 prev 指针（数据损坏信号）
    * `truncatable`  —— 可以安全截断的空闲堆页（无活跃行、无 prev 引用）
    * `bad_magic`    —— 魔数错误/不完整的页（不参与任何判断）
    """

    def __init__(self):
        self.live_pages = set()
        self.info = {}
        self.orphan_btree = set()
        self.dangling_prev = []
        self.truncatable = set()
        self.bad_magic = set()
        self.total_pages = 0
        self.free_in_truncatable = 0

    # ---------- 打印 / 断言友好的接口 ----------

    def nonempty_pages(self):
        return sorted(self.info)

    def max_live_page(self) -> int:
        """最大的"有东西"的页号（截断上界 = 本值 + 1）；没有则 1"""
        return max(self.live_pages) if self.live_pages else 1

    def summary(self) -> dict:
        return {
            'total_pages': self.total_pages,
            'live_pages': len(self.live_pages),
            'orphan_btree_pages': len(self.orphan_btree),
            'truncatable_pages': len(self.truncatable),
            'free_bytes_in_truncatable': self.free_in_truncatable,
            'dangling_prev': len(self.dangling_prev),
            'bad_magic_pages': len(self.bad_magic),
        }


def scan_pages(bp, bt, freepages=(), live_btree=None,
               protected_pages=()) -> PageLiveness:
    """
    扫描整份 `.dat`，算出每页的引用关系与可截断性。

    参数:
      bp            BufferPool（只调 read_page_bytes / total_pages）
      bt            BTree（用于 live_pages() 判可达树页）
      freepages     空闲页登记表（列/可迭代），用于交叉校验
      live_btree    预先算好的可达树页集合；None 时现场调 bt.live_pages()
      protected_pages
                    **逻辑上仍然活着、但本扫描看不出引用的页**
                    （阶段 2.3 第①步新增）。截断判据只覆盖"写在页上的
                    四条引用"，看不到下面两类，因此调用方必须显式传进来：

                      * 页上只剩**墓碑**（`xmax != 0`）而被 `_reachable_locs`
                        判为"不可达"的页 —— 但这些墓碑仍在**索引的版本链
                        头**上，老快照要沿链读到被删的旧值；
                      * 活跃事务**物理 undo 镜像**记着 (page, slot) 的页 ——
                        ROLLBACK 要把旧行写回原页原槽位。

                    传进来的页一律进 `live_pages`（因此拉高截断上界
                    `max_live_page()`）并被排除出 `truncatable`。

    返回 PageLiveness。**只读**，不修改任何页。
    """
    res = PageLiveness()
    n = bp.total_pages()
    res.total_pages = n
    if n <= 1:
        return res

    raw = {}
    for pid in range(1, n):
        data = bp.read_page_bytes(pid)
        if data is None or len(data) < PAGE_SIZE or \
                data[:4] != struct.pack('>I', PAGE_MAGIC):
            res.bad_magic.add(pid)
            continue
        raw[pid] = data
        res.info[pid] = PageInfo(pid, data[16])

    # ---- 1) B+Tree 可达页 ----
    tree_live = live_btree if live_btree is not None else bt.live_pages()
    for pid in tree_live:
        res.live_pages.add(pid)

    # ---- 2) 叶子兄弟链（next 指针） ----
    leaf_chain = set()
    for pid, data in raw.items():
        if data[16] != PAGE_TYPE_BTREE_LEAF:
            continue
        nxt = struct.unpack_from('<I', data, PAGE_HEADER_SIZE + 2)[0]
        if nxt:
            leaf_chain.add(nxt)
            res.live_pages.add(nxt)

    # 树页孤儿 = 是树页但既不可达也不在链上的
    for pid, data in raw.items():
        if data[16] in (PAGE_TYPE_BTREE_LEAF, PAGE_TYPE_BTREE_INTERNAL) \
                and pid not in res.live_pages:
            res.orphan_btree.add(pid)

    # ---- 3) 堆页: 槽位 → 行头（prev 指针 / xmax） ----
    slot_owner = {}          # (pid,sid) -> rb，供 dangling prev 判定
    for pid, data in raw.items():
        if data[16] != PAGE_TYPE_HEAP:
            continue
        n_slots = struct.unpack_from('<H', data, 17)[0]
        max_slots = (PAGE_SIZE - PAGE_HEADER_SIZE) // 4
        if n_slots > max_slots:
            res.bad_magic.add(pid)
            continue
        page = res.info[pid]
        for sid in range(n_slots):
            off, ln = struct.unpack_from(
                '<HH', data, PAGE_HEADER_SIZE + sid * 4)
            if off == 0 or ln == 0 or off + ln > PAGE_SIZE or \
                    ln < ROW_HEADER:
                continue
            rb = data[off:off + ln]
            slot_owner[(pid, sid)] = rb
            xmin, xmax = struct.unpack_from('<II', rb, 0)
            if xmax == 0:
                page.live_versions += 1
                page.active_rows += 1
            else:
                page.dead_rows += 1

    # ---- 4) prev 指针: 谁引用了谁（并检测悬空） ----
    for (pid, sid), rb in slot_owner.items():
        pp, ps = struct.unpack_from('<II', rb, 13)
        if pp == 0:
            continue
        target = slot_owner.get((pp, ps))
        owner = res.info.get(pp)
        if owner is None:
            res.dangling_prev.append(((pid, sid), (pp, ps), 'page-missing'))
            continue
        owner.referenced_by += 1
        res.live_pages.add(pp)
        if target is None:
            res.dangling_prev.append(((pid, sid), (pp, ps), 'slot-empty'))

    # ---- 5) 有活跃行的堆页必然要留 ----
    for pid, page in res.info.items():
        if page.page_type == PAGE_TYPE_HEAP and page.active_rows:
            res.live_pages.add(pid)

    # ---- 5b) 调用方声明的"逻辑上仍活着"的页 ----
    #     本扫描只能看到写在页上的引用（树指针/兄弟链/prev/索引项），
    #     看不到"索引版本链上的墓碑"和"活跃事务 undo 镜像记得的页"。
    #     漏掉它们的后果是**真丢数据**（实测: 未提交 DELETE + vacuum 后
    #     ROLLBACK 只恢复 28/100；已提交 DELETE + vacuum 后老快照
    #     200 行全读不到），所以这里必须无条件尊重调用方的声明。
    for pid in protected_pages:
        if pid in res.info:
            res.live_pages.add(pid)

    # ---- 6) 可截断的空闲堆页 ----
    for pid, page in res.info.items():
        if page.page_type != PAGE_TYPE_HEAP:
            continue
        if page.active_rows or page.referenced_by:
            continue
        if pid in res.live_pages:       # 理论上到不了，防御性
            continue
        res.truncatable.add(pid)
        data = raw[pid]
        np_ = struct.unpack_from('<H', data, 19)[0]
        ns = struct.unpack_from('<H', data, 17)[0]
        res.free_in_truncatable += max(
            0, np_ - PAGE_HEADER_SIZE - ns * 4)

    # ---- 7) 登记表里的页是否真的可用（交叉校验，不修改） ----
    for pid in list(freepages):
        page = res.info.get(pid)
        if page is None:
            res.bad_magic.add(pid)      # 越界/坏页: 登记表里是脏项
    return res


def prev_of(rb):
    """行字节里的 (prev_page, prev_slot)"""
    if rb is None or len(rb) < ROW_HEADER:
        return (0, 0)
    return struct.unpack_from('<II', rb, 13)


def xmin_xmax(rb):
    """行字节里的 (xmin, xmax)"""
    if rb is None or len(rb) < 8:
        return (0, 0)
    return struct.unpack_from('<II', rb, 0)


def is_synthetic(tid: int) -> bool:
    return tid >= SYNTHETIC_TXN_FLOOR
