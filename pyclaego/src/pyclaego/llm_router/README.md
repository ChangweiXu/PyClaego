# llm_router 模块 — 本地 LLM 转发代理

## 概述

`llm_router` 是一个运行在 localhost 的 LLM 转发代理（pass-through proxy）。它接受 OpenAI / Anthropic / Gemini / Ollama 格式的 REST 请求，根据请求体或路径中的 `model` 字段匹配配置中的路由规则，将请求转发到真实上游，仅重写模型名、URL 和认证头，响应原样回传。

每次调用都会：
1. 以 JSON 文件形式归档到磁盘（凭证被脱敏）
2. 在 SQLite 数据库中记录一行统计数据（延迟、Token 用量等）

### 文件结构

```
llm_router/
├── __init__.py                    # 公开导出：RouterConfig, create_app, load_router_config
├── app.py                         # FastAPI 应用工厂（lifespan + 中间件 + 路由注册）
├── config.py                      # RouterConfig 数据类 + load_router_config()
├── routing.py                     # RouteTable — (protocol, alias) → ResolvedRoute 索引
├── forwarder.py                   # OutboundForwarder — per-upstream httpx.AsyncClient
├── usage_extract.py               # 各协议 token 用量提取函数
├── handlers/
│   ├── base.py                    # HandlerContext, resolve_or_404, record_call 共享工具
│   ├── openai_handler.py          # OpenAI unary + stream handler
│   ├── anthropic_handler.py       # Anthropic unary + stream handler
│   ├── gemini_handler.py          # Gemini unary + stream handler
│   └── ollama_handler.py          # Ollama unary + stream handler（协议转译为 OpenAI）
├── inbound/
│   ├── openai_routes.py           # POST /v1/chat/completions, /v1/completions, /v1/embeddings, GET /v1/models
│   ├── anthropic_routes.py        # POST /v1/messages
│   ├── gemini_routes.py           # POST /v1beta/models/{model}:{action}
│   └── ollama_routes.py           # POST /api/chat, /api/generate, /api/embed, /api/show; GET /api/tags, /api/version
├── recording/
│   ├── call_dumper.py             # CallDumper — JSON 文件归档（脱敏）
│   ├── stats_store.py             # StatsStore — SQLite 调用统计
│   ├── stream_merger.py           # 流式 chunk → 等价非流式响应体合并
│   └── masker.py                  # 凭证脱敏（headers / body / query params）
└── translators/
    └── ollama_openai.py           # Ollama ↔ OpenAI 请求/响应格式纯函数转译
```

---

## 架构概览

```
客户端（Claude Code / Cursor / LLM SDK 等）
    │  OpenAI / Anthropic / Gemini / Ollama 协议
    ▼
┌─────────────────────────────────────────────────────┐
│  inbound/  ← FastAPI 路由层，按 model 分流             │
│  _InboundLogger 中间件（记录 inbound 请求到 NDJSON）   │
└───────────────┬─────────────────────────────────────┘
                │ (protocol, alias) → ResolvedRoute
                ▼
        RouteTable（内存索引）
                │ route.upstream_model, route.upstream
                ▼
┌─────────────────────────────────────────────────────┐
│  handlers/   ← 各协议 handler（重写 model + auth）    │
└───────────────┬─────────────────────────────────────┘
                │ httpx.AsyncClient（per-upstream）
                ▼
        OutboundForwarder
                │
                ▼
    真实上游（OpenAI / Anthropic / Gemini / OpenRouter / Ollama…）
                │
                ▼
┌─────────────────────────────────────────────────────┐
│  recording/  ← CallDumper（JSON 归档）+ StatsStore   │
└─────────────────────────────────────────────────────┘
```

---

## 配置（`config.py`）

### 配置来源

通过 `ConfigManager` 读取顶层 `llm_router` 键，通常在 `config.yaml` 中以 `!include` 引入独立文件：

```yaml
# config.yaml
llm_router: !include "./.config.d/llm_router.yaml"
```

### `llm_router.yaml` 结构示例

```yaml
server:
  host: "127.0.0.1"
  port: 18790

storage:
  call_dump_dir: "./.data/llm_router/calls"
  stats_db_path: "./.data/llm_router/stats.db"
  dump_enabled: true
  mask_keys:
    - "api_key"
    - "x-api-key"
    - "authorization"

upstreams:
  - id: "openai_official"
    protocol: "openai"           # openai | anthropic | gemini | ollama
    base_url: "https://api.openai.com/v1"
    api_key: "sk-xxx"
    headers: {}
    models:
      - alias: "gpt-4o"
        upstream_model: "gpt-4o"
      - alias: "gpt-4o-mini"
        upstream_model: "gpt-4o-mini"

  - id: "anthropic_official"
    protocol: "anthropic"
    base_url: "https://api.anthropic.com"
    api_key: "sk-ant-xxx"
    headers: {}
    models:
      - alias: "claude-sonnet"
        upstream_model: "claude-sonnet-4-7-20250219"

  - id: "gemini_official"
    protocol: "gemini"
    base_url: "https://generativelanguage.googleapis.com"
    api_key: "AIza-xxx"
    headers: {}
    models:
      - alias: "gemini-2.5-pro"
        upstream_model: "gemini-2.5-pro-preview-05-06"

  - id: "local_ollama"
    protocol: "ollama"
    base_url: "http://127.0.0.1:11434"
    api_key: ""
    headers: {}
    models:
      - alias: "qwen3:8b"
        upstream_model: "qwen3:8b"
```

### 配置数据类

| 数据类 | 主要字段 |
|--------|---------|
| `RouterConfig` | `server: ServerConfig`, `storage: StorageConfig`, `upstreams: tuple[UpstreamConfig, ...]` |
| `ServerConfig` | `host: str` (默认 `"127.0.0.1"`), `port: int` (默认 `18790`) |
| `StorageConfig` | `call_dump_dir: Path`, `stats_db_path: Path`, `dump_enabled: bool`, `mask_keys: tuple[str, ...]` |
| `UpstreamConfig` | `id: str`, `protocol: str`, `base_url: str`, `api_key: str`, `headers: dict`, `models: tuple[ModelEntry, ...]` |
| `ModelEntry` | `alias: str`（客户端发来的名称）, `upstream_model: str`（实际转发的名称）|

**`RouterConfigError`**：配置缺失或非法（包括同一 upstream 内 alias 重复）时抛出。

### `load_router_config() -> RouterConfig`

从全局 `ConfigManager` 加载并校验配置，返回 `RouterConfig`。

---

## 路由（`routing.py`）

### `RouteTable`

启动时由 `RouterConfig` 构建的内存索引，将 `(protocol, alias)` 映射到 `ResolvedRoute`。

```python
@dataclass(frozen=True)
class ResolvedRoute:
    upstream: UpstreamConfig   # 完整上游配置
    alias: str                 # 客户端模型名
    upstream_model: str        # 实际转发的模型名
    protocol: str              # 由 upstream.protocol 派生
```

**方法：**

| 方法 | 说明 |
|------|------|
| `resolve(protocol, alias) -> ResolvedRoute \| None` | 按协议 + 别名精确查找路由 |
| `list_aliases(protocol=None) -> list[ResolvedRoute]` | 列出所有（或指定协议的）路由 |

---

## FastAPI 应用（`app.py`）

### `create_app(config?) -> FastAPI`

构建 FastAPI 应用实例，`config` 为 `None` 时自动调用 `load_router_config()`。

**Lifespan 流程：**
1. 创建 `OutboundForwarder`，为每个 upstream 打开 `httpx.AsyncClient`（支持 HTTP/2）
2. 创建 `CallDumper`，准备落盘目录
3. 创建 `StatsStore`，连接（或创建）SQLite 数据库并建表
4. 将上述对象挂载到 `app.state`（`config`/`routes`/`forwarder`/`dumper`/`stats`）
5. 关闭时：关闭所有 httpx 客户端、关闭数据库连接

**中间件：**
- `_InboundLogger`：将每条入站请求（method + path + body 摘要）异步追加到 `{call_dump_dir}/inbounds/YYYYMMDD.ndjson`，`/healthz` 和 `/_router` 前缀路径跳过记录。

**元数据端点：**

| 端点 | 说明 |
|------|------|
| `GET /healthz` | 返回 `status: ok`、所有 upstream id 及完整 alias 列表 |
| `GET /_router/models` | 返回所有路由的详细信息（protocol / alias / upstream / upstream_model）|

---

## 入站路由（`inbound/`）

各协议路由器以 `APIRouter` 形式注册，在 `create_app` 中 `include_router` 挂载。

### OpenAI（`openai_routes.py`）

| 端点 | 说明 |
|------|------|
| `POST /v1/chat/completions` | 按 `stream` 字段分流到 `OpenAIStreamHandler` / `OpenAIUnaryHandler` |
| `POST /v1/completions` | 同上 |
| `POST /v1/embeddings` | 始终走非流式（embeddings 无流式场景） |
| `GET /v1/models` | 返回 openai 协议下所有 alias 的模型列表（OpenAI `{object: list, data: [...]}` 格式）|

### Anthropic（`anthropic_routes.py`）

| 端点 | 说明 |
|------|------|
| `POST /v1/messages` | 按 `stream` 字段分流到 `AnthropicStreamHandler` / `AnthropicUnaryHandler` |

### Gemini（`gemini_routes.py`）

| 端点 | 说明 |
|------|------|
| `POST /v1beta/models/{model}:generateContent` | Gemini 非流式 |
| `POST /v1beta/models/{model}:streamGenerateContent` | Gemini 流式（NDJSON 换行分隔 JSON） |

模型名从路径参数 `{model_action}` 中按 `:` 拆分提取。

### Ollama（`ollama_routes.py`）

| 端点 | 说明 |
|------|------|
| `POST /api/chat` | chat completions（默认流式；`stream: false` 走非流式）|
| `POST /api/generate` | text generation（同上）|
| `POST /api/embed` / `/api/embeddings` | embeddings（非流式）|
| `POST /api/show` | model details（alias → upstream_model 名称替换）|
| `GET /api/tags` | 返回所有 ollama alias 列表 |
| `GET /api/version` | 返回静态版本 stub |

**Ollama 流式默认规则**：缺省或 `stream` 字段非 `false` 时均走流式，与 Ollama 客户端的默认行为一致。

---

## Handler 层（`handlers/`）

### 公共工具（`base.py`）

**`HandlerContext`**：从 `request.app.state` 提取 `routes` / `forwarder` / `dumper` / `stats`，避免每个 handler 重复解包。

**`resolve_or_404(routes, protocol, alias) -> ResolvedRoute`**：查不到路由时抛出 HTTP 404（含协议感知的错误详情）。

**`record_call(...)`**：构造 `CallRecord` 并并发写 `CallDumper`（JSON 归档）和 `StatsStore`（SQLite 统计）。

### 各协议 Handler

每个协议有 `XxxUnaryHandler` 和 `XxxStreamHandler` 两个静态类，均提供 `handle(request, ...) -> Response`。

**通用流程（以 OpenAI 为例）：**
1. 解析请求体 JSON
2. 提取 `model` 字段，调用 `resolve_or_404` 查路由
3. 重写请求体中的 `model` 为 `upstream_model`
4. 注入认证头（OpenAI → `Authorization: Bearer <key>`；Anthropic → `x-api-key`；Gemini → `x-goog-api-key`）
5. 通过 `OutboundForwarder` 发送请求
6. 提取 token 用量（`usage_extract`）
7. 调用 `record_call` 记录归档

**流式 Handler** 额外：
- 使用 `forwarder.forward_stream()` context manager，以原始 SSE/NDJSON 字节流式中继给客户端（`StreamingResponse`）
- 捕获全部 chunks 供 `stream_merger` 合并为等价非流式体，存入归档的 `merged_body` 字段

**Ollama Handler** 额外（`ollama_handler.py`）：
- Ollama 协议在内部转译为 OpenAI 格式转发到上游（通过 `translators/ollama_openai.py`）
- 上游响应再从 OpenAI 格式反译回 Ollama 格式返回给客户端
- 支持通过 OpenRouter / Moonshot / DeepSeek 等 OpenAI 兼容上游代理 Ollama 格式客户端

---

## 出站转发（`forwarder.py`）

### `OutboundForwarder`

为每个 upstream 维护一个 `httpx.AsyncClient`（HTTP/2 启用，60s 连接超时，600s 读取超时），由 lifespan 管理生命周期。

| 方法 | 说明 |
|------|------|
| `startup()` | 创建所有 upstream 的 httpx 客户端 |
| `shutdown()` | 关闭所有客户端 |
| `client_for(upstream) -> httpx.AsyncClient` | 按 upstream id 取客户端 |
| `forward_unary(...) -> httpx.Response` | 完整响应转发 |
| `forward_stream(...) -> AsyncContextManager[httpx.Response]` | 流式响应转发（caller 负责消费字节流）|

**头部过滤：**
- 转发请求时去掉 hop-by-hop 头（`host`、`content-length`、`transfer-encoding`、`authorization`、`x-api-key`、`x-goog-api-key` 等），由 handler 重新注入认证头
- 回传响应时同样去掉 hop-by-hop 头

---

## 记录子系统（`recording/`）

### `CallDumper`（`call_dumper.py`）

将每次调用写入 `{call_dump_dir}/YYYYMMDD/HHMM/{HHMMSS}_{rand}_{protocol}_{alias}.json`（按半小时分桶）。写入在线程池中异步执行，不阻塞 asyncio 事件循环。

**`CallRecord` 字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `protocol` | `str` | 协议名 |
| `alias` | `str` | 客户端模型别名 |
| `upstream_id` | `str` | 上游 id |
| `upstream_model` | `str` | 实际模型名 |
| `request` | `dict` | 转发出去的请求（已脱敏） |
| `response` | `dict` | 上游返回的响应 |
| `timing` | `dict` | 延迟信息（started_at / first_byte_at / finished_at / latency_ms / ttft_ms）|
| `usage` | `dict` | Token 用量（prompt / completion / total）|
| `stream` | `bool` | 是否流式调用 |
| `error` | `str \| None` | 转发失败时的异常类名 |
| `merged_body` | `dict \| None` | 流式调用合并后的等价非流式响应体 |

### `StatsStore`（`stats_store.py`）

SQLite 单写者（`aiosqlite`），schema 极简，每次调用插入一行：

```sql
CREATE TABLE calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT,     -- ISO 8601 UTC
    protocol          TEXT,
    alias             TEXT,
    upstream_id       TEXT,
    upstream_model    TEXT,
    status            INTEGER,
    latency_ms        INTEGER,
    ttft_ms           INTEGER,  -- 首字节延迟（ms），流式调用有效
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    stream            INTEGER,
    error             TEXT,
    dump_path         TEXT      -- 对应 JSON 文件路径
);
```

建有 `alias` 和 `ts` 两个索引。

### `stream_merger.py`

将各协议流式 chunks 合并为等价非流式响应体，存入 `CallRecord.merged_body` 便于审计：

| 函数 | 说明 |
|------|------|
| `merge_openai_stream(chunks)` | 从 SSE `data:` 行重建 `chat.completion` 对象（支持 `content`、`reasoning_content`、`tool_calls` 累积）|
| `merge_anthropic_stream(chunks)` | 从 Anthropic SSE 事件重建 `Message` 对象（content blocks 累积）|
| `merge_gemini_stream(chunks)` | 从 NDJSON 流重建 `GenerateContentResponse` |

### `masker.py`

凭证脱敏工具，将声明的 key 名（大小写不敏感）替换为 `***REDACTED***`：

- `mask_headers(headers, mask_keys)` — 同时强制脱敏 `authorization`
- `mask_query_params(params, mask_keys)`
- `mask_body(body, mask_keys)` — 递归深拷贝 dict/list 树，匹配键名即脱敏

---

## Token 用量提取（`usage_extract.py`）

各协议返回 `(prompt_tokens, completion_tokens, total_tokens)` 三元组（值均为 `int | None`）：

| 函数 | 适用场景 |
|------|---------|
| `extract_openai_unary(body)` | OpenAI 非流式响应 `body.usage` |
| `extract_openai_stream(chunks)` | OpenAI 流式（从末尾 chunk 找 `usage`，需 `stream_options.include_usage=true`）|
| `extract_anthropic_unary(body)` | Anthropic 非流式响应 `body.usage` |
| `extract_anthropic_stream(chunks)` | Anthropic 流式（从 `message_delta` 事件提取 `usage`）|
| `extract_gemini_unary(body)` | Gemini 非流式响应 `usageMetadata` |
| `extract_gemini_stream(chunks)` | Gemini 流式（从末尾 chunk 找 `usageMetadata`）|
| `extract_ollama_unary(body)` | Ollama 非流式响应 `body.prompt_eval_count` / `eval_count` |
| `extract_ollama_stream(chunks)` | Ollama 流式（从最后一个含 `done:true` 的 chunk 提取）|

---

## Ollama 协议转译（`translators/ollama_openai.py`）

Ollama handler 在内部将请求转为 OpenAI 格式、响应从 OpenAI 转回 Ollama 格式，支持通过任意 OpenAI 兼容上游代理 Ollama 客户端。

**请求转译：**

| 函数 | 说明 |
|------|------|
| `req_ollama_chat_to_openai(body)` | `/api/chat` → `/v1/chat/completions`（messages 直传，`options.*` 映射为 temperature/top_p/max_tokens/seed，`format: json` → `response_format`）|
| `req_ollama_generate_to_openai(body)` | `/api/generate` → `/v1/chat/completions`（`prompt`/`system` 包装为 messages）|

**响应转译：**

| 函数 | 说明 |
|------|------|
| `resp_openai_to_ollama_chat(openai_body, model)` | 非流式 OpenAI → Ollama `/api/chat` 响应格式 |
| `resp_openai_to_ollama_generate(openai_body, model)` | 非流式 OpenAI → Ollama `/api/generate` 响应格式 |
| `resp_openai_stream_chunk_to_ollama_chat(chunk, model)` | 流式 SSE chunk → Ollama streaming 响应行 |
| `resp_openai_stream_chunk_to_ollama_generate(chunk, model)` | 同上，`/api/generate` 格式 |

---

## 启动方式

LLM Router 有两种启动方式：

**1. 通过 `core_server` 内嵌启动（`enable_llm_router: true`）：**
```yaml
# config.yaml
core:
  enable_llm_router: true
```

**2. 独立 CLI 命令：**
```bash
# 使用默认配置（从 ConfigManager 加载）
pyclaego-llm-router

# 等价：
uvicorn pyclaego.llm_router.app:create_app --factory --host 127.0.0.1 --port 18790
```

启动后验证：
```bash
curl http://127.0.0.1:18790/healthz
curl http://127.0.0.1:18790/_router/models
```

---

## 典型使用场景

### 1. 将 Claude Code 指向本地代理（Anthropic 协议）

```yaml
# config.yaml - upstreams
- id: "claude"
  protocol: "anthropic"
  base_url: "https://api.anthropic.com"
  api_key: "sk-ant-xxx"
  models:
    - alias: "claude-sonnet-4-7-20250219"
      upstream_model: "claude-sonnet-4-7-20250219"
```

Claude Code 设置：`ANTHROPIC_BASE_URL=http://127.0.0.1:18790`

### 2. 通过 OpenRouter 代理多个模型（OpenAI 协议）

```yaml
- id: "openrouter"
  protocol: "openai"
  base_url: "https://openrouter.ai/api/v1"
  api_key: "sk-or-xxx"
  models:
    - alias: "deepseek-r1"
      upstream_model: "deepseek/deepseek-r1"
    - alias: "qwen3-235b"
      upstream_model: "qwen/qwen3-235b-a22b"
```

### 3. 查询所有已配置的路由

```bash
curl http://127.0.0.1:18790/_router/models | python3 -m json.tool
```
