"""Pipeline 编排器与 PipelineStep 抽象基类

Pipeline     — 顺序执行 Step 列表；遇到 is_interrupted 或未处理异常时停止。
PipelineStep — 单一步骤抽象：execute() 接收 AgentState，返回新 AgentState。
LoopPipeline — 循环执行一组 Step（用于工具调用循环），支持 max_rounds 限制。

设计约定：
  - Step 的 execute() 必须返回新 AgentState（通过 with_*() 或 replace() 创建）
  - Step 不得直接赋值修改传入 state 的字段
  - execute() 若需记录日志，优先使用 state.loop_task_handler，其次 state.root_task_handler
  - 当 state.is_interrupted is True 时，Pipeline 跳过后续所有 Step
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from .state import AgentState


class PipelineStep(ABC):
    """Pipeline 步骤抽象基类。"""

    @abstractmethod
    async def execute(self, state: AgentState) -> AgentState:
        """执行此 Step 的逻辑，返回更新后的 AgentState。"""
        ...

    async def on_error(self, state: AgentState, error: Exception) -> AgentState:
        """当 execute() 抛出未捕获异常时的回调。

        默认行为：用 state.active_handler 记录错误日志，并标记 interrupted。
        子类可覆盖以实现降级（fallback）逻辑，例如返回一个部分结果而非中断。
        """
        reason = f"[{type(self).__name__}] {type(error).__name__}: {error}"
        handler = state.active_handler
        if handler:
            try:
                await handler.log_error(reason)
            except Exception:
                pass  # 日志失败不应再抛异常
        return state.interrupt(reason)


class Pipeline:
    """顺序执行 Step 列表的 Pipeline 编排器。

    遇到以下情况时停止执行后续 Step：
      1. state.is_interrupted is True（某 Step 主动标记中断）
      2. step.execute() 抛出异常，经 on_error() 处理后 state 仍为 interrupted

    正常情况下遍历完所有 Step 后返回最终 state。
    """

    def __init__(self, steps: list[PipelineStep]) -> None:
        self.steps = steps

    async def execute(self, initial_state: AgentState) -> AgentState:
        """顺序执行所有 Step，返回最终 AgentState。

        Args:
            initial_state: 初始状态（由 PipelineAgent.process_v2 构建）

        Returns:
            经所有 Step 处理后的最终状态
        """
        state = initial_state
        for step in self.steps:
            if state.is_interrupted:
                break
            try:
                state = await step.execute(state)
            except Exception as e:
                state = await step.on_error(state, e)
                if state.is_interrupted:
                    break
        return state


class LoopPipeline(PipelineStep):
    """循环执行一组 Step 的 Pipeline 编排器（用于工具调用循环）。

    每轮循环结束后检查：
      1. state.is_interrupted → 停止循环
      2. not state.has_tool_calls → 正常结束（LLM 不再需要工具）
      3. round_count >= max_rounds → 达到最大轮次，停止

    Args:
        steps:      每轮循环内执行的 Step 列表
        max_rounds: 最大循环轮次
        on_last_round: 可选回调，当达到最大轮次时调用，返回一个 Step 用于执行收尾逻辑
                       （例如 LastCallStep）。签名: () -> PipelineStep
    """

    def __init__(
        self,
        steps: list[PipelineStep],
        max_rounds: int = 5,
        on_last_round: Callable[[], PipelineStep] | None = None,
    ) -> None:
        self.steps = steps
        self.max_rounds = max_rounds
        self.on_last_round = on_last_round

    async def execute(self, initial_state: AgentState) -> AgentState:
        """循环执行所有 Step，返回最终 AgentState。"""
        state = initial_state

        for round_idx in range(1, self.max_rounds + 1):
            state = state.with_round_count(round_idx)

            for step in self.steps:
                if state.is_interrupted:
                    break
                try:
                    state = await step.execute(state)
                except Exception as e:
                    state = await step.on_error(state, e)
                    if state.is_interrupted:
                        break

            # 循环结束条件检查
            if state.is_interrupted:
                break
            if not state.has_tool_calls:
                # LLM 不再发起工具调用，正常结束 — 设置 final_response
                if not state.final_response:
                    state = state.complete(state.text_reply or "")
                break
            if round_idx >= self.max_rounds and self.on_last_round:
                # 达到最大轮次，执行收尾 Step
                last_step = self.on_last_round()
                try:
                    state = await last_step.execute(state)
                except Exception as e:
                    state = await last_step.on_error(state, e)
                break

        return state
