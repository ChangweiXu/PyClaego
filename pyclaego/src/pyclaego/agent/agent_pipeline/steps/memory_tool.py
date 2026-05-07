"""MemoryToolStep — 调用 context_handler.handle_memory_tool_calls，分离记忆工具结果。

对应当前 ToolCallLoopStep._run_memory_tools()。
"""
from __future__ import annotations

from ....llm import ToolCall, ToolCallResult
from ..pipeline import PipelineStep
from ..state import AgentState


class MemoryToolStep(PipelineStep):
    """调用 context_handler.handle_memory_tool_calls，分离记忆工具结果。

    将 tool_calls 分为记忆工具和非记忆工具，分别处理。
    记忆工具结果存入 state，非记忆工具保留供后续 ToolExecStep 执行。

    Returns:
        新 state，其中 tool_results 包含记忆工具结果，
        非记忆工具调用通过 state._non_memory_calls 传递（供 ToolExecStep 使用）。
    """

    async def execute(self, state: AgentState) -> AgentState:
        try:
            mem_status = await state.context_handler.handle_memory_tool_calls(
                tool_calls=state.tool_calls,
                loop_task_handler=state.loop_task_handler,
            )
        except Exception as exc:
            reason = f"[MemoryToolStep] {exc}"
            await state.loop_task_handler.log_error(reason)
            return state.interrupt(reason)

        if not mem_status:
            # handler 未返回任何内容，按"全部作为普通工具"处理
            await state.loop_task_handler.log_warning(
                "[MemoryToolStep] handle_memory_tool_calls 返回空，所有工具视为普通工具"
            )
            # 保存非记忆工具供后续步骤使用
            return state.with_non_memory_calls(list(state.tool_calls))

        non_memory_calls: list[ToolCall] = mem_status.get("non_memory_calls", [])
        memory_results: list[ToolCallResult] = mem_status.get("memory_tool_results", [])
        return state.with_tool_results(memory_results).with_non_memory_calls(non_memory_calls)
