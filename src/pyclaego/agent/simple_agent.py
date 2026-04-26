"""SimpleAgent - 基于 request_llm_call_v2 的单轮/多轮 LLM 调用策略

当 context_handler 为 BaseContextHandlerV2 实例时（如 SimpleContextHandler），
直接解包其返回的 dict（system / messages / tool_list），调用 request_llm_call_v2，
并支持原生工具调用检测与执行循环。

对旧版 BaseContextHandler 保持向后兼容：
旧版返回 List[Dict]，自动转换为 UnifiedMessage 后走相同流程。

历史消息写盘（context 侧）触发时机：
  before_loop      → _unpack_context 时传入 user_message dict（暂存）
  before_llm_call  ← （同 before_loop，适配不同调用时机）
  after_llm_call   ← （跳过，传入 assistant_msg dict 功能移交给 after_tool_call）
  after_tool_call  ← 每轮工具执行后传入 [assistant_tool_call, user_tool_result]（暂存）
  after_loop       ← process 结束前传入 assistant_final dict（暂存 + 批量写盘）
"""

import asyncio
import json
import traceback
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Callable

from .base_agent import BaseAgent
from ..security_executor import SecurityHandler
from ..context import (
    ContextCheckPoint,
    BaseContextHandler,
    BaseContextHandlerV2,
    BaseContextHandlerV3,
)
from ..context.system_prompts.simple_v2 import LAST_CALL_PROMPT
from ..llm import UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition, ChatResponseV2
from ..task_manager import SessionTaskHandler, SessionTaskHandlerV2, TaskType
from ..logging import get_running_log

_rlog = get_running_log()


class SimpleAgent(BaseAgent):
    """简单 Agent 策略（V2 版，基于 request_llm_call_v2）

    流程：
    1. 从 context_handler.get_llm_context(before_llm_call) 获取历史上下文，同时暂存 user 消息
    2. 追加用户消息到本轮 messages（用于传给 LLM）
    3. 调用 request_llm_call_v2
    4. 若 LLM 请求工具调用 → 执行工具 → 暂存工具轮消息 → 回传结果 → 重新调用 LLM
    5. 循环直至无工具调用或达到 max_tool_rounds
    6. 调用 context_handler.get_llm_context(after_loop) 触发批量写盘

    配置项（agent config 下）：
        llm:            LLM ID（默认 "kimi_code"）
        max_tool_rounds: 最大工具调用轮次（默认 5）
        use_tools:      是否启用工具调用（默认 True）
    """

    def __init__(self, config: Dict[str, Any], session_id: str):
        super().__init__(config, session_id)

        self.security_handler = SecurityHandler.get_instance()

        agent_config = self.config
        self.set_llm_id(agent_config.get("llm", "kimi_code"))
        self.use_tools: bool = agent_config.get("use_tools", True)
        agent_type = config.get("type", "spawn")
        strategy_config = agent_config.get(agent_type, {})
        self.max_tool_rounds: int = strategy_config.get("max_tool_rounds", 5)

        _rlog.info(
            f"session_{session_id}",
            f"[SimpleAgent] 已初始化 (llm_id={self.get_llm_id()}, "
            f"max_tool_rounds={self.max_tool_rounds}, use_tools={self.use_tools})",
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseContextHandler,
        user_id: str,
        **kwargs,
    ) -> str:
        raise NotImplementedError("请使用 process_v2 方法处理消息")

    async def process_v2(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseContextHandlerV3,
        session_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        user_text: str = user_message['content']
        final_response = ""
        interrupted = False

        await session_task_handler.log_info(
            f"[SimpleAgent] 开始处理消息 (user={session_task_handler.get_user_id()}) "
            f"`{user_text[:30]}...`"
        )

        try:
            # ── 1 获取初始 LLM 上下文（含历史消息、系统提示词、工具列表） ────
            llm_context = await context_handler.handle_before_loop(
                user_message,
            )

            # 解包 llm_context
            system_prompt: Optional[str] = llm_context.get("system")
            messages: List[UnifiedMessage] = llm_context.get("messages", [])
            tool_list: List[ToolDefinition] = llm_context.get("tool_list", [])

            await session_task_handler.log_info(
                f"[SimpleAgent] 上下文就绪。"
                f"消息数: {len(messages)}，"
                f"工具数: {len(tool_list)}",
            )

            # ── 2. 工具调用循环 ────
            round_count = 0
            loop_task_handler = None

            while round_count <= self.max_tool_rounds:
                round_count += 1
                loop_task_handler = await session_task_handler.create_subtask(
                    task_type=TaskType.AGENT_LOOP,
                    name=f"Agent Loop #{round_count}",
                    metadata={"round": round_count},
                )
                await loop_task_handler.log_info(
                    f"[SimpleAgent] === 第 {round_count} 轮工具循环 ==="
                )

                # 2.1 LLM 调用 (v3 接口)
                # V6 hook: 调用前允许 handler 改写 messages（例如驱逐过时工具结果）
                messages = await context_handler.handle_before_llm_call(
                    messages,
                    loop_task_handler=loop_task_handler,
                )
                llm_result: Dict[str, Any] = await self.security_handler.request_llm_call_v3(
                    session_task_handler=loop_task_handler,
                    llm_id=self.get_llm_id(),
                    system=system_prompt,
                    messages=messages,
                    tool_list=tool_list,
                    stream=False,
                    **kwargs,
                )

                # 2.2 提取 LLM 回复内容和工具调用
                if not llm_result["success"]:
                    interrupted = True
                    error_msg = llm_result.get("error", "Unknown error")
                    await loop_task_handler.log_error(
                        f"[SimpleAgent] [round {round_count}] LLM调用失败: {error_msg}"
                    )
                    final_response = f"[ERROR] LLM调用失败: {error_msg}"
                    break

                v2_resp: ChatResponseV2 = llm_result["v2_response"]
                stop_reason = v2_resp.stop_reason
                text_reply: Optional[str] = v2_resp.text
                tool_calls: Optional[List[ToolCall]] = v2_resp.tool_calls

                # 2.3 记录 LLM 回复，接收更新后的 messages
                messages = await context_handler.handle_after_llm_call(
                    text_reply=text_reply,
                    tool_calls=tool_calls,
                    reasoning=v2_resp.reasoning,
                    produced_by_provider=v2_resp.produced_by_provider,
                    produced_by_model=v2_resp.produced_by_model,
                )
                await loop_task_handler.log_info(
                    f"[SimpleAgent] [round {round_count}] LLM 回复: "
                    f"stop_reason={stop_reason}, "
                    f"text=`{(text_reply or '')[:30]}...`, "
                    f"tool_calls={len(tool_calls) if tool_calls else 0}"
                )

                # ── 3. 工具调用 ────
                # 3.1 检查工具调用
                if not tool_calls:
                    # 无工具调用，直接结束
                    final_response = text_reply or ""
                    await loop_task_handler.log_info(
                        f"[SimpleAgent] [round {round_count}] 无工具调用，结束循环"
                    )
                    await loop_task_handler.complete()
                    break
                
                
                await loop_task_handler.log_info(
                    f"[SimpleAgent] [round {round_count}] 开始执行 {len(tool_calls)} 个工具调用:\n"
                    + "\n".join([f'  - {t.name}({t.arguments})' for t in tool_calls])
                )

                # 3.2 执行 memory 工具调用
                memory_exec_status = await context_handler.handle_memory_tool_calls(
                    tool_calls=tool_calls,
                    loop_task_handler=loop_task_handler,
                )

                # 3.3 继续普通工具调用
                if not memory_exec_status:
                    # 极端情况：context handler 未返回任何内容，按无记忆工具调用处理
                    await loop_task_handler.log_warning(
                        f"[SimpleAgent] [round {round_count}] context_handler.handle_memory_tool_calls 返回空内容",
                    )
                    tool_results = await self._execute_tool_calls_v2(
                        tool_calls, round_count, loop_task_handler=loop_task_handler
                    )
                else:
                    await loop_task_handler.log_info(
                        f"[SimpleAgent] [round {round_count}] 继续执行其他工具",
                    )
                    non_memory_calls = memory_exec_status.get("non_memory_calls", [])
                    tool_results = await self._execute_tool_calls_v2(
                        non_memory_calls, round_count, loop_task_handler=loop_task_handler
                    )
                    tool_results.extend(memory_exec_status.get("memory_tool_results", []))

                # 3.4 将工具结果传给 context_handler，接收更新后的 messages
                is_last_round = (round_count >= self.max_tool_rounds)
                messages = await context_handler.handle_after_tool_calls(
                    tool_results=tool_results,
                    last_call_prompt=LAST_CALL_PROMPT if is_last_round else None,
                )

                # 3.6 回调更新进度
                await loop_task_handler.complete()

                # 3.7 检查是否达到最大轮次
                if is_last_round:
                    await session_task_handler.log_warning(
                        f"[SimpleAgent] 达到最大工具调用轮次 ({self.max_tool_rounds})，发起最终 LLM 调用",
                    )
                    final_response = await self._last_call(
                        system_prompt=system_prompt,
                        messages=messages,
                        session_task_handler=session_task_handler,
                    )
                    break

            # ── 4. 收尾 ────
            await session_task_handler.log_info(
                f"[SimpleAgent] 处理完成，共 {round_count} 轮, "
                f"最终回复: `{final_response[:30]}...`"
            )
            final_response = text_reply or "抱歉，未能生成回复。"

        except asyncio.CancelledError:
            interrupted = True
            final_response = "⚠️ 任务被取消"
            raise

        except Exception as e:
            interrupted = True
            await session_task_handler.log_error(
                f"[SimpleAgent] 处理失败: {e}\n{traceback.format_exc()}",
            )
            final_response = f"[ERROR] 处理消息时出现错误：{str(e)}"

        finally:
            if interrupted:
                # ── 5a. 中断：丢弃本轮未写盘消息，不污染历史 ────
                try:
                    await context_handler.handle_interruption()
                except Exception:
                    pass
            else:
                # ── 5b. 正常结束：通知 context handler 写盘最终 assistant 回复 ────
                assistant_msg: Dict[str, Any] = {
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
                        f"[SimpleAgent] after_loop 通知失败（不影响响应）: {e}\n{traceback.format_exc()}"
                    )

        return final_response

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    async def _unpack_context(
        self,
        context_handler: BaseContextHandler,
        new_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], List[UnifiedMessage], Optional[List[ToolDefinition]]]:
        raise NotImplementedError("SimpleAgent._unpack_context 已废弃")

    def _build_tool_round_dicts(
        self,
        text_reply: Optional[str],
        tool_calls: List[ToolCall],
        tool_results: List[ToolCallResult],
    ) -> List[Dict[str, Any]]:
        """构建工具调用轮的历史消息 dict 列表（用于传给 context handler 暂存）。

        返回两条消息：
          1. assistant 消息（含 tool_calls 字段）
          2. user 消息（含 tool_results 字段）

        Args:
            text_reply:   LLM 在发起工具调用时输出的文本（可能为 None）
            tool_calls:   LLM 请求的工具调用列表
            tool_results: 工具执行结果列表

        Returns:
            [assistant_dict, user_dict]
        """
        now = datetime.now().isoformat()
        assistant_dict: Dict[str, Any] = {
            "role": "assistant",
            "content": text_reply or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ],
            "type": "tool_call",
            "timestamp": now,
        }
        user_dict: Dict[str, Any] = {
            "role": "user",
            "content": "",
            "tool_results": [
                {
                    "tool_call_id": tr.tool_call_id,
                    "tool_name": tr.tool_name,
                    "content": tr.content,
                }
                for tr in tool_results
            ],
            "type": "tool_result",
            "timestamp": now,
        }
        return [assistant_dict, user_dict]

    async def _execute_tool_calls(
        self,
        tool_calls: List[ToolCall],
        round_index: int,
        loop_handler: Optional[SessionTaskHandler] = None,
    ) -> List[ToolCallResult]:
        raise NotImplementedError("请使用 _execute_tool_calls_v2 方法执行工具调用")

    async def _last_call(
        self,
        system_prompt: Optional[str],
        messages: List[UnifiedMessage],
        session_task_handler: SessionTaskHandlerV2,
    ) -> str:
        """达到最大工具调用轮次后，发起一次不带工具的最终 LLM 调用，获取综合性答案。

        Args:
            system_prompt: 系统提示词
            messages:      当前完整上下文（含 last_call_prompt 的工具结果消息）
            session_task_handler: 用于创建子任务和记录日志

        Returns:
            LLM 最终文本回复，失败时返回错误说明字符串
        """
        last_call_handler = None
        try:
            last_call_handler = await session_task_handler.create_subtask(
                task_type=TaskType.AGENT_LOOP,
                name="Last Call",
                metadata={"round": "last_call"},
            )
            await last_call_handler.log_info("[SimpleAgent] [last_call] 发起最终 LLM 调用（无工具）")

            llm_result: Dict[str, Any] = await self.security_handler.request_llm_call_v3(
                session_task_handler=last_call_handler,
                llm_id=self.get_llm_id(),
                system=system_prompt,
                messages=messages,
                tool_list=[],
                stream=False,
            )

            if not llm_result["success"]:
                error_msg = llm_result.get("error", "Unknown error")
                await last_call_handler.log_error(
                    f"[SimpleAgent] [last_call] LLM 调用失败: {error_msg}"
                )
                return f"[ERROR] 最终 LLM 调用失败: {error_msg}"

            v2_resp: ChatResponseV2 = llm_result["v2_response"]
            final_text = v2_resp.text or "抱歉，未能生成最终回复。"
            await last_call_handler.log_info(
                f"[SimpleAgent] [last_call] 获得最终回复: `{final_text[:50]}...`"
            )
            await last_call_handler.complete()
            return final_text

        except Exception as e:
            if last_call_handler:
                await last_call_handler.log_error(
                    f"[SimpleAgent] [last_call] 异常: {e}\n{traceback.format_exc()}"
                )
            return f"[ERROR] 最终回复生成异常: {str(e)}"

    async def _execute_tool_calls_v2(
        self,
        tool_calls: List[ToolCall],
        round_index: int,
        loop_task_handler: SessionTaskHandlerV2,
    ) -> List[ToolCallResult]:
        """顺序执行一批工具调用，返回统一格式的结果列表（V2 版本）。

        Args:
            tool_calls: LLM 返回的工具调用列表
            round_index: 当前轮次（仅用于日志）
            loop_task_handler: SessionTaskHandlerV2 进度通知

        Returns:
            List[ToolCallResult]
        """
        if not loop_task_handler:
            raise ValueError("SimpleAgent._execute_tool_calls_v2 需要有效的 loop_task_handler 参数")
        if not tool_calls:
            return []

        # 创建工具执行批次子任务
        tool_handler = None
        try:
            tool_handler = await loop_task_handler.create_subtask(
                task_type=TaskType.TOOL_EXECUTION,
                name=f"Tool Execution ({len(tool_calls)} tools)",
                description=f"执行 {len(tool_calls)} 个工具调用",
                metadata={
                    "tool_count": len(tool_calls),
                    "tool_names": [tc.name for tc in tool_calls],
                },
            )
            await tool_handler.start()
        except Exception as e:
            await loop_task_handler.log_warning(
                f"[SimpleAgent] 创建工具执行子任务失败: {e}"
            )
        if not tool_handler:
            raise RuntimeError("无法创建工具执行子任务，无法继续执行工具调用")

        tool_results: List[ToolCallResult] = []

        for tc in tool_calls:
            args_preview = str(tc.arguments)[:200]
            await tool_handler.log_info(
                f"[TOOL] name={tc.name} | id={tc.id} | args=\"{args_preview}\""
            )

            try:
                # 走 V2 包装：内部自动创建 TOOL_EXECUTION 子任务，
                # 并把 tool_args / tool_result / file_edit 工件挂到该子任务上。
                result = await self.security_handler.request_tool_call_v2(
                    loop_task_handler=tool_handler,
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                )

                content_parts = None
                if result.get("success"):
                    output = result.get("output") or ""
                    content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                    # 提取多模态内容块（图片、文档等）
                    content_parts = result.get("content_parts")
                    result_preview = content[:200]
                    await tool_handler.log_info(
                        f"[TOOL] name={tc.name} | success=true | output_len={len(content)} | result=\"{result_preview}\""
                    )
                else:
                    error = result.get("error", "Unknown error")
                    content = f"[ERROR] {error}"
                    await tool_handler.log_error(
                        f"[SimpleAgent] 工具执行失败: {tc.name}, error={error}"
                    )

            except Exception as e:
                content = f"[ERROR] 工具调用异常: {str(e)}"
                content_parts = None
                await tool_handler.log_error(
                    f"[SimpleAgent] 工具调用异常: {tc.name}\n{traceback.format_exc()}"
                )

            tool_results.append(
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=content,
                    content_parts=content_parts,
                )
            )

        # 标记工具执行批次完成
        try:
            success_count = sum(
                1 for tr in tool_results
                if not tr.content.startswith("[ERROR]")
            )
            await tool_handler.complete(
                result={
                    "success_count": success_count,
                    "total_count": len(tool_results),
                }
            )
        except Exception:
            pass

        return tool_results

    def get_info(self) -> Dict[str, Any]:
        """获取 Agent 信息"""
        info = super().get_info()
        info.update({
            "llm_id": self.get_llm_id(),
            "session_id": self.session_id,
            "max_tool_rounds": self.max_tool_rounds,
            "use_tools": self.use_tools,
        })
        return info
