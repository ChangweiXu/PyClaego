"""agent/subagent — 子 Agent 子模块

提供短生命周期子 Agent 的基类和具体实现：
  - BaseSubAgent              子 Agent 抽象基类（遗言机制、工作目录约定）
  - InfoGathererSubAgent      信息收集子 Agent（网页抓取/文件分析）
  - CodeExplorerSubAgent      代码探索子 Agent（只读代码分析）

子 Agent 类型路由：
  SUBAGENT_REGISTRY: Dict[str, Type[BaseSubAgent]]

  - AgentFactory.create_subagent() 通过此字典按 subagent_type 字符串查找对应类。
  - LLM 在 spawn_subagent 工具调用中指定 subagent_type 参数时，
    必须是 SUBAGENT_REGISTRY 中已注册的键名。
  - 新增子 Agent 类型时，在本文件的 SUBAGENT_REGISTRY 中追加注册即可。
"""

from typing import Dict, Type

from .base_subagent import BaseSubAgent
from .info_gatherer_subagent import InfoGathererSubAgent
from .code_explorer_subagent import CodeExplorerSubAgent

__all__ = [
    "BaseSubAgent",
    "CodeExplorerSubAgent",
    "InfoGathererSubAgent",
    "SUBAGENT_REGISTRY",
]

# str -> BaseSubAgent 子类的路由映射
# 键名即 spawn_subagent 工具的 subagent_type 参数值
SUBAGENT_REGISTRY: Dict[str, Type[BaseSubAgent]] = {
    "info_gatherer": InfoGathererSubAgent,
    "code_explorer": CodeExplorerSubAgent,
}
