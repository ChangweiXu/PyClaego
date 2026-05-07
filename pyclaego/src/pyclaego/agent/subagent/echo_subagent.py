"""EchoSubAgent — 最简子 Agent 实现（V3 生命周期版）

单次 LLM 调用，无工具循环。适合作为最小可验证的子 Agent 示例。
"""

import asyncio
import traceback
from datetime import datetime
from typing import Any

from ...context import BaseSubAgentContextHandler
from ...llm import ChatResponseV2
from ...logging import get_running_log
from ...security_executor import SecurityHandler
from ...task_manager import SessionTaskHandlerV2
from .base_subagent import BaseSubAgent

_rlog = get_running_log()


class EchoSubAgent(BaseSubAgent):
    """最简子 Agent — 单次 LLM 调用，无工具循环（V3）"""

    def __init__(self, config: dict[str, Any], session_id: str, subagent_id: str, workspace_path) -> None:
        super().__init__(config, session_id, subagent_id, workspace_path)
        self.security_handler = SecurityHandler.get_instance()
        self.set_llm_id(config.get("llm", "kimi_code"))

        _rlog.info(
            f"session_{session_id}",
            f"[EchoSubAgent] 初始化完成 (subagent_id={subagent_id}, llm_id={self.get_llm_id()})",
        )

    async def process(
        self,
        user_message: dict[str, Any],
        context_handler: BaseSubAgentContextHandler,
        subagent_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """单次 LLM 调用，完成后写 RESULT.md 并返回 RESULT.md 全文"""
        if not isinstance(context_handler, BaseSubAgentContextHandler):
            raise TypeError(
                f"[EchoSubAgent] context_handler 必须是 BaseSubAgentContextHandler 实例，"
                f"实际类型: {type(context_handler).__name__}"
            )

        user_text = user_message.get("content", "") if isinstance(user_message, dict) else str(user_message)
        final_response: str = ""
        exc_caught: Exception | None = None
        interrupted = False

        await subagent_task_handler.log_info(
            f"[EchoSubAgent: {self.subagent_id}] 开始执行 "
            f"(prompt=`{user_text[:200]}...`)"
        )

        try:
            # 1) 获取上下文（同时暂存 user 消息）
            llm_context = await context_handler.handle_before_loop(user_message, max_rounds=1)
            system: str | None = llm_context.get("system")
            messages = list(llm_context.get("messages", []))

            await subagent_task_handler.log_info(
                f"[EchoSubAgent: {self.subagent_id}] 上下文就绪: messages={len(messages)}"
            )

            # 2) 单次 LLM 调用（子 Agent 专属通道）
            llm_result = await self.security_handler.request_subagent_llm_call(
                session_id=self.session_id,
                subagent_id=self.subagent_id,
                llm_id=self.get_llm_id(),
                system=system,
                messages=messages,
                task_handler=subagent_task_handler,
                tool_list=None,
                **kwargs,
            )

            if not llm_result["success"]:
                error_msg = llm_result.get("error", "Unknown error")
                await subagent_task_handler.log_error(
                    f"[EchoSubAgent: {self.subagent_id}] LLM 调用失败: {error_msg}"
                )
                final_response = f"[LLM 调用失败] {error_msg}"
            else:
                v2_resp: ChatResponseV2 = llm_result["v2_response"]
                text_reply: str = v2_resp.text or ""
                tool_calls = v2_resp.tool_calls

                # 3) 回写 assistant 到 context 生命周期
                await context_handler.handle_after_llm_call(
                    text_reply=text_reply,
                    tool_calls=tool_calls,
                    reasoning=v2_resp.reasoning,
                    produced_by_provider=v2_resp.produced_by_provider,
                    produced_by_model=v2_resp.produced_by_model,
                )

                if tool_calls:
                    await subagent_task_handler.log_warning(
                        f"[EchoSubAgent: {self.subagent_id}] 收到 {len(tool_calls)} 个工具调用，但 EchoSubAgent 不执行工具"
                    )

                final_response = text_reply or ""
                await subagent_task_handler.log_info(
                    f"[EchoSubAgent: {self.subagent_id}] 单次调用完成，回复内容: {final_response}"
                )

        except asyncio.CancelledError:
            interrupted = True
            exc_caught = asyncio.CancelledError()
            final_response = "⚠️ 任务被取消"
            raise

        except Exception as e:
            interrupted = True
            exc_caught = e
            await subagent_task_handler.log_error(
                f"[EchoSubAgent: {self.subagent_id}] process 异常: {e}\n{traceback.format_exc()}"
            )
            final_response = f"[ERROR] {e!s}"

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
                    await context_handler.handle_after_loop(final_message=assistant_msg)
                except Exception as e:
                    await subagent_task_handler.log_error(
                        f"[EchoSubAgent: {self.subagent_id}] after_loop 通知失败（不影响结果）: {e}\n{traceback.format_exc()}"
                    )

            # 保持 BaseSubAgent 约定：无论成功/失败都写 RESULT.md
            self._write_result(final_response=final_response, exc=exc_caught)

        return self._read_result()
