"""主库复制管理器（决策 D 项：写**真 append-only binlog**）

历史说明：旧实现（`MasterReplicator` + `db.binlog` 的 JSONL）从未被主流程
调用，`Database.binlog` 也不存在过（构造即 AttributeError）。现在主库侧的
事件记录由执行器直接写 `Database.binlog_log`（`replication/
append_only_binlog.py`）——DML 在事务提交时成组落盘、DDL 成功后落盘，
不再需要"每个写入点手动调一次 MasterReplicator"。

本类保留为**便捷门面**（`SHOW MASTER STATUS` 的数据源仍是 binlog 文件）：
调用方若显式用它记录事件，事件同样写进同一份 append-only binlog。
"""
from ..logger import logger


class MasterReplicator:
    """主库侧门面：把显式调用的事件写进 append-only binlog"""

    def __init__(self, db):
        self.db = db
        # 惰性：不在这里打开文件（未发生写入的 Database 不占句柄）
        self._pending = []

    @property
    def binlog(self):
        """真 binlog 写器（未启用时 None）"""
        return self.db.binlog_log

    # ============ 事件记录（显式调用；执行器路径不需要它们）============
    def log_insert(self, table: str, row: list, txn_id: int = 0):
        rec = self.binlog
        if rec is None:
            return
        rec.record(rec.txn_group_id(txn_id), 'INSERT', table,
                   {'row': row}, txn_id=txn_id)

    def log_update(self, table: str, pk, new_row: list, txn_id: int = 0):
        rec = self.binlog
        if rec is None:
            return
        rec.record(rec.txn_group_id(txn_id), 'UPDATE', table,
                   {'pk': pk, 'new': new_row}, txn_id=txn_id)

    def log_delete(self, table: str, pk, txn_id: int = 0):
        rec = self.binlog
        if rec is None:
            return
        rec.record(rec.txn_group_id(txn_id), 'DELETE', table,
                   {'pk': pk}, txn_id=txn_id)

    def log_ddl(self, sql: str):
        rec = self.binlog
        if rec is None:
            return
        rec.write_ddl(sql)

    def log_commit(self, txn_id: int):
        rec = self.binlog
        if rec is None:
            return
        rec.commit_txn(txn_id)

    def log_rollback(self, txn_id: int):
        rec = self.binlog
        if rec is None:
            return
        rec.rollback_txn(txn_id)

    # ============ 供从库拉取 ============
    def fetch_events(self, start_pos: int = 0, limit: int = 1000):
        rec = self.binlog
        if rec is None:
            return []
        return rec.fetch_rows(int(start_pos or 0), limit)

    def status(self) -> dict:
        rec = self.binlog
        if rec is None:
            return {'file': None, 'position': None, 'enabled': False}
        return {
            'file': rec.current_file,
            'position': rec.current_pos,
            'data_events': rec.data_event_count,
            'enabled': True,
        }


__all__ = ['MasterReplicator']
