"""
数据库迁移工具
- MySQL / SQLite → LightDB
- 结构映射
- 数据分批导入
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Callable


@dataclass
class MigrationStats:
    total_tables: int = 0
    total_rows: int = 0
    migrated_tables: int = 0
    migrated_rows: int = 0
    failed_tables: int = 0
    errors: List[str] = field(default_factory=list)
    start_time: float = 0
    end_time: float = 0


class TypeMapper:
    """MySQL → LightDB 类型映射"""

    MAPPING = {
        # 整数
        'TINYINT': ('INT', ''),
        'SMALLINT': ('INT', ''),
        'MEDIUMINT': ('INT', ''),
        'INT': ('INT', ''),
        'INTEGER': ('INT', ''),
        'BIGINT': ('BIGINT', ''),
        'SERIAL': ('INT', ''),
        'TINYINT(1)': ('BOOLEAN', ''),
        # 小数
        'DECIMAL': ('DECIMAL', '{length}'),
        'NUMERIC': ('DECIMAL', '{length}'),
        'FLOAT': ('FLOAT', ''),
        'DOUBLE': ('DOUBLE', ''),
        'REAL': ('DOUBLE', ''),
        # 字符
        'CHAR': ('VARCHAR', '{length}'),
        'VARCHAR': ('VARCHAR', '{length}'),
        'TINYTEXT': ('TEXT', ''),
        'TEXT': ('TEXT', ''),
        'MEDIUMTEXT': ('TEXT', ''),
        'LONGTEXT': ('TEXT', ''),
        'BLOB': ('TEXT', ''),
        'JSON': ('JSON', ''),
        # 时间
        'DATE': ('DATE', ''),
        'TIME': ('TEXT', ''),
        'DATETIME': ('DATETIME', ''),
        'TIMESTAMP': ('DATETIME', ''),
        'YEAR': ('INT', ''),
    }

    @staticmethod
    def map_type(mysql_type: str) -> str:
        """映射类型"""
        t = mysql_type.upper().strip()

        # 提取 (len) 或 (len, decimal)
        m = re.match(r'^(\w+)\s*\(([^)]+)\)', t)
        if m:
            base = m.group(1)
            params = m.group(2)
        else:
            base = t
            params = ''

        # 特殊处理 TINYINT(1) → BOOLEAN
        if base == 'TINYINT' and params == '1':
            return 'BOOLEAN'

        mapped = TypeMapper.MAPPING.get(base)
        if not mapped:
            return 'TEXT'

        new_type, fmt = mapped
        if fmt == '{length}' and params:
            return f"{new_type}({params})"
        return new_type


class MySQLMigrator:
    """从 MySQL 迁移到 LightDB"""

    def __init__(self, source_conn, target_conn):
        self.source = source_conn
        self.target = target_conn

    def preview(self, tables: List[str] = None) -> Dict:
        """预览迁移"""
        try:
            all_tables = self.source.get_tables()
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        if tables:
            all_tables = [t for t in all_tables if t in tables]

        preview = []
        for t in all_tables:
            try:
                cols = self.source.get_columns(t)
                _, r = self.source.query(
                    f"SELECT COUNT(*) FROM `{t}`")
                rows = r[0][0] if r else 0

                mapped_cols = []
                for c in cols:
                    new_type = TypeMapper.map_type(c['type'])
                    mapped_cols.append({
                        'name': c['name'],
                        'old_type': c['type'],
                        'new_type': new_type,
                        'key': c.get('key', ''),
                        'null': c.get('null', 'YES'),
                        'default': c.get('default'),
                        'extra': c.get('extra', ''),
                    })

                preview.append({
                    'table': t,
                    'rows': rows,
                    'columns': mapped_cols,
                })
            except Exception as e:
                preview.append({
                    'table': t,
                    'error': str(e),
                })

        return {
            'ok': True,
            'tables': preview,
            'total_tables': len(preview),
        }

    def migrate(self, tables: List[str] = None,
                create_tables: bool = True,
                copy_data: bool = True,
                drop_existing: bool = False,
                batch_size: int = 500,
                progress_cb: Callable = None) -> MigrationStats:
        """
        progress_cb(stage, current, total, message)
        stage: 'table' / 'data'
        """
        stats = MigrationStats(start_time=time.time())

        try:
            all_tables = self.source.get_tables()
        except Exception as e:
            stats.errors.append(f"无法获取源表列表: {e}")
            return stats

        if tables:
            all_tables = [t for t in all_tables if t in tables]

        stats.total_tables = len(all_tables)

        for ti, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb('table', ti, len(all_tables),
                            f"处理表: {tname}")

            # 1. 建表
            if create_tables:
                ok = self._create_table(tname, drop_existing)
                if not ok:
                    stats.failed_tables += 1
                    continue

            # 2. 复制数据
            if copy_data:
                n_ok, n_err = self._copy_data(
                    tname, batch_size, progress_cb)
                stats.migrated_rows += n_ok
                if n_err:
                    stats.errors.append(
                        f"{tname}: {n_err} 行失败")

            stats.migrated_tables += 1

        stats.end_time = time.time()
        return stats

    def _create_table(self, table: str, drop_existing: bool) -> bool:
        try:
            if drop_existing:
                self.target.execute(f"DROP TABLE IF EXISTS `{table}`")

            # 检查目标是否已存在
            try:
                self.target.get_columns(table)
                # 已存在
                return True
            except Exception:
                pass

            # 从源获取 CREATE TABLE
            _, rows = self.source.query(f"SHOW CREATE TABLE `{table}`")
            if not rows or len(rows[0]) < 2:
                return False

            create_sql = rows[0][1]

            # 转换 MySQL 特有语法
            create_sql = self._normalize_create_sql(create_sql)

            _, err = self.target.execute(create_sql)
            if err:
                # 尝试用我们自己生成的建表语句
                return self._create_table_fallback(table)

            return True
        except Exception as e:
            return False

    def _normalize_create_sql(self, sql: str) -> str:
        """规范化 CREATE TABLE 语句"""
        # 反引号转普通
        sql = re.sub(r'`([^`]+)`', r'\1', sql)

        # 去掉 ENGINE/CHARSET/COLLATE
        sql = re.sub(r'ENGINE\s*=\s*\w+', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'DEFAULT\s+CHARSET\s*=\s*\w+', '', sql,
                     flags=re.IGNORECASE)
        sql = re.sub(r'COLLATE\s*=\s*\w+', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'COLLATE\s+\w+', '', sql, flags=re.IGNORECASE)

        # AUTO_INCREMENT 处理
        sql = re.sub(r'AUTO_INCREMENT\s*=\s*\d+', '', sql,
                     flags=re.IGNORECASE)

        # 去掉 UNSIGNED
        sql = re.sub(r'\bUNSIGNED\b', '', sql, flags=re.IGNORECASE)

        # COMMENT 'xxx'
        sql = re.sub(r"COMMENT\s+'[^']*'", '', sql, flags=re.IGNORECASE)

        # 类型映射
        def type_repl(m):
            old = m.group(0)
            return TypeMapper.map_type(old)
        sql = re.sub(
            r'\b(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|'
            r'DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL|'
            r'CHAR|VARCHAR|TINYTEXT|TEXT|MEDIUMTEXT|LONGTEXT|'
            r'BLOB|JSON|DATE|TIME|DATETIME|TIMESTAMP|YEAR)'
            r'(\([^)]+\))?',
            type_repl, sql, flags=re.IGNORECASE)

        # KEY `idx_xxx` (...)  --- 简单索引，跳过
        sql = re.sub(r',\s*KEY\s+\w+\s*\([^)]+\)', '', sql,
                     flags=re.IGNORECASE)
        sql = re.sub(r',\s*UNIQUE\s+KEY\s+\w+\s*\([^)]+\)', '', sql,
                     flags=re.IGNORECASE)
        sql = re.sub(r',\s*INDEX\s+\w+\s*\([^)]+\)', '', sql,
                     flags=re.IGNORECASE)
        sql = re.sub(r',\s*CONSTRAINT\s+[^,)]+', '', sql,
                     flags=re.IGNORECASE)
        sql = re.sub(r',\s*FOREIGN\s+KEY\s*\([^)]+\)[^,)]*', '', sql,
                     flags=re.IGNORECASE)

        # 尾部清理
        sql = re.sub(r',\s*\)', ')', sql)

        return sql

    def _create_table_fallback(self, table: str) -> bool:
        """用列信息手动建表"""
        try:
            cols = self.source.get_columns(table)
            col_defs = []
            pk_cols = []
            for c in cols:
                new_type = TypeMapper.map_type(c['type'])
                line = f"  `{c['name']}` {new_type}"
                if c.get('null', 'YES') == 'NO':
                    line += " NOT NULL"
                if 'auto_increment' in (c.get('extra', '') or '').lower():
                    line += " AUTO_INCREMENT"
                if c.get('default') is not None:
                    d = str(c['default'])
                    if d.upper() != 'NULL':
                        line += f" DEFAULT '{d}'"
                if c.get('key') == 'PRI':
                    pk_cols.append(c['name'])
                col_defs.append(line)

            if pk_cols:
                col_defs.append(
                    f"  PRIMARY KEY ({', '.join('`' + c + '`' for c in pk_cols)})")

            sql = f"CREATE TABLE `{table}` (\n"
            sql += ',\n'.join(col_defs)
            sql += "\n)"

            _, err = self.target.execute(sql)
            return err is None
        except Exception:
            return False

    def _copy_data(self, table: str, batch_size: int,
                   progress_cb) -> tuple:
        """复制数据"""
        try:
            cols, rows = self.source.query(
                f"SELECT * FROM `{table}`")
        except Exception as e:
            return 0, 1

        if not rows:
            return 0, 0

        cols_str = ', '.join(f"`{c}`" for c in cols)
        placeholders = ', '.join(['%s'] * len(cols))
        sql = f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders})"

        ok = 0
        err = 0
        total = len(rows)
        for i in range(0, total, batch_size):
            batch = rows[i:i + batch_size]
            try:
                self.target.execute_many(sql, batch)
                ok += len(batch)
            except Exception as e:
                err += len(batch)

            if progress_cb:
                progress_cb('data', i + len(batch), total,
                            f"{table}: {i + len(batch)}/{total}")

        return ok, err


class SQLiteMigrator:
    """SQLite → LightDB"""

    def __init__(self, sqlite_path: str, target_conn):
        self.sqlite_path = sqlite_path
        self.target = target_conn

    def preview(self) -> Dict:
        import sqlite3
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            result = []
            for t in tables:
                cur.execute(f"PRAGMA table_info({t})")
                cols = [{'name': c[1], 'type': c[2]} for c in cur.fetchall()]
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                n = cur.fetchone()[0]
                result.append({'table': t, 'columns': cols, 'rows': n})
            conn.close()
            return {'ok': True, 'tables': result}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def migrate(self, tables: List[str] = None,
                progress_cb: Callable = None) -> MigrationStats:
        import sqlite3
        stats = MigrationStats(start_time=time.time())

        try:
            conn = sqlite3.connect(self.sqlite_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            all_tables = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception as e:
            stats.errors.append(str(e))
            return stats

        if tables:
            all_tables = [t for t in all_tables if t in tables]
        stats.total_tables = len(all_tables)

        for ti, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb('table', ti, len(all_tables), tname)

            # 建表
            try:
                conn = sqlite3.connect(self.sqlite_path)
                cur = conn.cursor()
                cur.execute(f"PRAGMA table_info({tname})")
                cols = cur.fetchall()
                col_defs = []
                for c in cols:
                    cname = c[1]
                    ctype = (c[2] or 'TEXT').upper()
                    new_type = {
                        'INTEGER': 'INT',
                        'INT': 'INT',
                        'REAL': 'DOUBLE',
                        'FLOAT': 'DOUBLE',
                        'TEXT': 'TEXT',
                        'VARCHAR': 'VARCHAR(255)',
                        'BLOB': 'TEXT',
                        'BOOL': 'BOOLEAN',
                        'BOOLEAN': 'BOOLEAN',
                    }.get(ctype.split('(')[0], 'TEXT')
                    line = f"`{cname}` {new_type}"
                    if c[3]:   # not null
                        line += " NOT NULL"
                    if c[5]:   # pk
                        line += " PRIMARY KEY"
                    col_defs.append(line)

                create_sql = f"CREATE TABLE `{tname}` (\n  "
                create_sql += ',\n  '.join(col_defs)
                create_sql += "\n)"
                self.target.execute(f"DROP TABLE IF EXISTS `{tname}`")
                self.target.execute(create_sql)

                # 复制数据
                cur.execute(f"SELECT * FROM {tname}")
                rows = cur.fetchall()
                if rows:
                    col_names = [c[1] for c in cols]
                    cols_str = ', '.join(f"`{c}`" for c in col_names)
                    placeholders = ', '.join(['%s'] * len(col_names))
                    insert_sql = (f"INSERT INTO `{tname}` ({cols_str}) "
                                  f"VALUES ({placeholders})")
                    batch = 500
                    for i in range(0, len(rows), batch):
                        chunk = rows[i:i + batch]
                        self.target.execute_many(insert_sql, chunk)

                    stats.migrated_rows += len(rows)

                conn.close()
                stats.migrated_tables += 1
            except Exception as e:
                stats.failed_tables += 1
                stats.errors.append(f"{tname}: {e}")

        stats.end_time = time.time()
        return stats