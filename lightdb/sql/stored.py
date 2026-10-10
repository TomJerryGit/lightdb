"""
存储过程 + 函数管理
"""
import json
import re
import time
from pathlib import Path

from .pl import PLParser, PLInterpreter, PLContext, tokenize


class StoredProcedure:
    def __init__(self, name, params, body, comment=''):
        self.name = name
        self.params = params       # [{'name','type','mode':IN/OUT/INOUT}]
        self.body = body           # PL 文本
        self.comment = comment
        self.created_at = time.time()

    def to_dict(self):
        return {
            'name': self.name, 'params': self.params,
            'body': self.body, 'comment': self.comment,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls(d['name'], d['params'], d['body'], d.get('comment', ''))
        s.created_at = d.get('created_at', time.time())
        return s


class StoredFunction:
    def __init__(self, name, params, return_type, body):
        self.name = name
        self.params = params
        self.return_type = return_type
        self.body = body
        self.created_at = time.time()

    def to_dict(self):
        return {
            'name': self.name, 'params': self.params,
            'return_type': self.return_type, 'body': self.body,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        f = cls(d['name'], d['params'], d['return_type'], d['body'])
        f.created_at = d.get('created_at', time.time())
        return f


class StoredManager:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / '_stored.json'
        self.procedures = {}   # name -> StoredProcedure
        self.functions = {}    # name -> StoredFunction
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n, d in data.get('procedures', {}).items():
                self.procedures[n] = StoredProcedure.from_dict(d)
            for n, d in data.get('functions', {}).items():
                self.functions[n] = StoredFunction.from_dict(d)
        except Exception:
            pass

    def save(self):
        data = {
            'procedures': {n: p.to_dict() for n, p in self.procedures.items()},
            'functions': {n: f.to_dict() for n, f in self.functions.items()},
        }
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ============ 存储过程 ============
    def create_procedure(self, name, params, body, comment=''):
        if name in self.procedures:
            self.drop_procedure(name)
        self.procedures[name] = StoredProcedure(name, params, body, comment)
        self.save()

    def drop_procedure(self, name):
        if name in self.procedures:
            del self.procedures[name]
            self.save()

    def get_procedure(self, name):
        return self.procedures.get(name)

    def list_procedures(self):
        return list(self.procedures.keys())

    # ============ 存储函数 ============
    def create_function(self, name, params, return_type, body):
        if name in self.functions:
            self.drop_function(name)
        self.functions[name] = StoredFunction(name, params, return_type, body)
        self.save()

    def drop_function(self, name):
        if name in self.functions:
            del self.functions[name]
            self.save()

    def get_function(self, name):
        return self.functions.get(name)

    def list_functions(self):
        return list(self.functions.keys())


# ==================== 过程调用 ====================

def parse_params(s: str):
    """解析参数列表 'IN a INT, OUT b VARCHAR(50)'"""
    if not s.strip():
        return []
    params = []
    depth = 0
    cur = ''
    parts = []
    for ch in s:
        if ch == '(':
            depth += 1
            cur += ch
        elif ch == ')':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())

    for p in parts:
        toks = p.split()
        mode = 'IN'
        if toks and toks[0].upper() in ('IN', 'OUT', 'INOUT'):
            mode = toks[0].upper()
            toks = toks[1:]
        if not toks:
            continue
        name = toks[0]
        typ = toks[1] if len(toks) > 1 else 'VARCHAR'
        params.append({'name': name, 'type': typ, 'mode': mode})
    return params


def call_procedure(sql, ctx):
    """CALL name(args) — 由 PL 解释器调用"""
    m = re.match(r'\s*CALL\s+(\w+)\s*\((.*)\)\s*$', sql, re.IGNORECASE | re.DOTALL)
    if not m:
        raise SyntaxError(f"CALL 语法错误: {sql}")

    name = m.group(1)
    args_str = m.group(2).strip()
    args = []
    if args_str:
        args = [a.strip() for a in _split_args(args_str)]

    stored_mgr = getattr(ctx.executor, 'stored_mgr', None)
    if stored_mgr is None:
        raise RuntimeError("存储过程管理器未初始化")

    proc = stored_mgr.get_procedure(name)
    if proc is None:
        raise ValueError(f"存储过程 '{name}' 不存在")

    _execute_procedure(proc, args, ctx)
    return None


def _split_args(s):
    parts = []
    depth = 0
    cur = ''
    in_str = False
    for ch in s:
        if in_str:
            cur += ch
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
            cur += ch
        elif ch == '(':
            depth += 1
            cur += ch
        elif ch == ')':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _execute_procedure(proc, args, outer_ctx):
    """执行存储过程"""
    new_ctx = PLContext(outer_ctx.executor, outer_ctx.session, outer_ctx.db)
    # 继承外层变量（简化：仅继承 NEW/OLD）
    new_ctx.new_row = outer_ctx.new_row
    new_ctx.old_row = outer_ctx.old_row

    # 绑定参数
    out_params = []
    for i, param in enumerate(proc.params):
        arg = args[i] if i < len(args) else None
        if param['mode'] == 'IN':
            # 值传递
            value = _arg_to_value(arg, outer_ctx)
            new_ctx.variables[param['name']] = value
        elif param['mode'] == 'OUT':
            new_ctx.variables[param['name']] = None
            out_params.append((i, param['name'], arg))
        elif param['mode'] == 'INOUT':
            value = _arg_to_value(arg, outer_ctx)
            new_ctx.variables[param['name']] = value
            out_params.append((i, param['name'], arg))

    # 解析并执行过程体
    body_text = proc.body
    if body_text.strip().upper().startswith('BEGIN'):
        body_text = _strip_begin_end(body_text)

    parser = PLParser(body_text)
    stmts = parser.parse_program()
    interp = PLInterpreter(new_ctx)
    interp.run(stmts)

    # 回填 OUT 参数
    for i, pname, arg in out_params:
        if arg and arg.startswith('@'):
            outer_ctx.variables[arg[1:]] = new_ctx.variables.get(pname)


def _arg_to_value(arg, ctx):
    if arg is None:
        return None
    arg = arg.strip()
    if arg.startswith('@'):
        # 用户变量
        name = arg[1:]
        return ctx.variables.get(name)
    # 字面量
    if arg.startswith("'") and arg.endswith("'"):
        return arg[1:-1]
    try:
        if '.' in arg:
            return float(arg)
        return int(arg)
    except ValueError:
        pass
    if arg.upper() == 'NULL':
        return None
    # 当变量处理
    if arg in ctx.variables:
        return ctx.variables[arg]
    return arg


def _strip_begin_end(text):
    """去掉外层 BEGIN ... END"""
    text = text.strip()
    up = text.upper()
    if up.startswith('BEGIN') and up.endswith('END'):
        return text[5:-3].strip()
    return text