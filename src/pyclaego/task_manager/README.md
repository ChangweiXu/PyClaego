# task_manager 模块 — Agent 任务追踪与订阅分发

## 概述

`task_manager` 是 Agent 执行过程的**任务树追踪核心**。它把一次用户请求展开成一棵层级任务树（USER_MESSAGE → AGENT_LOOP → TOOL_EXECUTION / SUBAGENT_* / MEMORY_*），通过单例 `TaskManager` 维护任务状态、生命周期和父子关系，并以**订阅/发布**模式把任务事件并发推送给所有订阅者（文本导出、WebSocket 桥接、UI 仪表板等）。

`SessionTaskHandlerV2` 是 Agent 与 TaskManager 之间的薄包装：Agent 不直接持有 `TaskManager`，而是拿到一个绑定 `task_id` 的 handler，通过 `start()` / `complete()` / `create_subtask()` 等显式方法操作任务，同时承担**结构化日志**与**消息更新通知**。

### 文件结构

```
task_manager/
├── __init__.py            # 导出枚举、协议、Task、TaskEvent、TaskManager、Handler、TextSubscriber
├── base.py                # TaskStatus / TaskType / EventType / TaskSubscriber / BaseSubscriber / TaskNode
├── task.py                # Task 数据类 + generate_task_id()
├── event.py               # TaskEvent 数据类
├── manager.py             # TaskManager 单例（生命周期、订阅分发、清理、导出）
├── handler.py             # SessionTaskHandler（已废弃）/ SessionTaskHandlerV2
├── text_subscriber.py     # TextSubscriber —— 异步文件导出订阅者
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
| `MEMORY_BUDGET` / `MEMORY_BRIEF` / `MEMORY_WRITE_REVIEW` / `MEMORY_EVICT` | SoulV6 新增：token 预算、TurnBrief 合成、写入评审、过时工具结果驱逐 |

### `EventType`

```
SESSION_*  : SESSION_CREATED / STARTED / COMPLETED / FAILED / CANCELLED
TASK_*     : TASK_CREATED / STARTED / PROGRESS / COMPLETED / FAILED / CANCELLED / LOG
```

订阅者通过 `get_subscribed_events()` 返回**空集合**表示订阅所有事件，否则只接收声明的事件类型。

---

## 数据模型

### `Task`（`task.py`）

任务节点。必填字段：`task_id` / `session_id` / `task_type` / `name` / `status` / `created_at`。

可选字段：`started_at`、`completed_at`、`parent_id`、`children_ids`、`progress (0~1)`、`description`、`metadata`、`error`。

**树操作**：`add_child()`、`remove_child()`、`get_depth()`、`get_root()`、`is_leaf()`、`is_finished()`。

任务 ID 由 `generate_task_id(session_id)` 生成，格式：`{session_id}-{YYYYMMDD_HHMMSS}-{uuid[:4]}`。

### `TaskEvent`（`event.py`）

任务状态变化时由 `TaskManager` 构造并广播。字段：

```
event_type, session_id, task_id, timestamp, task_snapshot (Task.to_dict()), extra
```

`extra` 用于携带事件特有数据，如 `{"error": ...}`、`{"log_level": ..., "log_message": ...}`、`{"message": "进度描述"}`。

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

## `SessionTaskHandlerV2`（`handler.py`）

绑定 `(session_id, user_id, task_id, depth)` 的薄包装，**Agent 实际操作的接口**。由 `SessionManager.route_message()` 在创建顶层 `USER_MESSAGE` 任务后实例化，传入 `Session` → 透传到 `Agent.process_v2()`。

> `SessionTaskHandler`（V1）已废弃，仅保留作为 V2 的基类与历史兼容；其 `__call__()` 接口直接抛 `NotImplementedError`。新代码一律使用 V2。

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
| `src/session/manager.py` | `route_message()` 创建顶层任务、包装 handler，注册 `TextSubscriber` 与 `TaskBridgeServer` |
| `src/session/session.py` | `_enqueue_cron()` 为 cron 任务创建独立的 `USER_MESSAGE` 顶层任务 |
| `src/agent/*.py` | 通过 `session_task_handler` 创建子任务 / 报告进度 / 写日志 |
| `src/web/task_bridge.py` | 实现 `TaskSubscriber`，把事件序列化推送给前端 |

### 设计要点

- **单例 + asyncio**：`TaskManager` 单例线程安全（`threading.Lock` 仅保护实例创建）；事件分发用 `asyncio.Lock` + `gather`，与主事件循环共生。
- **父任务收尾自动闭合子任务**：避免 Agent 异常退出导致子任务永远悬挂为 `RUNNING`。
- **日志事件与状态事件分离**：`TASK_LOG` 不改任务状态，只用于 UI 流式输出，避免污染状态机。
- **兄弟任务**：解决"同父并行"场景（如 Agent loop 旁挂一个并行的 `MEMORY_COMPRESS`），不需要嵌套即可保持任务树清晰。
