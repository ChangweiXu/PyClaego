"""Command 模块 — 服务端 slash 命令分发器。

将用户发送的 `/cmd arg...` 路由到 context_handler 上对应的方法。
"""

from .dispatcher import CommandDispatcher

__all__ = ["CommandDispatcher"]
