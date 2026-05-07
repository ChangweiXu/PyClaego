"""BeforeLLMCallStep — 调用 context_handler.handle_before_llm_call。

对应当前 ToolCallLoopStep._run_before_llm_call()。
"""
from __future__ import annotations

from ..pipeline import PipelineStep
from ..state import AgentState


class BeforeLLMCallStep(PipelineStep):
    """调用 context_handler.handle_before_llm_call（驱逐/压缩等 hook）。"""

    async def execute(self, state: AgentState) -> AgentState:
        try:
            updated_messages = await state.context_handler.handle_before_llm_call(
                state.messages,
                loop_task_handler=state.loop_task_handler,
            )
            return state.with_messages(updated_messages)
        except Exception as exc:
            reason = f"[BeforeLLMCallStep] {exc}"
            await state.loop_task_handler.log_error(reason)
            return state.interrupt(reason)
