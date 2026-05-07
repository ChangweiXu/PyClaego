# cli 模块 — Console Script 入口

## 概述

`cli` 模块为 `pyproject.toml` 的 `[project.scripts]` 提供 console-script 入口。每个文件
封装一个独立可执行程序：解析参数、读取 `config.yaml`、构造对应的运行时对象并启动
`asyncio` 主循环。模块本身不包含业务逻辑，仅做"参数 → 配置 → 实例化 → 启动"的胶水。

### 文件结构

```
cli/
├── __init__.py       # 空（仅作为包标识）
├── core.py           # pyclaego-core    → CoreScheduler (+ 可选 web)
├── web.py            # pyclaego-web     → 独立 FastAPI / uvicorn
├── tui.py            # pyclaego-tui     → TUIClient（PersonalSpace 协议）
├── tui_ps.py         # pyclaego-tui-ps  → 最小化 PSChatTUI（直接走 ws）
└── feishu.py         # pyclaego-feishu  → FeishuGateway
```

### Console scripts（来自 `pyproject.toml`）

| 命令 | 入口 | 作用 |
|------|------|------|
| `pyclaego-core`    | `pyclaego.cli.core:main`    | 启动 `CoreScheduler`；`server.enable_web_server=true` 时同进程并发启动 Web |
| `pyclaego-web`     | `pyclaego.cli.web:main`     | 独立启动 FastAPI Web Server（`pyclaego.web.app:app`）|
| `pyclaego-tui`     | `pyclaego.cli.tui:main`     | 启动 `TUIClient`（PersonalSpace 协议） |
| `pyclaego-tui-ps`  | `pyclaego.cli.tui_ps:main`  | 启动最小化 `PSChatTUI`（仅 `open` / `chat` / `reply` / `error` / `event`）|
| `pyclaego-feishu`  | `pyclaego.cli.feishu:main`  | 启动 `FeishuGateway` 连接 CoreScheduler 与飞书 |

---

## 各入口说明

### `core.py` — `pyclaego-core`

- 读取 `server.*` 与 `personal_space.*`，构造 `CoreScheduler(host, port, root_path, max_active)`
- `server.enable_web_server=true` 时，在同一事件循环内 `asyncio.gather` 启动
  `CoreScheduler.start()` 与 `uvicorn` Web Server（共用进程，无需跨进程 Bridge）
- `KeyboardInterrupt` 时调用 `scheduler.stop()` 优雅关闭

### `web.py` — `pyclaego-web`

- `argparse` 接受 `--host` / `--port`，覆盖 `web.host` / `web.port` 配置
- 启动 `uvicorn.run(pyclaego.web.app:app, host, port)`
- 适合与 `pyclaego-core` 分进程部署的场景

### `tui.py` — `pyclaego-tui`

- 参数：`-p/--ps-id`（默认 `default`）、`-w/--widget-id`（默认 `w_chat_default`）、
  `-u/--user-id`（默认 `default_user`）、`--server`
- `--server` 缺省时从 `client.server_url` 读取（最终回退 `ws://127.0.0.1:8765`）
- 兼容旧调用：第一个位置参数会被当作 `ps_id`（旧版本是 `session_id`）
- 实际 TUI 实现在 `pyclaego.message.tui_client:TUIClient`

### `tui_ps.py` — `pyclaego-tui-ps`

最小化 TUI，**单文件自包含**（不依赖 `message.tui_client`），直接通过 `websockets`
说 PSGateway 协议：

| 出 | 入 |
|----|----|
| `{type:"open", request_id, ps_id}` | `{type:"ack", action, request_id}` |
| `{type:"chat", request_id, ps_id, widget_id, content, user_id}` | `{type:"reply"\|"error"\|"event", ...}` |

内置斜杠命令：`/quit` `/exit` `/q` 退出，`/help` 帮助，`/info` 显示连接信息；其它内容
作为 `chat` 发送。`max_size=20MB` 与 Core 一致，支持图片消息。

### `feishu.py` — `pyclaego-feishu`

- 读取 `feishu.*` / `client.*` / `server.*`
- 校验 `feishu.app_id` / `feishu.app_secret`，缺失即 `sys.exit(1)`
- 构造 `FeishuClient` + `FeishuGateway(server_url, feishu_client, feishu_config, user_id="feishu_bot")`
  并 `await gateway.start()`

---

## 依赖关系

```
cli/core.py    → core.scheduler.CoreScheduler, web.app.app, config, logging
cli/web.py     → web.app.app, config
cli/tui.py     → message.tui_client.TUIClient, config
cli/tui_ps.py  → websockets, config（无内部 message.* 依赖）
cli/feishu.py  → message.feishu_client, message.feishu_gateway, config, logging
```

所有模块都通过 `pyclaego.config.get_config()` 读取配置，通过 `pyclaego.logging.get_running_log()`
（在需要的入口）写运行日志。
