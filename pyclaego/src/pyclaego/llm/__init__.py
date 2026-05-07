
from .anthropic_client import AnthropicClient
from .base import LLMClient
from .deepseek_anthropic_client import DeepSeekAnthropicClient
from .deepseek_client import DeepSeekClient
from .factory import LLMClientFactory
from .gemini_client import GeminiClient
from .gemini_openai_client import GeminiOpenAIClient
from .kimi_code_client import KimiCodeClient
from .openai_client import OpenAIClient
from .types import (
    AnthropicThinkingBlocks,
    ChatResponseV2,
    ContentPart,
    DocumentPart,
    ImagePart,
    OpenAIReasoningContent,
    ReasoningArtifact,
    StreamChunk,
    StreamResponse,
    TextPart,
    ToolCall,
    ToolCallResult,
    ToolDefinition,
    UnifiedMessage,
    serialize_llm_response,
    summarize_content_parts,
    tool_description_to_definition,
)

__all__ = [
    # 客户端
    "LLMClient",                     # 抽象基类
    "OpenAIClient",                  # OpenAI 实现
    "DeepSeekClient",                # DeepSeek dialect（OpenAI 协议 + reasoning_content）
    "AnthropicClient",               # Anthropic 实现
    "KimiCodeClient",                # Kimi Code（Anthropic 协议兼容）
    "DeepSeekAnthropicClient",       # DeepSeek Anthropic-compat 端点
    "GeminiClient",                  # Gemini 实现
    "GeminiOpenAIClient",             # Gemini OpenAI 兼容端点（含 thought_signature 往返）
    "LLMClientFactory",              # 工厂类
    # v2 数据类型
    "ToolDefinition",                # 工具定义（协议无关）
    "ToolCall",                      # LLM 工具调用请求
    "ToolCallResult",                # 工具执行结果（回传给 LLM）
    "UnifiedMessage",                # 统一消息格式
    "ChatResponseV2",                    # chat_completion_v2 统一响应
    "StreamChunk",                       # 流式响应 chunk
    "StreamResponse",                    # 流式响应聚合结果
    "tool_description_to_definition", # BaseTool.get_description() -> ToolDefinition
    # 多模态内容块
    "TextPart",                      # 纯文本内容块
    "ImagePart",                     # 图片内容块
    "DocumentPart",                  # 文档内容块（PDF 等）
    "ContentPart",                   # ContentPart = TextPart | ImagePart | DocumentPart
    # provider 思考产物（多态）
    "ReasoningArtifact",             # 子类多态基类
    "OpenAIReasoningContent",        # DeepSeek 等 OpenAI dialect
    "AnthropicThinkingBlocks",       # Anthropic / Kimi Code / DeepSeek anthropic-compat
    # 序列化 / 摘要辅助函数
    "serialize_llm_response",        # 任意 raw_response → JSON-safe dict
    "summarize_content_parts",       # ContentPart 列表 → 元数据摘要（不含 base64）
]
