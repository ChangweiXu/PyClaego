"""AfterLLMCallStep — 调用 context_handler.handle_after_llm_call，暂存 assistant 消息。

对应当前 ToolCallLoopStep._run_after_llm_call()。
"""
from __future__ import annotations

from ..pipeline import PipelineStep
from ..state import AgentState


class AfterLLMCallStep(PipelineStep):
    """调用 context_handler.handle_after_llm_call，暂存 assistant 消息。"""

    async def execute(self, state: AgentState) -> AgentState:
        v2_resp = state.llm_response
        try:
            updated_messages = await state.context_handler.handle_after_llm_call(
                text_reply=state.text_reply,
                tool_calls=state.tool_calls or None,
                reasoning=v2_resp.reasoning if v2_resp else None,
                produced_by_provider=v2_resp.produced_by_provider if v2_resp else None,
                produced_by_model=v2_resp.produced_by_model if v2_resp else None,
            )
            return state.with_messages(updated_messages)
        except Exception as exc:
            reason = f"[AfterLLMCallStep] {exc}"
            await state.loop_task_handler.log_error(reason)
            return state.interrupt(reason)
