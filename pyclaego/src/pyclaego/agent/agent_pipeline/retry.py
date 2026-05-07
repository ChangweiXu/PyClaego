"""RetryPolicy — LLM 调用重试策略

供 ToolCallLoopStep._run_llm_call 使用：对瞬态错误（限流、超时、网络抖动）
自动重试，对永久性错误（认证失败、参数错误）立即放弃，不重试。

错误类型通过字符串子串匹配判断，避免硬依赖 openai / anthropic 等外部 SDK。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetryPolicy:
    """重试策略配置。

    Attributes:
        max_retries:            最大重试次数（不含首次调用）
        backoff_base:           指数退避基数；第 n 次重试前等待 backoff_base^n 秒
        backoff_max:            单次退避上限（秒）
        retryable_substrings:   错误消息包含这些子串时视为可重试
        non_retryable_substrings: 错误消息包含这些子串时立即放弃（优先级更高）
    """

    max_retries: int = 3
    backoff_base: float = 1.5
    backoff_max: float = 30.0

    retryable_substrings: tuple[str, ...] = (
        "rate_limit",
        "RateLimitError",
        "timeout",
        "TimeoutError",
        "ConnectionError",
        "ServiceUnavailableError",
        "overloaded",
        "529",
        "503",
        "502",
        "Too Many Requests",
        "Connection reset",
    )

    non_retryable_substrings: tuple[str, ...] = (
        "AuthenticationError",
        "InvalidRequestError",
        "invalid_api_key",
        "model_not_found",
        "context_length_exceeded",
        "Security check failed",
    )

    def is_retryable(self, error: Exception) -> bool:
        """判断该异常是否值得重试。

        non_retryable_substrings 优先级高于 retryable_substrings：
        只要命中 non_retryable，无论是否也命中 retryable，都返回 False。
        """
        error_str = f"{type(error).__name__}: {error!s}"
        lo = error_str.lower()

        for substr in self.non_retryable_substrings:
            if substr.lower() in lo:
                return False

        for substr in self.retryable_substrings:
            if substr.lower() in lo:
                return True

        return False

    def backoff_seconds(self, attempt: int) -> float:
        """计算第 attempt 次重试前应等待的秒数（指数退避，有上限）。

        Args:
            attempt: 重试序号，从 1 开始
        """
        return min(self.backoff_base ** attempt, self.backoff_max)


# 模块级默认策略，可直接引入使用或在 ToolCallLoopStep 构造时覆盖
DEFAULT_RETRY_POLICY = RetryPolicy()
