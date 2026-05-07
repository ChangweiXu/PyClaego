
"""Gemini LLM 客户端 - 支持 Google Gemini 系列模型"""

import base64
import json
import os
import uuid
from typing import Any

from google import genai
from google.genai import types as genai_types

from ..logging import get_running_log
from .base import LLMClient
from .types import (
    ChatResponseV2,
    ContentPart,
    DocumentPart,
    ImagePart,
    StreamChunk,
    TextPart,
    ToolCall,
    ToolDefinition,
    UnifiedMessage,
)

_rlog = get_running_log()


class GeminiClient(LLMClient):
    """Gemini LLM 客户端 - 支持 Google Gemini API

    功能：
    - 支持标准 Gemini Developer API
    - 支持自定义 base_url（通过 HttpOptions）
    - 异步调用
    - 继承 LLMClient 抽象基类
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ):
        """初始化新版 Gemini 客户端
        
        Args:
            api_key: Google Gemini API 密钥
            base_url: 自定义 API endpoint (可选)
            model: 模型名称 (如 gemini-2.5-flash, gemini-2.0-pro-exp)
            temperature: 默认温度参数
            max_tokens: 默认最大输出 token
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        # 初始化 Gemini 客户端
        client_kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["http_options"] = {
                "base_url": self.base_url
            }

        self.client = genai.Client(**client_kwargs)

    # ─────────────────────────────────────────────────────────────────
    #  chat_completion：旧版接口（OpenAI 格式消息）
    # ─────────────────────────────────────────────────────────────────

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> Any:
        raise NotImplementedError("旧版接口 chat_completion 未实现，请使用 chat_completion_v2")

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
        **kwargs,
    ) -> ChatResponseV2:
        """协议无关的统一 LLM 调用接口（Gemini 实现）

        将 UnifiedMessage 列表转换为 Gemini contents 格式，
        将 ToolDefinition 列表转换为 Gemini Tool/FunctionDeclaration 格式，
        将统一 tool_choice 字符串转换为 Gemini ToolConfig 格式，
        并将 GenerateContentResponse 转换为统一 ChatResponseV2。

        Gemini 特殊约束：
        - system 通过 GenerateContentConfig.system_instruction 传入
        - 角色名称：user → "user"，assistant → "model"，tool_results → "tool"
        - 禁用自动函数调用（automatic_function_calling），由上层统一管理工具执行

        Args:
            system:      系统提示词（None 表示不传）
            messages:    对话历史（UnifiedMessage 格式）
            tool_list:   可用工具列表（ToolDefinition 格式），None 表示不使用工具
            temperature: 温度参数（覆盖实例默认值）
            max_tokens:  最大输出 token 数（覆盖实例默认值）
            tool_choice: 工具选择策略（"auto" | "none" | "<tool_name>"）
            **kwargs:    透传给 Gemini API 的额外参数

        Returns:
            ChatResponseV2 统一响应对象
        """

        # 1. 构建配置参数 (新版 SDK 使用 GenerateContentConfig 统一管理)
        config_kwargs = {
            "temperature": temperature if temperature is not None else self.temperature,
            "max_output_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }

        if system:
            config_kwargs["system_instruction"] = system

        # 2. 转换工具定义
        if tool_list:
            gemini_tools = []
            for t in tool_list:
                gemini_tools.append(
                    genai_types.FunctionDeclaration(
                        name=t.name,
                        description=t.description,
                        parameters=t.parameters  # 兼容标准 JSON Schema
                    )
                )
            config_kwargs["tools"] = [genai_types.Tool(function_declarations=gemini_tools)]
            
            # 转换 tool_choice 策略
            if tool_choice == "none":
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.NONE
                    )
                )
            elif tool_choice and tool_choice not in ("auto", "none"):
                # 强制调用特定工具
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_choice]
                    )
                )
            else:
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.AUTO
                    )
                )

        config = genai_types.GenerateContentConfig(**config_kwargs)

        # 3. 转换消息格式
        contents = self._convert_messages(messages)

        try:
            # 4. 调用异步 API (新版 SDK 异步调用统一在 client.aio 下)
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
                **kwargs
            )

            # 5. 解析响应
            return self._parse_response(response)
        except Exception as e:
            error_msg = f"Gemini API 调用失败 (v2): {e!s}"
            _rlog.error("core_service", f"[GeminiClient] {error_msg}")
            raise RuntimeError(error_msg) from e

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
        """流式 LLM 调用接口（Gemini 实现）。

        使用 ``generate_content_stream()`` 发起 Gemini 流式请求，
        逐 chunk 解析并 yield 统一 StreamChunk。

        Yields:
            StreamChunk：包含 text_delta / tool_call_start|end / finish
        """

        # 1. 构建配置参数（与 chat_completion_v2 一致）
        config_kwargs: dict[str, Any] = {
            "temperature": temperature if temperature is not None else self.temperature,
            "max_output_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        if system:
            config_kwargs["system_instruction"] = system

        # 2. 工具定义
        if tool_list:
            gemini_tools = []
            for t in tool_list:
                gemini_tools.append(
                    genai_types.FunctionDeclaration(
                        name=t.name,
                        description=t.description,
                        parameters=t.parameters,
                    )
                )
            config_kwargs["tools"] = [genai_types.Tool(function_declarations=gemini_tools)]
            if tool_choice == "none":
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.NONE,
                    )
                )
            elif tool_choice and tool_choice not in ("auto", "none"):
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_choice],
                    )
                )
            else:
                config_kwargs["tool_config"] = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.AUTO,
                    )
                )

        config = genai_types.GenerateContentConfig(**config_kwargs)
        contents = self._convert_messages(messages)

        # ── 聚合状态 ──────────────────────────────────────────────
        accumulated_text: str = ""
        parsed_tool_calls: list[ToolCall] = []
        stop_reason_str: str = "stop"
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # 跟踪当前活跃的工具调用（Gemini 流式中一次只传一个 function_call）
        _active_tc_id: str | None = None
        _active_tc_name: str | None = None
        _active_tc_args: dict[str, Any] = {}

        try:
            async for chunk in await self.client.aio.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=config,
                **kwargs
            ):
                if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        # ── 文本增量 ────────────────────────────
                        if part.text:
                            accumulated_text += part.text
                            yield StreamChunk(type="text_delta", text_delta=part.text)

                        # ── 工具调用 ────────────────────────────
                        if part.function_call:
                            fc = part.function_call
                            call_id = f"call_{uuid.uuid4().hex[:8]}"
                            args = fc.args if isinstance(fc.args, dict) else dict(fc.args) if fc.args else {}

                            thought_sig: bytes | None = None
                            if hasattr(part, "thought_signature") and part.thought_signature:
                                thought_sig = bytes(part.thought_signature)

                            yield StreamChunk(
                                type="tool_call_start",
                                tool_call_id=call_id,
                                tool_call_name=fc.name,
                            )
                            tc = ToolCall(
                                id=call_id,
                                name=fc.name,
                                arguments=args,
                                gemini_thought_signature=thought_sig,
                            )
                            parsed_tool_calls.append(tc)
                            yield StreamChunk(
                                type="tool_call_end",
                                tool_call=tc,
                                tool_call_id=call_id,
                            )
                            stop_reason_str = "tool_use"

                # ── usage ──────────────────────────────────────
                if chunk.usage_metadata:
                    usage = {
                        "prompt_tokens": chunk.usage_metadata.prompt_token_count or 0,
                        "completion_tokens": chunk.usage_metadata.candidates_token_count or 0,
                        "total_tokens": chunk.usage_metadata.total_token_count or 0,
                    }

                # ── finish_reason ──────────────────────────────
                if chunk.candidates and chunk.candidates[0].finish_reason:
                    fr = chunk.candidates[0].finish_reason
                    if fr == genai_types.FinishReason.MAX_TOKENS:
                        stop_reason_str = "max_tokens"

        except Exception as e:
            error_msg = f"Gemini API 流式调用失败: {e!s}"
            _rlog.error("core_service", f"[GeminiClient] {error_msg}")
            raise RuntimeError(error_msg) from e

        yield StreamChunk(
            type="finish",
            stop_reason=stop_reason_str,
            usage=usage,
            reasoning=None,
            produced_by_provider="gemini",
            produced_by_model=self.model,
        )

    # ─────────────────────────────────────────────────────────────────
    #  内部辅助方法
    # ─────────────────────────────────────────────────────────────────

    def _convert_messages(self, messages: list[UnifiedMessage]) -> list[genai_types.Content]:
        """将 UnifiedMessage 转换为新版 SDK 的 Content 格式"""
        contents = []
        
        for msg in messages:
            # 角色映射：新版 SDK 严格区分 "user" 和 "model"
            role = "model" if msg.role == "assistant" else "user"
            parts = []

            # 1. 处理多模态/文本
            if msg.content_parts:
                for part in msg.content_parts:
                    parts.append(_gemini_part(part))
            elif msg.text:
                parts.append(genai_types.Part.from_text(text=msg.text))

            # 2. 处理助手发起的工具调用记录 (历史消息)
            if msg.tool_calls:
                # 仅当消息由 gemini 同模型产生时才回传 thought_signature；
                # 跨 provider/model 切换后 signature 失效，必须丢弃
                _sig_compatible = (
                    msg.produced_by_provider in (None, "gemini")
                    and (not msg.produced_by_model or msg.produced_by_model == self.model)
                )
                for tc in msg.tool_calls:
                    if tc.gemini_thought_signature and _sig_compatible:
                        # thinking 模型：必须保留 thought_signature，否则 API 报 400
                        parts.append(
                            genai_types.Part(
                                function_call=genai_types.FunctionCall(
                                    name=tc.name,
                                    args=tc.arguments,
                                ),
                                thought_signature=tc.gemini_thought_signature,
                            )
                        )
                    else:
                        parts.append(
                            genai_types.Part.from_function_call(
                                name=tc.name,
                                args=tc.arguments,
                            )
                        )

            # 3. 处理用户提交的工具执行结果
            if msg.tool_results:
                for tr in msg.tool_results:
                    # 将结果字符串尝试转为 dict 结构以符合规范
                    # 工具输出可能是 JSON 字符串，也可能是 Python repr 字符串（单引号格式）
                    response_dict = None
                    try:
                        response_dict = json.loads(tr.content)
                        if not isinstance(response_dict, dict):
                            response_dict = {"result": response_dict}
                    except (json.JSONDecodeError, ValueError):
                        import ast
                        try:
                            parsed = ast.literal_eval(tr.content)
                            if isinstance(parsed, dict):
                                response_dict = parsed
                            else:
                                response_dict = {"result": parsed}
                        except Exception:
                            response_dict = {"result": tr.content}

                    parts.append(
                        genai_types.Part.from_function_response(
                            name=tr.tool_name,
                            response=response_dict
                        )
                    )

                    # 多模态工具结果：追加 inline_data parts（图片/文档）
                    if tr.content_parts:
                        for p in tr.content_parts:
                            parts.append(_gemini_part(p))

            if parts:
                contents.append(genai_types.Content(role=role, parts=parts))

        return contents

    def _parse_response(self, response: genai_types.GenerateContentResponse) -> ChatResponseV2:
        """将新版响应对象转换为统一 ChatResponseV2"""

        text_content = ""
        tool_calls = []

        # 提取第一个候选结果 (Candidate) 中的 parts
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_content += part.text
                if part.function_call:
                    # Gemini 没有原生的工具调用 ID，生成一个 UUID 防止前端/管理层报错
                    call_id = f"call_{uuid.uuid4().hex[:8]}"

                    # 新版 SDK 的 args 通常已经是 dict
                    args = part.function_call.args
                    if not isinstance(args, dict):
                        # 兼容处理 Struct 转换
                        args = dict(args) if args else {}

                    # 保留 thought_signature（thinking 模型必须在历史消息中原样回传）
                    thought_sig: bytes | None = None
                    if hasattr(part, "thought_signature") and part.thought_signature:
                        thought_sig = bytes(part.thought_signature)

                    tool_calls.append(
                        ToolCall(
                            id=call_id,
                            name=part.function_call.name,
                            arguments=args,
                            gemini_thought_signature=thought_sig,
                        )
                    )

        # 映射停止原因
        stop_reason = "stop"
        if tool_calls:
            stop_reason = "tool_use"
        elif response.candidates:
            finish_reason = response.candidates[0].finish_reason
            if finish_reason == genai_types.FinishReason.MAX_TOKENS:
                stop_reason = "max_tokens"

        # Token 统计 (新版 SDK 提供了直观的 usage_metadata)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if response.usage_metadata:
            usage["prompt_tokens"] = response.usage_metadata.prompt_token_count or 0
            usage["completion_tokens"] = response.usage_metadata.candidates_token_count or 0
            usage["total_tokens"] = response.usage_metadata.total_token_count or 0

        return ChatResponseV2(
            text=text_content if text_content else None,
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            usage=usage,
            raw_response=response,
            produced_by_provider="gemini",
            produced_by_model=self.model,
        )

    # ─────────────────────────────────────────────────────────────────
    #  get_info
    # ─────────────────────────────────────────────────────────────────

    def get_info(self) -> dict[str, Any]:
        """获取客户端信息

        Returns:
            客户端配置信息
        """
        return {
            "model": self.model,
            "base_url": self.base_url or "https://generativelanguage.googleapis.com",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


def _gemini_part(part: ContentPart) -> genai_types.Part:
    """将单个 ContentPart 转换为 Gemini Part
    
    Args:
        part: ContentPart (TextPart、ImagePart 或 DocumentPart)
        
    Returns:
        genai_types.Part 对象
        
    Raises:
        ValueError: 当图片源类型为 url 时（Gemini 需要预先下载转为 base64）
        RuntimeError: 当遇到未知的 ContentPart 类型时
    """
    if isinstance(part, TextPart):
        return genai_types.Part.from_text(text=part.text)
    
    if isinstance(part, ImagePart):
        if part.source_type == "url":
            raise ValueError(
                "Gemini 不直接支持图片 URL，请在 Entry Point 层预先下载并转换为 base64"
            )
        # base64 → bytes
        try:
            image_bytes = base64.b64decode(part.data)
            return genai_types.Part.from_bytes(data=image_bytes, mime_type=part.media_type)
        except Exception as e:
            raise ValueError(f"解码 base64 图片数据失败: {e}")

    if isinstance(part, DocumentPart):
        # DocumentPart — Gemini 支持 inline_data（如 application/pdf）
        try:
            doc_bytes = base64.b64decode(part.data)
            return genai_types.Part.from_bytes(data=doc_bytes, mime_type=part.media_type)
        except Exception as e:
            raise ValueError(f"解码 base64 文档数据失败: {e}")

    # 未知类型
    raise RuntimeError(f"不支持的 ContentPart 类型: {type(part)}")
