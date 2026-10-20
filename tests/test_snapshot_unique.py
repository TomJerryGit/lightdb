# -*- coding: utf-8 -*-
"""P0-3：快照唯一性 / 去重 / 旧索引迁移（纯 core，无需数据库）。

验收（docs/GUI_OPTIMIZATION_ROADMAP.md P0-3）：
* 连续同名 `create_snapshot` → 名字唯一（`_2`/`_3` 后缀），结果返回实际名字；
* `remove(name)` 删除**所有**同名条目；`list_all()` 不再出现重复；
* 旧索引里的重复项加载时合并（保留文件、重写索引、产生告警）。
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from lightdb_studio.core.snapshot import Snapshot, SnapshotManager


class FakeConn:
    """满足 create_snapshot 最小接口的假连接（不依赖引擎/端口）。"""

    def __init__(self):
        self.config = SimpleNamespace(name='fake-conn', database='fake_db')

    def get_tables(self):
        return ['t1']

    def query(self, sql):
        s = sql.strip().upper()
        if s.startswith('SHOW CREATE TABLE'):
            return ['Table', 'Create Table'], [
                ('t1', 'CREATE TABLE `t1` (`id` INT PRIMARY KEY)')]
        if s.startswith('SELECT'):
            return ['id'], [(1,), (2,)]
        return [], []

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO'}]


def mgr(tmp_path):
    return SnapshotManager(dir=str(tmp_path))


def create(manager, name, conn=None):
    return manager.create_snapshot(conn or FakeConn(), name=name,
                                   tables=['t1'])


# ==================== 1. 同名 → 唯一名 ====================

def test_same_name_gets_unique_suffix(tmp_path):
    m = mgr(tmp_path)
    r1 = create(m, 'dup')
    r2 = create(m, 'dup')
    r3 = create(m, 'dup')
    assert r1.ok and r2.ok and r3.ok, (r1.error, r2.error, r3.error)
    names = [r1.snapshot.name, r2.snapshot.name, r3.snapshot.name]
    print(f"  → 连续同名创建得到: {names}")
    assert names == ['dup', 'dup_2', 'dup_3'], names
    # 结果里返回的是实际名字（strict=False 时也要一致）
    files = [r.snapshot.file for r in (r1, r2, r3)]
    assert len(set(files)) == 3, files
    for f in files:
        assert Path(f).exists() and Path(f).stat().st_size > 0, f
    listed = [s.name for s in m.list_all()]
    assert sorted(listed) == ['dup', 'dup_2', 'dup_3'], listed
    assert len(listed) == len(set(listed)), 'list_all 出现重复'


def test_strict_false_returns_snapshot_with_actual_name(tmp_path):
    m = mgr(tmp_path)
    create(m, 'snap')
    snap = m.create_snapshot(FakeConn(), name='snap', tables=['t1'],
                             strict=False)
    assert isinstance(snap, Snapshot)
    assert snap.name == 'snap_2', snap.name


def test_name_with_path_separators_is_sanitized(tmp_path):
    m = mgr(tmp_path)
    r = create(m, '../evil')
    name = r.snapshot.name
    print(f"  → 危险名 '../evil' → {name!r}, file={r.snapshot.file}")
    assert '/' not in name and '\\' not in name and '..' not in name, name
    assert Path(r.snapshot.file).parent == Path(tmp_path), r.snapshot.file
    assert not (Path(tmp_path).parent / 'evil.sql').exists()


# ==================== 2. remove 删除全部同名 ====================

def test_remove_deletes_all_same_name_entries(tmp_path):
    m = mgr(tmp_path)
    create(m, 'x', FakeConn())
    create(m, 'x', FakeConn())          # → x_2
    # 模拟旧索引残留：手工再塞一条同名 'x' 条目（文件复制一份，保持真实存在）
    old = m.get('x')
    extra_file = Path(tmp_path) / 'legacy_duplicate.sql'
    extra_file.write_text(Path(old.file).read_text(encoding='utf-8'),
                          encoding='utf-8')
    m.snapshots.append(Snapshot(
        name='x', connection='legacy', database='legacy_db',
        tables=['t1'], file=str(extra_file), created_at=old.created_at - 10,
        size=extra_file.stat().st_size))

    assert m.remove('x') is True
    names = [s.name for s in m.list_all()]
    assert 'x' not in names, names
    assert not Path(old.file).exists(), '同名快照文件未删除'
    assert not extra_file.exists(), '旧重复条目文件未删除'
    # 其它名字不受影响
    assert any(s.name == 'x_2' for s in m.snapshots)
    assert m.remove('不存在的名字') is False


# ==================== 3. list_all / 旧索引迁移 ====================

def test_list_all_is_defensively_deduplicated(tmp_path):
    m = mgr(tmp_path)
    create(m, 'a')
    a = m.get('a')
    m.snapshots.append(Snapshot(name='a', connection='dup', database='d',
                                tables=[], file=a.file,
                                created_at=a.created_at - 5))
    listed = m.list_all()
    assert [s.name for s in listed] == ['a'], [s.name for s in listed]


def test_old_index_duplicates_are_migrated(tmp_path):
    m = mgr(tmp_path)
    create(m, 'keep1')
    create(m, 'keep2')
    index = tmp_path / 'index.json'
    data = json.loads(index.read_text(encoding='utf-8'))
    # 老索引：把同一份条目重复写入 2 次（历史缺陷会累积）
    dup = dict(data['snapshots'][0])
    data['snapshots'].extend([dict(dup), dict(dup)])
    index.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    files_before = {p.name for p in tmp_path.glob('*.sql')}
    assert len(files_before) == 2

    m2 = SnapshotManager(dir=str(tmp_path))
    names = [s.name for s in m2.list_all()]
    print(f"  → 迁移后索引: {names}; warnings={m2.index_warnings}")
    assert len(names) == 2, names
    assert len(names) == len(set(names)), names
    # 迁移写回索引：文件里不再有重复条目
    raw = json.loads(index.read_text(encoding='utf-8'))
    assert len(raw['snapshots']) == 2, raw['snapshots']
    # 文件一个都不能丢
    assert {p.name for p in tmp_path.glob('*.sql')} == files_before
    assert any('迁移' in w for w in m2.index_warnings), m2.index_warnings


def test_new_snapshot_after_migration_keeps_unique(tmp_path):
    m = mgr(tmp_path)
    create(m, 'same')
    index = tmp_path / 'index.json'
    data = json.loads(index.read_text(encoding='utf-8'))
    data['snapshots'].append(dict(data['snapshots'][0]))
    index.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    m2 = SnapshotManager(dir=str(tmp_path))
    r = create(m2, 'same')
    assert r.snapshot.name == 'same_2', r.snapshot.name
    names = [s.name for s in m2.list_all()]
    assert sorted(names) == ['same', 'same_2'], names


def test_get_returns_newest_duplicate(tmp_path):
    m = mgr(tmp_path)
    create(m, 'g')
    g = m.get('g')
    newer = Snapshot(name='g', connection='new', database='d', tables=[],
                     file=g.file, created_at=g.created_at + 100)
    m.snapshots.append(newer)
    assert m.get('g').connection == 'new'
    assert m.get('missing') is None


if __name__ == '__main__':
    import tempfile
    import traceback
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for i, (name, fn) in enumerate(sorted(globals().items())):
            if name.startswith('test_') and callable(fn):
                try:
                    fn(base / f'case{i}')
                    print(f'[PASS] {name}')
                except Exception:
                    failed += 1
                    print(f'[FAIL] {name}')
                    traceback.print_exc()
    sys.exit(1 if failed else 0)
