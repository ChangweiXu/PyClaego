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

from typing import Any, Dict, Optional

from .openai_client import OpenAIClient
from .types import OpenAIReasoningContent, ReasoningArtifact, UnifiedMessage


class DeepSeekClient(OpenAIClient):
    """DeepSeek 客户端 - 在 OpenAI 协议之上叠加 reasoning_content 往返。"""

    #: 产出消息打 ``produced_by_provider="deepseek"`` 标签，
    #: 用于跨 provider 切换时的兼容性守卫。
    _PROVIDER_TAG: str = "deepseek"

    # ─────────────────────────────────────────────────────────────────
    #  解析侧：抽取 reasoning_content
    # ─────────────────────────────────────────────────────────────────

    def _extract_reasoning(self, message: Any) -> Optional[ReasoningArtifact]:
        """从 DeepSeek ``ChatCompletionMessage`` 中提取 reasoning_content。

        OpenAI SDK 把未声明的字段放进 ``model_extra``，老版本 SDK 也可能
        直接挂为属性 —— 两条路径都要尝试。
        """
        value: Optional[str] = getattr(message, "reasoning_content", None)
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
        self, msg_dict: Dict[str, Any], msg: UnifiedMessage
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
