"""列存储统计"""
from dataclasses import dataclass, field


@dataclass
class ColumnStoreStats:
    rows_added: int = 0
    rows_deleted: int = 0
    scans: int = 0
    projections: int = 0
    bytes_read: int = 0