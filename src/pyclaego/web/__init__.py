"""Web 接口层模块

提供 FastAPI Web 服务，包括 WebSocket 聊天接口。

主要组件:
- app: FastAPI 应用实例
- websocket_router: WebSocket 路由
"""

from .app import app

__all__ = ["app"]
