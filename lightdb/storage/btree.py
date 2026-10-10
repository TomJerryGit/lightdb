"""
完整的 B+Tree（支持内部节点分裂 + 递归 + 根退化）
- 键: 4字节整数
- 值: 8字节 (page_id:4 + slot_id:4)
- 顺序: 键按升序
- 语义: 唯一键，重复 key 覆盖旧值

修复历史:
- 第二批: scan(start=None) 走第一个子节点
- 第二批: delete() 后调用 _maybe_collapse_root()
- 第二批: 根分裂代码去重
- 第三批: 维护 _count 计数，count() 不再 O(n)
"""
import struct
from .page import (
    PAGE_HEADER_SIZE, PAGE_TYPE_BTREE_LEAF, PAGE_TYPE_BTREE_INTERNAL,
)

LEAF_HEADER = 6
INTERNAL_HEADER = 6
LEAF_ENTRY_SIZE = 12
INTERNAL_ENTRY_SIZE = 8

LEAF_CAPACITY = (4096 - PAGE_HEADER_SIZE - LEAF_HEADER) // LEAF_ENTRY_SIZE - 1
INTERNAL_CAPACITY = (4096 - PAGE_HEADER_SIZE - INTERNAL_HEADER) // INTERNAL_ENTRY_SIZE - 1


class BTree:

    def __init__(self, bp, root_page_id=None):
        self.bp = bp
        self._count = None       # None = 未初始化；懒加载

        if root_page_id is None:
            root = bp.new_page(PAGE_TYPE_BTREE_LEAF)
            self._init_leaf(root)
            self.root_page_id = root.page_id
            self._count = 0
        else:
            self.root_page_id = root_page_id

    # ==================== 叶子节点操作 ====================

    def _init_leaf(self, page):
        base = PAGE_HEADER_SIZE
        struct.pack_into('<H', page.data, base, 0)
        struct.pack_into('<I', page.data, base + 2, 0)
        page.dirty = True

    def _leaf_n(self, page):
        return struct.unpack_from('<H', page.data, PAGE_HEADER_SIZE)[0]

    def _leaf_next(self, page):
        return struct.unpack_from(
            '<I', page.data, PAGE_HEADER_SIZE + 2)[0]

    def _leaf_set_n(self, page, n):
        struct.pack_into('<H', page.data, PAGE_HEADER_SIZE, n)

    def _leaf_set_next(self, page, pid):
        struct.pack_into('<I', page.data, PAGE_HEADER_SIZE + 2, pid)

    def _leaf_entry(self, page, i):
        off = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE
        return struct.unpack_from('<III', page.data, off)

    def _leaf_set_entry(self, page, i, k, pid, sid):
        off = PAGE_HEADER_SIZE + LEAF_HEADER + i * LEAF_ENTRY_SIZE
        struct.pack_into('<III', page.data, off, k, pid, sid)

    # ==================== 内部节点操作 ====================

    def _init_internal(self, page):
        base = PAGE_HEADER_SIZE
        struct.pack_into('<H', page.data, base, 0)
        struct.pack_into('<I', page.data, base + 2, 0)
        page.dirty = True

    def _internal_n(self, page):
        return struct.unpack_from('<H', page.data, PAGE_HEADER_SIZE)[0]

    def _internal_first(self, page):
        return struct.unpack_from(
            '<I', page.data, PAGE_HEADER_SIZE + 2)[0]

    def _internal_set_n(self, page, n):
        struct.pack_into('<H', page.data, PAGE_HEADER_SIZE, n)

    def _internal_set_first(self, page, pid):
        struct.pack_into('<I', page.data, PAGE_HEADER_SIZE + 2, pid)

    def _internal_entry(self, page, i):
        off = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE
        return struct.unpack_from('<II', page.data, off)

    def _internal_set_entry(self, page, i, k, c):
        off = PAGE_HEADER_SIZE + INTERNAL_HEADER + i * INTERNAL_ENTRY_SIZE
        struct.pack_into('<II', page.data, off, k, c)

    # ==================== 查找 ====================

    def _find_leaf(self, key):
        pid = self.root_page_id
        while True:
            page = self.bp.get_page(pid)
            if page is None:
                return None
            if page.page_type == PAGE_TYPE_BTREE_LEAF:
                return page

            n = self._internal_n(page)
            child = self._internal_first(page)
            for i in range(n):
                k, c = self._internal_entry(page, i)
                if key < k:
                    break
                child = c
            pid = child

    def search(self, key):
        page = self._find_leaf(key)
        if page is None:
            return None
        n = self._leaf_n(page)
        for i in range(n):
            k, pid, sid = self._leaf_entry(page, i)
            if k == key:
                return (pid, sid)
            if k > key:
                return None
        return None

    # ==================== 插入 ====================

    def insert(self, key, page_id, slot_id):
        """插入/覆盖，处理分裂"""
        is_new = self.search(key) is None

        split = self._insert_rec(
            self.root_page_id, key, page_id, slot_id)

        if split:
            mid_key, new_right_pid = split
            new_root = self.bp.new_page(PAGE_TYPE_BTREE_INTERNAL)
            self._init_internal(new_root)
            self._internal_set_first(new_root, self.root_page_id)
            self._internal_set_entry(new_root, 0, mid_key, new_right_pid)
            self._internal_set_n(new_root, 1)
            self.root_page_id = new_root.page_id

        if is_new and self._count is not None:
            self._count += 1

    def _insert_rec(self, pid, key, page_id, slot_id):
        """
        返回 None 或 (mid_key, new_right_page_id)
        """
        page = self.bp.get_page(pid)
        if page is None:
            return None

        # ---------- 叶子 ----------
        if page.page_type == PAGE_TYPE_BTREE_LEAF:
            n = self._leaf_n(page)
            i = 0
            while i < n and self._leaf_entry(page, i)[0] < key:
                i += 1

            if i < n and self._leaf_entry(page, i)[0] == key:
                self._leaf_set_entry(page, i, key, page_id, slot_id)
                page.dirty = True
                return None

            if n < LEAF_CAPACITY:
                for j in range(n, i, -1):
                    k, p, s = self._leaf_entry(page, j - 1)
                    self._leaf_set_entry(page, j, k, p, s)
                self._leaf_set_entry(page, i, key, page_id, slot_id)
                self._leaf_set_n(page, n + 1)
                page.dirty = True
                return None

            return self._split_leaf(page, key, page_id, slot_id)

        # ---------- 内部 ----------
        n = self._internal_n(page)
        idx = 0
        child = self._internal_first(page)
        for i in range(n):
            k, c = self._internal_entry(page, i)
            if key < k:
                idx = i
                break
            child = c
            idx = i + 1

        sub = self._insert_rec(child, key, page_id, slot_id)
        if sub is None:
            return None

        mid_key, right_pid = sub

        if n < INTERNAL_CAPACITY:
            for j in range(n, idx, -1):
                k, c = self._internal_entry(page, j - 1)
                self._internal_set_entry(page, j, k, c)
            self._internal_set_entry(page, idx, mid_key, right_pid)
            self._internal_set_n(page, n + 1)
            page.dirty = True
            return None

        return self._split_internal(page, idx, mid_key, right_pid)

    def _split_leaf(self, leaf, new_key, new_pid, new_sid):
        """叶子分裂，返回 (right_min_key, right_pid)"""
        n = self._leaf_n(leaf)
        entries = [self._leaf_entry(leaf, i) for i in range(n)]

        idx = 0
        while idx < len(entries) and entries[idx][0] < new_key:
            idx += 1
        entries.insert(idx, (new_key, new_pid, new_sid))

        mid = len(entries) // 2

        for i in range(mid):
            self._leaf_set_entry(leaf, i, *entries[i])
        self._leaf_set_n(leaf, mid)

        new_leaf = self.bp.new_page(PAGE_TYPE_BTREE_LEAF)
        self._init_leaf(new_leaf)
        for i in range(mid, len(entries)):
            self._leaf_set_entry(new_leaf, i - mid, *entries[i])
        self._leaf_set_n(new_leaf, len(entries) - mid)

        self._leaf_set_next(new_leaf, self._leaf_next(leaf))
        self._leaf_set_next(leaf, new_leaf.page_id)

        leaf.dirty = True
        new_leaf.dirty = True

        right_min_key = self._leaf_entry(new_leaf, 0)[0]
        return (right_min_key, new_leaf.page_id)

    def _split_internal(self, node, idx, new_key, new_child):
        """内部节点分裂，返回 (mid_key, new_right_pid)"""
        n = self._internal_n(node)
        first_child = self._internal_first(node)

        keys = []
        children = [first_child]
        for i in range(n):
            k, c = self._internal_entry(node, i)
            keys.append(k)
            children.append(c)

        keys.insert(idx, new_key)
        children.insert(idx + 1, new_child)

        total = len(keys)
        mid = total // 2

        assert mid <= INTERNAL_CAPACITY
        assert total - mid - 1 <= INTERNAL_CAPACITY

        self._internal_set_first(node, children[0])
        for i in range(mid):
            self._internal_set_entry(node, i, keys[i], children[i + 1])
        self._internal_set_n(node, mid)
        node.dirty = True

        new_node = self.bp.new_page(PAGE_TYPE_BTREE_INTERNAL)
        self._init_internal(new_node)
        self._internal_set_first(new_node, children[mid + 1])
        for i in range(mid + 1, total):
            self._internal_set_entry(
                new_node, i - mid - 1, keys[i], children[i + 1])
        self._internal_set_n(new_node, total - mid - 1)
        new_node.dirty = True

        return (keys[mid], new_node.page_id)

    # ==================== 删除 ====================

    def delete(self, key) -> bool:
        """简单删除（不合并兄弟，但会收缩空根）"""
        ok = self._delete_impl(key)
        if ok:
            if self._count is not None and self._count > 0:
                self._count -= 1
            self._maybe_collapse_root()
        return ok

    def _delete_impl(self, key) -> bool:
        page = self._find_leaf(key)
        if page is None:
            return False

        n = self._leaf_n(page)
        for i in range(n):
            k, _, _ = self._leaf_entry(page, i)
            if k == key:
                for j in range(i, n - 1):
                    k2, p2, s2 = self._leaf_entry(page, j + 1)
                    self._leaf_set_entry(page, j, k2, p2, s2)
                self._leaf_set_n(page, n - 1)
                page.dirty = True
                return True
            if k > key:
                return False
        return False

    def _maybe_collapse_root(self):
        """根退化：内部节点只剩 0 个 key 时，唯一子节点成为新根"""
        root = self.bp.get_page(self.root_page_id)
        if root is None:
            return
        if root.page_type != PAGE_TYPE_BTREE_INTERNAL:
            return
        if self._internal_n(root) == 0:
            only_child = self._internal_first(root)
            if only_child and only_child != 0:
                self.root_page_id = only_child

    # ==================== 扫描 ====================

    def scan(self, start=None, end=None):
        """
        顺序扫描。
        - start=None: 从最左叶子开始
        - start=x:    从第一个 >= x 的叶子开始
        """
        pid = self.root_page_id
        while True:
            page = self.bp.get_page(pid)
            if page is None:
                return
            if page.page_type == PAGE_TYPE_BTREE_LEAF:
                break

            n = self._internal_n(page)
            child = self._internal_first(page)
            if start is not None:
                for i in range(n):
                    k, c = self._internal_entry(page, i)
                    if start < k:
                        break
                    child = c
            pid = child

        page = self.bp.get_page(pid)
        while page is not None:
            n = self._leaf_n(page)
            for i in range(n):
                k, p, s = self._leaf_entry(page, i)
                if start is not None and k < start:
                    continue
                if end is not None and k > end:
                    return
                yield (k, p, s)

            next_id = self._leaf_next(page)
            if next_id == 0:
                return
            page = self.bp.get_page(next_id)

    # ==================== 统计 ====================

    def count(self) -> int:
        if self._count is None:
            self._count = sum(1 for _ in self.scan())
        return self._count

    def height(self) -> int:
        h = 1
        pid = self.root_page_id
        while True:
            page = self.bp.get_page(pid)
            if page is None or page.page_type == PAGE_TYPE_BTREE_LEAF:
                return h
            h += 1
            pid = self._internal_first(page)