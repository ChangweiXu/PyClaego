# logging 模块 — 日志系统

## 概述

`logging` 模块提供三套互补的日志记录机制：

1. **`RunningLog`（`running_log.py`）** — 轻量级业务流水日志，按 `name` + 日期分文件追加写入，不依赖 `logging` 框架，适合 Session 运行轨迹、LLM 调用记录等场景。**（当前全项目主用）**
2. **`DynamicLogger`（`dynamic_logger.py`）** — 基于 Python 标准 `logging` 库、按模块名分文件 + 按天轮转，支持控制台级别过滤，适合需要结构化格式的场景。**（已激活，按需使用）**
3. **`LogManager`（`log_manager.py`）** — 完整的结构化日志方案，支持多格式输出和文件轮转。**（已实现但当前未导出，可按需启用）**

> **当前导出状态**：`__init__.py` 导出 `RunningLog`、`get_running_log`、`DynamicLogger`、`get_dynamic_logger`。`LogManager` 已实现但注释掉。

### 文件结构

```
logging/
├── __init__.py          # 导出 RunningLog / get_running_log / DynamicLogger / get_dynamic_logger
├── running_log.py       # RunningLog 实现（全项目主用）
├── dynamic_logger.py    # DynamicLogger 实现（基于 Python logging，按模块分文件 + 控制台过滤）
└── log_manager.py       # LogManager 完整实现（当前未对外导出，可按需启用）
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
from src.logging import get_running_log

rlog = get_running_log()

# 四个日志级别
rlog.info("session_abc123", "Session 启动")
rlog.warning("session_abc123", "LLM 响应超时，正在重试")
rlog.error("session_abc123", "工具调用失败: bash_executor")
rlog.debug("session_abc123", f"收到消息: {message[:100]}")

# exception：写入 ERROR 级别并附加当前 except 块内的完整 traceback
try:
    risky_call()
except Exception:
    rlog.exception("session_abc123", "risky_call 异常")

# 通用接口（自定义级别）
rlog.log("core_service", "自定义级别消息", level="TRACE")

# 查询日志文件路径（不写入）
path = rlog.get_log_file_path("session_abc123")
# → PosixPath('/Users/xxx/pyclaego/logs/running/session_abc123-20260407-run.log')
```

### 配置项（`config.yaml`）

```yaml
logging:
  log_root: !abs_path "~/pyclaego/logs"   # 日志根目录（与 LogManager 共用）
  running_log:
    subdir: "running"                      # 子目录，默认 "running"
    format: "[{time}] [{level}] {message}" # 行格式，支持 {time}/{level}/{message}
    time_format: "%Y-%m-%d %H:%M:%S"       # 时间格式（strftime）
    encoding: "utf-8"                      # 文件编码
```

### 生成的日志文件示例

```
~/pyclaego/logs/running/
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

### 公开方法一览

| 方法 | 说明 |
|------|------|
| `log(name, message, level="INFO")` | 通用写入接口 |
| `info(name, message)` | INFO 级别 |
| `warning(name, message)` | WARNING 级别 |
| `error(name, message)` | ERROR 级别 |
| `debug(name, message)` | DEBUG 级别 |
| `exception(name, message)` | ERROR 级别 + 自动附加当前 `except` 块的完整 traceback；在 `except` 块外调用时等同于 `error()` |
| `get_log_file_path(name, dt=None)` | 查询指定 name 在指定日期（默认今天）的日志文件路径，不写入 |

---

## DynamicLogger

### 设计特点

| 特性 | 说明 |
|------|------|
| **基于标准 `logging` 库** | 复用 Python 内建 `logging.Logger`，支持 `exc_info`、`stack_info` 等标准参数 |
| **按模块名分文件** | 首次以某 `module_name` 调用时，自动在 `{log_root}/dynamic/` 下为该模块创建专属文件 |
| **文件命名规则** | `{log_root}/dynamic/{module_name}-{YYYY-MM-DD}.log` |
| **按天自动轮转** | 自定义 `_DailyRotatingFileHandler`，跨天时在同一 `emit()` 调用中切换文件，无需重启 |
| **控制台级别过滤** | 共享一个 `StreamHandler`，默认只输出 `WARNING` / `ERROR` / `CRITICAL` 到控制台 |
| **自动附加异常栈** | `warning()` / `error()` 检测到活跃异常时自动设置 `exc_info=True` |
| **线程安全** | `_loggers` 字典的创建路径使用双重检查锁 |
| **单例模式** | `get_dynamic_logger()` 返回全局唯一 `DynamicLogger` 实例 |

### 快速使用

```python
from pyclaego.logging import get_dynamic_logger

dlog = get_dynamic_logger()

dlog.info("auth", "用户登录成功")          # → auth-2026-05-01.log
dlog.error("payment", "支付失败")          # → payment-2026-05-01.log，控制台也输出
dlog.warning("llm_router", "上游超时")     # → llm_router-2026-05-01.log，控制台也输出

# 在 except 块内：warning/error 自动附加 traceback
try:
    risky_call()
except Exception:
    dlog.error("my_module", "risky_call 异常")   # 自动 exc_info=True

# 显式 exception 方法（始终附加 traceback）
try:
    risky_call()
except Exception:
    dlog.exception("my_module", "risky_call 异常")
```

### `get_dynamic_logger()` 工厂函数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `config` | `dict \| None` | `None` | 完整配置字典；为 `None` 时自动从 `ConfigManager` 加载 |
| `console_levels` | `list[str] \| None` | `['WARNING', 'ERROR', 'CRITICAL']` | 需要输出到控制台的级别 |
| `file_level` | `int` | `logging.DEBUG` | 写入文件的最低级别 |
| `encoding` | `str` | `"utf-8"` | 日志文件编码 |

首次调用时创建单例，后续调用忽略参数直接返回已有实例。

### 公开方法一览

| 方法 | 说明 |
|------|------|
| `debug(module_name, msg, *args, **kwargs)` | DEBUG 级别 |
| `info(module_name, msg, *args, **kwargs)` | INFO 级别 |
| `warning(module_name, msg, *args, **kwargs)` | WARNING 级别，自动检测活跃异常并附加 |
| `error(module_name, msg, *args, **kwargs)` | ERROR 级别，自动检测活跃异常并附加 |
| `exception(module_name, msg, *args, **kwargs)` | ERROR 级别，始终附加异常栈（`exc_info=True`）|

所有方法支持 `logging.Logger.log()` 的标准 `**kwargs`（如 `exc_info`、`stack_info`、`extra`）。

### 生成的日志文件示例

```
~/pyclaego/logs/dynamic/
├── auth-2026-05-01.log
├── payment-2026-05-01.log
├── llm_router-2026-05-01.log
├── auth-2026-05-02.log          ← 跨天自动新建
└── ...
```

每行格式（固定）：
```
[2026-05-01 12:34:56] [INFO] [pyclaego.dynamic.auth] 用户登录成功
[2026-05-01 12:35:01] [ERROR] [pyclaego.dynamic.payment] 支付失败
Traceback (most recent call last):
  ...
```

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
from src.logging import get_logger

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
  log_root: !abs_path "~/pyclaego/logs"
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
