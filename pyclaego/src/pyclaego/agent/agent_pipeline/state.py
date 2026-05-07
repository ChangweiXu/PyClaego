"""AgentState — Pipeline 各 Step 之间传递的执行状态快照

约定：
  Step 应通过 with_*() 方法或 dataclasses.replace() 创建新实例，
  而不是直接赋值修改 state 的字段。这维持了状态变更的可追溯性，
  便于在 Pipeline 中诊断问题。

TaskHandler 层级说明：
  root_task_handler  — 来自 Widget 调用层，整个 process_v2 周期内不变
  loop_task_handler  — 每轮由 ToolCallLoopStep 通过 root_task_handler.create_subtask()
                       创建，完成后调用 .complete() / .fail()；写入新 state 时同时
                       重置 tool_calls / tool_results（前一轮产物不应污染下一轮）
  LLM / Tool 子任务  — 由 steps 内部创建，不存入 state（调用方自行管理生命周期）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...context.subagent import ConfigurableSubAgentContext
    from ...llm.types import (
        ChatResponseV2,
        StreamChunk,
        ToolCall,
        ToolCallResult,
        ToolDefinition,
        UnifiedMessage,
    )
    from ...task_manager import SessionTaskHandlerV2


@dataclass
class AgentState:
    """Agent 执行周期中各 Step 之间传递的状态对象。

    字段分组：
      身份/配置  : session_id, agent_config, widget_config
      上下文     : context_handler（持有引用，对象本身可变）,
                   system_prompt, messages, tool_list
      LLM 产物   : llm_response, text_reply, tool_calls
      工具产物   : tool_results
      控制流     : round_count, max_tool_rounds,
                   is_interrupted, interrupt_reason, final_response
      TaskHandler: root_task_handler（全程不变），loop_task_handler（每轮更新）
      输入       : input_message（由 process_v2 写入，之后只读）
    """

    # ── 身份 ──────────────────────────────────────────────────────────
    session_id: str

    # ── 配置 ──────────────────────────────────────────────────────────
    agent_config: dict[str, Any]
    widget_config: dict[str, Any]

    # ── 上下文处理器（可变对象，仅持有引用） ────────────────────────────
    context_handler: ConfigurableSubAgentContext

    # ── 本轮用户输入（由 PipelineAgent.process_v2 注入，之后只读） ──────
    input_message: dict[str, Any]

    # ── LLM 上下文（由 BeforeLoopStep 填充） ─────────────────────────
    system_prompt: str | None = None
    messages: list[UnifiedMessage] = field(default_factory=list)
    tool_list: list[ToolDefinition] = field(default_factory=list)

    # ── LLM 产物（由 ToolCallLoopStep._run_llm_call 填充） ────────────
    llm_response: ChatResponseV2 | None = None
    text_reply: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    # ── 工具产物（由 ToolCallLoopStep._run_tool_exec 填充） ───────────
    tool_results: list[ToolCallResult] = field(default_factory=list)

    # ── 中间状态：非记忆工具调用（由 MemoryToolStep 写入，ToolExecStep 读取） ─
    _non_memory_calls: list[ToolCall] = field(default_factory=list, repr=False)

    # ── 控制流 ────────────────────────────────────────────────────────
    round_count: int = 0
    max_tool_rounds: int = 5
    is_interrupted: bool = False
    interrupt_reason: str = ""
    final_response: str = ""

    # ── TaskHandler 层级 ──────────────────────────────────────────────
    # root_task_handler：来自 Widget 调用层，全程不变
    root_task_handler: SessionTaskHandlerV2 | None = field(default=None, repr=False)
    # loop_task_handler：每轮重新创建，由 ToolCallLoopStep 注入
    loop_task_handler: SessionTaskHandlerV2 | None = field(default=None, repr=False)

    # ── 流式回调 ─────────────────────────────────────────────────────
    # 若设置了此回调，LLMCallStep / LastCallStep 将使用流式路径，
    # 每收到一个 StreamChunk 即调用回调（用于实时推送到前端）。
    stream_callback: Callable[[StreamChunk], Awaitable[None]] | None = field(
        default=None, repr=False
    )

    # ── 元数据 ────────────────────────────────────────────────────────
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # ------------------------------------------------------------------
    # with_* 便捷方法（返回新实例，不修改自身）
    # ------------------------------------------------------------------

    def with_context(
        self,
        system_prompt: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition],
    ) -> AgentState:
        """更新 LLM 上下文三元组（BeforeLoopStep 调用）。"""
        return replace(
            self,
            system_prompt=system_prompt,
            messages=list(messages),
            tool_list=list(tool_list),
        )

    def with_messages(self, messages: list[UnifiedMessage]) -> AgentState:
        """更新 messages（handle_after_llm_call / handle_after_tool_calls 调用）。"""
        return replace(self, messages=list(messages))

    def with_llm_result(
        self,
        llm_response: ChatResponseV2 | None,
        text_reply: str,
        tool_calls: list[ToolCall],
    ) -> AgentState:
        """更新 LLM 调用产物。"""
        return replace(
            self,
            llm_response=llm_response,
            text_reply=text_reply,
            tool_calls=list(tool_calls or []),
        )

    def with_tool_results(self, tool_results: list[ToolCallResult]) -> AgentState:
        """更新工具执行产物。"""
        return replace(self, tool_results=list(tool_results))

    def with_non_memory_calls(self, calls: list[ToolCall]) -> AgentState:
        """保存非记忆工具调用（供 ToolExecStep 使用）。"""
        return replace(self, _non_memory_calls=list(calls))

    def with_loop_handler(
        self,
        handler: SessionTaskHandlerV2,
        round_count: int,
    ) -> AgentState:
        """开始新一轮：注入 loop_task_handler，重置轮次产物。"""
        return replace(
            self,
            loop_task_handler=handler,
            round_count=round_count,
            # 重置前一轮的产物，避免污染
            tool_calls=[],
            tool_results=[],
            text_reply="",
            llm_response=None,
        )

    def with_round_count(self, round_count: int) -> AgentState:
        """仅更新轮次计数（由 LoopPipeline 调用）。"""
        return replace(self, round_count=round_count)

    def interrupt(self, reason: str = "") -> AgentState:
        """标记中断并记录原因。"""
        return replace(self, is_interrupted=True, interrupt_reason=reason)

    def complete(self, response: str) -> AgentState:
        """设置最终回复，标记任务完成。"""
        return replace(self, final_response=response)

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_last_round(self) -> bool:
        return self.round_count >= self.max_tool_rounds

    @property
    def active_handler(self) -> SessionTaskHandlerV2 | None:
        """当前最合适的 TaskHandler（循环内用 loop，循环外用 root）。"""
        return self.loop_task_handler or self.root_task_handler
