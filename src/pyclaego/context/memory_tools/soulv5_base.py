"""SoulV5MemoryBaseTool — SoulV5 记忆工具基类

所有 SoulV5 记忆工具均继承此类。通过构造时注入 SoulV5MemoryManager 实例。
"""

from typing import Dict, Any, Optional

from ...tool.base_tool import BaseTool, ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class SoulV5MemoryBaseTool(BaseTool):
    """SoulV5 记忆工具基类

    Args:
        tool_config: 工具配置字典（含 tool_type / tool_name / enabled）
        memory_manager: SoulV5MemoryManager 实例
    """

    def __init__(self, tool_config: Dict[str, Any], memory_manager: Any):
        super().__init__(tool_config)
        self.memory_manager = memory_manager

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """不用于 SoulV5 记忆工具的输出脱敏，直接返回原始输出"""
        return raw_output

    def _fail(self, error: str, log_tag: str = "soulv5_tool") -> ToolResult:
        _rlog.error(log_tag, f"[{self.tool_name}] {error}")
        return ToolResult(status=ToolStatus.FAILED, error=error)

    def _success(self, output: Any, metadata: Optional[Dict[str, Any]] = None) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, output=output, metadata=metadata or {})
