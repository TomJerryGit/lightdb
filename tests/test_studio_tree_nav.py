# -*- coding: utf-8 -*-
"""task-87：对象树过滤 / 双击打开 / 右键菜单 / 刷新反馈 / 懒加载 / 多库保持。"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

_APP = None


def qapp():
    from PySide6.QtWidgets import QApplication
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    qapp()
    return tmp_path


class FakeConn:
    def __init__(self, table_count=5):
        self.config = SimpleNamespace(name='c1', host='h', port=8898,
                                      user='root')
        self.db = ''
        self.tables = [f'table_{i:03d}' for i in range(table_count)]

    def get_databases(self):
        return ['db1', 'db2']

    def get_tables(self):
        return list(self.tables)

    def switch_database(self, db):
        self.db = db

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI'}]

    def query(self, sql, params=None):
        up = str(sql).upper()
        if 'SHOW' in up:
            return [], []
        if 'COUNT(*)' in up:
            return ['COUNT(*)'], [[1]]
        return ['id'], [[1]]


def _items(tree):
    out = []
    def walk(parent):
        for i in range(parent.childCount()):
            item = parent.child(i)
            out.append(item)
            walk(item)
    walk(tree.tree.invisibleRootItem())
    return out


def _find(tree, text):
    return next((i for i in _items(tree) if i.text(0).endswith(text)), None)


def _db_item(tree, db):
    return next((i for i in _items(tree)
                 if i.data(0, 256) and i.data(0, 256)[0] == 'db'
                 and i.data(0, 256)[1] == db), None)


def _table_item(tree, name):
    return next((i for i in _items(tree)
                 if i.data(0, 256) and i.data(0, 256)[0] == 'table'
                 and i.data(0, 256)[1] == name), None)


def test_lazy_load_filter_double_click_context(home):
    from lightdb_studio.ui.object_tree import ObjectTree, NODE_DB, NODE_TABLE
    tree = ObjectTree()
    conn = FakeConn()
    tree.add_connection(conn.config, conn)
    try:
        db1 = _db_item(tree, 'db1')
        assert db1 is not None
        # 懒加载：DB 节点只有“加载中...”占位，未查询表
        assert db1.childCount() == 1
        assert '加载中' in db1.child(0).text(0)
        tree._load_db_children(db1)
        assert _table_item(tree, 'table_000') is not None
        # 过滤即时匹配（大小写不敏感）
        tree.filter_edit.setText('TABLE_003')
        qapp().processEvents()
        visible = [i for i in _items(tree)
                   if i.data(0, 256) and i.data(0, 256)[0] == NODE_TABLE
                   and not i.isHidden()]
        assert {i.data(0, 256)[1] for i in visible} == {'table_003'}
        tree.filter_edit.setText('')
        # 双击表 → table_double_clicked 信号（主窗口打开数据编辑器）
        got = []
        tree.table_double_clicked.connect(got.append)
        table = _table_item(tree, 'table_001')
        tree.tree.setCurrentItem(table)
        tree._on_double_click(table, 0)
        assert got == ['table_001']
        # 右键菜单：打开表/设计表/导出/复制表名
        menu, handlers = tree.build_context_menu(table)
        texts = [a.text() for a in menu.actions()]
        for expected in ('打开表', '设计表', '导出数据…', '复制表名'):
            assert expected in texts, texts
        open_act = next(a for a in menu.actions() if a.text() == '打开表')
        handlers[open_act]()
        assert got[-1] == 'table_001'
    finally:
        tree.close()


def test_refresh_feedback_and_multidb_state(home):
    from lightdb_studio.ui.object_tree import ObjectTree
    tree = ObjectTree()
    conn = FakeConn(table_count=3)
    tree.add_connection(conn.config, conn)
    try:
        db1 = _db_item(tree, 'db1')
        db2 = _db_item(tree, 'db2')
        tree._load_db_children(db1)
        tree._load_db_children(db2)
        db1.setExpanded(True)
        db2.setExpanded(True)
        table = _table_item(tree, 'table_001')
        tree.tree.setCurrentItem(table)
        errors = tree.refresh_all()
        assert errors == []
        assert '已刷新' in tree.refresh_status_label.text()
        # 多库展开状态保持
        db1 = _db_item(tree, 'db1')
        db2 = _db_item(tree, 'db2')
        assert db1.isExpanded() is True
        assert db2.isExpanded() is True
        cur = tree.tree.currentItem()
        assert cur is not None and cur.data(0, 256) == ('table', 'table_001')
    finally:
        tree.close()


def test_filter_100_tables_performance(home):
    from lightdb_studio.ui.object_tree import ObjectTree
    tree = ObjectTree()
    conn = FakeConn(table_count=120)
    tree.add_connection(conn.config, conn)
    try:
        db1 = _db_item(tree, 'db1')
        tree._load_db_children(db1)
        t0 = time.perf_counter()
        tree.filter_edit.setText('table_11')
        qapp().processEvents()
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, elapsed
        visible = [i for i in _items(tree)
                   if i.data(0, 256) and i.data(0, 256)[0] == 'table'
                   and not i.isHidden()]
        assert visible and all('table_11' in i.data(0, 256)[1]
                               for i in visible)
        print(f'  → 120 表过滤 {elapsed * 1000:.1f}ms')
    finally:
        tree.close()
