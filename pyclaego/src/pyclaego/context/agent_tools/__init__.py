"""context/agent_tools — Agent 专属工具子模块

提供用于 SpawnAgent 动态创建子 Agent 的工具：
  - AgentBaseTool              Agent 工具基类（类比 MemoryBaseTool）
  - SpawnSubagentTool          创建并执行子 Agent 的工具（类驱动，旧架构）
  - DynamicSpawnSubagentTool   创建并执行子 Agent 的工具（配置驱动，新架构）

工具名称集合：
  AGENT_TOOL_NAMES: frozenset

  SpawnAgent 在工具调用循环中通过此集合过滤拦截 Agent 工具调用，
  避免立即执行，而是收集后统一并发调度。
"""

from .base_agent_tool import AgentBaseTool
from .dynamic_spawn_subagent_tool import DynamicSpawnSubagentTool
from .spawn_subagent_tool import SpawnSubagentTool

__all__ = [
    "AGENT_TOOL_NAMES",
    "AgentBaseTool",
    "DynamicSpawnSubagentTool",
    "SpawnSubagentTool",
]

# Agent 工具名称集合，供 SpawnAgent 过滤拦截时使用
AGENT_TOOL_NAMES: frozenset = frozenset({
    "spawn_subagent",
})
