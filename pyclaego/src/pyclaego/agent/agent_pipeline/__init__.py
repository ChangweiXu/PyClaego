"""agent/agent_pipeline — Pipeline 框架核心及 PipelineAgent

公开接口：
  AgentState                 — Step 间传递的状态快照
  RetryPolicy / DEFAULT_RETRY_POLICY — LLM 调用重试策略
  Pipeline / PipelineStep / LoopPipeline — 管线编排器与步骤抽象基类
  BeforeLoopStep             — 初始化 LLM 上下文
  ToolCallLoopStep           — 工具调用主循环（兼容层，内部委托 LoopPipeline）
  LoopStartStep              — 创建 loop_task_handler
  BeforeLLMCallStep          — handle_before_llm_call
  LLMCallStep                — LLM 调用 + 重试
  AfterLLMCallStep           — handle_after_llm_call
  MemoryToolStep             — handle_memory_tool_calls
  ToolExecStep               — 执行普通工具
  AfterToolCallStep          — handle_after_tool_calls
  LoopEndStep                — 完成 loop_task_handler
  LastCallStep               — 最终无工具 LLM 调用
  AfterLoopStep              — 写盘/中断清理
  PipelineAgent              — 基于管线的主 Agent
"""
from .agent import PipelineAgent
from .pipeline import LoopPipeline, Pipeline, PipelineStep
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy
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
from .steps_legacy import ToolCallLoopStep

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "AfterLLMCallStep",
    "AfterLoopStep",
    "AfterToolCallStep",
    "AgentState",
    "BeforeLLMCallStep",
    "BeforeLoopStep",
    "LLMCallStep",
    "LastCallStep",
    "LoopEndStep",
    "LoopPipeline",
    "LoopStartStep",
    "MemoryToolStep",
    "Pipeline",
    "PipelineAgent",
    "PipelineStep",
    "RetryPolicy",
    "ToolCallLoopStep",
    "ToolExecStep",
]
