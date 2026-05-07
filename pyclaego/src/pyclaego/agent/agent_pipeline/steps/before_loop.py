"""BeforeLoopStep — 调用 context_handler.handle_before_loop，初始化 LLM 上下文。

在整个 process_v2 周期内只执行一次（Pipeline 的第一个 Step）。
"""
from __future__ import annotations

from typing import Any

from ..pipeline import PipelineStep
from ..state import AgentState


class BeforeLoopStep(PipelineStep):
    """调用 context_handler.handle_before_loop，将 system/messages/tool_list 写入 state。"""

    async def execute(self, state: AgentState) -> AgentState:
        user_text = state.input_message.get("content", "")[:30]
        await state.root_task_handler.log_info(
            f"[BeforeLoopStep] 开始处理消息 "
            f"(user={state.root_task_handler.get_user_id()}) `{user_text}...`"
        )

        llm_context: dict[str, Any] = await state.context_handler.handle_before_loop(
            state.input_message,
            max_rounds=state.max_tool_rounds,
        )

        state = state.with_context(
            system_prompt=llm_context.get("system"),
            messages=llm_context.get("messages") or [],
            tool_list=llm_context.get("tool_list") or [],
        )

        await state.root_task_handler.log_info(
            f"[BeforeLoopStep] 上下文就绪: "
            f"{len(state.messages)} msgs, {len(state.tool_list)} tools"
        )
        return state
