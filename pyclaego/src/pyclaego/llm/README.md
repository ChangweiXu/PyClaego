# llm 模块 — LLM 客户端抽象层

## 概述

`llm` 模块提供协议无关的统一 LLM 调用接口。调用方无需关心底层使用的是 OpenAI 协议还是 Anthropic 协议，只需使用统一的数据类型（`UnifiedMessage`、`ToolDefinition` 等）与模块交互，由各子类负责完成协议转换。

### 文件结构

```
llm/
├── __init__.py                    # 统一导出所有公开符号
├── base.py                        # LLMClient 抽象基类（含流式 chat_completion_stream 默认实现）
├── types.py                       # 协议无关的统一数据类型 + ReasoningArtifact 多态 + StreamChunk/StreamResponse
├── factory.py                     # LLMClientFactory 工厂类
├── openai_client.py               # OpenAI 协议基类（不含 reasoning）
├── deepseek_client.py             # DeepSeek dialect（继承 OpenAIClient，加 reasoning_content）
├── gemini_openai_client.py        # Gemini OpenAI 兼容客户端（继承 OpenAIClient，thought_signature 往返）
├── anthropic_client.py            # Anthropic 协议基类（含 thinking blocks）
├── kimi_code_client.py            # Kimi Code（继承 AnthropicClient，仅 vendor 标签隔离）
├── deepseek_anthropic_client.py   # DeepSeek Anthropic 兼容端点（继承 AnthropicClient）
└── gemini_client.py               # Gemini 协议实现（原生 google-genai SDK）
```

---

## 架构概览

```
调用方（Agent / SecurityExecutor 等）
         │
         │ 使用 UnifiedMessage / ToolDefinition / ReasoningArtifact 等统一类型
         ▼
  ┌──────────────────────────────────────────────────┐
  │              LLMClient (抽象基类)                  │
  │   chat_completion()          [旧版，保持兼容]      │
  │   chat_completion_v2()       [新版，协议无关]      │
  │   chat_completion_stream()   [流式，默认回退实现]  │
  └────────────────┬─────────────────────────────────┘
                   │
        ┌──────────┼──────────────────────────┐
        ▼          ▼                          ▼
  OpenAIClient  AnthropicClient          GeminiClient
  (openai SDK)  (anthropic SDK)          (google-genai SDK)
  纯协议         + thinking blocks        原生 SDK
        │         │
        ├── DeepSeekClient          (+ reasoning_content)
        ├── GeminiOpenAIClient      (+ thought_signature 往返，Gemini OpenAI-compat 端点)
        │
        ├── KimiCodeClient          (仅 vendor 标签隔离)
        └── DeepSeekAnthropicClient (DeepSeek anthropic-compat 端点)
```

**客户端类谱设计原则**：用继承隔离"wire 协议"与"vendor dialect"两维差异。基类只负责协议主体；子类通过以下钩子点定制：

- `_PROVIDER_TAG`：vendor 标签，用于跨 provider 切换时的 reasoning 兼容性守卫
- `_extract_reasoning(message)`：从 SDK 响应里提取多态 `ReasoningArtifact`
- `_inject_reasoning(msg_dict, msg)` 或 build 分支里的 `_is_compatible_thinking`：决定是否回传到下一轮请求
- `_extract_tool_call_extra(tc_raw)`：从工具调用响应中提取 vendor 专有附加字段（如 `gemini_thought_signature`）
- `_inject_tool_call_extra(tc_dict, tc)`：在构建工具调用请求时注入 vendor 专有附加字段
- `_extract_streaming_tool_call_delta_extra(tc_delta)`：在流式响应中提取工具调用 delta 的附加字段

---

## 数据类型（`types.py`）

### `TextPart`

纯文本内容块，用于 `UnifiedMessage.content_parts`。

```python
@dataclass
class TextPart:
    type: Literal["text"]   # 固定为 "text"，自动赋值
    text: str = ""
```

### `ImagePart`

图片内容块，支持 base64 和 URL 两种来源。

```python
@dataclass
class ImagePart:
    type: Literal["image"]              # 固定为 "image"，自动赋值
    source_type: Literal["base64", "url"] = "base64"
    data: str = ""                      # base64 数据或图片 URL
    media_type: str = "image/png"       # MIME 类型，仅 base64 来源时有意义
```

**注意：** Anthropic API 不支持 `url` 来源的图片，需在发送前预先下载并转为 base64。

### `DocumentPart`

文档内容块（PDF 等），仅支持 base64 来源。

```python
@dataclass
class DocumentPart:
    type: Literal["document"]                # 固定为 "document"，自动赋值
    source_type: Literal["base64"] = "base64"
    data: str = ""                           # base64 编码的文档数据
    media_type: str = "application/pdf"      # MIME 类型
```

**协议支持差异：**
- **Anthropic**：原生支持 `document` content block 类型
- **Gemini**：通过 `Part.from_bytes()` 以 `inline_data` 形式传入
- **OpenAI**：不原生支持文档，降级为文本描述 `"[Document: application/pdf]"`

### `ContentPart`

`TextPart`、`ImagePart`、`DocumentPart` 的判别联合类型：

```python
ContentPart = Union[TextPart, ImagePart, DocumentPart]
```

### `ToolParameterSchema`

**注意：** `ToolParameterSchema` 在早期版本中存在，但当前版本已简化。工具参数定义直接使用标准 JSON Schema 字典格式，不再需要单独的数据类。

### `ToolDefinition`

工具的完整定义（协议无关中间格式），与 `BaseTool.get_description()` 的返回结构对应。

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]  # 标准 JSON Schema 字典
```

**重要：** `parameters` 字段使用标准 JSON Schema 格式，例如：

```python
parameters = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "要执行的命令"
        },
        "timeout": {
            "type": "integer",
            "description": "超时时间（秒）",
            "default": 30
        }
    },
    "required": ["command"]
}
```

### `ToolCall`

LLM 返回的工具调用请求（统一格式）。

```python
@dataclass
class ToolCall:
    id: str                                        # 工具调用 ID（OpenAI tool_call.id / Anthropic tool_use block id）
    name: str                                      # 工具名称
    arguments: Dict[str, Any]                      # 工具参数（已解析为 dict）
    gemini_thought_signature: Optional[bytes] = None  # Gemini thinking 模型附加的签名字节串
```

**`gemini_thought_signature`**：Gemini thinking 模型在工具调用 part 上附加的私有签名，回传历史消息时必须原样保留，否则 API 会拒绝。该字段不占用 :attr:`UnifiedMessage.reasoning`，只挂在产生它的 `ToolCall` 上。

**`to_dict() -> Dict[str, Any]`**：序列化为字典，`gemini_thought_signature` 以十六进制字符串形式保留（仅当非 None 时才包含该字段）。

### `ToolCallResult`

工具执行结果，用于回传给 LLM 的消息。

```python
@dataclass
class ToolCallResult:
    tool_call_id: str                                  # 对应 ToolCall.id
    tool_name: str
    content: str                                       # 工具执行结果文本（始终必填，作为纯文本回退）
    content_parts: Optional[List[ContentPart]] = None  # 多模态内容块（图片、文档等）
```

**`content_parts` 与 `content` 的关系：**
- `content` 始终必填，作为纯文本回退（日志、不支持多模态的场景）
- `content_parts` 可选，携带多模态内容（`ImagePart`、`DocumentPart` 等）
- 当 `content_parts` 存在时，LLM 客户端优先使用它构建协议消息
- 当 `content_parts` 为 `None` 时，行为与之前完全一致

### `UnifiedMessage`

统一消息格式（协议无关），一条消息对应一轮对话中某一方的发言。

```python
@dataclass
class UnifiedMessage:
    role: str                                      # "user" / "assistant" / "tool"
    text: Optional[str]                            # 纯文本内容（向后兼容）
    content_parts: Optional[List[ContentPart]]     # 多模态内容块（优先于 text）
    tool_calls: Optional[List[ToolCall]]           # 工具调用请求（assistant 发出）
    tool_results: Optional[List[ToolCallResult]]   # 工具执行结果（user 回传）
    reasoning: Optional[ReasoningArtifact]         # provider 思考产物（多态）
    produced_by_provider: Optional[str]            # vendor 标签，跨 provider 切换守卫使用
    produced_by_model: Optional[str]               # 模型名，signature 与模型绑定
```

**`reasoning` 字段**：参见下文 :class:`ReasoningArtifact` 节。不同 provider 存放不同子类实例，持久化时调用 `to_dict()`，反序列化时调 `ReasoningArtifact.from_dict(...)` 按 `kind` 辨别符分发。

**`content_parts` 与 `text` 的优先级：** 若两者同时存在，`content_parts` 优先。`text` 保留用于向后兼容和纯文本便捷访问。

**辅助方法：**
- `has_multimodal() -> bool`：是否包含非文本内容块（即 `content_parts` 中有 `ImagePart`）
- `get_text() -> Optional[str]`：获取文本内容（若 `content_parts` 存在则拼接其中所有 `TextPart`，否则返回 `text`）

**多模态消息示例：**

```python
msg = UnifiedMessage(
    role="user",
    content_parts=[
        TextPart(text="这张图片里有什么？"),
        ImagePart(source_type="base64", data="<base64_data>", media_type="image/jpeg"),
    ]
)
```

三种内容类型可在同一条消息中共存（例如 Anthropic 的 assistant 消息可同时含有文本和工具调用）。

### `ChatResponseV2`

`chat_completion_v2` 的统一响应对象（协议无关）。

```python
@dataclass
class ChatResponseV2:
    text: Optional[str]                       # LLM 返回的文本内容
    tool_calls: Optional[List[ToolCall]]      # LLM 请求调用的工具列表
    stop_reason: str                          # "stop" / "tool_use" / "max_tokens" / "end_turn"
    usage: Dict[str, int]                     # {"prompt_tokens": x, "completion_tokens": y, "total_tokens": z}
    raw_response: Any                         # 原始 SDK 响应对象
    reasoning: Optional[ReasoningArtifact]    # provider 思考产物（多态）
    produced_by_provider: Optional[str]       # vendor 标签
    produced_by_model: Optional[str]          # 实际生成模型
```

### `StreamChunk`

流式响应的单个 chunk，所有 provider 的流实现统一 yield 此类型。

**生命周期顺序：**
```
text_delta* → [tool_call_start → tool_call_delta* → tool_call_end]* → finish
```

| `type` | 有效字段 | 说明 |
|--------|---------|------|
| `"text_delta"` | `text_delta` | 纯文本增量 |
| `"thinking_delta"` | `thinking_delta` | 思考/推理增量（DeepSeek R1、Anthropic thinking_delta）|
| `"tool_call_start"` | `tool_call_name`, `tool_call_id` | 开始一个新的工具调用 |
| `"tool_call_delta"` | `tool_call_id`, `tool_call_arguments_delta` | 工具参数 JSON 增量 |
| `"tool_call_end"` | `tool_call_id`, `tool_call` | 工具调用参数接收完毕，携带聚合的 `ToolCall` |
| `"finish"` | `stop_reason`, `usage`, `reasoning`, `produced_by_provider`, `produced_by_model` | 流结束 |
| `"fail"` | `error_code`, `error_message` | 流异常终止 |

### `StreamResponse`

流式响应的最终聚合结果，流结束后由 provider 聚合返回。

```python
@dataclass
class StreamResponse:
    text: str = ""                            # 聚合的完整文本
    tool_calls: Optional[List[ToolCall]]      # 聚合的完整工具调用列表
    stop_reason: str = "stop"                # 停止原因
    usage: Dict[str, int]                     # token 用量
    reasoning: Optional[ReasoningArtifact]    # reasoning 产物
    produced_by_provider: Optional[str]       # 产生该响应的 provider
    produced_by_model: Optional[str]          # 产生该响应的模型
```

---

### `ReasoningArtifact` （多态思考产物）

各 LLM provider 返回的 reasoning 形状不一（DeepSeek 是纯文本，Anthropic 是结构化 block 数组）。以子类多态封装 shape 差异，避免给 :class:`UnifiedMessage` / :class:`ChatResponseV2` 逐个增加字段。

```python
@dataclass
class ReasoningArtifact:
    kind: str                              # 辨别符，子类覆盖
    def to_dict(self) -> Dict[str, Any]: ...
    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Optional["ReasoningArtifact"]: ...  # 按 kind 分发
```

**已有子类**：

| 子类 | `kind` | 字段 | 使用方 |
|------|--------|------|--------|
| `OpenAIReasoningContent` | `"openai_reasoning_content"` | `content: str` | `DeepSeekClient`（并可被同样使用 `reasoning_content` 的 OpenAI dialect 复用） |
| `AnthropicThinkingBlocks` | `"anthropic_thinking_blocks"` | `blocks: List[Dict[str, Any]]` | `AnthropicClient` / `KimiCodeClient` / `DeepSeekAnthropicClient` |

**为什么多态不是不透明 dict：**
- 各 dialect 的字段名 / 类型不同，需要静态区分（`isinstance(msg.reasoning, AnthropicThinkingBlocks)`）才能仅在同一 wire 协议下回传
- 子类集中封装序列化逻辑，避免 caller 需要记住“哪个 provider 该拼哪个字段”

**跨 provider 守卫**：:attr:`UnifiedMessage.produced_by_provider` 标签在各 client 的 build 阶段被检查；如不匹配当前 `_PROVIDER_TAG` （或型别与本联不一致），则丢弃 reasoning，仅保留 text/tool_calls。

---

## 辅助函数

### `tool_description_to_definition`

将 `BaseTool.get_description()` 的返回字典转换为 `ToolDefinition` 对象。

```python
from pyclaego.llm import tool_description_to_definition

bash_desc = bash_tool.get_description()
# {
#   "name": "bash",
#   "description": "执行 bash 命令",
#   "parameters": {
#     "command": {"type": "string", "required": True, "description": "命令内容"}
#   }
# }

tool_def = tool_description_to_definition(bash_desc)
# → ToolDefinition(name="bash", description="...", parameters={...})
```

### `convert_to_json_schema`

将工具参数的自定义格式转换为标准 JSON Schema 格式。此函数支持递归处理嵌套的 `object` 和 `array` 类型。

**功能：**
- 将参数级别的 `required: True/False` 收集到父级的 `required: ["field1", "field2"]` 数组中
- 递归处理 `object` 类型的 `properties` 字段
- 递归处理 `array` 类型的 `items` 字段
- 保留所有其他 JSON Schema 字段（`enum`、`pattern`、`minLength`、`maxLength` 等）

**示例：**

输入（自定义格式）：
```python
{
    "name": {"type": "string", "required": True, "description": "用户名"},
    "age": {"type": "integer", "required": False, "description": "年龄"},
    "tags": {
        "type": "array",
        "required": True,
        "items": {"type": "string"},
        "description": "标签列表"
    }
}
```

输出（标准 JSON Schema）：
```python
{
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "用户名"},
        "age": {"type": "integer", "description": "年龄"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "标签列表"
        }
    },
    "required": ["name", "tags"]
}
```

### `build_tool_definition`

构建特定协议的完整工具定义（OpenAI 或 Anthropic 格式）。**注意：此函数不通过 `__init__.py` 导出，需从 `types` 直接导入。**

```python
from pyclaego.llm.types import build_tool_definition

# 构建 Anthropic 格式
anthropic_tool = build_tool_definition(func_desc, provider="anthropic")
# {
#   "name": "bash",
#   "description": "执行命令",
#   "input_schema": {...}  # 标准 JSON Schema
# }

# 构建 OpenAI 格式
openai_tool = build_tool_definition(func_desc, provider="openai")
# {
#   "type": "function",
#   "function": {
#     "name": "bash",
#     "description": "执行命令",
#     "parameters": {...}  # 标准 JSON Schema
#   }
# }
```

### `serialize_llm_response`

将原始 LLM SDK 响应对象序列化为 JSON 安全结构，用于日志记录和审计。

```python
from pyclaego.llm import serialize_llm_response

safe_dict = serialize_llm_response(response.raw_response)
# 写入日志 / 持久化到记录文件
```

支持：
- **OpenAI `ChatCompletion`**：提取 `choices`、`usage`，以及 DeepSeek dialect 的 `reasoning_content`
- **Anthropic `Message`**：提取 `content` blocks（含 `thinking` / `redacted_thinking` 的 `signature`/`data`）
- **Gemini `GenerateContentResponse`**：提取 `candidates`、`usage_metadata`（含 `thoughts_token_count`）
- **其他类型**：降级为 `str(response)`

### `summarize_content_parts`

将 `ContentPart` 列表摘要为元数据（不含 base64 原始数据），用于写日志时避免输出大量二进制内容。

```python
from pyclaego.llm import summarize_content_parts

summary = summarize_content_parts(msg.content_parts)
# [
#   {"type": "text", "text": "这张图片里..."},
#   {"type": "image", "media_type": "image/jpeg", "data_length": 102400}
# ]
```

每个条目包含 `type`、`media_type`（若有）、`data_length`（若有）以及文本的前 200 字符（超出截断）。

---

## 抽象基类：`LLMClient`（`base.py`）

所有 LLM 客户端必须继承此类并实现三个抽象方法：

### `chat_completion(messages, temperature, max_tokens, **kwargs)`

旧版接口，接受 OpenAI 格式的 `List[Dict]` 消息列表，返回各 SDK 的原始响应对象。保留用于兼容旧版代码。

### `chat_completion_v2(system, messages, tool_list, temperature, max_tokens, tool_choice, **kwargs) -> ChatResponseV2`

**推荐使用的新版接口**，协议无关的统一 LLM 调用。

| 参数 | 类型 | 说明 |
|------|------|------|
| `system` | `Optional[str]` | 系统提示词（None 表示不传） |
| `messages` | `List[UnifiedMessage]` | 对话历史 |
| `tool_list` | `Optional[List[ToolDefinition]]` | 可用工具列表，None 表示不启用工具 |
| `temperature` | `Optional[float]` | 覆盖实例默认值 |
| `max_tokens` | `Optional[int]` | 覆盖实例默认值 |
| `tool_choice` | `Optional[str]` | `None`/"auto"（自动）/ "none"（禁用）/ 工具名（强制调用） |
| **返回** | `ChatResponseV2` | 统一响应，包含 text / tool_calls / stop_reason / usage / raw_response |

### `chat_completion_stream(system, messages, tool_list, temperature, max_tokens, tool_choice, **kwargs) -> AsyncGenerator[StreamChunk, None]`

流式 LLM 调用接口（协议无关）。参数签名与 `chat_completion_v2` 完全一致，逐 token yield `StreamChunk`。

基类提供默认实现：调用 `chat_completion_v2` 后将结果包装为合成的 `StreamChunk` 序列 yield（非真实流式）。各 provider 子类应覆盖此方法以提供真正的逐 token 流式输出。

### `get_info() -> Dict[str, Any]`

返回客户端的配置信息（model、base_url、temperature、max_tokens 等）。

---

## 工厂类：`LLMClientFactory`（`factory.py`）

### `create_client(...) -> LLMClient`

通过显式参数创建客户端实例。

```python
from pyclaego.llm import LLMClientFactory

# 创建 OpenAI 客户端
client = LLMClientFactory.create_client(
    api="openai",
    api_key="sk-xxx",
    base_url="https://api.openai.com/v1",
    model="gpt-4",
    temperature=0.7,
    max_tokens=4096
)

# 创建 Anthropic 客户端
client = LLMClientFactory.create_client(
    api="anthropic",
    api_key="sk-ant-xxx",
    model="claude-3-5-sonnet-20241022",
    temperature=0.7
)

# 创建 Gemini 客户端
client = LLMClientFactory.create_client(
    api="gemini",
    api_key="AIza-xxx",
    model="gemini-2.0-flash-001",
    temperature=0.7
)
```

### `create_from_config(config: Dict[str, Any]) -> LLMClient`

从配置字典创建客户端，通常结合 `ConfigManager.get()` 使用。

```python
from pyclaego.config import get_config
from pyclaego.llm import LLMClientFactory

config = get_config()
llm_config = config.get("llm.providers.kimi_code")
client = LLMClientFactory.create_from_config(llm_config)
```

`config` 字典必须包含 `api` 字段（`"openai"`、`"anthropic"` 或 `"gemini"`），其余字段（`api_key`、`base_url`、`model`、`temperature`、`max_tokens`）均为可选。

### 支持的 API 类型

| `api` 值 | 对应实现类 | 适用场景 |
|---------|----------|---------|
| `"openai"` | `OpenAIClient` | OpenAI 官方及严格兼容的 OpenAI 协议代理（不含 reasoning） |
| `"deepseek"` | `DeepSeekClient` | DeepSeek 官方端点（OpenAI 协议 + `reasoning_content` dialect） |
| `"gemini_openai"` | `GeminiOpenAIClient` | Gemini OpenAI 兼容端点（`/v1beta/openai/chat/completions`，含 `thought_signature` 往返） |
| `"anthropic"` | `AnthropicClient` | Anthropic Claude 官方（含 thinking blocks） |
| `"kimi_anthropic"` | `KimiCodeClient` | Kimi Code anthropic-compat 代理端点 |
| `"deepseek_anthropic"` | `DeepSeekAnthropicClient` | DeepSeek anthropic-compat 端点 |
| `"gemini"` | `GeminiClient` | Google Gemini Developer API 及 Vertex AI（原生 google-genai SDK） |

旧 `base_url` 嗅探回退仍然保留（发出 DeprecationWarning）以兼容未迁移的 `llm.yaml`。

---

## 具体实现

### `OpenAIClient`

- 底层使用 `openai.AsyncOpenAI`（异步）
- 支持 `base_url` 自定义，兼容任何 OpenAI 格式 API
- `api_key` 默认从环境变量 `OPENAI_API_KEY` 读取
- `chat_completion_v2` 内部将 `UnifiedMessage` 转换为 OpenAI `messages` 格式，工具定义转换为 `tools` 参数
- `chat_completion_v2` 当前实现保留 `max_tokens` 入参但默认不下发到 OpenAI API（用于兼容部分代理对 `max_tokens` 的限制）
- **多模态支持**：`content_parts` 中的 `ImagePart` 转换为 OpenAI `image_url` 格式（base64 → `data:<media_type>;base64,<data>`，URL → 直接传入）；`DocumentPart` 降级为文本描述（OpenAI 不原生支持文档类型）
- **工具结果多模态**：当 `ToolCallResult.content_parts` 存在时，工具结果消息包含多模态内容块（图片可作为 `image_url` 传入），`content` 作为文本描述同时保留
- **reasoning 钩子**：基类 `_extract_reasoning` / `_inject_reasoning` 均为 no-op；纯 OpenAI 不暴露 reasoning artifacts

### `DeepSeekClient`（继承 `OpenAIClient`）

- 在 OpenAI Chat Completions 协议之上叠加 `reasoning_content` 往返
- 覆盖 `_PROVIDER_TAG = "deepseek"`
- `_extract_reasoning` 从 `message.reasoning_content`（或 `model_extra`）提取 → 返回 :class:`OpenAIReasoningContent`
- `_inject_reasoning` 仅在严格 provider 兼容性检查后重发 `reasoning_content`，避免跨 provider 的污染
- 同样使用 `reasoning_content` 的未来 dialect 只需 `class XXXClient(DeepSeekClient): _PROVIDER_TAG = "xxx"` 即可

### `AnthropicClient`

- 底层使用 `anthropic.AsyncAnthropic`（异步）
- 支持 `base_url` 自定义，用于 Kimi coding 等代理端点
- `api_key` 默认从环境变量 `ANTHROPIC_API_KEY` 读取
- `max_tokens` 默认值为 `8192`（Anthropic API 要求必须显式传递）
- `chat_completion_v2` 将 `system` 单独提取，将 `UnifiedMessage` 转换为 Anthropic content blocks 格式
- **多模态支持**：`content_parts` 中的 `ImagePart` 转换为 Anthropic `image` content block（仅支持 base64；URL 来源需在调用前预先下载转换）；`DocumentPart` 转换为原生 `document` content block
- **思考模式**：响应中的 `thinking` / `redacted_thinking` block 全量收集为 :class:`AnthropicThinkingBlocks`；构建下轮请求时，`_is_compatible_thinking` 检查 `produced_by_provider == self._PROVIDER_TAG` 且模型名匹配后，将 blocks 原样放回 assistant content 开头（API 硬限制）

### `KimiCodeClient`（继承 `AnthropicClient`）

- Kimi Code anthropic-compat 代理端点。仅覆盖 `_PROVIDER_TAG = "kimi_anthropic"` 以隔离 thinking signature 的 vendor 范围。协议主体复用基类。

### `DeepSeekAnthropicClient`（继承 `AnthropicClient`）

- DeepSeek anthropic-compat 端点。`_PROVIDER_TAG = "deepseek_anthropic"`。同上，仅作 vendor 隔离。

### `GeminiOpenAIClient`（继承 `OpenAIClient`）

- 通过 OpenAI Chat Completions 协议调用 Gemini 的兼容端点（`/v1beta/openai/chat/completions`），适合通过通用 OpenAI SDK 或 LLM Router 调用 Gemini。
- `_PROVIDER_TAG = "gemini"`，与原生 `GeminiClient` 一致，跨 provider 守卫互认。
- 覆盖三个轻量钩子处理 `thought_signature` 往返：
  - `_extract_tool_call_extra(tc_raw)`：从响应 tool_call 的 `extra_content.google.thought_signature`（base64 字符串）提取后 base64 解码为 bytes，存入 `ToolCall.gemini_thought_signature`。
  - `_inject_tool_call_extra(tc_dict, tc)`：在下一轮请求构建时将签名 base64 编码后注入回 `extra_content.google.thought_signature`。
  - `_extract_streaming_tool_call_delta_extra(tc_delta)`：在 SSE 流中从首条 tool_call delta 提取签名（逻辑与非流式一致）。
- 不复制父类消息构建循环，与 `DeepSeekClient` 的扩展模式一致。

### `GeminiClient`

- 底层使用 `google-genai>=1.72.0` SDK（`google.genai.Client`，异步通过 `client.aio`）
- 支持 `base_url` 自定义，通过 `genai_types.HttpOptions(base_url=...)` 传入
- `api_key` 默认从环境变量 `GEMINI_API_KEY` 读取
- `chat_completion_v2` 通过 `GenerateContentConfig.system_instruction` 注入 system 提示词
- 角色映射：`assistant` → `"model"`，`tool_results` → `"tool"`（Gemini 协议要求）
- 工具调用：禁用 `automatic_function_calling`，所有函数调用由上层统一管理
- `tool_choice` 映射：`"auto"` → `FunctionCallingConfig(mode="AUTO")`，`"none"` → `mode="NONE"`，工具名 → `mode="ANY" + allowed_function_names`
- **多模态支持**：`ImagePart` 中的 base64 数据转换为 `Part.from_bytes(...)`，URL 来源转换为 `Part.from_uri(...)`；`DocumentPart` 同样通过 `Part.from_bytes()` 传入
- **工具结果多模态**：当 `ToolCallResult.content_parts` 存在时，在 `function_response` part 之后追加多模态 part
- **reasoning**：Gemini 以 `gemini_thought_signature` 挂在 :class:`ToolCall` 上（以 tool_call 为作用域），不占用 :attr:`UnifiedMessage.reasoning`

所有客户端均通过 `from ..logging import get_running_log` 获取运行日志，记录每次 API 调用的关键信息。

---

## 典型使用示例

### Agent 中调用 LLM

```python
from pyclaego.llm import LLMClientFactory, UnifiedMessage, ToolDefinition, tool_description_to_definition

# 创建客户端
client = LLMClientFactory.create_from_config(llm_config)

# 构建消息
messages = [
    UnifiedMessage(role="user", text="帮我列出当前目录下的文件")
]

# 构建工具定义
tool_defs = [tool_description_to_definition(tool.get_description()) for tool in tools]

# 调用
response = await client.chat_completion_v2(
    system="你是一个有用的 AI 助手。",
    messages=messages,
    tool_list=tool_defs,
    tool_choice="auto"
)

# 处理响应
if response.stop_reason == "tool_use":
    for tc in response.tool_calls:
        result = await execute_tool(tc.name, tc.arguments)
        # 将结果回传给 LLM...
elif response.stop_reason == "stop":
    print(response.text)
```

---

## 被其他模块引用的方式

```python
from ..llm import LLMClientFactory                          # 创建 LLM 客户端
from ..llm import UnifiedMessage, ToolCall, ToolCallResult  # 消息类型
from ..llm import ToolDefinition, tool_description_to_definition  # 工具类型
from ..llm import ChatResponseV2                            # 响应类型
from ..llm import ReasoningArtifact, OpenAIReasoningContent, AnthropicThinkingBlocks  # 思考产物
```

| 调用方模块 | 导入内容 | 用途 |
|-----------|---------|------|
| `src/agent/simple_agent.py` | `UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition` | 构建对话历史，处理工具调用循环 |
| `src/agent/spawn_agent.py` | `UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition` | 在主循环中拆分普通工具调用与子 Agent 工具调用 |
| `src/agent/think_agent.py` | `UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition` | Think-then-Act 两阶段中维护统一消息与工具调用 |
| `src/context/simple_context.py` | `UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition, tool_description_to_definition` | 上下文处理器中转换并维护历史消息 |
| `src/context/soul_context_v5.py` | `UnifiedMessage, ToolCall, ToolCallResult, ToolDefinition, tool_description_to_definition` | SoulContext 上下文处理器 |
| `src/security_executor/handler.py` | `ToolDefinition, UnifiedMessage, ChatResponseV2` | 安全执行器中封装 LLM V2 调用请求 |
| `src/security_executor/rules/llm_bash_review_rule.py` | `UnifiedMessage, ChatResponseV2` | Bash 命令安全审查规则中构造 V2 消息并解析响应 |
