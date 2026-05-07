"""LoopEndStep — 完成本轮 loop_task_handler。

对应当前 ToolCallLoopStep 中 loop_handler.complete() 调用。
"""
from __future__ import annotations

from ..pipeline import PipelineStep
from ..state import AgentState


class LoopEndStep(PipelineStep):
    """完成本轮 loop_task_handler。

    如果 state 未被中断，调用 loop_task_handler.complete()。
    如果已中断，调用 loop_task_handler.fail()。
    """

    async def execute(self, state: AgentState) -> AgentState:
        if state.loop_task_handler is None:
            return state

        try:
            if state.is_interrupted:
                await state.loop_task_handler.fail(error=state.interrupt_reason)
            else:
                await state.loop_task_handler.complete()
        except Exception:
            pass  # TaskHandler 完成/失败不应中断流程

        return state
