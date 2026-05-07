"""LastCallStep — 达到 max_tool_rounds 后，发起不带工具的最终 LLM 调用。

对应当前 ToolCallLoopStep._run_last_call()。

当 state.stream_callback 不为 None 时，使用流式路径逐 chunk 转发，
并在本地聚合最终 ChatResponseV2。
"""
from __future__ import annotations

from typing import Any

from ....llm import ChatResponseV2
from ....security_executor import SecurityHandler
from ....task_manager import TaskType
from ..pipeline import PipelineStep
from ..state import AgentState


class LastCallStep(PipelineStep):
    """达到 max_tool_rounds 后，发起一次不带工具的最终 LLM 调用。

    此调用不写入循环计数，创建独立的 last_call 子任务。
    失败时返回错误说明字符串作为 final_response，不中断（已达最大轮次）。

    当 state.stream_callback 不为 None 时，使用流式路径逐 chunk 转发到回调，
    并在本地聚合最终 ChatResponseV2。
    """

    def __init__(self, llm_id: str) -> None:
        self.llm_id = llm_id
        self._security_handler = SecurityHandler.get_instance()

    async def execute(self, state: AgentState) -> AgentState:
        """执行最终 LLM 调用，自动选择流式或非流式路径。"""
        if state.stream_callback is not None:
            return await self._execute_stream(state)
        return await self._execute_non_stream(state)

    # ------------------------------------------------------------------
    # 非流式路径（原有逻辑）
    # ------------------------------------------------------------------

    async def _execute_non_stream(self, state: AgentState) -> AgentState:
        last_handler: Any | None = None
        try:
            last_handler = await state.root_task_handler.create_subtask(
                task_type=TaskType.AGENT_LOOP,
                name="Last Call",
                metadata={"round": "last_call"},
            )
            await last_handler.start()
            await last_handler.log_info("[LastCallStep] 发起最终 LLM 调用（无工具）")

            result = await self._security_handler.request_llm_call_v3(
                session_task_handler=last_handler,
                llm_id=self.llm_id,
                system=state.system_prompt,
                messages=state.messages,
                tool_list=[],
                stream=False,
            )

            if not result.get("success"):
                error_msg = result.get("error", "Unknown error")
                await last_handler.log_error(f"[LastCallStep] 失败: {error_msg}")
                await last_handler.fail(error=error_msg)
                return state.complete(f"[ERROR] 最终 LLM 调用失败: {error_msg}")

            v2_resp: ChatResponseV2 = result["v2_response"]
            final_text = v2_resp.text or "抱歉，未能生成最终回复。"
            await last_handler.log_info(
                f"[LastCallStep] 获得最终回复: `{final_text[:50]}...`"
            )
            await last_handler.complete()
            return state.complete(final_text)

        except Exception as exc:
            if last_handler:
                try:
                    await last_handler.fail(error=str(exc))
                except Exception:
                    pass
            await state.root_task_handler.log_error(
                f"[LastCallStep] 异常: {exc}"
            )
            return state.complete(f"[ERROR] 最终回复生成异常: {exc}")

    # ------------------------------------------------------------------
    # 流式路径
    # ------------------------------------------------------------------

    async def _execute_stream(self, state: AgentState) -> AgentState:
        """使用 SecurityHandler.request_llm_call_stream 逐 chunk 转发并聚合。"""
        callback = state.stream_callback
        last_handler: Any | None = None

        try:
            last_handler = await state.root_task_handler.create_subtask(
                task_type=TaskType.AGENT_LOOP,
                name="Last Call (stream)",
                metadata={"round": "last_call", "stream": True},
            )
            await last_handler.start()
            await last_handler.log_info("[LastCallStep] 发起最终流式 LLM 调用（无工具）")
        except Exception as exc:
            await state.root_task_handler.log_error(
                f"[LastCallStep] 创建 last_handler 失败: {exc}"
            )
            return state.complete(f"[ERROR] 最终回复生成异常: {exc}")

        # ── 聚合状态 ──────────────────────────────────────────────
        aggregated_text: str = ""
        stop_reason: str = "stop"

        try:
            async for chunk in self._security_handler.request_llm_call_stream(
                session_task_handler=last_handler,
                llm_id=self.llm_id,
                system=state.system_prompt,
                messages=state.messages,
                tool_list=[],
            ):
                # 转发到上层回调
                await callback(chunk)

                # 本地聚合
                if chunk.type == "text_delta" and chunk.text_delta:
                    aggregated_text += chunk.text_delta
                elif chunk.type == "fail":
                    error_msg = f"流式 LLM 失败: {chunk.error_message}"
                    if last_handler:
                        try:
                            await last_handler.fail(error=error_msg)
                        except Exception:
                            pass
                    await state.root_task_handler.log_error(
                        f"[LastCallStep] {error_msg}"
                    )
                    return state.complete(f"[ERROR] {error_msg}")
                elif chunk.type == "finish":
                    stop_reason = chunk.stop_reason or "stop"

        except Exception as exc:
            if last_handler:
                try:
                    await last_handler.fail(error=str(exc))
                except Exception:
                    pass
            await state.root_task_handler.log_error(
                f"[LastCallStep] 流式异常: {exc}"
            )
            return state.complete(f"[ERROR] 最终回复生成异常: {exc}")

        if stop_reason == "error":
            error_msg = "流式 LLM 返回 error stop_reason"
            if last_handler:
                try:
                    await last_handler.fail(error=error_msg)
                except Exception:
                    pass
            await state.root_task_handler.log_error(f"[LastCallStep] {error_msg}")
            return state.complete(f"[ERROR] {error_msg}")

        final_text = aggregated_text or "抱歉，未能生成最终回复。"
        await last_handler.log_info(
            f"[LastCallStep] 获得最终流式回复: `{final_text[:50]}...`"
        )
        await last_handler.complete()
        return state.complete(final_text)
