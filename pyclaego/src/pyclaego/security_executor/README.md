# security_executor 模块 — 安全执行器

## 概述

`security_executor` 模块在 Agent 与底层工具/LLM 之间充当安全中间层。它提供统一调用入口、规则审查（工具调用和 LLM 调用均支持）、交互式用户确认（`QUERY` 决策）、调用记录写盘（含 TTL 自动清理）等能力。

### 文件结构

```
security_executor/
├── __init__.py              # 导出所有公开符号
├── base_rule.py             # 安全规则抽象基类、枚举类型（含 QUERY 决策）
├── monitor.py               # SecurityMonitor - 规则引擎（钩子式 API）
├── auditor.py               # SecurityAuditor - 安全事件日志（环形缓冲 + JSONL 落盘）
├── rule_factory.py          # SecurityRuleFactory - 规则工厂
├── handler.py               # SecurityHandler - 对外接口（单例）
├── query_service.py         # QueryService - 交互式用户确认队列（单例）
├── record_store.py          # RecordStore - 调用记录写盘 + TTL 自动清理（单例）
└── rules/
    ├── __init__.py                     # RULE_REGISTRY 注册表
    ├── llm_bash_review_rule.py         # LLM bash 命令安全审查
    ├── llm_safe_bash_review_rule.py    # LLM safe_bash 命令树审查
    ├── llm_python_review_rule.py       # LLM Python 代码安全审查
    ├── llm_safe_python_review_rule.py  # LLM safe_python 代码审查
    ├── tool_confirm_rule.py            # 暂停并请求用户确认（QUERY 决策）
    ├── tool_call_loop_detector_rule.py # 工具调用死循环检测
    ├── rate_limit_rule.py              # 每会话请求频率限制
    ├── cost_budget_rule.py             # LLM Token 用量预算限制
    ├── network_egress_rule.py          # 出站网络请求过滤（SSRF 防护）
    ├── secret_egress_rule.py           # 凭证/密钥外泄扫描
    ├── subagent_depth_rule.py          # 子 Agent 递归深度限制
    ├── file_size_rule.py               # 文件读写大小限制
    └── workspace_path_rule.py          # 工作目录路径限制（当前已禁用）
```

### 调用记录与 TTL 自动清理

所有调用记录（工具调用、LLM 调用）通过 `RecordStore` 统一管理：
- 记录写入 `{log_root}/tool_calls/` 和 `{log_root}/llm_calls/` 目录下的 JSON 文件
- 子 Agent 记录写入各自 `subagents/{subagent_id}/` 子目录
- 可通过 `logging.record_store.record_ttl_hours` 配置过期自动删除（0 = 不删除）

---

## 架构总览

```
Agent（调用方）
    │
    │ request_tool_call() / request_tool_call_v2()
    │ request_llm_call_v2() / request_llm_call_v3() / request_llm_call_stream()
    ▼
SecurityHandler（单例，对外统一接口）
    │
    ├─── 1. SecurityMonitor.before_tool_call() / before_llm_call()（调用前规则审查）
    │         │
    │         └─── _evaluate_rules() → 遍历 rules → BaseSecurityRule.matches()
    │                                 → SecurityDecision: ALLOW / WARN / DENY / QUERY
    │                                 → SecurityAuditor.log()（事件写入环形缓冲 + JSONL）
    │                                 → rule.on_request_completed()（通知状态化规则）
    │
    ├─── 2a. DENY → 直接返回错误
    │    2b. QUERY → QueryService.enqueue() → await wait_resolved()
    │                （挂起等待用户从前端选择 allow / deny，超时取 default）
    │    2c. WARN → 记录日志，继续执行
    │
    ├─── 3. ToolManager.execute_tool()（工具执行）
    │         或 LLMClient.chat_completion_v2()（LLM 调用）
    │         或 LLMClient.chat_completion_stream()（流式 LLM）
    │
    ├─── 4. SecurityMonitor.after_tool_call() / after_llm_call()（调用后审计）
    │
    └─── 5. RecordStore.write_tool_call() / write_llm_call()（写盘，含 TTL 管理）
```

---

## 核心类

### `SecurityHandler`（`handler.py`）

**单例，所有 Agent 的统一入口**。

#### 获取实例

```python
from src.security_executor import SecurityHandler

handler = SecurityHandler.get_instance()
# 或直接实例化（单例）
handler = SecurityHandler()
```

初始化时自动完成：
1. 实例化 `SecurityMonitor`（加载安全规则）
2. 调用 `get_skill_manager().load_skills()` 加载全局技能
3. 实例化 `RecordStore` 并启动 TTL 清理后台任务

> **注意**：`PathResolver` 已移除。`{{WORKSPACE}}`、`{{SKILL:name}}` 等路径占位符不再在此处解析，调用方需自行传入真实路径。

#### `request_tool_call(session_id, tool_name, tool_args) -> Dict`

申请工具调用的基础方法。完整执行流程：

1. **安全审查**：`SecurityMonitor.before_tool_call(session_id, subagent_id=None, tool_name, tool_args)`
2. **DENY → 直接返回错误**
3. **QUERY → 挂起等待用户确认**：通过 `QueryService` 向前端推送选择题，等待用户选择；deny 或 cancel 则终止，allow 则继续
4. **WARN → 继续执行**（记录日志）
5. 注入 `_session_id` 到 `tool_args`（供需要 session_id 的工具使用）
6. **工具执行**：`ToolManager.execute_tool(tool_name, **resolved_args)`
7. **调用后审计**：`SecurityMonitor.after_tool_call(...)`
8. **写盘**：`RecordStore.write_tool_call(...)`

返回格式：
```python
{
    "success": bool,
    "output": str | None,
    "error": str | None,
    "security_decision": "allow" | "warn" | "deny" | "query",
    "content_parts": list | None  # 多模态内容块（ImagePart/DocumentPart）
}
```

#### `request_tool_call_v2(loop_task_handler, tool_name, tool_args) -> Dict`

**推荐在 Agent Loop 中使用**，在 `request_tool_call` 基础上增加：
- 为每次工具调用自动创建 `TaskType.TOOL_EXECUTION` 子任务（start/complete/fail）
- 自动上报 `tool_args`、`tool_result`、`error_trace`、`file_edit` 等工件到子任务
- 自动识别文件副作用工具（`write_file`、`file_edit`、`mkdir` 等）并附加 `KIND_FILE_EDIT` 工件

```python
result = await handler.request_tool_call_v2(
    loop_task_handler=loop_handler,   # 父 agent loop 任务的 handler
    tool_name="write_file",
    tool_args={"path": "/tmp/out.txt", "content": "hello"},
)
```

返回格式与 `request_tool_call` 相同。

#### `request_llm_call_v2(session_id, llm_id, system, messages, tool_list, ...) -> Dict`

**协议无关统一 LLM 调用接口（V2）**。

调用流程：
1. `SecurityMonitor.before_llm_call(...)` 安全审查
2. DENY 直接返回
3. `LLMClient.chat_completion_v2(...)` 执行调用
4. `SecurityMonitor.after_llm_call(...)` 调用后审计
5. `RecordStore.write_llm_call(...)` 写盘

| 参数 | 说明 |
|------|------|
| `session_id` | 会话 ID |
| `llm_id` | `config.yaml` 中 `llm.providers` 下的 key |
| `system` | 系统提示词 |
| `messages` | `List[UnifiedMessage]` |
| `tool_list` | `List[ToolDefinition]`，None 表示不启用工具 |
| `temperature` / `max_tokens` | 可选，覆盖 LLM 客户端默认值 |
| `tool_choice` | `"auto"` / `"none"` / 工具名 |

返回格式：
```python
{
    "success": bool,
    "v2_response": ChatResponseV2 | None,
    "response": str | None,               # 兼容字段 = v2_response.text
    "usage": {"prompt_tokens": x, ...},
    "security_decision": str,
    "error": str | None
}
```

#### `request_llm_call(**kwargs)`

**已废弃**，调用时抛出 `NotImplementedError`。请改用 `request_llm_call_v2`。

#### `request_llm_call_v3(session_task_handler, llm_id, system, messages, ...) -> Dict`

V3 接口用于与 `SessionTaskHandlerV2` 打通任务日志与进度记录。在 `request_llm_call_v2` 基础上增加：
- 自动创建 `TaskType.LLM_CALL` 子任务
- 自动上报 LLM 响应工件（`llm_response`）

返回格式与 `request_llm_call_v2` 相同。

#### `request_llm_call_stream(session_task_handler, llm_id, system, messages, ...) -> AsyncGenerator[StreamChunk]`

**流式 LLM 调用（V3 安全包装版）**。

- 流开始前执行安全审查；`DENY` 时 yield 一个 `type="fail"` 的 `StreamChunk` 后终止
- 调用底层 `LLMClient.chat_completion_stream()` 并透传 `StreamChunk`
- 流结束后自动写盘、记录审计、上报工件并标记子任务完成/失败
- 发生异常时 yield `StreamChunk(type="fail", error_code="llm_stream_error", error_message=...)`

```python
async for chunk in handler.request_llm_call_stream(
    session_task_handler=task_handler,
    llm_id="kimi_code",
    system="...",
    messages=messages,
    tool_list=tools,
):
    if chunk.type == "text_delta":
        print(chunk.text_delta, end="", flush=True)
    elif chunk.type == "finish":
        stop_reason = chunk.stop_reason
    elif chunk.type == "fail":
        print("Error:", chunk.error_message)
```

---

#### 子 Agent 专用接口

##### `request_subagent_llm_call(session_id, subagent_id, llm_id, system, messages, task_handler, ...) -> Dict`

子 Agent 专用 LLM 调用接口。

| 参数 | 说明 |
|------|------|
| `session_id` | 父会话 ID |
| `subagent_id` | 子 Agent 唯一标识符 |
| `task_handler` | `SessionTaskHandlerV2` 实例（**必填**，用于任务日志）|

记录写盘路径：`{log_root}/llm_calls/{session_id}/subagents/{subagent_id}/{timestamp}-{subagent_id}-{llm_id}.json`

返回格式与 `request_llm_call_v2` 相同。

##### `request_subagent_tool_call(session_id, subagent_id, tool_name, tool_args, task_handler) -> Dict`

子 Agent 专用工具调用接口。

| 参数 | 说明 |
|------|------|
| `session_id` | 父会话 ID |
| `subagent_id` | 子 Agent 唯一标识符 |
| `task_handler` | `SessionTaskHandlerV2` 实例（**必填**，用于任务日志）|

记录写盘路径：`{log_root}/tool_calls/{session_id}/subagents/{subagent_id}/{timestamp}.json`

返回格式与 `request_tool_call` 相同（包含 `content_parts` 字段）。

##### `request_subagent_call(session_id, subagent_id, subagent_type, ...) -> Dict`

子 Agent 创建与执行的统一安全通道。SpawnAgent 调用此方法确保所有子 Agent 创建都经过统一安全管理。

返回格式：
```python
{
    "success": bool,
    "output": str,
    "error": str | None,
    "security_decision": str
}
```

---

### `SecurityMonitor`（`monitor.py`）

规则引擎，由 `SecurityHandler` 内部持有。

#### 初始化

- 从 `config.yaml` 的 `security.rules` 列表加载规则（通过 `SecurityRuleFactory`）
- `security.enabled: false` 时跳过规则评估（直接返回 ALLOW）
- 安全事件日志委托给 `SecurityAuditor`（`security.log_enabled: true` 时写 JSONL）

> **当前状态**：`_evaluate_rules()` 已完全启用，无临时 ALLOW bypass。规则按配置正常评估。

#### 调用前审查

```python
result = await monitor.before_tool_call(
    session_id="sess_abc",
    subagent_id=None,
    tool_name="bash",
    tool_args={"command": "ls /"},
)
# → {"decision": "allow"|"warn"|"deny"|"query", "reason": str, "matched_rules": [...], "query_spec": {...}}

result = await monitor.before_llm_call(
    session_id="sess_abc",
    subagent_id=None,
    llm_id="kimi_code",
    system="...",
    messages=[...],
    tool_list=[...],
    tool_choices="auto",
    kwargs={}
)
```

#### 调用后审计

```python
await monitor.after_tool_call(
    session_id, subagent_id, tool_name, tool_args,
    success=True, output="...", error=None
)

await monitor.after_llm_call(
    session_id, subagent_id, llm_id, system, messages,
    tool_list, tool_choices, kwargs,
    success=True, stop_reason="end_turn", error=None
)
```

调用后审计不做规则评估，仅记录事件。

#### 规则评估优先级

遍历所有规则，**DENY 优先级最高，遇到即终止；QUERY 次之；WARN 高于 ALLOW**。每次评估后：
1. 调用 `SecurityAuditor.log()` 将事件追加到环形缓冲并异步写 JSONL
2. 调用 `rule.on_request_completed()` 通知状态化规则更新内部计数器

---

### `SecurityAuditor`（`auditor.py`）

**安全事件日志记录器**，从 `SecurityMonitor` 中拆分出来。

```python
auditor = SecurityAuditor(
    log_root=Path("/var/log/pyclaego"),
    log_enabled=True,
    event_buffer_size=1000,   # 环形缓冲大小，超出后滚动丢弃最旧事件
)
```

- **内存侧**：`deque(maxlen=event_buffer_size)` 环形缓冲，避免无限增长
- **磁盘侧**：异步写 `{log_root}/security_logs/security_YYYYMMDD.jsonl`
- `snapshot(limit=None)` — 返回最近 N 条事件的浅拷贝

---

### `QueryService`（`query_service.py`）

**交互式用户确认队列（单例）**。用于实现 `QUERY` 决策：Agent 执行被挂起，等待用户从前端选择 allow / deny。

设计要点：
- 以 `session_id` 为键维护 FIFO 队列，每个 session 同一时间只有队头的 query 可被 resolve
- 广播回调（`set_broadcast_fn`）由 `PSGateway` 注册，用于向前端推送 `PendingQuery`
- `/stop` 控制帧触发 `clear_all()`，取消队列中所有 Future

#### 核心数据类

```python
@dataclass
class Choice:
    value: str       # 内部值（用于与 deny_values 对比）
    label: str       # 显示给用户的标签
    description: str = ""

@dataclass
class PendingQuery:
    query_id: str
    session_id: str
    origin: str          # "rule" | "tool"
    tool_name: str | None
    tool_args: dict | None
    prompt: str
    choices: list[Choice]
    deny_values: list[str]  # 命中则阻止
    default: str | None     # 超时时取此值
    timeout_s: int | None

@dataclass
class ResolveOutcome:
    kind: ResolveKind   # ACCEPTED | REJECTED | STOPPED | PASS_THROUGH
    query_id: str | None
    value: str | None
    reason: str | None
```

#### 主要方法

```python
qs = QueryService.get_instance()

# PSGateway 注册广播回调
qs.set_broadcast_fn(gateway.publish_to_ps)

# SecurityHandler 挂起等待：
query_id = await qs.enqueue(pending)
chosen = await qs.wait_resolved(query_id)  # 返回 value 字符串 或 CANCEL_SENTINEL

# PSGateway 路由入站消息：
outcome = await qs.try_resolve(session_id, text)
```

---

### `RecordStore`（`record_store.py`）

**统一调用记录写盘 + TTL 自动删除（单例）**。

```python
store = RecordStore.get_instance()
store.start()  # 启动 TTL 后台清理任务（幂等）
```

配置（`config.yaml`）：
```yaml
logging:
  log_root: /var/log/pyclaego  # 默认 PYCLAEGO_DEFAULT_LOGS_ROOT
  record_store:
    record_ttl_hours: 72       # 0 = 不自动删除
```

#### 公开写入方法

| 方法 | 写入路径 |
|------|---------|
| `write_llm_call(session_id, llm_id, ...)` | `{log_root}/llm_calls/{session_id}/{timestamp}-{session_id}-{llm_id}.json` |
| `write_tool_call(session_id, tool_name, ...)` | `{log_root}/tool_calls/{session_id}/{timestamp}-{session_id}-{tool_name}.json` |
| `write_subagent_llm_call(session_id, subagent_id, llm_id, ...)` | `{log_root}/llm_calls/{session_id}/subagents/{subagent_id}/{timestamp}-{subagent_id}-{llm_id}.json` |
| `write_subagent_tool_call(session_id, subagent_id, ...)` | `{log_root}/tool_calls/{session_id}/subagents/{subagent_id}/{timestamp}.json` |

TTL 后台任务每 1 小时扫描一次，删除 mtime 超过 `ttl_hours` 的记录文件。

---

### `SecurityRuleFactory`（`rule_factory.py`）

规则工厂类（类方法，不需要实例化）。

- **注册时机**：`rules/__init__.py` 被导入时，`RULE_REGISTRY` 自动暴露所有内置规则
- `create_rule(rule_config)` — 按 `rule_type` 创建规则实例
- `create_rules_from_config(rules_config)` — 批量创建，单条失败不影响其余
- `register_rule(rule_type, rule_class)` — 注册自定义规则类型

```python
SecurityRuleFactory.register_rule("my_rule", MyCustomRule)
```

---

### `BaseSecurityRule`（`base_rule.py`）

所有规则的抽象基类。

| 配置字段 | 说明 | 默认值 |
|---------|------|--------|
| `rule_type` | 规则类型（必填）| — |
| `rule_id` | 规则 ID（可选，用于日志）| `{rule_type}_unnamed` |
| `enabled` | 是否启用 | `False` |
| `request_types` | 适用请求类型列表 | `["llm_call", "tool_call"]` |
| `action` | 匹配时的决策 | `"warn"` |
| `tool_names` | 工具名称过滤（空 = 匹配所有）| `[]` |

必须实现的抽象方法：
```python
async def matches(self, request: Dict[str, Any]) -> bool:
    """判断请求是否触发本规则"""
```

可选覆盖的方法：
- `get_match_reason() -> str` — 返回最近一次匹配的人类可读原因
- `on_request_completed(request, result)` — 请求完成回调，用于状态化规则更新计数器
- `applies_to_tool(tool_name) -> bool` — 配合 `tool_names` 过滤器使用

`get_decision()` 根据 `action` 字段返回 `SecurityDecision.ALLOW / DENY / WARN / QUERY`。

**`SecurityDecision` 枚举值：**
| 值 | 含义 |
|----|------|
| `ALLOW` | 允许执行 |
| `DENY` | 拒绝执行 |
| `WARN` | 允许但记录警告 |
| `QUERY` | 挂起，等待用户交互确认 |

---

## 十二种内置安全规则

### `LlmBashReviewRule`（LLM bash 审查）

调用 LLM 对 bash 命令进行智能分析，输出 XML 格式安全报告（`safe / warn / deny`）。

```yaml
rule_type: "llm_bash_review"
rule_id: "llm_bash_review"
enabled: false
request_types: ["tool_call"]
action: "warn"
llm_id: "kimi_code"
timeout: 30
fallback_action: "warn"   # LLM 调用失败时的降级决策
deny_on_deny: true        # LLM 输出 deny 时升级为 DENY 决策
```

### `LlmSafeBashReviewRule`（LLM safe_bash 审查）

继承自 `LlmBashReviewRule`，目标工具为 `safe_bash` / `safe_bash_executor`。提取 `command_tree` 结构并序列化后送给 LLM 审查；解析失败时回退为原始字符串。

```yaml
rule_type: "llm_safe_bash_review"
```

### `LlmPythonReviewRule`（LLM Python 审查）

对 `python_exec` 工具的代码内容进行 LLM 智能审查（文件操作、网络请求、subprocess、eval/exec 等风险）。继承 `LlmBashReviewRule` 的 LLM 调用基础设施，使用专为 Python 定制的 prompt。

```yaml
rule_type: "llm_python_review"
```

### `LlmSafePythonReviewRule`（LLM safe_python 审查）

继承自 `LlmPythonReviewRule`，目标工具为 `safe_python` / `safe_python_executor`。

```yaml
rule_type: "llm_safe_python_review"
```

### `ToolConfirmRule`（用户确认）

遇到特定工具时暂停执行，向用户推送选择题（`QUERY` 决策）。`action` 强制为 `"query"`。

```yaml
rule_type: "tool_confirm"
rule_id: "confirm_bash"
enabled: true
request_types: ["tool_call"]
tool_names: ["bash"]
query_spec:
  prompt: "Allow running this bash command?\n```\n{tool_args.command}\n```"
  choices:
    - {value: allow, label: "Yes, run it"}
    - {value: deny,  label: "No, cancel"}
  deny_values: [deny]
  default: deny
  timeout_s: 300
```

`prompt` 中支持 `{tool_args.<key>}` 简单模板替换。

### `ToolCallLoopDetectorRule`（死循环检测）

检测同一会话中连续相同（tool_name + args 哈希）的工具调用，防止 Agent 陷入死循环。

```yaml
rule_type: "tool_call_loop_detector"
rule_id: "tool_loop_detector"
enabled: false
request_types: ["tool_call"]
action: "warn"
threshold: 3    # 连续重复次数触发阈值
window: 10      # 保留最近 N 次调用历史
```

### `RateLimitRule`（频率限制）

基于滑动窗口的每会话请求频率限制（分钟级 + 小时级，工具调用和 LLM 调用分别计数）。

```yaml
rule_type: "rate_limit"
rule_id: "per_session_rate_limit"
enabled: false
request_types: ["tool_call", "llm_call"]
action: "deny"
tool_calls_per_min: 60
llm_calls_per_min: 30
tool_calls_per_hour: 600
llm_calls_per_hour: 300
```

### `CostBudgetRule`（Token 预算）

累计每会话 LLM Token 用量，超出预算后拒绝进一步请求。支持按 provider 配置定价并换算 USD 预算。

```yaml
rule_type: "cost_budget"
rule_id: "session_cost_budget"
enabled: false
request_types: ["llm_call"]
action: "deny"
max_total_tokens: 500000
max_input_tokens: 0         # 0 表示不限制
max_output_tokens: 0
max_usd: 0.0
pricing:
  kimi_code:
    input_per_1k: 0.0015
    output_per_1k: 0.003
```

### `NetworkEgressRule`（出站网络过滤）

对 `web_fetch` / `download_file` 等工具检查目标 URL 的主机名和 IP，防止 SSRF 攻击和私有网络访问。

```yaml
rule_type: "network_egress"
rule_id: "network_egress_filter"
enabled: false
request_types: ["tool_call"]
action: "deny"
tool_names: ["web_fetch", "web_fetch_v2", "web_search", "download_file"]
block_private_networks: true     # 拦截 RFC1918 / loopback / link-local
denied_hosts:
  - "169.254.169.254"            # AWS/GCP 元数据接口
  - ".internal"
denied_cidrs:
  - "10.0.0.0/8"
  - "172.16.0.0/12"
  - "192.168.0.0/16"
  - "127.0.0.0/8"
  - "169.254.0.0/16"
allowed_hosts: []                # 非空时进入严格白名单模式
```

### `SecretEgressRule`（凭证外泄扫描）

扫描工具参数中的常见凭证/密钥模式（AWS Access Key、GitHub Token、JWT、PEM 私钥、通用密码字段等），防止 AI 意外泄露凭证。

```yaml
rule_type: "secret_egress"
rule_id: "secret_egress_scan"
enabled: false
request_types: ["tool_call"]
action: "deny"
tool_names: ["web_fetch", "web_fetch_v2", "download_file", "write_file", "file_edit"]
extra_patterns:
  - "api_key\\s*=\\s*[A-Za-z0-9]{20,}"  # 自定义额外正则
```

### `SubagentDepthRule`（子 Agent 递归深度限制）

对 `spawn_subagent` 工具调用检查递归深度和总数量，防止子 Agent 无限嵌套。

```yaml
rule_type: "subagent_depth"
rule_id: "subagent_depth_limit"
enabled: false
request_types: ["tool_call"]
action: "deny"
max_depth: 3
max_total_subagents_per_session: 20
```

### `FileSizeRule`（文件大小限制）

对 `read_file` / `read_pdf` / `read_image_base64` 检查文件大小；对 `write_file` / `file_edit` 检查 content 长度。

```yaml
rule_type: "file_size"
rule_id: "file_size_limit"
enabled: false
request_types: ["tool_call"]
action: "deny"
max_read_bytes: 5242880    # 5 MiB
max_write_bytes: 2097152   # 2 MiB
```

### `WorkspacePathRule`（工作目录限制）【当前已禁用】

对指定工具检查路径参数是否位于当前 session 的 workspace 目录之内。

> ⚠️ 该规则在 widget 迁移阶段已禁用（`rules/__init__.py` 中注释掉）。

---

## 调用记录格式

### 工具调用记录

```json
{
  "start_timestamp": "2026-04-07T12:34:56",
  "end_timestamp": "2026-04-07T12:34:57",
  "tool_name": "bash",
  "tool_args": {"command": "ls /workspaces/sess_abc"},
  "security_decision": "allow",
  "success": true,
  "output": "file1.txt\nfile2.py",
  "error": "",
  "content_parts_summary": null
}
```

当工具返回多模态内容时，`content_parts_summary` 包含元数据摘要（不含 base64 数据）：
```json
{
  "content_parts_summary": [
    {"type": "image", "media_type": "image/png", "data_length": 16460}
  ]
}
```

### LLM 调用记录 V2

```json
{
  "start_timestamp": "...",
  "end_timestamp": "...",
  "llm_id": "kimi_code",
  "tool_list": [{"name": "bash", "description": "...", "parameters": {...}}],
  "messages": [...],
  "security_decision": "allow",
  "success": true,
  "response": {...},
  "error": "",
  "temperature": 0.7,
  "max_tokens": 8192,
  "tool_choice": "auto",
  "kwargs": {}
}
```

`messages` 中多模态内容（ImagePart / DocumentPart）的 `data` 字段截断为前 32 字符，避免日志膨胀。

---

## 依赖关系

### 导入的模块和函数

```python
from ..config import get_config, PYCLAEGO_DEFAULT_LOGS_ROOT
from ..logging import get_running_log
from ..skill import get_skill_manager
from ..llm import LLMClientFactory, ToolDefinition, UnifiedMessage, ChatResponseV2, StreamChunk
from ..llm import serialize_llm_response, summarize_content_parts
from ..tool import get_tool_manager
from ..task_manager import ArtifactReporter, SessionTaskHandlerV2, TaskType
```

| 导入内容 | 来源 | 用途 |
|---------|------|------|
| `get_config()` | `src.config` | 读取 LLM / session / security 配置 |
| `get_running_log()` | `src.logging` | 全程日志记录 |
| `get_skill_manager()` | `src.skill` | 初始化时加载技能 |
| `LLMClientFactory` | `src.llm` | 按需创建并缓存 LLM 客户端实例 |
| `get_tool_manager()` | `src.tool` | 执行工具调用 |
| `ToolDefinition, UnifiedMessage, ChatResponseV2, StreamChunk` | `src.llm` | V2/V3/Stream 接口的类型参数 |
| `serialize_llm_response, summarize_content_parts` | `src.llm` | 日志序列化辅助函数 |
| `ArtifactReporter, SessionTaskHandlerV2, TaskType` | `src.task_manager` | 任务日志与工件上报 |

### 被其他模块引用

```python
from ..security_executor import SecurityHandler
```

| 调用方模块 | 使用场景 |
|-----------|---------|
| `src/agent/echo_agent.py` | 主消息处理：`request_llm_call_v2` / `request_llm_call_v3` |
| `src/agent/simple_agent.py` | 工具循环：`request_tool_call_v2` + `request_llm_call_v3` |
| `src/agent/think_agent.py` | THINK / ACT 两阶段调用 `request_llm_call_v2` |
| `src/context/agent_tools/spawn_subagent_tool.py` | 子 Agent 创建执行：`request_subagent_call` |
| `src/agent/subagent/echo_subagent.py` | 子 Agent 内部 LLM 调用：`request_subagent_llm_call` |

```python
from ..security_executor import QueryService, get_query_service
```

| 调用方模块 | 使用场景 |
|-----------|---------|
| `src/personal_space/gateway.py`（PSGateway）| 注册广播回调；路由入站消息至 `try_resolve()` |
