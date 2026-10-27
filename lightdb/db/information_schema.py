"""information_schema 元数据表

这里的所有元数据都来自**真实的表定义**（`Table.cols`），并且与
`SHOW COLUMNS` 共用 `db/database.py` 的 `describe_columns` —— 同一份
信息在两条路径上不可能不一致。特别地：

* `COLUMN_DEFAULT` 是解析器真的解析下来的 `DEFAULT` 取值（没声明就是
  NULL），不再是恒 `None`；
* `EXTRA` 恒为空字符串（本引擎没有 `auto_increment` / `on update`），
  不再由主键**伪造**；
* `DATA_TYPE` 保留长度/精度（`varchar(20)`、`decimal(10,2)`）。
"""
from typing import List, Tuple

from .database import (
    INFORMATION_SCHEMA_DB, column_default, column_extra, column_key,
    describe_columns,
)


class InformationSchema:
    """元数据虚拟表"""
    
    def __init__(self, db):
        self.db = db
    
    def query(self, table: str) -> Tuple[List[str], List[Tuple]]:
        t = table.upper()
        if t == 'TABLES':
            return self._tables()
        if t == 'COLUMNS':
            return self._columns()
        if t == 'SCHEMATA':
            return self._schemata()
        if t == 'PARTITIONS':
            return self._partitions()
        return [], []

    #: MySQL 5.7/8.0 `information_schema.PARTITIONS` 的列（顺序照官方文档）
    PARTITIONS_COLUMNS = (
        'TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'PARTITION_NAME',
        'SUBPARTITION_NAME', 'PARTITION_ORDINAL_POSITION',
        'PARTITION_METHOD', 'PARTITION_EXPRESSION',
        'PARTITION_DESCRIPTION', 'TABLE_ROWS', 'AVG_ROW_LENGTH',
        'DATA_LENGTH', 'MAX_DATA_LENGTH', 'INDEX_LENGTH', 'DATA_FREE',
        'CREATE_TIME', 'UPDATE_TIME', 'CHECK_TIME', 'CHECKSUM',
        'PARTITION_COMMENT', 'NODEGROUP', 'TABLESPACE_NAME')

    def _partitions(self):
        """分区元数据（Navicat 打开表时查它；`NOT ISNULL(PARTITION_NAME)`）

        * 分区表：每个分区一行（名称/类型/表达式/描述来自真实
          `Table._partition` + parser 分区定义）；
        * 非分区表：MySQL 同样返回**一行**、分区字段为 NULL（Navicat 的
          `NOT ISNULL(PARTITION_NAME)` 因此会过滤掉它们），不是"没有行"。
        不编造 TABLE_ROWS / 数据长度：本引擎分区是逻辑路由（数据仍在同一
        `.dat`），无法给出每分区真实字节数 → 这些列报 NULL。
        """
        cols = list(self.PARTITIONS_COLUMNS)
        rows = []
        for tname, tbl in self.db.tables.items():
            spec = getattr(tbl, '_partition_spec', None) or {}
            mgr = getattr(tbl, '_partition', None)
            plist = getattr(mgr, 'partitions', None) if mgr is not None else None
            if spec and plist:
                ptype = str(spec.get('type', '')).upper()
                expr = spec.get('col')
                for ordinal, part in enumerate(plist, 1):
                    desc = None
                    value = getattr(part, 'value', None)
                    if ptype == 'RANGE' and isinstance(value, tuple):
                        lo, hi = value
                        desc = 'MAXVALUE' if hi is None else str(hi)
                    elif ptype == 'LIST' and isinstance(value, (list, tuple)):
                        desc = ','.join(str(v) for v in value)
                    elif value is not None:
                        desc = str(value)
                    rows.append((
                        'def', self.db.name, tname, getattr(part, 'name', None),
                        None, ordinal, ptype, expr, desc,
                        None, None, None, None, None, None,
                        None, None, None, None, '', None, None))
            else:
                rows.append((
                    'def', self.db.name, tname, None, None, None, None,
                    None, None, None, None, None, None, None, None,
                    None, None, None, None, '', None, None))
        return cols, rows
    
    def _tables(self):
        cols = ['TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'TABLE_TYPE',
                'ENGINE', 'TABLE_ROWS', 'TABLE_COLLATION', 'TABLE_COMMENT']
        rows = []
        for name, tbl in self.db.tables.items():
            rows.append((
                'def', self.db.name, name, 'BASE TABLE',
                'InnoDB', tbl.row_count, 'utf8mb4_general_ci', ''
            ))
        return cols, rows
    
    def _columns(self):
        cols = ['TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'COLUMN_NAME',
                'ORDINAL_POSITION', 'COLUMN_DEFAULT', 'IS_NULLABLE', 'DATA_TYPE',
                'COLUMN_KEY', 'EXTRA', 'COLUMN_COMMENT']
        rows = []
        for tname, tbl in self.db.tables.items():
            for pos, c in enumerate(describe_columns(tbl), 1):
                rows.append((
                    'def', self.db.name, tname, c['name'],
                    pos, c['default'],
                    c['null'],
                    c['type'].lower(),
                    c['key'],
                    c['extra'],
                    ''
                ))
        return cols, rows
    
    def _schemata(self):
        cols = ['CATALOG_NAME', 'SCHEMA_NAME', 'DEFAULT_CHARACTER_SET_NAME',
                'DEFAULT_COLLATION_NAME', 'SQL_PATH']
        rows = [
            ('def', name, 'utf8mb4', 'utf8mb4_general_ci', None)
            for name in self.db.database_names()
        ]
        return cols, rows