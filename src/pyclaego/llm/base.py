"""LLM 客户端抽象基类"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .types import ToolDefinition, UnifiedMessage, ChatResponseV2


class LLMClient(ABC):
    """LLM 客户端抽象基类
    
    所有 LLM 客户端实现必须继承此类并实现抽象方法。
    """
    
    @abstractmethod
    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Any:
        """调用聊天完成 API（旧版接口，保持不变）
        
        Args:
            messages: 消息列表（标准格式：[{"role": "user/assistant/system", "content": "..."}]）
            temperature: 温度参数（可选，覆盖默认值）
            max_tokens: 最大 token 数（可选，覆盖默认值）
            **kwargs: 其他 API 参数
            
        Returns:
            LLM API 响应对象（具体类型取决于实现）
        """
        pass

    @abstractmethod
    async def chat_completion_v2(
        self,
        system: Optional[str],
        messages: List[UnifiedMessage],
        tool_list: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> ChatResponseV2:
        """协议无关的统一 LLM 调用接口（新版接口）

        调用方无需区分底层协议（OpenAI / Anthropic），由各子类自行完成格式转换。

        Args:
            system:      系统提示词（None 表示不传 system 消息）
            messages:    对话历史，使用 UnifiedMessage 格式
            tool_list:   可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature: 温度参数（覆盖实例默认值）
            max_tokens:  最大输出 token 数（覆盖实例默认值）
            tool_choice: 工具选择策略：
                         - None / "auto"：LLM 自行决定是否调用工具
                         - "none"：禁用工具调用
                         - "<tool_name>"：强制调用指定工具
            **kwargs:    透传给底层 API 的额外参数（如 response_format、metadata 等）

        Returns:
            ChatResponseV2：统一响应对象，包含 text、tool_calls、stop_reason、usage、raw_response
        """
        pass
    
    @abstractmethod
    def get_info(self) -> Dict[str, Any]:
        """获取客户端信息
        
        Returns:
            客户端配置信息字典，至少包含：
            - model: 模型名称
            - base_url: API 基础 URL
            - temperature: 温度参数
            - max_tokens: 最大 token 数
        """
        pass
