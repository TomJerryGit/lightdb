"""
触发器
- BEFORE/AFTER INSERT/UPDATE/DELETE
- FOR EACH ROW
- NEW.column / OLD.column

F2 修复（DDL 假成功：触发器坏体锁表）
-------------------------------------
旧实现 `TriggerManager.create()` 只把 body 原样存进 `_triggers.json`，**不校验
语法**；坏体（例如 `SET @x=1`）建触发器时返回 OK，之后该表**任何写入**都在
触发路径解析失败 → 语句永久 1105，用户又无法 DROP TRIGGER（语法不存在），
只能改数据目录自救。

现在 `create()` 在落库**之前**做三层校验（失败抛 ValueError 子类，且绝不
写入半条定义）：

1. `PLParser` 结构校验（去掉 `BEGIN ... END` 外壳后，与运行期同类校验；
   `NEW.列` / `OLD.列` 先归一成合法标识符再交给 PL 解析器）；
2. 触发器体语句形态校验（与执行器运行时的小解释器**同一套语法**：
   `SET NEW|OLD.列 = 表达式` / `IF ... THEN ... [ELSE ...] END IF` /
   普通 SQL），坏形态（`SET @x=1`、游离 `END IF`、ELSEIF 等）显式报错；
3. `SET NEW.列` / `NEW.列` / `OLD.列` 的列存在性与"该事件有没有这一行"
   校验（由执行器传入表的列清单）。
"""
import json
import re
import time
from pathlib import Path

from .pl import PLParser, PLInterpreter, PLContext


# ==================== F2：触发器体校验 ====================

class TriggerBodyError(ValueError):
    """触发器体语法/结构错误（errno 1064 / sqlstate 42000）

    为什么带 errno/sqlstate：执行器 `_cmd_create_trigger` 优先采用异常自带
    的语义码（`sql/executor.py` 的 ValueError 分支同一约定），客户端看到的
    是 MySQL 语义的 1064，而不是笼统的 1105/1359。
    """

    errno = 1064
    sqlstate = '42000'


class TriggerColumnError(ValueError):
    """触发器体引用了表上不存在的列（1054 / 42S22）"""

    errno = 1054
    sqlstate = '42S22'


#: 触发器体里允许作为"普通 SQL 语句"起始的关键字（其余形态交给执行器的
#: 触发器小解释器；它只认 SET/IF 与普通 SQL，见 `Executor._run_trigger_stmt`）
_TRIGGER_SQL_KEYWORDS = frozenset((
    'INSERT', 'UPDATE', 'DELETE', 'SELECT', 'CALL', 'REPLACE',
))

#: 各类事件在触发器上下文里可用的行：INSERT 只有 NEW，UPDATE 两者都有，
#: DELETE 只有 OLD（与 `Executor._fire_triggers` 构造的 env 一致）
_EVENT_ROWS = {
    'INSERT': {'NEW'},
    'UPDATE': {'NEW', 'OLD'},
    'DELETE': {'OLD'},
}

_SET_REF_RE = re.compile(
    r'\bSET\s+(NEW|OLD)\.([A-Za-z_]\w*)\s*=', re.IGNORECASE)
_REF_RE = re.compile(r'\b(NEW|OLD)\.([A-Za-z_]\w*)', re.IGNORECASE)
_IF_STMT_RE = re.compile(r'^IF\s+(.+?)\s+THEN\s*(.*)$',
                         re.IGNORECASE | re.DOTALL)
_SET_NEW_OLD_RE = re.compile(
    r'^SET\s+(NEW|OLD)\.([A-Za-z_]\w*)\s*=\s*(.+)$',
    re.IGNORECASE | re.DOTALL)


def _strip_begin_end(text) -> str:
    """去掉尾部 `;` 与 `BEGIN ... END` 外壳（与 `Executor._strip_begin_end` 一致）"""
    t = str(text or '').strip()
    while t.endswith(';'):
        t = t[:-1].strip()
    up = t.upper()
    if up.startswith('BEGIN') and up.endswith('END'):
        return t[len('BEGIN'):-len('END')].strip()
    return t


def _split_statements(text):
    """按顶层分号切分语句块（引号/括号内的分号不算）

    与 `Executor._split_statements` 保持同一份语义：触发器体在校验期与
    运行期必须按**同一种方式**切分，否则校验通过的体在运行时可能解析失败。
    """
    out, cur, in_str, depth = [], '', None, 0
    for ch in str(text or ''):
        if in_str:
            cur += ch
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', '`'):
            in_str = ch
            cur += ch
        elif ch == '(':
            depth += 1
            cur += ch
        elif ch == ')':
            depth -= 1
            cur += ch
        elif ch == ';' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _mask_strings(text) -> str:
    """把字符串字面量（'...' / "..." / `...`）替换成空格

    只用于 `NEW.列` 引用扫描：字符串里的 `'NEW.x'` 不是列引用，运行期的
    `_substitute_words` 同样只替换**引号外**的引用。
    """
    s = str(text or '')
    out = []
    in_str = None
    for ch in s:
        if in_str:
            if ch == in_str:
                in_str = None
            out.append(' ')
            continue
        if ch in ("'", '"', '`'):
            in_str = ch
            out.append(' ')
            continue
        out.append(ch)
    return ''.join(out)


def _normalize_refs(text) -> str:
    """把引号外的 `NEW.列` / `OLD.列` 归一成合法标识符，供 PLParser 做结构校验

    PL 解析器的 `SET` 只认 `SET 变量 = 表达式`，而触发器语法是
    `SET NEW.列 = 表达式`；不归一的话**合法触发器**也会被 PL 解析器拒绝。
    归一后的标识符只用于语法校验，不会写进任何落盘数据。
    """
    masked = _mask_strings(text)
    out, last = [], 0
    for m in _REF_RE.finditer(masked):
        out.append(text[last:m.start()])
        out.append(f"{m.group(1).upper()}_{m.group(2)}")
        last = m.end()
    out.append(text[last:])
    return ''.join(out)


def _validate_row_reference(which, col, timing, event, columns, table):
    """校验一个 `NEW.列` / `OLD.列` 引用：列存在 + 该事件有这一行"""
    which_up = str(which).upper()
    if columns is not None and col not in columns:
        raise TriggerColumnError(
            f"触发器体引用了不存在的列 {which_up}.{col}：表 {table} 的列是 "
            f"{list(columns)}")
    available = _EVENT_ROWS.get(str(event or '').upper(), {'NEW', 'OLD'})
    if which_up not in available:
        raise TriggerBodyError(
            f"触发器体引用了 {which_up}.{col}，但 {str(event).upper()} 触发器"
            f"没有 {which_up} 行（INSERT 只有 NEW，DELETE 只有 OLD，"
            f"UPDATE 两者都有）")


def _validate_single_stmt(stmt, timing, event, table):
    """校验一条非 IF 包装的触发器语句（与运行时解释器的形态规则一致）"""
    s = str(stmt or '').strip()
    if not s:
        return
    setm = _SET_NEW_OLD_RE.match(s)
    if setm:
        which = setm.group(1).upper()
        if not setm.group(3).strip():
            raise TriggerBodyError(
                f"触发器体里的 SET {which}.{setm.group(2)} 赋值表达式为空: "
                f"{s[:120]}")
        if str(timing or '').upper() != 'BEFORE':
            raise TriggerBodyError(
                f"只有 BEFORE 触发器可以给 {which}.{setm.group(2)} 赋值"
                f"（当前是 {str(timing).upper()} 触发器）: {s[:120]}")
        return
    head = re.match(r'([A-Za-z_]+)', s)
    keyword = head.group(1).upper() if head else ''
    if keyword == 'SET':
        raise TriggerBodyError(
            f"触发器体里的 SET 只支持 SET NEW.列 = 表达式 / "
            f"SET OLD.列 = 表达式（不支持 SET @变量 或普通变量赋值）: "
            f"{s[:120]}")
    if keyword not in _TRIGGER_SQL_KEYWORDS:
        raise TriggerBodyError(
            f"触发器体里的语句不是本引擎支持的形态（只支持 "
            f"SET NEW|OLD.列=表达式、IF ... THEN ... END IF 与普通 SQL）: "
            f"{s[:120]}")
    # 普通 SQL 语句再用**运行期同一份 SQL 解析器**验一次：像
    # `INSERT INTO logs VALUES (NEW.id` 这种括号没闭合的体必须在这里就失败，
    # 不能等到第一次写入时才把整张表变成不可写。
    from .parser import parser as _sql_parser
    try:
        parsed = _sql_parser.parse(s)
    except (SyntaxError, ValueError) as e:
        raise TriggerBodyError(f"触发器体无法解析: {e}") from e
    if parsed is None:
        raise TriggerBodyError(
            f"触发器体里的语句无法解析: {s[:120]}")


def _validate_statements(stmts, timing, event, table):
    """按运行期的小解释器结构遍历语句块（IF/ELSE/END IF 配对 + 单条校验）"""
    depth = 0
    for raw in stmts:
        s = str(raw or '').strip()
        if not s:
            continue
        if re.match(r'^IF\b', s, re.IGNORECASE):
            im = _IF_STMT_RE.match(s)
            if not im or not im.group(1).strip():
                raise TriggerBodyError(f"IF 语句缺少条件或 THEN: {s[:120]}")
            depth += 1
            continue
        if re.match(r'^ELSEIF\b', s, re.IGNORECASE):
            raise TriggerBodyError(
                "触发器体里的 ELSEIF 暂不支持（请改用嵌套 IF）")
        if re.match(r'^ELSE\b', s, re.IGNORECASE):
            if depth == 0:
                raise TriggerBodyError(f"ELSE 没有对应的 IF: {s[:120]}")
            rest = re.sub(r'^ELSE\b', '', s, flags=re.IGNORECASE).strip()
            if rest:
                _validate_single_stmt(rest, timing, event, table)
            continue
        if re.match(r'^END\s+IF$', s, re.IGNORECASE):
            depth -= 1
            if depth < 0:
                raise TriggerBodyError(f"END IF 没有对应的 IF: {s[:120]}")
            continue
        if re.match(r'^END\b', s, re.IGNORECASE):
            raise TriggerBodyError(
                f"触发器体里出现无法配对的 END（BEGIN/END 必须成对且只包"
                f"在外层）: {s[:120]}")
        _validate_single_stmt(s, timing, event, table)
    if depth != 0:
        raise TriggerBodyError(
            f"触发器体里的 IF 缺少对应的 END IF（还有 {depth} 层未闭合）")


def validate_trigger_body(body, timing=None, event=None, columns=None,
                          table=None):
    """校验触发器体（F2）；不合法抛 `TriggerBodyError` / `TriggerColumnError`

    校验分三层（见模块头说明）：PL 结构 → 语句形态 → NEW/OLD 引用。
    **纯校验、无副作用**，可在建触发器之前安全调用。
    """
    raw = str(body or '').strip()
    if not raw:
        raise TriggerBodyError(
            "触发器体为空：FOR EACH ROW 之后没有可执行语句")
    # 外壳配对：以 BEGIN 开头就必须以 END 收尾（与运行期 _strip_begin_end
    # 的判定一致；不配对时执行器只会在触发时才发现，正是"坏体锁表"）
    shell = raw.rstrip(';').strip()
    up_shell = shell.upper()
    if up_shell.startswith('BEGIN') and not up_shell.endswith('END'):
        raise TriggerBodyError(
            "触发器体的 BEGIN 没有对应的 END（BEGIN ... END 必须成对）: "
            f"{raw[:120]}")
    if up_shell.endswith('END') and not up_shell.startswith('BEGIN'):
        raise TriggerBodyError(
            f"触发器体以 END 收尾但没有 BEGIN: {raw[:120]}")

    inner = _strip_begin_end(raw)
    if not inner:
        raise TriggerBodyError("触发器体为空（BEGIN ... END 之间没有语句）")

    masked = _mask_strings(inner)
    # 引用校验：列存在性 / 该事件是否有这一行；SET 额外要求 BEFORE
    for m in _REF_RE.finditer(masked):
        _validate_row_reference(m.group(1), m.group(2), timing, event,
                                columns, table)
    for m in _SET_REF_RE.finditer(masked):
        if str(timing or '').upper() != 'BEFORE':
            raise TriggerBodyError(
                f"只有 BEFORE 触发器可以给 {m.group(1).upper()}."
                f"{m.group(2)} 赋值（当前是 {str(timing).upper()} 触发器）")

    # ① PL 结构校验（与运行期同类校验；NEW/OLD 引用先归一）
    try:
        pl_parser = PLParser(_normalize_refs(inner))
        stmts = pl_parser.parse_program()
    except (SyntaxError, ValueError) as e:
        raise TriggerBodyError(f"触发器体无法解析: {e}") from e
    if not stmts:
        raise TriggerBodyError(f"触发器体无法解析: 没有任何可执行语句")

    # ② 语句形态校验（与执行器触发器小解释器的语法一致）
    _validate_statements(_split_statements(inner), timing, event, table)


class Trigger:
    def __init__(self, name, table, timing, event, body, order=0):
        self.name = name
        self.table = table
        self.timing = timing.upper()    # BEFORE / AFTER
        self.event = event.upper()      # INSERT / UPDATE / DELETE
        self.body = body
        self.order = order
        self.created_at = time.time()

    def to_dict(self):
        return {
            'name': self.name, 'table': self.table,
            'timing': self.timing, 'event': self.event,
            'body': self.body, 'order': self.order,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d['name'], d['table'], d['timing'], d['event'],
                d['body'], d.get('order', 0))
        t.created_at = d.get('created_at', time.time())
        return t


class TriggerManager:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / '_triggers.json'
        self.triggers = {}   # table -> [Trigger, ...]
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for table, tlist in data.items():
                self.triggers[table] = [Trigger.from_dict(d) for d in tlist]
        except Exception:
            pass

    def save(self):
        data = {
            table: [t.to_dict() for t in tlist]
            for table, tlist in self.triggers.items()
        }
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def create(self, name, table, timing, event, body, order=0,
               columns=None):
        """创建触发器（F2：**落库前**校验触发器体，失败不写入）

        `columns` 是目标表的真实列名清单（执行器传入）：用来校验
        `SET NEW.列` / `NEW.列` / `OLD.列` 引用的列存在。旧调用方不传时
        跳过列存在性校验，但语法/结构校验仍然执行。
        """
        # 检查同名
        for tlist in self.triggers.values():
            for t in tlist:
                if t.name == name:
                    raise ValueError(f"触发器 '{name}' 已存在")
        # ★ F2：坏体触发器必须在**落库前**被拒绝 —— 旧实现只存 body，
        #   等第一次写入时才在触发路径解析失败，把整张表变成不可写。
        validate_trigger_body(body, timing=timing, event=event,
                              columns=columns, table=table)
        t = Trigger(name, table, timing, event, body, order)
        self.triggers.setdefault(table, []).append(t)
        self.triggers[table].sort(key=lambda x: x.order)
        self.save()
        return t

    def drop(self, name):
        for table, tlist in self.triggers.items():
            for i, t in enumerate(tlist):
                if t.name == name:
                    del tlist[i]
                    self.save()
                    return True
        return False

    def list_all(self):
        result = []
        for tlist in self.triggers.values():
            result.extend(tlist)
        return result

    def fire(self, timing, event, table, old_row=None, new_row=None,
             executor=None, session=None):
        """触发某表上的触发器"""
        tlist = self.triggers.get(table, [])
        if not tlist:
            return

        # 准备列名映射（按行位置）
        tbl = executor.db.get_table(table)
        col_names = [c['name'] for c in tbl.cols]
        old_dict = dict(zip(col_names, old_row)) if old_row else None
        new_dict = dict(zip(col_names, new_row)) if new_row else None

        for trig in tlist:
            if trig.timing != timing or trig.event != event:
                continue
            try:
                self._run_trigger(trig, old_dict, new_dict, executor, session)
            except Exception as e:
                from ..logger import logger
                logger.error(f"触发器 {trig.name} 执行失败: {e}")

    def _run_trigger(self, trig, old_dict, new_dict, executor, session):
        body = trig.body
        if body.strip().upper().startswith('BEGIN'):
            # 去外层 BEGIN/END
            up = body.strip().upper()
            if up.startswith('BEGIN') and up.endswith('END'):
                body = body.strip()[5:-3].strip()

        p = PLParser(body)
        stmts = p.parse_program()

        ctx = PLContext(executor, session, executor.db)
        ctx.new_row = new_dict
        ctx.old_row = old_dict

        interp = PLInterpreter(ctx)
        interp.run(stmts)