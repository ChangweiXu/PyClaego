"""Kimi Code 客户端 - Anthropic 协议兼容（Moonshot 编码套餐）

Kimi Code (``https://api.kimi.com/coding``) 实现 Anthropic Messages API 形状，
但 ``thinking`` 块的 ``signature`` 是 vendor 私有 token，与真实 Anthropic 服务
不互通。必须通过独立的 ``produced_by_provider`` 标签隔离两侧的 thinking 历史，
避免跨厂商重发签名导致 400。

实现策略：
继承 :class:`AnthropicClient`，仅覆盖 :attr:`_PROVIDER_TAG`。其余协议处理
（消息构建 / thinking 块 / tool_use / 流式守卫）完全复用基类。
"""

from .anthropic_client import AnthropicClient


class KimiCodeClient(AnthropicClient):
    """Kimi Code (Moonshot) Anthropic-protocol 客户端。

    仅用于 ``base_url`` 指向 Moonshot Kimi Code 编码套餐（如 k2p5）的场景。
    标准 Moonshot OpenAI 协议接口请使用 :class:`OpenAIClient`。
    """

    #: 与真实 Anthropic 区分，避免 thinking signature 跨厂商误用
    _PROVIDER_TAG: str = "kimi_anthropic"
