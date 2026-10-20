# -*- coding: utf-8 -*-
"""P0 UI（2026-10-20 GUI 路线图）回归测试。

覆盖三项 UI P0 的验收（offscreen Qt，无需数据库）：
1. 数值/日期感知排序：88.5 < 270.5、负数/小数正确、NULL 升/降序都固定最后、
   表头点击切换方向（走真实 QTest 鼠标点击）；
2. 结果过滤条：匹配/全部列/大小写/清空恢复/空态提示/与排序叠加、
   1200 行过滤 < 200ms（只 hide/show，不查库）；
3. 错误面板：错误码/SQLSTATE/完整报文/复制按钮 + 查询/连接/权限分类；
   以及修好的 `show_message` 陈旧错误视图缺陷。

Qt 在沙箱里用 offscreen 平台：必须在导入 PySide6 之前设置 QT_QPA_PLATFORM。
"""
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None


def qapp():
    """整个测试会话共享一个 QApplication（被 GC 后建 QWidget 会崩）。"""
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


def make_result(columns, rows, elapsed=1.0):
    from lightdb_studio.ui.result_table import ResultTable
    qapp()
    rt = ResultTable()
    rt.show_result(columns, rows, elapsed)
    qapp().processEvents()
    return rt


def column_texts(result, column, visible_only=False):
    table = result.table
    out = []
    for r in range(table.rowCount()):
        if visible_only and table.isRowHidden(r):
            continue
        item = table.item(r, column)
        out.append(item.text() if item is not None else None)
    return out


def numeric_texts(texts):
    return [float(t) for t in texts if t != 'NULL']


# ==================== 1. 类型化排序 ====================

def test_numeric_sort_ascending_null_last():
    rt = make_result(['id', 'amount'],
                     [[1, 88.5], [2, 270.5], [3, -5.0], [4, None],
                      [5, 0], [6, 12.25]])
    from PySide6.QtCore import Qt
    rt.sort_by(1, Qt.AscendingOrder)
    vals = column_texts(rt, 1)
    print(f"  → asc 顺序: {vals}")
    assert vals[-1] == 'NULL', f'NULL 不在最后: {vals}'
    assert numeric_texts(vals) == sorted(numeric_texts(vals)), vals
    # 核心验收：88.5 必须排在 270.5 前面（旧实现按字符串字典序相反）
    assert vals.index('88.5') < vals.index('270.5'), vals


def test_numeric_sort_descending_null_still_last():
    rt = make_result(['id', 'amount'],
                     [[1, 88.5], [2, 270.5], [3, -5.0], [4, None],
                      [5, 0], [6, 12.25]])
    from PySide6.QtCore import Qt
    rt.sort_by(1, Qt.DescendingOrder)
    vals = column_texts(rt, 1)
    print(f"  → desc 顺序: {vals}")
    assert vals[-1] == 'NULL', f'降序 NULL 也必须最后: {vals}'
    assert numeric_texts(vals) == sorted(numeric_texts(vals), reverse=True), vals
    assert vals[0] == '270.5', vals


def test_date_sort_and_null_last():
    rt = make_result(['id', 'created'], [
        [1, '2026-01-02'],
        [2, '2026-01-01 23:59:59'],
        [3, None],
        [4, '2026-01-01'],
        [5, '2025-12-31 08:00:00'],
    ])
    from PySide6.QtCore import Qt
    rt.sort_by(1, Qt.AscendingOrder)
    vals = column_texts(rt, 1)
    print(f"  → 日期升序: {vals}")
    assert vals[-1] == 'NULL', vals
    assert vals[:-1] == ['2025-12-31 08:00:00', '2026-01-01',
                         '2026-01-01 23:59:59', '2026-01-02'], vals


def test_string_sort_is_case_insensitive():
    rt = make_result(['id', 'name'], [[1, 'banana'], [2, 'Apple'], [3, 'cherry']])
    from PySide6.QtCore import Qt
    rt.sort_by(1, Qt.AscendingOrder)
    vals = column_texts(rt, 1)
    print(f"  → 字符串升序: {vals}")
    assert vals == ['Apple', 'banana', 'cherry'], vals


def test_header_click_toggles_typed_sort():
    """真实表头点击：Qt 内置文本排序之后，我们的类型化重排必须纠正顺序。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    rt = make_result(['id', 'amount'],
                     [[1, 88.5], [2, 270.5], [3, 10.0], [4, None]])
    rt.resize(500, 300)
    rt.show()
    qapp().processEvents()
    header = rt.table.horizontalHeader()
    # 确保列宽足够点击
    for c in (0, 1):
        rt.table.setColumnWidth(c, 120)
    x = header.sectionPosition(1) + header.sectionSize(1) // 2
    QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(x, 5))
    qapp().processEvents()
    vals1 = column_texts(rt, 1)
    print(f"  → 点击一次: {vals1}")
    assert vals1[-1] == 'NULL', vals1
    assert numeric_texts(vals1) == sorted(numeric_texts(vals1)), vals1

    QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(x, 5))
    qapp().processEvents()
    vals2 = column_texts(rt, 1)
    print(f"  → 点击两次: {vals2}")
    assert vals2[-1] == 'NULL', vals2
    assert numeric_texts(vals2) == sorted(numeric_texts(vals2),
                                          reverse=True), vals2
    rt.close()


# ==================== 2. 结果过滤条 ====================

def _big_rows(n=1200):
    statuses = ['paid', 'pending', 'shipped', 'cancelled']
    return [[i, statuses[i % 4], round(10 + i * 0.5, 2)] for i in range(n)]


def test_filter_controls_exist_and_columns():
    rt = make_result(['id', 'status', 'amount'], _big_rows(5))
    assert rt.filter_edit is not None
    assert rt.filter_col.count() == 4, rt.filter_col.count()   # 全部列 + 3
    assert rt.filter_col.itemText(0) == '全部列'
    assert rt.filter_clear.text() == '清空'
    assert not rt.filter_bar.isHidden(), '有结果时过滤条应显示'


def test_filter_match_within_200ms_and_clear():
    rt = make_result(['id', 'status', 'amount'], _big_rows(1200))
    t0 = time.perf_counter()
    visible = rt.apply_filter('paid', column=1)
    elapsed = time.perf_counter() - t0
    print(f"  → 1200 行过滤耗时 {elapsed * 1000:.1f} ms，可见 {visible} 行")
    assert visible == 300, f'paid 行数={visible}'
    assert rt.visible_row_count() == 300
    assert elapsed < 0.2, f'过滤太慢（{elapsed * 1000:.0f}ms ≥ 200ms）'
    assert not rt.filter_bar.isHidden()

    rt.clear_filter()
    assert rt.visible_row_count() == 1200, '清空后没有恢复全部行'
    assert rt.filter_edit.text() == ''
    assert rt.filter_col.currentIndex() == 0
    assert rt.empty_label.isHidden()


def test_filter_all_columns_and_case_switch():
    rows = [[1, 'Beijing', 'Alice'], [2, 'Shanghai', 'Bob'],
            [3, 'beijing', 'Cara']]
    rt = make_result(['id', 'city', 'name'], rows)
    visible = rt.apply_filter('Beijing', column=-1)
    assert visible == 2, f'不区分大小写应匹配 2 行，实际 {visible}'
    rt.filter_case.setChecked(True)
    visible = rt.apply_filter('Beijing', column=-1)
    assert visible == 1, f'区分大小写后 "Beijing" 应只匹配 1 行，实际 {visible}'
    visible = rt.apply_filter('beijing', column=-1)
    assert visible == 1, f'区分大小写后 "beijing" 应只匹配 1 行，实际 {visible}'


def test_filter_empty_state_and_restore():
    rt = make_result(['id', 'status'], _big_rows(30))
    visible = rt.apply_filter('no-such-token-xyz')
    assert visible == 0
    assert not rt.empty_label.isHidden(), '无匹配时应显示空态提示'
    assert rt.table.isHidden(), '无匹配时表格应隐藏'
    assert '无匹配' in rt.empty_label.text()

    rt.clear_filter()
    assert rt.empty_label.isHidden()
    assert not rt.table.isHidden()
    assert rt.visible_row_count() == 30


def test_sort_then_filter_keeps_typed_order():
    rows = [[1, 'paid', 88.5], [2, 'pending', 270.5],
            [3, 'paid', -5.0], [4, 'paid', 12.25], [5, 'paid', None]]
    rt = make_result(['id', 'status', 'amount'], rows)
    from PySide6.QtCore import Qt
    rt.sort_by(2, Qt.AscendingOrder)
    rt.apply_filter('paid', column=1)
    visible_rows = column_texts(rt, 2, visible_only=True)
    print(f"  → 过滤后可见 amount: {visible_rows}")
    assert visible_rows[0] == '-5.0', visible_rows
    assert visible_rows[-1] == 'NULL', visible_rows
    assert numeric_texts(visible_rows) == sorted(numeric_texts(visible_rows)), \
        visible_rows


# ==================== 3. 错误面板 ====================

def test_error_copy_button_and_full_text():
    from lightdb_studio.ui.result_table import ResultTable
    qapp()
    rt = ResultTable()
    msg = '错误码  : 1146\nSQLSTATE: 42S02\n报文    : 表 no_such_table 不存在'
    rt.show_error(msg, errno=1146, sqlstate='42S02',
                  sql='SELECT * FROM no_such_table')
    shown = rt.error_display_text()
    assert '1146' in shown and '42S02' in shown
    assert rt.copy_btn.isVisible() or not rt.copy_btn.isHidden()
    copied = rt.copy_error()
    assert copied == shown
    assert qapp().clipboard().text() == copied
    assert rt.error_category_key == 'query'
    assert '查询错误' in rt.error_label.text()


def test_error_classification_permission_and_connection():
    from lightdb_studio.ui.result_table import ResultTable, classify_error
    qapp()
    rt = ResultTable()
    rt.show_error('Access denied for user', errno=1045, sqlstate='28000')
    assert rt.error_category_key == 'permission', rt.error_category_key
    assert '权限错误' in rt.error_label.text()

    rt.show_error('无法连接: Connection refused', errno=2003)
    assert rt.error_category_key == 'connection', rt.error_category_key
    assert '连接错误' in rt.error_label.text()

    # 纯函数也直接可测（不依赖 Qt 控件）
    assert classify_error(1045, '28000')['key'] == 'permission'
    assert classify_error(2003, 'HY000', 'connect refused')['key'] == 'connection'
    assert classify_error(1064, '42000', 'SQL syntax')['key'] == 'query'


def test_show_message_hides_stale_error():
    """P0-6：上一条错误视图不能留到"影响 N 行"之后（旧 xfail 缺陷）。"""
    from lightdb_studio.ui.result_table import ResultTable
    qapp()
    rt = ResultTable()
    rt.show_error('未知的表 no_such_table', errno=1146, sqlstate='42S02')
    assert not rt.error_view.isHidden()
    rt.show_message('影响 1 行')
    assert '影响 1 行' in rt.info_label.text()
    assert rt.error_view.isHidden(), '陈旧错误视图没有收起'
    assert not rt.table.isHidden(), '表格没有恢复显示'


def test_show_message_hides_filter_bar():
    from lightdb_studio.ui.result_table import ResultTable
    qapp()
    rt = ResultTable()
    rt.show_result(['id'], [[1], [2]])
    assert not rt.filter_bar.isHidden()
    rt.show_message('影响 1 行')
    assert rt.filter_bar.isHidden(), '无结果集消息时过滤条应隐藏'


if __name__ == '__main__':
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'[PASS] {name}')
            except Exception:
                failed += 1
                print(f'[FAIL] {name}')
                traceback.print_exc()
    sys.exit(1 if failed else 0)
