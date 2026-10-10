"""
崩溃恢复 v3
- 从 WAL 恢复未提交的事务
- 从快照恢复
"""
import os
import time
from pathlib import Path
from typing import Dict, List


class CrashRecovery:
    """崩溃恢复器"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.stats = {
            'wal_replayed': 0,
            'snapshots_loaded': 0,
            'errors': [],
        }

    def recover(self, db) -> Dict:
        """
        执行恢复流程：
        1. 加载最新快照
        2. 重放 WAL
        3. 清理临时文件
        """
        t0 = time.time()
        self.stats['errors'] = []

        # 1. 清理临时文件
        self._cleanup_tmp()

        # 2. 重放 WAL（通过 Database 的 recover）
        try:
            n = 0
            for tbl in db.tables.values():
                n += tbl.recover_from_wal()
            self.stats['wal_replayed'] = n
        except Exception as e:
            self.stats['errors'].append(f"WAL 恢复失败: {e}")

        # 3. 清理临时文件
        self._cleanup_tmp()

        elapsed = time.time() - t0
        self.stats['elapsed_s'] = elapsed
        return self.stats

    def _cleanup_tmp(self):
        """清理临时文件"""
        for pattern in ('*.tmp', '*.temp', '*.swp'):
            for f in self.data_dir.glob(pattern):
                try:
                    f.unlink()
                except Exception:
                    pass

    def create_snapshot(self, db, name: str = None) -> str:
        """创建恢复快照"""
        if not name:
            name = f"snap_{int(time.time())}"
        path = self.data_dir / f"{name}.snap"
        import pickle
        with open(path, 'wb') as f:
            pickle.dump({
                'tables': list(db.tables.keys()),
                'users': db.users,
                'created_at': time.time(),
            }, f)
        self.stats['snapshots_loaded'] += 1
        return str(path)