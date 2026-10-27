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
        if t == 'EVENTS':
            return self._events()
        if t == 'TRIGGERS':
            return self._triggers()
        if t == 'ROUTINES':
            return self._routines()
        return [], []

    #: Navicat 备份页会查这三张表；列名与 MySQL 5.7/8.0 官方列一致。
    #: 本引擎的事件/触发器/存储过程元数据由 Executor 的 manager 持有
    #: （不在 Database 对象里），从 db 层读不到真实对象行；但"对象为空"
    #: 是合法真实状态（新建库确实没有这些对象），因此返回**完整列 + 空集**，
    #: 而不是 1146/1054 或编造行。
    EVENTS_COLUMNS = (
        'EVENT_CATALOG', 'EVENT_SCHEMA', 'EVENT_NAME', 'DEFINER', 'TIME_ZONE',
        'EVENT_BODY', 'EVENT_DEFINITION', 'EVENT_TYPE', 'EXECUTE_AT',
        'INTERVAL_VALUE', 'INTERVAL_FIELD', 'SQL_MODE', 'STARTS', 'ENDS',
        'STATUS', 'ON_COMPLETION', 'CREATED', 'LAST_ALTERED',
        'LAST_EXECUTED', 'EVENT_COMMENT', 'ORIGINATOR',
        'CHARACTER_SET_CLIENT', 'COLLATION_CONNECTION',
        'DATABASE_COLLATION')
    TRIGGERS_COLUMNS = (
        'TRIGGER_CATALOG', 'TRIGGER_SCHEMA', 'TRIGGER_NAME',
        'EVENT_MANIPULATION', 'EVENT_OBJECT_CATALOG',
        'EVENT_OBJECT_SCHEMA', 'EVENT_OBJECT_TABLE', 'ACTION_ORDER',
        'ACTION_CONDITION', 'ACTION_STATEMENT', 'ACTION_ORIENTATION',
        'ACTION_TIMING', 'ACTION_REFERENCE_OLD_TABLE',
        'ACTION_REFERENCE_NEW_TABLE', 'ACTION_REFERENCE_OLD_ROW',
        'ACTION_REFERENCE_NEW_ROW', 'CREATED', 'SQL_MODE', 'DEFINER',
        'CHARACTER_SET_CLIENT', 'COLLATION_CONNECTION',
        'DATABASE_COLLATION')
    ROUTINES_COLUMNS = (
        'SPECIFIC_NAME', 'ROUTINE_CATALOG', 'ROUTINE_SCHEMA',
        'ROUTINE_NAME', 'ROUTINE_TYPE', 'DATA_TYPE',
        'CHARACTER_MAXIMUM_LENGTH', 'CHARACTER_OCTET_LENGTH',
        'NUMERIC_PRECISION', 'NUMERIC_SCALE', 'DATETIME_PRECISION',
        'CHARACTER_SET_NAME', 'COLLATION_NAME', 'DTD_IDENTIFIER',
        'ROUTINE_BODY', 'ROUTINE_DEFINITION', 'EXTERNAL_NAME',
        'EXTERNAL_LANGUAGE', 'PARAMETER_STYLE', 'IS_DETERMINISTIC',
        'SQL_DATA_ACCESS', 'SQL_PATH', 'SECURITY_TYPE', 'CREATED',
        'LAST_ALTERED', 'SQL_MODE', 'ROUTINE_COMMENT', 'DEFINER',
        'CHARACTER_SET_CLIENT', 'COLLATION_CONNECTION',
        'DATABASE_COLLATION')

    def _events(self):
        """information_schema.EVENTS：完整 MySQL 列 + 空集（引擎无事件对象）"""
        return list(self.EVENTS_COLUMNS), []

    def _triggers(self):
        """information_schema.TRIGGERS：完整 MySQL 列 + 空集

        触发器真实定义通过 `SHOW TRIGGERS` / TriggerManager 暴露；这里保持
        "对象树为空"这一合法真实状态，绝不编造行。
        """
        return list(self.TRIGGERS_COLUMNS), []

    def _routines(self):
        """information_schema.ROUTINES：完整 MySQL 列 + 空集

        存储过程/函数真实定义通过 `SHOW PROCEDURE/FUNCTION STATUS` 暴露；
        Navicat 备份页的 `BINARY ROUTINE_SCHEMA='default'` 查询据此返回
        空集而不是 1146/1054。
        """
        return list(self.ROUTINES_COLUMNS), []

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