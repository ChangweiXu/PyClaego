# `personal_space/` — PersonalSpace 运行时

PersonalSpace（PS）是用户的工作台。它取代了旧版的 `session/` 模块，通过引入 **Widget**
将"一个会话 = 一个 agent + 一个上下文"扩展为"一个工作台 = N 个独立但配置可层叠的 widget"。

## 模块结构

```
personal_space/
├── personal_space.py        # PersonalSpace 运行时
├── manager.py               # PersonalSpaceManager 单例 + LRU
├── widget.py                # Widget 运行时（agent / context / store / tools / hook）
├── models.py                # PSManifest / WidgetManifest / WidgetLayout
├── stream_state.py          # StreamStateManager — 流式消息 chunk 缓冲 + 持久化 + 重连回放
├── view_schema.py           # ViewSchema — dashboard 视图 discriminated union（Pydantic）
├── widget_classes/          # WidgetClassRegistry / WidgetClassSpec / WidgetHook 基类 + builtin
├── datastores/              # WidgetStore 协议 + SqliteStore / JsonlStore 实现
├── widget_tools/            # widget_db_query / widget_db_write / widget_emit + builder
└── cron/                    # WidgetCronScheduler / WidgetCronTrigger / render_prompt
```

## 磁盘结构

```
personal_spaces/
└── <ps_id>/
    ├── personal_space.json          # PSManifest（ps_id / title / widget_order / kind）
    ├── personal_space.config.json   # PS 级配置覆盖层（空 JSON = 不覆盖）
    └── widgets/
        └── <widget_id>/
            ├── widget.json          # WidgetManifest（widget_id / widget_class / layout / cron）
            └── widget.config.json   # Widget 级配置覆盖层
```

`PSManifest.kind` 取值：`"generic"`（默认）、`"feishu_chat"` 等。

---

## 关键类

### `PersonalSpaceManager` ([manager.py](manager.py))

进程级单例。asyncio 单循环使用，线程不安全。

| 方法 | 说明 |
|------|------|
| `instance(**kwargs)` | 返回或创建进程级单例；首次调用时用 `kwargs` 构造 |
| `reset_instance()` | 仅供测试：清掉单例 |
| `await get(ps_id, *, init_kind=None) -> PersonalSpace` | 获取（或懒加载）PS；首次访问时 bootstrap 磁盘目录 + 默认 chat widget；`init_kind` 仅对全新 PS 有效 |
| `await unload(ps_id) -> bool` | 幂等卸载 PS，返回是否实际卸载 |
| `await shutdown()` | 卸载所有 PS（进程退出钩子） |
| `await open_connection(conn_id, ps_id, *, init_kind=None) -> PersonalSpace` | 确保 PS 已加载并增加连接计数 |
| `await close_connection(conn_id, ps_id)` | 减少连接计数 |
| `list_loaded_ps_ids() -> List[str]` | 列出内存中已加载的 PS ID |
| `list_disk_ps_ids(*, exclude_kinds=None) -> List[str]` | 扫描磁盘目录，按 kind 过滤 |
| `is_loaded(ps_id) -> bool` | 检查 PS 是否在内存中 |

`ps_id` 必须匹配 `^[A-Za-z0-9_-][A-Za-z0-9._-]*$`，避免路径穿越。

**构造参数（来自 `config.yaml` 的 `personal_space.*`，可被显式参数覆盖）：**

| 参数 | 配置键 | 默认值 |
|------|--------|--------|
| `root_path` | `personal_space.root_path` | `~/pyclaego/personal_spaces` |
| `max_active` | `personal_space.max_active` | `64` |

**LRU 卸载**：加载 PS 前先调 `_evict_if_needed_locked`；超额时按 `last_activity_ts` 升序选 idle PS 卸载；仍超额时记警告日志（非 idle PS 不强制卸载）。

---

### `PersonalSpace` ([personal_space.py](personal_space.py))

| 方法 / 属性 | 说明 |
|------------|------|
| `await load()` | 加载 PSConfigManager、启动 watchfiles、注册配置变更订阅 |
| `await unload()` | 停止 watchfiles、退订配置变更、卸载所有 widget |
| `await get_widget(widget_id) -> Widget` | 懒加载 widget：读 manifest → 拿 class defaults → `resolve_widget` → `Widget.load()`；失败不缓存 |
| `get_manifest() -> PSManifest` | 返回 `personal_space.json` 解析后的 PSManifest |
| `kind -> str` | `PSManifest.kind` 的快捷访问 |
| `list_widget_ids() -> List[str]` | 列出配置中已知的所有 widget ID |
| `open_connection(conn_id)` / `close_connection(conn_id)` | 连接引用计数 |
| `active_connection_count -> int` | 当前活跃连接数 |
| `inc_in_flight()` / `dec_in_flight()` | in-flight 任务计数（由 PSGateway 管理） |
| `in_flight_tasks -> int` | 当前 in-flight 数 |
| `is_idle() -> bool` | `connections == 0 and in_flight == 0` |
| `last_activity_ts -> float` | 最近一次 `_touch()` 的 Unix 时间戳（用于 LRU） |
| `bootstrap_on_disk(ps_root, ps_id, *, kind)` *(classmethod)* | 首次创建时写入最小目录结构 + 默认 `w_chat_default` widget；幂等（已存在的文件不覆盖） |

**配置变更回调策略（`_on_config_changed`）：**

| scope | 行为 |
|-------|------|
| `("widget_config", wid)` / `("widget", wid)` | 仅弹出对应 widget 的缓存实例 |
| `("ps_config",)` / `("ps",)` | 清空所有已加载 widget 缓存（PS 级配置参与每个 widget 的 deep_merge）|

已弹出的旧实例不会主动 `unload()`，随 in-flight task 完成后 GC 回收。

---

### `Widget` ([widget.py](widget.py))

每个 widget 一个实例，拥有独立的 `workspace_dir`、`Agent`、`ContextHandler`、`WidgetStore` 和 `WidgetHook`。

**构造参数：**

```python
Widget(
    ps_id, manifest, workspace_dir, resolved_config,
    *,
    agent_factory=None,     # Callable[[Dict, str], Agent]  — 测试替换
    context_factory=None,   # Callable[[str, Path, Dict], ContextHandler]  — 测试替换
    widget_class_spec=None, # WidgetClassSpec  — 提供 hook_class
)
```

**生命周期：**

| 方法 | 说明 |
|------|------|
| `await load()` | 幂等。按顺序：`ContextHandler` → `WidgetStore`（可选，失败降级为 None）→ `widget_tools`（可选，失败降级为空）→ **ToolAgent 快照**（`ToolAgentManager.resolve_for_widget()`，可选，失败降级为空 dict）→ `Agent`（可选，失败降级无 agent）→ `WidgetHook.on_create()` |
| `await unload()` | 幂等。按顺序：`hook.on_destroy()` → 取消 in-flight task → `context_handler.close()` → `store.close()` |

**消息处理（`await process_message(...)`）：**

```python
result = await widget.process_message(
    message,                # dict，至少含 "content"；可含 "content_parts"
    source="chat",          # "chat" | "cron"
    request_id=None,
    *,
    task_handler,           # 必填，由 PSGateway / cron 传入
    user_id=None,
    stream_callback=None,   # async (StreamChunk) -> None，由调用方传入用于实时推送 chunk
)
# → {"type":"response", "ps_id", "widget_id", "request_id", "source", "content", "timestamp"[, "cancelled"]}
```

- `_processing_lock` 保证 widget 内消息串行；多 widget 之间天然并行
- **即时命令快速路径**：`CommandDispatcher.is_instant(content)` 为 True 的 `/cmd` 在锁外直接执行，不阻塞 agent
- **slash 命令拦截**：非 instant 命令在锁内先通过 `CommandDispatcher.dispatch()` 处理，返回 None 才进入 agent
- agent 抛异常被吸收，变成 `"抱歉，处理失败: ..."` 响应
- `CancelledError` 设置 `result["cancelled"] = True`
- 响应发出后异步执行 `hook.on_chat()` / `hook.on_cron(trigger_id, ...)`，hook 异常不影响响应

**其他方法与属性：**

| 方法 / 属性 | 说明 |
|------------|------|
| `cancel() -> bool` | 取消当前 in-flight agent 任务 |
| `compute_view() -> ViewSchema` | 返回 dashboard 视图 schema；有 agent 时为 `ChatLogSchema`，否则为 `KVTableSchema`（含 highlight 数据）；hook 可覆盖 |
| `await handle_command(command, args) -> dict` | 分发前端命令；内建 `stop` / `send`；hook 可用 `handle_command()` 扩展 |
| `session_id -> str` | `"{ps_id}__{widget_id}"` —— 供 Agent / Context 内部日志使用 |
| `current_request_id -> str \| None` | 当前正在处理的 `request_id`，仅 `process_message` 执行期间有效 |
| `is_loaded -> bool` | widget 是否已完成 `load()`，未 load 时 `process_message` 抛 `RuntimeError` |

---

### `StreamStateManager` ([stream_state.py](stream_state.py))

流式消息状态管理器，由 `PSGateway` 持有（与 PSGateway 同进程、同生命周期）。
每个会话轮次（`ps_id` + `widget_id` + `request_id`）对应一个内部 `_StreamSession`，以序列号（从 1 开始自增）标识每个 chunk，支持断线重连时按序列号回放。

**持久化路径：** `{data_dir}/streams/{ps_id}/{widget_id}/{request_id}.jsonl`

**并发安全：** 每个 stream 独立使用 `asyncio.Lock`。

| 方法 | 返回 | 说明 |
|------|------|------|
| `await start_stream(ps_id, widget_id, request_id)` | — | 初始化流缓冲区（幂等，重复调用清空重建）|
| `await add_chunk(ps_id, widget_id, request_id, chunk)` | `int`（seq）| 追加一个 chunk，返回其序列号；key 不存在时自动创建 |
| `await get_chunks_since(ps_id, widget_id, request_id, last_seq=0)` | `(chunks, current_seq, finished)` | 获取 `last_seq` 之后的所有 chunk；key 不在内存时尝试从持久化文件读取 |
| `await get_stream_state(ps_id, widget_id, request_id)` | `(current_seq, finished)` | 仅查询流状态，不返回 chunk 数据 |
| `await end_stream(ps_id, widget_id, request_id)` | — | 标记流结束并持久化到 JSONL（缓冲区保留，供后续 `get_chunks_since` 查询）|
| `await get_stream_history(ps_id, widget_id)` | `list[dict]` | 获取该 widget 下所有流的历史（内存中活跃流 + 磁盘已持久化流），用于前端刷新时重建流状态 |
| `await get_active_streams(ps_id)` | `list[dict]` | 获取该 PS 下所有未结束的流（`widget_id / request_id / seq / finished / started_at`），供客户端重连时发现需要恢复的流 |
| `await cleanup_old_streams(max_age_seconds=300)` | `int`（清理数量）| 清理已结束且超过 `max_age_seconds` 秒的内存缓冲区 |
| `await delete_stream(ps_id, widget_id, request_id)` | — | 从内存和磁盘彻底删除指定流 |

---

### `models.py` — 磁盘数据类

| 类 | 对应文件 | 关键字段 |
|----|---------|---------|
| `PSManifest` | `personal_space.json` | `ps_id`, `title`, `description`, `widget_order`, `kind`, `metadata` |
| `WidgetManifest` | `widget.json` | `widget_id`, `widget_class`, `title`, `layout`, `cron`, `metadata` |
| `WidgetLayout` | （内嵌于 WidgetManifest） | `x`, `y`, `w`, `h`（网格位置） |

均提供 `to_dict()` / `from_dict()` 互转。

---

### `view_schema.py` — Dashboard 视图 schema

基于 Pydantic 的 discriminated union，描述 widget 详情面板应如何渲染。前端以 `type` 字段分发到对应 React 组件。

**内容节点：**

| 类型 | 说明 |
|------|------|
| `ChatLogSchema` | 聊天消息日志（数据由前端 TanStack Query 缓存，schema 只含 `widget_id`） |
| `KVTableSchema` | 两列键值表（`[[key, value], ...]`） |
| `StatSchema` | 单指标展示（label / value / 可选 trend） |
| `MarkdownSchema` | 内联 Markdown 文本 |
| `TaskListSchema` | 任务状态列表 |
| `TreeSchema` | 通用树形结构（文件树、任务树等），支持 `on_select_command` |
| `DocumentListSchema` | 虚拟化 Markdown 文档列表（by `doc_ids`） |
| `CustomSchema` | 自定义渲染器转义口（`renderer` + `props`） |

**布局节点：**

| 类型 | 说明 |
|------|------|
| `ToolbarSchema` | 命令按钮行（`ButtonSpec` 含 `command` / `args` / `variant`） |
| `SplitSchema` | 左右或上下分割（`ratio` 控制比例） |
| `TabsSchema` | 标签页（`TabItem` 含 `label` + `content`） |
| `StackSchema` | 垂直堆叠（`children` 列表） |

**命令类型：**

- `WidgetCommand`：前端 `POST /widgets/{id}/commands` 的 payload（`command` + `args`）
- `CommandResult`：`{ok, data?, error?}`

---

### `widget_classes/` — WidgetClass 注册表

```
widget_classes/
├── registry.py    # WidgetClassRegistry 进程级单例
├── spec.py        # WidgetClassSpec 数据类（class_id / defaults / config_schema / hook_class / ...）
├── hook.py        # WidgetHook 基类（所有生命周期方法默认 no-op）
└── widgets/       # 内置 WidgetClass 目录
    ├── chat/      # chat WidgetClass（widget_class.json）
    └── notes/     # notes WidgetClass（widget_class.json + widget_class.py hook）
```

**`WidgetClassRegistry`** — 进程级单例，扫描 `widgets/` 目录加载所有 `widget_class.json`：

| 方法 | 说明 |
|------|------|
| `instance(**kwargs)` | 单例获取 |
| `load(*, force=False)` | 扫描 builtin 目录，加载 `WidgetClassSpec` |
| `get(class_id) -> WidgetClassSpec` | 按 class_id 查找规格 |
| `has(class_id) -> bool` | 是否已注册 |
| `get_defaults(class_id) -> Dict` | 返回 `spec.defaults`（用于配置 deep_merge 的 class 层） |
| `list() -> List[WidgetClassSpec]` | 列出所有已注册规格 |

**`WidgetClassSpec`** 关键字段：`class_id`, `title`, `description`, `defaults`, `config_schema`, `asset_dir`, `hook_class`, `schema_file`, `default_viewers_file`, `default_cron_file`

**`WidgetHook`** 生命周期方法（默认 no-op，子类按需覆盖）：

| 方法 | 调用时机 |
|------|---------|
| `async on_create()` | `Widget.load()` 完成后 |
| `async on_chat(message, response)` | 每条 chat 处理完后 |
| `async on_cron(trigger_id, message, response)` | 每次 cron 触发后 |
| `async on_destroy()` | `Widget.unload()` 早期 |
| `compute_highlight() -> dict` | Dashboard 卡片摘要数据 |
| `handle_command(command, args) -> dict` *(可选覆盖)* | 前端命令分发 |
| `compute_view() -> ViewSchema` *(可选覆盖)* | 详情面板视图 schema |
| `register_routes(router)` *(classmethod)* | 向 `/api/v2` 注册自定义端点（Web Server 启动时调用一次） |

---

### `datastores/` — WidgetStore 持久化层

```
datastores/
├── base.py         # WidgetStore 协议基类 + StoreEvent
├── factory.py      # create_widget_store(widget_config, workspace_dir) → WidgetStore | None
├── sqlite_store.py # SqliteStore — 结构化查询，支持 schema_file 自定义建表
└── jsonl_store.py  # JsonlStore  — 追加写日志，扫描式查询，零依赖
```

通过 `widget_config["store"]["type"]` 选择实现（`"sqlite"` / `"jsonl"`），未配置时返回 `None`。
数据目录为 `widget_dir/data/`。详细文档见 [datastores/README.md](datastores/README.md)。

---

### `widget_tools/` — Widget-aware 工具

```
widget_tools/
├── base.py             # WidgetTool 基类
├── builder.py          # build_widget_tools(widget_config, store, ps_id, widget_id) → List
├── widget_db_query.py  # WidgetDbQueryTool — 查询 WidgetStore
├── widget_db_write.py  # WidgetDbWriteTool — 写入 WidgetStore
└── widget_emit.py      # WidgetEmitTool    — 向 widget 发送消息（自己回路）
```

`build_widget_tools()` 根据配置决定创建哪些工具，通过 `Agent.inject_widget_tools()` 注入给 agent。
LLM-facing schema（`get_description()`）不暴露 `widget_id`、`ps_id` 等内部字段。
详细文档见 [widget_tools/README.md](widget_tools/README.md)。

---

### `cron/` — 定时触发调度器

```
cron/
├── scheduler.py  # WidgetCronScheduler — 进程级 APScheduler 薄包装 + 磁盘触发管理
├── trigger.py    # WidgetCronTrigger   — 单条 cron 触发条目
└── template.py   # render_prompt       — str.format 模板渲染（避免 jinja2 依赖）
```

cron **不直接调 `Widget.process_message`**，而是像普通 WS 客户端一样向 `PSGateway` 注入 chat 消息：
- `conn_id="cron:<scheduler_uuid>"`
- `request_id="cron:<trigger_id>:<run_id>"`

这样连接计数 / TaskHandler 创建 / 自动卸载路径完全复用，无新分支。
`/cron` slash 命令（list / run / pause / resume）通过 `CommandDispatcher` 的 `GLOBAL_COMMAND_REGISTRY` 路由，见 [command README](../command/README.md)。
详细文档见 [cron/README.md](cron/README.md)。

---

## 设计要点

- **Widget 不直接依赖 TaskManager 单例** — 调用方（PSGateway / cron）传入 `task_handler`，测试中可直接喂 `_FakeTaskHandler`。
- **`process_message` 返回 dict 而非字符串** — 含 `request_id` / `source` / 可选 `cancelled` 标记，方便 PSGateway 路由。
- **即时命令快速路径** — `is_instant()` 为 True 的命令在 `_processing_lock` 外执行，不阻塞正在运行的 agent 任务。
- **agent 抛异常被吸收** — 变成 `"抱歉，处理失败: ..."` 响应；只有 `not_loaded` / `bad task_handler` 这类调用方错误才会冒到外层。
- **配置热重载与 widget 缓存解耦** — 已加载 widget 不会被强制 unload；下次 `get_widget()` 按最新配置重建，旧实例随 in-flight task 完成后 GC 回收。
- **WidgetStore 可选降级** — `load()` 中 store 初始化失败时降级为 `None`，不阻塞整个 widget 加载。

## 测试

```bash
cd pyclaego
python -m pytest tests/test_personal_space.py tests/test_widget_runtime.py -q
```

更高一层的端到端测试在 `tests/test_ps_gateway.py` 与 `tests/test_ps_api.py`。
