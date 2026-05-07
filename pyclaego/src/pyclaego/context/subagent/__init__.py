"""context/subagent — 子 Agent 专属上下文子模块

提供 BaseSubAgentContextHandler：
  - 不从磁盘恢复历史，从参数直接构建初始上下文
  - 支持 memory_mode="empty" / "inherit"
  - 工具列表仅来自 ToolManager（天然不含 Memory / Agent 工具）

提供 ConfigurableSubAgentContext（新架构）：
  - 配置驱动，单一通用类，替代 InfoGatherer / CodeExplorer 继承链
  - 通过 SubagentProfile 控制系统提示词、工具白名单、压缩策略

提供 InfoGathererContextHandler（旧架构，保留兼容）：
  - 供 info_gatherer 子 Agent 使用
  - 使用信息收集专属系统提示词
  - 仅暴露读写 / 下载 / 网络工具

提供 SubAgentSoulV6* 系列（V6 升级版）：
  - SubAgentSoulV6ContextHandler：基类，含工具结果落盘、驱逐、预算、技能注入
  - SubAgentSoulV6InfoGathererContextHandler：信息收集 V6 版
  - SubAgentSoulV6CodeExplorerContextHandler：代码探索 V6 版

提供 SubAgentSummarizingContextHandler（Summarizing 策略）：
  - 工具结果落盘（复用 V6 ArtifactStore）
  - Token 偏移读取工具（tool_result_read）
  - LLM 主动摘要驱逐工具（tool_result_summarize_and_evict）
  - 冻结阶段：上下文压力超阈值时仅允许调用 evict 工具
  - Token 预算页脚：每轮注入上下文占用 / 迭代进度
"""

from .base_subagent_context import BaseSubAgentContextHandler
from .code_explorer_context import CodeExplorerContextHandler
from .configurable_subagent_context import ConfigurableSubAgentContext
from .info_gatherer_context import InfoGathererContextHandler
from .subagent_soulv6_code_explorer_context import SubAgentSoulV6CodeExplorerContextHandler
from .subagent_soulv6_context import SubAgentSoulV6ContextHandler
from .subagent_soulv6_info_gatherer_context import SubAgentSoulV6InfoGathererContextHandler
from .subagent_summarizing_context import SubAgentSummarizingContextHandler

__all__ = [
    "BaseSubAgentContextHandler",
    "CodeExplorerContextHandler",
    "ConfigurableSubAgentContext",
    "InfoGathererContextHandler",
    # V6
    "SubAgentSoulV6ContextHandler",
    "SubAgentSoulV6InfoGathererContextHandler",
    "SubAgentSoulV6CodeExplorerContextHandler",
    # Summarizing
    "SubAgentSummarizingContextHandler",
]
