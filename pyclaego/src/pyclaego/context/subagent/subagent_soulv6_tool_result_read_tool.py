"""SubAgentSoulV6ToolResultReadTool — 子 Agent 工具结果按需读取工具

这是一个上下文绑定的内联工具（不注册到全局 ToolManager），
由 SubAgentSoulV6ContextHandler 在 _build_tool_list() 时动态注入工具列表，
并在 handle_memory_tool_calls() 中拦截执行。

工具名：``subagent_soulv6_tool_result_read``

参数：
    tool_call_id (str, required): 要读取的工具调用 ID
    char_start   (int, optional): 起始字符偏移（默认 0）
    char_end     (int, optional): 结束字符偏移（默认读取至文件末尾，store 内的 token 预算截断）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .subagent_soulv6_artifact_store import SubAgentSoulV6ArtifactStore

TOOL_NAME = "subagent_soulv6_tool_result_read"

# JSON Schema 参数定义（供 ToolDefinition 使用）
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_call_id": {
            "type": "string",
            "description": (
                "The tool_call_id of the spilled tool result to read. "
                "Use the exact id shown in the placeholder message."
            ),
        },
        "char_start": {
            "type": "integer",
            "description": "Start character offset (inclusive, 0-based). Defaults to 0.",
        },
        "char_end": {
            "type": "integer",
            "description": (
                "End character offset (exclusive). "
                "Defaults to reading to end of file; content is capped by the store's token budget."
            ),
        },
    },
    "required": ["tool_call_id"],
}

TOOL_DESCRIPTION = (
    "Read the full or partial content of a spilled tool result from disk. "
    "When a tool result was too large to keep in context, it is stored on disk "
    "and a placeholder is shown instead. Use this tool to retrieve the actual content "
    "by providing the tool_call_id shown in the placeholder. "
    "Optionally specify char_start / char_end for partial reads."
)


@dataclass
class SubAgentSoulV6ToolExecuteResult:
    status: str   # "ok" | "not_found" | "failed"
    output: str
    error: str | None = None


class SubAgentSoulV6ToolResultReadTool:
    """子 Agent 工具结果读取工具（上下文绑定，非全局注册）"""

    name: str = TOOL_NAME

    def __init__(self, store: SubAgentSoulV6ArtifactStore) -> None:
        self._store = store

    def get_tool_definition(self):
        """返回 ToolDefinition（用于注入 LLM 工具列表）"""
        from ...llm.types import ToolDefinition
        return ToolDefinition(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            parameters=TOOL_PARAMETERS,
        )

    async def execute(
        self,
        tool_call_id: str,
        char_start: int | None = None,
        char_end: int | None = None,
        **_kwargs: Any,
    ) -> SubAgentSoulV6ToolExecuteResult:
        """执行读取操作"""
        try:
            char_range = None
            if char_start is not None and char_end is not None:
                char_range = (int(char_start), int(char_end))
            elif char_start is not None:
                # 不指定 char_end 时读到文件末尾，store.read() 内的 token 预算会截断
                char_range = (int(char_start), 10_000_000)

            result = await self._store.read(tool_call_id, char_range=char_range)

            if result is None:
                return SubAgentSoulV6ToolExecuteResult(
                    status="not_found",
                    output="",
                    error=f"No artifact found for tool_call_id={tool_call_id!r}",
                )

            header = (
                f"[SubAgentSoulV6ToolResultRead] tool_call_id={tool_call_id} "
                f"chars={result.start_char}..{result.end_char}/{result.total_chars} "
                f"tokens≈{result.total_tokens}"
                + (" [truncated]" if result.truncated else "")
                + "\n\n"
            )
            return SubAgentSoulV6ToolExecuteResult(
                status="ok",
                output=header + result.text,
            )
        except Exception as e:
            return SubAgentSoulV6ToolExecuteResult(
                status="failed",
                output="",
                error=str(e),
            )
