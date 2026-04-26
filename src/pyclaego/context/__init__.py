"""Context 模块 - 上下文管理系统

提供可扩展的上下文管理策略，支持不同的上下文构建方式。
"""

from .base_context import (
    BaseContextHandler,
    BaseContextHandlerV2,
    BaseContextHandlerV3,
    ContextCheckPoint,
)
from .subagent import (
    BaseSubAgentContextHandler,
    InfoGathererContextHandler,
)
from .history_manager import HistoryFileManager
from .context_factory import ContextFactory
from .soulv5_context_handler import SoulV5ContextHandler
from .soulv6_context_handler import SoulV6ContextHandler

# 注册上下文处理器类型
ContextFactory.register_handler("soul_v5", SoulV5ContextHandler)
ContextFactory.register_handler("soul_v6", SoulV6ContextHandler)

# 便捷函数
def get_context_factory():
    """获取 ContextFactory 实例"""
    return ContextFactory


__all__ = [
    "BaseContextHandler",
    "BaseContextHandlerV2",
    "BaseContextHandlerV3",
    # =====
    "BaseSubAgentContextHandler",
    "InfoGathererContextHandler",
    # =====
    "ContextCheckPoint",
    "HistoryFileManager",
    "ContextFactory",
    "SoulV5ContextHandler",
    "SoulV6ContextHandler",
    "get_context_factory"
]
