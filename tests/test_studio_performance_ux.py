# -*- coding: utf-8 -*-
"""task-89：启动打点 / 10 万行虚拟化 / 500 表 / 防抖 / 模型释放。"""
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

ROOT = Path(__file__).resolve().parent.parent
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
    monkeypatch.setenv('LIGHTDB_STUDIO_LAYOUT_DIR', str(tmp_path / 'layout'))
    qapp()
    return tmp_path


class FakeConn:
    def __init__(self, count=500):
        self.config = SimpleNamespace(name='c500', host='h', port=8898,
                                      user='root')
        self.db = ''
        self.tables = [f'table_{i:04d}' for i in range(count)]

    def get_databases(self):
        return ['bigdb']

    def get_tables(self):
        return list(self.tables)

    def switch_database(self, db):
        self.db = db

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI'}]

    def query(self, sql, params=None):
        return [], []


def _table_items(tree):
    out = []
    def walk(parent):
        for i in range(parent.childCount()):
            item = parent.child(i)
            out.append(item)
            walk(item)
    walk(tree.tree.invisibleRootItem())
    return out


def test_startup_timing_marks(home, tmp_path):
    env = dict(os.environ)
    env.update({
        'QT_QPA_PLATFORM': 'offscreen',
        'PYTHONIOENCODING': 'utf-8',
        'LIGHTDB_STUDIO_STARTUP_TIMING': '1',
        'HOME': str(tmp_path / 'home'),
        'USERPROFILE': str(tmp_path / 'home'),
        'PYTHONPYCACHEPREFIX': str(tmp_path / 'pycache'),
    })
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8', '-m', 'lightdb_studio.app',
         '--smoke', '--startup-timing'],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=120)
    wall = time.perf_counter() - t0
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    stderr = proc.stderr or ''
    assert 'MainWindow 构造完成' in stderr
    assert '事件循环首个事件' in stderr
    # 解析最后阶段耗时（宽松阈值：CI/冷启动机器差异大）
    marks = []
    for line in stderr.splitlines():
        if line.startswith('[startup]'):
            try:
                marks.append((float(line.split()[1].rstrip('s')),
                              line.split('s', 1)[1].strip()))
            except (ValueError, IndexError):
                pass
    assert marks
    construct = next(v for v, label in marks if 'MainWindow 构造完成' in label)
    visible = next(v for v, label in marks if '事件循环首个事件' in label)
    theme = next(v for v, label in marks if '主题应用完成' in label)
    construct_delta = construct - theme
    print(f'\n  启动打点：MainWindow构造段={construct_delta:.3f}s '
          f'（绝对 {construct:.3f}s），首事件={visible:.3f}s '
          f'进程 wall={wall:.2f}s')
    # 宽松阈值：子进程使用全新 pycache 时绝对导入耗时会受冷缓存影响；
    # 这里断言可控的 MainWindow 构造段，绝对首事件给宽上限。
    assert construct_delta < 5.0, construct_delta
    assert visible < 30.0, visible


def test_100k_rows_virtualized_not_items(home):
    from lightdb_studio.ui.result_table import ResultTable
    n = 100_000
    rows = [[i, f'user-{i}', i % 97, i * 1.5] for i in range(n)]
    rt = ResultTable()
    try:
        t0 = time.perf_counter()
        rt.show_result(['id', 'name', 'bucket', 'score'], rows, elapsed_ms=1.0)
        elapsed = time.perf_counter() - t0
        assert rt._virtual_active is True
        assert rt.table.rowCount() == 0, '十万行不得一次性建 QTableWidgetItem'
        assert rt.virtual_model.rowCount() == n
        assert rt.rows_loaded == n
        assert len(rt.virtual_view.model()._rows) == n
        # 局部访问虚拟模型（滚动只取可见单元格）
        t1 = time.perf_counter()
        for r in range(0, n, 997):
            idx = rt.virtual_model.index(r, 1)
            assert rt.virtual_model.data(idx) == f'user-{r}'
        scan = time.perf_counter() - t1
        print(f'  10 万行 show_result={elapsed:.3f}s 稀疏访问={scan:.4f}s')
        assert elapsed < 10.0, elapsed
        assert scan < 2.0, scan
        # 分块导出不阻塞/不一次性建控件（同步分块写，CSV 行数完整）
        out = home / 'huge.csv'
        t2 = time.perf_counter()
        written = rt.export_all(out, fmt='csv')
        export = time.perf_counter() - t2
        assert written == n and out.exists()
        assert sum(1 for _ in open(out, encoding='utf-8')) == n + 1
        print(f'  10 万行 CSV 导出={export:.3f}s')
        assert export < 20.0, export
    finally:
        rt.close()


def test_500_tables_load_cap_and_animated_restored(home):
    from lightdb_studio.ui.object_tree import (
        ObjectTree, NODE_DB, NODE_TABLE, TABLE_RENDER_LIMIT)
    tree = ObjectTree()
    conn = FakeConn(500)
    tree.add_connection(conn.config, conn)
    try:
        db = next(i for i in _table_items(tree)
                  if i.data(0, 256) and i.data(0, 256)[0] == NODE_DB)
        t0 = time.perf_counter()
        tree._load_db_children(db)
        elapsed = time.perf_counter() - t0
        tables = [i for i in _table_items(tree)
                  if i.data(0, 256) and i.data(0, 256)[0] == NODE_TABLE]
        print(f'  500 表展开={elapsed:.3f}s，节点={len(tables)}，'
              f'动画恢复={tree.tree.isAnimated()}')
        assert len(tables) == TABLE_RENDER_LIMIT
        assert len(tables) <= 500
        assert elapsed < 3.0, elapsed
        assert tree.tree.isAnimated() is True
        # 大树过滤防抖：输入后不立即遍历，flush 后可命中
        tree.filter_edit.setText('table_0499')
        assert tree._filter_timer.isActive()
        tree.flush_filter()
        visible = [i for i in tables if not i.isHidden()]
        assert [i.data(0, 256)[1] for i in visible] == ['table_0499']
    finally:
        tree.close()


def test_filter_debounce_and_switch_releases_cache(home):
    from lightdb_studio.ui.object_tree import ObjectTree, NODE_DB
    tree = ObjectTree()
    conn = FakeConn(260)
    tree.add_connection(conn.config, conn)
    try:
        db = next(i for i in _table_items(tree)
                  if i.data(0, 256) and i.data(0, 256)[0] == NODE_DB)
        tree._load_db_children(db)
        tree.filter_edit.setText('table_0259')
        assert tree._filter_timer.isActive()
        tree.flush_filter()
        tree._switch_database('bigdb')
        assert tree._table_cache == {}
        assert tree._table_count == 0
    finally:
        tree.close()


def test_result_model_release_and_rss(home):
    try:
        import psutil
        proc = psutil.Process()
    except Exception:
        pytest.skip('psutil 不可用')
    from lightdb_studio.ui.result_table import ResultTable
    rows = [[i, f'x{i}', i] for i in range(100_000)]
    rt = ResultTable()
    try:
        rt.show_result(['id', 's', 'n'], rows, elapsed_ms=1.0)
        gc.collect()
        before = proc.memory_info().rss
        # 释放后模型和原始行都必须清空（关闭 tab 的路径）
        rt.release()
        gc.collect()
        qapp().processEvents()
        after = proc.memory_info().rss
        assert rt.rows_loaded == 0
        assert rt._orig_rows == []
        assert rt.virtual_model.rowCount() == 0
        print(f'  release RSS {before / 1024 / 1024:.1f}MB -> '
              f'{after / 1024 / 1024:.1f}MB')
        # 允许缓存/分配器噪声；至少不显著增长
        assert after <= before + 30 * 1024 * 1024
    finally:
        rt.close()
