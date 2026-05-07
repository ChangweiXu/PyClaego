"""agent/subagent — 子 Agent 子模块

提供短生命周期子 Agent 的基类和具体实现：
  - BaseSubAgent            子 Agent 抽象基类（遗言机制、工作目录约定）
  - EchoSubAgent            最简子 Agent（单次 LLM 调用，无工具循环）
  - InfoGathererSubAgent    信息收集子 Agent（手写 Agent-Tool-Loop）
  - CodeExplorerSubAgent    代码探索子 Agent（手写 Agent-Tool-Loop）

配置驱动子 Agent（新架构）：
  - SubagentProfile         子代理类型配置数据类
  - SUBAGENT_PROFILES       配置驱动注册表（优先于 SUBAGENT_REGISTRY）
  - UniversalSubAgent       通用子代理（复用 PipelineAgent 的 LoopPipeline）

子 Agent 类型路由（查找到顺序）：
  1. SUBAGENT_PROFILES（新架构）→ UniversalSubAgent
  2. SUBAGENT_REGISTRY（旧架构）→ 对应 BaseSubAgent 子类

  - AgentFactory.create_subagent() 按此优先级查找。
  - 新增子 Agent 类型推荐使用 SubagentProfile（零代码）。
  - 旧 SUBAGENT_REGISTRY 保留以兼容现有代码。
"""

from .base_subagent import BaseSubAgent

# from .code_explorer_subagent import CodeExplorerSubAgent
# from .echo_subagent import EchoSubAgent
# from .info_gatherer_subagent import InfoGathererSubAgent
from .universal_subagent import UniversalSubAgent

__all__ = [
    "SUBAGENT_REGISTRY",
    "BaseSubAgent",
    # "CodeExplorerSubAgent",
    # "EchoSubAgent",
    # "InfoGathererSubAgent",
    "UniversalSubAgent",
]

# str -> BaseSubAgent 子类的路由映射（旧架构，保留兼容）
# 键名即 spawn_subagent 工具的 subagent_type 参数值
SUBAGENT_REGISTRY: dict[str, type[BaseSubAgent]] = {
    # "echo": EchoSubAgent,
    # "info_gatherer": InfoGathererSubAgent,
    # "code_explorer": CodeExplorerSubAgent,
}
