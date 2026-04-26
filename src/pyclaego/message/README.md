# message 模块 — 消息界面与网关

## 概述

`message` 模块提供两类消息网关，负责将外部输入（终端 TUI / 飞书 IM）与 `CoreScheduler` 打通：

- **TUI 系列**：基于 [Textual](https://textual.textualize.io/) 框架的终端交互界面
- **飞书系列**：基于飞书开放平台 API 的 IM 消息网关，支持单聊、群聊、富文本、卡片、批量发送

### 文件结构

```
message/
├── __init__.py                 # 导出 FeishuClient / FeishuEventListener / FeishuGateway
├── tui_client.py               # WebSocket TUI 客户端
├── feishu_client.py            # 飞书 REST API 封装（token 管理 + 各类型发送）
├── feishu_event_listener.py    # 飞书 WebSocket 长连接事件监听
└── feishu_gateway.py           # 飞书消息网关（整合收发 + CoreScheduler 路由）
```

---

## 一、TUI 系列

### `tui_client.py` — WebSocket TUI 客户端

通过 WebSocket 协议连接到 `CoreScheduler` 服务器，与 `core_server.py` 配合使用。

#### 启动方式

```bash
# 通过项目入口（创建新 Session）
python pyclaego/tui_client.py

# 指定已有 Session ID
python pyclaego/tui_client.py sess_abc123
```

#### 主要类

##### `PyClaegoCLI`（`textual.App` 子类）

终端界面主应用，包含：
- 上方：`RichLog`（聊天记录区，支持 Rich 格式标记）
- 下方：`Input`（消息输入框，固定在底部）
- 快捷键：`Ctrl+C` / `Ctrl+D` 退出

**启动流程**：

```
on_mount()
    │
    ├── connect_to_server()    WebSocket 连接到 ws://{host}:{port}
    ├── join_session()         发送 join_session 请求，获取/创建 Session
    └── asyncio.create_task(_message_listener())  启动后台消息监听
```

**消息发送**（`on_input_submitted`）：用户按 Enter 后直接通过 WebSocket 发出，**不阻塞等待响应**。响应由后台监听器 `_message_listener()` 异步接收并更新界面。

```python
# 发送格式
{"type": "user_message", "content": "你好", "user_id": "default_user"}
```

**接收的消息类型**：

| `type` 值 | 显示颜色 | 含义 |
|----------|---------|------|
| `response` | 黄色 `Session:` | Agent 的回复消息 |
| `error` | 红色 `错误:` | 服务器错误 |
| `broadcast` | 品红 `广播:` | 同 Session 其他客户端的广播消息 |
| 其他 | 灰色 `dim` | 未知类型，原始显示 |

##### `TUIClient`（封装类）

```python
from pyclaego.message.tui_client import TUIClient

client = TUIClient(
    server_url="ws://127.0.0.1:8765",
    session_id="sess_abc123",   # 可选，None 则创建新 Session
    user_id="default_user"
)

await client.start()   # 异步启动
client.run()           # 同步启动（阻塞）
```

---

## 二、飞书系列

飞书系列使用飞书开放平台自建应用机器人，通过 WebSocket 长连接接收消息，通过 REST API 发送消息。

### 整体架构

```
飞书用户 / 群聊
    │  im.message.receive_v1 事件（WebSocket 长连接）
    ▼
FeishuEventListener          ← 与飞书保持 WSS 连接，接收消息事件
    │  标准化为内部消息 dict（属性访问解析强类型 SDK 对象）
    ▼
FeishuGateway._on_feishu_message()
    │  ① 对原消息添加 [了解] 表情回应（即时反馈，不产生新消息）
    │  ② asyncio.create_task() 创建后台 Task
    ▼
FeishuGateway._handle_and_stream()   ← 后台 Task（不阻塞消息接收）
    │  注册 request_id → Future
    │  发送 user_message（含 request_id）
    │  await Future（超时 120s）
    ▼
CoreScheduler (WebSocket)    ← 核心调度器，管理 Session / Agent
    │  透传 request_id 到 response
    ▼
FeishuGateway._dispatch_loop()       ← 每条 session ws 的专属分发协程
    │  按 request_id 将响应路由到对应 Future
    ▼
_handle_and_stream() 收到 Future 结果
    │  调用 _reply_to_feishu()
    ▼
FeishuClient                 ← 调用飞书 REST API 发出回复
    ├── send_text()          → 单聊 / 群聊文本
    ├── send_rich_text()     → 富文本（post）
    ├── send_card()          → 交互卡片（interactive）
    ├── reply_text()         → 回复消息（线程中）
    ├── add_reaction()       → 消息表情回应
    └── batch_send()         → 批量群发（多用户 / 部门）
```

---

### `feishu_client.py` — 飞书 REST API 客户端

封装飞书 API 调用，含 `tenant_access_token` 自动刷新（有效期 2 小时，提前 5 分钟刷新）。

#### 初始化

```python
from pyclaego.message.feishu_client import FeishuClient

client = FeishuClient(app_id="cli_xxx", app_secret="xxx")
```

#### 发送文本消息

```python
# 单聊（receive_id_type = "open_id"）
await client.send_text("ou_xxx", "open_id", "你好，这是一条测试消息")

# 群聊（receive_id_type = "chat_id"）
await client.send_text("oc_xxx", "chat_id", "群内广播消息")

# 其他 ID 类型：user_id / email / union_id
await client.send_text("user@example.com", "email", "邮件地址发送")
```

#### 发送富文本（post 类型）

富文本支持多行、链接、@用户、代码块等内联元素：

```python
rows = [
    # 第一行：普通文字 + 超链接
    [
        {"tag": "text", "text": "查看详情："},
        {"tag": "a", "href": "https://feishu.cn", "text": "飞书官网"},
    ],
    # 第二行：@用户
    [
        {"tag": "text", "text": "请"},
        {"tag": "at", "user_id": "ou_xxx"},
        {"tag": "text", "text": " 处理"},
    ],
    # 第三行：代码块
    [{"tag": "code_block", "language": "python", "text": "print('hello')"}],
]
await client.send_rich_text("ou_xxx", "open_id", "任务通知", rows)
```

支持的内联元素 `tag` 类型：

| tag | 必填字段 | 说明 |
|-----|---------|------|
| `text` | `text` | 普通文字，可加 `style: ["bold"]` |
| `a` | `href`, `text` | 超链接 |
| `at` | `user_id` | @用户（`user_id` 为 open_id） |
| `img` | `image_key` | 图片（需先上传获取 key） |
| `code_block` | `language`, `text` | 代码块 |

#### 发送卡片消息（interactive）

```python
# 卡片 JSON 方式
card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "任务提醒"},
        "template": "blue",
    },
    "elements": [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**状态：** 进行中\n请及时处理"},
        }
    ],
}
await client.send_card("ou_xxx", "open_id", card)

# 卡片模板方式
await client.send_card("oc_xxx", "chat_id", {
    "type": "template",
    "data": {
        "template_id": "ctp_xxxxxx",
        "template_variable": {"status": "进行中", "assignee": "张三"},
    },
})
```

#### 批量群发

```python
# 批量发文本消息给多用户 + 一个部门
result = await client.batch_send(
    msg_type="text",
    content={"text": "系统维护通知：今晚 22:00-24:00 停服"},
    open_ids=["ou_aaa", "ou_bbb"],
    department_ids=["od-ccc"],
)
print(result["data"]["message_id"])   # bm-xxxxxx（以 bm- 开头）

# 批量发富文本
await client.batch_send(
    msg_type="post",
    content={
        "post": {
            "zh_cn": {
                "title": "公告",
                "content": [[{"tag": "text", "text": "内容"}]],
            }
        }
    },
    open_ids=["ou_aaa"],
)
```

**批量发送限制**：

| 限制项 | 值 |
|--------|---|
| 单次 ID 列表长度 | ≤ 200 |
| 每天发送上限 | 50 万条 |
| 接口类型 | 异步（有延迟），不保证实时 |
| 支持目标 | 用户（open_id/user_id/union_id）、部门 |
| 不支持 | 直接发群组 |

#### 回复消息

```python
# 回复原消息（文本）
await client.reply_text(message_id="om_xxx", text="收到，正在处理")

# 以话题形式回复（仅群聊）
await client.reply_text(message_id="om_xxx", text="已处理完毕", reply_in_thread=True)

# 回复富文本
await client.reply_rich_text(
    message_id="om_xxx",
    title="处理结果",
    content_rows=[[{"tag": "text", "text": "任务已完成"}]],
)
```

#### 消息表情回应

为消息添加 Reaction 表情（不产生新消息，仅在原消息上标注）：

```python
# 添加 [了解] 表情（默认，FeishuGateway 收到消息时使用）
await client.add_reaction(message_id="om_xxx")                     # emoji_type 默认 "DONE"

# 添加其他表情
await client.add_reaction(message_id="om_xxx", emoji_type="THUMBSUP")  # 👍 [赞]
await client.add_reaction(message_id="om_xxx", emoji_type="OnIt")      # 💪 [收到]
await client.add_reaction(message_id="om_xxx", emoji_type="Eyes")      # 👀 [看]
```

| emoji_type | 飞书表情显示 |
|------------|-------------|
| `"DONE"` | ✅ [了解]（默认）|
| `"THUMBSUP"` | 👍 [赞] |
| `"OnIt"` | 💪 [收到] |
| `"Eyes"` | 👀 [看] |

#### 下载消息资源（图片/文件）

下载消息中内嵌的图片或文件资源（FeishuGateway 在处理多模态消息时自动调用）：

```python
# 下载图片
img_bytes, media_type = await client.get_message_resource(
    message_id="om_xxx",
    file_key="img_v3_0210m_abc123",
    resource_type="image",   # 默认值
)
# img_bytes: bytes  原始图片数据
# media_type: str   如 "image/jpeg"、"image/png"
```

---

### `feishu_event_listener.py` — 飞书事件监听器

使用 `lark-oapi` SDK 在**后台线程**维持 WebSocket 长连接，将飞书事件桥接到 asyncio 事件循环。

#### 初始化

```python
from pyclaego.message.feishu_event_listener import FeishuEventListener

async def on_message(msg: dict) -> None:
    print(f"收到来自 {msg['sender_open_id']} 的消息: {msg['text']}")

listener = FeishuEventListener(
    app_id="cli_xxx",
    app_secret="xxx",
    on_message=on_message,
    encrypt_key="",             # 可选（长连接模式）
    verification_token="",      # 可选（长连接模式）
    bot_open_id="ou_bot_xxx",   # 过滤机器人自身消息
    dedupe_cache_size=1000,
)

loop = asyncio.get_running_loop()
listener.start(loop)            # 非阻塞，在后台线程维持连接
```

#### 内部消息格式（回调参数）

```python
{
    "source": "feishu",
    "event_id": "f7984f25...",       # 幂等去重 key（来自 data.header.event_id）
    "chat_type": "p2p",              # "p2p"（单聊）| "group"（群聊）
    "chat_id": "oc_xxx",             # 会话 chat_id
    "sender_open_id": "ou_xxx",      # 消息发送者 open_id
    "message_id": "om_xxx",          # 原始消息 ID，用于 reply_text
    "msg_type": "text",              # 飞书消息类型（text / image / post）
    "text": "用户输入的文本",          # 所有文本段落拼接（用于日志/纯文本场景）
    "image_keys": ["img_v3_xxx"],     # 消息中出现的图片 key 列表（按顺序）
    "ordered_parts": [               # 保留原始顺序的内容段落列表
        {"type": "text",      "text": "以下图片"},
        {"type": "image_key", "image_key": "img_v3_abc"},
        {"type": "text",      "text": "- 描述内容"},
    ],
    "raw_event": <P2ImMessageReceiveV1>,  # 原始 lark_oapi 事件对象
}
```

> **`ordered_parts` 说明**：对于富文本（`post`）消息，相邻文本行会合并为一个 `text` 段落，图片 key 单独作为 `image_key` 段落插入，保持文字和图片在原文中的相对顺序。`FeishuGateway` 依赖此字段下载图片并将最终的 `content_parts`（含 base64 图片数据）按正确顺序发送给 CoreScheduler。

> **注意**：`raw_event` 不再是 `dict`，而是 lark_oapi SDK 的强类型对象（`P2ImMessageReceiveV1`）。若需访问原始字段，请使用 `getattr()` 属性访问而非 `.get()`。

#### 关键特性

- **多模态支持**：自动解析 `image`（独立图片消息）和 `post`（富文本中嵌入的 `img` 标签）中的图片 key，通过 `image_keys` 和 `ordered_parts` 传递给 `FeishuGateway`
- **有序段落**：`ordered_parts` 保留文字和图片在原文中的相对顺序，`FeishuGateway` 依此构建正确交错的 `content_parts`
- **幂等去重**：使用 LRU 集合（`_LRUSet`）按 `event_id` 去重，防止飞书重发导致重复触发
- **自循环过滤**：设置 `bot_open_id` 后自动忽略机器人自身发送的消息
- **异步桥接**：SDK 在同步线程中触发事件，通过 `asyncio.run_coroutine_threadsafe` 安全桥接到调用方的 asyncio 循环
- **线程隔离**：后台线程拥有独立的 asyncio event loop（`asyncio.new_event_loop()`），并通过替换 `lark_oapi.ws.client.loop` 模块变量避免与主线程 loop 冲突（修复 `RuntimeWarning: coroutine '_connect' was never awaited`）
- **强类型解析**：事件解析直接对 SDK 对象做 `getattr()` 属性访问，不依赖 `.__dict__` 转换（修复 `'EventSender' object has no attribute 'get'`）
- **线程安全**：后台线程设为 `daemon=True`，主进程退出时自动清理

---

### `feishu_gateway.py` — 飞书消息网关

整合 `FeishuEventListener`（收消息）+ `CoreScheduler` WebSocket（路由）+ `FeishuClient`（发消息）。

#### Session 隔离与连接策略

| 消息类型 | Session Key | 示例 |
|---------|-------------|------|
| 单聊（p2p） | `feishu_p2p_{sender_open_id}` | `feishu_p2p_ou_abc` |
| 群聊（group） | `feishu_group_{chat_id}` | `feishu_group_oc_xyz` |

每个 Session Key 对应**一条持久 ws 连接**（`_sessions` 字典缓存）。同一用户 / 群的并发消息共享该连接，通过 `request_id` 区分响应归属。

**连接生命周期**：
- 首条消息到达时建立 ws，完成 `join_session` 握手
- ws 断开时由 `_dispatch_loop` 自动清理 `_sessions`，下次消息触发重建
- `stop()` 时关闭所有持久 ws

#### 初始化与启动

```python
from pyclaego.message.feishu_gateway import FeishuGateway
from pyclaego.message.feishu_client import FeishuClient

feishu_client = FeishuClient(app_id="cli_xxx", app_secret="xxx")
gateway = FeishuGateway(
    server_url="ws://127.0.0.1:8765",
    feishu_client=feishu_client,
    feishu_config={
        "app_id": "cli_xxx",
        "app_secret": "xxx",
        "bot_user_id": "ou_bot",
        "dedupe_cache_size": 1000,
        "default_reply_type": "text",   # "text" | "rich_text"
        "response_timeout": 120.0,      # 等待 CoreScheduler 响应的超时时间（秒）
    },
    user_id="feishu_bot",
)

await gateway.start()   # 阻塞，直到收到中断信号
```

#### 消息处理流程（request_id 多路复用）

收到飞书消息后，`_on_feishu_message` 的完整处理流程：

```
① 收到飞书消息
   │
   ├─→ 对原消息添加 [了解] 表情回应（add_reaction，emoji_type="DONE"）
   │       └─ 用户立刻看到 ✅ 回应（即时反馈，不产生新消息）
   │
   └─→ asyncio.create_task(_handle_and_stream(msg, text, sender))
           │  （后台 Task，不阻塞事件接收循环）
           │
           ├─→ _get_or_create_session_ws(session_key)
           │       └─ 获取 / 建立该 session 的持久 ws（首次建立时启动 _dispatch_loop）
           │
           ├─→ 生成 request_id（uuid4），注册 asyncio.Future 到 _pending_requests
           │
           ├─→ ws.send({
           │       "type": "user_message",
           │       "content": text,
           │       "user_id": sender,
           │       "request_id": request_id   ← 唯一请求标识
           │   })
           │
           └─→ await asyncio.wait_for(asyncio.shield(fut), timeout=120s)
                   │  等待 _dispatch_loop 将对应响应路由到此 Future
                   │
                   └─→ 收到结果 → _reply_to_feishu(msg, content) → 发飞书消息

_dispatch_loop（每条 session ws 的专属后台协程）：
    async for raw in ws:
        request_id = resp["request_id"]
        fut = _pending_requests[request_id]
        fut.set_result(resp)    ← 唤醒对应 _handle_and_stream
```

**多路复用效果**：同一用户在第一条消息处理期间发来第二条消息，两者共享同一 ws，通过各自的 `request_id` 独立路由，互不干扰。

#### 关键内部组件

| 组件 | 类型 | 说明 |
|------|------|------|
| `_sessions` | `Dict[str, ws]` | session_key → 持久 ws 连接 |
| `_session_locks` | `Dict[str, Lock]` | 防止并发重复建连 |
| `_dispatch_loops` | `Dict[str, Task]` | session_key → 分发协程 Task |
| `_pending_requests` | `Dict[str, Future]` | request_id → 等待结果的 Future |
| `_pending_lock` | `asyncio.Lock` | 保护 `_pending_requests` 的并发读写 |

#### `_dispatch_loop` 断连处理

当 ws 与 CoreScheduler 的连接断开时：
1. 从 `_sessions` / `_dispatch_loops` 清除该 session 记录
2. 对所有尚未完成的 `_pending_requests` Future 设置 `RuntimeError`
3. `_handle_and_stream` 捕获异常，向飞书回复 `[系统错误] ws 连接断开` 提示
4. 下次飞书消息到来时，`_get_or_create_session_ws` 自动重建连接

> **注意**：当前不含心跳/主动重连机制，断连检测依赖 `_dispatch_loop` 中 `async for raw in ws` 的异常触发。空闲期间若 CoreScheduler 静默关闭连接，需等下次消息发送时才能检测到并触发重建。

#### 主动发送 API

`FeishuGateway` 透传 `FeishuClient` 的全部发送方法，可在业务层直接调用：

```python
# 单聊
await gateway.send_text("ou_xxx", "open_id", "主动推送消息")

# 群聊
await gateway.send_text("oc_xxx", "chat_id", "群内公告")

# 富文本
await gateway.send_rich_text("ou_xxx", "open_id", "标题", rows)

# 交互卡片
await gateway.send_card("ou_xxx", "open_id", card_dict)

# 批量群发
await gateway.batch_send(
    msg_type="text",
    content={"text": "广播消息"},
    open_ids=["ou_aaa", "ou_bbb"],
)
```

#### 进度更新推送

处理过程中，CoreScheduler 可发送 `progress_update` 类型消息（无 `request_id`）；`_dispatch_loop` 识别后调用 `_on_progress_update`，根据 session_key 解析接收方并主动发消息到飞书：

- **单聊**（`feishu_p2p_*`）：使用 `sender_open_id`，`receive_id_type="open_id"` 发送
- **群聊**（`feishu_group_*`）：使用 `chat_id`，`receive_id_type="chat_id"` 发送

---

## 三、飞书网关入口脚本

项目根目录提供独立启动脚本 `pyclaego/feishu_gateway.py`（类比 `tui_client.py`）：

```bash
# 第一步：启动 Core 服务器
python pyclaego/core_server.py

# 第二步：启动飞书网关
python pyclaego/feishu_gateway.py
```

---

## 四、配置项（`config.yaml`）

```yaml
# 飞书集成配置
feishu:
  app_id: ${FEISHU_APP_ID:}               # 飞书应用 App ID（必填）
  app_secret: ${FEISHU_APP_SECRET:}       # 飞书应用 App Secret（必填）
  encrypt_key: ${FEISHU_ENCRYPT_KEY:}     # 事件加密 Key（长连接模式可留空）
  verification_token: ${FEISHU_VERIFICATION_TOKEN:}  # 验证 Token（长连接模式可留空）
  bot_user_id: ${FEISHU_BOT_OPEN_ID:}     # 机器人 open_id，用于过滤自身消息
  dedupe_cache_size: 1000                 # 幂等去重 LRU 缓存大小
  default_reply_type: "text"             # 回复类型: "text" | "rich_text"
  response_timeout: 120.0                # 等待 CoreScheduler 响应超时时间（秒）
```

---

## 五、与 `CoreScheduler` 的通信协议

飞书系列与 TUI 系列使用相同的 WebSocket 协议，在此基础上 FeishuGateway 额外使用 `request_id` 实现多路复用。

### 加入 Session

```json
// 发送
{"type": "join_session", "session_id": "feishu_p2p_ou_xxx", "user_id": "feishu_bot"}

// 接收（成功）
{
  "type": "session_joined",
  "session_id": "feishu_p2p_ou_xxx",
  "workspace_path": "/workspaces/feishu_p2p_ou_xxx",
  "is_new": true,
  "timestamp": "12:34:56"
}
```

### 发送 / 接收用户消息

```json
// 发送（FeishuGateway 携带 request_id；多模态消息额外携带 content_parts）
{
  "type": "user_message",
  "content": "用户输入的文本（纯文本摘要，可为空）",
  "user_id": "ou_xxx",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "content_parts": [
    {"type": "text",  "text": "以下图片"},
    {"type": "image", "source_type": "base64", "media_type": "image/jpeg", "data": "..."},
    {"type": "text",  "text": "描述内容"}
  ]
}

// 接收（CoreScheduler 透传 request_id，_dispatch_loop 按此路由到对应 Future）
{
  "type": "response",
  "session_id": "feishu_p2p_ou_xxx",
  "content": "Agent 的完整回复",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

> **与旧版流式协议的区别**：旧版使用 `done=true/false` 多帧流式输出；当前 FeishuGateway 等待最终单次 response，通过 `request_id` 路由，无需流式分帧处理。

---

## 六、依赖关系

### 外部依赖

| 包 | 版本 | 用途 |
|----|------|------|
| `textual` | `==0.47.0` | TUI 框架（TUI 系列） |
| `websockets` | `==12.0` | WebSocket 客户端（TUI 系列 + 飞书 Gateway） |
| `aiohttp` | `==3.9.1` | 异步 HTTP（飞书 REST API 调用） |
| `lark-oapi` | `>=1.3.0` | 飞书官方 Python SDK（WebSocket 长连接 + 事件封装） |

### 内部依赖

```python
# feishu_client.py / feishu_event_listener.py / feishu_gateway.py
from ..logging import get_running_log   # 日志记录

# feishu_gateway.py 还依赖
import websockets   # 连接 CoreScheduler
import uuid         # 生成 request_id
```

飞书系列模块不依赖 `src` 内的 `agent` / `context` / `session` 等模块，属于独立的网关层。

### 被其他模块引用

```python
# pyclaego/feishu_gateway.py（入口脚本）
from pyclaego.message.feishu_client import FeishuClient
from pyclaego.message.feishu_gateway import FeishuGateway

# pyclaego/tui_client.py（入口脚本）
from pyclaego.message.tui_client import TUIClient
```

---

## 七、前置条件（飞书系列）

1. 在[飞书开放平台](https://open.feishu.cn/app)创建**自建应用**，开启**机器人能力**
2. 在权限管理中申请以下权限：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message:send_as_bot`（以应用身份发消息）
   - `im:message.group_at_msg`（可选，@用户）
   - `im:batch_message`（可选，批量发送）
3. 将机器人发布上线，并确保目标用户 / 群组在机器人可用范围内
4. 发布应用版本，事件订阅选择**长连接方式**（无需公网 IP）
5. 若需使用表情回应（`add_reaction`），还需申请 `im:message.reaction` 权限
