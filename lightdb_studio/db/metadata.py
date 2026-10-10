"""元数据查询封装"""
from typing import List, Dict, Optional


class MetadataLoader:
    def __init__(self, connection):
        self.conn = connection

    def databases(self) -> List[str]:
        try:
            return self.conn.get_databases()
        except Exception:
            return []

    def tables(self) -> List[str]:
        try:
            return self.conn.get_tables()
        except Exception:
            return []

    def columns(self, table: str) -> List[Dict]:
        try:
            return self.conn.get_columns(table)
        except Exception:
            return []

    def table_row_count(self, table: str) -> int:
        try:
            cols, rows = self.conn.query(
                f"SELECT COUNT(*) FROM `{table}`")
            return rows[0][0] if rows else 0
        except Exception:
            return 0

    def procedures(self) -> List[str]:
        try:
            cols, rows = self.conn.query("SHOW PROCEDURES")
            return [r[0] for r in rows]
        except Exception:
            return []

    def functions(self) -> List[str]:
        try:
            cols, rows = self.conn.query("SHOW FUNCTIONS")
            return [r[0] for r in rows]
        except Exception:
            return []

    def triggers(self) -> List[str]:
        try:
            cols, rows = self.conn.query("SHOW TRIGGERS")
            return [r[0] for r in rows]
        except Exception:
            return []

    def events(self) -> List[str]:
        try:
            cols, rows = self.conn.query("SHOW EVENTS")
            return [r[0] for r in rows]
        except Exception:
            return []

    def partitions(self, table: str) -> List[tuple]:
        try:
            cols, rows = self.conn.query(f"SHOW PARTITIONS FROM `{table}`")
            return rows
        except Exception:
            return []

    def create_table_sql(self, table: str) -> str:
        try:
            cols, rows = self.conn.query(f"SHOW CREATE TABLE `{table}`")
            if rows and len(rows[0]) > 1:
                return rows[0][1]
        except Exception:
            pass
        return ""