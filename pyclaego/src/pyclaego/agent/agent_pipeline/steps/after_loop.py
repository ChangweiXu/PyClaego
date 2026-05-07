"""AfterLoopStep — 循环结束后处理（写盘或中断清理）。

对应当前 ToolCallLoopStep 的 finally 块逻辑。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..pipeline import PipelineStep
from ..state import AgentState


class AfterLoopStep(PipelineStep):
    """循环结束后处理。

    正常结束：通知 context_handler 写盘最终回复（handle_after_loop）。
    中断/异常：丢弃本轮未写盘消息，调用 handle_interruption。
    """

    async def execute(self, state: AgentState) -> AgentState:
        if state.is_interrupted:
            # 丢弃本轮未写盘消息，避免污染历史
            try:
                await state.context_handler.handle_interruption()
            except Exception:
                pass
        else:
            # 正常结束：通知 context_handler 写盘最终回复
            agent_type = state.agent_config.get("type", "pipeline")
            final_msg: dict[str, Any] = {
                "role": "assistant",
                "content": state.final_response,
                "agent_type": agent_type,
                "timestamp": datetime.now().isoformat(),
                "type": "assistant",
            }
            try:
                await state.context_handler.handle_after_loop(
                    final_message=final_msg
                )
            except Exception as exc:
                handler = state.active_handler
                if handler:
                    try:
                        await handler.log_error(
                            f"[AfterLoopStep] handle_after_loop 失败（不影响响应）: {exc}"
                        )
                    except Exception:
                        pass

        return state
