# config 模块 — 配置管理

## 概述

`config` 模块提供全局和 Session 级别的配置管理能力。它以单例模式维护一个 `ConfigManager` 实例，支持多路径配置文件查找、YAML 自定义标签、环境变量替换、配置项引用与拼接、配置文件拆分引入，以及敏感字段遮盖。

### 文件结构

```
config/
├── __init__.py                  # 导出所有公开符号 + 默认路径常量
├── manager.py                   # ConfigManager 核心实现（YAML 加载、标签、引用解析）
├── resolver.py                  # deep_merge / resolve_layers（纯函数，无状态）
├── json_loader.py               # JSON 配置加载器（复用 YAML 标签语义）
├── personal_space_config.py     # PersonalSpaceConfigManager + get_ps_config / get_ps_widget_config
docs/
  ├── CONFIG_PARSING.md                # 解析流水线总览
  ├── CONFIG_RESOLVE_WALKTHROUGH.md    # _resolve_config_references 逐步演示
  ├── FIX_RISKY_BEHAVIORS.md           # 历史风险修复记录
  ├── TEST_CASE_WALKTHROUGHS.md        # 测试用例说明
  └── INCLUDE_WALKTHROUGH.md           # !include / !include_dir 逐步演示
```

---

## 核心类与函数

### `ConfigManager`

配置管理器主类，负责：

| 职责 | 说明 |
|------|------|
| **配置文件搜索** | 按优先级顺序搜索：`~/pyclaego/config.yaml` → `./config.yaml` |
| **默认配置** | 内置 `server` / `client` / `logging` 三个段的默认值 |
| **深度合并** | 用户配置以深度合并方式覆盖默认配置，不会整体替换嵌套字典 |
| **环境变量替换** | 加载阶段递归处理 `${ENV_VAR}` / `${ENV_VAR:default}` 语法 |
| **配置项引用** | 合并后解析 `@{config.key.path}` 引用，防止循环引用 |
| **YAML 标签** | 支持 `!concat [...]` 字符串拼接、`!abs_path "..."` 路径展开、`!join_path [...]` 路径组件拼接并解析为绝对路径、`!include "path"` 引入单个子配置文件、`!include_dir "path"` 合并目录内所有 .yaml/.yml 文件、`!include_merge "path"` 内联合并引入文件的键到父字典 |
| **敏感字段遮盖** | `show_config()` 时自动将 key/password/secret/token 字段替换为 `***MASKED***` |

#### 配置访问

```python
from pyclaego.config import get_config

config = get_config()

# 点号路径访问
host = config.get("server.host")           # "127.0.0.1"
port = config.get("server.port")           # 8765
llm  = config.get("llm.default_provider")  # "kimi_code"

# 快捷方法
server_cfg  = config.get_server_config()   # Dict[str, Any]
client_cfg  = config.get_client_config()   # Dict[str, Any]
logging_cfg = config.get_logging_config()  # Dict[str, Any]

# 完整配置字典（深度副本）
full_cfg = config.to_dict()                   # Dict[str, Any]
```

---

### `get_config(config_path=None) -> ConfigManager`

返回全局单例 `ConfigManager`，第一次调用时创建并缓存。

```python
from pyclaego.config import get_config

config = get_config()                          # 使用自动搜索
config = get_config("/custom/path/config.yaml")  # 指定配置文件
```

---

### `get_session_config(session_id, workspace_path=None, config_path=None) -> ConfigManager`

返回合并了 Session 级配置的 `ConfigManager`。

执行流程：
1. 调用 `get_config()` 获取全局配置
2. 确定 Session 工作目录（参数 > 配置中的 `session.session_workspace_root` 映射 > 默认 `workspace_root/{session_id}`）
3. 在工作目录下查找 `config.yaml`
4. 仅将 Session 配置中的 `agent` / `context` / `context_subagents` / `session_metadata` / `cron` 段合并到全局配置中
5. 返回合并后的新 `ConfigManager` 实例（不修改全局单例）

```python
from pyclaego.config import get_session_config
from pathlib import Path

# 自动推断工作目录
cfg = get_session_config("session-abc123")

# 显式指定工作目录
cfg = get_session_config("session-abc123", workspace_path=Path("/workspaces/session-abc123"))
```

---

## 三种配置语法与解析顺序（IRON RULE）

配置系统提供三种互补的语法，它们**各司其职、不可互相替代**：

| 语法 | 作用域 | 能否内嵌在字符串中 | 典型用途 |
|------|--------|-------------------|----------|
| `${VAR:default}` | 外部输入（OS 环境变量） | ✅ 可以 | 密钥、主机端口等运行时覆盖 |
| `@{key.path}` | 内部交叉引用（配置项之间） | ✅ 可以 | 消除重复，DRY 原则 |
| `!tag` | 结构化变换（节点级） | ❌ 不可以 | 路径拼接、文件引入等 |

### 为什么是三种而不是一种

YAML 标签（`!tag`）是**节点级**语法，无法出现在字符串中间。如果只保留 `!tag`，则：

```yaml
# ❌ 所有内嵌变量的字符串都必须拆成 !concat 序列
base_url: !concat ["https://", !env "API_HOST:localhost", ":", !env "API_PORT:8080", "/v1"]

# ✅ 当前写法 — 自然、简洁
base_url: "https://${API_HOST:localhost}:${API_PORT:8080}/v1"
```

三种语法分别对应解析管线中不同的阶段，**顺序固定、不可调换**。

### 解析管线（Resolution Pipeline）

配置加载时严格按以下顺序执行，每一步的输入是上一步的输出：

```
┌─────────────────────────────────────────────────────────┐
│  Step 1 — YAML 解析                                     │
│  yaml.safe_load() 读取文件                               │
│  !tag 节点被构造为 Tag 对象（IncludeTag / ConcatTag 等）  │
│  但此时 **不求值**                                       │
├─────────────────────────────────────────────────────────┤
│  Step 2 — 文件引入展开  !include / !include_dir  ← 新增 │
│  递归将 IncludeTag / IncludeDirTag 替换为实际文件内容     │
│  相对路径相对于当前文件目录；循环引入立即报错             │
│  展开后的子树将与主树一同进入后续步骤                     │
├─────────────────────────────────────────────────────────┤
│  Step 3 — 环境变量替换    ${VAR} / ${VAR:default}        │
│  递归遍历所有节点（含 Tag 对象内部的字符串）              │
│  将 ${...} 替换为 os.environ 中的值或默认值               │
│  整串匹配时自动类型转换（int / float / bool）             │
├─────────────────────────────────────────────────────────┤
│  Step 4 — 深度合并    deep_merge                         │
│  将用户配置以深度合并方式覆盖默认配置                     │
├─────────────────────────────────────────────────────────┤
│  Step 5 — 配置引用 + Tag 求值    @{key} / !tag           │
│  递归解析 @{...} 引用（带循环检测）                       │
│  对 ConcatTag / AbsPathTag / JoinPathTag 求值为最终字符串 │
│  Tag 内部的 @{...} 在此步一并解析                        │
└─────────────────────────────────────────────────────────┘
```

**关键推论：**

- `!include` 在 Step 2 完成，被引入文件的内容与主文件内容合并为一棵树，后续步骤统一处理。
- 因此被引入文件中的 `${...}` 和 `@{...}` 与主文件行为完全一致。
- `${...}` 在 Step 3 完成，因此 `!tag` 和 `@{...}` 内部可以包含 `${...}`（已被替换为纯值）。
- `@{...}` 在 Step 5 完成，因此 `@{...}` 引用的目标值可以包含已替换的 `${...}` 结果。
- `!tag`（非 include）在 Step 5 完成，因此 Tag 的参数中可以同时使用 `@{...}` 和已替换的 `${...}`。
- `${...}` 中**不能**使用 `@{...}`（Step 3 时配置引用尚未解析）。
- `!include` 路径是字面字符串，**不支持** `${...}` 展开（Step 2 在 Step 3 之前）。

---

## YAML 特性说明

### 环境变量替换

```yaml
server:
  host: ${HOST:127.0.0.1}      # 读取 HOST 环境变量，不存在时使用 127.0.0.1
  port: ${PORT:8765}           # 整数类型会自动转换
  debug: ${DEBUG:false}        # 布尔类型会自动转换
```

- 格式：`${VAR}` 或 `${VAR:default_value}`
- 默认值缺失且环境变量不存在时，抛出 `ValueError`
- 当整个字符串是一个环境变量引用时，会自动尝试将结果转换为 `int` / `float` / `bool`

### 配置项引用

```yaml
client:
  server_url: !concat ["ws://", "@{server.host}", ":", "@{server.port}"]
```

- 格式：`@{config.key.path}` — 引用已解析的其他配置项
- `!concat [...]` — 将列表中所有元素拼接为字符串
- 支持在 `!concat` 中混合字面量字符串和 `@{...}` 引用

### 路径展开

```yaml
session:
  workspace_root: !abs_path "~/pyclaego/workspaces"
```

- `!abs_path "..."` — 展开 `~/` 并转换为绝对路径
- 路径内也可以包含 `@{...}` 引用

### 路径组件拼接

```yaml
logging:
  log_file: !join_path ["@{pyclaego.root_path}", "logs", "app.log"]
  # → /home/user/pyclaego/logs/app.log
```

- `!join_path [...]` — 将列表中的路径组件用 `os.path.join` 拼接，展开 `~/`，并解析为绝对路径
- 与 `!concat` + `!abs_path` 的区别：使用操作系统原生路径分隔符语义，若某组件为绝对路径则前面的组件被丢弃（标准 `os.path.join` 行为）
- 列表中的各组件同样支持 `@{...}` 引用

### 配置文件拆分

当主配置文件体积过大时，可以用 `!include` 和 `!include_dir` 将配置拆分到多个文件。

#### `!include "path"` — 引入单个文件

```yaml
# config.yaml
llm:
  providers: !include "./llm-providers.yaml"
```

```yaml
# llm-providers.yaml（内容直接替换上面的节点）
kimi_code:
  api: "anthropic"
  api_key: ${KIMI_CODE_API_KEY:}
  model: "k2p5"
```

#### `!include_dir "path"` — 合并目录内所有 .yaml/.yml 文件

```yaml
# config.yaml
tools: !include_dir "./conf.d/tools/"
```

```
conf.d/tools/
├── 01-bash.yaml       # { bash: { ... } }
├── 02-web_search.yaml # { web_search: { ... } }
└── 03-read_file.yaml  # { read_file: { ... } }
```

文件按名称字典序加载，后加载的文件覆盖同名键（last-wins）。

#### `!include_merge "path"` — 内联合并引入文件的键到父字典

```yaml
# config.yaml
parent:
  existing_key: value
  _ext1: !include_merge "extra.yaml"   # 哨兵键名任意，合并后不出现在结果中
  _ext2: !include_merge "more.yaml"
```

- 被引入文件的顶层必须是 dict，否则抛出 `ConfigIncludeError`
- 合并顺序与 YAML 中 `!include_merge` 节点出现的顺序一致
- 与 `!include` 的区别：`!include` 是替换当前键的值；`!include_merge` 是把引入文件的键提升到父 dict 层级

#### 路径规则

| 写法 | 解析基准 |
|------|----------|
| `./relative/path` | 相对于**当前包含文件所在目录** |
| `~/home/path` | 展开为用户主目录 |
| `/abs/path` | 绝对路径直接使用 |

#### 错误处理

| 情况 | 行为 |
|------|------|
| 文件/目录不存在 | 抛出 `ConfigIncludeError`（不静默回退） |
| 循环引用（A→B→A） | 抛出 `ConfigIncludeError`，错误信息包含完整引用链 |
| `!include_dir` 文件顶层非字典 | 抛出 `ConfigIncludeError` |

> **注意**：`!include` 路径是字面字符串，不支持 `${VAR}` 展开。被引入文件的**内容**中可以正常使用 `${VAR}`、`@{ref}` 和其他 `!tag`。

---

## 配置文件结构示例

```yaml
server:
  host: ${HOST:127.0.0.1}
  port: ${PORT:18765}

client:
  server_url: !concat ["ws://", "@{server.host}", ":", "@{server.port}"]
  reconnect_interval: 3

logging:
  level: ${LOG_LEVEL:INFO}
  log_root: !abs_path "~/pyclaego/logs"
  file_enabled: true
  console_enabled: true

# 拆分为独立文件 — 引用单个文件
llm:
  default_provider: "kimi_code"
  providers: !include "./llm-providers.yaml"

# 拆分为目录 — 合并目录内所有 .yaml/.yml
tools: !include_dir "./conf.d/tools/"

session:
  workspace_root: !abs_path "~/pyclaego/workspaces"
```

---

## 默认路径常量（`__init__.py`）

`config` 包在导入时根据 `Path.home() / "pyclaego"` 计算一组绝对路径常量，作为系统范围的**唯一真相来源**。
所有子系统应直接导入这些常量，而非硬编码字符串。

```python
from pyclaego.config import (
    PYCLAEGO_DEFAULT_ROOT,        # ~/pyclaego/
    PYCLAEGO_DEFAULT_LOG_ROOT,    # ~/pyclaego/logs/       — LogManager / RunningLog
    PYCLAEGO_DEFAULT_LOGS_ROOT,   # ~/pyclaego/.logs/      — SecurityExecutor 审计记录
    PYCLAEGO_DEFAULT_CACHE_ROOT,  # ~/pyclaego/.cache/     — TaskArtifactStore / public_paths
    PYCLAEGO_DEFAULT_WORKSPACES,  # ~/pyclaego/workspaces/ — session workspace root
)
```

---

## `resolver.py` — 深合并工具

无状态纯函数模块，供 `PersonalSpaceConfigManager` 与其他加载器共用。

```python
from pyclaego.config import deep_merge, resolve_layers
```

### `deep_merge(*layers) -> Dict`

递归深合并多层配置（后者覆盖前者）：
- `dict + dict` → 递归合并
- 其他类型（包括 list）→ 后者整体替换前者
- None / 空 dict 层跳过
- 返回新对象，不修改输入

### `resolve_layers(layers: Iterable[Dict]) -> Dict`

`deep_merge` 的列表形式入口，便于编程式构造层序。

---

## `json_loader.py` — JSON 配置加载器

PersonalSpace / Widget 的运行时配置以 **JSON** 形式存储（便于 Web UI 读写）。
本模块在 JSON 上提供与 YAML `ConfigManager` 等价的标签能力：

```python
from pyclaego.config import load_json_file, load_json_str, resolve_tree
```

### 节点级标签（JSON 中用单键对象表达 YAML 自定义标签）

```json
{"!concat":      ["a", "b"]},
{"!abs_path":    "~/foo"},
{"!join_path":   ["a", "b"]},
{"!include":     "./other.json"},
{"!include_dir": "./conf.d/"},
{"!include_merge": "./extra.json"}
```

### `load_json_file(path) -> Any`

读取 JSON 文件，翻译节点级标签为 Tag 数据类实例（**未**解析引用）。
是 `PersonalSpaceConfigManager` 的低层加载步骤。

### `load_json_str(text) -> Any`

从字符串解析（用于测试 / Web 表单回写）。

### `resolve_tree(tree, base_dir=None) -> Dict`

对一棵已 deep-merge 的配置树执行完整解析：include 展开 → 环境变量替换 → 配置引用 + Tag 求值。
`base_dir=None` 时禁用 `!include`（PS / Widget 配置通常不需要 include）。

---

## `PersonalSpaceConfigManager`（`personal_space_config.py`）

每个 PersonalSpace（PS）一个实例，管理四层配置的加载、合并、解析与热重载：

```
global  ←  personal_space.config.json  ←  widget_class.defaults  ←  widget.config.json
```

```python
from pyclaego.config import get_ps_config, get_ps_widget_config
```

### 文件约定

| 文件 | 说明 |
|------|------|
| `<ps_root>/personal_space.json` | PS manifest（元数据，raw） |
| `<ps_root>/personal_space.config.json` | PS 级配置覆盖层 |
| `<ps_root>/widgets/<wid>/widget.json` | Widget manifest |
| `<ps_root>/widgets/<wid>/widget.config.json` | Widget 级配置覆盖层 |

### 主要方法

| 方法 | 说明 |
|------|------|
| `load()` | 首次加载所有 PS 级文件 + 已存在的 widget 配置 |
| `resolve_ps() -> Dict` | 返回 PS 整体运行时配置（全局 + PS 配置层，有缓存） |
| `resolve_widget(widget_id, widget_class_defaults=None) -> Dict` | 返回某个 widget 的完整运行时配置（四层合并，有缓存） |
| `get_ps_manifest() -> Dict` | 返回 `personal_space.json` 的副本（raw） |
| `get_widget_manifest(widget_id) -> Dict` | 返回 `widget.json` 的副本（raw） |
| `list_widget_ids() -> List[str]` | 列出已知的所有 widget ID |
| `write_ps_config(new_config)` | 写回 `personal_space.config.json` 并重新加载 |
| `write_widget_config(widget_id, new_config)` | 写回 `widget.config.json` 并重新加载 |
| `reload_file(path)` | 单文件重新加载并清理相关缓存（由 watchfiles 触发或手动调用） |
| `subscribe(callback) -> unsubscribe_fn` | 注册配置变更回调，返回退订函数 |
| `await start_watching()` | 启动 watchfiles 热重载任务（依赖 `watchfiles` 包） |
| `await stop_watching()` | 停止热重载任务 |

**变更回调 scope 类型：**

| scope | 触发场景 |
|-------|---------|
| `("ps",)` | `personal_space.json` 更新 |
| `("ps_config",)` | `personal_space.config.json` 更新 |
| `("widget", widget_id)` | `widget.json` 更新 |
| `("widget_config", widget_id)` | `widget.config.json` 更新 |

### 工厂函数

#### `get_ps_config(ps_root, global_config_provider=None) -> PersonalSpaceConfigManager`

创建并返回已与全局配置单例绑定的 `PersonalSpaceConfigManager`（**未调用 `load()`**，
需调用方显式调用）。

#### `get_ps_widget_config(ps_root, widget_id, widget_class_defaults=None) -> Dict`

一次性返回某个 widget 的完整解析配置（内部创建临时 manager，调用 `load()` 后返回
`resolve_widget(...)` 结果）。适合不需要热重载的场景。

---

## 被其他模块引用的方式

本模块被多个上层模块依赖，均通过以下方式引入：

```python
from ..config import get_config
from ..config import get_config, get_session_config
from ..config import get_ps_config, get_ps_widget_config
from ..config import deep_merge, resolve_layers
from ..config import PYCLAEGO_DEFAULT_WORKSPACES  # 路径常量
```

| 调用方模块 | 用途 |
|-----------|------|
| `src/logging/log_manager.py` | 读取 `logging.*` 配置，确定日志级别、格式、存储路径及轮转策略 |
| `src/logging/running_log.py` | 读取 `logging.log_root` 和 `logging.running_log.*` 配置 |
| `src/tool/tool_manager.py` | 读取 `tools.*` 配置，决定哪些工具被启用或禁用 |
| `src/personal_space/personal_space.py` | 调用 `get_ps_config()` 管理 PS 级配置与热重载 |
| `src/personal_space/widget.py` | 通过 `PersonalSpaceConfigManager.resolve_widget()` 获取 widget 运行时配置 |
| `src/security_executor/handler.py` | 读取 `llm.*` 配置创建 LLM 客户端，读取 `session.*` 获取工作目录 |
| `src/security_executor/monitor.py` | 读取 `security.*` 配置获取安全策略 |
| `src/security_executor/path_resolver.py` | 读取 `session.*` 获取工作目录和 Session 路径映射 |
| `src/skill/manager.py` | 读取 `skill.*` 配置，确定技能搜索路径 |
