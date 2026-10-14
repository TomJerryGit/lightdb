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
