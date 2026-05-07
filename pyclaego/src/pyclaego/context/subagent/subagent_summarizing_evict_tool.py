"""SubAgentSummarizingEvictTool — LLM 主动调用的工具结果就地摘要驱逐工具

LLM 调用此工具，提供摘要文本，工具会：
  1. 确认 artifact 存在于磁盘（必须已落盘，否则返回 error）
  2. 在 _messages（UnifiedMessage 列表）中找到匹配的 ToolCallResult，
     将其 content 替换为摘要占位文本
  3. 同步更新 _pending_messages（写盘缓冲区）中对应的 dict 条目

工具名：``tool_result_summarize_and_evict``

参数：
    tool_call_id       (str, required): 要驱逐的工具调用 ID
    tool_result_summary (str, required): LLM 生成的摘要内容
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .subagent_soulv6_artifact_store import SubAgentSoulV6ArtifactStore

if TYPE_CHECKING:
    pass

TOOL_NAME = "tool_result_summarize_and_evict"

TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_call_id": {
            "type": "string",
            "description": (
                "The tool_call_id of the tool result to summarize and evict from context. "
                "Use the exact id shown in the placeholder or tool result."
            ),
        },
        "tool_result_summary": {
            "type": "string",
            "description": (
                "A concise summary of the tool result content. This will replace the full "
                "content in context. Include the key findings, data points, and any important "
                "information needed to complete the task."
            ),
        },
    },
    "required": ["tool_call_id", "tool_result_summary"],
}

TOOL_DESCRIPTION = (
    "Summarize and evict a tool result from context to free up space. "
    "Call this when the context window is getting full or when a tool result "
    "is too large and has been processed. Provide a concise summary of the key "
    "information from the tool result. The full content remains available on disk "
    "and can be retrieved later via tool_result_read if needed."
)

_SUMMARY_TEMPLATE = (
    "[SUMMARY by LLM]\n"
    "{summary}\n\n"
    "[Full content available via tool_result_read(tool_call_id='{tool_call_id}')]"
)


@dataclass
class EvictToolExecuteResult:
    status: str   # "ok" | "not_found" | "no_artifact" | "failed"
    output: str
    error: str | None = None


class SubAgentSummarizingEvictTool:
    """工具结果就地摘要驱逐工具（上下文绑定）

    通过持有 context handler 的弱引用（Any 类型避免循环导入）来访问
    _messages 和 _pending_messages。
    """

    name: str = TOOL_NAME

    def __init__(self, store: SubAgentSoulV6ArtifactStore, ctx: Any) -> None:
        """
        Args:
            store: artifact 磁盘存储，用于验证 artifact 是否存在
            ctx:   SubAgentSummarizingContextHandler 实例（Any 类型避免循环导入）
        """
        self._store = store
        self._ctx = ctx  # SubAgentSummarizingContextHandler

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
        tool_result_summary: str,
        **_kwargs: Any,
    ) -> EvictToolExecuteResult:
        """执行就地摘要驱逐

        Args:
            tool_call_id: 要驱逐的工具调用 ID
            tool_result_summary: LLM 提供的摘要文本

        Returns:
            EvictToolExecuteResult
        """
        try:
            # 1. 确认 artifact 存在（必须已落盘）
            artifact = self._store.get_artifact(tool_call_id)
            if artifact is None:
                return EvictToolExecuteResult(
                    status="no_artifact",
                    output="",
                    error=(
                        f"No artifact found for tool_call_id={tool_call_id!r}. "
                        "The tool result must have been spilled to disk before it can be evicted."
                    ),
                )

            replacement = _SUMMARY_TEMPLATE.format(
                summary=tool_result_summary.strip(),
                tool_call_id=tool_call_id,
            )

            # 2. 更新 _messages（UnifiedMessage 链）
            mutated_msgs = 0
            messages: list[Any] = getattr(self._ctx, "_messages", [])
            for msg in messages:
                tool_results = getattr(msg, "tool_results", None) or []
                for tr in tool_results:
                    if getattr(tr, "tool_call_id", None) == tool_call_id:
                        tr.content = replacement
                        # 清除 content_parts（如有）
                        if hasattr(tr, "content_parts"):
                            tr.content_parts = None  # type: ignore[attr-defined]
                        mutated_msgs += 1

            # 3. 同步更新 _pending_messages（写盘缓冲区）
            mutated_pending = 0
            pending: list[dict[str, Any]] = getattr(self._ctx, "_pending_messages", [])
            for pmsg in pending:
                if not isinstance(pmsg, dict):
                    continue
                tool_results_list = pmsg.get("tool_results", [])
                if not isinstance(tool_results_list, list):
                    continue
                for tr_dict in tool_results_list:
                    if isinstance(tr_dict, dict) and tr_dict.get("tool_call_id") == tool_call_id:
                        tr_dict["content"] = replacement
                        mutated_pending += 1

            if mutated_msgs == 0 and mutated_pending == 0:
                return EvictToolExecuteResult(
                    status="not_found",
                    output="",
                    error=(
                        f"tool_call_id={tool_call_id!r} not found in current message history. "
                        "It may have already been evicted."
                    ),
                )

            return EvictToolExecuteResult(
                status="ok",
                output=(
                    f"[tool_result_summarize_and_evict] Successfully evicted tool_call_id={tool_call_id!r}. "
                    f"Replaced in {mutated_msgs} message(s) and {mutated_pending} pending dict(s)."
                ),
            )

        except Exception as e:
            return EvictToolExecuteResult(
                status="failed",
                output="",
                error=str(e),
            )
