# -*- coding: utf-8 -*-
"""task-73 附加项：B+Tree 旧页格式检测 + `scripts/migrate_btree_format.py`

构造一个 task-72 之前的旧格式数据目录（叶页条目 12 字节、根页无 v2
格式标记），验证：
1. 新版本启动时显式拒绝（LegacyBTreeFormatError，含迁移脚本/文档指引）；
2. 迁移脚本逐表读取旧堆页→新格式重建，行数/主键/唯一性保持；
3. 新目录可正常打开、重启，重复主键仍 1062；
4. 已是新格式/目标非空时脚本显式报错，不静默覆盖。
"""
import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _legacy_cols():
    return [
        {'name': 'id', 'type': 'INT', 'primary': True, 'unique': False,
         'notnull': True, 'has_default': False, 'default': None,
         'check': None, 'implicit_pk': False},
        {'name': 'name', 'type': 'VARCHAR(20)', 'primary': False,
         'unique': False, 'notnull': False, 'has_default': False,
         'default': None, 'check': None},
    ]


def make_legacy_dir(base, rows):
    """写一个旧格式（uint32 键、12 字节叶条目、根页 marker=0）数据目录"""
    from lightdb.storage.buffer_pool import BufferPool
    from lightdb.storage.page import PAGE_TYPE_HEAP, PAGE_TYPE_BTREE_LEAF
    from lightdb.storage.btree import PAGE_HEADER_SIZE, LEAF_HEADER

    old = base / 'old'
    old.mkdir(parents=True)
    meta = {
        'tables': {
            'old_t': {
                'cols': _legacy_cols(),
                'partition': None,
                'unique_indexes': [],
                'indexes': [],
                'checks': None,
                'requested_engine': 'LightDB',
                'table_options': {'engine': 'LightDB',
                                  'charset': 'utf8mb4',
                                  'collate': 'utf8mb4_bin',
                                  'comment': None},
                'comment': '',
                'auto_increment_next': {},
            },
        },
        'users': {},
        'views': {},
        'pending_ddl': None,
    }
    with open(old / '_meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    dat = old / 'old_t.dat'
    bp = BufferPool(str(dat))
    heap = bp.new_page(PAGE_TYPE_HEAP)
    entries = []
    for i, (pk, name) in enumerate(rows):
        payload = json.dumps([pk, name], ensure_ascii=False).encode('utf-8')
        row_bytes = struct.pack(
            '<IIBIII', 1, 0, 0, len(payload), 0xFFFFFFFF, 0) + payload
        sid = heap.insert_row(row_bytes)
        assert sid is not None
        entries.append((pk, heap.page_id, sid))
    leaf = bp.new_page(PAGE_TYPE_BTREE_LEAF)
    # 旧格式叶页：n(2) + next(4) + 条目 12 字节（key/pid/sid）
    struct.pack_into('<H', leaf.data, PAGE_HEADER_SIZE, len(entries))
    struct.pack_into('<I', leaf.data, PAGE_HEADER_SIZE + 2, 0)
    for i, (key, pid, sid) in enumerate(entries):
        struct.pack_into('<III', leaf.data,
                         PAGE_HEADER_SIZE + LEAF_HEADER + i * 12,
                         int(key), int(pid), int(sid))
    leaf.dirty = True
    bp.flush()
    bp.close()
    (old / 'old_t.btroot').write_text(str(leaf.page_id))
    return old


def test_legacy_format_is_detected_with_migration_instructions(tmp_path):
    from lightdb.db.database import Database
    from lightdb.db.errors import LegacyBTreeFormatError
    old = make_legacy_dir(tmp_path, [(1, 'a'), (2, 'b')])
    with pytest.raises(LegacyBTreeFormatError) as ei:
        Database(str(old))
    msg = str(ei.value)
    assert 'migrate_btree_format.py' in msg
    assert 'MIGRATION_BTREE_V2.md' in msg
    assert 'old_t' in msg
    print('\n    旧格式启动检测：LegacyBTreeFormatError（含迁移脚本/文档指引）')


def test_migration_script_rebuilds_new_format(tmp_path):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    old = make_legacy_dir(tmp_path, [(1, 'a'), (2, 'b'), (3, 'O\'Brien')])
    new = tmp_path / 'new'
    script = os.path.join(ROOT, 'scripts', 'migrate_btree_format.py')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, script, str(old), str(new)],
                       capture_output=True, text=True, timeout=180, env=env,
                       encoding='utf-8', errors='replace')
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    assert '迁移完成' in p.stdout, p.stdout
    assert '校验=OK' in p.stdout, p.stdout
    # 新目录用当前格式正常打开，数据/主键/唯一性保持
    db = Database(str(new))
    ex = Executor(db)
    try:
        assert ex.execute_sql('SELECT id, name FROM old_t ORDER BY id',
                              {})['rows'] == [
            (1, 'a'), (2, 'b'), (3, "O'Brien")]
        r = ex.execute_sql('INSERT INTO old_t VALUES (1, \'dup\')', {})
        assert r.get('type') == 'error' and r.get('code') == 1062, r
        assert ex.execute_sql('SHOW INDEX FROM old_t', {})['rows'][0][2] == \
            'PRIMARY'
    finally:
        db.shutdown()
    # 重启后仍一致
    db2 = Database(str(new))
    ex2 = Executor(db2)
    try:
        assert ex2.execute_sql('SELECT COUNT(*) FROM old_t', {})['rows'] == \
            [(3,)]
    finally:
        db2.shutdown()
    print('\n    迁移脚本：旧格式→新格式，行数/主键/唯一性校验通过，重启一致')


def test_window_new_format_without_marker_is_not_false_positive(tmp_path):
    """task-72 格式升级到写 marker 之间创建的库：marker=0 但条目是新布局

    启动检测必须按条目结构判定为新格式（不能仅凭 marker=0 就拒绝），
    否则这些合法新库会被误判为旧格式。
    """
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    d = str(tmp_path / 'window_db')
    db = Database(d)
    ex = Executor(db)
    try:
        db.create_table('w', [
            {'name': 'id', 'type': 'INT', 'primary': True,
             'unique': False, 'notnull': True},
            {'name': 'v', 'type': 'INT'}])
        ex.execute_sql('INSERT INTO w VALUES (1,10),(2,20)', {})
    finally:
        db.shutdown()
    # 抹掉根页 v2 标记，模拟"新布局但尚未写标记"的历史库
    root_id = int(open(os.path.join(d, 'w.btroot')).read().strip())
    dat = os.path.join(d, 'w.dat')
    with open(dat, 'r+b') as f:
        f.seek(root_id * 4096 + 21)
        f.write(b'\x00')
    db2 = Database(d)
    ex2 = Executor(db2)
    try:
        assert ex2.execute_sql('SELECT * FROM w ORDER BY id', {})['rows'] == \
            [(1, 10), (2, 20)]
        print('\n    marker=0 的新布局库：结构判定为新格式，正常打开不误报')
    finally:
        db2.shutdown()


def test_migration_script_rejects_new_format_or_nonempty_target(tmp_path):
    from lightdb.db.database import Database
    # 新格式目录：脚本必须说"无需迁移"
    fresh = tmp_path / 'fresh'
    db = Database(str(fresh))
    try:
        db.create_table('t', [
            {'name': 'id', 'type': 'INT', 'primary': True,
             'unique': False, 'notnull': True}])
    finally:
        db.shutdown()
    script = os.path.join(ROOT, 'scripts', 'migrate_btree_format.py')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, script, str(fresh),
                        str(tmp_path / 'out')],
                       capture_output=True, text=True, timeout=120, env=env,
                       encoding='utf-8', errors='replace')
    assert p.returncode != 0
    assert '未检测到旧格式' in (p.stdout + p.stderr)
    # 目标目录非空且没有 --force → 拒绝（不静默覆盖）
    old = make_legacy_dir(tmp_path / 'again', [(1, 'a')])
    target = tmp_path / 'occupied'
    target.mkdir()
    (target / 'keep.txt').write_text('do-not-delete')
    p2 = subprocess.run([sys.executable, script, str(old), str(target)],
                        capture_output=True, text=True, timeout=120, env=env,
                        encoding='utf-8', errors='replace')
    assert p2.returncode != 0
    assert '非空' in (p2.stdout + p2.stderr)
    assert (target / 'keep.txt').exists()
    print('\n    迁移脚本边界：新格式目录/非空目标均显式拒绝，不覆盖数据')
