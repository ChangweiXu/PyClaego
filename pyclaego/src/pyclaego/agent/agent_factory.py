"""Agent 工厂 - 根据配置创建 Agent 实例"""

from pathlib import Path
from typing import Any

from ..logging import get_running_log
from ..tool_agent import ToolAgentConfig, resolve_profile
from .agent_pipeline import PipelineAgent
from .base_agent import BaseAgent
from .subagent.universal_subagent import UniversalSubAgent

# from .think_agent import ThinkAgent
# from .echo_agent import EchoAgent
# from .simple_agent import SimpleAgent
# from .spawn_agent import SpawnAgent

_rlog = get_running_log()


class AgentFactory:
    """Agent 工厂类
    
    功能：
    - 根据配置动态创建 Agent
    - 支持注册自定义 Agent 类型
    - 提供统一的创建接口
    """
    
    # Agent 类型注册表
    _agent_registry: dict[str, type[BaseAgent]] = {
        # "simple": SimpleAgent,
        # "think": ThinkAgent,
        # "echo": EchoAgent,
        # "spawn": SpawnAgent,
        "pipeline": PipelineAgent,
    }

    @classmethod
    def create_agent(cls, widget_config: dict[str, Any], session_id: str) -> BaseAgent:
        """创建 Agent 实例

        当策略配置中 max_concurrent_subagents > 0 时，自动创建 SpawnAgent（继承
        SimpleAgent），并将 AgentFactory.create_subagent 注入为其 _subagent_handler。
        调用方（Widget 层）传入 **完整** widget 配置；本工厂从中提取 agent 切片
        以判断类型/策略，但传给 Agent 构造器的仍是完整 widget_config，便于 Agent
        访问 ps_metadata 等横切字段。

        Args:
            widget_config: **完整** widget 配置字典（包含 agent / context / ...）
            session_id:    当前会话 ID

        Returns:
            Agent 实例

        Raises:
            ValueError: 如果 Agent 类型未注册
        """
        agent_config = widget_config.get("agent") or {}
        agent_type = agent_config.get("type")

        if not agent_type:
            raise ValueError("widget_config['agent'] 必须包含 'type' 字段")

        agent_class = cls._agent_registry.get(agent_type)

        if not agent_class:
            raise ValueError(
                f"未知的 Agent 类型: {agent_type}。"
                f"已注册的类型: {list(cls._agent_registry.keys())}"
            )

        # ── 检查是否启用子 Agent 能力 ────────────────────────────────────
        strategy_config = agent_config.get(agent_type, {})
        max_concurrent = strategy_config.get("max_concurrent_subagents", 0)

        if max_concurrent > 0:
            _rlog.info(
                "core_service",
                f"[AgentFactory] 创建 {agent_type} Agent (支持子Agent派遣, "
                f"max_concurrent_subagents={max_concurrent})",
            )
            agent = agent_class(widget_config, session_id)
            # 将 create_subagent 注入为 _subagent_handler（不污染 widget_config）
            inject = getattr(agent, "inject_subagent_handler", None)
            if callable(inject):
                inject(cls.create_subagent)
            else:
                _rlog.warning(
                    "core_service",
                    f"[AgentFactory] {agent_type} Agent 不支持子Agent派遣，"
                    f"子Agent配置将被忽略",
                )
            return agent

        _rlog.info("core_service", f"[AgentFactory] 创建 Agent: {agent_type}")
        return agent_class(widget_config, session_id)

    @classmethod
    def create_subagent(
        cls,
        subagent_type: str,
        session_id: str,
        subagent_id: str,
        workspace_path: Path,
        base_config: dict[str, Any],
    ) -> BaseAgent:
        """创建子 Agent 实例（优先使用 ToolAgentConfig 注册表，回退到旧 SUBAGENT_REGISTRY）。

        查找顺序：
            1. SUBAGENT_PROFILES（ToolAgentConfig 注册表）→ 创建 UniversalSubAgent
            2. SUBAGENT_REGISTRY（旧架构，类驱动）→ 创建对应的 BaseSubAgent 子类

        Args:
            subagent_type:  子 Agent 类型标识
            session_id:     父会话 ID
            subagent_id:    子 Agent 唯一标识
            workspace_path: 子 Agent 独立工作目录
            base_config:    主 Agent 配置（含 llm 等字段）

        Returns:
            UniversalSubAgent 或 BaseSubAgent 子类实例

        Raises:
            ValueError: subagent_type 未在任何注册表中找到
        """
        # ── 1. 优先尝试 ToolAgentConfig 注册表 ──────────────────────────
        try:
            profile = resolve_profile(subagent_type, base_config)

            # ── 1a. subagent_type 路由 ─────────────────────────────
            agent_class = cls._resolve_subagent_class(profile)

            _rlog.info(
                "core_service",
                f"[AgentFactory] 通过 ToolAgentConfig 创建 {agent_class.__name__} "
                f"(type={subagent_type}, id={subagent_id}, "
                f"strategy={profile.context_strategy})",
            )
            return agent_class(
                config=base_config,
                session_id=session_id,
                subagent_id=subagent_id,
                workspace_path=workspace_path,
                profile=profile,
            )
        except KeyError:
            raise ValueError(
                f"未知的子 Agent 类型: '{subagent_type}'。"
                f"未在 ToolAgentConfig 注册表中找到。"
            )
            # pass  # 回退到旧注册表

        # # ── 2. 回退到 SUBAGENT_REGISTRY（旧架构兼容） ──────────────────
        # from .subagent import SUBAGENT_REGISTRY, BaseSubAgent

        # sub_class = SUBAGENT_REGISTRY.get(subagent_type)
        # if sub_class is None:
        #     available_profiles = list(SUBAGENT_PROFILES.keys())
        #     available_classes = list(SUBAGENT_REGISTRY.keys())
        #     all_available = sorted(set(available_profiles + available_classes))
        #     raise ValueError(
        #         f"未知的子 Agent 类型: '{subagent_type}'。"
        #         f"可用类型: {all_available}"
        #     )
        # if not issubclass(sub_class, BaseSubAgent):
        #     raise TypeError(
        #         f"子 Agent 类型 '{subagent_type}' 对应的类 {sub_class.__name__} "
        #         f"未继承 BaseSubAgent，拒绝创建"
        #     )

        # _rlog.info(
        #     "core_service",
        #     f"[AgentFactory] 创建子Agent (type={subagent_type}, id={subagent_id})",
        # )
        # return sub_class(base_config, session_id, subagent_id, workspace_path)
    
    @classmethod
    def _resolve_subagent_class(
        cls,
        profile: "ToolAgentConfig",
    ) -> type[UniversalSubAgent]:
        """根据 ToolAgentConfig.subagent_type 解析子 Agent 类。

        Args:
            profile: 已解析的 ToolAgentConfig

        Returns:
            子 Agent 类（当前仅 UniversalSubAgent）
        """

        if profile.subagent_type != "universal":
            _rlog.info(
                "core_service",
                f"[AgentFactory] ToolAgentConfig.subagent_type="
                f"'{profile.subagent_type}' → 默认使用 UniversalSubAgent",
            )

        return UniversalSubAgent

    @classmethod
    def register_agent(cls, agent_type: str, agent_class: type[BaseAgent]) -> None:
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
