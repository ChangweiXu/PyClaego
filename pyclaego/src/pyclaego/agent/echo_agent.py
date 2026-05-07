"""EchoAgent - 最简单的单次 LLM 调用 Agent

收到用户消息后，完成**一次** request_llm_call_v2 调用并返回文本，
不含工具调用循环。使用 v2 LLM 调用协议。

配置项（agent config）：
    llm: LLM ID（默认 "kimi_code"）
"""

import asyncio
import traceback
from datetime import datetime
from typing import Any

from ..context.base_context import BaseContextHandlerV3
from ..llm import ChatResponseV2, StreamChunk, UnifiedMessage
from ..logging import get_running_log
from ..security_executor import SecurityHandler
from ..task_manager import SessionTaskHandlerV2, TaskType
from .base_agent import BaseAgent

_rlog = get_running_log()


class EchoAgent(BaseAgent):
    """最简单的单次 LLM 调用 Agent（V2 版）

    流程：
    1. 调用 context_handler.get_llm_context("before_llm_call", new_message=user_message)
       获取 system / messages（忽略 tool_list）
    2. 追加本轮用户消息到 messages
    3. 调用 request_llm_call_v2，只调一次，不循环
    4. 调用 context_handler.get_llm_context("after_loop", new_message=assistant_msg) 触发写盘
    5. 返回 LLM 文本响应

    注意：
    - 仅支持 BaseContextHandlerV2 类型的 context_handler
    - tool_list 始终传 None（不调用任何工具）
    """

    def __init__(self, config: dict[str, Any], session_id: str) -> None:
        super().__init__(config, session_id)
        self.security_handler = SecurityHandler.get_instance()
        # config 是完整 widget_config；self.config 已被基类绑定到 agent 切片
        self.set_llm_id(self.config.get("llm", "kimi_code"))

        _rlog.info(
            f"session_{session_id}",
            f"[EchoAgent] 已初始化 (llm_id={self.get_llm_id()})",
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: dict[str, Any],
        context_handler,
        user_id: str,
        **kwargs,
    ) -> str:
        raise NotImplementedError("请使用 process_v2 方法处理消息")

    async def process_v2(
        self,
        user_message: dict[str, Any],
        context_handler: BaseContextHandlerV3,
        session_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """处理用户消息（V3 生命周期版本）

        与 process 的差异：
        - 使用 BaseContextHandlerV3 的 handle_* 生命周期接口
        - 使用 request_llm_call_v3 + SessionTaskHandlerV2
        - 仍保持 EchoAgent 的“单次调用、无工具执行”语义
        """
        user_text: str = user_message.get("content", "") if isinstance(user_message, dict) else str(user_message)
        final_response = ""
        loop_task_handler = None
        interrupted = False
        stream_callback = kwargs.get("stream_callback")
        await session_task_handler.log_info(
            f"[EchoAgent] 开始处理消息 (user={session_task_handler.get_user_id()}) "
            f"`{user_text[:30]}...`"
        )

        try:
            # 1) 获取上下文并暂存 user 消息
            llm_context = await context_handler.handle_before_loop(user_message)
            system_prompt: str | None = llm_context.get("system")
            messages: list[UnifiedMessage] = llm_context.get("messages", [])

            await session_task_handler.log_info(
                f"[EchoAgent] 上下文就绪。消息数: {len(messages)}"
            )

            # 2) 创建单轮子任务并发起一次 LLM 调用
            loop_task_handler = await session_task_handler.create_subtask(
                task_type=TaskType.AGENT_LOOP,
                name="EchoAgent Loop #1",
                metadata={"round": 1},
            )
            await loop_task_handler.log_info("[EchoAgent] 单次 LLM 调用开始")

            llm_result = await self.security_handler.request_llm_call_v3(
                session_task_handler=loop_task_handler,
                llm_id=self.get_llm_id(),
                system=system_prompt,
                messages=messages,
                tool_list=None,
                stream=False,
                **kwargs,
            )

            if not llm_result["success"]:
                error_msg = llm_result.get("error", "Unknown error")
                await loop_task_handler.log_error(
                    f"[EchoAgent] LLM调用失败: {error_msg}"
                )
                final_response = f"[ERROR] LLM调用失败: {error_msg}"
            else:
                v2_resp: ChatResponseV2 = llm_result["v2_response"]
                text_reply: str = v2_resp.text or ""
                tool_calls = v2_resp.tool_calls

                # 3) 回写 assistant 消息到 context 内存态
                await context_handler.handle_after_llm_call(
                    text_reply=text_reply,
                    tool_calls=tool_calls,
                    reasoning=v2_resp.reasoning,
                    produced_by_provider=v2_resp.produced_by_provider,
                    produced_by_model=v2_resp.produced_by_model,
                )

                if tool_calls:
                    await loop_task_handler.log_warning(
                        f"[EchoAgent] 收到 {len(tool_calls)} 个工具调用，但 EchoAgent 不执行工具，直接返回文本"
                    )

                final_response = text_reply or ""
                await loop_task_handler.log_info(
                    f"[EchoAgent] 单次调用完成，响应长度: {len(final_response)} 字符"
                )

            if loop_task_handler:
                await loop_task_handler.complete()

        except asyncio.CancelledError:
            interrupted = True
            final_response = "⚠️ 任务被取消"
            raise

        except Exception as e:
            interrupted = True
            if stream_callback:
                try:
                    await stream_callback(StreamChunk(
                        type="fail",
                        error_code="agent_error",
                        error_message=str(e),
                    ))
                except Exception:
                    pass
            if loop_task_handler:
                await loop_task_handler.log_error(
                    f"[EchoAgent] 处理异常: {e}\n{traceback.format_exc()}"
                )
            await session_task_handler.log_error(
                f"[EchoAgent] 处理失败: {e}\n{traceback.format_exc()}"
            )
            final_response = f"[ERROR] 处理消息时出现错误：{e!s}"

        finally:
            if interrupted:
                try:
                    await context_handler.handle_interruption()
                except Exception:
                    pass
            else:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": final_response,
                    "agent_type": self.agent_type,
                    "timestamp": datetime.now().isoformat(),
                    "type": "assistant",
                }
                try:
                    await context_handler.handle_after_loop(
                        final_message=assistant_msg,
                    )
                except Exception as e:
                    await session_task_handler.log_error(
                        f"[EchoAgent] after_loop 通知失败（不影响响应）: {e}\n{traceback.format_exc()}"
                    )

        return final_response
