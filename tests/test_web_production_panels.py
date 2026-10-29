# -*- coding: utf-8 -*-
"""task-83：Web 生产化面板 API（复制/metrics/快照 CLI）+ 静态入口。"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_SECRET = 'task83-web-secret-0123456789abcdef'
ADMIN_PASSWORD = 'Task83-Admin-Pass-9f8e7d6c'
USERS = {'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps(USERS))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.delenv('LIGHTDB_METRICS_URL', raising=False)
    from web.auth import reset_secret_cache
    reset_secret_cache()
    from web.server import create_app
    return TestClient(create_app(admin_password=ADMIN_PASSWORD))


def auth(client):
    r = client.post('/api/v1/login',
                    json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


class FakeReplConn:
    def __init__(self, empty=False, fail=False):
        self.empty = empty
        self.fail = fail

    def query(self, sql, params=None):
        if self.fail:
            raise RuntimeError('unknown statement')
        if self.empty:
            return ['Master_Host'], []
        return (['Master_Host', 'Master_Port', 'Master_Log_File',
                 'Read_Master_Log_Pos', 'Exec_Master_Log_Pos',
                 'Seconds_Behind_Master', 'Slave_IO_Running',
                 'Slave_SQL_Running', 'Last_Error'],
                [('master.local', 3306, 'bin.000003', 1024, 900, 12,
                  'Yes', 'Yes', '')])


def use_fake(monkeypatch, fake):
    import web.api as api
    monkeypatch.setattr(api, '_get_conn', lambda token, name=None: fake)


def test_replication_status_api_and_state_file(client, monkeypatch, tmp_path):
    headers = auth(client)
    use_fake(monkeypatch, FakeReplConn())
    r = client.get('/api/v1/replication/status', headers=headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['configured'] is True
    assert d['fields']['master_host'] == 'master.local'
    assert d['fields']['master_port'] == '3306'
    assert d['fields']['last_error'] == '—'
    # SHOW 空 + 无状态文件 → 显式未配置
    use_fake(monkeypatch, FakeReplConn(empty=True))
    r2 = client.get('/api/v1/replication/status', headers=headers)
    assert r2.status_code == 200
    assert r2.json()['configured'] is False
    assert '未配置复制' in r2.json()['notice']
    # 状态文件 + task-81 新字段
    state = {'master_host': 'm', 'master_port': 3307, 'master_file': 'b.9',
             'last_seq': 77, 'lag_ms': 2500, 'applied': 42,
             'idempotent': 3, 'errors': 1, 'last_error': 'boom',
             'new_field': 'x'}
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    use_fake(monkeypatch, FakeReplConn(fail=True))
    r3 = client.get('/api/v1/replication/status?state_file=' +
                    str(path), headers=headers)
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3['configured'] is True
    assert d3['fields']['exec_pos'] == '77'
    assert d3['fields']['lag_seconds'] == '2.5'
    assert 'new_field' in d3['unknown_fields']
    # 未认证
    assert client.get('/api/v1/replication/status').status_code == 401


def test_metrics_summary_api_parse_unreachable_retry(client, monkeypatch):
    import web.production as prod
    headers = auth(client)
    sample = ('lightdb_queries_per_second 12.5\n'
              'lightdb_connections_current 7\n'
              'lightdb_errors_total 3\n'
              'lightdb_slow_queries_total 2\n'
              'lightdb_wal_bytes 4096\n'
              'lightdb_checkpoint_total 5\n'
              'lightdb_buffer_pool_pages_free 128\n')
    calls = {'n': 0}

    def fake_fetch(url, timeout=5.0):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('connection refused')
        return sample

    monkeypatch.setattr(prod, 'fetch_metrics', fake_fetch)
    bad = client.get('/api/v1/metrics/summary?url=http://metrics.local/metrics',
                     headers=headers)
    assert bad.status_code == 400
    assert bad.json()['detail']['code'] == 'metrics_unreachable'
    good = client.get('/api/v1/metrics/summary?url=http://metrics.local/metrics',
                      headers=headers)
    assert good.status_code == 200, good.text
    s = good.json()['summary']
    assert s['qps']['value'] == 12.5
    assert s['connections']['value'] == 7
    assert s['errors']['value'] == 3
    assert s['wal']['value'] == 4096
    assert s['checkpoint']['value'] == 5
    assert s['pool']['value'] == 128
    missing = client.get('/api/v1/metrics/summary?url=http://metrics.local/metrics',
                         headers=headers).json()['summary']
    assert missing['sample_count'] == 7
    # 未配置 URL 显式 400
    no_url = client.get('/api/v1/metrics/summary', headers=headers)
    assert no_url.status_code == 400
    assert no_url.json()['detail']['code'] == 'metrics_url_missing'


def test_snapshot_cli_api_confirm_and_errors(client, monkeypatch):
    import web.production as prod
    headers = auth(client)
    calls = []

    def fake_cli(script, args, timeout):
        calls.append((script, args))
        return {'ok': True, 'returncode': 0, 'command': [script] + args,
                'stdout': 'done', 'stderr': '', 'error': ''}

    monkeypatch.setattr(prod, '_run_cli', fake_cli)
    backup = client.post('/api/v1/snapshot/backup',
                         json={'data_dir': '/data', 'out': '/snap'},
                         headers=headers)
    assert backup.status_code == 200, backup.text
    assert backup.json()['ok'] is True
    assert calls[0][0] == 'snapshot_backup.py'
    # 恢复未确认 → 400，不执行
    no_confirm = client.post('/api/v1/snapshot/restore',
                             json={'snapshot': '/snap', 'data_dir': '/data'},
                             headers=headers)
    assert no_confirm.status_code == 400
    assert no_confirm.json()['detail']['code'] == 'confirmation_required'
    assert len(calls) == 1
    # 显式确认后执行
    ok = client.post('/api/v1/snapshot/restore',
                     json={'snapshot': '/snap', 'data_dir': '/data',
                           'confirm': True}, headers=headers)
    assert ok.status_code == 200
    assert calls[-1][0] == 'snapshot_restore.py'
    # 参数缺失显式
    missing = client.post('/api/v1/snapshot/backup', json={}, headers=headers)
    assert missing.status_code == 400
    assert client.post('/api/v1/snapshot/backup', json={}).status_code == 401


def test_web_static_production_controls():
    html = (ROOT / 'web' / 'static' / 'index.html').read_text(encoding='utf-8')
    js = (ROOT / 'web' / 'static' / 'app.js').read_text(encoding='utf-8')
    for cid in ('tool-production-btn', 'production-section',
                'prod-rep-refresh', 'prod-metrics-url',
                'prod-metrics-refresh', 'prod-snapshot-backup',
                'prod-snapshot-restore', 'prod-snapshot-log'):
        assert f'id="{cid}"' in html, cid
    for path in ('/replication/status', '/metrics/summary',
                 '/snapshot/backup', '/snapshot/restore'):
        assert path in js, path
    assert 'window.confirm' in js and '.innerHTML' not in js
