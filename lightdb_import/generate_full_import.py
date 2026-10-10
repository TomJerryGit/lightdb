#!/usr/bin/env python3
"""
generate_full_import.py
生成完整的 LightDB 资料导入 SQL。

用法:
    python generate_full_import.py /path/to/lightdb_project > full_import.sql
"""
import os
import sys
from pathlib import Path

# ---------- 数据定义 ----------
VERSIONS = [
    ('1.0.0', '基础版', '2024-06-01',
     'MySQL 协议 + 基础 SQL + 存储引擎 + 事务 + 复制 + 权限', '基础', 3.5),
    ('2.0.0', 'Enterprise Ready', '2025-02-01',
     '补齐 MySQL 8.0 特性：窗口函数、CTE、存储过程、JSON、全文索引、分区表', '~90%', 5.0),
    ('3.0.0', 'Production Ready', '2025-02-01',
     '性能提升 33%，稳定性到 30 天稳定，43 个核心指标', '80%', 7.0),
    ('4.0.0', 'Performance Breakthrough', '2025-08-01',
     '突破 GIL，多进程 + Cython + 列式存储 + 并行查询 + HTAP', '125%', 8.8),
]

MODULES = [
    # (code, name, category, layer, desc, sort)
    ('storage', '存储引擎', '内核', 'kernel', '页管理、B+Tree、WAL、MVCC、锁、事务', 10),
    # ... 省略，见 3.2 节
]

ISSUES = [
    # (module, file, severity, category, title, desc, suggestion)
    ('concurrency', 'shared_memory.py', 'critical', 'bug',
     '跨进程原子性不足',
     'atomic_increment 仅用 threading.Lock，多进程同时自增会丢更新',
     '改用 multiprocessing.Lock 或 fcntl.flock'),
    # ... 省略，见 3.5 节
]

MODULE_MAP = [
    # ... 见上文
]


def sql_escape(s):
    if s is None:
        return ''
    return str(s).replace('\\', '\\\\').replace("'", "''")


def q(s):
    """加引号并转义"""
    return "'" + sql_escape(s) + "'"


def emit_versions():
    print("-- ========== 版本 ==========")
    print("TRUNCATE TABLE `lightdb_versions`;")
    print("INSERT INTO `lightdb_versions` "
          "(version, codename, release_date, summary, mysql_compat, score) VALUES")
    parts = [f"({q(v)}, {q(c)}, {q(d)}, {q(s)}, {q(m)}, {sc})"
             for v, c, d, s, m, sc in VERSIONS]
    print(',\n'.join(parts) + ';')
    print("")


def emit_modules():
    print("-- ========== 模块 ==========")
    print("TRUNCATE TABLE `lightdb_modules`;")
    print("INSERT INTO `lightdb_modules` "
          "(code, name, category, layer, description, sort_order) VALUES")
    parts = [f"({q(c)}, {q(n)}, {q(cat)}, {q(l)}, {q(d)}, {s})"
             for c, n, cat, l, d, s in MODULES]
    print(',\n'.join(parts) + ';')
    print("")


def emit_issues():
    print("-- ========== 已知问题 ==========")
    print("TRUNCATE TABLE `lightdb_issues`;")
    print("INSERT INTO `lightdb_issues` "
          "(module_code, file_name, severity, category, title, "
          "description, suggestion) VALUES")
    parts = []
    for m, f, sev, cat, t, d, s in ISSUES:
        parts.append(
            f"({q(m)}, {q(f)}, {q(sev)}, {q(cat)}, "
            f"{q(t)}, {q(d)}, {q(s)})")
    print(',\n'.join(parts) + ';')
    print("")


def emit_files(root):
    print("-- ========== 文件 ==========")
    print("TRUNCATE TABLE `lightdb_files`;")
    rows = scan_files(root)
    batch = 100
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        values = []
        for r in chunk:
            values.append(
                f"((SELECT id FROM lightdb_modules "
                f"WHERE code={q(r['module'])} LIMIT 1), "
                f"{q(r['name'])}, {q(r['path'])}, {q(r['type'])}, "
                f"{q(r['lang'])}, {r['size']}, {r['lines']}, "
                f"'', 'provided', 0, '')")
        print("INSERT INTO `lightdb_files` "
              "(module_id, file_name, file_path, file_type, language, "
              "size_bytes, lines, summary, status, has_issue, duplicate_of) "
              "VALUES")
        print(',\n'.join(values) + ';')
    print(f"-- 共 {len(rows)} 个文件")
    print("")


def scan_files(root):
    root = Path(root)
    rows = []
    for fp in sorted(root.rglob('*')):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(root)).replace('\\', '/')
        if any(p in rel for p in
               ('__pycache__', '.git', 'node_modules', 'build/')):
            continue
        ext = fp.suffix.lower()
        ft = {'.py': 'python', '.pyx': 'cython', '.md': 'markdown',
              '.yaml': 'yaml', '.yml': 'yaml', '.json': 'json',
              '.toml': 'toml', '.txt': 'text', '.sh': 'shell'}.get(ext, 'other')
        module = 'other'
        for prefix, code in MODULE_MAP:
            if rel.startswith(prefix):
                module = code
                break
        rows.append({
            'module': module,
            'name': fp.name,
            'path': rel,
            'type': ft,
            'lang': ft,
            'size': fp.stat().st_size,
            'lines': 0,
        })
    return rows


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root = sys.argv[1]
    print("SET NAMES utf8mb4;")
    print("")
    emit_versions()
    emit_modules()
    emit_issues()
    emit_files(root)


if __name__ == '__main__':
    main()