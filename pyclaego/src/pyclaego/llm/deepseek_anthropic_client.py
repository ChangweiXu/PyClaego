"""DeepSeek Anthropic-compat 客户端 - Anthropic 协议形状的 DeepSeek 端点

DeepSeek 在 ``https://api.deepseek.com/anthropic`` 暴露了 Anthropic Messages API
兼容层，用于 Claude SDK / 工具链直接接入 ``deepseek-v4-pro`` 等模型。该端点
的 ``thinking`` 块格式与真实 Anthropic 一致，但 ``signature`` 由 DeepSeek
独立签发，不能与真实 Anthropic 端点交叉重发。

如需使用 DeepSeek 的 OpenAI 协议（含 ``reasoning_content`` dialect），请改用
:class:`DeepSeekClient`。
"""

from .anthropic_client import AnthropicClient


class DeepSeekAnthropicClient(AnthropicClient):
    """DeepSeek Anthropic-compat 端点客户端。"""

    #: 与真实 Anthropic / Kimi Anthropic 区分
    _PROVIDER_TAG: str = "deepseek_anthropic"
