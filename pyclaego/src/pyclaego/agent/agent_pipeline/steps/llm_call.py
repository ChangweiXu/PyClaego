"""LLMCallStep — 调用 LLM，支持 RetryPolicy 重试与流式输出。

对应当前 ToolCallLoopStep._run_llm_call()。

当 state.stream_callback 不为 None 时，使用流式路径（request_llm_call_stream），
每收到一个 StreamChunk 即转发给回调，同时在本地聚合为 ChatResponseV2。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ....llm import ChatResponseV2, StreamChunk
from ....security_executor import SecurityHandler
from ..pipeline import PipelineStep
from ..retry import DEFAULT_RETRY_POLICY, RetryPolicy
from ..state import AgentState


class LLMCallStep(PipelineStep):
    """调用 LLM，支持 RetryPolicy 重试与流式输出。

    成功时返回含 llm_response / text_reply / tool_calls 的新 state。
    重试耗尽或不可重试错误时标记 interrupted。

    当 state.stream_callback 不为 None 时，使用 SecurityHandler.request_llm_call_stream
    逐 chunk 转发，并在本地聚合最终 ChatResponseV2。

    Args:
        llm_id:       LLM provider ID
        use_tools:    是否向 LLM 暴露工具列表
        retry_policy: LLM 调用重试策略
    """

    def __init__(
        self,
        llm_id: str,
        use_tools: bool = True,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self.llm_id = llm_id
        self.use_tools = use_tools
        self.retry_policy = retry_policy
        self._security_handler = SecurityHandler.get_instance()

    async def execute(self, state: AgentState) -> AgentState:
        """执行 LLM 调用，自动选择流式或非流式路径。"""
        if state.stream_callback is not None:
            return await self._execute_stream(state)
        return await self._execute_non_stream(state)

    # ------------------------------------------------------------------
    # 非流式路径（原有逻辑）
    # ------------------------------------------------------------------

    async def _execute_non_stream(self, state: AgentState) -> AgentState:
        tool_list = state.tool_list if self.use_tools else None
        last_error: Exception | None = None

        for attempt in range(self.retry_policy.max_retries + 1):
            if attempt > 0:
                wait = self.retry_policy.backoff_seconds(attempt)
                await state.loop_task_handler.log_warning(
                    f"[LLMCallStep] 第 {attempt} 次重试（等待 {wait:.1f}s）"
                )
                await asyncio.sleep(wait)

            try:
                result: dict[str, Any] = await self._security_handler.request_llm_call_v3(
                    session_task_handler=state.loop_task_handler,
                    llm_id=self.llm_id,
                    system=state.system_prompt,
                    messages=state.messages,
                    tool_list=tool_list,
                    stream=False,
                )
            except Exception as exc:
                last_error = exc
                if not self.retry_policy.is_retryable(exc) or attempt >= self.retry_policy.max_retries:
                    reason = f"[LLMCallStep] LLM 异常（不可重试或已耗尽）: {exc}"
                    await state.loop_task_handler.log_error(reason)
                    callback = state.stream_callback
                    if callback:
                        try:
                            await callback(StreamChunk(
                                type="fail",
                                error_code="llm_error",
                                error_message=str(exc),
                            ))
                        except Exception:
                            pass
                    return state.interrupt(reason)
                continue

            if not result.get("success"):
                error_msg = result.get("error", "Unknown LLM error")
                fake_exc = RuntimeError(error_msg)
                last_error = fake_exc
                if not self.retry_policy.is_retryable(fake_exc) or attempt >= self.retry_policy.max_retries:
                    reason = f"[LLMCallStep] LLM 调用失败: {error_msg}"
                    await state.loop_task_handler.log_error(reason)
                    callback = state.stream_callback
                    if callback:
                        try:
                            await callback(StreamChunk(
                                type="fail",
                                error_code="llm_error",
                                error_message=error_msg,
                            ))
                        except Exception:
                            pass
                    return state.interrupt(reason)
                continue

            # 成功
            v2_resp: ChatResponseV2 = result["v2_response"]
            await state.loop_task_handler.log_info(
                f"[LLMCallStep] LLM 成功: stop={v2_resp.stop_reason}, "
                f"text=`{(v2_resp.text or '')[:40]}...`, "
                f"tool_calls={len(v2_resp.tool_calls or [])}"
            )
            return state.with_llm_result(
                llm_response=v2_resp,
                text_reply=v2_resp.text or "",
                tool_calls=v2_resp.tool_calls or [],
            )

        reason = f"[LLMCallStep] 重试耗尽: {last_error}"
        await state.loop_task_handler.log_error(reason)
        callback = state.stream_callback
        if callback:
            try:
                await callback(StreamChunk(
                    type="fail",
                    error_code="llm_error",
                    error_message=str(last_error),
                ))
            except Exception:
                pass
        return state.interrupt(reason)

    # ------------------------------------------------------------------
    # 流式路径
    # ------------------------------------------------------------------

    async def _execute_stream(self, state: AgentState) -> AgentState:
        """使用 SecurityHandler.request_llm_call_stream 逐 chunk 转发并聚合。"""
        tool_list = state.tool_list if self.use_tools else None
        callback = state.stream_callback

        # ── 聚合状态 ──────────────────────────────────────────────
        aggregated_text: str = ""
        aggregated_thinking: str = ""
        parsed_tool_calls: list = []
        stop_reason: str = "stop"
        usage: dict[str, int] = {}
        reasoning = None
        producer: str | None = None
        model_name: str | None = None

        try:
            async for chunk in self._security_handler.request_llm_call_stream(
                session_task_handler=state.loop_task_handler,
                llm_id=self.llm_id,
                system=state.system_prompt,
                messages=state.messages,
                tool_list=tool_list,
            ):
                # 转发到上层回调
                await callback(chunk)

                # 本地聚合
                if chunk.type == "text_delta" and chunk.text_delta:
                    aggregated_text += chunk.text_delta
                elif chunk.type == "thinking_delta" and chunk.thinking_delta:
                    aggregated_thinking += chunk.thinking_delta
                elif chunk.type == "tool_call_end" and chunk.tool_call:
                    parsed_tool_calls.append(chunk.tool_call)
                elif chunk.type == "fail":
                    reason = f"[LLMCallStep] 流式 LLM 失败: {chunk.error_message}"
                    await state.loop_task_handler.log_error(reason)
                    return state.interrupt(reason)
                elif chunk.type == "finish":
                    stop_reason = chunk.stop_reason or "stop"
                    usage = chunk.usage or {}
                    reasoning = chunk.reasoning
                    producer = chunk.produced_by_provider
                    model_name = chunk.produced_by_model

        except Exception as exc:
            reason = f"[LLMCallStep] 流式 LLM 异常: {exc}"
            await state.loop_task_handler.log_error(reason)
            if callback:
                try:
                    await callback(StreamChunk(
                        type="fail",
                        error_code="llm_error",
                        error_message=str(exc),
                    ))
                except Exception:
                    pass
            return state.interrupt(reason)

        if stop_reason == "error":
            reason = "[LLMCallStep] 流式 LLM 返回 error stop_reason"
            await state.loop_task_handler.log_error(reason)
            if callback:
                try:
                    await callback(StreamChunk(
                        type="fail",
                        error_code="llm_error",
                        error_message="LLM returned error stop_reason",
                    ))
                except Exception:
                    pass
            return state.interrupt(reason)

        # ── 构建 ChatResponseV2 ──────────────────────────────────
        v2_resp = ChatResponseV2(
            text=aggregated_text or None,
            tool_calls=parsed_tool_calls or None,
            stop_reason=stop_reason,
            usage=usage,
            raw_response=None,  # 流式无原始响应对象
            reasoning=reasoning,
            produced_by_provider=producer,
            produced_by_model=model_name,
        )

        await state.loop_task_handler.log_info(
            f"[LLMCallStep] 流式 LLM 成功: stop={stop_reason}, "
            f"text=`{aggregated_text[:40]}...`, "
            f"tool_calls={len(parsed_tool_calls)}"
        )
        return state.with_llm_result(
            llm_response=v2_resp,
            text_reply=aggregated_text,
            tool_calls=parsed_tool_calls,
        )
