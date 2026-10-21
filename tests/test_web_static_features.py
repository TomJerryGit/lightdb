# -*- coding: utf-8 -*-
"""Web 前端接入 P2 新 API 的静态 + 接口契约测试（不启动浏览器）。

覆盖：
- 静态：格式化按钮 / 报表模板区 / 数据质量区控件 id/data-action；
  app.js 使用后端 3 组 API 路径、textContent 渲染、blob 下载、统一 detail
  错误、loading/disabled、token 失效提示；无危险 innerHTML 拼接；
- 契约：TestClient 验证 3 组 API 的 JSON 字段/状态码与 static 中路径一致，
  避免前后端路径漂移；static 文件可由 Web 服务正常返回。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'web' / 'static'
INDEX = STATIC / 'index.html'
APP_JS = STATIC / 'app.js'
STYLE = STATIC / 'style.css'
sys.path.insert(0, str(ROOT))

TEST_SECRET = 'web-static-test-secret-0123456789abcdef'
ADMIN_PASSWORD = 'Static-Admin-Pass-9f8e7d6c'
USERS = {'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'}}


# ==================== 静态检查 ====================

def _read(path):
    assert path.exists(), f'缺少文件: {path}'
    return path.read_text(encoding='utf-8')


def test_static_controls_exist():
    html = _read(INDEX)
    required = [
        'id="format-btn"', 'id="sql-editor"',
        'id="tool-reports-btn"', 'id="reports-section"',
        'id="report-name-input"', 'id="report-save-btn"',
        'id="report-refresh-btn"', 'id="report-params-input"',
        'id="report-preview-btn"', 'id="report-preview-output"',
        'id="report-export-html"', 'id="report-export-csv"',
        'id="report-export-xlsx"', 'id="report-status"',
        'id="report-list"',
        'id="tool-quality-btn"', 'id="quality-section"',
        'id="quality-table-input"', 'id="quality-check-btn"',
        'id="quality-status"', 'id="quality-summary"',
        'id="quality-issues"',
    ]
    missing = [r for r in required if r not in html]
    assert not missing, f'index.html 缺少控件: {missing}'
    assert 'data-action="format-sql"' in html
    assert 'data-action="save-template"' in html
    assert 'data-action="quality-check"' in html
    assert 'data-tool="reports"' in html and 'data-tool="quality"' in html
    # 格式化按钮位于查询编辑器工具栏旁
    assert html.index('id="format-btn"') < html.index('id="sql-editor"')


def test_static_js_api_paths_and_safety():
    js = _read(APP_JS)
    for path in ("'/sql/format'", "'/reports/templates'", "'/quality/check'"):
        assert path in js, f'app.js 缺少 API 路径 {path}'
    assert '/reports/templates/' in js
    assert '/preview' in js and '/export?format=' in js
    for fmt in ('html', 'csv', 'xlsx'):
        assert f"'{fmt}'" in js or f'"{fmt}"' in js
    # 后端文本渲染：textContent；禁止 innerHTML 拼接未转义数据
    assert js.count('textContent') >= 10
    assert '.innerHTML' not in js, 'app.js 仍使用 innerHTML（应改用 textContent/DOM）'
    assert 'insertAdjacentHTML' not in js and 'document.write' not in js
    # 下载：blob + a.download + Content-Disposition 文件名
    assert 'createObjectURL' in js and 'a.download' in js
    assert 'Content-Disposition' in js and 'filename' in js
    assert "UTF-8''" in js or 'UTF-8' in js
    # 统一错误：detail / 非 2xx / token 失效
    assert 'extractDetail' in js and 'resp.ok' in js
    assert 'resp.status === 401' in js and '登录已过期' in js
    # loading/禁用 防重复
    assert 'setBusy' in js and 'disabled = true' in js
    # 删除二次确认
    assert 'window.confirm' in js
    # 报表参数化输入提示
    assert 'report-params-input' in js or 'params-input' in js
    assert 'JSON' in js and 'key=value' in js


def test_app_js_syntax_or_balanced():
    js = _read(APP_JS)
    node = shutil.which('node')
    if node:
        proc = subprocess.run([node, '--check', str(APP_JS)],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr[-2000:]
        print('  → node --check app.js OK')
        return
    # 无 Node 时降级检查：括号计数平衡（字符串/模板内的括号通常成对）
    for pair in (('{', '}'), ('(', ')'), ('[', ']')):
        assert js.count(pair[0]) == js.count(pair[1]), \
            f'无 Node，括号计数不平衡: {pair}'
    assert 'function ' in js and 'async function' in js


def test_styles_for_new_panels():
    css = _read(STYLE)
    for selector in ('.right-tabs', '.tab-btn', '.tool-section.hidden',
                     '.report-item', '.params-input', '.status-error',
                     '.level-error', '.level-warning'):
        assert selector in css, selector


def test_no_unescaped_innerhtml_patterns():
    """额外静态扫描：任何残留 innerHTML 都必须是无插值固定字符串。"""
    js = _read(APP_JS)
    for line in js.splitlines():
        if '.innerHTML' not in line:
            continue
        # 当前实现不应出现 innerHTML；出现则只允许 '' 或无 ${} 的固定串
        assert re.search(r'\.innerHTML\s*=\s*(\'\'|"")', line) or \
            '${' not in line, f'可疑 innerHTML 拼接: {line.strip()}'


# ==================== TestClient 契约 ====================

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps(USERS))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.delenv('LIGHTDB_WEB_CONNECTIONS_FILE', raising=False)
    from web.auth import reset_secret_cache
    reset_secret_cache()
    from web.server import create_app
    app = create_app(admin_password=ADMIN_PASSWORD,
                     reports_dir=str(tmp_path / 'reports'))
    return TestClient(app)


def auth(client):
    resp = client.post('/api/v1/login',
                       json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {'Authorization': 'Bearer ' + resp.json()['token']}


class FakeConn:
    def __init__(self):
        self.database = ''

    def switch_database(self, db):
        self.database = db

    def is_connected(self):
        return True

    def query(self, sql, params=None):
        up = str(sql).upper()
        if 'COUNT(*)' in up and 'GROUP BY' not in up:
            return ['c'], [[3]]
        if 'MIN(' in up or 'MAX(' in up:
            return ['min', 'max'], [[1, 3]]
        return ['id', 'name'], [[1, 'a'], [2, 'b']]

    def execute(self, sql, params=None):
        return 1, None

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI'}]


@pytest.fixture
def fake_conn(monkeypatch):
    import web.api as api
    fake = FakeConn()
    monkeypatch.setattr(api, '_get_conn', lambda token, name=None: fake)
    return fake


def test_route_paths_match_static(client):
    js = _read(APP_JS)
    # FastAPI 新版本用 _IncludedRouter 惰性挂载，app.routes 顶层不展开；
    # 用 OpenAPI schema 校验真实路由（与 TestClient 实际路由一致）。
    schema = client.get('/openapi.json').json()
    paths = set(schema.get('paths', {}).keys())
    expected = {
        '/api/v1/sql/format',
        '/api/v1/reports/templates',
        '/api/v1/quality/check',
    }
    for path in expected:
        assert path in paths, f'后端缺少路由 {path}'
        suffix = path.replace('/api/v1', '')
        assert suffix in js, f'app.js 未引用路由 {suffix}'
    # 报表子路径（preview/export）在静态与服务端都存在
    assert any('reports/templates/{name}/preview' in p for p in paths), paths
    assert any('reports/templates/{name}/export' in p for p in paths), paths
    assert '/reports/templates' in js and '/preview' in js and \
        '/export?format=' in js


def test_static_files_served(client):
    idx = client.get('/')
    assert idx.status_code == 200
    assert 'id="format-btn"' in idx.text
    assert 'id="report-preview-btn"' in idx.text
    assert 'id="quality-check-btn"' in idx.text
    js = client.get('/static/app.js')
    assert js.status_code == 200
    assert '/sql/format' in js.text and '/quality/check' in js.text
    css = client.get('/static/style.css')
    assert css.status_code == 200 and '.right-tabs' in css.text


def test_contract_sql_format(client):
    headers = auth(client)
    resp = client.post('/api/v1/sql/format',
                       json={'sql': 'select * from t where a=1'},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(('ok', 'sql', 'length_in', 'length_out')) <= set(data)
    assert data['ok'] is True and 'SELECT' in data['sql']
    empty = client.post('/api/v1/sql/format', json={'sql': '   '},
                        headers=headers)
    assert empty.status_code == 400
    assert empty.json()['detail']['code'] == 'empty_sql'


def test_contract_report_templates_preview_export(client, fake_conn):
    headers = auth(client)
    tmpl = {'name': '前端契约模板',
            'sql': 'SELECT id, name FROM t WHERE name = :name',
            'params': {'name': 'a'}, 'preview_limit': 50}
    save = client.post('/api/v1/reports/templates', json=tmpl,
                       headers=headers)
    assert save.status_code == 200, save.text
    listed = client.get('/api/v1/reports/templates', headers=headers).json()
    assert any(t['name'] == tmpl['name'] for t in listed['templates'])
    preview = client.post(
        '/api/v1/reports/templates/%s/preview' % tmpl['name'],
        json={'params': {'name': 'a'}}, headers=headers)
    assert preview.status_code == 200, preview.text
    pdata = preview.json()
    assert set(('ok', 'columns', 'rows', 'total', 'returned',
                'limit', 'params')) <= set(pdata)
    missing = client.post(
        '/api/v1/reports/templates/%s/preview' % tmpl['name'],
        json={'params': {}}, headers=headers)
    assert missing.status_code == 400
    assert missing.json()['detail']['code'] == 'invalid_params'
    for fmt in ('html', 'csv', 'xlsx'):
        resp = client.post(
            '/api/v1/reports/templates/%s/export?format=%s'
            % (tmpl['name'], fmt), json={'params': {'name': 'a'}},
            headers=headers)
        assert resp.status_code == 200, (fmt, resp.text)
        cd = resp.headers.get('content-disposition', '')
        assert ('.' + fmt) in cd
        assert len(resp.content) > 0
        if fmt == 'html':
            assert 'text/html' in resp.headers['content-type']
        if fmt == 'csv':
            assert 'text/csv' in resp.headers['content-type']
        if fmt == 'xlsx':
            assert 'spreadsheetml' in resp.headers['content-type']
    deleted = client.delete(
        '/api/v1/reports/templates/%s' % tmpl['name'], headers=headers)
    assert deleted.status_code == 200


def test_contract_quality(client, fake_conn):
    headers = auth(client)
    resp = client.post('/api/v1/quality/check', json={'table': 'users'},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(('ok', 'table', 'issues', 'summary')) <= set(data)
    assert data['ok'] is True and data['summary']['total'] == len(data['issues'])
    no_table = client.post('/api/v1/quality/check', json={}, headers=headers)
    assert no_table.status_code == 400
    assert no_table.json()['detail']['code'] == 'table_required'


def test_contract_requires_auth(client):
    for method, url, kwargs in (
            ('post', '/api/v1/sql/format', {'json': {'sql': 'SELECT 1'}}),
            ('get', '/api/v1/reports/templates', {}),
            ('post', '/api/v1/quality/check', {'json': {'table': 't'}})):
        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 401, (url, resp.status_code)
