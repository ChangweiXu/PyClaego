# config 模块 — 配置管理

## 概述

`config` 模块提供全局和 Session 级别的配置管理能力。它以单例模式维护一个 `ConfigManager` 实例，支持多路径配置文件查找、YAML 自定义标签、环境变量替换、配置项引用与拼接、配置文件拆分引入，以及敏感字段遮盖。

### 文件结构

```
config/
├── __init__.py      # 导出 ConfigManager / get_config / get_session_config
├── manager.py       # 核心实现
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
| **配置文件搜索** | 按优先级顺序搜索：`~/.pyclaego/config.yaml` → `./config.yaml` |
| **默认配置** | 内置 `server` / `client` / `logging` 三个段的默认值 |
| **深度合并** | 用户配置以深度合并方式覆盖默认配置，不会整体替换嵌套字典 |
| **环境变量替换** | 加载阶段递归处理 `${ENV_VAR}` / `${ENV_VAR:default}` 语法 |
| **配置项引用** | 合并后解析 `@{config.key.path}` 引用，防止循环引用 |
| **YAML 标签** | 支持 `!concat [...]` 字符串拼接、`!abs_path "..."` 路径展开、`!join_path [...]` 路径组件拼接并解析为绝对路径、`!include "path"` 引入单个子配置文件、`!include_dir "path"` 合并目录内所有 .yaml/.yml 文件 |
| **敏感字段遮盖** | `show_config()` 时自动将 key/password/secret/token 字段替换为 `***MASKED***` |

#### 配置访问

```python
from pyclaego.src.config import get_config

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
from pyclaego.src.config import get_config

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
4. 仅将 Session 配置中的 `agent` / `context` 段合并到全局配置中
5. 返回合并后的新 `ConfigManager` 实例（不修改全局单例）

```python
from pyclaego.src.config import get_session_config
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
  workspace_root: !abs_path "~/.pyclaego/workspaces"
```

- `!abs_path "..."` — 展开 `~/` 并转换为绝对路径
- 路径内也可以包含 `@{...}` 引用

### 路径组件拼接

```yaml
logging:
  log_file: !join_path ["@{pyclaego.root_path}", "logs", "app.log"]
  # → /home/user/.pyclaego/logs/app.log
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
  log_root: !abs_path "~/.pyclaego/logs"
  file_enabled: true
  console_enabled: true

# 拆分为独立文件 — 引用单个文件
llm:
  default_provider: "kimi_code"
  providers: !include "./llm-providers.yaml"

# 拆分为目录 — 合并目录内所有 .yaml/.yml
tools: !include_dir "./conf.d/tools/"

session:
  workspace_root: !abs_path "~/.pyclaego/workspaces"
```

---

## 被其他模块引用的方式

本模块被多个上层模块依赖，均通过以下方式引入：

```python
from ..config import get_config
from ..config import get_config, get_session_config
```

| 调用方模块 | 用途 |
|-----------|------|
| `src/logging/log_manager.py` | 读取 `logging.*` 配置，确定日志级别、格式、存储路径及轮转策略 |
| `src/logging/running_log.py` | 读取 `logging.log_root` 和 `logging.running_log.*` 配置 |
| `src/tool/tool_manager.py` | 读取 `tools.*` 配置，决定哪些工具被启用或禁用 |
| `src/session/session.py` | 同时调用 `get_config()` 和 `get_session_config()`，合并 Session 级 Agent/Context 配置 |
| `src/security_executor/handler.py` | 读取 `llm.*` 配置创建 LLM 客户端，读取 `session.*` 获取工作目录 |
| `src/security_executor/monitor.py` | 读取 `security.*` 配置获取安全策略 |
| `src/security_executor/path_resolver.py` | 读取 `session.*` 获取工作目录和 Session 路径映射 |
| `src/skill/manager.py` | 读取 `skill.*` 配置，确定技能搜索路径 |
