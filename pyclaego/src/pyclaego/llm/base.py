"""LLM 客户端抽象基类"""

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from .types import ChatResponseV2, StreamChunk, ToolDefinition, UnifiedMessage


class LLMClient(ABC):
    """LLM 客户端抽象基类
    
    所有 LLM 客户端实现必须继承此类并实现抽象方法。
    """
    
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
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
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
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

    async def chat_completion_stream(
        self,
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs
    ) -> AsyncGenerator[StreamChunk, None]:
        """流式 LLM 调用接口（协议无关）。

        默认实现：调用 chat_completion_v2 并将结果包装为单个 chunk + finish。
        各 provider 子类应覆盖此方法，提供真正的逐 token 流式输出。

        Args:
            与 chat_completion_v2 相同的参数签名。

        Yields:
            StreamChunk：逐 token 的流式响应片段。聚合数据嵌入在 type="finish" 的
            最后一个 chunk 中（含 stop_reason / usage / reasoning）。
        """
        result = await self.chat_completion_v2(
            system=system,
            messages=messages,
            tool_list=tool_list,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            **kwargs
        )
        # 将文本作为单个 chunk 发送
        if result.text:
            yield StreamChunk(type="text_delta", text_delta=result.text)
        # 发送完成的 tool_calls
        if result.tool_calls:
            for tc in result.tool_calls:
                yield StreamChunk(type="tool_call_start", tool_call_name=tc.name, tool_call_id=tc.id)
                yield StreamChunk(type="tool_call_delta", tool_call_id=tc.id,
                                  tool_call_arguments_delta=json.dumps(tc.arguments, ensure_ascii=False))
                yield StreamChunk(type="tool_call_end", tool_call=tc, tool_call_id=tc.id)
        # finish（聚合数据嵌在 chunk 中）
        yield StreamChunk(
            type="finish",
            stop_reason=result.stop_reason,
            usage=result.usage,
            reasoning=result.reasoning,
            produced_by_provider=result.produced_by_provider,
            produced_by_model=result.produced_by_model,
        )

    @abstractmethod
    def get_info(self) -> dict[str, Any]:
        """获取客户端信息
        
        Returns:
            客户端配置信息字典，至少包含：
            - model: 模型名称
            - base_url: API 基础 URL
            - temperature: 温度参数
            - max_tokens: 最大 token 数
        """
        pass
