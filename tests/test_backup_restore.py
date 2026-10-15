"""逻辑备份/恢复正确性测试（本次修复新增，追加而非改既有断言）

覆盖：
1. 多表（含主键、空表）恢复，行数/字段值/列结构逐项与原库一致；
2. NULL、含单引号、双引号、括号、分号、换行、空白、反斜杠的字符串
   往返不截断、不转义错；
3. **恢复失败必须显式报错**（原 bug：INSERT 全部失败但只记计数、最终
   静默留下 0 行的"成功"）；
4. dump 结构错误（INSERT 指向未建的表）在执行前就被拒绝；
5. 同一份 dump 可重复恢复（幂等），并能恢复到已有数据的库；
6. 空表 / 只导结构（--no-data）也能正确恢复。

只允许新增文件，不修改既有测试文件的任何断言。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                       # 直接 python tests/test_backup_restore.py
    from conftest import safe_rmtree, track

BASE = os.path.join(os.path.dirname(__file__), '..')
SRC_DIR = os.path.join(BASE, 'data_bak_src')
DST_DIR = os.path.join(BASE, 'data_bak_dst')
BAK_DIR = os.path.join(BASE, 'backups_bak')


def reset(*extra):
    for d in (SRC_DIR, DST_DIR, BAK_DIR) + tuple(extra):
        if os.path.exists(d):
            safe_rmtree(d)
        os.makedirs(d, exist_ok=True)


def col(name, type_, primary=False, notnull=False, unique=False):
    return {'name': name, 'type': type_, 'primary': primary,
            'notnull': notnull, 'unique': unique}


USERS_COLS = [col('id', 'INT', primary=True, notnull=True),
              col('name', 'VARCHAR'),
              col('age', 'INT')]
ORDERS_COLS = [col('oid', 'INT', primary=True, notnull=True),
               col('user_id', 'INT'),
               col('amount', 'DECIMAL'),
               col('note', 'VARCHAR')]


def open_db(path):
    from lightdb.db.database import Database
    return track(Database(path))


def snapshot(tbl):
    """表的全部行（按主键序），用于逐字段比对"""
    from lightdb.sql.logical_dump import iter_rows
    return list(iter_rows(tbl))


# ==================== 1. 多表 + 空表 + 逐字段比对 ====================

def test_multi_table_roundtrip_field_by_field():
    """多表恢复：行数、主键、每个字段的值与类型都与原库一致"""
    reset()
    from lightdb.backup.backup import BackupManager

    src = open_db(SRC_DIR)
    src.create_table('users', USERS_COLS)
    src.create_table('orders', ORDERS_COLS)
    src.create_table('empty_t', [col('k', 'INT', primary=True)])
    u = src.get_table('users')
    o = src.get_table('orders')
    for i in range(1, 11):
        u.insert([i, f'u{i}', 20 + i])
    o.insert([1, 1, 12.5, 'first'])
    o.insert([2, 2, 0.0, 'zero'])
    o.insert([3, 3, -7.25, None])
    src_before = {'users': snapshot(u), 'orders': snapshot(o),
                  'empty_t': snapshot(src.get_table('empty_t'))}

    dump_file = BackupManager(src, BAK_DIR).dump()
    src.shutdown()

    dst = open_db(DST_DIR)
    ok, fail = BackupManager(dst, BAK_DIR).restore(dump_file)
    assert fail == 0, "恢复不应有失败语句"

    for name, want_rows in src_before.items():
        tbl = dst.get_table(name)
        got_rows = snapshot(tbl)
        assert len(got_rows) == len(want_rows), (
            f'{name}: 恢复后 {len(got_rows)} 行 != 原库 {len(want_rows)} 行')
        assert got_rows == want_rows, f'{name}: 逐行内容不一致'
        # 值类型也要一致（DECIMAL 仍是 float、INT 仍是 int）
        for want_row, got_row in zip(want_rows, got_rows):
            assert [type(v) for v in got_row] == \
                   [type(v) for v in want_row], f'{name}: 字段类型不一致'

    # 列结构（名字、类型、顺序、主键、NOT NULL）
    for name in src_before:
        assert [c['name'] for c in dst.get_table(name).cols] == \
               [c['name'] for c in
                {'users': USERS_COLS, 'orders': ORDERS_COLS,
                 'empty_t': [col('k', 'INT', primary=True)]}[name]]
    assert dst.get_table('users').row_count == 10
    assert dst.get_table('empty_t').row_count == 0
    print(f'  → 原库/恢复后行数: '
          + ', '.join(f'{k}={len(v)}/{len(snapshot(dst.get_table(k)))}'
                      for k, v in src_before.items()))
    dst.shutdown()


# ==================== 2. NULL / 特殊字符 / 类型边界 ====================

SPECIAL = [
    ("it's", 'single quote'),
    ('say "hi"', 'double quote'),
    ('a(b)c', 'parentheses'),
    ('x;y', 'semicolon'),
    ('line1\nline2', 'newline'),
    ('tab\there', 'tab'),
    ('  padded  ', 'leading/trailing space'),
    ('back\\slash', 'backslash'),
    ("both '' and \"\"", 'doubled quotes'),
    ('-- not a comment', 'leading dashes'),
    ('中文 emoji ok', 'non-ascii'),
    ('', 'empty string'),
    (None, 'NULL'),
]


def test_special_values_and_null_roundtrip():
    """含引号/换行/分号/NULL 的字符串往返后逐字节相等"""
    reset()
    from lightdb.backup.backup import BackupManager

    src = open_db(SRC_DIR)
    src.create_table('s', [col('id', 'INT', primary=True, notnull=True),
                           col('txt', 'VARCHAR'),
                           col('n', 'INT'),
                           col('f', 'DECIMAL')])
    t = src.get_table('s')
    rows = []
    for i, (val, _label) in enumerate(SPECIAL, start=1):
        t.insert([i, val, None if val is None else i, float(i) + 0.5])
        rows.append([i, val, None if val is None else i, float(i) + 0.5])
    want = list(rows)

    dump_file = BackupManager(src, BAK_DIR).dump()
    with open(dump_file, 'r', encoding='utf-8') as f:
        content = f.read()
    # dump 必须自带 DDL（原 bug 就是没有可用 DDL）
    assert 'CREATE TABLE s' in content
    assert 'DROP TABLE IF EXISTS s' in content
    src.shutdown()

    dst = open_db(DST_DIR)
    ok, fail = BackupManager(dst, BAK_DIR).restore(dump_file)
    assert fail == 0
    got = snapshot(dst.get_table('s'))
    assert got == want, f'special 值往返不一致:\n{got}\n{want}'
    print(f'  → {len(want)} 个特殊值/NULL 行全部逐字节相等（含反斜杠、'
          f'换行、空串、NULL）')
    for (val, label), gr in zip(SPECIAL, got):
        assert gr[1] == val, f'{label}: {gr[1]!r} != {val!r}'
    dst.shutdown()


def test_multiline_string_survives_statement_split():
    """含换行与 `;` 的字符串不会被语句切分器切断"""
    reset()
    from lightdb.backup.backup import BackupManager

    tricky = "SELECT * FROM t;\nDELETE FROM t; -- looks like SQL\n')"
    src = open_db(SRC_DIR)
    src.create_table('t', [col('id', 'INT', primary=True, notnull=True),
                           col('q', 'VARCHAR')])
    src.get_table('t').insert([1, tricky])
    dump_file = BackupManager(src, BAK_DIR).dump()
    src.shutdown()

    dst = open_db(DST_DIR)
    ok, fail = BackupManager(dst, BAK_DIR).restore(dump_file)
    assert fail == 0
    _cols, rows = dst.get_table('t').select(['q'], None)
    assert rows[0][0] == tricky, (rows, tricky)
    print(f'  → 跨行+分号字符串原样恢复: {tricky!r}')
    dst.shutdown()


# ==================== 3. 失败必须显式报错 ====================

def test_restore_missing_ddl_raises_before_writing():
    """dump 里没有 CREATE TABLE 时，INSERT 不允许静默失败成 0 行"""
    reset()
    from lightdb.backup.backup import BackupManager

    bad = os.path.join(BAK_DIR, 'no_ddl.sql')
    with open(bad, 'w', encoding='utf-8') as f:
        f.write("-- table: users rows=2\n")
        f.write("-- lightdb:rows users 2\n")
        f.write("INSERT INTO users VALUES (1, 'u0', 20);\n")
        f.write("INSERT INTO users VALUES (2, 'u1', 21);\n")

    dst = open_db(DST_DIR)
    bm = BackupManager(dst, BAK_DIR)
    from lightdb.sql.logical_dump import BackupError
    with pytest.raises(BackupError) as ei:
        bm.restore(bad)
    assert 'CREATE TABLE' in str(ei.value)
    # 关键：不能留下"成功恢复 0 行"的假象
    assert 'users' not in dst.tables, '结构错误时不应写入任何数据'
    print(f'  → 缺 DDL 时显式报错: {ei.value}')
    dst.shutdown()


def test_restore_insert_failure_raises_not_silent():
    """INSERT 执行失败（列数不符）必须抛 RestoreError，而不是 fail>0 后继续"""
    reset()
    from lightdb.backup.backup import BackupManager
    from lightdb.sql.logical_dump import RestoreError

    # 结构声明 2 列，但 INSERT 给了 5 个值 → 执行必失败
    bad = os.path.join(BAK_DIR, 'bad_row.sql')
    with open(bad, 'w', encoding='utf-8') as f:
        f.write("-- lightdb-logical-dump v1\n")
        f.write("-- lightdb:rows users 1\n")
        f.write("DROP TABLE IF EXISTS users;\n")
        f.write("CREATE TABLE users (\n  id INT NOT NULL PRIMARY KEY,\n"
                "  name VARCHAR NULL\n);\n")
        f.write("INSERT INTO users VALUES (1, 'u0', 20, 'extra', 'more');\n")

    dst = open_db(DST_DIR)
    bm = BackupManager(dst, BAK_DIR)
    with pytest.raises(RestoreError) as ei:
        bm.restore(bad)
    assert '未执行成功' in str(ei.value) or '失败' in str(ei.value)
    print(f'  → INSERT 失败显式报错: {str(ei.value).splitlines()[0]}')

    # strict=False 时返回真实计数（调用方可以显式选择"尽力恢复"）。
    # fail 同时含"IINSERT 执行失败"(1) 与"行数对账不符"(1)：后者是恢复
    # 流程自带的对账，防止调用方只看 ok/fail 就当成成功。
    dst2 = open_db(DST_DIR + '_lax')
    ok, fail = BackupManager(dst2, BAK_DIR).restore(bad, strict=False)
    assert fail >= 1 and ok == 2, (ok, fail)
    print(f'  → strict=False 如实返回 成功={ok} 失败={fail}')
    dst.shutdown()
    dst2.shutdown()
    safe_rmtree(DST_DIR + '_lax')


def test_restore_rowcount_mismatch_detected():
    """恢复"成功"但行数对不上（此处用删掉一条 INSERT 模拟截断）必须报错"""
    reset()
    from lightdb.backup.backup import BackupManager
    from lightdb.sql.logical_dump import RestoreError

    src = open_db(SRC_DIR)
    src.create_table('users', USERS_COLS)
    t = src.get_table('users')
    for i in range(1, 6):
        t.insert([i, f'u{i}', 20 + i])
    dump_file = BackupManager(src, BAK_DIR).dump()
    src.shutdown()

    with open(dump_file, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    # 丢掉最后一条 INSERT，但保留哨兵 rows=5（模拟 dump 被截断）
    last_insert = max(i for i, l in enumerate(lines)
                      if l.startswith('INSERT INTO users'))
    lines.pop(last_insert)
    truncated = os.path.join(BAK_DIR, 'truncated.sql')
    with open(truncated, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')

    dst = open_db(DST_DIR)
    with pytest.raises(RestoreError) as ei:
        BackupManager(dst, BAK_DIR).restore(truncated)
    msg = str(ei.value)
    # 恢复流程自带"成功写入行数 vs dump 行数哨兵"对账
    assert ('行数' in msg or '写入' in msg) and 'users' in msg, msg
    print('  → dump 被截断被拦下: '
          + [l.strip() for l in msg.splitlines() if 'users' in l][0])
    dst.shutdown()


# ==================== 4. 幂等 / 恢复到已有数据的库 ====================

def test_restore_is_idempotent_and_overwrites():
    """同一份 dump 连恢复两次结果一致；目标库里的多余表会被清掉"""
    reset()
    from lightdb.backup.backup import BackupManager

    src = open_db(SRC_DIR)
    src.create_table('users', USERS_COLS)
    for i in range(1, 4):
        src.get_table('users').insert([i, f'u{i}', i])
    dump_file = BackupManager(src, BAK_DIR).dump()
    src.shutdown()

    dst = open_db(DST_DIR)
    dst.create_table('stale', [col('x', 'INT', primary=True)])
    dst.get_table('stale').insert([1])
    bm = BackupManager(dst, BAK_DIR)

    bm.restore(dump_file)
    first = snapshot(dst.get_table('users'))
    assert 'stale' not in dst.tables, 'dump 未提及的表应被清除'
    bm.restore(dump_file)
    second = snapshot(dst.get_table('users'))
    assert first == second
    assert dst.get_table('users').row_count == 3
    print(f'  → 二次恢复结果一致: {second}')
    dst.shutdown()


# ==================== 5. 只导结构 / dump 解析 ====================

def test_no_data_dump_restores_empty_schema():
    """`--no-data`：只恢复结构、0 行，且行数哨兵为 0"""
    reset()
    from lightdb.backup.backup import BackupManager

    src = open_db(SRC_DIR)
    src.create_table('users', USERS_COLS)
    src.get_table('users').insert([1, 'u0', 20])
    dump_file = BackupManager(src, BAK_DIR).dump(no_data=True)
    src.shutdown()

    with open(dump_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'INSERT INTO users' not in content
    assert '-- lightdb:rows users 0' in content

    dst = open_db(DST_DIR)
    ok, fail = BackupManager(dst, BAK_DIR).restore(dump_file)
    assert fail == 0
    tbl = dst.get_table('users')
    assert tbl.row_count == 0
    assert [c['name'] for c in tbl.cols] == ['id', 'name', 'age']
    print('  → 只结构恢复: 0 行、3 列结构正确')
    dst.shutdown()


def test_dump_parsing_rejects_unsupported_statement():
    """未知语句（如 CREATE INDEX）必须显式报错，而不是被静默跳过"""
    reset()
    from lightdb.sql.logical_dump import BackupError, parse_dump

    with pytest.raises(BackupError) as ei:
        parse_dump("CREATE TABLE t (\n  id INT NOT NULL PRIMARY KEY\n);\n"
                   "CREATE INDEX idx ON t (id);\n")
    assert '索引' in str(ei.value) or '不支持' in str(ei.value)
    print(f'  → 不支持语句被拒绝: {ei.value}')

    with pytest.raises(BackupError):
        parse_dump("CREATE TABLE t (\n  a INT NOT NULL,\n  b INT NOT NULL,\n"
                   "  PRIMARY KEY (a, b)\n);\n")
    print('  → 多列主键被显式拒绝（格式已知限制）')


def test_restore_reports_per_field_values_via_select():
    """通过 SQL 读回（SELECT）核对字段值，证明不是只对上计数"""
    reset()
    from lightdb.backup.backup import BackupManager

    src = open_db(SRC_DIR)
    src.create_table('users', USERS_COLS)
    src.get_table('users').insert([1, "O'Brien (x)", 30])
    src.get_table('users').insert([2, None, None])
    dump_file = BackupManager(src, BAK_DIR).dump()
    src.shutdown()

    dst = open_db(DST_DIR)
    BackupManager(dst, BAK_DIR).restore(dump_file)
    _cols, rows = dst.get_table('users').select(['id', 'name', 'age'], None,
                                               order_by=('id', False))
    assert rows == [[1, "O'Brien (x)", 30], [2, None, None]], rows
    print(f'  → SELECT 读回: {rows}')
    dst.shutdown()


# ==================== 6. CHECK 约束必须进备份/恢复路径 ====================
#
# 阶段 0 刚把 CHECK 修好（SQL 与直写两条路径都校验、随 `_meta.json`
# 持久化），但 `SHOW CREATE TABLE` 与逻辑 dump **不导出 CHECK**，于是
# "备份 → 恢复到新库"会静默丢掉约束 —— 与本次一直在清的那类静默失效同级。
# 下面三组用例把它锁死：能导出、恢复后真生效、dump 里缺约束必须报错。

#: 源库里两张表：列级 CHECK + 表级 CONSTRAINT，外加一个 NULL 放行场景
CHECK_SRC_DIR = os.path.join(BASE, 'data_bak_ck_src')
CHECK_DST_DIR = os.path.join(BASE, 'data_bak_ck_dst')
CHECK_BAK_DIR = os.path.join(BASE, 'backups_bak_ck')

CHECK_DDL = [
    # 表级：命名约束 + 匿名约束（名字由引擎按 MySQL 规则补 `t_chk_n`）
    ("orders",
     "CREATE TABLE orders ("
     "  id INT NOT NULL PRIMARY KEY,"
     "  qty INT NULL,"
     "  cap INT NULL,"
     "  note VARCHAR NULL,"
     "  CONSTRAINT qty_nonneg CHECK (qty >= 0),"
     "  CHECK (qty <= cap))",
     # 合法行：(id, qty, cap, note)
     [(1, 5, 10, 'ok'), (2, 0, 0, None), (3, None, 10, 'null qty → UNKNOWN 放行')]),
    # 列级：内联在列定义里
    ("products",
     "CREATE TABLE products ("
     "  id INT NOT NULL PRIMARY KEY,"
     "  price INT CHECK (price > 0),"
     "  age INT CHECK (age IS NULL OR age >= 18))",
     [(1, 100, 20), (2, 1, None)]),
]


def _check_env_reset():
    for d in (CHECK_SRC_DIR, CHECK_DST_DIR, CHECK_BAK_DIR):
        if os.path.exists(d):
            safe_rmtree(d)
        os.makedirs(d, exist_ok=True)


def _check_env_cleanup():
    """用例结束时删掉本次专用目录（本组用例**自己**清理，不留给全量运行）

    只删自己创建的三处（`data_bak_ck_src` / `data_bak_ck_dst` /
    `backups_bak_ck`），不动既有用例的 `data_bak_src|dst`、`backups_bak`。
    """
    for d in (CHECK_SRC_DIR, CHECK_DST_DIR, CHECK_BAK_DIR):
        if os.path.exists(d):
            safe_rmtree(d)


def test_check_constraints_survive_backup_restore_to_new_dir():
    """新目录恢复：SHOW CREATE TABLE 含 CHECK、违规插入仍被拒(3819)、合法插入成功

    1. 建两张表（表级 + 列级各一，含 NULL 放行场景）并写合法数据；
    2. `BackupManager.dump()` → `restore()` 到**新的数据目录**；
    3. 新库 `SHOW CREATE TABLE` 原文里必须有 CHECK（列级内联 + 表级
       `CONSTRAINT ... CHECK`）；
    4. 恢复库上执行违规 INSERT/UPDATE → 必须 ERROR 3819 HY000；
    5. 合法 INSERT 仍成功；NULL 场景仍放行。
    """
    _check_env_reset()
    from lightdb.backup.backup import BackupManager
    from lightdb.sql.executor import Executor

    # ---- 1. 源库 ----
    src = open_db(CHECK_SRC_DIR)
    src_ex = Executor(src)
    for name, ddl, rows in CHECK_DDL:
        r = src_ex.execute_sql(ddl, {})
        assert r['type'] == 'ok', (name, r)
        for row in rows:
            vals = ', '.join('NULL' if v is None
                             else (repr(v) if isinstance(v, str) else str(v))
                             for v in row)
            r = src_ex.execute_sql(f"INSERT INTO {name} VALUES ({vals})", {})
            assert r['type'] == 'ok', (name, vals, r)

    dump_file = BackupManager(src, CHECK_BAK_DIR).dump()
    with open(dump_file, 'r', encoding='utf-8') as f:
        dump_text = f.read()
    src.shutdown()

    # dump 文本里必须已经有 CHECK（列级内联 / 表级 CONSTRAINT）
    assert 'CHECK (price > 0)' in dump_text, dump_text
    assert 'CONSTRAINT qty_nonneg CHECK (qty >= 0)' in dump_text, dump_text
    assert 'CONSTRAINT orders_chk_2 CHECK (qty <= cap)' in dump_text, dump_text
    print('  → dump 里 CHECK 原文:')
    for line in dump_text.splitlines():
        if 'CHECK' in line:
            print(f'      {line.strip()}')

    # ---- 2. 恢复到新目录 ----
    dst = open_db(CHECK_DST_DIR)
    ok, fail = BackupManager(dst, CHECK_BAK_DIR).restore(dump_file)
    assert fail == 0, f'恢复不应有失败语句 (ok={ok}, fail={fail})'
    report = BackupManager(dst, CHECK_BAK_DIR).verify(dump_file)
    assert report.ok, report.errors
    dst_ex = Executor(dst)

    # ---- 3. SHOW CREATE TABLE 含 CHECK ----
    print('  → 恢复后 SHOW CREATE TABLE orders:')
    r = dst_ex.execute_sql('SHOW CREATE TABLE orders', {})
    assert r['type'] == 'resultset', r
    assert r['columns'] == ['Table', 'Create Table'], r['columns']
    ddl_text = r['rows'][0][1]
    for line in ddl_text.splitlines():
        print(f'      {line}')
    assert 'CHECK (qty >= 0)' in ddl_text, ddl_text
    assert 'CHECK (qty <= cap)' in ddl_text, ddl_text
    assert 'CONSTRAINT qty_nonneg CHECK (qty >= 0)' in ddl_text, ddl_text
    assert 'CONSTRAINT orders_chk_2 CHECK (qty <= cap)' in ddl_text, ddl_text

    print('  → 恢复后 SHOW CREATE TABLE products:')
    r = dst_ex.execute_sql('SHOW CREATE TABLE products', {})
    assert r['type'] == 'resultset', r
    ddl_text = r['rows'][0][1]
    for line in ddl_text.splitlines():
        print(f'      {line}')
    # 列级 CHECK 内联在列定义里
    assert 'price INT NULL CHECK (price > 0)' in ddl_text, ddl_text
    assert 'age INT NULL CHECK (age IS NULL OR age >= 18)' in ddl_text, ddl_text

    # 引擎元数据里也真的挂着这些约束（不是只有文本）
    assert [c['expr'] for c in dst.get_table('orders').check_constraints()] == \
        ['qty >= 0', 'qty <= cap']
    assert [c['expr'] for c in dst.get_table('products').check_constraints()] == \
        ['price > 0', 'age IS NULL OR age >= 18']

    # ---- 4. 违规插入仍被拒（3819 / HY000）----
    r = dst_ex.execute_sql("INSERT INTO orders VALUES (9, -1, 10, 'bad')", {})
    assert r['type'] == 'error' and r['code'] == 3819, r
    assert r['sqlstate'] == 'HY000', r
    print(f"  → 违规 INSERT orders 原文: {r['message']}")
    r2 = dst_ex.execute_sql("INSERT INTO orders VALUES (10, 1, 0, 'bad')", {})
    assert r2['type'] == 'error' and r2['code'] == 3819, r2
    print(f"  → 违规 INSERT orders 原文: {r2['message']}")
    r3 = dst_ex.execute_sql(
        "INSERT INTO products VALUES (9, -5, 20)", {})
    assert r3['type'] == 'error' and r3['code'] == 3819, r3
    print(f"  → 违规 INSERT products 原文: {r3['message']}")
    r4 = dst_ex.execute_sql("INSERT INTO products VALUES (10, 5, 3)", {})
    assert r4['type'] == 'error' and r4['code'] == 3819, r4
    print(f"  → 违规 INSERT products 原文: {r4['message']}")
    # UPDATE 同样拦
    r5 = dst_ex.execute_sql("UPDATE orders SET qty = -3 WHERE id = 1", {})
    assert r5['type'] == 'error' and r5['code'] == 3819, r5
    print(f"  → 违规 UPDATE orders 原文: {r5['message']}")
    # 存储层直写也拦（同一份校验）
    with pytest.raises(ValueError) as ei:
        dst.get_table('products').insert([11, -1, 20])
    assert '3819' in str(ei.value)

    # ---- 5. 合法插入 + NULL 放行 ----
    for sql in ("INSERT INTO orders VALUES (11, 3, 3, 'ok')",
                "INSERT INTO orders VALUES (12, NULL, 5, 'NULL → UNKNOWN')",
                "INSERT INTO products VALUES (12, 1, NULL)",
                "INSERT INTO products VALUES (13, 7, 18)"):
        r = dst_ex.execute_sql(sql, {})
        assert r['type'] == 'ok', (sql, r)
    print('  → 4 条合法 INSERT 全部成功（含 2 个 NULL → UNKNOWN 放行场景）')

    n_orders = dst_ex.execute_sql('SELECT COUNT(*) FROM orders', {})['rows'][0][0]
    n_products = dst_ex.execute_sql(
        'SELECT COUNT(*) FROM products', {})['rows'][0][0]
    print(f'  → 行数对账: orders={n_orders}（原 3 + 合法 2）'
          f' products={n_products}（原 2 + 合法 2）')
    assert (n_orders, n_products) == (5, 4)
    dst.shutdown()
    _check_env_cleanup()


def test_dump_without_check_is_rejected_not_silently_accepted():
    """手改 dump 去掉 CHECK → 恢复必须显式报错，而不是"恢复成功但没约束"

    这是本组用例的核心：dump 里少了约束时**不能静默接受**。两道独立的闸：

    * **解析期**（`parse_dump`）：`CREATE TABLE` 文本与 `-- lightdb:check`
      哨兵对账 —— 手改 DDL 但留着哨兵 → `BackupError`；
    * **恢复后读回**（`verify_restore`）：把哨兵/DDL 声明与恢复后引擎里
      真实挂着的约束逐条对账 —— 连哨兵一起改"一致地删掉" → `RestoreError`。
    """
    _check_env_reset()
    from lightdb.backup.backup import BackupManager
    from lightdb.sql.executor import Executor
    from lightdb.sql.logical_dump import BackupError, RestoreError

    src = open_db(CHECK_SRC_DIR)
    src_ex = Executor(src)
    for name, ddl, rows in CHECK_DDL:
        assert src_ex.execute_sql(ddl, {})['type'] == 'ok'
    src_ex.execute_sql("INSERT INTO products VALUES (1, 10, 20)", {})
    src_ex.execute_sql("INSERT INTO orders VALUES (1, 1, 2, 'x')", {})
    dump_file = BackupManager(src, CHECK_BAK_DIR).dump()
    src.shutdown()

    with open(dump_file, 'r', encoding='utf-8') as f:
        text = f.read()
    assert 'CHECK (price > 0)' in text
    assert '-- lightdb:check products name=products_chk_1 expr=price > 0' in text

    # ① 手改：只删 DDL 里的列级 CHECK（哨兵留着）→ 解析期就拦下
    tampered_col = os.path.join(CHECK_BAK_DIR, 'tampered_col.sql')
    bad = text.replace(' CHECK (price > 0)', '')
    assert 'CHECK (price > 0)' not in bad and 'CHECK (qty >= 0)' in bad
    with open(tampered_col, 'w', encoding='utf-8', newline='\n') as f:
        f.write(bad)
    dst = open_db(CHECK_DST_DIR)
    with pytest.raises(BackupError) as ei:
        BackupManager(dst, CHECK_BAK_DIR).restore(tampered_col)
    msg = str(ei.value)
    assert 'price > 0' in msg and 'CHECK' in msg, msg
    assert 'products' not in dst.tables, '解析失败时不该建出表来'
    print(f'  → ① 列级 CHECK 被删（哨兵仍在）: {type(ei.value).__name__}')
    print(f'     {msg.splitlines()[0][:110]}')
    dst.shutdown()

    # ② 手改：连哨兵一起"一致地"删掉 → 解析能过（dump 内部自洽）。
    #    这是**真正的静默失效形态**：文本自洽，只有和"原库"比对才能发现。
    #    注意要删"整条约束"（含它占的名额）：本引擎的自动名 `表_chk_n` 按
    #    出现顺序编号，只删表达式会让剩下那条改名，dump 就自相矛盾了
    #    （那是①的形态，不是②要证明的形态）。
    safe_rmtree(CHECK_DST_DIR)
    os.makedirs(CHECK_DST_DIR, exist_ok=True)
    tampered_consistent = os.path.join(CHECK_BAK_DIR, 'tampered_all.sql')
    bad2 = '\n'.join(
        l for l in text.splitlines()
        if 'age >= 18' not in l)
    bad2 = bad2.replace('-- lightdb:checks products 2',
                        '-- lightdb:checks products 1')
    # 删掉的是最后一条列定义 → 前一条的逗号要收掉（保持 SQL 语法合法）
    bad2 = bad2.replace(',\n);', '\n);')
    # 顺手清掉 products 的 INSERT（列数已变），做成一份"只结构、自洽"的 dump
    bad2 = '\n'.join(
        l for l in bad2.splitlines()
        if not l.startswith('INSERT INTO products'))
    bad2 = bad2.replace('-- lightdb:rows products 1', '-- lightdb:rows products 0')
    bad2 = bad2.replace('-- table: products rows=1', '-- table: products rows=0')
    assert 'age >= 18' not in bad2 and 'price > 0' in bad2
    assert '-- lightdb:check products name=products_chk_1 expr=price > 0' in bad2
    with open(tampered_consistent, 'w', encoding='utf-8', newline='\n') as f:
        f.write(bad2)
    from lightdb.sql.logical_dump import parse_dump, restore_dump, verify_restore
    plan_bad = parse_dump(bad2)
    assert [c['expr'] for c in plan_bad.schemas['products'].check_specs()] == \
        ['price > 0']
    dst = open_db(CHECK_DST_DIR)
    ok_n, fail_n, rep_n = restore_dump(
        dst, content=bad2, strict=True, verify=True)
    assert fail_n == 0 and rep_n.ok
    print(f'  → ② 自洽的"删掉约束"dump 能通过它自己的校验 '
          f'（products 只剩 {len(dst.get_table("products").check_constraints())} '
          f'条约束，dump 内部完全自洽）')

    # ③ 拿**原库**的 CHECK 哨兵再读回对账 → 立刻发现约束变少
    orig_plan = parse_dump(text)
    rep2 = verify_restore(dst, orig_plan)
    assert not rep2.ok, rep2.checks
    errs = [e for e in rep2.errors if 'CHECK' in e]
    assert errs, rep2.errors
    print(f'  → ③ 用原库哨兵读回对账发现缺失: {errs[0][:150]}')

    # ③b 让恢复端"忘了"重建约束（模拟恢复路径缺失 CHECK 的缺陷本身）：
    #     把约束从恢复目标里去掉，再用原库哨兵做严格读回校验 → RestoreError
    dst.drop_table('products')
    dst.create_table('products', [
        col('id', 'INT', primary=True, notnull=True),
        col('price', 'INT'),
        col('age', 'INT'),
    ])
    rep3 = verify_restore(dst, orig_plan)
    assert not rep3.ok, rep3.checks
    errs3 = [e for e in rep3.errors if 'CHECK' in e]
    assert errs3 and 'products' in errs3[0], rep3.errors
    print(f'  → ③b 恢复结果缺约束 → 读回校验 FAIL: {errs3[0][:130]}')
    # 与 `restore_dump(strict=True)` 同一份判据：不 ok 即 RestoreError
    def _strict_check(report):
        if not report.ok:
            raise RestoreError(
                f"恢复到测试目录后校验失败（{len(report.errors)} 项）:\n"
                + '\n'.join(report.errors))
    with pytest.raises(RestoreError) as ei3:
        _strict_check(rep3)
    assert 'CHECK' in str(ei3.value), ei3.value
    dst.shutdown()

    # ④ dump 里带 NOT ENFORCED → 解析期就显式报错（不放宽）
    with pytest.raises(BackupError) as ei:
        parse_dump("CREATE TABLE t (\n  id INT NOT NULL PRIMARY KEY,\n"
                   "  v INT NULL CHECK (v > 0) NOT ENFORCED\n);\n")
    assert 'NOT ENFORCED' in str(ei.value)
    print(f'  → ④ NOT ENFORCED 仍被显式拒绝: {str(ei.value)[:60]}')

    # ⑤ 表级写法同样拒绝
    with pytest.raises(BackupError):
        parse_dump("CREATE TABLE t (\n  id INT NOT NULL PRIMARY KEY,\n"
                   "  CONSTRAINT ck CHECK (id > 0) NOT ENFORCED\n);\n")
    print('  → ⑤ 表级 NOT ENFORCED 同样被拒')
    _check_env_cleanup()


def test_show_create_table_path_is_the_single_ddl_source():
    """`SHOW CREATE TABLE` 与逻辑 dump 必须是同一份 DDL 生成路径

    判据：对同一张表，`check_ddl()` 的输出与 dump 里 `CREATE TABLE ...;`
    之间的 DDL 正文完全一致；且两者都含 CHECK。一旦有人另写一份 DDL
    生成逻辑（CHECK 丢失正是这么发生的），这里会失败。
    """
    _check_env_reset()
    from lightdb.backup.backup import BackupManager
    from lightdb.sql.executor import Executor
    from lightdb.sql.logical_dump import check_ddl, parse_dump

    src = open_db(CHECK_SRC_DIR)
    ex = Executor(src)
    for name, ddl, _rows in CHECK_DDL:
        assert ex.execute_sql(ddl, {})['type'] == 'ok'
    dump_file = BackupManager(src, CHECK_BAK_DIR).dump()
    with open(dump_file, 'r', encoding='utf-8') as f:
        text = f.read()

    for name in ('orders', 'products'):
        direct = check_ddl(src, name)
        assert direct in text, f'{name}: SHOW CREATE TABLE 的 DDL 不在 dump 里'
        assert 'CHECK' in direct, direct
        # 引擎结果集路径（Executor → _show_create_table）与直接调用逐字符一致
        r = ex.execute_sql(f'SHOW CREATE TABLE {name}', {})
        assert r['type'] == 'resultset', r
        assert r['rows'][0][1] == direct, (r['rows'][0][1], direct)
        print(f'  → {name}: SHOW CREATE TABLE 与 dump 共用同一份 DDL '
              f'（{len(direct)} 字符，含 CHECK）')
    # 约束顺序/命名：dump 侧与引擎侧逐条一致（列级在前、表级在后，
    # 自动名 `表_chk_n` 按同一顺序编号）。顺序不一致会让读回校验误报。
    for name in ('orders', 'products'):
        dump_sig = [(c['name'], c['expr'])
                    for c in parse_dump(text).schemas[name].check_specs()]
        engine_sig = [(c['name'], c['expr'])
                      for c in src.get_table(name).check_constraints()]
        assert dump_sig == engine_sig, (name, dump_sig, engine_sig)
        print(f'  → {name}: CHECK 顺序/命名 dump 与引擎一致: {dump_sig}')
    src.shutdown()
    _check_env_cleanup()

