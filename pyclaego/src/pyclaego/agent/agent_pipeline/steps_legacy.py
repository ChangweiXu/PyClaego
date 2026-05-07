"""ToolCallLoopStep — 向后兼容层。

将原 ToolCallLoopStep 的完整逻辑委托给 LoopPipeline + 细粒度 Step。
现有代码中所有引用 ToolCallLoopStep 的地方无需修改。
"""
from __future__ import annotations

from ...context.system_prompts.simple_v2 import LAST_CALL_PROMPT
from .pipeline import LoopPipeline, PipelineStep
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy
from .state import AgentState
from .steps.after_llm import AfterLLMCallStep
from .steps.after_tool import AfterToolCallStep
from .steps.before_llm import BeforeLLMCallStep
from .steps.last_call import LastCallStep
from .steps.llm_call import LLMCallStep
from .steps.loop_end import LoopEndStep
from .steps.loop_start import LoopStartStep
from .steps.memory_tool import MemoryToolStep
from .steps.tool_exec import ToolExecStep


class ToolCallLoopStep(PipelineStep):
    """工具调用主循环 Step（兼容层）。

    内部使用 LoopPipeline 驱动细粒度 Step，行为与原实现完全一致。

    Args:
        llm_id:       LLM provider ID（同 agent.llm 配置）
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

    async def execute(self, state: AgentState) -> AgentState:
        """构建 LoopPipeline 并执行。

        max_rounds 从 state 读取（而非构造参数），以支持运行时配置。
        """

        def _make_last_call() -> PipelineStep:
            """达到最大轮次时：先附加 last_call_prompt，再发起最终调用。"""
            return _LastCallSequence(
                after_tool=AfterToolCallStep(last_call_prompt=LAST_CALL_PROMPT),
                last_call=LastCallStep(llm_id=self.llm_id),
            )

        loop = LoopPipeline(
            steps=[
                LoopStartStep(),
                BeforeLLMCallStep(),
                LLMCallStep(
                    llm_id=self.llm_id,
                    use_tools=self.use_tools,
                    retry_policy=self.retry_policy,
                ),
                AfterLLMCallStep(),
                MemoryToolStep(),
                ToolExecStep(),
                AfterToolCallStep(),
                LoopEndStep(),
            ],
            max_rounds=state.max_tool_rounds,
            on_last_round=_make_last_call,
        )
        return await loop.execute(state)


class _LastCallSequence(PipelineStep):
    """组合 Step：先执行 after_tool_calls(last_call_prompt)，再执行 last_call。"""

    def __init__(self, after_tool: PipelineStep, last_call: PipelineStep) -> None:
        self._after_tool = after_tool
        self._last_call = last_call

    async def execute(self, state: AgentState) -> AgentState:
        state = await self._after_tool.execute(state)
        if state.is_interrupted:
            return state
        return await self._last_call.execute(state)

    async def on_error(self, state: AgentState, error: Exception) -> AgentState:
        return await self._last_call.on_error(state, error)
