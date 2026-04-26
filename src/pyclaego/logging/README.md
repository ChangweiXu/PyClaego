# logging 模块 — 日志系统

## 概述

`logging` 模块提供两套互补的日志记录机制：

1. **`LogManager`（`log_manager.py`）** — 基于 Python 标准 `logging` 库的结构化日志，支持多格式输出、文件轮转，适合系统级别的调试与错误追踪。
2. **`RunningLog`（`running_log.py`）** — 轻量级业务流水日志，按 `name` + 日期分文件追加写入，不依赖 `logging` 框架，适合 Session 运行轨迹、LLM 调用记录等场景。

> **注意**：当前模块的 `__init__.py` 仅导出 `RunningLog` 和 `get_running_log`。`LogManager` 已实现但被注释掉，系统其他模块均通过 `get_running_log()` 使用 `RunningLog`。

### 文件结构

```
logging/
├── __init__.py      # 导出 RunningLog / get_running_log
├── log_manager.py   # LogManager 完整实现（当前未对外导出，可按需启用）
└── running_log.py   # RunningLog 实现（当前主用）
```

---

## RunningLog（推荐使用）

### 设计特点

| 特性 | 说明 |
|------|------|
| **单例模式** | 全局唯一实例，通过 `get_running_log()` 获取 |
| **按名称 + 日期分文件** | 每个 `name` 每天生成独立日志文件，自动跨天切换 |
| **文件命名规则** | `{log_root}/running/{name}-YYYYMMDD-run.log` |
| **直接追加写入** | 不依赖 Python `logging` 框架，直接 `open(..., "a")` 写文件 |
| **线程安全** | 所有写入操作受 `threading.Lock` 保护 |
| **非阻塞设计** | 写入失败只打印警告，不抛出异常，不影响主业务 |
| **配置驱动** | 从 `config.yaml` 的 `logging.*` 读取路径和格式，加载失败时自动降级到默认值 |

### 快速使用

```python
from pyclaego.logging import get_running_log

rlog = get_running_log()

# 四个日志级别
rlog.info("session_abc123", "Session 启动")
rlog.warning("session_abc123", "LLM 响应超时，正在重试")
rlog.error("session_abc123", "工具调用失败: bash_executor")
rlog.debug("session_abc123", f"收到消息: {message[:100]}")

# 通用接口（自定义级别）
rlog.log("core_service", "自定义级别消息", level="TRACE")

# 查询日志文件路径（不写入）
path = rlog.get_log_file_path("session_abc123")
# → PosixPath('/Users/xxx/.pyclaego/logs/running/session_abc123-20260407-run.log')
```

### 配置项（`config.yaml`）

```yaml
logging:
  log_root: !abs_path "~/.pyclaego/logs"   # 日志根目录（与 LogManager 共用）
  running_log:
    subdir: "running"                      # 子目录，默认 "running"
    format: "[{time}] [{level}] {message}" # 行格式，支持 {time}/{level}/{message}
    time_format: "%Y-%m-%d %H:%M:%S"       # 时间格式（strftime）
    encoding: "utf-8"                      # 文件编码
```

### 生成的日志文件示例

```
~/.pyclaego/logs/running/
├── session_abc123-20260407-run.log
├── core_service-20260407-run.log
├── session_abc123-20260408-run.log   ← 跨天自动新建
└── ...
```

每行格式（默认）：
```
[2026-04-07 12:34:56] [INFO] Session 启动
[2026-04-07 12:35:01] [WARNING] LLM 响应超时，正在重试
[2026-04-07 12:35:03] [ERROR] 工具调用失败: bash_executor
```

### `name` 的使用约定

各模块在调用 `rlog` 时，通常传入如下值作为 `name`：

| 模块 | 常用 `name` | 说明 |
|------|------------|------|
| `core/scheduler.py` | `"core_service"` | 调度器服务日志 |
| `session/session.py` | `session_id` | 每个 Session 独立日志文件 |
| `agent/simple_agent.py` | `session_id` | Agent 主循环与工具回合日志 |
| `security_executor/*.py` | `session_id` 或固定名 | 安全执行器日志 |

---

## LogManager（备用，当前未激活）

### 设计特点

| 特性 | 说明 |
|------|------|
| **单例模式** | 内部 `_instance` 保证全进程唯一实例 |
| **基于标准 `logging` 库** | 每个模块对应一个独立的 `logging.Logger` |
| **双通道输出** | 同时输出到控制台（`StreamHandler`）和文件（`RotatingFileHandler` 或 `TimedRotatingFileHandler`） |
| **两种日志格式** | `text`（人类可读）或 `json`（机器可读，含时间戳、级别、模块、函数、行号） |
| **文件轮转** | 支持按大小（`size`，默认 10MB）或按时间（`time`，默认每天午夜）轮转 |
| **配置驱动** | 从 `config.yaml` 的 `logging.*` 读取，失败时降级到默认配置 |

### 使用方式（如需启用）

首先在 `__init__.py` 取消注释相应导出，然后：

```python
from pyclaego.logging import get_logger

logger = get_logger(__name__)
logger.info("Application started")
logger.error("An error occurred", exc_info=True)
```

每个模块名对应一个 Logger 实例，日志文件路径为：
```
{log_root}/{module_name_with_underscores}.log
```

### 配置项（`config.yaml`）

```yaml
logging:
  level: ${LOG_LEVEL:INFO}         # DEBUG / INFO / WARNING / ERROR / CRITICAL
  format: "text"                   # "text" 或 "json"
  log_root: !abs_path "~/.pyclaego/logs"
  file_enabled: true
  console_enabled: true
  rotation:
    type: "time"                   # "size" 或 "time"
    max_bytes: 10485760            # 按大小轮转时的单文件最大字节数（10MB）
    backup_count: 5                # 保留备份文件数量
    when: "midnight"               # 按时间轮转的周期
```

### JSON 格式日志示例

```json
{
  "timestamp": "2026-04-07T12:34:56.789012",
  "level": "INFO",
  "module": "src.session.session",
  "message": "Session abc123 已启动",
  "function": "start",
  "line": 85
}
```

---

## 依赖关系

### 内部依赖

`log_manager.py` 和 `running_log.py` 均在初始化时从 `config` 模块加载配置：

```python
from ..config import get_config

config = get_config()
logging_cfg = config.get("logging", {})
```

- **`get_config()`**（来自 `src.config`）：获取全局 `ConfigManager` 单例，读取 `logging.*` 配置段。
- 如果 `get_config()` 抛出异常（例如配置文件不存在），两个实现均有 `try/except` 降级到内置默认值，确保日志子系统不会阻塞主业务启动。

### 被其他模块引用的方式

全项目几乎所有模块均通过以下方式在模块级别获取日志实例：

```python
from ..logging import get_running_log

_rlog = get_running_log()  # 模块级单例，避免重复获取
```

| 调用方模块 | 使用场景 |
|-----------|---------|
| `src/core/scheduler.py` | 记录客户端连接/断开、消息路由、广播事件 |
| `src/session/session.py` | 记录 Session 生命周期事件（创建、消息处理、Agent 响应） |
| `src/session/manager.py` | 记录 Session 管理操作 |
| `src/session/command_handler.py` | 记录命令处理过程 |
| `src/agent/simple_agent.py` | 记录 Agent 每轮调用和工具执行结果 |
| `src/agent/spawn_agent.py` | 记录子 Agent 调度与并发执行过程 |
| `src/agent/think_agent.py` | 记录思考内容提取和两阶段执行过程 |
| `src/agent/agent_factory.py` | 记录 Agent 创建过程 |
| `src/tool/base_tool.py` | 记录工具执行基类事件 |
| `src/tool/tool_manager.py` | 记录工具注册和配置加载 |
| `src/tool/tool_call_parser.py` | 记录工具调用解析结果 |
| `src/context/context_factory.py` | 记录上下文处理器创建 |
| `src/context/simple_context.py` | 记录上下文截断和处理事件 |
| `src/context/soul_context_v*.py` | 记录 SoulContext 状态变化 |
| `src/context/memory_manager.py` | 记录记忆管理操作 |
| `src/context/token_counter.py` | 记录 Token 计数结果 |
| `src/security_executor/handler.py` | 记录安全检查结果和 LLM 审核过程 |
| `src/security_executor/monitor.py` | 记录安全监控事件 |
| `src/security_executor/rule_factory.py` | 记录安全规则加载 |
| `src/security_executor/base_rule.py` | 记录规则执行基类事件 |
| `src/security_executor/path_resolver.py` | 记录路径解析结果 |
| `src/llm/openai_client.py` | 记录 OpenAI API 调用详情 |
| `src/llm/anthropic_client.py` | 记录 Anthropic API 调用详情 |
| `src/skill/manager.py` | 记录技能加载和管理事件 |
