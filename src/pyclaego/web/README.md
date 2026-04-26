# web 模块 — Web 接口层

## 概述

`web` 模块提供基于 FastAPI 的 HTTP 和 WebSocket 接口层，连接浏览器客户端与 `CoreScheduler`：

- **聊天 WebUI** — 浏览器聊天界面，支持多模态输入（文本 + 图片）
- **任务管理器 WebUI** — 实时任务树监控界面
- **聊天 WebSocket** `/chat/{session_id}` — 浏览器 ↔ CoreScheduler 双向代理
- **任务 WebSocket** `/ws/tasks` — 通过 TaskBridge 推送任务树实时更新
- **任务 HTTP API** `/api/tasks/*` — RESTful 任务查询接口
- **辅助 API** `/api/sessions`、`/health`、`/api/info`

---

## 文件结构

```
web/
├── __init__.py            # 导出 app
├── app.py                 # FastAPI 应用实例、路由注册、生命周期管理
├── websocket.py           # 聊天 WebSocket 路由 /chat/{session_id}
├── task_websocket.py      # 任务 WebSocket 路由 /ws/tasks + TaskBridgeClient
├── task_bridge.py         # TaskBridgeServer（供 CoreScheduler 进程注册任务事件）
├── task_api.py            # 任务 HTTP API /api/tasks/*
├── task_subscriber.py     # 备用订阅者实现（当前主链路未接入）
└── static/
    ├── index.html         # 聊天页面
    ├── tasks.html         # 任务管理器页面 V1（/tasks_v1）
    ├── tasks2.html        # 任务详情仪表盘（/tasks，当前主用版本）
    ├── css/
    │   ├── chat.css       # 聊天界面样式
    │   └── tasks.css      # 任务管理器样式
    └── js/
        ├── chat.js        # 聊天 WebSocket 客户端（含多模态图片处理）
        ├── tasks.js       # 任务 WebSocket 客户端 V1
        └── tasks2.js      # 任务详情仪表盘客户端（对应 tasks2.html）
```

> 启动脚本 `pyclaego/web_server.py` 位于项目根目录，不在本目录下。

---

## 运行架构

### 聊天链路

```
Browser (index.html / chat.js)
  └─ ws://<web_host>:<web_port>/chat/{session_id}  (max_size=20MB)
       └─ websocket.py（session_id 校验 + 双向代理）
            └─ ws://<core_host>:<core_port>  (CoreScheduler)
```

**关键行为：**
- 连接建立后自动发送 `join_session` 到 CoreScheduler
- 双向透明转发，支持携带 `content_parts` 的多模态消息（base64 图片）
- `max_size=20MB`，支持单条图片消息

### 任务链路

```
CoreScheduler 进程                       Web Server 进程                    Browser
TaskManager → TaskBridgeServer  ══════  TaskBridgeClient → /ws/tasks  →  tasks.html
             (task_bridge.py)               (task_websocket.py)
```

- `TaskBridgeServer` 在 CoreScheduler 进程内监听，地址 `config.web.task_bridge.host:port`
- `TaskBridgeClient` 在 Web Server 启动时自动连接，跨进程接收任务事件
- 浏览器连接 `/ws/tasks` 后先收到完整任务树快照，之后持续接收增量更新

---

## FastAPI 路由

### 页面与基础接口（`app.py`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 聊天 WebUI（`static/index.html`） |
| GET | `/tasks` | 任务详情仪表盘（`static/tasks2.html`，当前主用版本） |
| GET | `/tasks_v1` | 任务管理器 V1（`static/tasks.html`）|
| GET | `/api/info` | JSON 接口列表 |
| GET | `/api/sessions` | Session 列表（workspace 目录扫描，支持 `?user_id=` 过滤） |
| GET | `/health` | 健康检查 `{"status": "healthy"}` |

### 聊天 WebSocket（`websocket.py`）

**`WS /chat/{session_id}`**

`session_id` 格式：`^[a-z_][a-z0-9_]*$`，不合法直接 `close(code=1003)`。

连接流程：

```
1. 接受 WebSocket 连接
2. 连接 CoreScheduler（max_size=20MB）
3. 发送 join_session
4. 接收 session_joined，转发给浏览器
5. 启动双向转发任务（client_to_core / core_to_client）
```

发送格式（浏览器 → CoreScheduler）：

```json
{
  "type": "user_message",
  "content": "消息文本",
  "user_id": "web_user",
  "request_id": "req_001",
  "content_parts": [
    {"type": "text", "text": "图中是什么？"},
    {"type": "image", "source_type": "base64", "media_type": "image/jpeg", "data": "..."}
  ]
}
```

> `content_parts` 仅在发送图片时携带；纯文本消息无此字段。

接收消息类型：

| `type` | 说明 |
|--------|------|
| `session_joined` | Session 加入确认 |
| `response` | Agent 最终回复 |
| `progress_update` | 执行进度推送 |
| `error` | 错误信息 |

### 任务 WebSocket（`task_websocket.py`）

**`WS /ws/tasks`**

连接后立即推送初始快照，之后持续推送增量更新：

```json
// 初始快照
{"type": "initial_snapshot", "timestamp": "...", "task_tree": {"session_a": [...], ...}}

// 增量更新
{"type": "task_update", "timestamp": "...", "event": {...}, "task_tree": {"session_a": [...]}}
```

### 任务 HTTP API（`task_api.py`）

前缀：`/api/tasks`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks/` | 所有 Session 的完整任务树 |
| GET | `/api/tasks/sessions` | 活跃 Session 列表 |
| GET | `/api/tasks/sessions/{session_id}` | 指定 Session 任务树 |
| GET | `/api/tasks/tasks/{task_id}` | 指定任务详情 |
| GET | `/api/tasks/stats` | 任务统计（状态分布） |

---

## WebUI 使用指南

### 聊天页面（`/`）

**连接 Session：**
- 留空 Session ID → 系统自动生成 `web_<timestamp>_<random>`
- 填入已有 Session ID → 连接历史 Session

**发送消息：**
- `Enter` 发送，`Shift+Enter` 换行
- 支持拖拽或粘贴图片（PNG/JPEG/GIF/WEBP 直接发送；AVIF/BMP 等自动转换为 JPEG）

**多 Session 管理：**
- 点击 `☰` 打开侧边栏，基于浏览器 LocalStorage 显示历史 Session
- 显示最后使用时间和消息数量，点击即可切换

**消息历史：**
- 每个 Session 自动保存最近 50 条消息到 LocalStorage
- 页面刷新后自动恢复最后的 Session 和历史消息

**消息类型：**

| 类型 | 样式 | 说明 |
|------|------|------|
| 用户消息 | 蓝色气泡，右对齐 | 发送内容（含图片预览） |
| Agent 回复 | 白色气泡，左对齐 | 支持 Markdown 渲染 + 代码高亮 |
| 进度更新 | 黄色背景 | Agent 执行进度 |
| 系统消息 | 灰色文本 | 连接状态等 |
| 错误消息 | 红色背景 | 服务器错误 |

**图片输入处理（`chat.js` `_handleImageFiles()`）：**
- 浏览器端自动检测格式：PNG/JPEG/GIF/WEBP 原格式通过，AVIF/BMP 等通过 Canvas 转 JPEG
- base64 编码后以 `content_parts` 字段随消息发送

**前端依赖库（CDN，无需本地安装）：**
- `marked.js` v11.1.1 — Markdown 渲染
- `highlight.js` v11.9.0 — 代码语法高亮（GitHub Dark 主题）
- `DOMPurify` v3.0.6 — XSS 防护

### 任务管理器页面（`/tasks`）

`/tasks` 现在指向 `tasks2.html`（任务详情仪表盘），使用 `tasks2.js` 客户端：
- **实时任务树**：WebSocket 推送，任务状态变化 < 100ms 更新到页面
- **Session 过滤**：下拉选择器切换 Session 或查看全部
- **状态过滤**：按 pending / running / completed / failed / cancelled 过滤
- **进度条**：运行中任务显示实时百分比
- **任务详情**：创建/开始/结束时间、错误信息、子任务嵌套

`/tasks_v1` 保留旧版任务管理器界面（`tasks.html` + `tasks.js`）。

任务状态图标：⏳ 等待中 · 🔄 运行中 · ✅ 已完成 · ❌ 失败 · 🚫 已取消

---

## 配置项（`config.yaml`）

```yaml
# CoreScheduler 地址（聊天 WebSocket 使用）
server:
  host: 127.0.0.1
  port: 18765

# Web 服务配置
web:
  host: 0.0.0.0
  port: 8000
  task_bridge:
    host: 127.0.0.1
    port: 18766   # TaskBridgeServer ↔ TaskBridgeClient 通道
```

---

## 启动方式

```bash
# 第一步：启动 CoreScheduler（必需）
cd pyclaego
python core_server.py

# 第二步：启动 Web Server
python web_server.py                          # 默认配置
python web_server.py --port 9000              # 指定端口
python web_server.py --host 127.0.0.1 --port 9000
```

启动成功输出：
```
============================================================
PyClaw-CC Web API Server
============================================================
Web API:   http://0.0.0.0:8000
WebSocket: ws://0.0.0.0:8000/chat/{session_id}
API Docs:  http://0.0.0.0:8000/docs
============================================================
```

---

## 关键实现说明

1. **`app.py`** — 使用 FastAPI `startup/shutdown` 生命周期事件管理 `TaskBridgeClient` 的启动与关闭
2. **`websocket.py`** — `max_size=20MB` 支持带 base64 图片的大消息；双向转发均为独立 `asyncio.Task`，任意一方断开则同时取消另一方
3. **`task_websocket.py`** — 维护 `Set[WebSocket]` 连接池，任务更新时广播到所有浏览器连接
4. **`task_bridge.py`** — 继承 `BaseSubscriber`，订阅 `TaskManager` 事件，通过 WebSocket 跨进程推送到 Web Server
5. **`task_subscriber.py`** — 另一套"直连 WebSocket"实现，当前主链路使用 Bridge 架构，此文件暂未接入

---

## 安全注意事项

| 项目 | 当前状态 | 生产建议 |
|------|---------|---------|
| CORS | `allow_origins=["*"]` | 限制为实际域名 |
| 聊天 WebSocket 鉴权 | 仅 session_id 格式校验 | 添加 JWT Token |
| `/api/sessions` | workspace 目录扫描 | 限制访问权限 |
| XSS 防护 | DOMPurify 前端清洗 | 已实现 |

---

## 依赖关系

### 外部依赖

| 包 | 用途 |
|----|------|
| `fastapi` | Web 框架 |
| `uvicorn[standard]` | ASGI 服务器 |
| `websockets` | 连接 CoreScheduler 的 ws 客户端 |

### 内部依赖

```python
from ..utility import validate_session_id   # session_id 格式校验
from ..config import get_config             # 读取配置
from ..logging import get_running_log       # 日志记录
```

`web` 模块不直接依赖 `session`、`agent`、`context` 等核心模块，属于独立的接口层。

### 被引用

```python
# pyclaego/web_server.py
from pyclaego.src.web import app
```
