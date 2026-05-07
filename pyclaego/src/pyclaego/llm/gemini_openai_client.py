"""Gemini OpenAI 兼容客户端 — 处理 thought_signature 往返

Gemini 通过 OpenAI 兼容端点调用时，tool_call 响应包含
``extra_content.google.thought_signature``，必须在多轮 tool_use 交互中
原样回传，否则 Gemini 会返回 400 错误。

本客户端继承 :class:`OpenAIClient`，通过两个轻量钩子处理该 dialect：

- **解析侧**：:meth:`_extract_tool_call_extra` 从响应的 tool_call 中提取
  ``extra_content.google.thought_signature``，base64 解码后存入
  :attr:`ToolCall.gemini_thought_signature`。
- **构建侧**：:meth:`_inject_tool_call_extra` 在下一轮请求中将签名
  base64 编码后注入回 ``extra_content.google.thought_signature``。

实现策略与 :class:`DeepSeekClient` 一致：仅覆盖 3 个轻量钩子，
不复制父类的消息构建循环。
"""

from __future__ import annotations

import base64
from typing import Any

from .openai_client import OpenAIClient
from .types import ToolCall


class GeminiOpenAIClient(OpenAIClient):
    """Gemini OpenAI 兼容客户端 — 处理 thought_signature 往返。

    与原生 :class:`GeminiClient`（Google GenAI SDK）不同，本客户端使用
    OpenAI Chat Completions 协议（``/v1beta/openai/chat/completions``），
    适合通过通用 OpenAI SDK 或 LLM Router 调用 Gemini。
    """

    #: 产出消息打 ``produced_by_provider="gemini"`` 标签，
    #: 与原生 GeminiClient 统一，用于跨 provider 切换时的兼容性守卫。
    _PROVIDER_TAG: str = "gemini"

    # ─────────────────────────────────────────────────────────────────
    #  解析侧：从 tool_call 响应中提取 thought_signature
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_tool_call_extra(tc_raw: Any) -> dict[str, Any]:
        """从 Gemini OpenAI 兼容端点返回的 tool_call 中提取 thought_signature。

        Gemini 的 OpenAI 兼容端点将 thought_signature 放在
        ``tc_raw.extra_content.google.thought_signature``（base64 字符串）。
        提取后 base64 解码为 bytes，通过 ``ToolCall.gemini_thought_signature`` 存储。

        Args:
            tc_raw: OpenAI SDK 的 ``ChatCompletionMessageToolCall`` 对象。

        Returns:
            dict，包含 ``gemini_thought_signature``（bytes）键，供调用方
            传入 :class:`ToolCall` 构造。若响应中无签名则返回 ``{}``。
        """
        # OpenAI SDK 将未声明字段放入 model_extra，也可能直接作为属性暴露
        extra_content = getattr(tc_raw, "extra_content", None)
        if extra_content is None:
            extras = getattr(tc_raw, "model_extra", None) or {}
            extra_content = extras.get("extra_content")
        if not extra_content or not isinstance(extra_content, dict):
            return {}

        google = extra_content.get("google")
        if not isinstance(google, dict):
            return {}

        sig_b64 = google.get("thought_signature")
        if not sig_b64:
            return {}

        try:
            sig_bytes = base64.b64decode(sig_b64)
        except Exception:
            return {}

        return {"gemini_thought_signature": sig_bytes}

    # ─────────────────────────────────────────────────────────────────
    #  构建侧：将 thought_signature 注入 tool_call 请求 dict
    # ─────────────────────────────────────────────────────────────────

    def _inject_tool_call_extra(
        self, tc_dict: dict[str, Any], tc: ToolCall
    ) -> None:
        """将 gemini_thought_signature 注入回 tool_call 请求 dict。

        仅在 ``tc.gemini_thought_signature`` 不为 None 时注入。
        格式：``{"extra_content": {"google": {"thought_signature": "<base64>"}}}``

        Args:
            tc_dict: 正在构建的单个 tool_call 请求 dict（会被原地修改）。
            tc: 对应的 :class:`ToolCall` 统一对象。
        """
        if not tc.gemini_thought_signature:
            return
        sig_b64 = base64.b64encode(tc.gemini_thought_signature).decode("ascii")
        tc_dict["extra_content"] = {
            "google": {
                "thought_signature": sig_b64,
            }
        }

    # ─────────────────────────────────────────────────────────────────
    #  流式侧：从流式 tool_call delta 中提取 thought_signature
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_streaming_tool_call_delta_extra(tc_delta: Any) -> dict[str, Any]:
        """从流式 tool_call delta 中提取 Gemini thought_signature。

        在 SSE 流中，``extra_content.google.thought_signature`` 出现在
        首条 tool_call delta（携带 ``id`` 和 ``function.name`` 的那条）的
        ``model_extra`` 中。提取逻辑与非流式 :meth:`_extract_tool_call_extra`
        完全一致。
        """
        # 与非流式 _extract_tool_call_extra 相同的提取逻辑
        extra_content = getattr(tc_delta, "extra_content", None)
        if extra_content is None:
            extras = getattr(tc_delta, "model_extra", None) or {}
            extra_content = extras.get("extra_content")
        if not extra_content or not isinstance(extra_content, dict):
            return {}

        google = extra_content.get("google")
        if not isinstance(google, dict):
            return {}

        sig_b64 = google.get("thought_signature")
        if not sig_b64:
            return {}

        try:
            sig_bytes = base64.b64decode(sig_b64)
        except Exception:
            return {}

        return {"gemini_thought_signature": sig_bytes}
