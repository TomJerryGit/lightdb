"""事务管理器 + MVCC"""
import threading


class Transaction:
    def __init__(self, txn_id: int):
        self.txn_id = txn_id
        self.snapshot = set()
        self.min_active = txn_id
        self.max_active = txn_id
        self.status = 'ACTIVE'
        self.writes = []


class TxnManager:
    def __init__(self):
        self.lock = threading.RLock()
        self._next_id = 1
        self.active = {}
        self.committed = set()
        self.aborted = set()
    
    def begin(self) -> Transaction:
        with self.lock:
            tid = self._next_id
            self._next_id += 1
            txn = Transaction(tid)
            txn.snapshot = set(self.active.keys())
            txn.min_active = min(txn.snapshot) if txn.snapshot else tid
            txn.max_active = tid
            self.active[tid] = txn
            return txn
    
    def commit(self, txn: Transaction):
        with self.lock:
            txn.status = 'COMMITTED'
            self.active.pop(txn.txn_id, None)
            self.committed.add(txn.txn_id)
    
    def abort(self, txn: Transaction):
        with self.lock:
            txn.status = 'ABORTED'
            self.active.pop(txn.txn_id, None)
            self.aborted.add(txn.txn_id)
    
    def is_visible(self, xmin: int, xmax: int, txn: Transaction) -> bool:
        """检查行是否对事务可见"""
        # 创建者
        if xmin == txn.txn_id:
            pass
        elif xmin in self.aborted:
            return False
        elif xmin in self.active:
            return False
        elif xmin > txn.max_active:
            return False
        # xmin已提交且可见
        
        # 删除者
        if xmax == 0:
            return True
        if xmax == txn.txn_id:
            return False
        if xmax in self.aborted:
            return True
        if xmax in self.active:
            return True
        if xmax > txn.max_active:
            return True
        return False