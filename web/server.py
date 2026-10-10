"""
FastAPI Web 服务器
"""
import os
import sys
from pathlib import Path
from typing import List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# 依赖注入的全局状态
app_state = {
    'connections': [],
    'users': {
        'admin': {'password': 'admin123', 'role': 'admin'},
        'dev': {'password': 'dev123', 'role': 'developer'},
        'analyst': {'password': 'analyst123', 'role': 'analyst'},
        'viewer': {'password': 'viewer123', 'role': 'viewer'},
    },
    'db': None,
    'executor': None,
}


def create_app(db=None, executor=None, connections: List = None):
    """创建 FastAPI 应用"""
    if db is not None:
        app_state['db'] = db
    if executor is not None:
        app_state['executor'] = executor
    if connections is not None:
        app_state['connections'] = connections

    app = FastAPI(
        title="LightDB Studio Web",
        version="1.0.0",
        description="LightDB Studio 的 Web 版本",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

    # API 路由
    from .api import router
    app.include_router(router)

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


def run_server(host='0.0.0.0', port=8080, db=None, executor=None,
               connections=None):
    """启动 Web 服务器"""
    try:
        import uvicorn
    except ImportError:
        print("请先安装: pip install -r requirements_web.txt")
        sys.exit(1)

    app = create_app(db, executor, connections)
    print("=" * 60)
    print("  LightDB Studio Web")
    print("=" * 60)
    print(f"  URL: http://{host}:{port}/")
    print(f"  API: http://{host}:{port}/api/v1/health")
    print(f"  API 文档: http://{host}:{port}/docs")
    print("=" * 60)
    print("  默认用户:")
    for u, info in app_state['users'].items():
        print(f"    {u} / {info['password']}  ({info['role']})")
    print("=" * 60)

    uvicorn.run(app, host=host, port=port, log_level='info')


if __name__ == '__main__':
    run_server()