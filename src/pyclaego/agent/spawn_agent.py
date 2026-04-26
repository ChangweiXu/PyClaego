"""SpawnAgent — 支持动态创建子 Agent 的主 Agent

继承 SimpleAgent，重写工具调用循环中的执行阶段，增加对 AGENT_TOOL_NAMES 的拦截、
并发调度和队列等待能力。

工具调用循环各步骤：
    Step A  context_handler 拦截记忆工具（来自 after_llm_call，复用 SimpleAgent 逻辑）
    Step B  从 non_memory_calls 中分离出 Agent 工具调用 (agent_calls) 和普通工具调用 (normal_calls)
    Step C  顺序执行 normal_calls（复用 SimpleAgent._execute_tool_calls）
    Step D  并发执行 agent_calls（分批，每批不超过 max_concurrent_subagents）
            超出批次的放入 _pending_agent_calls，下一批继续；直到本轮所有 agent_calls 完成
    Step E  合并 normal_results + subagent_results + memory_tool_results，统一回传 LLM

配置项：
    agent.{agent_type}.max_concurrent_subagents (int, >0): 子 Agent 并发上限
    agent.llm (str): LLM provider ID（继承自 SimpleAgent）
    agent.simple.max_tool_rounds (int): 工具调用最大轮次（继承自 SimpleAgent）
"""

import asyncio
import traceback
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable, Tuple

from .simple_agent import SimpleAgent
from ..context.base_context import BaseContextHandlerV3
from ..context.system_prompts.simple_v2 import LAST_CALL_PROMPT
from ..context.agent_tools import AGENT_TOOL_NAMES, SpawnSubagentTool
from ..llm import UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition, ChatResponseV2
from ..task_manager import SessionTaskHandlerV2, TaskType
from ..logging import get_running_log

_rlog = get_running_log()


class SpawnAgent(SimpleAgent):
    """支持动态创建子 Agent 的主 Agent

    继承 SimpleAgent，核心变更：
    - 工具调用循环中拦截 AGENT_TOOL_NAMES 里的调用（不立即执行）
    - 普通工具执行后，并发执行已缓存的子 Agent 工具调用
    - 超过 max_concurrent_subagents 上限的调用分批执行，不返回 ERROR
    - 使用 asyncio.gather(*tasks, return_exceptions=True) 并发驱动

    Attributes:
        _subagent_handler:         AgentFactory.create_subagent 引用（由 AgentFactory 注入）
        max_concurrent_subagents:  子 Agent 并发上限（从配置读取）
    """

    def __init__(self, config: Dict[str, Any], session_id: str) -> None:
        super().__init__(config, session_id)

        # 子 Agent 并发上限（AgentFactory 保证 max_concurrent > 0 时才创建 SpawnAgent）
        agent_type = config.get("type", "spawn")
        strategy_config = config.get(agent_type, {})
        self.max_concurrent_subagents: int = strategy_config.get(
            "max_concurrent_subagents", 1
        )

        # 子 Agent handler（由 AgentFactory.create_agent 注入，不污染 config）
        self._subagent_handler: Optional[Callable] = None
        self._spawn_tool: Optional[SpawnSubagentTool] = None

        _rlog.info(
            f"session_{session_id}",
            f"[SpawnAgent] 初始化完成 (max_concurrent_subagents={self.max_concurrent_subagents}, "
            f"llm_id={self.get_llm_id()}, max_tool_rounds={self.max_tool_rounds})",
        )

    # ------------------------------------------------------------------
    # 主入口 — 重写 process()，整合子 Agent 调用分支
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: Dict[str, Any],
        context_handler,
        user_id: str,
        **kwargs,
    ) -> str:
        raise NotImplementedError("请使用 SpawnAgent.process_v2() instead.")

    async def process_v2(
        self,
        user_message: Dict[str, Any],
        context_handler: BaseContextHandlerV3,
        session_task_handler: SessionTaskHandlerV2,
        **kwargs,
    ) -> str:
        """处理用户消息（V3 生命周期版本，支持 Spawn 子 Agent）"""
        user_text: str = user_message.get("content", "") if isinstance(user_message, dict) else str(user_message)
        final_response = ""
        interrupted = False

        await session_task_handler.log_info(
            f"[SpawnAgent] 开始处理消息 (user={session_task_handler.get_user_id()}) "
            f"`{user_text[:30]}...`"
        )

        try:
            # 1) 获取初始上下文
            llm_context = await context_handler.handle_before_loop(user_message)
            system_prompt: Optional[str] = llm_context.get("system")
            messages: List[UnifiedMessage] = llm_context.get("messages", [])
            tool_list: List[ToolDefinition] = llm_context.get("tool_list", [])

            # 2) 注入 spawn_subagent 工具定义
            if self._spawn_tool is not None:
                spawn_def = self._spawn_tool.to_tool_definition()
                existing_names = {t.name for t in (tool_list or [])}
                if spawn_def.name not in existing_names:
                    tool_list = list(tool_list or []) + [spawn_def]

            await session_task_handler.log_info(
                f"[SpawnAgent] 上下文就绪。消息数: {len(messages)}，工具数: {len(tool_list)}"
            )

            # 3) 工具调用循环
            round_count = 0
            loop_task_handler = None

            while round_count <= self.max_tool_rounds:
                round_count += 1
                loop_task_handler = await session_task_handler.create_subtask(
                    task_type=TaskType.AGENT_LOOP,
                    name=f"SpawnAgent Loop #{round_count}",
                    metadata={"round": round_count, "agent_type": "spawn"},
                )
                await loop_task_handler.log_info(
                    f"[SpawnAgent] ===== 第 {round_count} 轮工具循环 ====="
                )

                # V6 hook: 调用前允许 handler 改写 messages
                messages = await context_handler.handle_before_llm_call(
                    messages,
                    loop_task_handler=loop_task_handler,
                )
                llm_result: Dict[str, Any] = await self.security_handler.request_llm_call_v3(
                    session_task_handler=loop_task_handler,
                    llm_id=self.get_llm_id(),
                    system=system_prompt,
                    messages=messages,
                    tool_list=tool_list if self.use_tools else None,
                    stream=False,
                    **kwargs,
                )

                if not llm_result["success"]:
                    interrupted = True
                    error_msg = llm_result.get("error", "Unknown error")
                    await loop_task_handler.log_error(
                        f"[SpawnAgent] [round {round_count}] LLM调用失败: {error_msg}"
                    )
                    final_response = f"[ERROR] LLM调用失败: {error_msg}"
                    break

                v2_resp: ChatResponseV2 = llm_result["v2_response"]
                stop_reason = v2_resp.stop_reason
                text_reply: Optional[str] = v2_resp.text
                tool_calls: Optional[List[ToolCall]] = v2_resp.tool_calls

                messages = await context_handler.handle_after_llm_call(
                    text_reply=text_reply,
                    tool_calls=tool_calls,
                    reasoning=v2_resp.reasoning,
                    produced_by_provider=v2_resp.produced_by_provider,
                    produced_by_model=v2_resp.produced_by_model,
                )
                await loop_task_handler.log_info(
                    f"[SpawnAgent] [round {round_count}] LLM 回复: "
                    f"stop_reason={stop_reason}, "
                    f"text=`{(text_reply or '')[:30]}...`, "
                    f"tool_calls={len(tool_calls) if tool_calls else 0}"
                )

                if not tool_calls:
                    final_response = text_reply or ""
                    await loop_task_handler.log_info(
                        f"[SpawnAgent] [round {round_count}] 无工具调用，结束循环"
                    )
                    await loop_task_handler.complete()
                    break

                await loop_task_handler.log_info(
                    f"[SpawnAgent] [round {round_count}] 开始执行 {len(tool_calls)} 个工具调用"
                )

                memory_exec_status = await context_handler.handle_memory_tool_calls(
                    tool_calls=tool_calls,
                    loop_task_handler=loop_task_handler,
                )

                if not memory_exec_status:
                    non_memory_calls = tool_calls
                    memory_tool_results: List[ToolCallResult] = []
                else:
                    non_memory_calls = memory_exec_status.get("non_memory_calls", [])
                    memory_tool_results = memory_exec_status.get("memory_tool_results", [])

                spawn_tool_results = await self._execute_tool_calls_with_spawn(
                    all_tool_calls=non_memory_calls,
                    round_count=round_count,
                    context_handler=context_handler,
                    loop_task_handler=loop_task_handler,
                )
                tool_results = list(spawn_tool_results) + list(memory_tool_results)

                is_last_round = (round_count >= self.max_tool_rounds)
                messages = await context_handler.handle_after_tool_calls(
                    tool_results=tool_results,
                    last_call_prompt=LAST_CALL_PROMPT if is_last_round else None,
                    loop_task_handler=loop_task_handler,
                )

                await loop_task_handler.complete()

                if is_last_round:
                    await session_task_handler.log_warning(
                        f"[SpawnAgent] 达到最大工具调用轮次 ({self.max_tool_rounds})，发起最终 LLM 调用",
                    )
                    final_response = await self._last_call(
                        system_prompt=system_prompt,
                        messages=messages,
                        session_task_handler=session_task_handler,
                    )
                    break

            await session_task_handler.log_info(
                f"[SpawnAgent] 处理完成，共 {round_count} 轮, "
                f"最终回复: `{final_response[:30]}...`"
            )

        except asyncio.CancelledError:
            interrupted = True
            final_response = "⚠️ 任务被取消"
            raise

        except Exception as e:
            interrupted = True
            await session_task_handler.log_error(
                f"[SpawnAgent] 处理失败: {e}\n{traceback.format_exc()}"
            )
            final_response = f"[ERROR] 处理消息时出现错误：{str(e)}"

        finally:
            if interrupted:
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
                    await context_handler.handle_after_loop(
                        final_message=assistant_msg,
                    )
                except Exception as e:
                    await session_task_handler.log_error(
                        f"[SpawnAgent] after_loop 通知失败（不影响响应）: {e}\n{traceback.format_exc()}"
                    )

        return final_response

    # ------------------------------------------------------------------
    # 核心：带子 Agent 调度的工具执行方法
    # ------------------------------------------------------------------

    async def _execute_tool_calls_with_spawn(
        self,
        all_tool_calls: List[ToolCall],
        round_count: int,
        context_handler: BaseContextHandlerV3,
        loop_task_handler: SessionTaskHandlerV2,
    ) -> List[ToolCallResult]:
        """带子 Agent 分离和并发调度的工具执行方法

        Step B: 分离 agent_calls 和 normal_calls
        Step C: 顺序执行 normal_calls
        Step D: 分批并发执行 agent_calls（超上限的分批执行，不报 ERROR）
        Step E: 合并结果

        Args:
            all_tool_calls:      本轮所有非记忆工具调用（包含 agent 工具和普通工具）
            round_count:         当前轮次（仅用于日志）
            context_handler:     父 Agent 上下文处理器（获取 spawn_context_init 快照用）
            loop_task_handler:   进度通知回调（SessionTaskHandlerV2 对象）

        Returns:
            合并后的 ToolCallResult 列表
        """
        # ── Step B: 分离 ────────────────────────────────────────────
        agent_calls: List[ToolCall] = []
        normal_calls: List[ToolCall] = []

        for tc in all_tool_calls:
            if tc.name in AGENT_TOOL_NAMES:
                agent_calls.append(tc)
            else:
                normal_calls.append(tc)

        await loop_task_handler.log_info(
            f"[SpawnAgent] [round {round_count}] "
            f"normal_calls={len(normal_calls)}, agent_calls={len(agent_calls)}",
        )

        # ── Step C: 顺序执行普通工具 ─────────────────────────────────
        normal_results: List[ToolCallResult] = await self._execute_tool_calls_v2(
            normal_calls, round_count, loop_task_handler=loop_task_handler
        )

        # 如果没有调用 Agent 工具，直接返回
        if not agent_calls:
            return normal_results

        # ── Step D: 并发执行子 Agent 工具调用 ────────────────────────
        # 获取父 Agent 上下文快照（供 inherit 模式使用）
        parent_snapshot: Dict[str, Any] = {}
        try:
            parent_snapshot = await context_handler.handle_spawn_context_snapshot()
        except Exception as snap_err:
            await loop_task_handler.log_warning(
                f"[SpawnAgent] [round {round_count}] "
                f"获取 spawn_context_init 快照失败: {snap_err}\n{traceback.format_exc()}",
            )

        try:
            assert self._spawn_tool is not None, "_spawn_tool 未初始化，无法执行子 Agent 工具调用"
        except Exception as build_err:
            # 未注入 _subagent_handler，降级为 ERROR 结果
            await loop_task_handler.log_error(
                f"[SpawnAgent] [round {round_count}] "
                f"spawn_tool 创建失败: {build_err}\n{traceback.format_exc()}",
            )
            error_results: List[ToolCallResult] = []
            for tc in agent_calls:
                error_results.append(ToolCallResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content="[ERROR] SpawnAgent._subagent_handler 未初始化，无法创建子 Agent",
                ))
            return normal_results + error_results

        # 分批并发执行（每批 <= max_concurrent_subagents）
        subagent_results: List[ToolCallResult] = []
        batch_size = max(self.max_concurrent_subagents, 1)

        for batch_start in range(0, len(agent_calls), batch_size):
            batch = agent_calls[batch_start: batch_start + batch_size]
            await loop_task_handler.log_info(
                f"[SpawnAgent] [round {round_count}] 执行子Agent批次 "
                f"[{batch_start+1}~{batch_start+len(batch)}/"
                f"{len(agent_calls)}]",
            )

            # 构建每个 agent_call 的 coroutine
            coroutines = []
            for tc in batch:
                coro = self._execute_single_spawn_call(
                    tc=tc,
                    spawn_tool=self._spawn_tool,
                    parent_context_snapshot=parent_snapshot,
                    loop_task_handler=loop_task_handler,
                    subagent_index=batch_start + len(coroutines),  # 1-based index for logging
                )
                coroutines.append(coro)

            # 并发执行，return_exceptions=True 防止单个异常打断整批
            batch_outcomes = await asyncio.gather(*coroutines, return_exceptions=True)

            for tc, outcome in zip(batch, batch_outcomes):
                if isinstance(outcome, BaseException):
                    await loop_task_handler.log_error(
                        f"[SpawnAgent] 子Agent并发调用异常 (tool_call_id={tc.id}): {outcome}",
                    )
                    subagent_results.append(ToolCallResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        content=f"[ERROR] 子 Agent 执行异常: {outcome}",
                    ))
                else:
                    subagent_results.append(outcome)

        return normal_results + subagent_results

    async def _execute_single_spawn_call(
        self,
        tc: ToolCall,
        spawn_tool: "SpawnSubagentTool",
        parent_context_snapshot: Dict[str, Any],
        loop_task_handler: SessionTaskHandlerV2,
        subagent_index: int,
        **kwargs,
    ) -> ToolCallResult:
        """执行单个 spawn_subagent 工具调用

        解析 tc.arguments 并调用 spawn_tool.execute()，将结果转换为 ToolCallResult。

        Args:
            tc:                  工具调用对象（含 id/name/arguments）
            spawn_tool:          SpawnSubagentTool 实例
            parent_snapshot:     父 Agent 上下文快照（inherit 模式用）
            loop_task_handler:   进度通知回调
            subagent_index:      子 Agent 序号（用于日志）

        Returns:
            ToolCallResult
        """
        args = tc.arguments or {}

        await loop_task_handler.log_info(
            f"[SpawnAgent] 执行子Agent调用: "
            f"subagent_index={subagent_index}, "
            f"tool_call_id={tc.id}, "
            f"task_prompt={args.get('task_prompt', '')[:30]}..., "
            f"subagent_type={args.get('subagent_type', '')}, "
            f"memory_mode={args.get('memory_mode', 'empty')}"
        )

        try:
            tool_result = await spawn_tool.execute(
                task_prompt=args.get("task_prompt", ""),
                subagent_type=args.get("subagent_type", ""),
                memory_mode=args.get("memory_mode", "empty"),
                parent_context_snapshot=parent_context_snapshot if args.get("memory_mode") == "inherit" else None,
                session_task_handler=loop_task_handler,  # 注入带子 Agent 标记的回调函数
            )

            if tool_result.is_success():
                content = str(tool_result.output) if tool_result.output is not None else ""
            else:
                content = f"[ERROR] {tool_result.error or '子 Agent 执行失败'}"

        except Exception as e:
            content = f"[ERROR] 子 Agent 调用异常: {str(e)}"
            await loop_task_handler.log_error(
                f"[SpawnAgent] _execute_single_spawn_call 异常: {e}\n{traceback.format_exc()}",
            )

        return ToolCallResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            content=content,
        )

    # ------------------------------------------------------------------
    # 辅助：构建 SpawnSubagentTool 实例
    # ------------------------------------------------------------------

    def _build_spawn_tool(self) -> "SpawnSubagentTool":
        """构建 SpawnSubagentTool 实例（需 _subagent_handler 已注入）

        Returns:
            SpawnSubagentTool 实例，若 _subagent_handler 未注入则返回 None
        """
        if self._subagent_handler is None:
            raise RuntimeError("_subagent_handler 未注入，无法执行子 Agent 工具调用")
        return SpawnSubagentTool(
            tool_config={},
            subagent_handler=self._subagent_handler,
            session_id=self.session_id,
            base_agent_config=self.config,
        )

    def inject_subagent_handler(self, handler: Callable) -> None:
        """注入子 Agent handler（供 AgentFactory 使用）"""
        self._subagent_handler = handler
        self._spawn_tool = self._build_spawn_tool()

    def get_info(self) -> Dict[str, Any]:
        """获取 Agent 信息"""
        info = super().get_info()
        info.update({
            "max_concurrent_subagents": self.max_concurrent_subagents,
            "has_subagent_handler": self._subagent_handler is not None,
        })
        return info
