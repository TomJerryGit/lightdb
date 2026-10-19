"""Web 版启动入口

安全默认值（P0-D 修复）：
* ``--host`` 默认 ``127.0.0.1``（不再默认 ``0.0.0.0`` 暴露到全网）；
* 连接配置可通过 ``--connections-file`` / ``LIGHTDB_WEB_CONNECTIONS_FILE``
  载入，并通过新增的 ``POST /api/v1/connections`` 在运行期增删（admin）；
* 不打印任何口令；``--admin-password`` 仅作为临时便利参数（会出现在进程
  参数列表里，生产请改用 ``LIGHTDB_WEB_USERS`` 环境变量）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_HOST = '127.0.0.1'
ENV_CONNECTIONS_FILE = 'LIGHTDB_WEB_CONNECTIONS_FILE'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='lightdb-studio-web')
    ap.add_argument('--host', default=DEFAULT_HOST,
                    help='监听地址（默认 127.0.0.1，仅本机可达；'
                         '对外暴露请自行加 TLS/反向代理）')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--reload', action='store_true')
    ap.add_argument('--connections-file', default=None,
                    help='连接配置 JSON 文件（数组）路径；'
                         f'默认读环境变量 {ENV_CONNECTIONS_FILE}')
    ap.add_argument('--admin-password', default=None,
                    help='初始 admin 口令（不推荐：会出现在进程参数里；'
                         '建议改用 LIGHTDB_WEB_USERS）')
    args = ap.parse_args(argv)

    try:
        from web.server import create_app, load_connections_file
    except ImportError as e:
        print(f"导入失败: {e}")
        print("请先安装: pip install -r requirements_web.txt")
        sys.exit(1)

    connections_file = (args.connections_file
                        or os.environ.get(ENV_CONNECTIONS_FILE) or None)
    connections = []
    if connections_file:
        connections = load_connections_file(connections_file)

    app = create_app(connections=connections,
                     admin_password=args.admin_password,
                     connections_file=connections_file)

    try:
        import uvicorn
    except ImportError:
        print("请先安装 uvicorn: pip install uvicorn[standard]")
        sys.exit(1)

    uvicorn.run(app, host=args.host, port=args.port,
                reload=args.reload, log_level='info')


if __name__ == '__main__':
    main()
