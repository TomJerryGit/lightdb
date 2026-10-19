"""P0-A 安全默认值：默认认证 / 默认回环监听 / 连接限制 / IP 白名单。

背景（本仓库上一轮全面分析确认的 P0 缺陷）：

* 默认部署 = 无认证：新建数据目录里 root 的 `auth_hash` 是空串，
  `root/''` 可以直接登录，且默认监听 `0.0.0.0`；
* `config.max_connections` / `max_conn_per_ip` / `ip_whitelist_enabled`
  之前**没有任何消费者**，等于没有连接数上限、没有 IP 校验。

本文件验证修复后的**真实行为**（全部走真实 `Database` + 真实
`MySQLServer` + pymysql，不用桩）：

1. 全新数据目录默认生成随机 root 口令，空口令登录被拒（1045），
   读 `<data_dir>/root.password` 的口令能登录成功；
2. 旧数据目录（root 空口令）升级时自动迁移成随机口令（不再无认证）；
3. 显式降级 `LIGHTDB_ALLOW_EMPTY_PASSWORD=1` / `--allow-empty-password`
   才允许空口令（本地开发/演示用），且会留下明确标记；
4. `LIGHTDB_ROOT_PASSWORD` / `--root-password` 显式口令生效；
5. 默认监听地址是 127.0.0.1（0.0.0.0 只能显式指定）；
6. 连接数上限（总量 / 单 IP）与 IP 白名单在握手前真实拒绝
   （1040 / 1130），释放连接后能重新连上。

注意：`tests/conftest.py` 顶层把 `LIGHTDB_ALLOW_EMPTY_PASSWORD` 设为 1
（让既有 700+ 空口令用例保持原语义）。本文件里凡是验证"默认安全行为"
的用例都显式传 `allow_empty_password=False` 或 `monkeypatch.delenv`，
不会被那个降级开关掩盖。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_auth_defaults.py
    from conftest import track

from lightdb.config import Config
from lightdb.db.database import (
    ROOT_PASSWORD_FILE,
    Database,
    hash_native_password,
)

pymysql = pytest.importorskip('pymysql')


# ==================== 工具 ====================

def _serve_in_background(srv, timeout: float = 5.0):
    """后台线程跑 serve_forever，并等它真正开始调度。

    注意：**不要**用"先 connect 一次探活"的方式等待 —— 那次探测连接本身
    也会进入 `verify_request` 的连接计数，若它晚于用例的断言被 accept，
    会在计数里凭空多出一条，造成偶发失败。监听 socket 在 MySQLServer
    构造时就已 bind/listen，客户端 connect 由内核 backlog 先接住即可。
    """
    started = threading.Event()

    def _run():
        started.set()
        srv.serve_forever()

    threading.Thread(target=_run, daemon=True).start()
    assert started.wait(timeout), 'serve_forever 线程没有启动'


def _wait_admission_idle(srv, timeout: float = 5.0):
    """等最近关闭的连接把准入计数减回去（避免用例间互相干扰）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if srv.connection_stats()['active_connections'] == 0:
            return
        time.sleep(0.02)
    raise AssertionError(
        f'连接计数没有回落: {srv.connection_stats()}')


def _start_server(tmp_path, *, data_dir=None, db_kwargs=None,
                  max_connections=100, max_conn_per_ip=20,
                  ip_whitelist_enabled=False, ip_whitelist=None):
    """启动一个真实的 MySQLServer（后台线程 + 随机端口），返回 (srv, db, cfg)"""
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    data_dir = data_dir or str(tmp_path / 'data')
    db = track(Database(data_dir, **(db_kwargs or {})))
    ex = track(Executor(db))

    cfg = Config()
    cfg.host = '127.0.0.1'
    # 端口 0 = 让操作系统分配空闲端口。不要先"探测空闲端口再 bind"：
    # 两者之间存在竞态（并行跑测试/别的进程抢先 bind 会让用例误失败）。
    cfg.port = 0
    cfg.max_connections = max_connections
    cfg.max_conn_per_ip = max_conn_per_ip
    cfg.ip_whitelist_enabled = ip_whitelist_enabled
    cfg.ip_whitelist = list(ip_whitelist or [])

    srv = track(MySQLServer(cfg, db, ex))
    cfg.port = srv.server_address[1]
    _serve_in_background(srv)
    return srv, db, cfg


def _connect(port: int, password: str, user: str = 'root',
             database: str = None):
    """建立一条受 track() 管理的 pymysql 连接（用例失败也会被关掉）"""
    kwargs = {}
    if database is not None:
        kwargs['database'] = database
    return track(pymysql.connect(
        host='127.0.0.1', port=port, user=user, password=password,
        charset='utf8mb4', connect_timeout=5, read_timeout=10,
        write_timeout=10, autocommit=True, **kwargs))


def _err_code(exc: BaseException):
    """pymysql 错误 -> errno（失败时返回 None）"""
    args = getattr(exc, 'args', ())
    return args[0] if args else None


def _read_generated_password(data_dir) -> str:
    path = os.path.join(str(data_dir), ROOT_PASSWORD_FILE)
    assert os.path.isfile(path), f'缺少自动生成的 root 口令文件: {path}'
    with open(path, 'r', encoding='utf-8') as f:
        return f.read().strip()


def _clear_password_env(monkeypatch):
    """清掉与口令相关的环境变量，验证真正的安全默认"""
    monkeypatch.delenv('LIGHTDB_ALLOW_EMPTY_PASSWORD', raising=False)
    monkeypatch.delenv('LIGHTDB_ROOT_PASSWORD', raising=False)


# ==================== 1. 默认不再空口令 ====================

def test_fresh_data_dir_generates_random_root_password(tmp_path, monkeypatch):
    """全新数据目录：生成随机口令、落盘 root.password、users 里存的是哈希"""
    _clear_password_env(monkeypatch)
    data = tmp_path / 'fresh'
    db = track(Database(str(data), allow_empty_password=False))

    password = _read_generated_password(data)
    assert len(password) >= 16, f'自动生成的口令太短: {len(password)}'
    stored = db.users['root']['auth_hash']
    assert stored, 'root 仍然是空口令 hash（安全默认失效）'
    assert stored == hash_native_password(password), \
        'root.password 里的口令与 _meta.json 里的哈希不一致'
    assert stored != hash_native_password('')

    # 口令文件尽量 0600（POSIX 上可验证；Windows 的 chmod 基本是 no-op）
    if os.name == 'posix':
        import stat
        mode = stat.S_IMODE(os.stat(data / ROOT_PASSWORD_FILE).st_mode)
        assert mode & 0o077 == 0, f'root.password 权限过宽: {oct(mode)}'

    # 明文绝不能进 _meta.json
    with open(str(data / '_meta.json'), 'r', encoding='utf-8') as f:
        meta_text = f.read()
    assert password not in meta_text, '_meta.json 里出现了 root 明文口令'


def test_empty_password_login_is_denied_by_default(tmp_path, monkeypatch):
    """默认安全行为：root/'' 登录 → 1045；root.password 里的口令 → 成功"""
    _clear_password_env(monkeypatch)
    srv, _db, cfg = _start_server(
        tmp_path, db_kwargs={'allow_empty_password': False})
    password = _read_generated_password(tmp_path / 'data')

    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(cfg.port, '')
    assert _err_code(ei.value) == 1045, ei.value
    assert 'Access denied' in str(ei.value)

    conn = _connect(cfg.port, password)
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)


def test_legacy_empty_root_is_migrated_on_upgrade(tmp_path, monkeypatch):
    """旧数据目录（root 空口令）升级后不再无认证：自动迁移成随机口令"""
    _clear_password_env(monkeypatch)
    data = tmp_path / 'legacy'

    # 模拟旧版本行为：root 空口令，且已持久化到 _meta.json
    legacy = Database(str(data), allow_empty_password=True)
    try:
        assert legacy.users['root']['auth_hash'] == ''
        legacy.save_meta()
    finally:
        legacy.shutdown()
    assert not os.path.exists(str(data / ROOT_PASSWORD_FILE))

    # 升级启动（没有显式降级开关）→ 必须迁移
    migrated = Database(str(data))          # 不传 allow_empty_password，读环境
    try:
        password = _read_generated_password(data)
        assert migrated.users['root']['auth_hash'] == \
            hash_native_password(password)
    finally:
        migrated.shutdown()

    # 迁移结果确实落盘了（重启/换进程读到的也是新哈希）
    import json
    with open(str(data / '_meta.json'), 'r', encoding='utf-8') as f:
        meta = json.load(f)
    assert meta['users']['root']['auth_hash'] != '', \
        '迁移没有写回 _meta.json，重启后仍是无认证'
    assert meta['users']['root']['auth_hash'] == \
        hash_native_password(password)

    # 端到端：空口令被拒，迁移后的口令可登录
    srv, _db, cfg = _start_server(tmp_path, data_dir=str(data))
    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(cfg.port, '')
    assert _err_code(ei.value) == 1045
    conn = _connect(cfg.port, password)
    with conn.cursor() as cur:
        cur.execute('SELECT VERSION()')
        assert 'LightDB' in str(cur.fetchone()[0])


# ==================== 2. 显式口令 / 显式降级 ====================

def test_explicit_allow_empty_password(tmp_path, monkeypatch):
    """显式降级（env）= 允许空口令；这是本地开发/演示的开关"""
    _clear_password_env(monkeypatch)
    monkeypatch.setenv('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
    srv, db, cfg = _start_server(tmp_path)   # 不传参数 -> 读环境变量

    assert db.users['root']['auth_hash'] == ''
    assert not os.path.exists(os.path.join(str(tmp_path / 'data'),
                                           ROOT_PASSWORD_FILE)), \
        '显式允许空口令时不应生成 root.password'

    conn = _connect(cfg.port, '')
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)


def test_allow_empty_password_via_cli_parser_and_check(monkeypatch):
    """CLI：--allow-empty-password / --root-password 被解析并接入 Config"""
    from lightdb.main import build_parser as build_main_parser
    from lightdb.main_v4 import build_parser as build_v4_parser

    for build in (build_main_parser, build_v4_parser):
        args = build().parse_args([
            '--allow-empty-password', '--root-password', 'S3cret!'])
        assert args.allow_empty_password is True
        assert args.root_password == 'S3cret!'
        assert build().parse_args([]).allow_empty_password is False

    # --check 是纯配置校验：安全默认下应退出 0，且横幅里能看出默认认证方式
    from lightdb import main as main_mod
    _clear_password_env(monkeypatch)
    rc = main_mod.main(['--check', '--config',
                        str(monkeypatch_tmp_path('no_such_config.json'))])
    assert rc == 0


def monkeypatch_tmp_path(name: str) -> str:
    """--check 用的"一定不存在"的配置文件路径（不依赖 CWD）"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), name)


def test_root_password_from_env_and_argument(tmp_path, monkeypatch):
    """显式口令优先：LIGHTDB_ROOT_PASSWORD / root_password= 参数都生效"""
    _clear_password_env(monkeypatch)
    monkeypatch.setenv('LIGHTDB_ROOT_PASSWORD', 'EnvPass-123456')
    env_db = track(Database(str(tmp_path / 'envdir')))
    assert env_db.users['root']['auth_hash'] == \
        hash_native_password('EnvPass-123456')

    arg_db = track(Database(
        str(tmp_path / 'argdir'),
        root_password='ArgPass-123456', allow_empty_password=False))
    assert arg_db.users['root']['auth_hash'] == \
        hash_native_password('ArgPass-123456')
    assert not os.path.exists(str(tmp_path / 'argdir' / ROOT_PASSWORD_FILE))


def test_config_env_mapping_and_defaults(monkeypatch):
    """Config 默认监听回环；0.0.0.0 只能通过 env/CLI/config.json 显式指定"""
    _clear_password_env(monkeypatch)
    cfg = Config()
    assert cfg.host == '127.0.0.1', \
        '默认监听地址必须回到环回地址（P0：默认部署暴露到网络）'
    assert cfg.max_connections == 100
    assert cfg.max_conn_per_ip == 20
    assert cfg.allow_empty_password is False
    assert cfg.root_password == ''
    assert cfg.validate() == []

    # 显式指定仍然有效：--host / config.json / env 都能恢复对外监听
    from lightdb.main import build_parser as build_main_parser
    assert build_main_parser().parse_args(
        ['--host', '0.0.0.0']).host == '0.0.0.0'
    assert Config.from_dict({'host': '0.0.0.0'}).host == '0.0.0.0'

    # 环境变量映射：显式指定 0.0.0.0 / 口令 / 降级开关
    monkeypatch.setenv('LIGHTDB_HOST', '0.0.0.0')
    monkeypatch.setenv('LIGHTDB_ROOT_PASSWORD', 'FromEnv-1')
    monkeypatch.setenv('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
    loaded = Config.load(os.path.join(str(monkeypatch_tmp_path(
        'no_such.json'))))
    assert loaded.host == '0.0.0.0'
    assert loaded.root_password == 'FromEnv-1'
    assert loaded.allow_empty_password is True
    # 两者同时设置是矛盾配置：validate 必须报出来
    assert any('allow_empty_password' in e for e in loaded.validate()), \
        loaded.validate()


# ==================== 3. 连接限制 / IP 白名单 ====================

class _FakeRequest:
    """socketserver 传给 verify_request/shutdown_request 的 request 桩"""

    def __init__(self):
        self.sent = b''
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        self.closed = True

    def close(self):
        self.closed = True


def _errno_of_sent(payload: bytes) -> int:
    """服务端首包（[3B len][1B seq][0xFF][2B code]）里的错误码"""
    assert payload and payload[4] == 0xFF, payload[:8]
    return int.from_bytes(payload[5:7], 'little')


def test_per_ip_connection_limit_enforced(tmp_path, monkeypatch):
    """单 IP 并发连接超限 → 握门前 ERR 1040；释放后可重新连上"""
    _clear_password_env(monkeypatch)
    srv, _db, cfg = _start_server(
        tmp_path, db_kwargs={'allow_empty_password': True},
        max_conn_per_ip=2, max_connections=10)

    first = _connect(cfg.port, '')
    second = _connect(cfg.port, '')
    with first.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)
    with second.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)

    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(cfg.port, '')
    assert _err_code(ei.value) == 1040, ei.value
    assert 'max_conn_per_ip' in str(ei.value)

    # 释放一条后应能重新连上（证明 shutdown_request 真的减了计数）
    first.close()
    deadline = time.time() + 5.0
    last_err = None
    while time.time() < deadline:
        try:
            again = _connect(cfg.port, '')
            with again.cursor() as cur:
                cur.execute('SELECT 1')
                assert cur.fetchone() == (1,)
            break
        except pymysql.err.OperationalError as e:   # 计数还没减完，稍后再试
            last_err = e
            time.sleep(0.05)
    else:                                          # pragma: no cover - 失败路径
        raise AssertionError(f'连接释放后仍无法重连: {last_err}')


def test_total_connection_limit_and_counts(tmp_path, monkeypatch):
    """全局连接上限（max_connections）也在握门前生效"""
    _clear_password_env(monkeypatch)
    srv, _db, cfg = _start_server(
        tmp_path, db_kwargs={'allow_empty_password': True},
        max_connections=2, max_conn_per_ip=20)

    conns = [_connect(cfg.port, '') for _ in range(2)]
    assert srv.connection_stats()['active_connections'] == 2

    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(cfg.port, '')
    assert _err_code(ei.value) == 1040, ei.value
    assert 'max_connections' in str(ei.value)

    for c in conns:
        c.close()
    _wait_admission_idle(srv)


def test_verify_request_rejects_without_network(tmp_path, monkeypatch):
    """直接验证准入逻辑（不依赖网络时序）：白名单 1130 / 超限 1040"""
    _clear_password_env(monkeypatch)

    # ---------- 白名单 ----------
    srv_wl, _db, _cfg = _start_server(
        tmp_path / 'wl', db_kwargs={'allow_empty_password': True},
        ip_whitelist_enabled=True, ip_whitelist=['127.0.0.1/32'])

    allowed = _FakeRequest()
    assert srv_wl.verify_request(allowed, ('127.0.0.1', 12345)) is True
    denied = _FakeRequest()
    assert srv_wl.verify_request(denied, ('10.66.66.66', 12345)) is False
    assert _errno_of_sent(denied.sent) == 1130
    srv_wl.shutdown_request(allowed)
    assert srv_wl.connection_stats()['active_connections'] == 0

    # ---------- 连接数上限 ----------
    srv, _db2, _cfg2 = _start_server(
        tmp_path / 'conn', db_kwargs={'allow_empty_password': True},
        max_connections=3, max_conn_per_ip=2)

    # 单 IP 上限 2：同一 IP 第 3 条被 1040 拒绝
    a = _FakeRequest()
    b = _FakeRequest()
    c = _FakeRequest()
    assert srv.verify_request(a, ('10.0.0.1', 1)) is True
    assert srv.verify_request(b, ('10.0.0.1', 2)) is True
    assert srv.verify_request(c, ('10.0.0.1', 3)) is False
    assert _errno_of_sent(c.sent) == 1040
    srv.shutdown_request(a)
    srv.shutdown_request(b)

    # 全局上限 3：三个不同 IP 占满后第 4 个被拒
    d, e, f = _FakeRequest(), _FakeRequest(), _FakeRequest()
    g = _FakeRequest()
    assert srv.verify_request(d, ('10.0.0.2', 1)) is True
    assert srv.verify_request(e, ('10.0.0.3', 1)) is True
    assert srv.verify_request(f, ('10.0.0.4', 1)) is True
    assert srv.verify_request(g, ('10.0.0.5', 1)) is False
    assert _errno_of_sent(g.sent) == 1040
    for req in (d, e, f):
        srv.shutdown_request(req)

    stats = srv.connection_stats()
    assert stats['active_connections'] == 0, stats
    assert stats['max_connections'] == 3
    assert stats['max_conn_per_ip'] == 2


def test_ip_whitelist_rejects_outside_hosts(tmp_path, monkeypatch):
    """白名单启用时，非白名单 IP 在握手前收到 1130"""
    _clear_password_env(monkeypatch)
    srv, _db, cfg = _start_server(
        tmp_path, db_kwargs={'allow_empty_password': True},
        ip_whitelist_enabled=True, ip_whitelist=['10.0.0.0/8'])

    with pytest.raises(pymysql.err.OperationalError) as ei:
        _connect(cfg.port, '')
    assert _err_code(ei.value) == 1130, ei.value
    assert 'not allowed' in str(ei.value)

    # 换成包含回环地址的规则就能连（证明白名单真的按规则放行）
    srv2, _db2, cfg2 = _start_server(
        tmp_path / 'allowed', db_kwargs={'allow_empty_password': True},
        ip_whitelist_enabled=True, ip_whitelist=['127.0.0.1/32'])
    conn = _connect(cfg2.port, '')
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone() == (1,)
