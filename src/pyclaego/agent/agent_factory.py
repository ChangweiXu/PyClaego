"""Agent 工厂 - 根据配置创建 Agent 实例"""

from pathlib import Path
from typing import Dict, Any, Type
from .base_agent import BaseAgent
from .simple_agent import SimpleAgent  # internal base class; not user-facing
from .spawn_agent import SpawnAgent
from ..logging import get_running_log

_rlog = get_running_log()


class AgentFactory:
    """Agent 工厂类
    
    功能：
    - 根据配置动态创建 Agent
    - 支持注册自定义 Agent 类型
    - 提供统一的创建接口
    """
    
    # Agent 类型注册表
    _agent_registry: Dict[str, Type[BaseAgent]] = {
        "spawn": SpawnAgent,
    }

    @classmethod
    def create_agent(cls, agent_config: Dict[str, Any], session_id: str) -> BaseAgent:
        """创建 Agent 实例

        当策略配置中 max_concurrent_subagents > 0 时，自动创建 SpawnAgent（继承
        SimpleAgent），并将 AgentFactory.create_subagent 注入为其 _subagent_handler。
        调用方（Session 层）无需感知此变化，入参签名保持不变。
        
        Args:
            agent_config: Agent 配置字典，必须包含 "type" 字段
            session_id:   当前会话 ID
            
        Returns:
            Agent 实例
            
        Raises:
            ValueError: 如果 Agent 类型未注册
        """
        agent_type = agent_config.get("type")

        if not agent_type:
            raise ValueError("Agent 配置必须包含 'type' 字段")

        agent_class = cls._agent_registry.get(agent_type)

        if not agent_class:
            raise ValueError(
                f"未知的 Agent 类型: {agent_type}。"
                f"已注册的类型: {list(cls._agent_registry.keys())}"
            )

        # ── 检查是否启用子 Agent 能力 ────────────────────────────────────
        strategy_config = agent_config.get(agent_type, {})
        max_concurrent = strategy_config.get("max_concurrent_subagents", 0)

        # TODO 这里的判断方式把 max_concurrent 和 SpawnAgent 耦合，不优雅
        if max_concurrent > 0:
            from .spawn_agent import SpawnAgent
            _rlog.info(
                "core_service",
                f"[AgentFactory] 创建 SpawnAgent (agent_type={agent_type}, "
                f"max_concurrent_subagents={max_concurrent})",
            )
            agent = SpawnAgent(agent_config, session_id)
            # 将 create_subagent 注入为 _subagent_handler，不污染 agent_config
            agent.inject_subagent_handler(cls.create_subagent)
            return agent

        _rlog.info("core_service", f"[AgentFactory] 创建 Agent: {agent_type}")
        return agent_class(agent_config, session_id)

    @classmethod
    def create_subagent(
        cls,
        subagent_type: str,
        session_id: str,
        subagent_id: str,
        workspace_path: Path,
        base_config: Dict[str, Any],
    ) -> BaseAgent:
        """从 SUBAGENT_REGISTRY 查找子 Agent 类并实例化

        只允许创建继承了 BaseSubAgent 的类，防止误注册普通 Agent。

        Args:
            subagent_type:  子 Agent 类型（SUBAGENT_REGISTRY 的键名）
            session_id:     父会话 ID
            subagent_id:    子 Agent 唯一标识（由 SpawnSubagentTool 自动生成）
            workspace_path: 子 Agent 独立工作目录
            base_config:    主 Agent 配置（含 llm 等字段）

        Returns:
            BaseSubAgent 子类实例

        Raises:
            ValueError: subagent_type 未注册
            TypeError:  注册类未继承 BaseSubAgent
        """
        from .subagent import SUBAGENT_REGISTRY, BaseSubAgent

        sub_class = SUBAGENT_REGISTRY.get(subagent_type)
        if sub_class is None:
            raise ValueError(
                f"未知的子 Agent 类型: '{subagent_type}'。"
                f"已注册的类型: {list(SUBAGENT_REGISTRY.keys())}"
            )
        if not issubclass(sub_class, BaseSubAgent):
            raise TypeError(
                f"子 Agent 类型 '{subagent_type}' 对应的类 {sub_class.__name__} "
                f"未继承 BaseSubAgent，拒绝创建"
            )

        _rlog.info(
            "core_service",
            f"[AgentFactory] 创建子Agent (type={subagent_type}, id={subagent_id})",
        )
        return sub_class(base_config, session_id, subagent_id, workspace_path)
    
    @classmethod
    def register_agent(cls, agent_type: str, agent_class: Type[BaseAgent]) -> None:
        """注册自定义 Agent 类型
        
        Args:
            agent_type: Agent 类型名称
            agent_class: Agent 类
        """
        if agent_type in cls._agent_registry:
            _rlog.warning("core_service",(f"[AgentFactory] 警告: Agent 类型 '{agent_type}' 已存在，将被覆盖"))

        cls._agent_registry[agent_type] = agent_class
        _rlog.info("core_service",(f"[AgentFactory] 已注册 Agent 类型: {agent_type}"))
    
    @classmethod
    def list_available_agents(cls) -> list:
        """列出所有可用的 Agent 类型
        
        Returns:
            Agent 类型列表
        """
        return list(cls._agent_registry.keys())
