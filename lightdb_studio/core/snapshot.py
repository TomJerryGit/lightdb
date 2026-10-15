"""
数据库快照 / 克隆
- 快照：记录所有表的 CREATE TABLE + INSERT
- 克隆：从快照恢复到新库
- 支持批量导出

设计原则（静默失效修复）
------------------------
备份/恢复是数据安全路径：**宁可失败，也绝不产出"看起来成功"的快照**。

1. 任何底层语句失败（`SHOW CREATE TABLE` 报 1064/1146、`SELECT *` 失败、
   `SHOW COLUMNS` 失败……）都会记进失败清单，并让 `ok=False`；
2. 某张表拿不到 `CREATE TABLE` 时，要么用元数据重建（并在文件里显式标注
   "重建"），要么拒绝导出，绝不写空 schema 后报成功；
3. 写入前先做**自检**：`all_tables` 里每张表在文件里必须有对应的
   `CREATE TABLE`；缺一张就视为失败，绝不落盘、绝不进索引；
4. 文件先写 `.partial`，自检通过才原子改名成 `.sql`——失败时不留下
   半成品文件，也不往 index.json 里塞一条"成功"记录。
"""
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional   # ★ 修复：补 Optional

SNAPSHOT_DIR = Path.home() / '.lightdb_studio' / 'snapshots'

OP_SCHEMA = 'schema'
OP_DATA = 'data'
OP_LIST_TABLES = 'list_tables'


@dataclass
class SnapshotFailure:
    """失败清单里的一条记录（表名 / 语句 / 错误原文）。"""
    table: str = ""
    operation: str = OP_SCHEMA
    statement: str = ""
    error: str = ""
    errno: Optional[int] = None
    sqlstate: Optional[str] = None
    error_text: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d['error_text']:
            d['error_text'] = d['error']
        return d

    def __getitem__(self, key):
        # 兼容 dict 式访问：f['table'] / f['statement'] / f['error']
        return self.to_dict()[key]

    def __str__(self):
        head = f"[{self.operation}] {self.table or '(未指明表)'}"
        if self.statement:
            head += f" :: {self.statement}"
        return f"{head} → {self.error_text or self.error}"


@dataclass
class SnapshotResult:
    """快照创建结果。

    `ok` 为 True 当且仅当：没有失败清单、且自检确认每张表的 `CREATE TABLE`
    都真的写进了文件。
    """
    ok: bool = False
    snapshot: Optional['Snapshot'] = None
    error: str = ""
    failures: List[SnapshotFailure] = field(default_factory=list)
    incomplete_tables: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    file: str = ""
    create_table_counts: Dict[str, int] = field(default_factory=dict)
    missing_create_table: List[str] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            n = len(self.tables)
            return f"快照成功：{n} 张表，全部含 CREATE TABLE"
        lines = [f"快照失败：{self.error or '存在未导出成功的对象'}"]
        if self.incomplete_tables:
            lines.append('结构不完整的表: ' + ', '.join(self.incomplete_tables))
        for f in self.failures:
            lines.append('  - ' + str(f))
        return '\n'.join(lines)

    def __getitem__(self, key):
        # 兼容旧代码的 dict 式访问（executed/failed/errors 风格）
        if key in ('snapshot', 'name'):
            return self.snapshot
        if key == 'errors':
            return [str(f) for f in self.failures]
        return getattr(self, key)


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
    incomplete_tables: List[str] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if self.tables is None:
            self.tables = []
        if self.row_counts is None:
            self.row_counts = {}
        if self.incomplete_tables is None:
            self.incomplete_tables = []


def _fail(table, operation, statement, exc):
    """把底层异常压成一条失败记录（errno / sqlstate / 完整报文，不丢字段）。"""
    errno = getattr(exc, 'errno', None)
    sqlstate = getattr(exc, 'sqlstate', None)
    args = getattr(exc, 'args', None) or ()
    if not isinstance(errno, int) and args and isinstance(args[0], int):
        errno = args[0]
    if errno is not None and args and isinstance(args[0], int) and len(args) >= 2:
        message = str(args[1])
    elif args and isinstance(args[0], int):
        message = f'(错误码 {args[0]}，服务端未提供报文)'
    else:
        message = str(exc)
    errno = errno if isinstance(errno, int) else None
    sqlstate = str(sqlstate) if sqlstate else None

    parts = []
    if errno is not None:
        parts.append(f"错误码 {errno}")
    if sqlstate:
        parts.append(f"SQLSTATE {sqlstate}")
    parts.append(message)
    return SnapshotFailure(
        table=table or '',
        operation=operation,
        statement=statement or '',
        error=message,
        errno=errno,
        sqlstate=sqlstate,
        error_text=' | '.join(parts),
    )


class SnapshotManager:
    def __init__(self, dir: str = None):
        self.dir = Path(dir) if dir else SNAPSHOT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshots: List[Snapshot] = []
        self.index_warnings: List[str] = []
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
        except Exception as exc:
            # 索引损坏属于非关键路径（不影响新建快照），但绝不能静默：
            # 记进可读的告警清单并打印到 stderr，避免"快照列表空了"却无人知道。
            msg = f"快照索引 {self._index_path()} 读取失败: {exc}"
            self.index_warnings.append(msg)
            print(f"[snapshot] WARN {msg}", file=sys.stderr)

    def _save_index(self):
        data = {'snapshots': [asdict(s) for s in self.snapshots]}
        tmp = self._index_path().with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._index_path())

    # ==================== 结构自检 ====================

    @staticmethod
    def _count_create_table(text: str, tname: str) -> int:
        """统计文件里 `CREATE TABLE` 的条数。

        同时接受 ``CREATE TABLE `t``` 与 ``CREATE TABLE t``，大小写不敏感。
        """
        low = text.lower()
        a = f"create table `{tname.lower()}`"
        b = f"create table {tname.lower()}"
        n = low.count(a)
        if b != a:
            n += low.count(b)
        return n

    def validate_snapshot_file(self, path, tables: List[str]) -> List[str]:
        """自检：返回"文件里没有 CREATE TABLE"的表名清单（空 = 通过）。"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        except Exception as exc:
            return list(tables) + [f"<文件不可读: {exc}>"]
        return [t for t in tables
                if self._count_create_table(text, t) <= 0]

    # ==================== 创建快照 ====================

    def create_snapshot(self, conn, name: str = None,
                        tables: List[str] = None,
                        progress_cb: Callable = None,
                        strict: bool = True) -> SnapshotResult:
        """创建快照。

        `strict=True`（默认，数据安全路径）时返回 `SnapshotResult`：

        * `ok=True`   → 所有目标表都拿到了 `CREATE TABLE`，文件已落盘并入索引；
        * `ok=False`  → 有底层语句失败 / 有表拿不到结构 / 自检不通过；
                        此时**不落盘 .sql、不入索引**，失败清单在
                        `result.failures`、不完整的表在 `result.incomplete_tables`。

        返回 `SnapshotResult` 而不是抛异常，是为了让 GUI 能逐条展示失败清单；
        失败细节一律不丢。`strict=False` 保留旧调用习惯，返回 `Snapshot`
        （失败时抛 `RuntimeError`，绝不返回"成功"的快照对象）。
        """
        if not name:
            name = f"snapshot_{time.strftime('%Y%m%d_%H%M%S')}"

        failures: List[SnapshotFailure] = []
        incomplete: List[str] = []
        snapshot_file = self.dir / f"{name}.sql"

        def _result(ok, error='', snap=None, all_tables=None,
                    row_counts=None, out_file='', missing=None, warnings=None):
            counts = {}
            if out_file and os.path.exists(out_file):
                try:
                    with open(out_file, 'r', encoding='utf-8') as fh:
                        text = fh.read()
                    counts = {t: self._count_create_table(text, t)
                              for t in (all_tables or [])}
                except Exception as exc:
                    print(f"[snapshot] WARN 回读 {out_file} 计数失败: {exc}",
                          file=sys.stderr)
                    counts = {}
            return SnapshotResult(
                ok=ok, snapshot=snap, error=error, failures=list(failures),
                incomplete_tables=list(incomplete),
                tables=list(all_tables or []), file=out_file,
                create_table_counts=counts,
                missing_create_table=list(missing or []),
                # 失败时明确告诉调用者：什么文件不该有、索引在哪
                artifacts={'file': str(snapshot_file),
                           'index': str(self._index_path())},
                # 索引损坏之类的"非关键路径"问题也一并交给调用方，不静默
                warnings=list(warnings or []) + list(self.index_warnings),
            )

        # ---- 1. 取表清单（失败 = 整体失败，绝不产出空快照 ----）
        try:
            all_tables = list(conn.get_tables() or [])
        except Exception as exc:
            failures.append(_fail('', OP_LIST_TABLES, 'SHOW TABLES', exc))
            return _result(False, error=f"无法获取表清单: {exc}")
        if tables:
            wanted = list(tables)
            all_tables = [t for t in all_tables if t in wanted]
            for t in wanted:
                if t not in all_tables:
                    failures.append(SnapshotFailure(
                        table=t, operation=OP_SCHEMA,
                        statement=f"SHOW CREATE TABLE `{t}`",
                        error=f"表不存在或不在当前库: {t}",
                        error_text=f"表不存在或不在当前库: {t}"))
                    if t not in incomplete:
                        incomplete.append(t)

        lines = [
            "-- LightDB Studio Snapshot",
            f"-- Name: {name}",
            f"-- Created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"-- Connection: {getattr(conn.config, 'name', '')}",
            f"-- Database: {getattr(conn.config, 'database', '')}",
            "",
            "SET NAMES utf8mb4;",
            "",
        ]

        row_counts = {}

        total = len(all_tables)
        for i, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb(i, total, tname)

            # ---- 2. 结构（CREATE TABLE）：失败 → 该表标记不完整 ----
            ddl = None
            stmt_create = f"SHOW CREATE TABLE `{tname}`"
            try:
                _cols, rows = conn.query(stmt_create)
                if rows and len(rows[0]) > 1 and str(rows[0][1] or '').strip():
                    ddl = str(rows[0][1]).strip().rstrip(';')
                else:
                    failures.append(SnapshotFailure(
                        table=tname, operation=OP_SCHEMA, statement=stmt_create,
                        error='服务端未返回 CREATE TABLE 内容',
                        error_text='服务端未返回 CREATE TABLE 内容'))
            except Exception as exc:
                failures.append(_fail(tname, OP_SCHEMA, stmt_create, exc))

            # 兜底：语句不可用（老引擎 1064 / 表刚被删 1146）时用元数据重建，
            # 并在文件里显式标注"重建"，不假装是服务端原文。
            rebuild_note = None
            if ddl is None:
                try:
                    cols = conn.get_columns(tname)
                except Exception as exc:
                    cols = None
                    failures.append(
                        _fail(tname, OP_SCHEMA, f"SHOW COLUMNS FROM `{tname}`", exc))
                if cols:
                    ddl = (f"CREATE TABLE `{tname}` (\n  "
                           + ',\n  '.join(
                               f"`{c.get('name')}` {c.get('type')}"
                               + ('' if str(c.get('null', 'YES')).upper() == 'NO'
                                  else ' NULL')
                               for c in cols)
                           + '\n)')
                    rebuild_note = ("-- ⚠ 以下结构由 SHOW COLUMNS 元数据重建"
                                    "（服务端未提供 CREATE TABLE）")
                else:
                    if tname not in incomplete:
                        incomplete.append(tname)

            # ---- 3. 数据：失败 → 记失败清单（结构仍在，不算结构不完整） ----
            stmt_data = f"SELECT * FROM `{tname}`"
            try:
                cols, rows = conn.query(stmt_data)
            except Exception as exc:
                cols, rows = None, None
                failures.append(_fail(tname, OP_DATA, stmt_data, exc))

            # ---- 4. 落块 ----
            lines.append(f"-- Table: {tname}")
            if ddl is None:
                lines.append(f"-- ✗ 结构不完整：{tname} 未导出 CREATE TABLE")
            else:
                if rebuild_note:
                    lines.append(rebuild_note)
                lines.append(f"DROP TABLE IF EXISTS `{tname}`;")
                lines.append(ddl + ";")
            lines.append("")
            if rows is not None:
                row_counts[tname] = len(rows)
                if rows:
                    cols_str = ', '.join(f"`{c}`" for c in cols)
                    for r in rows:
                        vals = ', '.join(self._sql_lit(v) for v in r)
                        lines.append(
                            f"INSERT INTO `{tname}` ({cols_str}) VALUES ({vals});")
                    lines.append("")

        # ---- 5. 自检（写入前）：每张表都必须有 CREATE TABLE ----
        text = '\n'.join(lines)
        missing = [t for t in all_tables
                   if self._count_create_table(text, t) <= 0]
        for t in missing:
            if t not in incomplete:
                incomplete.append(t)

        if failures or incomplete or missing:
            reasons = []
            if failures:
                reasons.append(f"{len(failures)} 条底层语句失败")
            if incomplete:
                reasons.append(
                    "以下表拿不到 CREATE TABLE: " + ', '.join(incomplete))
            if missing:
                reasons.append(
                    "自检发现文件里缺少 CREATE TABLE: " + ', '.join(missing))
            error = "；".join(reasons) or "快照未通过完整性自检"
            if strict:
                return _result(False, error=error, all_tables=all_tables,
                               row_counts=row_counts, missing=missing)
            raise RuntimeError(
                f"快照 {name} 创建失败（未落盘）：{error}\n失败清单:\n"
                + '\n'.join('  - ' + str(f) for f in failures))

        # ---- 6. 原子落盘：先 .partial，再改名 ----
        partial = self.dir / f"{name}.sql.partial"
        try:
            with open(partial, 'w', encoding='utf-8') as f:
                f.write(text)
            partial.replace(snapshot_file)
        except Exception as exc:
            try:
                if partial.exists():
                    partial.unlink()
            except Exception as cleanup_exc:
                # 非关键路径：清理失败不影响 conclude，但必须留痕
                print(f"[snapshot] WARN 清理半成品文件 {partial} 失败: "
                      f"{cleanup_exc}", file=sys.stderr)
            failures.append(SnapshotFailure(
                table='', operation='write', statement=str(snapshot_file),
                error=f"写入快照文件失败: {exc}",
                error_text=f"写入快照文件失败: {exc}"))
            if strict:
                return _result(False, error=f"写入快照文件失败: {exc}",
                               all_tables=all_tables, row_counts=row_counts)
            raise

        # 写盘后复检：真读文件数 CREATE TABLE，防止"0 个 CREATE TABLE 却 ok=True"
        post_missing = self.validate_snapshot_file(snapshot_file, all_tables)
        if post_missing:
            try:
                snapshot_file.unlink()
            except Exception as unlink_exc:
                print(f"[snapshot] WARN 复检失败后删除 {snapshot_file} 失败: "
                      f"{unlink_exc}（该文件不完整，请手工删除）", file=sys.stderr)
            failures.append(SnapshotFailure(
                table='', operation='validate', statement=str(snapshot_file),
                error='落盘复检失败，缺少 CREATE TABLE: ' + ', '.join(post_missing),
                error_text='落盘复检失败，缺少 CREATE TABLE: '
                           + ', '.join(post_missing)))
            if strict:
                return _result(
                    False, error='落盘复检失败（快照已删除）',
                    all_tables=all_tables, row_counts=row_counts,
                    missing=post_missing)
            raise RuntimeError(
                f"快照 {name} 落盘复检失败，缺失 CREATE TABLE: {post_missing}")

        snap = Snapshot(
            name=name,
            connection=getattr(conn.config, 'name', ''),
            database=getattr(conn.config, 'database', '') or '',
            tables=all_tables,
            file=str(snapshot_file),
            size=os.path.getsize(snapshot_file),
            row_counts=row_counts,
            incomplete_tables=[],
        )
        self.snapshots.append(snap)
        try:
            self._save_index()
        except Exception as exc:
            # 索引写不进去 = 快照在列表里看不见，属于"静默失效"：
            # 回滚内存列表、删掉刚写的文件，并如实返回失败。
            self.snapshots.remove(snap)
            try:
                snapshot_file.unlink()
            except Exception as unlink_exc:
                print(f"[snapshot] WARN 索引失败后删除 {snapshot_file} 失败: "
                      f"{unlink_exc}", file=sys.stderr)
            failures.append(SnapshotFailure(
                table='', operation='index',
                statement=str(self._index_path()),
                error=f"写入快照索引失败（快照已回滚）: {exc}",
                error_text=f"写入快照索引失败（快照已回滚）: {exc}"))
            if strict:
                return _result(False, error=f"写入快照索引失败: {exc}",
                               all_tables=all_tables, row_counts=row_counts)
            raise
        if strict:
            return _result(True, snap=snap, all_tables=all_tables,
                           row_counts=row_counts, out_file=str(snapshot_file))
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
        """恢复快照。**任何一条语句失败 → ok=False**，失败清单完整返回。"""
        if not snapshot or not snapshot.file:
            return {'ok': False, 'error': '快照对象为空或缺少文件路径',
                    'executed': 0, 'failed': 0, 'errors': [], 'failures': []}
        if not os.path.exists(snapshot.file):
            return {'ok': False, 'error': f'快照文件不存在: {snapshot.file}',
                    'executed': 0, 'failed': 0, 'errors': [], 'failures': []}

        try:
            with open(snapshot.file, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as exc:
            return {'ok': False, 'error': f'快照文件读取失败: {exc}',
                    'executed': 0, 'failed': 0, 'errors': [], 'failures': []}

        statements = self._split_statements(content)
        total = len(statements)

        ok = 0
        failed = 0
        errors = []
        failures = []

        for i, stmt in enumerate(statements):
            if progress_cb:
                progress_cb(i, total, stmt[:60])

            stmt = stmt.strip()
            if not stmt or stmt.startswith('--'):
                continue

            if not drop_existing and stmt.upper().startswith('DROP TABLE'):
                continue

            try:
                _n, err = conn.execute(stmt)
                if err:
                    failed += 1
                    errors.append(f"{stmt[:120]} → {err}")
                    failures.append({'statement': stmt, 'error': str(err)})
                else:
                    ok += 1
            except Exception as e:
                failed += 1
                errors.append(f"{stmt[:120]} → {e}")
                failures.append({'statement': stmt, 'error': str(e)})

        result = {
            'ok': failed == 0,
            'executed': ok,
            'failed': failed,
            # 不再 [:5] 截断：失败清单必须完整，否则恢复"部分失败"看不全
            'errors': errors,
            'failures': failures,
        }
        if failed:
            result['error'] = f"{failed} 条语句失败（共 {ok + failed} 条执行）"
        return result

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
        try:
            all_tables = list(source_conn.get_tables() or [])
        except Exception as exc:
            return {'ok': False, 'error': f'无法获取源表清单: {exc}',
                    'cloned': 0, 'failed': 0, 'errors': [str(exc)],
                    'failures': [{'table': '', 'error': str(exc)}]}
        if tables:
            all_tables = [t for t in all_tables if t in tables]

        total = len(all_tables)
        ok = 0
        failed = 0
        errors = []
        failures = []

        for i, tname in enumerate(all_tables):
            if progress_cb:
                progress_cb(i, total, tname)

            try:
                if drop_existing:
                    _n, derr = target_conn.execute(
                        f"DROP TABLE IF EXISTS `{tname}`")
                    if derr:
                        failed += 1
                        errors.append(f"Drop {tname}: {derr}")
                        failures.append({'table': tname, 'operation': 'drop',
                                         'error': str(derr)})
                        continue
                _c, rows = source_conn.query(f"SHOW CREATE TABLE `{tname}`")
                if not rows or len(rows[0]) <= 1 or not str(rows[0][1] or '').strip():
                    failed += 1
                    errors.append(f"Create {tname}: 服务端未返回 CREATE TABLE")
                    failures.append({'table': tname, 'operation': 'schema',
                                     'error': '服务端未返回 CREATE TABLE'})
                    continue
                _n, err = target_conn.execute(rows[0][1])
                if err:
                    failed += 1
                    errors.append(f"Create {tname}: {err}")
                    failures.append({'table': tname, 'operation': 'schema',
                                     'error': str(err)})
                    continue
            except Exception as e:
                failed += 1
                errors.append(f"Create {tname}: {e}")
                failures.append({'table': tname, 'operation': 'schema',
                                 'error': str(e)})
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
                    _n, merr = target_conn.execute_many(sql, batch)
                    if merr:
                        raise RuntimeError(str(merr))
                ok += 1
            except Exception as e:
                failed += 1
                errors.append(f"Data {tname}: {e}")
                failures.append({'table': tname, 'operation': 'data',
                                 'error': str(e)})

        result = {
            'ok': failed == 0,
            'cloned': ok,
            'failed': failed,
            'errors': errors,
            'failures': failures,
        }
        if failed:
            result['error'] = f"{failed} 张表克隆失败（成功 {ok} 张）"
        return result

    def remove(self, name: str):
        for i, s in enumerate(self.snapshots):
            if s.name == name:
                try:
                    if os.path.exists(s.file):
                        os.unlink(s.file)
                except Exception as exc:
                    # 非关键路径：删除失败必须让调用方知道，不能"假装删掉了"
                    self.index_warnings.append(
                        f"删除快照文件失败 {s.file}: {exc}")
                    print(f"[snapshot] WARN 删除快照文件失败 {s.file}: {exc}",
                          file=sys.stderr)
                    return False
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
