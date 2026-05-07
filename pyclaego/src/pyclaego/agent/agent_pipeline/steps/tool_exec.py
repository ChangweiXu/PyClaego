"""ToolExecStep — 顺序执行普通工具调用。

对应当前 ToolCallLoopStep._run_tool_exec()。
"""
from __future__ import annotations

import json
import traceback

from ....llm import ToolCall, ToolCallResult
from ....security_executor import SecurityHandler
from ....task_manager import TaskType
from ..pipeline import PipelineStep
from ..state import AgentState


class ToolExecStep(PipelineStep):
    """顺序执行普通工具调用（使用 security_handler.request_tool_call_v2）。

    每个工具调用结果均转换为 ToolCallResult 追加到 state.tool_results。
    单个工具失败不影响其他工具执行；失败结果以 [ERROR] 前缀写入 content。
    """

    def __init__(self) -> None:
        self._security_handler = SecurityHandler.get_instance()

    async def execute(self, state: AgentState) -> AgentState:
        tool_calls: list[ToolCall] = state._non_memory_calls
        if not tool_calls:
            return state

        tool_handler = None
        try:
            tool_handler = await state.loop_task_handler.create_subtask(
                task_type=TaskType.TOOL_EXECUTION,
                name=f"Tool Execution ({len(tool_calls)} tools)",
                metadata={"tool_count": len(tool_calls),
                          "tool_names": [tc.name for tc in tool_calls]},
            )
            await tool_handler.start()
        except Exception as exc:
            await state.loop_task_handler.log_warning(
                f"[ToolExecStep] 创建 tool_handler 失败: {exc}，工具调用将被跳过"
            )
            empty_results = [
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=f"[ERROR] tool_handler 创建失败: {exc}",
                )
                for tc in tool_calls
            ]
            return state.with_tool_results(state.tool_results + empty_results)

        results: list[ToolCallResult] = []

        for tc in tool_calls:
            args_preview = str(tc.arguments)[:200]
            await tool_handler.log_info(
                f"[TOOL] {tc.name} | id={tc.id} | args={args_preview!r}"
            )

            try:
                exec_result = await self._security_handler.request_tool_call_v2(
                    loop_task_handler=tool_handler,
                    tool_name=tc.name,
                    tool_args=tc.arguments or {},
                )

                if exec_result.get("success"):
                    output = exec_result.get("output") or ""
                    content = (
                        output
                        if isinstance(output, str)
                        else json.dumps(output, ensure_ascii=False)
                    )
                    results.append(
                        ToolCallResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=content,
                            content_parts=exec_result.get("content_parts"),
                        )
                    )
                    await tool_handler.log_info(
                        f"[TOOL] {tc.name} 成功: `{content[:80]}...`"
                    )
                else:
                    error_msg = exec_result.get("error", "工具执行失败")
                    results.append(
                        ToolCallResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=f"[ERROR] {error_msg}",
                        )
                    )
                    await tool_handler.log_warning(
                        f"[TOOL] {tc.name} 失败: {error_msg}"
                    )

            except Exception as exc:
                err_str = f"[ERROR] 工具调用异常: {exc}"
                results.append(
                    ToolCallResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        content=err_str,
                    )
                )
                await tool_handler.log_error(
                    f"[TOOL] {tc.name} 异常: {exc}\n{traceback.format_exc()}"
                )

        try:
            await tool_handler.complete()
        except Exception:
            pass

        # 合并已有结果与新结果
        all_results = list(state.tool_results) + results
        return state.with_tool_results(all_results)
