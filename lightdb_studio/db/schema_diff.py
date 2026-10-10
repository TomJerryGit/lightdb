"""
数据库结构对比
- 对比两张表：列、索引、主键
- 生成差异 SQL（ALTER）
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class ColumnDef:
    name: str = ''
    type: str = ''
    length: str = ''
    primary: bool = False
    unique: bool = False
    notnull: bool = False
    auto_increment: bool = False
    default: str = ''

    def signature(self):
        """用于快速比较的签名（忽略大小写）"""
        return (
            self.name.lower(),
            self.type.upper(),
            (self.length or '').upper(),
            self.primary,
            self.unique,
            self.notnull,
            self.auto_increment,
            (self.default or '').strip("'\"").lower(),
        )

    def sql_type(self):
        if self.length:
            return f"{self.type}({self.length})"
        return self.type


@dataclass
class TableDiff:
    table: str
    columns_added: List[ColumnDef] = field(default_factory=list)
    columns_removed: List[str] = field(default_factory=list)
    columns_changed: List[tuple] = field(default_factory=list)  # (old, new)
    pk_changed: bool = False
    pk_old: List[str] = field(default_factory=list)
    pk_new: List[str] = field(default_factory=list)

    @property
    def is_empty(self):
        return not (self.columns_added or self.columns_removed
                    or self.columns_changed or self.pk_changed)

    def to_sql(self, table_name: str) -> List[str]:
        """生成差异 ALTER 语句"""
        stmts = []
        qt = f"`{table_name}`"

        for c in self.columns_added:
            sql = f"ALTER TABLE {qt} ADD COLUMN `{c.name}` {c.sql_type()}"
            if c.notnull:
                sql += " NOT NULL"
            if c.auto_increment:
                sql += " AUTO_INCREMENT"
            if c.default:
                sql += f" DEFAULT {c.default}"
            if c.unique:
                sql += " UNIQUE"
            stmts.append(sql + ";")

        for name in self.columns_removed:
            stmts.append(f"ALTER TABLE {qt} DROP COLUMN `{name}`;")

        for old, new in self.columns_changed:
            stmts.append(
                f"ALTER TABLE {qt} MODIFY COLUMN `{new.name}` {new.sql_type()}"
                + (" NOT NULL" if new.notnull else "")
                + (" AUTO_INCREMENT" if new.auto_increment else "")
                + (f" DEFAULT {new.default}" if new.default else "")
                + ";"
            )

        if self.pk_changed:
            try:
                stmts.append(f"ALTER TABLE {qt} DROP PRIMARY KEY;")
            except Exception:
                pass
            if self.pk_new:
                cols = ', '.join(f"`{c}`" for c in self.pk_new)
                stmts.append(f"ALTER TABLE {qt} ADD PRIMARY KEY ({cols});")

        return stmts


class SchemaDiffer:
    """结构对比器"""

    @staticmethod
    def column_def_from_meta(meta: dict) -> ColumnDef:
        """从 SHOW COLUMNS 的结果转 ColumnDef"""
        name = meta.get('name', '')
        full_type = meta.get('type', '').strip()
        # 解析 VARCHAR(100) → type, length
        length = ''
        col_type = full_type
        if '(' in full_type:
            col_type, _, rest = full_type.partition('(')
            length = rest.rstrip(')')

        key = meta.get('key', '')
        return ColumnDef(
            name=name,
            type=col_type.strip().upper(),
            length=length,
            primary=(key == 'PRI'),
            unique=(key == 'UNI'),
            notnull=(meta.get('null', 'YES') == 'NO'),
            auto_increment=('auto_increment' in
                            (meta.get('extra', '') or '').lower()),
            default=meta.get('default', '') or '',
        )

    @staticmethod
    def diff_tables(cols_a: List[dict], cols_b: List[dict],
                    table_name: str) -> TableDiff:
        """
        cols_a: 源表结构
        cols_b: 目标表结构
        """
        td = TableDiff(table=table_name)

        map_a = {SchemaDiffer.column_def_from_meta(c).name.lower():
                 SchemaDiffer.column_def_from_meta(c) for c in cols_a}
        map_b = {SchemaDiffer.column_def_from_meta(c).name.lower():
                 SchemaDiffer.column_def_from_meta(c) for c in cols_b}

        for name, cb in map_b.items():
            if name not in map_a:
                td.columns_added.append(cb)
            elif map_a[name].signature() != cb.signature():
                td.columns_changed.append((map_a[name], cb))

        for name, ca in map_a.items():
            if name not in map_b:
                td.columns_removed.append(ca.name)

        # 主键变化
        pk_a = [c.name for c in map_a.values() if c.primary]
        pk_b = [c.name for c in map_b.values() if c.primary]
        if sorted(pk_a) != sorted(pk_b):
            td.pk_changed = True
            td.pk_old = pk_a
            td.pk_new = pk_b

        return td


def load_table_schema(conn, table_name: str) -> List[dict]:
    """加载表结构"""
    return conn.get_columns(table_name)


def diff_databases(conn_a, conn_b,
                   tables_a: List[str], tables_b: List[str]) -> dict:
    """
    对比两个数据库的所有表
    返回 {table_name: TableDiff}
    """
    result = {}
    all_tables = set(tables_a) | set(tables_b)

    for t in all_tables:
        cols_a = []
        cols_b = []
        if t in tables_a:
            try:
                cols_a = load_table_schema(conn_a, t)
            except Exception:
                pass
        if t in tables_b:
            try:
                cols_b = load_table_schema(conn_b, t)
            except Exception:
                pass

        td = SchemaDiffer.diff_tables(cols_a, cols_b, t)

        if t not in tables_a:
            # 只在 B 中存在 → 需要 DROP
            td.columns_removed = ['__TABLE_NOT_IN_A__']
        elif t not in tables_b:
            # 只在 A 中存在 → 需要 CREATE
            td.columns_added = [SchemaDiffer.column_def_from_meta(c)
                                for c in cols_a]

        if not td.is_empty or t not in tables_a or t not in tables_b:
            result[t] = td

    return result