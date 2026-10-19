"""P0-D Web 安全回归测试（FastAPI TestClient；不联网、不依赖真实数据库）。

覆盖修复点：
  1. 无 token → 401；旧硬编码 SECRET 伪造的 admin token → 401；alg=none /
     过期 token → 401；``LIGHTDB_WEB_SECRET`` 生效；
  2. 未配置用户时默认口令 admin/admin123、dev/dev123 等 → 401；
     未设置 env 时自动生成随机 admin 口令并写入 0600 文件（日志不打印口令）；
  3. ``LIGHTDB_WEB_USERS`` 多用户登录；失败登录常量时间比较 + 固定延迟；
  4. 已认证 SQL 注入：order_by / db / table / 列名 payload → 400；
  5. CORS 只回显白名单 origin，evil origin 不回显（不再 ``*``）；
  6. 新增 ``POST /api/v1/connections``：仅 admin，写入后 ``/api/v1/query``
     能真正执行（用假 DBConnection）；未配置连接时 404 语义保持；
     连接配置可持久化到 ``LIGHTDB_WEB_CONNECTIONS_FILE``；
  7. ``/connections/test`` 仅 admin + ``LIGHTDB_WEB_ALLOWED_HOSTS`` 白名单；
  8. ``/metrics`` 已挂载（Prometheus 文本）。

隔离方式：``web.server.app_state`` 是模块级全局 dict、JWT 密钥有进程内
缓存，所以每个用例用 monkeypatch 改临时 HOME + 清环境变量 + 重置缓存 +
重新 ``create_app()``。临时 HOME 保证生成的口令/密钥文件不落到真实主目录。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

# 历史缺陷里的硬编码密钥（源码公开 = 谁都能伪造 admin token）。
OLD_HARDCODED_SECRET = 'lightdb_studio_secret_2025_change_me'

TEST_SECRET = 'unit-test-web-jwt-secret-9f8e7d6c'
ADMIN_PASSWORD = 'Adm1n-Pass-9f8e7d6c'
DEV_PASSWORD = 'Dev-Pass-4a3b2c1d'
USERS = {
    'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'},
    'dev': {'password': DEV_PASSWORD, 'role': 'developer'},
}

_ENV_KEYS = (
    'LIGHTDB_WEB_SECRET',
    'LIGHTDB_WEB_USERS',
    'LIGHTDB_WEB_ORIGINS',
    'LIGHTDB_WEB_CONNECTIONS_FILE',
    'LIGHTDB_WEB_ALLOWED_HOSTS',
)

EVIL_ORIGIN = 'https://evil.example.com'
ALLOWED_ORIGIN = 'http://127.0.0.1:8080'


# ==================== 工具 ====================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _forge(secret: str, username: str = 'attacker', role: str = 'admin',
           expires_in: int = 3600) -> str:
    """用指定 secret 手工签名 JWT（模拟攻击者拿到旧硬编码 SECRET 的场景）。"""
    header = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    payload = _b64url(json.dumps({
        'sub': username,
        'role': role,
        'iat': int(time.time()),
        'exp': int(time.time()) + expires_in,
    }).encode())
    signing_input = f'{header}.{payload}'.encode()
    sig = _b64url(hmac.new(secret.encode(), signing_input,
                           hashlib.sha256).digest())
    return f'{header}.{payload}.{sig}'


def _make_client(*args, **kwargs) -> TestClient:
    from web.server import create_app
    return TestClient(create_app(*args, **kwargs))


def _login(client: TestClient, username: str, password: str):
    return client.post('/api/v1/login',
                       json={'username': username, 'password': password})


def _headers(client: TestClient, username: str = 'admin',
             password: str = ADMIN_PASSWORD) -> dict:
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return {'Authorization': 'Bearer ' + resp.json()['token']}


class FakeDBConnection:
    """DBConnection 的替身：不联网，记录收到的 SQL（用于验证 200 链路与拼接）。"""

    instances = []
    created = 0

    def __init__(self, config):
        self.config = config
        self.server_version = '8.0.0-fake'
        self.queries = []
        self.database = getattr(config, 'database', '') or ''
        FakeDBConnection.created += 1
        FakeDBConnection.instances.append(self)

    def connect(self):
        return self

    def is_connected(self):
        return True

    def close(self):
        pass

    def switch_database(self, db_name):
        self.database = db_name

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        if 'COUNT(*)' in sql.upper():
            return ['COUNT(*)'], [(1,)]
        if sql.strip().upper().startswith('SELECT 1'):
            return ['1'], [(1,)]
        return ['id'], [(1,)]

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        return 1, None

    def get_databases(self):
        return ['main']

    def get_tables(self, db=None):
        return ['users']

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT'}]


# ==================== 环境隔离 ====================

@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """临时 HOME + 固定测试密钥 + 清空所有 Web 环境变量 + 重置全局状态。"""
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LIGHTDB_WEB_SECRET', TEST_SECRET)

    from web import api, auth, server

    auth.reset_secret_cache()
    server.app_state.clear()
    api._session_conns.clear()
    FakeDBConnection.instances.clear()
    FakeDBConnection.created = 0

    yield tmp_path

    auth.reset_secret_cache()
    server.app_state.clear()
    api._session_conns.clear()


@pytest.fixture
def configured_users(web_env, monkeypatch):
    """配置了 LIGHTDB_WEB_USERS 的环境（admin + developer）。"""
    monkeypatch.setenv('LIGHTDB_WEB_USERS', json.dumps(USERS))
    return web_env


def _patch_fake_db(monkeypatch):
    from web import api
    monkeypatch.setattr(api, 'DBConnection', FakeDBConnection)


# ==================== 1. 认证 ====================

def test_missing_token_is_401(configured_users):
    client = _make_client()
    assert client.get('/api/v1/me').status_code == 401
    assert client.get('/api/v1/connections').status_code == 401
    assert client.post('/api/v1/query',
                       json={'sql': 'SELECT 1'}).status_code == 401
    assert client.get('/api/v1/history').status_code == 401


def test_old_hardcoded_secret_token_is_rejected(configured_users):
    """用旧源码里公开的 SECRET 签的 admin token 必须是 401（核心 P0）。"""
    client = _make_client()
    forged = _forge(OLD_HARDCODED_SECRET)
    headers = {'Authorization': 'Bearer ' + forged}

    assert client.get('/api/v1/me', headers=headers).status_code == 401
    assert client.get('/api/v1/connections', headers=headers).status_code == 401
    assert client.post('/api/v1/query', json={'sql': 'SELECT 1'},
                       headers=headers).status_code == 401

    # 而用当前配置的 LIGHTDB_WEB_SECRET 手工签发的 token 是有效的
    # （证明密钥来源确实切换到 env，而不是仍然是旧硬编码串）
    valid = _forge(TEST_SECRET)
    assert client.get('/api/v1/me',
                      headers={'Authorization': 'Bearer ' + valid}
                      ).status_code == 200


def test_alg_none_and_expired_tokens_are_rejected(configured_users):
    client = _make_client()
    none_header = _b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
    none_payload = _b64url(json.dumps({
        'sub': 'attacker', 'role': 'admin',
        'exp': int(time.time()) + 3600,
    }).encode())
    none_token = f'{none_header}.{none_payload}.x'
    assert client.get('/api/v1/me', headers={
        'Authorization': 'Bearer ' + none_token}).status_code == 401

    expired = _forge(TEST_SECRET, expires_in=-10)
    assert client.get('/api/v1/me', headers={
        'Authorization': 'Bearer ' + expired}).status_code == 401

    assert client.get('/api/v1/me', headers={
        'Authorization': 'Bearer not-a-jwt'}).status_code == 401


def test_configured_users_can_login(configured_users):
    client = _make_client()
    resp = _login(client, 'admin', ADMIN_PASSWORD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['ok'] is True
    assert body['user'] == {'username': 'admin', 'role': 'admin'}

    me = client.get('/api/v1/me',
                    headers={'Authorization': 'Bearer ' + body['token']})
    assert me.status_code == 200
    assert me.json()['username'] == 'admin'

    # 旧的默认口令不再可用
    assert _login(client, 'admin', 'admin123').status_code == 401


def test_unknown_user_login_is_401_and_delayed(configured_users):
    client = _make_client()
    started = time.perf_counter()
    resp = _login(client, 'admin', 'wrong-password-9f8e')
    elapsed = time.perf_counter() - started
    assert resp.status_code == 401
    assert elapsed >= 0.25, f'失败登录没有固定延迟（{elapsed:.3f}s）'

    # 不存在的用户与口令错误返回同一错误（不泄露用户是否存在）
    unknown = _login(client, 'no_such_user_9f8e', 'whatever')
    assert unknown.status_code == 401
    assert unknown.json()['detail'] == resp.json()['detail']


def test_default_admin_password_is_gone_and_random_admin_file(web_env, capsys):
    """未配置 LIGHTDB_WEB_USERS 时：默认口令 401，随机 admin 口令落 0600 文件。"""
    client = _make_client()
    captured = capsys.readouterr()

    assert _login(client, 'admin', 'admin123').status_code == 401
    assert _login(client, 'dev', 'dev123').status_code == 401

    admin_file = web_env / '.lightdb_studio' / 'web_admin.txt'
    assert admin_file.exists(), '未生成 web_admin.txt'
    generated = admin_file.read_text(encoding='utf-8').strip()
    assert generated, 'web_admin.txt 为空'
    assert generated not in ('admin123', 'dev123', 'analyst123', 'viewer123')
    assert len(generated) >= 16, '自动生成的 admin 口令熵太低'

    # 启动日志只应出现文件路径，绝不出现口令
    assert generated not in captured.out, '启动日志泄露了 admin 口令'
    assert generated not in captured.err, '启动日志泄露了 admin 口令'
    assert 'web_admin.txt' in captured.out

    # 生成的口令确实可用，且是 admin 角色
    resp = _login(client, 'admin', generated)
    assert resp.status_code == 200, resp.text
    assert resp.json()['user']['role'] == 'admin'
    token = resp.json()['token']
    assert client.get('/api/v1/me',
                      headers={'Authorization': 'Bearer ' + token}
                      ).status_code == 200


def test_explicit_admin_password_parameter(web_env):
    client = _make_client(admin_password='Param-Pass-9f8e7d6c')
    assert _login(client, 'admin', 'Param-Pass-9f8e7d6c').status_code == 200
    assert _login(client, 'admin', 'admin123').status_code == 401


def test_secret_file_persisted_when_env_unset(web_env, monkeypatch):
    """env 未设置时密钥持久化到 ~/.lightdb_studio/web_secret（0600）并可复用。"""
    monkeypatch.delenv('LIGHTDB_WEB_SECRET', raising=False)
    from web import auth
    auth.reset_secret_cache()

    first = auth.get_secret()
    secret_file = web_env / '.lightdb_studio' / 'web_secret'
    assert secret_file.exists(), '未生成 web_secret 文件'
    assert auth.get_secret() == first, '同一进程内密钥应稳定'

    # 模拟重启：清缓存后重新解析，必须还是同一个密钥
    auth.reset_secret_cache()
    assert auth.get_secret() == first, '重启后密钥发生变化（token 会全部失效）'

    token = auth.create_token('u', 'viewer')
    assert auth.decode_token(token)['sub'] == 'u'

    if os.name != 'nt':       # Windows 上 chmod 语义有限，只在 POSIX 上断言
        assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_secret_falls_back_to_process_random_when_unwritable(web_env,
                                                             monkeypatch):
    """env 未设置且持久化路径不可写时：回退进程内随机密钥（重启即失效）。"""
    monkeypatch.delenv('LIGHTDB_WEB_SECRET', raising=False)
    from pathlib import Path

    from web import auth

    blocker = web_env / 'blocked'
    blocker.write_text('not a directory', encoding='utf-8')
    monkeypatch.setattr(auth, 'state_dir',
                        lambda: Path(blocker) / '.lightdb_studio')

    auth.reset_secret_cache()
    first = auth.get_secret()
    assert auth.get_secret() == first, '同一进程内回退密钥应稳定'

    # 模拟重启：进程内随机密钥不再复用（旧 token 全部失效）
    auth.reset_secret_cache()
    second = auth.get_secret()
    assert second != first, '不可写时应回退进程内随机密钥，而不是复用旧值'


# ==================== 2. 已认证 SQL 注入 ====================

INJECTION = 'id` DESC -- '


def test_order_by_injection_rejected(configured_users):
    client = _make_client()
    headers = _headers(client)
    resp = client.get('/api/v1/databases/main/tables/users/data',
                      params={'order_by': INJECTION}, headers=headers)
    assert resp.status_code == 400, resp.text


def test_path_identifier_injection_rejected(configured_users):
    client = _make_client()
    headers = _headers(client)
    evil = quote(INJECTION, safe='')

    assert client.get(f'/api/v1/databases/{evil}/tables',
                      headers=headers).status_code == 400
    assert client.get(f'/api/v1/databases/main/tables/{evil}/data',
                      headers=headers).status_code == 400
    assert client.get(f'/api/v1/databases/main/tables/{evil}/columns',
                      headers=headers).status_code == 400
    # /query 的 database 字段也会进入连接会话切换，同样必须校验
    assert client.post('/api/v1/query', headers=headers,
                       json={'sql': 'SELECT 1',
                             'database': INJECTION}).status_code == 400


def test_column_injection_rejected(configured_users):
    """INSERT/UPDATE/DELETE 的列名此前直接拼进反引号，现在必须 400。"""
    client = _make_client()
    headers = _headers(client)

    evil_col = 'c` = 1 -- '
    resp = client.post('/api/v1/databases/main/tables/users/insert',
                       json={evil_col: 'x'}, headers=headers)
    assert resp.status_code == 400, resp.text

    resp = client.put('/api/v1/databases/main/tables/users/update',
                      json={'set_data': {evil_col: 'x'},
                            'where': {'id': 1}}, headers=headers)
    assert resp.status_code == 400, resp.text

    resp = client.put('/api/v1/databases/main/tables/users/update',
                      json={'set_data': {'name': 'x'},
                            'where': {evil_col: 1}}, headers=headers)
    assert resp.status_code == 400, resp.text

    # DELETE 的 where 是单一 body 参数：整个 body 就是 where 字典
    resp = client.request(
        'DELETE', '/api/v1/databases/main/tables/users/delete',
        json={evil_col: 1}, headers=headers)
    assert resp.status_code == 400, resp.text


# ==================== 3. CORS ====================

def test_cors_evil_origin_not_echoed(configured_users):
    client = _make_client()
    preflight = client.options('/api/v1/health', headers={
        'Origin': EVIL_ORIGIN,
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'authorization',
    })
    assert preflight.headers.get('access-control-allow-origin') != EVIL_ORIGIN
    assert 'access-control-allow-origin' not in {
        k.lower() for k in preflight.headers}

    # 实际请求也不能回显 evil origin
    actual = client.post('/api/v1/login',
                         json={'username': 'admin', 'password': ADMIN_PASSWORD},
                         headers={'Origin': EVIL_ORIGIN})
    assert actual.headers.get('access-control-allow-origin') != EVIL_ORIGIN

    # 白名单内的本机 origin 正常放行（含凭据）
    allowed = client.options('/api/v1/health', headers={
        'Origin': ALLOWED_ORIGIN,
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'authorization',
    })
    assert allowed.headers.get('access-control-allow-origin') == ALLOWED_ORIGIN
    assert allowed.headers.get('access-control-allow-credentials') == 'true'


def test_cors_origins_env_whitelist(configured_users, monkeypatch):
    monkeypatch.setenv('LIGHTDB_WEB_ORIGINS', 'https://studio.example.com')
    client = _make_client()
    custom = client.options('/api/v1/health', headers={
        'Origin': 'https://studio.example.com',
        'Access-Control-Request-Method': 'GET',
    })
    assert custom.headers.get(
        'access-control-allow-origin') == 'https://studio.example.com'

    default_origin = client.options('/api/v1/health', headers={
        'Origin': ALLOWED_ORIGIN,
        'Access-Control-Request-Method': 'GET',
    })
    assert default_origin.headers.get('access-control-allow-origin') is None


# ==================== 4. 连接管理 + /query 链路 ====================

def _create_connection(client, headers, name='local'):
    return client.post('/api/v1/connections', headers=headers, json={
        'name': name, 'host': '127.0.0.1', 'port': 18888,
        'user': 'root', 'password': 'db-secret-pwd', 'database': 'main',
    })


def test_query_without_connection_keeps_404(configured_users):
    client = _make_client()
    headers = _headers(client)
    resp = client.post('/api/v1/query', headers=headers,
                       json={'sql': 'SELECT 1'})
    assert resp.status_code == 404


def test_configured_connection_enables_query(configured_users, monkeypatch):
    """验收核心：POST /connections（admin）之后 /api/v1/query 真的能执行。"""
    _patch_fake_db(monkeypatch)
    client = _make_client()
    headers = _headers(client)

    created = _create_connection(client, headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body['ok'] is True
    assert 'password' not in json.dumps(body), '连接接口回显了口令'

    # 连接可被列出，且同样不泄露口令
    listed = client.get('/api/v1/connections', headers=headers)
    assert listed.status_code == 200
    names = [c['name'] for c in listed.json()]
    assert names == ['local']
    assert 'db-secret-pwd' not in json.dumps(listed.json())

    # /query 走真实链路（DBConnection 被替换为不联网的 fake）
    resp = client.post('/api/v1/query', headers=headers,
                       json={'sql': 'SELECT 1', 'connection': 'local'})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result['ok'] is True
    assert result['columns'] == ['1']
    assert result['rows'] == [[1]]
    assert result['row_count'] == 1

    fake = FakeDBConnection.instances[-1]
    assert fake.queries and fake.queries[0][0] == 'SELECT 1', \
        'SQL 被意外改写'

    # 同一个连接名再次 POST 是覆盖（replaced=True），不是重复追加
    replaced = _create_connection(client, headers)
    assert replaced.status_code == 200
    assert replaced.json()['replaced'] is True
    assert [c['name'] for c in client.get(
        '/api/v1/connections', headers=headers).json()] == ['local']


def test_create_app_legacy_positional_signature(configured_users, monkeypatch):
    """兼容旧签名 create_app(db, executor, connections)（位置参数）。"""
    _patch_fake_db(monkeypatch)
    from lightdb_studio.config import ConnectionInfo

    seeded = ConnectionInfo(name='seeded', host='127.0.0.1', port=0)
    client = _make_client(None, None, [seeded])   # 三个位置参数
    headers = _headers(client)

    resp = client.post('/api/v1/query', headers=headers,
                       json={'sql': 'SELECT 1'})
    assert resp.status_code == 200, resp.text
    assert resp.json()['rows'] == [[1]]


def test_connection_creation_requires_admin(configured_users, monkeypatch):
    _patch_fake_db(monkeypatch)
    client = _make_client()

    # 未登录 401
    assert client.post('/api/v1/connections',
                       json={'name': 'x'}).status_code == 401

    # developer（无 admin 权限）403
    dev_headers = _headers(client, 'dev', DEV_PASSWORD)
    assert client.post('/api/v1/connections', headers=dev_headers,
                       json={'name': 'x'}).status_code == 403

    # 空名字 400（admin）
    admin_headers = _headers(client)
    assert client.post('/api/v1/connections', headers=admin_headers,
                       json={'name': '   '}).status_code == 400


def test_connection_persisted_to_file(configured_users, monkeypatch, tmp_path):
    _patch_fake_db(monkeypatch)
    conn_file = tmp_path / 'web_connections.json'
    monkeypatch.setenv('LIGHTDB_WEB_CONNECTIONS_FILE', str(conn_file))

    client = _make_client()
    headers = _headers(client)
    resp = _create_connection(client, headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()['persisted'] is True

    data = json.loads(conn_file.read_text(encoding='utf-8'))
    assert isinstance(data, list) and data[0]['name'] == 'local'

    # start.py 的加载路径能读回同一条连接
    from web.server import load_connections_file
    loaded = load_connections_file(conn_file)
    assert [c.name for c in loaded] == ['local']
    assert loaded[0].host == '127.0.0.1'


def test_connection_test_requires_admin_and_host_whitelist(configured_users,
                                                           monkeypatch):
    _patch_fake_db(monkeypatch)
    client = _make_client()
    admin_headers = _headers(client)
    dev_headers = _headers(client, 'dev', DEV_PASSWORD)
    body = {'name': 'probe', 'host': '127.0.0.1', 'port': 18888,
            'user': 'root', 'password': ''}

    # 非 admin 403（此前任意登录用户都能打任意主机端口 → SSRF）
    assert client.post('/api/v1/connections/test', json=body,
                       headers=dev_headers).status_code == 403
    # 匿名 401
    assert client.post('/api/v1/connections/test',
                       json=body).status_code == 401

    # 未设置白名单：默认允许（并记审计日志）
    allowed = client.post('/api/v1/connections/test', json=body,
                          headers=admin_headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {'ok': True, 'version': '8.0.0-fake'}

    # 设置白名单后，名单外主机 403、名单内主机允许
    monkeypatch.setenv('LIGHTDB_WEB_ALLOWED_HOSTS', '10.0.0.1')
    denied = client.post('/api/v1/connections/test', json=body,
                         headers=admin_headers)
    assert denied.status_code == 403, denied.text
    ok = client.post('/api/v1/connections/test',
                     json={**body, 'host': '10.0.0.1'},
                     headers=admin_headers)
    assert ok.status_code == 200, ok.text


def test_order_by_is_validated_then_parameterized(configured_users, monkeypatch):
    """合法 order_by 只以「白名单标识符 + ASC/DESC 枚举」形式拼进 SQL。"""
    _patch_fake_db(monkeypatch)
    client = _make_client()
    headers = _headers(client)
    assert _create_connection(client, headers).status_code == 200

    resp = client.get('/api/v1/databases/main/tables/users/data',
                      params={'order_by': 'id', 'order_desc': 'true'},
                      headers=headers)
    assert resp.status_code == 200, resp.text

    fake = FakeDBConnection.instances[-1]
    sql, params = fake.queries[-1]
    assert sql == 'SELECT * FROM `users` ORDER BY `id` DESC LIMIT 100 OFFSET 0'
    assert params is None


# ==================== 5. 指标端点 ====================

def test_metrics_endpoint_mounted(configured_users):
    """web/metrics.py 之前是死代码（从未挂载）；现在 /metrics 可访问。"""
    client = _make_client()
    resp = client.get('/metrics')
    assert resp.status_code == 200
    assert 'text/plain' in resp.headers.get('content-type', '')
    assert 'version=0.0.4' in resp.headers.get('content-type', '')

    json_metrics = client.get('/api/v1/metrics/json')
    assert json_metrics.status_code == 200
    assert 'counters' in json_metrics.json()
