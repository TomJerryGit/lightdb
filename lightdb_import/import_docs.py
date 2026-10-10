#!/usr/bin/env python3
"""
import_docs.py - 用 PyMySQL 导入 LightDB 资料
"""
import pymysql
from pathlib import Path

HOST = '127.0.0.1'
PORT = 8888
USER = 'root'
PASS = ''
DB   = 'default'

FILES = [
    '01_schema.sql',
    '02_versions.sql',
    '03_modules.sql',
    '04_features.sql',
    '05_benchmarks.sql',
    '06_issues.sql',
    '07_api.sql',
    '08_docs.sql',
    '09_tags.sql',
    '10_files.sql',
]

conn = pymysql.connect(
    host=HOST, port=PORT, user=USER, password=PASS,
    database=DB, autocommit=True, charset='utf8mb4')
cur = conn.cursor()

for fname in FILES:
    path = Path(fname)
    if not path.exists():
        print(f"跳过 {fname}（不存在）")
        continue

    print(f">>> 导入 {fname}")
    content = path.read_text(encoding='utf-8')

    # 按分号切分（简化；生产建议用状态机）
    statements = split_sql(content)
    ok = 0
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt or stmt.startswith('--'):
            continue
        try:
            cur.execute(stmt)
            ok += 1
        except Exception as e:
            print(f"  ❌ {stmt[:60]}... → {e}")
    print(f"  ✓ 执行 {ok} 条")

cur.close()
conn.close()
print("✅ 全部导入完成")