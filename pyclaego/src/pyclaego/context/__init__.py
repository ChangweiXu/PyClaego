"""Context 模块 - 上下文管理系统

提供可扩展的上下文管理策略，支持不同的上下文构建方式。
"""

# from .mcp_context.kbase_context import KbaseContextHandler
from .agent_pipeline.context import PipelineWindowContextHandler
from .agent_pipeline.simple_v3 import SimpleV3ContextHandler
from .base_context import (
    BaseContextHandler,
    BaseContextHandlerV2,
    BaseContextHandlerV3,
    ContextCheckPoint,
)
from .context_factory import ContextFactory
from .history_manager import HistoryFileManager

# from .simple_context import SimpleContextHandler
# from .simple_context_v2 import SimpleContextHandlerV2
# from .soul_context import SoulContextHandler
# from .soul_context_v2 import SoulContextHandlerV2
# from .soul_context_v3 import SoulContextHandlerV3
# from .soul_context_v4 import SoulContextHandlerV4
# from .soul_context_v5 import SoulContextHandlerV5
# from .soulv5_context_handler import SoulV5ContextHandler
# from .soulv6_context_handler import SoulV6ContextHandler
from .subagent import (
    BaseSubAgentContextHandler,
    # InfoGathererContextHandler,
)

# from .window_context import WindowContextHandler
# from .window_context_v2 import WindowContextHandlerV2

# 注册上下文处理器类型
# ContextFactory.register_handler("simple", SimpleContextHandler)
# ContextFactory.register_handler("simple_v2", SimpleContextHandlerV2)
ContextFactory.register_handler("simple_v3", SimpleV3ContextHandler)
# ContextFactory.register_handler("soul", SoulContextHandler)
# ContextFactory.register_handler("soul_v2", SoulContextHandlerV2)
# ContextFactory.register_handler("soul_v3", SoulContextHandlerV3)
# ContextFactory.register_handler("soul_v4", SoulContextHandlerV4)
# ContextFactory.register_handler("soul_v5", SoulContextHandlerV5)
# ContextFactory.register_handler("soul_v5", SoulV5ContextHandler)
# ContextFactory.register_handler("soul_v6", SoulV6ContextHandler)
# ContextFactory.register_handler("window", WindowContextHandler)
# ContextFactory.register_handler("window_v2", WindowContextHandlerV2)
# ContextFactory.register_handler("kbase_mcp", KbaseContextHandler)
ContextFactory.register_handler("window_v3", PipelineWindowContextHandler)
ContextFactory.register_handler("pipeline_window", PipelineWindowContextHandler)

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
    # "InfoGathererContextHandler",
    # =====
    "ContextCheckPoint",
    "HistoryFileManager",
    "ContextFactory",
    # "SimpleContextHandler",
    # "SimpleContextHandlerV2",
    # "SoulContextHandler",
    # "SoulContextHandlerV2",
    # "SoulContextHandlerV3",
    # "SoulContextHandlerV4",
    # "SoulContextHandlerV5",
    # "SoulV5ContextHandler",
    # "SoulV6ContextHandler",
    # "WindowContextHandler",
    # "WindowContextHandlerV2",
    # "KbaseContextHandler",
    "get_context_factory"
]
