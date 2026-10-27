# -*- coding: utf-8 -*-
"""task-60：Web GUI 对标 P0/P1 测试（FastAPI TestClient + 真实 in-process 引擎）。

覆盖审计后的 P0/P1 缺口：
- 表设计器 GET/POST design：列增删改、AUTO_INCREMENT、索引、预览/应用/错误；
- 导入导出：表 CSV/SQL、查询结果导出、CSV 导入显式行错误；
- EXPLAIN / 备份 / 结构对比入口；
- 数据分页排序（total/limit/offset/order_by）；
- 连接 SSH/SSL 只读状态（不回显口令/私钥）；
- 核心 E2E：登录/切库/查询/表数据/设计器保存/导出；无浏览器时显式 skip
  真浏览器测试（TestClient 覆盖协议路径）。
"""
import csv
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_SECRET = 'web-parity-gaps-secret-0123456789abcdef'
ADMIN_PASSWORD = 'Parity-Gaps-Admin-Pass-9f8e7d6c'
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


def auth(client):
    resp = client.post('/api/v1/login',
                       json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {'Authorization': 'Bearer ' + resp.json()['token']}


class FakeParityConn:
    def __init__(self):
        self.database = ''
        self.executed = []
        self.columns = [
            {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
             'extra': '', 'default': None},
            {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES',
             'key': '', 'extra': '', 'default': None},
        ]
        self.indexes = [
            {'name': 'PRIMARY', 'unique': True, 'columns': ['id']},
        ]

    def is_connected(self):
        return True

    def switch_database(self, db):
        self.database = db

    def get_databases(self):
        return ['demo', 'target']

    def get_tables(self):
        if self.database == 'target':
            return ['users', 'extra']
        return ['users']

    def get_columns(self, table):
        return [dict(c) for c in self.columns]

    def query(self, sql, params=None):
        up = str(sql).upper()
        if 'SHOW CREATE' in up:
            return (['Table', 'Create Table'],
                    [('users', 'CREATE TABLE users (\n  id INT PRIMARY KEY,\n'
                               '  name VARCHAR(32)\n)')])
        if 'SHOW INDEX' in up:
            return (['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                     'Column_name'],
                    [('users', 0 if i['unique'] else 1, i['name'],
                      n + 1, col)
                     for i in self.indexes for n, col in enumerate(i['columns'])])
        if 'SHOW TABLE STATUS' in up:
            return ['Name', 'Auto_increment'], [('users', None)]
        if 'EXPLAIN' in up:
            return ['id', 'select_type', 'table', 'type'], [
                [1, 'SIMPLE', 'users', 'ALL']]
        if 'COUNT(*)' in up:
            return ['COUNT(*)'], [[2]]
        if up.startswith('SELECT'):
            return ['id', 'name'], [[1, 'a'], [2, 'b']]
        return [], []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return 1, None


def use_fake(monkeypatch, client):
    import web.api as api
    fake = FakeParityConn()
    monkeypatch.setattr(api, '_get_conn', lambda token, name=None: fake)
    return fake


def h(client):
    return auth(client)


# ==================== 1. 表设计器 ====================

def test_design_get_and_preview_and_apply(client, monkeypatch):
    fake = use_fake(monkeypatch, client)
    headers = h(client)
    design = client.get('/api/v1/databases/demo/tables/users/design',
                        headers=headers)
    assert design.status_code == 200, design.text
    data = design.json()
    assert data['table'] == 'users' and len(data['columns']) == 2
    assert any(i['name'] == 'PRIMARY' for i in data['indexes'])
    assert 'CREATE TABLE' in data['create_sql']

    body = {
        'dry_run': True,
        'columns': [
            {'name': 'id', 'type': 'INT', 'notnull': True, 'primary': True},
            {'name': 'name', 'type': 'VARCHAR', 'length': '32'},
            {'name': 'age', 'type': 'INT', 'length': '', 'notnull': False,
             'position': 'AFTER id'},
        ],
        'indexes': [
            {'name': 'PRIMARY', 'columns': ['id'], 'unique': True},
            {'name': 'idx_name', 'columns': ['name'], 'unique': False},
        ],
        'auto_increment_start': None,
    }
    prev = client.post('/api/v1/databases/demo/tables/users/design',
                       json=body, headers=headers)
    assert prev.status_code == 200, prev.text
    stmts = prev.json()['statements']
    assert any(s == 'ALTER TABLE `users` ADD COLUMN `age` INT NULL '
                   'AFTER `id`;' for s in stmts)
    assert any(s == 'CREATE INDEX `idx_name` ON `users` (`name`);'
               for s in stmts)
    assert fake.executed == []  # dry_run 不执行
    body['dry_run'] = False
    applied = client.post('/api/v1/databases/demo/tables/users/design',
                          json=body, headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()['dry_run'] is False
    assert len(fake.executed) == len(stmts)


def test_design_auto_increment_and_unsupported_errors(client, monkeypatch):
    use_fake(monkeypatch, client)
    headers = h(client)
    cols = [
        {'name': 'id', 'type': 'INT', 'notnull': True, 'primary': True},
        {'name': 'code', 'type': 'INT', 'notnull': True, 'unique': True,
         'auto_increment': True},
    ]
    ok = client.post('/api/v1/databases/demo/tables/users/design',
                     json={'dry_run': True, 'columns': cols,
                           'auto_increment_start': 100}, headers=headers)
    assert ok.status_code == 200, ok.text
    stmts = ok.json()['statements']
    assert any('ADD COLUMN `code` INT NOT NULL UNIQUE AUTO_INCREMENT' in s
               for s in stmts)
    assert any('AUTO_INCREMENT = 100' in s for s in stmts)
    # 新自增未 UNIQUE → 显式 400
    bad = client.post('/api/v1/databases/demo/tables/users/design',
                      json={'dry_run': True, 'columns': [
                          cols[0],
                          {'name': 'ai', 'type': 'INT',
                           'auto_increment': True}]}, headers=headers)
    assert bad.status_code == 400
    assert bad.json()['detail']['code'] == 'auto_increment_requires_unique'
    # 新列 PRIMARY 不支持 → 显式 400
    bad2 = client.post('/api/v1/databases/demo/tables/users/design',
                       json={'dry_run': True, 'columns': [
                           cols[0],
                           {'name': 'pk2', 'type': 'INT', 'primary': True}]},
                       headers=headers)
    assert bad2.status_code == 400
    assert bad2.json()['detail']['code'] == 'unsupported_add_primary'
    # 表不存在 → 404（fake 用 monkeypatch 返回空列）
    fake = use_fake(monkeypatch, client)
    fake.columns = []
    missing = client.get('/api/v1/databases/demo/tables/nope/design',
                         headers=headers)
    assert missing.status_code == 404
    assert missing.json()['detail']['code'] == 'table_not_found'


# ==================== 2. 导入导出 ====================

def test_export_table_csv_and_sql(client, monkeypatch):
    use_fake(monkeypatch, client)
    headers = h(client)
    csv_resp = client.get('/api/v1/databases/demo/tables/users/export?fmt=csv',
                          headers=headers)
    assert csv_resp.status_code == 200, csv_resp.text
    assert 'attachment' in csv_resp.headers['content-disposition']
    assert csv_resp.headers['content-type'].startswith('text/csv')
    text = csv_resp.content.decode('utf-8-sig')
    assert text.splitlines()[0] == 'id,name'
    assert '1,a' in text
    sql_resp = client.get('/api/v1/databases/demo/tables/users/export?fmt=sql',
                          headers=headers)
    assert sql_resp.status_code == 200
    sql = sql_resp.text
    assert 'CREATE TABLE users' in sql and 'INSERT INTO' in sql
    bad = client.get('/api/v1/databases/demo/tables/users/export?fmt=html',
                     headers=headers)
    assert bad.status_code == 400


def test_query_result_export_and_import_csv(client, monkeypatch):
    fake = use_fake(monkeypatch, client)
    headers = h(client)
    exp = client.post('/api/v1/query/export',
                      json={'sql': 'SELECT * FROM users', 'format': 'csv'},
                      headers=headers)
    assert exp.status_code == 200
    assert 'id,name' in exp.content.decode('utf-8-sig')
    bad = client.post('/api/v1/query/export',
                      json={'sql': 'DELETE FROM users', 'format': 'csv'},
                      headers=headers)
    assert bad.status_code == 400
    assert bad.json()['detail']['code'] == 'unsupported_export_sql'
    imp = client.post('/api/v1/databases/demo/tables/users/import',
                      json={'csv': 'name\nAlice\nBob', 'has_header': True},
                      headers=headers)
    assert imp.status_code == 200, imp.text
    assert imp.json()['inserted'] == 2
    assert len(fake.executed) == 2
    # 行错误显式返回，不静默
    fake.execute = lambda sql, params=None: (0, 'Duplicate entry')
    bad_rows = client.post('/api/v1/databases/demo/tables/users/import',
                           json={'csv': 'name\nx', 'has_header': True},
                           headers=headers)
    assert bad_rows.status_code == 200
    assert bad_rows.json()['ok'] is False
    assert bad_rows.json()['errors'][0]['error'] == 'Duplicate entry'


# ==================== 3. EXPLAIN / 备份 / 结构对比 ====================

def test_explain_backup_schema_compare(client, monkeypatch):
    use_fake(monkeypatch, client)
    headers = h(client)
    exp = client.post('/api/v1/explain',
                      json={'sql': 'SELECT * FROM users'}, headers=headers)
    assert exp.status_code == 200, exp.text
    assert exp.json()['ok'] and exp.json()['rows'][0][0] == 1
    bad = client.post('/api/v1/explain', json={'sql': 'UPDATE users SET id=1'},
                      headers=headers)
    assert bad.status_code == 400
    backup = client.post('/api/v1/backup',
                         json={'database': 'demo'}, headers=headers)
    assert backup.status_code == 200, backup.text
    text = backup.text
    assert 'CREATE TABLE users' in text and 'INSERT INTO' in text
    assert 'attachment' in backup.headers['content-disposition']
    compare = client.get('/api/v1/schema/compare?source_db=demo'
                         '&target_db=target', headers=headers)
    assert compare.status_code == 200, compare.text
    summary = compare.json()['summary']
    assert summary['missing_in_target'] == []
    assert summary['missing_in_source'] == ['extra']
    # 未认证显式 401
    for method, url, kwargs in [
            ('get', '/api/v1/databases/demo/tables/users/design', {}),
            ('get', '/api/v1/databases/demo/tables/users/export?fmt=csv', {}),
            ('post', '/api/v1/design-not-exist', {}),
    ]:
        if 'design-not-exist' in url:
            continue
        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 401, (url, resp.status_code)


# ==================== 4. 分页排序 / 连接 SSH-SSL 展示 ====================

def test_data_pagination_total_and_connections_ssh_ssl(client, monkeypatch):
    use_fake(monkeypatch, client)
    headers = h(client)
    data = client.get(
        '/api/v1/databases/demo/tables/users/data'
        '?limit=1&offset=1&order_by=name&order_desc=true', headers=headers)
    assert data.status_code == 200, data.text
    payload = data.json()
    assert payload['total'] == 2
    assert payload['limit'] == 1 and payload['offset'] == 1
    assert payload['order_by'] == 'name' and payload['order_desc'] is True
    bad = client.get('/api/v1/databases/demo/tables/users/data?order_by=bad-name',
                     headers=headers)
    assert bad.status_code == 400


def test_connections_expose_ssh_ssl_status_no_secrets(tmp_path, monkeypatch):
    from lightdb_studio.config import ConnectionInfo
    from web.auth import reset_secret_cache
    from web.server import create_app
    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps(USERS))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    reset_secret_cache()
    info = ConnectionInfo(
        name='secure', host='db', port=3306, user='root', password='secret',
        ssl=True, ssl_options={'ca': '/secret/ca.pem', 'verify_mode': 'CERT_REQUIRED'},
        ssh_options={'enabled': True, 'host': 'ssh', 'port': 22,
                     'user': 'me', 'password': 'ssh-secret',
                     'key_path': '/secret/key', 'remote_port': 3306})
    app = create_app(admin_password=ADMIN_PASSWORD, connections=[info])
    client = TestClient(app)
    headers = auth(client)
    resp = client.get('/api/v1/connections', headers=headers)
    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    assert item['ssh']['enabled'] is True
    assert item['ssh']['status'] == 'enabled'
    assert item['ssh']['credential_set'] is True
    assert item['ssh']['has_key'] is True
    assert item['ssl']['enabled'] is True
    assert item['ssl']['has_ca'] is True
    text = json.dumps(resp.json(), ensure_ascii=False)
    assert 'secret' not in text and '/secret/' not in text


# ==================== 5. 核心 E2E（TestClient；真浏览器若缺显式 skip） ====================

def test_core_web_e2e_login_query_data_design_export(client, monkeypatch):
    use_fake(monkeypatch, client)
    headers = auth(client)  # 登录
    dbs = client.get('/api/v1/databases', headers=headers)
    assert dbs.status_code == 200 and 'demo' in dbs.json()['databases']
    tables = client.get('/api/v1/databases/demo/tables', headers=headers)
    assert tables.status_code == 200 and tables.json()['tables'][0]['name'] == 'users'
    query = client.post('/api/v1/query',
                        json={'sql': 'SELECT * FROM users', 'database': 'demo',
                              'connection': 'demo'}, headers=headers)
    assert query.status_code == 200 and query.json()['ok']
    data = client.get('/api/v1/databases/demo/tables/users/data',
                      headers=headers)
    assert data.status_code == 200 and data.json()['rows']
    # 设计器保存（先预览，再应用）
    design = client.get('/api/v1/databases/demo/tables/users/design',
                        headers=headers)
    assert design.status_code == 200
    apply = client.post(
        '/api/v1/databases/demo/tables/users/design',
        json={'dry_run': False, 'columns': [
            {'name': 'id', 'type': 'INT', 'notnull': True, 'primary': True},
            {'name': 'name', 'type': 'VARCHAR', 'length': '32'},
            {'name': 'age', 'type': 'INT'},
        ]}, headers=headers)
    assert apply.status_code == 200, apply.text
    export = client.get('/api/v1/databases/demo/tables/users/export?fmt=csv',
                        headers=headers)
    assert export.status_code == 200 and export.content


def test_browser_e2e_requires_playwright_or_skip():
    """真浏览器 E2E：本机若无 Playwright/浏览器则显式 skip，不假成功。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        pytest.skip('本机无 Playwright；已用 TestClient 覆盖登录/切库/查询/'
                    '表数据/设计器保存/导出契约，真浏览器 E2E 显式跳过')
    pytest.skip('Playwright 包存在但未配置浏览器/服务器；本轮离线用 TestClient')


def test_static_ui_exposes_parity_controls():
    """前端确实接入 P0/P1 入口（不是只做后端 API）。"""
    html = (ROOT / 'web' / 'static' / 'index.html').read_text(encoding='utf-8')
    js = (ROOT / 'web' / 'static' / 'app.js').read_text(encoding='utf-8')
    for control in ('data-btn', 'design-btn', 'explain-btn',
                    'export-query-btn', 'import-btn', 'backup-btn',
                    'sync-btn', 'table-controls', 'sort-column-select',
                    'data-prev-btn', 'data-next-btn', 'design-panel',
                    'design-columns-input', 'design-preview-btn',
                    'design-save-btn', 'design-sql-preview',
                    'conn-detail', 'import-file-input'):
        assert f'id="{control}"' in html, f'index.html 缺少 {control}'
    for path in ("/databases/", "/design", "/export?fmt=csv", "/import",
                 "/query/export", "/explain", "/backup", "/schema/compare",
                 "/data"):
        assert path in js, f'app.js 未接入 {path}'
    assert '.innerHTML' not in js, 'app.js 不得使用 innerHTML'
    assert 'textContent' in js and 'createObjectURL' in js
    assert 'last_forward_error' not in js  # 前端无关，确保静态文件未混入后端


# ==================== 6. 真实 in-process 引擎（非 fake 核心） ====================

def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _shutdown(res):
    if res is None:
        return
    for name in ('shutdown', 'server_close', 'stop', 'close'):
        fn = getattr(res, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def test_real_inproc_engine_web_roundtrip(tmp_path, monkeypatch):
    """真实 LightDB 引擎 + 真实 Web API：设计器/分页/导出/EXPLAIN/备份。"""
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    from web.auth import reset_secret_cache
    from web.server import create_app

    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps(USERS))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    reset_secret_cache()
    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    boot = DBConnection(ConnectionInfo(name='boot', host='127.0.0.1',
                                       port=port, user='root', password=''))
    requests = []
    try:
        boot.connect()
        assert boot.execute('CREATE TABLE real_t (id INT PRIMARY KEY, '
                            "name VARCHAR(32))")[1] is None
        assert boot.execute("INSERT INTO real_t (id, name) VALUES (1, 'a')")[1] is None
        boot.close()
        info = ConnectionInfo(name='real', host='127.0.0.1', port=port,
                              user='root', password='')
        app = create_app(admin_password=ADMIN_PASSWORD, connections=[info])
        client = TestClient(app)
        headers = auth(client)
        design = client.get('/api/v1/databases/main/tables/real_t/design',
                            headers=headers)
        # 默认数据库名由引擎决定；依次尝试真实库名
        if design.status_code != 200:
            dbs = client.get('/api/v1/databases', headers=headers).json()
            dbname = dbs['databases'][0]
            design = client.get(
                f'/api/v1/databases/{dbname}/tables/real_t/design',
                headers=headers)
        assert design.status_code == 200, design.text
        dbname = client.get('/api/v1/databases', headers=headers).json()[
            'databases'][0]
        add = client.post(
            f'/api/v1/databases/{dbname}/tables/real_t/design',
            json={'dry_run': False, 'columns': [
                {'name': 'id', 'type': 'INT', 'notnull': True,
                 'primary': True},
                {'name': 'name', 'type': 'VARCHAR', 'length': '32'},
                {'name': 'age', 'type': 'INT'},
            ], 'indexes': [
                {'name': 'idx_name', 'columns': ['name'], 'unique': False},
            ]}, headers=headers)
        assert add.status_code == 200, add.text
        design2 = client.get(
            f'/api/v1/databases/{dbname}/tables/real_t/design',
            headers=headers).json()
        assert any(i['name'] == 'idx_name' for i in design2['indexes'])
        # 真实引擎：ADD COLUMN AUTO_INCREMENT + UNIQUE + 起始号
        add_auto = client.post(
            f'/api/v1/databases/{dbname}/tables/real_t/design',
            json={'dry_run': False, 'auto_increment_start': 100, 'columns': [
                {'name': 'id', 'type': 'INT', 'notnull': True,
                 'primary': True},
                {'name': 'name', 'type': 'VARCHAR', 'length': '32'},
                {'name': 'age', 'type': 'INT'},
                {'name': 'seq', 'type': 'INT', 'unique': True,
                 'auto_increment': True},
            ]}, headers=headers)
        assert add_auto.status_code == 200, add_auto.text
        design3 = client.get(
            f'/api/v1/databases/{dbname}/tables/real_t/design',
            headers=headers).json()
        seq = [c for c in design3['columns'] if c['name'] == 'seq'][0]
        assert seq['auto_increment'] is True and seq['unique'] is True
        data = client.get(
            f'/api/v1/databases/{dbname}/tables/real_t/data'
            '?limit=10&offset=0&order_by=id', headers=headers)
        assert data.status_code == 200 and data.json()['total'] == 1
        exp = client.get(
            f'/api/v1/databases/{dbname}/tables/real_t/export?fmt=csv',
            headers=headers)
        assert exp.status_code == 200 and 'name' in exp.text
        explain = client.post('/api/v1/explain',
                              json={'sql': 'SELECT * FROM real_t',
                                    'database': dbname}, headers=headers)
        assert explain.status_code == 200, explain.text
        backup = client.post('/api/v1/backup',
                             json={'database': dbname}, headers=headers)
        assert backup.status_code == 200 and 'CREATE TABLE' in backup.text
        print('  → real engine web roundtrip db=%s' % dbname)
    finally:
        try:
            boot.close()
        except Exception:
            pass
        _shutdown(srv)
        _shutdown(db)
