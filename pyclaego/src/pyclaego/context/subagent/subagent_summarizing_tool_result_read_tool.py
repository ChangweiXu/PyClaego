"""SubAgentSummarizingToolResultReadTool — 基于 token 偏移的工具结果按需读取工具

与 V6 版本（char-offset）的区别：
  - 参数改为 token 偏移（start_token / token_limit）
  - token_limit 硬上限 20 000
  - 内部通过 TokenCounter.slice_tokens() 精确提取 token 范围

工具名：``tool_result_read``

参数：
    tool_call_id  (str, required): 要读取的工具调用 ID
    start_token   (int, optional): 起始 token 偏移（默认 0）
    token_limit   (int, optional): 最多读取的 token 数（默认 & 上限 20 000）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .subagent_soulv6_artifact_store import SubAgentSoulV6ArtifactStore

TOOL_NAME = "tool_result_read"
MAX_TOKEN_LIMIT = 20_000

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
        "start_token": {
            "type": "integer",
            "description": "Start token offset (inclusive, 0-based). Defaults to 0.",
        },
        "token_limit": {
            "type": "integer",
            "description": (
                f"Maximum number of tokens to read (default and hard cap: {MAX_TOKEN_LIMIT}). "
                "If the content has more tokens, it will be truncated."
            ),
        },
    },
    "required": ["tool_call_id"],
}

TOOL_DESCRIPTION = (
    "Read the full or partial content of a spilled tool result from disk using token offsets. "
    "When a tool result was too large to keep in context, it is stored on disk "
    "and a placeholder is shown instead. Use this tool to retrieve the actual content "
    "by providing the tool_call_id shown in the placeholder. "
    f"Optionally specify start_token for pagination (reads up to {MAX_TOKEN_LIMIT} tokens per call)."
)


@dataclass
class SummarizingToolExecuteResult:
    status: str   # "ok" | "not_found" | "failed"
    output: str
    error: str | None = None


class SubAgentSummarizingToolResultReadTool:
    """子 Agent 工具结果读取工具（token 偏移版，上下文绑定）"""

    name: str = TOOL_NAME

    def __init__(self, store: SubAgentSoulV6ArtifactStore) -> None:
        self._store = store

        from ..token_counter import TokenCounter
        self._token_counter = TokenCounter()

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
        start_token: int = 0,
        token_limit: int = MAX_TOKEN_LIMIT,
        **_kwargs: Any,
    ) -> SummarizingToolExecuteResult:
        """执行基于 token 偏移的读取操作"""
        try:
            # 校验并 clamp token_limit
            start_token = max(0, int(start_token))
            token_limit = max(1, min(int(token_limit), MAX_TOKEN_LIMIT))

            artifact = self._store.get_artifact(tool_call_id)
            if artifact is None or not artifact.path.exists():
                return SummarizingToolExecuteResult(
                    status="not_found",
                    output="",
                    error=f"No artifact found for tool_call_id={tool_call_id!r}",
                )

            import asyncio
            content = await asyncio.to_thread(artifact.path.read_text, "utf-8")

            total_tokens = self._token_counter.count_tokens(content)
            end_token = start_token + token_limit
            sliced_text = self._token_counter.slice_tokens(content, start_token, end_token)
            actual_end = start_token + self._token_counter.count_tokens(sliced_text)
            truncated = actual_end < total_tokens

            header = (
                f"[tool_result_read] tool_call_id={tool_call_id} "
                f"tokens={start_token}..{actual_end}/{total_tokens}"
                + (" [truncated]" if truncated else "")
                + "\n\n"
            )
            return SummarizingToolExecuteResult(
                status="ok",
                output=header + sliced_text,
            )
        except Exception as e:
            return SummarizingToolExecuteResult(
                status="failed",
                output="",
                error=str(e),
            )
