"""SpawnToolExecStep — 带子 Agent 分离和并发调度的工具执行 Step。

继承 ToolExecStep 以复用普通工具的顺序执行逻辑，
在此基础上增加：
  - Step B: 按 AGENT_TOOL_NAMES 分离 agent_calls / normal_calls
  - Step C: 委托父类执行 normal_calls（顺序）
  - Step D: 分批并发执行 agent_calls（asyncio.gather）
  - Step E: 合并结果
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any

from ....llm import ToolCall, ToolCallResult
from ....task_manager import TaskType
from ..state import AgentState
from .tool_exec import ToolExecStep


class SpawnToolExecStep(ToolExecStep):
    """带子 Agent 分离和并发调度的工具执行 Step。

    Args:
        spawn_tool:       已构建的 SpawnSubagentTool 实例
        max_concurrent:   子 Agent 并发上限
    """

    def __init__(self, spawn_tool: Any, max_concurrent: int = 5) -> None:
        super().__init__()
        self._spawn_tool = spawn_tool
        self._max_concurrent = max(max_concurrent, 1)

    async def execute(self, state: AgentState) -> AgentState:
        tool_calls: list[ToolCall] = state._non_memory_calls
        if not tool_calls:
            return state

        # ── Step B: 分离 agent_calls 和 normal_calls ──────────────────
        from ....context.agent_tools import AGENT_TOOL_NAMES

        agent_calls: list[ToolCall] = []
        normal_calls: list[ToolCall] = []

        for tc in tool_calls:
            if tc.name in AGENT_TOOL_NAMES:
                agent_calls.append(tc)
            else:
                normal_calls.append(tc)

        await state.loop_task_handler.log_info(
            f"[SpawnToolExecStep] normal_calls={len(normal_calls)}, "
            f"agent_calls={len(agent_calls)}",
        )

        # ── Step C: 顺序执行普通工具（委托父类） ─────────────────────
        normal_state = state
        if normal_calls:
            normal_state = state.with_non_memory_calls(normal_calls)
            normal_state = await super().execute(normal_state)
        normal_results = normal_state.tool_results if normal_calls else []

        # 如果没有 Agent 工具调用，直接返回
        if not agent_calls:
            return state.with_tool_results(normal_results)

        # ── 创建 tool_handler ────────────────────────────────────────
        tool_handler = None
        try:
            tool_handler = await state.loop_task_handler.create_subtask(
                task_type=TaskType.TOOL_EXECUTION,
                name=f"Spawn Tool Execution ({len(agent_calls)} agents)",
                metadata={
                    "agent_count": len(agent_calls),
                    "max_concurrent": self._max_concurrent,
                },
            )
            await tool_handler.start()
        except Exception as exc:
            await state.loop_task_handler.log_warning(
                f"[SpawnToolExecStep] 创建 tool_handler 失败: {exc}"
            )
            error_results = [
                ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=f"[ERROR] tool_handler 创建失败: {exc}",
                )
                for tc in agent_calls
            ]
            return state.with_tool_results(normal_results + error_results)

        # ── 获取父 Agent 上下文快照（供 inherit 模式使用） ──────────
        parent_snapshot: dict[str, Any] = {}
        try:
            parent_snapshot = await state.context_handler.handle_spawn_context_snapshot()
        except Exception as snap_err:
            await tool_handler.log_warning(
                f"[SpawnToolExecStep] 获取 spawn_context_init 快照失败: {snap_err}\n{traceback.format_exc()}",
            )

        # ── Step D: 分批并发执行 agent_calls ─────────────────────────
        subagent_results: list[ToolCallResult] = []
        batch_size = self._max_concurrent

        # 提取流式回调工厂（供子 Agent 流式推送用）
        _stream_factory = getattr(state.stream_callback, '_factory', None) if state.stream_callback else None

        for batch_start in range(0, len(agent_calls), batch_size):
            batch = agent_calls[batch_start: batch_start + batch_size]
            await tool_handler.log_info(
                f"[SpawnToolExecStep] 执行子Agent批次 "
                f"[{batch_start + 1}~{batch_start + len(batch)}/"
                f"{len(agent_calls)}]",
            )

            coroutines = []
            for tc in batch:
                coro = self._execute_single_spawn(
                    tc=tc,
                    parent_snapshot=parent_snapshot,
                    tool_handler=tool_handler,
                    _stream_factory=_stream_factory,
                )
                coroutines.append(coro)

            batch_outcomes = await asyncio.gather(*coroutines, return_exceptions=True)

            all_success = True
            for tc, outcome in zip(batch, batch_outcomes):
                if isinstance(outcome, BaseException):
                    all_success = False
                    await tool_handler.log_error(
                        f"[SpawnToolExecStep] 子Agent并发调用异常 "
                        f"(tool_call_id={tc.id}): {outcome}\n{traceback.format_exc()}",
                    )
                    await tool_handler.fail(
                        f"子Agent并发调用异常: {outcome}"
                    )
                    subagent_results.append(
                        ToolCallResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=f"[ERROR] 子 Agent 执行异常: {outcome}",
                        )
                    )
                else:
                    subagent_results.append(outcome)
            if all_success:
                await tool_handler.complete()

        # ── Step E: 合并结果 ─────────────────────────────────────────
        return state.with_tool_results(normal_results + subagent_results)

    async def _execute_single_spawn(
        self,
        tc: ToolCall,
        parent_snapshot: dict[str, Any],
        tool_handler: Any,
        _stream_factory: Any = None,
    ) -> ToolCallResult:
        """执行单个 spawn_subagent 工具调用。

        解析 tc.arguments 并调用 _spawn_tool.execute()，结果转为 ToolCallResult。
        """
        args = tc.arguments or {}

        await tool_handler.log_info(
            f"[SpawnToolExecStep] 执行子Agent: "
            f"tool_call_id={tc.id}, "
            f"task_prompt={args.get('task_prompt', '')[:30]}..., "
            f"subagent_type={args.get('subagent_type', '')}, "
            f"memory_mode={args.get('memory_mode', 'empty')}",
        )

        try:
            execute_kwargs: dict[str, Any] = {
                "task_prompt": args.get("task_prompt", ""),
                "subagent_type": args.get("subagent_type", ""),
                "memory_mode": args.get("memory_mode", "empty"),
                "parent_context_snapshot": (
                    parent_snapshot
                    if args.get("memory_mode") == "inherit"
                    else None
                ),
                "session_task_handler": tool_handler,
            }
            if _stream_factory is not None:
                execute_kwargs["_stream_factory"] = _stream_factory
            tool_result = await self._spawn_tool.execute(**execute_kwargs)

            if tool_result.is_success():
                content = (
                    str(tool_result.output)
                    if tool_result.output is not None
                    else ""
                )
            else:
                content = f"[ERROR] {tool_result.error or '子 Agent 执行失败'}"

        except Exception as e:
            content = f"[ERROR] 子 Agent 调用异常: {e!s}"
            await tool_handler.log_error(
                f"[SpawnToolExecStep] _execute_single_spawn 异常: {e}\n"
                f"{traceback.format_exc()}",
            )

        return ToolCallResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            content=content,
        )
