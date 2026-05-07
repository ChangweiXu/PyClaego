"""context/agent_pipeline — Pipeline 专用上下文处理器"""
# 子代理体系已统一迁移至 ToolAgentConfig。
# 向后兼容：从 tool_agent 重新导出。
from ...tool_agent import (
    SUBAGENT_PROFILES,
    ToolAgentConfig,
    get_profile,
    list_profile_names,
    register_profile,
    resolve_profile,
)
from .context import BasePipelineContextHandler, PipelineWindowContextHandler
from .simple_v3 import SimpleV3ContextHandler

__all__ = [
    "SUBAGENT_PROFILES",
    "BasePipelineContextHandler",
    "PipelineWindowContextHandler",
    "SimpleV3ContextHandler",
    "ToolAgentConfig",
    "get_profile",
    "list_profile_names",
    "register_profile",
    "resolve_profile",
]
