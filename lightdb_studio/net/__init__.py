# lightdb_studio/lightdb_studio/net/__init__.py
"""网络模块

实时协作 WebSocket 服务（原 `net/collab_server.py`）已随 Studio 死代码清理
删除：GUI 协作面板只使用进程内 `core.collaboration.CollaborationStore`，
网络实时共享从未接线（原实现依赖可选库 websockets，且无任何 UI 入口）。
"""
