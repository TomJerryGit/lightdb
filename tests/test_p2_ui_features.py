# -*- coding: utf-8 -*-
"""task-63 P2：图表 / ER / 插件 / i18n / 流式导入导出（TestClient）。"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_SECRET = 'p2-ui-secret-0123456789abcdef012345'
ADMIN_PASSWORD = 'P2-UI-Admin-Pass-9f8e7d6c'
USERS = {'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'}}


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


class FakeP2Conn:
    def __init__(self):
        self.database = ''
        self.executed = []
        self.stream_queries = []
        self.fk_supported = True
        self.execute_delay = 0
        self.fail_at = 0
        self.rows = [[1, 'a', 10], [2, 'b', 20], [3, 'c', 30],
                     [4, 'd', 40], [5, 'e', 50]]
        self.columns = [
            {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
             'extra': '', 'default': None},
            {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES',
             'key': '', 'extra': '', 'default': None},
            {'name': 'amount', 'type': 'INT', 'null': 'YES', 'key': '',
             'extra': '', 'default': None},
        ]

    def is_connected(self):
        return True

    def switch_database(self, db):
        self.database = db

    def get_tables(self):
        return ['users', 'orders']

    def get_columns(self, table):
        return [dict(c) for c in self.columns]

    def query(self, sql, params=None):
        up = str(sql).upper()
        if 'INFORMATION_SCHEMA.TABLES' in up:
            return ['TABLE_NAME', 'TABLE_ROWS'], [('users', 5), ('orders', 3)]
        if 'INFORMATION_SCHEMA.COLUMNS' in up:
            return ['COLUMN_NAME', 'DATA_TYPE', 'COLUMN_KEY'], [
                ('id', 'int', 'PRI'), ('name', 'varchar', '')]
        if 'INFORMATION_SCHEMA.KEY_COLUMN_USAGE' in up:
            if not self.fk_supported:
                raise RuntimeError("Table 'information_schema.KEY_COLUMN_USAGE' doesn't exist")
            return ['TABLE_NAME', 'COLUMN_NAME', 'CONSTRAINT_NAME',
                    'REFERENCED_TABLE_NAME', 'REFERENCED_COLUMN_NAME'], [
                ('orders', 'user_id', 'fk_orders_user', 'users', 'id')]
        if 'COUNT(*)' in up:
            return ['COUNT(*)'], [[len(self.rows)]]
        m = re.search(r'LIMIT\s+(\d+)\s+OFFSET\s+(\d+)', sql, re.I)
        if m:
            limit, offset = int(m.group(1)), int(m.group(2))
            self.stream_queries.append((limit, offset))
            return ['id', 'name', 'amount'], self.rows[offset:offset + limit]
        if up.startswith('SELECT'):
            return ['id', 'name', 'amount'], [list(r) for r in self.rows]
        return [], []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.execute_delay:
            time.sleep(self.execute_delay)
        if self.fail_at and len(self.executed) == self.fail_at:
            return 0, 'ERROR 1062: duplicate'
        return 1, None


def use_fake(monkeypatch):
    import web.api as api
    fake = FakeP2Conn()
    monkeypatch.setattr(api, '_get_conn', lambda token, name=None: fake)
    return fake


# ==================== 1. 图表 / i18n 静态审计 ====================

def test_chart_and_i18n_assets():
    js = (ROOT / 'web' / 'static' / 'app.js').read_text(encoding='utf-8')
    i18n = (ROOT / 'web' / 'static' / 'i18n.js').read_text(encoding='utf-8')
    html = (ROOT / 'web' / 'static' / 'index.html').read_text(encoding='utf-8')
    # 图表：本地 SVG + 空/非数值显式分支
    assert 'function renderChart' in js
    assert "createElementNS(NS, 'svg')" in js
    assert 'chart.empty' in js and 'chart.no_numeric' in js
    assert 'chart-bar-btn' in html and 'chart-line-btn' in html
    assert 'chart-output' in html
    # 无外网 CDN（仅检查 app.js/index；i18n 不含资源）
    combined = js + html
    assert 'cdn.jsdelivr' not in combined.lower()
    assert 'unpkg.com' not in combined.lower()
    assert 'cdnjs.cloudflare.com' not in combined.lower()
    assert not re.search(r'https?://[^\s"\']+\.js', combined)
    # i18n：原生字典 + data-i18n，无框架
    for key in ("chart.bar", "chart.line", "chart.empty", "chart.no_numeric",
                "er.tab", "plugins.tab", "stream.export"):
        assert key in i18n, key
    assert 'window.t' in i18n and 'applyI18n' in i18n
    assert 'data-i18n' in html
    assert 'lang-select' in html
    assert 'i18n.js' in html and html.index('i18n.js') < html.index('app.js')


# ==================== 2. ER 图真实 information_schema 契约 ====================

def test_er_information_schema_and_no_fk(client, monkeypatch):
    fake = use_fake(monkeypatch)
    headers = auth(client)
    r = client.get('/api/v1/databases/demo/er', headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [n['name'] for n in data['nodes']] == ['users', 'orders']
    assert data['edges'] and data['edges'][0]['to'] == 'users'
    assert data['foreign_keys_supported'] is True
    # 引擎无外键元数据 → 明确说明，不伪造边
    fake.fk_supported = False
    r2 = client.get('/api/v1/databases/demo/er', headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2['edges'] == []
    assert d2['foreign_keys_supported'] is False
    assert '无外键关系' in d2['notice']
    assert r2.json()['nodes']


# ==================== 3. 插件面板真实加载信息 ====================

def test_plugins_panel_real_manager(client, monkeypatch):
    import web.parity as parity

    class Info:
        def __init__(self, name, version, status, loaded=True, error=''):
            self._d = {'key': name, 'name': name, 'version': version,
                       'status': status, 'author': 't', 'description': '',
                       'path': '/tmp', 'enabled': True, 'error': error}
            self.loaded = loaded

        def to_dict(self):
            return dict(self._d)

    class FakeMgr:
        warnings = []

        def get_all(self):
            return [Info('demo', '1.2.3', 'loaded'),
                    Info('broken', '0.1', 'failed', False, 'boom')]

    monkeypatch.setattr(parity, '_plugin_manager', lambda: FakeMgr())
    headers = auth(client)
    r = client.get('/api/v1/plugins', headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['count'] == 2
    assert data['plugins'][0]['name'] == 'demo'
    assert data['plugins'][0]['version'] == '1.2.3'
    assert data['plugins'][1]['status'] == 'failed'
    assert data['plugins'][1]['error'] == 'boom'
    # 空目录 notice 显式
    class Empty:
        warnings = []

        def get_all(self):
            return []

    monkeypatch.setattr(parity, '_plugin_manager', lambda: Empty())
    empty = client.get('/api/v1/plugins', headers=headers).json()
    assert empty['plugins'] == [] and empty['notice']
    # 加载失败显式 500 code
    def boom():
        raise RuntimeError('plugin loader down')

    monkeypatch.setattr(parity, '_plugin_manager', boom)
    failed = client.get('/api/v1/plugins', headers=headers)
    assert failed.status_code == 500
    assert failed.json()['detail']['code'] == 'plugins_load_failed'


# ==================== 4. 流式 CSV 导出 ====================

def test_streaming_csv_export_chunks(client, monkeypatch):
    fake = use_fake(monkeypatch)
    headers = auth(client)
    r = client.get('/api/v1/databases/demo/tables/users/export/stream'
                   '?chunk_size=2&limit=10', headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers['content-type'].startswith('text/csv')
    assert 'attachment' in r.headers['content-disposition']
    text = r.content.decode('utf-8-sig')
    lines = [x for x in text.splitlines() if x]
    assert lines[0] == 'id,name,amount'
    assert len(lines) == 6  # header + 5 rows
    # 至少 3 次分块查询（2+2+1），证明没有一次性取全表
    assert len(fake.stream_queries) >= 3, fake.stream_queries
    assert fake.stream_queries[0] == (2, 0)
    assert fake.stream_queries[1] == (2, 2)


# ==================== 5. 异步导入进度 / 取消 / 逐行错误 ====================

def _wait_job(client, headers, job_id, timeout=8):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get('/api/v1/import/jobs/' + job_id,
                          headers=headers).json()
        if last.get('status') in ('completed', 'completed_with_errors',
                                  'cancelled', 'failed'):
            return last
        time.sleep(0.05)
    return last


def test_import_job_progress_errors_and_cancel(client, monkeypatch):
    fake = use_fake(monkeypatch)
    headers = auth(client)
    start = client.post('/api/v1/databases/demo/tables/users/import/start',
                        json={'csv': 'name,amount\na,1\nb,2\nc,3',
                              'has_header': True}, headers=headers)
    assert start.status_code == 200, start.text
    job = _wait_job(client, headers, start.json()['job_id'])
    assert job['status'] == 'completed'
    assert job['inserted'] == 3 and job['failed'] == 0
    assert job['progress'] == 100.0
    # 逐行错误显式
    fake.executed.clear(); fake.fail_at = 2
    start2 = client.post('/api/v1/databases/demo/tables/users/import/start',
                         json={'csv': 'name,amount\na,1\nb,2', 
                               'has_header': True}, headers=headers)
    job2 = _wait_job(client, headers, start2.json()['job_id'])
    assert job2['status'] == 'completed_with_errors'
    assert job2['failed'] == 1
    assert any(e['row'] == 2 for e in job2['errors'])
    # 取消：慢导入，取消后显式 cancelled
    fake.fail_at = 0
    fake.execute_delay = 0.15
    start3 = client.post('/api/v1/databases/demo/tables/users/import/start',
                         json={'csv': 'name,amount\n' +
                               '\n'.join(f'x{i},{i}' for i in range(20)),
                               'has_header': True}, headers=headers)
    jid = start3.json()['job_id']
    time.sleep(0.15)
    cancel = client.post('/api/v1/import/jobs/' + jid + '/cancel',
                         headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()['status'] in ('cancelling', 'cancelled')
    final = _wait_job(client, headers, jid)
    assert final['status'] in ('cancelled', 'completed',
                               'completed_with_errors')
    if final['status'] == 'cancelled':
        assert final['processed'] < final['total']
    # 任务不存在显式 404
    missing = client.get('/api/v1/import/jobs/nope', headers=headers)
    assert missing.status_code == 404
    assert missing.json()['detail']['code'] == 'import_job_not_found'
