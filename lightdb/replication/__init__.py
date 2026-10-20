# lightdb/replication/__init__.py
"""复制

* `append_only_binlog`：真 · append-only binlog（决策 D 项）——独立文件、
  CRC32、组提交、位点只增不回退、**日志轮转**（binlog.000001…，旧文件
  只读保留，`__BINLOG_FETCH__.lsn` 用跨文件全局字节偏移）；`SHOW MASTER
  STATUS` / `SHOW BINLOG EVENTS` / `__BINLOG_FETCH__` 与从库复制器都以它为准。
* `binlog`：旧 JSONL 实现（仅为兼容 `db.binlog` 的历史调用方保留）。
* `master` / `slave`：主库门面 / 从库复制器（持久化位点续传、DDL 重放）。

决策记录：**不实现 GTID**（复制按"文件 + 位点"）、**不实现半同步**
（原 semi_sync 模块里的死代码已在阶段 2 移除；包上不再有任何导出）。
"""
from .append_only_binlog import (
    AppendOnlyBinlog, BinlogReader, BinlogRecord,
    BinlogError, BinlogCorrupted, BinlogTruncated, BinlogWriteError,
    binlog_enabled, binlog_fsync, binlog_strict, binlog_max_bytes,
    TYPE_FORMAT_DESCRIPTION, TYPE_BEGIN, TYPE_COMMIT, TYPE_ROLLBACK,
    TYPE_INSERT, TYPE_UPDATE, TYPE_DELETE, TYPE_DDL,
)
from .binlog import Binlog, BinlogEvent
from .master import MasterReplicator
from .slave import SlaveReplicator, RelayLog

__all__ = [
    'AppendOnlyBinlog', 'BinlogReader', 'BinlogRecord',
    'BinlogError', 'BinlogCorrupted', 'BinlogTruncated', 'BinlogWriteError',
    'binlog_enabled', 'binlog_fsync', 'binlog_strict', 'binlog_max_bytes',
    'TYPE_FORMAT_DESCRIPTION', 'TYPE_BEGIN', 'TYPE_COMMIT', 'TYPE_ROLLBACK',
    'TYPE_INSERT', 'TYPE_UPDATE', 'TYPE_DELETE', 'TYPE_DDL',
    'Binlog', 'BinlogEvent',
    'MasterReplicator',
    'SlaveReplicator', 'RelayLog',
]
