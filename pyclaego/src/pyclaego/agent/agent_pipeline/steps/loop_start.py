"""LoopStartStep — 创建本轮 loop_task_handler，重置轮次产物。

对应当前 ToolCallLoopStep.execute() 开头的循环初始化逻辑。
"""
from __future__ import annotations

from ....task_manager import TaskType
from ..pipeline import PipelineStep
from ..state import AgentState


class LoopStartStep(PipelineStep):
    """创建本轮 loop_task_handler 并注入 state。

    每轮循环开始时执行，负责：
      - 通过 root_task_handler.create_subtask() 创建 loop_task_handler
      - 调用 loop_handler.start()
      - 通过 state.with_loop_handler() 重置本轮产物（tool_calls, tool_results 等）
    """

    async def execute(self, state: AgentState) -> AgentState:
        round_idx = state.round_count
        loop_handler = await state.root_task_handler.create_subtask(
            task_type=TaskType.AGENT_LOOP,
            name=f"Agent Loop #{round_idx}",
            metadata={"round": round_idx},
        )
        await loop_handler.start()
        await loop_handler.log_info(
            f"[LoopStartStep] ===== 第 {round_idx} 轮 ====="
        )
        return state.with_loop_handler(loop_handler, round_idx)
