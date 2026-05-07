"""AfterToolCallStep — 调用 context_handler.handle_after_tool_calls，暂存工具结果消息。

对应当前 ToolCallLoopStep._run_after_tool_calls()。
"""
from __future__ import annotations

from ..pipeline import PipelineStep
from ..state import AgentState


class AfterToolCallStep(PipelineStep):
    """调用 context_handler.handle_after_tool_calls，暂存工具结果消息。

    如果是最后一轮，会附加 last_call_prompt 提示 LLM 给出最终回复。
    """

    def __init__(self, last_call_prompt: str | None = None) -> None:
        self.last_call_prompt = last_call_prompt

    async def execute(self, state: AgentState) -> AgentState:
        try:
            updated_messages = await state.context_handler.handle_after_tool_calls(
                tool_results=state.tool_results,
                last_call_prompt=self.last_call_prompt,
                loop_task_handler=state.loop_task_handler,
            )
            return state.with_messages(updated_messages)
        except Exception as exc:
            reason = f"[AfterToolCallStep] {exc}"
            await state.loop_task_handler.log_error(reason)
            return state.interrupt(reason)
