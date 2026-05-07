"""消息网关模块

提供 TUI 和飞书两种消息网关：
- TUIGateway / TUIClient: 基于 Textual 的本地 TUI 客户端
- FeishuClient: 飞书 REST API 发送客户端
- FeishuEventListener: 飞书 WebSocket 长连接事件监听
- FeishuGateway: 飞书消息网关（整合收发与 CoreScheduler 路由）
"""

from .feishu_client import FeishuClient
from .feishu_event_listener import FeishuEventListener
from .feishu_gateway import FeishuGateway

__all__ = [
    "FeishuClient",
    "FeishuEventListener",
    "FeishuGateway",
]
