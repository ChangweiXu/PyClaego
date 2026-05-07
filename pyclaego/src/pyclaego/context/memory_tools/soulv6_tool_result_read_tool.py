"""SoulV6ToolResultReadTool — 从磁盘按需读取被 spill 的工具结果

签名：``tool_result_read(tool_call_id: str, range: Optional[List[int]] = None)``

行为：
- ``tool_call_id``：先前某次工具调用的 id
- ``range``：可选的 [start_char, end_char] 字符索引窗口；不传则返回前 N 字符

与普通记忆工具不同，此工具由主对话直接调用，返回纯文本片段。
"""

from typing import Any

from ...logging import get_running_log
from ...tool.base_tool import BaseTool, ToolResult, ToolStatus
from ..soulv6_tool_result_store import SoulV6ToolResultStore

_rlog = get_running_log()


class SoulV6ToolResultReadTool(BaseTool):
    """按 tool_call_id 从磁盘读取被 spill 的工具结果片段"""

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def __init__(
        self,
        tool_config: dict[str, Any],
        store: SoulV6ToolResultStore | None = None,
    ) -> None:
        super().__init__(tool_config)
        self._store = store or SoulV6ToolResultStore.get_instance()

    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        return raw_output

    async def execute(self, **kwargs) -> ToolResult:
        tool_call_id = kwargs.get("tool_call_id")
        if not tool_call_id or not isinstance(tool_call_id, str):
            return ToolResult(
                status=ToolStatus.FAILED,
                error="tool_call_id 必填，且必须是字符串",
            )

        raw_range = kwargs.get("range")
        char_range: tuple | None = None
        if raw_range is not None:
            if (
                not isinstance(raw_range, (list, tuple))
                or len(raw_range) != 2
                or not all(isinstance(x, int) for x in raw_range)
            ):
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error="range 必须是 [start_char:int, end_char:int] 数组",
                )
            char_range = (int(raw_range[0]), int(raw_range[1]))

        slice_ = await self._store.read(tool_call_id, char_range)
        if slice_ is None:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=(
                    f"未找到 tool_call_id={tool_call_id} 的工具结果。"
                    "可能原因：(1) id 拼写错误；(2) 该结果未被 spill 到磁盘；"
                    "(3) 该结果来自旧会话且已被清理。"
                ),
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={
                "tool_call_id": slice_.tool_call_id,
                "start_char": slice_.start_char,
                "end_char": slice_.end_char,
                "total_chars": slice_.total_chars,
                "total_tokens": slice_.total_tokens,
                "truncated": slice_.truncated,
                "text": slice_.text,
            },
        )

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "按 tool_call_id 从磁盘读取先前被 SoulV6 spill（因过大而未完整塞进上下文）"
                "的工具结果片段。若缩略消息里出现 `[Full content available via tool_result_read(...)]`，"
                "调用本工具获取完整内容。可选 `range` 限定字符窗口以控制返回体积。"
            ),
            "parameters": {
                "tool_call_id": {
                    "type": "string",
                    "required": True,
                    "description": "工具调用 ID（来自缩略消息里的 tool_call_id）",
                },
                "range": {
                    "type": "array",
                    "required": False,
                    "description": (
                        "可选 [start_char, end_char] 字符索引窗口；不传则读取前 N 字符"
                    ),
                    "items": {"type": "integer"},
                },
            },
            "is_readonly": True,
            "is_parallelizable": True,
        }
