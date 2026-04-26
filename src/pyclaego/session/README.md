# session 模块 — 会话管理

## 概述

`session` 模块负责管理用户会话（Session）的完整生命周期：工作空间初始化、配置加载、消息队列处理、Agent 调度、命令拦截、Session 级 cron 调度，以及订阅者跟踪。同时，`SessionManager` 负责将用户消息接入 TaskManager、TextSubscriber 和任务桥接通道，并向 Session 注入 unsolicited 广播回调。

### 文件结构

```
session/
├── __init__.py         # 导出 Session / SessionManager
├── session.py          # Session - 单个会话类（含 cron 入队/落盘/广播逻辑）
├── manager.py          # SessionManager - 会话管理器（含 broadcast 注入、shutdown_all）
├── command_handler.py  # CommandHandler - /命令 解析与执行（立即/队列两类）
└── cron.py             # SessionCronScheduler / CronJob - APScheduler 封装
```

---

## Session ID 格式要求

**格式规则**: Session ID 只能包含**小写字母(a-z)、数字(0-9)和下划线(_)**，且必须以字母或下划线开头。

### 合法示例
- `sess_abc123` ✓
- `user_session_001` ✓
- `feishu_p2p_abc` ✓
- `_private_session` ✓

### 不合法示例
- `Sess_ABC` ✗ (包含大写字母)
- `sess-123` ✗ (包含短横线)
- `sess.test` ✗ (包含点号)
- `../escape` ✗ (路径穿越字符)
- `123start` ✗ (以数字开头)

### 自动验证

Session ID 在以下时机自动验证:
1. `Session.__init__()` - 创建 Session 对象时
2. `SessionManager.get_or_create_session()` - 用户提供 session_id 时

格式不合法时抛出 `ValueError`:
```python
ValueError: Invalid session_id format: 'INVALID-ID'. 
Session ID must contain only lowercase letters, digits, and underscores.
```

### 自动生成

使用 `generate_session_id()` 函数自动生成符合格式的 ID:
```python
from pyclaego.session.session import generate_session_id

sid = generate_session_id()  # → "sess_abc123xyz" (sess_ + 12位十六进制)
```

生成的 ID 格式: `sess_` + 12位UUID十六进制字符(仅包含小写字母a-f和数字0-9)，符合所有格式要求。

---

## 工作空间结构

每个 Session 在文件系统上对应一个独立目录：

```
{workspace_root}/{session_id}/        ← 默认路径，或 config.yaml 中的自定义映射
├── session.json     ← 会话元数据（ID、用户、时间戳、订阅状态）
├── config.yaml      ← Session 级配置（覆盖全局 agent / context / cron 配置段）
├── SOUL.md          ← Agent 身份定位文档（供 SoulContext 系列读取）
├── skills/          ← Session 独有技能目录（供 SkillManager 扫描）
│   └── my_skill/SKILL.md
└── cron/            ← cron 任务输出目录（仅启用 cron 后存在）
    └── 20260422-093000-daily_brief.md   ← 单次触发的产出（YAML front-matter + 正文）
```

---

## 核心类

### `Session`（`session.py`）

**一个 Session 对应一个用户会话**。每次 `join_session` 请求都会获取或创建一个 Session 对象。

#### 构造函数

```python
session = Session(
    session_id="sess_abc123",
    user_id="default_user",
    workspace_root="./workspaces"
)
```

**初始化执行流程**：

1. 确定工作目录（检查 `config.yaml` 中的 `session.session_workspace_root` 映射）
2. `_ensure_workspace()` — 创建工作目录，写入 `session.json`、`config.yaml`（空）、`SOUL.md`（默认内容）、`skills/`
3. 调用 `get_session_config(session_id, workspace_path)` — 合并全局配置 + Session 配置
4. 初始化 `ContextFactory.create_handler()` — 按 `context.type` 配置创建上下文处理器（必须为 `BaseContextHandlerV3` 子类）
5. 初始化 `AgentFactory.create_agent()` — 按 `agent.type` 配置创建 Agent
6. 启动消息处理器后台任务（`asyncio.create_task`）
7. 初始化 `CommandHandler`
8. 预留 `_broadcast_handler` 占位（由 `SessionManager` 注入），用于无客户端 await 的主动推送（如 cron 结果）
9. 若 `cron.enabled=true`，构造并启动 `SessionCronScheduler`

> Session 内部实际通过 `agent.process_v2(...)` 执行消息处理。

#### 消息处理机制

采用**异步队列 + 单消费者**模式，确保同一 Session 内消息串行处理：

```
process_message(message)
    │
    ├── 命令检测（以 / 开头）
    │       └─→ CommandHandler.handle_command()（直接返回，不入队）
    │
    └── 普通消息
            └─→ asyncio.Queue.put()
                    │
                    └─→ _message_processor_loop()（后台任务）
                            │
                            ├── asyncio.create_task(_do_process_message())
                            │       └─→ Agent.process_v2(...)
                            └── await response_future（等待 Agent 完成）
```

关键特性：
- `_processing_lock` 确保同一时间只有一条消息在处理
- `_current_agent_task` 保存当前 Agent 任务引用，供 `/stop` 命令取消
- 支持 `msg_update_handler` 进度回调，用于实时推送处理进度给 WebSocket 客户端
- `CancelledError` 被捕获后返回 `{"cancelled": True, "content": "⚠️ 任务已被 /stop 命令取消"}`

#### 主要方法

```python
# 处理消息（命令或普通消息）
response = await session.process_message(
    message={"type": "user_message", "content": "你好"},
    user_id="user1",
    msg_update_handler=ws_send_callback   # 可选，用于推送进度
)
# → {"type": "response", "session_id": ..., "content": "...", "timestamp": "..."}

# 订阅/取消订阅（客户端连接/断开时调用）
session.subscribe()
session.unsubscribe()

# 获取 Session 信息（用于 join_session 响应）
info = session.get_info()
# → {"session_id": ..., "workspace_path": ..., "created_at": ..., "subscriber_count": ...}

# 注入 unsolicited 广播回调（由 SessionManager 调用）
session.set_broadcast_handler(async_fn)

# 优雅关闭：停 cron → 取消当前 agent task → 排空 processor → 持久化
await session.shutdown()
```

#### 主动消息（cron 等）

非用户触发的消息（目前主要是 cron）通过 `Session._enqueue_cron(job)` 走与普通用户消息同一条串行队列：

1. 在 `TaskManager` 创建 `TaskType.USER_MESSAGE` 顶层任务，附带 `source="cron"`、`cron_name`、`schedule` 元数据
2. 用 `SessionTaskHandlerV2` 包装一个 handler（`original_handler=None`）
3. 合成 `user_message` 入 `_message_queue`，等待响应 future
4. 处理完成后：
   - `save_to_file=True` → 原子写入 `<workspace>/cron/YYYYMMDD-HHMMSS-<slug>.md`（YAML front-matter 含 `cron_name/schedule/fired_at/prompt`）
   - `broadcast=True` 且已注入 `_broadcast_handler` → 推送 `cron_response` 消息给已订阅客户端

#### 工作目录解析规则

```yaml
# config.yaml 中可配置 Session → 工作目录 的自定义映射
session:
  workspace_root: !abs_path "~/.pyclaego/workspaces"   # 默认路径
  session_workspace_root:
    my_project: "/Users/user/my_project"               # 自定义映射
```

- 若 `session_id` 在 `session_workspace_root` 中存在 → 使用自定义路径
- 否则 → `workspace_root / session_id`

---

### `SessionCronScheduler` / `CronJob`（`cron.py`）

基于 **APScheduler v3 `AsyncIOScheduler`** 的每 Session 一个的 cron 调度器。仅当 `<workspace>/config.yaml` 中存在 `cron.enabled: true` 时由 `Session` 构造并启动。

#### 配置示例

```yaml
# <workspace>/config.yaml
cron:
  enabled: true
  timezone: Asia/Shanghai          # 默认 UTC
  jobs:
    - name: daily_brief            # 必填，唯一；用于 /cron 子命令、文件名 slug
      schedule: "0 9 * * *"        # 必填，标准 5 字段 crontab
      prompt: "总结昨天的待办进展"  # 必填，触发时合成的 user_message 内容
      enabled: true                # 默认 true；false 时启动跳过
      output:
        save_to_file: true         # 落盘到 <workspace>/cron/
        broadcast: true            # 通过 broadcast handler 推给客户端
      metadata: {}                 # 任意 dict，留作扩展
```

#### 触发链路

```
APScheduler 触发
   └─→ SessionCronScheduler._on_fire(name)
          └─→ Session._enqueue_cron(job)
                 └─→ _message_queue → 复用普通用户消息处理路径
```

#### 主要方法

```python
scheduler.start()                  # 注册 enabled 任务并启动
scheduler.shutdown(wait=False)     # 关闭（不等待回调结束）
scheduler.list_jobs()              # → [{name, schedule, enabled, paused, next_fire_time, prompt_preview}]
scheduler.pause(name) / resume(name)   # 内存态开关，重启后失效
await scheduler.run_now(name)      # 立即入队触发一次
```

`slugify(name)` 工具函数将任务名转成文件系统安全 slug（仅 `a-z 0-9 _ -`），用于落盘文件名。

依赖：`apscheduler>=3.10,<4`。未安装时构造抛 `ImportError`，Session 启动会捕获并记录错误（不影响其它功能）。

---

### `SessionManager`（`manager.py`）

管理所有 Session 对象的生命周期，由 `CoreScheduler` 持有和使用。采用**懒加载策略**，仅在实际请求时创建或加载 Session。

#### 初始化

```python
manager = SessionManager(workspace_root="./workspaces")
```

**懒加载策略**: 启动时不扫描工作目录，仅在用户请求特定 session_id 时才加载或创建 Session。

**优势**:
- ✅ 启动速度更快(无需扫描所有目录)
- ✅ 内存占用更低(仅加载活跃 Session)
- ✅ 避免历史不合法 Session 目录产生警告日志

#### 核心方法

```python
# 获取或创建 Session（最常用的方法）
session = await manager.get_or_create_session(
    session_id="sess_abc123",   # 可选，None 则自动生成
    user_id="default_user"
)
# 自动验证格式: 不合法的 session_id 抛出 ValueError

# 直接获取 Session (仅从内存缓存中查找)
session = manager.get_session("sess_abc123")  # → Session | None

# 列出 Session (仅列出内存中的 Session)
infos = manager.list_sessions()                     # 所有
infos = manager.list_sessions(user_id="user1")      # 按用户过滤

# 订阅/取消订阅
await manager.subscribe_session("sess_abc123")
await manager.unsubscribe_session("sess_abc123")

# 路由消息（CoreScheduler 的核心调用）
# 【2026年04月10日新增】集成 TaskManager: 自动创建任务、包装 handler、标记完成/失败
response = await manager.route_message(
    session_id="sess_abc123",
    message={"type": "user_message", "content": "..."},
    user_id="user1",
    msg_update_handler=ws_callback   # 可选进度回调
)

# 统计信息
stats = manager.get_stats()
# → {"total_sessions": N, "active_sessions": M, "workspace_root": "..."}

# 注入全局 unsolicited 广播回调（由 CoreScheduler 调用）
# 后续创建的 Session 自动获得；已存在的 Session 会被补设。
manager.set_broadcast_fn(async_fn)

# 程序退出时关闭所有 Session（停 cron、排空队列、持久化）
await manager.shutdown_all()
```

#### TaskManager 集成（2026年04月10日新增）

`SessionManager.route_message()` 现已集成 **TaskManager**，自动追踪所有用户消息处理任务：

**集成流程**:
1. **创建顶层任务** - 为每条用户消息创建 `TaskType.USER_MESSAGE` 任务
2. **包装 handler** - 使用 `SessionTaskHandlerV2` 包装原始 `msg_update_handler`
3. **传递给 Session** - Session 处理消息时通过包装后的 handler 更新任务状态
4. **任务完成/失败** - 自动标记任务完成或失败

**任务追踪示例**:
```python
# 用户发送消息后，TaskManager 自动创建任务
# 任务树结构：
# USER_MESSAGE (sess_abc-20260410-a3f9)
#   └─ AGENT_LOOP (由 Agent 通过 handler 创建)
#        ├─ TOOL_EXECUTION (read_file)
#        └─ TOOL_EXECUTION (write_file)

# 导出任务状态供 UI 展示
from pyclaego.task_manager import TaskManager
task_manager = TaskManager.get_instance()
tasks = task_manager.export_session_tasks("sess_abc123")
print(tasks)
```

详见: task_manager/README.md

#### 历史 Session 目录处理

**懒加载策略下的行为**:
- 历史遗留的不合法 Session 目录(如 `INVALID-ID`)不会在启动时被扫描
- 只有在用户明确请求访问时才验证格式
- 如需访问历史数据，管理员应手动重命名为合法格式:
  ```bash
  cd workspaces/
  mv "INVALID-ID" "invalid_id"
  ```

---

### `CommandHandler`（`command_handler.py`）

处理以 `/` 开头的控制命令，由 `Session` 持有，在消息入队列前拦截命令消息。

#### 命令分类

命令分两类（在 `CommandHandler` 中由 `IMMEDIATE_COMMANDS` / `QUEUED_COMMANDS` 集合声明）：

- **立即命令**：收到即执行，不入队，可与正在处理的消息并发。
- **队列命令**：入队，等到当前消息处理完毕再串行执行；可被 `/stop` 取消。

#### 内置命令

| 命令 | 类型 | 说明 |
|------|------|------|
| `/stop` | 立即 | 取消当前正在执行的 Agent 任务（`cancel()` 当前 `_current_agent_task`），并清空消息队列 |
| `/help` | 立即 | 返回可用命令列表 |
| `/llm [provider_id]` | 立即 | 查看或动态切换当前 session 的 LLM provider（见下方详述）|
| `/cron [list\|next\|pause\|resume\|run\|help] [name]` | 立即 | 管理 Session 级 cron 任务（见下方详述）|
| `/compress [--llm]` | 队列 | 强制对短期记忆执行常态化截断（仅 `soul_v5` context 策略有效），`--llm` 预留 |
| `/rebuild_memory_index` | 立即 | 从 MD 文件树重建 SQLite 索引（仅 `soul_v5` 策略有效）|
| `/pin <id>` | 立即 | **SoulV6**：将记忆 pin 到上下文 |
| `/unpin <id>` | 立即 | **SoulV6**：取消 pin |
| `/close_loop [query]` | 立即 | **SoulV6**：闭合记忆循环 |
| `/memories [...]` | 立即 | **SoulV6**：列出当前记忆 |
| `/forget <id>` | 立即 | **SoulV6**：删除指定记忆 |
| `/why [query]` | 立即 | **SoulV6**：解释为何召回某段记忆 |
| `/export_memory [path]` | 立即 | **SoulV6**：导出记忆 |

> SoulV6 命令通过鸭子类型检查 `context_handler` 是否拥有 `cmd_pin` / `cmd_close_loop` 方法；非 `soul_v6` 策略时返回不支持提示。

#### 命令处理流程

```python
# 判断是否为命令
CommandHandler.is_command({"content": "/stop"})   # → True
CommandHandler.is_command({"content": "hello"})    # → False

# 解析命令
name, args = CommandHandler.parse_command({"content": "/stop now"})
# → ("stop", ["now"])

# 执行命令（由 Session.process_message 自动调用）
response = await handler.handle_command(message, user_id)
# → {"type": "command_response", "session_id": ..., "content": ..., "success": True}
```

#### `/llm` 命令详述

`/llm` 通过 `BaseAgent.get_llm_id()` / `set_llm_id()` 接口统一读写 agent 的 LLM provider，**不直接访问 `agent.llm_id` 属性**，保证对当前 Agent 类型（EchoAgent / SimpleAgent / ThinkAgent / SpawnAgent）的正确性。

```python
# 查看当前 LLM 设置（屏蔽 api_key）
# 输入: /llm
# 输出示例:
# 🤖 当前 LLM 配置 (session: echo):
#   llm_id: kimi_code
#   provider 详情:
#     api                  anthropic
#     model                k2p5
#     temperature          0.7
#     api_key              ***MASKED***
# 可用 providers: kimi_code, moonshot_kimi, glm5

# 动态切换 LLM（立即生效，仅影响本次进程运行时，不写磁盘）
# 输入: /llm moonshot_kimi
# 输出示例:
# ✅ LLM 已切换: kimi_code → moonshot_kimi
#   model:   kimi-k2.5
#   api:     openai
```

可用 provider 列表来自全局 `config.yaml` 的 `llm.providers` 字典。切换后进程重启会恢复配置文件中的默认值。

#### `/cron` 命令详述

```
/cron               # 等价于 /cron list
/cron list          # 列出所有任务（含 enabled/paused 标志、下次触发时间、prompt 预览）
/cron next          # 同 list，但按下次触发时间排序
/cron pause  <name> # 暂停指定任务（仅内存态，重启后失效）
/cron resume <name> # 恢复已暂停的任务
/cron run    <name> # 立即触发一次（绕过 schedule，复用 _enqueue_cron 路径）
/cron help
```

未启用 cron（`<workspace>/config.yaml` 没有 `cron.enabled: true`）时返回错误提示。

#### 自定义命令注册

`CommandHandler` 的 `commands` 字典可在实例化后扩展：

```python
async def _my_command(payload):
    return {"type": "command_response", ..., "content": "自定义命令结果"}

session.command_handler.commands["my_cmd"] = _my_command
# 之后 /my_cmd 即可触发
```

---

## 消息处理整体流程

```
CoreScheduler.handle_message("user_message")
    │
    └─→ SessionManager.route_message(session_id, message, user_id, callback)
            │
            └─→ Session.process_message(message, user_id, msg_update_handler)
                    │
                    ├─[立即命令]─→ CommandHandler.handle_command()
                    │              ├── /stop  → cancel _current_agent_task + 清队列
                    │              ├── /help  → 返回帮助文本
                    │              ├── /llm   → 查看或切换 agent.llm_id
                    │              ├── /cron  → list/pause/resume/run
                    │              └── /pin、/unpin、/close_loop ...（SoulV6）
                    │
                    ├─[队列命令]─→ Queue.put({type:"command"}) → 等当前消息完毕后串行执行
                    │              └── /compress、/rebuild_memory_index
                    │
                    └─[普通消息]─→ Queue.put({type:"message"}) → _message_processor_loop()
                                        │
                                        └─→ _do_process_message()
                                                │
                                                └─→ Agent.process_v2(
                                                        user_message,
                                                        context_handler,
                                                        session_task_handler
                                                    )

APScheduler cron 触发
    │
    └─→ SessionCronScheduler._on_fire(name)
            │
            └─→ Session._enqueue_cron(job)
                    │
                    ├── TaskManager.create_task(USER_MESSAGE, source="cron")
                    ├── Queue.put(合成 user_message)
                    ├── 等待 response_future
                    ├── 落盘 <workspace>/cron/YYYYMMDD-HHMMSS-<slug>.md
                    └── _broadcast_handler({type:"cron_response", ...})
```

---

## 依赖关系

### 导入的模块和函数

```python
# session.py
from ..config import get_config, get_session_config        # 配置加载
from ..utility import validate_session_id                  # ID 格式校验
from ..agent import AgentFactory, BaseAgent                # 创建 Agent
from ..context import ContextFactory, BaseContextHandlerV3 # 创建 Context Handler（要求 V3）
from ..context.system_prompts.default_soul import DEFAULT_SOUL
from ..task_manager import SessionTaskHandlerV2, TaskManager, TaskType
from ..logging import get_running_log                      # 日志记录
from .cron import SessionCronScheduler, CronJob, slugify   # cron 子系统
from .command_handler import CommandHandler                # 命令处理器（延迟导入）

# manager.py
from .session import Session, generate_session_id, validate_session_id
from ..config import get_config                            # 读取 web.task_bridge 配置
from ..task_manager import TaskManager, TaskType, SessionTaskHandlerV2, TextSubscriber
from ..web.task_bridge import TaskBridgeServer             # 任务事件 → 前端
from ..logging import get_running_log

# cron.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from ..logging import get_running_log

# command_handler.py
from ..config import get_config                            # /llm 读取 llm.providers
from ..logging import get_running_log
```

| 导入内容 | 来源 | 用途 |
|---------|------|------|
| `get_config()` | `src.config` | 读取 `session.session_workspace_root` 确定工作目录 |
| `get_session_config()` | `src.config` | 合并全局配置与 Session 级 `config.yaml`，用于初始化 Agent/Context |
| `AgentFactory` | `src.agent` | 按 `agent.type` 配置创建对应 Agent 实例 |
| `BaseAgent` | `src.agent` | Agent 实例的类型注解 |
| `ContextFactory` | `src.context` | 按 `context.type` 配置创建 Context Handler |
| `get_running_log()` | `src.logging` | 全程记录 Session 生命周期事件 |
| `TaskManager, TaskType, SessionTaskHandlerV2` | `src.task_manager` | 在 `SessionManager.route_message()` 与 `Session._enqueue_cron()` 中创建/更新任务树 |
| `TextSubscriber` | `src.task_manager` | 将任务树实时导出到文本文件 |
| `TaskBridgeServer` | `src.web.task_bridge` | 将任务事件通过 websocket 推送给 Web 层 |
| `apscheduler.AsyncIOScheduler` | 第三方 | Session 级 cron 调度（仅 `cron.enabled=true` 时加载）|

### 被其他模块引用

```python
from pyclaego.session import SessionManager

# 在 core/scheduler.py 中（延迟导入）
from pyclaego.session import SessionManager
```

| 调用方模块 | 导入内容 | 用途 |
|-----------|---------|------|
| `src/core/scheduler.py` | `SessionManager` | `start()` 内延迟导入，管理所有 Session 的路由和生命周期 |
