"""AgentBaseTool — Agent 工具基类

所有 Agent 专属工具均继承此类。通过构造时注入 subagent_handler、
session_id 和 base_agent_config，避免依赖全局状态。

类比 MemoryBaseTool 对 MemoryManager 的注入方式。
"""

from collections.abc import Callable
from typing import Any

from ...logging import get_running_log
from ...tool.base_tool import BaseTool, ToolResult, ToolStatus

_rlog = get_running_log()


class AgentBaseTool(BaseTool):
    """Agent 工具基类

    Args:
        tool_config:       工具配置字典（需含 tool_type / tool_name / enabled）
        subagent_handler:  AgentFactory.create_subagent 的引用（由 SpawnAgent 注入）
        session_id:        当前主 Agent 的 session ID（子 Agent 工作目录推断 + SecurityHandler 调用用）
        base_agent_config: 主 Agent 的配置字典（传递 llm 等字段给子 Agent）
    """

    def __init__(
        self,
        tool_config: dict[str, Any],
        subagent_handler: Callable,
        session_id: str,
        base_agent_config: dict[str, Any],
    ) -> None:
        super().__init__(tool_config)
        self.subagent_handler: Callable = subagent_handler
        self.session_id: str = session_id
        self.base_agent_config: dict[str, Any] = base_agent_config

    # ------------------------------------------------------------------
    # 快捷构造结果（类比 MemoryBaseTool）
    # ------------------------------------------------------------------

    def _fail(self, error: str, log_tag: str = "agent_tool") -> ToolResult:
        """快捷构造失败结果"""
        _rlog.error(log_tag, f"[{self.tool_name}] {error}")
        return ToolResult(status=ToolStatus.FAILED, error=error)

    def _success(self, output: Any, metadata: dict[str, Any] | None = None) -> ToolResult:
        """快捷构造成功结果"""
        return ToolResult(status=ToolStatus.SUCCESS, output=output, metadata=metadata or {})

    # ------------------------------------------------------------------
    # mask_output 默认实现（字符串输出直接替换，无路径脱敏需求）
    # ------------------------------------------------------------------

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """对工具输出进行路径脱敏（Agent 工具输出通常为文本摘要，按字符串处理）"""
        if not path_mask_map:
            return raw_output
        if isinstance(raw_output, str):
            return BaseTool._mask_string(raw_output, path_mask_map)
        if isinstance(raw_output, dict):
            return {k: self.mask_output(v, path_mask_map) for k, v in raw_output.items()}
        if isinstance(raw_output, list):
            return [self.mask_output(item, path_mask_map) for item in raw_output]
        return raw_output
