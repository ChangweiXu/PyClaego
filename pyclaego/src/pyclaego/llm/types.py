"""LLM 统一协议中间层数据类型

定义协议无关的统一数据结构，供 chat_completion_v2 使用。
调用方通过这套类型与 LLM 交互，无需关心底层是 OpenAI 还是 Anthropic。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# ─────────────────────────────────────────────────────────────────
#  多模态内容块
# ─────────────────────────────────────────────────────────────────

@dataclass
class TextPart:
    """纯文本内容块"""

    type: Literal["text"] = field(default="text", init=False)
    text: str = ""


@dataclass
class ImagePart:
    """图片内容块

    支持两种图片来源：
    - base64：data 字段为 base64 编码的图片数据，media_type 字段为 MIME 类型
    - url：data 字段为图片 URL，media_type 字段无意义（可保留默认值）

    注意：Anthropic 不支持 url 来源，需在 Entry Point 层预先下载并转换为 base64。
    """

    type: Literal["image"] = field(default="image", init=False)
    source_type: Literal["base64", "url"] = "base64"
    data: str = ""
    """base64 编码的图片数据，或图片 URL"""
    media_type: str = "image/png"
    """MIME 类型（base64 来源时使用，如 "image/png" / "image/jpeg"）"""


@dataclass
class DocumentPart:
    """文档内容块（PDF 等）

    仅支持 base64 来源。
    Anthropic 原生支持 document 类型；Gemini 支持 inline_data；
    OpenAI 不原生支持，需降级为文本摘要或页面图片。
    """

    type: Literal["document"] = field(default="document", init=False)
    source_type: Literal["base64"] = "base64"
    data: str = ""
    """base64 编码的文档数据"""
    media_type: str = "application/pdf"
    """MIME 类型，如 "application/pdf"""


# ContentPart 是 TextPart、ImagePart、DocumentPart 的判别联合类型
ContentPart = Union[TextPart, ImagePart, DocumentPart]


# ─────────────────────────────────────────────────────────────────
#  思考产物（provider 及 dialect 不同，多态代表）
# ─────────────────────────────────────────────────────────────────

@dataclass
class ReasoningArtifact:
    """provider 思考产物的多态基类。

    各 LLM provider 返回的 reasoning 形状不一（DeepSeek 是纯文本，
    Anthropic 是结构化 block 数组）。以子类微型差异问题与不同 shape，
    避免给 :class:`UnifiedMessage` / :class:`ChatResponseV2` 逐个增加字段。

    所有子类必须实现 :meth:`to_dict` / :meth:`from_dict`，以便序列化
    到会话记录、日志工件及跨进程传输。反序列化根据 ``kind``
    辨别符分发到对应子类。
    """

    #: 辨别符，子类必须覆盖为唯一字符串。
    kind: str = field(default="", init=False)

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ReasoningArtifact | None:
        """根据 ``d['kind']`` 分发到具体子类。未知 kind 返回 None。"""
        if not d:
            return None
        kind = d.get("kind") or ""
        cls = _REASONING_REGISTRY.get(kind)
        if cls is None:
            return None
        return cls._from_dict_inner(d)

    @classmethod
    def _from_dict_inner(cls, d: dict[str, Any]) -> ReasoningArtifact:
        """子类覆盖以从 dict 中提取自身字段。"""
        raise NotImplementedError


@dataclass
class OpenAIReasoningContent(ReasoningArtifact):
    """DeepSeek 等 OpenAI dialect 返回的 ``reasoning_content`` 字符串。

    同一带 reasoning_content 的子类客户端（如未来 MoonshotKimiThinking）可复用。
    """

    kind: str = field(default="openai_reasoning_content", init=False)
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "content": self.content}

    @classmethod
    def _from_dict_inner(cls, d: dict[str, Any]) -> OpenAIReasoningContent:
        return cls(content=d.get("content", "") or "")


@dataclass
class AnthropicThinkingBlocks(ReasoningArtifact):
    """Anthropic 系端点返回的 ``thinking`` / ``redacted_thinking`` 原始 block 列表。

    下一轮请求必须原样放到 assistant content 开头，否则 API 400。
    同一字段被 Kimi Code / DeepSeek anthropic-compat 等同协议 vendor 复用。
    """

    kind: str = field(default="anthropic_thinking_blocks", init=False)
    blocks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "blocks": self.blocks}

    @classmethod
    def _from_dict_inner(cls, d: dict[str, Any]) -> AnthropicThinkingBlocks:
        blocks = d.get("blocks") or []
        return cls(blocks=list(blocks))


#: 子类注册表，供 :meth:`ReasoningArtifact.from_dict` 辨别符分发使用。
_REASONING_REGISTRY: dict[str, type] = {
    "openai_reasoning_content": OpenAIReasoningContent,
    "anthropic_thinking_blocks": AnthropicThinkingBlocks,
}


# ─────────────────────────────────────────────────────────────────
#  工具定义相关
# ─────────────────────────────────────────────────────────────────

@dataclass
class ToolDefinition:
    """工具定义（协议无关中间格式）

    与 BaseTool.get_description() 的返回结构对应，可通过
    tool_description_to_definition() 函数直接转换。
    """

    name: str
    """工具名称"""

    description: str
    """工具功能描述"""

    parameters: dict[str, Any] = field(default_factory=dict)
    """参数定义，key 为参数名，value 为 Any"""


# ─────────────────────────────────────────────────────────────────
#  工具调用相关
# ─────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """LLM 返回的工具调用请求（统一格式）"""

    id: str
    """工具调用 ID（OpenAI tool_call.id / Anthropic tool_use block id）"""

    name: str
    """工具名称"""

    arguments: dict[str, Any]
    """工具参数（已解析为 dict）"""

    gemini_thought_signature: bytes | None = None
    """Gemini thinking 模型附加的签名字节串，回传历史消息时必须原样保留"""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        _res = {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.gemini_thought_signature:
            _res["gemini_thought_signature"] = self.gemini_thought_signature.hex()
        return _res


@dataclass
class ToolCallResult:
    """工具执行结果（用于回传给 LLM 的消息）

    content 字段始终必填，作为纯文本回退（向后兼容、日志、不支持多模态的协议）。
    content_parts 可选，携带多模态内容块（图片、文档等）。
    当 content_parts 存在时，LLM 客户端优先使用 content_parts 构建协议消息；
    当 content_parts 为 None 时，行为与之前完全一致。
    """

    tool_call_id: str
    """对应的工具调用 ID（与 ToolCall.id 一致）"""

    tool_name: str
    """工具名称"""

    content: str
    """工具执行结果文本（始终必填，作为纯文本回退）"""

    content_parts: list[ContentPart] | None = None
    """多模态内容块列表（图片、文档等）；为 None 时使用 content 纯文本"""


# ─────────────────────────────────────────────────────────────────
#  统一消息格式
# ─────────────────────────────────────────────────────────────────

@dataclass
class UnifiedMessage:
    """统一消息格式（协议无关）

    一条 UnifiedMessage 对应一轮对话中某一方的发言，支持以下内容类型：
      - 纯文本：text 不为 None
      - 多模态内容：content_parts 不为 None（含文本块和图片块）
      - 工具调用请求（assistant 发起）：tool_calls 不为 None
      - 工具执行结果（user 回传）：tool_results 不为 None

    优先级：当 content_parts 与 text 同时存在时，content_parts 优先。
    text 保留用于向后兼容和纯文本便捷访问。
    """

    role: str
    """消息角色：`user` 或 `assistant` 或 `tool`"""

    text: str | None = None
    """纯文本内容（向后兼容字段；若 content_parts 存在则以 content_parts 为准）"""

    content_parts: list[ContentPart] | None = None
    """多模态内容块列表（含 TextPart / ImagePart；优先于 text 字段）"""

    tool_calls: list[ToolCall] | None = None
    """工具调用请求列表（assistant 发出，表示要调用哪些工具）"""

    tool_results: list[ToolCallResult] | None = None
    """工具执行结果列表（user 回传，与前一轮 assistant 的 tool_calls 对应）"""

    reasoning: ReasoningArtifact | None = None
    """provider 思考产物，多态。各 provider 使用不同子类：
    - DeepSeek (OpenAI dialect)：:class:`OpenAIReasoningContent`
    - Anthropic / Kimi Code / DeepSeek anthropic-compat：:class:`AnthropicThinkingBlocks`
    下一轮请求必须原样回传，否则部分 API 会 400。
    跨 provider 切换时 由 :attr:`produced_by_provider` 守卫过滤。"""

    produced_by_provider: str | None = None
    """产生该消息的 LLM provider 名称（"openai" / "anthropic" / "gemini"）。
    用于模型切换场景：跳过不同 provider 产出的 reasoning artifacts（避免下一轮 API 拒绝）。"""

    produced_by_model: str | None = None
    """产生该消息的具体模型 ID（如 "claude-sonnet-4-7"）。
    Anthropic thinking signature 与模型绑定，同 provider 但不同模型也应跳过重发。"""

    def has_multimodal(self) -> bool:
        """是否包含多模态内容（即 content_parts 中有非文本块）"""
        if not self.content_parts:
            return False
        return any(p.type != "text" for p in self.content_parts)

    def get_text(self) -> str | None:
        """获取文本内容。

        若 content_parts 存在，拼接其中所有 TextPart 的文本；
        否则返回 text 字段。
        """
        if self.content_parts:
            parts = [p.text for p in self.content_parts if isinstance(p, TextPart)]
            return "\n".join(parts) if parts else None
        return self.text


# ─────────────────────────────────────────────────────────────────
#  统一响应格式
# ─────────────────────────────────────────────────────────────────

@dataclass
class ChatResponseV2:
    """chat_completion_v2 统一响应（协议无关）"""

    text: str | None
    """LLM 返回的文本内容（若无则为 None）"""

    tool_calls: list[ToolCall] | None
    """LLM 请求调用的工具列表（若无则为 None）"""

    stop_reason: str
    """停止原因：
      - "stop"：正常结束
      - "tool_use"：需要调用工具
      - "max_tokens"：达到最大 token 限制
      - "end_turn"：等同于 stop
    """

    usage: dict[str, int]
    # token 用量：{prompt_tokens: x, completion_tokens: y, total_tokens: z}

    raw_response: Any
    """原始响应对象（ChatCompletion 或 Anthropic Message），供高级用途"""

    reasoning: ReasoningArtifact | None = None
    """provider 思考产物（多态，详见 :class:`ReasoningArtifact` 子类）"""

    produced_by_provider: str | None = None
    """产生该响应的 LLM provider 名称（"openai" / "anthropic" / "gemini"）"""

    produced_by_model: str | None = None
    """产生该响应的具体模型 ID"""


# ─────────────────────────────────────────────────────────────────
#  流式响应 chunk
# ─────────────────────────────────────────────────────────────────

@dataclass
class StreamChunk:
    """流式响应的单个 chunk。

    所有 provider 的流实现统一 yield 此类型，
    调用方无需关心底层是 OpenAI SSE 还是 Anthropic stream event。

    Lifecycle:
        text_delta* → [tool_call_start → tool_call_delta* → tool_call_end]* → finish
    """

    #: chunk 类型
    type: Literal[
        "text_delta",        # 纯文本增量
        "thinking_delta",    # 思考/推理增量（DeepSeek R1 reasoning_content, Anthropic thinking_delta）
        "tool_call_start",   # 开始一个新的工具调用
        "tool_call_delta",   # 工具参数增量（JSON 字符串片段）
        "tool_call_end",     # 工具调用参数接收完毕
        "finish",            # 流结束（携带 stop_reason / usage / reasoning）
        "fail",              # 流异常终止（携带 error_code / error_message）
    ]

    #: 文本增量（type="text_delta" 时有效）
    text_delta: str | None = None

    #: 思考/推理增量（type="thinking_delta" 时有效）
    thinking_delta: str | None = None

    #: 工具调用的名称（type="tool_call_start" 时有效）
    tool_call_name: str | None = None

    #: 工具调用的 ID（type="tool_call_start" 及后续有效）
    tool_call_id: str | None = None

    #: 工具参数的增量（type="tool_call_delta" 时有效）
    tool_call_arguments_delta: str | None = None

    #: 聚合的完整工具调用（type="tool_call_end" 时有效）
    tool_call: ToolCall | None = None

    #: 停止原因（type="finish" 时有效）
    stop_reason: str | None = None

    #: token 用量（type="finish" 时有效）
    usage: dict[str, int] | None = None

    #: reasoning 产物（type="finish" 时有效）
    reasoning: ReasoningArtifact | None = None

    #: 产生该响应的 provider 名称
    produced_by_provider: str | None = None

    #: 产生该响应的模型 ID
    produced_by_model: str | None = None

    #: 错误码（type="fail" 时有效）
    error_code: str | None = None

    #: 错误描述（type="fail" 时有效）
    error_message: str | None = None


@dataclass
class StreamResponse:
    """流式响应的最终聚合结果。

    流结束后由 provider 的 chat_completion_stream 聚合返回，
    供 SecurityHandler 用于记录、日志、artifact 等。
    """

    text: str = ""
    """聚合的完整文本"""

    tool_calls: list[ToolCall] | None = None
    """聚合的完整工具调用列表"""

    stop_reason: str = "stop"
    """停止原因"""

    usage: dict[str, int] = field(default_factory=dict)
    """token 用量"""

    reasoning: ReasoningArtifact | None = None
    """reasoning 产物"""

    produced_by_provider: str | None = None
    """产生该响应的 provider"""

    produced_by_model: str | None = None
    """产生该响应的模型"""


# ─────────────────────────────────────────────────────────────────
#  辅助函数
# ─────────────────────────────────────────────────────────────────

# 【2026年04月09日20:11:16新增】 Claude-Opus-4.5 生成
def _is_naive_params_format(d: dict) -> bool:
    """判断 dict 是否为朴素参数格式（所有值均为 dict，且不含 'type'/'properties' 等 JSON Schema 顶层键）。

    朴素格式：每个键是参数名，值是含 type/description/required 的字段描述 dict。
    标准 JSON Schema 格式：顶层含 "type"/"properties"/"required" 等键，值类型混杂。
    """
    if not d:
        return False
    json_schema_top_keys = {"type", "properties", "required", "additionalProperties"}
    # 如果顶层含 JSON Schema 保留键，视为标准格式
    if json_schema_top_keys & d.keys():
        return False
    # 所有值均为 dict → 朴素格式
    return all(isinstance(v, dict) for v in d.values())


def convert_to_json_schema(params: dict) -> dict:
    """
    将自定义参数格式转换为标准 JSON Schema 格式。
    递归处理嵌套的 object 和 array。

    同时兼容两种输入：
    - 朴素格式（naive）：{param_name: {type, required, description, ...}}
      ── 由 TOOL_PARAMETERS 风格的工具使用
    - 标准 JSON Schema：{type, properties, required, ...}
      ── 由 QUERY_USER_PARAMETERS 风格的工具使用，直接透传
    """
    # 如果已经是标准 JSON Schema，直接透传，无需转换
    if not _is_naive_params_format(params):
        return params

    properties = {}
    required = []

    for name, field in params.items():
        # 收集 required
        if field.get("required", False):
            required.append(name)

        # 构建该字段的 schema（排除自定义的 required 字段）
        field_schema = {k: v for k, v in field.items() if k != "required"}

        # 递归处理 object 类型
        if field.get("type") == "object":
            # 检查是否有 properties 子字段需要递归
            if "properties" in field:
                field_schema = {
                    "type": "object",
                    **convert_to_json_schema(field["properties"])
                }
                # 保留 description
                if "description" in field:
                    field_schema["description"] = field["description"]

        # 递归处理 array 中的 object 类型
        elif field.get("type") == "array":
            items = field.get("items", {})
            if isinstance(items, dict) and items.get("type") == "object":
                if "properties" in items:
                    if _is_naive_params_format(items["properties"]):
                        # 朴素格式子属性：递归提取 boolean required
                        field_schema["items"] = {
                            "type": "object",
                            **convert_to_json_schema(items["properties"])
                        }
                        if "description" in items:
                            field_schema["items"]["description"] = items["description"]
                    else:
                        # 已是标准 JSON Schema：直接透传，保留 required 列表
                        field_schema["items"] = items

        properties[name] = field_schema

    result = {
        "type": "object",
        "properties": properties
    }

    if required:
        result["required"] = required

    return result


# 【2026年04月09日20:11:16新增】 Claude-Opus-4.5 生成
def build_tool_definition(func_desc: dict, provider: str = "anthropic") -> dict:
    """
    构建完整的 tool definition。
    
    Args:
        func_desc: 你的自定义格式
        provider: "anthropic" 或 "openai"
    """
    schema = convert_to_json_schema(func_desc["parameters"])
    
    if provider == "anthropic":
        return {
            "name": func_desc["name"],
            "description": func_desc["description"],
            "input_schema": schema
        }
    else:  # openai
        return {
            "type": "function",
            "function": {
                "name": func_desc["name"],
                "description": func_desc["description"],
                "parameters": schema
            }
        }


# ─────────────────────────────────────────────────────────────────
#  LLM 响应序列化 / 内容块摘要 辅助函数
# ─────────────────────────────────────────────────────────────────

def serialize_llm_response(response: Any) -> Any:
    """Serialise a raw LLM response object to a JSON-safe structure.

    Supports OpenAI ChatCompletion, Anthropic Message, Gemini
    GenerateContentResponse. All other types fall back to ``str()``.
    Intended for record-writing (logs, audit trails).
    """
    # Lazy imports to keep llm/types.py free of hard runtime deps when the
    # provider SDK is not installed.
    try:
        from openai.types.chat.chat_completion import ChatCompletion as _CC
    except ImportError:
        _CC = None  # type: ignore[assignment,misc]
    try:
        from anthropic.types import Message as _Msg
    except ImportError:
        _Msg = None  # type: ignore[assignment,misc]
    try:
        from google.genai.types import GenerateContentResponse as _GCR
    except ImportError:
        _GCR = None  # type: ignore[assignment,misc]

    if _CC is not None and isinstance(response, _CC):
        data: Any = {
            "id": response.id,
            "object": response.object,
            "created": response.created,
            "model": response.model,
            "choices": [
                {
                    "index": choice.index,
                    "message": {
                        "role": choice.message.role,
                        "content": choice.message.content,
                        # DeepSeek/OpenAI 思考模式：reasoning_content 必须持久化以便回放/调试
                        "reasoning_content": (
                            getattr(choice.message, "reasoning_content", None)
                            or (getattr(choice.message, "model_extra", None) or {}).get("reasoning_content")
                        ),
                        "tool_calls": [
                            {
                                "tool_name": call.function.name,        # type: ignore[attr-defined]
                                "arguments": call.function.arguments,   # type: ignore[attr-defined]
                            }
                            for call in choice.message.tool_calls
                        ] if hasattr(choice.message, "tool_calls") and choice.message.tool_calls is not None else None,
                    },
                    "finish_reason": choice.finish_reason,
                }
                for choice in response.choices
            ],
        }
        if response.usage:
            data["usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        prompt_filter_results = getattr(response, "prompt_filter_results", None)
        if prompt_filter_results is not None:
            data["prompt_filter_results"] = prompt_filter_results
        return data

    if _Msg is not None and isinstance(response, _Msg):
        def _serialize_anthropic_block(block: Any) -> dict[str, Any]:
            btype = block.type
            if btype == "text":
                return {"type": "text", "text": block.text}
            if btype == "tool_use":
                return {"type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input}
            # Anthropic 思考模式：thinking / redacted_thinking 必须保留 signature/data
            # 否则下一轮回传时 API 会拒绝
            if btype in ("thinking", "redacted_thinking"):
                if hasattr(block, "model_dump"):
                    return block.model_dump()
                return {"type": btype, "raw": str(block)}
            return {"type": btype, "raw": str(block)}

        return {
            "id": response.id,
            "type": response.type,
            "role": response.role,
            "model": response.model,
            "content": [_serialize_anthropic_block(block) for block in response.content],
            "stop_reason": response.stop_reason,
            "stop_sequence": response.stop_sequence,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }

    if _GCR is not None and isinstance(response, _GCR):
        candidates_data = []
        for cand in (response.candidates or []):
            parts_data = []
            if cand.content and cand.content.parts:
                for part in cand.content.parts:
                    if part.text:
                        parts_data.append({"type": "text", "text": part.text})
                    if part.function_call:
                        args = part.function_call.args
                        if not isinstance(args, dict):
                            args = dict(args) if args else {}
                        parts_data.append({
                            "type": "function_call",
                            "id": part.function_call.id,
                            "name": part.function_call.name,
                            "args": args,
                        })
            candidates_data.append({
                "index": cand.index,
                "role": cand.content.role if cand.content else None,
                "parts": parts_data,
                "finish_reason": str(cand.finish_reason) if cand.finish_reason else None,
            })
        usage_data: dict[str, Any] = {}
        if response.usage_metadata:
            usage_data = {
                "prompt_tokens": response.usage_metadata.prompt_token_count or 0,
                "completion_tokens": response.usage_metadata.candidates_token_count or 0,
                "thoughts_tokens": response.usage_metadata.thoughts_token_count or 0,
                "total_tokens": response.usage_metadata.total_token_count or 0,
            }
        return {
            "model_version": response.model_version,
            "response_id": response.response_id,
            "candidates": candidates_data,
            "usage": usage_data,
        }

    return str(response) if response else ""


def summarize_content_parts(content_parts: list[ContentPart] | None) -> list | None:
    """Summarise a ContentPart list to metadata-only (no base64 data).

    Used for log records to avoid writing large binary payloads to disk while
    retaining type, media_type, and data-length information.
    """
    if not content_parts:
        return None
    summary = []
    for p in content_parts:
        entry: dict[str, Any] = {"type": p.type}
        if hasattr(p, "media_type"):
            entry["media_type"] = p.media_type
        if hasattr(p, "data"):
            entry["data_length"] = len(p.data)
        if hasattr(p, "text"):
            entry["text"] = p.text[:200] + "...[truncated]" if len(p.text) > 200 else p.text
        summary.append(entry)
    return summary


def tool_description_to_definition(desc: dict[str, Any]) -> ToolDefinition:
    """将 BaseTool.get_description() 的返回值转换为 ToolDefinition

    BaseTool.get_description() 的 parameters 字段格式：
    ::

        {
            "param_name": {
                "type": "string",
                "required": True,
                "description": "参数说明",
                # 可选：支持完整的 JSON Schema 特性
                "enum": ["a", "b"],
                "items": {"type": "string"},
                "pattern": "^[a-z]+$",
                "minLength": 1,
                "maxLength": 100,
                ...
            }
        }

    Args:
        desc: BaseTool.get_description() 的返回字典

    Returns:
        ToolDefinition 实例

    Example::

        bash_desc = bash_tool.get_description()
        tool_def = tool_description_to_definition(bash_desc)
        # 将 tool_def 传给 chat_completion_v2(tool_list=[tool_def], ...)
    """
    params: dict[str, Any] = convert_to_json_schema(desc.get("parameters", {}))

    return ToolDefinition(
        name=desc["name"],
        description=desc.get("description", ""),
        parameters=params,
    )
