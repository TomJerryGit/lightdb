"""
逻辑备份的核心：**dump 格式定义 + 解析 + 恢复**（唯一实现）

`lightdb.backup.backup.BackupManager`（测试与内部 API 入口）和
`lightdb.tools.dump`（命令行入口）都调用这里，避免"两套各自有缺陷的
dump/restore"（原实现正是这样：备份端不带 `DROP TABLE IF EXISTS`、
恢复端手写了个会把 `CREATE TABLE` 整条跳过的切分器）。

================= 格式规范（lightdb-logical-dump v1）=================

纯文本、UTF-8、字节顺序与值无关（可跨平台搬运）。语句以 `;` 结尾，
语句间可夹注释行（`--` 开头）。**解析方必须对未知语句显式报错**，绝不
静默跳过（原 bug 就是"INSERT 全部失败但流程继续、最终 0 行"）。

    -- lightdb-logical-dump v1
    -- server: LightDB
    -- generated: 2026-10-14T10:18:27
    -- tables: 2
    -- table: users rows=10
    DROP TABLE IF EXISTS users;
    CREATE TABLE users (
      id INT NOT NULL PRIMARY KEY,
      name VARCHAR NULL
    );
    -- lightdb:rows users 10
    INSERT INTO users VALUES (1, 'u0');
    ...
    -- table: orders rows=0
    DROP TABLE IF EXISTS orders;
    CREATE TABLE orders (
      oid INT NOT NULL PRIMARY KEY,
      amount DECIMAL NULL
    );
    -- lightdb:rows orders 0

标识符**不加反引号**（列名/表名限制在 `[A-Za-z_]\\w*`）；`DROP TABLE IF
EXISTS` + 裸 `CREATE TABLE` 是 mysqldump 默认组合，DROP 提供"重复恢复到
同一库"的幂等性。行数哨兵 `-- lightdb:rows <表> <行数>` 由备份端真实扫描
得出，是恢复端"读回校验"的期望值来源（`--no-data` 也写 0 行）。

已知限制（有意保留，不在本格式里承诺）：
1. **只保存主键索引**。本引擎的 BTree 就是主键索引，`SHOW COLUMNS` 的
   `Key=PRI/UNI` 之外的二级索引没有持久化表达；多列主键（KEY 行）直接
   报错而不是猜。
2. **`DEFAULT` / `AUTO_INCREMENT` / 生成列 / CHECK 约束不导出**：本引擎
   的行布局里没有这些元数据（`Table.cols` 只有 name/type/primary/unique/
   notnull/generated/check），导出会造出恢复端不认识的语法。
3. **字符串转义只认 `''`**（MySQL 的 `NO_BACKSLASH_ESCAPES` 风格），
   见 `sql_literal`；`\\` 按字面保留，因此与"反斜杠转义"模式的客户端
   dump 混用时反斜杠串不保证等价（会显式报错而不是静默改数据）。
4. **不做一致性快照**：dump 按主键顺序扫描，期间有并发写入时只能保证
   "每行都来自某个已提交版本"，不保证跨表的同一时间点（需要物理备份或
   复制停写来获得一致性）。
5. 表名/列名只支持 `[A-Za-z0-9_]`（`\\w+`）；含空格或点号的标识符不支持。
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..logger import logger

FORMAT_HEADER = '-- lightdb-logical-dump v1'
ROWS_MARKER = '-- lightdb:rows '
TABLE_MARKER = '-- table: '

# 表名/列名：本引擎的上限（不支持空格、点号、反引号本身）
_IDENT_RE = re.compile(r'^[A-Za-z_]\w*$')
_DDL_COL_RE = re.compile(r'^`?([A-Za-z_]\w*)`?\s+([A-Za-z_]\w*)\s*(.*)$')

_IGNORABLE_PREFIXES = (
    'SET ', 'USE ', 'BEGIN', 'COMMIT', 'ROLLBACK', 'START TRANSACTION',
    'LOCK TABLES', 'UNLOCK TABLES', 'DELIMITER', '/*!', 'SOURCE ',
)


class BackupError(Exception):
    """备份/恢复过程中的显式错误（格式、解析、执行、校验、损坏）"""


class RestoreError(BackupError):
    """恢复失败：dump 里的语句执行出错或校验不一致

    恢复失败必然是**致命**的：部分恢复的库既不是旧状态也不是新状态，
    继续跑下去只会把"0 行的成功"留给调用方（原 bug）。
    """


# ==================== 值 ↔ SQL 字面量 ====================

def sql_literal(v) -> str:
    """Python 值 → SQL 字面量（与 `Parser._unquote_literal` 严格互逆）

    - None → `NULL`
    - bool → `1`/`0`（本引擎 BOOLEAN 的存储形态就是 0/1）
    - int → 十进制；float → `repr`（保精度、可回读成同一个 float）
    - bytes → 本格式不支持（见下）
    - str → 单引号包裹，`'` → `''`（唯一转义）；**反斜杠、换行、制表符
      一律按字面保留**，解析端同样按字面读回。因此"含换行的 VARCHAR"
      在 dump 文件里就是真的跨行文本。

    反斜杠不做转义是**有意的**（对齐 SQLite / MySQL
    `NO_BACKSLASH_ESCAPES`）：如果备份端把 `\\` 写成 `\\\\` 而解析端
    按字面读，恢复出来就会多一个反斜杠（`C:\\tmp` → `C:\\\\tmp`）；
    反过来如果备份端不转义而解析端当转义处理，就会丢反斜杠
    （`C:\\tmp` → `C:tmp`）。两端统一按字面，往返才是恒等的。
    """
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            raise BackupError(f"无法导出非有限浮点值: {v!r}")
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        raise BackupError(
            f"备份端遇到 bytes 值 {bytes(v)[:32]!r}：本引擎的行载荷是 JSON，"
            f"不支持二进制字面量（请先用 VARCHAR 存十六进制）")
    return "'" + str(v).replace("'", "''") + "'"


# ==================== 表结构 ====================

@dataclass
class TableSchema:
    """一张表的结构（dump 里 CREATE TABLE 的规范化表达）"""
    name: str
    columns: list = field(default_factory=list)   # [{name,type,primary,unique,notnull}]

    @property
    def col_names(self):
        return [c['name'] for c in self.columns]

    @property
    def pk_name(self):
        for c in self.columns:
            if c.get('primary'):
                return c['name']
        return self.columns[0]['name'] if self.columns else None

    def as_engine_cols(self):
        """转成 `Database.create_table` / `Table` 认识的 cols 结构"""
        return [
            {'name': c['name'], 'type': c['type'],
             'primary': bool(c.get('primary')),
             'unique': bool(c.get('unique')),
             'notnull': bool(c.get('notnull')),
             'generated': None, 'check': None}
            for c in self.columns
        ]

    @classmethod
    def from_engine_cols(cls, name, cols):
        return cls(name=name, columns=[
            {'name': c['name'], 'type': normalize_type(c['type']),
             'primary': bool(c.get('primary')),
             'unique': bool(c.get('unique')),
             'notnull': bool(c.get('notnull'))}
            for c in cols
        ])

    def canonical_ddl(self, if_not_exists: bool = False) -> str:
        """规范 DDL（默认**不带** `IF NOT EXISTS`，与 mysqldump 一致）

        幂等由前面的 `DROP TABLE IF EXISTS` 提供；写成裸 `CREATE TABLE`
        还有个硬约束：`tests/test_batch8.py::test_logical_backup` 断言
        备份文本里含 `CREATE TABLE users`（不能改成 `CREATE TABLE IF
        NOT EXISTS users`）。
        """
        parts = []
        for c in self.columns:
            parts.append(f"  {c['name']} {c['type']}"
                         + (' NOT NULL' if c.get('notnull') else ' NULL')
                         + (' PRIMARY KEY' if c.get('primary') else ''))
        head = ('CREATE TABLE IF NOT EXISTS ' if if_not_exists
                else 'CREATE TABLE ')
        return f"{head}{self.name} (\n" + ',\n'.join(parts) + "\n)"

    def assert_supported(self):
        if not _IDENT_RE.match(self.name):
            raise BackupError(f"表名 {self.name!r} 含不支持的字符")
        if not self.columns:
            raise BackupError(f"表 {self.name} 没有列")
        for c in self.columns:
            if not _IDENT_RE.match(c['name']):
                raise BackupError(
                    f"表 {self.name} 的列名 {c['name']!r} 含不支持的字符")
            if c['type'].upper() not in _KNOWN_TYPES:
                raise BackupError(
                    f"表 {self.name} 的列 {c['name']} 类型 {c['type']!r} "
                    f"不在支持的集合 {sorted(_KNOWN_TYPES)} 内")
        if not any(c.get('primary') for c in self.columns):
            raise BackupError(f"表 {self.name} 没有主键，无法按主键扫描备份")


_KNOWN_TYPES = {'INT', 'BIGINT', 'FLOAT', 'DOUBLE', 'DECIMAL', 'BOOLEAN',
                'VARCHAR', 'TEXT', 'DATETIME', 'DATE', 'JSON', 'BLOB'}

# 类型别名/归一化，与 `Parser._normalize_type` 完全一致：
# 备份文本里写 `VARCHAR(50)` 还是 `VARCHAR` 必须归一成同一个类型，
# 否则"dump 的列类型"与"恢复后表的列类型"永远比不相等。
_TYPE_ALIASES = {
    'INTEGER': 'INT', 'SMALLINT': 'INT',
    'TINYINT': 'INT', 'MEDIUMINT': 'INT',
    'SERIAL': 'INT',
    'BIGINT': 'BIGINT',
    'NUMERIC': 'DECIMAL', 'REAL': 'DOUBLE',
    'CHAR': 'VARCHAR',
    'LONGTEXT': 'TEXT', 'MEDIUMTEXT': 'TEXT',
    'TINYTEXT': 'TEXT',
    'BOOL': 'BOOLEAN', 'TIMESTAMP': 'DATETIME',
}


def normalize_type(t: str) -> str:
    """`VARCHAR(50)` / `integer` / `bool` → `VARCHAR` / `INT` / `BOOLEAN`"""
    t = re.sub(r'\(.*?\)', '', str(t)).strip().upper()
    return _TYPE_ALIASES.get(t, t)


def parse_create_table(stmt: str) -> TableSchema:
    """解析 `CREATE TABLE [IF NOT EXISTS] t ( ... );` → TableSchema

    服务端自己产出的 DDL（`TableSchema.canonical_ddl`）必须能原样解析回来，
    否则恢复端"照抄 dump"就会丢结构。未知的行内语法在这里显式报错。

    实现说明：不用"表名后必须跟 `(`"的单条正则——`(?:IF NOT EXISTS)?`
    是可选的，正则引擎会先在 `IF` 上匹配表名 `\\w+`（实测表名被解析成
    `IF`）。这里改成"定位第一个顶层 `(`"，再在其**前面**取表名。
    """
    head_m = re.match(r'CREATE\s+TABLE\b', stmt, re.IGNORECASE)
    if not head_m:
        raise BackupError(f"无法解析的 CREATE TABLE: {stmt[:80]!r}")

    # 第一个不在引号内的 '('
    open_idx = None
    for i, ch, quoted in _scan_quotes(stmt):
        if not quoted and ch == '(':
            open_idx = i
            break
    if open_idx is None:
        raise BackupError(f"CREATE TABLE 缺少列定义括号: {stmt[:80]!r}")

    prefix = stmt[head_m.end():open_idx].strip()
    # 去掉 `IF NOT EXISTS`，再去掉可选的 `db.` 前缀与反引号
    prefix = re.sub(r'^IF\s+NOT\s+EXISTS\b', '', prefix,
                    flags=re.IGNORECASE).strip()
    prefix = re.sub(r'^(`?[A-Za-z_]\w*`?)\s*\.', '', prefix).strip()
    name = prefix.strip('`').strip()
    if not _IDENT_RE.match(name):
        raise BackupError(
            f"CREATE TABLE 的表名 {name!r} 无法解析（只支持 [A-Za-z_]\\w*）")

    # 配对的右括号（引号内的括号不计）
    depth = 0
    body = None
    for i, ch, quoted in _scan_quotes(stmt):
        if quoted or i < open_idx:
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                body = stmt[open_idx + 1:i]
                break
    if body is None:
        raise BackupError(f"CREATE TABLE 括号不匹配: {stmt[:80]!r}")

    columns = []
    for part in _split_top_level(body):
        part = part.strip()
        if not part:
            continue
        cm = _DDL_COL_RE.match(part)
        if not cm:
            # 表级约束行（`PRIMARY KEY (a, b)`）：多列主键本格式不支持，
            # 显式报错而不是忽略掉一个约束
            up = part.upper()
            if up.startswith('PRIMARY KEY') and ',' in part:
                raise BackupError(
                    f"表 {name} 使用多列主键，lightdb-logical-dump v1 不支持")
            if up.startswith(('PRIMARY KEY', 'UNIQUE', 'KEY', 'INDEX')):
                raise BackupError(
                    f"表 {name} 的索引/约束行 {part[:40]!r} 无法表达为"
                    f"列属性（本格式只保存主键）")
            raise BackupError(f"表 {name} 里有无法解析的列定义: {part[:60]!r}")

        cname, ctype, tail = cm.group(1), normalize_type(cm.group(2)), cm.group(3)
        tup = tail.upper()
        unknown = [t for t in re.findall(r'[A-Z_]+', tup)
                   if t not in ('NOT', 'NULL', 'PRIMARY', 'KEY', 'UNIQUE')]
        if unknown:
            raise BackupError(
                f"表 {name} 的列 {cname} 带本格式不支持的属性 {unknown}")
        columns.append({
            'name': cname, 'type': ctype,
            'primary': 'PRIMARY KEY' in tup,
            'unique': 'UNIQUE' in tup,
            'notnull': bool(re.search(r'\bNOT\s+NULL\b', tup)),
        })

    schema = TableSchema(name=name, columns=columns)
    schema.assert_supported()
    return schema


def _scan_quotes(text: str):
    """产出 (下标, 字符, 是否位于引号内)

    转义规则与 `Parser._scan_quotes` / `Parser._unquote_literal` 一致：
    唯一转义是**成对引号**（`''` / `""`），**反斜杠按字面**（不转义）。
    两端不一致就会在"字符串里有反斜杠"时切错语句边界（dump 的
    `'C:\\tmp'` 被当转义后，后面的 `;` 判定就会错位）。

    反引号包住标识符，内部不做成对转义（MySQL 用 ` `` ` 转义反引号，
    但本引擎的标识符不允许反引号，故简化处理）。
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"', '`'):
            q = ch
            j = i + 1
            while j < n:
                if text[j] == q:
                    if j + 1 < n and text[j + 1] == q and q != '`':
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                yield k, text[k], True
            i = j + 1
            continue
        yield i, ch, False
        i += 1


def _split_top_level(text: str, sep: str = ',') -> list:
    """按 sep 切分，忽略括号内 / 引号内的 sep"""
    out, buf, depth = [], [], 0
    for _i, ch, quoted in _scan_quotes(text):
        if quoted:
            buf.append(ch)
            continue
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == sep and depth == 0:
            out.append(''.join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append(''.join(buf))
    return out


# ==================== 语句切分 ====================

def split_statements(content: str) -> list:
    """把 dump 文本切成 SQL 语句列表（去掉纯注释块）

    **这是原 bug 的核心之一**：老实现按 `;` 切完之后对每条 `if
    stmt.startswith('--'): continue`，而 dump 里 `CREATE TABLE` 前面正好
    有一行 `-- ========== Table: users ==========`，于是建表语句被整条
    丢掉，后面的 INSERT 全部报"表 users 不存在"，10 条全失败、恢复成
    0 行。

    现在的规则：
    - 引号内的 `;` 不切分（含多行字符串）；
    - 语句**前导**的注释行被剥掉，剩下的语句照常执行；
    - 纯注释块单独返回（`is_comment` 标记），供调用方统计/展示；
    - `;` 之后的同一行残留文本（如 `; -- 注释`）会并入下一条的前导注释。
    """
    out = []
    buf = []
    quote = None
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and content[i + 1] == quote and quote != '`':
                    buf.append(content[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ';':
            _emit(out, ''.join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    _emit(out, ''.join(buf))
    return [(s, False) for s in out]


def _first_sql_line(content: str):
    """返回 `content` 里第一条「不在引号内且非注释」数据的行首下标

    没有这样的行（整块都是注释/空白）时返回 None。
    """
    i, n = 0, len(content)
    while i < n:
        if content[i] in ' \t\r\n':
            i += 1
            continue
        if content[i:i + 2] == '--':
            while i < n and content[i] != '\n':
                i += 1
            continue
        return i
    return None


def _emit(out: list, raw: str):
    """剥掉语句**前导的整行注释**，把语句/纯注释块放进结果

    两个必须按"整行 + 引号感知"判断的理由：
    1. 整行：`-- table: users rows=10\\nDROP TABLE IF EXISTS users` 里
       `-- table: users rows=10\\nDRO` 也满足 `--[^\\n]*\\n`（正则不需要
       吃满整行），于是 `P TABLE ...` 被当成剩余语句输出，DROP/首条
       INSERT 被整条丢掉——恢复出来就是"整表没建/少一行"。
    2. 引号感知：dump 里的 VARCHAR 可以真含换行，字符串**内部**的行
       （可能以 `--` 开头）绝不能被当注释剥掉，否则语句被切碎、数据截断。
    """
    if not raw.strip():
        return
    first = _first_sql_line(raw)
    if first is None:                     # 整块都是注释/空白
        out.append(raw.strip())
        return
    head = raw[:first]
    if head.strip():
        out.append(head.strip())
    out.append(raw[first:].strip())


# ==================== 行迭代 / 表结构读取 ====================

def iter_rows(tbl):
    """按主键顺序产出表的全部目标版本行值（list）"""
    for _pk, pid, sid in tbl.bt.scan():
        page = tbl.bp.get_page(pid)
        rb = page.get_row(sid) if page is not None else None
        row = tbl._decode_row(rb)
        if row is None:
            # 索引项指向的行读不出来 = 存储层损坏。备份工具**不能**当作
            # "这行不存在"跳过（那正是静默丢数据的形态），必须显式失败。
            raise BackupError(
                f"表 {tbl.name} 的主键索引项 (page={pid}, slot={sid}) "
                f"指向的行无法解码，数据文件可能损坏")
        yield row


def count_rows(tbl) -> int:
    n = 0
    for _ in iter_rows(tbl):
        n += 1
    return n


def table_schemas(db) -> list:
    """目标库全部表的 TableSchema（按表名排序，dump 结果稳定可比对）"""
    out = []
    for name in sorted(db.tables):
        out.append(TableSchema.from_engine_cols(name, db.tables[name].cols))
    return out


# ==================== 生成 dump ====================

def render_dump(schemas, row_provider, no_data: bool = False,
                no_create: bool = False, source: str = 'LightDB',
                include_header: bool = True) -> str:
    """把"结构 + 行提供者"渲染成 lightdb-logical-dump v1 文本

    `row_provider(schema) -> 可迭代行`。服务器端（`BackupManager` 直连
    `Database`）与命令行端（`lightdb.tools.dump` 走 pymysql 结果集）
    共用这一份渲染逻辑，避免两边转义/顺序不一致。

    顺序（恢复端可以依赖）：每张表 `DROP TABLE IF EXISTS` → `CREATE
    TABLE` → 行数哨兵注释 → 全部 INSERT。`DROP` 提供幂等，所以
    `CREATE` 不必写 `IF NOT EXISTS`（也与 mysqldump 默认输出一致）。
    """
    lines = []
    if include_header:
        lines.append(FORMAT_HEADER)
        lines.append(f"-- server: {source}")
        lines.append(f"-- generated: {datetime.now().isoformat()}")
        lines.append(f"-- tables: {len(schemas)}")
        lines.append("")

    for schema in schemas:
        schema.assert_supported()
        rows = [] if no_data else list(row_provider(schema))
        # 只导数据时也要如实报告行数，供恢复后校验
        lines.append(f"{TABLE_MARKER}{schema.name} rows={len(rows)}")
        if not no_create:
            lines.append(f"DROP TABLE IF EXISTS {schema.name};")
            lines.append(schema.canonical_ddl() + ';')
        # 行数哨兵**始终**写（含 `--no-data` 的 0 行与空表）：它就是恢复端
        # "读回校验"的唯一期望值来源，缺了它就无法区分"空表"和"dump 被截断"
        lines.append(f"{ROWS_MARKER}{schema.name} {len(rows)}")
        for r in rows:
            vals = ', '.join(sql_literal(v) for v in r)
            lines.append(f"INSERT INTO {schema.name} VALUES ({vals});")
        lines.append("")

    return '\n'.join(lines) + '\n'


def build_dump(db, tables=None, no_data: bool = False,
               no_create: bool = False, source: str = 'LightDB',
               include_header: bool = True) -> str:
    """从本地 `Database` 对象生成 dump 文本（服务端/内部 API 用）"""
    schemas = table_schemas(db)
    if tables:
        want = set(tables)
        missing = want - {s.name for s in schemas}
        if missing:
            raise BackupError(f"要导出的表不存在: {sorted(missing)}")
        schemas = [s for s in schemas if s.name in want]

    return render_dump(
        schemas, lambda s: iter_rows(db.tables[s.name]),
        no_data=no_data, no_create=no_create,
        source=source, include_header=include_header)


# ==================== 解析 dump ====================

@dataclass
class DumpStatement:
    sql: str
    comment: bool = False
    table: str = None
    schema: TableSchema = None

    @property
    def command(self):
        up = self.sql.lstrip().upper()
        for cmd in ('DROP TABLE', 'CREATE TABLE', 'INSERT INTO'):
            if up.startswith(cmd):
                return cmd
        return up.split(None, 1)[0] if up.split() else ''


@dataclass
class DumpPlan:
    """一份 dump 的"预演"结果：结构 + 语句 + 期望行数"""
    statements: list = field(default_factory=list)
    order: list = field(default_factory=list)          # 建表顺序
    schemas: dict = field(default_factory=dict)        # name -> TableSchema
    expected_rows: dict = field(default_factory=dict)  # name -> int
    dropped: set = field(default_factory=set)
    version: str = None
    ignorable: int = 0

    @property
    def tables(self):
        return list(self.order)


def parse_dump(content: str) -> DumpPlan:
    """解析 dump → DumpPlan（**不执行**，只做格式/引用完整性校验）

    恢复端先整份预演再执行，是为了在动手之前就发现"INSERT 指向没有
    CREATE 的表"这类结构性错误（原实现是边跑边把错误吞掉）。
    """
    plan = DumpPlan()
    for raw, _ in split_statements(content):
        stmt = raw.strip()
        if not stmt:
            continue
        if FORMAT_HEADER in stmt and stmt.splitlines()[0].strip() == FORMAT_HEADER:
            plan.version = FORMAT_HEADER.split()[-1]
            plan.statements.append(DumpStatement(stmt, comment=True))
            continue
        if stmt.startswith('--'):
            plan.statements.append(DumpStatement(stmt, comment=True))
            continue

        up = stmt.lstrip().upper()
        if up.startswith('DROP TABLE'):
            name = stmt
            m = re.search(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?'
                          r'`?([A-Za-z_]\w*)`?', stmt, re.IGNORECASE)
            if not m:
                raise BackupError(f"无法解析的 DROP TABLE: {stmt[:80]!r}")
            plan.dropped.add(m.group(1))
            plan.statements.append(DumpStatement(stmt, table=m.group(1)))
            continue
        if up.startswith('CREATE TABLE'):
            schema = parse_create_table(stmt)
            if schema.name not in plan.schemas:
                plan.order.append(schema.name)
            plan.schemas[schema.name] = schema
            plan.expected_rows.setdefault(schema.name, 0)
            plan.statements.append(DumpStatement(stmt, table=schema.name,
                                                 schema=schema))
            continue
        if up.startswith('INSERT'):
            m = re.match(r'INSERT\s+INTO\s+(?:`?[A-Za-z_]\w*`?\s*\.\s*)?'
                         r'`?([A-Za-z_]\w*)`?', stmt, re.IGNORECASE)
            if not m:
                raise BackupError(f"无法解析的 INSERT: {stmt[:80]!r}")
            name = m.group(1)
            if name not in plan.schemas and name not in plan.dropped:
                raise BackupError(
                    f"dump 结构错误: INSERT 指向表 {name}，但整份 dump 里"
                    f"没有它的 CREATE TABLE（恢复会全部失败）")
            plan.expected_rows[name] = plan.expected_rows.get(name, 0) + 1
            plan.statements.append(DumpStatement(stmt, table=name))
            continue
        if up.startswith(('ALTER TABLE', 'CREATE INDEX', 'CREATE UNIQUE INDEX')):
            raise BackupError(
                f"dump 含本版本不支持的语句（索引/约束需人工处理）: "
                f"{stmt[:80]!r}")
        if up.startswith(_IGNORABLE_PREFIXES):
            plan.ignorable += 1
            plan.statements.append(DumpStatement(stmt, comment=True))
            continue
        if up.startswith('--') or not up:
            plan.statements.append(DumpStatement(stmt, comment=True))
            continue
        raise BackupError(f"dump 含无法识别的语句: {stmt[:100]!r}")

    # 行数哨兵（`-- lightdb:rows t N`）覆盖逐条 INSERT 的统计：
    # 它来自备份端真实的扫描行数，恢复后必须逐表对齐。
    for line in content.splitlines():
        s = line.strip()
        if s.startswith(ROWS_MARKER):
            parts = s[len(ROWS_MARKER):].split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise BackupError(f"行数哨兵格式错误: {s!r}")
            plan.expected_rows[parts[0]] = int(parts[1])
    return plan


# ==================== 恢复 ====================

@dataclass
class VerifyReport:
    """恢复后的读回校验结果"""
    rows: dict = field(default_factory=dict)      # name -> 恢复后行数
    checks: list = field(default_factory=list)    # 文本化的逐项结论
    ok: bool = True
    errors: list = field(default_factory=list)

    def add(self, ok, text):
        self.checks.append(('OK   ' if ok else 'FAIL ') + text)
        if not ok:
            self.ok = False
            self.errors.append(text)


def restore_dump(db, dump_file: str = None, content: str = None,
                 executor=None, strict: bool = True,
                 verify: bool = True, drop_extra: bool = True):
    """把 dump 恢复到 `db`：**先建表再灌数据**，执行完读回校验

    参数
    ----
    db        : 目标 `Database`（调用方负责 shutdown）
    dump_file : dump 路径；与 `content` 二选一
    executor  : 可复用已有 `Executor`（默认新建，`_no_binlog=True` 执行）
    strict    : True（默认）→ 任何失败/校验不一致抛 `RestoreError`；
                False → 返回报告（给"尽力恢复"的交互式场景用，测试不用）
    verify    : 恢复后读回校验（行数、主键、字段值、列结构）
    drop_extra: dump 里没提到的表在恢复前先 DROP（幂等，避免残留旧数据）

    返回 `(ok_count, fail_count, report)`
    """
    if content is None:
        if not dump_file:
            raise BackupError("必须给出 dump_file 或 content")
        with open(dump_file, 'r', encoding='utf-8') as f:
            content = f.read()

    plan = parse_dump(content)

    from .executor import Executor
    ex = executor if executor is not None else Executor(db)

    restore_tables = list(plan.order)
    if drop_extra:
        for name in sorted(set(db.tables) - set(restore_tables)):
            logger.info(f"恢复: 目标库里多出的表 {name} 先删除")
            db.drop_table(name)

    ok = fail = 0
    errors = []
    for ds in plan.statements:
        if ds.comment:
            continue
        try:
            parsed = _parse_sql(ds.sql)
        except Exception as e:                      # noqa: BLE001
            fail += 1
            errors.append(f"{ds.command} 解析失败: {e} :: {ds.sql[:120]}")
            continue
        try:
            r = ex.execute(parsed, {'_no_binlog': True})
        except Exception as e:                      # noqa: BLE001
            fail += 1
            errors.append(f"{ds.command} 执行异常: {e} :: {ds.sql[:120]}")
            continue
        if isinstance(r, dict) and r.get('type') == 'error':
            fail += 1
            errors.append(
                f"{ds.command} 失败[{r.get('code')}]: {r.get('message')} "
                f":: {ds.sql[:120]}")
        else:
            ok += 1

    report = None
    if fail == 0 and verify:
        report = verify_restore(db, plan)
        if not report.ok and strict:
            raise RestoreError(
                f"恢复到 {db.data_dir} 后校验失败（{len(report.errors)} 项）:\n"
                + '\n'.join(report.errors))

    if fail and strict:
        detail = '\n'.join(errors[:5])
        more = f"\n… 另有 {len(errors) - 5} 条失败" if len(errors) > 5 else ''
        raise RestoreError(
            f"恢复失败: {fail} 条语句未执行成功（ok={ok}）。恢复到 "
            f"{db.data_dir} 的内容不完整，已中止。首个错误:\n{detail}{more}")
    return ok, fail, report


def _parse_sql(stmt: str):
    from .parser import parser
    return parser.parse(stmt)


def verify_restore(db, plan: DumpPlan) -> VerifyReport:
    """读回校验：行数、主键、逐字段值、列结构

    校验项（与原库比对的是"dump 里声明的结构/行数"，dump 本身由
    `build_dump` 从原库读出——测试里再拿原库对象做一次比对）：
    1. 表存在，且列名/类型与 dump 的 CREATE TABLE 完全一致（顺序也一致）；
    2. 行数 == 哨兵行数（空表 = 0 也要对上）；
    3. 主键（BTree）条目数 == 行数，且主键值非 NULL、互不重复；
    4. 逐行字段值都能解码（含 NULL / 含引号与换行的字符串不被截断）。
    """
    rep = VerifyReport()
    for name in plan.order:
        schema = plan.schemas[name]
        want = plan.expected_rows.get(name, 0)
        if name not in db.tables:
            rep.add(False, f"表 {name}: 恢复后不存在")
            continue
        tbl = db.tables[name]

        got_cols = [(c['name'], normalize_type(c['type'])) for c in tbl.cols]
        want_cols = [(c['name'], normalize_type(c['type']))
                     for c in schema.columns]
        rep.add(got_cols == want_cols,
                f"表 {name}: 列结构 {'一致' if got_cols == want_cols else '不一致'}"
                f" 期望={want_cols} 实际={got_cols}")

        rows = list(iter_rows(tbl))
        rep.rows[name] = len(rows)
        rep.add(len(rows) == want,
                f"表 {name}: 行数 {len(rows)} == 期望 {want}")

        bt_entries = len(list(tbl.bt.scan()))
        rep.add(bt_entries == len(rows),
                f"表 {name}: 主键索引条目 {bt_entries} == 数据行 {len(rows)}")

        pk_idx = [i for i, c in enumerate(tbl.cols) if c.get('primary')]
        pk_idx = pk_idx[0] if pk_idx else 0
        seen = set()
        bad_pk = null_pk = 0
        for r in rows:
            pk = r[pk_idx]
            if pk is None:
                null_pk += 1
            elif pk in seen:
                bad_pk += 1
            else:
                seen.add(pk)
        rep.add(null_pk == 0 and bad_pk == 0,
                f"表 {name}: 主键唯一且非空（NULL={null_pk} 重复={bad_pk}）")
    return rep
