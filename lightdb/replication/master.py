"""主库复制管理器"""
from ..logger import logger


class MasterReplicator:
    """主库侧：把每个变更写入 binlog"""

    def __init__(self, db):
        self.db = db
        self.binlog = db.binlog

    # ============ 事件记录 ============
    def log_insert(self, table: str, row: list, txn_id: int = 0):
        self.binlog.append(txn_id, 'INSERT', table, {'row': row})

    def log_update(self, table: str, pk, new_row: list, txn_id: int = 0):
        self.binlog.append(txn_id, 'UPDATE', table,
                           {'pk': pk, 'new': new_row})

    def log_delete(self, table: str, pk, txn_id: int = 0):
        self.binlog.append(txn_id, 'DELETE', table, {'pk': pk})

    def log_ddl(self, sql: str):
        self.binlog.append(0, 'DDL', None, {'sql': sql})

    def log_commit(self, txn_id: int):
        self.binlog.append(txn_id, 'COMMIT', None, {})

    # ============ 供从库拉取 ============
    def fetch_events(self, start_pos: int, limit: int = 1000):
        return self.binlog.read_from(start_pos, limit)

    def status(self) -> dict:
        return {
            'file': self.binlog.current_file(),
            'position': self.binlog.current_pos(),
        }