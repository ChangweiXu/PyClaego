# core 模块 — 核心调度器

## 概述

`core` 模块包含系统核心入口：`CoreScheduler`（WebSocket 服务器薄壳）与 `PSGateway`（PS/Widget 路由网关）。

架构分层：

```
客户端 WebSocket
      │
      ▼
CoreScheduler          ← 字节流 ↔ JSON 转换，连接注册/注销，出站路由
      │ handle_inbound(conn_id, msg)
      ▼
PSGateway              ← open / close / chat / control 路由，流管理，状态广播
      │
      ▼
PersonalSpace / Widget ← 具体 Agent 处理
```

### 文件结构

```
core/
├── __init__.py      # 模块说明（仅 docstring）
├── scheduler.py     # CoreScheduler（WS server + 生命周期 + 出站路由）
└── ps_gateway.py    # PSGateway —— 协议路由 + 流状态管理
```

---

## `CoreScheduler`

```python
from src.pyclaego.core.scheduler import CoreScheduler
```

### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | `str` | `"127.0.0.1"` | WebSocket 监听地址 |
| `port` | `int` | `8765` | WebSocket 监听端口 |
| `root_path` | `str \| None` | `None` | PS 根目录（传给 `PersonalSpaceManager`） |
| `max_active` | `int \| None` | `None` | 最大并发活跃 PS 数 |

### 内部状态

```
_conns: dict[int, WebSocketServerProtocol]   ← conn_id（int）→ ws 连接
gateway: PSGateway                           ← 业务路由
ps_manager: PersonalSpaceManager             ← PS 生命周期
feishu_gateway: FeishuGateway | None         ← 进程内飞书网关
cron_scheduler: WidgetCronScheduler | None   ← cron 定时任务
llm_router_server: uvicorn.Server | None     ← 内建 LLM 转发代理
task_bridge: TaskBridgeServer                ← TaskManager → WS 桥接
```

> `conn_id` = `str(id(websocket))`，整数 ID 的字符串形式，在 `_conns` 字典中以 `int` 为键。

### `start()` 启动序列

1. 初始化 `PersonalSpaceManager`（单例，可配置 `root_path` / `max_active`）
2. 初始化 `ToolAgentManager`：加载 builtin + global 层，注册子 Agent profiles
3. 构建 `PSGateway(ps_manager, publish_fn=self._publish)`
4. 可选启动 `FeishuGateway`（读取 `feishu.enabled` 配置，仅在端口就绪后以 `asyncio.create_task` 启动）
5. 可选启动 `WidgetCronScheduler`（扫描 `widget.json` 中的 `cron[]` 定义）
6. 可选启动 LLM Router（`server.enable_llm_router=true` 时以 uvicorn 启动）
7. 注册 `TextSubscriber` 到 `TaskManager`（写盘缓存）
8. 启动 `TaskBridgeServer`（`127.0.0.1:18766`，将任务事件桥接到 WebSocket）
9. `websockets.serve(max_size=20MB)` 绑定端口后，`await asyncio.Future()` 永久挂起

### `stop()` 关闭顺序

`FeishuGateway` → LLM Router（`should_exit=True`）→ `WidgetCronScheduler` → `PSGateway.shutdown()` → `PersonalSpaceManager.shutdown()`

### `_handle_client(websocket)`

单连接生命周期：

```
连接建立
  → _conns[conn_id] = ws
  → gateway.register_connection(conn_id)
  → 循环读取 raw bytes
      → JSON 解析失败 → 发送 error/bad_json
      → 成功 → gateway.handle_inbound(conn_id, msg)
连接断开（ConnectionClosed 或异常）
  → gateway.unregister_connection(conn_id)
  → _conns.pop(conn_id)
```

### `_publish(conn_id, msg)`

出站回调，被 `PSGateway` 调用：

- `conn_id` 以 `"feishu:"` 开头 → 转发给 `FeishuGateway.publish()`
- 否则 → 从 `_conns[int(conn_id)]` 取到 ws 对象，`json.dumps` 后发送
- 连接不存在或已关闭 → 静默丢弃并记 debug 日志

---

## `PSGateway`

```python
from src.pyclaego.core.ps_gateway import PSGateway
```

**设计目标**：协议解耦——不直接依赖 `websockets`，由 `CoreScheduler` 通过 `publish_fn` 回调发送出站消息，便于纯 asyncio 单元测试。

### 构造参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `ps_manager` | `PersonalSpaceManager` | PS 生命周期管理 |
| `publish_fn` | `async (conn_id, msg) -> None` | 出站发送回调 |
| `task_manager` | `TaskManager \| None` | `None` 时懒加载单例 |

### 内部状态

```
_conn_ps: dict[str, set[str]]         ← conn_id → 已 open 的 ps_id 集合
_ps_conns: dict[str, set[str]]        ← ps_id → 订阅该 PS 的 conn_id 集合（反向索引）
_widget_inflight: dict[tuple, int]    ← (ps_id, widget_id) → 并发请求计数
_inflight: set[asyncio.Task]          ← 后台 chat task 集合（shutdown 时 cancel）
stream_state: StreamStateManager      ← 流 chunk 持久化与重连回放
```

### `handle_inbound(conn_id, msg)` 分发表

| `msg["type"]` | 处理方法 | 行为 |
|---------------|----------|------|
| `"open"` | `_handle_open` | 同步；建立 conn↔PS 订阅，回放活跃流，返回 `ack` |
| `"close"` | `_handle_close` | 同步；拆除 conn↔PS 订阅，返回 `ack` |
| `"chat"` | `_handle_chat` | 立即 `ack`，后台 task 执行 `_dispatch_chat` |
| `"control"` | `_handle_control` | 当前仅支持 `action="stop"` |
| 其他 | — | 返回 `error/unknown_type` |

未注册的 `conn_id` 会被自动注册（兼容简单客户端）。

### `_handle_open` — 连接 PS 并回放流

1. `ps_manager.open_connection(conn_id, ps_id)` 建立关联
2. 更新 `_conn_ps` / `_ps_conns` 双向索引
3. **流回放**：
   - 若消息携带 `resume_streams`（`dict[widget_id, dict[request_id, last_seq]]`）→ 增量回放（仅回放 `last_seq` 之后的 chunk）
   - 否则 → 全量回放该 PS 下所有**未结束**的活跃流（已结束的流数据由 History REST 接口提供）
4. 返回 `ack`，携带 `widget_ids`、`replayed`（可选）、`active_streams`（可选）

### `_dispatch_chat` — chat 消息后台处理

```
QueryService.has_pending(session_id)?
  ├─ 是 → try_resolve()
  │       ACCEPTED   → 直接返回（agent 循环恢复）
  │       STOPPED    → _cancel_widget_and_cleanup_stream()，返回
  │       REJECTED   → 发送 event/query.rejected，返回
  │       PASS_THROUGH → 继续正常处理
  └─ 否 → 继续
兼容检查：conn 未 open 该 PS → 自动 open_connection
取 Widget 对象（ps.get_widget）
创建 TaskManager 任务 → 生成 WidgetTaskHandler
发送 event/widget.status_changed {busy: true}
stream_state.start_stream(ps_id, widget_id, request_id)
widget.process_message(content, stream_callback=_on_stream_chunk)
  ├─ 正常完成 → 发送 reply{done:true}，end_stream，存 TaskArtifact
  ├─ 取消 → 发送 reply{cancelled:true}，delete_stream
  └─ 异常 → fail_task，delete_stream，发送 error/widget_error
发送 event/widget.status_changed {busy: false}（并发计数归零时）
```

### `_on_stream_chunk` — 流 chunk 类型

| `chunk_type` | 说明 |
|--------------|------|
| `text_delta` | 模型输出文本片段 |
| `thinking_delta` | 思考内容，用 `<details>` 折叠包裹 |
| `tool_call_start` | 工具调用开始，携带 `tool_call_name` / `tool_call_id` |
| `tool_call_end` | 工具调用结束，携带 `tool_call_id` |
| `finish` | 单轮完成，插入 `round_separator`（含轮次/模型/token 统计） |
| `fail` | 流异常终止，`done:true`，触发 `end_stream` 并设 `_stream_terminated` |

所有 chunk 均写入 `StreamStateManager`（附带自增 `seq` 号），再通过 `publish_to_ps` 广播到该 PS 的所有订阅连接。

### `_handle_control` — 停止任务

`action="stop"`：调用 `_cancel_widget_and_cleanup_stream`：

1. 从 widget 读取 `current_request_id`（cancel 前读，cancel 后该字段清空）
2. `widget.cancel()`
3. `stream_state.delete_stream(ps_id, widget_id, req_id)`
4. 广播 `event/stream_cancelled`
5. `QueryService.clear_all(session_id)`

返回 `ack{cancelled:true}`。

### `publish_to_ps(ps_id, msg)`

将消息广播到所有已订阅该 PS 的连接（通过 `_ps_conns` 反向索引）。单条发送失败静默忽略。

### `shutdown()`

`cancel` 所有 `_inflight` 中的后台 chat task，`gather` 等待完成。

---

## 客户端通信协议（v2）

### 入站消息

```json
// 打开 PS
{"type": "open", "request_id": "...", "ps_id": "alice",
 "resume_streams": {"w_chat_default": {"req-001": 12}}}  // 可选，重连增量回放

// 发送消息
{"type": "chat", "request_id": "...", "ps_id": "alice",
 "widget_id": "w_chat_default", "content": "你好",
 "user_id": "user1",
 "content_parts": [...]}  // 可选，多模态（含图片）

// 停止任务
{"type": "control", "action": "stop", "request_id": "...",
 "ps_id": "alice", "widget_id": "w_chat_default"}

// 关闭 PS 订阅
{"type": "close", "request_id": "...", "ps_id": "alice"}
```

### 出站消息

| `type` | 场景 |
|--------|------|
| `ack` | open / close / chat（即时确认）/ control/stop 完成 |
| `reply` | 流式 chunk（`done:false`）及最终完成（`done:true`） |
| `error` | 协议错误或业务异常 |
| `event` | `widget.status_changed`（`busy`）/ `stream_cancelled` / `query.rejected` |

```json
// ack（open）
{"type": "ack", "conn_id": "...", "request_id": "...", "ps_id": "alice",
 "action": "open", "widget_ids": ["w_chat_default"],
 "replayed": {"w_chat_default": {"req-001": 5}},
 "active_streams": [...], "timestamp": "..."}

// reply（流式 chunk）
{"type": "reply", "request_id": "...", "ps_id": "alice",
 "widget_id": "w_chat_default", "content": "你好！",
 "source": "chat", "done": false, "chunk_type": "text_delta",
 "seq": 3, "timestamp": "..."}

// reply（完成）
{"type": "reply", "request_id": "...", "ps_id": "alice",
 "widget_id": "w_chat_default", "content": "", "full_content": "...",
 "source": "chat", "done": true, "timestamp": "..."}

// event（widget 忙碌/空闲）
{"type": "event", "event": "widget.status_changed",
 "ps_id": "alice", "widget_id": "w_chat_default",
 "busy": true, "timestamp": "..."}
```

> **WebSocket 消息大小限制**：`websockets.serve` 已设置 `max_size=20MB`，支持携带 base64 图片的多模态消息。

---

## 运行入口

```python
# pyclaego/core_server.py（简化示意）
from src.pyclaego.config import get_config
from src.pyclaego.core.scheduler import CoreScheduler

config = get_config()
scheduler = CoreScheduler(
    host=config.get("server", {}).get("host", "127.0.0.1"),
    port=config.get("server", {}).get("port", 8765),
    root_path=config.get("pyclaego", {}).get("root_path"),
)
asyncio.run(scheduler.start())
```
