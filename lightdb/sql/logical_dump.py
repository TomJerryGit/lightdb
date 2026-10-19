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
      name VARCHAR NULL,
      age INT NULL CHECK (age >= 0),
      CONSTRAINT users_chk_2 CHECK (name <> '')
    );
    -- lightdb:rows users 10
    -- lightdb:check users name=users_chk_1 expr=age >= 0
    -- lightdb:check users name=users_chk_2 expr=name <> ''
    -- lightdb:checks users 2
    INSERT INTO users VALUES (1, 'u0', 20);
    ...
    -- table: orders rows=0
    DROP TABLE IF EXISTS orders;
    CREATE TABLE orders (
      oid INT NOT NULL PRIMARY KEY,
      amount DECIMAL NULL
    );
    -- lightdb:rows orders 0
    -- lightdb:checks orders 0

标识符**不加反引号**（列名/表名限制在 `[A-Za-z_]\\w*`）；`DROP TABLE IF
EXISTS` + 裸 `CREATE TABLE` 是 mysqldump 默认组合，DROP 提供"重复恢复到
同一库"的幂等性。行数哨兵 `-- lightdb:rows <表> <行数>` 由备份端真实扫描
得出，是恢复端"读回校验"的期望值来源（`--no-data` 也写 0 行）。

**CHECK 约束**同样有哨兵（`-- lightdb:check` / `-- lightdb:checks`），与
`-- lightdb:rows` 是同一个设计：备份端把**真实读出来的**约束清单写成注释，
恢复端拿去和"dump 里 CREATE TABLE 解析出来的约束"对账。为什么要两处都写：
`CREATE TABLE` 是给人看、能被手工编辑的文本，而"手改 dump 删掉一条 CHECK"
是**必须能被发现**的静默失效（恢复端只看 CREATE TABLE 的话，"dump 少了
约束"和"原本就没有约束"完全无法区分）。`-- lightdb:checks <表> <条数>`
是截断/篡改的第二道哨兵（条数与逐条声明不符 → 报错）。

    格式：`-- lightdb:check <表> name=<名字> expr=<表达式原文>`
    解析：`name=` 到 ` expr=` 之间是名字；` expr=` 之后到行尾是表达式
    （表达式里的空格原样保留，末尾空白忽略）；一行一条、顺序即引擎里的
    顺序（列级在前、表级在后）。

已知限制（有意保留，不在本格式里承诺）：
1. **只保存主键索引**。本引擎的 BTree 就是主键索引，`SHOW COLUMNS` 的
   `Key=PRI/UNI` 之外的二级索引没有持久化表达；多列主键（KEY 行）直接
   报错而不是猜。
2. **`DEFAULT` / `AUTO_INCREMENT` / 生成列不导出**：本引擎的行布局里没有
   这些元数据（`Table.cols` 只有 name/type/primary/unique/notnull/
   generated/check），导出会造出恢复端不认识的语法。
2b. **CHECK 约束导出**（2026-10 修复，原先登记为"不导出"）：列级 CHECK
   内联在列定义里（`price INT NULL CHECK (price > 0)`），表级 CHECK 写成
   `CONSTRAINT <名字> CHECK (<expr>)` 行。
   **依据（已核对 MySQL 8.0 文档）**：MySQL 8.0.16+ 的 `CREATE TABLE`
   确实接受**列内联**的列级约束（`c1 INT CHECK (c1 > 10)`、
   `c2 INT CONSTRAINT c2_positive CHECK (c2 > 0)`），因此内联写法的
   **语法合法**；而 MySQL 的 `SHOW CREATE TABLE` 会把列级与表级**统一**
   渲染成末尾的 `CONSTRAINT ... CHECK` 行，逐条按"在语句里出现的顺序"
   编号（`t1_chk_1`…）。本引擎选择**内联**列级，理由有二：① 列级约束
   本来就存在 `cols[i]['check']` 里，内联让 dump 文本自证"这条约束属于
   哪一列"，恢复端不必猜一个匿名约束该挂到哪一列；② 与 MySQL 渲染成
   表级相比，内联在**语义上等价**（两条路径都进 `Table.check_constraints()`
   并逐行求值），只在文本形态上不同。两种形态的**出现顺序都保持
   "列级在前、表级在后"**，自动编号也用同一份 `checks.resolve_check_names`
   （顺序编号 `表名_chk_n`），所以 dump 侧与恢复后引擎侧的约束名字逐字符
   一致。
   恢复端把两者都交给
   `Database.create_table(checks=...)` / `cols[i]['check']` 重建，并**读回
   校验**约束集合与表达式（不一致 → `RestoreError`，而不是"恢复成功但
   约束没了"）。
   `CHECK (...) NOT ENFORCED` 仍**显式拒绝**（见 `parser._parse_check`）：
   本引擎的 CHECK 一律强制，不会因为要导出就放宽。
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
CHECK_MARKER = '-- lightdb:check '
CHECKS_COUNT_MARKER = '-- lightdb:checks '

# 表名/列名：本引擎的上限（不支持空格、点号、反引号本身）
_IDENT_RE = re.compile(r'^[A-Za-z_]\w*$')
# 列定义：`列名 类型[(参数)] 其余属性`。
# ★ 参数（`VARCHAR(20)` / `DECIMAL(10,2)`）必须单独捕获：旧正则的类型组是
#   `[A-Za-z_]\w*`，`(20)` 会掉进"其余属性"里被当成未知属性忽略 —— 于是
#   dump 里声明的长度在解析侧静默消失（`canonical_ddl` 现在会写出真实
#   长度，这个洞必须一起补上）。
_DDL_COL_RE = re.compile(
    r'^`?([A-Za-z_]\w*)`?\s+([A-Za-z_]\w*)\s*(\([^)]*\))?\s*(.*)$')

#: dump 里的**表级键声明**行：`[CONSTRAINT 名字] PRIMARY KEY (列...)` /
#: `[CONSTRAINT 名字] UNIQUE [KEY|INDEX] [索引名] (列...)`。
#: `canonical_ddl` 自己写出的是 `UNIQUE KEY 名字 (列...)`，但手写/外部
#: 生成的 dump 也可能用 `UNIQUE INDEX` / `UNIQUE` / `CONSTRAINT` 形态；
#: 这些是同一份语义，解析端必须都认，否则"备份能写、恢复读不回"。
_DDL_KEY_ROW_RE = re.compile(
    r'^(?:CONSTRAINT\s+`?([A-Za-z_]\w*)`?\s+)?'
    r'(PRIMARY\s+KEY|UNIQUE(?:\s+(?:KEY|INDEX))?)'
    r'\s*(?:`?([A-Za-z_]\w*)`?\s*)?'
    r'\((.*)\)\s*$', re.IGNORECASE | re.DOTALL)

#: `CONSTRAINT ...` / `PRIMARY KEY ...` / `UNIQUE ...` 的**词边界**前缀
#: （`unique_col INT` 这种列名不能被当成表级键声明）。
_DDL_CONSTRAINT_RE = re.compile(r'^CONSTRAINT\b', re.IGNORECASE)
_DDL_KEY_PREFIX_RE = re.compile(r'^(?:PRIMARY\s+KEY|UNIQUE)\b', re.IGNORECASE)
_DDL_UNSUPPORTED_INDEX_RE = re.compile(
    r'^(?:KEY\b|INDEX\b|FULLTEXT\b|SPATIAL\b|FOREIGN\s+KEY\b)',
    re.IGNORECASE)

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

#: 无引号 DEFAULT 值里的函数调用（`CURRENT_TIMESTAMP` / `NOW()` /
#: `CURRENT_TIMESTAMP(3)`）—— 与 `sql/parser.py::_DEFAULT_FUNC_RE` 是同一份
#: 取值范围。DDL 文本里这类默认值**绝不能加引号**：`DEFAULT 'NOW()'` 是
#: 一个字符串常量，而 `DEFAULT NOW()` 是"写入时求值"，语义完全不同。
_DEFAULT_FUNC_RE = re.compile(
    r'^(?:CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME|NOW|LOCALTIME|'
    r'LOCALTIMESTAMP|UUID|CURDATE|CURTIME)\s*(?:\(\s*\d*\s*\))?$',
    re.IGNORECASE)

#: 数字字面量（不加引号写出来）
_NUM_LITERAL_RE = re.compile(r'^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$')


def _default_literal(col) -> str:
    """把列的真实 `DEFAULT` 渲染成**能被解析回来**的 SQL 字面量

    三种形态（与 `parser._parse_column_default` 接受的范围一一对应）：

    * `None` → `NULL`；
    * 数字原文（解析器存的是字符串）→ 不加引号写出；
    * 当前时间函数一族 → 不加引号写出（加引号会变成字符串常量）；
    * 其余 → 单引号包裹（`'` 按 SQL 规则写成 `''`）。
    """
    raw = col.get('default')
    if raw is None:
        return 'NULL'
    text = str(raw)
    if _DEFAULT_FUNC_RE.match(text):
        return text.upper()
    if _NUM_LITERAL_RE.match(text):
        return text
    return "'" + text.replace("'", "''") + "'"


@dataclass
class TableSchema:
    """一张表的结构（dump 里 CREATE TABLE 的规范化表达）

    `columns` 里的每项可有 `check`（列级 CHECK：`{'name','expr'}`）；
    `table_checks` 是表级 CHECK（`CONSTRAINT <name> CHECK (<expr>)`）。

    ⚠️ CHECK 是**约束**不是"列属性"：老版本这里既没有 `check` 也没有
    `table_checks`，于是 `SHOW CREATE TABLE` / 逻辑 dump 都只写列定义，
    "备份 → 恢复到新库"会把约束静默丢光（同会话一直在清的那类静默失效）。
    """
    name: str
    columns: list = field(default_factory=list)   # [{name,type,primary,unique,notnull,has_default,default,check}]
    table_checks: list = field(default_factory=list)  # [{'name','expr'}]
    #: 表级 `UNIQUE KEY 名字 (列...)`（**真实强制**的唯一约束，不是列属性）。
    #: 本引擎没有独立唯一索引结构，唯一性由写入路径检查强制；这里把它
    #: 原样写回 DDL，恢复后仍然强制（`SHOW INDEX` 报 UNIQUE-CHECK）。
    unique_indexes: list = field(default_factory=list)  # [{'name','columns'}]

    @property
    def col_names(self):
        return [c['name'] for c in self.columns]

    @property
    def pk_name(self):
        for c in self.columns:
            if c.get('primary'):
                return c['name']
        return self.columns[0]['name'] if self.columns else None

    def check_specs(self):
        """全部 CHECK，列级在前、表级在后（与 `Table.check_constraints()` 同序）

        ⚠️ 不能改顺序：恢复端拿它和引擎里的约束逐项比对，顺序不一致会
        把"集合相同"误判成"约束不一致"。
        """
        out = []
        for c in self.columns:
            ck = c.get('check') or {}
            if ck.get('expr'):
                out.append({'name': ck.get('name') or '',
                            'expr': str(ck['expr']).strip()})
        for ck in self.table_checks:
            if ck.get('expr'):
                out.append({'name': ck.get('name') or '',
                            'expr': str(ck['expr']).strip()})
        return out

    def as_engine_cols(self):
        """转成 `Database.create_table` / `Table` 认识的 cols 结构"""
        return [
            {'name': c['name'], 'type': c['type'],
             'primary': bool(c.get('primary')),
             'unique': bool(c.get('unique')),
             'notnull': bool(c.get('notnull')),
             # ★ `DEFAULT` 必须跟着走：`has_default` 区分"声明了 DEFAULT
             #   NULL"与"根本没写 DEFAULT"（`SHOW COLUMNS` 的 Default 列
             #   两者都是 NULL，但 `_meta.json` 里的 `has_default` 不同，
             #   丢了它就把"显式 NULL 默认值"降级成"没有默认值"）。
             'has_default': bool(c.get('has_default')),
             'default': c.get('default'),
             'generated': None,
             # 列级 CHECK 必须原样带回 `cols`：它本来就随列定义持久化
             # （`_meta.json` 的 cols），丢了就等于恢复后少一条约束。
             'check': ({'name': c['check']['name'],
                        'expr': c['check']['expr']}
                       if (c.get('check') or {}).get('expr') else None)}
            for c in self.columns
        ]

    @classmethod
    def from_engine_cols(cls, name, cols, checks=None, unique_indexes=None):
        columns = []
        for c in cols:
            ck = c.get('check') or {}
            columns.append({
                'name': c['name'],
                # ★ 保留**真实声明**（`VARCHAR(20)` / `DECIMAL(10,2)`）。
                #   旧实现在这里套了 `normalize_type`，把长度/精度抹掉，
                #   于是 `SHOW CREATE TABLE` 报的是 `VARCHAR`、而
                #   `SHOW COLUMNS` 报的是 `VARCHAR(20)`：同一张表两个答案，
                #   照抄 DDL 去重建表就会静默丢掉长度。
                #   `normalize_type` 仍然在**解析/比对**侧使用（`parse_create_table`
                #   与 `verify_restore`），因此本改动不会让比对口径漂移。
                'type': str(c.get('type') or ''),
                'primary': bool(c.get('primary')),
                'unique': bool(c.get('unique')),
                'notnull': bool(c.get('notnull')),
                'has_default': bool(c.get('has_default')),
                'default': c.get('default'),
                'check': ({'name': ck.get('name') or '',
                           'expr': str(ck['expr']).strip()}
                          if ck.get('expr') else None),
            })
        table_checks = []
        for ck in (checks or []):
            if isinstance(ck, dict) and ck.get('expr'):
                table_checks.append({'name': ck.get('name') or '',
                                     'expr': str(ck['expr']).strip()})
        uq = []
        for spec in (unique_indexes or []):
            if not isinstance(spec, dict):
                continue
            uq_cols = [str(c) for c in (spec.get('columns') or [])]
            if not uq_cols:
                continue
            uq.append({'name': str(spec.get('name') or uq_cols[0]),
                       'columns': uq_cols})
        schema = cls(name=name, columns=columns, table_checks=table_checks,
                     unique_indexes=uq)
        # 用与 `Database.create_table` 完全相同的一份命名规则补全自动名：
        # 否则"dump 里的匿名约束"与"恢复后引擎自动命名的约束"名字对不上，
        # 恢复校验会把一次正确恢复误判成失败。
        return _assign_check_names(schema)

    def canonical_ddl(self, if_not_exists: bool = False) -> str:
        """规范 DDL（默认**不带** `IF NOT EXISTS`，与 mysqldump 一致）

        幂等由前面的 `DROP TABLE IF EXISTS` 提供；写成裸 `CREATE TABLE`
        还有个硬约束：`tests/test_batch8.py::test_logical_backup` 断言
        备份文本里含 `CREATE TABLE users`（不能改成 `CREATE TABLE IF
        NOT EXISTS users`）。

        CHECK 的落位（`SHOW CREATE TABLE` 与逻辑 dump **共用这一份**）：

        * 列级 → 内联在该列定义的末尾：`price INT NULL CHECK (price > 0)`。
          MySQL 8.0.16+ 的 `CREATE TABLE` 接受列内联的列级约束
          （`c1 INT CHECK (c1 > 10)`），语法合法；而 MySQL 的
          `SHOW CREATE TABLE` 会把列级与表级**统一**渲染到末尾的
          `CONSTRAINT ... CHECK` 行。本引擎选择内联，是因为列级约束本来
          就存在 `cols[i]['check']` 里 —— 内联让 dump/DDL 文本自证"这条
          约束属于哪一列"，恢复端不必猜匿名约束该挂到哪一列（语义与
          MySQL 等价，只是文本形态不同）。
        * 表级 → 显式 `CONSTRAINT <name> CHECK (<expr>)` 行（名字来自
          `Database.create_table` 的规范化命名，与 dump 侧同一份规则）。
        * 两者都保持"**列级在前、表级在后**"，与
          `Table.check_constraints()` 的顺序一致，自动编号同规则。

        表级 `UNIQUE KEY` 写在列定义之后、CHECK 之前：
        `UNIQUE KEY <名字> (<列>, ...)`。它是**真强制**的约束（写入路径
        唯一性检查），不写出来就等于"备份 → 恢复"把唯一性丢掉。列级
        `col INT UNIQUE` 仍按列属性内联（保持原有行为，它是另一类声明）。

        没有任何 CHECK / UNIQUE KEY 时输出与旧版本**逐字节相同**（老 dump
        的解析/恢复行为不变）。
        """
        parts = []
        for c in self.columns:
            # 列定义的属性顺序按 MySQL 的 `SHOW CREATE TABLE` 习惯：
            #   `类型` → `NOT NULL|NULL` → `DEFAULT` → `PRIMARY KEY`
            #   → `UNIQUE` → `CHECK (...)`。
            # ★ 类型来自 `from_engine_cols` 的**真实声明**（含长度/精度）；
            # ★ DEFAULT / UNIQUE 也必须写出来：旧实现只写
            #   NULL/NOT NULL/PRIMARY KEY，于是 `SHOW CREATE TABLE` 与
            #   逻辑 dump 都**看不到** DEFAULT 与 UNIQUE —— 照它重建表会
            #   静默丢掉默认值和唯一性声明（同会话一直在清的那类失效）。
            line = (f"  {c['name']} {c['type']}"
                    + (' NOT NULL' if c.get('notnull') else ' NULL'))
            if c.get('has_default'):
                line += ' DEFAULT ' + _default_literal(c)
            if c.get('primary'):
                line += ' PRIMARY KEY'
            if c.get('unique'):
                line += ' UNIQUE'
            ck = c.get('check') or {}
            if ck.get('expr'):
                line += f" CHECK ({ck['expr']})"
            parts.append(line)
        # 表级 UNIQUE 约束（真强制）：必须写回 DDL，否则恢复后唯一性
        # 静默消失。名字/列名都在 `assert_supported` 里校验过能原样解析。
        for uq in self.unique_indexes:
            cols_sql = ', '.join(str(c) for c in (uq.get('columns') or []))
            parts.append(f"  UNIQUE KEY {uq['name']} ({cols_sql})")
        # 表级约束的自动名序号要**接在列级之后**（MySQL 的 `表名_chk_n`
        # 按约束在语句里的出现顺序编号），否则 dump 里写出的名字与恢复后
        # 引擎重新编号的名字会错位。
        n = sum(1 for c in self.columns if (c.get('check') or {}).get('expr'))
        for ck in self.table_checks:
            if not ck.get('expr'):
                continue
            n += 1
            cname = ck.get('name') or f"{self.name}_chk_{n}"
            parts.append(f"  CONSTRAINT {cname} CHECK ({ck['expr']})")
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
            # 按**基类型**判定：`VARCHAR(20)` / `DECIMAL(10,2)` 的参数是
            # 真实声明的一部分（必须保留），不能因为带了参数就被判成
            # "未知类型"。
            if normalize_type(c['type']) not in _KNOWN_TYPES:
                raise BackupError(
                    f"表 {self.name} 的列 {c['name']} 类型 {c['type']!r} "
                    f"不在支持的集合 {sorted(_KNOWN_TYPES)} 内")
        for spec in self.check_specs():
            if not str(spec['expr'] or '').strip():
                raise BackupError(
                    f"表 {self.name} 有一条空表达式的 CHECK 约束，"
                    f"无法导出（导出空约束等于恢复出一张没有约束的表）")
            if not _check_ident_ok(spec['name']):
                raise BackupError(
                    f"表 {self.name} 的 CHECK 约束名 {spec['name']!r} 含"
                    f"不支持的字符（只支持 [A-Za-z_]\\w*）")
        col_names = {c['name'] for c in self.columns}
        seen_uq = set()
        for spec in self.unique_indexes:
            if not _check_ident_ok(spec.get('name')):
                raise BackupError(
                    f"表 {self.name} 的 UNIQUE 索引名 {spec.get('name')!r} 含"
                    f"不支持的字符（只支持 [A-Za-z_]\\w*，否则 dump 无法"
                    f"往返）")
            low = str(spec.get('name') or '').lower()
            if low == 'primary' or low in seen_uq:
                raise BackupError(
                    f"表 {self.name} 的 UNIQUE 索引名 {spec.get('name')!r} "
                    f"重复或占用了 PRIMARY（无法原样恢复）")
            seen_uq.add(low)
            if not spec.get('columns'):
                raise BackupError(
                    f"表 {self.name} 的 UNIQUE 索引 {spec.get('name')!r} "
                    f"没有列，无法导出（导出空约束等于恢复出不强制唯一性"
                    f"的表）")
            if len(set(spec['columns'])) != len(spec['columns']):
                raise BackupError(
                    f"表 {self.name} 的 UNIQUE 索引 {spec.get('name')!r} "
                    f"列清单里有重复列 {spec['columns']}（无法原样恢复）")
            for cn in spec['columns']:
                if cn not in col_names:
                    raise BackupError(
                        f"表 {self.name} 的 UNIQUE 索引 {spec.get('name')!r} "
                        f"引用了不存在的列 {cn!r}，无法导出")
        if not any(c.get('primary') for c in self.columns):
            raise BackupError(f"表 {self.name} 没有主键，无法按主键扫描备份")


_KNOWN_TYPES = {'INT', 'BIGINT', 'FLOAT', 'DOUBLE', 'DECIMAL', 'BOOLEAN',
                'VARCHAR', 'TEXT', 'DATETIME', 'DATE', 'JSON', 'BLOB'}


def _check_ident_ok(name, table: str = '') -> bool:
    """CHECK 约束名/列名是否能用本格式的裸标识符写出

    约束名走的是**同一套**标识符规则（`[A-Za-z_]\\w*`，不加反引号）：
    dump 文本里的 `CONSTRAINT a b CHECK ...` 在解析端会被当成"表名后多余
    内容"，与其恢复时才发现，不如导出时就显式报错。
    """
    return bool(_IDENT_RE.match(str(name or '')))


def _assign_check_names(schema: 'TableSchema') -> 'TableSchema':
    """按 `checks.resolve_check_names` 的规则补全缺失的约束名

    必须用**同一份实现**（`sql/checks.py`）命名，否则"dump 侧算出的名字"
    与"恢复后引擎里实际的名字"会出现偏差，校验就会把一次正确恢复判成失败。
    """
    from .checks import resolve_check_names
    # 传到 `resolve_check_names` 的是**副本**，它只改副本里的名字
    cols = [{'name': c['name'],
             'check': (dict(c['check']) if (c.get('check') or {}).get('expr')
                       else None)}
            for c in schema.columns]
    table_checks = [dict(ck) for ck in schema.table_checks
                    if isinstance(ck, dict) and ck.get('expr')]
    resolved = resolve_check_names(schema.name, cols, table_checks)
    for col, stub in zip(schema.columns, cols):
        ck = stub.get('check') or {}
        if ck.get('expr'):
            col['check'] = {'name': ck.get('name') or '',
                            'expr': str(ck['expr']).strip()}
    # `resolve_check_names` 按输入顺序产出表级清单（已滤掉空项），
    # 与上面过滤后的输入逐项对应；写回 **schema 自己的** dict（不是副本）。
    for src, ck in zip(schema.table_checks, resolved):
        src['name'] = ck.get('name') or ''
        src['expr'] = str(ck.get('expr')).strip()
    return schema


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


def _parse_ddl_key_row(part: str, table: str):
    """解析 dump 里的一条**表级**键声明

    返回 `(kind, constraint_name, index_name, columns)`；不是表级键声明
    （例如 `CONSTRAINT ... CHECK`）返回 None。列清单只接受整列（可带
    ASC/DESC）；前缀/表达式索引显式报错 —— 与引擎的 `CREATE TABLE`
    解析器口径一致，绝不把不认识的声明静默跳过。
    """
    m = _DDL_KEY_ROW_RE.match(str(part).strip())
    if not m:
        return None
    constraint_name, keyword, index_name, inner = m.groups()
    kind = ('PRIMARY' if keyword.upper().startswith('PRIMARY')
            else 'UNIQUE')
    columns = []
    for raw in _split_top_level(inner):
        item = raw.strip()
        if not item:
            raise BackupError(
                f"表 {table} 的 {keyword} 列清单为空")
        cm = re.fullmatch(
            r'`?([A-Za-z_]\w*)`?(?:\s+(?:ASC|DESC))?', item,
            re.IGNORECASE)
        if not cm:
            raise BackupError(
                f"表 {table} 的 {keyword} 列清单项 {item!r} 无法解析"
                f"（本格式只支持整列，不支持前缀/表达式索引）")
        columns.append(cm.group(1))
    return kind, constraint_name, index_name, columns


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
    table_checks = []
    table_pk_cols = None
    table_unique = []
    for part in _split_top_level(body):
        part = part.strip()
        if not part:
            continue
        up = part.upper()

        # ---- 表级键声明（PRIMARY KEY / UNIQUE，含 CONSTRAINT 形态）----
        #
        # `canonical_ddl` 现在会为表级 UNIQUE 写出 `UNIQUE KEY 名字 (列...)`；
        # 解析端若不认，备份就会"能导出、不能恢复"（恢复时在这里报
        # "列 UNIQUE 带不支持的属性"）。这里把四种等价写法都读成同一份
        # `TableSchema.unique_indexes`，恢复后仍由写入路径强制唯一性。
        if (_DDL_CONSTRAINT_RE.match(part)
                or _DDL_KEY_PREFIX_RE.match(part)):
            key_row = _parse_ddl_key_row(part, name)
            if key_row is not None:
                kind, constraint_name, index_name, key_cols = key_row
                if kind == 'PRIMARY':
                    if table_pk_cols is not None:
                        raise BackupError(
                            f"表 {name} 有两条 PRIMARY KEY 声明"
                            f"（Multiple primary key defined）")
                    if index_name:
                        raise BackupError(
                            f"表 {name} 的 PRIMARY KEY 不接受索引名 "
                            f"{index_name!r}")
                    table_pk_cols = key_cols
                else:
                    uname = (index_name or constraint_name
                             or (key_cols[0] if key_cols else ''))
                    if not uname:
                        raise BackupError(
                            f"表 {name} 的 UNIQUE 声明没有索引名且没有列，"
                            f"无法表达")
                    table_unique.append({'name': uname,
                                         'columns': list(key_cols)})
                continue

        # 表级 CHECK 行：`[CONSTRAINT <名字>] CHECK (<expr>)`
        #
        # 老版本走到下面 `_DDL_COL_RE` 匹配失败的分支直接 `raise`（不会静默
        # 丢），但结果是"带表级 CHECK 的 dump 根本恢复不了"。现在显式解析，
        # 表达式交给 `parse_check_predicate` 语法自检 —— 解析不了必须报错，
        # 绝不"跳过这条约束、假装恢复成功"。
        ck_name = None
        ck_body = part
        if _DDL_CONSTRAINT_RE.match(part):
            cnm = re.match(r'CONSTRAINT\s+`?([A-Za-z_]\w*)`?\s+(.*)$',
                           part, re.IGNORECASE | re.DOTALL)
            if not cnm:
                raise BackupError(
                    f"表 {name} 的约束行 {part[:60]!r} 无法解析"
                    f"（本格式只支持 CONSTRAINT <名字> CHECK (<expr>) / "
                    f"UNIQUE / PRIMARY KEY）")
            ck_name, ck_body = cnm.group(1), cnm.group(2).strip()
        if ck_body.upper().startswith('CHECK'):
            ck = _parse_check_clause(ck_body, table=f"{name}")
            if ck_name:
                ck['name'] = ck_name
            table_checks.append(ck)
            continue
        if _DDL_CONSTRAINT_RE.match(part):
            raise BackupError(
                f"表 {name} 的 CONSTRAINT 行 {part[:60]!r} 既不是 CHECK，"
                f"也不是可解析的 UNIQUE/PRIMARY KEY（显式拒绝，不静默丢弃）")

        # 走到这里还以 PRIMARY KEY / UNIQUE 开头，说明是**能认出是键声明、
        # 但本格式不支持的形态**（例如 `UNIQUE KEY u (a) USING BTREE`）。
        # 必须在 `_DDL_COL_RE` 之前显式拒绝，否则它会被当成一根名为
        # UNIQUE 的假列建出来。
        if _DDL_KEY_PREFIX_RE.match(part):
            raise BackupError(
                f"表 {name} 的表级键声明 {part[:60]!r} 无法解析"
                f"（本格式只支持 PRIMARY KEY (列) / UNIQUE [KEY|INDEX] "
                f"[名字] (列...)，不支持 USING / 前缀 / 表达式索引）")

        # 非唯一二级索引 / FULLTEXT / SPATIAL / 外键：本格式不保存，
        # 显式拒绝而不是让 `_DDL_COL_RE` 把它当成一根假列建出来。
        if _DDL_UNSUPPORTED_INDEX_RE.match(part):
            raise BackupError(
                f"表 {name} 含本格式不支持的索引/外键行 {part[:60]!r}"
                f"（本引擎没有对应实现，拒绝静默丢弃）")

        cm = _DDL_COL_RE.match(part)
        if not cm:
            # 表级约束行（`PRIMARY KEY (a, b)`）：多列主键本格式不支持，
            # 显式报错而不是忽略掉一个约束
            if up.startswith('PRIMARY KEY') and ',' in part:
                raise BackupError(
                    f"表 {name} 使用多列主键，lightdb-logical-dump v1 不支持")
            if (_DDL_KEY_PREFIX_RE.match(part)
                    or _DDL_UNSUPPORTED_INDEX_RE.match(part)):
                raise BackupError(
                    f"表 {name} 的索引/约束行 {part[:40]!r} 无法表达为"
                    f"列属性（本格式只保存主键）")
            raise BackupError(f"表 {name} 里有无法解析的列定义: {part[:60]!r}")

        cname = cm.group(1)
        # 类型原样保留（含 `(20)` / `(10,2)`）；比对侧（`verify_restore`）
        # 走 `normalize_type`，因此这里保留参数不会让比对口径漂移。
        ctype = (cm.group(2) or '') + (cm.group(3) or '')
        tail = cm.group(4)
        # 列级 CHECK（内联在列定义末尾）：先摘出来，剩下的尾巴才按
        # NOT NULL / PRIMARY KEY / UNIQUE 判定，否则 `CHECK` 会被当成
        # "本格式不支持的属性"而拒收（正是"备份写不出、恢复读不回"的形态）。
        col_check = None
        cpos = _find_check_keyword(tail)
        if cpos is not None:
            col_check = _parse_check_clause(tail[cpos:], table=f"{name}.{cname}")
            tail = tail[:cpos]
        # 列级 DEFAULT：`canonical_ddl` 会把它写出来，这里必须读得回来，
        # 否则"带 DEFAULT 的 dump"会被下面的未知属性检查拒收
        # （备份能写、恢复读不回 = 备份等于没有）。
        col_default, col_has_default, tail = _extract_column_default(
            tail, table=name, column=cname)
        tup = tail.upper()
        unknown = [t for t in re.findall(r'[A-Z_]+', tup)
                   if t not in ('NOT', 'NULL', 'PRIMARY', 'KEY', 'UNIQUE')]
        if unknown:
            # 明确点出 MySQL 允许、但本引擎解析器不接受的形态：
            # `列 类型 CONSTRAINT 名字 CHECK (expr)`（列内联命名约束）。
            # 这里**显式拒绝**而不是跳过 —— 跳过就等于静默丢掉一条 CHECK。
            if 'CONSTRAINT' in tup:
                raise BackupError(
                    f"表 {name} 的列 {cname} 使用了列内联的命名约束写法"
                    f"{part[:60]!r}：本引擎的 `_column_def` 解析不了"
                    f"（只接受未命名的列级 `... CHECK (expr)` 或表级 "
                    f"`CONSTRAINT 名字 CHECK (expr)`）。请改写后再导出/恢复，"
                    f"本格式不会跳过这条约束")
            raise BackupError(
                f"表 {name} 的列 {cname} 带本格式不支持的属性 {unknown}")
        columns.append({
            'name': cname, 'type': ctype,
            'primary': 'PRIMARY KEY' in tup,
            'unique': 'UNIQUE' in tup,
            'notnull': bool(re.search(r'\bNOT\s+NULL\b', tup)),
            'has_default': col_has_default,
            'default': col_default,
            'check': col_check,
        })

    # 表级 `PRIMARY KEY (col)`：本引擎主键 B+Tree 只支持单列，dump 侧
    # 同样只接受单列；复合主键显式报错（不静默降级成复合语义）。
    if table_pk_cols is not None:
        if len(table_pk_cols) != 1:
            raise BackupError(
                f"表 {name} 使用多列主键 PRIMARY KEY "
                f"({', '.join(table_pk_cols)})，lightdb-logical-dump v1 不支持"
                f"（主键 B+Tree 只支持单列）")
        if any(c.get('primary') for c in columns):
            raise BackupError(
                f"表 {name} 同时有列级 PRIMARY KEY 与表级 PRIMARY KEY"
                f"（Multiple primary key defined）")
        target = table_pk_cols[0]
        hit = [c for c in columns if c['name'] == target]
        if not hit:
            raise BackupError(
                f"表 {name} 的表级 PRIMARY KEY 引用了不存在的列 {target!r}")
        hit[0]['primary'] = True

    schema = TableSchema(name=name, columns=columns,
                         table_checks=table_checks,
                         unique_indexes=table_unique)
    # 匿名约束按与 `Database.create_table` 相同的规则补名，再自检支持面
    _assign_check_names(schema)
    schema.assert_supported()
    return schema


def _extract_column_default(tail: str, table: str, column: str):
    """从列定义尾巴里摘出 `DEFAULT <字面量>`；返回 `(值, 是否声明, 剩余尾巴)`

    与 `TableSchema.canonical_ddl` / `_default_literal` 是**一对**：那边写
    什么形态，这边就必须读得回来。取值形态只允许 `_default_literal` 会
    产出的三种（`NULL` / 数字 / 时间函数 / 单引号字符串），其余显式报错
    —— 读不懂就拒绝恢复，绝不"当没有 DEFAULT"继续（那会静默丢默认值）。
    """
    m = re.search(r'\bDEFAULT\b', tail, re.IGNORECASE)
    if not m:
        return None, False, tail
    before = tail[:m.start()]
    rest = tail[m.end():].lstrip()
    if not rest:
        raise BackupError(
            f"表 {table} 的列 {column}: DEFAULT 后面缺少取值")
    if re.match(r'NULL\b', rest, re.IGNORECASE):
        value, used = None, len('NULL')
    elif rest[0] in ("'", '"'):
        q = rest[0]
        i, n = 1, len(rest)
        while i < n:
            if rest[i] == q:
                if i + 1 < n and rest[i + 1] == q:      # `''` 转义
                    i += 2
                    continue
                break
            i += 1
        else:
            raise BackupError(
                f"表 {table} 的列 {column}: DEFAULT 字符串字面量未闭合: "
                f"{rest[:40]!r}")
        value, used = rest[1:i], i + 1
    else:
        tok = rest.split()[0]
        if _DEFAULT_FUNC_RE.match(tok):
            value, used = tok.upper(), len(tok)
        elif _NUM_LITERAL_RE.match(tok):
            value, used = tok, len(tok)
        else:
            raise BackupError(
                f"表 {table} 的列 {column}: DEFAULT 取值 {tok[:40]!r} 本格式"
                f"不支持（只接受 NULL / 数字 / 当前时间函数 / 字符串字面量）")
    return value, True, before + rest[used:]


def _find_check_keyword(text: str):
    """返回 `text` 里**不在引号内**的 `CHECK` 关键字起始下标；没有则 None

    必须引号感知：`COMMENT 'CHECK (x)'` 之类的字符串里出现 CHECK 时不能
    误当成约束起点（本格式不支持 COMMENT，但解析器必须先把边界找对，
    否则报错信息会指到字符串里，误导排查）。
    """
    quoted_idx = {i for i, _c, q in _scan_quotes(text) if q}
    for m in re.finditer(r'\bCHECK\b', text, re.IGNORECASE):
        if m.start() not in quoted_idx:
            return m.start()
    return None


def _parse_check_clause(text: str, table: str = '') -> dict:
    """解析 `CHECK (<expr>) [ENFORCED]` → `{'expr': ...}`

    与 SQL 解析器（`Parser._parse_check`）保持**同一套边界规则**：

    * 括号不配对 / 表达式为空 / 多余尾巴 → 显式 `BackupError`（不静默丢）；
    * `NOT ENFORCED` → 显式 `BackupError`。本引擎的 CHECK 一律强制，
      不会因为"要能导出"就放宽成静默强制或静默忽略（与阶段 0 的决定一致）。
    """
    m = re.match(r'CHECK\s*\(', text, re.IGNORECASE)
    if not m:
        raise BackupError(f"CHECK 约束语法错误: {text[:80]!r}")
    # `_extract_paren`：配对扫描，引号内括号不计
    depth = 0
    end = None
    for i, ch, quoted in _scan_quotes(text):
        if quoted or i < m.end() - 1:
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise BackupError(f"CHECK 约束括号不配对: {text[:80]!r}")
    expr = text[m.end():end].strip()
    tail = text[end + 1:].strip()
    if tail:
        tm = re.match(r'(NOT\s+)?ENFORCED\b', tail, re.IGNORECASE)
        if tm:
            if tm.group(1):
                raise BackupError(
                    "不支持 CHECK (...) NOT ENFORCED：本引擎的 CHECK 一律"
                    "强制执行，不会按 NOT ENFORCED 放行（也不导出成这种"
                    "形态）。")
        else:
            raise BackupError(
                f"CHECK 约束后面有多余内容 {tail!r}: {text[:80]!r}")
    if not expr:
        raise BackupError(f"CHECK 约束的表达式为空: {text[:80]!r}")
    # 语法自检：解析不了（含本引擎不支持的形态）→ 显式报错。
    # 恢复端绝不能把"这条约束永远不会生效"的 dump 当成功。
    from .checks import parse_check_predicate
    parse_check_predicate(expr)
    return {'name': '', 'expr': expr}


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
    """目标库全部表的 TableSchema（按表名排序，dump 结果稳定可比对）

    CHECK 约束在这里被**真正读出来**：列级在 `cols[i]['check']`，表级在
    `Table._checks`。老版本只读 name/type/primary/unique/notnull，于是
    "备份 → 恢复"把约束静默丢光。
    """
    out = []
    for name in sorted(db.tables):
        tbl = db.tables[name]
        out.append(TableSchema.from_engine_cols(
            name, tbl.cols, getattr(tbl, '_checks', None),
            unique_indexes=tbl.unique_indexes()))
    return out


def check_ddl(db, name: str, if_not_exists: bool = False) -> str:
    """`SHOW CREATE TABLE` 的**唯一**实现（与逻辑 dump 共用一份渲染）

    引擎侧（`Executor._show_create_table`）与备份侧（`render_dump` →
    `TableSchema.canonical_ddl`）都从这里出发，避免出两条会各自漂移的
    DDL 生成路径。CHECK 的落位见 `TableSchema.canonical_ddl`。
    """
    if name not in db.tables:
        raise BackupError(f"表 {name} 不存在")
    tbl = db.tables[name]
    schema = TableSchema.from_engine_cols(
        name, tbl.cols, getattr(tbl, '_checks', None),
        unique_indexes=tbl.unique_indexes())
    schema.assert_supported()
    return schema.canonical_ddl(if_not_exists=if_not_exists)


# ==================== 生成 dump ====================

def check_attestation_line(table: str, spec: dict) -> str:
    """一条 CHECK 的哨兵行（`-- lightdb:check <表> name=.. expr=..`）"""
    name = str(spec.get('name') or '')
    expr = str(spec.get('expr') or '').strip()
    return f"{CHECK_MARKER}{table} name={name} expr={expr}"


def checks_count_line(table: str, n: int) -> str:
    return f"{CHECKS_COUNT_MARKER}{table} {n}"


def decode_check_attestation(line: str):
    """哨兵行 → (表名, {'name','expr'})；不是 CHECK 哨兵则返回 None

    解析不合法（缺 `name=` / `expr=`、表名非法）→ `BackupError`：哨兵行是
    恢复端唯一的"约束期望值来源"，读不懂它就必须停下来，绝不能"当没有
    这一行"继续恢复。
    """
    s = line.strip()
    if not s.startswith(CHECK_MARKER):
        return None
    body = s[len(CHECK_MARKER):]
    parts = body.split(None, 1)
    if not parts or not _IDENT_RE.match(parts[0]):
        raise BackupError(f"CHECK 哨兵格式错误（表名）: {s!r}")
    table, rest = parts[0], (parts[1] if len(parts) > 1 else '')
    if not rest.startswith('name='):
        raise BackupError(f"CHECK 哨兵格式错误（缺 name=）: {s!r}")
    rest = rest[len('name='):]
    nm = re.match(r'([A-Za-z_]\w*)?\s+expr=(.*)$', rest, re.DOTALL)
    if not nm:
        raise BackupError(f"CHECK 哨兵格式错误（缺 expr=）: {s!r}")
    name = nm.group(1) or ''
    expr = nm.group(2).strip()
    if not expr:
        raise BackupError(f"CHECK 哨兵的表达式为空: {s!r}")
    return table, {'name': name, 'expr': expr}


def render_dump(schemas, row_provider, no_data: bool = False,
                no_create: bool = False, source: str = 'LightDB',
                include_header: bool = True) -> str:
    """把"结构 + 行提供者"渲染成 lightdb-logical-dump v1 文本

    `row_provider(schema) -> 可迭代行`。服务器端（`BackupManager` 直连
    `Database`）与命令行端（`lightdb.tools.dump` 走 pymysql 结果集）
    共用这一份渲染逻辑，避免两边转义/顺序不一致。

    顺序（恢复端可以依赖）：每张表 `DROP TABLE IF EXISTS` → `CREATE
    TABLE` → 行数哨兵注释 → **CHECK 哨兵注释** → 全部 INSERT。`DROP` 提供
    幂等，所以 `CREATE` 不必写 `IF NOT EXISTS`（也与 mysqldump 默认输出
    一致）。
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
        # CHECK 哨兵：同样始终写（0 条也写），否则"dump 里本来没有 CHECK"
        # 与"CHECK 被删掉"无法区分
        specs = schema.check_specs()
        for spec in specs:
            lines.append(check_attestation_line(schema.name, spec))
        lines.append(checks_count_line(schema.name, len(specs)))
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
    """一份 dump 的"预演"结果：结构 + 语句 + 期望行数 + 期望 CHECK"""
    statements: list = field(default_factory=list)
    order: list = field(default_factory=list)          # 建表顺序
    schemas: dict = field(default_factory=dict)        # name -> TableSchema
    expected_rows: dict = field(default_factory=dict)  # name -> int
    #: name -> [{'name','expr'}, ...]（`-- lightdb:check` 哨兵，备份端真实
    #: 读出的约束清单；老版本 dump 没有这一段 → 空 + `has_check_attestation`
    #: 为 False，恢复端据此**如实说明**"无法核对约束"而不是假装核对过）
    expected_checks: dict = field(default_factory=dict)
    has_check_attestation: bool = False
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
    #
    # CHECK 哨兵（`-- lightdb:check` / `-- lightdb:checks`）同样来自备份端
    # 真实读出的约束：**先解析 CREATE TABLE 得到结构，再拿它和哨兵对账**，
    # 两者不一致（手改 dump 删掉一条 CHECK 却留着哨兵，或反过来）在这里
    # 就报错 —— 而不是恢复完才靠"恢复后读回"发现（那时已经区分不出
    # "dump 本来就没有约束"和"约束被删了"）。
    checks_declared = {}          # name -> 逐条哨兵的原始清单
    checks_count = {}
    for line in content.splitlines():
        s = line.strip()
        if s.startswith(ROWS_MARKER):
            parts = s[len(ROWS_MARKER):].split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise BackupError(f"行数哨兵格式错误: {s!r}")
            plan.expected_rows[parts[0]] = int(parts[1])
        elif s.startswith(CHECK_MARKER):
            table, spec = decode_check_attestation(s)
            checks_declared.setdefault(table, []).append(spec)
            plan.has_check_attestation = True
        elif s.startswith(CHECKS_COUNT_MARKER):
            parts = s[len(CHECKS_COUNT_MARKER):].split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise BackupError(f"CHECK 条数哨兵格式错误: {s!r}")
            if not _IDENT_RE.match(parts[0]):
                raise BackupError(f"CHECK 条数哨兵表名非法: {s!r}")
            checks_count[parts[0]] = int(parts[1])
            plan.has_check_attestation = True

    if plan.has_check_attestation:
        for table, specs in checks_declared.items():
            want_n = checks_count.get(table)
            if want_n is None:
                raise BackupError(
                    f"表 {table} 有 CHECK 哨兵行但没有 `-- lightdb:checks "
                    f"{table} <条数>` 行（dump 被截断或手改过）")
            if want_n != len(specs):
                raise BackupError(
                    f"表 {table}: `-- lightdb:checks` 声明 {want_n} 条 CHECK，"
                    f"实际只有 {len(specs)} 条 `-- lightdb:check` 行"
                    f"（dump 被截断或手改过）")
        for table, n in checks_count.items():
            if n != len(checks_declared.get(table, [])):
                raise BackupError(
                    f"表 {table}: `-- lightdb:checks` 声明 {n} 条 CHECK，"
                    f"实际只有 {len(checks_declared.get(table, []))} 条"
                    f" `-- lightdb:check` 行（dump 被截断或手改过）")
            plan.expected_checks[table] = list(checks_declared.get(table, []))
        # 哨兵与 CREATE TABLE 必须一致：这是"手改 dump"能被打出来的关键
        for name in plan.order:
            schema = plan.schemas[name]
            want = [(str(s.get('name') or ''), str(s.get('expr') or '').strip())
                    for s in plan.expected_checks.get(name, [])]
            have = [(str(s.get('name') or ''), str(s.get('expr') or '').strip())
                    for s in schema.check_specs()]
            if want != have:
                raise BackupError(
                    f"表 {name}: dump 里的 CHECK 哨兵与 CREATE TABLE 声明的"
                    f"约束不一致（哨兵 {want} / CREATE TABLE {have}）——"
                    f"dump 被手改或截断，拒绝恢复（静默接受会丢掉约束）")
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
    restored_rows = {}
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
            if parsed and parsed.get('command') == 'INSERT':
                tname = parsed.get('table')
                # 逐表累计"真正执行成功的 INSERT 行数"，与 dump 的行数哨兵
                # 对账。原 bug 的形态正是"10 条 INSERT 全失败 → 返回
                # (ok=0, fail=10) → 调用方只用 ok/fail 判断 → 数据库其实是
                # 空的"。这里把对账做成恢复流程的**一部分**（不依赖
                # `verify`，也不需要调用方多写一步）。
                restored_rows[tname] = (
                    restored_rows.get(tname, 0) + len(parsed.get('values') or []))

    # 行数对账：dump 声明的行数（`-- lightdb:rows`，备份端真实扫描值）
    # 与本次成功写入的行数必须逐表一致
    for name, want in plan.expected_rows.items():
        got = restored_rows.get(name, 0)
        if got != want:
            fail += 1
            errors.append(
                f"表 {name}: 成功写入 {got} 行 != dump 声明 {want} 行"
                f"（{'缺少 INSERT 或部分语句失败' if got < want else '行数多出'}）")

    report = None
    if fail == 0 and verify:
        report = verify_restore(db, plan)
        # 如实报告"这次恢复里 CHECK 是怎么核对的"：有哨兵 → 能识别"约束被
        # 删"；老版本 dump 没哨兵 → 只能退化成"与 CREATE TABLE 文本对账"，
        # 明确写进报告，不让人误以为约束被完整核对过。
        n_ck = sum(len(v) for v in plan.expected_checks.values())
        if plan.has_check_attestation:
            report.checks.append(
                f"INFO 恢复对账: CHECK 哨兵覆盖 {len(plan.expected_checks)} "
                f"表 / {n_ck} 条约束（能识别'约束被删'）")
        else:
            report.checks.append(
                "INFO 恢复对账: 本 dump 没有 `-- lightdb:check` 哨兵"
                "（旧版本产物），CHECK 只能与 CREATE TABLE 文本对账，"
                "**无法识别'约束被删除'**；请用当前版本重新导出备份")
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
    """读回校验：行数、主键、逐字段值、列结构、**CHECK 约束**

    校验项（与原库比对的是"dump 里声明的结构/行数"，dump 本身由
    `build_dump` 从原库读出——测试里再拿原库对象做一次比对）：
    1. 表存在，且列名/类型与 dump 的 CREATE TABLE 完全一致（顺序也一致）；
    2. 行数 == 哨兵行数（空表 = 0 也要对上）；
    3. 主键（BTree）条目数 == 行数，且主键值非 NULL、互不重复；
    4. 逐行字段值都能解码（含 NULL / 含引号与换行的字符串不被截断）；
    5. **CHECK 约束逐条对上**：dump 里声明的每一条约束（列级 + 表级）
       在恢复后的表里都存在、表达式相同、引用的列都存在；dump 里没有的
       约束不允许凭空出现。少一条 → `RestoreError`（"备份→恢复"丢掉
       约束正是本次要清掉的静默失效），多一条 → 同样报错。
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

        # ---- CHECK 约束（dump 声明 vs 恢复后真实挂上的）----
        _verify_checks(rep, name, plan, schema, tbl)

        # ---- 表级 UNIQUE 约束（dump 声明 vs 恢复后真实挂上的）----
        # 这条不是"锦上添花"：UNIQUE 由写入路径强制，恢复时若 CREATE TABLE
        # 文本被改掉/解析丢了，表照样能建出来、行照样能灌进去，只是唯一性
        # 悄悄没了。必须逐项对账（名字 + 列顺序）并在报告里显式记一行。
        want_uq = [(str(s.get('name') or ''),
                    tuple(str(c) for c in (s.get('columns') or [])))
                   for s in schema.unique_indexes]
        try:
            have_uq = [(str(s.get('name') or ''),
                        tuple(str(c) for c in (s.get('columns') or [])))
                       for s in tbl.unique_indexes()]
        except Exception as e:                      # noqa: BLE001
            rep.add(False, f"表 {name}: 读取表级 UNIQUE 约束失败: "
                           f"{type(e).__name__}: {e}")
        else:
            rep.add(
                want_uq == have_uq,
                f"表 {name}: 表级 UNIQUE 约束 {len(want_uq)} 条"
                f"{'逐项一致' if want_uq == have_uq else '不一致'} "
                f"（dump={want_uq} / 恢复后={have_uq}）")

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


def _check_sig(specs):
    """约束集合的可比对签名：`((名字, 表达式), ...)`（顺序也参与比较）"""
    return tuple((str(s.get('name') or ''), str(s.get('expr') or '').strip())
                 for s in specs)


def _verify_checks(rep: VerifyReport, name: str, plan: DumpPlan,
                   schema: TableSchema, tbl):
    """恢复后**读回** CHECK 约束并逐条对账（不一致 → 报告 FAIL）

    期望值来源优先级：

    1. `-- lightdb:check` 哨兵（`plan.expected_checks`）—— 备份端**真实
       读出来**的约束清单，是最可信的期望值；
    2. 没有哨兵的老版本 dump → 退回"CREATE TABLE 文本里解析出来的约束"，
       并在报告里**如实写明**用的是降级来源（不假装核对过）。
    """
    if name in plan.expected_checks:
        want = _check_sig(plan.expected_checks[name])
        src_note = '哨兵'
    else:
        want = _check_sig(schema.check_specs())
        src_note = ('CREATE TABLE 文本（老版本 dump 无 CHECK 哨兵，'
                    '无法识别"约束被删"）')
    try:
        have_specs = tbl.check_constraints()
    except Exception as e:                          # noqa: BLE001
        rep.add(False, f"表 {name}: 读取 CHECK 约束失败: "
                       f"{type(e).__name__}: {e}")
        return
    have = _check_sig(have_specs)

    if want == have:
        if want:
            rep.add(True, f"表 {name}: CHECK 约束 {len(want)} 条逐条一致"
                          f"（期望来源: {src_note}） {list(want)}")
        else:
            rep.add(True, f"表 {name}: CHECK 约束 0 条（dump 与恢复后都没有）")
    else:
        missing = [s for s in want if s not in have]
        extra = [s for s in have if s not in want]
        detail = []
        if missing:
            detail.append(f"丢失 {missing}")
        if extra:
            detail.append(f"多出 {extra}")
        rep.add(False, f"表 {name}: CHECK 约束不一致（dump {len(want)} 条 / "
                       f"恢复后 {len(have)} 条；{'；'.join(detail)}；"
                       f"期望来源: {src_note}）")

    # 约束引用的列必须都还存在 —— 列引用失效的约束在写入时会**每次都抛
    # CheckConstraintError**（不会静默放行），但那已经不是 dump 声明的语义了
    if have:
        col_names = [c['name'] for c in tbl.cols]
        for spec in have_specs:
            try:
                cols = check_expr_columns(spec['expr'])
            except Exception as e:                  # noqa: BLE001
                rep.add(False, f"表 {name}: CHECK 约束 {spec.get('name')!r} "
                               f"表达式无法解析: {e}")
                continue
            unknown = [c for c in cols if c not in col_names]
            if not unknown:
                rep.add(True, f"表 {name}: CHECK 约束 "
                              f"{spec.get('name')!r} 引用的列 {cols} 全部存在")
            else:
                rep.add(False,
                        f"表 {name}: CHECK 约束 {spec.get('name')!r} 引用了"
                        f"不存在的列 {unknown}（表达式 {spec['expr']!r}）")


def check_expr_columns(expr: str) -> list:
    """一条 CHECK 表达式引用的列名（交给 `sql/checks.py` 的同一份实现）"""
    from .checks import check_columns, parse_check_predicate
    return [str(c) for c in check_columns(parse_check_predicate(expr))]

