"""OpenAI 客户端 - 支持 OpenAI 兼容接口"""

import json
import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion

from .base import LLMClient
from .types import (
    ContentPart,
    DocumentPart,
    ImagePart,
    ReasoningArtifact,
    TextPart,
    ToolCall,
    ToolDefinition,
    UnifiedMessage,
    ChatResponseV2,
)
from ..logging import get_running_log

_rlog = get_running_log()


class OpenAIClient(LLMClient):
    """OpenAI 客户端 - 支持自定义 base_url 的 OpenAI 格式调用
    
    功能：
    - 支持标准 OpenAI API
    - 支持自定义 base_url（兼容其他 OpenAI 格式 API）
    - 异步调用
    - 继承 LLMClient 抽象基类
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ):
        """初始化 OpenAI 客户端
        
        Args:
            api_key: API 密钥（默认从环境变量 OPENAI_API_KEY 读取）
            base_url: API 基础 URL（可选，用于自定义服务）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        # 来自配置文件的额外调用参数（如 extra_body / reasoning_effort 等），
        # 在每次 chat_completion_v2 调用时合并到 api_params。
        # 排除 client 元数据键（不属于 OpenAI API 参数）。
        _META_KEYS = {"max_context_tokens", "api"}
        self.extra_call_params: Dict[str, Any] = {
            k: v for k, v in kwargs.items() if k not in _META_KEYS
        }

        # 初始化 OpenAI 客户端
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        
        self.client = AsyncOpenAI(**client_kwargs)
    
    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> ChatCompletion:
        raise NotImplementedError("请使用 chat_completion_v2 方法，支持更丰富的功能和统一的输入输出格式。")

    # ─────────────────────────────────────────────────────────────────
    #  chat_completion_v2：协议无关的统一调用接口
    # ─────────────────────────────────────────────────────────────────

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
        """协议无关的统一 LLM 调用接口（OpenAI 实现）

        将 UnifiedMessage 列表转换为 OpenAI messages 格式，
        将 ToolDefinition 列表转换为 OpenAI tools 格式，
        将统一 tool_choice 字符串转换为 OpenAI 格式，
        并将 ChatCompletion 响应转换为统一 ChatResponseV2。

        Args:
            system:      系统提示词（None 表示不传 system 消息）
            messages:    对话历史（UnifiedMessage 格式）
            tool_list:   可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature: 温度参数（覆盖实例默认值）
            max_tokens:  最大输出 token 数（覆盖实例默认值）
            tool_choice: 工具选择策略（"auto" | "none" | "<tool_name>"）
            **kwargs:    透传给 OpenAI API 的额外参数

        Returns:
            ChatResponseV2 统一响应对象
        """
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        # 1. 构建 OpenAI messages
        openai_messages = self._build_openai_messages(system, messages)

        # 2. 构建 API 参数
        api_params: Dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": temp,
        }

        # 【2026年04月10日22:00:32移除】经常会出现报错: 
        #  "this model is not supported MaxTokens, please use MaxCompletionTokens"
        # if max_tok is not None:
        #     api_params["max_tokens"] = max_tok

        # 3. 工具定义（仅在 tool_list 非空且 tool_choice != "none" 时附加）
        if tool_list and tool_choice != "none":
            api_params["tools"] = [
                self._convert_tool_definition_openai(t) for t in tool_list
            ]
            api_params["tool_choice"] = self._convert_tool_choice_openai(tool_choice)
        elif tool_list and tool_choice == "none":
            # 明确禁用工具
            api_params["tool_choice"] = "none"

        # 4. 注入实例级配置参数（如 extra_body / reasoning_effort 等）
        #    per-call kwargs 优先于实例级配置；extra_body 走浅合并
        merged_extras: Dict[str, Any] = dict(self.extra_call_params)
        instance_extra_body = merged_extras.pop("extra_body", None)
        call_extra_body = kwargs.pop("extra_body", None)
        if instance_extra_body or call_extra_body:
            api_params["extra_body"] = {
                **(instance_extra_body or {}),
                **(call_extra_body or {}),
            }
        for k, v in merged_extras.items():
            api_params.setdefault(k, v)

        # 5. 透传额外参数（per-call 优先）
        api_params.update(kwargs)

        # 6. 调用 OpenAI API
        try:
            response: ChatCompletion = await self.client.chat.completions.create(
                **api_params
            )
            return self._parse_openai_response(response)
        except Exception as e:
            error_msg = f"OpenAI API 调用失败 (v2): {str(e)}"
            _rlog.error("core_service", f"[OpenAIClient] {error_msg}")
            raise RuntimeError(error_msg)

    # ─────────────────────────────────────────────────────────────────
    #  私有：消息转换
    # ─────────────────────────────────────────────────────────────────

    def _build_openai_messages(
        self,
        system: Optional[str],
        messages: List[UnifiedMessage],
    ) -> List[Dict[str, Any]]:
        """将 system + UnifiedMessage 列表转换为 OpenAI messages 格式

        转换规则：
        - system                          → {"role": "system", "content": system}
        - UnifiedMessage(role, text)      → {"role": role, "content": text}
        - UnifiedMessage(assistant, tool_calls)
                                          → {"role": "assistant", "content": None,
                                             "tool_calls": [...]}
        - UnifiedMessage(user, tool_results)
                                          → 多条 {"role": "tool", "tool_call_id": ..., "content": ...}
                                            （每个 tool_result 展开为独立消息）
        """
        result: List[Dict[str, Any]] = []

        # system 消息
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            if msg.tool_results:
                # 工具执行结果：展开为多条 role=tool 消息
                for tr in msg.tool_results:
                    if tr.content_parts:
                        # 多模态工具结果：构建 content 数组
                        content_arr: List[Dict[str, Any]] = []
                        for p in tr.content_parts:
                            if isinstance(p, DocumentPart):
                                # OpenAI 不原生支持文档类型，降级为文本
                                content_arr.append({"type": "text", "text": tr.content})
                                break
                            else:
                                content_arr.append(_openai_part(p))
                        # 确保有文本描述
                        if not any(c.get("type") == "text" for c in content_arr):
                            content_arr.insert(0, {"type": "text", "text": tr.content})
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr.tool_call_id,
                            "content": content_arr,
                        })
                    else:
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr.tool_call_id,
                            "content": tr.content,
                        })
            elif msg.tool_calls:
                # assistant 发起工具调用
                openai_tool_calls = []
                for tc in msg.tool_calls:
                    openai_tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    })
                msg_dict: Dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.text,  # 可为 None 或同时包含文本
                    "tool_calls": openai_tool_calls,
                }
                # 子类钩子：注入 reasoning_content 等 dialect 私有字段（如 DeepSeek）。
                # 基类 OpenAIClient 是 no-op，保持纯 OpenAI Chat Completions 协议。
                self._inject_reasoning(msg_dict, msg)
                result.append(msg_dict)
            else:
                # 普通文本消息（或多模态消息）
                if msg.content_parts:
                    content: Any = [
                        _openai_part(p) for p in msg.content_parts
                    ]
                else:
                    content = msg.text or ""
                msg_dict_plain: Dict[str, Any] = {"role": msg.role, "content": content}
                if msg.role == "assistant":
                    self._inject_reasoning(msg_dict_plain, msg)
                result.append(msg_dict_plain)

        return result

    # ─────────────────────────────────────────────────────────────────
    #  子类可重写的 dialect 钩子（基类全部 no-op，保持纯 OpenAI 协议）
    # ─────────────────────────────────────────────────────────────────

    #: 标记此 client 产出的消息所属 provider，用于跨 provider 切换守卫。
    #: 子类（如 DeepSeekClient）通过覆盖此类属性来打标。
    _PROVIDER_TAG: str = "openai"

    def _extract_reasoning(self, message: Any) -> Optional[ReasoningArtifact]:
        """从 LLM 响应中提取多态 ReasoningArtifact。

        基类返回 None（纯 OpenAI 不暴露 reasoning artifacts 给客户端）。
        DeepSeek 等带思考字段的 dialect 子类应覆盖此方法，返回具体子类。
        """
        return None

    def _inject_reasoning(
        self, msg_dict: Dict[str, Any], msg: UnifiedMessage
    ) -> None:
        """在构建出去的 assistant 消息 dict 中注入 reasoning 产物。

        基类不注入任何私有字段。需要回传思考产物的子类（如 DeepSeek）应覆盖：
        在严格的 provider 兼容性检查后向 ``msg_dict`` 写入相应字段。
        """
        return None

    @staticmethod
    def _openai_content_part(part: ContentPart) -> Dict[str, Any]:
        """将单个 ContentPart 转换为 OpenAI content 数组元素"""
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        if isinstance(part, DocumentPart):
            # OpenAI 不原生支持文档类型，降级为文本占位
            return {"type": "text", "text": f"[Document: {part.media_type}]"}
        if isinstance(part, ImagePart):
            # 图片支持 URL 或 base64，两者都转换为 data URL 形式
            if part.source_type == "url":
                return {"type": "image_url", "image_url": {"url": part.data}}
            # base64
            data_url = f"data:{part.media_type};base64,{part.data}"
            return {"type": "image_url", "image_url": {"url": data_url}}
        raise ValueError(f"Unsupported ContentPart type: {type(part)}")

    # ─────────────────────────────────────────────────────────────────
    #  私有：工具定义转换
    # ─────────────────────────────────────────────────────────────────

    def _convert_tool_definition_openai(self, tool: ToolDefinition) -> Dict[str, Any]:
        """将 ToolDefinition 转换为 OpenAI tools 格式

        OpenAI 格式：
        {
            "type": "function",
            "function": {
                "name": ...,
                "description": ...,
                "parameters": {
                    "type": "object",
                    "properties": {param_name: {完整的 JSON Schema, 不含 required}},
                    "required": [...]
                }
            }
        }
        """
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    # ─────────────────────────────────────────────────────────────────
    #  私有：tool_choice 转换
    # ─────────────────────────────────────────────────────────────────

    def _convert_tool_choice_openai(self, tool_choice: Optional[str]) -> Any:
        """将统一 tool_choice 字符串转换为 OpenAI 协议格式

        统一语义 → OpenAI 格式：
        - None / "auto"      → "auto"
        - "none"             → "none"
        - "<tool_name>"      → {"type": "function", "function": {"name": "<tool_name>"}}
        """
        if tool_choice is None or tool_choice == "auto":
            return "auto"
        if tool_choice == "none":
            return "none"
        # 强制调用指定工具
        return {"type": "function", "function": {"name": tool_choice}}

    # ─────────────────────────────────────────────────────────────────
    #  私有：响应解析
    # ─────────────────────────────────────────────────────────────────

    def _parse_openai_response(self, response: ChatCompletion) -> ChatResponseV2:
        """将 OpenAI ChatCompletion 转换为统一 ChatResponseV2

        - finish_reason == "tool_calls" → stop_reason = "tool_use"，tool_calls 不为 None
        - finish_reason == "stop"       → stop_reason = "stop"，text 不为 None
        - finish_reason == "length"     → stop_reason = "max_tokens"
        """
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason or "stop"

        # 通过子类钩子提取 reasoning 产物；基类返回 None（纯 OpenAI 不暴露）。
        reasoning: Optional[ReasoningArtifact] = self._extract_reasoning(message)

        # 解析 tool_calls
        parsed_tool_calls: Optional[List[ToolCall]] = None
        if finish_reason == "tool_calls" and message.tool_calls:
            parsed_tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                    _rlog.warning(
                        "core_service",
                        f"[OpenAIClient] 无法解析工具参数 JSON: {tc.function.arguments!r}",
                    )
                parsed_tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        # 映射 stop_reason
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "stop"

        # 提取 usage
        usage: Dict[str, int] = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return ChatResponseV2(
            text=message.content or None,   # 保留工具调用时的前置文字（如思考决策内容）
            tool_calls=parsed_tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw_response=response,
            reasoning=reasoning,
            produced_by_provider=self._PROVIDER_TAG,
            produced_by_model=getattr(response, "model", None) or self.model,
        )

    def get_info(self) -> Dict[str, Any]:
        """获取客户端信息
        
        Returns:
            客户端配置信息
        """
        return {
            "model": self.model,
            "base_url": self.base_url or "https://api.openai.com/v1",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }


def _openai_part(part: ContentPart) -> Dict[str, Any]:
    return OpenAIClient._openai_content_part(part)
