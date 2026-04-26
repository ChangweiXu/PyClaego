# security_executor 模块 — 安全执行器

## 概述

`security_executor` 模块在 Agent 与底层工具/LLM 之间充当安全中间层。它提供统一调用入口、规则审查（工具调用和 LLM 调用均支持）、路径占位符解析、调用记录写盘等能力。

### 文件结构

```
security_executor/
├── __init__.py              # 导出所有公开符号，并完成规则类型注册
├── base_rule.py             # 安全规则抽象基类、枚举类型
├── monitor.py               # SecurityMonitor - 规则引擎（钩子式 API）
├── rule_factory.py          # SecurityRuleFactory - 规则工厂
├── handler.py               # SecurityHandler - 对外接口（单例）
├── path_resolver.py         # PathResolver - 路径占位符解析器
└── rules/
    ├── __init__.py
    ├── keyword_match_rule.py      # 关键词匹配规则
    ├── tool_block_rule.py         # 工具黑名单规则
    ├── bash_command_list_rule.py  # Bash 命令白/黑名单规则
    ├── llm_bash_review_rule.py    # LLM 智能安全审查规则
    └── workspace_path_rule.py     # 工作目录路径限制规则
```

### 子 Agent 调用记录

子 Agent 的调用记录保存在独立的子目录中：

#### 子 Agent 工具调用记录（`tool_calls/{session_id}/subagents/{subagent_id}/*.json`）

格式与主 Agent 工具调用记录相同，但存储在 `subagents/{subagent_id}/` 子目录下。

#### 子 Agent LLM 调用记录（`llm_calls/{session_id}/subagents/{subagent_id}/*.json`）

格式与主 Agent LLM 调用记录相同，但存储在 `subagents/{subagent_id}/` 子目录下。文件名格式：`{timestamp}-{subagent_id}-{llm_id}.json`。

---

## 架构总览

```
Agent（调用方）
    │
    │ request_tool_call() / request_llm_call_v2() / request_llm_call_v3()
    ▼
SecurityHandler（单例，对外统一接口）
    │
    ├─── 1. SecurityMonitor.before_tool_call() / before_llm_call()（调用前规则审查）
    │         │
    │         └─── _evaluate_rules() → 遍历 rules → BaseSecurityRule.matches()
    │                                 → SecurityDecision: ALLOW / WARN / DENY
    │              ⚠️ 当前 _evaluate_rules 临时直接返回 ALLOW（过渡阶段）
    │
    ├─── 2. PathResolver.resolve()（路径占位符替换，仅工具调用）
    │         {{SKILL:xxx}} / {{WORKSPACE}} / {{SESSION_SKILL_ROOT}} / {{PROJECT}} / {{TEMP}}
    │
    ├─── 3. ToolManager.execute_tool()（工具执行）
    │         或 LLMClient.chat_completion_v2()（LLM 调用）
    │
    ├─── 4. SecurityMonitor.after_tool_call() / after_llm_call()（调用后审计记录）
    │
    └─── 5. _save_tool_call_record() / _save_llm_call_record_v2()（写盘）
```

---

## 核心类

### `SecurityHandler`（`handler.py`）

**单例，所有 Agent 的统一入口**。

#### 获取实例

```python
from pyclaego.security_executor import SecurityHandler

handler = SecurityHandler.get_instance()
# 或直接实例化（单例）
handler = SecurityHandler()
```

初始化时自动完成：
1. 实例化 `SecurityMonitor`（加载安全规则）
2. 调用 `get_skill_manager().load_skills()` 加载全局技能
3. 实例化 `PathResolver`，绑定技能路径查询回调

#### `request_tool_call(session_id, tool_name, tool_args) -> Dict`

申请工具调用的主方法。完整执行流程：

1. **安全审查**：调用 `SecurityMonitor.before_tool_call(session_id, subagent_id=None, tool_name, tool_args)`
2. **DENY 直接返回**：`{"success": False, "output": None, "error": "Security check failed: ..."}`
3. **WARN 记录警告**：继续执行但日志记录警告
4. **路径解析**：检测参数字符串中的路径占位符，调用 `PathResolver.resolve()`；先重新扫描 Session 技能目录
5. **工具执行**：`ToolManager.execute_tool(tool_name, **resolved_args)`
6. **调用后审计**：`SecurityMonitor.after_tool_call(...)` 记录结果
7. **写盘**：`_save_tool_call_record()` → `{workspace_root}/tool_calls/{session_id}/{timestamp}-{session_id}-{tool_name}.json`

返回格式：
```python
{
    "success": bool,
    "output": str | None,
    "error": str | None,
    "security_decision": "allow" | "warn" | "deny",
    "content_parts": list | None  # 多模态内容块（ImagePart/DocumentPart），由工具通过 ToolResult.content_parts 返回
}
```

当工具返回多模态内容时（如 `read_image_base64` 返回 `ImagePart`、`read_pdf` 返回 `DocumentPart`），`content_parts` 字段非 `None`，Agent 层将其传递到 `ToolCallResult.content_parts`，最终由 LLM 客户端转换为对应协议格式。

#### `request_llm_call_v2(session_id, llm_id, system, messages, tool_list, ...) -> Dict`

**推荐使用的 LLM 调用方法**，协议无关统一接口（V2）。

调用流程：
1. `SecurityMonitor.before_llm_call(...)` 安全审查
2. DENY 直接返回
3. `LLMClient.chat_completion_v2(...)` 执行调用
4. `SecurityMonitor.after_llm_call(...)` 调用后审计
5. `_save_llm_call_record_v2(...)` 写盘

| 参数 | 说明 |
|------|------|
| `session_id` | 会话 ID（用于记录和路径推断）|
| `llm_id` | `config.yaml` 中 `llm.providers` 下的 key，如 `"kimi_code"` |
| `system` | 系统提示词 |
| `messages` | `List[UnifiedMessage]` 统一格式消息历史 |
| `tool_list` | `List[ToolDefinition]`，None 表示不启用工具 |
| `temperature` / `max_tokens` | 可选，覆盖 LLM 客户端默认值 |
| `tool_choice` | `"auto"` / `"none"` / 工具名 |

返回格式：
```python
{
    "success": bool,
    "v2_response": ChatResponseV2 | None,  # 完整响应（含 tool_calls 等）
    "response": str | None,               # 兼容字段 = v2_response.text
    "usage": {"prompt_tokens": x, ...},
    "security_decision": str,
    "error": str | None
}
```

调用记录写盘路径：`{workspace_root}/llm_calls/{session_id}/{timestamp}-{session_id}-{llm_id}.json`

LLM 原始响应序列化支持三种协议：OpenAI `ChatCompletion`、Anthropic `Message`、Gemini `GenerateContentResponse`，其他类型回退为 `str()`。

#### `request_llm_call(**kwargs)`

**已废弃**，调用时抛出 `NotImplementedError`。请改用 `request_llm_call_v2`。

#### `request_llm_call_v3(session_task_handler, llm_id, system, messages, tool_list, ...) -> Dict`

V3 接口用于与 `SessionTaskHandlerV2` 打通任务日志与进度记录。内部复用 `request_llm_call_v2` 的核心调用逻辑，并追加结构化日志记录（通过 `SessionTaskHandlerV2` 创建 LLM_CALL 子任务）。

额外参数：
- `session_task_handler`: `SessionTaskHandlerV2` 实例，`session_id` 由其 `.get_session_id()` 获取
- `stream`: 是否流式输出（当前 v2 底层不保证支持）

返回格式与 `request_llm_call_v2` 相同。

---

#### 子 Agent 专用接口

SecurityHandler 为 SpawnAgent 提供了三个专用接口，用于支持子 Agent 的独立调用和执行。

##### `request_subagent_llm_call(session_id, subagent_id, llm_id, system, messages, task_handler, tool_list, ...) -> Dict`

子 Agent 专用的 LLM 调用接口。

**参数：**
| 参数 | 说明 |
|------|------|
| `session_id` | 父会话 ID |
| `subagent_id` | 子 Agent 的唯一标识符 |
| `llm_id` | LLM 配置 ID |
| `system` | 系统提示词 |
| `messages` | `List[UnifiedMessage]` |
| `task_handler` | `SessionTaskHandlerV2` 实例（**必填**，用于任务日志）|
| `tool_list` / `temperature` / `max_tokens` / `tool_choice` | 同 v2 |

**特点：**
- 经由 `SecurityMonitor.before_llm_call` / `after_llm_call` 审查与审计
- 日志路径：`{workspace_root}/llm_calls/{session_id}/subagents/{subagent_id}/{timestamp}-{subagent_id}-{llm_id}.json`
- 返回格式与 `request_llm_call_v2` 完全相同

##### `request_subagent_tool_call(session_id, subagent_id, tool_name, tool_args, task_handler) -> Dict`

子 Agent 专用的工具调用接口。

**参数：**
| 参数 | 说明 |
|------|------|
| `session_id` | 父会话 ID |
| `subagent_id` | 子 Agent 的唯一标识符 |
| `tool_name` | 工具名称 |
| `tool_args` | 工具参数字典 |
| `task_handler` | `SessionTaskHandlerV2` 实例（**必填**，用于任务日志）|

**特点：**
- 经由 `SecurityMonitor.before_tool_call` / `after_tool_call` 审查与审计
- 支持路径占位符解析（与主 Agent 一致）
- 日志路径：`{workspace_root}/tool_calls/{session_id}/subagents/{subagent_id}/{timestamp}.json`
- 返回格式与 `request_tool_call` 完全相同（包含 `content_parts` 字段）

##### `request_subagent_call(session_id, subagent_id, subagent_type, ...) -> Dict`

子 Agent 创建与执行的统一安全通道。

**参数：**
| 参数 | 说明 |
|------|------|
| `session_id` | 父会话 ID |
| `subagent_id` | 子 Agent 唯一标识 |
| `subagent_type` | 子 Agent 类型（SUBAGENT_REGISTRY 的键）|
| `subagent_handler` | AgentFactory.create_subagent 的引用 |
| `workspace_path` | 子 Agent 独立工作目录（已创建）|
| `base_config` | 主 Agent 配置（含 llm 等字段）|
| `context_handler` | BaseSubAgentContextHandler 实例（已初始化）|
| `user_message` | 任务消息 dict |
| `subagent_task_handler` | `SessionTaskHandlerV2` 实例（用于子 Agent 任务日志与进度）|

**返回格式：**
```python
{
    "success": bool,
    "output": str,           # 子 Agent 最终结果
    "error": str | None,
    "security_decision": str
}
```

**用途：**
主 Agent（SpawnAgent）调用此方法创建并执行子 Agent，确保所有子 Agent 的创建和执行都经过统一的安全管理通道。

---

### `SecurityMonitor`（`monitor.py`）

规则引擎，由 `SecurityHandler` 内部持有。提供钩子式 API，区分调用前审查与调用后审计。

#### 初始化

- 从 `config.yaml` 的 `security.rules` 列表加载规则（通过 `SecurityRuleFactory`）
- `security.enabled: false` 时跳过规则评估
- `security.log_enabled: true` 时将事件写入 `{workspace_root}/security_logs/security_YYYYMMDD.jsonl`

> ⚠️ **当前状态**：`_evaluate_rules()` 内部已在开头插入直接返回 ALLOW 的逻辑（过渡阶段），所有安全规则实际上不会被触发。规则加载逻辑仍保留，待后续恢复。

#### 调用前审查

```python
result = await monitor.before_tool_call(
    session_id="sess_abc",
    subagent_id=None,          # 子 Agent 调用时传 subagent_id
    tool_name="bash",
    tool_args={"command": "rm -rf /"}
)
# → {"decision": "allow"|"warn"|"deny", "reason": str, "matched_rules": [...]}

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

调用后审计仅记录事件，不做规则评估。

#### 规则评估优先级

遍历所有规则，**DENY 优先级最高，遇到即终止；WARN 优先级高于 ALLOW**。

---

### `SecurityRuleFactory`（`rule_factory.py`）

规则工厂类（类方法，不需要实例化）。

- **注册时机**：`__init__.py` 被导入时自动注册五种内置规则
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

必须实现的抽象方法：
```python
async def matches(self, request: Dict[str, Any]) -> bool:
    """判断请求是否触发本规则"""
```

`get_decision()` 根据 `action` 字段返回 `SecurityDecision.ALLOW / DENY / WARN`。

---

## 五种内置安全规则

### `KeywordMatchRule`（关键词匹配）

在请求指定字段中搜索敏感关键词。

```yaml
rule_type: "keyword_match"
rule_id: "block_sensitive_data"
enabled: true
request_types: ["llm_call", "tool_call"]
action: "deny"
keywords: ["password:", "api_key:", "secret:"]
search_fields: ["content", "messages"]  # 可选，默认 ["content"]
case_sensitive: false                    # 可选，默认 false
```

### `ToolBlockRule`（工具黑名单）

直接拒绝特定工具名称的调用。

```yaml
rule_type: "tool_block"
rule_id: "block_dangerous_tools"
enabled: true
request_types: ["tool_call"]
action: "deny"
blocked_tools: ["rm", "delete", "format", "drop_database"]
```

### `BashCommandListRule`（Bash 命令清单）

基于 allow/deny 列表审查 bash 命令。

```yaml
rule_type: "bash_command_list"
rule_id: "bash_command_whitelist"
enabled: true
request_types: ["tool_call"]
action: "warn"
allow_commands: ["ls", "cd", "pwd", "echo", "cat"]
deny_commands: ["rm", "dd", "mkfs"]
allow_compound_commands: false  # 是否允许 && || ; 等复合命令
strict_mode: false              # 仅允许白名单命令
```

审查优先级：检测复合命令 → deny 列表 → allow 列表 → 未知命令（warn）

### `LlmBashReviewRule`（LLM 智能审查）

调用 LLM 对 bash 命令进行智能分析，输出 XML 格式的安全报告（`safe / warn / deny`）。

```yaml
rule_type: "llm_bash_review"
rule_id: "llm_bash_review"
enabled: true
request_types: ["tool_call"]
action: "warn"   # 基础 action，实际决策由 LLM 审查结果（safe/warn/deny）决定
llm_id: "kimi_code"
```

### `WorkspacePathRule`（工作目录路径限制）

对指定工具检查其路径参数是否位于当前 session 的 workspace 目录之内，防止路径逃逸访问。

```yaml
rule_type: "workspace_path"
rule_id: "subagent_workspace_restriction"
enabled: true
request_types: ["tool_call"]
action: "deny"
restricted_tools:
  - "write_file"
  - "mkdir"
  - "download_file"
  - "read_file"
```

- 路径参数名按优先级依次检查：`path` → `dest` → `file_path` → `directory` → `dir_path`
- 支持 `{{WORKSPACE}}` 占位符（检查前替换为真实路径）
- workspace 推断逻辑与 `PathResolver.get_workspace_path` 一致

---

### `PathResolver`（`path_resolver.py`）

解析工具参数中的路径占位符，将其替换为真实绝对路径。

#### 支持的占位符

| 占位符 | 解析为 |
|--------|--------|
| `{{SKILL:skill_name}}` | 技能目录的绝对路径（全局或 Session 独有技能均支持）|
| `{{WORKSPACE}}` | Session 的工作目录（`workspace_root/{session_id}` 或自定义映射）|
| `{{SESSION_SKILL_ROOT}}` | Session 独有技能根目录（`workspace/{session_id}/skills`）|
| `{{PROJECT}}` | 项目根目录（`Path.cwd()`）|
| `{{TEMP}}` | 临时目录（`/tmp`）|

```python
# Agent 在工具参数中使用占位符
tool_args = {"command": "bash {{SKILL:python_helper}}/setup.sh {{WORKSPACE}}/output"}

# SecurityHandler 自动解析为真实路径
# → {"command": "bash /path/to/skills/python_helper/setup.sh /workspaces/sess_abc/output"}
```

#### Workspace 路径推断

`get_workspace_path(session_id)` 的逻辑：
1. 优先查 `config.yaml` 中 `session.session_workspace_root.<session_id>` 自定义映射
2. 否则使用 `session.workspace_root/{session_id}`

#### `get_skill_path_from_manager(skill_manager, skill_name, session_id)`

辅助函数，优先在 Session 独有技能中查找，再回退到全局技能。

---

## 调用记录格式

### 工具调用记录（`tool_calls/{session_id}/{timestamp}-{session_id}-{tool_name}.json`）

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
  "tool_name": "read_image_base64",
  "success": true,
  "output": "图片已读取: logo.png (image/png, 12345 bytes)",
  "content_parts_summary": [
    {"type": "image", "media_type": "image/png", "data_length": 16460}
  ]
}
```

### LLM 调用记录 V2（`llm_calls/{session_id}/{timestamp}-{session_id}-{llm_id}.json`）

```json
{
  "start_timestamp": "...",
  "end_timestamp": "...",
  "llm_id": "kimi_code",
  "tool_list": [
    {"name": "bash", "description": "...", "parameters": {...}}
  ],
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

`messages` 中的多模态内容（ImagePart / DocumentPart）的 `data` 字段截断为前 32 字符，避免日志膨胀。

---

## 依赖关系

### 导入的模块和函数

```python
from ..config import get_config         # 读取 security.* / session.* / llm.* 配置
from ..logging import get_running_log   # 运行日志记录
from ..skill import get_skill_manager   # 获取 SkillManager 单例（加载技能）
from ..llm import LLMClientFactory      # 创建 LLM 客户端（延迟导入）
from ..llm import ToolDefinition, UnifiedMessage, ChatResponseV2  # 类型定义
from ..tool import get_tool_manager     # 获取 ToolManager 单例（延迟导入）
from ..task_manager import SessionTaskHandlerV2, TaskType  # 任务日志
```

| 导入内容 | 来源 | 用途 |
|---------|------|------|
| `get_config()` | `src.config` | 读取 LLM 配置创建客户端；读取 session.workspace_root |
| `get_running_log()` | `src.logging` | 全程日志记录（安全决策、路径解析、执行结果）|
| `get_skill_manager()` | `src.skill` | 初始化时加载技能；路径占位符解析时查询技能路径 |
| `LLMClientFactory` | `src.llm` | 按需创建并缓存 LLM 客户端实例 |
| `get_tool_manager()` | `src.tool` | 执行工具调用 |
| `ToolDefinition, UnifiedMessage, ChatResponseV2` | `src.llm` | V2/V3 接口的类型参数 |
| `SessionTaskHandlerV2, TaskType` | `src.task_manager` | V3 / 子 Agent 接口的任务日志 |

### 被其他模块引用

```python
from ..security_executor import SecurityHandler
```

| 调用方模块 | 使用场景 |
|-----------|---------|
| `src/agent/echo_agent.py` | 主消息处理：`request_llm_call_v2` / `request_llm_call_v3` |
| `src/agent/simple_agent.py` | 工具循环：`request_tool_call` + `request_llm_call_v2` / `request_llm_call_v3` |
| `src/agent/think_agent.py` | THINK / ACT 两阶段调用 `request_llm_call_v2` |
| `src/context/agent_tools/spawn_subagent_tool.py` | 子 Agent 创建执行：`request_subagent_call` |
| `src/agent/subagent/echo_subagent.py` | 子 Agent 内部 LLM 调用：`request_subagent_llm_call` |
