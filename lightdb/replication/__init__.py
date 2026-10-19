# lightdb/replication/__init__.py
"""复制

* `append_only_binlog`：真 · append-only binlog（决策 D 项）——独立文件、
  CRC32、组提交、位点只增不回退；`SHOW MASTER STATUS` / `SHOW BINLOG
  EVENTS` / `__BINLOG_FETCH__` 与从库复制器都以它为准。
* `binlog`：旧 JSONL 实现（仅为兼容 `db.binlog` 的历史调用方保留）。
* `master` / `slave`：主库门面 / 从库复制器（持久化位点续传、DDL 重放）。
* `semi_sync`：半同步控制器（**未接线**，仅 tests 使用；如实标注）。
"""
from .append_only_binlog import (
    AppendOnlyBinlog, BinlogReader, BinlogRecord,
    BinlogError, BinlogCorrupted, BinlogTruncated,
    TYPE_FORMAT_DESCRIPTION, TYPE_BEGIN, TYPE_COMMIT, TYPE_ROLLBACK,
    TYPE_INSERT, TYPE_UPDATE, TYPE_DELETE, TYPE_DDL,
)
from .binlog import Binlog, BinlogEvent
from .master import MasterReplicator
from .slave import SlaveReplicator, RelayLog
from .semi_sync import SemiSyncMaster, SemiSyncSlave

__all__ = [
    'AppendOnlyBinlog', 'BinlogReader', 'BinlogRecord',
    'BinlogError', 'BinlogCorrupted', 'BinlogTruncated',
    'TYPE_FORMAT_DESCRIPTION', 'TYPE_BEGIN', 'TYPE_COMMIT', 'TYPE_ROLLBACK',
    'TYPE_INSERT', 'TYPE_UPDATE', 'TYPE_DELETE', 'TYPE_DDL',
    'Binlog', 'BinlogEvent',
    'MasterReplicator',
    'SlaveReplicator', 'RelayLog',
    'SemiSyncMaster', 'SemiSyncSlave',
]
