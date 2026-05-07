# task_manager 模块 — Agent 任务追踪与订阅分发

## 概述

`task_manager` 是 Agent 执行过程的**任务树追踪核心**。它把一次用户请求展开成一棵层级任务树（USER_MESSAGE → AGENT_LOOP → TOOL_EXECUTION / SUBAGENT_* / MEMORY_*），通过单例 `TaskManager` 维护任务状态、生命周期和父子关系，并以**订阅/发布**模式把任务事件并发推送给所有订阅者（文本导出、WebSocket 桥接、UI 仪表板等）。

`WidgetTaskHandler`（PersonalSpace 模型）/ `SessionTaskHandlerV2`（兼容旧 Session 模型）是 Agent 与 TaskManager 之间的薄包装：Agent 不直接持有 `TaskManager`，而是拿到一个绑定 `task_id` 的 handler，通过 `start()` / `complete()` / `create_subtask()` 等显式方法操作任务，同时承担**结构化日志**与**消息更新通知**。

### 文件结构

```
task_manager/
├── __init__.py            # 导出枚举、协议、Task、TaskEvent、TaskBelonging、TaskManager、Handler、TextSubscriber、ArtifactStore
├── base.py                # TaskStatus / TaskType / EventType / TaskSubscriber / BaseSubscriber / TaskNode
├── task.py                # Task 数据类 + generate_task_id()
├── event.py               # TaskEvent 数据类
├── belonging.py           # TaskBelonging —— 任务归属三元组（ps_id / widget_id / subagent_id）
├── manager.py             # TaskManager 单例（生命周期、订阅分发、清理、导出）
├── handler.py             # SessionTaskHandler（已废弃）/ SessionTaskHandlerV2 / WidgetTaskHandler
├── text_subscriber.py     # TextSubscriber —— 异步文件导出订阅者
├── artifact_store.py      # TaskArtifactStore / ArtifactReporter / ArtifactRef / KIND_* 常量
└── _README/               # 内部设计笔记（按日期归档）
```

---

## 核心枚举

### `TaskStatus`

| 值 | 含义 |
|----|------|
| `PENDING` | 已创建，未开始 |
| `RUNNING` | 正在执行 |
| `COMPLETED` | 成功完成 |
| `FAILED` | 失败（含 error 信息）|
| `CANCELLED` | 被取消 |

`Task.is_finished()` ↔ `status ∈ {COMPLETED, FAILED, CANCELLED}`。

### `TaskType`

| 类型 | 用途 |
|------|------|
| `USER_MESSAGE` | 顶层任务（每条用户消息或 cron 触发对应一棵树）|
| `AGENT_LOOP` | Agent 主循环 |
| `TOOL_EXECUTION` | 单次工具调用 |
| `SUBAGENT_SPAWN` / `SUBAGENT_LOOP` | 子 Agent 创建与循环 |
| `LLM_CALL` | LLM 调用（可选，多数 Agent 不细分）|
| `MEMORY_COMPRESS` / `MEMORY_RECALL` | 记忆压缩 / 召回（自动或手动 `/compress`）|
| `MEMORY_READ` / `MEMORY_WRITE` | 记忆工具调用（read_memory / write_memory）|
| `MEMORY_BUDGET` / `MEMORY_BRIEF` / `MEMORY_WRITE_REVIEW` / `MEMORY_EVICT` | SoulV6 新增：token 预算、TurnBrief 合成、写入评审、过时工具结果驱逐 |

### `EventType`

```
PS_*       : PS_CREATED / PS_OPENED / PS_CLOSED
WIDGET_*   : WIDGET_CREATED / WIDGET_STARTED / WIDGET_COMPLETED / WIDGET_FAILED / WIDGET_CANCELLED
TASK_*     : TASK_CREATED / STARTED / PROGRESS / COMPLETED / FAILED / CANCELLED / LOG
```

> **向后兼容**：旧的 `SESSION_*` 名称已替换为 `WIDGET_*`（`WIDGET_CREATED` 等），但 `EventType` 中仍保留 `SESSION_*` 作为别名（`SESSION_CREATED = WIDGET_CREATED` 等），供过渡期内旧代码使用；Cleanup 阶段会移除别名。

订阅者通过 `get_subscribed_events()` 返回**空集合**表示订阅所有事件，否则只接收声明的事件类型。

---

## 数据模型

### `Task`（`task.py`）

任务节点。必填字段：`task_id` / `session_id` / `task_type` / `name` / `status` / `created_at`。

可选字段：`started_at`、`completed_at`、`parent_id`、`children_ids`、`progress (0~1)`、`description`、`metadata`、`error`、`seq`（在同一父节点下的单调递增序号，从 0 开始）、`belongs_to: TaskBelonging`。

> **PersonalSpace 模型迁移**：字段 `session_id` 保留为兼容字段；新代码应使用 `belongs_to`（类型 `TaskBelonging`，含 `ps_id / widget_id? / subagent_id?`）。若只传 `session_id`，会自动包成 `TaskBelonging(ps_id=session_id)`，`belongs_to.key() == session_id`，调用方无感。

**树操作**：`add_child()`、`remove_child()`、`get_depth()`、`get_root()`、`is_leaf()`、`is_finished()`。

任务 ID 由 `generate_task_id(session_id_or_belonging)` 生成，接受旧式字符串或 `TaskBelonging`，格式：`{key}-{YYYYMMDD_HHMMSS}-{uuid[:4]}`。

### `TaskEvent`（`event.py`）

任务状态变化时由 `TaskManager` 构造并广播。字段：

```
event_type, session_id, task_id, timestamp, task_snapshot (Task.to_dict()), extra
belongs_to: TaskBelonging | None   # 新增；None 时自动从 session_id 包装
```

`extra` 用于携带事件特有数据，如 `{"error": ...}`、`{"log_level": ..., "log_message": ...}`、`{"message": "进度描述"}`。

### `TaskBelonging`（`belonging.py`）

任务归属三元组（`frozen dataclass`），描述任务属于哪个 PersonalSpace / Widget / SubAgent。

```python
@dataclass(frozen=True)
class TaskBelonging:
    ps_id: str             # PersonalSpace ID（必填）
    widget_id: str | None  # Widget ID（可选；None 表示 PS 级任务）
    subagent_id: str | None  # SubAgent ID（可选；须配合 widget_id 使用）
```

**主要方法：**

| 方法 | 说明 |
|------|------|
| `key() -> str` | 稳定字符串键：`"ps_id"` / `"ps_id__widget_id"` / `"ps_id__widget_id__subagent_id"`，用于 task_id 拼接和文件路径 |
| `widget_key() -> str` | 只到 widget 层的键（忽略 subagent_id），用于按 widget 聚合任务 |
| `with_subagent(subagent_id) -> TaskBelonging` | 返回挂上 subagent_id 的不可变副本 |
| `without_subagent() -> TaskBelonging` | 返回去掉 subagent_id 的不可变副本 |
| `to_dict() / from_dict(d)` | 序列化/反序列化 |
| `legacy_session_id` (property) | 等价于 `key()`，供旧代码读取 `session_id` 时使用 |

---

## `TaskArtifactStore` / `ArtifactReporter`（`artifact_store.py`）

为每个 `task_id` 持久化关联的工件（LLM 响应、工具参数/结果、文件编辑、错误堆栈等），供任务图谱仪表盘按需懒加载。

### 工件类型常量（`KIND_*`）

| 常量 | 值 | 含义 |
|------|----|------|
| `KIND_LLM_RESPONSE` | `"llm_response"` | LLM 完整响应 |
| `KIND_TOOL_ARGS` | `"tool_args"` | 工具调用入参 |
| `KIND_TOOL_RESULT` | `"tool_result"` | 工具调用结果 |
| `KIND_ERROR_TRACE` | `"error_trace"` | 错误堆栈 |
| `KIND_META` | `"meta"` | 自由元数据 |
| `KIND_FILE_EDIT` | `"file_edit"` | 文件编辑摘要 |
| `KIND_STREAM_CONTENT` | `"stream_content"` | 流式内容片段 |

### `ArtifactRef` 数据类

单个工件引用，由 `TaskArtifactStore.attach()` 返回：

```
artifact_id, task_id, kind, name, mime, size, ext, created_at, extra
```

### `TaskArtifactStore`（单例）

磁盘布局（扁平）：

```
~/pyclaego/.cache/task_artifact/
    {task_id}/
        index.json         # 工件列表（每行一个 ArtifactRef）
        {artifact_id}.json # 工件 payload（文本 / JSON / 二进制）
```

| 方法 | 说明 |
|------|------|
| `get_instance() -> TaskArtifactStore` | 获取单例 |
| `attach(task_id, kind, payload, name, mime, extra) -> ArtifactRef` | 挂载工件，同步落盘；payload 支持 str/bytes/dict/list |
| `list_for_task(task_id) -> list[dict]` | 列出某 task 下所有工件的元数据（每次读盘，支持跨进程） |
| `fetch(task_id, artifact_id) -> (bytes, mime, name) \| None` | 取出工件 payload |
| `digest(task_id) -> dict` | 按 kind 计数 + total_size，供前端徽标显示 |

配置（`logging.task_artifact_store.*`）：

```yaml
logging:
  task_artifact_store:
    artifact_ttl_hours: 48   # 工件目录过期时间（小时），0 = 不清理
```

### `ArtifactReporter`（便捷上报器）

绑定到单个 `task_id` 的薄包装，提供按 kind 命名的方法：

```python
reporter = ArtifactReporter.for_task(task_id)
reporter.tool_args("read_file", {"path": "/x"})
reporter.tool_result("read_file", "file contents...", success=True)
reporter.llm_response({...})
reporter.error_trace(traceback_str)
reporter.meta("summary", {...})
reporter.file_edit("/path/to/file.py", summary_dict)
reporter.list()    # → list[dict]（工件元数据列表）
reporter.digest()  # → dict（kind 计数）
```

### `safe_attach(reporter, fn_name, *args, **kwargs)`

安全调用 reporter 的某个方法，捕获所有异常并降级日志，**绝不打断主流程**。供不确定 reporter 是否为 None 的调用场景使用。

---

## `TaskManager`（`manager.py`）

**单例**，由 `TaskManager.get_instance()` 获取。

### 生命周期方法（全部 async）

| 方法 | 说明 |
|------|------|
| `create_task(session_id, task_type, name, parent_id=None, description="", **metadata) -> task_id` | 创建任务；校验 `parent_id` 存在与深度 ≤ `max_task_depth`（默认 10）；自动建立父子关系；触发 `TASK_CREATED` |
| `start_task(task_id)` | 状态 → `RUNNING`，记 `started_at`；触发 `TASK_STARTED` |
| `update_task_progress(task_id, progress, message="")` | 校验 `0.0 ≤ progress ≤ 1.0`；触发 `TASK_PROGRESS`（`extra={"message": ...}`）|
| `complete_task(task_id, result=None)` | 状态 → `COMPLETED`，`progress=1.0`，`metadata["result"]=result`；**递归结束未完成的子任务**；触发 `TASK_COMPLETED` |
| `fail_task(task_id, error)` | 状态 → `FAILED`，`task.error=error`；**递归结束未完成的子任务（标记为 FAILED）**；触发 `TASK_FAILED` |
| `cancel_task(task_id, recursive=True)` | 状态 → `CANCELLED`；递归取消子任务；触发 `TASK_CANCELLED` |
| `end_child_tasks(task_id, result=TaskStatus.FAILED)` | 内部使用：把所有未结束的子任务按指定终态收尾 |
| `emit_log_event(task_id, level, message)` | 不改状态，仅触发 `TASK_LOG`（`extra={"log_level", "log_message"}`）|

> **重要**：`complete_task` / `fail_task` 都会调用 `end_child_tasks`，所以**父任务收尾会自动闭合所有未完成的子任务**，子任务会沿用父任务的终态语义。
> 此外，`complete_task` 和 `fail_task` 在结束时还会调用内部的 `_compute_digest(task)` 把机械生成的简短摘要写入 `task.metadata["digest"]`（不调 LLM，O(1) 完成），供仪表盘 tooltip 使用。

### 订阅 / 发布

```python
manager.subscribe(subscriber)            # subscriber 必须实现 TaskSubscriber 协议
manager.unsubscribe(subscriber_id)
```

事件分发使用 `asyncio.gather(..., return_exceptions=True)` **并发**通知所有订阅者，单个订阅者抛异常被捕获并记日志，**不影响其它订阅者**。订阅者声明的 `subscribed_events` 用于事件过滤。

### 状态导出

```python
manager.export_session_tasks(session_id, include_completed=False) -> dict
manager.export_all_tasks() -> dict
manager.get_active_sessions() -> List[str]
manager.get_task(task_id) -> Optional[Task]
```

`export_session_tasks` 返回根任务列表，子任务通过 `children` 字段递归嵌套。

### 配置（`config.yaml` → `task_manager.*`）

```yaml
task_manager:
  max_memory_tasks_per_session: 100   # 每 Session 内存中保留的任务上限，超出按完成时间剔除最老的已完成任务
  max_task_depth: 10                  # 任务树最大深度
```

---

## 订阅者

### `TaskSubscriber` 协议（`base.py`）

```python
class TaskSubscriber(Protocol):
    async def on_event(self, event: TaskEvent) -> None: ...
    def get_subscriber_id(self) -> str: ...
    def get_subscribed_events(self) -> Set[EventType]: ...   # 空集合 = 订阅所有
```

> **v2.0 起 `on_event` 改为 `async`**。如继承 `BaseSubscriber`，子类只需实现 `async def on_event(...)`。

### `BaseSubscriber`

提供默认的 `subscriber_id` 自动生成、`subscribe_to(*events)` / `unsubscribe_from(*events)` 工具方法，子类只需重写 `on_event`。

### `TextSubscriber`（`text_subscriber.py`）

把任务树**实时导出到文本文件**，供调试和外部 Dashboard 读取。

- 异步队列消费：`on_event` 仅入队，后台 `_worker_loop()` 顺序写盘，**无 CPU 轮询**
- 多层目录组织：`output_dir/sessions/<session_id>/tasks/<task_id>...`
- 双时间戳（事件发出时间 / 文件写入时间，精度 ms）
- 缓存 `_task_cache[session_id][task_id]` 用于重建完整视图
- 由 `SessionManager.__init__()` 自动实例化，`output_dir = workspace_root.parent / .cache / task_output`

```python
sub = TextSubscriber(output_dir=Path(".cache/task_output"), auto_start=True)
TaskManager.get_instance().subscribe(sub)
```

> 还有一个外部订阅者 `src.web.task_bridge.TaskBridgeServer`，将事件通过 WebSocket 广播给前端 Dashboard，由 `SessionManager` 注册。

---

## `SessionTaskHandlerV2` / `WidgetTaskHandler`（`handler.py`）

`handler.py` 中定义了三个类，形成继承链：

```
SessionTaskHandler (V1，已废弃)
    └── SessionTaskHandlerV2
            └── WidgetTaskHandler  ← PersonalSpace 模型推荐使用
```

**`SessionTaskHandlerV2`** 绑定 `(session_id, user_id, task_id, depth)` 的薄包装，**Agent 实际操作的接口**。由 `SessionManager.route_message()` 在创建顶层 `USER_MESSAGE` 任务后实例化，传入 `Session` → 透传到 `Agent.process_v2()`。

**`WidgetTaskHandler`** 是在 PersonalSpace 模型下的推荐句柄，额外承载 `belongs_to: TaskBelonging`，使任务归属在整棵子树中保持一致。通过 `WidgetTaskHandler.from_belonging(belonging, user_id, task_id, ...)` 构造。

> `SessionTaskHandler`（V1）已废弃，`__call__()` 直接抛 `NotImplementedError`；`SessionTaskHandlerV2.__call__()` 也已废弃，抛 `RuntimeError`。新代码一律使用显式方法。

### 任务状态方法（async）

```python
await h.start()
await h.complete(result={...})
await h.fail(error="...")
await h.cancel()
await h.update_task_status(status, result=..., error=...)   # 不推荐直接用，会路由到上面四个
```

### 子任务 / 兄弟任务

```python
# 创建子任务（depth + 1）
child = await h.create_subtask(
    task_type=TaskType.AGENT_LOOP,
    name="Agent Loop #1",
    description="...",
    metadata={...},
)
await child.start()
# ... 工作 ...
await child.complete(result=...)

# 创建与当前任务共享父节点的兄弟任务（depth 不变），用于并行场景
sibling = await h.create_sibling_task(
    task_type=TaskType.MEMORY_COMPRESS,
    name="parallel compress",
)
```

深度上限：硬编码 `10`，超出抛 `ValueError`。

### 日志方法（async）

| 方法 | 行为 |
|------|------|
| `msg_update(msg)` | 走 `original_handler({"type":"message_update", ...})` + `emit_log_event(level="message")` |
| `log_info(msg)`   | running log + `original_handler({"type":"log","level":"info"})` + `emit_log_event("info")` |
| `log_warning(msg)`| 同上，level=`warning` |
| `log_error(msg)`  | 同上，level=`error` |

`original_handler` 调用失败被静默捕获（不影响主流程）；`emit_log_event` 异常也被静默吞掉。

### 子代理标识

```python
h.set_subagent_id("subagent_xyz")
h.get_subagent_id()
```

供 SpawnAgent 在子代理任务上挂额外标识。

### `WidgetTaskHandler` 专属方法

| 方法 | 说明 |
|------|------|
| `from_belonging(belonging, user_id, task_id, ...) -> WidgetTaskHandler` | 类方法，用 TaskBelonging 构造句柄（推荐入口）|
| `belongs_to` (property) | 返回 `TaskBelonging` 归属 |
| `get_belonging() -> TaskBelonging` | 同上，方法形式 |
| `derive_for_subagent(subagent_id) -> WidgetTaskHandler` | 派生挂有 subagent_id 的不可变副本（共享 task_id / user_id / handler） |

`WidgetTaskHandler.create_subtask()` / `create_sibling_task()` 均已重写，确保返回的子句柄仍是 `WidgetTaskHandler` 并继承 `belongs_to`（不继承 `subagent_id`）。

---

## 典型调用链

```
SessionManager.route_message(session_id, message, user_id, msg_update_handler)
    │
    ├─ TaskManager.create_task(USER_MESSAGE, parent=None)              ──► task_id
    ├─ wrap = SessionTaskHandlerV2(session_id, user_id, task_id, msg_update_handler)
    │
    └─ Session.process_message(message, user_id, msg_update_handler=wrap)
            │
            └─ Agent.process_v2(user_message, context_handler, session_task_handler=wrap)
                    │
                    ├─ wrap.start()                          # USER_MESSAGE → RUNNING
                    │
                    ├─ loop = await wrap.create_subtask(AGENT_LOOP, ...)
                    │   await loop.start()
                    │
                    ├─ tool = await loop.create_subtask(TOOL_EXECUTION, name="read_file")
                    │   await tool.start()
                    │   await tool.complete(result={...})
                    │
                    ├─ await loop.complete()
                    │
                    └─ ↑ Agent return → SessionManager.complete_task(task_id, result)
                            │
                            └─ TaskManager.complete_task(task_id)
                                    │
                                    ├─ end_child_tasks(...)  # 兜底闭合任何遗漏的子任务
                                    └─ _notify_subscribers(TASK_COMPLETED)
                                            │
                                            ├─► TextSubscriber._update_queue.put(...)
                                            └─► TaskBridgeServer  → WebSocket → 前端 Dashboard
```

---

## 内存管理

- 每 Session 内存中至多保留 `max_memory_tasks_per_session`（默认 100）个任务
- 超限时按 `completed_at` 升序剔除**已完成**的任务，未完成任务永不回收
- 剔除时同步从父任务的 `children` 中移除

`_remove_task(task_id)` 是内部方法，不对外暴露。

---

## 依赖关系

### 内部依赖

```python
from ..config import get_config        # 读取 task_manager.* 配置
from ..logging import get_running_log  # 任务日志
```

### 被其他模块引用

| 调用方 | 用途 |
|--------|------|
| `src/personal_space/manager.py` / `src/session/manager.py` | `route_message()` 创建顶层任务、包装 handler，注册 `TextSubscriber` 与 `TaskBridgeServer` |
| `src/session/session.py` | `_enqueue_cron()` 为 cron 任务创建独立的 `USER_MESSAGE` 顶层任务 |
| `src/agent/*.py` | 通过 `session_task_handler` / `WidgetTaskHandler` 创建子任务 / 报告进度 / 写日志 |
| `src/web/task_bridge.py` | 实现 `TaskSubscriber`，把事件序列化推送给前端 |
| `src/security_executor/handler.py` | `request_tool_call_v2()` 用 `ArtifactReporter` 记录工具参数/结果工件 |

### 设计要点

- **单例 + asyncio**：`TaskManager` 单例线程安全（`threading.Lock` 仅保护实例创建）；事件分发用 `asyncio.gather`，与主事件循环共生。
- **父任务收尾自动闭合子任务**：避免 Agent 异常退出导致子任务永远悬挂为 `RUNNING`。
- **日志事件与状态事件分离**：`TASK_LOG` 不改任务状态，只用于 UI 流式输出，避免污染状态机。
- **兄弟任务**：解决"同父并行"场景（如 Agent loop 旁挂一个并行的 `MEMORY_COMPRESS`），不需要嵌套即可保持任务树清晰。
- **TaskBelonging 三元组**：PersonalSpace 模型下，任务不再单纯归属 session_id；归属信息由 `(ps_id, widget_id?, subagent_id?)` 组成，兼容旧 `session_id` 字符串接口。
- **TaskArtifactStore 跨进程**：CoreScheduler 进程写入，Web 进程按需读取磁盘（每次直接读盘不走进程内缓存），双进程共享工件数据。
