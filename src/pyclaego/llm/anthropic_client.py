"""Anthropic LLM 客户端 - 支持 Claude 系列模型"""

import os
from typing import Any, Dict, List, Optional

import anthropic
from anthropic.types import Message

from .base import LLMClient
from .types import (
    AnthropicThinkingBlocks,
    ContentPart,
    DocumentPart,
    ImagePart,
    TextPart,
    ToolCall,
    ToolDefinition,
    UnifiedMessage,
    ChatResponseV2,
)
from ..logging import get_running_log

_rlog = get_running_log()


class AnthropicClient(LLMClient):
    """Anthropic LLM 客户端 - 支持 Claude API
    
    功能：
    - 支持标准 Anthropic API
    - 支持自定义 base_url（兼容代理服务）
    - 异步调用
    - 继承 LLMClient 抽象基类

    子类可仅覆盖 :attr:`_PROVIDER_TAG` 以区分同为 Anthropic 协议但不同厂商的
    端点（如 Kimi Code、DeepSeek anthropic-compat）。thinking 块的 ``signature``
    与原始生成端点绑定，跨厂商重发必需以 vendor 粒度拦截。
    """

    #: 该客户端产出消息的 ``produced_by_provider`` 标签。
    #: 子类重写以区分供应商（如 ``"kimi_anthropic"`` / ``"deepseek_anthropic"``）。
    _PROVIDER_TAG: str = "anthropic"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        temperature: float = 0.7,
        max_tokens: Optional[int] = 8192,
        **kwargs,
    ):
        """初始化 Anthropic 客户端
        
        Args:
            api_key: API 密钥（默认从环境变量 ANTHROPIC_API_KEY 读取）
            base_url: API 基础 URL（可选，用于自定义服务）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens or 8192  # Anthropic 需要显式 max_tokens

        # 来自配置文件的额外调用参数（如 extra_body / reasoning_effort 等），
        # 在每次 chat_completion_v2 调用时合并到 api_params。
        # 排除 client 元数据键（不属于 Anthropic API 参数）。
        _META_KEYS = {"max_context_tokens", "api"}
        self.extra_call_params: Dict[str, Any] = {
            k: v for k, v in kwargs.items() if k not in _META_KEYS
        }

        # 初始化 Anthropic 客户端
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        
        self.client = anthropic.AsyncAnthropic(**client_kwargs)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Message:
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
        """协议无关的统一 LLM 调用接口（Anthropic 实现）

        将 UnifiedMessage 列表转换为 Anthropic messages 格式（system 作为顶层参数），
        将 ToolDefinition 列表转换为 Anthropic tools 格式（input_schema），
        将统一 tool_choice 字符串转换为 Anthropic 格式，
        并将 Anthropic Message 响应转换为统一 ChatResponseV2。

        Anthropic 特殊约束：
        - system 作为独立顶层参数，不混入 messages
        - messages 必须以 user 消息开头，且 user/assistant 严格交替
        - 工具结果合并为同一条 role=user 消息的 content 数组

        Args:
            system:      系统提示词（None 表示不传 system 消息）
            messages:    对话历史（UnifiedMessage 格式）
            tool_list:   可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature: 温度参数（覆盖实例默认值）
            max_tokens:  最大输出 token 数（覆盖实例默认值）
            tool_choice: 工具选择策略（"auto" | "none" | "<tool_name>"）
            **kwargs:    透传给 Anthropic API 的额外参数

        Returns:
            ChatResponseV2 统一响应对象
        """
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        # 1. 构建 Anthropic messages（tool_results 合并为 user content 数组）
        anthropic_messages = self._build_anthropic_messages(messages)

        # 2. 构建 API 参数
        api_params: Dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "temperature": temp,
            "max_tokens": max_tok,
        }

        # 3. system 作为顶层参数
        if system:
            api_params["system"] = system

        # 4. 工具定义（tool_choice="none" 时不传 tools 即可实现禁用）
        if tool_list and tool_choice != "none":
            api_params["tools"] = [
                self._convert_tool_definition_anthropic(t) for t in tool_list
            ]
            api_params["tool_choice"] = self._convert_tool_choice_anthropic(tool_choice)

        # 5. 注入实例级配置参数（如 extra_body / reasoning_effort 等）
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

        # 6. 透传额外参数（per-call 优先）
        api_params.update(kwargs)

        # 7. 调用 Anthropic API
        # 当 max_tokens > 8192 时，Anthropic SDK 要求使用流式调用
        # （非流式调用可能因 10 分钟超时被 SDK 端拦截）
        try:
            if max_tok > 8192:
                async with self.client.messages.stream(**api_params) as stream:
                    response: Message = await stream.get_final_message()
            else:
                response = await self.client.messages.create(**api_params)
            return self._parse_anthropic_response(response)
        except Exception as e:
            error_msg = f"Anthropic API 调用失败 (v2): {str(e)}"
            _rlog.error("core_service", f"[AnthropicClient] {error_msg}")
            raise RuntimeError(error_msg)

    # ─────────────────────────────────────────────────────────────────
    #  私有：消息转换
    # ─────────────────────────────────────────────────────────────────

    def _build_anthropic_messages(
        self, messages: List[UnifiedMessage]
    ) -> List[Dict[str, Any]]:
        """将 UnifiedMessage 列表转换为 Anthropic messages 格式

        转换规则：
        - UnifiedMessage(role=user, text)       → {"role": "user",      "content": text}
        - UnifiedMessage(role=assistant, text)  → {"role": "assistant", "content": text}
        - UnifiedMessage(role=assistant, tool_calls)
                                                → {"role": "assistant",
                                                   "content": [{"type":"tool_use","id":...,"name":...,"input":{...}}]}
        - UnifiedMessage(role=user, tool_results)
                                                → {"role": "user",
                                                   "content": [{"type":"tool_result","tool_use_id":...,"content":"..."}]}

        注意：
        - 若 assistant 消息同时含 text 和 tool_calls，两者均放入 content 数组
        - Anthropic 要求 messages 以 user 开头且 user/assistant 严格交替；
          若检测到违例会记录警告（不抛异常，由 API 校验兜底）
        """
        result: List[Dict[str, Any]] = []

        for msg in messages:
            if msg.tool_results:
                # 工具执行结果 → role=user，content 为 tool_result 数组
                content_blocks = []
                for tr in msg.tool_results:
                    if tr.content_parts:
                        # 多模态工具结果：content 为内容块数组
                        tool_content = []
                        for p in tr.content_parts:
                            tool_content.append(_anthropic_part(p))
                        # 确保有文本描述
                        if not any(isinstance(p, TextPart) for p in tr.content_parts):
                            tool_content.insert(0, {"type": "text", "text": tr.content})
                        content_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tr.tool_call_id,
                            "content": tool_content,
                        })
                    else:
                        content_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tr.tool_call_id,
                            "content": tr.content,
                        })
                result.append({"role": "user", "content": content_blocks})

            elif msg.tool_calls:
                # assistant 发起工具调用，content 为 tool_use block 数组
                content_blocks: List[Dict[str, Any]] = []

                # Anthropic 思考模式：thinking / redacted_thinking 块必须原样位于
                # 同一 assistant 消息的 content 开头（在 text / tool_use 之前），
                # 否则下一轮 API 会报错 400。
                # 跨 provider/model 切换后 signature 失效，必须跳过。
                if (
                    isinstance(msg.reasoning, AnthropicThinkingBlocks)
                    and msg.reasoning.blocks
                    and self._is_compatible_thinking(msg)
                ):
                    content_blocks.extend(msg.reasoning.blocks)

                # 若同时包含文本，先放文本 block
                if msg.text:
                    content_blocks.append({"type": "text", "text": msg.text})

                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })

                result.append({"role": "assistant", "content": content_blocks})

            else:
                # 普通文本消息（或多模态消息）
                if (
                    msg.role == "assistant"
                    and isinstance(msg.reasoning, AnthropicThinkingBlocks)
                    and msg.reasoning.blocks
                    and self._is_compatible_thinking(msg)
                ):
                    # 同上：思考块必须原样回传，与文本一起放入 content 数组
                    plain_blocks: List[Dict[str, Any]] = list(
                        msg.reasoning.blocks
                    )
                    if msg.content_parts:
                        plain_blocks.extend(_anthropic_part(p) for p in msg.content_parts)
                    elif msg.text:
                        plain_blocks.append({"type": "text", "text": msg.text})
                    result.append({"role": "assistant", "content": plain_blocks})
                elif msg.content_parts:
                    content: Any = [
                        _anthropic_part(p) for p in msg.content_parts
                    ]
                    result.append({"role": msg.role, "content": content})
                else:
                    result.append({"role": msg.role, "content": msg.text or ""})

        # 防御：合并相邻同角色消息（Anthropic 要求严格交替）
        result = self._coalesce_same_role_messages(result)

        # 校验 Anthropic 消息顺序约束：必须以 user 开头，且 user/assistant 交替
        self._validate_anthropic_message_order(result)

        return result

    def _is_compatible_thinking(self, msg: UnifiedMessage) -> bool:
        """判断消息中的 Anthropic thinking 块是否可安全回传给当前端点。

        Anthropic thinking 块携带与生成端点 + 模型绑定的 ``signature``，
        跨供应商或跨模型重发会被 API 拒绝。

        守卫规则：
        - 旧消息未打标（``produced_by_provider`` 为 None）默认允许，向后兼容。
        - ``produced_by_provider`` 必须严格等于当前子类的 :attr:`_PROVIDER_TAG`
          （同为 Anthropic 协议但 vendor 不同会被拦截）。
        - ``produced_by_model`` 如已记录，必须与当前 ``self.model`` 一致。
        """
        if msg.produced_by_provider is None:
            return True
        if msg.produced_by_provider != self._PROVIDER_TAG:
            return False
        # 同 provider 但模型变化时，signature 也会失效
        if msg.produced_by_model and msg.produced_by_model != self.model:
            return False
        return True

    @staticmethod
    def _coalesce_same_role_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并相邻同角色消息：把后一条的 content 追加到前一条。

        - 字符串 content 规范化为 ``[{"type":"text","text":...}]`` 再拼接。
        - 保留 block 顺序。
        - 不修改输入列表。
        """
        def _as_blocks(content: Any) -> List[Dict[str, Any]]:
            if isinstance(content, list):
                return list(content)
            if isinstance(content, str):
                return [{"type": "text", "text": content}] if content else []
            return [{"type": "text", "text": str(content)}]

        merged: List[Dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev_blocks = _as_blocks(merged[-1]["content"])
                curr_blocks = _as_blocks(msg["content"])
                merged[-1] = {
                    "role": msg["role"],
                    "content": prev_blocks + curr_blocks,
                }
            else:
                merged.append(dict(msg))
        return merged

    def _validate_anthropic_message_order(
        self, messages: List[Dict[str, Any]]
    ) -> None:
        """校验 Anthropic messages 的顺序约束，违例时记录警告

        Anthropic 要求：
        1. 第一条消息必须是 role=user
        2. user 和 assistant 消息必须严格交替
        """
        if not messages:
            return

        if messages[0]["role"] != "user":
            _rlog.warning(
                "core_service",
                "[AnthropicClient] Anthropic 要求 messages 第一条必须是 user 消息，"
                f"当前第一条为 role={messages[0]['role']!r}",
            )

        for i in range(1, len(messages)):
            prev_role = messages[i - 1]["role"]
            curr_role = messages[i]["role"]
            if prev_role == curr_role:
                _rlog.warning(
                    "core_service",
                    f"[AnthropicClient] Anthropic 要求 user/assistant 消息必须交替，"
                    f"检测到连续相同角色：index={i - 1}/{i} role={curr_role!r}",
                )

    # ─────────────────────────────────────────────────────────────────
    #  私有：工具定义转换
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _anthropic_content_part(part: ContentPart) -> Dict[str, Any]:
        """将单个 ContentPart 转换为 Anthropic content block 元素"""
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        elif isinstance(part, ImagePart):
            # ImagePart — Anthropic 只支持 base64，不支持 url
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.data,
                },
            }
        elif isinstance(part, DocumentPart):
            # DocumentPart — Anthropic 原生支持 document 类型
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.data,
                },
            }
        else:
            raise ValueError(f"Unsupported ContentPart type for Anthropic: {type(part)}")

    def _convert_tool_definition_anthropic(
        self, tool: ToolDefinition
    ) -> Dict[str, Any]:
        """将 ToolDefinition 转换为 Anthropic tools 格式

        Anthropic 格式：
        {
            "name": ...,
            "description": ...,
            "input_schema": {
                "type": "object",
                "properties": {param_name: {完整的 JSON Schema, 不含 required}},
                "required": [...]
            }
        }
        """
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    # ─────────────────────────────────────────────────────────────────
    #  私有：tool_choice 转换
    # ─────────────────────────────────────────────────────────────────

    def _convert_tool_choice_anthropic(
        self, tool_choice: Optional[str]
    ) -> Dict[str, Any]:
        """将统一 tool_choice 字符串转换为 Anthropic 协议格式

        统一语义 → Anthropic 格式：
        - None / "auto"      → {"type": "auto"}
        - "none"             → 不传 tools（调用方在此之前已处理，此处不应被调用）
        - "<tool_name>"      → {"type": "tool", "name": "<tool_name>"}
        """
        if tool_choice is None or tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "none":
            # 此路径通常不会到达（chat_completion_v2 已在传入 tools 之前拦截）
            return {"type": "auto"}
        return {"type": "tool", "name": tool_choice}

    # ─────────────────────────────────────────────────────────────────
    #  私有：响应解析
    # ─────────────────────────────────────────────────────────────────

    def _parse_anthropic_response(self, response: Message) -> ChatResponseV2:
        """将 Anthropic Message 转换为统一 ChatResponseV2

        解析规则：
        - content 中 type=text 的 block → text
        - content 中 type=tool_use 的 block → tool_calls
        - stop_reason="tool_use" → stop_reason="tool_use"
        - stop_reason="end_turn" / "stop_sequence" → stop_reason="stop"
        - stop_reason="max_tokens" → stop_reason="max_tokens"
        """
        text_parts: List[str] = []
        parsed_tool_calls: List[ToolCall] = []
        anthropic_provider_reasoning_blocks: List[Dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                if block.text:
                    text_parts.append(block.text)
            elif block.type == "tool_use":
                parsed_tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )
            elif block.type in ("thinking", "redacted_thinking"):
                # Anthropic 思考模式：原样保留 dict（含 signature），下一轮回传必须原样塞回
                if hasattr(block, "model_dump"):
                    anthropic_provider_reasoning_blocks.append(block.model_dump())
                else:
                    anthropic_provider_reasoning_blocks.append(dict(block))

        # 映射 stop_reason
        anthropic_stop = response.stop_reason or "end_turn"
        if anthropic_stop == "tool_use":
            stop_reason = "tool_use"
        elif anthropic_stop == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "stop"

        # 提取 usage
        usage: Dict[str, int] = {}
        if response.usage:
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }

        return ChatResponseV2(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=parsed_tool_calls if parsed_tool_calls else None,
            stop_reason=stop_reason,
            usage=usage,
            raw_response=response,
            reasoning=(
                AnthropicThinkingBlocks(blocks=anthropic_provider_reasoning_blocks)
                if anthropic_provider_reasoning_blocks
                else None
            ),
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
            "base_url": self.base_url or "https://api.anthropic.com",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }


def _anthropic_part(part: ContentPart) -> Dict[str, Any]:
    return AnthropicClient._anthropic_content_part(part)
