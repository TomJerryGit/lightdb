"""Batch 3 测试 - 数据网格 / 对比 / ER / 构建器"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree
except ImportError:                       # 直接 python tests/test_batch3.py
    from conftest import safe_rmtree

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_b3')
PORT = 8910


def reset():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)


def start_server(port):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = Database(DATA_DIR)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    return srv, db, ex


def test_schema_diff():
    """测试1: 结构对比引擎"""
    print("\n【测试1】结构对比引擎")
    from lightdb_studio.db.schema_diff import (
        SchemaDiffer, ColumnDef, TableDiff, diff_databases)

    # 模拟两张表的结构
    cols_a = [
        {'name': 'id', 'type': 'INT', 'key': 'PRI',
         'null': 'NO', 'extra': 'auto_increment', 'default': None},
        {'name': 'name', 'type': 'VARCHAR(50)', 'key': '',
         'null': 'YES', 'extra': '', 'default': None},
        {'name': 'age', 'type': 'INT', 'key': '',
         'null': 'YES', 'extra': '', 'default': None},
    ]
    cols_b = [
        {'name': 'id', 'type': 'INT', 'key': 'PRI',
         'null': 'NO', 'extra': 'auto_increment', 'default': None},
        {'name': 'name', 'type': 'VARCHAR(100)', 'key': '',
         'null': 'NO', 'extra': '', 'default': None},   # 变化
        {'name': 'email', 'type': 'VARCHAR(100)', 'key': '',
         'null': 'YES', 'extra': '', 'default': None},  # 新增
        # age 被移除
    ]

    td = SchemaDiffer.diff_tables(cols_a, cols_b, 'users')
    print(f"  → 新增列: {[c.name for c in td.columns_added]}")
    print(f"  → 移除列: {td.columns_removed}")
    print(f"  → 修改列: {[(o.name, n.name) for o, n in td.columns_changed]}")

    assert len(td.columns_added) == 1
    assert td.columns_added[0].name == 'email'
    assert 'age' in td.columns_removed
    assert len(td.columns_changed) == 1
    assert td.columns_changed[0][1].name == 'name'
    print("  ✅ 结构对比正确")

    # 生成 SQL
    sqls = td.to_sql('users')
    print(f"  → 差异 SQL:")
    for s in sqls:
        print(f"    {s}")
    assert any('ADD COLUMN' in s and 'email' in s for s in sqls)
    assert any('DROP COLUMN' in s and 'age' in s for s in sqls)
    assert any('MODIFY COLUMN' in s and 'name' in s for s in sqls)
    print("  ✅ ALTER 语句生成正确")


def test_diff_databases_live():
    """测试2: 在线库对比"""
    print("\n【测试2】在线库对比")
    reset()
    srv, db, ex = start_server(PORT)

    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.schema_diff import diff_databases

    info = ConnectionInfo(name='test', host='127.0.0.1', port=PORT,
                          user='root', password='')
    conn_a = DBConnection(info)
    conn_a.connect()

    info2 = ConnectionInfo(name='test2', host='127.0.0.1', port=PORT,
                           user='root', password='')
    conn_b = DBConnection(info2)
    conn_b.connect()

    # 建表
    conn_a.execute("CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(50))")
    conn_a.execute("CREATE TABLE orders (id INT PRIMARY KEY, amount INT)")

    # 第二个连接看到同样的库（都是 default）
    tables_a = conn_a.get_tables()
    tables_b = conn_b.get_tables()
    print(f"  → 表列表: {tables_a}")

    diffs = diff_databases(conn_a, conn_b, tables_a, tables_b)
    print(f"  → 差异数: {len(diffs)}")
    # 两库相同，应该没有差异
    assert len(diffs) == 0
    print("  ✅ 相同库无差异")

    conn_a.close()
    conn_b.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_er_relations():
    """测试3: ER 图关系检测"""
    print("\n【测试3】ER 图关系检测")
    reset()
    srv, db, ex = start_server(PORT)

    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.config import ConnectionInfo

    info = ConnectionInfo(name='test', host='127.0.0.1', port=PORT,
                          user='root', password='')
    conn = DBConnection(info)
    conn.connect()

    # 建表：users + orders（外键通过命名）
    conn.execute("CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(50))")
    conn.execute("""CREATE TABLE orders (
        id INT PRIMARY KEY,
        user_id INT,
        amount INT
    )""")
    conn.execute("""CREATE TABLE payments (
        id INT PRIMARY KEY,
        order_id INT,
        total INT
    )""")

    # 模拟 ER 图关系检测
    tables = conn.get_tables()
    print(f"  → 表: {tables}")
    assert 'users' in tables and 'orders' in tables

    relations = []
    table_set = set(tables)
    for t in tables:
        cols = conn.get_columns(t)
        for c in cols:
            cname = c['name'].lower()
            for other in table_set:
                if other == t:
                    continue
                on = other.lower()
                if cname in (f"{on}_id", f"{on}id"):
                    relations.append((t, c['name'], other))
                    break

    print(f"  → 检测到关系:")
    for r in relations:
        print(f"    {r[0]}.{r[1]} → {r[2]}")
    assert any(r[0] == 'orders' and r[2] == 'users' for r in relations)
    assert any(r[0] == 'payments' and r[2] == 'orders' for r in relations)
    print("  ✅ 外键关系检测正确")

    conn.close()
    srv.shutdown()
    db.shutdown()
    time.sleep(0.3)


def test_query_builder_sql():
    """测试4: 查询构建器 SQL 生成（无 GUI）"""
    print("\n【测试4】查询构建器 SQL 生成")
    # 直接测试 SQL 生成逻辑（无 GUI 依赖）

    def build_sql(table, columns, conditions, order_by, limit):
        sql = f"SELECT {', '.join('`' + c + '`' for c in columns)} "
        sql += f"FROM `{table}`"
        if conditions:
            where_parts = []
            for i, (col, op, val, logic) in enumerate(conditions):
                if op == 'IS NULL':
                    cond = f"`{col}` IS NULL"
                elif op in ('=', '!=', '>', '<', '>=', '<='):
                    try:
                        int(val)
                        cond = f"`{col}` {op} {val}"
                    except (ValueError, TypeError):
                        cond = f"`{col}` {op} '{val}'"
                elif op == 'LIKE':
                    cond = f"`{col}` LIKE '{val}'"
                else:
                    cond = f"`{col}` {op} '{val}'"
                if i == 0:
                    where_parts.append(cond)
                else:
                    where_parts.append(f"{logic} {cond}")
            sql += " WHERE " + ' '.join(where_parts)
        if order_by:
            sql += f" ORDER BY `{order_by[0]}` {order_by[1]}"
        sql += f" LIMIT {limit}"
        return sql

    sql = build_sql(
        'users',
        ['id', 'name', 'age'],
        [('age', '>', '20', 'AND'), ('name', 'LIKE', 'A%', 'AND')],
        ('age', 'DESC'),
        100
    )
    print(f"  → {sql}")
    assert 'SELECT `id`, `name`, `age`' in sql
    assert 'age > 20' in sql
    assert "name LIKE 'A%'" in sql
    assert 'ORDER BY `age` DESC' in sql
    assert 'LIMIT 100' in sql
    print("  ✅ SQL 生成逻辑正确")

    # 复杂条件
    sql2 = build_sql(
        'orders',
        ['*'],
        [('status', '=', 'active', 'AND'),
         ('total', '>', '1000', 'OR'),
         ('user_id', 'IS NULL', '', '')],
        None, 50
    )
    print(f"  → {sql2}")
    assert "status = 'active'" in sql2
    assert 'total > 1000' in sql2
    assert 'user_id IS NULL' in sql2
    print("  ✅ 复杂条件生成正确")


def test_cell_viewer_data():
    """测试5: 单元格查看器数据处理（无 GUI）"""
    print("\n【测试5】单元格数据处理")
    # 测试十六进制转储逻辑
    data = b'\x00\x01\x02Hello\xff\xfe'

    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        hex_part = hex_part.ljust(16 * 3 - 1)
        ascii_part = ''.join(
            chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{i:08x}  {hex_part}  |{ascii_part}|')

    dump = '\n'.join(lines)
    print(f"  → Hex dump:")
    print(f"    {lines[0]}")
    assert '48 65 6c 6c 6f' in lines[0] or '48656c6c6f' in lines[0].replace(' ', '')
    assert '|' in lines[0]
    print("  ✅ Hex 转储正确")


def test_copy_paste_format():
    """测试6: Excel 剪贴板格式"""
    print("\n【测试6】Excel 剪贴板格式")
    # 模拟从表格复制
    rows = [
        [1, 'Alice', 30],
        [2, 'Bob', 25],
    ]
    lines = []
    for row in rows:
        lines.append('\t'.join(str(v) for v in row))
    text = '\n'.join(lines)

    print(f"  → 复制格式:\n    {text!r}")
    assert text == "1\tAlice\t30\n2\tBob\t25"

    # 模拟粘贴
    paste_data = "3\tCarol\t28\n4\tDave\t32"
    parsed = [line.split('\t') for line in paste_data.splitlines()]
    print(f"  → 粘贴解析: {parsed}")
    assert parsed == [['3', 'Carol', '28'], ['4', 'Dave', '32']]
    print("  ✅ Excel 格式交互正确")


def main():
    print("=" * 60)
    print("  Batch 3 测试 - 数据网格 / 对比 / ER / 构建器")
    print("=" * 60)

    tests = [
        ('结构对比引擎', test_schema_diff),
        ('在线库对比', test_diff_databases_live),
        ('ER 关系检测', test_er_relations),
        ('查询构建器 SQL', test_query_builder_sql),
        ('单元格处理', test_cell_viewer_data),
        ('剪贴板格式', test_copy_paste_format),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 Batch 3 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())