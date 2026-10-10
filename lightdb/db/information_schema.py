"""information_schema 元数据表"""
from typing import List, Tuple


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
        return [], []
    
    def _tables(self):
        cols = ['TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'TABLE_TYPE',
                'ENGINE', 'TABLE_ROWS', 'TABLE_COLLATION', 'TABLE_COMMENT']
        rows = []
        for name, tbl in self.db.tables.items():
            rows.append((
                'def', 'default', name, 'BASE TABLE',
                'InnoDB', tbl.row_count, 'utf8mb4_general_ci', ''
            ))
        return cols, rows
    
    def _columns(self):
        cols = ['TABLE_CATALOG', 'TABLE_SCHEMA', 'TABLE_NAME', 'COLUMN_NAME',
                'ORDINAL_POSITION', 'COLUMN_DEFAULT', 'IS_NULLABLE', 'DATA_TYPE',
                'COLUMN_KEY', 'EXTRA', 'COLUMN_COMMENT']
        rows = []
        for tname, tbl in self.db.tables.items():
            for pos, c in enumerate(tbl.cols, 1):
                rows.append((
                    'def', 'default', tname, c['name'],
                    pos, None,
                    'NO' if c.get('notnull') else 'YES',
                    c['type'].lower(),
                    'PRI' if c.get('primary') else 'UNI' if c.get('unique') else '',
                    'auto_increment' if c.get('primary') else '',
                    ''
                ))
        return cols, rows
    
    def _schemata(self):
        cols = ['CATALOG_NAME', 'SCHEMA_NAME', 'DEFAULT_CHARACTER_SET_NAME',
                'DEFAULT_COLLATION_NAME', 'SQL_PATH']
        rows = [
            ('def', 'default', 'utf8mb4', 'utf8mb4_general_ci', None),
            ('def', 'information_schema', 'utf8_general_ci', 'utf8_general_ci', None),
        ]
        return cols, rows