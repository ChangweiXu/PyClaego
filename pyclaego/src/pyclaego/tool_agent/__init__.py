"""ToolAgent 模块 — 磁盘驱动的子代理配置系统

提供从磁盘目录发现和加载子代理配置的能力，遵循 SKILL 的分层模式：
  builtin/ → global/ → PS/ → Widget/

主要类：
  - ToolAgentConfig: 子代理配置数据类（唯一数据模型）
  - ToolAgentManager: 子代理配置管理器（单例）

注册表（取代原 SubagentProfile 体系）：
  - SUBAGENT_PROFILES: 全局子代理注册表
  - register_profile / get_profile / resolve_profile

使用示例：
    >>> from pyclaego.tool_agent import get_tool_agent_manager
    >>> tam = get_tool_agent_manager()
    >>> tam.load_builtins()
    >>> tam.load_globals()
    >>> tam.register_all_to_subagent_profiles()
"""


def get_tool_agent_manager():
    """获取 ToolAgentManager 单例"""
    return ToolAgentManager.get_instance()


from .config import ToolAgentConfig
from .exceptions import (
    ToolAgentConfigError,
    ToolAgentError,
    ToolAgentLoadError,
    ToolAgentNotFoundError,
)
from .manager import ToolAgentManager
from .registry import (
    SUBAGENT_PROFILES,
    get_profile,
    list_profile_names,
    register_profile,
    resolve_profile,
    unregister_profile,
)

__all__ = [
    "SUBAGENT_PROFILES",
    "ToolAgentConfig",
    "ToolAgentConfigError",
    "ToolAgentError",
    "ToolAgentLoadError",
    "ToolAgentManager",
    "ToolAgentNotFoundError",
    "get_profile",
    "get_tool_agent_manager",
    "list_profile_names",
    "register_profile",
    "resolve_profile",
    "unregister_profile",
]

__version__ = "0.1.0"
