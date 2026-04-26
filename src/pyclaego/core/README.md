# core 模块 — 核心调度器

## 概述

`core` 模块包含系统的核心入口：`CoreScheduler`（中心化调度器）。它以 WebSocket 服务器的形式运行，负责管理客户端连接、Session 生命周期、消息路由，以及进度广播。

### 文件结构

```
core/
├── __init__.py      # 模块说明（仅 docstring）
└── scheduler.py     # 核心调度器实现
```

---

## 核心类：`CoreScheduler`

```python
from pyclaego.core.scheduler import CoreScheduler
```

### 职责

| 职责 | 说明 |
|------|------|
| **WebSocket 服务器** | 通过 `websockets` 库监听客户端连接 |
| **Session 管理** | 延迟初始化 `SessionManager`，按需创建或复用 Session |
| **消息路由** | 将 `user_message` 路由到对应 Session，由 Session 内的 Agent 处理 |
| **并发处理** | `user_message` 以 `asyncio.create_task` fire-and-forget 方式执行，保证 `/stop` 等控制命令可即时到达 |
| **广播** | `_broadcast_to_session()` 将响应/进度推送给 Session 的所有订阅者 |
| **权限验证** | `_check_permission()` 预留扩展点（当前版本直接返回 `True`） |

### 状态映射关系

```
client_id  ──────────────►  session_id          (client_sessions)
session_id ──────────────►  Set[client_id]       (session_subscribers)
client_id  ──────────────►  WebSocketProtocol    (client_websockets)
```

### 消息类型与处理流程

```
客户端发送                      CoreScheduler 行为
─────────────────────────────────────────────────────
join_session  ─────────────►  同步处理：创建/获取 Session，
                              建立 client_id -> session_id 映射，
                              返回 session_joined 响应

user_message  ─────────────►  fire-and-forget：
                              create_task(handle_user_message_task)
                              ↓
                              route_message(session_id, message)
                              ↓
                              Session → Agent → 工具调用
                              ↓
                              ① 尝试直接发给发送者（ConnectionClosed 仅记日志，不中断）
                              ② 无论发送者是否在线，广播给同 Session 其他订阅者

其他类型      ─────────────►  返回 error 响应
```

---

## 构造与启动

```python
import asyncio
from pyclaego.core.scheduler import CoreScheduler

scheduler = CoreScheduler(
    host="127.0.0.1",
    port=8765,
    workspace_root="./workspaces"
)

asyncio.run(scheduler.start())
```

`start()` 方法：
1. 实例化 `SessionManager`（来自 `src.session`，延迟导入）
2. 启动 `websockets.serve(handle_client, host, port, max_size=20MB)`（支持图片等大消息）
3. `await asyncio.Future()` 永久挂起，保持服务器运行

---

## 关键方法说明

### `handle_client(websocket)`

处理单个客户端连接的完整生命周期：
- 注册客户端到 `clients` 和 `client_websockets`
- 创建连接级别的 `msg_update_handler` 进度回调（广播 `progress_update`）
- 循环接收消息：
  - `join_session` → 同步处理（建立 session_id 绑定）
  - `user_message` → fire-and-forget Task（`handle_user_message_task`）
- 断开时调用 `_handle_client_disconnect()` 清理

#### `handle_user_message_task`（内嵌协程）

```
① handle_message() → 等待 Session/Agent 处理完成，得到 response
② websocket.send(response) → 直接发给发送者
   └─ ConnectionClosed → 仅记日志，继续执行广播
③ _broadcast_to_session() → 广播给同 Session 其他订阅者
   └─ 无论发送者是否在线，此步骤始终执行
```

这保证了多客户端场景下（如 TUI1 发消息后断线），TUI2 仍能收到最终响应。

### `_handle_user_message(message, client_id, msg_update_handler)`

路由消息到 Session 并透传 `request_id`：

1. 检查客户端是否已通过 `join_session` 绑定 session
2. 从 `message` 读取 `request_id`（可选，供多路复用客户端按 ID 路由响应）
3. 调用 `session_manager.route_message()`，等待 Agent 处理
4. 将 `request_id` 写入 response（若存在），原样返回给发送者

### `_broadcast_to_session(session_id, message, exclude_client_id)`

将消息广播给指定 Session 的所有订阅者（排除指定 client）。

- 通过 `session_subscribers[session_id]` 查找所有订阅者 client_id
- 通过 `client_websockets[client_id]` 找到对应 WebSocket 连接并发送

---

## 依赖关系

### 直接导入

```python
from ..logging import get_running_log
```

- **`get_running_log()`**（来自 `src.logging`）：获取 `RunningLog` 单例。模块级调用一次，赋值给 `_rlog`，用于全程记录服务器日志（连接/断开/消息/错误）。

### 延迟导入（在 `start()` 方法内）

```python
from pyclaego.session import SessionManager
```

- **`SessionManager`**（来自 `src.session`）：Session 生命周期管理器。
  - `get_or_create_session(session_id, user_id)` — 获取或创建 Session
  - `route_message(session_id, message, user_id, msg_update_handler)` — 将消息路由到 Session
  - `unsubscribe_session(session_id)` — Session 取消订阅
  - `get_stats()` — 获取 Session 统计信息

> 采用延迟导入的原因：避免循环依赖，并允许在测试场景中替换 `session_manager`。

---

## 运行入口

项目通过 `pyclaego/core_server.py` 启动调度器：先读取配置，再创建 `CoreScheduler` 并运行。

```python
# core_server.py（简化示意）
from pyclaego.config import get_config
from pyclaego.core.scheduler import CoreScheduler

config = get_config()
server_config = config.get_server_config()
session_config = config.get("session", {})

scheduler = CoreScheduler(
    host=server_config.get("host", "127.0.0.1"),
    port=server_config.get("port", 8765),
    workspace_root=session_config.get("workspace_root", "./workspaces")
)
asyncio.run(scheduler.start())
```

---

## 客户端通信协议

### 加入 Session

```json
// 发送
{"type": "join_session", "session_id": "abc123", "user_id": "user1"}

// 接收
{
  "type": "session_joined",
  "session_id": "abc123",
  "workspace_path": "/workspaces/abc123",
  "is_new": false,
  "session_info": { ... },
  "timestamp": "12:34:56"
}
```

> `is_new` 的判定逻辑是 `session_id` 是否由服务端自动生成（即请求未携带 `session_id`）。若客户端传入 `session_id`，即使该 Session 是首次创建，`is_new` 也会是 `false`。

### 发送消息

```json
// 发送（需先 join_session）
// request_id 为可选字段，多路复用客户端（如 FeishuGateway）用于将响应路由到对应请求
// content_parts 为可选字段，用于多模态消息（含图片）
{
  "type": "user_message",
  "content": "帮我列出当前目录",
  "user_id": "user1",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",   // 可选
  "content_parts": [                                        // 可选，多模态
    {"type": "text", "text": "这张图片里有什么？"},
    {"type": "image", "source_type": "base64", "data": "<base64>", "media_type": "image/jpeg"}
  ]
}

// 接收（异步，可能收到多条 progress_update 后收到 response）
{"type": "progress_update", "session_id": "abc123", "content": "...", ...}
{
  "type": "response",
  "session_id": "abc123",
  "content": "...",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",  // 透传（若请求携带）
  "timestamp": "12:34:57"
}
```

> **request_id 透传机制**：若 `user_message` 携带 `request_id`，服务端会将其原样写入 `response`。`FeishuGateway` 利用此机制在单条持久 ws 上区分多条并发消息的响应（见 `message` 模块文档）。

> **WebSocket 消息大小限制**：`websockets.serve` 和代理层的 `websockets.connect` 均已设置 `max_size=20MB`，支持携带 base64 图片的消息。
