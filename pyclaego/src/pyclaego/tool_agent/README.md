# tool_agent 模块 — 子代理配置系统

## 概述

`tool_agent` 模块提供**磁盘驱动的子代理配置**能力。每个子代理（Tool Agent）由磁盘上一个目录中的 `config.json` 文件定义，系统在启动时扫描并加载，供主 Agent 通过 `DynamicSpawnSubagentTool` 等机制动态派生子 Agent。

### 文件结构

```
tool_agent/
├── __init__.py      # 公开 API + get_tool_agent_manager()
├── config.py        # ToolAgentConfig 数据类（唯一数据模型）
├── exceptions.py    # 自定义异常层级
├── manager.py       # ToolAgentManager 单例（目录扫描 + 分层缓存）
└── registry.py      # SUBAGENT_PROFILES 全局注册表
```

### 目录分层（优先级低→高）

```
builtin/   ← 随项目发布的内置子代理（~/.pyclaego/tool_agents/builtin/）
global/    ← 用户全局自定义子代理（~/.pyclaego/tool_agents/）
PS/        ← PersonalSpace 级子代理（<ps_root>/<ps_id>/tool_agents/）
Widget/    ← Widget 级子代理（<ps_root>/<ps_id>/widgets/<widget_id>/tool_agents/）
```

同名子代理高优先级层覆盖低优先级层。

---

## `ToolAgentConfig`

`frozen=True` 的 dataclass，唯一数据模型，通过 `config.json` 定义。

### `config.json` 字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | 目录名 | 子代理名称（必填，须与目录名一致） |
| `description` | `str` | `""` | 给 LLM 看的能力描述（必填） |
| `system_prompt` | `str` | `""` | 系统提示词模板，支持 `{workspace_path}` / `{project_root}` 占位符（必填） |
| `subagent_type` | `str` | `"universal"` | 编排逻辑类型（目前仅 `"universal"`） |
| `allowed_tools` | `list[str] \| "*"` | `[]` | 工具白名单；`["*"]` 或 `"*"` 表示全部工具；`[]` 表示无工具 |
| `max_tool_rounds` | `int` | `20` | 最大工具调用轮次（≥1） |
| `context_strategy` | `str` | `"none"` | 上下文管理策略（见下表） |
| `llm` | `str` | `""` | LLM provider ID；空字符串表示继承父 Agent |
| `temperature` | `float \| null` | `null` | LLM 温度参数 |
| `workspace` | `str` | `"./workspace"` | 工作目录，相对于 `config.json` 所在目录 |
| `skills` | `list[str] \| "*"` | `[]` | 技能列表；`["*"]` 表示全部技能 |
| `metadata` | `dict` | `{}` | 扩展元数据（`version`、`tags` 等） |

**`context_strategy` 可选值：**

| 值 | 说明 |
|----|------|
| `"none"` | 不压缩上下文，直接累积 |
| `"summarizing"` | 超出限制时自动摘要历史 |
| `"soulv6"` | soulv6 压缩策略 |
| `"fork"` | fork 模式 |

### 关键属性

```python
cfg.source_dir          # config.json 所在目录（绝对路径）
cfg.resolved_workspace  # 解析后的工作目录绝对路径
cfg.uses_all_tools      # 是否配置为 allowed_tools = ["*"]
cfg.uses_all_skills     # 是否配置为 skills = ["*"]
```

### `render_system_prompt(**kwargs)`

将 `system_prompt` 模板渲染为实际字符串：

```python
prompt = cfg.render_system_prompt(
    workspace_path="/tmp/ws/agent_001",
    project_root="/home/user/myproject",
)
```

模板中引用了未提供的占位符时，尽力渲染（不抛异常）。

### `from_json(json_path)` 工厂方法

```python
cfg = ToolAgentConfig.from_json(Path("tool_agents/builtin/echo/config.json"))
```

加载后自动调用 `validate()`，校验失败时抛出 `ToolAgentConfigError`。

---

## `ToolAgentManager`

进程级单例，负责从磁盘目录发现、解析、缓存子代理配置。

```python
from pyclaego.tool_agent import get_tool_agent_manager

tam = get_tool_agent_manager()
```

### 初始化（`CoreScheduler.start()` 中调用）

```python
tam.load_builtins()                   # 扫描 builtin 层
tam.load_globals()                    # 扫描 global 层
tam.register_all_to_subagent_profiles()  # 注册到 SUBAGENT_PROFILES
```

### 目录配置

优先读取 `config.yaml` 中的 `tool_agents.directories` 列表；未配置时默认为：

```
~/.pyclaego/tool_agents/builtin/
```

### 分层缓存

内部使用 `_layer_cache: dict[str, dict[str, ToolAgentConfig]]`，cache key 格式：

| 层 | Key 格式 |
|----|---------|
| builtin | `"builtin"` |
| global | `"global"` |
| PS 层 | `"ps:<ps_id>"` |
| Widget 层 | `"widget:<ps_id>:<widget_id>"` |

### 核心 API

#### `resolve_for_widget(ps_id, widget_id) -> dict[str, ToolAgentConfig]`

返回指定 Widget 可用的全量子代理快照（已按优先级合并四层）。Widget 持有此快照，生命周期由 Widget 自身管理。

```python
agents = tam.resolve_for_widget("alice", "w_chat_default")
# {"echo": ToolAgentConfig(...), "code_explorer": ToolAgentConfig(...), ...}
```

#### `get_agent(name, ps_id=None, widget_id=None) -> ToolAgentConfig | None`

按名称查找单个配置，可指定 PS/Widget 作用域。

#### `list_agent_names(ps_id=None, widget_id=None) -> list[str]`

返回当前作用域内所有子代理名称（排序后）。

#### `resolve_skills_for_agent(cfg, session_id) -> list[str]`

处理 `skills = ["*"]` 通配符，展开为实际可用技能名称列表。

#### `register_all_to_subagent_profiles() -> int`

将 builtin + global 层的配置批量注册到 `SUBAGENT_PROFILES`（已注册的跳过）。返回本次新注册数量。

#### `reload() -> tuple[int, int]`

清除 builtin + global 层缓存并重新扫描，返回 `(builtin_count, global_count)`。

#### `clear_cache()`

清空全部分层缓存（含 PS/Widget 层）。

---

## `SUBAGENT_PROFILES` 注册表

进程级全局字典，`DynamicSpawnSubagentTool` 等工具从此处查找可用子代理类型。

```python
from pyclaego.tool_agent import (
    register_profile,
    get_profile,
    resolve_profile,
    unregister_profile,
    list_profile_names,
    SUBAGENT_PROFILES,
)
```

### `register_profile(profile)`

注册一个 `ToolAgentConfig`。同名已存在时抛出 `ValueError`（需先 `unregister_profile`）。

### `get_profile(name) -> ToolAgentConfig`

按名称查找原始配置（不含 YAML 覆盖）。未找到时抛出 `KeyError`。

### `resolve_profile(name, base_config) -> ToolAgentConfig`

合并 widget 级 YAML 覆盖，返回最终生效配置。

**LLM 解析优先级：**

```
base_config["subagents"][name]["llm"]   ← widget YAML 显式配置
  ↓ 未配置
profile.llm                             ← config.json 中的 llm 字段
  ↓ 为空
base_config["llm"]                      ← 继承主 Agent
  ↓ 未配置
"kimi_code"                             ← 最终兜底
```

`max_tool_rounds`、`skills`、`workspace` 也支持从 `base_config["subagents"][name]` 读取覆盖。

---

## 异常层级

```
ToolAgentError                ← 基础异常
├── ToolAgentConfigError      ← config.json 格式或校验错误
├── ToolAgentNotFoundError    ← 指定名称的子代理不存在
└── ToolAgentLoadError        ← 子代理加载失败
```

---

## 磁盘目录结构

每个子代理对应磁盘上一个同名目录：

```
tool_agents/
└── builtin/
    ├── echo/
    │   └── config.json
    ├── code_explorer/
    │   └── config.json
    └── pipeline/
        └── config.json
```

### 典型 `config.json` 示例

```json
{
  "name": "code_explorer",
  "description": "代码探索子 Agent，支持只读文件浏览工具，适合代码阅读与理解任务。",
  "system_prompt": "# Sub-Agent Identity\n你是代码探索子 Agent。\n工作目录：`{workspace_path}`\n探索目标：`{project_root}`",
  "subagent_type": "universal",
  "allowed_tools": ["glob", "list_directory", "read_file", "search_text", "write_file"],
  "max_tool_rounds": 15,
  "context_strategy": "summarizing",
  "llm": "",
  "temperature": null,
  "workspace": "./workspace",
  "skills": ["code-review-checklist"],
  "metadata": {
    "version": "1.0.0",
    "tags": ["builtin", "code", "exploration"]
  }
}
```

---

## 完整使用流程

```python
# 1. 启动时（CoreScheduler.start() 内）
from pyclaego.tool_agent import get_tool_agent_manager

tam = get_tool_agent_manager()
tam.load_builtins()
tam.load_globals()
tam.register_all_to_subagent_profiles()

# 2. Widget 初始化时，获取该 Widget 可用的子代理快照
agents_snapshot = tam.resolve_for_widget("alice", "w_chat_default")

# 3. 主 Agent 派生子 Agent 时，解析最终配置
from pyclaego.tool_agent import resolve_profile

final_cfg = resolve_profile("code_explorer", base_config={
    "llm": "claude-sonnet",
    "subagents": {
        "code_explorer": {"max_tool_rounds": 30}
    }
})
prompt = final_cfg.render_system_prompt(
    workspace_path="/tmp/ws/agent_001",
    project_root="/home/user/myproject",
)
```
