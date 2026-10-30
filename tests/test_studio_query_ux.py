# -*- coding: utf-8 -*-
"""task-88：查询页体验（状态/排序/复制/列宽/错误/导出/大结果提示/历史）。"""
import json
import os
import sys
from pathlib import Path

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
    monkeypatch.setenv('LIGHTDB_STUDIO_LAYOUT_DIR',
                       str(tmp_path / 'layout'))
    qapp()
    return tmp_path


def make_result(rows=None):
    from lightdb_studio.ui.result_table import ResultTable
    rt = ResultTable()
    rt.show_result(['id', 'name', 'score'],
                   rows or [[1, 'alice', 88.5], [2, 'bob', 270.5],
                            [3, 'carol', None]], elapsed_ms=12.5)
    return rt


def test_local_sort_copy_and_multiline(home):
    rt = make_result()
    try:
        # 表头点击本地排序 ≠ 改 SQL；数值类型化排序
        from PySide6.QtCore import Qt
        rt.sort_by(2, Qt.AscendingOrder)
        vals = [rt.table.item(r, 2).text() for r in range(3)]
        assert vals == ['88.5', '270.5', 'NULL']
        rt.sort_by(2, Qt.DescendingOrder)  # NULL 仍固定最后
        vals_desc = [rt.table.item(r, 2).text() for r in range(3)]
        assert vals_desc[0] == '270.5' and vals_desc[-1] == 'NULL'
        assert 'SELECT' not in rt.info_label.text()
        # 多行/单元格复制 + CSV
        rt.table.setCurrentCell(0, 0)
        rt.table.selectRow(0)
        text = rt.copy_without_headers()
        assert text and '\t' in text
        csv_text = rt.copy_as_csv()
        assert 'id,name,score' in csv_text
        assert rt.copy_with_headers().startswith('id\tname\tscore')
    finally:
        rt.close()


def test_column_width_persistence_and_double_click_fit(home):
    from lightdb_studio.ui.result_table import (
        ResultTable, load_column_widths)
    rt = make_result()
    try:
        rt._user_resizing = True
        rt.table.setColumnWidth(0, 222)
        rt.table.setColumnWidth(1, 333)
        qapp().processEvents()
        saved = load_column_widths()
        assert saved.get('id') == 222
        assert saved.get('name') == 333
        rt._user_resizing = False
        rt2 = make_result()
        try:
            assert rt2.table.columnWidth(0) == 222
            assert rt2.table.columnWidth(1) == 333
            width = rt2.fit_column(0)
            assert width is not None and width >= 40
            assert rt2.column_widths().get('id') == width
        finally:
            rt2.close()
    finally:
        rt.close()


def test_truncated_limit_info_and_quick_export(home, tmp_path):
    from lightdb_studio.ui.result_table import ResultTable
    rt = ResultTable()
    try:
        rt.show_result(['id', 'name'], [[1, 'a'], [2, 'b']],
                       elapsed_ms=3.2, truncated=True, limit=5000)
        assert '已取 2 行' in rt.info_label.text()
        assert '限制 5000 行' in rt.info_label.text()
        assert rt.time_label.text().startswith('3.2')
        assert set(rt.quick_export_btns) == {'csv', 'json', 'sql'}
        for fmt in ('csv', 'json', 'sql'):
            p = tmp_path / f'out.{fmt}'
            n = rt.export_all(p, fmt=fmt)
            assert n == 2 and p.exists() and p.stat().st_size > 0
        json_rows = json.loads((tmp_path / 'out.json').read_text('utf-8'))
        assert json_rows[0]['name'] == 'a'
        sql_text = (tmp_path / 'out.sql').read_text('utf-8')
        assert 'INSERT INTO' in sql_text and "'a'" in sql_text
    finally:
        rt.close()


def test_error_highlight_location_and_copy(home):
    from lightdb_studio.ui.result_table import ResultTable
    rt = ResultTable()
    try:
        rt.show_error('错误码  : 1064\nYou have an error at line 3 near \'FORM\'',
                      errno=1064, sqlstate='42000', sql='SELECT * FORM t')
        assert rt.error_display_text().startswith('错误码: 1064')
        assert rt.error_display_is_selectable()
        assert rt.error_line == 3
        assert '第 3 行' in rt.error_label.text()
        assert rt.info_label.text().startswith('错误')
        copied = rt.copy_error()
        assert 'FORM' in copied and copied == rt.error_display_text()
        # 无行号时不伪造
        rt.show_error('syntax error', errno=1064)
        assert rt.error_line is None
        assert '第 ' not in rt.error_label.text()
    finally:
        rt.close()


def test_history_search_filter(home):
    from lightdb_studio.utils.history import QueryHistory
    from lightdb_studio.ui.query_history import QueryHistoryPanel
    h = QueryHistory()
    h.clear()
    h.add('SELECT * FROM users WHERE id=1', 'c')
    h.add('UPDATE orders SET x=1', 'c')
    panel = QueryHistoryPanel(h)
    try:
        panel.search_edit.setText('orders')
        qapp().processEvents()
        assert panel.list_widget.count() == 1
        item = panel.list_widget.item(0)
        assert 'orders' in item.data(0x0100)  # Qt.UserRole
        panel.search_edit.setText('')
        assert panel.list_widget.count() == 2
    finally:
        panel.close()


def test_execute_cancel_status_strings():
    """状态反馈文本契约（主窗口 _set_status/_on_async_* 使用）。"""
    import inspect
    import lightdb_studio.ui.main_window as mw
    src = inspect.getsource(mw.MainWindow._on_async_result)
    assert '完成 (' in src and '已取' in src and '限制' in src
    src_cancel = inspect.getsource(mw.MainWindow._on_async_cancelled)
    assert '已取消' in src_cancel
