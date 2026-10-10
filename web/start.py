"""Web 版启动入口"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(prog='lightdb-studio-web')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--reload', action='store_true')
    args = ap.parse_args()

    try:
        from web.server import create_app
    except ImportError as e:
        print(f"导入失败: {e}")
        print("请先安装: pip install -r requirements_web.txt")
        sys.exit(1)

    app = create_app()

    try:
        import uvicorn
    except ImportError:
        print("请先安装 uvicorn: pip install uvicorn[standard]")
        sys.exit(1)

    uvicorn.run(app, host=args.host, port=args.port,
                reload=args.reload, log_level='info')


if __name__ == '__main__':
    main()