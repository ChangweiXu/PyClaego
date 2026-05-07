"""PipelineAgent — 基于 Pipeline 框架的 Agent 实现

外部接口与 SimpleAgent 兼容：
  - 上游 Widget 调用 agent.process_v2(user_message, context_handler, session_task_handler)
  - 下游使用 SecurityHandler / ToolManager / TaskManager / logging 等现有基础设施

配置示例（widget_config["agent"]）：
  type:           "pipeline"
  llm:            "kimi_code"   # LLM provider ID
  use_tools:      true          # 是否向 LLM 暴露工具
  pipeline:
    max_tool_rounds: 5          # 工具调用最大轮次
    max_concurrent_subagents: 5 # 子 Agent 并发上限（>0 启用 spawn 能力）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...security_executor import SecurityHandler
from ...task_manager import SessionTaskHandlerV2
from ..base_agent import BaseAgent
from .pipeline import LoopPipeline, Pipeline, PipelineStep
from .retry import RetryPolicy
from .state import AgentState
from .steps import (
    AfterLLMCallStep,
    AfterLoopStep,
    AfterToolCallStep,
    BeforeLLMCallStep,
    BeforeLoopStep,
    LastCallStep,
    LLMCallStep,
    LoopEndStep,
    LoopStartStep,
    MemoryToolStep,
    ToolExecStep,
)


class PipelineAgent(BaseAgent):
    """基于 Pipeline 框架的主 Agent。

    对比 SimpleAgent：
      - 循环逻辑分解为可组合的 PipelineStep
      - 内置 RetryPolicy（LLM 调用重试）
      - 支持子类覆盖单个 Step，无需重写整个循环

    子 Agent 派遣（spawn）：
      - 配置 pipeline.max_concurrent_subagents > 0 即可启用
      - 通过 inject_subagent_handler / inject_widget_workspace 注入依赖
      - process_v2 自动将 spawn_subagent 工具追加到 tool_list
      - _build_pipeline 自动切换到 SpawnToolExecStep（并发执行）
    """

    def __init__(
        self,
        config: dict[str, Any],   # 完整 widget_config
        session_id: str,
    ) -> None:
        super().__init__(config, session_id)
        self._security_handler = SecurityHandler.get_instance()

        # self.config = widget_config["agent"]（由 BaseAgent 赋值）
        self.set_llm_id(self.config.get("llm", "kimi_code"))
        self.use_tools: bool = self.config.get("use_tools", True)

        # Pipeline 专属策略配置 —— 存放在 agent.pipeline.xxx
        strategy_cfg: dict[str, Any] = self.config.get("pipeline", {})
        self.max_tool_rounds: int = int(strategy_cfg.get("max_tool_rounds", 5))
        self.max_concurrent_subagents: int = int(
            strategy_cfg.get("max_concurrent_subagents", 0)
        )

        # ── 子 Agent 派遣基础设施（延迟注入） ──────────────────────────
        self._subagent_handler: Callable | None = None
        self._spawn_tool: Any = None  # SpawnSubagentTool 实例
        self._widget_workspace: Path | None = None
        self._tool_agents: dict[str, Any] = {}  # ToolAgentConfig 快照（由 Widget 注入）

    # ------------------------------------------------------------------
    # 注入接口（由 AgentFactory / Widget.load 调用）
    # ------------------------------------------------------------------

    def inject_subagent_handler(self, handler: Callable) -> None:
        """注入子 Agent handler（供 AgentFactory 使用）。

        若 widget_workspace 已注入，则同时构建 _spawn_tool。
        """
        self._subagent_handler = handler
        if self._widget_workspace is not None:
            self._spawn_tool = self._build_spawn_tool()

    def inject_widget_workspace(self, workspace: Path) -> None:
        """注入当前 Widget 的工作目录（由 Widget.load() 调用）。

        若 _subagent_handler 也已注入，则立即构建 _spawn_tool。
        """
        self._widget_workspace = workspace
        if self._subagent_handler is not None and self._spawn_tool is None:
            self._spawn_tool = self._build_spawn_tool()

    def inject_tool_agents(self, tool_agents: dict[str, Any]) -> None:
        """注入 Widget 级 tool_agents 快照（由 Widget.load() 调用）。"""
        self._tool_agents = tool_agents or {}

    def _build_spawn_tool(self) -> Any:
        """构建 SpawnSubagentTool 实例（需 handler + workspace 已注入）。"""
        if self._subagent_handler is None:
            raise RuntimeError("_subagent_handler 未注入")
        if self._widget_workspace is None:
            raise RuntimeError("widget_workspace 未注入")
        from ...context.agent_tools import DynamicSpawnSubagentTool

        return DynamicSpawnSubagentTool(
            tool_config={},
            subagent_handler=self._subagent_handler,
            session_id=self.session_id,
            base_agent_config=self.config,
            widget_config=self.widget_config,
            widget_workspace=self._widget_workspace,
        )

    @property
    def _spawn_enabled(self) -> bool:
        """子 Agent 派遣是否可用。"""
        return self.max_concurrent_subagents > 0 and self._spawn_tool is not None

    # ------------------------------------------------------------------
    # BaseAgent 接口实现
    # ------------------------------------------------------------------

    async def process(
        self,
        user_message: dict[str, Any],
        context_handler: Any,
        user_id: str,
        **kwargs: Any,
    ) -> str:
        raise NotImplementedError(
            "PipelineAgent 仅支持 process_v2；请通过 Widget 调用。"
        )

    async def process_v2(
        self,
        user_message: dict[str, Any],
        context_handler: Any,
        session_task_handler: SessionTaskHandlerV2,
        *,
        stream_callback: Callable[[Any], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> str:
        """处理一次用户消息，返回 Agent 最终回复字符串。

        当 _spawn_enabled 时，自动将 spawn_subagent 工具注入 tool_list，
        并使用 SpawnToolExecStep 处理并发子 Agent 执行。

        Args:
            user_message:          用户消息 dict（含 "content" 或 "content_parts"）
            context_handler:       实现 BaseContextHandlerV3 的上下文处理器
            session_task_handler:  来自 Widget 的根 TaskHandler
            stream_callback:       可选流式回调 async (StreamChunk) -> None。
                                   设置后 LLMCallStep / LastCallStep 将使用流式路径，
                                   每收到一个 StreamChunk 即调用回调。
            **kwargs:              保留扩展参数（向前兼容）

        Returns:
            Agent 最终回复字符串
        """
        # 将 session_task_handler 注入 context_handler（若支持）
        if hasattr(context_handler, "set_session_task_handler"):
            context_handler.set_session_task_handler(session_task_handler)

        initial_state = AgentState(
            session_id=self.session_id,
            agent_config=self.config,
            widget_config=self.widget_config,
            context_handler=context_handler,
            input_message=user_message,
            max_tool_rounds=self.max_tool_rounds,
            root_task_handler=session_task_handler,
            stream_callback=stream_callback,
        )

        pipeline = self._build_pipeline()
        final_state = await pipeline.execute(initial_state)
        return final_state.final_response

    # ------------------------------------------------------------------
    # Pipeline 构建（子类可覆盖以定制步骤）
    # ------------------------------------------------------------------

    def _build_pipeline(self) -> Pipeline:
        """构造执行管线（子类可覆盖以插入/替换 Step）。

        使用细粒度 Step + LoopPipeline 驱动工具调用循环。
        当 _spawn_enabled 时，ToolExecStep 替换为 SpawnToolExecStep。
        """
        from ...context.system_prompts.simple_v2 import LAST_CALL_PROMPT

        def _make_last_call() -> PipelineStep:
            """达到最大轮次时：先附加 last_call_prompt，再发起最终调用。"""
            return _LastCallSequence(
                after_tool=AfterToolCallStep(last_call_prompt=LAST_CALL_PROMPT),
                last_call=LastCallStep(llm_id=self.get_llm_id()),
            )

        # 选择工具执行步骤：spawn 模式 vs 普通模式
        tool_exec_step: PipelineStep
        if self._spawn_enabled:
            from .steps.spawn_tool_exec import SpawnToolExecStep

            tool_exec_step = SpawnToolExecStep(
                spawn_tool=self._spawn_tool,
                max_concurrent=self.max_concurrent_subagents,
            )
        else:
            tool_exec_step = ToolExecStep()

        return Pipeline(
            [
                BeforeLoopStep(),
                _ToolAgentInfoStep(self) if self._tool_agents else _NoopStep(),
                _SpawnToolInjectStep(self) if self._spawn_enabled else _NoopStep(),
                LoopPipeline(
                    steps=[
                        LoopStartStep(),
                        BeforeLLMCallStep(),
                        LLMCallStep(
                            llm_id=self.get_llm_id(),
                            use_tools=self.use_tools,
                            retry_policy=RetryPolicy(),
                        ),
                        AfterLLMCallStep(),
                        MemoryToolStep(),
                        tool_exec_step,
                        AfterToolCallStep(),
                        LoopEndStep(),
                    ],
                    max_rounds=self.max_tool_rounds,
                    on_last_round=_make_last_call,
                ),
                AfterLoopStep(),
            ]
        )


# ===========================================================================
# 内部辅助 Steps
# ===========================================================================


class _NoopStep(PipelineStep):
    """空操作 Step（spawn 未启用时占位）。"""

    async def execute(self, state: AgentState) -> AgentState:
        return state


class _ToolAgentInfoStep(PipelineStep):
    """将可用 tool_agents 摘要注入 system_prompt。

    在 BeforeLoopStep 之后、_SpawnToolInjectStep 之前执行，
    使主 Agent 在决策前就能了解所有可用的子代理类型及其能力。
    """

    def __init__(self, agent: PipelineAgent) -> None:
        self._agent = agent

    async def execute(self, state: AgentState) -> AgentState:
        tool_agents = self._agent._tool_agents
        if not tool_agents:
            return state

        lines = [
            "\n\n---\n\n# SubAgent Delegation\n",
            "## 可用子代理类型（ToolAgent）\n",
        ]
        for i, (name, cfg) in enumerate(sorted(tool_agents.items()), 1):
            # cfg 是 ToolAgentConfig
            tools_str = (
                "全部工具"
                if cfg.uses_all_tools
                else ", ".join(cfg.allowed_tools) if cfg.allowed_tools else "无"
            )
            skills_str = (
                "全部技能"
                if cfg.uses_all_skills
                else ", ".join(cfg.skills) if cfg.skills else "无"
            )
            lines.append(f"### {i}. {name}")
            lines.append(f"- **描述**: {cfg.description}")
            lines.append(f"- **工具**: {tools_str}")
            lines.append(f"- **技能**: {skills_str}")
            lines.append(f"- **最大轮次**: {cfg.max_tool_rounds}")
            lines.append(f"- **上下文策略**: {cfg.context_strategy}")
            lines.append("")
        lines += [
            "\n## Delegation Principles\n",
            "> 使用 spawn_subagent 工具派遣子代理时，subagent_type 参数使用以上名称。",
            "> task_prompt 必须包含：**明确目标**、**输出格式要求**、**长度限制**。",
            "> 要求子 Agent 返回结构化摘要（而非原始数据），包含关键发现和引用位置。",
            "> 一次可并发多个子 Agent（各自独立），但每个子 Agent 的任务应当自包含。",
            "> 当结果不需要工具时，用 echo 类型；需要文件/网络操作时，用 info_gatherer 类型；需要执行代码或编辑文件时，用 pipeline 类型。",
        ]

        new_prompt = (state.system_prompt or "") + "\n".join(lines)
        state = state.with_context(
            system_prompt=new_prompt,
            messages=state.messages,
            tool_list=state.tool_list,
        )
        return state


class _SpawnToolInjectStep(PipelineStep):
    """在 BeforeLoopStep 之后将 spawn_subagent 工具注入 state.tool_list。

    必须在 BeforeLoopStep 之后执行（因为 tool_list 由 BeforeLoopStep 填充），
    且在 LoopPipeline 之前执行（因为 LLMCallStep 需要完整的 tool_list）。
    """

    def __init__(self, agent: PipelineAgent) -> None:
        self._agent = agent

    async def execute(self, state: AgentState) -> AgentState:
        spawn_tool = self._agent._spawn_tool
        if spawn_tool is None:
            return state

        spawn_def = spawn_tool.to_tool_definition()
        existing_names = {t.name for t in state.tool_list}
        if spawn_def.name not in existing_names:
            new_tool_list = list(state.tool_list) + [spawn_def]
            state = state.with_context(
                system_prompt=state.system_prompt,
                messages=state.messages,
                tool_list=new_tool_list,
            )
        return state


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
