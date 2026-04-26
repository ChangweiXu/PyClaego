"""context/subagent — 子 Agent 专属上下文子模块

提供 BaseSubAgentContextHandler：
  - 不从磁盘恢复历史，从参数直接构建初始上下文
  - 支持 memory_mode="empty" / "inherit"
  - 工具列表仅来自 ToolManager（天然不含 Memory / Agent 工具）
  - 新增 checkpoint "spawn_context_init"（父 Agent Context 按需实现）

提供 InfoGathererContextHandler：
  - 供 info_gatherer 子 Agent 使用
  - 使用信息收集专属系统提示词
  - 仅暴露读写 / 下载 / 网络工具，排除 bash 等危险工具
"""

from .base_subagent_context import BaseSubAgentContextHandler
from .code_explorer_context import CodeExplorerContextHandler
from .info_gatherer_context import InfoGathererContextHandler

__all__ = [
    "BaseSubAgentContextHandler",
    "CodeExplorerContextHandler",
    "InfoGathererContextHandler",
]
