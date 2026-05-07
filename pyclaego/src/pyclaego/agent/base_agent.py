"""Agent 基类"""

from abc import ABC, abstractmethod
from typing import Any

from anthropic.types import Message

# 导入 LLM 响应类型
from openai.types.chat.chat_completion import ChatCompletion

from ..context import BaseContextHandlerV3
from ..task_manager import SessionTaskHandlerV2


class BaseAgent(ABC):
    """Agent 抽象基类
    
    所有 Agent 实现都必须继承此类并实现 process 方法
    """
    
    def __init__(self, config: dict[str, Any], session_id: str):
        """初始化 Agent

        Args:
            config: **完整** widget 配置字典（包含 ``agent`` / ``context`` /
                ``ps_metadata`` 等顶层键）。
                ``self.config`` 自动绑定到 ``config["agent"]`` 切片，便于
                现有子类继续按旧语义读取策略；``self.widget_config`` 保留
                完整字典，供 SpawnAgent → SpawnSubagentTool 等横向访问。
            session_id: 会话 ID
        """
        self.widget_config: dict[str, Any] = config or {}
        # 兼容旧语义：self.config = agent 配置切片
        self.config: dict[str, Any] = self.widget_config.get("agent") or {}
        self.agent_type = self.config.get("type", "unknown")
        self.session_id = session_id
        self._llm_id: str = ""  # 由子类 __init__ 通过 set_llm_id() 设置
        # 由 Widget.load() 通过 inject_widget_tools() 注入；默认空列表
        self.widget_tools: list = []

    def get_llm_id(self) -> str:
        """获取当前使用的 LLM provider ID

        Returns:
            当前 llm_id 字符串，若未设置则返回空字符串
        """
        return self._llm_id

    def set_llm_id(self, llm_id: str) -> None:
        """设置（覆盖）LLM provider ID

        供子类 __init__ 初始化，以及 /llm 命令动态切换时调用。

        Args:
            llm_id: LLM provider ID（对应 config.yaml llm.providers 中的键名）
        """
        self.config["llm"] = llm_id
        self._llm_id = llm_id

    def inject_widget_tools(self, tools: list) -> None:
        """由 Widget.load() 注入 widget-aware 工具实例（Phase 3.4）。

        默认行为：仅记录到 ``self.widget_tools``，不自动暴露给 LLM。
        子类如需把这些工具加入 ``tool_list`` 并参与 tool_use 调用，应在
        自身的 ``process_v2`` 中合并并路由 ``execute()``。
        """
        self.widget_tools = list(tools or [])
    
    @abstractmethod
    async def process(
        self,
        user_message: dict[str, Any],
        context_handler,  # BaseContextHandler 类型
        user_id: str,
        **kwargs
    ) -> str:
        """处理用户消息并返回 Agent 响应

        Args:
            user_message: 用户输入消息字典，包含以下字段：
                - content (str):   用户输入文字（必填）
                - role (str):      消息角色，固定为 "user"
                - user_id (str):   用户 ID
                - timestamp (str): ISO 格式时间戳
                - type (str):      消息类型，固定为 "user"
            context_handler: 上下文处理器（来自 context 模块）
            user_id: 用户 ID

        Returns:
            Agent 的响应文本
        """
        pass

    @abstractmethod
    async def process_v2(
        self,
        user_message: dict[str, Any],
        context_handler: "BaseContextHandlerV3",
        session_task_handler: "SessionTaskHandlerV2",
        **kwargs
    ) -> str:
        """处理用户消息并返回 Agent 响应（异步版本）

        Args:
            user_message: 用户输入消息字典
            context_handler: 上下文处理器（来自 context 模块）
            user_id: 用户 ID
            task_handler: 任务处理器（来自 task_manager 模块）

        Returns:
            Agent 的响应文本
        """
        ...
    
    @staticmethod
    def extract_response_content(response: str | ChatCompletion | Message) -> str:
        """提取 LLM 响应内容
        
        支持三种响应格式：
        1. 字符串：直接返回
        2. OpenAI ChatCompletion：从 choices[0].message.content 提取
        3. Anthropic Message：从 content[0].text 提取
        
        Args:
            response: LLM 响应对象（可能是字符串、OpenAI 或 Anthropic 格式）
            
        Returns:
            提取的文本内容
            
        Examples:
            >>> # 字符串格式
            >>> content = BaseAgent.extract_response_content("Hello")
            >>> print(content)  # "Hello"
            
            >>> # OpenAI 格式
            >>> openai_response = ChatCompletion(...)
            >>> content = BaseAgent.extract_response_content(openai_response)
            
            >>> # Anthropic 格式
            >>> anthropic_response = Message(...)
            >>> content = BaseAgent.extract_response_content(anthropic_response)
        """
        # 情况 1: 字符串类型
        if isinstance(response, str):
            return response
        
        # 情况 2: OpenAI ChatCompletion 格式
        if isinstance(response, ChatCompletion):
            if response.choices and len(response.choices) > 0:
                return response.choices[0].message.content or ""
            return ""
        
        # 情况 3: Anthropic Message 格式
        if isinstance(response, Message):
            if response.content and len(response.content) > 0:
                first_block = response.content[0]
                # Anthropic 的 content 是 ContentBlock 列表，通常第一个是 TextBlock
                if hasattr(first_block, 'text'):
                    return first_block.text
                return str(first_block)
            return ""
        
        # 未知类型，尝试转换为字符串
        return str(response)
    
    def get_info(self) -> dict[str, Any]:
        """获取 Agent 信息
        
        Returns:
            Agent 信息字典
        """
        return {
            "agent_type": self.agent_type,
            "config": self.config
        }
