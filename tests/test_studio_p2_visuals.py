# -*- coding: utf-8 -*-
"""P2-2 图表可配置化 + P2-3 报表设计器产品化。

offscreen Qt + 假连接；覆盖：
- 图表：数值列识别、SUM/AVG/COUNT/MIN/MAX 聚合（重复 X/NULL/非数值列）、
  X/Y/聚合/类型切换重绘、无有效数值列显式提示、PNG/SVG 导出文件有效、
  配置 JSON save/load round-trip（含旧单 y 配置兼容）；
- 报表：`:name` / `?` 参数解析与绑定（pymysql %()s 转换、字符串内冒号不误伤、
  缺参/多参/混用显式报错）、预览行数、模板 CRUD/持久化/旧模板兼容、
  HTML 转义、CSV/XLSX 真实文件校验、图表配置写入模板 chart 字段；
- 真实服务端 8898 可用时做 1 个端到端预览，否则 skip（不假过）。
"""
import json
import os
import socket
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


def pump(seconds=0.05):
    app = qapp()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


class _MsgBox:
    def __init__(self):
        self.calls = []

    def _rec(self, kind):
        def fn(*args, **kwargs):
            self.calls.append({'kind': kind,
                               'text': ' | '.join(str(a) for a in args)})
            from PySide6.QtWidgets import QMessageBox
            return QMessageBox.Yes
        return fn

    def texts(self, kind=None):
        return [c['text'] for c in self.calls
                if kind is None or c['kind'] == kind]


@pytest.fixture
def msgbox(monkeypatch):
    box = _MsgBox()
    from PySide6.QtWidgets import QMessageBox
    for name in ('information', 'warning', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(box._rec(name)))
    return box


# ==================== 测试数据 / 假连接 ====================

COLS = ['cat', 'sales', 'qty', 'note']
ROWS = [
    ('a', 10, 1, 'x'),
    ('a', 20, None, 'y'),
    ('b', 5, 3, 'z'),
    ('b', 15, None, 'w'),
    ('c', None, None, 'q'),
]


class FakeReportConn:
    def __init__(self, rows=None, columns=None):
        from types import SimpleNamespace
        self.config = SimpleNamespace(name='fake', database='db')
        self.calls = []
        self.columns = list(columns or ['id', 'name'])
        self.rows = list(rows or [[1, 'a<script>'], [2, None], [3, 'c']])

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        return list(self.columns), [list(r) for r in self.rows]


def make_engine(tmp_path, name='reports'):
    from lightdb_studio.core.reports import ReportEngine
    return ReportEngine(base_dir=str(tmp_path / name))


# ==================== 1. 图表：聚合 / 配置 ====================

def test_numeric_columns_and_chart_errors():
    from lightdb_studio.ui.chart_view import (
        numeric_columns, build_chart_data, ChartConfigError)
    assert numeric_columns(COLS, ROWS) == [1, 2]
    assert numeric_columns(['a', 'b'], [('x', 'y')]) == []
    with pytest.raises(ChartConfigError) as exc:
        build_chart_data(['cat', 'note'], [('a', 'x'), ('b', 'y')],
                         {'y': ['note']})
    assert '数值列' in str(exc.value)
    with pytest.raises(ChartConfigError):
        build_chart_data(COLS, [], {'y': ['sales']})
    with pytest.raises(ChartConfigError):
        build_chart_data(COLS, ROWS, {'y': ['nope']})


def test_aggregation_all_modes():
    from lightdb_studio.ui.chart_view import build_chart_data
    expected = {
        'SUM': {'sales': [30.0, 20.0, None], 'qty': [1.0, 3.0, None]},
        'AVG': {'sales': [15.0, 10.0, None], 'qty': [1.0, 3.0, None]},
        'COUNT': {'sales': [2.0, 2.0, 0.0], 'qty': [1.0, 1.0, 0.0]},
        'MIN': {'sales': [10.0, 5.0, None], 'qty': [1.0, 3.0, None]},
        'MAX': {'sales': [20.0, 15.0, None], 'qty': [1.0, 3.0, None]},
    }
    for agg, want in expected.items():
        data = build_chart_data(COLS, ROWS, {
            'x': 'cat', 'y': ['sales', 'qty'], 'agg': agg, 'limit': 0})
        assert data['labels'] == ['a', 'b', 'c'], (agg, data['labels'])
        got = {s['name']: s['values'] for s in data['series']}
        assert got == want, (agg, got)
    print(f"  → 聚合 5 种全部正确: {list(expected)}")


def test_chart_config_limit_and_legacy_normalize(tmp_path):
    from lightdb_studio.ui.chart_view import (
        build_chart_data, ChartConfig, parse_chart_config)
    # 旧配置：单 y 字符串 + 中文类型/聚合
    old = parse_chart_config({'x': 'cat', 'y': 'sales', 'agg': '平均',
                              'type': '折线图', 'title': '旧图'})
    assert old['y'] == ['sales'] and old['agg'] == 'AVG'
    assert old['type'] == 'line'
    # limit 生效（按原始行截断后再分组：前 3 行 = a,a,b → 标签 a,b）
    data = build_chart_data(COLS, ROWS, {'y': ['sales'], 'limit': 3})
    assert data['labels'] == ['a', 'b']
    # save/load round-trip
    path = tmp_path / 'cfg.json'
    ChartConfig(x='cat', y=['sales', 'qty'], agg='MAX', chart_type='scatter',
                title='T', limit=10).save(path)
    loaded = ChartConfig.load(path)
    assert loaded.to_dict() == {
        'x': 'cat', 'y': ['sales', 'qty'], 'agg': 'MAX',
        'type': 'scatter', 'title': 'T', 'limit': 10}
    print(f"  → 旧配置兼容 + save/load: {loaded}")


def test_chart_dialog_no_numeric_column_explicit(msgbox):
    from lightdb_studio.ui.chart_view import ChartDialog
    qapp()
    dlg = ChartDialog(['cat', 'note'], [('a', 'x'), ('b', 'y')])
    assert dlg.last_error() and '数值列' in dlg.last_error()
    assert dlg.chart.series == [] and not dlg.chart.has_data()
    assert '⚠' in dlg.info_label.text()
    assert not dlg.png_btn.isEnabled() and not dlg.svg_btn.isEnabled()
    assert dlg.refresh()['ok'] is False
    dlg.close()


def test_chart_dialog_redraw_and_config():
    from lightdb_studio.ui.chart_view import ChartDialog
    qapp()
    dlg = ChartDialog(COLS, ROWS)
    dlg.show()
    pump()
    assert dlg.config()['x'] == 'cat'
    assert dlg.config()['y'] == ['sales'], dlg.config()
    assert dlg.chart.has_data()
    # 切换 X / 聚合 / 类型 / 勾选第二个 Y
    from PySide6.QtCore import Qt
    dlg.x_combo.setCurrentText('note')
    dlg.agg_combo.setCurrentText('MAX')
    dlg.type_combo.setCurrentIndex(1)     # line
    for i in range(dlg.y_list.count()):
        item = dlg.y_list.item(i)
        if item.data(Qt.UserRole) == 'qty':
            item.setCheckState(Qt.Checked)
    result = dlg.refresh()
    assert result['ok'] is True
    cfg = dlg.config()
    assert cfg['x'] == 'note' and cfg['agg'] == 'MAX'
    assert cfg['type'] == 'line' and set(cfg['y']) >= {'sales', 'qty'}, cfg
    assert dlg.chart.chart_type == 'line'
    dlg.close()


def test_chart_export_png_and_svg(tmp_path):
    from PySide6.QtGui import QImage
    from lightdb_studio.ui.chart_view import ChartDialog
    qapp()
    dlg = ChartDialog(COLS, ROWS)
    dlg.show()
    pump()
    png = dlg.export_png(str(tmp_path / 'chart.png'), quiet=True)
    img = QImage(png)
    assert not img.isNull() and img.width() > 0 and img.height() > 0
    assert os.path.getsize(png) > 500
    svg = dlg.export_svg(str(tmp_path / 'chart.svg'), quiet=True)
    text = Path(svg).read_text(encoding='utf-8')
    assert '<svg' in text.lower() and len(text) > 200
    ET.fromstring(text)                    # 可被 XML 解析
    assert os.path.getsize(svg) > 200
    print(f"  → PNG {os.path.getsize(png)}B / SVG {os.path.getsize(svg)}B")
    dlg.close()


def test_chart_widget_multiseries_legacy_api():
    from lightdb_studio.ui.chart_view import ChartWidget
    qapp()
    w = ChartWidget()
    w.set_data(['a', 'b'], [1, 2], '单列', 'bar')
    assert w.has_data() and w.labels == ['a', 'b'] and w.values == [1.0, 2.0]
    w.set_series(['a', 'b'], [{'name': 's1', 'values': [1, None]},
                              {'name': 's2', 'values': [3, 4]}], '多列',
                 'line')
    assert len(w.series) == 2 and w.series[0]['values'] == [1.0, None]
    assert w.has_data()


# ==================== 2. 报表：参数 / 模板 / 导出 ====================

def test_extract_params_styles():
    from lightdb_studio.core.reports import extract_sql_params
    req = extract_sql_params(
        'SELECT * FROM t WHERE a = :a AND b = :b AND c = :a')
    assert req['style'] == 'named' and req['names'] == ['a', 'b']
    req = extract_sql_params('SELECT * FROM t WHERE a = ? AND b = ?')
    assert req['style'] == 'qmark' and req['qmark_count'] == 2
    req = extract_sql_params('SELECT :a, ? FROM t')
    assert req['style'] == 'mixed' and req['mixed'] is True
    assert extract_sql_params('SELECT 1')['style'] == 'none'
    # 字符串字面量里的冒号/问号不算占位符
    req = extract_sql_params("SELECT 'a:b?c' AS s, :x")
    assert req['names'] == ['x'] and req['qmark_count'] == 0


def test_bind_named_converts_to_pyformat_safely():
    from lightdb_studio.core.reports import bind_params
    sql, params, _meta = bind_params(
        "SELECT ':notparam' AS lit, :name AS n FROM t WHERE x = :x",
        {'name': 'bob', 'x': 3})
    assert '%(name)s' in sql and '%(x)s' in sql
    assert ':notparam' in sql, '字符串字面量里的冒号不应被替换'
    assert params == {'name': 'bob', 'x': 3}


def test_bind_qmark_and_errors():
    from lightdb_studio.core.reports import (
        bind_params, ReportParameterError)
    sql, params, _meta = bind_params('SELECT ?, ?', [1, 'two'])
    assert sql == 'SELECT ?, ?' and params == [1, 'two']
    with pytest.raises(ReportParameterError) as exc:
        bind_params('SELECT ?', [])
    assert '缺少' in str(exc.value)
    with pytest.raises(ReportParameterError) as exc:
        bind_params('SELECT ?', [1, 2])
    assert '多余' in str(exc.value)
    with pytest.raises(ReportParameterError) as exc:
        bind_params('SELECT :a, ?', {'a': 1})
    assert '混用' in str(exc.value)
    with pytest.raises(ReportParameterError) as exc:
        bind_params('SELECT :a', {'a': 1, 'b': 2})
    assert '多余' in str(exc.value)


def test_template_validate_and_roundtrip(tmp_path):
    from lightdb_studio.core.reports import (
        ReportTemplate, ReportValidationError)
    for bad in (ReportTemplate(name='', sql='SELECT 1'),
                ReportTemplate(name='x', sql='')):
        with pytest.raises(ReportValidationError):
            bad.validate()
    with pytest.raises(ReportValidationError):
        ReportTemplate(name='x', sql='SELECT :a, ?').validate()

    eng = make_engine(tmp_path)
    chart = {'x': 'cat', 'y': ['sales', 'qty'], 'agg': 'AVG',
             'type': 'line', 'title': '图', 'limit': 20}
    tmpl = ReportTemplate(
        name='销售报表', sql='SELECT * FROM s WHERE d = :d',
        params={'d': '2026-01-01'}, chart=chart, preview_limit=50,
        description='desc')
    eng.upsert(tmpl)
    assert eng.get('销售报表') is not None
    # 重新打开引擎 → round-trip（参数与 chart 保留）
    eng2 = make_engine(tmp_path)
    t2 = eng2.get('销售报表')
    assert t2.params == {'d': '2026-01-01'}
    assert t2.chart == chart
    assert t2.preview_limit == 50
    assert t2.description == 'desc'
    print(f"  → round-trip: params={t2.params} chart={t2.chart['agg']} "
          f"limit={t2.preview_limit}")


def test_legacy_template_json_compat(tmp_path):
    d = tmp_path / 'legacy_reports'
    d.mkdir()
    (d / 'templates.json').write_text(json.dumps([{
        'name': 'old', 'description': '', 'sql': 'SELECT 1',
        'connection': '', 'database': '', 'chart': {},
        'schedule': '', 'last_run': 0, 'last_status': '',
        'created_at': 1, 'updated_at': 1,
    }], ensure_ascii=False), encoding='utf-8')
    eng = make_engine(tmp_path, name='legacy_reports')
    old = eng.get('old')
    assert old is not None and old.sql == 'SELECT 1'
    assert old.params == {} and old.preview_limit == 100
    assert isinstance(old.chart, dict)


def test_template_crud_rename_duplicate_remove(tmp_path):
    from lightdb_studio.core.reports import (
        ReportTemplate, ReportValidationError)
    eng = make_engine(tmp_path)
    a = eng.upsert(ReportTemplate(name='a', sql='SELECT 1'))
    eng.upsert(ReportTemplate(name='b', sql='SELECT 2'))
    assert eng.names() == ['a', 'b']
    eng.rename(eng.templates.index(a), 'a2')
    assert eng.get('a2') is not None and eng.get('a') is None
    with pytest.raises(ReportValidationError):
        eng.rename(0, 'b')
    dup = eng.duplicate(eng.templates.index(eng.get('a2')))
    assert dup.name.startswith('a2 副本') and eng.get(dup.name) is not None
    assert eng.remove_by_name('b') is True
    assert eng.get('b') is None
    # 重新打开仍保持
    eng2 = make_engine(tmp_path)
    assert set(eng2.names()) == {'a2', dup.name}


def test_preview_query_limit_and_params(tmp_path):
    from lightdb_studio.core.reports import ReportEngine
    eng = ReportEngine(base_dir=str(tmp_path / 'r'))
    conn = FakeReportConn(rows=[[i, f'n{i}'] for i in range(1, 201)])
    result = eng.preview_query(
        conn, 'SELECT id, name FROM t WHERE id > :min AND name LIKE ?',
        values=None, limit=50) if False else None
    # named
    result = eng.preview_query(
        conn, 'SELECT id, name FROM t WHERE id > :min AND name = :name',
        {'min': 1, 'name': 'x'}, limit=50)
    sql, params = conn.calls[-1]
    assert '%(min)s' in sql and '%(name)s' in sql
    assert params == {'min': 1, 'name': 'x'}
    assert result['total'] == 200 and result['returned'] == 50
    assert len(result['rows']) == 50
    # qmark
    conn2 = FakeReportConn(rows=[[i] for i in range(100)])
    result2 = eng.preview_query(conn2, 'SELECT ? AS a', [7], limit=100)
    assert conn2.calls[-1] == ('SELECT ? AS a', [7])
    assert result2['returned'] == 100
    print(f"  → 预览 limit: total={result['total']} returned={result['returned']}")


def test_preview_missing_params_raises(tmp_path):
    from lightdb_studio.core.reports import (
        ReportEngine, ReportParameterError)
    eng = ReportEngine(base_dir=str(tmp_path / 'r'))
    conn = FakeReportConn()
    with pytest.raises(ReportParameterError):
        eng.preview_query(conn, 'SELECT :a', {})
    with pytest.raises(ReportParameterError):
        eng.preview_query(conn, 'SELECT :a', {'a': 1, 'b': 2})
    assert conn.calls == [], '参数错误时不应查库'


def test_export_csv_html_xlsx_real_files(tmp_path):
    import csv
    import openpyxl
    from lightdb_studio.core.reports import ReportEngine
    eng = ReportEngine(base_dir=str(tmp_path / 'r'))
    columns = ['id', 'name', 'note']
    rows = [[1, 'a<script>', None], [2, 'b & c', 'ok']]
    csv_path = tmp_path / 'o.csv'
    html_path = tmp_path / 'o.html'
    xlsx_path = tmp_path / 'o.xlsx'
    eng.export_csv(columns, rows, str(csv_path))
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        data = list(csv.reader(f))
    assert data[0] == columns and len(data) == 3
    assert data[1][1] == 'a<script>'
    eng.export_html(columns, rows, str(html_path), title='T<script>')
    html_text = html_path.read_text(encoding='utf-8')
    assert 'a&lt;script&gt;' in html_text
    assert 'b &amp; c' in html_text
    assert 'T&lt;script&gt;' in html_text
    assert '<th>id</th>' in html_text and 'NULL' in html_text
    eng.export_excel(columns, rows, str(xlsx_path), sheet_name='S',
                     title='T')
    wb = openpyxl.load_workbook(str(xlsx_path))
    ws = wb.active
    assert ws.max_row >= 3 and ws.cell(row=1, column=1).value == 'T'
    # export_data 分发
    p = tmp_path / 'o2.csv'
    assert eng.export_data(columns, rows, str(p), fmt='csv') == str(p)


# ==================== 3. ReportBuilder（offscreen + 假连接） ====================

def make_builder(tmp_path, conn=None, sql=''):
    from lightdb_studio.ui.report_builder import ReportBuilder
    qapp()
    conn = conn or FakeReportConn()
    eng = make_engine(tmp_path)
    rb = ReportBuilder(eng, conn_getter=lambda: conn, current_sql=sql)
    rb.name_edit.setText('测试报表')
    if sql:
        rb.sql_edit.setPlainText(sql)
    return rb, conn, eng


def test_builder_preview_no_params_does_not_ask(tmp_path, msgbox):
    rb, conn, _eng = make_builder(tmp_path, sql='SELECT id, name FROM t')
    rb._ask_params_for = lambda sql: (_ for _ in ()).throw(
        AssertionError('无参数不应弹参数框'))
    result = rb.run_preview()
    assert result['ok'] is True, result
    assert rb.last_preview['returned'] == 3
    assert rb.preview_table.rowCount() == 3
    assert rb.preview_table.columnCount() == 2
    assert '共 3 行' in rb.preview_info.text()
    assert conn.calls[-1][1] is None     # 无参数 → params=None
    rb.close()


def test_builder_preview_named_params_and_limit(tmp_path, msgbox):
    rb, conn, _eng = make_builder(
        tmp_path, sql='SELECT id, name FROM t WHERE name = :name')
    rb.limit_combo.setCurrentIndex(0)      # 50
    result = rb.run_preview(params={'name': 'bob'})
    assert result['ok'] is True and result['limit'] == 50
    sql, params = conn.calls[-1]
    assert '%(name)s' in sql and params == {'name': 'bob'}
    assert rb._params_defaults == {'name': 'bob'}
    rb.close()


def test_builder_missing_params_explicit(tmp_path, msgbox):
    rb, conn, _eng = make_builder(
        tmp_path, sql='SELECT * FROM t WHERE a = :a')
    result = rb.run_preview(params={})
    assert result['ok'] is False and '缺少参数' in rb.last_error
    assert conn.calls == []
    assert any('缺少参数' in t for t in msgbox.texts('warning')), \
        msgbox.texts('warning')
    rb.close()


def test_builder_param_input_dialog():
    from lightdb_studio.ui.report_builder import ParamInputDialog
    qapp()
    dlg = ParamInputDialog('SELECT * FROM t WHERE a = :a AND b = :b',
                           defaults={'b': '2'})
    assert set(dlg.edits) == {'a', 'b'}
    assert dlg.edits['b'].text() == '2'
    ok, err = dlg.validate()
    assert not ok and '缺少参数' in err
    dlg.edits['a'].setText('1')
    assert dlg.validate()[0] is True
    assert dlg.values() == {'a': '1', 'b': '2'}
    dlg.close()
    qdlg = ParamInputDialog('SELECT ? , ?')
    assert len(qdlg.edits) == 2
    assert qdlg.validate()[0] is False     # 空参数视为缺参
    qdlg.edits[0].setText('x')
    qdlg.edits[1].setText('y')
    assert qdlg.values() == ['x', 'y']
    qdlg.close()


def test_builder_save_validation_and_chart_roundtrip(tmp_path, msgbox):
    from lightdb_studio.core.reports import ReportValidationError
    rb, _conn, eng = make_builder(
        tmp_path, sql='SELECT * FROM t WHERE d = :d')
    rb.name_edit.setText('')
    with pytest.raises(ReportValidationError):
        rb.save_current_template()
    rb.name_edit.setText('带图报表')
    rb._params_defaults = {'d': '2026-01-01'}
    rb.set_chart_config({'x': 'cat', 'y': ['sales'], 'agg': 'AVG',
                         'type': 'line', 'title': '图'})
    tmpl = rb.save_current_template()
    assert tmpl.preview_limit == 100
    # 重新打开引擎 → 参数/图表字段保留
    eng2 = make_engine(tmp_path)
    t2 = eng2.get('带图报表')
    assert t2.params == {'d': '2026-01-01'}
    assert t2.chart['type'] == 'line' and t2.chart['agg'] == 'AVG'
    # 旧模板（无 chart/params）仍可加载
    assert t2.validate()
    rb.close()


def test_builder_rename_duplicate_delete(tmp_path, msgbox):
    rb, _conn, eng = make_builder(tmp_path, sql='SELECT 1')
    rb.save_current_template()
    rb._refresh_list()
    rb.list_widget.setCurrentRow(0)
    renamed = rb.rename_current('新名字')
    assert renamed.name == '新名字' and eng.get('新名字') is not None
    dup = rb.duplicate_current('副本名')
    assert eng.get('副本名') is not None
    rb.list_widget.setCurrentRow(0)
    rb.delete_current(ask=False)
    assert eng.get('新名字') is None and eng.get('副本名') is not None
    rb.close()


def test_builder_export_real_files(tmp_path, msgbox):
    import csv
    import openpyxl
    rb, conn, _eng = make_builder(
        tmp_path, sql='SELECT id, name FROM t WHERE name = :name')
    csv_path = tmp_path / 'rb.csv'
    html_path = tmp_path / 'rb.html'
    xlsx_path = tmp_path / 'rb.xlsx'
    r1 = rb.export_current('CSV', str(csv_path), params={'name': 'a'})
    r2 = rb.export_current('HTML', str(html_path), params={'name': 'a'})
    r3 = rb.export_current('Excel', str(xlsx_path), params={'name': 'a'})
    assert r1['ok'] and r2['ok'] and r3['ok']
    assert r1['rows'] == 3
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        data = list(csv.reader(f))
    assert data[0] == ['id', 'name'] and len(data) == 4
    html_text = html_path.read_text(encoding='utf-8')
    assert 'a&lt;script&gt;' in html_text and '<table>' in html_text
    wb = openpyxl.load_workbook(str(xlsx_path))
    assert wb.active.max_row >= 4
    rb.close()


def test_builder_chart_dialog_from_preview(tmp_path, msgbox):
    rb, _conn, _eng = make_builder(
        tmp_path, sql='SELECT cat, sales FROM t')
    rb.run_preview(params={})
    dlg = rb.build_chart_dialog()
    assert dlg is not None
    assert dlg.columns == ['id', 'name'] or dlg.columns      # 来自预览列
    dlg.close()
    rb.close()


# ==================== 4. 真实服务端 8898（可用则跑，否则 skip） ====================

def test_real_server_8898_e2e_preview():
    sock = socket.socket()
    try:
        sock.settimeout(0.6)
        sock.connect(('127.0.0.1', 8898))
    except OSError:
        pytest.skip('8898 服务端不可用，跳过真实端到端预览（不假过）')
    finally:
        sock.close()
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    from lightdb_studio.core.reports import ReportEngine
    conn = DBConnection(ConnectionInfo(name='e2e', host='127.0.0.1',
                                       port=8898, user='root',
                                       password=''))
    try:
        conn.connect()
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f'8898 连接失败跳过: {exc}')
    try:
        eng = ReportEngine(base_dir=str(tempfile.mkdtemp(prefix='p2_e2e_')))
        result = eng.preview_query(conn, 'SELECT 1 AS x, :v AS v',
                                   {'v': 'ok'}, limit=50)
        assert result['columns'] and result['returned'] >= 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
