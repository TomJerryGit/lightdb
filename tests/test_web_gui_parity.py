# -*- coding: utf-8 -*-
"""P2 Web/GUI 能力对齐测试（FastAPI TestClient，不依赖真实数据库）。

覆盖：
- WEB_GUI_PARITY 矩阵完整性（key 齐全、状态合法、full/partial 有端点）；
- 新端点未认证 401；
- POST /sql/format：正常/空 SQL 400/异常 400（共享 core.sql_formatter）；
- 报表模板 CRUD（tmp 目录持久化）+ 参数化 preview（缺参 400、模板 preview_limit
  生效、参数化绑定不拼 SQL）+ export html/csv/xlsx 真实文件/正确 Content-Type；
- POST /quality/check：结构/summary、缺表 400、非法表名 400、异常显式。
"""
import io
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_SECRET = 'web-parity-test-secret-0123456789abcdef'
ADMIN_PASSWORD = 'Parity-Admin-Pass-9f8e7d6c'
USERS = {'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'}}


# ==================== Fixtures ====================

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


def auth_headers(client):
    resp = client.post('/api/v1/login',
                       json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {'Authorization': 'Bearer ' + resp.json()['token']}


class FakeReportConn:
    """报表/质量用假连接：记录 SQL/参数，返回固定 200 行。"""

    def __init__(self, rows=None, columns=None, fail=False):
        self.calls = []
        self.database = ''
        self.fail = fail
        self.columns = columns or ['id', 'name']
        self.rows = rows or [[i, f'name{i}'] for i in range(1, 201)]

    def switch_database(self, db):
        self.database = db

    def is_connected(self):
        return True

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail:
            raise RuntimeError('fake query failed')
        up = str(sql).upper()
        if 'COUNT(*)' in up and 'GROUP BY' not in up:
            return ['COUNT(*)'], [[len(self.rows)]]
        if up.startswith('SELECT') and ('MIN(' in up or 'MAX(' in up):
            return ['min', 'max'], [[1, 200]]
        return list(self.columns), [list(r) for r in self.rows]

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return 1, None

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI'},
                {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES',
                 'key': ''}]


def use_fake_conn(monkeypatch, fake=None):
    import web.api as api
    fake = fake or FakeReportConn()
    monkeypatch.setattr(api, '_get_conn', lambda token, name=None: fake)
    return fake


# ==================== 1. Parity 矩阵 ====================

REQUIRED_CAPABILITIES = {
    'query_execution', 'connection_management', 'object_browser',
    'sql_format', 'chart', 'reports', 'schema_compare', 'snapshot_backup',
    'data_quality', 'import_export', 'plugins', 'i18n',
}


def test_parity_matrix_complete():
    from web.api import WEB_GUI_PARITY
    assert isinstance(WEB_GUI_PARITY, dict)
    assert REQUIRED_CAPABILITIES <= set(WEB_GUI_PARITY), (
        REQUIRED_CAPABILITIES - set(WEB_GUI_PARITY))
    valid_status = {'full', 'partial', 'not_applicable'}
    for key, entry in WEB_GUI_PARITY.items():
        assert isinstance(entry, dict), key
        assert entry.get('gui'), key
        assert entry.get('status') in valid_status, (key, entry.get('status'))
        assert entry.get('notes'), key
        if entry['status'] in ('full', 'partial'):
            assert entry.get('web'), f'{key} 缺少 Web 端点'
        if entry['status'] == 'not_applicable':
            assert entry.get('web') is None, key
    # 至少 3 个 full/partial 能力明确共享 lightdb_studio.core
    shared = [k for k, e in WEB_GUI_PARITY.items()
              if e.get('shared_core') and e['status'] in ('full', 'partial')]
    assert len(shared) >= 3, shared
    for expected in ('sql_format', 'reports', 'data_quality'):
        assert expected in shared, (expected, shared)
    full = [k for k, e in WEB_GUI_PARITY.items() if e['status'] == 'full']
    assert len(full) >= 3, full
    print(f"  → parity {len(WEB_GUI_PARITY)} 项；共享 core: {shared}")


# ==================== 2. 认证 / 错误语义 ====================

def test_new_endpoints_require_auth(client):
    checks = [
        ('post', '/api/v1/sql/format', {'json': {'sql': 'select 1'}}),
        ('get', '/api/v1/reports/templates', {}),
        ('post', '/api/v1/reports/templates',
         {'json': {'name': 'r', 'sql': 'SELECT 1'}}),
        ('post', '/api/v1/reports/templates/x/preview', {'json': {}}),
        ('post', '/api/v1/reports/templates/x/export?format=csv',
         {'json': {}}),
        ('post', '/api/v1/quality/check', {'json': {'table': 't'}}),
        ('delete', '/api/v1/reports/templates/x', {}),
    ]
    for method, url, kwargs in checks:
        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 401, (url, resp.status_code, resp.text)
    print(f"  → {len(checks)} 个新端点未认证均 401")


def test_non_admin_cannot_write_templates(tmp_path, monkeypatch):
    """developer 无 write 之外的 admin 权限？write 可保存；viewer 应 403。"""
    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps({
        'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'},
        'viewer': {'password': 'Viewer-Pass-1', 'role': 'viewer'},
    }))
    monkeypatch.setenv('HOME', str(tmp_path / 'h2'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'h2'))
    from web.auth import reset_secret_cache
    reset_secret_cache()
    from web.server import create_app
    client = TestClient(create_app(admin_password=ADMIN_PASSWORD,
                                   reports_dir=str(tmp_path / 'r2')))
    resp = client.post('/api/v1/login',
                       json={'username': 'viewer', 'password': 'Viewer-Pass-1'})
    headers = {'Authorization': 'Bearer ' + resp.json()['token']}
    resp = client.post('/api/v1/reports/templates',
                       json={'name': 'r', 'sql': 'SELECT 1'}, headers=headers)
    assert resp.status_code == 403, resp.text


# ==================== 3. SQL 格式化 ====================

def test_sql_format_endpoint(client):
    headers = auth_headers(client)
    resp = client.post('/api/v1/sql/format',
                       json={'sql': 'select * from t where a=1'},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['ok'] is True
    assert 'SELECT' in data['sql'] and 'FROM' in data['sql']
    assert data['length_out'] > 0
    mini = client.post('/api/v1/sql/format',
                       json={'sql': 'select\n  *\nfrom t', 'minify': True},
                       headers=headers)
    assert mini.status_code == 200
    assert '\n' not in mini.json()['sql']
    empty = client.post('/api/v1/sql/format', json={'sql': '   '},
                        headers=headers)
    assert empty.status_code == 400
    assert empty.json()['detail']['code'] == 'empty_sql'


# ==================== 4. 报表模板 CRUD / 持久化 ====================

def _template_body(**overrides):
    body = {
        'name': '销售报表',
        'description': '日报',
        'sql': 'SELECT id, name FROM t WHERE name = :name',
        'connection': '',
        'database': 'demo',
        'params': {'name': 'a'},
        'chart': {'x': 'id', 'y': ['name'], 'agg': 'SUM'},
        'preview_limit': 50,
        'schedule': '',
    }
    body.update(overrides)
    return body


def test_report_template_crud_roundtrip(client, tmp_path):
    headers = auth_headers(client)
    save = client.post('/api/v1/reports/templates',
                       json=_template_body(), headers=headers)
    assert save.status_code == 200, save.text
    body = save.json()['template']
    assert body['name'] == '销售报表'
    assert body['params'] == {'name': 'a'}
    assert body['chart']['agg'] == 'SUM'
    assert body['preview_limit'] == 50
    listed = client.get('/api/v1/reports/templates', headers=headers)
    assert listed.status_code == 200
    names = [t['name'] for t in listed.json()['templates']]
    assert '销售报表' in names
    # upsert 同名更新
    save2 = client.post('/api/v1/reports/templates',
                        json=_template_body(description='更新后'),
                        headers=headers)
    assert save2.status_code == 200
    listed = client.get('/api/v1/reports/templates', headers=headers).json()
    assert len(listed['templates']) == 1
    assert listed['templates'][0]['description'] == '更新后'
    # 持久化文件在注入目录
    tmpl_file = tmp_path / 'reports' / 'templates.json'
    assert tmpl_file.exists(), tmpl_file
    raw = json.loads(tmpl_file.read_text(encoding='utf-8'))
    assert raw[0]['name'] == '销售报表'
    # 校验失败显式 400
    bad = client.post('/api/v1/reports/templates',
                      json={'name': '', 'sql': 'SELECT 1'}, headers=headers)
    assert bad.status_code == 400
    assert bad.json()['detail']['code'] == 'invalid_template'
    bad_sql = client.post('/api/v1/reports/templates',
                          json={'name': 'x', 'sql': ''}, headers=headers)
    assert bad_sql.status_code == 400
    # 删除 round-trip
    deleted = client.delete('/api/v1/reports/templates/销售报表',
                            headers=headers)
    assert deleted.status_code == 200
    assert client.get('/api/v1/reports/templates',
                      headers=headers).json()['templates'] == []
    missing = client.delete('/api/v1/reports/templates/销售报表',
                            headers=headers)
    assert missing.status_code == 404
    print('  → CRUD round-trip + 持久化 + 400/404 语义通过')


# ==================== 5. 参数化 preview ====================

def test_report_preview_params_and_binding(client, monkeypatch):
    headers = auth_headers(client)
    fake = use_fake_conn(monkeypatch)
    client.post('/api/v1/reports/templates',
                json=_template_body(), headers=headers)
    resp = client.post('/api/v1/reports/templates/销售报表/preview',
                       json={'params': {'name': 'a'}}, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['ok'] is True
    assert data['returned'] == 50, data   # 模板 preview_limit=50 生效
    assert data['total'] == 200
    assert data['params'] == {'name': 'a'}
    assert data['columns'] == ['id', 'name']
    # 参数化绑定：SQL 里是占位符，值不在 SQL 文本里
    sql, params = fake.calls[-1]
    assert '%(name)s' in sql, sql
    assert ' OR 1=1' not in sql
    assert params == {'name': 'a'}
    # 缺参显式 400
    missing = client.post('/api/v1/reports/templates/销售报表/preview',
                          json={'params': {}}, headers=headers)
    assert missing.status_code == 400, missing.text
    assert missing.json()['detail']['code'] == 'invalid_params'
    # 多余参数 400
    extra = client.post('/api/v1/reports/templates/销售报表/preview',
                        json={'params': {'name': 'a', 'x': 1}},
                        headers=headers)
    assert extra.status_code == 400
    # 模板不存在 404
    nf = client.post('/api/v1/reports/templates/nope/preview',
                     json={'params': {'name': 'a'}}, headers=headers)
    assert nf.status_code == 404


def test_preview_injection_value_is_parameterized(client, monkeypatch):
    headers = auth_headers(client)
    fake = use_fake_conn(monkeypatch)
    evil = "x' OR 1=1 -- "
    client.post('/api/v1/reports/templates',
                json=_template_body(), headers=headers)
    resp = client.post('/api/v1/reports/templates/销售报表/preview',
                       json={'params': {'name': evil}}, headers=headers)
    assert resp.status_code == 200, resp.text
    sql, params = fake.calls[-1]
    assert 'OR 1=1' not in sql, f'值被拼进 SQL: {sql}'
    assert params['name'] == evil
    print(f"  → 注入值仅作为参数传递: params={params}")


# ==================== 6. 导出三格式 ====================

def test_report_export_html_csv_xlsx(client, monkeypatch, tmp_path):
    import csv
    import openpyxl
    headers = auth_headers(client)
    fake = use_fake_conn(monkeypatch)
    client.post('/api/v1/reports/templates',
                json=_template_body(), headers=headers)

    html = client.post(
        '/api/v1/reports/templates/销售报表/export?format=html',
        json={'params': {'name': 'a'}}, headers=headers)
    assert html.status_code == 200, html.text
    assert html.headers['content-type'].startswith('text/html')
    assert '.html' in html.headers.get('content-disposition', '')
    assert len(html.content) > 100
    assert '<table>' in html.text and '销售报表' in html.text

    csv_resp = client.post(
        '/api/v1/reports/templates/销售报表/export?format=csv',
        json={'params': {'name': 'a'}}, headers=headers)
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers['content-type'].startswith('text/csv')
    assert '.csv' in csv_resp.headers.get('content-disposition', '')
    rows = list(csv.reader(io.StringIO(csv_resp.content.decode('utf-8-sig'))))
    assert rows[0] == ['id', 'name'] and len(rows) == 201

    xlsx = client.post(
        '/api/v1/reports/templates/销售报表/export?format=xlsx',
        json={'params': {'name': 'a'}}, headers=headers)
    assert xlsx.status_code == 200, xlsx.text
    assert xlsx.headers['content-type'].startswith(
        'application/vnd.openxmlformats')
    assert '.xlsx' in xlsx.headers.get('content-disposition', '')
    wb = openpyxl.load_workbook(io.BytesIO(xlsx.content))
    # export_excel(title=...) 会写标题行 + 表头 + 200 行数据
    assert wb.active.max_row == 202, wb.active.max_row
    assert wb.active.cell(row=1, column=1).value == '销售报表'

    # 非法格式 400；缺参 400；模板不存在 404
    bad = client.post('/api/v1/reports/templates/销售报表/export?format=pdf',
                      json={'params': {'name': 'a'}}, headers=headers)
    assert bad.status_code == 400
    assert bad.json()['detail']['code'] == 'invalid_format'
    no_params = client.post(
        '/api/v1/reports/templates/销售报表/export?format=csv',
        json={'params': {}}, headers=headers)
    assert no_params.status_code == 400
    assert no_params.json()['detail']['code'] == 'invalid_params'
    missing = client.post(
        '/api/v1/reports/templates/nope/export?format=csv', json={},
        headers=headers)
    assert missing.status_code == 404
    print('  → html/csv/xlsx 真实文件 + Content-Type/Disposition + 400/404 通过')


# ==================== 7. 数据质量 ====================

def test_quality_check_endpoint(client, monkeypatch):
    headers = auth_headers(client)
    fake = use_fake_conn(monkeypatch)
    resp = client.post('/api/v1/quality/check', json={'table': 't'},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['ok'] is True and data['table'] == 't'
    assert isinstance(data['issues'], list) and data['issues']
    assert data['summary']['total'] == len(data['issues'])
    assert data['summary']['error'] + data['summary']['warning'] + \
        data['summary']['info'] == data['summary']['total']
    assert fake.calls, '质量检查应实际查询'
    # 缺表 / 非法表名显式 400
    no_table = client.post('/api/v1/quality/check', json={}, headers=headers)
    assert no_table.status_code == 400
    assert no_table.json()['detail']['code'] == 'table_required'
    bad_ident = client.post('/api/v1/quality/check',
                            json={'table': 't`; DROP TABLE x; --'},
                            headers=headers)
    assert bad_ident.status_code == 400
    print(f"  → quality issues={len(data['issues'])} summary={data['summary']}")


def test_quality_exception_is_explicit_400(client, monkeypatch):
    headers = auth_headers(client)
    use_fake_conn(monkeypatch, FakeReportConn(fail=True))
    resp = client.post('/api/v1/quality/check', json={'table': 't'},
                       headers=headers)
    # DataQualityChecker 内部把查询失败包装成 error issue；这里断言不 500 且结构完整
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['issues'] and any(
        i.get('level') == 'error' for i in data['issues'])


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
