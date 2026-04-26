"""CodeExplorerSubAgent — 代码探索子 Agent（Agent-Tool-Loop 版）

只读探索代码库，运行完整的 agent-tool-loop：
- 允许工具集：glob / list_directory / read_file / search_text /
              find_line / file_info / write_file / mkdir
- 只读工具用于浏览 project_root 下的代码
- write_file / mkdir 仅限 workspace 目录内（保存 RESULT.md 和笔记）
- 不允许：bash、网络工具、文件编辑、文件删除等

配置（来自主 Agent config，按需覆盖）：
  llm:             LLM provider ID（继承主 Agent 配置）
  max_tool_rounds: 最大工具循环轮次（默认 15）
"""

import asyncio
import traceback
from datetime import datetime
import json
from typing import Dict, Any, List, Optional

from .base_subagent import BaseSubAgent
from ...security_executor import SecurityHandler
from ...context import BaseSubAgentContextHandler
from ...task_manager import SessionTaskHandlerV2, TaskType
from ...llm import UnifiedMessage, ToolCall, ToolCallResult, ChatResponseV2
from ...logging import get_running_log

_rlog = get_running_log()


class CodeExplorerSubAgent(BaseSubAgent):
    """代码探索子 Agent — 完整 agent-tool-loop，只读工具集"""

    def __init__(
        self,
        config: Dict[str, Any],
        session_id: str,
        subagent_id: str,
        workspace_path,
    ) -> None:
        super().__init__(config, session_id, subagent_id, workspace_path)
        self.security_handler = SecurityHandler.get_instance()
        self.set_llm_id(config.get("llm", "kimi_code"))
        subagent_cfg = config.get("subagents", {}).get("code_explorer", {})
        self.max_tool_rounds: int = int(subagent_cfg.get("max_tool_rounds", 15))

        _rlog.info(
            f"session_{session_id}",
            f"[CodeExplorerSubAgent: {subagent_id}] 初始化完成 "
            f"(llm_id={self.get_llm_id()}, max_tool_rounds={self.max_tool_rounds})",
        )

    # ------------------------------------------------------------------
    # 核心：agent-tool-loop
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseSubAgentContextHandler,
        subagent_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """执行 agent-tool-loop，写 RESULT.md，返回其全文。"""
        user_text = (
            user_message.get("content", "")
            if isinstance(user_message, dict)
            else str(user_message)
        )
        final_response: str = ""
        exc_caught: Optional[BaseException] = None
        cancelled = False

        await subagent_task_handler.log_info(
            f"[CodeExplorerSubAgent: {self.subagent_id}] 开始执行. "
            f"Prompt=`{user_text[:30]}...`"
        )

        try:
            # 1) 获取初始上下文
            llm_context = await context_handler.handle_before_loop(user_message)
            system: Optional[str] = llm_context.get("system")
            messages: List[UnifiedMessage] = list(llm_context.get("messages", []))
            tool_list = llm_context.get("tool_list")

            await subagent_task_handler.log_info(
                f"[CodeExplorerSubAgent: {self.subagent_id}] 上下文就绪: "
                f"messages={len(messages)}, tools={len(tool_list) if tool_list else 0}"
            )

            # 2) agent-tool-loop
            round_count = 0
            while round_count < self.max_tool_rounds:
                round_count += 1

                loop_task_handler = await subagent_task_handler.create_subtask(
                    task_type=TaskType.AGENT_LOOP,
                    name=f"CodeExplorer Loop #{round_count}",
                    metadata={"round": round_count, "agent_type": "code_explorer"},
                )

                await loop_task_handler.log_info(
                    f"[CodeExplorerSubAgent: {self.subagent_id}] === 第 {round_count} 轮工具循环 ==="
                )

                # 2.1 LLM 调用
                llm_result = await self.security_handler.request_subagent_llm_call(
                    session_id=self.session_id,
                    subagent_id=self.subagent_id,
                    llm_id=self.get_llm_id(),
                    system=system,
                    messages=messages,
                    task_handler=loop_task_handler,
                    tool_list=tool_list,
                )

                if not llm_result["success"]:
                    error_msg = llm_result.get("error", "Unknown error")
                    await loop_task_handler.log_error(
                        f"[CodeExplorerSubAgent: {self.subagent_id}] [round {round_count}] LLM 调用失败: {error_msg}"
                    )
                    final_response = f"[ERROR] LLM 调用失败: {error_msg}"
                    await loop_task_handler.complete()
                    break

                v2_resp: ChatResponseV2 = llm_result["v2_response"]
                text_reply: str = v2_resp.text or ""
                tool_calls: Optional[List[ToolCall]] = v2_resp.tool_calls

                messages = await context_handler.handle_after_llm_call(
                    text_reply=text_reply,
                    tool_calls=tool_calls,
                    reasoning=v2_resp.reasoning,
                    produced_by_provider=v2_resp.produced_by_provider,
                    produced_by_model=v2_resp.produced_by_model,
                )

                await loop_task_handler.log_info(
                    f"[CodeExplorerSubAgent: {self.subagent_id}] [round {round_count}] LLM 回复: "
                    f"stop_reason={v2_resp.stop_reason}, "
                    f"text=`{(text_reply or '')[:30]}...`, "
                    f"tool_calls={len(tool_calls) if tool_calls else 0}"
                )

                # 2.2 无工具调用 → 结束
                if not tool_calls:
                    final_response = text_reply or ""
                    await loop_task_handler.log_info(
                        f"[CodeExplorerSubAgent: {self.subagent_id}] [round {round_count}] 无工具调用，结束循环"
                    )
                    await loop_task_handler.complete()
                    self._write_loop_json(round_count, text_reply, tool_calls=None, tool_results=None)
                    break

                # 2.3 处理记忆工具（通常 pass-through，返回 None）
                memory_exec_status = await context_handler.handle_memory_tool_calls(
                    tool_calls=tool_calls,
                    loop_task_handler=loop_task_handler,
                )

                if memory_exec_status:
                    non_memory_calls: List[ToolCall] = memory_exec_status.get(
                        "non_memory_calls", tool_calls
                    )
                    memory_results: List[ToolCallResult] = memory_exec_status.get(
                        "memory_tool_results", []
                    )
                else:
                    non_memory_calls = tool_calls
                    memory_results = []

                # 2.4 执行工具调用
                tool_results: List[ToolCallResult] = await self._execute_tool_calls(
                    tool_calls=non_memory_calls,
                    round_count=round_count,
                    task_handler=loop_task_handler,
                )
                tool_results = tool_results + memory_results

                messages = await context_handler.handle_after_tool_calls(
                    tool_results=tool_results,
                )

                await loop_task_handler.complete()
                self._write_loop_json(round_count, text_reply, tool_calls, tool_results)

            else:
                # 超出最大轮次 — 发起汇总 LLM 调用
                await subagent_task_handler.log_warning(
                    f"[CodeExplorerSubAgent: {self.subagent_id}] 达到最大工具调用轮次 ({self.max_tool_rounds})，发起汇总调用"
                )
                final_response = await self._summary_call(
                    system=system,
                    messages=messages,
                    round_count=round_count,
                    subagent_task_handler=subagent_task_handler,
                )

            await subagent_task_handler.log_info(
                f"[CodeExplorerSubAgent: {self.subagent_id}] 处理完成，共 {round_count} 轮"
            )

        except asyncio.CancelledError:
            cancelled = True
            exc_caught = asyncio.CancelledError()
            final_response = "⚠️ 任务被取消"
            raise

        except Exception as e:
            exc_caught = e
            await subagent_task_handler.log_error(
                f"[CodeExplorerSubAgent: {self.subagent_id}] process 异常: {e}\n{traceback.format_exc()}"
            )
            final_response = f"[ERROR] {str(e)}"

        finally:
            if cancelled:
                try:
                    await context_handler.handle_interruption()
                except Exception:
                    pass
            else:
                assistant_msg: Dict[str, Any] = {
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
                        f"[CodeExplorerSubAgent: {self.subagent_id}] after_loop 通知失败（不影响结果）: {e}\n{traceback.format_exc()}"
                    )

            # 保持 BaseSubAgent 约定：无论成功/失败都写 RESULT.md
            self._write_result(final_response=final_response, exc=exc_caught)

        return self._read_result()

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def _execute_tool_calls(
        self,
        tool_calls: List[ToolCall],
        round_count: int,
        task_handler: SessionTaskHandlerV2,
    ) -> List[ToolCallResult]:
        """顺序执行工具调用，返回 ToolCallResult 列表。"""
        results: List[ToolCallResult] = []

        for tc in tool_calls:
            args_preview = []
            for k, v in (tc.arguments or {}).items():
                if len(str(v)) > 50:
                    args_preview.append(f"{k}={str(v)[:30]}...")
                else:
                    args_preview.append(f"{k}={str(v)}")
            args_preview = ", ".join(args_preview)
            await task_handler.log_info(
                f"[CodeExplorerSubAgent: {self.subagent_id}] [TOOL] name={tc.name} | round={round_count} | args: {args_preview}"
            )

            tool_result = await self.security_handler.request_subagent_tool_call(
                session_id=self.session_id,
                subagent_id=self.subagent_id,
                tool_name=tc.name,
                tool_args=tc.arguments or {},
                task_handler=task_handler,
            )

            if tool_result["success"]:
                content = str(tool_result.get("output") or "")
                try:
                    json_content = json.loads(content)
                    result_preview = []
                    for k, v in json_content.items():
                        if len(str(v)) > 50:
                            result_preview.append(f"{k}={str(v)[:30]}...")
                        else:
                            result_preview.append(f"{k}={str(v)}")
                    result_preview = ", ".join(result_preview)
                except Exception:
                    result_preview = content[:200]
                await task_handler.log_info(
                    f"[CodeExplorerSubAgent: {self.subagent_id}] [TOOL] name={tc.name} | success=true | output_len={len(content)} | result=\"{result_preview}\""
                )
            else:
                content = f"[ERROR] {tool_result.get('error', '工具执行失败')}"
                await task_handler.log_error(
                    f"[CodeExplorerSubAgent: {self.subagent_id}] [TOOL] name={tc.name} | success=false | error={tool_result.get('error')}"
                )

            results.append(
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=content,
                )
            )

        return results
