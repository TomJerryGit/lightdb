"""
FastAPI Web 服务器

安全基线（P0-D 修复）：

* **不再内置任何默认用户/口令**（历史上是 admin/admin123、dev/dev123…且启动
  时明文打印）。用户来源按优先级：
    1. ``LIGHTDB_WEB_USERS`` 环境变量，JSON 形如
       ``{"admin": {"password": "...", "role": "admin"}}``；
    2. ``create_app(admin_password=...)`` / ``--admin-password``；
    3. ``LIGHTDB_WEB_ADMIN_PASSWORD`` 环境变量（容器编排注入单 admin 口令，
       不进入 argv/进程列表——``docker/entrypoint*.sh`` 用这条通道）；
    4. 自动生成随机 admin 口令并写入 ``~/.lightdb_studio/web_admin.txt``
       （0600），启动日志只打印文件路径、**永不打印口令**；该路径不可写时
       只提示设置 ``LIGHTDB_WEB_USERS``。
* CORS 从 ``allow_origins=['*']`` 改为显式白名单
  （``LIGHTDB_WEB_ORIGINS``，逗号分隔），默认只允许本机 Studio Web 源。
* 默认监听 ``127.0.0.1``（对外暴露需显式 ``--host``，并自行加 TLS/反代）。
* ``web/metrics.py`` 路由真正挂载：``/metrics`` 提供 Prometheus 文本。
"""
import json
import logging
import os
import secrets
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .auth import state_dir, write_private_file

logger = logging.getLogger(__name__)

ENV_USERS = 'LIGHTDB_WEB_USERS'
ENV_ADMIN_PASSWORD = 'LIGHTDB_WEB_ADMIN_PASSWORD'
ENV_ORIGINS = 'LIGHTDB_WEB_ORIGINS'
ENV_CONNECTIONS_FILE = 'LIGHTDB_WEB_CONNECTIONS_FILE'
ENV_ALLOWED_HOSTS = 'LIGHTDB_WEB_ALLOWED_HOSTS'

DEFAULT_ORIGINS = ('http://127.0.0.1:8080,http://localhost:8080')
ADMIN_FILE_NAME = 'web_admin.txt'
DEFAULT_HOST = '127.0.0.1'

# 依赖注入的全局状态（create_app 每次都会整体重置，便于测试隔离）
app_state = {
    'connections': [],
    'connections_file': None,
    'users': {},
    'db': None,
    'executor': None,
}


# ==================== 用户解析 ====================

def load_env_users() -> Optional[dict]:
    """从 ``LIGHTDB_WEB_USERS`` 解析用户；未配置/非法时返回 None。"""
    raw = (os.environ.get(ENV_USERS) or '').strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except Exception as e:                        # noqa: BLE001
        logger.warning('%s 不是合法 JSON（%s），已忽略', ENV_USERS, e)
        return None
    if not isinstance(data, dict) or not data:
        logger.warning('%s 必须是非空对象 {"用户名": {"password": ..., '
                       '"role": ...}}，已忽略', ENV_USERS)
        return None

    users = {}
    for name, info in data.items():
        if not isinstance(info, dict):
            logger.warning('%s 中用户 %r 的配置不是对象，已跳过', ENV_USERS, name)
            continue
        password = info.get('password')
        if not isinstance(password, str) or not password:
            logger.warning('%s 中用户 %r 缺少非空 password，已跳过',
                           ENV_USERS, name)
            continue
        role = info.get('role')
        if not isinstance(role, str) or not role:
            # 未写 role 的 admin 账号按 admin 处理，避免把自己锁在门外；
            # 其它账号默认最小权限 user（无任何权限）。
            role = 'admin' if str(name) == 'admin' else 'user'
        users[str(name)] = {'password': password, 'role': role}

    if not users:
        logger.warning('%s 中没有有效用户，已忽略', ENV_USERS)
        return None
    return users


def generate_admin_password() -> str:
    """自动生成 admin 初始口令（高熵，非猜测型）。"""
    return secrets.token_urlsafe(24)


def resolve_users(admin_password: Optional[str] = None) -> dict:
    """按文档优先级解析用户表；任何情况下都不打印口令。

    优先级：``LIGHTDB_WEB_USERS``（多用户 JSON）> 显式 ``admin_password``
    参数 > ``LIGHTDB_WEB_ADMIN_PASSWORD``（容器/进程编排注入单 admin 口令）
    > 自动生成随机 admin 口令并落盘。最后一条的 env 通道供
    ``docker/entrypoint*.sh`` 使用：容器启动时生成/注入口令但不放进
    argv（避免出现在进程列表里），服务端从环境变量读取。
    """
    env_users = load_env_users()
    if env_users:
        logger.info('已从 %s 加载 %d 个用户', ENV_USERS, len(env_users))
        return env_users

    if admin_password:
        logger.info('使用显式传入的 admin 口令（来自 --admin-password）')
        return {'admin': {'password': admin_password, 'role': 'admin'}}

    env_admin_password = (os.environ.get(ENV_ADMIN_PASSWORD) or '').strip()
    if env_admin_password:
        logger.info('使用 %s 提供的 admin 口令（不打印明文）',
                    ENV_ADMIN_PASSWORD)
        return {'admin': {'password': env_admin_password, 'role': 'admin'}}

    password = generate_admin_password()
    path = state_dir() / ADMIN_FILE_NAME
    if write_private_file(path, password + '\n'):
        print(f"[LightDB Web] 已生成随机 admin 口令并写入: {path}（权限 0600）")
        print("[LightDB Web] 登录用户名: admin；也可用 LIGHTDB_WEB_USERS 配置多用户。")
    else:
        print(f"[LightDB Web] 无法写入 {path}（权限不足）；"
              "未生成可用的初始口令。")
        print("[LightDB Web] 请设置 LIGHTDB_WEB_USERS 环境变量后再启动，例如：")
        print('             LIGHTDB_WEB_USERS=\'{"admin": '
              '{"password": "换成长口令", "role": "admin"}}\'')
    return {'admin': {'password': password, 'role': 'admin'}}


# ==================== CORS ====================

def resolve_cors_origins() -> List[str]:
    """CORS 白名单：``LIGHTDB_WEB_ORIGINS``（逗号分隔）> 本机默认。"""
    raw = (os.environ.get(ENV_ORIGINS) or '').strip()
    if raw:
        origins = []
        for item in raw.split(','):
            origin = item.strip()
            if not origin:
                continue
            if origin == '*':
                logger.warning('%s 含通配 "*"，已忽略（必须显式白名单）',
                               ENV_ORIGINS)
                continue
            origins.append(origin)
        if origins:
            return origins
        logger.warning('%s 中没有有效 origin，使用默认白名单', ENV_ORIGINS)
    return [o.strip() for o in DEFAULT_ORIGINS.split(',') if o.strip()]


# ==================== 连接配置文件 ====================

def resolve_connections_file() -> Optional[str]:
    """当前连接持久化文件路径：create_app 参数 > env；未配置返回 None。"""
    path = app_state.get('connections_file') or os.environ.get(
        ENV_CONNECTIONS_FILE)
    return str(path) if path else None


def load_connections_file(path) -> List:
    """从 JSON 文件加载连接（数组，或 ``{"connections": [...]}`` 对象）。

    用于 ``web/start.py --connections-file`` / ``LIGHTDB_WEB_CONNECTIONS_FILE``。
    单条损坏只跳过该条并告警，不拖垮其余连接。
    """
    from lightdb_studio.config import CONNECTION_FIELDS, ConnectionInfo

    p = Path(path).expanduser()
    if not p.exists():
        print(f"[LightDB Web] 连接配置文件不存在: {p}（将以空连接启动）")
        return []

    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:                        # noqa: BLE001
        print(f"[LightDB Web] 读取连接配置失败 {p}: {e}")
        return []

    if isinstance(data, dict):
        data = data.get('connections', [])
    if not isinstance(data, list):
        print(f"[LightDB Web] 连接配置必须是 JSON 数组: {p}")
        return []

    connections = []
    for item in data:
        try:
            if isinstance(item, ConnectionInfo):
                connections.append(item)
            elif isinstance(item, dict):
                known = {k: v for k, v in item.items()
                         if k in CONNECTION_FIELDS}
                connections.append(ConnectionInfo(**known))
            else:
                raise TypeError(f'连接配置必须是对象，收到 {type(item).__name__}')
        except Exception as e:                    # noqa: BLE001
            print(f"[LightDB Web] 跳过无法解析的连接 {item!r}: {e}")
    return connections


def _connection_to_dict(conn) -> dict:
    if is_dataclass(conn):
        return asdict(conn)
    if isinstance(conn, dict):
        return dict(conn)
    raise TypeError(f'不支持的连接对象: {type(conn).__name__}')


def save_connections_file(path, connections) -> bool:
    """把连接列表持久化为 JSON（文件含口令，必须 0600）。"""
    try:
        items = [_connection_to_dict(c) for c in connections]
    except TypeError as e:
        logger.warning('连接序列化失败: %s', e)
        return False
    text = json.dumps(items, ensure_ascii=False, indent=2) + '\n'
    return write_private_file(Path(path).expanduser(), text)


# ==================== 应用工厂 ====================

def create_app(db=None, executor=None, connections: List = None,
               admin_password: Optional[str] = None,
               connections_file: Optional[str] = None,
               reports_dir: Optional[str] = None) -> FastAPI:
    """创建 FastAPI 应用。

    前三个参数保持历史签名 ``create_app(db=None, executor=None,
    connections=None)``；``admin_password`` / ``connections_file`` 为新增
    可选参数（也可分别用 env ``LIGHTDB_WEB_USERS`` /
    ``LIGHTDB_WEB_CONNECTIONS_FILE`` 配置）。

    注意：``app_state`` 是模块级全局 dict，这里每次都会整体重置
    （连接/用户/db/executor），因此多次 ``create_app()``（如测试）互不污染。
    """
    from .api import _session_conns, router

    app_state['db'] = db
    app_state['executor'] = executor
    app_state['connections'] = list(connections) if connections else []
    app_state['connections_file'] = str(connections_file) if connections_file \
        else (os.environ.get(ENV_CONNECTIONS_FILE) or None)
    app_state['users'] = resolve_users(admin_password)
    app_state['reports_dir'] = str(reports_dir) if reports_dir else None
    _session_conns.clear()

    app = FastAPI(
        title="LightDB Studio Web",
        version="1.0.0",
        description="LightDB Studio 的 Web 版本",
    )

    # CORS：显式白名单，不再允许 '*'（与 allow_credentials=True 组合是
    # P0 级配置错误：任意站点都能带着用户凭据跨源调用本 API）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolve_cors_origins(),
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

    # API 路由
    app.include_router(router)

    # Prometheus 指标（web/metrics.py 之前是死代码，从未挂载）
    try:
        from .metrics import router as metrics_router
        app.include_router(metrics_router)
        logger.info('已挂载指标端点 /metrics 与 /api/v1/metrics/json')
    except Exception as e:                        # noqa: BLE001
        logger.warning('指标端点挂载失败（不影响主 API）: %s', e)

    # 静态文件
    static_dir = Path(__file__).parent / 'static'
    if static_dir.exists():
        app.mount('/static', StaticFiles(directory=str(static_dir)),
                  name='static')

        @app.get('/', response_class=HTMLResponse)
        async def index():
            index_file = static_dir / 'index.html'
            if index_file.exists():
                return HTMLResponse(index_file.read_text(encoding='utf-8'))
            return HTMLResponse("<h1>LightDB Studio Web</h1>"
                                "<p>index.html 未找到</p>")

    return app


def run_server(host: str = DEFAULT_HOST, port: int = 8080, db=None,
               executor=None, connections=None,
               admin_password: Optional[str] = None,
               connections_file: Optional[str] = None):
    """启动 Web 服务器。

    注意：这里**不打印任何口令**。自动生成的 admin 口令只落在
    ``~/.lightdb_studio/web_admin.txt``（0600），日志只给出文件路径。
    """
    try:
        import uvicorn
    except ImportError:
        print("请先安装: pip install -r requirements_web.txt")
        sys.exit(1)

    app = create_app(db, executor, connections,
                     admin_password=admin_password,
                     connections_file=connections_file)

    print("=" * 60)
    print("  LightDB Studio Web")
    print("=" * 60)
    print(f"  URL: http://{host}:{port}/")
    print(f"  API: http://{host}:{port}/api/v1/health")
    print(f"  API 文档: http://{host}:{port}/docs")
    print(f"  CORS 白名单: {', '.join(resolve_cors_origins())}")
    conns = app_state.get('connections') or []
    print(f"  连接数: {len(conns)}"
          + (f"（配置文件: {app_state.get('connections_file')}）"
             if app_state.get('connections_file') else ""))
    print("  认证: 用户来自 LIGHTDB_WEB_USERS / --admin-password / "
          "自动生成的 admin 口令文件")
    print("  （本日志不打印任何口令）")
    print("=" * 60)

    uvicorn.run(app, host=host, port=port, log_level='info')


if __name__ == '__main__':
    run_server()
