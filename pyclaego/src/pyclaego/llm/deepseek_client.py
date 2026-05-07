"""DeepSeek 客户端 - OpenAI Chat Completions 兼容 + 思考模式 dialect

DeepSeek 在 OpenAI 协议之上扩展了 ``reasoning_content`` 字段，用于承载
``deepseek-reasoner`` 等思考模型的思维链产物。该 dialect 与纯 OpenAI 协议
不通用：

- **解析**：响应 ``message.reasoning_content`` 必须提取并随 ChatResponseV2 携带，
  以便上层持久化、展示及（在同一 tool_use 周期内）下一轮回传。
- **构建**：仅当历史 assistant 消息确实由 DeepSeek 系 dialect 产出时，
  才在请求中重发 ``reasoning_content``；跨 provider 切换或回放纯 OpenAI 历史
  时必须丢弃，否则真实 OpenAI / Anthropic / Gemini 等端点会忽略或报错。

实现策略：
继承 :class:`OpenAIClient`，仅通过三个钩子点定制 dialect 行为，避免重复
~60 行的消息构建循环。

未来若要支持 Moonshot Kimi Thinking 等同样使用 ``reasoning_content`` 的
dialect，只需 ``class MoonshotKimiThinkingClient(DeepSeekClient): _PROVIDER_TAG = "moonshot"``。
"""

import json
from collections.abc import AsyncGenerator
from typing import Any

from .openai_client import OpenAIClient
from .types import OpenAIReasoningContent, ReasoningArtifact, StreamChunk, UnifiedMessage


class DeepSeekClient(OpenAIClient):
    """DeepSeek 客户端 - 在 OpenAI 协议之上叠加 reasoning_content 往返。"""

    #: 产出消息打 ``produced_by_provider="deepseek"`` 标签，
    #: 用于跨 provider 切换时的兼容性守卫。
    _PROVIDER_TAG: str = "deepseek"

    # ─────────────────────────────────────────────────────────────────
    #  解析侧：抽取 reasoning_content
    # ─────────────────────────────────────────────────────────────────

    def _extract_reasoning(self, message: Any) -> ReasoningArtifact | None:
        """从 DeepSeek ``ChatCompletionMessage`` 中提取 reasoning_content。

        OpenAI SDK 把未声明的字段放进 ``model_extra``，老版本 SDK 也可能
        直接挂为属性 —— 两条路径都要尝试。
        """
        value: str | None = getattr(message, "reasoning_content", None)
        if not value:
            extras = getattr(message, "model_extra", None) or {}
            value = extras.get("reasoning_content") or None
        if not value:
            return None
        return OpenAIReasoningContent(content=value)

    # ─────────────────────────────────────────────────────────────────
    #  构建侧：注入 reasoning_content（带兼容性守卫）
    # ─────────────────────────────────────────────────────────────────

    def _inject_reasoning(
        self, msg_dict: dict[str, Any], msg: UnifiedMessage
    ) -> None:
        """将历史 assistant 消息的 reasoning_content 注入回请求体。

        守卫规则：
        - 不是 :class:`OpenAIReasoningContent` → 不注入。
        - ``produced_by_provider`` 为 None（旧消息未打标）→ 默认允许，保留
          向后兼容（既有持久化的 DeepSeek 会话不会失效）。
        - 否则必须等于 ``"deepseek"``，否则视为来自其他 provider 的产物，
          重发会被忽略或报错，必须丢弃。
        """
        artifact = msg.reasoning
        if not isinstance(artifact, OpenAIReasoningContent) or not artifact.content:
            return
        if not self._is_compatible(msg):
            return
        msg_dict["reasoning_content"] = artifact.content

    @classmethod
    def _is_compatible(cls, msg: UnifiedMessage) -> bool:
        """判断历史消息的 reasoning_content 是否可安全回传给当前 client。"""
        if msg.produced_by_provider is None:
            return True
        return msg.produced_by_provider == cls._PROVIDER_TAG

    # ─────────────────────────────────────────────────────────────────
    #  流式：带 reasoning_content 提取的流式调用
    # ─────────────────────────────────────────────────────────────────

    async def chat_completion_stream(
        self,
        system: str | None,
        messages: list,
        tool_list: list | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        **kwargs
    ) -> "AsyncGenerator[StreamChunk, None]":
        """流式 LLM 调用（DeepSeek dialect：附加 reasoning_content 提取）。

        在父类 OpenAI 流式实现的基础上，从每个 chunk 的 delta 中提取
        ``reasoning_content``，累计为完整的思考产物，在 finish chunk 中
        随 :class:`OpenAIReasoningContent` 一并返回。
        """
        accumulated_reasoning: str = ""

        # 委托父类构建参数并启动流（复用完整的 OpenAI SSE 解析）
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        from .types import ToolCall
        openai_messages = self._build_openai_messages(system, messages)

        api_params: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": temp,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tok is not None:
            api_params["max_tokens"] = max_tok

        if tool_list and tool_choice != "none":
            api_params["tools"] = [
                self._convert_tool_definition_openai(t) for t in tool_list
            ]
            api_params["tool_choice"] = self._convert_tool_choice_openai(tool_choice)
        elif tool_list and tool_choice == "none":
            api_params["tool_choice"] = "none"

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
        tool_calls_acc: dict[int, dict[str, str]] = {}
        _tc_started: set = set()
        parsed_tool_calls: list = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}

        from ..logging import get_running_log
        _rlog_local = get_running_log()

        try:
            stream = await self.client.chat.completions.create(**api_params)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None

                # ── DeepSeek reasoning_content 提取 ─────────────
                if delta:
                    rc = getattr(delta, "reasoning_content", None)
                    if not rc:
                        extras = getattr(delta, "model_extra", None) or {}
                        rc = extras.get("reasoning_content") or None
                    if rc:
                        accumulated_reasoning += rc
                        yield StreamChunk(type="thinking_delta", thinking_delta=rc)

                # ── 文本增量 ────────────────────────────────────
                if delta and delta.content:
                    accumulated_text += delta.content
                    yield StreamChunk(type="text_delta", text_delta=delta.content)

                # ── 工具调用增量 ────────────────────────────────
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
                        elif tc_delta.id:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tool_calls_acc[idx]["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments

                        if idx not in _tc_started:
                            _tc_started.add(idx)
                            yield StreamChunk(
                                type="tool_call_start",
                                tool_call_id=tool_calls_acc[idx]["id"],
                                tool_call_name=tool_calls_acc[idx]["name"],
                            )
                        if tc_delta.function and tc_delta.function.arguments:
                            yield StreamChunk(
                                type="tool_call_delta",
                                tool_call_id=tool_calls_acc[idx]["id"],
                                tool_call_arguments_delta=tc_delta.function.arguments,
                            )

                # ── usage ──────────────────────────────────────
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }

                # ── finish_reason ──────────────────────────────
                if chunk.choices and chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

        except Exception as e:
            error_msg = f"DeepSeek API 流式调用失败: {e!s}"
            _rlog_local.error("core_service", f"[DeepSeekClient] {error_msg}")
            raise RuntimeError(error_msg)

        # ── 工具调用完成 ──────────────────────────────────────
        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except (json.JSONDecodeError, AttributeError):
                args = {}
            parsed_tc = ToolCall(id=tc["id"], name=tc["name"], arguments=args)
            parsed_tool_calls.append(parsed_tc)
            yield StreamChunk(
                type="tool_call_end",
                tool_call=parsed_tc,
                tool_call_id=tc["id"],
            )

        # ── reasoning ─────────────────────────────────────────
        reasoning: ReasoningArtifact | None = (
            OpenAIReasoningContent(content=accumulated_reasoning)
            if accumulated_reasoning
            else None
        )

        # ── stop_reason 映射 ──────────────────────────────────
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
