"""OpenAI 客户端 - 支持 OpenAI 兼容接口"""

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion

from ..logging import get_running_log
from .base import LLMClient
from .types import (
    ChatResponseV2,
    ContentPart,
    DocumentPart,
    ImagePart,
    ReasoningArtifact,
    StreamChunk,
    TextPart,
    ToolCall,
    ToolDefinition,
    UnifiedMessage,
)

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
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: int | None = None,
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
        self.extra_call_params: dict[str, Any] = {
            k: v for k, v in kwargs.items() if k not in _META_KEYS
        }

        # 初始化 OpenAI 客户端
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        
        self.client = AsyncOpenAI(**client_kwargs)
    
    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs
    ) -> ChatCompletion:
        raise NotImplementedError("请使用 chat_completion_v2 方法，支持更丰富的功能和统一的输入输出格式。")

    # ─────────────────────────────────────────────────────────────────
    #  chat_completion_v2：协议无关的统一调用接口
    # ─────────────────────────────────────────────────────────────────

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
        api_params: dict[str, Any] = {
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
        merged_extras: dict[str, Any] = dict(self.extra_call_params)
        instance_extra_body = merged_extras.pop("extra_body", None)
        call_extra_body = kwargs.pop("extra_body", None)
        if instance_extra_body or call_extra_body:
            api_params["extra_body"] = {
                **(instance_extra_body or {}),
                **(call_extra_body or {}),
            }
        for k, v in merged_extras.items():
            api_params.setdefault(k, v)

        # 5. 透传额外参数（per-call 优先），过滤上游泄漏的非 API 参数
        _NON_API_KWARGS = {"stream_callback"}
        api_params.update({k: v for k, v in kwargs.items() if k not in _NON_API_KWARGS})

        # 6. 调用 OpenAI API
        try:
            response: ChatCompletion = await self.client.chat.completions.create(
                **api_params
            )
            return self._parse_openai_response(response)
        except Exception as e:
            error_msg = f"OpenAI API 调用失败 (v2): {e!s}"
            _rlog.error("core_service", f"[OpenAIClient] {error_msg}")
            raise RuntimeError(error_msg)

    # ─────────────────────────────────────────────────────────────────
    #  chat_completion_stream：流式 LLM 调用
    # ─────────────────────────────────────────────────────────────────

    async def chat_completion_stream(
        self,
        system: str | None,
        messages: list[UnifiedMessage],
        tool_list: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs
    ) -> "AsyncGenerator[StreamChunk, None]":
        """流式 LLM 调用接口（OpenAI SSE 实现）。

        使用 ``stream=True`` 发起 OpenAI Chat Completions 流式请求，
        逐 chunk 解析 SSE 事件并 yield 统一 StreamChunk。

        Yields:
            StreamChunk：包含 text_delta / tool_call_start|delta|end / finish
        """

        from .types import StreamChunk

        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        # 1. 构建 OpenAI messages
        openai_messages = self._build_openai_messages(system, messages)

        # 2. 构建 API 参数（与 chat_completion_v2 一致，多加 stream）
        api_params: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": temp,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tok is not None:
            api_params["max_tokens"] = max_tok

        # 3. 工具定义
        if tool_list and tool_choice != "none":
            api_params["tools"] = [
                self._convert_tool_definition_openai(t) for t in tool_list
            ]
            api_params["tool_choice"] = self._convert_tool_choice_openai(tool_choice)
        elif tool_list and tool_choice == "none":
            api_params["tool_choice"] = "none"

        # 4. 注入实例级配置参数
        merged_extras: dict[str, Any] = dict(self.extra_call_params)
        instance_extra_body = merged_extras.pop("extra_body", None)
        call_extra_body = kwargs.pop("extra_body", None)
        if instance_extra_body or call_extra_body:
            api_params["extra_body"] = {
                **(instance_extra_body or {}),
                **(call_extra_body or {}),
            }
        for k, v in merged_extras.items():
            api_params.setdefault(k, v)
        # 透传额外参数，过滤上游泄漏的非 API 参数
        _NON_API_KWARGS = {"stream_callback"}
        api_params.update({k: v for k, v in kwargs.items() if k not in _NON_API_KWARGS})

        # ── 聚合状态 ──────────────────────────────────────────────
        accumulated_text: str = ""
        # index → {id, name, arguments_str}
        tool_calls_acc: dict[int, dict[str, str]] = {}
        # index → 已被 yield tool_call_start 的 name
        _tc_started: set = set()
        parsed_tool_calls: list[ToolCall] = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        reasoning: ReasoningArtifact | None = None

        try:
            stream = await self.client.chat.completions.create(**api_params)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None

                # ── 文本增量 ────────────────────────────────────────
                if delta and delta.content:
                    accumulated_text += delta.content
                    yield StreamChunk(type="text_delta", text_delta=delta.content)

                # ── reasoning_content（DeepSeek R1 等模型的思考 token）─
                if delta:
                    rc = getattr(delta, "reasoning_content", None)
                    if not rc:
                        extras = getattr(delta, "model_extra", None) or {}
                        rc = extras.get("reasoning_content") or None
                    if rc:
                        yield StreamChunk(type="thinking_delta", thinking_delta=rc)

                # ── 工具调用增量 ────────────────────────────────────
                if delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tc_name = tc_delta.function.name if tc_delta.function else ""
                            tc_id = tc_delta.id or ""
                            tool_calls_acc[idx] = {
                                "id": tc_id,
                                "name": tc_name,
                                "arguments": "",
                            }
                            # 子类钩子：提取 provider 特有 delta 字段（如 Gemini extra_content）
                            tc_extra = self._extract_streaming_tool_call_delta_extra(tc_delta)
                            tool_calls_acc[idx].update(tc_extra)
                        elif tc_delta.id:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tool_calls_acc[idx]["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments

                        # 首次出现时发送 tool_call_start
                        if idx not in _tc_started:
                            _tc_started.add(idx)
                            yield StreamChunk(
                                type="tool_call_start",
                                tool_call_id=tool_calls_acc[idx]["id"],
                                tool_call_name=tool_calls_acc[idx]["name"],
                            )
                        # 参数增量
                        if tc_delta.function and tc_delta.function.arguments:
                            yield StreamChunk(
                                type="tool_call_delta",
                                tool_call_id=tool_calls_acc[idx]["id"],
                                tool_call_arguments_delta=tc_delta.function.arguments,
                            )

                # ── usage（final chunk with stream_options）───────────
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }

                # ── finish_reason ────────────────────────────────────
                if chunk.choices and chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

        except Exception as e:
            error_msg = f"OpenAI API 流式调用失败: {e!s}"
            _rlog.error("core_service", f"[OpenAIClient] {error_msg}")
            raise RuntimeError(error_msg)

        # ── 流结束后：发送完整的 tool_call_end ────────────────────
        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except (json.JSONDecodeError, AttributeError):
                args = {}
                _rlog.warning(
                    "core_service",
                    f"[OpenAIClient] 流式工具参数 JSON 解析失败: {tc['arguments']!r}",
                )
            parsed_tc = ToolCall(
                id=tc["id"],
                name=tc["name"],
                arguments=args,
                gemini_thought_signature=tc.pop("gemini_thought_signature", None),
            )
            parsed_tool_calls.append(parsed_tc)
            yield StreamChunk(
                type="tool_call_end",
                tool_call=parsed_tc,
                tool_call_id=tc["id"],
            )

        # ── 子类钩子：提取 reasoning（DeepSeek 等覆盖）───────────
        reasoning = None  # 基类在流式下暂不提取；子类可覆盖

        # ── 映射 stop_reason ──────────────────────────────────────
        if finish_reason == "tool_calls":
            stop_reason_str = "tool_use"
        elif finish_reason == "length":
            stop_reason_str = "max_tokens"
        else:
            stop_reason_str = finish_reason or "stop"

        yield StreamChunk(
            type="finish",
            stop_reason=stop_reason_str,
            usage=usage,
            reasoning=reasoning,
            produced_by_provider=self._PROVIDER_TAG,
            produced_by_model=self.model,
        )

    # ─────────────────────────────────────────────────────────────────
    #  私有：消息转换
    # ─────────────────────────────────────────────────────────────────

    def _build_openai_messages(
        self,
        system: str | None,
        messages: list[UnifiedMessage],
    ) -> list[dict[str, Any]]:
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
        result: list[dict[str, Any]] = []

        # system 消息
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            if msg.tool_results:
                # 工具执行结果：展开为多条 role=tool 消息
                for tr in msg.tool_results:
                    if tr.content_parts:
                        # 多模态工具结果：构建 content 数组
                        content_arr: list[dict[str, Any]] = []
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
                    tc_dict = {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    # 子类钩子：注入 provider 特有的 tool_call 字段（如 Gemini extra_content）
                    self._inject_tool_call_extra(tc_dict, tc)
                    openai_tool_calls.append(tc_dict)
                msg_dict: dict[str, Any] = {
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
                msg_dict_plain: dict[str, Any] = {"role": msg.role, "content": content}
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

    def _extract_reasoning(self, message: Any) -> ReasoningArtifact | None:
        """从 LLM 响应中提取多态 ReasoningArtifact。

        基类返回 None（纯 OpenAI 不暴露 reasoning artifacts 给客户端）。
        DeepSeek 等带思考字段的 dialect 子类应覆盖此方法，返回具体子类。
        """
        return None

    def _inject_reasoning(
        self, msg_dict: dict[str, Any], msg: UnifiedMessage
    ) -> None:
        """在构建出去的 assistant 消息 dict 中注入 reasoning 产物。

        基类不注入任何私有字段。需要回传思考产物的子类（如 DeepSeek）应覆盖：
        在严格的 provider 兼容性检查后向 ``msg_dict`` 写入相应字段。
        """
        return None

    @staticmethod
    def _extract_tool_call_extra(tc_raw: Any) -> dict[str, Any]:
        """从原始 tool_call 响应对象中提取 provider 特有字段。

        基类返回空 dict。子类（如 GeminiOpenAIClient）可覆盖以提取
        extra_content / thought_signature 等 dialect 特定数据。

        Returns:
            dict，可包含 ``gemini_thought_signature``（bytes）等键。
            调用方负责将返回值中的字段传入 :class:`ToolCall` 构造。
        """
        return {}

    def _inject_tool_call_extra(
        self, tc_dict: dict[str, Any], tc: "ToolCall"
    ) -> None:
        """在构建请求的 tool_call dict 时注入 provider 特有字段。

        基类 no-op。子类可覆盖以注入 extra_content 等方言字段。
        """
        return None

    @staticmethod
    def _extract_streaming_tool_call_delta_extra(tc_delta: Any) -> dict[str, Any]:
        """从流式 tool_call delta 中提取 provider 特有字段。

        基类返回空 dict。子类（如 GeminiOpenAIClient）可覆盖以提取
        extra_content / thought_signature 等 dialect 特定数据。
        返回值中的键会被合并到 ``tool_calls_acc[idx]`` 累积 dict 中。
        """
        return {}

    @staticmethod
    def _openai_content_part(part: ContentPart) -> dict[str, Any]:
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

    def _convert_tool_definition_openai(self, tool: ToolDefinition) -> dict[str, Any]:
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

    def _convert_tool_choice_openai(self, tool_choice: str | None) -> Any:
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
        reasoning: ReasoningArtifact | None = self._extract_reasoning(message)

        # 解析 tool_calls
        parsed_tool_calls: list[ToolCall] | None = None
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
                # 子类钩子：提取 provider 特有字段（如 Gemini extra_content）
                tc_extra = self._extract_tool_call_extra(tc)
                gemini_sig = tc_extra.pop("gemini_thought_signature", None)
                parsed_tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                        gemini_thought_signature=gemini_sig,
                    )
                )

        # 映射 stop_reason
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "stop"

        # 提取 usage
        usage: dict[str, int] = {}
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
            produced_by_model=self.model,
        )

    def get_info(self) -> dict[str, Any]:
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


def _openai_part(part: ContentPart) -> dict[str, Any]:
    return OpenAIClient._openai_content_part(part)
