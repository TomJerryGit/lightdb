# -*- coding: utf-8 -*-
"""LightDB 项目静态诊断脚本（一次性）"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(r"D:\lightdb_project")
SKIP_DIRS = {'.git', '__pycache__', '1.0 lightdb 运行'}
TYPING_NAMES = {'List', 'Dict', 'Set', 'Tuple', 'Optional', 'Union', 'Any', 'Callable',
                'Iterable', 'Iterator', 'Sequence', 'Mapping', 'DefaultDict', 'Deque',
                'FrozenSet', 'Counter', 'NamedTuple', 'Type', 'TypeVar', 'Generic',
                'Literal', 'Final', 'NoReturn', 'ClassVar', 'Generator', 'Awaitable',
                'AsyncIterator', 'AsyncIterable', 'Coroutine', 'Hashable', 'KeysView',
                'ItemsView', 'ValuesView', 'Reversible', 'SupportsInt', 'TextIO',
                'BinaryIO', 'IO', 'Match', 'Pattern', 'AbstractSet', 'MutableSet',
                'MutableMapping', 'MutableSequence', 'ByteString', 'Container',
                'Collection', 'ChainMap', 'OrderedDict', 'Dict', 'List'}

issues = []

def scan_file(path):
    rel = path.relative_to(ROOT)
    try:
        src = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        src = path.read_text(encoding='gbk', errors='replace')
    except Exception as e:
        issues.append((str(rel), 'READ_FAIL', str(e)))
        return

    # 空文件
    if not src.strip():
        issues.append((str(rel), 'EMPTY_FILE', '0 bytes'))
        return

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        issues.append((str(rel), 'SYNTAX', f"line {e.lineno}: {e.msg}"))
        return

    # 收集 typing 导入
    imported = set()
    typing_imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == 'typing':
                for a in node.names:
                    typing_imported.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.asname or a.name)

    # 收集注解中使用的名字（subscript 形式 List[...] 或裸名）
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef)):
            ann = node.annotation if isinstance(node, ast.AnnAssign) else node.returns
            if ann:
                collect_names(ann, used)
        if isinstance(node, ast.arg) and node.annotation:
            collect_names(node.annotation, used)

    missing = used - typing_imported - imported - set(dir(__builtins__) if isinstance(__builtins__, dict) else dir(__builtins__))
    missing = {m for m in missing if m in TYPING_NAMES}
    if missing:
        issues.append((str(rel), 'TYPING_MISSING', ','.join(sorted(missing))))

def collect_names(node, out):
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, ast.Subscript):
        collect_names(node.value, out)
        if isinstance(node.slice, ast.Tuple):
            for el in node.slice.elts:
                collect_names(el, out)
        else:
            collect_names(node.slice, out)
    elif isinstance(node, ast.Attribute):
        collect_names(node.value, out)
    elif isinstance(node, ast.Constant):
        pass
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for el in node.elts:
            collect_names(el, out)
    elif isinstance(node, ast.BinOp):
        collect_names(node.left, out)
        collect_names(node.right, out)
    elif isinstance(node, ast.Index):
        collect_names(node.value, out)

for p in ROOT.rglob('*.py'):
    relparts = p.relative_to(ROOT).parts
    if any(skip in relparts for skip in SKIP_DIRS):
        continue
    scan_file(p)

print(f"扫描完成，共发现问题 {len(issues)} 项")
print("=" * 60)
from collections import Counter
cat_counter = Counter(i[1] for i in issues)
for cat, cnt in cat_counter.most_common():
    print(f"{cat}: {cnt}")
print("=" * 60)
for rel, cat, detail in issues:
    print(f"[{cat}] {rel} :: {detail}")
