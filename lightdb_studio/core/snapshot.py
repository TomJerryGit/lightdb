"""
数据库快照 / 克隆
- 快照：记录所有表的 CREATE TABLE + INSERT
- 克隆：从快照恢复到新库
- 支持批量导出
"""
import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Callable, Optional   # ★ 修复：补 Optional

SNAPSHOT_DIR = Path.home() / '.lightdb_studio' / 'snapshots'


@dataclass
class Snapshot:
    name: str = ""
    connection: str = ""
    database: str = ""
    tables: List[str] = None
    file: str = ""
    created_at: float = 0
    size: int = 0
    row_counts: dict = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if self.tables is None:
            self.tables = []
        if self.row_counts is None:
            self.row_counts = {}


class SnapshotManager:
    def __init__(self):
        self.dir = SNAPSHOT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshots: List[Snapshot] = []
        self._load_index()

    def _index_path(self):
        return self.dir / 'index.json'

    def _load_index(self):
        if not self._index_path().exists():
            return
        try:
            with open(self._index_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            for s in data.get('snapshots', []):
                self.snapshots.append(Snapshot(**s))
        except Exception:
            pass

    def _save_index(self):
        data = {'snapshots': [asdict(s) for s in self.snapshots]}
        tmp = self._index_path().with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._index_path())

    def create_snapshot(self, conn, name: str = None,
                        tables: List[str] = None,
                        progress_cb: Callable = None) -> Snapshot:
        if not name:
            name = f"snapshot_{time.strftime('%Y%m%d_%H%M%S')}"

        all_tables = conn.get_tables()
        if tables:
            all_tables = [t for t in all_tables if t in tables]

        snapshot_file = self.dir / f"{name}.sql"

        lines = [
            f"-- LightDB Studio Snapshot",
            f"-- Name: {name}",
            f"-- Created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"-- Connection: {conn.config.name}",
            f"-- Database: {conn.config.database}",
            "",
            "SET NAMES utf8mb4;",
            "",
        ]

        row_counts = {}

        total = len(all_tables)
        for i, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb(i, total, tname)

            try:
                cols, rows = conn.query(f"SHOW CREATE TABLE `{tname}`")
                if rows and len(rows[0]) > 1:
                    lines.append(f"-- Table: {tname}")
                    lines.append(f"DROP TABLE IF EXISTS `{tname}`;")
                    lines.append(rows[0][1] + ";")
                    lines.append("")
            except Exception:
                pass

            try:
                cols, rows = conn.query(f"SELECT * FROM `{tname}`")
                row_counts[tname] = len(rows)
                if rows:
                    cols_str = ', '.join(f"`{c}`" for c in cols)
                    for r in rows:
                        vals = ', '.join(self._sql_lit(v) for v in r)
                        lines.append(
                            f"INSERT INTO `{tname}` ({cols_str}) VALUES ({vals});")
                    lines.append("")
            except Exception:
                pass

        with open(snapshot_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        snap = Snapshot(
            name=name,
            connection=conn.config.name,
            database=conn.config.database or '',
            tables=all_tables,
            file=str(snapshot_file),
            size=os.path.getsize(snapshot_file),
            row_counts=row_counts,
        )
        self.snapshots.append(snap)
        self._save_index()
        return snap

    def _sql_lit(self, v):
        if v is None:
            return 'NULL'
        if isinstance(v, bool):
            return '1' if v else '0'
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, bytes):
            return "X'" + v.hex() + "'"
        s = str(v).replace('\\', '\\\\').replace("'", "''")
        return f"'{s}'"

    def restore_snapshot(self, conn, snapshot: Snapshot,
                         drop_existing: bool = False,
                         progress_cb: Callable = None) -> dict:
        if not os.path.exists(snapshot.file):
            return {'ok': False, 'error': '快照文件不存在'}

        with open(snapshot.file, 'r', encoding='utf-8') as f:
            content = f.read()

        statements = self._split_statements(content)
        total = len(statements)

        ok = 0
        failed = 0
        errors = []

        for i, stmt in enumerate(statements):
            if progress_cb:
                progress_cb(i, total, stmt[:60])

            stmt = stmt.strip()
            if not stmt or stmt.startswith('--'):
                continue

            if not drop_existing and stmt.upper().startswith('DROP TABLE'):
                continue

            try:
                _, err = conn.execute(stmt)
                if err:
                    failed += 1
                    if len(errors) < 5:
                        errors.append(f"{stmt[:60]}... → {err}")
                else:
                    ok += 1
            except Exception as e:
                failed += 1
                if len(errors) < 5:
                    errors.append(str(e))

        return {
            'ok': True,
            'executed': ok,
            'failed': failed,
            'errors': errors,
        }

    def _split_statements(self, content):
        stmts = []
        buf = ''
        in_str = None
        i = 0
        n = len(content)
        while i < n:
            ch = content[i]
            if in_str:
                buf += ch
                if ch == '\\' and i + 1 < n:
                    buf += content[i + 1]
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
            elif ch in ("'", '"'):
                in_str = ch
                buf += ch
                i += 1
            elif ch == '-' and i + 1 < n and content[i + 1] == '-':
                j = content.find('\n', i)
                i = j if j >= 0 else n
                buf += '\n'
            elif ch == ';':
                if buf.strip():
                    stmts.append(buf.strip())
                buf = ''
                i += 1
            else:
                buf += ch
                i += 1
        if buf.strip():
            stmts.append(buf.strip())
        return stmts

    def clone_database(self, source_conn, target_conn,
                       tables: List[str] = None,
                       drop_existing: bool = True,
                       progress_cb: Callable = None) -> dict:
        all_tables = source_conn.get_tables()
        if tables:
            all_tables = [t for t in all_tables if t in tables]

        total = len(all_tables)
        ok = 0
        failed = 0
        errors = []

        for i, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb(i, total, tname)

            try:
                if drop_existing:
                    target_conn.execute(f"DROP TABLE IF EXISTS `{tname}`")
                _, rows = source_conn.query(f"SHOW CREATE TABLE `{tname}`")
                if rows and len(rows[0]) > 1:
                    _, err = target_conn.execute(rows[0][1])
                    if err:
                        failed += 1
                        errors.append(f"Create {tname}: {err}")
                        continue
            except Exception as e:
                failed += 1
                errors.append(f"Create {tname}: {e}")
                continue

            try:
                cols, rows = source_conn.query(f"SELECT * FROM `{tname}`")
                if not rows:
                    ok += 1
                    continue

                cols_str = ', '.join(f"`{c}`" for c in cols)
                placeholders = ', '.join(['%s'] * len(cols))
                sql = (f"INSERT INTO `{tname}` ({cols_str}) "
                       f"VALUES ({placeholders})")

                batch_size = 500
                for j in range(0, len(rows), batch_size):
                    batch = rows[j:j + batch_size]
                    target_conn.execute_many(sql, batch)
                ok += 1
            except Exception as e:
                failed += 1
                errors.append(f"Data {tname}: {e}")

        return {
            'ok': True,
            'cloned': ok,
            'failed': failed,
            'errors': errors[:5],
        }

    def remove(self, name: str):
        for i, s in enumerate(self.snapshots):
            if s.name == name:
                try:
                    if os.path.exists(s.file):
                        os.unlink(s.file)
                except Exception:
                    pass
                del self.snapshots[i]
                self._save_index()
                return True
        return False

    def get(self, name: str) -> Optional[Snapshot]:   # ★ 修复：Optional 已 import
        for s in self.snapshots:
            if s.name == name:
                return s
        return None

    def list_all(self) -> List[Snapshot]:
        return sorted(self.snapshots, key=lambda x: -x.created_at)