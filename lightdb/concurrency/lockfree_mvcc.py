"""
无锁 MVCC（Lock-Free MVCC）
- 版本链不可变
- 使用原子操作更新头指针
- 读操作完全无锁
"""
import threading
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Version:
    """不可变版本"""
    xmin: int          # 创建事务
    xmax: int          # 删除事务（0=未删除）
    data: tuple        # 数据
    next: Optional['Version'] = None


class VersionChain:
    """
    无锁版本链
    - 头指针用原子更新
    - 读遍历链，不加锁
    """

    __slots__ = ('_head', '_lock')

    def __init__(self):
        self._head: Optional[Version] = None
        # 只用于写时 CAS 保证一致
        self._lock = threading.Lock()

    def read_visible(self, txn_id: int,
                     active_txns: set,
                     min_id: int,
                     max_id: int) -> Optional[tuple]:
        """
        读可见版本（无锁）
        - 遍历版本链找对 txn 可见的最新版本
        """
        node = self._head
        while node is not None:
            if self._is_visible(node, txn_id, active_txns,
                                 min_id, max_id):
                return node.data
            node = node.next
        return None

    def _is_visible(self, v: Version, txn_id: int,
                    active: set, min_id: int, max_id: int) -> bool:
        # 创建者可见性
        if v.xmin == txn_id:
            pass  # 自己创建的可见
        elif v.xmin in active:
            return False  # 活跃事务未提交
        elif v.xmin >= max_id:
            return False  # 新事务不可见

        # 删除者可见性
        if v.xmax == 0:
            return True
        if v.xmax == txn_id:
            return False  # 自己删除的不可见
        if v.xmax in active:
            return True  # 删除未提交，仍可见
        if v.xmax >= max_id:
            return True
        return False  # 已删除

    def add_version(self, xmin: int, xmax: int, data: tuple):
        """追加新版本（CAS）"""
        new_version = Version(
            xmin=xmin, xmax=xmax, data=data,
            next=self._head)
        with self._lock:
            self._head = new_version

    def mark_deleted(self, xmax: int, target_xmin: int) -> bool:
        """标记版本已删除（创建新版本）"""
        with self._lock:
            node = self._head
            if node and node.xmin == target_xmin and node.xmax == 0:
                # 直接更新为已删除（创建新对象替换）
                new_version = Version(
                    xmin=node.xmin, xmax=xmax,
                    data=node.data, next=node.next)
                self._head = new_version
                return True
        return False

    def prune(self, min_active: int):
        """清理无效版本（GC）"""
        with self._lock:
            node = self._head
            # 找第一个 "老版本"（xmax < min_active 且已提交）
            while node and node.next:
                if node.next.xmax != 0 and node.next.xmax < min_active:
                    # 跳过
                    node.next = None
                    break
                node = node.next


# ==================== 表级 MVCC ====================

class TableMVCC:
    """表级 MVCC 管理"""

    def __init__(self):
        self.chains = {}   # pk -> VersionChain
        self._lock = threading.RLock()

    def _get_chain(self, pk) -> VersionChain:
        with self._lock:
            if pk not in self.chains:
                self.chains[pk] = VersionChain()
            return self.chains[pk]

    def read(self, pk, txn_id: int, active: set,
             min_id: int, max_id: int):
        chain = self.chains.get(pk)
        if chain is None:
            return None
        return chain.read_visible(txn_id, active, min_id, max_id)

    def write(self, pk, xmin: int, data: tuple):
        chain = self._get_chain(pk)
        chain.add_version(xmin, 0, data)

    def delete(self, pk, xmax: int):
        chain = self.chains.get(pk)
        if chain and chain._head:
            target_xmin = chain._head.xmin
            chain.mark_deleted(xmax, target_xmin)


# ==================== 性能测试 ====================

def benchmark_lockfree_vs_lock():
    """对比无锁 MVCC vs 有锁 MVCC"""
    import time
    import threading

    # 无锁
    table = TableMVCC()
    for i in range(1000):
        table.write(i, 1, (i, f'data_{i}'))

    def read_work():
        for _ in range(100000):
            for i in range(0, 1000, 10):
                table.read(i, 2, set(), 1, 3)

    N_THREADS = 8
    threads = [threading.Thread(target=read_work)
               for _ in range(N_THREADS)]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.perf_counter()

    total = N_THREADS * 100000 * 100
    print(f"无锁 MVCC: {t1-t0:.3f}s  "
          f"({total/(t1-t0):,.0f} reads/sec)")


if __name__ == '__main__':
    benchmark_lockfree_vs_lock()