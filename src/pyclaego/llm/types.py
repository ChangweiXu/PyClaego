"""LLM 统一协议中间层数据类型

定义协议无关的统一数据结构，供 chat_completion_v2 使用。
调用方通过这套类型与 LLM 交互，无需关心底层是 OpenAI 还是 Anthropic。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union
import json


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

    def to_dict(self) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Optional["ReasoningArtifact"]:
        """根据 ``d['kind']`` 分发到具体子类。未知 kind 返回 None。"""
        if not d:
            return None
        kind = d.get("kind") or ""
        cls = _REASONING_REGISTRY.get(kind)
        if cls is None:
            return None
        return cls._from_dict_inner(d)

    @classmethod
    def _from_dict_inner(cls, d: Dict[str, Any]) -> "ReasoningArtifact":
        """子类覆盖以从 dict 中提取自身字段。"""
        raise NotImplementedError


@dataclass
class OpenAIReasoningContent(ReasoningArtifact):
    """DeepSeek 等 OpenAI dialect 返回的 ``reasoning_content`` 字符串。

    同一带 reasoning_content 的子类客户端（如未来 MoonshotKimiThinking）可复用。
    """

    kind: str = field(default="openai_reasoning_content", init=False)
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "content": self.content}

    @classmethod
    def _from_dict_inner(cls, d: Dict[str, Any]) -> "OpenAIReasoningContent":
        return cls(content=d.get("content", "") or "")


@dataclass
class AnthropicThinkingBlocks(ReasoningArtifact):
    """Anthropic 系端点返回的 ``thinking`` / ``redacted_thinking`` 原始 block 列表。

    下一轮请求必须原样放到 assistant content 开头，否则 API 400。
    同一字段被 Kimi Code / DeepSeek anthropic-compat 等同协议 vendor 复用。
    """

    kind: str = field(default="anthropic_thinking_blocks", init=False)
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "blocks": self.blocks}

    @classmethod
    def _from_dict_inner(cls, d: Dict[str, Any]) -> "AnthropicThinkingBlocks":
        blocks = d.get("blocks") or []
        return cls(blocks=list(blocks))


#: 子类注册表，供 :meth:`ReasoningArtifact.from_dict` 辨别符分发使用。
_REASONING_REGISTRY: Dict[str, type] = {
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

    parameters: Dict[str, Any] = field(default_factory=dict)
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

    arguments: Dict[str, Any]
    """工具参数（已解析为 dict）"""

    gemini_thought_signature: Optional[bytes] = None
    """Gemini thinking 模型附加的签名字节串，回传历史消息时必须原样保留"""

    def to_dict(self) -> Dict[str, Any]:
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

    content_parts: Optional[List[ContentPart]] = None
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

    text: Optional[str] = None
    """纯文本内容（向后兼容字段；若 content_parts 存在则以 content_parts 为准）"""

    content_parts: Optional[List[ContentPart]] = None
    """多模态内容块列表（含 TextPart / ImagePart；优先于 text 字段）"""

    tool_calls: Optional[List[ToolCall]] = None
    """工具调用请求列表（assistant 发出，表示要调用哪些工具）"""

    tool_results: Optional[List[ToolCallResult]] = None
    """工具执行结果列表（user 回传，与前一轮 assistant 的 tool_calls 对应）"""

    reasoning: Optional[ReasoningArtifact] = None
    """provider 思考产物，多态。各 provider 使用不同子类：
    - DeepSeek (OpenAI dialect)：:class:`OpenAIReasoningContent`
    - Anthropic / Kimi Code / DeepSeek anthropic-compat：:class:`AnthropicThinkingBlocks`
    下一轮请求必须原样回传，否则部分 API 会 400。
    跨 provider 切换时 由 :attr:`produced_by_provider` 守卫过滤。"""

    produced_by_provider: Optional[str] = None
    """产生该消息的 LLM provider 名称（"openai" / "anthropic" / "gemini"）。
    用于模型切换场景：跳过不同 provider 产出的 reasoning artifacts（避免下一轮 API 拒绝）。"""

    produced_by_model: Optional[str] = None
    """产生该消息的具体模型 ID（如 "claude-sonnet-4-7"）。
    Anthropic thinking signature 与模型绑定，同 provider 但不同模型也应跳过重发。"""

    def has_multimodal(self) -> bool:
        """是否包含多模态内容（即 content_parts 中有非文本块）"""
        if not self.content_parts:
            return False
        return any(p.type != "text" for p in self.content_parts)

    def get_text(self) -> Optional[str]:
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

    text: Optional[str]
    """LLM 返回的文本内容（若无则为 None）"""

    tool_calls: Optional[List[ToolCall]]
    """LLM 请求调用的工具列表（若无则为 None）"""

    stop_reason: str
    """停止原因：
      - "stop"：正常结束
      - "tool_use"：需要调用工具
      - "max_tokens"：达到最大 token 限制
      - "end_turn"：等同于 stop
    """

    usage: Dict[str, int]
    # token 用量：{prompt_tokens: x, completion_tokens: y, total_tokens: z}

    raw_response: Any
    """原始响应对象（ChatCompletion 或 Anthropic Message），供高级用途"""

    reasoning: Optional[ReasoningArtifact] = None
    """provider 思考产物（多态，详见 :class:`ReasoningArtifact` 子类）"""

    produced_by_provider: Optional[str] = None
    """产生该响应的 LLM provider 名称（"openai" / "anthropic" / "gemini"）"""

    produced_by_model: Optional[str] = None
    """产生该响应的具体模型 ID"""


# ─────────────────────────────────────────────────────────────────
#  辅助函数
# ─────────────────────────────────────────────────────────────────

# 【2026年04月09日20:11:16新增】 Claude-Opus-4.5 生成
def convert_to_json_schema(params: dict) -> dict:
    """
    将自定义参数格式转换为标准 JSON Schema 格式。
    递归处理嵌套的 object 和 array。
    """
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
                    field_schema["items"] = {
                        "type": "object",
                        **convert_to_json_schema(items["properties"])
                    }
                    if "description" in items:
                        field_schema["items"]["description"] = items["description"]
        
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


def tool_description_to_definition(desc: Dict[str, Any]) -> ToolDefinition:
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
    params: Dict[str, Any] = convert_to_json_schema(desc.get("parameters", {}))

    return ToolDefinition(
        name=desc["name"],
        description=desc.get("description", ""),
        parameters=params,
    )
