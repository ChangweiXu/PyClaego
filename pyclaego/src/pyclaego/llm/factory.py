"""LLM 客户端工厂 - 根据配置创建对应的客户端实例"""

from typing import Any

from ..logging import get_running_log
from .anthropic_client import AnthropicClient
from .base import LLMClient
from .deepseek_anthropic_client import DeepSeekAnthropicClient
from .deepseek_client import DeepSeekClient
from .gemini_client import GeminiClient
from .gemini_openai_client import GeminiOpenAIClient
from .kimi_code_client import KimiCodeClient
from .openai_client import OpenAIClient

_rlog = get_running_log()


class LLMClientFactory:
    """LLM 客户端工厂类
    
    根据配置参数自动创建对应的 LLM 客户端实例。
    支持的 API 类型：
    - openai: 纯 OpenAI Chat Completions（不含 reasoning_content dialect）
    - deepseek: OpenAI 协议 + DeepSeek reasoning_content 思考模式
    - anthropic: Anthropic Claude 接口
    - kimi_anthropic: Kimi Code （Anthropic 协议兼容层）
    - deepseek_anthropic: DeepSeek Anthropic 兼容端点
    - gemini: Google Gemini 接口
    """
    
    # 支持的 API 类型映射
    _CLIENT_MAP = {
        "openai": OpenAIClient,
        "deepseek": DeepSeekClient,
        "anthropic": AnthropicClient,
        "kimi_anthropic": KimiCodeClient,
        "deepseek_anthropic": DeepSeekAnthropicClient,
        "gemini": GeminiClient,
        "gemini_openai": GeminiOpenAIClient,
    }
    
    @classmethod
    def create_client(
        cls,
        api: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs
    ) -> LLMClient:
        """创建 LLM 客户端实例
        
        Args:
            api: API 类型（"openai" 或 "anthropic"）
            api_key: API 密钥
            base_url: API 基础 URL（可选）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            **kwargs: 其他参数
            
        Returns:
            LLMClient 实例（OpenAIClient 或 AnthropicClient）
            
        Raises:
            ValueError: 当 API 类型不支持时
            
        Examples:
            >>> # 创建 OpenAI 客户端
            >>> client = LLMClientFactory.create_client(
            ...     api="openai",
            ...     api_key="sk-xxx",
            ...     model="gpt-4"
            ... )
            
            >>> # 创建 Anthropic 客户端
            >>> client = LLMClientFactory.create_client(
            ...     api="anthropic",
            ...     api_key="sk-ant-xxx",
            ...     model="claude-3-5-sonnet-20241022"
            ... )
            
            >>> # 从配置字典创建
            >>> config = {
            ...     "api": "openai",
            ...     "api_key": "sk-xxx",
            ...     "model": "gpt-4",
            ...     "temperature": 0.7
            ... }
            >>> client = LLMClientFactory.create_from_config(config)
        """
        # 标准化 API 类型
        api = api.lower().strip()

        # 向后兼容：旧配置使用 ``api: openai`` + DeepSeek base_url。
        # 自动转发到 DeepSeekClient 并发出弃用警告。下一个 release 清理。
        if api == "openai" and base_url and "deepseek" in base_url.lower():
            _rlog.warning(
                "core_service",
                f"[LLMClientFactory] 检测到 base_url={base_url!r} 指向 DeepSeek，"
                f"但配置 api=openai。已自动路由到 DeepSeekClient。"
                f"请在配置中显式设置 api=deepseek 以获取思考模式支持。该 fallback 将于后续版本移除。"
            )
            api = "deepseek"

        # 向后兼容：旧配置使用 ``api: anthropic`` + vendor base_url。
        # Kimi Code / DeepSeek anthropic-compat 都使用 Anthropic 协议，但 thinking
        # signature 不能跨厂商重发，必须以 vendor 粒度隔离。
        if api == "anthropic" and base_url:
            _bu = base_url.lower()
            if "kimi.com" in _bu:
                _rlog.warning(
                    "core_service",
                    f"[LLMClientFactory] 检测到 base_url={base_url!r} 指向 Kimi Code，"
                    f"但配置 api=anthropic。已自动路由到 KimiCodeClient。"
                    f"请在配置中显式设置 api=kimi_anthropic。该 fallback 将于后续版本移除。"
                )
                api = "kimi_anthropic"
            elif "deepseek" in _bu:
                _rlog.warning(
                    "core_service",
                    f"[LLMClientFactory] 检测到 base_url={base_url!r} 指向 DeepSeek anthropic 端点，"
                    f"但配置 api=anthropic。已自动路由到 DeepSeekAnthropicClient。"
                    f"请在配置中显式设置 api=deepseek_anthropic。该 fallback 将于后续版本移除。"
                )
                api = "deepseek_anthropic"

        # 检查是否支持该 API 类型
        if api not in cls._CLIENT_MAP:
            supported = ", ".join(cls._CLIENT_MAP.keys())
            raise ValueError(
                f"不支持的 API 类型: {api}. "
                f"支持的类型: {supported}"
            )
        
        # 获取对应的客户端类
        client_class = cls._CLIENT_MAP[api]
        
        # 创建客户端实例
        return client_class(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs
        )
    
    @classmethod
    def create_from_config(cls, config: dict[str, Any]) -> LLMClient:
        """从配置字典创建客户端
        
        Args:
            config: 配置字典，应包含以下字段：
                - api: API 类型（必需）
                - api_key: API 密钥（可选）
                - base_url: API 基础 URL（可选）
                - model: 模型名称（可选，默认根据 API 类型）
                - temperature: 温度参数（可选，默认 0.7）
                - max_tokens: 最大 token 数（可选）
                
        Returns:
            LLMClient 实例
            
        Raises:
            ValueError: 当配置缺少必需字段时
            
        Examples:
            >>> config = {
            ...     "api": "openai",
            ...     "api_key": "sk-xxx",
            ...     "base_url": "https://api.kimi.com/coding",
            ...     "model": "k2p5",
            ...     "temperature": 0.7,
            ...     "max_tokens": 8192
            ... }
            >>> client = LLMClientFactory.create_from_config(config)
        """
        # 检查必需字段
        if "api" not in config:
            raise ValueError("配置中缺少必需字段: api")
        
        # 提取配置参数
        api = config.get("api", "openai")
        api_key = config.get("api_key")
        base_url = config.get("base_url")
        model = config.get("model", "gpt-4" if api == "openai" else "claude-3-5-sonnet-20241022")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens")
        
        # 提取其他参数
        extra_params = {
            k: v for k, v in config.items()
            if k not in ["api", "api_key", "base_url", "model", "temperature", "max_tokens"]
        }
        
        # 创建客户端
        return cls.create_client(
            api=api,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_params
        )
    
    @classmethod
    def get_supported_apis(cls) -> list[str]:
        """获取支持的 API 类型列表
        
        Returns:
            支持的 API 类型列表
        """
        return list(cls._CLIENT_MAP.keys())
    
    @classmethod
    def is_supported(cls, api: str) -> bool:
        """检查是否支持指定的 API 类型
        
        Args:
            api: API 类型
            
        Returns:
            是否支持
        """
        return api.lower().strip() in cls._CLIENT_MAP
