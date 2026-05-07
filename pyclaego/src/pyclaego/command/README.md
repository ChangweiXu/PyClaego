# command 模块 — 服务端 slash 命令分发器

## 概述

`command` 模块负责把用户消息中的 `/cmd arg ...` 路由到合适的处理器：
- **context 命令**：调用当前 widget 的 `context_handler` 上对应的异步方法
- **全局命令**：调用与 context 解耦的独立异步函数（当前只有 `/cron`）
- **内建命令**：`/help`（纯字符串拼接，无副作用）

模块设计为**无状态**——`CommandDispatcher` 的所有方法均为 classmethod，添加新命令
只需在注册表里加一行。

> `/stop` 不走本模块，由客户端通过 control 帧独立发送。

### 文件结构

```
command/
├── __init__.py        # 导出 CommandDispatcher
├── dispatcher.py      # CommandDispatcher + COMMAND_REGISTRY + GLOBAL_COMMAND_REGISTRY
└── cron_handlers.py   # /cron 子命令实现（list / run / pause / resume）
```

---

## `CommandDispatcher`（`dispatcher.py`）

### `is_instant(content) -> bool`

判断是否可在 widget 的 `_processing_lock` **之外**立即执行（仅纯内存读 / 写）。
内置 `INSTANT_COMMANDS` 集合：

```python
INSTANT_COMMANDS = frozenset({
    "/memories", "/why", "/pin", "/unpin", "/export_memory",
})
```

`/help` 也视为 instant。其它命令必须在锁内执行（避免与 agent 主循环交错）。

### `await dispatch(content, context_handler, task_handler) -> Optional[str]`

主入口。

| 返回 | 含义 |
|------|------|
| `None` | `content` 不以 `/` 开头，调用方应走正常 agent 流程 |
| `str` | 命令已执行，返回响应文本（含错误信息） |

执行顺序：

1. 解析 `cmd` 与 `args`
2. `cmd == "/help"` → 调用 `_build_help(context_handler)`
3. 命中 `GLOBAL_COMMAND_REGISTRY` → `await fn(args)`
4. 命中 `COMMAND_REGISTRY` → 通过 `getattr(context_handler, method_name)` 调用
5. 否则返回未知命令提示（附带当前 context 实际可用的命令数）

任何异常都会被吞掉，转换为 `❌ 命令 {cmd} 执行失败: {exc}` 字符串返回。

---

## `COMMAND_REGISTRY` — context 命令

格式：`slash_name → (context_method_name, args_extractor_fn)`

| Slash | context 方法 | 参数提取 |
|-------|--------------|----------|
| `/compress`        | `force_compress`        | `_no_args` |
| `/rebuild_index`   | `rebuild_memory_index`  | `_no_args` |
| `/pin`             | `cmd_pin`               | `_first_arg("tool_call_id")` |
| `/unpin`           | `cmd_unpin`             | `_first_arg("tool_call_id")` |
| `/close_loop`      | `cmd_close_loop`        | `_rest_as_str("query")` |
| `/memories`        | `cmd_memories`          | `_passthrough_list("args")` |
| `/forget`          | `cmd_forget`            | `_first_arg("md_path")` |
| `/why`             | `cmd_why`               | `_rest_as_str("query")` |
| `/export_memory`   | `cmd_export_memory`     | `_first_arg("target_dir")` |

参数提取器一览：

| 函数 | 行为 |
|------|------|
| `_no_args(args)`              | `{}` |
| `_first_arg(name)(args)`      | `{name: args[0] if args else ""}` |
| `_rest_as_str(name)(args)`    | `{name: " ".join(args)}` |
| `_passthrough_list(name)(args)` | `{name: args}` |

> 当 context_handler 没有实现对应方法时，`/help` 会标记 `[不可用]`，`dispatch()` 会
> 返回 `⚠️ 命令 {cmd} 在当前 context 类型 ... 中不可用`。

---

## `GLOBAL_COMMAND_REGISTRY` — 全局命令

格式：`slash_name → async fn(args: List[str]) -> str`，与 `context_handler` 完全解耦。

| Slash | 实现 |
|-------|------|
| `/cron` | `cron_handlers.handle_cron` |

注册表通过 `_load_global_registry()` 延迟加载，避免循环导入。

---

## `/cron` 子命令（`cron_handlers.py`）

入口 `handle_cron(args)` 通过 `WidgetCronScheduler.get_instance()` 获取全局调度器
（未初始化时返回错误）。`id` 支持三种格式：短 `tid`（全局唯一时）、`ps/widget/tid`、
或 `ps__widget__tid`。

| 子命令 | 行为 |
|--------|------|
| `/cron`              | `list_all_disk_triggers()` 列出全部条目（启用/禁用、schedule、next_run、prompt 预览）|
| `/cron run <id>`     | `run_once(...)` 立即触发一次（不影响调度），返回 `run_id` |
| `/cron pause <id>`   | `pause_trigger(...)` 写入 `widget.json` 并移除 APScheduler job |
| `/cron resume <id>`  | `resume_trigger(...)` 写入 `widget.json` 并重新注册 job |

---

## 调用方约定

`Widget.process_message` 在拿到 `content` 后：

```python
if CommandDispatcher.is_instant(content):
    # 锁外立即执行，避免阻塞 widget 主循环
    reply = await CommandDispatcher.dispatch(content, ctx, task_handler)
else:
    async with self._processing_lock:
        reply = await CommandDispatcher.dispatch(content, ctx, task_handler)

if reply is not None:
    return reply  # 命令已处理，不进入 agent 流程
```

`reply is None` 时按普通用户消息送给 agent 处理。
